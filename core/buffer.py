"""Lazy text buffers: open/save/dirty/external-change, backups and reloading.

A :class:`Buffer` owns one note's in-memory text. Content is loaded on first
access (``content()``); ``set_content`` marks it dirty and bumps ``revision``;
``save`` writes atomically and clears dirty only when no edit raced the write.
External changes are detected by mtime and then verified by byte comparison so
a mere touch is benign but a real edit flags ``FILE_CHANGED``.

The module is Qt-free. ``ui.core_bridge`` turns the manager's calls into signals
and events; tests drive the same API directly.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any, Sequence

from . import fsutil, paths
from .errors import (
    BufferNotFoundError,
    CoreError,
    ReadOnlyError,
    SaveError,
    UnsupportedError,
)
from .models import BufferInfo, BufferRecord, BufferState, LineEnding

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .concurrency import TaskRunner
    from .events import EventBus
    from .notebook import NotebookManager

log = logging.getLogger(__name__)

MAX_COMPARE_BYTES = 50 * 1024 * 1024
BACKUP_PREFIX = b"vnotex_backup_file "
BACKUP_SUFFIX = ".vswp"


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class BufferSource:
    """Abstraction over where a buffer's bytes live."""

    @property
    def path(self) -> str:
        raise NotImplementedError

    def read(self) -> bytes:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def mtime_ms(self) -> int:
        raise NotImplementedError

    def exists(self) -> bool:
        raise NotImplementedError

    @property
    def writable(self) -> bool:
        return True


class FileSource(BufferSource):
    """Bytes on the filesystem, written atomically."""

    def __init__(self, path: str) -> None:
        self._path = os.path.abspath(os.path.expanduser(path))

    @property
    def path(self) -> str:
        return self._path

    def read(self) -> bytes:
        return fsutil.read_bytes(self._path)

    def write(self, data: bytes) -> None:
        fsutil.atomic_write_bytes(self._path, data)

    def mtime_ms(self) -> int:
        return fsutil.mtime_ms(self._path)

    def exists(self) -> bool:
        return os.path.isfile(self._path)


class MemorySource(BufferSource):
    """In-memory bytes for virtual ``vx://`` or scratch buffers."""

    def __init__(self, data: bytes = b"", name: str = "memory") -> None:
        self._data = bytes(data)
        self._name = name
        self._writable = True

    @property
    def path(self) -> str:
        return self._name

    def read(self) -> bytes:
        return self._data

    def write(self, data: bytes) -> None:
        self._data = bytes(data)

    def mtime_ms(self) -> int:
        return 0

    def exists(self) -> bool:
        return True

    @property
    def writable(self) -> bool:
        return self._writable

    def set_writable(self, value: bool) -> None:
        self._writable = bool(value)


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------

