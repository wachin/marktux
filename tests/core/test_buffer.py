"""Phase 2 acceptance: buffers (lazy load, save/dirty, external-change, backup).

Behavioural specs ported from vxcore's buffer tests. mtime is manipulated
explicitly because vxcore (and fsutil) use millisecond stamps and two writes in
the same millisecond would otherwise be indistinguishable.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from core import (
    BufferState,
    CoreContext,
    FileSource,
    LineEnding,
    MemorySource,
    SaveError,
)

from .helpers import make_context


def _bump_mtime(path: str, delta_ns: int = 5_000_000) -> None:
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + delta_ns))


class BufferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.ctx = make_context(self.tmp, recover=False)
        self.addCleanup(self.ctx.close)
        self.nb = self.ctx.notebooks.create(os.path.join(self.tmp, "nb"), "NB")

    def _note(self, name="note.md", text="# Title\n\nHello\n") -> str:
        path = os.path.join(self.nb.root, name)
        pathlib.Path(path).write_text(text, encoding="utf-8")
        return path

    # -- lazy load / dedupe -------------------------------------------------

    def test_open_is_lazy_and_deduplicated(self):
        self._note()
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        self.assertFalse(buf.content_loaded)
        self.assertEqual(buf.revision, 0)
        self.assertFalse(buf.modified)
        self.assertIs(buf, self.ctx.buffers.open(self.nb.id, "note.md"))

    def test_content_loads_lazily_and_preserves_text(self):
        self._note(text="# Title\r\n\r\nHello\r\n")  # CRLF
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        text = buf.content()
        self.assertTrue(buf.content_loaded)
        self.assertEqual(text, "# Title\n\nHello\n")  # normalised for QTextEdit
        self.assertEqual(buf.line_ending, LineEnding.CRLF)
        self.assertIsNone(self.ctx.buffers.find(self.nb.id, "missing.md"))

    def test_open_abs_resolves_into_notebook(self):
        path = self._note()
        buf = self.ctx.buffers.open_abs(path)
        self.assertEqual(buf.notebook_id, self.nb.id)
        self.assertEqual(buf.file_path, "note.md")

    # -- dirty / save -------------------------------------------------------

    def test_set_content_marks_dirty_and_bumps_revision(self):
        self._note()
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        buf.content()
        rev = buf.revision
        buf.set_content("new\n")
        self.assertTrue(buf.modified)
        self.assertEqual(buf.revision, rev + 1)
        self.assertTrue(buf.info().can_save)

    def test_save_writes_exact_bytes_and_clears_dirty(self):
        path = self._note()
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        buf.content()
        buf.set_content("# Title\n\nHello World\n")
        rev = buf.revision
        buf.save()
        self.assertEqual(pathlib.Path(path).read_bytes(),
                         b"# Title\n\nHello World\n")
        self.assertFalse(buf.modified)
        self.assertEqual(buf.revision, rev + 1)
        self.assertEqual(buf.state, BufferState.NORMAL)

    def test_save_preserves_bom_and_eol(self):
        path = self._note()
        raw = "\ufeff# T\r\nline\r\n".encode("utf-8")
        pathlib.Path(path).write_bytes(raw)
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        buf.content()
        self.assertEqual(buf.encoding, "utf-8")
        self.assertEqual(buf._bom, b"\xef\xbb\xbf")
        self.assertEqual(buf.line_ending, LineEnding.CRLF)
        buf.set_content("# T\r\nchanged\r\n")
        buf.save()
        self.assertEqual(pathlib.Path(path).read_bytes(),
                         "\ufeff# T\r\nchanged\r\n".encode("utf-8"))

    def test_save_failure_keeps_old_bytes_state_and_no_temp(self):
        path = self._note()
        original = pathlib.Path(path).read_bytes()
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        buf.content()
        buf.set_content("should not be written\n")

        import core.fsutil as fsutil
        real_replace = fsutil.os.replace

        def fail_replace(src, dst):
            if os.path.abspath(dst) == os.path.abspath(path):
                raise OSError("simulated disk failure")
            return real_replace(src, dst)

        with mock.patch.object(fsutil.os, "replace", side_effect=fail_replace):
            with self.assertRaises(SaveError):
                buf.save()
        self.assertEqual(pathlib.Path(path).read_bytes(), original)
        self.assertTrue(buf.modified)
        self.assertEqual(buf.state, BufferState.SAVE_FAILED)
        leftovers = [n for n in os.listdir(os.path.dirname(path))
                     if ".tmp-" in n]
        self.assertEqual(leftovers, [])

    def test_read_only_buffer_refuses_save(self):
        self._note()
        buf = self.ctx.buffers.open(self.nb.id, "note.md", read_only=True)
        buf.content()
        buf.set_content("nope\n")
        with self.assertRaises(Exception):  # ReadOnlyError
            buf.save()

    # -- external change matrix --------------------------------------------

    def test_external_change_real_change_flags_and_keeps_flagging(self):
        path = self._note()
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        buf.content()
        pathlib.Path(path).write_text("totally different\n", encoding="utf-8")
        _bump_mtime(path)
        self.assertEqual(buf.check_external(), BufferState.FILE_CHANGED)
        stale = buf._stamp_ms
        # Stamp stays stale, so it keeps flagging on repeated checks.
        self.assertEqual(buf.check_external(), BufferState.FILE_CHANGED)
        self.assertEqual(buf._stamp_ms, stale)

    def test_identical_byte_change_is_benign_and_refreshes_stamp(self):
        path = self._note()
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        buf.content()
        old_stamp = buf._stamp_ms
        pathlib.Path(path).write_text("# Title\n\nHello\n", encoding="utf-8")
        _bump_mtime(path)
        self.assertEqual(buf.check_external(), BufferState.NORMAL)
        self.assertGreater(buf._stamp_ms, old_stamp)

    def test_eol_only_change_is_benign(self):
        path = self._note(text="a\nb\n")
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        buf.content()
        pathlib.Path(path).write_bytes(b"a\r\nb\r\n")
        _bump_mtime(path)
        self.assertEqual(buf.check_external(), BufferState.NORMAL)

    def test_delete_flags_file_missing(self):
        path = self._note()
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        buf.content()
        os.unlink(path)
        self.assertEqual(buf.check_external(), BufferState.FILE_MISSING)

    def test_external_check_skips_unloaded_buffer(self):
        path = self._note()
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        self.assertFalse(buf.content_loaded)
        pathlib.Path(path).write_text("changed\n", encoding="utf-8")
        _bump_mtime(path)
        self.assertEqual(buf.check_external(), BufferState.NORMAL)
        self.assertEqual(buf.state, BufferState.NORMAL)

    # -- reload / backup ----------------------------------------------------

    def test_reload_discards_unsaved_changes(self):
        path = self._note()
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        buf.content()
        buf.set_content("unsaved\n")
        buf.reload()
        self.assertFalse(buf.modified)
        self.assertEqual(buf.content(), "# Title\n\nHello\n")

    def test_backup_header_recover_and_discard(self):
        path = self._note()
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        buf.content()
        buf.set_content("version two\n")
        backup = buf.create_backup()
        self.assertTrue(os.path.isfile(backup))
        header = b"vnotex_backup_file " + path.encode("utf-8") + b"|"
        self.assertTrue(pathlib.Path(backup).read_bytes().startswith(header))
        buf.set_content("version three\n")
        buf.recover_backup()
        self.assertEqual(buf.content(), "version two\n")
        buf.discard_backup()
        self.assertFalse(buf.has_backup())

    def test_backup_and_save_with_non_ascii_path(self):
        path = self._note(name="café 笔记.md")
        buf = self.ctx.buffers.open(self.nb.id, "café 笔记.md")
        buf.content()
        buf.set_content("unicode path\n")
        buf.save()
        self.assertEqual(pathlib.Path(path).read_text(), "unicode path\n")
        self.assertTrue(buf.has_backup())

    # -- encoding failures / virtual ---------------------------------------

    def test_undecodable_buffer_is_read_only_and_lossy(self):
        path = self._note()
        pathlib.Path(path).write_bytes(b"\xff\xfe\x00not utf8\x80\x81")
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        buf.content()
        self.assertTrue(buf.lossy)
        self.assertTrue(buf.read_only)

    def test_virtual_memory_buffer(self):
        buf = self.ctx.buffers.open_virtual("scratch", "hello")
        self.assertTrue(buf.is_virtual)
        self.assertEqual(buf.content(), "hello")
        buf.set_content("changed")
        buf.save()  # in-memory write, no disk
        self.assertFalse(buf.modified)
        self.assertEqual(buf.source.read(), b"changed")
        self.assertEqual(self.ctx.buffers.session_records(), [])  # not persisted

    # -- watchdog / session -------------------------------------------------

    def test_check_external_changes_emits_only_on_transition(self):
        path = self._note()
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        buf.content()
        pathlib.Path(path).write_text("changed\n", encoding="utf-8")
        _bump_mtime(path)
        changes = self.ctx.buffers.check_external_changes()
        self.assertIn(buf.id, changes)
        self.assertEqual(changes[buf.id], BufferState.FILE_CHANGED)
        self.assertEqual(self.ctx.buffers.check_external_changes(), {})

    def test_buffer_session_roundtrip(self):
        self._note()
        buf = self.ctx.buffers.open(self.nb.id, "note.md")
        buf.content()
        self.ctx.prepare_shutdown()
        self.ctx.close()

        ctx2 = CoreContext(
            app_data_dir=os.path.join(self.tmp, "app"),
            local_data_dir=os.path.join(self.tmp, "local"),
            recover_session=True,
        ).open()
        self.addCleanup(ctx2.close)
        records = ctx2.config.session.buffers
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].file_path, "note.md")
        restored = ctx2.buffers.restore(records)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].content(), "# Title\n\nHello\n")


if __name__ == "__main__":
    unittest.main()
