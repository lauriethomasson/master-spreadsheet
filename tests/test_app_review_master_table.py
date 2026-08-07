"""
Regression tests for the Master default view's "Remove N selected row(s)"
button (see pages/2_Review_and_Master.py's _render_master_table) - a real-
browser report of this button sometimes needing two clicks to register.

Root cause: the OLD design put row selection in a manually-added "Select"
CheckboxColumn living inside the SAME st.data_editor as real field edits.
Checking a box is itself a grid-cell EDIT that has to commit to Streamlit's
backend before a rerun sees it - and in a real browser that commit could
race against a separate st.button's click landing in close succession.
Rigorous AppTest-level state manipulation (setting the editor's own
edited_rows/selection state directly, reproducing both a single combined
rerun and two genuinely sequential ones) proved this was never a stale-
Python-state bug: every scenario computed the correct selection given
synchronized widget state. The unfixable-in-pure-Python part is exactly
that race, which no restructuring of which session_state key gets read can
fix on its own.

The fix: selection now lives in ITS OWN widget - a compact, read-only
st.dataframe using Streamlit's native on_select/selection_mode row
selection (see _render_row_selector) - never an in-grid edit at all, so it
can never race against anything else committing to the SAME widget. The
real, editable st.data_editor no longer has a "Select" column at all.

These tests use the same technique the codebase already relies on for
data_editor (see test_app_review_master_search.py's own docstring): AppTest
has no dedicated wrapper for driving data_editor cell edits or dataframe
row-selection clicks, so tests set the relevant widget's own session_state
value directly (the same shape Streamlit itself would write after a real
interaction) and then call .run() - this exercises the exact same
Python-side code path a real interaction would, while making no claim
about reproducing real-browser DOM-level event timing.

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

import display_utils
import master_writer
from schema import ListingRow

BASE = Path(__file__).resolve().parent.parent
KEY = "master_table_default_view"
SELECTOR_KEY = f"{KEY}_selector"


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


class OneClickRemovalTests(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def _remove_button(self, at):
        return next(b for b in at.button if b.label.startswith("Remove "))

    def _select_rows(self, at, positions):
        """Sets the row-selector widget's own session_state to the shape
        Streamlit itself writes after a real multi-row selection - see this
        file's module docstring for why this is the right level to drive
        the test at, rather than through any AppTest UI wrapper (none
        exists for this widget's row-click interaction)."""
        at.session_state[SELECTOR_KEY] = {
            "selection": {"rows": list(positions), "columns": [], "cells": []}
        }

    def _write_three_rows(self):
        master_writer.write_master([
            ListingRow(building="28 Gresham Street", provider="Kitt's", floor_unit="1st",
                       property_id="row-A", size_sqft=1000.0),
            ListingRow(building="10 Fleet Place", provider="GPE", floor_unit="2nd",
                       property_id="row-B", size_sqft=2000.0),
            ListingRow(building="1 Wells Street", provider="BNP", floor_unit="3rd",
                       property_id="row-C", size_sqft=3000.0),
        ])

    def _position_of(self, property_id):
        # Positions in the row selector match sort_by_provider's own order
        # (exactly what _render_full_master_view passes into
        # _render_master_table) - NOT insertion order, so a test must look
        # this up rather than assume it, or it would be trivially correct
        # by accident whenever insertion order and sorted order coincide.
        sorted_df = display_utils.sort_by_provider(master_writer.load_master_as_dataframe())
        return int(sorted_df.index[sorted_df["property_id"] == property_id][0])

    def test_selecting_one_row_and_clicking_remove_once_removes_it(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-A")])
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(df), 2)
        self.assertNotIn("row-A", df["property_id"].tolist())
        self.assertEqual(set(df["property_id"].tolist()), {"row-B", "row-C"})

    def test_selecting_multiple_rows_removes_all_in_one_click(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-A"), self._position_of("row-C")])
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(df), 1)
        self.assertEqual(df["property_id"].tolist(), ["row-B"])

    def test_filtered_view_selection_removes_correct_underlying_row(self):
        # "Kitt's" sits at unfiltered position 0, so filtering to it and
        # selecting filtered position 0 is only a genuine proof of
        # position-translation correctness if the FILTERED position (0)
        # and the UNDERLYING position of a DIFFERENT, non-Kitt's row don't
        # coincide - use a provider that is not first alphabetically/by
        # sort so a naive positional removal would hit the wrong row.
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        filter_input = next(t for t in at.text_input if t.key == f"{KEY}_filter")
        filter_input.set_value("Fleet Place").run()

        self._select_rows(at, [0])  # the only row visible in the filtered selector
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(df), 2)
        self.assertNotIn("row-B", df["property_id"].tolist())
        self.assertIn("row-A", df["property_id"].tolist())
        self.assertIn("row-C", df["property_id"].tolist())

    def test_unselected_rows_remain_untouched(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-B")])
        self._remove_button(at).click().run()

        df = master_writer.load_master_as_dataframe()
        remaining = set(df["property_id"].tolist())
        self.assertEqual(remaining, {"row-A", "row-C"})

    def test_selection_state_clears_after_removal(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-A")])
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        self.assertEqual(at.session_state["export_selected_property_ids"], set())
        self.assertEqual(len(at.session_state["export_selected_df"]), 0)
        # The selector widget re-renders on this same settle-rerun (with a
        # freshly empty default, since export_selected_property_ids is now
        # empty) - its key legitimately exists again with an EMPTY
        # selection, which is the correct end state, not a leftover stale
        # one.
        self.assertEqual(at.session_state[SELECTOR_KEY]["selection"]["rows"], [])

        remove_btn = self._remove_button(at)
        self.assertEqual(remove_btn.label, "Remove 0 selected row(s)")

    def test_table_reflects_removal_immediately_after_one_click(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-A")])
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        # All of this reflects the SAME rerun the click itself triggered -
        # no second interaction was needed for any of it to catch up: the
        # button's own label already reflects zero selected, the disk copy
        # of master.xlsx already excludes the removed row, and the
        # confirmation is already showing.
        self.assertEqual(self._remove_button(at).label, "Remove 0 selected row(s)")
        self.assertEqual(len(master_writer.load_master_as_dataframe()), 2)
        markdown_text = "".join(m.value for m in at.markdown)
        self.assertIn("1 row removed", markdown_text)

    def test_undo_restores_removed_rows(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-A")])
        self._remove_button(at).click().run()
        self.assertEqual(len(master_writer.load_master_as_dataframe()), 2)

        markdown_text = "".join(m.value for m in at.markdown)
        self.assertIn("1 row removed", markdown_text)

        undo_btn = next(b for b in at.button if b.label == "Undo")
        undo_btn.click().run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(df), 3)
        self.assertEqual(set(df["property_id"].tolist()), {"row-A", "row-B", "row-C"})

    def test_select_only_changes_do_not_create_manual_edit_writes(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        log_before = master_writer.get_master_write_log()

        self._select_rows(at, [0, 1])
        at.run()
        self.assertFalse(at.exception)

        self.assertEqual(master_writer.get_master_write_log(), log_before)
        self.assertEqual(len(master_writer.load_master_as_dataframe()), 3)

    def test_actual_editable_cell_changes_still_save(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        # Same technique as _select_rows above, applied to the data_editor's
        # OWN key instead of the selector's - the shape Streamlit itself
        # writes there after a real cell edit.
        at.session_state[KEY] = {
            "edited_rows": {1: {"size_sqft": 4500.0}},
            "added_rows": [],
            "deleted_rows": [],
        }
        at.run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        edited_row = df[df["size_sqft"] == 4500.0]
        self.assertEqual(len(edited_row), 1)
        self.assertEqual(len(df), 3)  # a save, not a removal

    def test_removal_uses_property_id_not_stale_positional_identity(self):
        # Manually editing a row re-sorts the master (sort_by_provider) on
        # the very next load, shifting row positions out from under any
        # stale positional reference - selecting AFTER that edit must still
        # resolve to the correct row by property_id, not by whatever
        # position it happened to occupy before the edit.
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        at.session_state[KEY] = {
            "edited_rows": {1: {"size_sqft": 9999.0}},
            "added_rows": [],
            "deleted_rows": [],
        }
        at.run()
        # AppTest doesn't always fully settle a save-triggered st.rerun() in
        # one .run() call - an extra no-op run here just lets the harness's
        # own element tree catch up before the next simulated click; it has
        # no effect on and proves nothing about the app's own logic (which
        # is the thing under test below).
        at.run()
        self.assertFalse(at.exception)

        df_after_edit = master_writer.load_master_as_dataframe()
        edited_property_id = df_after_edit.loc[df_after_edit["size_sqft"] == 9999.0, "property_id"].iloc[0]
        position_of_edited_row_now = df_after_edit.index[df_after_edit["property_id"] == edited_property_id][0]

        self._select_rows(at, [int(position_of_edited_row_now)])
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        df_after_removal = master_writer.load_master_as_dataframe()
        self.assertEqual(len(df_after_removal), 2)
        self.assertNotIn(edited_property_id, df_after_removal["property_id"].tolist())


if __name__ == "__main__":
    unittest.main()
