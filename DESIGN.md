# marktux — core port design decisions

This document records the architectural decisions behind the Python port of
vxcore (the `src/marktux/core/` package and the `src/marktux/ui/` Qt adapter). The editor component's
own decisions live in [`libs/mtextedit/DESIGN.md`](libs/mtextedit/DESIGN.md).

VNote's vxcore (`libs/vxcore`, ~61k LOC of C++) is the reference for behaviour
and on-disk formats; we reuse its *contract*, not its code.

---

## C1. Reimplement vxcore's contract, not the C ABI

**Decision.** `src/marktux/core/` is a Pythonic reimplementation of vxcore's data model and
behaviour, not a ctypes wrapper. There is no `VxCoreContext*` handle, no 241
`vxcore_*` functions, no JSON-in/JSON-out marshalling and no 37 error codes.
Instead: dataclasses, a single `CoreContext` facade, and a Python exception
hierarchy.

**Why.** The C ABI exists because the C library cannot own threads or raise
exceptions; Python has both. Mirroring it would import all of vxcore's rough
edges for no benefit.

**Consequences.** Existing VNote notebooks still open unchanged (formats are
preserved, see C2), but code written against the C ABI does not transfer.

## C2. vxcore-compatible on-disk formats

**Decision.** Bundled notebooks use `vx_notebook/config.json` and
`vx_notebook/contents/**/vx.json`; the SQLite cache is per-notebook at
`<local-data>/notebooks/<uuid>/metadata.db` with vxcore's schema v4; sessions
live in `vxsession.json` and app config in `vxcore.json`. A plain directory with
no `vx_notebook/` opens as a **raw** notebook.

**Why.** Opening a user's existing VNote notebooks unchanged is a hard goal,
and it lets vxcore's C++ behavioural tests serve as specs. The cache is a
*derived* artifact: `vx.json` is the source of truth and `rebuild_cache()`
re-derives it.

**Deviations (deliberate, documented in code).** Note saves are atomic
(temp+rename+fsync) rather than vxcore's plain `ofstream`; `schema_version` is
read and a mismatch rebuilds the cache; unknown JSON keys are preserved in an
`extra` dict; `available_name` checks disk and index case-insensitively. Like
vxcore, content search has no FTS index and re-scans indexed notes per query;
the schema v4 cache stays exact and a pluggable `rg` backend provides the fast
path. Millisecond timestamps, `/`-separated relative paths and the `vx.json`
file/attachment structures match vxcore exactly.

## C3. Qt-free core + thin Qt adapter

**Decision.** `src/marktux/core/` imports no PyQt6. It uses plain classes, `threading`,
`concurrent.futures` and exceptions. `src/marktux/ui/core_bridge.py` (`CoreBridge(QObject)`)
submits core calls to `ctx.tasks` and re-emits results as Qt signals;
`src/marktux/ui/tree_model.py` is the Qt-only notebook tree model.

**Why.** The core must be importable and testable headless, and reusable in the
e-Sword-like fourth-panel project, which should not drag in marktux's UI layer.
A test AST-scans `src/marktux/core/` and fails on any Qt import.

**Consequences.** All thread-safety lives in core (per-notebook/per-buffer locks,
WAL SQLite); the UI only ever crosses the boundary through signals. `list_children`
is fast enough (one `scandir`) to run inline in the tree model.

## C4. Scope: spine + P1, sync/encryption excluded

**Decision.** Port the spine VNote actually exercises: context/config, notebook
open/create/close, folder+file CRUD, buffers (open/save/dirty/external-change/
backup), SQLite cache, tags, search, workspaces/session restore, assets, file
types. **Out of scope:** git/WebDAV/Jianguoyun sync, `.vne` encryption, recycle
bin, cross-notebook transfer, snippets, templates, the mobile API and the C ABI.

**Why.** VNote's frontend calls ~213 of the 239 C functions, and a much smaller
spine covers "open a notebook, browse the tree, edit and save a note". Sync and
encryption are large, isolated subsystems with their own failure modes and are
not needed for the core editing experience.

## C5. Caller-owned daemon task pool instead of caller-drained work queues

**Decision.** vxcore exposes named work queues that the *caller's* threads must
drain (`process_next`). Here `src/marktux/core/concurrency.py` (`TaskRunner`) owns a small
fixed pool of **daemon** threads and returns a `Task` handle with a
`CancellationToken`. Long operations (load/save/rebuild/search/CRUD) are
offloaded; trivial ones (list children, set text, session mutations) stay inline.

**Why.** Python can own threads; daemon threads guarantee a stuck search or a
blocked network mount cannot keep the process alive after the window closes.
Cancellation is cooperative and checked per file / every N lines.

## C6. Session ownership split

