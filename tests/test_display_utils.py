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


if __name__ == "__main__":
    unittest.main()
