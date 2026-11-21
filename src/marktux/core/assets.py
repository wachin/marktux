"""Assets and attachments, ported from vxcore's ``StandardBufferProvider``.

A note's assets live under its **public assets folder**:

    <notebook_root>/<note_parent>/<assetsFolder>/<note_uuid>/

for the default relative ``vx_assets`` setting; an absolute ``assetsFolder`` is
used verbatim. Inserted names are deduplicated with ``_1``, ``_2`` … before the
bytes are published, and the returned path is relative to the notebook root with
``/`` separators (the form markdown links use).

Attachments are assets whose basename is also recorded in the note's ``vx.json``
``attachments`` list (mirrored to the SQLite cache). Raw notebooks have no
``vx.json``, so assets work there but attachments raise ``UnsupportedError``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from . import fsutil, paths
from .errors import (
    InvalidNameError,
    NodeNotFoundError,
    ReadOnlyError,
    UnsupportedError,
)
from .models import NotebookType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .notebook import Notebook


class AssetManager:
    """Insert/delete assets and attachments for a notebook."""

    def __init__(self, notebook: "Notebook") -> None:
        self._notebook = notebook

    # -- paths --------------------------------------------------------------

    def public_folder(self, file_path: str) -> str:
        """Absolute folder that holds the assets trees for ``file_path``."""
        configured = self._notebook.config.assets_folder or "vx_assets"
        if os.path.isabs(configured):
            return os.path.normpath(configured)
        parent, _ = paths.split_rel(paths.normalize_rel(file_path))
        base = self._notebook.root
        if parent:
            base = os.path.join(base, *parent.split("/"))
        return os.path.join(base, *configured.split("/"))

    def assets_folder(self, file_path: str) -> str:
        """Absolute directory holding this note's assets (``<public>/<uuid>``)."""
        rel = paths.normalize_rel(file_path)
        record = self._notebook.folders.get_file_info(rel)
        return os.path.join(self.public_folder(rel), record.id)

    # -- assets -------------------------------------------------------------

    def insert(self, file_path: str, source_path: str) -> str:
        """Copy a local file into the note's assets folder; return its rel path."""
        self._require_writable()
        if not os.path.isfile(source_path):
            raise NodeNotFoundError(
                "asset source does not exist", path=source_path)
        name = os.path.basename(source_path)
        return self.insert_bytes(file_path, name, fsutil.read_bytes(source_path))

    def insert_bytes(self, file_path: str, name: str, data: bytes) -> str:
        """Write ``data`` as an asset; return its notebook-relative path."""
        self._require_writable()
        name = self._validate_name(name)
        folder = self._ensure_folder(file_path)
        unique = self._unique_name(name, folder)
        dest = os.path.join(folder, unique)
        if not paths.is_inside(self._notebook.root, dest):
            raise UnsupportedError(
                "assets folder is outside the notebook", path=folder)
        fsutil.atomic_write_bytes(dest, data)
        return paths.relative_to(self._notebook.root, dest)

    def delete(self, file_path: str, relative_path: str) -> None:
        """Delete an asset addressed by its notebook-relative path."""
        self._require_writable()
        target = paths.resolve_inside(
            self._notebook.root, relative_path,
            must_exist=True, reject_symlinks=True,
        )
        if not os.path.isfile(target):
            raise NodeNotFoundError("asset does not exist", path=relative_path)
        os.unlink(target)

    # -- attachments --------------------------------------------------------

    def list_attachments(self, file_path: str) -> list[str]:
        """Return the note's attachment basenames."""
        self._require_attachments()
        return self._notebook.folders.get_attachments(file_path)

    def insert_attachment(self, file_path: str, source_path: str) -> str:
        """Insert an asset and record it as an attachment; return its basename."""
        self._require_attachments()
        relative = self.insert(file_path, source_path)
        name = relative.rsplit("/", 1)[-1]
        self._notebook.folders.add_attachment(file_path, name)
        return name

    def delete_attachment(self, file_path: str, filename: str) -> None:
        """Remove an attachment from metadata, then delete its file."""
        self._require_attachments()
        filename = self._validate_name(filename)
        # Metadata first: if the unlink fails the list is still consistent.
        self._notebook.folders.remove_attachment(file_path, filename)
        target = os.path.join(self.assets_folder(file_path), filename)
        if os.path.isfile(target):
            os.unlink(target)

    # -- helpers ------------------------------------------------------------

    def _require_writable(self) -> None:
        if self._notebook.read_only:
            raise ReadOnlyError(
                "notebook is read-only", path=self._notebook.root)

    def _require_attachments(self) -> None:
        if self._notebook.type is not NotebookType.BUNDLED:
            raise UnsupportedError(
                "attachments are not supported for raw notebooks",
                path=self._notebook.root,
            )

    def _ensure_folder(self, file_path: str) -> str:
        folder = self.assets_folder(file_path)
        fsutil.ensure_dir(folder)
        return folder

    @staticmethod
    def _validate_name(name: str) -> str:
        if (not name or name in (".", "..") or "/" in name
                or "\\" in name or "\0" in name):
            raise InvalidNameError(
                "asset name must be a single filename", path=name)
        return name

    @staticmethod
    def _unique_name(name: str, folder: str) -> str:
        """Return ``name`` or ``stem_N.ext`` if it already exists (vxcore rule)."""
        if not os.path.exists(os.path.join(folder, name)):
            return name
        stem, ext = os.path.splitext(name)
        counter = 1
        while True:
            candidate = f"{stem}_{counter}{ext}"
            if not os.path.exists(os.path.join(folder, candidate)):
                return candidate
            counter += 1


__all__ = ["AssetManager"]
