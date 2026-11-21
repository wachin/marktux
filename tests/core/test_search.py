"""Phase 5 acceptance: content/file search, streaming, encodings, backends.

Behavioural specs ported from vxcore's ``test_search.cpp``.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from marktux.core import (
    CancellationToken,
    ContentQuery,
    CoreConfig,
    CoreContext,
    InvalidQueryError,
    FileQuery,
    SearchOption,
    SearchScope,
)

from .helpers import make_bundled

STREAM_CFG = CoreConfig.from_json({"search": {"backends": ["simple"]}})


def _ctx(tmp, *, backends=None, config=None, name="app"):
    return CoreContext(
        app_data_dir=os.path.join(tmp, name + "-app"),
        local_data_dir=os.path.join(tmp, name + "-local"),
        recover_session=False, config=config, search_backends=backends,
    ).open()


def _note(root, rel, text, *, encoding="utf-8", index=True):
    """Write a note and, if requested, add it to the index of its folder.

    Creates any missing intermediate folder configs so nested notes (e.g.
    ``notes/sub/b.md``) are traversable by ``iterate_all_files()``.
    """
    path = Path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding) if isinstance(text, str) else text)
    if not index:
        return
    parts = rel.split("/")
    name = parts[-1]
    folders = parts[:-1]
    contents = Path(root, "vx_notebook", "contents")

    # Ensure each ancestor folder has a vx.json and is registered in its parent.
    for depth in range(1, len(folders) + 1):
        seg = folders[:depth]
        cfg_path = contents.joinpath(*seg, "vx.json")
        parent_path = (contents / "vx.json" if depth == 1
                       else contents.joinpath(*seg[:-1], "vx.json"))
        if not cfg_path.exists():
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            child = {
                "id": f"folder-{'-'.join(seg)}", "name": seg[-1],
                "createdUtc": 1, "modifiedUtc": 1,
                "metadata": {}, "files": [], "folders": [],
            }
            cfg_path.write_text(json.dumps(child), encoding="utf-8")
        parent_cfg = json.loads(parent_path.read_text(encoding="utf-8"))
        if seg[-1] not in parent_cfg.setdefault("folders", []):
            parent_cfg["folders"].append(seg[-1])
            parent_path.write_text(json.dumps(parent_cfg), encoding="utf-8")

    cfg_path = contents.joinpath(*folders, "vx.json") if folders else contents / "vx.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg.setdefault("files", []).append({
        "id": f"file-{'-'.join(parts)}", "name": name,
        "createdUtc": 1, "modifiedUtc": 1, "metadata": {},
    })
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")


class ContentSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_bundled(self.tmp, with_note=False)
        _note(self.root, "notes/alpha.md", "prefix\nhello world\nsuffix\n")
        _note(self.root, "notes/beta.md", "line 1\nHello World\n")
        self.ctx = _ctx(self.tmp, backends=["simple"])
        self.addCleanup(self.ctx.close)
        self.nb = self.ctx.open_notebook(self.root)
        self.scope = SearchScope(folder_path="notes")

    def query(self, pattern, **kwargs):
        return ContentQuery(pattern=pattern, scope=self.scope, **kwargs)

    def test_basic_case_insensitive(self):
        result = self.nb.search.search(self.query("hello"))
        self.assertEqual(len(result.results), 2)
        alpha = next(r for r in result.results if r.path.endswith("alpha.md"))
        match = alpha.matches[0]
        self.assertEqual((match.line_number, match.column_start, match.column_end),
                         (2, 0, 5))
        self.assertEqual(match.line_text, "hello world")
        self.assertFalse(result.truncated)

    def test_case_sensitive(self):
        result = self.nb.search.search(
            self.query("Hello", options=SearchOption.CASE_SENSITIVE))
        self.assertEqual([r.path for r in result.results], ["notes/beta.md"])

    def test_whole_word(self):
        Path(self.root, "notes", "whole.md").write_text(
            "testing test case tester\n", encoding="utf-8")
        _note(self.root, "notes/whole2.md", "", index=False)
        # Replace alpha content to isolate whole-word semantics.
        Path(self.root, "notes", "alpha.md").write_text(
            "testing test case tester\n", encoding="utf-8")
        result = self.nb.search.search(
            self.query("test", options=SearchOption.WHOLE_WORD))
        matches = [m for r in result.results for m in r.matches]
        self.assertTrue(all(m.column_start == 8 and m.column_end == 12
                            for m in matches))

    def test_regex_and_multiple_matches(self):
        Path(self.root, "notes", "alpha.md").write_text(
            "id 123\nabc\nline 45 and 678\n", encoding="utf-8")
        result = self.nb.search.search(
            self.query(r"\d+", options=SearchOption.REGEX),
            files=["notes/alpha.md"])
        matches = [m for r in result.results for m in r.matches]
        self.assertEqual([(m.column_start, m.column_end) for m in matches],
                         [(3, 6), (5, 7), (12, 15)])

    def test_multiple_matches_per_line_literal(self):
        Path(self.root, "notes", "alpha.md").write_text("aaa\n", encoding="utf-8")
        result = self.nb.search.search(self.query("a"))
        matches = next(r for r in result.results if r.path.endswith("alpha.md")).matches
        self.assertEqual([(m.column_start, m.column_end) for m in matches],
                         [(0, 1), (1, 2), (2, 3)])

    def test_undecodable_bytes_use_replacement(self):
        Path(self.root, "notes", "alpha.md").write_bytes(
            b"\xff\xfe needle \x80 text\n")
        result = self.nb.search.search(self.query("needle"))
        self.assertTrue(any(r.path.endswith("alpha.md")
                            for r in result.results))

    def test_empty_pattern(self):
        self.assertEqual(self.nb.search.search(self.query("")).results, ())

    def test_no_matches(self):
        result = self.nb.search.search(self.query("absent-word"))
        self.assertEqual(result.results, ())
        self.assertFalse(result.truncated)

    def test_invalid_regex_raises(self):
        with self.assertRaises(InvalidQueryError):
            self.nb.search.search(self.query("[", options=SearchOption.REGEX))

    def test_max_results_truncates(self):
        for i in range(5):
            _note(self.root, f"notes/hit{i}.md", "hit\nhit\nhit\n")
        result = self.nb.search.search(self.query("hit", max_results=4))
        total = sum(len(r.matches) for r in result.results)
        self.assertEqual(total, 4)
        self.assertTrue(result.truncated)

    def test_first_match_per_file(self):
        Path(self.root, "notes", "alpha.md").write_text(
            "needle one\nneedle two\nneedle three\n", encoding="utf-8")
        result = self.nb.search.search(
            self.query("needle", options=SearchOption.FIRST_MATCH_PER_FILE))
        alpha = next(r for r in result.results if r.path.endswith("alpha.md"))
        self.assertEqual(len(alpha.matches), 1)
        self.assertEqual(alpha.matches[0].line_number, 1)

    def test_exclude_patterns(self):
        Path(self.root, "notes", "alpha.md").write_text(
            "skip needle\nkeep needle\n", encoding="utf-8")
        result = self.nb.search.search(
            ContentQuery("needle", exclude_patterns=["skip"], scope=self.scope))
        matches = [m for r in result.results for m in r.matches]
        self.assertEqual([m.line_number for m in matches], [2])

    def test_unicode_columns_are_codepoints(self):
        Path(self.root, "notes", "alpha.md").write_bytes(
            "😀needle needle\n".encode("utf-8"))
        result = self.nb.search.search(self.query("needle"))
        matches = next(r for r in result.results if r.path.endswith("alpha.md")).matches
        # Python indexes Unicode codepoints (emoji = 1), unlike vxcore's UTF-16.
        self.assertEqual([(m.column_start, m.column_end) for m in matches],
                         [(1, 7), (8, 14)])


class EncodingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_bundled(self.tmp, with_note=False)
        _note(self.root, "notes/utf8.md", "你好 UTF-8\n", encoding="utf-8")
        _note(self.root, "notes/gb.md", "你好 GB18030\n", encoding="gb18030")
        self.addCleanup(lambda: None)

    def test_both_encodings(self):
        ctx = _ctx(self.tmp, backends=["simple"], name="both")
        self.addCleanup(ctx.close)
        nb = ctx.open_notebook(self.root)
        result = nb.search.search(ContentQuery("你好", scope=SearchScope(folder_path="notes")))
        self.assertEqual(sorted(r.name for r in result.results), ["gb.md", "utf8.md"])

    def test_utf8_only_skips_gb18030(self):
        cfg = CoreConfig.from_json({"search": {"backends": ["simple"],
                                               "encodings": ["UTF-8"]}})
        ctx = _ctx(self.tmp, config=cfg, name="utf8only")
        self.addCleanup(ctx.close)
        nb = ctx.open_notebook(self.root)
        result = nb.search.search(ContentQuery("你好", scope=SearchScope(folder_path="notes")))
        self.assertEqual([r.name for r in result.results], ["utf8.md"])


class StreamingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_bundled(self.tmp, with_note=False)
        for i in range(130):
            hit = "needle here\n" if i % 10 == 0 else "nothing\n"
            _note(self.root, f"notes/f{i:03}.md", hit)
        self.ctx = _ctx(self.tmp, backends=["simple"], name="stream")
        self.addCleanup(self.ctx.close)
        self.nb = self.ctx.open_notebook(self.root)

    def test_batches_include_empty_chunks(self):
        # 130 indexed files → 3 chunks (64/64/2); the last chunk has no hits.
        batches = self.nb.search.search_stream(
            ContentQuery("needle", scope=SearchScope(folder_path="notes")),
            batch_size=64)
        self.assertEqual(len(batches), 3)
        self.assertEqual([len(b.results) for b in batches], [7, 6, 0])
        self.assertEqual(batches[0].total, len(batches))
        self.assertEqual(sum(b.index for b in batches),
                         sum(range(len(batches))))

    def test_stream_blob_parity(self):
        query = ContentQuery("needle", scope=SearchScope(folder_path="notes"))
        blob = self.nb.search.search(query)
        batches = self.nb.search.search_stream(query, batch_size=1)
        reassembled = [r for b in batches for r in b.results]
        self.assertEqual([(r.path, r.matches) for r in reassembled],
                         [(r.path, r.matches) for r in blob.results])

    def test_cancel_before_start(self):
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(Exception):
            self.nb.search.search_stream(
                ContentQuery("needle", scope=SearchScope(folder_path="notes")),
                batch_size=1, cancel=token)

    def test_cancel_between_chunks(self):
        token = CancellationToken()
        seen = []

        def on_batch(batch):
            seen.append(batch.index)
            token.cancel()

        with self.assertRaises(Exception):
            self.nb.search.search_stream(
                ContentQuery("needle", scope=SearchScope(folder_path="notes")),
                batch_size=1, cancel=token, on_batch=on_batch)
        self.assertEqual(seen, [0])

    def test_cancel_within_chunk(self):
        # The simple backend checks the token before each file, so cancellation
        # takes effect inside a chunk, not only between chunks.
        from marktux.core.search import SimpleSearchBackend, _File

        backend = SimpleSearchBackend()
        query = ContentQuery("needle")
        backend.prepare(query)
        token = CancellationToken()
        files = [_File(path=str(i), file_id=str(i), name=str(i), tags=(),
                       text="needle\n") for i in range(5)]
        seen = []

        def on_file(item):
            seen.append(item.path)
            if len(seen) == 2:
                token.cancel()

        with self.assertRaises(Exception):
            backend.matches(files, query, cancel=token, on_file=on_file)
        self.assertEqual(seen, ["0", "1"])


class BackendParityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_bundled(self.tmp, with_note=False)
        _note(self.root, "notes/alpha.md", "hello 世界\nneedle 123\n")
        _note(self.root, "notes/beta.md", "nothing\n")

    def test_rg_matches_simple(self):
        if shutil.which("rg") is None:
            self.skipTest("ripgrep not installed")
        simple = _ctx(self.tmp, backends=["simple"], name="simple")
        rg = _ctx(self.tmp, backends=["rg"], name="rg")
        self.addCleanup(simple.close)
        self.addCleanup(rg.close)
        query = ContentQuery("needle", scope=SearchScope(folder_path="notes"))
        a = simple.open_notebook(self.root).search.search(query)
        b = rg.open_notebook(self.root).search.search(query)
        self.assertEqual([(r.path, r.matches) for r in a.results],
                         [(r.path, r.matches) for r in b.results])

    def test_missing_rg_falls_back(self):
        seen = []
        ctx = _ctx(self.tmp, backends=["rg"], name="fallback")
        self.addCleanup(ctx.close)
        ctx.events.subscribe("search.backend_fallback", lambda n, p: seen.append(p))
        # Force the rg backend to appear unavailable.
        import marktux.core.search as search_mod
        original = search_mod.RgSearchBackend.available.fget
        try:
            search_mod.RgSearchBackend.available = property(lambda self: False)
            nb = ctx.open_notebook(self.root)
            self.assertEqual(nb.search._active.name, "simple")
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0]["to"], "simple")
        finally:
            search_mod.RgSearchBackend.available = property(original)


class ScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_bundled(self.tmp, with_note=False)
        _note(self.root, "notes/a.md", "needle\n")
        # add a subfolder note
        os.makedirs(os.path.join(self.root, "notes", "sub"), exist_ok=True)
        _note(self.root, "notes/sub/b.md", "needle\n")
        self.ctx = _ctx(self.tmp, backends=["simple"], name="scope")
        self.addCleanup(self.ctx.close)
        self.nb = self.ctx.open_notebook(self.root)

    def test_non_recursive_scope(self):
        result = self.nb.search.search(ContentQuery(
            "needle", scope=SearchScope(folder_path="notes", recursive=False)))
        self.assertEqual([r.path for r in result.results], ["notes/a.md"])

    def test_exclude_path_pattern(self):
        result = self.nb.search.search(ContentQuery(
            "needle", scope=SearchScope(folder_path="notes",
                                       exclude_path_patterns=["b.md"])))
        self.assertEqual([r.path for r in result.results], ["notes/a.md"])

    def test_find_files_by_name(self):
        files = self.nb.search.find_files(FileQuery(pattern="a.md"))
        self.assertTrue(any(f["name"] == "a.md" for f in files))
