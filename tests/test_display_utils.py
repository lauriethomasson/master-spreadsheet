"""
Regression tests for display_utils.py's own formatting helpers used by the
Review & Master page's compact before/after rows (see pages/2_Review_and_
Master.py's _render_field_rows/_render_compact_diff_table) - purely
cosmetic (see display_utils.py's own module docstring: these never affect
what gets written to staging/master).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_display_utils -v
"""

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import display_utils


class FriendlyFieldLabelTests(unittest.TestCase):
    def test_explicit_overrides(self):
        cases = {
            "address_1": "Address",
            "size_sqft": "Size",
            "rent_pcm": "Rent PCM",
            "rent_psf": "Rent PSF",
            "floor_unit": "Floor / Unit",
            "desks_min": "Minimum desks",
            "desks_max": "Maximum desks",
        }
        for field, expected in cases.items():
            self.assertEqual(display_utils.friendly_field_label(field), expected, field)

    def test_falls_back_to_title_case_label_for_everything_else(self):
        self.assertEqual(display_utils.friendly_field_label("submarket"), "Submarket")
        self.assertEqual(display_utils.friendly_field_label("special_features"), "Special Features")
        self.assertEqual(display_utils.friendly_field_label("brochure_link"), "Brochure Link")

    def test_underlying_field_name_never_appears_in_the_label(self):
        # A friendly label must never leak the raw internal snake_case name.
        for field in ("address_1", "size_sqft", "rent_pcm", "rent_psf", "floor_unit", "desks_min", "desks_max"):
            self.assertNotIn("_", display_utils.friendly_field_label(field))


class FormatFieldValueForDisplayTests(unittest.TestCase):
    def test_blank_values_render_as_an_em_dash(self):
        for value in (None, ""):
            self.assertEqual(display_utils.format_field_value_for_display("size_sqft", value), "—")

    def test_size_sqft_gets_a_comma_and_sq_ft_suffix(self):
        self.assertEqual(display_utils.format_field_value_for_display("size_sqft", 5028.0), "5,028 sq ft")
        self.assertEqual(display_utils.format_field_value_for_display("size_sqft", 1638), "1,638 sq ft")

    def test_rent_pcm_gets_a_pound_sign_and_no_decimals_when_whole(self):
        self.assertEqual(display_utils.format_field_value_for_display("rent_pcm", 49500.0), "£49,500")

    def test_rent_psf_keeps_decimals_when_not_a_whole_number(self):
        self.assertEqual(display_utils.format_field_value_for_display("rent_psf", 64.5732), "£64.57")

    def test_desks_fields_get_comma_grouping_as_plain_integers(self):
        self.assertEqual(display_utils.format_field_value_for_display("desks_max", 1200), "1,200")
        self.assertEqual(display_utils.format_field_value_for_display("desks_min", 52), "52")

    def test_other_fields_render_unchanged(self):
        self.assertEqual(display_utils.format_field_value_for_display("submarket", "Shoreditch"), "Shoreditch")
        self.assertEqual(display_utils.format_field_value_for_display("address_1", "34-37 Liverpool Street"), "34-37 Liverpool Street")

    def test_never_raises_on_an_unparseable_numeric_value(self):
        # A display nicety failing to apply must never crash the page.
        self.assertEqual(display_utils.format_field_value_for_display("size_sqft", "not a number"), "not a number")

    def test_underlying_value_is_never_mutated(self):
        # format_field_value_for_display must be purely read-only.
        value = 5028.0
        display_utils.format_field_value_for_display("size_sqft", value)
        self.assertEqual(value, 5028.0)


