"""Notebooks and the notebook registry.

A notebook is a directory. Two kinds exist, exactly as in vxcore:

* **bundled** — has ``vx_notebook/config.json``; structure lives in
  ``vx_notebook/contents/**/vx.json``; a per-notebook SQLite cache accelerates
  queries.
* **raw** — a plain directory with no metadata files at all. Structure is read
  from disk and ids are derived with ``uuid5`` so they are stable across runs.

``NotebookManager`` owns the open notebooks, deduplicates by canonical root and
resolves absolute paths back to ``(notebook, relative path)``. Open notebooks
are recorded in ``vxsession.json`` so the next start can restore them.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from . import fsutil, paths
from .assets import AssetManager
from .concurrency import CancellationToken
from .errors import (
    ConfigError,
    NotebookExistsError,
    NotebookNotFoundError,
    NodeNotFoundError,
    UnsupportedError,
)
from .folder import FolderManager, create_folder_manager
from .metadata import SqliteMetadataStore, StoreFile, StoreFolder
from .models import (
    FolderConfig,
    NotebookConfig,
    NotebookInfo,
    NotebookRecord,
    NotebookType,
    NodeRef,
)
from .search import SearchManager
from .tags import TagManager

log = logging.getLogger(__name__)

_METADATA_DIR = "vx_notebook"
_CONTENTS_DIR = "contents"
_CONFIG_NAME = "config.json"
_ROOT_FOLDER_CONFIG = "vx.json"


@dataclass(frozen=True, slots=True)
class RebuildStats:
    folders: int
    files: int
    duration_ms: int
    errors: tuple[str, ...] = ()


class Notebook:
    def __init__(
        self,
        manager: "NotebookManager",
        *,
        root: str,
        kind: NotebookType,
        config: NotebookConfig,
        read_only: bool = False,
        trusted: bool = False,
    ) -> None:
        self._manager = manager
        self._root = root
        self._type = kind
        self._config = config
        self._read_only = bool(read_only)
        self._trusted = bool(trusted)
        self._store = SqliteMetadataStore(manager.config.notebook_db_path(config.id))
        self._store.open()
        self._folders: FolderManager = create_folder_manager(self)
        self._tags = TagManager(self, self._store, getattr(manager, "_events", None))
        self._assets = AssetManager(self)
        search_cfg = manager.config.core.search
        self._search = SearchManager(
            self, search_cfg.backends, encodings=search_cfg.encodings,
            file_types=manager.config.core.file_types,
        )
        # A crashed atomic write leaves ``.<name>.tmp-*`` next to its target.
        # Clear stale ones in the two places open always touches.
        fsutil.sweep_temp_files(self._root)
        fsutil.sweep_temp_files(os.path.join(self._root, _METADATA_DIR))
        if kind is NotebookType.RAW:
            self._seed_raw_config()
        # SQLite is a *derived* cache. vxcore detects a cold cache via a
        # version stamp and rebuilds on open; here folder config is the source
        # of truth, so populate the structure whenever it is still empty. That
        # makes path lookups and CRUD correct before the user has expanded any
        # folder (list_children otherwise only syncs one level lazily).
        if self._store.counts()["folders"] == 0:
            self.rebuild_cache()
        # vxcore syncs tag definitions into the mirror on open. Do the same so
        # tag queries are correct even before the user touches the tag dock.
        if kind is NotebookType.BUNDLED:
            self._tags.sync_to_store()

    # -- identity -----------------------------------------------------------

    @property
    def manager(self) -> "NotebookManager":
        return self._manager

    @property
    def id(self) -> str:
        return self._config.id

    @property
    def name(self) -> str:
        return self._config.name or os.path.basename(self._root)

    @property
    def root(self) -> str:
        return self._root

    @property
    def type(self) -> NotebookType:
        return self._type

    @property
    def config(self) -> NotebookConfig:
        """Live configuration object; treat as read-only, use update_config."""
        return self._config

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def trusted(self) -> bool:
        return self._trusted

    @property
    def store(self) -> SqliteMetadataStore:
        return self._store

    @property
    def folders(self) -> FolderManager:
        return self._folders

    @property
    def tags(self) -> TagManager:
        return self._tags

    @property
    def search(self) -> SearchManager:
        return self._search

    @property
    def assets(self) -> AssetManager:
        return self._assets

    @property
    def db_path(self) -> str:
        return self._store.path

    @property
    def metadata_root(self) -> str | None:
        if self._type is not NotebookType.BUNDLED:
            return None
        return os.path.join(self._root, _METADATA_DIR)

    def info(self) -> NotebookInfo:
        return NotebookInfo(
            id=self.id, name=self.name, root=self._root, type=self._type,
            read_only=self._read_only, trusted=self._trusted,
        )

    # -- paths --------------------------------------------------------------

    def clean_relative_path(self, path: str) -> str:
        return paths.normalize_rel(path)

    def absolute_path(self, rel: str) -> str:
        return paths.resolve_inside(self._root, rel)

    def contains(self, abs_path: str) -> bool:
        return paths.is_inside(self._root, abs_path)

    # -- configuration ------------------------------------------------------

    def _config_path(self) -> str:
        return os.path.join(self._root, _METADATA_DIR, _CONFIG_NAME)

    def update_config(self, config: NotebookConfig) -> None:
        if self._read_only:
            from .errors import ReadOnlyError

            raise ReadOnlyError("notebook is read-only", path=self._root)
        if self._type is NotebookType.BUNDLED:
            fsutil.write_json(self._config_path(), config.to_json())
        self._config = config

    def _seed_raw_config(self) -> None:
        """Persist the raw notebook's name/description on first open."""
        if self._store.get_meta("config.name") is not None:
            return
        self._store.set_meta("config.name", self._config.name)
        self._store.set_meta("config.description", self._config.description)
        self._store.set_meta("config.assetsFolder", self._config.assets_folder)
        self._store.set_meta("config.ignored", "\n".join(self._config.ignored))

    # -- maintenance --------------------------------------------------------

    def rebuild_cache(
        self,
        *,
        progress: Callable[[int, int], None] | None = None,
        cancel: CancellationToken | None = None,
    ) -> RebuildStats:
        started = fsutil.now_ms()
        errors: list[str] = []
        folder_count = 0
        file_count = 0

        self._store.reset_structure()
        if self._type is NotebookType.BUNDLED:
            configs: list[tuple[str, FolderConfig]] = []
            base = os.path.join(self._root, _METADATA_DIR, _CONTENTS_DIR)
            for dirpath, _dirnames, filenames in os.walk(base):
                if _ROOT_FOLDER_CONFIG not in filenames:
                    continue
                rel_dir = os.path.relpath(dirpath, base)
                rel = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
                try:
                    configs.append((rel, self._folders.get_config(rel)))
                except ConfigError as error:
                    errors.append(f"{rel or '/'}: {error}")

            total = len(configs)
            for index, (rel, config) in enumerate(configs):
                if cancel is not None:
                    cancel.raise_if_cancelled()
                if progress is not None:
                    progress(index, total)
                try:
                    self._sync_config(rel, config)
                    folder_count += 1
                    file_count += len(config.files)
                except Exception as error:  # noqa: BLE001 - collect, keep going
                    errors.append(f"{rel or '/'}: {error}")
        else:
            for rel, config in self._walk_raw():
                if cancel is not None:
                    cancel.raise_if_cancelled()
                self._sync_config(rel, config)
                folder_count += 1
                file_count += len(config.files)

        return RebuildStats(
            folders=folder_count,
            files=file_count,
            duration_ms=fsutil.now_ms() - started,
            errors=tuple(errors),
        )

    def _sync_config(self, rel: str, config: FolderConfig) -> None:
        parent_rel, _ = paths.split_rel(rel)
        parent = self._store.get_folder_by_path(parent_rel) if rel else None
        self._store.create_folder(StoreFolder(
            id=config.id,
            parent_id=parent.id if parent else "",
            name="" if not rel else paths.split_rel(rel)[1],
            created_utc=config.created_utc,
            modified_utc=config.modified_utc,
            metadata=config.metadata,
        ))
        for record in config.files:
            self._store.create_file(StoreFile(
                id=record.id, folder_id=config.id, name=record.name,
                created_utc=record.created_utc, modified_utc=record.modified_utc,
                metadata=record.metadata,
                attachments=tuple(record.attachments),
            ))
            if record.tags:
                self._store.set_file_tags(record.id, record.tags)

    def _walk_raw(self) -> Iterable[tuple[str, FolderConfig]]:
        stack = [""]
        while stack:
            rel = stack.pop()
            try:
                config = self._folders.get_config(rel)
            except NodeNotFoundError:
                continue
            yield rel, config
            for name in config.folders:
                stack.append(paths.join_rel(rel, name))

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._store.close()


