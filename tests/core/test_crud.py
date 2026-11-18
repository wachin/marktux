"""Phase 3 acceptance: folder/file CRUD, cache write-through, rollback.

Behavioural specs ported from vxcore's folder operation tests.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from core import (
    CoreContext,
    CoreError,
    InvalidNameError,
    NodeExistsError,
    NodeNotFoundError,
    ReadOnlyError,
    UnsupportedError,
)

from .helpers import make_bundled, make_context, make_raw


class BundledCrudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.root = make_bundled(self.tmp)
        self.ctx = make_context(self.tmp, recover=False)
        self.addCleanup(self.ctx.close)
        self.nb = self.ctx.open_notebook(self.root)
        self.f = self.nb.folders

    def _config(self, rel=""):
        parts = [self.root, "vx_notebook", "contents"]
        if rel:
            parts.extend(rel.split("/"))
        parts.append("vx.json")
        return json.loads(pathlib.Path(os.path.join(*parts)).read_text())

    # -- create -------------------------------------------------------------

    def test_create_folder_updates_config_and_cache(self):
        ref = self.f.create_folder("", "Inbox")
        self.assertEqual(ref.path, "Inbox")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "Inbox")))
        self.assertIn("Inbox", self._config()["folders"])
        self.assertIsNotNone(self.nb.store.get_folder_by_path("Inbox"))

    def test_create_file_writes_body_and_index(self):
        ref = self.f.create_file("notes", "new.md", "# New\n")
        self.assertEqual(ref.path, "notes/new.md")
        self.assertEqual(pathlib.Path(os.path.join(self.root, "notes", "new.md")).read_text(), "# New\n")
        names = [f["name"] for f in self._config("notes")["files"]]
        self.assertIn("new.md", names)
        self.assertIsNotNone(self.nb.store.get_file_by_path("notes/new.md"))

    def test_create_rejects_duplicate_and_invalid_names(self):
        with self.assertRaises(NodeExistsError):
            self.f.create_file("notes", "hello.md")  # already exists
        with self.assertRaises(InvalidNameError):
            self.f.create_folder("", "../escape")
        with self.assertRaises(InvalidNameError):
            self.f.create_file("notes", "CON.md")  # Windows reserved

    def test_available_name_detects_case_insensitive_collision(self):
        self.assertEqual(self.f.available_name("notes", "hello.md"), "hello_1.md")
        self.f.create_file("notes", "HELLO2.md")
        self.assertEqual(self.f.available_name("notes", "hello2.md"), "hello2_1.md")

    # -- rename / move / copy -----------------------------------------------

    def test_rename_file_updates_everything(self):
        before = self.nb.store.get_file_by_path("notes/hello.md")
        ref = self.f.rename_file("notes/hello.md", "renamed.md")
        self.assertEqual(ref.path, "notes/renamed.md")
        self.assertFalse(os.path.exists(os.path.join(self.root, "notes", "hello.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "notes", "renamed.md")))
        self.assertIn("renamed.md", [f["name"] for f in self._config("notes")["files"]])
        self.assertIsNotNone(self.nb.store.get_file_by_path("notes/renamed.md"))
        self.assertIsNone(self.nb.store.get_file_by_path("notes/hello.md"))
        self.assertEqual(ref.id, before.id)  # UUID preserved on rename

    def test_move_file_between_folders(self):
        self.f.create_folder("", "archive")
        ref = self.f.move_file("notes/hello.md", "archive")
        self.assertEqual(ref.path, "archive/hello.md")
        self.assertNotIn("hello.md", [f["name"] for f in self._config("notes")["files"]])
        self.assertIn("hello.md", [f["name"] for f in self._config("archive")["files"]])
        self.assertIsNotNone(self.nb.store.get_file_by_path("archive/hello.md"))

    def test_copy_file_regenerates_uuid(self):
        source = self.f.get_file_info("notes/hello.md")
        ref = self.f.copy_file("notes/hello.md", "notes")
        self.assertNotEqual(ref.id, source.id)
        self.assertIn("hello_1.md", [f["name"] for f in self._config("notes")["files"]])
        self.assertEqual(self.nb.store.counts()["files"], 2)

    def test_rename_updates_open_buffer_path(self):
        buf = self.ctx.buffers.open(self.nb.id, "notes/hello.md")
        self.f.rename_file("notes/hello.md", "renamed.md")
        self.assertEqual(buf.file_path, "notes/renamed.md")
        self.assertIs(self.ctx.buffers.find(self.nb.id, "notes/renamed.md"), buf)

    # -- delete -------------------------------------------------------------

    def test_delete_file_removes_config_cache_and_disk(self):
        self.f.delete_file("notes/hello.md")
        self.assertFalse(os.path.exists(os.path.join(self.root, "notes", "hello.md")))
        self.assertNotIn("hello.md", [f["name"] for f in self._config("notes")["files"]])
        self.assertIsNone(self.nb.store.get_file_by_path("notes/hello.md"))

    def test_delete_folder_removes_config_tree_and_cache_cascade(self):
        self.f.create_file("notes", "extra.md")
        self.f.delete_folder("notes")
        self.assertFalse(os.path.exists(os.path.join(self.root, "notes")))
        self.assertFalse(os.path.exists(os.path.join(
            self.root, "vx_notebook", "contents", "notes", "vx.json")))
        self.assertIsNone(self.nb.store.get_folder_by_path("notes"))
        self.assertEqual(self.nb.store.counts()["files"], 0)

    def test_delete_config_failure_rolls_back(self):
        # A config write failure must restore the renamed file and leave the
        # config/cache untouched.
        note_abs = os.path.join(self.root, "notes", "hello.md")
        original = pathlib.Path(note_abs).read_bytes()
        with mock.patch.object(self.f, "_save_config", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.f.delete_file("notes/hello.md")
        self.assertTrue(os.path.isfile(note_abs))
        self.assertEqual(pathlib.Path(note_abs).read_bytes(), original)
        self.assertIsNotNone(self.nb.store.get_file_by_path("notes/hello.md"))
        self.assertEqual(os.listdir(os.path.dirname(note_abs)), ["hello.md"])

    def test_cannot_delete_root(self):
        with self.assertRaises(InvalidNameError):
            self.f.delete_folder("")

    # -- ordering / read-only ----------------------------------------------

    def test_set_children_order_permutation_and_noop(self):
        self.f.create_folder("", "Alpha")
        self.f.create_folder("", "Beta")
        config_before = self._config()["folders"]
        self.f.set_children_order("", ["Beta", "Alpha", "notes"])
        self.assertEqual(self._config()["folders"], ["Beta", "Alpha", "notes"])
        # No-op does not rewrite (patch to detect any write).
        with mock.patch.object(self.f, "_save_config") as save:
            self.f.set_children_order("", ["Beta", "Alpha", "notes"])
            save.assert_not_called()
        with self.assertRaises(InvalidNameError):
            self.f.set_children_order("", ["Alpha"])

    def test_read_only_notebook_rejects_all_mutations(self):
        self.ctx.notebooks.set_read_only(self.nb.id, True)
        for call in (
            lambda: self.f.create_folder("", "X"),
            lambda: self.f.create_file("", "x.md"),
            lambda: self.f.delete_file("notes/hello.md"),
            lambda: self.f.rename_file("notes/hello.md", "x.md"),
        ):
            with self.assertRaises(ReadOnlyError):
                call()


class RawCrudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.root = make_raw(self.tmp)
        self.ctx = make_context(self.tmp, recover=False)
        self.addCleanup(self.ctx.close)
        self.nb = self.ctx.open_notebook(self.root)
        self.f = self.nb.folders

    def test_raw_create_rename_delete(self):
        ref = self.f.create_file("", "new.md", "raw")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "new.md")))
        r = self.f.rename_file("new.md", "renamed.md")
        self.assertEqual(r.path, "renamed.md")
        self.f.delete_file("renamed.md")
        self.assertFalse(os.path.exists(os.path.join(self.root, "renamed.md")))

    def test_raw_create_folder(self):
        ref = self.f.create_folder("", "folder")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "folder")))
        self.assertEqual(ref.path, "folder")

    def test_raw_id_stable_after_rename(self):
        before = self.f.get_file_info("a.md").id
        self.f.rename_file("a.md", "z.md")
        # Raw ids are path-derived, so a rename changes the id (vxcore does the same).
        after = self.f.get_file_info("z.md").id
        self.assertNotEqual(before, after)
        self.assertEqual(after, self.nb.folders._node_id("z.md"))

    def test_raw_attachments_unsupported(self):
        with self.assertRaises(UnsupportedError):
            self.f.add_attachment("a.md", "x.png")


if __name__ == "__main__":
    unittest.main()