**Decision.** `core` owns `vxsession.json` (open notebooks, workspaces, buffers,
read-only/trusted flags) and writes it once at `prepare_shutdown()`. The Qt app
owns window geometry in `QSettings` and never writes it into core's session file.

**Why.** vxcore makes this split and it keeps geometry (a UI concern) out of a
portable backend that may run in another project.

## C7. Populate the derived SQLite cache on notebook open

**Decision.** When a notebook is opened (or created), `Notebook.__init__` calls
`rebuild_cache()` if the structure tables are empty. `vx.json` remains the sole
source of truth; the database is always a disposable projection of it.

**Why.** vxcore re-derives its cache on open using a version stamp. Here folder
config is authoritative, so a cold cache would otherwise make path lookups and
CRUD fail until the user manually expanded every folder: write-through syncing
would attach a new folder to no parent (`parent_id=""`), breaking
`get_folder_by_path` and cascade deletes. Rebuilding when empty keeps open fast
for an already-warm notebook while making a freshly opened one immediately
correct.

## C8. Tags live in config; the SQLite mirror is stamp-driven

**Decision.** Tag definitions form a forest in `NotebookConfig.tags`
(`name`/`parent`/`metadata`) with a `tagsModifiedUtc` stamp, persisted in the
bundled notebook's `config.json`. A derived copy is mirrored into the SQLite
`tags`/`file_tags` tables. `TagManager.sync_to_store()` refreshes the mirror
only when `tagsModifiedUtc > tagsSyncedUtc` (stored in `notebook_metadata`),
inserting parents before children in one transaction and deleting orphans. File
tag assignments are stored per-file in `vx.json` and mirrored to the cache. The
stamp is forced strictly increasing so two edits in the same millisecond still
trigger a sync.

**Why.** This is vxcore's `SyncTagsToMetadataStore` contract: config is the
source of truth and the mirror answers queries without scanning every `vx.json`.
The stamp skips redundant resyncs on open; a monotonic stamp closes a race where
rapid edits collide at millisecond resolution. Raw notebooks have no persisted
config for tags, so all tag mutations raise `UnsupportedError` (as in vxcore).

## C9. Search scans the index with pluggable backends and streams batches

**Decision.** Content search never walks the filesystem blindly: it iterates
the notebook's `vx.json` index (`iterate_all_files()`), decodes each note with
the configured codec list (UTF-8 then GB18030 by default, replacement on
failure) and matches with one of two interchangeable backends.
`SimpleSearchBackend` compiles one Python `re` pattern; `RgSearchBackend` shells
out to `rg --json` and converts its byte offsets to Python codepoint columns.
When `rg` is configured but unavailable a `search.backend_fallback` event is
emitted and the manager falls back to `simple`. Matches use 1-based line numbers
and 0-based exclusive character columns. Large notebooks are streamed in
index-order chunks (default 64) and a `SearchBatch` is emitted for every chunk,
including empty ones, so the UI can show deterministic progress; `max_results`
caps total match occurrences for the blob API. Cancellation is checked before
each file, so it takes effect inside a chunk. `CoreBridge.search()` runs the
stream off-thread and re-emits batches as Qt signals, with `cancel_search()` for
early stop.

**Why.** It mirrors vxcore's `vxcore_search` payload (line/column conventions,
scope filters, encoding list, backend fallback) so behavioural tests port
directly, while keeping the Qt-free boundary: the GUI depends only on signals.
Streaming empty chunks and per-file cancellation make a large search responsive
and interruptible rather than blocking until the whole scan finishes.

## C10. Workspaces and session restore mirror vxcore's snapshot rules

**Decision.** `WorkspaceManager` owns a named, ordered list of open buffer ids
per workspace and folds notebooks, workspaces and buffers into
`vxsession.json`. Notebooks are always reopened from the session; buffers and
workspaces are restored only when `recoverLastSession` is true. Buffer records
carry a `fileId`, so a note renamed between sessions reopens at its current
path by resolving the id against the notebook index; a missing file is skipped
and a workspace's current buffer falls back to the first survivor. Preview
workspaces are transient: on save they are dropped along with any buffer only
they reference, and a current preview workspace is replaced by the first
ordinary one. `prepare_shutdown()` snapshots once and is idempotent; `close()`
never overwrites that snapshot. Window geometry stays in the app's `QSettings`.

**Why.** This is vxcore's `PreviewWorkspaceFilter` / `LoadBuffers` /
`LoadWorkspaces` contract. Stable buffer ids across a restart keep workspace
references valid, and the `fileId` fallback closes a real gap: without it a
renamed note silently disappears from the restored tabs. Keeping geometry out
of `vxsession.json` makes the file portable and backend-only.

## C11. Assets are per-note folders; attachments are indexed assets