class NotebookManager:
    def __init__(self, config: Any, events: Any) -> None:
        self._config = config
        self._events = events
        self._notebooks: dict[str, Notebook] = {}
        self._by_root: dict[str, str] = {}

    @property
    def config(self) -> Any:
        return self._config

    # -- open / create ------------------------------------------------------

    @staticmethod
    def _canonical(root: str) -> str:
        return os.path.realpath(os.path.abspath(os.path.expanduser(root)))

    @staticmethod
    def _detect_type(root: str) -> NotebookType:
        if os.path.isfile(os.path.join(root, _METADATA_DIR, _CONFIG_NAME)):
            return NotebookType.BUNDLED
        return NotebookType.RAW

    def open(
        self,
        root: str,
        *,
        type: NotebookType | None = None,
        read_only: bool = False,
        trusted: bool = False,
    ) -> Notebook:
        canonical = self._canonical(root)
        if not os.path.isdir(canonical):
            raise NotebookNotFoundError("notebook directory does not exist",
                                        path=root)
        existing = self._by_root.get(os.path.normcase(canonical))
        if existing is not None:
            return self._notebooks[existing]

        kind = type or self._detect_type(canonical)
        if kind is NotebookType.BUNDLED:
            config = self._load_bundled_config(canonical, read_only=read_only)
        else:
            config = self._raw_config(canonical)

        notebook = Notebook(
            self, root=canonical, kind=kind, config=config,
            read_only=read_only, trusted=trusted,
        )
        self._notebooks[config.id] = notebook
        self._by_root[os.path.normcase(canonical)] = config.id
        self._record_session(notebook)
        self._events.emit("notebook.opened", {
            "notebookId": notebook.id, "root": canonical,
            "type": str(kind),
        })
        return notebook

    def _load_bundled_config(self, root: str, *,
                             read_only: bool) -> NotebookConfig:
        config_path = os.path.join(root, _METADATA_DIR, _CONFIG_NAME)
        payload = fsutil.read_json(config_path, required=True)
        if not isinstance(payload, dict):
            raise ConfigError("config.json must contain an object", path=config_path)
        config = NotebookConfig.from_json(payload)
        if not config.name:
            config.name = os.path.basename(root)
        if not config.id:
            config.id = fsutil.new_uuid()
            if not read_only:
                fsutil.write_json(config_path, config.to_json())

        root_config = os.path.join(root, _METADATA_DIR, _CONTENTS_DIR,
                                   _ROOT_FOLDER_CONFIG)
        if not os.path.isfile(root_config):
            raise ConfigError("notebook is missing its root folder config",
                              path=root_config)
        return config

    def _raw_config(self, root: str) -> NotebookConfig:
        notebook_id = fsutil.stable_uuid(f"file://{root}")
        return NotebookConfig(id=notebook_id, name=os.path.basename(root))

    def create(
        self,
        root: str,
        name: str,
        *,
        type: NotebookType = NotebookType.BUNDLED,
        description: str = "",
    ) -> Notebook:
        canonical = self._canonical(root)
        config_path = os.path.join(canonical, _METADATA_DIR, _CONFIG_NAME)
        if os.path.exists(config_path):
            raise NotebookExistsError("a notebook already exists here",
                                      path=canonical)

        fsutil.ensure_dir(canonical)
        config = NotebookConfig(
            id=fsutil.new_uuid(), name=name or os.path.basename(canonical),
            description=description,
        )
        if type is NotebookType.BUNDLED:
            contents = os.path.join(canonical, _METADATA_DIR, _CONTENTS_DIR)
            fsutil.ensure_dir(contents)
            fsutil.write_json(config_path, config.to_json())
            root_folder = FolderConfig.new(".")
            fsutil.write_json(os.path.join(contents, _ROOT_FOLDER_CONFIG),
                              root_folder.to_json())
        return self.open(canonical, type=type)

    # -- lookup -------------------------------------------------------------

    def get(self, notebook_id: str) -> Notebook:
        notebook = self._notebooks.get(notebook_id)
        if notebook is None:
            raise NotebookNotFoundError("notebook is not open",
                                        path=notebook_id)
        return notebook

    def find_by_root(self, root: str) -> Notebook | None:
        canonical = self._canonical(root)
        notebook_id = self._by_root.get(os.path.normcase(canonical))
        return self._notebooks.get(notebook_id) if notebook_id else None

    def list(self) -> list[NotebookInfo]:
        return [notebook.info() for notebook in
                sorted(self._notebooks.values(), key=lambda item: item.name.casefold())]

    def resolve_path(self, abs_path: str) -> tuple[Notebook, str] | None:
        target = os.path.realpath(os.path.abspath(abs_path))
        best: Notebook | None = None
        for notebook in self._notebooks.values():
            if paths.is_inside(notebook.root, target):
                if best is None or len(notebook.root) > len(best.root):
                    best = notebook
        if best is None:
            return None
        return best, paths.relative_to(best.root, target)

    def resolve_node(self, node_id: str) -> NodeRef | None:
        for notebook in self._notebooks.values():
            record = notebook.store.get_file(node_id)
            if record is not None:
                path = notebook.store.node_path(node_id)
                return NodeRef(
                    notebook_id=notebook.id, path=path,
                    name=record.name, id=record.id, is_folder=False,
                    exists=os.path.isfile(notebook.absolute_path(path)),
                    created_utc=record.created_utc,
                    modified_utc=record.modified_utc,
                    tags=record.tags, metadata=record.metadata,
                )
            folder = notebook.store.get_folder(node_id)
            if folder is not None:
                path = notebook.store.folder_path(node_id)
                return NodeRef(
                    notebook_id=notebook.id, path=path, name=folder.name,
                    id=folder.id, is_folder=True,
                    exists=os.path.isdir(notebook.absolute_path(path)),
                    created_utc=folder.created_utc,
                    modified_utc=folder.modified_utc,
                    metadata=folder.metadata,
                )
        return None

    # -- state --------------------------------------------------------------

    def set_read_only(self, notebook_id: str, read_only: bool) -> None:
        notebook = self.get(notebook_id)
        notebook._read_only = bool(read_only)  # noqa: SLF001 - same package
        self._record_session(notebook)

    def set_trusted(self, notebook_id: str, trusted: bool) -> None:
        notebook = self.get(notebook_id)
        notebook._trusted = bool(trusted)  # noqa: SLF001 - same package
        self._record_session(notebook)

    # -- session ------------------------------------------------------------

    def _record_session(self, notebook: Notebook) -> None:
        records = self._config.session.notebooks
        for record in records:
            if record.id == notebook.id:
                record.root_folder = notebook.root
                record.type = notebook.type
                record.read_only = notebook.read_only
                record.trusted = notebook.trusted
                return
        records.append(NotebookRecord(
            id=notebook.id, root_folder=notebook.root, type=notebook.type,
            read_only=notebook.read_only, trusted=notebook.trusted,
        ))

    def session_records(self) -> list[NotebookRecord]:
        return list(self._config.session.notebooks)

    def restore_from_session(self) -> list[Notebook]:
        opened: list[Notebook] = []
        for record in list(self._config.session.notebooks):
            if not record.root_folder or not os.path.isdir(record.root_folder):
                continue
            try:
                opened.append(self.open(
                    record.root_folder, type=record.type,
                    read_only=record.read_only, trusted=record.trusted,
                ))
            except Exception:  # noqa: BLE001 - one bad notebook must not block
                log.exception("cannot restore notebook %s", record.root_folder)
        return opened

    # -- close --------------------------------------------------------------

    def close(self, notebook_id: str, *, remove_from_session: bool = True) -> None:
        notebook = self._notebooks.pop(notebook_id, None)
        if notebook is None:
            return
        self._by_root.pop(os.path.normcase(notebook.root), None)
        notebook.close()
        if remove_from_session:
            self._config.session.notebooks = [
                record for record in self._config.session.notebooks
                if record.id != notebook_id
            ]
        self._events.emit("notebook.closed", {"notebookId": notebook_id})

    def close_all(self) -> None:
        # Shutdown closes the stores but must keep the session records, so the
        # notebooks are reopened on the next start.
        for notebook_id in list(self._notebooks):
            self.close(notebook_id, remove_from_session=False)


__all__ = ["Notebook", "NotebookManager", "RebuildStats"]
