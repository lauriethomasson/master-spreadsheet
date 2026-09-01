"""
Regression tests for the Master default view's "Edit selected property" form
(see pages/2_Review_and_Master.py's _render_edit_property_form/
_save_property_edit) - the single-property edit UI that replaced the old
direct-cell-editing st.data_editor grid.

Why a form, not a grid or a dialog: the main table is now ONE natively
row-selectable st.dataframe, not an editable grid - st.data_editor has no
selection_mode at all in the installed Streamlit version (1.60), and
manually adding a "Select" Boolean column back into an editable grid is the
exact design that caused a real two-click/light-trackpad selection race
before this codebase's own prior fix. Streamlit 1.60 does have st.dialog,
but AppTest has no way to interact with a dialog's own contents (no API to
open one, find widgets inside it, or click Save/Cancel within it) - an
expander/form directly under the table is used instead so this form's own
Save/Cancel/field behavior stays covered by real regression tests.

Saving reuses master_merge.build_manual_edit/apply_merge/write_master - the
exact same path the old data_editor grid rode - so a manual edit from this
form is not a new kind of write, just a differently-collected delta.

A real bug found while testing this form end-to-end, fixed before these
tests were written: st.number_input has no representable "blank" state (it
always returns 0.0 for an unset int/float field, since there's no None it
could show instead) - comparing the saved value against the raw original
(None) would see every UNTOUCHED blank numeric field as "changed to 0.0"
and silently zero it out on ANY save. Fixed by comparing against the
widget's own default (0.0) for a field that started blank, not the raw
original None - see ZeroingBlankNumericFieldsTests below.

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_master_table.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_edit_property -v
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
    return next(d.key for d in at.dataframe if d.key.startswith(_SELECTOR_KEY_PREFIX))


class EditPropertyFormTests(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def _write_two_rows(self):
        master_writer.write_master([
            ListingRow(building="28 Gresham Street", provider="Kitt's", floor_unit="1st",
                       property_id="row-A", size_sqft=1000.0),
            ListingRow(building="10 Fleet Place", provider="GPE", floor_unit="2nd",
                       property_id="row-B", size_sqft=2000.0),
        ])

    def _position_of(self, property_id):
        sorted_df = display_utils.sort_by_provider(master_writer.load_master_as_dataframe())
        return int(sorted_df.index[sorted_df["property_id"] == property_id][0])

    def _select(self, at, positions):
        at.session_state[_selector_key(at)] = {
            "selection": {"rows": list(positions), "columns": [], "cells": []}
        }

    def _open_and_select(self, property_id):
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self._select(at, [self._position_of(property_id)])
        at.run()
        return at

    def _click_edit(self, at):
        edit_btn = next(b for b in at.button if b.label == "Edit selected property")
        edit_btn.click().run()

    # 1/16. Edit form loads the correct property's own current values.
    def test_edit_form_loads_the_correct_property(self):
        self._write_two_rows()
        at = self._open_and_select("row-A")
        self._click_edit(at)

        self.assertTrue(any("28 Gresham Street" in e.label for e in at.expander))
        size_input = next(n for n in at.number_input if n.label == "Size Sqft")
        self.assertEqual(size_input.value, 1000.0)
        provider_input = next(t for t in at.text_input if t.label == "Provider")
        self.assertEqual(provider_input.value, "Kitt's")

    # 2/17. Save updates exactly the selected property, using the existing
    # build_manual_edit/write_master path (versioned, logged as manual_edit).
    def test_save_updates_exactly_the_selected_property(self):
        self._write_two_rows()
        at = self._open_and_select("row-B")
        self._click_edit(at)

        size_input = next(n for n in at.number_input if n.label == "Size Sqft")
        size_input.set_value(4500.0).run()
        save_btn = next(b for b in at.button if b.label == "Save")
        save_btn.click().run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        self.assertEqual(df.loc[df["property_id"] == "row-B", "size_sqft"].iloc[0], 4500.0)
        self.assertEqual(df.loc[df["property_id"] == "row-A", "size_sqft"].iloc[0], 1000.0)

        log = master_writer.get_master_write_log()
        self.assertEqual(log[-1]["source"], "manual_edit")
        self.assertEqual(log[-1]["fields_changed"], 1)

    # The save confirmation's own diff (_render_manual_edit_confirmation ->
    # _render_compact_diff_table) renders as a real Field/Current/New HTML
    # table, unconditionally (no "View changes" expander gating it) - see
    # that function's own docstring on why it's always shown inline.
    def test_save_confirmation_shows_the_diff_as_a_table(self):
        self._write_two_rows()
        at = self._open_and_select("row-B")
        self._click_edit(at)

        size_input = next(n for n in at.number_input if n.label == "Size Sqft")
        size_input.set_value(4500.0).run()
        save_btn = next(b for b in at.button if b.label == "Save")
        save_btn.click().run()
        self.assertFalse(at.exception)

        markdown_text = "".join(m.value or "" for m in at.markdown)
        self.assertIn('<table class="diff-table">', markdown_text)
        self.assertIn("<td>Size</td>", markdown_text)
        self.assertIn("<td>2,000 sq ft</td>", markdown_text)
        self.assertIn("<td>4,500 sq ft</td>", markdown_text)

    # 3/18. Cancel changes nothing - no write, form closes.
    def test_cancel_writes_nothing(self):
        self._write_two_rows()
        at = self._open_and_select("row-A")
        self._click_edit(at)

        size_input = next(n for n in at.number_input if n.label == "Size Sqft")
        size_input.set_value(9999.0).run()
        log_before = master_writer.get_master_write_log()

        cancel_btn = next(b for b in at.button if b.label == "Cancel")
        cancel_btn.click().run()
        self.assertFalse(at.exception)

        self.assertEqual(master_writer.get_master_write_log(), log_before)
        df = master_writer.load_master_as_dataframe()
        self.assertEqual(df.loc[df["property_id"] == "row-A", "size_sqft"].iloc[0], 1000.0)
        # The form is gone - editing_property_id was cleared.
        self.assertEqual([e for e in at.expander if "Edit" in e.label], [])

    # A no-op Save (nothing actually changed) creates no version either.
    def test_saving_with_no_changes_is_a_no_op(self):
        self._write_two_rows()
        at = self._open_and_select("row-A")
        self._click_edit(at)
        log_before = master_writer.get_master_write_log()

        save_btn = next(b for b in at.button if b.label == "Save")
        save_btn.click().run()
        self.assertFalse(at.exception)

        self.assertEqual(master_writer.get_master_write_log(), log_before)
        info_text = "".join(i.value for i in at.info)
        self.assertIn("No changes to save", info_text)

    # 19. property_id is never an editable field - shown read-only only.
    def test_property_id_is_not_editable(self):
        self._write_two_rows()
        at = self._open_and_select("row-A")
        self._click_edit(at)

        self.assertEqual([t for t in at.text_input if t.label == "Property Id"], [])
        self.assertEqual([n for n in at.number_input if n.label == "Property Id"], [])
        captions = "".join(c.value for c in at.caption)
        self.assertIn("row-A", captions)

    # A text field can genuinely be edited and saved too, not just numeric.
    def test_text_field_edit_saves_correctly(self):
        self._write_two_rows()
        at = self._open_and_select("row-A")
        self._click_edit(at)

        provider_input = next(t for t in at.text_input if t.label == "Provider")
        provider_input.set_value("New Provider Name").run()
        save_btn = next(b for b in at.button if b.label == "Save")
        save_btn.click().run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        self.assertEqual(df.loc[df["property_id"] == "row-A", "provider"].iloc[0], "New Provider Name")

    # A text field cleared to blank saves as genuinely blank (None), not an
    # empty-string artifact.
    def test_clearing_a_text_field_saves_as_blank(self):
        master_writer.write_master([
            ListingRow(building="28 Gresham Street", provider="Kitt's", floor_unit="1st",
                       property_id="row-A", submarket="The City"),
        ])
        at = self._open_and_select("row-A")
        self._click_edit(at)

        submarket_input = next(t for t in at.text_input if t.label == "Submarket")
        submarket_input.set_value("").run()
        save_btn = next(b for b in at.button if b.label == "Save")
        save_btn.click().run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        self.assertIsNone(df.loc[df["property_id"] == "row-A", "submarket"].iloc[0])


class ZeroingBlankNumericFieldsTests(unittest.TestCase):
    """
    Regression coverage for a real bug found while testing this form:
    st.number_input has no representable "blank" state, so a blank lat/lng/
    desks_max/etc. field defaults its widget to 0.0 - comparing the SAVED
    value against the raw original (None) would see every untouched blank
    numeric field as "changed to 0.0" and silently zero it out on ANY save
    at all, not just one touching that field.
    """

    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def _position_of(self, property_id):
        sorted_df = display_utils.sort_by_provider(master_writer.load_master_as_dataframe())
        return int(sorted_df.index[sorted_df["property_id"] == property_id][0])

    def _select(self, at, positions):
        at.session_state[_selector_key(at)] = {
            "selection": {"rows": list(positions), "columns": [], "cells": []}
        }

    def test_untouched_blank_numeric_fields_stay_blank_after_saving_a_different_field(self):
        master_writer.write_master([
            # lat/lng/desks_max/rent_pcm/rent_psf all genuinely blank -
            # the real shape of a manually-added or partially-filled row.
            ListingRow(building="28 Gresham Street", provider="Kitt's", floor_unit="1st",
                       property_id="row-A", size_sqft=1000.0),
        ])
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self._select(at, [self._position_of("row-A")])
        at.run()

        edit_btn = next(b for b in at.button if b.label == "Edit selected property")
        edit_btn.click().run()
        size_input = next(n for n in at.number_input if n.label == "Size Sqft")
        size_input.set_value(4500.0).run()
        save_btn = next(b for b in at.button if b.label == "Save")
        save_btn.click().run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        row = df.loc[df["property_id"] == "row-A"].iloc[0]
        self.assertEqual(row["size_sqft"], 4500.0)
        self.assertIsNone(row["lat"])
        self.assertIsNone(row["lng"])

        log = master_writer.get_master_write_log()
        self.assertEqual(log[-1]["fields_changed"], 1)

    def test_deliberately_setting_a_numeric_field_still_saves(self):
        master_writer.write_master([
            ListingRow(building="28 Gresham Street", provider="Kitt's", floor_unit="1st", property_id="row-A"),
        ])
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self._select(at, [self._position_of("row-A")])
        at.run()

        edit_btn = next(b for b in at.button if b.label == "Edit selected property")
        edit_btn.click().run()
        lat_input = next(n for n in at.number_input if n.label == "Lat")
        lat_input.set_value(51.52).run()
        save_btn = next(b for b in at.button if b.label == "Save")
        save_btn.click().run()
        self.assertFalse(at.exception)

        df = master_writer.load_master_as_dataframe()
        self.assertEqual(df.loc[df["property_id"] == "row-A", "lat"].iloc[0], 51.52)

    def test_lat_lng_inputs_render_at_full_precision_unlike_other_numeric_fields(self):
        # Same display-precision fix as display_utils.render_new_value_
        # input's own LATLNG_FIELDS (see its docstring) - this form's own
        # SEPARATE st.number_input call site needed the identical fix.
        master_writer.write_master([
            ListingRow(
                building="28 Gresham Street", provider="Kitt's", floor_unit="1st", property_id="row-A",
                lat=-0.1442492, rent_psf=55.5,
            ),
        ])
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self._select(at, [self._position_of("row-A")])
        at.run()

        edit_btn = next(b for b in at.button if b.label == "Edit selected property")
        edit_btn.click().run()
        lat_input = next(n for n in at.number_input if n.label == "Lat")
        rent_input = next(n for n in at.number_input if n.label == "Rent Psf")

        self.assertEqual(lat_input.proto.format, "%.7f")
        self.assertEqual(lat_input.value, -0.1442492)
        # A non-lat/lng numeric field must stay completely unaffected.
        self.assertEqual(rent_input.proto.format, "%0.2f")


if __name__ == "__main__":
    unittest.main()
