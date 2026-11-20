"""Tests for ``File -> Open Markdown File…`` (main.py, offscreen).

The window is exercised in a child process on purpose. ``QMainWindow`` needs a
``QApplication``, while ``test_bridge.py`` creates a ``QCoreApplication`` first;
the two cannot coexist in one interpreter. Running the child lets this test own
the Qt application type, forces ``QT_QPA_PLATFORM=offscreen`` for itself, and
keeps a headless run from aborting the whole suite if no platform plugin loads.

The assertions are user-visible guarantees, not implementation details:

* the File menu offers the action;
* a Markdown file outside every notebook opens, and its folder becomes a *raw*
  notebook (the file is editable through core's normal save path);
* a file already inside an open bundled notebook reuses that notebook instead of
  creating a second, conflicting view of the same files;
* a path that is not a file leaves the editor content untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from ..core.helpers import make_bundled

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DRIVER = r'''
import json, os, sys, time
root, app_dir, local_dir, settings_path = (sys.argv[1], sys.argv[2],
                                           sys.argv[3], sys.argv[4])
raw_file, bundled_file, bundled_root, missing = sys.argv[5:9]
sys.path.insert(0, root)

from PyQt6.QtCore import QEventLoop, QSettings, QTimer
from PyQt6.QtWidgets import QApplication
import main as app_main
from core import CoreContext

def pump(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()

def wait_for(predicate, timeout_ms=15000):
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if predicate():
            return True
        pump(50)
    return False

ctx = CoreContext(app_data_dir=app_dir, local_data_dir=local_dir,
                  recover_session=False).open()
app = QApplication([])
settings = QSettings(settings_path, QSettings.Format.IniFormat)
win = app_main.MainWindow(ctx=ctx, settings=settings)

out = {"menu": [a.text() for a in win.menuBar().actions()[0].menu().actions()]}

# 1) A file outside every notebook: its folder opens as a raw notebook.
win.open_markdown_file(raw_file)
opened = wait_for(lambda: win.editor.toPlainText() == "# Raw\n")
out["raw_opened"] = opened
out["raw_types"] = sorted({nb.type.value for nb in ctx.notebooks.list()})
out["raw_roots"] = [nb.root for nb in ctx.notebooks.list()]
out["editor_after_raw"] = win.editor.toPlainText()

# 2) A file inside an open bundled notebook: reuse it, never duplicate it.
ctx.open_notebook(bundled_root)
count_before = len(ctx.notebooks.list())
win.open_markdown_file(bundled_file)
reused = wait_for(lambda: "Body text." in win.editor.toPlainText()
                  and win.notebook_root == bundled_root)
out["bundled_reused"] = reused
out["bundled_counts"] = [count_before, len(ctx.notebooks.list())]
out["bundled_editor"] = win.editor.toPlainText()

# 3) A path that is not a file must not disturb the open note.
win.open_markdown_file(missing)
pump(300)
out["editor_after_missing"] = win.editor.toPlainText()
out["status_after_missing"] = win.statusBar().currentMessage()

win.open_markdown_file(raw_file)
wait_for(lambda: win.editor.toPlainText() == "# Raw\n")
out["last_path"] = settings.value("last_markdown_file")

# Restore the last path into the same window (a restart creates a new one).
settings.setValue("last_markdown_file", raw_file)
settings.sync()
out["restore_started"] = win.restore_last_file()
out["restored"] = wait_for(
    lambda: win.editor.toPlainText() == "# Raw\n")
out["restored_text"] = win.editor.toPlainText()
win.close()
app.quit()
print("__RESULT__" + json.dumps(out))
'''


class OpenMarkdownFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.addClassCleanup(lambda: shutil.rmtree(cls.tmp, ignore_errors=True))
        cls._result = None

        # A folder that is not a notebook, holding one Markdown file.
        cls.notes_dir = os.path.join(cls.tmp, "notes")
        os.makedirs(cls.notes_dir)
        cls.raw_file = os.path.join(cls.notes_dir, "raw.md")
        with open(cls.raw_file, "w", encoding="utf-8") as handle:
            handle.write("# Raw\n")
        cls.missing = os.path.join(cls.notes_dir, "missing.md")

        # A bundled notebook with one indexed note, opened by the driver.
        fixture = os.path.join(cls.tmp, "fixture")
        os.makedirs(fixture)
        cls.bundled_root = make_bundled(fixture, "Bundled Fixture")
        cls.bundled_file = os.path.join(
            cls.bundled_root, "notes", "hello.md")

    def _run_driver(self) -> dict:
        """Run the offscreen window once and share its JSON result."""
        if type(self)._result is not None:
            return type(self)._result
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"  # headless, and no real display
        args = [
            sys.executable, "-c", DRIVER, ROOT,
            os.path.join(self.tmp, "app"), os.path.join(self.tmp, "local"),
            os.path.join(self.tmp, "settings.ini"),
            self.raw_file, self.bundled_file, self.bundled_root, self.missing,
        ]
        try:
            process = subprocess.run(
                args, cwd=ROOT, env=env, capture_output=True, text=True,
                timeout=180, check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail("the offscreen window did not finish within 180s")
        for line in process.stdout.splitlines():
            if line.startswith("__RESULT__"):
                type(self)._result = json.loads(line[len("__RESULT__"):])
                return type(self)._result
        self.fail(
            "driver produced no result (exit {})\nstdout tail:\n{}\n"
            "stderr tail:\n{}".format(
                process.returncode,
                "\n".join(process.stdout.splitlines()[-15:]),
                "\n".join(process.stderr.splitlines()[-15:]),
            )
        )

    def test_file_menu_offers_open_markdown_file(self):
        self.assertIn("Open Markdown File…", self._run_driver()["menu"])

    def test_markdown_file_outside_a_notebook_opens_as_raw_notebook(self):
        result = self._run_driver()
        self.assertTrue(result["raw_opened"])
        self.assertEqual(result["editor_after_raw"], "# Raw\n")
        self.assertEqual(result["raw_types"], ["raw"])
        self.assertIn(self.notes_dir, result["raw_roots"])

    def test_file_inside_an_open_notebook_reuses_that_notebook(self):
        result = self._run_driver()
        self.assertTrue(result["bundled_reused"])
        self.assertEqual(result["bundled_counts"][0],
                         result["bundled_counts"][1])
        self.assertEqual(result["bundled_editor"], "# Hello\n\nBody text.\n")

    def test_last_file_is_remembered_and_reopened(self):
        result = self._run_driver()
        self.assertEqual(result["last_path"], os.path.realpath(self.raw_file))
        self.assertTrue(result["restore_started"])
        self.assertTrue(result["restored"])
        self.assertEqual(result["restored_text"], "# Raw\n")

    def test_non_file_path_leaves_the_editor_untouched(self):
        result = self._run_driver()
        self.assertTrue(result["bundled_reused"])
        self.assertEqual(result["editor_after_missing"],
                         result["bundled_editor"])
        self.assertIn("Not a file", result["status_after_missing"])


if __name__ == "__main__":
    unittest.main()
