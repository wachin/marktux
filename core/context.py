"""``CoreContext`` — the single entry point to the notebook backend.

Owns the configuration, event bus, task runner and every manager, and defines
the startup/shutdown order. The Qt layer only ever talks to this object (via
``ui.core_bridge``), which keeps lifecycle decisions in one place.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Sequence

from .buffer import BufferManager
from .concurrency import BufferWatchdog, TaskRunner
from .config import ConfigManager
from .events import EventBus
from .models import CoreConfig
from .notebook import Notebook, NotebookManager
from .workspace import WorkspaceManager

log = logging.getLogger(__name__)


class CoreContext:
    def __init__(
        self,
        *,
        app_data_dir: str | None = None,
        local_data_dir: str | None = None,
        config: CoreConfig | None = None,
        max_workers: int = 3,
        recover_session: bool | None = None,
        search_backends: Sequence[str] | None = None,
    ) -> None:
        self.config = ConfigManager(app_data_dir, local_data_dir)
        # Keep the injected config: load() reads from disk and would otherwise
        # discard it (callers pass config to override search/encoding settings).
        self._core_config = config
        if config is not None:
            self.config.core = config
        self.events = EventBus()
        self.tasks = TaskRunner(max_workers)
        self.notebooks = NotebookManager(self.config, self.events)
        self.buffers = BufferManager(
            self.notebooks, self.events, self.tasks,
            encodings=self.config.core.search.encodings,
        )
        self.notebooks._buffers = self.buffers  # rekey hook for folder CRUD
        self.workspaces = WorkspaceManager(self.config, self.buffers, self.notebooks)
        self.watchdog = BufferWatchdog(self.buffers, self.events)
        self._recover_session = recover_session
        self._session_prepared = False
        self._search_backends = (
            list(search_backends) if search_backends is not None else None
        )
        self._opened = False

    # -- lifecycle ----------------------------------------------------------

    @property
    def opened(self) -> bool:
        return self._opened

    def open(self) -> "CoreContext":
        if self._opened:
            return self
        self.config.load()
        if self._core_config is not None:
            self.config.core = self._core_config
        if self._search_backends is not None:
            self.config.core.search.backends = list(self._search_backends)
        recover = (self.config.core.recover_last_session
                   if self._recover_session is None else self._recover_session)
        # vxcore always reopens the notebooks listed in the session; only the
        # buffers and workspaces honour ``recoverLastSession``.
        self.notebooks.restore_from_session()
        self.buffers.configure(self.config.core.search.encodings)
        self.workspaces.restore(recover)
        self.watchdog.start()
        self._opened = True
        return self

    def close(self) -> None:
        if not self._opened:
            return
        self.watchdog.stop()
        # Snapshot before closing notebooks: resolving a renamed buffer needs
        # the notebook index, and a prepare_shutdown snapshot must win.
        try:
            if not self._session_prepared:
                self.workspaces.snapshot()
                self.config.save_session()
                self._session_prepared = True
        except Exception:  # noqa: BLE001 - never block shutdown
            log.exception("cannot save session")
        try:
            self.notebooks.close_all()
        finally:
            self.tasks.shutdown(wait=True, timeout=3.0)
            self._opened = False

    def __enter__(self) -> "CoreContext":
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- session ------------------------------------------------------------

    def prepare_shutdown(self) -> None:
        """Snapshot the open state into ``vxsession.json`` (idempotent).

        Writes exactly once; a later :meth:`close` will not overwrite the
        snapshot (vxcore's ``prepare_shutdown`` contract).
        """
        if self._session_prepared:
            return
        self.workspaces.snapshot()
        self.config.save_session()
        self._session_prepared = True
        self.events.emit("session.saved", {})

    # -- convenience --------------------------------------------------------

    def open_notebook(self, root: str, **kwargs: Any) -> Notebook:
        return self.notebooks.open(os.path.expanduser(root), **kwargs)

    def close_notebook(self, notebook_id: str) -> None:
        self.notebooks.close(notebook_id)


__all__ = ["CoreContext"]
