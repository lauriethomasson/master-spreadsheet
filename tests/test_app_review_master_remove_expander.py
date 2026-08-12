"""
Regression tests for the Master default view's ONE main table (see pages/
2_Review_and_Master.py's _render_master_table) - selection, export, and
removal all happen directly against the single table a reviewer already
sees, with no expander and no second table to open or discover.

History behind this design, in order:
1. Row selection originally lived inside a collapsed st.expander labeled
   "Remove rows" - a real regression report confirmed this made the SAME
   selection mechanism's other purpose (feeding the Export step) look like
   it had disappeared entirely, since nothing about "Remove rows" suggests
   "this is also where you select rows to export".
2. Fixed by making the selector always-visible - but that meant a compact,
   4-column, permanently-read-only selector table sat ABOVE a second, full,
   editable st.data_editor, both visible by default. A reviewer who tested
   editability on the FIRST (read-only-by-construction) table concluded
   editing didn't work at all - the real editable widget was a second table
   further down, easy to miss.
3. This is the current, final resolution: there is exactly ONE table. It
   shows the full column set (not a narrow 4-column strip) and supports
   native row selection directly. Editing one property at a time is its
   own explicit "Edit selected property" action (see tests/
   test_app_review_edit_property.py for that form's own dedicated coverage)
   - never a second grid-shaped widget on screen.

There is also only ONE search bar now (previously two independent ones -
a Remove-rows-only search and the master table's own - each narrowing a
DIFFERENT one of the two tables). With one table, one search bar is enough;
see _SEARCH_COLUMNS in pages/2_Review_and_Master.py.

Deliberately does NOT re-test property_id-based row identity in depth,
Undo/version restore mechanics, or the one-click removal fix itself - those
are already covered by tests/test_app_review_master_table.py and unaffected
by this file's own structural focus (this file's own test_undo_still_works
and test_filtered_removal_still_removes_correct_property_id exist only to
prove those same behaviors survive the one-table restructure, not to
re-derive them).

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
FILTER_KEY = f"{KEY}_filter"


def _selector_key(at) -> str:
    """The (one) main table's REAL current key - see test_app_review_
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

    def _filter_input(self, at):
        return next(t for t in at.text_input if t.key == FILTER_KEY)

    def _table_df(self, at):
        return next(d for d in at.dataframe if d.key.startswith(_SELECTOR_KEY_PREFIX)).value

    # 1. The main table is visible without opening anything at all - the
    # actual regression this file guards against.
    def test_table_is_visible_with_no_expander_to_open(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        # No "Remove rows" (or any other) expander gates the table at all -
        # it must be reachable on a completely fresh render, with no
        # simulated click/interaction of any kind.
        self.assertEqual([e for e in at.expander if e.label == "Remove rows"], [])

        self.assertTrue(any(t.key == FILTER_KEY for t in at.text_input))
        self.assertTrue(any(d.key.startswith(_SELECTOR_KEY_PREFIX) for d in at.dataframe))
        self.assertTrue(any(b.label.startswith("Remove ") for b in at.button))

    # 2. There is exactly ONE table by default - no compact selector plus a
    # separate full table, no data_editor grid either.
    def test_exactly_one_table_is_rendered_by_default(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self.assertEqual(len(at.dataframe), 1)

    # 3. Selection status/caption is visible without opening anything.
    def test_selection_status_caption_is_visible_by_default(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        captions = "".join(c.value for c in at.caption)
        self.assertIn("row(s) selected", captions)

    # 4. Search filters the (one) table - full column set, not a narrow
    # identity strip, so this also proves the merged column set is real.
    def test_search_filters_the_table(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._filter_input(at).set_value("Fleet Place").run()
        self.assertFalse(at.exception)

        shown = self._table_df(at)
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown["building"].tolist(), ["10 Fleet Place"])
        # postcode remains searchable from this one box - the real request
        # a second, removal-only search bar used to serve on its own.
        self._filter_input(at).set_value("EC4M 7RB").run()
        self.assertEqual(self._table_df(at)["building"].tolist(), ["10 Fleet Place"])

    # 5. Filtered removal still removes correct property_id.
    def test_filtered_removal_still_removes_correct_property_id(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._filter_input(at).set_value("Fleet Place").run()
        self._select_rows(at, [0])  # the only row visible in the filtered table
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        remaining = set(df["property_id"].tolist())
        self.assertEqual(remaining, {"row-A", "row-C"})

    # 6. Opening/re-rendering the page does not alter master data.
    def test_rerendering_the_page_does_not_touch_master_data_or_write_log(self):
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

    def test_typing_in_search_does_not_write_to_master(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        log_before = master_writer.get_master_write_log()

        self._filter_input(at).set_value("Fleet Place").run()
        self.assertFalse(at.exception)

        self.assertEqual(master_writer.get_master_write_log(), log_before)
        self.assertEqual(len(master_writer.load_master_as_dataframe()), 3)

    # 7. Selection remains stable while narrowing the search.
    def test_selection_survives_narrowing_the_search(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        # Select two rows while the table is unfiltered.
        self._select_rows(at, [self._position_of("row-A"), self._position_of("row-B")])
        at.run()
        self.assertEqual(
            at.session_state["export_selected_property_ids"], {"row-A", "row-B"}
        )

        # Narrow the search so only ONE of the two previously-selected rows
        # is even visible in the table now.
        self._filter_input(at).set_value("Fleet Place").run()
        self.assertFalse(at.exception)
        shown = self._table_df(at)
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown["building"].tolist(), ["10 Fleet Place"])

        # The tracked selection must still include BOTH rows - the one now
        # hidden by the filter is preserved (see master_merge.
        # merge_selected_property_ids), not silently dropped.
        self.assertEqual(
            at.session_state["export_selected_property_ids"], {"row-A", "row-B"}
        )

        # Clearing the filter and clicking Remove removes BOTH originally-
        # selected rows, not just the one that happened to still be visible.
        self._filter_input(at).set_value("").run()
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        remaining = set(master_writer.load_master_as_dataframe()["property_id"].tolist())
        self.assertEqual(remaining, {"row-C"})

    # 8. Undo still works.
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

    # 9. Selecting one row populates export_selected_df with exactly that
    # row - the actual "Export selected rows" workflow the original
    # regression report was about, distinct from removal.
    def test_selecting_one_row_populates_export_selected_df(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-B")])
        at.run()
        self.assertFalse(at.exception)

        exported = at.session_state["export_selected_df"]
        self.assertEqual(exported["property_id"].tolist(), ["row-B"])

    # 10. Selecting multiple rows populates export_selected_df with all of
    # them, in a stable order.
    def test_selecting_multiple_rows_populates_export_selected_df_with_all(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-A"), self._position_of("row-C")])
        at.run()
        self.assertFalse(at.exception)

        exported = at.session_state["export_selected_df"]
        self.assertEqual(set(exported["property_id"].tolist()), {"row-A", "row-C"})

    # 11. An unselected row is excluded from export_selected_df - selecting
    # some rows must never pull in ones the reviewer didn't check.
    def test_unselected_rows_are_excluded_from_export_selected_df(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-A")])
        at.run()
        self.assertFalse(at.exception)

        exported = at.session_state["export_selected_df"]
        self.assertNotIn("row-B", exported["property_id"].tolist())
        self.assertNotIn("row-C", exported["property_id"].tolist())

    # 12. "Clear selection" empties export_selected_df too, not just the
    # tracked property_id set - the Export page reads THIS, not the set.
    def test_clear_selection_empties_export_selected_df(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-A"), self._position_of("row-B")])
        at.run()
        self.assertEqual(len(at.session_state["export_selected_df"]), 2)

        clear_btn = next(b for b in at.button if b.label == "Clear selection")
        clear_btn.click().run()
        self.assertFalse(at.exception)

        self.assertEqual(len(at.session_state["export_selected_df"]), 0)
        self.assertEqual(at.session_state["export_selected_property_ids"], set())

    # 13. "Export selected ->" is disabled with nothing selected - selection
    # alone (never this button) is what carries rows to the Export page, and
    # a disabled state is the visible signal that nothing is selected yet.
    def test_export_button_is_disabled_with_nothing_selected(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        export_btn = next(b for b in at.button if b.label == "Export selected →")
        self.assertTrue(export_btn.disabled)

    # 14. Selecting a row enables "Export selected ->".
    def test_export_button_is_enabled_once_a_row_is_selected(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-A")])
        at.run()

        export_btn = next(b for b in at.button if b.label == "Export selected →")
        self.assertFalse(export_btn.disabled)

    # 15. Clicking "Export selected ->" must not clear/alter the selection
    # state it's meant to carry across BEFORE it navigates - session_state
    # is set earlier in _render_master_table's own render, ahead of the
    # button/st.switch_page call, so it's already correct by the time the
    # click is even processed, regardless of what happens to the click
    # itself. Deliberately does NOT assert at.exception is empty here:
    # st.switch_page("pages/3_Export.py") genuinely raises
    # StreamlitAPIException under THIS harness specifically, because
    # AppTest.from_file loads pages/2_Review_and_Master.py as if it were
    # the standalone entrypoint, so "pages/3_Export.py" can't resolve
    # relative to it the way it does in the real app (where app.py is the
    # true entrypoint and page_flow.py's own Back/Next buttons already rely
    # on this exact same st.switch_page("pages/...") pattern without issue).
    # A harness limitation of testing one multipage sub-page in isolation,
    # not a product bug - confirmed by this being the identical call
    # page_flow.render_nav_buttons already makes from this same page.
    def test_export_button_click_does_not_clear_selection_state(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-A"), self._position_of("row-B")])
        at.run()
        selected_before = set(at.session_state["export_selected_property_ids"])

        export_btn = next(b for b in at.button if b.label == "Export selected →")
        export_btn.click().run()

        self.assertEqual(at.session_state["export_selected_property_ids"], selected_before)

    # 16. "Edit selected property" is enabled only for exactly one selected
    # row - see tests/test_app_review_edit_property.py for the form itself.
    def test_edit_button_enabled_only_for_exactly_one_selected_row(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        edit_btn = next(b for b in at.button if b.label == "Edit selected property")
        self.assertTrue(edit_btn.disabled)

        self._select_rows(at, [self._position_of("row-A")])
        at.run()
        edit_btn = next(b for b in at.button if b.label == "Edit selected property")
        self.assertFalse(edit_btn.disabled)

        self._select_rows(at, [self._position_of("row-A"), self._position_of("row-B")])
        at.run()
        edit_btn = next(b for b in at.button if b.label == "Edit selected property")
        self.assertTrue(edit_btn.disabled)


if __name__ == "__main__":
    unittest.main()
