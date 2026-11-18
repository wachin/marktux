"""Folder managers: the structural heart of a notebook.

Two strategies share one base class:

* :class:`BundledFolderManager` — a VNote bundled notebook. Folder structure
  lives in ``vx_notebook/contents/<rel>/vx.json``; the SQLite store is a
  write-through cache used for tag queries, path lookups and rebuilds.
* :class:`RawFolderManager` — a plain directory. Structure is whatever is on
  disk; ids are derived deterministically with ``uuid5`` so they survive a
  restart without a session.

vxcore duplicates ~8.7k lines between its two managers. Here the filesystem
work is written once and only the metadata hooks differ, which is why this
module is a fraction of the size.
"""

from __future__ import annotations

import logging
import os
import shutil
from abc import ABC, abstractmethod
from typing import Any, Callable, Iterable, Sequence

from . import fsutil, paths
from .errors import (
    ConfigError,
    InvalidNameError,
    NodeExistsError,
    NodeNotFoundError,
    PathEscapeError,
    ReadOnlyError,
    TagNotFoundError,
    UnsupportedError,
)
from .metadata import MetadataStore, StoreFile, StoreFolder
from .models import (
    FileRecord,
    FolderConfig,
    FolderContents,
    NodeRef,
    NotebookType,
)

log = logging.getLogger(__name__)

_METADATA_DIR = "vx_notebook"
_CONTENTS_DIR = "contents"
_CONFIG_NAME = "vx.json"


