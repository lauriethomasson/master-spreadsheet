"""
Regression test for the text filter above the Master default view's ONE
main table (see pages/2_Review_and_Master.py's _render_master_table/
_render_row_selector) - previously only the read-only lookup on the
pending-review screen (_render_master_lookup) had any way to narrow the
view; the main table had none.

The main table is a natively-selectable st.dataframe (its key fingerprinted
on the currently-visible property_id sequence - see _selector_widget_key),
not an editable st.data_editor grid - editing one property at a time is a
separate "Edit selected property" form (see tests/
test_app_review_edit_property.py). This file only proves the filter narrows
what's DISPLAYED in that one table. The separate, more important
correctness guarantee - that a manual edit or removal made while filtered
still resolves to the correct underlying row, never one at the same visual
position - is proven at the pure-logic level in tests/test_master_merge.py's
BuildManualEditTests.test_edit_at_a_filtered_position_updates_the_correct_
real_row and MergeSelectedPropertyIdsTests (both directly exercise the
translation logic this page calls into, with no Streamlit/AppTest involved).

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_master_lookup.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_master_search -v
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
_SELECTOR_KEY_PREFIX = "master_table_default_view_selector_"


class MasterTableSearchFilterTests(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def _seed_master(self):
        master_writer.write_master([
            ListingRow(building="28 Gresham Street", provider="Kitt's", floor_unit="3rd Floor"),
            ListingRow(building="Wells Street", provider="UNION", floor_unit="2nd Floor"),
        ])

    def _filter_input(self, at):
        return next(t for t in at.text_input if t.key == "master_table_default_view_filter")

    def _table_value(self, at):
        # Keyed by fingerprint prefix, not a fixed key or at.dataframe[0] -
        # see _selector_widget_key's own docstring for why the main table's
        # real key changes whenever its visible property_id sequence does
        # (a filter change is exactly that).
        return next(d for d in at.dataframe if d.key.startswith(_SELECTOR_KEY_PREFIX)).value

    def test_unfiltered_view_shows_every_row(self):
        self._seed_master()

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        shown = self._table_value(at)
        self.assertEqual(set(shown["building"]), {"28 Gresham Street", "Wells Street"})

    def test_filter_narrows_to_matching_rows_only(self):
        self._seed_master()

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._filter_input(at).set_value("Gresham").run()
        self.assertFalse(at.exception)

        shown = self._table_value(at)
        self.assertIn("28 Gresham Street", shown["building"].tolist())
        self.assertNotIn("Wells Street", shown["building"].tolist())

    def test_filter_matches_case_insensitively_across_provider_too(self):
        self._seed_master()

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._filter_input(at).set_value("union").run()

        shown = self._table_value(at)
        self.assertEqual(shown["building"].tolist(), ["Wells Street"])

    def test_no_match_shows_an_empty_table_not_an_error(self):
        self._seed_master()

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._filter_input(at).set_value("Nonexistent Building").run()
        self.assertFalse(at.exception)

        shown = self._table_value(at)
        self.assertEqual(len(shown), 0)


if __name__ == "__main__":
    unittest.main()
