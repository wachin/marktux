"""Offscreen Qt tests for the Phase 1 adapter (CoreBridge + tree model)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from PyQt6.QtCore import QCoreApplication, QEventLoop, Qt, QTimer

from core import (
    ContentQuery,
    CoreContext,
    NotebookInfo,
    NotebookType,
    SearchScope,
)
from ui import CoreBridge, NotebookNodeModel

from ..core.helpers import make_bundled, make_raw


def _app() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _pump(ms: int = 350) -> None:
    """Let worker-thread callbacks and queued signals run."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.ctx = CoreContext(
            app_data_dir=os.path.join(self.tmp, "app"),
            local_data_dir=os.path.join(self.tmp, "local"),
            recover_session=False,
        ).open()
        self.bridge = CoreBridge(self.ctx)

    def tearDown(self):
        self.bridge.shutdown()

    def _info(self, root: str, name: str, kind: NotebookType) -> NotebookInfo:
        return NotebookInfo(id="test-id", name=name, root=root, type=kind)

    def test_open_notebook_emits_notebook_opened(self):
        root = make_bundled(self.tmp, "Fixture Notebook")
        seen: list[NotebookInfo] = []
        errors: list[tuple[str, str]] = []
        self.bridge.notebookOpened.connect(seen.append)
        self.bridge.error.connect(lambda op, msg: errors.append((op, msg)))

        self.bridge.open_notebook(root)
        _pump()

        self.assertEqual(errors, [])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].name, "Fixture Notebook")
        self.assertEqual(seen[0].type, NotebookType.BUNDLED)

    def test_open_missing_directory_emits_error(self):
        errors: list[tuple[str, str]] = []
        opened: list = []
        self.bridge.error.connect(lambda op, msg: errors.append((op, msg)))
        self.bridge.notebookOpened.connect(opened.append)

        self.bridge.open_notebook(os.path.join(self.tmp, "does-not-exist"))
        _pump()

        self.assertEqual(opened, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], "open_notebook")

    def test_list_children_emits_contents(self):
        root = make_bundled(self.tmp, "Fixture Notebook")
        nb = self.ctx.open_notebook(root)
        seen: list[tuple[str, str, object]] = []
        self.bridge.childrenLoaded.connect(
            lambda nid, rel, contents: seen.append((nid, rel, contents)))

        self.bridge.list_children(nb.id, "")
        _pump()

        self.assertEqual(len(seen), 1)
        notebook_id, rel, contents = seen[0]
        self.assertEqual(notebook_id, nb.id)
        self.assertEqual(rel, "")
        self.assertEqual([n.name for n in contents.folders], ["notes"])

    def test_shutdown_is_idempotent(self):
        self.bridge.shutdown()
        self.bridge.shutdown()  # must not raise


class BridgeCrudTest(unittest.TestCase):
    """Phase 3: CRUD bridge methods run off-thread and emit nodeChanged."""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.ctx = CoreContext(
            app_data_dir=os.path.join(self.tmp, "app"),
            local_data_dir=os.path.join(self.tmp, "local"),
            recover_session=False,
        ).open()
        self.bridge = CoreBridge(self.ctx)
        self.root = make_bundled(self.tmp, "Fixture Notebook")
        self.nb = self.ctx.open_notebook(self.root)
        self.changed: list[str] = []
        self.errors: list[tuple[str, str]] = []
        self.bridge.nodeChanged.connect(self.changed.append)
        self.bridge.error.connect(
            lambda op, msg: self.errors.append((op, msg)))

    def tearDown(self):
        self.bridge.shutdown()

    def test_create_file_writes_disk_and_emits_change(self):
        self.bridge.create_file(self.nb.id, "notes", "new.md", "# New\n")
        _pump()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.changed, [self.nb.id])
        self.assertTrue(
            os.path.isfile(os.path.join(self.root, "notes", "new.md")))

    def test_rename_then_delete(self):
        self.bridge.rename_file(self.nb.id, "notes/hello.md", "renamed.md")
        _pump()
        self.assertEqual(self.errors, [])
        self.assertFalse(
            os.path.exists(os.path.join(self.root, "notes", "hello.md")))
        self.assertTrue(
            os.path.isfile(os.path.join(self.root, "notes", "renamed.md")))

        self.bridge.delete_file(self.nb.id, "notes/renamed.md")
        _pump()
        self.assertEqual(self.errors, [])
        self.assertFalse(
            os.path.exists(os.path.join(self.root, "notes", "renamed.md")))
        self.assertEqual(len(self.changed), 2)

    def test_duplicate_create_routes_error_without_change(self):
        self.bridge.create_file(self.nb.id, "notes", "hello.md")
        _pump()
        self.assertEqual(self.changed, [])
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(self.errors[0][0], "create_file")