class FolderManager(ABC):
    def __init__(self, notebook: Any) -> None:
        self._notebook = notebook
        # Directories already checked for crashed-write temp files.
        self._swept_dirs: set[str] = set()

    # -- notebook shortcuts -------------------------------------------------

    @property
    def notebook(self) -> Any:
        return self._notebook

    @property
    def root(self) -> str:
        return self._notebook.root

    @property
    def notebook_id(self) -> str:
        return self._notebook.id

    @property
    def store(self) -> MetadataStore:
        return self._notebook.store

    @property
    def read_only(self) -> bool:
        return self._notebook.read_only

    # -- strategy hooks -----------------------------------------------------

    @abstractmethod
    def get_config(self, folder_path: str = "") -> FolderConfig:
        """Return a deep copy of the folder's configuration."""

    @abstractmethod
    def list_children(
        self,
        folder: str = "",
        *,
        include_folders: bool = True,
        include_external: bool = True,
    ) -> FolderContents:
        """List one level of children."""

    @abstractmethod
    def content_path(self, rel: str) -> str:
        """Absolute path of the folder's content directory."""

    def indexed_names(self, folder: str) -> set[str]:
        """Names known to the index for ``folder`` (used by available_name)."""
        cfg = self.get_config(folder)
        names = set(cfg.folders)
        names.update(record.name for record in cfg.files)
        return names

    def _sweep_once(self, directory: str) -> None:
        """Remove crash-leftover temp files the first time a dir is listed."""
        if directory in self._swept_dirs:
            return
        self._swept_dirs.add(directory)
        try:
            fsutil.sweep_temp_files(directory)
        except OSError:  # cleanup must never block browsing
            log.debug("cannot sweep %s", directory, exc_info=True)

    # -- shared operations --------------------------------------------------

    def absolute_path(self, rel: str) -> str:
        return paths.absolute(self.root, rel)

    def available_name(
        self,
        folder: str,
        desired: str,
        *,
        existing: Iterable[str] | None = None,
    ) -> str:
        return paths.available_name(
            self.content_path(folder), desired,
            existing=self.indexed_names(folder) if existing is None else existing,
        )

    def get_file_info(self, path: str) -> FileRecord:
        rel = paths.normalize_rel(path)
        parent, name = paths.split_rel(rel)
        cfg = self.get_config(parent)
        record = cfg.file(name)
        if record is None:
            raise NodeNotFoundError("file is not indexed", path=rel)
        return record.clone()

    # -- file tags ----------------------------------------------------------
    # Tag assignments live in each folder's ``vx.json`` (the source of truth)
    # and are mirrored into the SQLite cache for queries. Only bundled
    # notebooks have a ``vx.json``; raw notebooks reject these operations.

    def _require_taggable(self) -> None:
        if self.notebook.type is not NotebookType.BUNDLED:
            raise UnsupportedError(
                "tags are not supported for raw notebooks", path=self.root
            )

    def iterate_all_files(self) -> Iterable[tuple[str, FileRecord]]:
        """Yield ``(relative_path, record)`` for every indexed file."""
        self._require_taggable()
        stack: list[str] = [""]
        while stack:
            folder = stack.pop()
            try:
                cfg = self.get_config(folder)
            except (ConfigError, NodeNotFoundError):
                continue
            for name in cfg.folders:
                stack.append(paths.join_rel(folder, name))
            for record in cfg.files:
                yield paths.join_rel(folder, record.name), record.clone()

    def get_file_tags(self, path: str) -> list[str]:
        self._require_taggable()
        return list(self.get_file_info(path).tags)

    def update_file_tags(self, path: str, tags: Sequence[str]) -> list[str]:
        """Replace a file's tag assignments; return the stored list."""
        self._require_taggable()
        self._check_writable()
        rel = paths.normalize_rel(path)
        parent, name = paths.split_rel(rel)
        cfg = self.get_config(parent)
        record = cfg.file(name)
        if record is None:
            raise NodeNotFoundError("file is not indexed", path=rel)

        wanted = list(dict.fromkeys(tags))
        defined = {tag.name for tag in self.notebook.config.tags}
        # A new, undefined tag name is invalid; an existing orphan assignment
        # may be preserved (mirrors vxcore's UpdateFileTags validation).
        for tag in wanted:
            if tag not in defined and tag not in record.tags:
                raise TagNotFoundError("tag is not defined", path=tag)

        record.tags = wanted
        stamp = fsutil.now_ms()
        record.modified_utc = stamp
        cfg.modified_utc = stamp
        self._save_config(parent, cfg)
        self.store.set_file_tags(record.id, wanted)
        self._emit("file.tags_changed", {
            "notebookId": self.notebook_id, "path": rel, "tags": wanted,
        })
        return wanted

    def tag_file(self, path: str, tag: str) -> None:
        self._require_taggable()
        wanted = self.get_file_tags(path)
        if tag not in wanted:
            self.update_file_tags(path, wanted + [tag])

    def untag_file(self, path: str, tag: str) -> None:
        self._require_taggable()
        wanted = self.get_file_tags(path)
        if tag in wanted:
            self.update_file_tags(path, [name for name in wanted if name != tag])

    def _sync_folder(self, rel: str, cfg: FolderConfig) -> None:
        """Write the folder and its files into the cache (idempotent)."""
        parent_rel, _ = paths.split_rel(rel)
        parent = self.store.get_folder_by_path(parent_rel) if rel else None
        self.store.create_folder(StoreFolder(
            id=cfg.id,
            parent_id=parent.id if parent else "",
            name="" if not rel else paths.split_rel(rel)[1],
            created_utc=cfg.created_utc,
            modified_utc=cfg.modified_utc,
            metadata=cfg.metadata,
        ))
        for record in cfg.files:
            self.store.create_file(StoreFile(
                id=record.id,
                folder_id=cfg.id,
                name=record.name,
                created_utc=record.created_utc,
                modified_utc=record.modified_utc,
                metadata=record.metadata,
                attachments=tuple(record.attachments),
            ))
            if record.tags:
                self.store.set_file_tags(record.id, record.tags)

    def _file_ref(self, cfg: FolderConfig, folder: str, record: FileRecord,
                  content_dir: str) -> NodeRef:
        exists = os.path.isfile(os.path.join(content_dir, record.name))
        return NodeRef(
            notebook_id=self.notebook_id,
            path=paths.join_rel(folder, record.name),
            name=record.name,
            id=record.id,
            is_folder=False,
            exists=exists,
            indexed=True,
            created_utc=record.created_utc,
            modified_utc=record.modified_utc,
            tags=tuple(record.tags),
            metadata=dict(record.metadata),
        )

    # -- strategy hooks (overridden by bundled/raw) -------------------------

    @property
    def bundled(self) -> bool:
        return self.notebook.type is NotebookType.BUNDLED

    def _save_config(self, rel: str, cfg: FolderConfig) -> None:
        """Persist a folder's structure. No-op for raw (disk is the truth)."""
        raise NotImplementedError

    def _remove_config(self, rel: str) -> None:
        """Remove a folder's config file. No-op for raw."""
        raise NotImplementedError

    def _new_folder_config(self, name: str, rel: str) -> FolderConfig:
        """Fresh config for a newly created folder (id strategy differs)."""
        raise NotImplementedError

    def _new_file_record(self, name: str, rel: str) -> FileRecord:
        """Fresh record for a newly created file (id strategy differs)."""
        raise NotImplementedError

    # -- guards -------------------------------------------------------------

    def _check_writable(self) -> None:
        if self.read_only:
            raise ReadOnlyError("notebook is read-only", path=self.root)

    def _events(self) -> Any:
        return self.notebook.manager.config  # config holds no events; use manager

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        events = getattr(self.notebook.manager, "_events", None)
        if events is not None:
            events.emit(name, payload)

    def _assert_folder(self, rel: str) -> None:
        rel = paths.normalize_rel(rel)
        if not os.path.isdir(self.absolute_path(rel)):
            raise NodeNotFoundError("folder does not exist", path=rel)

    def _assert_file(self, rel: str) -> None:
        rel = paths.normalize_rel(rel)
        if not os.path.isfile(self.absolute_path(rel)):
            raise NodeNotFoundError("file does not exist", path=rel)

    def _sync_tree(self, *rels: str) -> None:
        """Write-through the SQLite cache for the affected folders."""
        for rel in rels:
            rel = paths.normalize_rel(rel)
            try:
                cfg = self.get_config(rel)
            except (ConfigError, NodeNotFoundError):
                # Folder was deleted; drop its row from the cache.
                row = self.store.get_folder_by_path(rel)
                if row is not None:
                    self.store.delete_folder(row.id)
                continue
            self._sync_folder(rel, cfg)

    def _rekey_buffers(self, notebook_id: str, old: str, new: str) -> None:
        manager = getattr(self.notebook.manager, "_buffers", None)
        if manager is not None and old != new:
            manager.update_paths(notebook_id, old, new)

    # -- create -------------------------------------------------------------

    def create_folder(self, parent: str, name: str) -> NodeRef:
        self._check_writable()
        name = paths.validate_name(name)
        parent = paths.normalize_rel(parent)
        self._assert_folder(parent)
        rel = paths.join_rel(parent, name)
        target = paths.resolve_inside(self.root, rel)
        # vxcore fails a Create operation when getAvailableName(name) != name:
        # an explicit create collision is an error, never a silent rename.
        if (os.path.lexists(target) or
                paths.casefold_key(name) in
                {paths.casefold_key(n) for n in self.indexed_names(parent)}):
            raise NodeExistsError("folder already exists", path=rel)

        parent_cfg = self.get_config(parent)
        os.makedirs(target, exist_ok=False)
        try:
            parent_cfg.folders.append(name)
            self._save_config(parent, parent_cfg)
            if self.bundled:
                self._save_config(rel, self._new_folder_config(name, rel))
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            if hasattr(self, "invalidate"):
                self.invalidate(parent)
            raise
        self._sync_tree(parent, rel)
        self._emit("folder.created", {"notebookId": self.notebook_id, "path": rel})
        return NodeRef(notebook_id=self.notebook_id, path=rel, name=name,
                       id=self.get_config(rel).id, is_folder=True)

    def create_file(self, parent: str, name: str,
                    content: str = "") -> NodeRef:
        self._check_writable()
        name = paths.validate_name(name)
        parent = paths.normalize_rel(parent)
        self._assert_folder(parent)
        rel = paths.join_rel(parent, name)
        target = paths.resolve_inside(self.root, rel)
        # See create_folder: an explicit create collision is an error.
        if (os.path.lexists(target) or
                paths.casefold_key(name) in
                {paths.casefold_key(n) for n in self.indexed_names(parent)}):
            raise NodeExistsError("file already exists", path=rel)

        parent_cfg = self.get_config(parent)
        record = self._new_file_record(name, rel)
        # Note creation does not go through a buffer: write the file, then the
        # body separately (vxcore convention).
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)
        try:
            parent_cfg.files.append(record)
            self._save_config(parent, parent_cfg)
        except BaseException:
            try:
                os.unlink(target)
            except OSError:
                pass
            raise
        self._sync_tree(parent)
        self._emit("file.created", {"notebookId": self.notebook_id, "path": rel})
        return self._file_ref(parent_cfg, parent, record,
                              self.content_path(parent))

    # -- rename / move ------------------------------------------------------

    def rename_file(self, rel: str, new_name: str) -> NodeRef:
        self._check_writable()
        new_name = paths.validate_name(new_name)
        parent, old_name = paths.split_rel(rel)
        self._assert_file(rel)
        new_name = self.available_name(parent, new_name,
                                       existing={n for n in
                                                 self.indexed_names(parent)
                                                 if n != old_name})
        new_rel = paths.join_rel(parent, new_name)
        if new_name == old_name:
            return self._node_ref(rel)  # no-op

        old_abs = paths.resolve_inside(self.root, rel, reject_symlinks=True)
        new_abs = paths.resolve_inside(self.root, new_rel)
        cfg = self.get_config(parent)
        record = cfg.file(old_name)
        if record is None:
            raise NodeNotFoundError("file is not indexed", path=rel)

        os.rename(old_abs, new_abs)
        try:
            record.name = new_name
            record.modified_utc = fsutil.now_ms()
            self._save_config(parent, cfg)
        except BaseException:
            os.rename(new_abs, old_abs)
            raise
        self._sync_tree(parent)
        self._rekey_buffers(self.notebook_id, rel, new_rel)
        self._emit("file.moved", {"notebookId": self.notebook_id,
                                  "from": rel, "to": new_rel})
        return self._node_ref(new_rel)

    def move_file(self, rel: str, dest_folder: str) -> NodeRef:
        self._check_writable()
        src_parent, name = paths.split_rel(rel)
        dest_folder = paths.normalize_rel(dest_folder)
        if src_parent == dest_folder:
            return self._node_ref(rel)
        self._assert_file(rel)
        self._assert_folder(dest_folder)
        new_name = self.available_name(dest_folder, name)
        new_rel = paths.join_rel(dest_folder, new_name)
        old_abs = paths.resolve_inside(self.root, rel, reject_symlinks=True)
        new_abs = paths.resolve_inside(self.root, new_rel)

        src_cfg = self.get_config(src_parent)
        dst_cfg = self.get_config(dest_folder)
        record = src_cfg.file(name)
        if record is None:
            raise NodeNotFoundError("file is not indexed", path=rel)

        os.rename(old_abs, new_abs)
        try:
            src_cfg.files.remove(record)
            record.name = new_name
            record.modified_utc = fsutil.now_ms()
            dst_cfg.files.append(record)
            self._save_config(src_parent, src_cfg)
            if dest_folder != src_parent:
                self._save_config(dest_folder, dst_cfg)
        except BaseException:
            os.rename(new_abs, old_abs)
            raise
        self._sync_tree(src_parent, dest_folder)
        self._rekey_buffers(self.notebook_id, rel, new_rel)
        self._emit("file.moved", {"notebookId": self.notebook_id,
                                  "from": rel, "to": new_rel})
        return self._node_ref(new_rel)

    def copy_file(self, rel: str, dest_folder: str,
                  new_name: str | None = None) -> NodeRef:
        self._check_writable()
        src_parent, name = paths.split_rel(rel)
        self._assert_file(rel)
        dest_folder = paths.normalize_rel(dest_folder)
        self._assert_folder(dest_folder)
        desired = new_name or name
        if new_name is None:
            desired = self.available_name(dest_folder, desired)
        else:
            desired = paths.validate_name(desired)
        new_rel = paths.join_rel(dest_folder, desired)
        if paths.casefold_key(new_rel) == paths.casefold_key(rel):
            desired = self.available_name(dest_folder, desired)
            new_rel = paths.join_rel(dest_folder, desired)

        src_abs = paths.resolve_inside(self.root, rel, reject_symlinks=True)
        new_abs = paths.resolve_inside(self.root, new_rel)
        dst_cfg = self.get_config(dest_folder)
        # A copy regenerates its UUID.
        record = self._new_file_record(desired, new_rel)

        shutil.copy2(src_abs, new_abs)
        try:
            if record.name in [f.name for f in dst_cfg.files]:
                dst_cfg.files = [f for f in dst_cfg.files if f.name != record.name]
            dst_cfg.files.append(record)
            self._save_config(dest_folder, dst_cfg)
        except BaseException:
            try:
                os.unlink(new_abs)
            except OSError:
                pass
            raise
        self._sync_tree(dest_folder)
        self._emit("file.created", {"notebookId": self.notebook_id,
                                    "path": new_rel})
        return self._node_ref(new_rel)

    # -- delete -------------------------------------------------------------

    def delete_file(self, rel: str) -> None:
        self._check_writable()
        rel = paths.normalize_rel(rel)
        parent, name = paths.split_rel(rel)
        self._assert_file(rel)
        cfg = self.get_config(parent)
        record = cfg.file(name)
        if record is None:
            raise NodeNotFoundError("file is not indexed", path=rel)

        abs_path = paths.resolve_inside(self.root, rel, reject_symlinks=True)
        # Rename-to-temp first: if config save fails we can restore by renaming
        # back, so a crash never leaves an indexed file missing on disk.
        temp = abs_path + ".marktux-delete"
        os.rename(abs_path, temp)
        try:
            cfg.files.remove(record)
            self._save_config(parent, cfg)
        except BaseException:
            os.rename(temp, abs_path)
            raise
        try:
            os.unlink(temp)
        except OSError:
            pass
        row = self.store.get_file_by_path(rel)
        if row is not None:
            self.store.delete_file(row.id)
        self._emit("file.deleted", {"notebookId": self.notebook_id, "path": rel})

    def delete_folder(self, rel: str) -> None:
        self._check_writable()
        rel = paths.normalize_rel(rel)
        if not rel:
            raise InvalidNameError("cannot delete the root folder", path=rel)
        parent, name = paths.split_rel(rel)
        self._assert_folder(rel)
        cfg = self.get_config(parent)
        if name not in cfg.folders:
            raise NodeNotFoundError("folder is not indexed", path=rel)

        abs_path = paths.resolve_inside(self.root, rel, reject_symlinks=True)
        temp = abs_path + ".marktux-delete"
        os.rename(abs_path, temp)
        try:
            cfg.folders.remove(name)
            self._save_config(parent, cfg)
            if self.bundled:
                self._remove_config(rel)
        except BaseException:
            os.rename(temp, abs_path)
            raise
        shutil.rmtree(temp, ignore_errors=True)
        row = self.store.get_folder_by_path(rel)
        if row is not None:
            self.store.delete_folder(row.id)
        self._emit("folder.deleted", {"notebookId": self.notebook_id, "path": rel})

    # -- ordering -----------------------------------------------------------

    def set_children_order(self, folder: str, names: Sequence[str]) -> None:
        """Reorder children of ``folder``; ``names`` must be an exact permutation.

        A no-op (same order) does not touch disk.
        """
        self._check_writable()
        folder = paths.normalize_rel(folder)
        cfg = self.get_config(folder)
        current = list(cfg.folders)  # folder order only; files stay indexed order
        if list(names) == current:
            return
        if sorted(paths.casefold_key(n) for n in names) != \
                sorted(paths.casefold_key(n) for n in current):
            raise InvalidNameError("names must be an exact permutation of children")
        order = {paths.casefold_key(n): i for i, n in enumerate(names)}
        cfg.folders = sorted(cfg.folders, key=lambda n: order[paths.casefold_key(n)])
        self._save_config(folder, cfg)
        self._emit("folder.children_reordered", {
            "notebookId": self.notebook_id, "path": folder,
        })

    # -- lookups ------------------------------------------------------------

    def _node_ref(self, rel: str) -> NodeRef:
        rel = paths.normalize_rel(rel)
        parent, name = paths.split_rel(rel)
        node_id = ""
        try:
            cfg = self.get_config(parent)
            record = cfg.file(name)
            if record is not None:
                node_id = record.id
            elif name in cfg.folders:
                node_id = self.get_config(rel).id
        except (ConfigError, NodeNotFoundError):
            pass
        if not node_id:
            row = self.store.get_file_by_path(rel) or self.store.get_folder_by_path(rel)
            node_id = row.id if row else fsutil.stable_uuid(f"file://{rel}")
        is_dir = os.path.isdir(self.absolute_path(rel))
        return NodeRef(notebook_id=self.notebook_id, path=rel, name=name,
                       id=node_id, is_folder=is_dir)

    # -- attachments --------------------------------------------------------
    # Attachment names live in each file's ``vx.json`` record (mirrored to the
    # cache). Raw notebooks have no ``vx.json``, so they reject attachments
    # while still allowing plain assets.

    def _require_attachments(self) -> None:
        if self.notebook.type is not NotebookType.BUNDLED:
            raise UnsupportedError(
                "attachments are not supported for raw notebooks", path=self.root
            )

    def get_attachments(self, path: str) -> list[str]:
        self._require_attachments()
        return list(self.get_file_info(path).attachments)

    def add_attachment(self, path: str, name: str) -> list[str]:
        """Record ``name`` as an attachment of ``path``; return the new list."""
        self._require_attachments()
        return self._update_attachments(path, add=name)

    def remove_attachment(self, path: str, name: str) -> list[str]:
        """Drop ``name`` from ``path``'s attachments; return the new list."""
        self._require_attachments()
        return self._update_attachments(path, remove=name)

    def _update_attachments(self, path: str, *, add: str | None = None,
                            remove: str | None = None) -> list[str]:
        self._check_writable()
        rel = paths.normalize_rel(path)
        parent, name = paths.split_rel(rel)
        cfg = self.get_config(parent)
        record = cfg.file(name)
        if record is None:
            raise NodeNotFoundError("file is not indexed", path=rel)

        wanted = list(dict.fromkeys(record.attachments))
        if add is not None and add not in wanted:
            wanted.append(add)
        if remove is not None:
            wanted = [item for item in wanted if item != remove]
        record.attachments = wanted
        stamp = fsutil.now_ms()
        record.modified_utc = stamp
        cfg.modified_utc = stamp
        self._save_config(parent, cfg)
        self.store.set_file_attachments(record.id, wanted)
        self._emit("file.attachments_changed", {
            "notebookId": self.notebook_id, "path": rel,
            "attachments": wanted,
        })
        return wanted


