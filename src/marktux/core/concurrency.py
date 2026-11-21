"""Background execution and cancellation.

vxcore exposes named work queues that the *caller's* threads must drain. That
contract exists because the C library cannot own threads; in Python we can, so
the core keeps its own small pool of **daemon** threads and returns a ``Task``
handle. Daemon threads matter: a stuck search or a blocked network mount must
never keep the process alive after the window closes.

The core API stays synchronous. Callers that need responsiveness (the Qt
bridge) submit a function and react to a signal; tests call the same functions
directly.
"""

from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import Future
from concurrent.futures import CancelledError as _FutureCancelled
from typing import Any, Callable, Generic, TypeVar

from .errors import CancelledError

log = logging.getLogger(__name__)

T = TypeVar("T")

_SENTINEL = object()


class CancellationToken:
    """Cooperative cancellation flag shared by a task and its caller."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledError("operation cancelled")

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


class Task(Generic[T]):
    """Handle for work submitted to a :class:`TaskRunner`."""

    __slots__ = ("future", "token")

    def __init__(self, future: Future, token: CancellationToken) -> None:
        self.future = future
        self.token = token

    def cancel(self) -> bool:
        self.token.cancel()
        return self.future.cancel()

    @property
    def cancelled(self) -> bool:
        return self.token.cancelled or self.future.cancelled()

    @property
    def done(self) -> bool:
        return self.future.done()

    def result(self, timeout: float | None = None) -> T:
        try:
            return self.future.result(timeout)
        except _FutureCancelled:
            raise CancelledError("operation cancelled") from None

    def exception(self, timeout: float | None = None) -> BaseException | None:
        return self.future.exception(timeout)

    def add_done_callback(self, fn: Callable[["Task[T]"], None]) -> None:
        self.future.add_done_callback(lambda _future: fn(self))


class TaskRunner:
    """Fixed pool of daemon threads with cancellation-aware tasks."""

    def __init__(self, max_workers: int = 3, name: str = "marktux-core") -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._queue: queue.Queue = queue.Queue()
        self._threads = [
            threading.Thread(
                target=self._work, daemon=True, name=f"{name}-{index}"
            )
            for index in range(max_workers)
        ]
        self._closed = False
        for thread in self._threads:
            thread.start()

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> Task[T]:
        future: Future = Future()
        token = CancellationToken()
        if self._closed:
            raise RuntimeError("task runner is shut down")
        self._queue.put((future, token, fn, args, kwargs))
        return Task(future, token)

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                return
            future, token, fn, args, kwargs = item
            if token.cancelled or not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as error:  # noqa: BLE001 - propagate to caller
                future.set_exception(error)
            else:
                future.set_result(result)

    def shutdown(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        # Cancel everything still queued, then let the workers drain and exit.
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _SENTINEL:
                continue
            future, token, *_rest = item
            token.cancel()
            future.cancel()
        for _ in self._threads:
            self._queue.put(_SENTINEL)
        if wait:
            for thread in self._threads:
                thread.join(timeout)

    @property
    def closed(self) -> bool:
        return self._closed


class BufferWatchdog:
    """Poll loaded buffers for external modification on a daemon thread."""

    def __init__(self, buffers: Any, events: Any, interval: float = 1.5) -> None:
        self._buffers = buffers
        self._events = events
        self._interval = max(0.1, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="marktux-buffer-watchdog"
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - polling must never die
                log.exception("external-change poll failed")

    def poll_once(self) -> dict[str, Any]:
        changes = self._buffers.check_external_changes()
        for buffer_id, state in changes.items():
            self._events.emit(
                "buffer.state_changed",
                {"bufferId": buffer_id, "state": str(state)},
            )
        return changes


__all__ = ["BufferWatchdog", "CancellationToken", "Task", "TaskRunner"]
