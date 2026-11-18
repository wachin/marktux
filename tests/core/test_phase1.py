"""Phase 1 acceptance: foundation + read-only browsing.

Behavioural specs ported from vxcore's folder/notebook tests plus new checks for
the Python-specific guarantees (Qt-free core, missing-node detection, raw
notebooks, session persistence).
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import tempfile
import unittest

from core import CoreContext, NotebookType, NodeRef

from .helpers import make_bundled, make_context, make_raw


class CoreIsQtFreeTest(unittest.TestCase):
    def test_no_qt_imports_in_core(self):
        root = pathlib.Path(__file__).resolve().parents[2] / "core"
        bad: list[str] = []
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name.split(".")[0] in {"PyQt6", "PySide6", "PyQt5"}:
                        bad.append(f"{path.name}:{node.lineno} {name}")
        self.assertEqual(bad, [], "core/ must not import Qt: " + ", ".join(bad))


class BundledNotebookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.root = make_bundled(self.tmp, "Fixture Notebook")
        self.ctx = make_context(self.tmp, recover=False)
        self.addCleanup(self.ctx.close)
        self.nb = self.ctx.open_notebook(self.root)

    def test_open_detects_bundled_and_reads_identity(self):
        self.assertEqual(self.nb.type, NotebookType.BUNDLED)
        self.assertEqual(self.nb.name, "Fixture Notebook")
        self.assertEqual(self.nb.id, "11111111-1111-1111-1111-111111111111")
        self.assertTrue(os.path.isfile(self.nb.db_path),
                        "a per-notebook SQLite cache should be created")

    def test_root_lists_subfolder_with_correct_uuid(self):
        contents = self.nb.folders.list_children("")
        self.assertEqual([n.name for n in contents.folders], ["notes"])
        notes = contents.folders[0]
        self.assertTrue(notes.is_folder)
        self.assertEqual(notes.id, "33333333-3333-3333-3333-333333333333")

    def test_subfolder_lists_file_with_correct_uuid(self):
        contents = self.nb.folders.list_children("notes")
        self.assertEqual(len(contents.files), 1)
        note = contents.files[0]
        self.assertEqual(note.name, "hello.md")
        self.assertEqual(note.id, "22222222-2222-2222-2222-222222222222")
        self.assertEqual(note.path, "notes/hello.md")
        self.assertTrue(note.exists)

    def test_missing_file_is_flagged_without_mutating_sources(self):
        config_path = os.path.join(self.root, "vx_notebook", "contents",
                                   "notes", "vx.json")
        note_path = os.path.join(self.root, "notes", "hello.md")
        before_config = pathlib.Path(config_path).read_bytes()
        before_db = pathlib.Path(self.nb.db_path).read_bytes()

        os.unlink(note_path)
        contents = self.nb.folders.list_children("notes")
        self.assertEqual(len(contents.files), 1)
        self.assertFalse(contents.files[0].exists)

        self.assertEqual(pathlib.Path(config_path).read_bytes(), before_config)
        self.assertEqual(pathlib.Path(self.nb.db_path).read_bytes(), before_db)

    def test_cache_is_populated_and_path_lookup_works(self):
        self.nb.folders.list_children("")
        self.nb.folders.list_children("notes")
        counts = self.nb.store.counts()
        self.assertEqual(counts["folders"], 2)  # root + notes
        self.assertEqual(counts["files"], 1)
        row = self.nb.store.get_file_by_path("notes/hello.md")
        self.assertIsNotNone(row)
        self.assertEqual(row.id, "22222222-2222-2222-2222-222222222222")

    def test_absolute_path_resolves_note_body(self):
        target = self.nb.folders.absolute_path("notes/hello.md")
        self.assertEqual(target, os.path.join(self.root, "notes", "hello.md"))
        self.assertTrue(os.path.isfile(target))

    def test_resolve_path_back_to_node(self):
        target = os.path.join(self.root, "notes", "hello.md")
        resolved = self.ctx.notebooks.resolve_path(target)
        self.assertIsNotNone(resolved)
        notebook, rel = resolved
        self.assertEqual(notebook.id, self.nb.id)
        self.assertEqual(rel, "notes/hello.md")


class RawNotebookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.root = make_raw(self.tmp)
        self.ctx = make_context(self.tmp, recover=False)
        self.addCleanup(self.ctx.close)

    def test_plain_directory_opens_as_raw(self):
        nb = self.ctx.open_notebook(self.root)
        self.assertEqual(nb.type, NotebookType.RAW)
        self.assertFalse(os.path.exists(os.path.join(self.root, "vx_notebook")))

    def test_raw_id_is_stable_across_reopen(self):
        first = self.ctx.open_notebook(self.root)
        first_id = first.id
        self.ctx.close()
        # Re-opening with a fresh context and no session must derive the same id.
        ctx2 = CoreContext(
            app_data_dir=os.path.join(self.tmp, "app"),
            local_data_dir=os.path.join(self.tmp, "local"),
            recover_session=False,
        ).open()
        self.addCleanup(ctx2.close)
        second = ctx2.open_notebook(self.root)
        self.assertEqual(second.id, first_id)

    def test_raw_tree_lists_notes_from_disk(self):
        nb = self.ctx.open_notebook(self.root)
        root = nb.folders.list_children("")
        names = [n.name for n in root.nodes]
        self.assertIn("a.md", names)
        self.assertIn("sub", names)
        sub = next(n for n in root.nodes if n.name == "sub")
        self.assertTrue(sub.is_folder)
        sub_children = nb.folders.list_children(sub.path)
        self.assertEqual([n.name for n in sub_children.nodes], ["b.md"])


class SessionTest(unittest.TestCase):
    def test_open_notebooks_are_restored_from_session(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        root = make_bundled(tmp)
        ctx = make_context(tmp, recover=False)
        nb = ctx.open_notebook(root)
        opened_id = nb.id
        ctx.close()

        ctx2 = CoreContext(
            app_data_dir=os.path.join(tmp, "app"),
            local_data_dir=os.path.join(tmp, "local"),
            recover_session=True,
        ).open()
        self.addCleanup(ctx2.close)
        restored = ctx2.notebooks.list()
        self.assertEqual([info.id for info in restored], [opened_id])
        self.assertTrue(os.path.isfile(
            os.path.join(tmp, "local", "vxsession.json")))


if __name__ == "__main__":
    unittest.main()
