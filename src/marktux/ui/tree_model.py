"""Lazy notebook tree model (moved out of the Qt-free ``core`` package).

One top-level row per open notebook. Folders and files are fetched one level at
a time through ``canFetchMore``/``fetchMore`` — exactly the VNote
``NotebookNodeModel`` contract. Each row exposes its ``NodeRef`` under
``UserRole`` so ``main.py`` can open a note without reaching into core internals.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from PyQt6.QtCore import QAbstractItemModel, QModelIndex, Qt, pyqtSignal

from marktux.core import CoreContext, NodeRef, NotebookInfo

log = logging.getLogger(__name__)


class _Node:
    __slots__ = ("parent", "children", "ref", "loaded", "label")

    def __init__(
        self,
        ref: NodeRef,
        label: str,
        parent: "_Node | None" = None,
    ) -> None:
        self.ref = ref
        self.label = label
        self.parent = parent
        self.children: list[_Node] = []
        self.loaded = False

    @classmethod
    def notebook(cls, info: NotebookInfo) -> "_Node":
        # A notebook is represented as a synthetic folder NodeRef so every row
        # has the same shape and UserRole always returns a NodeRef.
        ref = NodeRef(
            notebook_id=info.id,
            path="",
            name=info.name,
            id=info.id,
            is_folder=True,
        )
        return cls(ref, info.name)

    @classmethod
    def child(cls, ref: NodeRef, parent: "_Node") -> "_Node":
        label = ref.name or ref.path
        if not ref.exists:
            label += "  (missing)"
        return cls(ref, label, parent)


class NotebookNodeModel(QAbstractItemModel):
    """Lazy tree of open notebooks, folders and files."""

    notebookCountChanged = pyqtSignal(int)

    def __init__(self, ctx: CoreContext, parent: Any = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._roots: list[_Node] = []

    # -- public mutation ----------------------------------------------------

    def set_notebooks(self, infos: list[NotebookInfo]) -> None:
        """Replace the whole tree with ``infos`` (used on startup/restore)."""
        self.beginResetModel()
        self._roots = [_Node.notebook(info) for info in infos]
        self.endResetModel()
        self.notebookCountChanged.emit(len(self._roots))

    def add_notebook(self, info: NotebookInfo) -> None:
        if any(node.ref.notebook_id == info.id for node in self._roots):
            return
        row = len(self._roots)
        self.beginInsertRows(QModelIndex(), row, row)
        self._roots.append(_Node.notebook(info))
        self.endInsertRows()
        self.notebookCountChanged.emit(len(self._roots))

    def remove_notebook(self, notebook_id: str) -> None:
        for row, node in enumerate(self._roots):
            if node.ref.notebook_id == notebook_id:
                self.beginRemoveRows(QModelIndex(), row, row)
                self._roots.pop(row)
                self.endRemoveRows()
                self.notebookCountChanged.emit(len(self._roots))
                return

    def refresh_notebook(self, notebook_id: str) -> None:
        """Drop cached children of a notebook so the next expand re-reads disk."""
        for node in self._roots:
            if node.ref.notebook_id == notebook_id:
                self._clear_subtree(node)
                return

    def _clear_subtree(self, node: _Node) -> None:
        for child in list(node.children):
            self._clear_subtree(child)
        node.loaded = False
        if node.children:
            index = self._index_of(node)
            count = len(node.children)
            self.beginRemoveRows(index, 0, count - 1)
            node.children = []
            self.endRemoveRows()

    def _index_of(self, node: _Node) -> QModelIndex:
        if node.parent is None:
            return self.createIndex(self._roots.index(node), 0, node)
        parent_index = self._index_of(node.parent)
        return self.createIndex(node.parent.children.index(node), 0, node)

    def notebook_ids(self) -> list[str]:
        return [node.ref.notebook_id for node in self._roots]

    # -- QAbstractItemModel -------------------------------------------------

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(row, column, self._roots[row])
        parent_node = cast(_Node, parent.internalPointer())
        if 0 <= row < len(parent_node.children):
            return self.createIndex(row, column, parent_node.children[row])
        return QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        node = cast(_Node, index.internalPointer())
        if node.parent is None:
            return QModelIndex()
        grand = node.parent.parent
        siblings = grand.children if grand is not None else self._roots
        try:
            row = siblings.index(node.parent)
        except ValueError:
            return QModelIndex()
        return self.createIndex(row, 0, node.parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.column() > 0:
            return 0
        if not parent.isValid():
            return len(self._roots)
        return len((cast(_Node, parent.internalPointer())).children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        node = cast(_Node, index.internalPointer())
        if role == Qt.ItemDataRole.DisplayRole:
            return node.label
        if role == Qt.ItemDataRole.UserRole:
            return node.ref
        if role == Qt.ItemDataRole.ToolTipRole:
            ref = node.ref
            return f"{ref.path or '/'}\n{ref.id}"
        return None

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return "Notebook"
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # -- lazy fetch ---------------------------------------------------------

    def canFetchMore(self, parent: QModelIndex = QModelIndex()) -> bool:
        if not parent.isValid():
            return False
        node = cast(_Node, parent.internalPointer())
        return node.ref.is_folder and not node.loaded

    def fetchMore(self, parent: QModelIndex = QModelIndex()) -> None:
        if not self.canFetchMore(parent):
            return
        node = cast(_Node, parent.internalPointer())
        try:
            notebook = self._ctx.notebooks.get(node.ref.notebook_id)
            contents = notebook.folders.list_children(node.ref.path)
        except Exception:  # noqa: BLE001 - a broken folder must not kill the tree
            log.exception("cannot list children of %s", node.ref.path)
            return

        # Configured folders/files first, then anything found on disk but not
        # indexed (external). Dedupe by relative path.
        refs: list[NodeRef] = []
        seen: set[str] = set()
        for ref in (
            *contents.folders,
            *contents.files,
            *contents.external_folders,
            *contents.external_files,
        ):
            if ref.path in seen:
                continue
            seen.add(ref.path)
            refs.append(ref)

        node.loaded = True
        if not refs:
            return
        row = len(node.children)
        self.beginInsertRows(parent, row, row + len(refs) - 1)
        node.children.extend(_Node.child(ref, node) for ref in refs)
        self.endInsertRows()

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        if not parent.isValid():
            return len(self._roots) > 0
        node = cast(_Node, parent.internalPointer())
        if not node.ref.is_folder:
            return False
        return True  # folders always offer an expand arrow until fetched


__all__ = ["NotebookNodeModel"]
