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

The cmark sources are vendored as a git submodule at
`libs/mtextedit/libs/cmarkgfm/libs/cmark`; its `COPYING` file ships with
the submodule, and the binding's `LICENSE.txt` ships with `cmarkgfm`.

## Reference only

`external/vnote/` is a checkout of VNote (**LGPL-3.0**) kept for research and
comparison. No VNote source code is copied into this repository.

One data file is bundled with attribution: `libs/mtextedit/mtextedit/themes/native.theme`
is VNote's **Native** editor theme, copied unmodified from
`src/data/extra/themes/native/text-editor.theme` and redistributed under
**LGPL-3.0** (copyright the VNote authors). `mtextedit` itself remains MIT; that
file keeps its own license. The theme loader is original code.

## Colour palette

`libs/mtextedit/mtextedit/code_style.py` uses colour values from
`github-light.theme` (`SPDX-FileCopyrightText: 2020 GitHub Inc.`,
`SPDX-License-Identifier: MIT`). The values came from the KSyntaxHighlighting
fork that was removed; only the palette was kept.