class Buffer:
    """One note's editable text with vxcore-compatible state semantics."""

    def __init__(
        self,
        manager: "BufferManager",
        buffer_id: str,
        source: BufferSource,
        *,
        notebook_id: str = "",
        file_path: str = "",
        display_name: str = "",
        encodings: Sequence[str] = ("UTF-8", "GB18030"),
        read_only: bool = False,
        is_virtual: bool = False,
        is_external: bool = False,
        line_ending_override: LineEnding | None = None,
    ) -> None:
        self._manager = manager
        self._id = buffer_id
        self._source = source
        self._notebook_id = notebook_id
        self._file_path = file_path
        self._display_name = display_name or os.path.basename(source.path) or source.path
        self._encodings = tuple(encodings)
        self._read_only = bool(read_only) or not source.writable
        self._is_virtual = bool(is_virtual)
        self._is_external = bool(is_external)
        self._line_ending_override = line_ending_override

        self._lock = threading.RLock()
        self._content: str | None = None
        self._revision = 0
        self._modified = False
        self._state = BufferState.NORMAL
        self._encoding = "utf-8"
        self._bom = b""
        self._line_ending = LineEnding.LF
        self._lossy = False
        self._stamp_ms = 0

    # -- identity -----------------------------------------------------------

    @property
    def id(self) -> str:
        return self._id

    @property
    def notebook_id(self) -> str:
        return self._notebook_id

    @property
    def file_path(self) -> str:
        return self._file_path

    @property
    def source(self) -> BufferSource:
        return self._source

    @property
    def absolute_path(self) -> str:
        return self._source.path

    # -- state --------------------------------------------------------------

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def modified(self) -> bool:
        with self._lock:
            return self._modified

    @property
    def state(self) -> BufferState:
        with self._lock:
            return self._state

    @property
    def content_loaded(self) -> bool:
        with self._lock:
            return self._content is not None

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def is_virtual(self) -> bool:
        return self._is_virtual

    @property
    def is_external(self) -> bool:
        return self._is_external

    @property
    def encoding(self) -> str:
        return self._encoding

    @property
    def line_ending(self) -> LineEnding:
        return self._line_ending_override or self._line_ending

    @property
    def lossy(self) -> bool:
        return self._lossy

    def info(self) -> BufferInfo:
        with self._lock:
            return BufferInfo(
                id=self._id,
                notebook_id=self._notebook_id,
                file_path=self._file_path,
                display_name=self._display_name,
                revision=self._revision,
                modified=self._modified,
                state=self._state,
                content_loaded=self._content is not None,
                can_save=(not self._read_only and self._source.writable
                          and self._modified and self._content is not None),
                can_reload=self._content is not None and not self._is_virtual,
                is_virtual=self._is_virtual,
                is_external=self._is_external,
                read_only=self._read_only,
                encoding=self._encoding,
                line_ending=self.line_ending,
                lossy=self._lossy,
            )

    # -- content ------------------------------------------------------------

    def content(self) -> str:
        """Return the text, loading it lazily on first access."""
        with self._lock:
            if self._content is None:
                self._load_locked()
            return self._content or ""

    def _load_locked(self) -> None:
        """Decode the source into ``_content`` (caller holds ``_lock``)."""
        if not self._source.exists():
            self._content = ""
            self._state = BufferState.FILE_MISSING
            self._stamp_ms = 0
            return
        try:
            raw = self._source.read()
        except OSError as error:
            self._content = ""
            self._state = BufferState.FILE_MISSING
            log.warning("cannot read buffer source %s: %s", self._source.path, error)
            return

        decoded = fsutil.decode_text_lossy(
            raw, encodings=self._encodings, fallback="utf-8"
        )
        # The editor (QTextEdit) works in '\n'; remember the original EOL and
        # re-apply it on save so the on-disk bytes round-trip unchanged.
        self._content = fsutil.normalize_eol(decoded.text)
        self._encoding = decoded.encoding
        self._bom = decoded.bom
        self._line_ending = decoded.line_ending
        self._lossy = decoded.lossy
        self._stamp_ms = self._source.mtime_ms()
        self._state = BufferState.NORMAL
        # Never write back bytes we could not decode — that would destroy data.
        if decoded.lossy:
            self._read_only = True

    def set_content(self, text: str) -> None:
        """Replace the text, marking dirty and bumping revision."""
        with self._lock:
            self._content = text
            self._modified = True
            self._revision += 1
        self._manager._on_modified(self)  # noqa: SLF001 - same package

    # -- save / reload ------------------------------------------------------

    def save(self, *, raise_on_external: bool = False) -> None:
        """Atomically write the current text and clear dirty on success.

        The byte write happens *outside* the lock so a slow disk cannot hold up
        reads/edits. If an edit lands during the write, the saved bytes are the
        snapshot and the buffer stays dirty so the user can save again.
        """
        with self._lock:
            if self._read_only or not self._source.writable:
                raise ReadOnlyError("buffer is read-only", path=self._source.path)
            if self._content is None:
                return  # nothing loaded -> nothing to save
            if raise_on_external and self._state is BufferState.FILE_CHANGED:
                raise CoreError("file changed on disk; reload before saving",
                                path=self._source.path)
            content = self._content
            revision = self._revision
            data = fsutil.encode_text(
                content,
                encoding=self._encoding,
                bom=self._bom,
                line_ending=self.line_ending,
            )

        # A non-virtual buffer keeps a recoverable .vswp before the write.
        if not self._is_virtual and self._source.exists():
            try:
                self.create_backup(data)
            except OSError:  # backup must never block a save
                log.warning("cannot create backup for %s", self._source.path,
                            exc_info=True)

        try:
            self._source.write(data)
        except OSError as error:
            with self._lock:
                self._state = BufferState.SAVE_FAILED
                self._modified = True
            raise SaveError("cannot save buffer", path=self._source.path) from error

        with self._lock:
            new_stamp = self._source.mtime_ms()
            self._stamp_ms = new_stamp
            self._state = BufferState.NORMAL
            if self._revision == revision:
                # No edit landed during the byte write: save is clean.
                self._modified = False
                self._revision = revision + 1
            else:
                # An edit raced the write; the saved bytes are the snapshot,
                # so stay dirty and keep the editor's newer revision.
                self._modified = True
        self._manager._on_saved(self)  # noqa: SLF001 - same package

    def reload(self) -> None:
        """Discard unsaved changes and re-read from the source."""
        with self._lock:
            self._content = None
            self._load_locked()
            self._revision += 1
            self._modified = False
        self._manager._on_reloaded(self)  # noqa: SLF001 - same package

    # -- external change ----------------------------------------------------

    def check_external(self) -> BufferState:
        """Detect an on-disk change; return the new state.

        Skips until content is loaded. A missing file is ``FILE_MISSING``; an
        unchanged mtime is ``NORMAL``; a different mtime triggers a byte compare
        (exact, then EOL-normalised). An equal file is benign and refreshes the
        stamp; a genuinely different file is ``FILE_CHANGED`` and leaves the
        stamp stale so it keeps flagging until reload/save.
        """
        with self._lock:
            if self._content is None:
                return self._state
            if self._is_virtual:
                return self._state
            if not self._source.exists():
                self._state = BufferState.FILE_MISSING
                return self._state
            current = self._source.mtime_ms()
            if current == self._stamp_ms:
                return self._state

            # mtime differs: compare bytes (bounded to avoid huge reads).
            try:
                raw = fsutil.read_bytes(self._source.path, max_size=MAX_COMPARE_BYTES)
            except OSError:
                self._state = BufferState.FILE_MISSING
                return self._state
            decoded = fsutil.decode_text_lossy(
                raw, encodings=self._encodings, fallback="utf-8"
            )
            if (decoded.text == self._content or
                    fsutil.normalize_eol(decoded.text) ==
                    fsutil.normalize_eol(self._content)):
                # Benign: only mtime/EOL changed. Refresh the stamp, stay normal.
                self._stamp_ms = current
                self._state = BufferState.NORMAL
            else:
                self._state = BufferState.FILE_CHANGED
                # Leave _stamp_ms stale so the change keeps being reported.
            return self._state

    # -- backup -------------------------------------------------------------

    @property
    def backup_path(self) -> str:
        return self._source.path + BACKUP_SUFFIX

    def create_backup(self, data: bytes | None = None) -> str:
        """Write ``<abs>.vswp`` with vxcore's header; return its path."""
        if data is None:
            with self._lock:
                data = fsutil.encode_text(
                    self.content(), encoding=self._encoding,
                    bom=self._bom, line_ending=self.line_ending,
                )
        header = BACKUP_PREFIX + self._source.path.encode("utf-8") + b"|"
        fsutil.atomic_write_bytes(self.backup_path, header + data)
        return self.backup_path

    def recover_backup(self) -> None:
        """Restore content from ``.vswp`` if its header matches this buffer."""
        path = self.backup_path
        if not os.path.isfile(path):
            raise CoreError("no backup file", path=path)
        raw = fsutil.read_bytes(path)
        header = BACKUP_PREFIX + self._source.path.encode("utf-8") + b"|"
        if not raw.startswith(header):
            raise CoreError("backup file does not match this buffer", path=path)
        data = raw[len(header):]
        decoded = fsutil.decode_text_lossy(
            data, encodings=self._encodings, fallback="utf-8"
        )
        self.set_content(decoded.text)

    def discard_backup(self) -> None:
        """Remove ``.vswp`` if present."""
        try:
            os.unlink(self.backup_path)
        except FileNotFoundError:
            pass

    def has_backup(self) -> bool:
        return os.path.isfile(self.backup_path)

    # -- line ending --------------------------------------------------------

    def set_line_ending_override(self, ending: LineEnding | None) -> None:
        self._line_ending_override = ending

    def set_encoding(self, encoding: str) -> None:
        """Force the encoding used for decode/encode on next load/save."""
        self._encoding = encoding


