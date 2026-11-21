"""marktux -- the lightweight PyQt6 Markdown notebook application.

This is the end-to-end integration of the pieces recommended in the design:

* ``marktux.core`` (Python port of VNote's ``vxcore``): a Qt-free backend with
  bundled/raw notebooks, a per-notebook SQLite cache and background tasks.
* ``marktux.ui``: a thin Qt adapter (signals + lazy tree model) over the core.
* ``mtextedit`` (reusable editor): async renderer, single-pane in-place preview,
  AST outline, find/replace.

Run:
    python main.py [notebook_directory]
    QT_QPA_PLATFORM=offscreen python main.py   # headless smoke test

The editor component lives under ``libs/mtextedit`` so it can be reused in other
PyQt6 projects (e.g. the e-Sword-like fourth panel).
"""

from __future__ import annotations

import os
import posixpath
import sys
import time
from pathlib import Path

_LOCAL_EDITOR = Path(__file__).resolve().parents[2] / "libs" / "mtextedit" / "src"
if _LOCAL_EDITOR.is_dir() and str(_LOCAL_EDITOR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_EDITOR))

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QSettings, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mtextedit import (
    MTextEdit,
    OutlineView,
    RenderManager,
    FindReplaceWidget,
)
from .core import (
    ContentQuery,
    CoreContext,
    CoreError,
    NodeRef,
    SearchScope,
    paths,
)
from .ui import CoreBridge, NotebookNodeModel
from mtextedit.linguistic import (
    create_service,
    show_download_manager,
    toolkit_available,
    toolkit_error,
)

# ``Open Markdown File…`` picker filter. The editor renders any UTF-8 text, so a
# plain "All files" entry stays available next to the Markdown extensions.
MARKDOWN_FILE_FILTER = "Markdown (*.md *.markdown *.mdown *.mkd);;All files (*)"


