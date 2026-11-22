# AGENTS.md — working on marktux with an AI agent

This file is the shared instruction set for AI coding agents (and a quick
orientation for humans). Read it before changing code.

## Read first

1. [`ARCHITECTURE.md`](ARCHITECTURE.md) — what exists, how layers connect,
   invariants, extension points.
2. [`DESIGN.md`](DESIGN.md) — why the backend is shaped this way (C1–C13).
3. [`libs/mtextedit/DESIGN.md`](libs/mtextedit/DESIGN.md) — editor decisions
   (D1–D19).
4. [`CONTRIBUTING.md`](CONTRIBUTING.md) — setup, style, commit conventions.

If a task contradicts these documents, say so before implementing; update the
documents when a decision changes.

## Kickoff prompt

Paste this into a new agent session:

> You are contributing to marktux at `<repository path>`. Read `AGENTS.md`,
> `ARCHITECTURE.md`, `DESIGN.md` and `libs/mtextedit/DESIGN.md` before changing
> anything. Establish a baseline by running both suites:
>
> ```bash
> python3 -m unittest discover -s tests -t .
> (cd libs/mtextedit && python3 -m unittest discover -s tests -t .)
> ```
>
> Follow the non-negotiable rules in `AGENTS.md`: `src/marktux/core/` stays Qt-free,
> `vx.json` is the source of truth, writes are atomic, paths are contained, and
> slow work stays off the GUI thread. Do not edit `external/vnote/` or the
> `libs/*` submodules unless the task requires it. Before reporting success,
> rerun both suites, update the docs the change affects, and quote the real
> command output. Ask before pushing or performing destructive or shared-state
> actions.

For a specific task, append:

> Task: `<what to build or fix>`. Scope: `<files or areas>`. Acceptance:
> `<observable behaviour and tests>`. Out of scope: `<what not to touch>`.

## Repository map

```
main.py                     thin wrapper for `python main.py`
pyproject.toml              setuptools + src layout
src/marktux/app.py          composition root (QMainWindow + wiring)
src/marktux/core/           Qt-free backend — stdlib only
src/marktux/ui/core_bridge.py  Qt adapter: off-thread calls -> signals
src/marktux/ui/tree_model.py   lazy notebook tree model
libs/mtextedit/             standalone editor (git submodule, src/mtextedit)
tests/core/                 headless backend tests + fixtures
tests/ui/                   offscreen Qt adapter tests
external/vnote/             read-only upstream reference, git-ignored
```

## Commands

```bash
# one-time setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
git submodule update --init --recursive
pip install -e libs/mtextedit --no-deps
pip install -e . --no-deps

# full verification (run before claiming success)
python3 -m unittest discover -s tests -t .
(cd libs/mtextedit && python3 -m unittest discover -s tests -t .)

# focused
python3 -m unittest tests.core.test_buffer -v
QT_QPA_PLATFORM=offscreen python3 -m unittest tests.ui.test_bridge -v

# smoke-test the app without a display (it will block; use a timeout)
timeout 5 env QT_QPA_PLATFORM=offscreen python3 main.py
```

Run discovery from the repository root with `-t .`; `tests/__init__.py`
adds `src` and `libs/mtextedit/src` to the path for source checkouts.

## Non-negotiable rules

1. **`src/marktux/core/` imports no Qt.** Not even inside `try:` or `TYPE_CHECKING`. A test
   AST-scans the package.
2. **`vx.json` / notebook config are the source of truth.** SQLite
   (`metadata.db`) is a disposable cache; anything you store there must be
   re-derivable, except `notebook_metadata`.
3. **Every file write is atomic** via `src/marktux/core/fsutil.atomic_write_bytes()`
   (temp file in the destination directory, `fsync`, `os.replace`).
4. **Contain every path.** Use `src/marktux/core/paths.py` (`resolve_inside`,
   `normalize_rel`, `validate_name`); never join untrusted input onto a root.