class CoercedNewValueTests(unittest.TestCase):
    def test_int_kind_coerces_to_a_real_int(self):
        result = display_utils.coerced_new_value(30.0, "int")
        self.assertEqual(result, 30)
        self.assertIsInstance(result, int)

    def test_float_kind_coerces_to_a_real_float(self):
        result = display_utils.coerced_new_value(1638, "float")
        self.assertEqual(result, 1638.0)
        self.assertIsInstance(result, float)

    def test_str_kind_passes_a_real_value_through_unchanged(self):
        self.assertEqual(display_utils.coerced_new_value("34-37 Liverpool Street", "str"), "34-37 Liverpool Street")

    def test_str_kind_empty_string_becomes_none(self):
        self.assertIsNone(display_utils.coerced_new_value("", "str"))

    def test_matches_what_render_new_value_input_would_return_by_default(self):
        # The whole point of this helper: byte-identical to the widget's
        # own default (untouched) return value - see its own docstring.
        # int/float default to 0/0.0 for a None new_val, mirroring st.
        # number_input's own default=0.0 fallback there.
        self.assertEqual(display_utils.coerced_new_value(None, "int"), 0)
        self.assertEqual(display_utils.coerced_new_value(None, "float"), 0.0)


class VisibleColumnsAlwaysHiddenTests(unittest.TestCase):
    """
    visible_columns' own ALWAYS_HIDDEN_COLUMNS filter - internal/
    traceability-only columns that never belong in front of Mark/Laurie on
    the review grid, regardless of value (see that list's own comment).
    brochure_link_broken is pipeline diagnostics (same reasoning as
    staging_writer.HIDDEN_COLUMNS, which hides it from the exported .xlsx
    for the identical reason) - this is the separate list governing the
    on-screen grid instead.
    """

    def test_brochure_link_broken_is_never_shown_on_the_review_grid(self):
        df = pd.DataFrame([{"building": "A", "brochure_link_broken": True}])
        self.assertNotIn("brochure_link_broken", display_utils.visible_columns(df))

    def test_source_file_and_property_id_are_never_shown_either(self):
        df = pd.DataFrame([{"building": "A", "source_file": "x.pdf", "property_id": "p1"}])
        visible = display_utils.visible_columns(df)
        self.assertNotIn("source_file", visible)
        self.assertNotIn("property_id", visible)

    def test_an_ordinary_column_is_unaffected(self):
        df = pd.DataFrame([{"building": "A", "brochure_link_broken": True}])
        self.assertIn("building", display_utils.visible_columns(df))


class WithBrochureLinkStatusTests(unittest.TestCase):
    """
    with_brochure_link_status - the on-screen-grid equivalent of staging_
    writer.write_rows_to_xlsx's own "Broken link" label swap, needed
    because st.column_config.LinkColumn can't vary display_text per row
    (confirmed against the installed Streamlit's own docs, not assumed -
    see that function's own docstring). brochure_link itself must never be
    touched by this - only the new synthetic column carries the signal.
    """

    def test_confirmed_broken_row_gets_the_label(self):
        df = pd.DataFrame([{"brochure_link": "https://example.com/dead.pdf", "brochure_link_broken": True}])
        result = display_utils.with_brochure_link_status(df)
        self.assertEqual(result[display_utils.BROCHURE_LINK_STATUS_COLUMN].iloc[0], "⚠️ Broken link")

    def test_working_link_gets_a_blank_status(self):
        df = pd.DataFrame([{"brochure_link": "https://example.com/fine.pdf", "brochure_link_broken": False}])
        result = display_utils.with_brochure_link_status(df)
        self.assertEqual(result[display_utils.BROCHURE_LINK_STATUS_COLUMN].iloc[0], "")

    def test_never_rechecked_link_gets_a_blank_status(self):
        df = pd.DataFrame([{"brochure_link": "https://example.com/unknown.pdf", "brochure_link_broken": None}])
        result = display_utils.with_brochure_link_status(df)
        self.assertEqual(result[display_utils.BROCHURE_LINK_STATUS_COLUMN].iloc[0], "")

    def test_label_matches_the_exported_xlsx_wording_exactly(self):
        # The whole point: the on-screen grid and the downloaded file must
        # never describe the same brochure_link_broken fact two different
        # ways - see BROCHURE_LINK_BROKEN_LABEL's own comment.
        from staging_writer import BROKEN_LINK_DISPLAY_TEXT
        self.assertIn(BROKEN_LINK_DISPLAY_TEXT, display_utils.BROCHURE_LINK_BROKEN_LABEL)

    def test_new_column_is_inserted_immediately_after_brochure_link(self):
        df = pd.DataFrame([{"building": "A", "brochure_link": "u", "floorplan_link": "f"}])
        df["brochure_link_broken"] = True
        result = display_utils.with_brochure_link_status(df)
        columns = list(result.columns)
        self.assertEqual(
            columns.index(display_utils.BROCHURE_LINK_STATUS_COLUMN), columns.index("brochure_link") + 1,
        )

    def test_absent_brochure_link_broken_is_a_complete_no_op(self):
        df = pd.DataFrame([{"building": "A", "brochure_link": "u"}])
        result = display_utils.with_brochure_link_status(df)
        self.assertIs(result, df)
        self.assertNotIn(display_utils.BROCHURE_LINK_STATUS_COLUMN, result.columns)

    def test_survives_visible_columns_narrowing(self):
        # Callers must run this BEFORE visible_columns (see this function's
        # own docstring) - confirms the synthetic column is never itself
        # treated as an ALWAYS_HIDDEN_COLUMNS entry.
        df = pd.DataFrame([{"building": "A", "brochure_link": "u", "brochure_link_broken": True}])
        result = display_utils.with_brochure_link_status(df)
        self.assertIn(display_utils.BROCHURE_LINK_STATUS_COLUMN, display_utils.visible_columns(result))

    def test_original_dataframe_is_never_mutated(self):
        df = pd.DataFrame([{"brochure_link": "u", "brochure_link_broken": True}])
        display_utils.with_brochure_link_status(df)
        self.assertNotIn(display_utils.BROCHURE_LINK_STATUS_COLUMN, df.columns)


