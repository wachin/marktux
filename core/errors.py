"""Exception hierarchy for the marktux core.

The C++ vxcore returns numeric ``VxCoreError`` codes and keeps a thread-local
"last error" string. In Python that is a poor fit: failures are exceptional and
should carry their own context. Every expected failure here raises a
``CoreError`` subclass with a stable ``code`` string, an optional ``path`` and a
``details`` mapping. No numeric codes, no global error state.

Unexpected exceptions (bugs) are deliberately *not* wrapped; callers should let
them propagate so they are visible with a traceback.
"""

from __future__ import annotations

from typing import Any, Mapping


class CoreError(Exception):
    """Base class for all expected core failures."""

    code = "core_error"

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.path = path
        self.details: dict[str, Any] = dict(details or {})

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.path:
            return f"{self.message} ({self.path})"
        return self.message


# -- configuration ----------------------------------------------------------

class ConfigError(CoreError):
    code = "config_error"


class JsonError(ConfigError):
    code = "json_error"


# -- paths and names --------------------------------------------------------

class PathError(CoreError):
    code = "path_error"


class PathEscapeError(PathError):
    code = "path_escape"


class InvalidNameError(PathError):
    code = "invalid_name"


# -- notebook nodes ---------------------------------------------------------

class NodeNotFoundError(CoreError):
    code = "node_not_found"


class NodeExistsError(CoreError):
    code = "node_exists"


class UnsupportedError(CoreError):
    code = "unsupported"


class ReadOnlyError(CoreError):
    code = "read_only"


# -- notebooks --------------------------------------------------------------

class NotebookError(CoreError):
    code = "notebook_error"


class NotebookNotFoundError(NotebookError):
    code = "notebook_not_found"


class NotebookExistsError(NotebookError):
    code = "notebook_exists"


# -- buffers ----------------------------------------------------------------

class BufferError(CoreError):
    code = "buffer_error"


class BufferNotFoundError(BufferError):
    code = "buffer_not_found"


class SaveError(BufferError):
    code = "save_error"


class ExternalChangeError(BufferError):
    code = "external_change"


# -- tags -------------------------------------------------------------------

class TagError(CoreError):
    code = "tag_error"


class TagNotFoundError(TagError):
    code = "tag_not_found"


class TagExistsError(TagError):
    code = "tag_exists"


# -- workspaces -------------------------------------------------------------

class WorkspaceError(CoreError):
    code = "workspace_error"


class WorkspaceNotFoundError(WorkspaceError):
    code = "workspace_not_found"


# -- storage ----------------------------------------------------------------

class DatabaseError(CoreError):
    code = "database_error"


class SchemaError(DatabaseError):
    code = "schema_error"


# -- search -----------------------------------------------------------------

class SearchError(CoreError):
    code = "search_error"


class InvalidQueryError(SearchError):
    code = "invalid_query"


# -- control flow -----------------------------------------------------------

class CancelledError(CoreError):
    code = "cancelled"


class InvalidStateError(CoreError):
    code = "invalid_state"


__all__ = [
    "BufferError",
    "BufferNotFoundError",
    "CancelledError",
    "ConfigError",
    "CoreError",
    "DatabaseError",
    "ExternalChangeError",
    "InvalidNameError",
    "InvalidQueryError",
    "InvalidStateError",
    "JsonError",
    "NodeExistsError",
    "NodeNotFoundError",
    "NotebookError",
    "NotebookExistsError",
    "NotebookNotFoundError",
    "PathError",
    "PathEscapeError",
    "ReadOnlyError",
    "SaveError",
    "SchemaError",
    "SearchError",
    "TagError",
    "TagExistsError",
    "TagNotFoundError",
    "UnsupportedError",
    "WorkspaceError",
    "WorkspaceNotFoundError",
]