# ---------------------------------------------------------------------------
# BufferManager
# ---------------------------------------------------------------------------

class BufferManager:
    """Owns all open buffers, deduplicated by ``(notebook_id, file_path)``."""

    def __init__(
        self,
        notebooks: "NotebookManager",
        events: "EventBus",
        tasks: "TaskRunner | None" = None,
        *,
        encodings: Sequence[str] = ("UTF-8", "GB18030"),
    ) -> None:
        self._notebooks = notebooks
        self._events = events
        self._tasks = tasks
        self._encodings = tuple(encodings)
        self._buffers: dict[str, Buffer] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()

    def configure(self, encodings: Sequence[str]) -> None:
        self._encodings = tuple(encodings)

    # -- open ---------------------------------------------------------------

    def open(
        self,
        notebook_id: str,
        file_path: str,
        *,
        read_only: bool | None = None,
        line_ending_override: LineEnding | None = None,
        buffer_id: str | None = None,
    ) -> Buffer:
        """Open a notebook-relative file, returning an existing buffer if any.

        ``buffer_id`` lets session restore keep the persisted id (vxcore does
        the same) so workspace ``bufferIds`` references stay valid.
        """
        notebook = self._notebooks.get(notebook_id)
        rel = paths.normalize_rel(file_path)
        key = (notebook_id, rel)
        with self._lock:
            existing_id = self._by_key.get(key)
            if existing_id is not None:
                return self._buffers[existing_id]
            if buffer_id and buffer_id in self._buffers:
                buffer_id = None

        abs_path = notebook.folders.absolute_path(rel)
        ro = notebook.read_only if read_only is None else read_only
        buffer = Buffer(
            self, buffer_id or fsutil.new_uuid(), FileSource(abs_path),
            notebook_id=notebook_id, file_path=rel,
            display_name=os.path.basename(rel),
            encodings=self._encodings, read_only=ro,
            line_ending_override=line_ending_override,
        )
        self._register(buffer, key)
        return buffer

    def open_abs(self, abs_path: str) -> Buffer:
        """Open an absolute path, resolving it into a notebook when possible."""
        target = os.path.abspath(os.path.expanduser(abs_path))
        resolved = self._notebooks.resolve_path(target)
        if resolved is not None:
            notebook, rel = resolved
            return self.open(notebook.id, rel)
        # Outside any notebook -> external buffer keyed by its absolute path.
        key = ("", os.path.normcase(target))
        with self._lock:
            existing_id = self._by_key.get(key)
            if existing_id is not None:
                return self._buffers[existing_id]
        buffer = Buffer(
            self, fsutil.new_uuid(), FileSource(target),
            display_name=os.path.basename(target),
            encodings=self._encodings, is_external=True,
        )
        self._register(buffer, key)
        return buffer

    def open_virtual(self, name: str, text: str = "") -> Buffer:
        """Create a memory-backed buffer (no disk source, no backup)."""
        buffer = Buffer(
            self, fsutil.new_uuid(), MemorySource(text.encode("utf-8"), name),
            display_name=name, encodings=self._encodings, is_virtual=True,
        )
        with self._lock:
            self._buffers[buffer.id] = buffer
        self._events.emit("buffer.opened", {"bufferId": buffer.id})
        return buffer

    def _register(self, buffer: Buffer, key: tuple[str, str]) -> None:
        with self._lock:
            self._buffers[buffer.id] = buffer
            self._by_key[key] = buffer.id
        self._events.emit("buffer.opened", {
            "bufferId": buffer.id,
            "notebookId": buffer.notebook_id,
            "filePath": buffer.file_path,
        })

    # -- lookup -------------------------------------------------------------

    def get(self, buffer_id: str) -> Buffer:
        with self._lock:
            buffer = self._buffers.get(buffer_id)
        if buffer is None:
            raise BufferNotFoundError("buffer is not open", path=buffer_id)
        return buffer

    def find(self, notebook_id: str, file_path: str) -> Buffer | None:
        key = (notebook_id, paths.normalize_rel(file_path))
        with self._lock:
            buffer_id = self._by_key.get(key)
            return self._buffers.get(buffer_id) if buffer_id else None

    def list(self) -> list[BufferInfo]:
        with self._lock:
            return [buffer.info() for buffer in self._buffers.values()]

    # -- lifecycle ----------------------------------------------------------

    def close(self, buffer_id: str, *, force: bool = False) -> None:
        with self._lock:
            buffer = self._buffers.get(buffer_id)
            if buffer is None:
                return
            if buffer.modified and not force:
                raise CoreError("buffer has unsaved changes", path=buffer_id)
            for key, value in list(self._by_key.items()):
                if value == buffer_id:
                    self._by_key.pop(key, None)
            self._buffers.pop(buffer_id, None)
        self._events.emit("buffer.closed", {"bufferId": buffer_id})

    def close_all(self, *, force: bool = True) -> None:
        for buffer_id in list(self._buffers):
            self.close(buffer_id, force=force)

    def update_paths(self, notebook_id: str, old_rel: str, new_rel: str) -> None:
        """Re-key a buffer after a rename/move (phase 3 CRUD hook)."""
        old_key = (notebook_id, paths.normalize_rel(old_rel))
        with self._lock:
            buffer_id = self._by_key.pop(old_key, None)
            if buffer_id is None:
                return
            self._by_key[(notebook_id, paths.normalize_rel(new_rel))] = buffer_id
            buffer = self._buffers[buffer_id]
            buffer._file_path = paths.normalize_rel(new_rel)  # noqa: SLF001

    # -- operations ---------------------------------------------------------

    def save(self, buffer_id: str) -> None:
        self.get(buffer_id).save()

    def reload(self, buffer_id: str) -> None:
        self.get(buffer_id).reload()

    def check_external_changes(self) -> dict[str, BufferState]:
        with self._lock:
            buffers = list(self._buffers.values())
        changes: dict[str, BufferState] = {}
        for buffer in buffers:
            before = buffer.state
            after = buffer.check_external()
            if after is not before:
                changes[buffer.id] = after
                self._events.emit("buffer.state_changed", {
                    "bufferId": buffer.id, "state": str(after),
                })
        return changes

    # -- internal event hooks ----------------------------------------------

    def _on_modified(self, buffer: Buffer) -> None:
        self._events.emit("buffer.modified", {
            "bufferId": buffer.id, "modified": buffer.modified,
        })

    def _on_saved(self, buffer: Buffer) -> None:
        self._events.emit("file.saved", {
            "bufferId": buffer.id, "notebookId": buffer.notebook_id,
            "filePath": buffer.file_path,
        })
        self._events.emit("buffer.modified", {
            "bufferId": buffer.id, "modified": False,
        })

    def _on_reloaded(self, buffer: Buffer) -> None:
        self._events.emit("buffer.reloaded", {"bufferId": buffer.id})

    # -- session ------------------------------------------------------------

    def session_records(self) -> list[BufferRecord]:
        """Snapshot open notebook buffers for ``vxsession.json``.

        ``fileId`` is persisted so a note renamed between sessions can be found
        again on restore. Virtual and external buffers are not persisted.
        """
        with self._lock:
            buffers = [buffer for buffer in self._buffers.values()
                       if not buffer.is_virtual and buffer.notebook_id]
        return [
            BufferRecord(
                id=buffer.id, notebook_id=buffer.notebook_id,
                file_path=buffer.file_path,
                file_id=self._file_id_for(buffer),
            )
            for buffer in buffers
        ]

    def _file_id_for(self, buffer: Buffer) -> str:
        try:
            notebook = self._notebooks.get(buffer.notebook_id)
            return notebook.folders.get_file_info(buffer.file_path).id
        except CoreError:
            return ""

    def _resolve_restore_path(self, record: BufferRecord) -> str:
        """Return the current relative path for ``record`` (``""`` if gone).

        The persisted path wins; when it no longer exists, the persisted
        ``fileId`` is looked up in the notebook index so a renamed or moved
        note reopens under its new path.
        """
        try:
            notebook = self._notebooks.get(record.notebook_id)
        except CoreError:
            return ""
        if record.file_path:
            rel = paths.normalize_rel(record.file_path)
            try:
                if os.path.isfile(notebook.folders.absolute_path(rel)):
                    return rel
            except CoreError:
                pass
        if record.file_id:
            resolved = notebook.store.node_path(record.file_id)
            if resolved:
                try:
                    if os.path.isfile(notebook.folders.absolute_path(resolved)):
                        return resolved
                except CoreError:
                    pass
        return ""

    def restore(self, records: Sequence[BufferRecord]) -> list[Buffer]:
        """Open buffers from persisted records, skipping missing files.

        Preserves each record's buffer id and resolves renamed files through
        ``fileId`` before giving up.
        """
        restored: list[Buffer] = []
        for record in records:
            if not record.notebook_id:
                continue
            rel = self._resolve_restore_path(record)
            if not rel:
                continue
            try:
                buffer = self.open(record.notebook_id, rel,
                                   buffer_id=record.id or None)
            except CoreError:
                continue
            restored.append(buffer)
        return restored

    def flush(self) -> None:
        """Save every dirty, writable buffer."""
        with self._lock:
            buffers = list(self._buffers.values())
        for buffer in buffers:
            if buffer.modified and not buffer.read_only:
                buffer.save()


__all__ = [
    "BACKUP_PREFIX",
    "BACKUP_SUFFIX",
    "Buffer",
    "BufferManager",
    "BufferSource",
    "FileSource",
    "MemorySource",
]
