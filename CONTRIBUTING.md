# Contributing to marktux

Thanks for helping. This guide covers setup, the test loop, and the conventions
that keep the codebase coherent. For the system overview read
[`ARCHITECTURE.md`](ARCHITECTURE.md); for the rationale behind the backend read
[`DESIGN.md`](DESIGN.md).

## 1. Prerequisites

* Python **3.10+** (the code uses `X | None`, `dataclass(slots=True)`)
* A C-free Qt runtime: PyQt6 wheels from PyPI
* Optional: [`ripgrep`](https://github.com/BurntSushi/ripgrep) (`rg`) on `PATH`
  for the fast search backend — the built-in Python backend is used otherwise
* Optional: a desktop session for running the GUI; tests run headless

## 2. Get the code

```bash
git clone --recurse-submodules https://github.com/wachin/marktux
cd marktux
```

Already cloned without submodules?

```bash
git submodule update --init --recursive
```

`libs/mtextedit` is required. `libs/QHotkey`, `libs/m3` and `libs/vxcore` are
reference forks and are not imported by the application; `external/vnote/` is a
read-only reference checkout and is git-ignored.

## 3. Set up a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e libs/mtextedit --no-deps
pip install -e . --no-deps
```

`requirements.txt` installs the three runtime dependencies (`PyQt6`,
`cmarkgfm` (VNote's cmark) and `pygments`). The editor component is used directly
from
`libs/mtextedit` through `src/marktux/app.py` when running from a checkout.
Install it editable for packaging and for the `python -m marktux` entry point:

```bash
pip install -e libs/mtextedit
# optional extras
cd libs/mtextedit && pip install -e ".[webengine,linguistic]"
```

## 4. Run

```bash
python main.py                      # source-tree wrapper
python -m marktux                   # installed package
python main.py /path/to/notebook    # open a notebook directory
QT_QPA_PLATFORM=offscreen python main.py   # headless smoke test

cd libs/mtextedit && python examples/demo.py   # editor only
```

## 5. Test loop

Run the whole suite before every commit:

```bash
# core + UI adapter
python3 -m unittest discover -s tests -t .

# editor component
cd libs/mtextedit && python3 -m unittest discover -s tests -t .
```

Focused runs:

```bash
python3 -m unittest tests.core.test_buffer -v
python3 -m unittest tests.core.test_hardening.ConcurrentSaveTest -v
QT_QPA_PLATFORM=offscreen python3 -m unittest tests.ui.test_bridge -v
```

Two gotchas:

* **Always pass `-t .`** when discovering from the repository root;
  `tests/__init__.py` adds `src` and `libs/mtextedit/src` for source checkouts.
* Qt tests need `QT_QPA_PLATFORM=offscreen` on machines without a display.

## 6. Repository conventions

**Python**

* `from __future__ import annotations` at the top of every module.
* Type hints on public functions and methods; use built-in generics
  (`list[str]`, `dict[str, Any]`) and `X | None`.
* Dataclasses for data (`src/marktux/core/models.py`), plain classes for behaviour.
* Line length ~88; no formatter is enforced, match the surrounding code.
* Imports: standard library, then third-party, then local — separated by blank
  lines.

**Architecture**

* `src/marktux/core/` must import **no Qt**. A test enforces this; never add a PyQt6 import
  there, not even behind `try:`.
* Slow work goes through `CoreBridge._submit()`; the GUI thread never scans
  disks, parses Markdown, decodes images or searches.
* `vx.json` and notebook config are the source of truth; `metadata.db` is a
  derived cache and must be re-derivable.
* All file writes go through `fsutil.atomic_write_bytes()`.
* All paths that come from users or config go through `src/marktux/core/paths.py`
  (`resolve_inside`, `normalize_rel`); never concatenate onto a root.
* Raise the specific exception from `src/marktux/core/errors.py`, not `Exception`.
* New public names must be exported from `src/marktux/core/__init__.py`.

**Events**

* Use the constants in `src/marktux/core/events.py`; payload keys stay camelCase to match
  vxcore (`filePath`, `bufferId`, `notebookId`).
* Listeners must tolerate exceptions: the bus logs and swallows them.

**Tests**

* `unittest`, not pytest — the suite must run with a bare Python install.
* Put core tests in `tests/core/test_<area>.py`, UI tests in `tests/ui/`,
  editor tests in `libs/mtextedit/tests/`.
* Build fixtures with `tests/core/helpers.py` so tests read/write the same JSON
  VNote does.
* Prefer behavioural assertions over implementation details. For concurrency,
  assert the invariant (no torn file, no temp files), not a particular
  interleaving.

**Comments and docs**

* Comments explain *why*, not *what*.
* A non-obvious design choice gets an entry in `DESIGN.md` (core) or
  `libs/mtextedit/DESIGN.md` (editor) in the existing
  Decision / Why / Consequences style.

## 7. Adding a feature

1. **Locate the layer.** Data/format → `src/marktux/core/models.py`; notebook lifecycle →
   `src/marktux/core/notebook.py`; paths → `src/marktux/core/paths.py`; UI-only behaviour → `src/marktux/ui/` or
   `src/marktux/app.py`; editor behaviour → `libs/mtextedit`.
2. **Write the test** (or extend an existing one) under the matching suite.
3. **Implement** the smallest change that satisfies it.
4. **Export** new public API from `src/marktux/core/__init__.py`.
5. **Bridge it** in `src/marktux/ui/core_bridge.py` if the UI needs it, as an off-thread
   call with a signal.
6. **Run both suites** and, for UI changes, the app headless.
7. **Update docs**: `ARCHITECTURE.md` (module/flow), `DESIGN.md` (a decision if
   the choice is non-obvious), and `README.md` status if a user-visible
   capability changed.

## 8. Working with submodules

`libs/mtextedit`, `libs/QHotkey`, `libs/m3` and `libs/vxcore` are separate
repositories. Changes to the editor must be committed **in `libs/mtextedit`
first**, pushed there, and then the submodule pointer is bumped in this
repository:

```bash
cd libs/mtextedit
git switch -c my-editor-change
# edit, test, commit, push
cd ../..
git add libs/mtextedit
git commit -m "Update mtextedit for ..."
```

Do not edit `external/vnote/`; it is an upstream reference checkout and is
ignored by git.

## 9. Commits and pull requests

* Imperative, scoped subject: `core: reject empty tag names`,
  `mtextedit: cancel stale image decodes`.
* The body explains *why* and lists the commands you ran.
* Keep unrelated refactors out of a bug-fix commit.
* A PR should state: what changed, how it was tested, and any user-visible
  behaviour change. Include a screenshot for UI changes.
* Never commit generated files (`__pycache__`, `.venv`, build output) or a
  user's notebooks.

## 10. Reporting bugs

Include the platform, Python and PyQt versions, the exact command, the
traceback, and — if it involves a notebook — the smallest notebook layout that
reproduces it (the `vx.json` files plus the note). Do not attach private notes;
reduce them first.

## 11. License

The application is licensed under the **GNU General Public License v3.0**; see
[`LICENSE`](LICENSE). By contributing you agree that your changes are licensed
under the same terms.

`libs/mtextedit` is a separate repository and remains MIT-licensed so it can be
embedded in other projects; editor contributions follow that repository's
license.
