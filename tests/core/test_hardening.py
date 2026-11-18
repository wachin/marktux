"""Phase 8 acceptance: hardening.

Covers the guarantees that only show up under stress or on a bad filesystem:

* concurrent saves never tear the file and never leave temp files behind;
* a stale *or missing* ``schema_version`` rebuilds the derived cache while the
  device-local ``notebook_metadata`` rows survive;
* a write that dies before the rename leaves the original file valid, and a
  crashed write's leftover temp file can be swept;
* the legacy ``~/.cache/marktux/meta.db`` is never opened;
* a filesystem that refuses WAL falls back to ``DELETE`` + ``synchronous=FULL``.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from core import BufferState, CoreContext, ContentQuery, SearchScope
from core import fsutil
from core.metadata import SCHEMA_VERSION, SqliteMetadataStore, StoreFolder

from .helpers import make_bundled, make_context


class ConcurrentSaveTest(unittest.TestCase):
    """Twenty threads saving one buffer must never produce a torn file."""

    THREADS = 20
    ROUNDS = 5
    REPEATS = 200

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.ctx = make_context(self.tmp, recover=False)
        self.addCleanup(self.ctx.close)
        self.root = make_bundled(self.tmp, "Concurrent")
        self.nb = self.ctx.open_notebook(self.root)
        self.note_dir = os.path.join(self.root, "notes")
        self.note_path = os.path.join(self.note_dir, "hello.md")

    def _payload(self, index: int, round_no: int) -> str:
        marker = f"thread-{index:02d}-round-{round_no}"
        # Big enough that a non-atomic write would very likely be observed
        # half-written by one of the concurrent readers/savers.
        body = "\n".join(f"{marker} line {n}" for n in range(self.REPEATS))
        return f"# {marker}\n\n{body}\n\nEND-{marker}\n"

    def test_concurrent_saves_never_tear_and_leave_no_temps(self):
        buffer = self.ctx.buffers.open(self.nb.id, "notes/hello.md")
        buffer.content()  # force the lazy load before racing
        payloads = [self._payload(i, r)
                    for i in range(self.THREADS) for r in range(self.ROUNDS)]
        original = pathlib.Path(self.note_path).read_text(encoding="utf-8")

        barrier = threading.Barrier(self.THREADS)
        errors: list[BaseException] = []
        lock = threading.Lock()
        observed: list[str] = []
        stop = threading.Event()

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                for round_no in range(self.ROUNDS):
                    payload = self._payload(index, round_no)
                    buffer.set_content(payload)
                    buffer.save()
            except BaseException as error:  # noqa: BLE001 - surfaced below
                with lock:
                    errors.append(error)

        def reader() -> None:
            # Sample the file while it is being replaced. With an atomic rename
            # every sample is a whole snapshot; a truncate+write would expose
            # empty or half-written states here.
            while not stop.is_set():
                try:
                    data = pathlib.Path(self.note_path).read_bytes()
                except FileNotFoundError:
                    observed.append("<missing>")
                    continue
                observed.append(data.decode("utf-8", errors="replace"))
                time.sleep(0.0002)

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        threads = [
            threading.Thread(target=worker, args=(index,), daemon=True)
            for index in range(self.THREADS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), "a save thread hung")
        stop.set()
        reader_thread.join(timeout=10)

        self.assertEqual(errors, [], f"concurrent save raised: {errors!r}")
        self.assertIsNot(buffer.state, BufferState.SAVE_FAILED)

        allowed = set(payloads) | {original}
        torn = [sample for sample in observed if sample not in allowed]
        self.assertEqual(torn, [], f"observed {len(torn)} torn read(s)")
        self.assertGreaterEqual(len(observed), 20,
                                "the reader barely sampled the file")

        # Every os.replace is atomic, so the file must be exactly one complete
        # snapshot - never empty, never a mixture of two writers.
        raw = pathlib.Path(self.note_path).read_bytes()
        text = raw.decode("utf-8")
        self.assertIn(text, payloads, "the note is a torn write")

        # No temp file survived any of the racing atomic writes.
        leftovers = [name for name in os.listdir(self.note_dir)
                     if name.startswith(".") and ".tmp-" in name]
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")

        # A final single-threaded save settles the dirty flag.
        buffer.set_content(payloads[0])
        buffer.save()
        self.assertFalse(buffer.modified)
        self.assertEqual(pathlib.Path(self.note_path).read_text(encoding="utf-8"),
                         payloads[0])


class SchemaRebuildTest(unittest.TestCase):
    """A stale or absent schema version must not be trusted."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db = os.path.join(self.tmp, "metadata.db")

    def _seed(self) -> None:
        store = SqliteMetadataStore(self.db, journal_mode="delete")
        store.open()
        self.addCleanup(store.close)
        store.create_folder(StoreFolder(id="root-uuid", parent_id="", name=""))
        store.create_folder(
            StoreFolder(id="sub-uuid", parent_id="root-uuid", name="notes")
        )
        store.set_meta("last_sync_utc", "1700000000000")
        store.close()

    def _tamper_version(self, version: int | None) -> None:
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DELETE FROM schema_version")
            if version is not None:
                conn.execute("INSERT INTO schema_version VALUES (?)", (version,))
            conn.commit()
        finally:
            conn.close()

    def _assert_rebuilt(self) -> None:
        store = SqliteMetadataStore(self.db, journal_mode="delete")
        store.open()
        self.addCleanup(store.close)
        self.assertEqual(store.counts()["folders"], 0,
                         "stale structure should have been dropped")
        self.assertIsNone(store.get_folder("root-uuid"))
        # Device-local values cannot be re-derived, so they must survive.
        self.assertEqual(store.get_meta("last_sync_utc"), "1700000000000")
        row = store._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()
        self.assertEqual(int(row[0]), SCHEMA_VERSION)

    def test_stale_version_rebuilds_structure(self):
        self._seed()
        self._tamper_version(SCHEMA_VERSION - 1)
        self._assert_rebuilt()

    def test_missing_version_rebuilds_structure(self):
        self._seed()
        self._tamper_version(None)
        self._assert_rebuilt()

    def test_newer_version_rebuilds_structure(self):
        self._seed()
        self._tamper_version(SCHEMA_VERSION + 1)
        self._assert_rebuilt()

    def test_notebook_rebuilds_from_vx_json_after_version_loss(self):
        root = make_bundled(self.tmp, "Rebuild")
        ctx = make_context(self.tmp, recover=False)
        self.addCleanup(ctx.close)
        nb = ctx.open_notebook(root)
        nb.store.set_meta("last_sync_utc", "42")
        db_path = nb.db_path
        ctx.close_notebook(nb.id)

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM schema_version")
            conn.commit()
        finally:
            conn.close()

        reopened = ctx.open_notebook(root)
        children = reopened.folders.list_children("")
        self.assertEqual([node.name for node in children.folders], ["notes"],
                         "structure should be rebuilt from vx.json")
        self.assertEqual(reopened.store.get_meta("last_sync_utc"), "42")


