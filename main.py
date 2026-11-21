"""Source-tree wrapper for ``python main.py``.

The application lives in ``src/marktux``. Installing the project
(``pip install -e .``) is the normal way to run it; this wrapper keeps the
historical command working from a checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from marktux.app import main

if __name__ == "__main__":
    main()
