"""
Regression test for the Master default view's "Remove N selected row(s)"
button (see pages/2_Review_and_Master.py's _render_master_table) - a real-
browser report of this button sometimes needing two clicks to register.

That was diagnosed as a likely DOM-level timing issue (a click landing on
the button in the split second before its own disabled/label state has
visually updated right after checking a row) - something Streamlit's
AppTest cannot reproduce at all, since it drives the script directly and
has no concept of real browser click timing. This test does NOT attempt to
reproduce that browser-level bug; it only proves the agreed fix's own
no-op behavior is correct: the button is never disabled, and clicking it
with nothing selected is a safe no-op (a friendly message, no write to
master.xlsx) rather than erroring or silently doing something destructive.

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_master_lookup.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_master_table -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import master_writer
from schema import ListingRow

BASE = Path(__file__).resolve().parent.parent


class RemoveSelectedRowNoOpTests(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def _remove_button(self, at):
        return next(b for b in at.button if b.label.startswith("Remove "))

    def test_remove_button_is_never_disabled(self):
        # The old `disabled=not selected_positions` is exactly what the
        # agreed fix removes - this is the deliberate un-disabling, not a
        # claim that the underlying double-click bug is reproduced here.
        master_writer.write_master([ListingRow(building="28 Gresham Street", provider="Kitt's")])

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        self.assertFalse(self._remove_button(at).disabled)

    def test_clicking_remove_with_nothing_selected_is_a_safe_no_op(self):
        master_writer.write_master([ListingRow(building="28 Gresham Street", provider="Kitt's")])
        log_before = master_writer.get_master_write_log()

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        info_text = "".join(i.value for i in at.info)
        self.assertIn("Select at least one row first", info_text)

        # Nothing was written - master.xlsx still has its one original row,
        # and no new version was created by this click (the write log is
        # exactly what it was after the setup write above, no more).
        df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(df), 1)
        self.assertEqual(master_writer.get_master_write_log(), log_before)

    def test_clicking_remove_with_nothing_selected_does_not_show_removed_confirmation(self):
        master_writer.write_master([ListingRow(building="28 Gresham Street", provider="Kitt's")])

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._remove_button(at).click().run()

        markdown_text = "".join(m.value for m in at.markdown)
        self.assertNotIn("row removed", markdown_text)
        self.assertNotIn("rows removed", markdown_text)


if __name__ == "__main__":
    unittest.main()
