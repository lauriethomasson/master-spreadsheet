"""
Regression tests for the Master default view's row-selection/removal
behavior (see pages/2_Review_and_Master.py's _render_master_table/
_render_row_selector/_render_selection_actions).

There is ONE master table on this page - a natively row-selectable
st.dataframe, full column set - not a compact selector plus a separate
editable grid. Editing a property is a deliberate, separate action ("Edit
selected property" - see tests/test_app_review_edit_property.py), not a
second spreadsheet-shaped widget competing for the same screen space.

Real-browser history behind the "Remove selected" button specifically: a
report of it sometimes needing two clicks to register. Root cause: the OLD
design put row selection in a manually-added "Select" CheckboxColumn living
inside the SAME st.data_editor as real field edits - checking a box is
itself a grid-cell EDIT that has to commit to Streamlit's backend before a
rerun sees it, and in a real browser that commit could race against a
separate st.button's click landing in close succession. Rigorous AppTest-
level state manipulation (setting the editor's own edited_rows/selection
state directly, reproducing both a single combined rerun and two genuinely
sequential ones) proved this was never a stale-Python-state bug: every
scenario computed the correct selection given synchronized widget state.
The unfixable-in-pure-Python part is exactly that race, which no
restructuring of which session_state key gets read can fix on its own. The
fix: selection lives in ITS OWN widget using Streamlit's native on_select/
selection_mode row selection - never an in-grid edit at all, so it can
never race against anything else committing to the SAME widget. Separately,
and deliberately, the Remove button's own `disabled=` was removed entirely
- conditionally disabling it based on live selection reintroduces the same
class of rerun-timing risk (see RemoveSelectedRowNoOpTests' own test for
this specific, still-load-bearing decision).

These tests use the same technique the codebase already relies on for
data_editor/dataframe-selection (see test_app_review_master_search.py's own
docstring): AppTest has no dedicated wrapper for driving data_editor cell
edits or dataframe row-selection clicks, so tests set the relevant widget's
own session_state value directly (the same shape Streamlit itself would
write after a real interaction) and then call .run() - this exercises the
exact same Python-side code path a real interaction would, while making no
claim about reproducing real-browser DOM-level event timing.

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
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import display_utils
import master_writer
from schema import ListingRow

BASE = Path(__file__).resolve().parent.parent
KEY = "master_table_default_view"
_SELECTOR_KEY_PREFIX = f"{KEY}_selector_"


def _selector_key(at) -> str:
    """
    The row-selector widget's REAL current key - fingerprinted on the
    property_id sequence it's currently backed by (see pages/2_Review_and_
    Master.py's own _selector_widget_key), not a fixed constant, so it
    changes whenever a removal (or anything else that changes the master's
    row set/order) happens. Looked up from at.dataframe - which always
    reflects the most recent .run()'s real widgets - rather than
    recomputed independently, so these tests can never drift out of sync
    with the app's own fingerprint algorithm by duplicating it.
    """
    return next(d.key for d in at.dataframe if d.key.startswith(_SELECTOR_KEY_PREFIX))


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
        exists for this widget's row-click interaction). Uses the widget's
        REAL current key (see _selector_key) - requires at least one
        at.run() to have already happened so at.dataframe has something to
        look the key up from, true of every caller here."""
        at.session_state[_selector_key(at)] = {
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
        # empty) - under a NEW fingerprinted key too, since master just
        # shrank by one row - legitimately existing again with an EMPTY
        # selection, which is the correct end state, not a leftover stale
        # one.
        self.assertEqual(at.session_state[_selector_key(at)]["selection"]["rows"], [])

        captions = "".join(c.value for c in at.caption)
        self.assertIn("0 of 2 row(s) selected", captions)

    def test_table_reflects_removal_immediately_after_one_click(self):
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-A")])
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        # All of this reflects the SAME rerun the click itself triggered -
        # no second interaction was needed for any of it to catch up: the
        # selection caption already reflects zero selected, the disk copy
        # of master.xlsx already excludes the removed row, and the
        # confirmation is already showing.
        captions = "".join(c.value for c in at.caption)
        self.assertIn("0 of 2 row(s) selected", captions)
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

    def test_removal_uses_property_id_not_stale_positional_identity(self):
        # Editing a property (via "Edit selected property" - see
        # tests/test_app_review_edit_property.py for that form's own
        # dedicated coverage) re-sorts the master (sort_by_provider) on the
        # very next load, shifting row positions out from under any stale
        # positional reference - selecting AFTER that edit must still
        # resolve to the correct row by property_id, not by whatever
        # position it happened to occupy before the edit.
        self._write_three_rows()
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [self._position_of("row-A")])
        at.run()
        edit_btn = next(b for b in at.button if b.label == "Edit selected property")
        edit_btn.click().run()
        size_input = next(n for n in at.number_input if n.label == "Size Sqft")
        size_input.set_value(9999.0).run()
        save_btn = next(b for b in at.button if b.label == "Save")
        save_btn.click().run()
        self.assertFalse(at.exception)

        df_after_edit = master_writer.load_master_as_dataframe()
        self.assertEqual(df_after_edit.loc[df_after_edit["property_id"] == "row-A", "size_sqft"].iloc[0], 9999.0)

        self._select_rows(at, [self._position_of("row-A")])
        # AppTest doesn't always fully settle a save-triggered st.rerun() in
        # one .run() call - an extra no-op run here just lets the harness's
        # own element tree catch up before the next simulated click; it has
        # no effect on and proves nothing about the app's own logic (which
        # is the thing under test below).
        at.run()
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        df_after_removal = master_writer.load_master_as_dataframe()
        self.assertEqual(len(df_after_removal), 2)
        self.assertNotIn("row-A", df_after_removal["property_id"].tolist())


