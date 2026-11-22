# marktux

A lightweight, **async-first** Markdown notebook application built with PyQt6.
It is a from-scratch, performance-focused rethinking of VNote's ideas: speed
comes from the *architecture* (threaded, coalesced rendering; lazy notebook
tree; off-thread search) rather than the programming language.

It opens existing VNote notebooks unchanged — same `vx_notebook/` layout, same
`vx.json` files, same session format.

## Documentation

| Document | What it covers |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | What exists, layer map, module reference, on-disk formats, threading, key flows, invariants, extension points |
| [`DESIGN.md`](DESIGN.md) | Backend decision record (C1–C13): why the core is a Python port, formats, Qt-free core, tags, search, workspaces, assets, hardening, opening files |
| [`libs/mtextedit/DESIGN.md`](libs/mtextedit/DESIGN.md) | Editor decision record (D1–D19): single-pane preview, async rendering, image decoding |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Setup, test loop, style, submodules, PR guidance |
| [`AGENTS.md`](AGENTS.md) | Operating instructions for AI coding agents (and a quick orientation for humans) |

## Quick start

Requirements: Python **3.10+**; a desktop session to use the GUI (tests run
headless). [`ripgrep`](https://github.com/BurntSushi/ripgrep) is optional and
enables the fast search backend.

```bash
git clone --recurse-submodules https://github.com/wachin/marktux
cd marktux
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e libs/mtextedit --no-deps   # editor submodule
pip install -e . --no-deps                # the marktux package

python main.py                     # wrapper from the source tree
python -m marktux                  # installed package
python main.py /path/to/notebook   # open a notebook directory
```

**File → Open Markdown File…** (`Ctrl+O`) opens a single note. If the file is
inside an already-open notebook that notebook is reused; otherwise its folder
opens as a raw notebook, so the note gets a real tree entry and core's atomic
save rather than being edited outside the backend. The last opened file is
remembered in `QSettings` (`~/.config/marktux/marktux.conf`) and reopened at
startup; if it no longer exists, the remembered path is dropped.

Editor component on its own:

```bash
cd libs/mtextedit && python examples/demo.py
```

## Layout

```
marktux/
  main.py                # thin wrapper for `python main.py`
  pyproject.toml         # setuptools + src layout
  src/marktux/
    __init__.py
    __main__.py          # `python -m marktux`
    app.py               # composition root: window, menus, docks, wiring
    core/                # Qt-free Python port of vxcore (stdlib only)
      icons/             # VNote UI action icons (LGPL-3.0 / ISC)
      logo/              # MarkTux logo: SVG master + generated PNG/ICO/ICNS
      translations/      # reserved for MarkTux catalogs
      LICENSES/          # license texts for the bundled icons
      context.py         # CoreContext facade + lifecycle
      notebook.py        # bundled/raw notebooks, registry, cache rebuild
      folder.py          # folder tree (vx.json for bundled, disk for raw)
      metadata.py        # per-notebook SQLite cache (vxcore schema v4, WAL)
      buffer.py          # lazy buffers, save/dirty/external-change, backups
      tags.py search.py workspace.py assets.py
      config.py events.py concurrency.py models.py paths.py fsutil.py errors.py
    ui/                  # thin Qt adapter over the Qt-free core
      core_bridge.py     # CoreBridge(QObject): background calls -> Qt signals
      tree_model.py      # NotebookNodeModel: lazy canFetchMore/fetchMore tree
  tests/
    core/                # headless core tests + fixture builders
    ui/                  # offscreen Qt adapter tests
  libs/
    mtextedit/            # reusable Markdown editor (git submodule)
      src/mtextedit/
        editor.py         # MTextEdit -- QTextEdit surface with in-place preview
        inplace.py        # code/table/image previews above their source block
        layout.py         # custom QAbstractTextDocumentLayout (default)
        highlighter.py    # fallback: block regions + inline highlighting
        renderer/         # RenderWorker, RenderManager (debounce+coalesce+cancel)
        outline.py        # AST-derived outline + click-to-navigate
        find_replace.py   # in-file find & replace
        linguistic/       # optional spell-check via pyqt6-linguistic-tools
      examples/demo.py    # standalone editor demo
    m3/ QHotkey/ vxcore/ # reference forks; not imported by the application
  external/vnote/        # read-only upstream reference checkout (git-ignored)
```

`mtextedit` is standalone and reusable — embed it as the Markdown note panel
in any other PyQt6 project (e.g. the fourth panel of an e-Sword-like tool).
It has no dependency on `src/marktux/core/`.

## Why it is fast (vs VNote)

* The Markdown parse (VNote's cmark, via `cmarkgfm`) runs on a `QThreadPool`
  worker, never the GUI thread.
* `RenderManager` debounces and **coalesces** edits and uses a generation token
  so only the newest render produces UI output; stale in-flight parses abort.
* The editor is a **single pane** with VNote-style **in-place preview**: code
  blocks, tables and images render above their source block (a reserved band),
  instead of a second pane or a `QWebEngineView` reload per keystroke.
* The notebook tree is **lazy** (`canFetchMore`/`fetchMore`) — opening a big
  notebook does not materialise the whole hierarchy.
* Notebook structure is read one level at a time; `src/marktux/core/` keeps its own pool of
  daemon threads so a slow disk or search never blocks the GUI thread.
* Content search streams results in index-order batches over the notebook's
  `vx.json` index (like VNote, there is no content index to go stale). A
  `ripgrep` backend is used when available, with an automatic fallback to a
  built-in Python backend; searches run off-thread and can be cancelled.

## Design decisions

The editor's decisions (single-pane in-place preview, `QTextEdit`, band
reservation via block margins, preview scope, async renderer, loop guard) are
recorded in [`libs/mtextedit/DESIGN.md`](libs/mtextedit/DESIGN.md). App-level
decisions are in [`DESIGN.md`](DESIGN.md); the short version:

* **`vxcore` is reimplemented in Python, not wrapped.** `src/marktux/core/` is a Qt-free
  port of vxcore's *contract*: bundled notebooks (`vx_notebook/config.json`,
  `contents/**/vx.json`) and raw plain-directory notebooks, a per-notebook
  SQLite cache (vxcore schema v4, WAL), and background tasks with cancellation.
  Sync/encryption/recycle-bin are explicit non-goals.
