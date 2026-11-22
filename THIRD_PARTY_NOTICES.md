# Third-party notices

marktux is licensed under **GPL-3.0-or-later** (see `LICENSE`). The reusable
editor package in `libs/mtextedit` is **MIT** unless a file says otherwise.

## Runtime dependencies

| Component | License | Copyright / source |
|---|---|---|
| PyQt6 / Qt 6 | GPL-3.0 or LGPL-3.0 | Riverbank Computing / The Qt Company |
| Pygments | BSD-2-Clause | Georg Brandl and contributors |
| cmark (`wachin/cmark`) | BSD-2-Clause | John MacFarlane; our fork of VNote's fork, pinned at `6148f31` |
| cmarkgfm | MIT | theacodes (A. Jesse Jiryu Davis); fork at `github.com/wachin/cmarkgfm` |
| ripgrep (optional search backend) | MIT / Unlicense | Andrew Gallant |
| matplotlib (optional `mtextedit[math]`) | Matplotlib License (BSD-compatible) | Matplotlib Development Team |

The cmark sources are vendored as a git submodule at
`libs/mtextedit/libs/cmarkgfm/libs/cmark`; its `COPYING` file ships with
the submodule, and the binding's `LICENSE.txt` ships with `cmarkgfm`.

## VNote UI icons

The UI action icons in `src/marktux/core/icons/` were copied from VNote's
`src/data/core/icons/` at commit
`c966a995044f975d92a59453ec46738bce0e8282`. They are redistributed with
attribution:

- **VNote-specific icons**: **LGPL-3.0**, copyright the VNote authors.
- **Icons derived from Lucide**: **ISC**, copyright Lucide contributors (and
  Feather contributors for the inherited icons). Most files fall into this
  group; VNote's per-icon mapping is linked from
  [`src/marktux/core/icons/README.md`](src/marktux/core/icons/README.md).

The full license texts ship in
[`src/marktux/core/LICENSES/`](src/marktux/core/LICENSES/). The SVG files are
copied unmodified; MarkTux does not claim authorship of them.

VNote's **application** icons (`vnote.icns`, `vnote.ico`, `vnote.rc`), its
**logo**, and its **translation catalogs** are intentionally not bundled.
MarkTux uses its own logo in `src/marktux/core/logo/` and reserves
`src/marktux/core/translations/` for its own catalogs.

`external/vnote/` remains a read-only checkout (**LGPL-3.0**) kept for research
and comparison. No VNote source code is copied into MarkTux.

One editor data file is bundled with attribution:
`libs/mtextedit/src/mtextedit/themes/native.theme` is VNote's **Native** editor
theme, copied unmodified from
`src/data/extra/themes/native/text-editor.theme` and redistributed under
**LGPL-3.0** (copyright the VNote authors). `mtextedit` itself remains MIT; that
file keeps its own license. The theme loader is original code.

## Colour palette

`libs/mtextedit/mtextedit/code_style.py` uses colour values from
`github-light.theme` (`SPDX-FileCopyrightText: 2020 GitHub Inc.`,
`SPDX-License-Identifier: MIT`). The values came from the KSyntaxHighlighting
fork that was removed; only the palette was kept.