class LinkStatusColumnConfigTests(unittest.TestCase):
    def test_config_present_when_column_present(self):
        df = pd.DataFrame([{display_utils.BROCHURE_LINK_STATUS_COLUMN: "⚠️ Broken link"}])
        config = display_utils.link_status_column_config(df)
        self.assertIn(display_utils.BROCHURE_LINK_STATUS_COLUMN, config)
        self.assertTrue(config[display_utils.BROCHURE_LINK_STATUS_COLUMN]["disabled"])

    def test_no_op_when_column_absent(self):
        df = pd.DataFrame([{"building": "A"}])
        self.assertEqual(display_utils.link_status_column_config(df), {})


class RestoreHiddenColumnsDropsSyntheticStatusColumnTests(unittest.TestCase):
    """
    The Export page relies on restore_hidden_columns to drop with_brochure_
    link_status's synthetic column automatically (it reindexes to original_
    df's own columns - see that function's own docstring) rather than
    needing its own explicit strip step before dataframe_to_listing_rows.
    """

    def test_synthetic_status_column_never_survives_restore(self):
        # Mirrors the REAL pages/3_Export.py pipeline exactly: with_brochure_
        # link_status runs first, then visible_columns narrows the frame
        # actually handed to st.data_editor (excluding brochure_link_broken,
        # per ALWAYS_HIDDEN_COLUMNS, but keeping the new synthetic column) -
        # restore_hidden_columns only reindexes to original_df's own columns
        # when something was ACTUALLY missing from edited_df to restore;
        # skipping the visible_columns narrowing step (as an earlier,
        # incorrect version of this test did) leaves brochure_link_broken
        # still present in "edited_df", which short-circuits that reindex
        # entirely and would wrongly report this as passing either way.
        original_df = pd.DataFrame([{"brochure_link": "u", "brochure_link_broken": True}])
        with_status = display_utils.with_brochure_link_status(original_df)
        edited_df = with_status[display_utils.visible_columns(with_status)]

        restored = display_utils.restore_hidden_columns(edited_df, original_df)

        self.assertNotIn(display_utils.BROCHURE_LINK_STATUS_COLUMN, restored.columns)
        self.assertEqual(list(restored.columns), list(original_df.columns))


if __name__ == "__main__":
    unittest.main()
