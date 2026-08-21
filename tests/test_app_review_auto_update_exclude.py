"""
Regression tests for the per-property "Don't apply this update" checkbox in
the "Automatic updates" -> "View changes" expander (see pages/
2_Review_and_Master.py's _render_auto_updates_diff).

Nothing is written to master until "Approve -> Master" is clicked, so
checking this box just removes that one property from auto_updates before
it's merged into `updates` - the property keeps its OLD master values
untouched on approve, exactly as if the upload had never mentioned it.
Every OTHER auto-updated property in the same batch must still apply
normally.

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_page_restructure.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_auto_update_exclude -v
"""

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import master_writer
from schema import ListingRow
from storage.file_store import save_staging_file
from streamlit.testing.v1 import AppTest

BASE = Path(__file__).resolve().parent.parent

_DONT_APPLY = "↩ Don't apply this update"


class IsolatedCwdTestCase(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()


def _run_review_page():
    at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
    at.run()
    return at


class AutoUpdateExcludeCheckboxTests(IsolatedCwdTestCase):
    def _two_pentonville_style_auto_updates(self):
        master_writer.write_master([
            ListingRow(
                building="44 Pentonville Road", provider="MetSpace",
                special_features="4 MR + 3 PB; Available: Now",
                property_id=str(uuid.uuid4()),
            ),
            ListingRow(
                building="50 Pentonville Road", provider="MetSpace",
                special_features="2 MR + 1 PB; Available: Now",
                property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [
                ListingRow(
                    building="44 Pentonville Road", provider="MetSpace",
                    special_features="4 MR + 3 PB; Available: December",
                ),
                ListingRow(
                    building="50 Pentonville Road", provider="MetSpace",
                    special_features="2 MR + 1 PB; Available: December",
                ),
            ],
            "pentonville_pair.xlsx", content_hash="pentonville-pair-hash",
        )
        return _run_review_page()

    def test_checkbox_shown_per_property_in_view_changes(self):
        at = self._two_pentonville_style_auto_updates()
        self.assertFalse(at.exception)

        view_changes = [e for e in at.expander if e.label == "View changes"]
        self.assertEqual(len(view_changes), 1)
        exclude_checkboxes = [c for c in view_changes[0].checkbox if c.label == _DONT_APPLY]
        self.assertEqual(len(exclude_checkboxes), 2)

    def test_view_changes_renders_the_new_html_diff_table_per_property(self):
        # Same Field/Current/New table styling as _render_compact_diff_
        # table (see _diff_table_divider_row_html/_diff_table_row_html) -
        # one small table per property here (a live checkbox can't live
        # inside static HTML), each with its own divider-style header row.
        at = self._two_pentonville_style_auto_updates()
        view_changes = [e for e in at.expander if e.label == "View changes"][0]
        markdown_text = "".join(m.value or "" for m in view_changes.markdown)

        self.assertEqual(markdown_text.count('<table class="diff-table">'), 4)  # 2 divider tables + 2 field tables
        self.assertIn('<td colspan="3">44 Pentonville Road — MetSpace</td>', markdown_text)
        self.assertIn('<td colspan="3">50 Pentonville Road — MetSpace</td>', markdown_text)
        self.assertIn("<td>Special Features</td>", markdown_text)
        self.assertIn("<td>4 MR + 3 PB; Available: December</td>", markdown_text)

    def test_checking_it_excludes_only_that_property_on_approve(self):
        at = self._two_pentonville_style_auto_updates()
        view_changes = [e for e in at.expander if e.label == "View changes"][0]
        exclude_checkboxes = [c for c in view_changes.checkbox if c.label == _DONT_APPLY]

        # Exclude the first property shown, leave the second one untouched.
        exclude_checkboxes[0].set_value(True)
        at.run()

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        by_building = {row["building"]: row for _, row in master_df.iterrows()}
        self.assertEqual(by_building["44 Pentonville Road"]["special_features"], "4 MR + 3 PB; Available: Now")
        self.assertEqual(by_building["50 Pentonville Road"]["special_features"], "2 MR + 1 PB; Available: December")

    def test_leaving_both_unchecked_applies_both_as_before(self):
        at = self._two_pentonville_style_auto_updates()
        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        by_building = {row["building"]: row for _, row in master_df.iterrows()}
        self.assertEqual(by_building["44 Pentonville Road"]["special_features"], "4 MR + 3 PB; Available: December")
        self.assertEqual(by_building["50 Pentonville Road"]["special_features"], "2 MR + 1 PB; Available: December")


if __name__ == "__main__":
    unittest.main()