**Decision.** A note's assets live under
`<notebook_root>/<note_parent>/<assetsFolder>/<note_uuid>/` (default relative
`vx_assets`; an absolute `assetsFolder` is used verbatim). Inserts dedupe names
with `_1`, `_2` … before publishing and return a notebook-relative `/` path.
Deletion resolves the path inside the notebook root and refuses traversal or
symlink crossings. Attachments are assets whose basename is also stored in the
note's `vx.json` `attachments` list (mirrored to SQLite, omitted when empty).
Raw notebooks have no `vx.json`, so assets work there (ids are path-derived)
but attachments raise `UnsupportedError`. The editor exposes a host
`set_image_paster` hook: a paste stores the image through `AssetManager` and
inserts a note-relative Markdown link.

**Why.** This is vxcore's `StandardBufferProvider` layout, so existing VNote
notebooks keep resolving their images. Keeping the editor notebook-agnostic via
a callback preserves `mtextedit`'s reusability. File types are a config table
(`FileTypesConfig`) with `by_suffix`/`by_name` and an `Others` fallback.

## C12. The cache is rebuilt, not migrated; writes are atomic and swept

**Decision.** `schema_version` is read, never trusted blindly: a stale *or
missing* row drops and recreates the four structure tables (`folders`, `files`,
`tags`, `file_tags`) and rewrites the version, while `notebook_metadata`
(device-local `last_sync_utc`, history, `tags_synced_utc`) is preserved. If the
structure is then empty, the notebook repopulates it from `vx.json`. Every
config/note write goes through `fsutil.atomic_write_bytes` (temp file in the
destination directory, `fsync`, `os.replace` with a Windows `PermissionError`
backoff); a failure unlinks the temp file and leaves the original untouched.
Stale `.<name>.tmp-*` files left by a crash are swept (older than one hour)
when a notebook is opened and the first time a folder is listed. If the
filesystem refuses WAL (SMB/NFS), the store falls back to
`journal_mode=DELETE` with `synchronous=FULL`. The legacy flat
`~/.cache/marktux/meta.db` is never opened; per-notebook caches live under
`<local_data>/notebooks/<id>/metadata.db`.

**Why.** The SQLite database is a derived cache, so rebuilding is always safer
(and cheaper to maintain) than a migration ladder; only the device-local table
is irreplaceable. Atomic replace plus a sweep makes a crash at any point leave
either the old or the new file, never a truncated one.

## C13. A standalone Markdown file opens through a notebook

**Decision.** *File → Open Markdown File…* never edits a file as a
notebook-less document. If the picked file lies inside an **open** notebook
(bundled or raw), that notebook is reused and the note opens at its
`/`-separated relative path. Otherwise the file's **folder** is opened as a raw
notebook and the file opens there as an ordinary note.

**Why.** `BufferManager.open_abs()` can already wrap any absolute path in an
external buffer, and that is the right primitive for a host with no notebook
model at all. Here it would be the wrong default: an external buffer has no
notebook-relative path, no row in the tree, no derived-cache entry and no file
id, so the search dock, tag dock, rename/duplicate/move and session restore would
silently not apply to it — the note would be half-first-class. Opening the folder
as a raw notebook keeps every write on core's atomic save path (C12) and gives
the note a path-derived `uuid5` id (C2) that survives restarts.

**Consequences.** Picking `~/dir/note.md` turns `~/dir` into a notebook, which
core also records in the session, so it is still open after a restart. That is
literal: a file in a huge or inappropriate directory indexes that directory. The
index build runs on the task pool, so the window stays responsive meanwhile.
Containment is checked first, so a note inside an already-open bundled notebook
keeps its tags and `vx.json` identity instead of being shadowed by a raw view of
the same files.

## Delivery phases

1. **Foundation + read-only browsing** — config/metadata reads, notebook/folder
   listing, lazy tree, session persistence. *(implemented)*
2. **Buffers** — lazy load, revision/dirty, atomic save, external-change matrix,
   `.vswp` backups. *(implemented)*
3. **CRUD** — folder/file create/rename/move/copy/delete, cache write-through,
   read-only enforcement, tree context menu. *(implemented)*
4. **Tags** — tag tree in config, store sync, AND/OR queries, counts, delete
   cascade, tag dock. *(implemented)*
5. **Search** — simple + ripgrep backends, streaming batches, cancellation,
   scope filters, encodings. *(implemented)*
6. **Workspaces + session restore** — preview filtering, buffer restore,
   `fileId` rename recovery, write-once shutdown. *(implemented)*
7. **Assets + file types** — asset insert/delete, attachments, paste-image.
   *(implemented)*
8. **Hardening** — concurrency stress, WAL fallback, temp sweep, legacy DB
   handling, docs. *(implemented)*

## Non-goals

* Porting C++ sync/encryption/recycle-bin/transfer code.
* A C ABI or JSON-string marshalling layer.
* Importing PyQt6 anywhere under `src/marktux/core/`.