class MainWindow(QMainWindow):
    def __init__(self, parent=None, ctx: CoreContext | None = None,
                 settings: QSettings | None = None):
        super().__init__(parent)
        self.setWindowTitle("MarkTux")
        self.setAccessibleName("MarkTux")
        self.setAccessibleDescription("Markdown notebook editor")
        self.resize(1200, 800)

        # Core backend + Qt adapter. Core owns config under XDG dirs; the old
        # ~/.cache/marktux/meta.db is never read. Tests inject an already-open
        # context so they never touch the user's real config or session.
        self.ctx = ctx if ctx is not None else CoreContext().open()
        # Tests inject a context and must not inherit the user's last file;
        # the desktop entry point gets the real settings store.
        self._settings = settings
        if self._settings is None and ctx is None:
            self._settings = QSettings("marktux", "marktux")
        self.bridge = CoreBridge(self.ctx, self)
        self.notebook_id: str | None = None
        self.notebook_root: str | None = None
        self._current_buffer = None
        self._loading = False
        self._tag_nodes: list = []
        self._tag_counts: dict[str, int] = {}
        self._search_hits: list[tuple[str, str]] = []
        self._search_query_id = 0
        # Set while a picked Markdown file waits for its folder to open as a
        # raw notebook; consumed by ``_on_notebook_opened``.
        self._pending_markdown: str | None = None
        self._service = None
        self._dict_dialog = None
        self._spell_decorator = None

        self.manager = RenderManager(self)

        # Center: a single editor pane with VNote-style in-place preview.
        self.editor = MTextEdit(self)
        self.editor.set_render_manager(self.manager)
        self.editor.set_image_paster(self._paste_image)
        self.editor.set_image_dialog_enabled(True)
        self.find_replace = FindReplaceWidget(self.editor)
        self.find_replace.hide()

        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self.find_replace)
        cl.addWidget(self.editor)
        self.setCentralWidget(center)

        # Left dock: notebook tree + search.
        left = QWidget()
        ll = QVBoxLayout(left)
        self.tree = QTreeView()
        self.tree_model = NotebookNodeModel(self.ctx, self)
        self.tree.setModel(self.tree_model)
        self.tree.doubleClicked.connect(self._on_tree_activated)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_tree_menu)
        ll.addWidget(self.tree)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search notes…")
        self.search_edit.returnPressed.connect(self._run_search)
        ll.addWidget(self.search_edit)
        self.results = QListWidget()
        self.results.itemDoubleClicked.connect(self._on_result_activated)
        ll.addWidget(self.results)
        left_dock = QDockWidget("Notebook", self)
        left_dock.setWidget(left)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, left_dock)

        # Left dock (tabbed): tag forest with per-tag file counts.
        tag_panel = QWidget()
        tl = QVBoxLayout(tag_panel)
        tl.setContentsMargins(0, 0, 0, 0)
        self.tag_tree = QTreeWidget()
        self.tag_tree.setHeaderLabels(["Tag", "Files"])
        self.tag_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tag_tree.customContextMenuRequested.connect(self._show_tag_menu)
        self.tag_tree.itemDoubleClicked.connect(self._on_tag_activated)
        tl.addWidget(self.tag_tree)
        tag_dock = QDockWidget("Tags", self)
        tag_dock.setWidget(tag_panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, tag_dock)
        self.tabifyDockWidget(left_dock, tag_dock)

        # Right dock: outline.
        self.outline = OutlineView(self)
        right_dock = QDockWidget("Outline", self)
        right_dock.setWidget(self.outline)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, right_dock)

        # Wiring.
        self.editor.renderRequested.connect(self.manager.request)
        self.manager.headingsAvailable.connect(self.outline.set_headings)
        self.manager.previewImagesAvailable.connect(self.editor.set_preview_images)
        self.manager.tokensAvailable.connect(self.editor.set_preview_tokens)
        self.outline.headingActivated.connect(self.editor.goToLine)
        self.editor.textChanged.connect(self._on_editor_text_changed)
        self.bridge.notebookOpened.connect(self._on_notebook_opened)
        self.bridge.bufferStateChanged.connect(self._on_buffer_state)
        self.bridge.nodeChanged.connect(self._on_node_changed)
        self.bridge.tagsLoaded.connect(self._on_tags_loaded)
        self.bridge.tagCountsLoaded.connect(self._on_tag_counts)
        self.bridge.tagFilesLoaded.connect(self._on_tag_files)
        self.bridge.searchBatch.connect(self._on_search_batch)
        self.bridge.searchFinished.connect(self._on_search_finished)
        self.bridge.backendFallback.connect(self._on_backend_fallback)
        self.bridge.error.connect(self._on_core_error)

        self._build_menu()

        # Session restore is owned by core; notebooks recorded in vxsession.json
        # are re-opened in NotebookManager. Show whatever came back.
        restored = self.ctx.notebooks.list()
        if restored:
            self.tree_model.set_notebooks(restored)
            self.notebook_id = restored[0].id
            self.notebook_root = restored[0].root
            self.bridge.list_tags(self.notebook_id)
            self.bridge.tag_counts(self.notebook_id)
            self._restore_open_buffer()
        elif len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
            self.open_notebook(sys.argv[1])

    def _restore_open_buffer(self):
        """Reopen the current workspace's buffer (or the first restored one)."""
        target = ""
        current = self.ctx.workspaces.current()
        if current:
            try:
                workspace = self.ctx.workspaces.get(current)
                target = (workspace.current_buffer_id
                          or (workspace.buffer_ids[0]
                              if workspace.buffer_ids else ""))
            except CoreError:
                target = ""
        try:
            if target:
                info = self.ctx.buffers.get(target).info()
            else:
                open_buffers = self.ctx.buffers.list()
                if not open_buffers:
                    return
                info = open_buffers[0]
            self._open_file(info.notebook_id, info.file_path)
        except CoreError:
            return

    # -- menu ---------------------------------------------------------------
    def _build_menu(self):
        f = self.menuBar().addMenu("File")
        f.addAction("Open Notebook…").triggered.connect(self._pick_notebook)
        open_md = f.addAction("Open Markdown File…")
        open_md.setShortcut("Ctrl+O")
        open_md.triggered.connect(self._pick_markdown_file)
        f.addSeparator()
        f.addAction("New Note").triggered.connect(self._new_note)
        f.addAction("New Folder").triggered.connect(self._new_folder)
        f.addSeparator()
        save_act = f.addAction("Save")
        save_act.setShortcut("Ctrl+S")
        save_act.triggered.connect(self._save_current)
        f.addAction("Quit").triggered.connect(self.close)
        e = self.menuBar().addMenu("Edit")
        fa = e.addAction("Find / Replace")
        fa.setShortcut("Ctrl+F")
        fa.triggered.connect(self._toggle_find)

        tools = self.menuBar().addMenu("Tools")
        tools.addAction("Dictionary Download Manager…").triggered.connect(
            self._open_dict_manager
        )
        self.spell_act = tools.addAction("Spell Check")
        self.spell_act.setCheckable(True)
        self.spell_act.triggered.connect(self._toggle_spellcheck)

        view = self.menuBar().addMenu("View")
        inplace_act = view.addAction("In-place Preview")
        inplace_act.setCheckable(True)
        inplace_act.setChecked(True)
        inplace_act.triggered.connect(self.editor.set_inplace_preview_enabled)

    def _toggle_find(self):
        self.find_replace.setVisible(not self.find_replace.isVisible())
        if self.find_replace.isVisible():
            self.find_replace.find_edit.setFocus()

    # -- core signal handlers ------------------------------------------------
    def _on_core_error(self, op: str, message: str):
        if op == "open_notebook":
            # The notebook this file was waiting for never opened.
            self._pending_markdown = None
        self.statusBar().showMessage(f"{op}: {message}")
        QMessageBox.warning(self, op.replace("_", " ").title(), message)

    def _on_notebook_opened(self, info):
        self._adopt_notebook(info)
        self.statusBar().showMessage(f"Opened {info.name} ({info.type})")
        self._open_pending_markdown(info)

    def _adopt_notebook(self, info):
        """Make ``info`` the notebook the tree and tag dock follow."""
        self.tree_model.add_notebook(info)  # no-op when already in the model
        self.notebook_id = info.id
        self.notebook_root = info.root
        self.bridge.list_tags(info.id)
        self.bridge.tag_counts(info.id)

    # -- tags ---------------------------------------------------------------
    def _on_tags_loaded(self, notebook_id: str, tags):
        if notebook_id != self.notebook_id:
            return
        self._tag_nodes = list(tags)
        self._rebuild_tag_tree()

    def _on_tag_counts(self, notebook_id: str, counts):
        if notebook_id != self.notebook_id:
            return
        self._tag_counts = dict(counts)
        self._rebuild_tag_tree()

    def _rebuild_tag_tree(self):
        """Lay the flat tag list out as a forest under each parent."""
        self.tag_tree.clear()
        counts = self._tag_counts
        children: dict[str, list] = {}
        names = {tag.name for tag in self._tag_nodes}
        for tag in self._tag_nodes:
            parent = tag.parent if tag.parent in names else ""
            children.setdefault(parent, []).append(tag)

        def fill(parent_item, parent_name: str):
            for tag in sorted(children.get(parent_name, []),
                              key=lambda item: item.name.casefold()):
                row = QTreeWidgetItem([tag.name, str(counts.get(tag.name, 0))])
                row.setData(0, Qt.ItemDataRole.UserRole, tag.name)
                if parent_item is None:
                    self.tag_tree.addTopLevelItem(row)
                else:
                    parent_item.addChild(row)
                fill(row, tag.name)

        fill(None, "")
        self.tag_tree.expandAll()

    def _selected_tag(self) -> str | None:
        item = self.tag_tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None

    def _show_tag_menu(self, pos):
        if not self.notebook_id:
            return
        item = self.tag_tree.itemAt(pos)
        menu = QMenu(self)
        menu.addAction("New Tag…").triggered.connect(self._new_tag)
        tag = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if tag:
            menu.addAction("New Child Tag…").triggered.connect(
                lambda: self._new_tag(tag))
            menu.addSeparator()
            menu.addAction("Find Notes").triggered.connect(
                lambda: self._find_by_tag(tag))
            menu.addAction("Delete").triggered.connect(
                lambda: self._delete_tag(tag))
        menu.exec(self.tag_tree.viewport().mapToGlobal(pos))

    def _new_tag(self, parent: str = ""):
        if not self.notebook_id:
            return
        name, ok = QInputDialog.getText(
            self, "New Tag", "Tag name:", text="new tag")
        if ok and name.strip():
            self.bridge.create_tag(self.notebook_id, name.strip(), parent)

    def _delete_tag(self, tag: str):
        if not self.notebook_id:
            return
        answer = QMessageBox.question(
            self, "Delete Tag",
            f"Delete tag '{tag}' and its children? (Notes are kept.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.bridge.delete_tag(self.notebook_id, tag)

    def _on_tag_activated(self, item, _column=0):
        tag = item.data(0, Qt.ItemDataRole.UserRole)
        if tag:
            self._find_by_tag(tag)

    def _find_by_tag(self, tag: str):
        if self.notebook_id:
            self.bridge.find_tag_files(self.notebook_id, [tag], "OR")

    def _on_tag_files(self, notebook_id: str, tags, results):
        if notebook_id != self.notebook_id:
            return
        self.results.clear()
        for result in results:
            item = QListWidgetItem(result.file_name)
            item.setData(Qt.ItemDataRole.UserRole, result.file_path)
            item.setToolTip(result.file_path)
            self.results.addItem(item)
        self.statusBar().showMessage(
            f"{len(results)} note(s) tagged {', '.join(tags)}")

    def _on_buffer_state(self, buffer_id: str, state: str):
        if self._current_buffer is not None and buffer_id == self._current_buffer.id:
            self.statusBar().showMessage(f"Note changed on disk ({state})")

    # -- notebook -----------------------------------------------------------
    def _pick_notebook(self):
        path = QFileDialog.getExistingDirectory(self, "Open Notebook")
        if path:
            self.open_notebook(path)

    def open_notebook(self, root: str):
        self.notebook_root = root
        self.statusBar().showMessage(f"Opening {root}…")
        self.bridge.open_notebook(root)

    # -- open a standalone Markdown file ------------------------------------
    def _pick_markdown_file(self):
        start = self.notebook_root or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Markdown File", start, MARKDOWN_FILE_FILTER)
        if path:
            self.open_markdown_file(path)

    def open_markdown_file(self, path: str):
        """Load ``path`` into the editor, notebook-aware.

        A file already inside an open notebook opens through that notebook, so
        its bundled metadata, tags and ``/``-separated relative path apply.
        Otherwise the file's folder is opened as a *raw* notebook: the open then
        goes through core's own code path (path-derived ids, derived cache,
        atomic save) instead of editing the file behind the backend's back.
        """
        target = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
        if not os.path.isfile(target):
            self.statusBar().showMessage(f"Not a file: {path}")
            return
        self._remember_last_file(target)
        resolved = self.ctx.notebooks.resolve_path(target)
        if resolved is not None:
            notebook, rel = resolved
            self._adopt_notebook(notebook.info())
            self._open_file(notebook.id, rel)
            return
        # ``NotebookManager.open`` is idempotent per root and runs on the task
        # pool; the file is loaded once ``notebookOpened`` reports it open.
        self._pending_markdown = target
        self.open_notebook(os.path.dirname(target))

    def _remember_last_file(self, path: str) -> None:
        if self._settings is None:
            return
        self._settings.setValue("last_markdown_file", path)
        self._settings.sync()

    def restore_last_file(self) -> bool:
        """Reopen the last Markdown file from settings, if it still exists."""
        if self._settings is None:
            return False
        path = self._settings.value("last_markdown_file", "")
        if not isinstance(path, str) or not path:
            return False
        if not os.path.isfile(path):
            self._settings.remove("last_markdown_file")
            self._settings.sync()
            return False
        self.open_markdown_file(path)
        return True

    def _open_pending_markdown(self, info):
        """Consume a file that was waiting for its folder to open."""
        target, self._pending_markdown = self._pending_markdown, None
        if not target:
            return
        if not paths.is_inside(info.root, target):
            self.statusBar().showMessage(
                f"{os.path.basename(target)} is outside {info.name}")
            return
        self._open_file(info.id, paths.relative_to(info.root, target))

    # -- navigation ---------------------------------------------------------
    def _on_tree_activated(self, index):
        ref = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(ref, NodeRef) or ref.is_folder:
            return
        if not ref.exists:
            self.statusBar().showMessage("File is missing on disk")
            return
        self._open_file(ref.notebook_id, ref.path)

    def _open_file(self, notebook_id: str, rel: str):
        try:
            buffer = self.ctx.buffers.open(notebook_id, rel)
            text = buffer.content()  # lazy load
        except CoreError as error:
            self.statusBar().showMessage(f"Cannot open note: {error}")
            return
        self._current_buffer = buffer
        self.editor.set_base_path(os.path.dirname(buffer.absolute_path))
        self._loading = True
        try:
            self.editor.setPlainText(text)
        finally:
            self._loading = False
        self.editor._last_emitted_text = ""  # force first render of loaded text
        self.manager.force_render(text)
        self._update_title()
        self.statusBar().showMessage(
            f"{rel}{'  [read-only]' if buffer.read_only else ''}"
        )

    def _paste_image(self, image):
        """Store a pasted image as an asset and return its note-relative path."""
        if self._current_buffer is None:
            return None
        data = QByteArray()
        sink = QBuffer(data)
        sink.open(QIODevice.OpenModeFlag.WriteOnly)
        try:
            if not image.save(sink, "PNG"):
                return None
        finally:
            sink.close()
        name = f"image_{int(time.time() * 1000)}.png"
        try:
            notebook = self.ctx.notebooks.get(self._current_buffer.notebook_id)
            rel = notebook.assets.insert_bytes(
                self._current_buffer.file_path, name, bytes(data))
        except CoreError as error:
            self.statusBar().showMessage(f"Cannot insert image: {error}")
            return None
        note_dir = os.path.dirname(self._current_buffer.absolute_path)
        asset_abs = notebook.absolute_path(rel)
        return os.path.relpath(asset_abs, note_dir).replace(os.sep, "/")

    # -- tree CRUD ----------------------------------------------------------

    def _selected_ref(self) -> NodeRef | None:
        index = self.tree.currentIndex()
        if not index.isValid():
            return None
        ref = index.data(Qt.ItemDataRole.UserRole)
        return ref if isinstance(ref, NodeRef) else None

    @staticmethod
    def _target_folder(ref: NodeRef | None, notebook_id: str) -> str:
        """Folder in which a create happens, based on the current selection."""
        if ref is not None and ref.notebook_id == notebook_id:
            return ref.path if ref.is_folder else posixpath.dirname(ref.path)
        return ""

    def _show_tree_menu(self, pos):
        ref = self._selected_ref()
        nb_id = ref.notebook_id if ref is not None else self.notebook_id
        known = {info.id for info in self.ctx.notebooks.list()}
        if not nb_id or nb_id not in known:
            return
        menu = QMenu(self)
        menu.addAction("New Note").triggered.connect(self._new_note)
        menu.addAction("New Folder").triggered.connect(self._new_folder)
        if ref is not None and not ref.is_folder and ref.exists:
            menu.addSeparator()
            menu.addAction("Rename…").triggered.connect(self._rename_node)
            menu.addAction("Duplicate").triggered.connect(self._duplicate_node)
            menu.addAction("Move to Folder…").triggered.connect(self._move_node)
            menu.addAction("Tags…").triggered.connect(self._edit_note_tags)
        if ref is not None and ref.path:  # never delete a notebook root
            menu.addSeparator()
            menu.addAction("Delete").triggered.connect(self._delete_node)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _new_note(self):
        if not self.notebook_id:
            return
        parent = self._target_folder(self._selected_ref(), self.notebook_id)
        name, ok = QInputDialog.getText(
            self, "New Note", "File name:", text="new note.md")
        if ok and name.strip():
            self.bridge.create_file(self.notebook_id, parent, name.strip())

    def _new_folder(self):
        if not self.notebook_id:
            return
        parent = self._target_folder(self._selected_ref(), self.notebook_id)
        name, ok = QInputDialog.getText(
            self, "New Folder", "Folder name:", text="New Folder")
        if ok and name.strip():
            self.bridge.create_folder(self.notebook_id, parent, name.strip())

    def _rename_node(self):
        ref = self._selected_ref()
        if ref is None or ref.is_folder or not ref.exists:
            return
        name, ok = QInputDialog.getText(
            self, "Rename", "New name:", text=ref.name)
        if ok and name.strip() and name.strip() != ref.name:
            self.bridge.rename_file(ref.notebook_id, ref.path, name.strip())

    def _duplicate_node(self):
        ref = self._selected_ref()
        if ref is None or ref.is_folder or not ref.exists:
            return
        self.bridge.copy_file(
            ref.notebook_id, ref.path, posixpath.dirname(ref.path))

    def _move_node(self):
        ref = self._selected_ref()
        if ref is None or ref.is_folder or not ref.exists:
            return
        dest, ok = QInputDialog.getText(
            self, "Move", "Destination folder (blank = root):", text="")
        if ok:
            self.bridge.move_file(ref.notebook_id, ref.path, dest.strip())

    def _edit_note_tags(self):
        ref = self._selected_ref()
        if ref is None or ref.is_folder or not ref.exists:
            return
        try:
            existing = self.ctx.notebooks.get(ref.notebook_id).folders.get_file_tags(
                ref.path)
        except CoreError:
            existing = []
        current = ", ".join(existing)
        text, ok = QInputDialog.getText(
            self, "Note Tags", "Comma-separated tags:", text=current)
        if not ok:
            return
        tags = [part.strip() for part in text.split(",") if part.strip()]
        self.bridge.set_file_tags(ref.notebook_id, ref.path, tags)

    def _delete_node(self):
        ref = self._selected_ref()
        if ref is None or not ref.path:
            return
        kind = "folder" if ref.is_folder else "note"
        answer = QMessageBox.question(
            self, f"Delete {kind}",
            f"Delete {kind} '{ref.name}'? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not ref.is_folder:
            self._close_buffer_if_open(ref.notebook_id, ref.path)
        if ref.is_folder:
            self.bridge.delete_folder(ref.notebook_id, ref.path)
        else:
            self.bridge.delete_file(ref.notebook_id, ref.path)

    def _close_buffer_if_open(self, notebook_id: str, rel: str):
        buf = self.ctx.buffers.find(notebook_id, rel)
        if buf is None:
            return
        self.ctx.buffers.close(buf.id, force=True)
        if self._current_buffer is buf:
            self._current_buffer = None
            self.editor.clear()
            self._update_title()

    def _on_node_changed(self, notebook_id: str):
        """A CRUD op finished: reload the affected tree and refresh paths."""
        self.tree_model.refresh_notebook(notebook_id)
        for row in range(self.tree_model.rowCount()):
            index = self.tree_model.index(row, 0)
            ref = index.data(Qt.ItemDataRole.UserRole)
            if isinstance(ref, NodeRef) and ref.notebook_id == notebook_id:
                self.tree_model.fetchMore(index)
                break
        if self._current_buffer is not None:
            self.editor.set_base_path(
                os.path.dirname(self._current_buffer.absolute_path))

    # -- buffer sync / save -------------------------------------------------
    def _on_editor_text_changed(self):
        if self._loading or self._current_buffer is None:
            return
        text = self.editor.toPlainText()
        if text == self._current_buffer.content():
            return  # block-format churn, not a real edit
        self._current_buffer.set_content(text)
        self._update_title()

    def _update_title(self):
        dirty = self._current_buffer is not None and self._current_buffer.modified
        self.setWindowTitle(f"marktux{' *' if dirty else ''}")

    def _save_current(self):
        if self._current_buffer is None:
            return
        try:
            self._current_buffer.save()
        except CoreError as error:
            self.statusBar().showMessage(f"Save failed: {error}")
            return
        self._update_title()
        self.statusBar().showMessage(f"Saved {self._current_buffer.file_path}")

    # -- search -------------------------------------------------------------
    def _run_search(self):
        """Dispatch a content search to core.search via the bridge."""
        pattern = self.search_edit.text().strip()
        if not pattern or not self.notebook_id:
            return
        # Cancel any previous search before starting a new one.
        if self._search_query_id:
            self.bridge.cancel_search(str(self._search_query_id))
        self._search_query_id += 1
        query_id = str(self._search_query_id)
        self._search_hits = []
        self.results.clear()
        self.statusBar().showMessage(f"Searching for “{pattern}”…")
        query = ContentQuery(pattern=pattern, scope=SearchScope(),
                             max_results=200)
        self.bridge.search(self.notebook_id, query, query_id=query_id)

    def _on_search_batch(self, batch):
        """Append one streamed chunk of results to the results list."""
        for result in batch.results:
            snippet = result.snippet
            label = f"{result.name} — {snippet}" if snippet else result.name
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, result.path)
            tip = f"{result.path}\n{snippet}" if snippet else result.path
            item.setToolTip(tip)
            self.results.addItem(item)
            self._search_hits.append((result.name, result.path))

    def _on_search_finished(self, _query_id):
        self.statusBar().showMessage(f"{len(self._search_hits)} result(s)")

    def _on_backend_fallback(self, notebook_id, _payload):
        if notebook_id == self.notebook_id:
            self.statusBar().showMessage(
                "ripgrep unavailable — using the built-in search backend")

    def _on_result_activated(self, item):
        rel = item.data(Qt.ItemDataRole.UserRole)
        if not rel or not self.notebook_id:
            return
        self._open_file(self.notebook_id, rel)

    # -- shutdown -----------------------------------------------------------
    def _shutdown_core(self):
        try:
            self.bridge.shutdown()
        except Exception:  # noqa: BLE001
            pass

    def _shutdown_linguistic(self):
        """Stop the async spell-check worker and release the service cleanly."""
        if self._spell_decorator is not None:
            try:
                self._spell_decorator.set_spellcheck_enabled(False)
                self._spell_decorator.detach()
            except Exception:
                pass
            self._spell_decorator = None
        if self._dict_dialog is not None:
            try:
                self._dict_dialog.close()
            except Exception:
                pass
            self._dict_dialog = None
        if self._service is not None:
            try:
                self._service.close()
            except Exception:
                pass
            self._service = None

    def closeEvent(self, event):
        self._shutdown_linguistic()
        self._shutdown_core()
        super().closeEvent(event)

    def _ensure_service(self):
        if self._service is None:
            self._service = create_service(namespace="marktux")
        return self._service

    def _open_dict_manager(self):
        if not toolkit_available():
            QMessageBox.information(
                self, "Dictionary Manager",
                f"The linguistic toolkit is unavailable:\n{toolkit_error()}",
            )
            return
        try:
            service = self._ensure_service()
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "Dictionary Manager", str(error))
            return
        if self._dict_dialog is not None:
            self._dict_dialog.show()
            self._dict_dialog.raise_()
            self._dict_dialog.activateWindow()
            return
        self._dict_dialog = show_download_manager(self, service, namespace="marktux")
        if self._dict_dialog is None:
            QMessageBox.information(
                self, "Dictionary Manager",
                "Could not open the dictionary download manager.",
            )

    def _toggle_spellcheck(self, checked: bool):
        if not toolkit_available():
            self.spell_act.setChecked(False)
            QMessageBox.information(
                self, "Spell Check",
                f"The linguistic toolkit is unavailable:\n{toolkit_error()}",
            )
            return
        try:
            from pyqt6_linguistic_tools.qt import LinguisticTextEditDecorator  # type: ignore
            service = self._ensure_service()
        except Exception as error:  # noqa: BLE001
            self.spell_act.setChecked(False)
            QMessageBox.warning(self, "Spell Check", str(error))
            return
        if checked:
            if self._spell_decorator is None:
                self._spell_decorator = LinguisticTextEditDecorator(
                    self.editor, service
                )
            self._spell_decorator.set_spellcheck_enabled(True)
            self.statusBar().showMessage("Spell check enabled")
        else:
            if self._spell_decorator is not None:
                self._spell_decorator.set_spellcheck_enabled(False)
            self.statusBar().showMessage("Spell check disabled")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("MarkTux")
    app.setApplicationDisplayName("MarkTux")
    app.setOrganizationName("marktux")
    win = MainWindow()
    app.aboutToQuit.connect(win._shutdown_linguistic)
    app.aboutToQuit.connect(win._shutdown_core)
    win.show()
    win.restore_last_file()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