* **Qt-free core + thin adapter.** `src/marktux/core/` imports no PyQt6 and is fully
  headless-testable; `src/marktux/ui/` turns core calls and events into Qt signals. The two
  evolve independently, and `src/marktux/core/` is reusable in other projects.
* **Lazy notebook tree.** `marktux.ui.NotebookNodeModel` loads folders on demand, so
  opening a large notebook does not materialise the whole hierarchy.
* **Single-pane editor.** No side-by-side preview pane; rendered code/tables/
  images appear above their source inside the editor.
* **`m3` and `QHotkey` are out of scope** for the editor; app shortcuts use
  PyQt6 `QShortcut`, and OS-wide hotkeys would use `pynput` only if needed.
* **Optional, not default:** `QWebEngineView` preview and the linguistic
  toolkit's heavy engines are loaded only on demand.

## Development

Runtime: Python 3.10+, PyQt6, Pygments and the pinned `cmarkgfm` fork. The
cmark parser is C: a source install needs a C compiler, Python headers and
`cffi`, which build `_cmark.abi3.so` (CI produces wheels).

Optional runtime extras:

- `libs/mtextedit[math]` — matplotlib; renders inline and block formulas. Without
  it the TeX source stays visible.
- `libs/mtextedit[webengine]` / `[linguistic]` — Chromium HTML preview and the
  spell-check/thesaurus service.
- `ripgrep` — faster content-search backend.

Development and test tools:

- Python `unittest` for both suites (no pytest needed), run with
  `QT_QPA_PLATFORM=offscreen`.
- `ruff` for lint.
- `git` plus the `libs/mtextedit`, `cmarkgfm` and `cmark` submodules.
- **Orca** (GNOME screen reader) and **Accerciser** (AT-SPI inspector) for the
  manual accessibility checks. They are development-only and are exercised
  through **DeepSeek Harness**, not CI. Install with
  `sudo apt install orca accerciser` and enable toolkit accessibility; the
  step-by-step checklist is in [`ACCESSIBILITY.md`](ACCESSIBILITY.md).
- `matplotlib` is optional: the math tests skip when it is absent.

## Tests

```bash
# core + UI adapter (headless Qt)
python3 -m unittest discover -s tests -t .

# editor component
(cd libs/mtextedit && python3 -m unittest discover -s tests -t .)
```

