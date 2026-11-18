"""Content search with simple and ripgrep backends, ported from vxcore.

The **source of truth** for searchable files is the notebook's index; a query
scans only indexed notes.  Matches carry 1-based line numbers and 0-based
character columns (exclusive end), matching vxcore's ``lineNumber`` /
``columnStart`` / ``columnEnd`` payload.

Two backends share one file-discovery + decoding layer:

* :class:`SimpleSearchBackend` — Python ``re``, always available.
* :class:`RgSearchBackend` — shells out to ``rg --json``.  Byte offsets from
  rg are converted to Python character columns; if ``rg`` is unavailable the
  manager falls back to ``simple`` and emits ``search.backend_fallback``.

Large notebooks are streamed in index-order chunks of ``chunk_size`` files.
A batch is emitted for every chunk, including chunks with zero matches, so a
UI can show deterministic progress.  ``max_results`` caps total match
occurrences across the whole search and sets ``truncated`` on the blob result.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from .concurrency import CancellationToken
from .errors import InvalidQueryError, SearchError
from .models import (
    ContentQuery,
    FileQuery,
    SearchBatch,
    SearchBatchResult,
    SearchMatch,
    SearchOption,
    SearchResult,
    SearchScope,
)

# Python codec names for the encoder strings vxcore exposes.
_CODECS = {
    "utf-8": "utf-8", "utf8": "utf-8",
    "gb18030": "gb18030", "gbk": "gbk", "gb2312": "gb2312",
    "utf-16": "utf-16", "utf-16le": "utf-16-le", "utf-16be": "utf-16-be",
    "latin1": "latin-1", "latin-1": "latin-1", "ascii": "ascii",
}


@dataclass(slots=True)
class _File:
    """One indexed file prepared for scanning."""

    path: str          # relative, '/'-separated
    file_id: str
    name: str
    tags: tuple[str, ...]
    abs_path: str = ""
    root: str = ""
    text: str | None = None
    encoding: str = "utf-8"


class SearchBackend:
    """Backend contract: turn prepared files + query into match lists."""

    name = "base"

    def matches(self, files: list[_File], query: ContentQuery,
                cancel: CancellationToken | None = None,
                on_file: Callable[[_File], None] | None = None
                ) -> list[list[SearchMatch]]:
        raise NotImplementedError


class SimpleSearchBackend(SearchBackend):
    name = "simple"

    def __init__(self) -> None:
        self._regex: re.Pattern[str] | None = None
        self._exclude: list[re.Pattern[str]] = []

    def prepare(self, query: ContentQuery) -> None:
        """Compile patterns once, before any file is read.

        An invalid regex raises :class:`InvalidQueryError` here so callers get
        an error before a single file is scanned.
        """
        pattern = query.pattern or ""
        flags = 0 if query.options & SearchOption.CASE_SENSITIVE else re.IGNORECASE
        if query.options & SearchOption.REGEX:
            source = pattern
        else:
            source = re.escape(pattern)
        if query.options & SearchOption.WHOLE_WORD:
            source = r"(?<!\w)(?:" + source + r")(?!\w)"
        try:
            self._regex = re.compile(source, flags)
        except re.error as error:
            raise InvalidQueryError(f"invalid regular expression: {error}") from error
        self._exclude = []
        for item in query.exclude_patterns:
            try:
                self._exclude.append(re.compile(item, re.IGNORECASE))
            except re.error as error:
                raise InvalidQueryError(f"invalid exclude pattern: {error}") from error

    def matches(self, files: list[_File], query: ContentQuery,
                cancel: CancellationToken | None = None,
                on_file: Callable[[_File], None] | None = None
                ) -> list[list[SearchMatch]]:
        if self._regex is None or not query.pattern:
            return [[] for _ in files]
        out: list[list[SearchMatch]] = []
        first_only = bool(query.options & SearchOption.FIRST_MATCH_PER_FILE)
        for item in files:
            if cancel is not None:
                cancel.raise_if_cancelled()
            if on_file is not None:
                on_file(item)
            out.append(self._scan_text(item.text or "", first_only))
        return out

    def _scan_text(self, text: str, first_only: bool) -> list[SearchMatch]:
        assert self._regex is not None
        found: list[SearchMatch] = []
        for number, line in enumerate(text.splitlines(), start=1):
            if any(pattern.search(line) for pattern in self._exclude):
                continue
            for match in self._regex.finditer(line):
                found.append(SearchMatch(
                    line_number=number, column_start=match.start(),
                    column_end=match.end(), line_text=line,
                ))
                if first_only:
                    return found
        return found


class RgSearchBackend(SearchBackend):
    """ripgrep-backed matcher; results are byte-offsets accurate to ``simple``.

    rg is grouped by file encoding (via ``--encoding``) so GB18030 notes match
    too.  Its JSON ``start``/``end`` are byte offsets inside the UTF-8 line;
    they are converted to Python character columns before returning.
    """

    name = "rg"

    def __init__(self, *, executable: str = "rg") -> None:
        self._exe = executable
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        if self._available is None:
            self._available = shutil.which(self._exe) is not None
        return self._available

    def prepare(self, query: ContentQuery) -> None:
        self._query = query
        self._exclude = [re.compile(item, re.IGNORECASE)
                         for item in query.exclude_patterns]

    def _args(self, encoding: str, paths: Sequence[str]) -> list[str]:
        query: ContentQuery = self._query
        args = [self._exe, "--json", "--color", "never", "--no-config",
                "--encoding", encoding]
        if not (query.options & SearchOption.CASE_SENSITIVE):
            args.append("-i")
        if query.options & SearchOption.WHOLE_WORD:
            args.append("-w")
        if query.options & SearchOption.REGEX:
            args.extend(["-e", query.pattern])
        else:
            args.extend(["-F", "-e", query.pattern])
        args.append("--")
        args.extend(path.replace("/", os.sep) for path in paths)
        return args

    def matches(self, files: list[_File], query: ContentQuery,
                cancel: CancellationToken | None = None,
                on_file: Callable[[_File], None] | None = None
                ) -> list[list[SearchMatch]]:
        if not query.pattern or not files:
            return [[] for _ in files]
        by_path = {item.path: [] for item in files}
        by_encoding: dict[str, list[_File]] = defaultdict(list)
        for item in files:
            by_encoding[_codec_for(item.encoding)].append(item)

        first_only = bool(query.options & SearchOption.FIRST_MATCH_PER_FILE)
        for codec, group in by_encoding.items():
            if cancel is not None:
                cancel.raise_if_cancelled()
            root = group[0].root
            try:
                proc = subprocess.run(
                    self._args(codec, [item.path.replace("/", os.sep)
                                       for item in group]),
                    cwd=root, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", check=False,
                )
            except OSError as error:  # rg disappeared between check and run
                raise SearchError(f"ripgrep failed: {error}") from error
            for line in proc.stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") != "match":
                    continue
                data = event["data"]
                raw_path = data.get("path", {}).get("text", "")
                rel = raw_path.replace(os.sep, "/")
                if rel not in by_path:
                    continue
                line_text = data.get("lines", {}).get("text", "").rstrip("\r\n")
                if any(p.search(line_text) for p in self._exclude):
                    continue
                number = int(data.get("line_number", 0))
                line_bytes = line_text.encode("utf-8")
                for sub in data.get("submatches", []):
                    start_byte = int(sub.get("start", 0))
                    end_byte = int(sub.get("end", 0))
                    start = len(line_bytes[:start_byte].decode("utf-8", "replace"))
                    end = len(line_bytes[:end_byte].decode("utf-8", "replace"))
                    by_path[rel].append(SearchMatch(
                        line_number=number, column_start=start,
                        column_end=end, line_text=line_text,
                    ))
        if on_file is not None:
            for item in files:
                on_file(item)
        result = [by_path.get(item.path, []) for item in files]
        if first_only:
            result = [matches[:1] for matches in result]
        return result


def _codec_for(encoding: str) -> str:
    return _CODECS.get(encoding.lower(), encoding)


class SearchManager:
    """Coordinate file discovery, decoding, backends and result streaming."""

    def __init__(
        self,
        notebook: Any,
        backends: Sequence[str] | None = None,
        *,
        encodings: Sequence[str] = ("UTF-8", "GB18030"),
        file_types: Any = None,
        chunk_size: int = 64,
    ) -> None:
        self.notebook = notebook
        self.encodings = list(encodings) or ["UTF-8"]
        self.file_types = file_types
        self.chunk_size = max(1, int(chunk_size))
        names = list(backends) if backends else ["simple", "rg"]
        self._simple = SimpleSearchBackend()
        self._rg = RgSearchBackend()
        self._backends = names
        self._active = self._select_backend(names)

    # -- backend selection --------------------------------------------------

    def _events(self) -> Any:
        return getattr(self.notebook.manager, "_events", None)

    def _select_backend(self, names: Sequence[str]) -> SearchBackend:
        # rg is the fast path; prefer it when configured and available. If it
        # is configured but missing, announce the fallback and use simple.
        if "rg" in names:
            if self._rg.available:
                return self._rg
            events = self._events()
            if events is not None:
                events.emit("search.backend_fallback", {
                    "notebookId": self.notebook.id,
                    "from": "rg", "to": "simple",
                })
        return self._simple

    # -- file discovery -----------------------------------------------------

    def _collect(self, scope: SearchScope,
                 explicit: Sequence[str] | None) -> list[_File]:
        """Return candidate files in index order, applying scope filters."""
        by_path: dict[str, tuple[Any, str]] = {}
        for path, record in self.notebook.folders.iterate_all_files():
            by_path[path] = (record, path)
        store = self.notebook.store
        items: list[_File] = []
        base = scope.folder_path.strip("/")

        names = [self.notebook.clean_relative_path(name) for name in explicit] \
            if explicit is not None else list(by_path)

        for path in names:
            entry = by_path.get(path)
            if entry is None:
                continue
            record = entry[0]
            if base:
                if path == base or path.startswith(base + "/"):
                    pass
                else:
                    continue
                if not scope.recursive and "/" in path[len(base) + 1:].strip("/"):
                    continue
            elif not scope.recursive and "/" in path:
                continue
            if not self._path_allowed(path, scope):
                continue
            tags = tuple(store.get_file_tags(record.id))
            if scope.tags and not self._tags_match(tags, scope):
                continue
            if scope.exclude_tags and any(t in tags for t in scope.exclude_tags):
                continue
            items.append(_File(
                path=path, file_id=record.id, name=record.name,
                tags=tags, abs_path=self.notebook.absolute_path(path),
                root=self.notebook.root,
            ))
        return items

    @staticmethod
    def _path_allowed(path: str, scope: SearchScope) -> bool:
        targets = [path, path.rsplit("/", 1)[-1]]
        if scope.path_patterns:
            if not any(fnmatch.fnmatch(t, p) for t in targets
                       for p in scope.path_patterns):
                return False
        if scope.exclude_path_patterns and any(
            fnmatch.fnmatch(t, p) for t in targets
            for p in scope.exclude_path_patterns
        ):
            return False
        return True

    @staticmethod
    def _tags_match(tags: tuple[str, ...], scope: SearchScope) -> bool:
        wanted = list(scope.tags)
        if scope.tag_operator.upper() == "OR":
            return any(tag in tags for tag in wanted)
        return all(tag in tags for tag in wanted)

    def _decode(self, items: list[_File]) -> None:
        """Fill each file's text using the first encoding that decodes it."""
        for item in items:
            try:
                with open(item.abs_path, "rb") as handle:
                    raw = handle.read()
            except OSError:
                item.text = ""
                continue
            text = ""
            used = "utf-8"
            for enc in self.encodings:
                codec = _codec_for(enc)
                try:
                    text = raw.decode(codec)
                    used = enc
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                # Undecodable bytes: keep searching with replacement chars.
                text = raw.decode(_codec_for(self.encodings[0]), errors="replace")
                used = self.encodings[0]
            item.text = text
            item.encoding = used

    # -- preparation --------------------------------------------------------

    def _prepare(self, query: ContentQuery,
                 files: Sequence[str] | None = None
                 ) -> list[_File]:
        if not query.pattern:
            return []
        # Compile the query before scanning (invalid regex raises here).
        backend = self._active
        if hasattr(backend, "prepare"):
            backend.prepare(query)
        items = self._collect(query.scope, files)
        self._decode(items)
        return items

    @staticmethod
    def _to_results(notebook_id: str, items: list[_File],
                    matches: list[list[SearchMatch]]) -> list[SearchResult]:
        results: list[SearchResult] = []
        for item, found in zip(items, matches):
            if not found:
                continue
            results.append(SearchResult(
                notebook_id=notebook_id, path=item.path, file_id=item.file_id,
                name=item.name, matches=tuple(found), tags=item.tags,
            ))
        return results

    # -- blob API -----------------------------------------------------------

    def search(self, query: ContentQuery,
               files: Sequence[str] | None = None,
               cancel: CancellationToken | None = None) -> SearchBatchResult:
        """Run a full search and return all results, honouring ``max_results``."""
        items = self._prepare(query, files)
        matches = self._active.matches(items, query, cancel)
        results = self._to_results(self.notebook.id, items, matches)
        total = sum(len(result.matches) for result in results)
        truncated = total > query.max_results
        if truncated:
            kept: list[SearchResult] = []
            remaining = query.max_results
            for result in results:
                if remaining <= 0:
                    break
                matches_kept = result.matches[:remaining]
                kept.append(SearchResult(
                    notebook_id=result.notebook_id, path=result.path,
                    file_id=result.file_id, name=result.name,
                    matches=matches_kept, tags=result.tags,
                ))
                remaining -= len(matches_kept)
            results = kept
        return SearchBatchResult(results=tuple(results), truncated=truncated)

    # -- streaming API ------------------------------------------------------

    def search_stream(
        self,
        query: ContentQuery,
        files: Sequence[str] | None = None,
        *,
        batch_size: int | None = None,
        cancel: CancellationToken | None = None,
        on_batch: Callable[[SearchBatch], None] | None = None,
    ) -> list[SearchBatch]:
        """Yield index-order chunks (including empty ones) as they are scanned.

        ``total`` is the number of chunks; streaming owns no truncation.
        """
        size = max(1, int(batch_size or self.chunk_size))
        items = self._prepare(query, files)
        chunks = [items[i:i + size] for i in range(0, len(items), size)]
        total = len(chunks)
        batches: list[SearchBatch] = []
        for index, chunk in enumerate(chunks):
            if cancel is not None:
                cancel.raise_if_cancelled()
            matches = self._active.matches(chunk, query, cancel)
            results = self._to_results(self.notebook.id, chunk, matches)
            batch = SearchBatch(index=index, total=total, results=tuple(results))
            batches.append(batch)
            if on_batch is not None:
                on_batch(batch)
        return batches

    # -- file-name search ---------------------------------------------------

    def find_files(self, query: FileQuery) -> list[dict[str, Any]]:
        """Search indexed file/folder names (vxcore ``vxcore_search_files``)."""
        pattern = query.pattern or ""
        scope = query.scope
        found: list[tuple[int, str, str, str]] = []  # rank, path, id, name
        for path, record in self.notebook.folders.iterate_all_files():
            if not self._path_allowed(path, scope):
                continue
            name = record.name
            targets: list[tuple[int, str]] = []
            if query.match_target in ("name", "name_and_path"):
                targets.append((0 if name.casefold().startswith(pattern.casefold()) else 1, name))
            if query.match_target in ("path", "name_and_path"):
                targets.append((2, path))
            if not any(fnmatch.fnmatch(t, f"*{pattern}*") for _, t in targets):
                continue
            rank = min(rank for rank, _ in targets)
            found.append((rank, path, record.id, name))
        found.sort(key=lambda item: (item[0], item[1].casefold()))
        return [
            {"path": path, "id": file_id, "name": name}
            for _, path, file_id, name in found[:query.max_results]
        ]


__all__ = [
    "SearchManager", "SearchBackend", "SimpleSearchBackend", "RgSearchBackend",
]
