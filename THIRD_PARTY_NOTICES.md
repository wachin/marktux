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

## VNote assets

These assets were copied from VNote at commit
`c966a995044f975d92a59453ec46738bce0e8282`:

| Local path | Upstream path |
|---|---|
| `src/marktux/core/icons/` | `src/data/core/icons/` |
| `src/marktux/core/logo/` | `src/data/core/logo/` |
| `src/marktux/core/translations/` | `src/data/core/translations/` |

They are redistributed with attribution:

- **VNote**, its logo and its translation catalogs: **LGPL-3.0**, copyright
  the VNote authors.
- **Icons derived from Lucide**: **ISC**, copyright Lucide contributors (and
  Feather contributors for the inherited icons). Most files in `icons/` fall
  into this group; VNote's per-icon mapping is linked from
  [`src/marktux/core/icons/README.md`](src/marktux/core/icons/README.md).

The full license texts and per-directory provenance notes ship with the assets
in [`src/marktux/core/LICENSES/`](src/marktux/core/LICENSES/). The files are
copied unmodified; MarkTux does not claim authorship of them, and the VNote
name/logo does not imply endorsement.

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
