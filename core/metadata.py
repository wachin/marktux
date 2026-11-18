"""Per-notebook SQLite cache (vxcore schema v4).

The database is a **derived cache**, never the source of truth: for a bundled
notebook every structural fact lives in ``vx_notebook/contents/**/vx.json`` and
``RebuildCache`` can re-derive the tables at any time. Only ``notebook_metadata``
holds device-local values that cannot be reconstructed (``last_sync_utc``,
``history``, ``tags_synced_utc``), so schema upgrades preserve that table.

Differences from vxcore, all deliberate:

* ``schema_version`` is *read*; a mismatch rebuilds the structure instead of
  silently trusting a stale cache.
* The connection is shared but guarded by an ``RLock`` and opened with
  ``check_same_thread=False``; transactions use ``BEGIN IMMEDIATE``.
* WAL is requested and verified; if the filesystem refuses it (SMB/NFS) we fall
  back to ``journal_mode=DELETE`` with ``synchronous=FULL``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Protocol, Sequence

from .errors import DatabaseError, InvalidStateError, NodeExistsError
from . import fsutil

SCHEMA_VERSION = 4

_DDL = """
CREATE TABLE IF NOT EXISTS folders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uuid TEXT NOT NULL UNIQUE,
  parent_id INTEGER,
  name TEXT NOT NULL,
  created_utc INTEGER NOT NULL,
  modified_utc INTEGER NOT NULL,
  metadata TEXT,
  FOREIGN KEY (parent_id) REFERENCES folders(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);
CREATE INDEX IF NOT EXISTS idx_folders_uuid ON folders(uuid);

CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uuid TEXT NOT NULL UNIQUE,
  folder_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  created_utc INTEGER NOT NULL,
  modified_utc INTEGER NOT NULL,
  metadata TEXT,
  attachments TEXT,
  FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id);
CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
CREATE INDEX IF NOT EXISTS idx_files_uuid ON files(uuid);

CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  parent_id INTEGER,
  metadata TEXT,
  FOREIGN KEY (parent_id) REFERENCES tags(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
CREATE INDEX IF NOT EXISTS idx_tags_parent ON tags(parent_id);

CREATE TABLE IF NOT EXISTS file_tags (
  file_id INTEGER NOT NULL,
  tag_id INTEGER NOT NULL,
  PRIMARY KEY (file_id, tag_id),
  FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
  FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_file_tags_tag ON file_tags(tag_id);

CREATE TABLE IF NOT EXISTS notebook_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY
);
"""

_STRUCTURE_TABLES = ("file_tags", "files", "tags", "folders")


@dataclass(frozen=True, slots=True)
class StoreFolder:
    id: str
    parent_id: str
    name: str
    created_utc: int = 0
    modified_utc: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StoreFile:
    id: str
    folder_id: str
    name: str
    created_utc: int = 0
    modified_utc: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    attachments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StoreTag:
    name: str
    parent_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TagQueryResult:
    file_id: str
    folder_id: str
    file_name: str
    file_path: str
    tags: tuple[str, ...]


class MetadataStore(Protocol):
    """Structural cache contract used by notebooks, folders and tags."""

    @property
    def is_open(self) -> bool: ...

    def close(self) -> None: ...

    def reset_structure(self) -> None: ...

    def create_folder(self, record: StoreFolder) -> None: ...

    def delete_folder(self, folder_id: str) -> None: ...

    def get_folder(self, folder_id: str) -> StoreFolder | None: ...

    def get_folder_by_path(self, path: str) -> StoreFolder | None: ...

    def list_folders(self, parent_id: str) -> list[StoreFolder]: ...

    def folder_path(self, folder_id: str) -> str: ...

    def node_path(self, node_id: str) -> str: ...

    def create_file(self, record: StoreFile) -> None: ...

    def delete_file(self, file_id: str) -> None: ...

    def get_file(self, file_id: str) -> StoreFile | None: ...

    def get_file_by_path(self, path: str) -> StoreFile | None: ...

    def list_files(self, folder_id: str) -> list[StoreFile]: ...

    def set_file_tags(self, file_id: str, tags: Sequence[str]) -> None: ...

    def get_file_tags(self, file_id: str) -> list[str]: ...

    def add_tag_to_file(self, file_id: str, tag: str) -> None: ...

    def remove_tag_from_file(self, file_id: str, tag: str) -> None: ...

    def upsert_tag(self, tag: StoreTag) -> None: ...

    def delete_tag(self, name: str) -> None: ...

    def get_tag(self, name: str) -> StoreTag | None: ...

    def list_tags(self) -> list[StoreTag]: ...

    def find_files_by_tags(self, tags: Sequence[str], *,
                           match_all: bool = True) -> list[TagQueryResult]: ...

    def count_files_by_tag(self) -> dict[str, int]: ...

    def transaction(self) -> Iterator[None]: ...

    def set_file_attachments(self, file_id: str, attachments: Sequence[str]) -> None: ...

    def get_meta(self, key: str) -> str | None: ...

    def set_meta(self, key: str, value: str) -> None: ...


class SqliteMetadataStore:
    def __init__(
        self,
        db_path: str,
        *,
        journal_mode: str = "wal",
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._path = os.path.abspath(db_path)
        self._requested_journal = journal_mode.lower()
        self._busy_timeout = busy_timeout_ms
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._in_transaction = False
        self.journal_mode = ""

    # -- lifecycle ----------------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def open(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            fsutil.ensure_dir(os.path.dirname(self._path) or ".")
            try:
                conn = sqlite3.connect(
                    self._path,
                    timeout=self._busy_timeout / 1000,
                    check_same_thread=False,
                    isolation_level=None,
                )
            except sqlite3.Error as error:
                raise DatabaseError(f"cannot open database: {error}",
                                    path=self._path) from error
            conn.row_factory = sqlite3.Row
            self._conn = conn
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout)}")
                self.journal_mode = self._configure_journal(conn)
                conn.executescript(_DDL)
                self._check_version()
            except sqlite3.Error as error:
                conn.close()
                self._conn = None
                raise DatabaseError(f"cannot initialise database: {error}",
                                    path=self._path) from error

    def _configure_journal(self, conn: sqlite3.Connection) -> str:
        if self._requested_journal not in ("wal", "delete", "truncate",
                                           "persist", "memory", "off"):
            self._requested_journal = "wal"
        try:
            row = conn.execute(
                f"PRAGMA journal_mode = {self._requested_journal.upper()}"
            ).fetchone()
            mode = str(row[0]).lower() if row else ""
        except sqlite3.Error:
            mode = ""
        if self._requested_journal == "wal" and mode != "wal":
            # Network filesystems cannot do WAL's shared-memory locking; prefer
            # durability over concurrency rather than failing to open.
            try:
                row = conn.execute("PRAGMA journal_mode = DELETE").fetchone()
                mode = str(row[0]).lower() if row else "delete"
                conn.execute("PRAGMA synchronous = FULL")
            except sqlite3.Error:
                mode = "delete"
        return mode

    def _check_version(self) -> None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        # A stale *or missing* version means the structure cannot be trusted;
        # rebuild it while keeping the device-local ``notebook_metadata`` rows.
        if row is None or int(row[0]) != SCHEMA_VERSION:
            self.reset_structure()

    def close(self) -> None:
        with self._lock:
            conn = self._conn
            self._conn = None
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def __enter__(self) -> "SqliteMetadataStore":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- plumbing -----------------------------------------------------------

    def _require(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is None:
            raise InvalidStateError("metadata store is not open", path=self._path)
        return conn

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        try:
            return self._require().execute(sql, params)
        except sqlite3.Error as error:
            raise DatabaseError(str(error), path=self._path) from error

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self._execute(sql, params).fetchall())

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self._execute(sql, params).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            if self._in_transaction:
                raise InvalidStateError("nested transactions are not supported")
            conn = self._require()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                raise DatabaseError(str(error), path=self._path) from error
            self._in_transaction = True
            try:
                yield
            except BaseException:
                self._in_transaction = False
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            else:
                self._in_transaction = False
                try:
                    conn.execute("COMMIT")
                except sqlite3.Error as error:
                    raise DatabaseError(str(error), path=self._path) from error

    # -- schema -------------------------------------------------------------

    def reset_structure(self) -> None:
        """Drop and recreate the structural tables, keeping device-local meta."""
        with self._lock:
            conn = self._require()
            try:
                for table in _STRUCTURE_TABLES:
                    conn.execute(f"DROP TABLE IF EXISTS {table}")
                conn.executescript(_DDL)
                conn.execute("DELETE FROM schema_version")
                conn.execute("INSERT INTO schema_version VALUES (?)",
                             (SCHEMA_VERSION,))
            except sqlite3.Error as error:
                raise DatabaseError(f"cannot reset schema: {error}",
                                    path=self._path) from error

    # -- folders ------------------------------------------------------------

    def _folder_pk(self, folder_id: str) -> int | None:
        if not folder_id:
            return None
        row = self._one("SELECT id FROM folders WHERE uuid = ?", (folder_id,))
        return int(row["id"]) if row else None

    def _folder_row_by_pk(self, pk: int | None) -> sqlite3.Row | None:
        if pk is None:
            return None
        return self._one("SELECT * FROM folders WHERE id = ?", (pk,))

    @staticmethod
    def _folder_from_row(row: sqlite3.Row, parent_uuid: str) -> StoreFolder:
        return StoreFolder(
            id=row["uuid"],
            parent_id=parent_uuid,
            name=row["name"],
            created_utc=int(row["created_utc"]),
            modified_utc=int(row["modified_utc"]),
            metadata=_load_json(row["metadata"], {}),
        )

    def create_folder(self, record: StoreFolder) -> None:
        self._write_folder(record, replace=True)

    def insert_folder(self, record: StoreFolder) -> None:
        self._write_folder(record, replace=False)

    def _write_folder(self, record: StoreFolder, *, replace: bool) -> None:
        with self._lock:
            parent_pk = self._folder_pk(record.parent_id)
            if record.parent_id and parent_pk is None:
                raise DatabaseError(f"parent folder not found: {record.parent_id}")
            params = (record.id, parent_pk, record.name, record.created_utc,
                      record.modified_utc,
                      json.dumps(record.metadata, ensure_ascii=False))
            if replace:
                # UPSERT, never INSERT OR REPLACE: replacing the row would
                # delete it and cascade to every child folder and file.
                sql = (
                    "INSERT INTO folders "
                    "(uuid, parent_id, name, created_utc, modified_utc, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(uuid) DO UPDATE SET "
                    "parent_id = excluded.parent_id, name = excluded.name, "
                    "created_utc = excluded.created_utc, "
                    "modified_utc = excluded.modified_utc, "
                    "metadata = excluded.metadata"
                )
            else:
                sql = (
                    "INSERT INTO folders "
                    "(uuid, parent_id, name, created_utc, modified_utc, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?)"
                )
            try:
                self._execute(sql, params)
            except DatabaseError as error:
                if "UNIQUE" in str(error) and not replace:
                    raise NodeExistsError("folder already indexed",
                                          path=record.id) from error
                raise

    def update_folder(self, folder_id: str, name: str, modified_utc: int,
                      metadata: dict[str, Any]) -> None:
        with self._lock:
            self._execute(
                "UPDATE folders SET name = ?, modified_utc = ?, metadata = ? "
                "WHERE uuid = ?",
                (name, modified_utc, json.dumps(metadata, ensure_ascii=False),
                 folder_id),
            )

    def delete_folder(self, folder_id: str) -> None:
        with self._lock:
            self._execute("DELETE FROM folders WHERE uuid = ?", (folder_id,))

    def get_folder(self, folder_id: str) -> StoreFolder | None:
        with self._lock:
            row = self._one("SELECT * FROM folders WHERE uuid = ?", (folder_id,))
            if row is None:
                return None
            parent = self._folder_row_by_pk(row["parent_id"])
            parent_uuid = parent["uuid"] if parent else ""
            return self._folder_from_row(row, parent_uuid)

    def get_folder_by_path(self, path: str) -> StoreFolder | None:
        from .paths import normalize_rel

        with self._lock:
            rel = normalize_rel(path)
            root = self._one("SELECT * FROM folders WHERE parent_id IS NULL")
            if root is None:
                return None
            if not rel:
                return self._folder_from_row(root, "")
            parent_pk = int(root["id"])
            row = None
            for part in rel.split("/"):
                row = self._one(
                    "SELECT * FROM folders WHERE parent_id IS ? AND name = ?",
                    (parent_pk, part),
                )
                if row is None:
                    return None
                parent_pk = int(row["id"])
            assert row is not None
            parent = self._folder_row_by_pk(row["parent_id"])
            return self._folder_from_row(row, parent["uuid"] if parent else "")

    def list_folders(self, parent_id: str) -> list[StoreFolder]:
        with self._lock:
            parent_pk = self._folder_pk(parent_id)
            rows = self._query(
                "SELECT * FROM folders WHERE parent_id IS ? ORDER BY name COLLATE NOCASE",
                (parent_pk,),
            )
            return [self._folder_from_row(row, parent_id) for row in rows]

    def folder_path(self, folder_id: str) -> str:
        with self._lock:
            parts: list[str] = []
            row = self._one("SELECT * FROM folders WHERE uuid = ?", (folder_id,))
            guard = 0
            while row is not None and guard < 10_000:
                guard += 1
                name = str(row["name"])
                if name:
                    parts.append(name)
                row = self._folder_row_by_pk(row["parent_id"])
            return "/".join(reversed(parts))

    def node_path(self, node_id: str) -> str:
        with self._lock:
            row = self._one("SELECT * FROM files WHERE uuid = ?", (node_id,))
            if row is not None:
                folder = self._folder_row_by_pk(row["folder_id"])
                base = self.folder_path(folder["uuid"]) if folder else ""
                return f"{base}/{row['name']}" if base else str(row["name"])
            folder = self._one("SELECT * FROM folders WHERE uuid = ?", (node_id,))
            if folder is None:
                return ""
            return self.folder_path(node_id)

    def move_folder(self, folder_id: str, new_parent_id: str) -> None:
        with self._lock:
            parent_pk = self._folder_pk(new_parent_id)
            self._execute("UPDATE folders SET parent_id = ? WHERE uuid = ?",
                          (parent_pk, folder_id))

    # -- files --------------------------------------------------------------

    def _file_from_row(self, row: sqlite3.Row, folder_uuid: str,
                       tags: tuple[str, ...] = ()) -> StoreFile:
        return StoreFile(
            id=row["uuid"],
            folder_id=folder_uuid,
            name=row["name"],
            created_utc=int(row["created_utc"]),
            modified_utc=int(row["modified_utc"]),
            metadata=_load_json(row["metadata"], {}),
            tags=tags,
            attachments=tuple(_load_json(row["attachments"], [])),
        )

    def create_file(self, record: StoreFile) -> None:
        self._write_file(record, replace=True)

    def insert_file(self, record: StoreFile) -> None:
        self._write_file(record, replace=False)

    def _write_file(self, record: StoreFile, *, replace: bool) -> None:
        with self._lock:
            folder_pk = self._folder_pk(record.folder_id)
            if folder_pk is None:
                raise DatabaseError(f"folder not found: {record.folder_id}")
            attachments = json.dumps(list(record.attachments),
                                     ensure_ascii=False) if record.attachments else None
            params = (record.id, folder_pk, record.name, record.created_utc,
                      record.modified_utc,
                      json.dumps(record.metadata, ensure_ascii=False),
                      attachments)
            if replace:
                # UPSERT for the same reason as folders: REPLACE would delete
                # the row and cascade away its tag links.
                sql = (
                    "INSERT INTO files "
                    "(uuid, folder_id, name, created_utc, modified_utc, metadata, "
                    "attachments) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(uuid) DO UPDATE SET "
                    "folder_id = excluded.folder_id, name = excluded.name, "
                    "created_utc = excluded.created_utc, "
                    "modified_utc = excluded.modified_utc, "
                    "metadata = excluded.metadata, "
                    "attachments = excluded.attachments"
                )
            else:
                sql = (
                    "INSERT INTO files "
                    "(uuid, folder_id, name, created_utc, modified_utc, metadata, "
                    "attachments) VALUES (?, ?, ?, ?, ?, ?, ?)"
                )
            try:
                self._execute(sql, params)
            except DatabaseError as error:
                if "UNIQUE" in str(error) and not replace:
                    raise NodeExistsError("file already indexed",
                                          path=record.id) from error
                raise

    def update_file(self, file_id: str, name: str, modified_utc: int,
                    metadata: dict[str, Any]) -> None:
        with self._lock:
            self._execute(
                "UPDATE files SET name = ?, modified_utc = ?, metadata = ? "
                "WHERE uuid = ?",
                (name, modified_utc, json.dumps(metadata, ensure_ascii=False),
                 file_id),
            )

    def delete_file(self, file_id: str) -> None:
        with self._lock:
            self._execute("DELETE FROM files WHERE uuid = ?", (file_id,))

    def get_file(self, file_id: str) -> StoreFile | None:
        with self._lock:
            row = self._one("SELECT * FROM files WHERE uuid = ?", (file_id,))
            if row is None:
                return None
            folder = self._folder_row_by_pk(row["folder_id"])
            tags = tuple(self.get_file_tags(file_id))
            return self._file_from_row(row, folder["uuid"] if folder else "", tags)

    def get_file_by_path(self, path: str) -> StoreFile | None:
        from .paths import normalize_rel, split_rel

        with self._lock:
            rel = normalize_rel(path)
            parent, name = split_rel(rel)
            folder = self.get_folder_by_path(parent)
            if folder is None:
                return None
            row = self._one(
                "SELECT f.* FROM files f JOIN folders d ON f.folder_id = d.id "
                "WHERE d.uuid = ? AND f.name = ?",
                (folder.id, name),
            )
            if row is None:
                return None
            tags = tuple(self.get_file_tags(str(row["uuid"])))
            return self._file_from_row(row, folder.id, tags)

    def list_files(self, folder_id: str) -> list[StoreFile]:
        with self._lock:
            folder_pk = self._folder_pk(folder_id)
            if folder_pk is None:
                return []
            rows = self._query(
                "SELECT * FROM files WHERE folder_id = ? ORDER BY name COLLATE NOCASE",
                (folder_pk,),
            )
            if not rows:
                return []
            tag_rows = self._query(
                "SELECT ft.file_id, t.name FROM file_tags ft "
                "JOIN tags t ON ft.tag_id = t.id "
                "WHERE ft.file_id IN (SELECT id FROM files WHERE folder_id = ?)",
                (folder_pk,),
            )
            by_file: dict[int, list[str]] = {}
            for tag_row in tag_rows:
                by_file.setdefault(int(tag_row["file_id"]), []).append(
                    str(tag_row["name"])
                )
            return [
                self._file_from_row(
                    row, folder_id, tuple(by_file.get(int(row["id"]), ()))
                )
                for row in rows
            ]

    def move_file(self, file_id: str, new_folder_id: str) -> None:
        with self._lock:
            folder_pk = self._folder_pk(new_folder_id)
            if folder_pk is None:
                raise DatabaseError(f"folder not found: {new_folder_id}")
            self._execute("UPDATE files SET folder_id = ? WHERE uuid = ?",
                          (folder_pk, file_id))

    def iterate_files(
        self, callback: Callable[[str, StoreFile], bool]
    ) -> None:
        """Visit every indexed file with its relative path; stop when False."""
        with self._lock:
            rows = self._query("SELECT * FROM files ORDER BY name COLLATE NOCASE")
            paths = {
                str(row["uuid"]): self.folder_path(str(row["uuid"]))
                for row in self._query("SELECT uuid FROM folders")
            }
            folder_uuids = {
                int(row["id"]): str(row["uuid"])
                for row in self._query("SELECT id, uuid FROM folders")
            }
            tag_rows = self._query(
                "SELECT ft.file_id, t.name FROM file_tags ft "
                "JOIN tags t ON ft.tag_id = t.id"
            )
            tags: dict[int, list[str]] = {}
            for tag_row in tag_rows:
                tags.setdefault(int(tag_row["file_id"]), []).append(
                    str(tag_row["name"])
                )
            for row in rows:
                folder_uuid = folder_uuids.get(int(row["folder_id"]), "")
                base = paths.get(folder_uuid, "")
                path = f"{base}/{row['name']}" if base else str(row["name"])
                record = self._file_from_row(
                    row, folder_uuid, tuple(tags.get(int(row["id"]), ()))
                )
                if callback(path, record) is False:
                    return

    # -- tags ---------------------------------------------------------------

    def upsert_tag(self, tag: StoreTag) -> None:
        with self._lock:
            parent_pk = None
            if tag.parent_name:
                row = self._one("SELECT id FROM tags WHERE name = ?",
                                (tag.parent_name,))
                if row is not None:
                    parent_pk = int(row["id"])
            self._execute(
                "INSERT INTO tags (name, parent_id, metadata) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET parent_id = excluded.parent_id, "
                "metadata = excluded.metadata",
                (tag.name, parent_pk,
                 json.dumps(tag.metadata, ensure_ascii=False)),
            )

    def delete_tag(self, name: str) -> None:
        with self._lock:
            self._execute("DELETE FROM tags WHERE name = ?", (name,))

    def get_tag(self, name: str) -> StoreTag | None:
        with self._lock:
            row = self._one("SELECT * FROM tags WHERE name = ?", (name,))
            if row is None:
                return None
            parent_name = ""
            if row["parent_id"] is not None:
                parent = self._one("SELECT name FROM tags WHERE id = ?",
                                   (row["parent_id"],))
                parent_name = str(parent["name"]) if parent else ""
            return StoreTag(str(row["name"]), parent_name,
                            _load_json(row["metadata"], {}))

    def list_tags(self) -> list[StoreTag]:
        with self._lock:
            rows = self._query("SELECT * FROM tags ORDER BY name COLLATE NOCASE")
            names = {int(row["id"]): str(row["name"]) for row in rows}
            return [
                StoreTag(
                    str(row["name"]),
                    names.get(int(row["parent_id"]), "") if row["parent_id"] else "",
                    _load_json(row["metadata"], {}),
                )
                for row in rows
            ]

    def set_file_tags(self, file_id: str, tags: Sequence[str]) -> None:
        with self._lock:
            row = self._one("SELECT id FROM files WHERE uuid = ?", (file_id,))
            if row is None:
                raise DatabaseError(f"file not found: {file_id}")
            file_pk = int(row["id"])
            self._execute("DELETE FROM file_tags WHERE file_id = ?", (file_pk,))
            for name in dict.fromkeys(tags):
                tag_row = self._one("SELECT id FROM tags WHERE name = ?", (name,))
                if tag_row is None:
                    self._execute("INSERT INTO tags (name) VALUES (?)", (name,))
                    tag_row = self._one("SELECT id FROM tags WHERE name = ?",
                                        (name,))
                if tag_row is not None:
                    self._execute(
                        "INSERT OR IGNORE INTO file_tags (file_id, tag_id) "
                        "VALUES (?, ?)",
                        (file_pk, int(tag_row["id"])),
                    )

    def add_tag_to_file(self, file_id: str, tag: str) -> None:
        with self._lock:
            current = self.get_file_tags(file_id)
            if tag not in current:
                self.set_file_tags(file_id, current + [tag])

    def remove_tag_from_file(self, file_id: str, tag: str) -> None:
        with self._lock:
            current = [name for name in self.get_file_tags(file_id) if name != tag]
            self.set_file_tags(file_id, current)

    def get_file_tags(self, file_id: str) -> list[str]:
        with self._lock:
            row = self._one("SELECT id FROM files WHERE uuid = ?", (file_id,))
            if row is None:
                return []
            rows = self._query(
                "SELECT t.name FROM file_tags ft JOIN tags t ON ft.tag_id = t.id "
                "WHERE ft.file_id = ? ORDER BY t.name COLLATE NOCASE",
                (int(row["id"]),),
            )
            return [str(item["name"]) for item in rows]

    def find_files_by_tags(self, tags: Sequence[str], *,
                           match_all: bool = True) -> list[TagQueryResult]:
        wanted = list(dict.fromkeys(tags))
        if not wanted:
            return []
        with self._lock:
            placeholders = ",".join("?" for _ in wanted)
            having = "HAVING COUNT(DISTINCT t.name) = ?" if match_all else ""
            params: list[Any] = list(wanted)
            if match_all:
                params.append(len(wanted))
            rows = self._query(
                "SELECT f.uuid AS file_uuid, f.name AS file_name, d.uuid AS folder_uuid "
                "FROM files f "
                "JOIN folders d ON f.folder_id = d.id "
                "JOIN file_tags ft ON ft.file_id = f.id "
                "JOIN tags t ON ft.tag_id = t.id "
                f"WHERE t.name IN ({placeholders}) "
                "GROUP BY f.id " + having,
                params,
            )
            results: list[TagQueryResult] = []
            for row in rows:
                file_id = str(row["file_uuid"])
                folder_id = str(row["folder_uuid"])
                base = self.folder_path(folder_id)
                path = f"{base}/{row['file_name']}" if base else str(row["file_name"])
                results.append(TagQueryResult(
                    file_id=file_id,
                    folder_id=folder_id,
                    file_name=str(row["file_name"]),
                    file_path=path,
                    tags=tuple(self.get_file_tags(file_id)),
                ))
            results.sort(key=lambda item: item.file_path.casefold())
            return results

    def count_files_by_tag(self) -> dict[str, int]:
        with self._lock:
            rows = self._query(
                "SELECT t.name, COUNT(ft.file_id) AS total FROM tags t "
                "LEFT JOIN file_tags ft ON ft.tag_id = t.id "
                "GROUP BY t.id ORDER BY t.name COLLATE NOCASE"
            )
            return {str(row["name"]): int(row["total"]) for row in rows}

    # -- attachments --------------------------------------------------------

    def set_file_attachments(self, file_id: str,
                             attachments: Sequence[str]) -> None:
        with self._lock:
            payload = json.dumps(list(dict.fromkeys(attachments)),
                                 ensure_ascii=False) if attachments else None
            self._execute("UPDATE files SET attachments = ? WHERE uuid = ?",
                          (payload, file_id))

    def get_file_attachments(self, file_id: str) -> list[str]:
        with self._lock:
            row = self._one("SELECT attachments FROM files WHERE uuid = ?",
                            (file_id,))
            return list(_load_json(row["attachments"], [])) if row else []

    # -- notebook metadata --------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._one("SELECT value FROM notebook_metadata WHERE key = ?",
                            (key,))
            return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._execute(
                "INSERT INTO notebook_metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete_meta(self, key: str) -> None:
        with self._lock:
            self._execute("DELETE FROM notebook_metadata WHERE key = ?", (key,))

    # -- diagnostics --------------------------------------------------------

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "folders": int(self._one("SELECT COUNT(*) AS n FROM folders")["n"]),
                "files": int(self._one("SELECT COUNT(*) AS n FROM files")["n"]),
                "tags": int(self._one("SELECT COUNT(*) AS n FROM tags")["n"]),
            }


def _load_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "MetadataStore",
    "SCHEMA_VERSION",
    "SqliteMetadataStore",
    "StoreFile",
    "StoreFolder",
    "StoreTag",
    "TagQueryResult",
]
