"""Tag tree management, ported from vxcore's ``Notebook`` tag operations.

Tag *definitions* are part of the notebook configuration
(``vx_notebook/config.json`` on a bundled notebook, ``tags`` plus
``tagsModifiedUtc``).  They form a forest: every :class:`TagNode` has a ``name``
and an optional ``parent`` name.  A derived copy is mirrored into the SQLite
``tags`` table so file queries can be answered without scanning every
``vx.json``.

The mirror is refreshed by :meth:`TagManager.sync_to_store`, driven by the
``tagsModifiedUtc > tagsSyncedUtc`` stamp comparison (exactly like vxcore's
``SyncTagsToMetadataStore``).  vxcore syncs on open, after tag edits and after a
cache rebuild; this module offers the same entry points and the notebook calls
it at those moments.

Raw notebooks have no persisted configuration for tags, so all mutations raise
:class:`UnsupportedError` (matching vxcore).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import fsutil, paths
from .errors import (
    InvalidNameError,
    InvalidQueryError,
    ReadOnlyError,
    TagExistsError,
    TagNotFoundError,
    UnsupportedError,
)
from .metadata import StoreTag, TagQueryResult
from .models import NotebookType, TagNode

if TYPE_CHECKING:  # pragma: no cover
    from .notebook import Notebook


class TagManager:
    """Manage a notebook's tag forest and its derived SQLite mirror."""

    def __init__(self, notebook: "Notebook", store: Any, events: Any) -> None:
        self._notebook = notebook
        self._store = store
        self._events = events

    # -- properties ---------------------------------------------------------

    @property
    def notebook(self) -> "Notebook":
        return self._notebook

    @property
    def events(self) -> Any:
        return self._events

    # -- queries ------------------------------------------------------------

    def list(self) -> list[TagNode]:
        """Return a copy of the tag forest as a flat list of nodes."""
        return [tag.clone() for tag in self._config().tags]

    def has(self, name: str) -> bool:
        return self._find(name) is not None

    def get(self, name: str) -> TagNode:
        tag = self._find(name)
        if tag is None:
            raise TagNotFoundError("tag does not exist", path=name)
        return tag.clone()

    def children(self, parent: str = "") -> list[TagNode]:
        """Direct children of ``parent`` (root tags when parent is empty)."""
        return [node for node in self.list() if node.parent == parent]

    def find_files(self, tags: list[str], *, match_all: bool = True) -> list[TagQueryResult]:
        """Return files carrying all (AND) or any (OR) of ``tags``."""
        if not tags:
            return []
        return self._store.find_files_by_tags(tags, match_all=match_all)

    # vxcore names these AND/OR; keep an operator-flavoured alias for callers.
    def files(self, tags: list[str], operator: str = "AND") -> list[TagQueryResult]:
        op = operator.upper()
        if op not in ("AND", "OR"):
            raise InvalidQueryError("tag operator must be AND or OR")
        return self.find_files(tags, match_all=(op == "AND"))

    def count(self) -> dict[str, int]:
        """Map each tag name to the number of files carrying it."""
        return self._store.count_files_by_tag()

    # -- mutations ----------------------------------------------------------

    def create(self, name: str, parent: str = "") -> TagNode:
        """Create a tag; ``parent`` (if given) must already exist."""
        self._require_writable()
        name = paths.validate_name(name)
        if not name:
            raise InvalidNameError("tag name must not be empty")
        if self._find(name) is not None:
            raise TagExistsError("tag already exists", path=name)
        if parent and self._find(parent) is None:
            raise TagNotFoundError("parent tag does not exist", path=parent)

        config = self._config()
        node = TagNode(name=name, parent=parent)
        config.tags.append(node)
        self._commit(config, "created", name)
        return node.clone()

    def create_path(self, path: str, *, separator: str = "/") -> list[TagNode]:
        """Create every missing tag along ``a/b/c``; existing nodes are kept."""
        self._require_writable()
        path = (path or "").strip(separator)
        if not path:
            raise InvalidNameError("tag path must not be empty")
        components = [part for part in path.split(separator) if part]
        created: list[TagNode] = []
        for index, name in enumerate(components):
            name = paths.validate_name(name)
            parent = components[index - 1] if index > 0 else ""
            if self._find(name) is None:
                created.append(self.create(name, parent))
        return created

    def delete(self, name: str) -> None:
        """Delete a tag and all its descendants, detaching them from files."""
        self._require_writable()
        root = self._find(name)
        if root is None:
            raise TagNotFoundError("tag does not exist", path=name)

        # Collect the whole subtree (name + all descendants).
        doomed: list[str] = [name]
        frontier = [name]
        while frontier:
            current = frontier.pop()
            for node in self._config().tags:
                if node.parent == current:
                    doomed.append(node.name)
                    frontier.append(node.name)
        doomed_set = set(doomed)

        # vxcore detaches the deleted tags from every file before removing the
        # definitions. update_file_tags validates names against the config, so
        # this must happen while the definitions still exist.
        folders = self._notebook.folders
        try:
            for file_path, record in list(folders.iterate_all_files()):
                kept = [tag for tag in record.tags if tag not in doomed_set]
                if len(kept) != len(record.tags):
                    folders.update_file_tags(file_path, kept)
        except UnsupportedError:
            pass  # raw notebooks have no file-level tag assignments

        config = self._config()
        config.tags = [node for node in config.tags if node.name not in doomed_set]
        self._commit(config, "deleted", name)

    def move(self, name: str, parent: str = "") -> None:
        """Reparent a tag; rejects self/cyclic moves and missing parents."""
        self._require_writable()
        if name == parent:
            raise InvalidNameError("a tag cannot be its own parent", path=name)
        node = self._find(name)
        if node is None:
            raise TagNotFoundError("tag does not exist", path=name)
        if parent and self._find(parent) is None:
            raise TagNotFoundError("parent tag does not exist", path=parent)

        # Walk the would-be parent chain; a cycle means name is an ancestor.
        seen: set[str] = set()
        cursor = parent
        while cursor:
            if cursor == name:
                raise InvalidNameError(
                    "cannot move a tag under its own descendant", path=name
                )
            if cursor in seen:
                break  # pre-existing cycle elsewhere; stop walking
            seen.add(cursor)
            ancestor = self._find(cursor)
            cursor = ancestor.parent if ancestor is not None else ""

        config = self._config()
        node.parent = parent
        self._commit(config, "moved", name)

    # -- file-tag convenience delegates ------------------------------------

    def tag_file(self, path: str, tag: str) -> None:
        self._notebook.folders.tag_file(path, tag)

    def untag_file(self, path: str, tag: str) -> None:
        self._notebook.folders.untag_file(path, tag)

    def update_file_tags(self, path: str, tags: list[str]) -> list[str]:
        return self._notebook.folders.update_file_tags(path, tags)

    # -- synchronisation ----------------------------------------------------

    def sync_to_store(self) -> bool:
        """Refresh the SQLite tag mirror when the config stamp is newer.

        Returns ``True`` when a sync was performed, ``False`` when the mirror
        was already up to date.  Tags are inserted root-first so parent rows
        exist before their children; orphans (in the DB, not in the config) are
        removed.  All writes share one transaction.
        """
        modified = self._config().tags_modified_utc
        synced = _to_int(self._store.get_meta("tags_synced_utc"))
        if modified > 0 and modified <= synced:
            return False

        config = self._config()
        depths = _tag_depths(config.tags)
        ordered = sorted(config.tags, key=lambda node: depths.get(node.name, 10**6))

        with self._store.transaction():
            for node in ordered:
                self._store.upsert_tag(StoreTag(
                    name=node.name, parent_name=node.parent,
                    metadata=dict(node.metadata),
                ))
            db_names = {tag.name for tag in self._store.list_tags()}
            for orphan in db_names - set(depths):
                self._store.delete_tag(orphan)

        stamp = modified if modified > 0 else fsutil.now_ms()
        self._store.set_meta("tags_synced_utc", str(stamp))
        if modified == 0 and config.tags:
            config.tags_modified_utc = stamp
            # Persist the stamp without bumping it again or emitting an event.
            self._notebook.update_config(config)
        return True

    # -- internal -----------------------------------------------------------

    def _config(self):
        return self._notebook.config

    def _find(self, name: str):
        for node in self._config().tags:
            if node.name == name:
                return node
        return None

    def _require_writable(self) -> None:
        if self._notebook.type is not NotebookType.BUNDLED:
            raise UnsupportedError(
                "tags are not supported for raw notebooks",
                path=self._notebook.root,
            )
        if self._notebook.read_only:
            raise ReadOnlyError("notebook is read-only", path=self._notebook.root)

    def _commit(self, config, action: str, name: str) -> None:
        # The stamp drives sync via ``modified > synced``; keep it strictly
        # increasing so two edits in the same millisecond still trigger a sync.
        stamp = fsutil.now_ms()
        if stamp <= config.tags_modified_utc:
            stamp = config.tags_modified_utc + 1
        config.tags_modified_utc = stamp
        self._notebook.update_config(config)
        if self._events is not None:
            self._events.emit("tags.changed", {
                "notebookId": self._notebook.id, "action": action,
                "name": name, "tagsModifiedUtc": config.tags_modified_utc,
            })
        # Keep the derived mirror current after every definition change.
        self.sync_to_store()


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _tag_depths(tags: list[TagNode]) -> dict[str, int]:
    """Compute nesting depth for each tag (roots = 0, orphans = large)."""
    depths: dict[str, int] = {}
    for node in tags:
        if not node.parent:
            depths[node.name] = 0
    changed = True
    while changed:
        changed = False
        for node in tags:
            if node.name in depths:
                continue
            if node.parent and node.parent in depths:
                depths[node.name] = depths[node.parent] + 1
                changed = True
    return depths


__all__ = ["TagManager"]
