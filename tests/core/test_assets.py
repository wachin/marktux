"""Phase 7 acceptance: assets, attachments and file types.

Behavioural specs ported from vxcore's ``test_asset_utils.cpp`` and
``test_filetype_config.cpp``.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from marktux.core import (
    FileTypesConfig,
    InvalidNameError,
    NodeNotFoundError,
    PathEscapeError,
    ReadOnlyError,
    UnsupportedError,
)

from .helpers import make_bundled, make_context, make_raw

FILE_ID = "22222222-2222-2222-2222-222222222222"


class BundledAssetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_bundled(self.tmp)
        self.ctx = make_context(self.tmp, recover=False)
        self.addCleanup(self.ctx.close)
        self.nb = self.ctx.open_notebook(self.root)
        self.sources = os.path.join(self.tmp, "sources")
        os.makedirs(self.sources)

    def _source(self, name: str, data: bytes) -> str:
        path = os.path.join(self.sources, name)
        Path(path).write_bytes(data)
        return path

    def test_insert_asset_relative_path_and_bytes(self):
        src = self._source("pic.png", b"fake png data")
        rel = self.nb.assets.insert("notes/hello.md", src)
        self.assertEqual(rel, f"notes/vx_assets/{FILE_ID}/pic.png")
        self.assertEqual(
            Path(self.root, rel).read_bytes(), b"fake png data")

    def test_dedupe_adds_numeric_suffix(self):
        src = self._source("pic.png", b"one")
        first = self.nb.assets.insert("notes/hello.md", src)
        second = self.nb.assets.insert("notes/hello.md", src)
        self.assertTrue(first.endswith("/pic.png"))
        self.assertTrue(second.endswith("/pic_1.png"))
        self.assertEqual(Path(self.root, first).read_bytes(), b"one")
        self.assertEqual(Path(self.root, second).read_bytes(), b"one")

    def test_insert_bytes(self):
        rel = self.nb.assets.insert_bytes(
            "notes/hello.md", "diagram.svg", b"<svg/>")
        self.assertEqual(rel, f"notes/vx_assets/{FILE_ID}/diagram.svg")
        self.assertEqual(Path(self.root, rel).read_bytes(), b"<svg/>")

    def test_delete_asset(self):
        src = self._source("pic.png", b"data")
        rel = self.nb.assets.insert("notes/hello.md", src)
        self.nb.assets.delete("notes/hello.md", rel)
        self.assertFalse(Path(self.root, rel).exists())
        with self.assertRaises(NodeNotFoundError):
            self.nb.assets.delete("notes/hello.md", rel)

    def test_delete_rejects_path_escape(self):
        with self.assertRaises(PathEscapeError):
            self.nb.assets.delete("notes/hello.md", "../outside.txt")

    def test_invalid_asset_name(self):
        with self.assertRaises(InvalidNameError):
            self.nb.assets.insert_bytes("notes/hello.md", "../evil", b"x")
        with self.assertRaises(InvalidNameError):
            self.nb.assets.insert_bytes("notes/hello.md", "..", b"x")

    def test_attachments_dedupe_and_metadata(self):
        src = self._source("file.txt", b"attachment")
        first = self.nb.assets.insert_attachment("notes/hello.md", src)
        second = self.nb.assets.insert_attachment("notes/hello.md", src)
        self.assertEqual(first, "file.txt")
        self.assertEqual(second, "file_1.txt")
        self.assertEqual(
            self.nb.assets.list_attachments("notes/hello.md"),
            ["file.txt", "file_1.txt"])

        cfg_path = Path(self.root, "vx_notebook", "contents", "notes", "vx.json")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        record = next(item for item in cfg["files"] if item["id"] == FILE_ID)
        self.assertEqual(record["attachments"], ["file.txt", "file_1.txt"])

    def test_attachment_removal_omits_empty_key(self):
        src = self._source("file.txt", b"attachment")
        name = self.nb.assets.insert_attachment("notes/hello.md", src)
        self.nb.assets.delete_attachment("notes/hello.md", name)
        self.assertEqual(self.nb.assets.list_attachments("notes/hello.md"), [])
        self.assertFalse(
            Path(self.root, f"notes/vx_assets/{FILE_ID}/{name}").exists())

        cfg_path = Path(self.root, "vx_notebook", "contents", "notes", "vx.json")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        record = next(item for item in cfg["files"] if item["id"] == FILE_ID)
        self.assertNotIn("attachments", record)

    def test_read_only_rejects_assets(self):
        ctx = make_context(self.tmp, recover=False)
        self.addCleanup(ctx.close)
        nb = ctx.open_notebook(self.root, read_only=True)
        src = self._source("pic.png", b"data")
        with self.assertRaises(ReadOnlyError):
            nb.assets.insert("notes/hello.md", src)


class RawAssetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_raw(self.tmp)
        self.ctx = make_context(self.tmp, recover=False)
        self.addCleanup(self.ctx.close)
        self.nb = self.ctx.open_notebook(self.root)
        self.src = os.path.join(self.tmp, "src.bin")
        Path(self.src).write_bytes(b"raw asset")

    def test_raw_assets_work(self):
        rel = self.nb.assets.insert("a.md", self.src)
        self.assertTrue(rel.startswith("vx_assets/"))
        self.assertTrue(rel.endswith("/src.bin"))
        self.assertEqual(Path(self.root, rel).read_bytes(), b"raw asset")
        self.nb.assets.delete("a.md", rel)
        self.assertFalse(Path(self.root, rel).exists())

    def test_raw_attachments_unsupported(self):
        with self.assertRaises(UnsupportedError):
            self.nb.assets.insert_attachment("a.md", self.src)
        with self.assertRaises(UnsupportedError):
            self.nb.assets.list_attachments("a.md")
        with self.assertRaises(UnsupportedError):
            self.nb.folders.get_attachments("a.md")


class FileTypeTest(unittest.TestCase):
    def test_by_suffix(self):
        config = FileTypesConfig.defaults()
        self.assertEqual(config.by_suffix("md").name, "Markdown")
        self.assertEqual(config.by_suffix(".markdown").name, "Markdown")
        self.assertEqual(config.by_suffix("txt").name, "Text")
        self.assertEqual(config.by_suffix("xyz").name, "Others")

    def test_by_name_and_validation(self):
        config = FileTypesConfig.defaults()
        self.assertEqual(config.by_name("Markdown").display_name, "Markdown")
        self.assertIsNone(config.by_name("Missing"))
        with self.assertRaises(ValueError):
            config.set_types([config.types[0], config.types[0]])


if __name__ == "__main__":
    unittest.main()
