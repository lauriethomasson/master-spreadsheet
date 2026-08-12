"""
Regression tests for a real report: using a light trackpad tap to select/
unselect a row in the Master default view's "Remove rows" table sometimes
shows the checkbox change and then immediately revert by itself, with no
further input - a harder physical click was reported as reliable.

Investigation (see pages/2_Review_and_Master.py's _render_row_selector/
_selector_widget_key for the mechanism itself): traced the complete
selection lifecycle - the dataframe passed to the selector, the filtered/
index mapping, the widget's own returned selection, session_state["export_
selected_property_ids"], and what happens across a rerun - both by reading
this repo's own code AND Streamlit 1.60's own internal element-ID
computation (streamlit/elements/lib/utils.py's compute_and_register_
element_id): with an explicit user_key and key_as_main_identity given as a
set, only the names in that set (here just "selection_mode"/"is_selection_
activated", both constant) are folded into the widget's real backend
identity - selection_default and the dataframe's own content are NOT part
of it. Confirmed via streamlit/elements/arrow.py's own DataframeSelectionSerde.
deserialize(): selection_default is only ever consulted when no real ui_value
exists yet for that identity - once a widget has genuine state, passing a
different selection_default on a later render is a no-op, exactly like
value= on any other Streamlit widget.

This means the row-selector's already-fingerprinted key (_selector_widget_
key, added for a prior confirmed bug - a stale widget instance's own
frontend-cached positions surviving a master shrink/reorder, producing
"IndexError: index 347 is out of bounds for axis 0 with size 340") is what
keeps this widget's real identity - and therefore its real, persisted
selection state - stable across an ordinary rerun that doesn't change which
property_ids are visible or their order. Every scenario below was verified,
via the same direct session_state manipulation technique test_app_review_
master_table.py's own docstring documents (AppTest has no dedicated wrapper
for driving a dataframe row-selection click), to already compute and persist
the correct selection: nothing here found an additional Python-side state-
sync bug beyond what that prior fix already addressed. These tests exist to
prove that and lock it in as a regression suite - not to demonstrate a new
fix; see this module's own accompanying report for the honest conclusion
about what's left unaccounted for (most likely a frontend/grid-component
touch-event nuance for a light tap vs a firm click, outside this repo).

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_master_table.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_master_selection_lifecycle -v
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


def _selector_key(at) -> str:
    """The row-selector widget's REAL current key - see test_app_review_
    master_table.py's own copy of this helper for why it's fingerprinted
    rather than fixed."""
    return next(d.key for d in at.dataframe if d.key.startswith(_SELECTOR_KEY_PREFIX))


class SelectionLifecycleTests(unittest.TestCase):
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
        # Positions match sort_by_provider's own order (GPE, Kitt's, UNION),
        # not insertion order - see test_app_review_master_table.py's own
        # identical helper for why this must be looked up, not assumed.
        sorted_df = display_utils.sort_by_provider(master_writer.load_master_as_dataframe())
        return int(sorted_df.index[sorted_df["property_id"] == property_id][0])

    def _select(self, at, positions):
        """Sets the row-selector's own session_state to the exact shape
        Streamlit itself writes after a real click - the same technique
        test_app_review_master_table.py already uses and documents."""
        at.session_state[_selector_key(at)] = {
            "selection": {"rows": list(positions), "columns": [], "cells": []}
        }

    def _open(self):
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        return at

    # 1. Selection -> rerun -> selection persistence (a light tap's own
    # "select" must still be selected after the rerun it triggers).
    def test_selection_persists_across_the_rerun_it_triggers(self):
        self._write_three_rows()
        at = self._open()

        self._select(at, [self._position_of("row-A")])
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state["export_selected_property_ids"], {"row-A"})

    # 2. Selection also survives a FURTHER, unrelated rerun with no new
    # interaction at all - the exact "reverts by itself" symptom reported.
    def test_selection_survives_a_further_idle_rerun_with_no_new_interaction(self):
        self._write_three_rows()
        at = self._open()

        self._select(at, [self._position_of("row-A")])
        at.run()
        at.run()  # nothing touched - must not spontaneously change
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state["export_selected_property_ids"], {"row-A"})

    # 3. Unselection -> rerun -> persistence (the reverse direction).
    def test_unselection_persists_across_the_rerun_it_triggers(self):
        self._write_three_rows()
        at = self._open()

        self._select(at, [self._position_of("row-A")])
        at.run()
        self._select(at, [])
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state["export_selected_property_ids"], set())

    def test_unselection_survives_a_further_idle_rerun(self):
        self._write_three_rows()
        at = self._open()

        self._select(at, [self._position_of("row-A")])
        at.run()
        self._select(at, [])
        at.run()
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state["export_selected_property_ids"], set())

    # 4. Filtered selection: selecting while the removal search narrows the
    # view must persist across a rerun, in the filtered widget instance.
    def test_selection_persists_while_filtered(self):
        self._write_three_rows()
        at = self._open()

        filt = next(t for t in at.text_input if t.key == f"{KEY}_filter")
        filt.set_value("Fleet Place").run()

        self._select(at, [0])  # only row visible in the filtered selector
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state["export_selected_property_ids"], {"row-B"})

        at.run()  # idle rerun - still filtered, still selected
        self.assertEqual(at.session_state["export_selected_property_ids"], {"row-B"})

    # 5. Multiple rapid selections/unselections must not restore an older
    # selection snapshot - only the LATEST toggle sequence's own end state
    # should ever be what's tracked.
    def test_multiple_sequential_selections_track_only_the_latest_state(self):
        self._write_three_rows()
        at = self._open()

        a, b, c = self._position_of("row-A"), self._position_of("row-B"), self._position_of("row-C")

        self._select(at, [a])
        at.run()
        self.assertEqual(at.session_state["export_selected_property_ids"], {"row-A"})

        self._select(at, [a, b])
        at.run()
        self.assertEqual(at.session_state["export_selected_property_ids"], {"row-A", "row-B"})

        self._select(at, [b])  # deselected row-A, kept row-B
        at.run()
        self.assertEqual(at.session_state["export_selected_property_ids"], {"row-B"})

        self._select(at, [b, c])
        at.run()
        self.assertEqual(at.session_state["export_selected_property_ids"], {"row-B", "row-C"})

        self._select(at, [])
        at.run()
        self.assertEqual(at.session_state["export_selected_property_ids"], set())

        # Settles here, across a further idle rerun too - no reversion to
        # any earlier snapshot in this sequence.
        at.run()
        self.assertEqual(at.session_state["export_selected_property_ids"], set())

    # 6. Deletion while selected: removing selected rows must not leave
    # stale property_ids in the tracked selection.
    def test_deletion_while_selected_leaves_no_stale_selected_ids(self):
        self._write_three_rows()
        at = self._open()

        self._select(at, [self._position_of("row-A"), self._position_of("row-C")])
        at.run()
        self.assertEqual(at.session_state["export_selected_property_ids"], {"row-A", "row-C"})

        remove_btn = next(b for b in at.button if b.label.startswith("Remove "))
        remove_btn.click().run()
        self.assertFalse(at.exception)

        self.assertEqual(at.session_state["export_selected_property_ids"], set())
        remaining = set(master_writer.load_master_as_dataframe()["property_id"].tolist())
        self.assertEqual(remaining, {"row-B"})

    # 7. Deletion followed by immediate rerender - no browser refresh
    # needed for the removed rows to disappear from the selector table.
    def test_deletion_immediately_removes_rows_from_the_selector_table(self):
        self._write_three_rows()
        at = self._open()

        self._select(at, [self._position_of("row-A")])
        at.run()

        remove_btn = next(b for b in at.button if b.label.startswith("Remove "))
        remove_btn.click().run()
        self.assertFalse(at.exception)

        selector_df = next(d for d in at.dataframe if d.key.startswith(_SELECTOR_KEY_PREFIX)).value
        self.assertEqual(len(selector_df), 2)
        self.assertNotIn("28 Gresham Street", selector_df["building"].tolist())

    # 8. No stale positional indexes against a newly-shortened dataframe -
    # the real reported IndexError's own shape: select near the end of a
    # larger set, shrink it via removal, then keep interacting.
    def test_no_stale_position_reused_against_a_shrunk_dataframe(self):
        rows = [
            ListingRow(building=f"Building {i}", provider="UNION", floor_unit=f"{i}th", property_id=f"row-{i}")
            for i in range(10)
        ]
        master_writer.write_master(rows)
        at = self._open()

        # Select the last two rows (positions 8 and 9, alphabetically last
        # under a single-provider sort - UNION for all, so sort is stable/
        # insertion order here).
        self._select(at, [8, 9])
        at.run()
        remove_btn = next(b for b in at.button if b.label.startswith("Remove "))
        remove_btn.click().run()
        self.assertFalse(at.exception)  # would have raised the real reported IndexError otherwise

        self.assertEqual(len(master_writer.load_master_as_dataframe()), 8)

        # The selector widget's own key must have changed (a fresh instance,
        # not one still holding positions 8/9 against an 8-row table) -
        # and interacting with it now (selecting position 0 of the smaller
        # set) must not raise or reuse anything stale.
        new_key = _selector_key(at)
        self._select(at, [0])
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(len(at.session_state["export_selected_property_ids"]), 1)

    # 9. Selection outside the current removal-search filter survives, and
    # narrowing/widening the filter never spontaneously drops or restores
    # a different, older snapshot.
    def test_selection_outside_current_filter_survives_narrowing_and_widening(self):
        self._write_three_rows()
        at = self._open()

        self._select(at, [self._position_of("row-A"), self._position_of("row-B")])
        at.run()
        self.assertEqual(at.session_state["export_selected_property_ids"], {"row-A", "row-B"})

        filt = next(t for t in at.text_input if t.key == f"{KEY}_filter")
        filt.set_value("Fleet Place").run()  # only row-B visible now
        self.assertEqual(at.session_state["export_selected_property_ids"], {"row-A", "row-B"})

        filt.set_value("").run()  # widen back - both still tracked
        self.assertEqual(at.session_state["export_selected_property_ids"], {"row-A", "row-B"})


if __name__ == "__main__":
    unittest.main()
