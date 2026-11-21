"""Phase 6 acceptance: workspaces, session restore, preview filtering.

Behavioural specs ported from vxcore's ``test_workspace.cpp`` and
``test_session_persistence.cpp``.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from marktux.core import (
    BufferNotFoundError,
    BufferRecord,
    CoreContext,
    InvalidStateError,
    SessionConfig,
    WorkspaceConfig,
    WorkspaceNotFoundError,
    filter_session,
)

from .helpers import make_bundled, make_context

OTHER_ID = "33333333-3333-3333-3333-333333333333"


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_bundled(self.tmp)
        self._add_note("notes/other.md", "# Other\n\nBody.\n", OTHER_ID)
        self.ctx = make_context(self.tmp, recover=False)
        self.addCleanup(self.ctx.close)

    def _add_note(self, rel: str, text: str, file_id: str) -> None:
        path = Path(self.root, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        folder = "" if "/" not in rel else rel.rsplit("/", 1)[0]
        cfg_path = Path(self.root, "vx_notebook", "contents", folder, "vx.json")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.setdefault("files", []).append({
            "id": file_id, "name": rel.rsplit("/", 1)[-1],
            "createdUtc": 1, "modifiedUtc": 1, "metadata": {},
        })
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    def _reopen(self, *, recover: bool) -> CoreContext:
        ctx = CoreContext(
            app_data_dir=os.path.join(self.tmp, "app"),
            local_data_dir=os.path.join(self.tmp, "local"),
            recover_session=recover,
        ).open()
        self.addCleanup(ctx.close)
        return ctx

    def _session_file(self) -> dict:
        return json.loads(
            Path(self.tmp, "local", "vxsession.json").read_text(encoding="utf-8"))

    # -- workspace CRUD -----------------------------------------------------

    def test_workspace_crud_and_ordering(self):
        ws = self.ctx.workspaces.create("WS")
        self.assertEqual(self.ctx.workspaces.get(ws.id).name, "WS")

        self.ctx.workspaces.rename(ws.id, "Renamed")
        self.assertEqual(self.ctx.workspaces.get(ws.id).name, "Renamed")

        nb = self.ctx.open_notebook(self.root)
        b1 = self.ctx.buffers.open(nb.id, "notes/hello.md")
        b2 = self.ctx.buffers.open(nb.id, "notes/other.md")
        self.ctx.workspaces.add_buffer(ws.id, b1.id)
        self.ctx.workspaces.add_buffer(ws.id, b2.id)
        self.ctx.workspaces.set_current_buffer(ws.id, b2.id)
        self.ctx.workspaces.set_buffer_order(ws.id, [b2.id, b1.id])

        saved = self.ctx.workspaces.get(ws.id)
        self.assertEqual(saved.buffer_ids, [b2.id, b1.id])
        self.assertEqual(saved.current_buffer_id, b2.id)

        with self.assertRaises(InvalidStateError):
            self.ctx.workspaces.set_buffer_order(ws.id, [b1.id])
        with self.assertRaises(InvalidStateError):
            self.ctx.workspaces.set_current_buffer(ws.id, "missing")

        self.ctx.workspaces.delete(ws.id)
        with self.assertRaises(WorkspaceNotFoundError):
            self.ctx.workspaces.get(ws.id)

    def test_add_buffer_rejects_unopened_buffer(self):
        ws = self.ctx.workspaces.create("WS")
        with self.assertRaises(BufferNotFoundError):
            self.ctx.workspaces.add_buffer(ws.id, "not-open")

    def test_remove_buffer_closes_orphan(self):
        nb = self.ctx.open_notebook(self.root)
        b1 = self.ctx.buffers.open(nb.id, "notes/hello.md")
        ws = self.ctx.workspaces.create("WS")
        self.ctx.workspaces.add_buffer(ws.id, b1.id)

        self.ctx.workspaces.remove_buffer(ws.id, b1.id)
        with self.assertRaises(BufferNotFoundError):
            self.ctx.buffers.get(b1.id)

    def test_remove_buffer_keeps_modified_buffer_open(self):
        nb = self.ctx.open_notebook(self.root)
        b1 = self.ctx.buffers.open(nb.id, "notes/hello.md")
        b1.content()
        b1.set_content("dirty\n")
        ws = self.ctx.workspaces.create("WS")
        self.ctx.workspaces.add_buffer(ws.id, b1.id)

        self.ctx.workspaces.remove_buffer(ws.id, b1.id)
        self.assertTrue(self.ctx.buffers.get(b1.id).modified)

    # -- persistence --------------------------------------------------------

    def test_workspace_and_buffers_roundtrip(self):
        nb = self.ctx.open_notebook(self.root)
        buf = self.ctx.buffers.open(nb.id, "notes/hello.md")
        buf.content()
        ws = self.ctx.workspaces.create("Work")
        self.ctx.workspaces.add_buffer(ws.id, buf.id)
        self.ctx.workspaces.set_current_buffer(ws.id, buf.id)
        self.ctx.workspaces.set_current(ws.id)
        self.ctx.prepare_shutdown()
        self.ctx.close()

        ctx2 = self._reopen(recover=True)
        self.assertEqual([item.name for item in ctx2.workspaces.list()], ["Work"])
        restored = ctx2.workspaces.get(ws.id)
        self.assertEqual(restored.buffer_ids, [buf.id])
        self.assertEqual(restored.current_buffer_id, buf.id)
        self.assertEqual(ctx2.workspaces.current(), ws.id)
        # The persisted buffer id survives, so workspace references still work.
        reopened = ctx2.buffers.get(buf.id)
        self.assertEqual(reopened.content(), "# Hello\n\nBody text.\n")

    def test_recover_disabled_keeps_notebooks_but_not_buffers(self):
        nb = self.ctx.open_notebook(self.root)
        buf = self.ctx.buffers.open(nb.id, "notes/hello.md")
        ws = self.ctx.workspaces.create("Work")
        self.ctx.workspaces.add_buffer(ws.id, buf.id)
        self.ctx.prepare_shutdown()
        self.ctx.close()

        ctx2 = self._reopen(recover=False)
        self.assertEqual([info.id for info in ctx2.notebooks.list()], [nb.id])
        self.assertEqual(ctx2.buffers.list(), [])
        self.assertEqual(ctx2.workspaces.list(), [])

    def test_missing_file_skipped_and_current_falls_back(self):
        nb = self.ctx.open_notebook(self.root)
        b1 = self.ctx.buffers.open(nb.id, "notes/hello.md")
        b2 = self.ctx.buffers.open(nb.id, "notes/other.md")
        ws = self.ctx.workspaces.create("WS")
        self.ctx.workspaces.add_buffer(ws.id, b1.id)
        self.ctx.workspaces.add_buffer(ws.id, b2.id)
        self.ctx.workspaces.set_current_buffer(ws.id, b2.id)
        self.ctx.prepare_shutdown()
        self.ctx.close()

        os.unlink(os.path.join(self.root, "notes", "other.md"))

        ctx2 = self._reopen(recover=True)
        self.assertEqual(ctx2.buffers.get(b1.id).file_path, "notes/hello.md")
        with self.assertRaises(BufferNotFoundError):
            ctx2.buffers.get(b2.id)
        restored = ctx2.workspaces.get(ws.id)
        self.assertEqual(restored.buffer_ids, [b1.id])
        self.assertEqual(restored.current_buffer_id, b1.id)

    def test_renamed_file_found_by_file_id(self):
        nb = self.ctx.open_notebook(self.root)
        buf = self.ctx.buffers.open(nb.id, "notes/hello.md")
        buf.content()
        self.ctx.prepare_shutdown()
        self.ctx.close()

        # Rename on disk and in vx.json, keeping the UUID, then drop the
        # derived cache so it rebuilds from vx.json on the next open.
        old = Path(self.root, "notes", "hello.md")
        old.rename(Path(self.root, "notes", "renamed.md"))
        cfg_path = Path(self.root, "vx_notebook", "contents", "notes", "vx.json")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        for record in cfg["files"]:
            if record["name"] == "hello.md":
                record["name"] = "renamed.md"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        shutil.rmtree(Path(self.tmp, "local", "notebooks"), ignore_errors=True)

        ctx2 = self._reopen(recover=True)
        reopened = ctx2.buffers.get(buf.id)
        self.assertEqual(reopened.file_path, "notes/renamed.md")
        self.assertEqual(reopened.content(), "# Hello\n\nBody text.\n")

    def test_preview_workspace_filtered_and_current_rewritten(self):
        nb = self.ctx.open_notebook(self.root)
        b1 = self.ctx.buffers.open(nb.id, "notes/hello.md")
        b2 = self.ctx.buffers.open(nb.id, "notes/other.md")
        ordinary = self.ctx.workspaces.create("Ordinary")
        self.ctx.workspaces.add_buffer(ordinary.id, b1.id)
        preview = self.ctx.workspaces.create("Preview", is_preview=True)
        self.ctx.workspaces.add_buffer(preview.id, b2.id)
        self.ctx.workspaces.set_current(preview.id)

        self.ctx.prepare_shutdown()
        self.ctx.close()

        session = self._session_file()
        self.assertEqual([item["name"] for item in session["workspaces"]],
                         ["Ordinary"])
        self.assertEqual(session["currentWorkspaceId"], ordinary.id)
        self.assertEqual([item["id"] for item in session["buffers"]], [b1.id])

        ctx2 = self._reopen(recover=True)
        self.assertEqual([item.name for item in ctx2.workspaces.list()],
                         ["Ordinary"])
        self.assertEqual(ctx2.workspaces.current(), ordinary.id)
        with self.assertRaises(BufferNotFoundError):
            ctx2.buffers.get(b2.id)

    def test_shutdown_writes_session_once(self):
        nb = self.ctx.open_notebook(self.root)
        self.ctx.buffers.open(nb.id, "notes/hello.md")
        writes: list[int] = []
        original = self.ctx.config.save_session

        def counting() -> None:
            writes.append(1)
            original()

        self.ctx.config.save_session = counting  # type: ignore[method-assign]
        try:
            self.ctx.prepare_shutdown()
            self.ctx.prepare_shutdown()  # idempotent
            self.ctx.close()             # must not overwrite the snapshot
        finally:
            self.ctx.config.save_session = original  # type: ignore[method-assign]
        self.assertEqual(len(writes), 1)

    def test_session_file_has_no_geometry(self):
        nb = self.ctx.open_notebook(self.root)
        self.ctx.buffers.open(nb.id, "notes/hello.md")
        self.ctx.prepare_shutdown()
        self.ctx.close()

        session = self._session_file()
        self.assertEqual(set(session), {
            "notebooks", "workspaces", "buffers", "currentWorkspaceId"})
        self.assertNotIn("geometry", json.dumps(session).lower())


class FilterSessionTest(unittest.TestCase):
    def test_preview_only_buffers_dropped(self):
        session = SessionConfig(
            workspaces=[
                WorkspaceConfig(id="ordinary", buffer_ids=["a"]),
                WorkspaceConfig(id="preview", is_preview=True,
                                buffer_ids=["b", "a"]),
            ],
            buffers=[
                BufferRecord(id="a", notebook_id="n", file_path="a.md"),
                BufferRecord(id="b", notebook_id="n", file_path="b.md"),
            ],
            current_workspace_id="preview",
        )
        filtered = filter_session(session)
        self.assertEqual([item.id for item in filtered.workspaces], ["ordinary"])
        self.assertEqual([item.id for item in filtered.buffers], ["a"])
        self.assertEqual(filtered.current_workspace_id, "ordinary")

    def test_no_previews_is_shallow_copy(self):
        session = SessionConfig(
            workspaces=[WorkspaceConfig(id="w", buffer_ids=["a"])],
            buffers=[BufferRecord(id="a", notebook_id="n", file_path="a.md")],
            current_workspace_id="w",
        )
        filtered = filter_session(session)
        self.assertEqual(filtered.current_workspace_id, "w")
        self.assertEqual([item.id for item in filtered.workspaces], ["w"])
        self.assertEqual([item.id for item in filtered.buffers], ["a"])


if __name__ == "__main__":
    unittest.main()