Focused runs and the `-t .` gotcha are covered in
[`CONTRIBUTING.md`](CONTRIBUTING.md#5-test-loop). CI runs both suites on Python
3.10 and 3.13 (`.github/workflows/tests.yml`).

## Status

Implemented and running headless:

* **Editor** (`mtextedit`): single-pane in-place preview (code/tables/images),
  async renderer with debounce/coalesce/cancel, token-coloured code blocks
  (Pygments, computed off-thread), off-thread image decode with a
  configurable LRU cache, AST outline, find/replace.
* **Core port** (phases 1–8): Qt-free `vxcore`-compatible backend — bundled +
  raw notebooks, `vx.json` tree, per-notebook SQLite cache (schema v4, rebuilt
  on open), lazy browsing, missing-node detection, session persistence,
  background tasks; buffers (lazy load, dirty/save, atomic writes, `.vswp`
  backups, external-change detection); folder/file CRUD with cache write-through
  and read-only enforcement; tag forest (nested tags, stamp-driven store sync,
  AND/OR queries, counts, delete cascade); content search (ripgrep + built-in
  backends, streaming batches, scope filters, UTF-8/GB18030, cancellation);
  workspaces + session restore (preview filtering, stable buffer ids,
  `fileId` rename recovery, write-once shutdown); assets/attachments (per-note
  `vx_assets/<uuid>/` folders, `_1` dedupe, path-containment checks) and
  file-type lookup. Hardened: concurrent saves stay atomic, a stale or missing
  `schema_version` rebuilds the cache without losing device-local metadata,
  crashed-write temp files are swept, and WAL-less filesystems fall back to
  `DELETE` + `FULL` sync.
* **UI adapter**: `CoreBridge` + lazy `NotebookNodeModel`; notebook tree
  context menu (new note/folder, rename, duplicate, move, delete, tag); tag dock
  (create/delete/find notes); off-thread search with live result list and cancel;
  restored notebooks and the current buffer reopen on startup; pasting an image
  stores it as a note asset and inserts a relative Markdown link; opening a
  Markdown file outside a notebook makes its folder a raw notebook (or reuses
  the notebook that already contains it); `src/marktux/core/` has
  zero Qt imports (enforced by a test).
* The bundled MarkTux logo is applied to `QApplication` and `MainWindow`
  (`src/marktux/app.py:app_icon`).
* Linguistic download manager + spell-check.

Not yet implemented:

* Workspace switching in the UI (workspaces exist in core; the current one is
  restored), settings dialog, recent-notebooks list, multi-window, export/print.
* Application packaging/installer — there is no generated `.deb`,
  AppImage or platform installer yet; run from a source checkout or an
  editable install (`pip install -e .`).
* Editor roadmap: KaTeX math preview, Obsidian-style inline rendering.
* Windows/macOS are implemented but not exercised in CI (Linux is).

See [`ARCHITECTURE.md` §12](ARCHITECTURE.md#12-known-gaps) for details and
[`DESIGN.md`](DESIGN.md) for the non-goals (sync, encryption, recycle bin,
cross-notebook transfer, snippets, templates, mobile API).

## Spelling / thesaurus (incl. Windows)

`Tools → Dictionary Download Manager…` opens the verified download manager from
`libs/mtextedit/libs/pyqt6-linguistic-tools` (57-entry catalog, SHA-256 gated,
installs into a managed provider off the GUI thread — the supported path on
Windows/macOS where system Hunspell/MyThes packages don't exist).
`Tools → Spell Check` attaches the toolkit's spell-check decorator to the editor.
Both are wired through `mtextedit.linguistic`, which lazily bootstraps the
vendored toolkit (or honours `MARKTUX_LINGUISTIC_TOOLKIT`).

## Contributing

Contributions are welcome. Start with [`CONTRIBUTING.md`](CONTRIBUTING.md);
if you work with an AI agent, point it at [`AGENTS.md`](AGENTS.md).

## Acknowledgements

The UI action icons under `src/marktux/core/icons/` are reused from
[VNote](https://github.com/vnotex/vnote) (LGPL-3.0); most are Lucide-derived
(ISC). See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and
[`src/marktux/core/icons/README.md`](src/marktux/core/icons/README.md).
The MarkTux logo and translations are MarkTux's own.

## License

marktux is released under the **GNU General Public License v3.0**; see
[`LICENSE`](LICENSE). The reusable editor component in
[`libs/mtextedit`](libs/mtextedit) is MIT-licensed (see its `pyproject.toml`) so
it can be embedded in other projects.
