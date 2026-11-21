"""Dataclasses, enums and JSON codecs for the core.

Two families live here:

* **Persisted records** (``NotebookConfig``, ``FolderConfig``, ``FileRecord``,
  ``WorkspaceConfig``, ``SessionConfig``, ``CoreConfig``) — mutable, with
  ``from_json``/``to_json``. Every one keeps unknown keys in ``extra`` so a file
  written by a newer VNote survives a marktux round-trip.
* **Read-only views** (``NodeRef``, ``FolderContents``, ``NotebookInfo``,
  ``BufferInfo``, search results) — frozen, returned to callers and to Qt.

Timestamps are integer milliseconds, matching vxcore's ``*Utc`` fields.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import IntFlag, StrEnum
from typing import Any, Mapping


# -- enums ------------------------------------------------------------------

class NotebookType(StrEnum):
    BUNDLED = "bundled"
    RAW = "raw"


class BufferState(StrEnum):
    NORMAL = "normal"
    FILE_MISSING = "file_missing"
    FILE_CHANGED = "file_changed"
    SAVE_FAILED = "save_failed"


class LineEnding(StrEnum):
    UNSPECIFIED = "unspecified"
    LF = "lf"
    CRLF = "crlf"
    CR = "cr"


class SearchOption(IntFlag):
    NONE = 0
    CASE_SENSITIVE = 1 << 0
    WHOLE_WORD = 1 << 1
    REGEX = 1 << 2
    FIRST_MATCH_PER_FILE = 1 << 3


# -- helpers ----------------------------------------------------------------

def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _take_extra(payload: Mapping[str, Any], known: set[str]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in known}


def _as_int(value: Any, default: int = 0) -> int:
    # bool is an int subclass; timestamps are never booleans.
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


# -- notebook configuration -------------------------------------------------

@dataclass(slots=True)
class TagNode:
    name: str
    parent: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "TagNode":
        known = {"name", "parent", "metadata"}
        return cls(
            name=str(payload.get("name", "")),
            parent=str(payload.get("parent", "")),
            metadata=_as_dict(payload.get("metadata")),
            extra=_take_extra(payload, known),
        )

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "parent": self.parent,
            "metadata": dict(self.metadata),
        }
        data.update(self.extra)
        return data

    def clone(self) -> "TagNode":
        return TagNode.from_json(self.to_json())


@dataclass(slots=True)
class NotebookConfig:
    id: str = ""
    name: str = ""
    description: str = ""
    assets_folder: str = "vx_assets"
    recycle_bin_folder: str = "vx_notebook/recycle_bin"
    metadata: dict[str, Any] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)
    tags: list[TagNode] = field(default_factory=list)
    tags_modified_utc: int = 0
    sync_enabled: bool = False
    sync_backend: str = ""
    sync_remote_url: str = ""
    auto_sync_enabled: bool = True
    encryption_initialized: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "NotebookConfig":
        known = {
            "id", "name", "description", "assetsFolder", "recycleBinFolder",
            "metadata", "tags", "tagsModifiedUtc", "ignored", "syncEnabled",
            "syncBackend", "syncRemoteUrl", "autoSyncEnabled",
            "encryptionInitialized",
        }
        tags = [TagNode.from_json(item) for item in _as_list(payload.get("tags"))
                if isinstance(item, Mapping)]
        encryption = payload.get("encryptionInitialized")
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            description=str(payload.get("description", "")),
            assets_folder=str(payload.get("assetsFolder", "vx_assets")),
            recycle_bin_folder=str(
                payload.get("recycleBinFolder", "vx_notebook/recycle_bin")
            ),
            metadata=_as_dict(payload.get("metadata")),
            ignored=[str(item) for item in _as_list(payload.get("ignored"))],
            tags=tags,
            tags_modified_utc=_as_int(payload.get("tagsModifiedUtc")),
            sync_enabled=bool(payload.get("syncEnabled", False)),
            sync_backend=str(payload.get("syncBackend", "")),
            sync_remote_url=str(payload.get("syncRemoteUrl", "")),
            auto_sync_enabled=bool(payload.get("autoSyncEnabled", True)),
            encryption_initialized=(
                bool(encryption) if isinstance(encryption, bool) else None
            ),
            extra=_take_extra(payload, known),
        )

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "assetsFolder": self.assets_folder,
            "recycleBinFolder": self.recycle_bin_folder,
            "metadata": dict(self.metadata),
            "tags": [tag.to_json() for tag in self.tags],
            "tagsModifiedUtc": self.tags_modified_utc,
            "ignored": list(self.ignored),
            "syncEnabled": self.sync_enabled,
            "syncBackend": self.sync_backend,
            "syncRemoteUrl": self.sync_remote_url,
            "autoSyncEnabled": self.auto_sync_enabled,
        }
        if self.encryption_initialized is not None:
            data["encryptionInitialized"] = self.encryption_initialized
        data.update(self.extra)
        return data

    def clone(self) -> "NotebookConfig":
        return NotebookConfig.from_json(self.to_json())


# -- folder / file configuration -------------------------------------------

@dataclass(slots=True)
class FileRecord:
    id: str
    name: str
    created_utc: int = 0
    modified_utc: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def new(cls, name: str) -> "FileRecord":
        stamp = _now_ms()
        return cls(id=str(uuid.uuid4()), name=name,
                   created_utc=stamp, modified_utc=stamp)

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "FileRecord":
        known = {"id", "name", "createdUtc", "modifiedUtc", "metadata",
                 "tags", "attachments"}
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            created_utc=_as_int(payload.get("createdUtc")),
            modified_utc=_as_int(payload.get("modifiedUtc")),
            metadata=_as_dict(payload.get("metadata")),
            tags=[str(item) for item in _as_list(payload.get("tags"))],
            attachments=[str(item) for item in _as_list(payload.get("attachments"))],
            extra=_take_extra(payload, known),
        )

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "createdUtc": self.created_utc,
            "modifiedUtc": self.modified_utc,
            "metadata": dict(self.metadata),
        }
        if self.tags:
            data["tags"] = list(self.tags)
        if self.attachments:
            data["attachments"] = list(self.attachments)
        data.update(self.extra)
        return data

    def clone(self) -> "FileRecord":
        return FileRecord.from_json(self.to_json())


@dataclass(slots=True)
class FolderConfig:
    id: str
    name: str
    created_utc: int = 0
    modified_utc: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    files: list[FileRecord] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def new(cls, name: str) -> "FolderConfig":
        stamp = _now_ms()
        return cls(id=str(uuid.uuid4()), name=name,
                   created_utc=stamp, modified_utc=stamp)

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "FolderConfig":
        known = {"id", "name", "createdUtc", "modifiedUtc", "metadata",
                 "files", "folders"}
        files = [FileRecord.from_json(item)
                 for item in _as_list(payload.get("files"))
                 if isinstance(item, Mapping)]
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            created_utc=_as_int(payload.get("createdUtc")),
            modified_utc=_as_int(payload.get("modifiedUtc")),
            metadata=_as_dict(payload.get("metadata")),
            files=files,
            folders=[str(item) for item in _as_list(payload.get("folders"))],
            extra=_take_extra(payload, known),
        )

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "createdUtc": self.created_utc,
            "modifiedUtc": self.modified_utc,
            "metadata": dict(self.metadata),
            "files": [item.to_json() for item in self.files],
            "folders": list(self.folders),
        }
        data.update(self.extra)
        return data

    def clone(self) -> "FolderConfig":
        return FolderConfig.from_json(self.to_json())

    def file(self, name: str) -> FileRecord | None:
        for record in self.files:
            if record.name == name:
                return record
        return None


# -- session ----------------------------------------------------------------

@dataclass(slots=True)
class NotebookRecord:
    id: str
    root_folder: str
    type: NotebookType = NotebookType.BUNDLED
    read_only: bool = False
    trusted: bool = False

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "NotebookRecord":
        try:
            kind = NotebookType(str(payload.get("type", "bundled")))
        except ValueError:
            kind = NotebookType.BUNDLED
        return cls(
            id=str(payload.get("id", "")),
            root_folder=str(payload.get("rootFolder", "")),
            type=kind,
            read_only=bool(payload.get("readOnly", False)),
            trusted=bool(payload.get("trusted", False)),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rootFolder": self.root_folder,
            "type": str(self.type),
            "readOnly": self.read_only,
            "trusted": self.trusted,
        }


@dataclass(slots=True)
class BufferRecord:
    id: str
    notebook_id: str
    file_path: str
    file_id: str = ""

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "BufferRecord":
        return cls(
            id=str(payload.get("id", "")),
            notebook_id=str(payload.get("notebookId", "")),
            file_path=str(payload.get("filePath", "")),
            file_id=str(payload.get("fileId", "")),
        )

    def to_json(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "notebookId": self.notebook_id,
            "filePath": self.file_path,
        }
        if self.file_id:
            data["fileId"] = self.file_id
        return data


@dataclass(slots=True)
class WorkspaceConfig:
    id: str
    name: str = ""
    is_preview: bool = False
    buffer_ids: list[str] = field(default_factory=list)
    current_buffer_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    buffer_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "WorkspaceConfig":
        raw_meta = payload.get("bufferMetadata")
        buffer_metadata: dict[str, dict[str, Any]] = {}
        if isinstance(raw_meta, Mapping):
            buffer_metadata = {
                str(key): _as_dict(value) for key, value in raw_meta.items()
            }
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            is_preview=bool(payload.get("isPreview", False)),
            buffer_ids=[str(item) for item in _as_list(payload.get("bufferIds"))],
            current_buffer_id=str(payload.get("currentBufferId", "")),
            metadata=_as_dict(payload.get("metadata")),
            buffer_metadata=buffer_metadata,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "isPreview": self.is_preview,
            "bufferIds": list(self.buffer_ids),
            "currentBufferId": self.current_buffer_id,
            "metadata": dict(self.metadata),
            "bufferMetadata": {key: dict(value)
                               for key, value in self.buffer_metadata.items()},
        }


@dataclass(slots=True)
class SessionConfig:
    notebooks: list[NotebookRecord] = field(default_factory=list)
    workspaces: list[WorkspaceConfig] = field(default_factory=list)
    buffers: list[BufferRecord] = field(default_factory=list)
    current_workspace_id: str = ""

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "SessionConfig":
        return cls(
            notebooks=[NotebookRecord.from_json(item)
                       for item in _as_list(payload.get("notebooks"))
                       if isinstance(item, Mapping)],
            workspaces=[WorkspaceConfig.from_json(item)
                        for item in _as_list(payload.get("workspaces"))
                        if isinstance(item, Mapping)],
            buffers=[BufferRecord.from_json(item)
                     for item in _as_list(payload.get("buffers"))
                     if isinstance(item, Mapping)],
            current_workspace_id=str(payload.get("currentWorkspaceId", "")),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "notebooks": [item.to_json() for item in self.notebooks],
            "workspaces": [item.to_json() for item in self.workspaces],
            "buffers": [item.to_json() for item in self.buffers],
            "currentWorkspaceId": self.current_workspace_id,
        }


# -- read-only views --------------------------------------------------------

@dataclass(frozen=True, slots=True)
class NodeRef:
    """A child of a folder, as shown in the tree."""

    notebook_id: str
    path: str
    name: str
    id: str
    is_folder: bool
    exists: bool = True
    indexed: bool = True
    created_utc: int = 0
    modified_utc: int = 0
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FolderContents:
    folders: tuple[NodeRef, ...] = ()
    files: tuple[NodeRef, ...] = ()
    external_folders: tuple[NodeRef, ...] = ()
    external_files: tuple[NodeRef, ...] = ()

    @property
    def nodes(self) -> tuple[NodeRef, ...]:
        return self.folders + self.files


@dataclass(frozen=True, slots=True)
class NotebookInfo:
    id: str
    name: str
    root: str
    type: NotebookType
    read_only: bool = False
    trusted: bool = False


@dataclass(frozen=True, slots=True)
class BufferInfo:
    id: str
    notebook_id: str
    file_path: str
    display_name: str
    revision: int
    modified: bool
    state: BufferState
    content_loaded: bool
    can_save: bool
    can_reload: bool
    is_virtual: bool
    is_external: bool
    read_only: bool
    encoding: str = "utf-8"
    line_ending: LineEnding = LineEnding.UNSPECIFIED
    lossy: bool = False


# -- search -----------------------------------------------------------------

@dataclass(slots=True)
class SearchScope:
    folder_path: str = ""
    recursive: bool = True
    path_patterns: list[str] = field(default_factory=list)
    exclude_path_patterns: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    exclude_tags: list[str] = field(default_factory=list)
    tag_operator: str = "AND"
    date_filter_field: str = ""
    date_filter_from: int = 0
    date_filter_to: int = 0


@dataclass(slots=True)
class ContentQuery:
    pattern: str
    exclude_patterns: list[str] = field(default_factory=list)
    options: SearchOption = SearchOption.NONE
    scope: SearchScope = field(default_factory=SearchScope)
    max_results: int = 100


@dataclass(slots=True)
class FileQuery:
    pattern: str
    include_files: bool = True
    include_folders: bool = True
    match_target: str = "name_and_path"
    scope: SearchScope = field(default_factory=SearchScope)
    max_results: int = 100


@dataclass(slots=True)
class TagQuery:
    tags: list[str]
    tag_operator: str = "AND"
    scope: SearchScope = field(default_factory=SearchScope)
    max_results: int = 100


@dataclass(frozen=True, slots=True)
class SearchMatch:
    line_number: int
    column_start: int
    column_end: int
    line_text: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    notebook_id: str
    path: str
    file_id: str
    name: str
    matches: tuple[SearchMatch, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def snippet(self) -> str:
        if not self.matches:
            return ""
        text = self.matches[0].line_text.strip()
        return text if len(text) <= 160 else text[:157] + "…"


@dataclass(frozen=True, slots=True)
class SearchBatch:
    index: int
    total: int
    results: tuple[SearchResult, ...]


@dataclass(frozen=True, slots=True)
class SearchBatchResult:
    results: tuple[SearchResult, ...]
    truncated: bool = False
    cancelled: bool = False


# -- app configuration ------------------------------------------------------

@dataclass(slots=True)
class FileType:
    name: str
    suffixes: list[str] = field(default_factory=list)
    newable: bool = True
    searchable: bool = False
    display_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "FileType":
        known = {"name", "suffixes", "newable", "searchable", "displayName",
                 "metadata"}
        return cls(
            name=str(payload.get("name", "")),
            suffixes=[str(item).lower() for item in _as_list(payload.get("suffixes"))],
            newable=bool(payload.get("newable", True)),
            searchable=bool(payload.get("searchable", False)),
            display_name=str(payload.get("displayName", "")),
            metadata=_as_dict(payload.get("metadata")),
            extra=_take_extra(payload, known),
        )

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "suffixes": list(self.suffixes),
            "newable": self.newable,
            "searchable": self.searchable,
            "displayName": self.display_name,
            "metadata": dict(self.metadata),
        }
        data.update(self.extra)
        return data


@dataclass(slots=True)
class FileTypesConfig:
    types: list[FileType] = field(default_factory=list)
    schema_version: int = 1

    @classmethod
    def defaults(cls) -> "FileTypesConfig":
        return cls(types=[
            FileType("Markdown", ["md", "markdown"], True, True, "Markdown"),
            FileType("Text", ["txt"], True, True, "Text"),
            FileType("MindMap", ["mindmap"], True, True, "Mind Map"),
            FileType("Typst", ["typ"], True, True, "Typst"),
            FileType("PDF", ["pdf"], False, False, "PDF"),
            FileType("Others", [], False, False, "Others"),
        ])

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "FileTypesConfig":
        types = [FileType.from_json(item)
                 for item in _as_list(payload.get("types"))
                 if isinstance(item, Mapping)]
        if not types:
            return cls.defaults()
        return cls(types=types, schema_version=_as_int(payload.get("schemaVersion"), 1))

    def to_json(self) -> dict[str, Any]:
        return {
            "types": [item.to_json() for item in self.types],
            "schemaVersion": self.schema_version,
        }

    def by_suffix(self, suffix: str) -> FileType:
        key = suffix.lower().lstrip(".")
        for item in self.types:
            if key in item.suffixes:
                return item
        for item in self.types:
            if item.name == "Others":
                return item
        return FileType("Others", [], False, False, "Others")

    def by_name(self, name: str) -> FileType | None:
        for item in self.types:
            if item.name == name:
                return item
        return None

    def set_types(self, types: list[FileType]) -> None:
        seen: set[str] = set()
        for item in types:
            if not item.name:
                raise ValueError("file type name must not be empty")
            if item.name in seen:
                raise ValueError(f"duplicate file type: {item.name}")
            seen.add(item.name)
        self.types = list(types)


@dataclass(slots=True)
class SearchConfig:
    backends: list[str] = field(default_factory=lambda: ["simple", "rg"])
    encodings: list[str] = field(default_factory=lambda: ["UTF-8", "GB18030"])

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "SearchConfig":
        return cls(
            backends=[str(item) for item in _as_list(payload.get("backends"))]
            or ["simple", "rg"],
            encodings=[str(item) for item in _as_list(payload.get("encodings"))]
            or ["UTF-8", "GB18030"],
        )

    def to_json(self) -> dict[str, Any]:
        return {"backends": list(self.backends),
                "encodings": list(self.encodings)}


@dataclass(slots=True)
class CoreConfig:
    version: str = "0.1.0"
    search: SearchConfig = field(default_factory=SearchConfig)
    file_types: FileTypesConfig = field(default_factory=FileTypesConfig.defaults)
    recover_last_session: bool = True
    auto_sync_debounce_seconds: int = 120
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "CoreConfig":
        known = {"version", "search", "fileTypes", "recoverLastSession",
                 "autoSyncDebounceSeconds"}
        return cls(
            version=str(payload.get("version", "0.1.0")),
            search=SearchConfig.from_json(_as_dict(payload.get("search"))),
            file_types=FileTypesConfig.from_json(_as_dict(payload.get("fileTypes"))),
            recover_last_session=bool(payload.get("recoverLastSession", True)),
            auto_sync_debounce_seconds=_as_int(
                payload.get("autoSyncDebounceSeconds"), 120
            ),
            extra=_take_extra(payload, known),
        )

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": self.version,
            "search": self.search.to_json(),
            "fileTypes": self.file_types.to_json(),
            "recoverLastSession": self.recover_last_session,
            "autoSyncDebounceSeconds": self.auto_sync_debounce_seconds,
        }
        data.update(self.extra)
        return data


__all__ = [
    "BufferInfo",
    "BufferRecord",
    "BufferState",
    "ContentQuery",
    "CoreConfig",
    "FileQuery",
    "FileRecord",
    "FileType",
    "FileTypesConfig",
    "FolderConfig",
    "FolderContents",
    "LineEnding",
    "NodeRef",
    "NotebookConfig",
    "NotebookInfo",
    "NotebookRecord",
    "NotebookType",
    "SearchBatch",
    "SearchBatchResult",
    "SearchConfig",
    "SearchMatch",
    "SearchOption",
    "SearchResult",
    "SearchScope",
    "SessionConfig",
    "TagNode",
    "TagQuery",
    "WorkspaceConfig",
]
