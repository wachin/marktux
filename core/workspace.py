"""Workspaces and session persistence, ported from vxcore.

A **workspace** is a named, ordered list of open buffer ids with one current
buffer. Preview workspaces (opened by transient actions such as quick-look) are
never written to ``vxsession.json``: on save they are dropped together with any
buffer that only a preview workspace references, and a current preview
workspace is replaced by the first ordinary one.

Session lifecycle mirrors vxcore:

* On open, **notebooks are always restored**; buffers and workspaces are
  restored only when ``recoverLastSession`` is true.
* A buffer whose file is missing is skipped. If its record carries a
  ``fileId``, the current path is resolved from the notebook index first, so a
  renamed/moved note reopens under its new name (bundled notebooks keep stable
  UUIDs; raw notebooks derive ids from the path).
* ``prepare_shutdown`` snapshots runtime state to ``vxsession.json`` **once**
  and is idempotent; a later ``close`` must not overwrite that snapshot. That
  is why :class:`CoreContext` records whether a shutdown snapshot was taken.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Sequence

from . import fsutil
from .errors import CoreError, InvalidStateError, WorkspaceNotFoundError
from .events import WORKSPACE_CHANGED
from .models import SessionConfig, WorkspaceConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .buffer import BufferManager
    from .config import ConfigManager
    from .notebook import NotebookManager

log = logging.getLogger(__name__)


def _clone(workspace: WorkspaceConfig) -> WorkspaceConfig:
    return WorkspaceConfig.from_json(workspace.to_json())


class _PreviewFilter:
    """vxcore's ``PreviewWorkspaceFilter``: which buffers previews own alone."""

    def __init__(self, workspaces: Sequence[WorkspaceConfig],
                 current_id: str) -> None:
        self.has_previews = any(item.is_preview for item in workspaces)
        self.current_is_preview = False
        self.first_ordinary: WorkspaceConfig | None = None
        self.preview_ids: set[str] = set()
        self.ordinary_ids: set[str] = set()
        if not self.has_previews:
            return
        for record in workspaces:
            target = self.preview_ids if record.is_preview else self.ordinary_ids
            target.update(record.buffer_ids)
            if record.is_preview:
                if record.id == current_id:
                    self.current_is_preview = True
            elif self.first_ordinary is None:
                self.first_ordinary = record

    def preview_only(self, buffer_id: str) -> bool:
        return buffer_id in self.preview_ids and buffer_id not in self.ordinary_ids


def filter_session(session: SessionConfig) -> SessionConfig:
    """Return a copy with preview workspaces and preview-only buffers removed.

    ``currentWorkspaceId`` falls back to the first ordinary workspace when the
    current one is a preview. With no previews this is a plain shallow copy.
    """
    previews = _PreviewFilter(session.workspaces, session.current_workspace_id)
    if not previews.has_previews:
        return SessionConfig(
            notebooks=list(session.notebooks),
            workspaces=list(session.workspaces),
            buffers=list(session.buffers),
            current_workspace_id=session.current_workspace_id,
        )
    current = session.current_workspace_id
    if previews.current_is_preview:
        current = previews.first_ordinary.id if previews.first_ordinary else ""
    return SessionConfig(
        notebooks=list(session.notebooks),
        workspaces=[item for item in session.workspaces if not item.is_preview],
        buffers=[item for item in session.buffers
                 if not previews.preview_only(item.id)],
        current_workspace_id=current,
    )