# ---------------------------------------------------------------------------
# Bundled notebooks
# ---------------------------------------------------------------------------

class BundledFolderManager(FolderManager):
    def __init__(self, notebook: Any) -> None:
        super().__init__(notebook)
        self._cache: dict[str, FolderConfig] = {}
        self._cache_mtime: dict[str, int] = {}

    # -- paths --------------------------------------------------------------

    def _config_path(self, rel: str) -> str:
        parts = [_METADATA_DIR, _CONTENTS_DIR]
        rel = paths.normalize_rel(rel)
        if rel:
            parts.extend(rel.split("/"))
        parts.append(_CONFIG_NAME)
        return os.path.join(self.root, *parts)

    def content_path(self, rel: str) -> str:
        return paths.absolute(self.root, rel)

    # -- configuration ------------------------------------------------------

    def get_config(self, folder_path: str = "") -> FolderConfig:
        return self._load_config(folder_path).clone()

    def _load_config(self, rel: str) -> FolderConfig:
        rel = paths.normalize_rel(rel)
        path = self._config_path(rel)
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError as error:
            raise ConfigError("folder configuration is missing", path=path) from error

        cached = self._cache.get(rel)
        if cached is not None and self._cache_mtime.get(rel) == stamp:
            return cached

        payload = fsutil.read_json(path, required=True)
        if not isinstance(payload, dict):
            raise ConfigError("vx.json must contain an object", path=path)
        config = FolderConfig.from_json(payload)
        if not config.id:
            config.id = fsutil.new_uuid()
        self._cache[rel] = config
        self._cache_mtime[rel] = stamp
        return config

    def invalidate(self, rel: str = "") -> None:
        rel = paths.normalize_rel(rel)
        for key in [key for key in self._cache
                    if key == rel or key.startswith(rel + "/")]:
            self._cache.pop(key, None)
            self._cache_mtime.pop(key, None)

    # -- CRUD strategy hooks ------------------------------------------------

    def _save_config(self, rel: str, cfg: FolderConfig) -> None:
        path = self._config_path(rel)
        fsutil.write_json(path, cfg.to_json())
        self.invalidate(rel)

    def _remove_config(self, rel: str) -> None:
        # Remove this folder's vx.json and any descendants', so no orphaned
        # configs remain after a recursive folder delete.
        base = os.path.dirname(self._config_path(rel))
        if os.path.isdir(base):
            shutil.rmtree(base, ignore_errors=True)
        self.invalidate(rel)

    def _new_folder_config(self, name: str, rel: str) -> FolderConfig:
        return FolderConfig.new(name)

    def _new_file_record(self, name: str, rel: str) -> FileRecord:
        return FileRecord.new(name)

    # -- listing ------------------------------------------------------------

    def list_children(
        self,
        folder: str = "",
        *,
        include_folders: bool = True,
        include_external: bool = True,
    ) -> FolderContents:
        rel = paths.normalize_rel(folder)
        cfg = self._load_config(rel)
        self._sync_folder(rel, cfg)
        content_dir = self.content_path(rel)
        self._sweep_once(content_dir)

        folder_refs: list[NodeRef] = []
        if include_folders:
            for name in cfg.folders:
                child_rel = paths.join_rel(rel, name)
                folder_refs.append(self._child_folder_ref(child_rel, name, cfg.id))

        file_refs = [
            self._file_ref(cfg, rel, record, content_dir)
            for record in cfg.files
        ]

        external_folders: list[NodeRef] = []
        external_files: list[NodeRef] = []
        if include_external:
            external_folders, external_files = self._list_external(
                rel, cfg, content_dir
            )

        return FolderContents(
            folders=tuple(folder_refs),
            files=tuple(file_refs),
            external_folders=tuple(external_folders),
            external_files=tuple(external_files),
        )

    def _child_folder_ref(self, child_rel: str, name: str,
                          parent_id: str) -> NodeRef:
        content = self.content_path(child_rel)
        exists = os.path.isdir(content)
        child_id = ""
        created = modified = 0
        metadata: dict[str, Any] = {}
        try:
            child_cfg = self._load_config(child_rel)
        except ConfigError:
            child_cfg = None
        if child_cfg is not None:
            child_id = child_cfg.id
            created = child_cfg.created_utc
            modified = child_cfg.modified_utc
            metadata = dict(child_cfg.metadata)
            # Index the child so path lookups and tags work even before the
            # user expands it.
            self.store.create_folder(StoreFolder(
                id=child_id, parent_id=parent_id, name=name,
                created_utc=created, modified_utc=modified, metadata=metadata,
            ))
        return NodeRef(
            notebook_id=self.notebook_id,
            path=child_rel,
            name=name,
            id=child_id,
            is_folder=True,
            exists=exists,
            indexed=True,
            created_utc=created,
            modified_utc=modified,
            metadata=metadata,
        )

    def _list_external(self, rel: str, cfg: FolderConfig,
                       content_dir: str) -> tuple[list[NodeRef], list[NodeRef]]:
        known_folders = set(cfg.folders)
        known_files = {record.name for record in cfg.files}
        skip = {_METADATA_DIR}
        skip.add(self._notebook.config.assets_folder.split("/")[0])
        skip.add(self._notebook.config.recycle_bin_folder.split("/")[0])

        folders: list[NodeRef] = []
        files: list[NodeRef] = []
        try:
            entries = sorted(os.scandir(content_dir), key=lambda item: item.name.casefold())
        except OSError:
            return folders, files
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name in skip:
                continue
            if fsutil.is_symlink_or_reparse(entry.path):
                continue
            if entry.is_dir(follow_symlinks=False):
                if name in known_folders:
                    continue
                folders.append(NodeRef(
                    notebook_id=self.notebook_id,
                    path=paths.join_rel(rel, name),
                    name=name, id="", is_folder=True,
                    exists=True, indexed=False,
                ))
            elif entry.is_file(follow_symlinks=False):
                if name in known_files:
                    continue
                files.append(NodeRef(
                    notebook_id=self.notebook_id,
                    path=paths.join_rel(rel, name),
                    name=name, id="", is_folder=False,
                    exists=True, indexed=False,
                    modified_utc=fsutil.mtime_ms(entry.path),
                ))
        return folders, files


