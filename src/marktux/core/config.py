"""Application and session configuration.

Two files, mirroring vxcore's split:

* ``vxcore.json`` in the app data directory — search backends/encodings, file
  types, ``recoverLastSession``, auto-sync debounce. Written on first run,
  preserved verbatim except for the keys we own.
* ``vxsession.json`` in the local data directory — which notebooks are open,
  their read-only/trusted flags, the buffers and workspaces. Core owns this
  file; window geometry belongs to the app (``QSettings``) and must never be
  written here.

Both are written atomically. Unknown keys survive a load/save round-trip.
"""

from __future__ import annotations

import os
import sys
import threading

from . import fsutil
from .errors import ConfigError
from .models import CoreConfig, SessionConfig


def _xdg_dir(env: str, fallback: str) -> str:
    value = os.environ.get(env)
    if value:
        return value
    return os.path.join(os.path.expanduser("~"), fallback)


def default_app_data_dir(app_name: str = "marktux") -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, app_name)
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Application Support"),
                            app_name)
    return os.path.join(_xdg_dir("XDG_DATA_HOME", ".local/share"), app_name)


def default_local_data_dir(app_name: str = "marktux") -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or \
            os.path.expanduser("~")
        return os.path.join(base, app_name)
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Caches"), app_name)
    return os.path.join(_xdg_dir("XDG_CACHE_HOME", ".cache"), app_name)


class ConfigManager:
    def __init__(
        self,
        app_data_dir: str | None = None,
        local_data_dir: str | None = None,
        *,
        app_name: str = "marktux",
    ) -> None:
        self._app_dir = os.path.abspath(
            app_data_dir or default_app_data_dir(app_name)
        )
        self._local_dir = os.path.abspath(
            local_data_dir or default_local_data_dir(app_name)
        )
        self._lock = threading.RLock()
        self.core = CoreConfig()
        self.session = SessionConfig()

    # -- paths --------------------------------------------------------------

    @property
    def app_data_dir(self) -> str:
        return self._app_dir

    @property
    def local_data_dir(self) -> str:
        return self._local_dir

    @property
    def core_path(self) -> str:
        return os.path.join(self._app_dir, "vxcore.json")

    @property
    def session_path(self) -> str:
        return os.path.join(self._local_dir, "vxsession.json")

    def notebook_data_dir(self, notebook_id: str) -> str:
        return os.path.join(self._local_dir, "notebooks", notebook_id)

    def notebook_db_path(self, notebook_id: str) -> str:
        return os.path.join(self.notebook_data_dir(notebook_id), "metadata.db")

    def ensure_dirs(self) -> None:
        fsutil.ensure_dir(self._app_dir)
        fsutil.ensure_dir(self._local_dir)
        fsutil.ensure_dir(os.path.join(self._local_dir, "notebooks"))

    # -- load / save --------------------------------------------------------

    def load(self, *, load_session: bool = True) -> "ConfigManager":
        self.ensure_dirs()
        with self._lock:
            payload = fsutil.read_json(self.core_path)
            if payload is None:
                self.core = CoreConfig()
                self.save_core()
            elif not isinstance(payload, dict):
                raise ConfigError("vxcore.json must contain an object",
                                  path=self.core_path)
            else:
                self.core = CoreConfig.from_json(payload)

            if load_session:
                session_payload = fsutil.read_json(self.session_path)
                if session_payload is None:
                    self.session = SessionConfig()
                elif not isinstance(session_payload, dict):
                    raise ConfigError("vxsession.json must contain an object",
                                      path=self.session_path)
                else:
                    self.session = SessionConfig.from_json(session_payload)
        return self

    def save_core(self) -> None:
        with self._lock:
            fsutil.write_json(self.core_path, self.core.to_json())

    def save_session(self) -> None:
        with self._lock:
            fsutil.write_json(self.session_path, self.session.to_json())


__all__ = [
    "ConfigManager",
    "default_app_data_dir",
    "default_local_data_dir",
]