class WorkspaceManager:
    """Owns workspaces and folds buffer/session state into ``vxsession.json``."""

    def __init__(
        self,
        config: "ConfigManager",
        buffers: "BufferManager",
        notebooks: "NotebookManager | None" = None,
    ) -> None:
        self._config = config
        self._buffers = buffers
        self._notebooks = notebooks
        self._workspaces: dict[str, WorkspaceConfig] = {}
        self._current = ""
        self._lock = threading.RLock()

    # -- helpers ------------------------------------------------------------

    def _events(self) -> Any:
        return getattr(self._buffers, "_events", None)

    def _emit(self, action: str, workspace_id: str) -> None:
        events = self._events()
        if events is not None:
            events.emit(WORKSPACE_CHANGED, {
                "workspaceId": workspace_id, "action": action,
            })

    def _has_buffer(self, buffer_id: str) -> bool:
        try:
            self._buffers.get(buffer_id)
            return True
        except CoreError:
            return False

    # -- restore ------------------------------------------------------------

    def restore(self, recover: bool = True) -> list[WorkspaceConfig]:
        """Load workspaces (and buffers when ``recover``) from the session.

        Notebooks are restored by :class:`NotebookManager` independently of
        this flag, matching vxcore: turning off recovery keeps the notebook
        list but drops transient buffers and workspaces.
        """
        with self._lock:
            self._workspaces.clear()
            self._current = ""
            session = self._config.session
            if not recover:
                session.buffers = []
                session.workspaces = []
                session.current_workspace_id = ""
                return []

            filtered = filter_session(session)
            self._config.session = filtered
            self._buffers.restore(filtered.buffers)

            for record in filtered.workspaces:
                workspace = _clone(record)
                workspace.buffer_ids = [item for item in workspace.buffer_ids
                                        if self._has_buffer(item)]
                if workspace.current_buffer_id not in workspace.buffer_ids:
                    workspace.current_buffer_id = (
                        workspace.buffer_ids[0] if workspace.buffer_ids else ""
                    )
                self._workspaces[workspace.id] = workspace

            if filtered.current_workspace_id in self._workspaces:
                self._current = filtered.current_workspace_id
            return [_clone(item) for item in self._workspaces.values()]

    # -- queries ------------------------------------------------------------

    def get(self, workspace_id: str) -> WorkspaceConfig:
        with self._lock:
            workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(
                "workspace does not exist", path=workspace_id)
        return _clone(workspace)

    def list(self) -> list[WorkspaceConfig]:
        with self._lock:
            return [_clone(item) for item in self._workspaces.values()]

    def current(self) -> str:
        with self._lock:
            return self._current

    # -- mutations ----------------------------------------------------------

    def create(self, name: str, *, is_preview: bool = False) -> WorkspaceConfig:
        workspace = WorkspaceConfig(
            id=fsutil.new_uuid(), name=name, is_preview=bool(is_preview))
        with self._lock:
            self._workspaces[workspace.id] = workspace
        self.save()
        self._emit("created", workspace.id)
        return _clone(workspace)

    def delete(self, workspace_id: str) -> None:
        with self._lock:
            if workspace_id not in self._workspaces:
                raise WorkspaceNotFoundError(
                    "workspace does not exist", path=workspace_id)
            del self._workspaces[workspace_id]
            if self._current == workspace_id:
                self._current = ""
        self.save()
        self._emit("deleted", workspace_id)

    def rename(self, workspace_id: str, name: str) -> None:
        with self._lock:
            workspace = self._require(workspace_id)
            workspace.name = name
        self.save()
        self._emit("renamed", workspace_id)

    def set_current(self, workspace_id: str) -> None:
        with self._lock:
            self._require(workspace_id)
            self._current = workspace_id
        self.save()
        self._emit("current_changed", workspace_id)

    def add_buffer(self, workspace_id: str, buffer_id: str) -> None:
        self._buffers.get(buffer_id)  # validate: must be open
        with self._lock:
            workspace = self._require(workspace_id)
            if buffer_id not in workspace.buffer_ids:
                workspace.buffer_ids.append(buffer_id)
            if not workspace.current_buffer_id:
                workspace.current_buffer_id = buffer_id
        self.save()
        self._emit("buffer_added", workspace_id)

    def remove_buffer(self, workspace_id: str, buffer_id: str) -> None:
        with self._lock:
            workspace = self._require(workspace_id)
            workspace.buffer_ids = [
                item for item in workspace.buffer_ids if item != buffer_id]
            if workspace.current_buffer_id == buffer_id:
                workspace.current_buffer_id = (
                    workspace.buffer_ids[0] if workspace.buffer_ids else "")
        self._close_if_orphaned(buffer_id)
        self.save()
        self._emit("buffer_removed", workspace_id)

    def set_current_buffer(self, workspace_id: str, buffer_id: str) -> None:
        with self._lock:
            workspace = self._require(workspace_id)
            if buffer_id not in workspace.buffer_ids:
                raise InvalidStateError(
                    "buffer is not in the workspace", path=buffer_id)
            workspace.current_buffer_id = buffer_id
        self.save()
        self._emit("current_buffer_changed", workspace_id)

    def set_buffer_order(self, workspace_id: str,
                         buffer_ids: Sequence[str]) -> None:
        order = list(buffer_ids)
        with self._lock:
            workspace = self._require(workspace_id)
            if (len(set(order)) != len(order)
                    or sorted(order) != sorted(workspace.buffer_ids)):
                raise InvalidStateError(
                    "buffer order must be an exact permutation")
            if order == workspace.buffer_ids:
                return
            workspace.buffer_ids = order
        self.save()
        self._emit("buffer_order_changed", workspace_id)

    def set_buffer_metadata(self, workspace_id: str, buffer_id: str,
                            metadata: dict[str, Any]) -> None:
        with self._lock:
            workspace = self._require(workspace_id)
            if buffer_id not in workspace.buffer_ids:
                raise InvalidStateError(
                    "buffer is not in the workspace", path=buffer_id)
            workspace.buffer_metadata[buffer_id] = dict(metadata)
        self.save()
        self._emit("buffer_metadata_changed", workspace_id)

    # -- persistence --------------------------------------------------------

    def snapshot(self) -> SessionConfig:
        """Fold runtime state into ``config.session`` and return the snapshot.

        Workspaces are cloned and pruned to currently-open buffers, then the
        preview filter is applied so the persisted view never contains
        transient state.
        """
        with self._lock:
            workspaces: list[WorkspaceConfig] = []
            for record in self._workspaces.values():
                workspace = _clone(record)
                workspace.buffer_ids = [item for item in workspace.buffer_ids
                                        if self._has_buffer(item)]
                if workspace.current_buffer_id not in workspace.buffer_ids:
                    workspace.current_buffer_id = (
                        workspace.buffer_ids[0] if workspace.buffer_ids else "")
                workspaces.append(workspace)
            session = SessionConfig(
                notebooks=list(self._config.session.notebooks),
                workspaces=workspaces,
                buffers=self._buffers.session_records(),
                current_workspace_id=self._current,
            )
        filtered = filter_session(session)
        self._config.session = filtered
        return filtered

    def save(self) -> None:
        self.snapshot()
        self._config.save_session()

    # -- internals ----------------------------------------------------------

    def _require(self, workspace_id: str) -> WorkspaceConfig:
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(
                "workspace does not exist", path=workspace_id)
        return workspace

    def _close_if_orphaned(self, buffer_id: str) -> None:
        """Close a buffer no workspace references any more.

        A modified buffer is left open so unsaved edits are never discarded.
        """
        with self._lock:
            referenced = any(buffer_id in item.buffer_ids
                             for item in self._workspaces.values())
        if referenced:
            return
        try:
            self._buffers.close(buffer_id)
        except CoreError:
            log.debug("leaving orphaned modified buffer %s open", buffer_id)


__all__ = [
    "WorkspaceManager",
    "filter_session",
]
