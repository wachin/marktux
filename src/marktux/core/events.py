"""In-process event bus.

vxcore emits ``file.saved``, ``folder.created`` and friends through a C
callback. Here listeners are plain callables; payload keys keep vxcore's
camelCase spelling so the Qt bridge and any future sync backend can share them.

A misbehaving listener must never break a save, so exceptions are logged and
swallowed.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

FILE_CREATED = "file.created"
FILE_SAVED = "file.saved"
FILE_DELETED = "file.deleted"
FILE_MOVED = "file.moved"
FOLDER_CREATED = "folder.created"
FOLDER_DELETED = "folder.deleted"
FOLDER_CHILDREN_REORDERED = "folder.children_reordered"
NOTEBOOK_OPENED = "notebook.opened"
NOTEBOOK_CLOSED = "notebook.closed"
BUFFER_OPENED = "buffer.opened"
BUFFER_CLOSED = "buffer.closed"
BUFFER_STATE_CHANGED = "buffer.state_changed"
BUFFER_ENCODING_CHANGED = "buffer.encoding_changed"
TAGS_CHANGED = "tags.changed"
FILE_TAGS_CHANGED = "file.tags_changed"
FILE_ATTACHMENTS_CHANGED = "file.attachments_changed"
CACHE_REBUILT = "cache.rebuilt"
SEARCH_BACKEND_FALLBACK = "search.backend_fallback"
WORKSPACE_CHANGED = "workspace.changed"
SESSION_SAVED = "session.saved"

Listener = Callable[[str, dict[str, Any]], None]


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._named: dict[int, tuple[str, Listener]] = {}
        self._all: dict[int, Listener] = {}
        self._next = 1

    def subscribe(self, name: str, listener: Listener) -> int:
        with self._lock:
            token = self._next
            self._next += 1
            self._named[token] = (name, listener)
            return token

    def subscribe_all(self, listener: Listener) -> int:
        with self._lock:
            token = self._next
            self._next += 1
            self._all[token] = listener
            return token

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            self._named.pop(token, None)
            self._all.pop(token, None)

    def emit(self, name: str, payload: dict[str, Any] | None = None) -> None:
        data = dict(payload or {})
        with self._lock:
            targeted = [
                listener for event, listener in self._named.values()
                if event == name
            ]
            everyone = list(self._all.values())
        for listener in targeted + everyone:
            try:
                listener(name, data)
            except Exception:  # noqa: BLE001 - a listener must not break core
                log.exception("event listener failed for %s", name)

    def emit_async(self, name: str, payload: dict[str, Any] | None = None) -> None:
        """Emit on a short-lived daemon thread (fire-and-forget events)."""
        thread = threading.Thread(
            target=self.emit, args=(name, payload), daemon=True,
            name=f"marktux-event-{name}",
        )
        thread.start()

    def clear(self) -> None:
        with self._lock:
            self._named.clear()
            self._all.clear()


__all__ = [
    "BUFFER_CLOSED",
    "BUFFER_ENCODING_CHANGED",
    "BUFFER_OPENED",
    "BUFFER_STATE_CHANGED",
    "CACHE_REBUILT",
    "EventBus",
    "FILE_ATTACHMENTS_CHANGED",
    "FILE_CREATED",
    "FILE_TAGS_CHANGED",
    "FILE_DELETED",
    "FILE_MOVED",
    "FILE_SAVED",
    "FOLDER_CHILDREN_REORDERED",
    "FOLDER_CREATED",
    "FOLDER_DELETED",
    "NOTEBOOK_CLOSED",
    "NOTEBOOK_OPENED",
    "SEARCH_BACKEND_FALLBACK",
    "SESSION_SAVED",
    "TAGS_CHANGED",
    "WORKSPACE_CHANGED",
]
