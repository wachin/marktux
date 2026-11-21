"""Fixture builders shared by the core tests.

Fixtures are written with the public model codecs (``to_json``) so they mirror
the exact on-disk format vxcore/VNote uses. Each helper returns a fresh
``tempfile.TemporaryDirectory`` that the caller must clean up (``addCleanup``).
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
from typing import Any

from marktux.core import (
    CoreContext,
    FileRecord,
    FolderConfig,
    NotebookConfig,
)


def _write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


def make_bundled(tmp: str, name: str = "Notebook", *,
                 with_note: bool = True) -> str:
    """Create a bundled notebook with a root vx.json and return its root path.

    Structure::

        <root>/vx_notebook/config.json
        <root>/vx_notebook/contents/vx.json      (root folder)
        <root>/notes/vx.json                       (a subfolder)
        <root>/notes/hello.md                      (one note, if ``with_note``)

    Pass ``with_note=False`` for tests that build their own indexed notes and
    need an empty notebook.
    """
    root = os.path.join(tmp, "bundled")
    meta = os.path.join(root, "vx_notebook")
    contents = os.path.join(meta, "contents")

    cfg = NotebookConfig(id="11111111-1111-1111-1111-111111111111", name=name)
    _write_json(os.path.join(meta, "config.json"), cfg.to_json())

    root_folder = FolderConfig.new(".")
    root_folder.folders.append("notes")
    _write_json(os.path.join(contents, "vx.json"), root_folder.to_json())

    # A subfolder, optionally with one indexed note.
    sub = FolderConfig(id="33333333-3333-3333-3333-333333333333", name="notes")
    if with_note:
        record = FileRecord(id="22222222-2222-2222-2222-222222222222",
                            name="hello.md")
        sub.files.append(record)
    _write_json(os.path.join(contents, "notes", "vx.json"), sub.to_json())

    os.makedirs(os.path.join(root, "notes"), exist_ok=True)
    if with_note:
        pathlib.Path(os.path.join(root, "notes", "hello.md")).write_text(
            "# Hello\n\nBody text.\n", encoding="utf-8")
    return root


def make_raw(tmp: str, name: str = "Raw") -> str:
    """Create a plain directory (no vx_notebook/) with two notes."""
    root = os.path.join(tmp, "raw")
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    pathlib.Path(os.path.join(root, "a.md")).write_text("# A\n", encoding="utf-8")
    pathlib.Path(os.path.join(root, "sub", "b.md")).write_text(
        "# B\n", encoding="utf-8")
    return root


def make_context(tmp: str, *, recover: bool = False) -> CoreContext:
    """Open a CoreContext with isolated app/local data dirs under ``tmp``."""
    return CoreContext(
        app_data_dir=os.path.join(tmp, "app"),
        local_data_dir=os.path.join(tmp, "local"),
        recover_session=recover,
    ).open()