5. **The GUI thread never blocks on I/O.** Add slow operations to
   `CoreBridge._submit()` and return via a signal.
6. **Cancellation is cooperative.** Long loops take a `CancellationToken` and
   call `raise_if_cancelled()` at file/chunk boundaries.
7. **Raise `src/marktux/core/errors.py` types**, not `Exception`; export new
   public API from `src/marktux/core/__init__.py`.
8. **Preserve on-disk compatibility.** Millisecond timestamps, `/`-separated
   relative paths, camelCase event payloads, schema v4, unknown JSON keys in
   `extra`.

## Where changes belong

| Task | Location |
|---|---|
| JSON shapes, dataclasses, enums | `src/marktux/core/models.py` |
| Notebook open/close, cache rebuild, registry | `src/marktux/core/notebook.py` |
| Folder/file CRUD, `vx.json` handling, attachments | `src/marktux/core/folder.py` |
| Path algebra and containment | `src/marktux/core/paths.py` |
| Atomic writes, encodings, ids, clocks | `src/marktux/core/fsutil.py` |
| SQLite schema and queries | `src/marktux/core/metadata.py` |
| Buffers, save/dirty, external change | `src/marktux/core/buffer.py` |
| Tags | `src/marktux/core/tags.py` |
| Search backends and streaming | `src/marktux/core/search.py` |
| Workspaces and `vxsession.json` | `src/marktux/core/workspace.py` |
| Assets | `src/marktux/core/assets.py` |
| Async UI calls | `src/marktux/ui/core_bridge.py` |
| Tree behaviour | `src/marktux/ui/tree_model.py` |
| Window, menus, docks | `src/marktux/app.py` |
| Editor, preview, outline, find/replace | `libs/mtextedit/` |

## Workflow

1. **Explore before editing.** Read the module and its tests; do not propose
   changes to code you have not read.
2. **State the plan** for multi-file changes, then implement.
3. **Test the invariant, not the implementation.** For concurrency or
   crash-safety work, assert the user-visible guarantee (no torn file, no temp
   files, metadata preserved).
4. **Run both suites.** If a test fails, fix the cause; never skip or delete a
   failing test to get green.
5. **Update documentation** when behaviour, public API, or a design decision
   changes: `ARCHITECTURE.md` for structure/flows, `DESIGN.md` for a new
   decision, `README.md` for user-visible status.
6. **Report evidence.** Quote the actual command output and test counts; do not
   claim a run you did not perform.

## Submodules and references

* `libs/mtextedit` is a separate repository. Editor changes must be committed
  there first, then the pointer bumped here (`git add libs/mtextedit`). Do not
  fold editor edits into an app commit silently.
* `libs/m3` is a reference fork for mind maps; the app does not import it.
  Do not modify it for app features.
* `external/vnote/` is an upstream checkout for reading. Never edit it, never
  add it to git (it is ignored).

## Do not

* Add a dependency without updating `requirements.txt` (and
  `libs/mtextedit/pyproject.toml` for editor-only dependencies).
* Move backend logic into `src/marktux/ui/` or `src/marktux/app.py` for
  convenience.
* Write window geometry or UI state into `vxsession.json`; that file is owned by
  core. Use `QSettings` for app preferences.
* Commit generated files (`__pycache__`, `.venv`, build output) or user
  notebooks.
* Run the GUI interactively in a loop; use the offscreen smoke test.
* Rewrite unrelated code while fixing a bug.

## Definition of done

* The change is implemented in the correct layer.
* New behaviour has a test (or an existing test was extended).
* `python3 -m unittest discover -s tests -t .` passes.
* `(cd libs/mtextedit && python3 -m unittest discover -s tests -t .)` passes
  when the editor was touched.
* Public API is exported and documented.
* `ARCHITECTURE.md` / `DESIGN.md` / `README.md` reflect the change.
* The final report lists what changed, what was run, and any remaining gap.
