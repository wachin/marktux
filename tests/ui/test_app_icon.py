"""The packaged MarkTux logo must be usable as the application icon."""

from __future__ import annotations

import unittest

from PyQt6.QtWidgets import QApplication

from marktux.app import app_icon


class AppIconTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_bundled_icon_loads(self):
        icon = app_icon()
        self.assertFalse(icon.isNull())
        self.assertFalse(icon.pixmap(32, 32).isNull())

    def test_missing_icon_returns_an_empty_icon(self):
        # The helper must not crash when package data is absent.
        import marktux.app as app_module

        original = app_module.Path
        try:
            app_module.Path = lambda *args, **kwargs: original("/nonexistent")
            self.assertTrue(app_module.app_icon().isNull())
        finally:
            app_module.Path = original


if __name__ == "__main__":
    unittest.main()
