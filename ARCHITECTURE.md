# marktux architecture

This document describes **what exists today** and how the pieces fit together.
For *why* each decision was made, read [`DESIGN.md`](DESIGN.md) (core) and
[`libs/mtextedit/DESIGN.md`](libs/mtextedit/DESIGN.md) (editor). For setup and
workflow, read [`CONTRIBUTING.md`](CONTRIBUTING.md).

## 1. What marktux is

A Markdown notebook for the desktop: notebooks on disk, a lazy tree, one editor
pane with VNote-style in-place preview, tags, search, and session restore.
It is a from-scratch PyQt6 reimplementation of VNote's ideas — not a wrapper
around VNote or its C++ backend.

The core port (delivery phases 1–8) is complete: bundled and raw notebooks,
buffers, CRUD, tags, search, workspaces/session, assets, and the hardening
guarantees. The UI is functional but deliberately small; see
[§12 Known gaps](#12-known-gaps).

## 2. Goals and non-goals

**Hard goals**

* Open existing VNote notebooks unchanged (same files, same `vx.json` schema).
* Keep the GUI responsive: no Markdown parse, image decode, disk scan or
  content search on the GUI thread.
* Keep the backend reusable: `core/` must import **no** Qt and must be testable
  headless.
* Keep the editor reusable: `mtextedit` is a standalone package with its own
  `pyproject.toml`, usable in another PyQt6 application.

**Non-goals** (explicitly out of scope, not "unfinished")

* VNote's sync, encryption, recycle bin, cross-notebook transfer, snippets,
  templates, mobile API and C ABI.
* A side-by-side preview pane or a `QWebEngineView` reload per keystroke.
* A second Markdown dialect: parsing is `markdown-it-py` with a fixed preset.

## 3. System map

```
┌──────────────────────────────────────────────────────────────────────┐
│ main.py — composition root                                           │
│   QMainWindow: notebook tree, editor, outline, tag dock, search dock │
└───────────────┬───────────────────────────────┬──────────────────────┘
                │ Qt signals/slots              │ direct widget API
┌───────────────▼──────────────┐   ┌────────────▼─────────────────────┐
│ ui/ — thin Qt adapter        │   │ libs/mtextedit — editor package  │
│   CoreBridge(QObject)        │   │   MTextEdit, OutlineView,        │
│   NotebookNodeModel          │   │   RenderManager, FindReplace     │
└───────────────┬──────────────┘   └────────────┬─────────────────────┘
                │ Python calls (off-thread)      │ Qt only
┌───────────────▼────────────────────────────────▼─────────────────────┐
│ core/ — Qt-free backend (stdlib only)                                │
│   CoreContext → config · events · tasks · notebooks · buffers ·      │
│                 workspaces · watchdog                                │
└───────────────┬──────────────────────────────────────────────────────┘
                │ filesystem + SQLite
┌───────────────▼──────────────────────────────────────────────────────┐
│ On disk: vx_notebook/config.json, contents/**/vx.json, note files,   │
│          <local-data>/notebooks/<id>/metadata.db, vxsession.json     │
└──────────────────────────────────────────────────────────────────────┘
```

Import direction is one-way: `main.py` → `ui/` → `core/`, and `main.py` →
`mtextedit`. `core/` never imports `ui/`, `main`, or PyQt6. `mtextedit` never
imports `core/`.

## 4. Layers

| Layer | Location | May import | Responsibility |
|---|---|---|---|
| Composition root | `main.py` | PyQt6, `core`, `ui`, `mtextedit` | Window, menus, docks, wiring signals, restoring the last buffer |
| Qt adapter | `ui/` | PyQt6, `core` | Submit slow core calls to the task pool; re-emit results as signals; lazy tree model |
| Backend | `core/` | Python stdlib only | Notebooks, folders, buffers, metadata, tags, search, workspaces, assets |
| Editor | `libs/mtextedit/` | PyQt6, `markdown-it-py`, `pygments` | Single-pane editor, async renderer, in-place preview, outline, find/replace |

`main.py` puts `libs/mtextedit` on `sys.path` before importing `mtextedit`
(`main.py:26-27`), so no install step is required for the application. The
editor can also be installed on its own (`pip install -e libs/mtextedit`).

## 5. Core module reference

| Module | Owns |
|---|---|
| `context.py` | `CoreContext` — the only entry point; lifecycle, wiring, `open()`/`close()`/`prepare_shutdown()` |
| `config.py` | `ConfigManager` — `vxcore.json` (app data) and `vxsession.json` (local data), XDG/Windows/macOS paths |
| `models.py` | All dataclasses/enums and their JSON codecs (`NotebookConfig`, `FolderConfig`, `FileRecord`, `BufferRecord`, `WorkspaceConfig`, `SessionConfig`, `ContentQuery`, …) |
| `notebook.py` | `Notebook`, `NotebookManager` — bundled/raw detection, open/close, cache rebuild, session records, read-only/trusted flags |
| `folder.py` | `FolderManager` (abstract), `BundledFolderManager` (`vx.json`), `RawFolderManager` (disk, uuid5 ids), CRUD, attachments |
| `metadata.py` | `SqliteMetadataStore` — per-notebook derived cache, schema v4, WAL with fallback, transactions |
| `buffer.py` | `Buffer`, `BufferManager` — lazy load, revision/dirty, atomic save, BOM/EOL, external-change state machine, `.vswp` backups |
| `tags.py` | `TagManager` — tag forest in config, stamp-driven SQLite mirror, AND/OR queries, counts, delete cascade |
| `search.py` | `SearchManager` + `SimpleSearchBackend` / `RgSearchBackend` — index-guided content search, streaming batches, cancellation |
| `workspace.py` | `WorkspaceManager`, `filter_session()` — workspaces, preview filtering, snapshot, orphan handling |
| `assets.py` | `AssetManager` — per-note asset folders, dedupe, containment, attachment metadata |
| `paths.py` | Relative-path algebra: normalize/split/join, containment, name validation |
| `fsutil.py` | Atomic writes, BOM/EOL codecs, ms clocks, UUIDs, temp sweep |
| `events.py` | `EventBus` and the event-name constants |
| `concurrency.py` | `CancellationToken`, `Task`, `TaskRunner`, `BufferWatchdog` |
| `errors.py` | `CoreError` hierarchy with stable `code` strings |

Everything a caller needs is re-exported from `core/__init__.py`.

## 6. Data model and on-disk formats

### 6.1 Notebook kinds

* **Bundled** — has `vx_notebook/config.json`. Structure is indexed in
  `vx_notebook/contents/**/vx.json`; note ids are stable UUIDs stored there.
* **Raw** — any plain directory. Structure is whatever is on disk; ids are
  deterministic `uuid5` values derived from the relative path, so they survive
  restarts without a session. Tags and attachments are rejected
  (`UnsupportedError`) because there is no `vx.json` to hold them.

```
<bundled root>/
  vx_notebook/
    config.json                 # id, name, tags, file-type overrides
    contents/
      vx.json                   # root folder: folders[] + files[]
      notes/vx.json             # subfolder index
  notes/
    hello.md
    vx_assets/<note uuid>/image.png
```

```
<raw root>/
  a.md
  sub/b.md
```

### 6.2 Application files

| File | Location (Linux) | Owner |
|---|---|---|
| `vxcore.json` | `$XDG_DATA_HOME/marktux/` (default `~/.local/share/marktux/`) | `ConfigManager` |
| `vxsession.json` | `$XDG_CACHE_HOME/marktux/` (default `~/.cache/marktux/`) | `ConfigManager` / `WorkspaceManager` |
| `metadata.db` | `<local-data>/notebooks/<notebook uuid>/` | `SqliteMetadataStore` |

Windows uses `%APPDATA%` and `%LOCALAPPDATA%`; macOS uses
`~/Library/Application Support` and `~/Library/Caches`. The legacy flat
`~/.cache/marktux/meta.db` from earlier prototypes is **never opened**.

Unknown JSON keys survive a load/save round-trip (`extra` dicts), so files
written by newer versions are not silently stripped.

### 6.3 SQLite cache (schema v4)

Tables: `folders`, `files`, `tags`, `file_tags`, `notebook_metadata`,
`schema_version`.

* The database is a **derived cache**. `vx.json` and notebook config are the
  source of truth; `Notebook.rebuild_cache()` re-derives the structure.
* It is rebuilt when the structure tables are empty, or when `schema_version`
  is stale **or missing**. `notebook_metadata` (device-local `last_sync_utc`,
  history, `tags_synced_utc`) is preserved across rebuilds.
* One connection per notebook, `check_same_thread=False`, guarded by an
  `RLock`; transactions use `BEGIN IMMEDIATE`.
* WAL is requested and verified. If the filesystem refuses it (SMB/NFS), the
  store falls back to `journal_mode=DELETE` with `synchronous=FULL`.

### 6.4 Source-of-truth rules

| Fact | Truth | Mirror |
|---|---|---|
| Folder/file structure | `vx.json` | `folders` / `files` |
| File tags | `vx.json` `tags` | `file_tags` |
| Attachments | `vx.json` `attachments` | `files.attachments` |
| Tag definitions | `config.json` `tags` + `tagsModifiedUtc` | `tags` |
| Note text | the note file | in-memory `Buffer` |
| Open notebooks/buffers | `vxsession.json` | in-memory managers |
| Search index | none (re-scans indexed notes) | — |

## 7. Concurrency model

| Worker | Where | What runs there |
|---|---|---|
| GUI thread | Qt | Widgets, painting, signal delivery. Never disk scans, parses, decodes or searches. |
| `TaskRunner` pool | `core/concurrency.py` | Slow core calls submitted by `CoreBridge._submit`; default 3 workers; each task carries a `CancellationToken` |
| `BufferWatchdog` | daemon thread | Polls `buffers.check_external_changes()` every 1.5 s and emits state events |
| Render pool | `mtextedit.renderer` | Markdown parse/highlight (`QRunnable` on `QThreadPool`); generation token discards stale results |
| Image decode | render worker | Decoding happens off-thread; the GUI only wraps the finished `QImage` in a `QPixmap` |

Rules that keep this safe:

* Core objects take their own locks; callers do not hold a lock across a call
  into another manager.
* `Buffer.save()` snapshots bytes under the lock, writes **outside** the lock,
  and only clears `dirty` if the revision did not change during the write.
* Cancellation is cooperative: `CancellationToken.raise_if_cancelled()` at file
  boundaries; search also checks per file and per chunk.
* Events are delivered synchronously to listeners; a listener exception is
  logged and swallowed so it can never break a save.

## 8. Key flows

### Startup / session restore

1. `CoreContext().open()` loads `vxcore.json`, reopens the notebooks listed in
   `vxsession.json`, configures buffer encodings, then restores workspaces
   (unless `recoverLastSession` is false, in which case notebooks still open
   but buffers/workspaces are skipped).
2. `CoreBridge` is constructed and subscribes to core events; the tree model is
   seeded with the open notebooks.
3. `MainWindow._restore_open_buffer()` picks the current workspace's
   `current_buffer_id` (or its first buffer) and opens it.

### Open a notebook

`ctx.open_notebook(root)` → detect bundled vs raw → load/validate config →
open `metadata.db` (schema check/rebuild) → rebuild the structure if empty →
sync tags (bundled) → sweep stale temp files → record the notebook in the
session.

### Open a note

`ctx.buffers.open(notebook_id, rel)` returns a `Buffer` immediately; the file
is read lazily on the first `content()` call. The buffer tracks encoding, BOM,
line ending, revision, dirty flag and a state
(`NORMAL` / `FILE_CHANGED` / `FILE_MISSING` / `SAVE_FAILED`).

### Edit and save

Editor text → `buffer.set_content()` (revision++, dirty) → `buffer.save()`:
encode with the original encoding/BOM/EOL, write a `.vswp` backup, write the
file atomically (temp in the same directory → `fsync` → `os.replace`), then
clear dirty if no edit raced the write. A failed write leaves the original
untouched and sets `SAVE_FAILED`.

### Search

`ContentQuery` → `SearchManager.search_stream()` enumerates candidate files
from the index (scope/tag/exclude filters) → the selected backend (`rg --json`
when available, else the built-in Python backend) → decode with the configured
encodings → emit `SearchBatch`es of 64 files (including empty chunks, so the
UI can show progress) → `search.backend_fallback` if `rg` disappears mid-run.
Cancellation is checked per file and per chunk.

### Tags

Tag definitions live in the notebook config as a forest. `TagManager._commit()`
forces a strictly increasing `tagsModifiedUtc` and mirrors the forest into
SQLite. File tags are written to the note's `vx.json` and mirrored to
`file_tags`. Queries support AND/OR and per-tag counts.

### Assets

`AssetManager.insert()` writes to
`<note parent>/<assetsFolder>/<note uuid>/<name>`, deduplicating with `_1`,
`_2`… and returning a notebook-relative `/` path. All resolved paths are
checked for containment (and symlink crossings are rejected). Attachments are
assets whose basename is also recorded in the note's `vx.json`.

### Shutdown

`prepare_shutdown()` snapshots workspaces into `vxsession.json` exactly once;
`close()` skips a second snapshot, closes notebooks, then shuts the task pool
down. Window geometry is the app's business (`QSettings`) and is never written
into the session file.

## 9. Invariants

These are enforced by tests; do not weaken them without updating the docs.

1. **`core/` imports no Qt.** `tests/core/test_phase1.py` AST-scans every module.
2. **The cache is disposable.** Anything in `metadata.db` must be re-derivable
   from `vx.json`/config, except `notebook_metadata`.
3. **Every file write is atomic.** Config, notes, backups and cache-adjacent
   files go through `fsutil.atomic_write_bytes`.
4. **Paths stay inside the notebook.** Use `paths.resolve_inside(...)`; never
   join user input onto a root directly.
5. **The GUI never blocks on core I/O.** New slow calls go through
   `CoreBridge._submit()`.
6. **Cancellation is cooperative and checked often.** Long loops accept a
   `CancellationToken`.
7. **Events never break operations.** Listener failures are logged, not raised.
8. **Units and spellings match vxcore.** Millisecond timestamps, `/`-separated
   relative paths, camelCase payload keys, schema v4.

## 10. Extension points

* **New file type** — add an entry to `FileTypesConfig.defaults()` in
  `core/models.py`; `by_suffix`/`by_name` and the `Others` fallback do the rest.
* **New search backend** — implement `SearchBackend` in `core/search.py`
  (`search_file`, `available`), register it in the configured backend list;
  emit `search.backend_fallback` if it becomes unavailable.
* **New core capability** — add a manager module, construct it in
  `CoreContext`, expose it from `core/__init__.py`, add bridge methods in
  `ui/core_bridge.py` if the UI needs it, and write tests under `tests/core/`.
* **New UI panel** — subscribe to core events through `CoreBridge`; never call
  slow core methods on the GUI thread.
* **New editor feature** — implement it inside `libs/mtextedit` with its own
  tests and a decision entry in `libs/mtextedit/DESIGN.md`. The editor must stay
  notebook-agnostic; use host hooks (for example `set_image_paster`) instead of
  importing `core/`.

## 11. Testing

| Suite | Command | Notes |
|---|---|---|
| Core + UI | `python3 -m unittest discover -s tests -t .` | 160 tests; Qt tests run offscreen |
| Core only | `python3 -m unittest discover -s tests/core -t .` | No Qt import |
| UI only | `QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests/ui -t .` | `QCoreApplication` |
| Editor | `cd libs/mtextedit && python3 -m unittest discover -s tests -t .` | 10 tests |

`-t .` matters: without it, `tests/core/__init__.py` can shadow the real
`core` package during discovery.

Fixtures are built with the public model codecs (`tests/core/helpers.py`), so
tests exercise the same JSON that VNote writes. Hardening tests live in
`tests/core/test_hardening.py`.

## 12. Known gaps

Not implemented, in rough priority order:

* **UI surface**: no workspace switcher (workspaces exist in core and the
  current one is restored), no settings dialog, no recent-notebooks list, no
  multi-window support, no export/print.
* **Packaging**: the application has no `pyproject.toml` or installer; it runs
  from the repository (`python main.py`). `mtextedit` is packaged.
* **Editor roadmap**: KaTeX math preview, Obsidian-style inline rendering,
  richer code-block highlighting.
* **Platform coverage**: developed and tested on Linux; Windows/macOS paths are
  implemented but not exercised in CI.

Deliberately absent (see [§2](#2-goals-and-non-goals)): sync, encryption,
recycle bin, cross-notebook transfer, snippets, templates, mobile API.
