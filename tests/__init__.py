"""marktux test suite."""

import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
for _path in (_ROOT / "src", _ROOT / "libs" / "mtextedit" / "src"):
    if _path.is_dir() and str(_path) not in _sys.path:
        _sys.path.insert(0, str(_path))
