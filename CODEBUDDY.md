# CODEBUDDY.md

Project instructions for CodeBuddy Code. The full agent guide is
[`AGENTS.md`](AGENTS.md); this file is the short version that must not be
missed.

## Project

marktux — a PyQt6 Markdown notebook. `core/` is a Qt-free Python port of
VNote's `vxcore`; `ui/` adapts it to Qt; `libs/mtextedit` is a standalone
editor package (git submodule); `main.py` wires the window.

Read [`ARCHITECTURE.md`](ARCHITECTURE.md) before structural work and
[`DESIGN.md`](DESIGN.md) before changing backend behaviour.

## Commands

```bash
python3 -m unittest discover -s tests -t .            # core + UI (160 tests)
(cd libs/mtextedit && python3 -m unittest discover -s tests -t .)   # editor (10)
```

Always use `-t .` from the repository root; otherwise `tests/core/__init__.py`
shadows the real `core` package. Qt tests run with
`QT_QPA_PLATFORM=offscreen`.

## Hard rules

1. `core/` must not import PyQt6 — a test AST-scans for it.
2. `vx.json` and notebook config are the source of truth; `metadata.db` is a
   disposable cache.
3. All file writes go through `core/fsutil.atomic_write_bytes()`.
4. All user/config paths go through `core/paths.py` containment helpers.
5. Slow work goes through `CoreBridge._submit()`; never block the GUI thread.
6. Raise `core.errors` types; export new public API from `core/__init__.py`.
7. Preserve on-disk compatibility: ms timestamps, `/` paths, camelCase event
   payloads, schema v4.
8. `libs/mtextedit` is a separate repository: commit editor changes there and
   bump the submodule pointer. Never edit `external/vnote/`.

## Before reporting success

Run both suites and quote the real output. Update `ARCHITECTURE.md`,
`DESIGN.md` or `README.md` when behaviour, API or a decision changes.
