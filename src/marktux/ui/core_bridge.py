"""Qt adapter around the Qt-free :mod:`core` package.

Every slow core operation is submitted to ``ctx.tasks`` (a pool of daemon
threads). The worker emits a Qt signal on completion; because the bridge lives
on the GUI thread, Qt auto-queues the signal to the GUI thread before any slot
runs. Tests can therefore drive either the bridge (offscreen Qt) or the core
directly (headless) with the same semantics.

Phase 1 wires ``open_notebook`` and ``list_children``. The remaining signals
(buffer/search/cache) are declared up front so phase 2+ only add methods, never
change the wiring in ``main.py``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from PyQt6.QtCore import QObject, pyqtSignal

from marktux.core import CancelledError, CancellationToken, CoreContext

log = logging.getLogger(__name__)


def _message(error: BaseException) -> str:
    """Human-readable one-liner for the ``error`` signal."""
    text = str(error).strip()
    return text or error.__class__.__name__


class CoreBridge(QObject):
    # -- notebook / tree ----------------------------------------------------
    notebookOpened = pyqtSignal(object)   # NotebookInfo
    notebookClosed = pyqtSignal(str)      # notebook id
    childrenLoaded = pyqtSignal(str, str, object)  # notebook id, rel, FolderContents
    nodeChanged = pyqtSignal(str)         # notebook id whose tree changed (CRUD)

    # -- buffers (phases 2+) -------------------------------------------------
    bufferOpened = pyqtSignal(object)     # BufferInfo
    bufferContentLoaded = pyqtSignal(str, str)  # buffer id, text
    bufferModified = pyqtSignal(str, bool)      # buffer id, modified
    bufferStateChanged = pyqtSignal(str, str)   # buffer id, state name
    saved = pyqtSignal(str)               # buffer id

    # -- search / cache (phases 5 / 8) --------------------------------------
    searchBatch = pyqtSignal(object)      # SearchBatch (one per streamed chunk)
    searchFinished = pyqtSignal(str)      # query id
    backendFallback = pyqtSignal(str, object)  # notebook id, payload
    cacheRebuilt = pyqtSignal(str, object)  # notebook id, RebuildStats

    # -- errors -------------------------------------------------------------
    error = pyqtSignal(str, str)          # operation, message

    def __init__(self, ctx: CoreContext, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._tasks: set[Any] = set()
        self._tasks_lock = threading.Lock()
        self._search_tokens: dict[str, CancellationToken] = {}
        self._shutdown = False

        # Re-broadcast core search events on the GUI thread.
        events = getattr(ctx, "events", None)
        if events is not None:
            events.subscribe("search.backend_fallback",
                             self._on_backend_fallback)

    # -- generic submission -------------------------------------------------

    def _submit(
        self,
        fn: Callable[[], Any],
        *,
        op: str,
        on_done: Callable[[Any], None] | None = None,
    ) -> Any | None:
        """Run ``fn`` on the core pool; route the result/error to signals.

        The completion callback runs on the worker thread; emitting from there
        is safe because every receiver lives on the GUI thread (auto-queue).
        """
        if self._shutdown:
            self.error.emit(op, "core is shutting down")
            return None
        try:
            task = self.ctx.tasks.submit(fn)
        except Exception as error:  # noqa: BLE001 - surface to the UI
            self.error.emit(op, _message(error))
            return None

        with self._tasks_lock:
            self._tasks.add(task)

        def finished(done_task: Any) -> None:
            with self._tasks_lock:
                self._tasks.discard(done_task)
            if done_task.cancelled:
                return
            exc = done_task.exception()
            if exc is not None:
                self.error.emit(op, _message(exc))
                return
            if on_done is not None:
                try:
                    on_done(done_task.result())
                except Exception as error:  # noqa: BLE001
                    log.exception("callback failed for %s", op)
                    self.error.emit(op, _message(error))

        task.add_done_callback(finished)
        return task

    # -- notebooks ----------------------------------------------------------

    def open_notebook(self, root: str, **kwargs: Any) -> Any | None:
        """Open ``root`` in the background; emit ``notebookOpened`` on success."""
        return self._submit(
            lambda: self.ctx.open_notebook(root, **kwargs),
            op="open_notebook",
            on_done=lambda notebook: self.notebookOpened.emit(notebook.info()),
        )

    def close_notebook(self, notebook_id: str) -> None:
        self._submit(
            lambda: self.ctx.close_notebook(notebook_id),
            op="close_notebook",
            on_done=lambda _: self.notebookClosed.emit(notebook_id),
        )

    # ``ctx.notebooks.list()`` is a handful of dict lookups — no need to offload,
    # but routing through the pool keeps one code path and one error channel.
    def list_notebooks(self) -> Any | None:
        return self._submit(
            lambda: self.ctx.notebooks.list(),
            op="list_notebooks",
        )

    def list_children(self, notebook_id: str, rel: str = "") -> Any | None:
        """List one tree level; emit ``childrenLoaded(notebook_id, rel, contents)``."""
        def work() -> Any:
            notebook = self.ctx.notebooks.get(notebook_id)
            return notebook.folders.list_children(rel)

        return self._submit(
            work,
            op="list_children",
            on_done=lambda contents: self.childrenLoaded.emit(
                notebook_id, rel, contents
            ),
        )

    # -- tags (phase 4) -----------------------------------------------------
    tagsLoaded = pyqtSignal(str, object)          # notebook id, list[TagNode]
    tagFilesLoaded = pyqtSignal(str, object, object)  # notebook id, tags, results
    tagCountsLoaded = pyqtSignal(str, object)     # notebook id, dict[str, int]

    # -- node CRUD (phase 3) ------------------------------------------------

    def _node_mutation(self, fn: Callable[[], Any], *, op: str,
                       notebook_id: str) -> Any | None:
        """Submit a structural mutation; emit ``nodeChanged`` on success."""
        return self._submit(
            fn, op=op,
            on_done=lambda _: self.nodeChanged.emit(notebook_id),
        )

    def create_folder(self, notebook_id: str, parent: str, name: str):
        def work():
            return self.ctx.notebooks.get(notebook_id).folders.create_folder(
                parent, name)

        return self._node_mutation(
            work, op="create_folder", notebook_id=notebook_id)

    def create_file(self, notebook_id: str, parent: str, name: str,
                    content: str = ""):
        def work():
            return self.ctx.notebooks.get(notebook_id).folders.create_file(
                parent, name, content)

        return self._node_mutation(
            work, op="create_file", notebook_id=notebook_id)

    def rename_file(self, notebook_id: str, rel: str, new_name: str):
        def work():
            return self.ctx.notebooks.get(notebook_id).folders.rename_file(
                rel, new_name)

        return self._node_mutation(
            work, op="rename_file", notebook_id=notebook_id)

    def move_file(self, notebook_id: str, rel: str, dest_folder: str):
        def work():
            return self.ctx.notebooks.get(notebook_id).folders.move_file(
                rel, dest_folder)

        return self._node_mutation(
            work, op="move_file", notebook_id=notebook_id)

    def copy_file(self, notebook_id: str, rel: str, dest_folder: str,
                  new_name: str | None = None):
        def work():
            return self.ctx.notebooks.get(notebook_id).folders.copy_file(
                rel, dest_folder, new_name)

        return self._node_mutation(
            work, op="copy_file", notebook_id=notebook_id)

    def delete_file(self, notebook_id: str, rel: str):
        def work():
            return self.ctx.notebooks.get(notebook_id).folders.delete_file(rel)

        return self._node_mutation(
            work, op="delete_file", notebook_id=notebook_id)

    def delete_folder(self, notebook_id: str, rel: str):
        def work():
            return self.ctx.notebooks.get(notebook_id).folders.delete_folder(rel)

        return self._node_mutation(
            work, op="delete_folder", notebook_id=notebook_id)

    # -- tags (phase 4) -----------------------------------------------------

    def list_tags(self, notebook_id: str):
        def work():
            return self.ctx.notebooks.get(notebook_id).tags.list()

        return self._submit(
            work, op="list_tags",
            on_done=lambda tags: self.tagsLoaded.emit(notebook_id, tags),
        )

    def _tag_mutation(self, fn: Callable[[], Any], *, op: str,
                      notebook_id: str):
        """Submit a tag mutation; reload the forest on success."""
        return self._submit(
            fn, op=op,
            on_done=lambda _: self.list_tags(notebook_id),
        )

    def create_tag(self, notebook_id: str, name: str, parent: str = ""):
        def work():
            return self.ctx.notebooks.get(notebook_id).tags.create(name, parent)

        return self._tag_mutation(
            work, op="create_tag", notebook_id=notebook_id)

    def delete_tag(self, notebook_id: str, name: str):
        def work():
            return self.ctx.notebooks.get(notebook_id).tags.delete(name)

        return self._tag_mutation(
            work, op="delete_tag", notebook_id=notebook_id)

    def move_tag(self, notebook_id: str, name: str, parent: str = ""):
        def work():
            return self.ctx.notebooks.get(notebook_id).tags.move(name, parent)

        return self._tag_mutation(
            work, op="move_tag", notebook_id=notebook_id)

    def tag_file(self, notebook_id: str, path: str, tag: str):
        def work():
            self.ctx.notebooks.get(notebook_id).tags.tag_file(path, tag)
            return None

        return self._tag_mutation(
            work, op="tag_file", notebook_id=notebook_id)

    def untag_file(self, notebook_id: str, path: str, tag: str):
        def work():
            self.ctx.notebooks.get(notebook_id).tags.untag_file(path, tag)
            return None

        return self._tag_mutation(
            work, op="untag_file", notebook_id=notebook_id)

    def set_file_tags(self, notebook_id: str, path: str, tags: list[str]):
        def work():
            self.ctx.notebooks.get(notebook_id).tags.update_file_tags(path, tags)
            return None

        return self._tag_mutation(
            work, op="set_file_tags", notebook_id=notebook_id)

    def find_tag_files(self, notebook_id: str, tags: list[str],
                       operator: str = "AND"):
        def work():
            return self.ctx.notebooks.get(notebook_id).tags.files(tags, operator)

        return self._submit(
            work, op="find_tag_files",
            on_done=lambda results: self.tagFilesLoaded.emit(
                notebook_id, tags, results),
        )

    def tag_counts(self, notebook_id: str):
        def work():
            return self.ctx.notebooks.get(notebook_id).tags.count()

        return self._submit(
            work, op="tag_counts",
            on_done=lambda counts: self.tagCountsLoaded.emit(
                notebook_id, counts),
        )

    # -- search (phase 5) ---------------------------------------------------

    def _on_backend_fallback(self, _name: str, payload: dict[str, Any]) -> None:
        self.backendFallback.emit(str(payload.get("notebookId", "")), payload)

    def search(self, notebook_id: str, query: Any, *, query_id: str = "",
               batch_size: int | None = None) -> Any | None:
        """Run a content search off-thread, streaming ``searchBatch`` chunks.

        A :class:`SearchBatch` is emitted for every chunk (including empty
        ones) and ``searchFinished(query_id)`` fires on completion.  Pass the
        same ``query_id`` to :meth:`cancel_search` to stop early; a cancelled
        search finishes quietly without an ``error`` signal.
        """
        token = CancellationToken()
        with self._tasks_lock:
            self._search_tokens[query_id] = token

        def work() -> None:
            notebook = self.ctx.notebooks.get(notebook_id)
            try:
                notebook.search.search_stream(
                    query, batch_size=batch_size, cancel=token,
                    on_batch=self.searchBatch.emit,
                )
            except CancelledError:
                return

        def on_done(_result: Any) -> None:
            with self._tasks_lock:
                self._search_tokens.pop(query_id, None)
            self.searchFinished.emit(query_id)

        return self._submit(work, op="search", on_done=on_done)

    def cancel_search(self, query_id: str) -> None:
        """Cancel the in-flight search registered under ``query_id``."""
        with self._tasks_lock:
            token = self._search_tokens.get(query_id)
        if token is not None:
            token.cancel()

    # -- teardown -----------------------------------------------------------

    def shutdown(self) -> None:
        """Cancel pending work, snapshot the session and close the core."""
        if self._shutdown:
            return
        self._shutdown = True
        with self._tasks_lock:
            tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        try:
            self.ctx.prepare_shutdown()
        except Exception:  # noqa: BLE001 - never block shutdown
            log.exception("cannot prepare core shutdown")
        self.ctx.close()


__all__ = ["CoreBridge"]
