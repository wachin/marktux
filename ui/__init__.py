"""Qt layer for marktux.

The ``core`` package is deliberately Qt-free; everything in this package is a
thin adapter that turns core calls/events into Qt signals for ``main.py``.

``core_bridge``  CoreBridge(QObject) — background submission + signals + teardown
``tree_model``   NotebookNodeModel(QAbstractItemModel) — lazy notebook tree
"""

from .core_bridge import CoreBridge
from .tree_model import NotebookNodeModel

__all__ = ["CoreBridge", "NotebookNodeModel"]