class CrashSafetyTest(unittest.TestCase):
    """A write that dies before the rename must leave the original intact."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.path = os.path.join(self.tmp, "note.md")
        pathlib.Path(self.path).write_text("original\n", encoding="utf-8")

    def test_failed_replace_keeps_original_and_removes_temp(self):
        with mock.patch("core.fsutil.os.replace",
                        side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                fsutil.atomic_write_bytes(self.path, b"replacement\n")

        self.assertEqual(pathlib.Path(self.path).read_bytes(), b"original\n")
        self.assertEqual(os.listdir(self.tmp), ["note.md"],
                         "the temp file should be removed on failure")

    def test_crash_leftover_temp_is_swept(self):
        # Simulate a process killed between mkstemp() and os.replace().
        stale = os.path.join(self.tmp, ".note.md.tmp-crashed")
        fresh = os.path.join(self.tmp, ".note.md.tmp-running")
        pathlib.Path(stale).write_bytes(b"partial")
        pathlib.Path(fresh).write_bytes(b"partial")
        old = time.time() - 7200
        os.utime(stale, (old, old))

        removed = fsutil.sweep_temp_files(self.tmp, older_than_s=3600)

        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh),
                        "a temp file still being written must be kept")
        self.assertEqual(pathlib.Path(self.path).read_bytes(), b"original\n")

    def test_sweep_ignores_unrelated_dotfiles(self):
        keep = os.path.join(self.tmp, ".hidden")
        pathlib.Path(keep).write_text("x", encoding="utf-8")
        old = time.time() - 7200
        os.utime(keep, (old, old))

        self.assertEqual(fsutil.sweep_temp_files(self.tmp), 0)
        self.assertTrue(os.path.exists(keep))


class TempSweepIntegrationTest(unittest.TestCase):
    """Crash leftovers are cleaned where the user browses, not just by hand."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.ctx = make_context(self.tmp, recover=False)
        self.addCleanup(self.ctx.close)
        self.root = make_bundled(self.tmp, "Sweep")
        self.nb = self.ctx.open_notebook(self.root)

    def _age(self, path: str, seconds: int = 7200) -> None:
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_listing_a_folder_sweeps_stale_temps(self):
        note_dir = os.path.join(self.root, "notes")
        stale = os.path.join(note_dir, ".hello.md.tmp-crashed")
        fresh = os.path.join(note_dir, ".hello.md.tmp-running")
        pathlib.Path(stale).write_bytes(b"partial")
        pathlib.Path(fresh).write_bytes(b"partial")
        self._age(stale)

        self.nb.folders.list_children("notes")

        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh))
        self.assertEqual(
            pathlib.Path(os.path.join(note_dir, "hello.md")).read_text(
                encoding="utf-8"),
            "# Hello\n\nBody text.\n",
        )

    def test_open_sweeps_stale_temps_in_the_notebook_root(self):
        self.ctx.close_notebook(self.nb.id)
        stale = os.path.join(self.root, ".config.json.tmp-crashed")
        pathlib.Path(stale).write_bytes(b"partial")
        self._age(stale)

        self.ctx.open_notebook(self.root)

        self.assertFalse(os.path.exists(stale))