# ---------------------------------------------------------------------------
# Raw notebooks
# ---------------------------------------------------------------------------

class RawFolderManager(FolderManager):
    def content_path(self, rel: str) -> str:
        return paths.absolute(self.root, rel)

    def _node_id(self, rel: str) -> str:
        return fsutil.stable_uuid(f"file://{os.path.realpath(self.root)}/{rel}")

    # -- CRUD strategy hooks: disk is the only truth ------------------------

    def _save_config(self, rel: str, cfg: FolderConfig) -> None:
        pass  # structure is derived from disk on every get_config()

    def _remove_config(self, rel: str) -> None:
        pass

    def _new_folder_config(self, name: str, rel: str) -> FolderConfig:
        cfg = FolderConfig(id=self._node_id(rel), name=name)
        cfg.created_utc = cfg.modified_utc = fsutil.now_ms()
        return cfg

    def _new_file_record(self, name: str, rel: str) -> FileRecord:
        record = FileRecord(id=self._node_id(rel), name=name)
        record.created_utc = record.modified_utc = fsutil.now_ms()
        return record

    def get_config(self, folder_path: str = "") -> FolderConfig:
        rel = paths.normalize_rel(folder_path)
        content_dir = self.content_path(rel)
        try:
            stamp = os.stat(content_dir)
        except OSError as error:
            raise NodeNotFoundError("folder does not exist", path=rel) from error
        name = "" if not rel else paths.split_rel(rel)[1]
        config = FolderConfig(
            id=self._node_id(rel),
            name=name,
            created_utc=stamp.st_ctime_ns // 1_000_000,
            modified_utc=stamp.st_mtime_ns // 1_000_000,
        )
        try:
            entries = sorted(os.scandir(content_dir),
                             key=lambda item: item.name.casefold())
        except OSError:
            entries = []
        for entry in entries:
            if entry.name.startswith(".") or entry.name == _METADATA_DIR:
                continue
            if entry.is_dir(follow_symlinks=False):
                config.folders.append(entry.name)
            elif entry.is_file(follow_symlinks=False):
                config.files.append(FileRecord(
                    id=self._node_id(paths.join_rel(rel, entry.name)),
                    name=entry.name,
                    created_utc=entry.stat().st_ctime_ns // 1_000_000,
                    modified_utc=entry.stat().st_mtime_ns // 1_000_000,
                ))
        return config

    def list_children(
        self,
        folder: str = "",
        *,
        include_folders: bool = True,
        include_external: bool = True,
    ) -> FolderContents:
        rel = paths.normalize_rel(folder)
        cfg = self.get_config(rel)
        content_dir = self.content_path(rel)
        self._sweep_once(content_dir)

        folders = [
            NodeRef(
                notebook_id=self.notebook_id,
                path=paths.join_rel(rel, name),
                name=name,
                id=self._node_id(paths.join_rel(rel, name)),
                is_folder=True,
            )
            for name in cfg.folders
        ] if include_folders else []

        files = [
            self._file_ref(cfg, rel, record, content_dir)
            for record in cfg.files
        ]
        return FolderContents(folders=tuple(folders), files=tuple(files))


def create_folder_manager(notebook: Any) -> FolderManager:
    from .models import NotebookType

    if notebook.type is NotebookType.BUNDLED:
        return BundledFolderManager(notebook)
    return RawFolderManager(notebook)


__all__ = [
    "BundledFolderManager",
    "FolderManager",
    "RawFolderManager",
    "create_folder_manager",
]
