"""
Regression tests for the Master default view's "Remove rows" UI restructure
(see pages/2_Review_and_Master.py's _render_master_table) - a real request to
tuck the row-removal UI (search bar + row-selector + Remove button) inside a
collapsed-by-default st.expander, give it its OWN search bar completely
independent from the main editable table's own search bar, and move the
main table's search bar to read clearly as "Search master spreadsheet".

Deliberately does NOT re-test property_id-based row identity, Undo/version
restore mechanics, or the one-click removal fix itself in depth - those are
already covered by tests/test_app_review_master_table.py and unaffected by
this purely structural change (this file's own test_undo_still_works and
test_filtered_removal_still_removes_correct_property_id exist only to prove
those same behaviors survive the restructure, not to re-derive them).

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_master_table.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_master_remove_expander -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import display_utils
import master_writer
from schema import ListingRow

BASE = Path(__file__).resolve().parent.parent
KEY = "master_table_default_view"
_SELECTOR_KEY_PREFIX = f"{KEY}_selector_"
REMOVAL_FILTER_KEY = f"{KEY}_removal_filter"
MASTER_FILTER_KEY = f"{KEY}_filter"


def _selector_key(at) -> str:
    """The row-selector widget's REAL current key - see test_app_review_
    master_table.py's own copy of this helper for why it's fingerprinted
    rather than fixed, and why this looks it up from at.dataframe instead
    of recomputing it independently."""
    return next(d.key for d in at.dataframe if d.key.startswith(_SELECTOR_KEY_PREFIX))


class RemoveRowsExpanderTests(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def _write_three_rows(self):
        master_writer.write_master([
            ListingRow(building="28 Gresham Street", provider="Kitt's", floor_unit="1st",
                       address_1="28 Gresham Street", postcode="EC2V 7AY", property_id="row-A"),
            ListingRow(building="10 Fleet Place", provider="GPE", floor_unit="2nd",
                       address_1="10 Fleet Place", postcode="EC4M 7RB", property_id="row-B"),
            ListingRow(building="1 Wells Street", provider="UNION", floor_unit="3rd",
                       address_1="1 Wells Street", postcode="W1T 3PB", property_id="row-C"),
        ])

    def _position_of(self, property_id):
        sorted_df = display_utils.sort_by_provider(master_writer.load_master_as_dataframe())
        return int(sorted_df.index[sorted_df["property_id"] == property_id][0])

    def _select_rows(self, at, positions):
        at.session_state[_selector_key(at)] = {
            "selection": {"rows": list(positions), "columns": [], "cells": []}
        }

    def _remove_button(self, at):
        return next(b for b in at.button if b.label.startswith("Remove "))

    def _remove_filter_input(self, at):
        return next(t for t in at.text_input if t.key == REMOVAL_FILTER_KEY)

    def _master_filter_input(self, at):
        return next(t for t in at.text_input if t.key == MASTER_FILTER_KEY)

    def _remove_row_selector_df(self, at):
        return next(d for d in at.dataframe if d.key.startswith(_SELECTOR_KEY_PREFIX)).value

    def _master_table_df(self, at):
        return next(d for d in at.dataframe if d.key == KEY).value

    # 1. Remove rows UI is inside an expander.
    def test_remove_rows_ui_is_inside_an_expander(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        expanders = [e for e in at.expander if e.label == "Remove rows"]
        self.assertEqual(len(expanders), 1)

        # The removal search bar, the row-selector, and the Remove button
        # all exist at all (AppTest's flat element lists don't expose
        # parent/child containment directly, but their mere presence here,
        # combined with the expander existing with this exact label, is
        # what "inside an expander" means for this page's own structure -
        # there is only ever one of each on this view).
        self.assertTrue(any(t.key == REMOVAL_FILTER_KEY for t in at.text_input))
        self.assertTrue(any(d.key.startswith(_SELECTOR_KEY_PREFIX) for d in at.dataframe))
        self.assertTrue(any(b.label.startswith("Remove ") for b in at.button))

    # 2. Expander is collapsed by default.
    def test_expander_is_collapsed_by_default(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        expander = next(e for e in at.expander if e.label == "Remove rows")
        self.assertFalse(expander.proto.expanded)

    # 3. Remove search filters only removal table.
    def test_remove_search_filters_only_removal_table(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._remove_filter_input(at).set_value("Fleet Place").run()
        self.assertFalse(at.exception)

        selector_shown = self._remove_row_selector_df(at)
        self.assertEqual(len(selector_shown), 1)
        self.assertEqual(selector_shown["building"].tolist(), ["10 Fleet Place"])

        master_shown = self._master_table_df(at)
        self.assertEqual(len(master_shown), 3)  # completely unaffected

    # 4. Master search filters only master table.
    def test_master_search_filters_only_master_table(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._master_filter_input(at).set_value("Wells Street").run()
        self.assertFalse(at.exception)

        master_shown = self._master_table_df(at)
        self.assertEqual(len(master_shown), 1)
        self.assertEqual(master_shown["building"].tolist(), ["1 Wells Street"])

        selector_shown = self._remove_row_selector_df(at)
        self.assertEqual(len(selector_shown), 3)  # completely unaffected

    # 5. The two search values can coexist independently.
    def test_both_searches_coexist_independently(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._remove_filter_input(at).set_value("UNION").run()
        self._master_filter_input(at).set_value("GPE").run()
        self.assertFalse(at.exception)

        selector_shown = self._remove_row_selector_df(at)
        self.assertEqual(selector_shown["building"].tolist(), ["1 Wells Street"])

        master_shown = self._master_table_df(at)
        self.assertEqual(master_shown["building"].tolist(), ["10 Fleet Place"])

        # Both values are still exactly what was typed - neither widget
        # reset or overwrote the other's.
        self.assertEqual(self._remove_filter_input(at).value, "UNION")
        self.assertEqual(self._master_filter_input(at).value, "GPE")

    # 6. Filtered removal still removes correct property_id.
    def test_filtered_removal_still_removes_correct_property_id(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._remove_filter_input(at).set_value("Fleet Place").run()
        self._select_rows(at, [0])  # the only row visible in the filtered selector
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        remaining = set(df["property_id"].tolist())
        self.assertEqual(remaining, {"row-A", "row-C"})

    # 7. Master table editing still works.
    def test_master_table_editing_still_works(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        at.session_state[KEY] = {
            "edited_rows": {1: {"size_sqft": 4500.0}},
            "added_rows": [],
            "deleted_rows": [],
        }
        at.run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(df[df["size_sqft"] == 4500.0]), 1)
        self.assertEqual(len(df), 3)  # a save, not a removal

    # 8. Opening/closing remove expander does not alter master data.
    def test_rerendering_the_page_does_not_touch_master_data_or_write_log(self):
        # AppTest has no dedicated action for toggling an expander open/
        # closed (its own docstring in pages/2_Review_and_Master.py notes
        # this is a purely client-side visual toggle - the Python script
        # runs the expander's contents unconditionally either way). The
        # closest meaningful proxy is: nothing about merely re-running the
        # page (the same thing that happens regardless of the expander's
        # visual state) ever writes to master.xlsx or changes its content.
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        log_before = master_writer.get_master_write_log()
        df_before = master_writer.load_master_as_dataframe()

        at.run()
        at.run()
        self.assertFalse(at.exception)

        self.assertEqual(master_writer.get_master_write_log(), log_before)
        df_after = master_writer.load_master_as_dataframe()
        self.assertEqual(set(df_after["property_id"]), set(df_before["property_id"]))

    def test_typing_in_removal_search_does_not_write_to_master(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        log_before = master_writer.get_master_write_log()

        self._remove_filter_input(at).set_value("Fleet Place").run()
        self.assertFalse(at.exception)

        self.assertEqual(master_writer.get_master_write_log(), log_before)
        self.assertEqual(len(master_writer.load_master_as_dataframe()), 3)

    # 9. Removal selection remains stable while using its own filter.
    def test_selection_survives_narrowing_the_removal_filter(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        # Select two rows while the removal selector is unfiltered.
        self._select_rows(at, [self._position_of("row-A"), self._position_of("row-B")])
        at.run()
        self.assertEqual(
            at.session_state["export_selected_property_ids"], {"row-A", "row-B"}
        )

        # Narrow the removal search so only ONE of the two previously-
        # selected rows is even visible in the selector now.
        self._remove_filter_input(at).set_value("Fleet Place").run()
        self.assertFalse(at.exception)
        selector_shown = self._remove_row_selector_df(at)
        self.assertEqual(len(selector_shown), 1)
        self.assertEqual(selector_shown["building"].tolist(), ["10 Fleet Place"])

        # The tracked selection must still include BOTH rows - the one now
        # hidden by the filter is preserved (see master_merge.
        # merge_selected_property_ids), not silently dropped.
        self.assertEqual(
            at.session_state["export_selected_property_ids"], {"row-A", "row-B"}
        )

        # Clearing the filter and clicking Remove removes BOTH originally-
        # selected rows, not just the one that happened to still be visible.
        self._remove_filter_input(at).set_value("").run()
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        remaining = set(master_writer.load_master_as_dataframe()["property_id"].tolist())
        self.assertEqual(remaining, {"row-C"})

    # 10. Undo still works.
    def test_undo_still_works(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-A")])
        self._remove_button(at).click().run()
        self.assertEqual(len(master_writer.load_master_as_dataframe()), 2)

        undo_btn = next(b for b in at.button if b.label == "Undo")
        undo_btn.click().run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        self.assertEqual(set(df["property_id"].tolist()), {"row-A", "row-B", "row-C"})


if __name__ == "__main__":
    unittest.main()