class LegacyCacheTest(unittest.TestCase):
    """The old flat ``meta.db`` cache must never be opened."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.local = os.path.join(self.tmp, "local")
        self.legacy = os.path.join(self.local, "meta.db")
        os.makedirs(self.local, exist_ok=True)
        pathlib.Path(self.legacy).write_bytes(b"legacy-cache-sentinel")

        self.opened: list[str] = []
        real_connect = sqlite3.connect

        def recording_connect(database, *args, **kwargs):
            self.opened.append(os.path.abspath(str(database)))
            return real_connect(database, *args, **kwargs)

        patcher = mock.patch("sqlite3.connect", side_effect=recording_connect)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.ctx = CoreContext(
            app_data_dir=os.path.join(self.tmp, "app"),
            local_data_dir=self.local,
            recover_session=False,
        ).open()
        self.addCleanup(self.ctx.close)

    def test_only_per_notebook_databases_are_opened(self):
        root = make_bundled(self.tmp, "Legacy")
        nb = self.ctx.open_notebook(root)
        buffer = self.ctx.buffers.open(nb.id, "notes/hello.md")
        buffer.set_content("# saved\n")
        buffer.save()
        nb.tags.create("topic")
        nb.search.search(ContentQuery(pattern="saved", scope=SearchScope()))

        self.assertTrue(self.opened, "the notebook cache should be opened")
        self.assertNotIn(os.path.abspath(self.legacy), self.opened)
        self.assertFalse(
            [path for path in self.opened
             if os.path.basename(path) == "meta.db"],
            "the legacy flat cache must never be used",
        )
        expected = os.path.join(self.local, "notebooks", nb.id, "metadata.db")
        self.assertIn(os.path.abspath(expected), self.opened)
        for path in self.opened:
            self.assertTrue(
                path.startswith(os.path.join(self.local, "notebooks") + os.sep),
                f"unexpected database opened: {path}",
            )
        self.assertEqual(pathlib.Path(self.legacy).read_bytes(),
                         b"legacy-cache-sentinel")


class _FakeRow:
    def __init__(self, value: str) -> None:
        self._value = value

    def __getitem__(self, index: int) -> str:
        return self._value


class _FakeCursor:
    def __init__(self, value: str) -> None:
        self._value = value

    def fetchone(self) -> _FakeRow:
        return _FakeRow(self._value)


class _WalRefusingConnection:
    """Delegates to a real connection but pretends WAL is unavailable."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value) -> None:
        setattr(object.__getattribute__(self, "_conn"), name, value)

    def execute(self, sql: str, *args):
        if sql.strip().upper().startswith("PRAGMA JOURNAL_MODE = WAL"):
            return _FakeCursor("delete")
        return object.__getattribute__(self, "_conn").execute(sql, *args)


class JournalFallbackTest(unittest.TestCase):
    """SMB/NFS cannot do WAL; we must still open with full durability."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db = os.path.join(self.tmp, "metadata.db")

    def test_wal_refusal_falls_back_to_delete_with_full_sync(self):
        real_connect = sqlite3.connect

        def refusing_connect(database, *args, **kwargs):
            return _WalRefusingConnection(real_connect(database, *args, **kwargs))

        with mock.patch("sqlite3.connect", side_effect=refusing_connect):
            store = SqliteMetadataStore(self.db, journal_mode="wal")
            store.open()
        self.addCleanup(store.close)

        self.assertEqual(store.journal_mode, "delete")
        self.assertEqual(store._requested_journal, "wal")
        synchronous = store._conn.execute("PRAGMA synchronous").fetchone()[0]
        self.assertEqual(int(synchronous), 2, "DELETE fallback needs FULL sync")

        # The database must still be fully usable after the downgrade.
        store.create_folder(StoreFolder(id="root-uuid", parent_id="", name=""))
        self.assertEqual(store.counts()["folders"], 1)

    def test_normal_open_reports_a_usable_journal_mode(self):
        store = SqliteMetadataStore(self.db, journal_mode="wal")
        store.open()
        self.addCleanup(store.close)

        self.assertIn(store.journal_mode, ("wal", "delete", "truncate",
                                          "persist", "memory", "off"))
        store.create_folder(StoreFolder(id="root-uuid", parent_id="", name=""))
        self.assertEqual(store.counts()["folders"], 1)


if __name__ == "__main__":
    unittest.main()