class StaleSelectionArchitectureTests(unittest.TestCase):
    """
    Regression coverage for a real production bug report: an IndexError
    ("index N is out of bounds for axis 0 with size M") from
    _render_row_selector after removing rows, a checkbox that sometimes
    unchecks itself on a light trackpad tap, and removed rows lingering in
    the Remove-rows selector until a manual browser refresh.

    Root cause: the selector widget's key was a plain constant
    (f"{key}_selector"), so Streamlit reused ITS OWN frontend-cached
    position-based selection state across a rerun where the underlying
    row set/order had genuinely changed (a removal, an edit-triggered
    re-sort, a removal-search filter change) - a stale position could then
    be out of range for the new, smaller data (the IndexError), or simply
    point at the wrong row. The fix (_selector_widget_key) folds a
    fingerprint of the CURRENTLY visible property_id sequence into the
    key, so Streamlit mounts a genuinely new widget instance - no
    inherited state at all - exactly when that sequence changes, and keeps
    the SAME key otherwise (an unrelated rerun must not remount the widget
    - that would itself reintroduce the exact kind of race a real light/
    quick trackpad tap can lose).

    AppTest cannot simulate a real browser's click-timing/light-tap
    behavior (see _render_row_selector's own docstring on the earlier,
    similar race that motivated splitting selection into its own widget)
    - these tests prove the underlying state-lifecycle mechanics
    (key stability/change, no crash on a stale position, immediate same-
    rerun consistency) are correct by construction, not that a real
    browser tap can no longer be mistimed.
    """

    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def _select_rows(self, at, positions):
        at.session_state[_selector_key(at)] = {
            "selection": {"rows": list(positions), "columns": [], "cells": []}
        }

    def _remove_button(self, at):
        return next(b for b in at.button if b.label.startswith("Remove "))

    def _write_rows(self, n):
        master_writer.write_master([
            ListingRow(building=f"Building {i}", provider=f"Provider {i}", floor_unit="1st",
                       property_id=f"row-{i}")
            for i in range(n)
        ])

    # 3. Remove after another prior removal.
    def test_remove_after_a_prior_removal(self):
        self._write_rows(4)
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        self._select_rows(at, [0])
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)
        self.assertEqual(len(master_writer.load_master_as_dataframe()), 3)

        # Second removal, same session, same widget/key machinery - must
        # resolve fresh against the NEW (3-row) dataframe, not anything
        # left over from the first removal's own (4-row) one.
        self._select_rows(at, [0])
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(df), 2)

    # 4/13. Stale selected position larger than the new dataframe length
    # never crashes.
    def test_stale_out_of_range_position_never_crashes(self):
        self._write_rows(3)
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        # Simulates the exact real report: a position that would have been
        # valid against a LARGER prior dataframe, injected directly against
        # the CURRENT (correct) widget key - the defensive bounds-clamp in
        # _render_row_selector must swallow this, never raise, regardless
        # of the key-fingerprint fix already making this scenario far less
        # likely to occur for real.
        at.session_state[_selector_key(at)] = {
            "selection": {"rows": [347], "columns": [], "cells": []}
        }
        at.run()

        self.assertFalse(at.exception)
        self.assertEqual(len(master_writer.load_master_as_dataframe()), 3)

    # 6/7/8. The (one, only) master table immediately loses deleted rows,
    # in the SAME rerun the removal click itself triggers - no browser
    # refresh simulated or required.
    def test_remove_table_immediately_loses_deleted_rows_same_rerun(self):
        self._write_rows(3)
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        selector_before = next(d for d in at.dataframe if d.key.startswith(_SELECTOR_KEY_PREFIX))
        self.assertEqual(len(selector_before.value), 3)

        self._select_rows(at, [0])
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        selector_after = next(d for d in at.dataframe if d.key.startswith(_SELECTOR_KEY_PREFIX))
        self.assertEqual(len(selector_after.value), 2)

    # 9. Selection state clears deleted property IDs.
    def test_selection_state_drops_removed_property_ids(self):
        self._write_rows(3)
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        removed_id = master_writer.load_master_as_dataframe().iloc[0]["property_id"]
        self._select_rows(at, [0])
        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        self.assertNotIn(removed_id, at.session_state["export_selected_property_ids"])
        self.assertEqual(at.session_state["export_selected_property_ids"], set())

    # 10. Non-removed selection remains valid when a removal attempt fails
    # (nothing was actually deleted, so nothing should be silently
    # unselected either).
    def test_failed_removal_leaves_selection_and_master_untouched(self):
        self._write_rows(3)
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        selected_id = master_writer.load_master_as_dataframe().iloc[0]["property_id"]
        self._select_rows(at, [0])

        with patch("master_writer.write_master", side_effect=RuntimeError("disk full")):
            self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        self.assertEqual(len(master_writer.load_master_as_dataframe()), 3)
        self.assertIn(selected_id, at.session_state["export_selected_property_ids"])

    # 12. Changing the removal filter after selecting never removes the
    # wrong (now-visible-instead) row.
    def test_filter_change_after_selection_removes_the_originally_selected_row(self):
        self._write_rows(4)
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        target_id = master_writer.load_master_as_dataframe().iloc[0]["property_id"]
        self._select_rows(at, [0])
        at.run()
        self.assertEqual(at.session_state["export_selected_property_ids"], {target_id})

        # Filter narrows the selector to a completely DIFFERENT row than
        # the one still tracked as selected.
        filter_input = next(t for t in at.text_input if t.key == f"{KEY}_filter")
        filter_input.set_value("Building 3").run()
        self.assertFalse(at.exception)

        self._remove_button(at).click().run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        self.assertNotIn(target_id, df["property_id"].tolist())
        self.assertEqual(len(df), 3)

    # 14. Widget keys remain stable across an ordinary rerun that doesn't
    # change the underlying row set/order.
    def test_selector_key_is_stable_across_an_unrelated_rerun(self):
        self._write_rows(3)
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        key_before = _selector_key(at)

        # An idle rerun - nothing touched master, the search filter, or
        # selection - must not remount the widget either; only a genuine
        # change to the visible property_id sequence should do that (see
        # _selector_widget_key's own docstring).
        at.run()
        self.assertFalse(at.exception)

        self.assertEqual(_selector_key(at), key_before)

    # 15. Selection state is not overwritten/reset on an ordinary rerun.
    def test_selection_persists_across_an_unrelated_rerun(self):
        self._write_rows(3)
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        target_id = master_writer.load_master_as_dataframe().iloc[0]["property_id"]
        self._select_rows(at, [0])
        at.run()
        self.assertEqual(at.session_state["export_selected_property_ids"], {target_id})

        master_filter = next(t for t in at.text_input if t.key == f"{KEY}_filter")
        master_filter.set_value("something unrelated").run()
        self.assertFalse(at.exception)

        self.assertEqual(at.session_state["export_selected_property_ids"], {target_id})


if __name__ == "__main__":
    unittest.main()