class BridgeTagTest(unittest.TestCase):
    """Phase 4: tag bridge methods run off-thread and emit tag signals."""

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.ctx = CoreContext(
            app_data_dir=os.path.join(self.tmp, "app"),
            local_data_dir=os.path.join(self.tmp, "local"),
            recover_session=False,
        ).open()
        self.bridge = CoreBridge(self.ctx)
        self.root = make_bundled(self.tmp, "Fixture Notebook")
        self.nb = self.ctx.open_notebook(self.root)
        self.errors: list[tuple[str, str]] = []
        self.bridge.error.connect(lambda op, msg: self.errors.append((op, msg)))

    def tearDown(self):
        self.bridge.shutdown()

    def test_create_tag_emits_tags_loaded(self):
        seen: list = []
        self.bridge.tagsLoaded.connect(
            lambda nid, tags: seen.append((nid, [t.name for t in tags])))
        self.bridge.create_tag(self.nb.id, "work")
        _pump()
        self.assertEqual(self.errors, [])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], (self.nb.id, ["work"]))

    def test_tag_file_and_find(self):
        self.bridge.create_tag(self.nb.id, "work")
        _pump()
        self.bridge.tag_file(self.nb.id, "notes/hello.md", "work")
        _pump()

        found: list = []
        self.bridge.tagFilesLoaded.connect(
            lambda nid, tags, results: found.append([r.file_path for r in results]))
        self.bridge.find_tag_files(self.nb.id, ["work"], "OR")
        _pump()
        self.assertEqual(self.errors, [])
        self.assertEqual(found, [["notes/hello.md"]])

    def test_tag_counts(self):
        self.bridge.create_tag(self.nb.id, "work")
        _pump()
        self.bridge.tag_file(self.nb.id, "notes/hello.md", "work")
        _pump()
        counts: list = []
        self.bridge.tagCountsLoaded.connect(
            lambda nid, data: counts.append(data))
        self.bridge.tag_counts(self.nb.id)
        _pump()
        self.assertEqual(self.errors, [])
        self.assertEqual(counts, [{"work": 1}])

    def test_delete_tag_emits_reload(self):
        self.bridge.create_tag(self.nb.id, "work")
        _pump()
        self.bridge.delete_tag(self.nb.id, "work")
        _pump()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.nb.tags.list(), [])


class BridgeSearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.ctx = CoreContext(
            app_data_dir=os.path.join(self.tmp, "app"),
            local_data_dir=os.path.join(self.tmp, "local"),
            recover_session=False, search_backends=["simple"],
        ).open()
        self.bridge = CoreBridge(self.ctx)
        self.root = make_bundled(self.tmp, "Fixture Notebook", with_note=False)
        self._add_note("notes/alpha.md", "needle here\n")
        self._add_note("notes/beta.md", "nothing\n")
        self.nb = self.ctx.open_notebook(self.root)
        self.errors: list[tuple[str, str]] = []
        self.bridge.error.connect(lambda op, msg: self.errors.append((op, msg)))

    def tearDown(self):
        self.bridge.shutdown()

    def _add_note(self, rel: str, text: str) -> None:
        with open(os.path.join(self.root, rel), "w", encoding="utf-8") as fh:
            fh.write(text)
        folder = os.path.dirname(rel)
        cfg_path = os.path.join(self.root, "vx_notebook", "contents",
                                folder, "vx.json")
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg.setdefault("files", []).append({
            "id": f"file-{rel}", "name": os.path.basename(rel),
            "createdUtc": 1, "modifiedUtc": 1, "metadata": {},
        })
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

    def test_search_streams_batches_and_finishes(self):
        batches: list = []
        finished: list[str] = []
        self.bridge.searchBatch.connect(batches.append)
        self.bridge.searchFinished.connect(finished.append)
        query = ContentQuery("needle", scope=SearchScope(folder_path="notes"),
                             max_results=10)
        self.bridge.search(self.nb.id, query, query_id="q1", batch_size=1)
        _pump()
        self.assertEqual(self.errors, [])
        self.assertEqual(finished, ["q1"])
        hits = [r.path for batch in batches for r in batch.results]
        self.assertEqual(hits, ["notes/alpha.md"])

    def test_cancel_search_finishes_without_error(self):
        finished: list[str] = []
        self.bridge.searchFinished.connect(finished.append)
        query = ContentQuery("needle", scope=SearchScope(folder_path="notes"))
        self.bridge.search(self.nb.id, query, query_id="q2")
        self.bridge.cancel_search("q2")
        _pump()
        self.assertEqual(self.errors, [])
        self.assertEqual(finished, ["q2"])


class TreeModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.ctx = CoreContext(
            app_data_dir=os.path.join(self.tmp, "app"),
            local_data_dir=os.path.join(self.tmp, "local"),
            recover_session=False,
        ).open()

    def tearDown(self):
        self.ctx.close()

    def test_lazy_fetch_of_bundled_tree(self):
        root = make_bundled(self.tmp, "Fixture Notebook")
        nb = self.ctx.open_notebook(root)
        model = NotebookNodeModel(self.ctx)

        model.set_notebooks([nb.info()])
        self.assertEqual(model.rowCount(), 1)
        top = model.index(0, 0)
        self.assertEqual(top.data(Qt.ItemDataRole.DisplayRole), "Fixture Notebook")
        self.assertTrue(model.canFetchMore(top))

        model.fetchMore(top)
        self.assertEqual(model.rowCount(top), 1)
        notes = model.index(0, 0, top)
        self.assertEqual(notes.data(Qt.ItemDataRole.DisplayRole), "notes")
        self.assertFalse(model.canFetchMore(notes) is False)  # can still fetch

        model.fetchMore(notes)
        self.assertEqual(model.rowCount(notes), 1)
        note = model.index(0, 0, notes)
        self.assertEqual(note.data(Qt.ItemDataRole.DisplayRole), "hello.md")
        self.assertEqual(note.data(Qt.ItemDataRole.UserRole).id,
                         "22222222-2222-2222-2222-222222222222")
        self.assertEqual(model.parent(note).data(Qt.ItemDataRole.DisplayRole),
                         "notes")

    def test_missing_file_shown_with_marker(self):
        root = make_bundled(self.tmp, "Fixture Notebook")
        nb = self.ctx.open_notebook(root)
        model = NotebookNodeModel(self.ctx)
        model.set_notebooks([nb.info()])
        top = model.index(0, 0)
        model.fetchMore(top)
        notes = model.index(0, 0, top)
        model.fetchMore(notes)
        note = model.index(0, 0, notes)
        self.assertEqual(note.data(Qt.ItemDataRole.DisplayRole), "hello.md")

        os.unlink(os.path.join(root, "notes", "hello.md"))
        model.refresh_notebook(nb.id)
        top = model.index(0, 0)
        model.fetchMore(top)  # re-fetch after refresh cleared children
        notes = model.index(0, 0, top)
        model.fetchMore(notes)
        note = model.index(0, 0, notes)
        self.assertIn("missing", note.data(Qt.ItemDataRole.DisplayRole))
        self.assertFalse(note.data(Qt.ItemDataRole.UserRole).exists)

    def test_add_and_remove_notebook(self):
        root = make_bundled(self.tmp, "Fixture Notebook")
        raw_root = make_raw(self.tmp)
        nb1 = self.ctx.open_notebook(root)
        model = NotebookNodeModel(self.ctx)
        model.set_notebooks([])
        self.assertEqual(model.rowCount(), 0)

        model.add_notebook(nb1.info())
        self.assertEqual(model.rowCount(), 1)
        model.add_notebook(nb1.info())  # duplicate ignored
        self.assertEqual(model.rowCount(), 1)

        nb2 = self.ctx.open_notebook(raw_root)
        model.add_notebook(nb2.info())
        self.assertEqual(model.rowCount(), 2)

        model.remove_notebook(nb1.id)
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.notebook_ids(), [nb2.id])


if __name__ == "__main__":
    unittest.main()
