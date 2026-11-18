"""marktux core — a Python port of VNote's ``vxcore`` backend.

The package is deliberately Qt-free: it imports nothing from PyQt6 and can be
used (and tested) headless. ``ui.core_bridge`` adapts it to Qt signals.

Layout:

``context``    CoreContext facade and lifecycle
``notebook``   bundled/raw notebooks and the registry
``folder``     folder structure (``vx.json`` for bundled, disk for raw)
``buffer``     lazy buffers, save/dirty/external-change, backups
``metadata``   per-notebook SQLite cache (vxcore schema v4)
``tags``       tag tree and file-tag queries
``search``     simple and ripgrep content search
``workspace``  workspaces and ``vxsession.json``
``config``     ``vxcore.json`` / ``vxsession.json``
``paths``      relative-path algebra and containment
``fsutil``     atomic writes, codecs, ids, clocks
``models``     dataclasses, enums and JSON codecs
``events``     event bus
``concurrency`` tasks, cancellation, external-change watchdog
"""

from __future__ import annotations

from .assets import AssetManager
from .buffer import Buffer, BufferManager, BufferSource, FileSource, MemorySource
from .concurrency import CancellationToken
from .config import ConfigManager
from .context import CoreContext
from .errors import (
    BufferNotFoundError,
    CancelledError,
    ConfigError,
    CoreError,
    DatabaseError,
    InvalidNameError,
    InvalidQueryError,
    InvalidStateError,
    JsonError,
    NodeExistsError,
    NodeNotFoundError,
    NotebookError,
    NotebookExistsError,
    NotebookNotFoundError,
    PathError,
    PathEscapeError,
    ReadOnlyError,
    SaveError,
    SearchError,
    TagError,
    TagExistsError,
    TagNotFoundError,
    UnsupportedError,
    WorkspaceError,
    WorkspaceNotFoundError,
)
from .events import EventBus
from .metadata import SCHEMA_VERSION, SqliteMetadataStore
from .models import (
    BufferInfo,
    BufferRecord,
    BufferState,
    ContentQuery,
    CoreConfig,
    FileQuery,
    FileRecord,
    FileType,
    FileTypesConfig,
    FolderConfig,
    FolderContents,
    LineEnding,
    NodeRef,
    NotebookConfig,
    NotebookInfo,
    NotebookRecord,
    NotebookType,
    SearchBatch,
    SearchMatch,
    SearchOption,
    SearchResult,
    SearchScope,
    SessionConfig,
    TagNode,
    TagQuery,
    WorkspaceConfig,
)
from .notebook import Notebook, NotebookManager, RebuildStats
from .search import SearchManager
from .tags import TagManager
from .workspace import WorkspaceManager, filter_session

__version__ = "0.1.0"

__all__ = [
    "AssetManager",
    "Buffer",
    "BufferInfo",
    "BufferNotFoundError",
    "BufferManager",
    "BufferRecord",
    "BufferSource",
    "BufferState",
    "CancellationToken",
    "FileSource",
    "MemorySource",
    "CancelledError",
    "ConfigError",
    "ConfigManager",
    "ContentQuery",
    "CoreConfig",
    "CoreContext",
    "CoreError",
    "DatabaseError",
    "EventBus",
    "FileQuery",
    "FileRecord",
    "FileType",
    "FileTypesConfig",
    "FolderConfig",
    "FolderContents",
    "InvalidNameError",
    "InvalidQueryError",
    "InvalidStateError",
    "JsonError",
    "LineEnding",
    "NodeExistsError",
    "NodeNotFoundError",
    "NodeRef",
    "Notebook",
    "NotebookConfig",
    "NotebookError",
    "NotebookExistsError",
    "NotebookInfo",
    "NotebookManager",
    "NotebookNotFoundError",
    "NotebookRecord",
    "NotebookType",
    "PathError",
    "PathEscapeError",
    "ReadOnlyError",
    "RebuildStats",
    "SCHEMA_VERSION",
    "SaveError",
    "SearchBatch",
    "SearchError",
    "SearchManager",
    "SearchMatch",
    "SearchOption",
    "SearchResult",
    "SearchScope",
    "SessionConfig",
    "SqliteMetadataStore",
    "TagError",
    "TagExistsError",
    "TagManager",
    "TagNode",
    "TagNotFoundError",
    "TagQuery",
    "UnsupportedError",
    "WorkspaceConfig",
    "WorkspaceError",
    "WorkspaceManager",
    "WorkspaceNotFoundError",
    "__version__",
    "filter_session",
]
