"""
Regression tests for app.py's fill_missing_provider - the post-extraction,
source-agnostic pass that fills provider/internal_ref from a filename-
derived guess (extract_spreadsheet.guess_provider_name), but ONLY for
spreadsheet-sourced rows (apply_filename_guess=True) - never for PDF/email,
where a blank provider is frequently Gemini's own deliberate, meaningful
answer (a landlord-direct brochure with no presenting agent), not a missed
extraction. Applying a filename guess there would fabricate an agent for a
listing that genuinely has none.

Importing app.py directly runs Streamlit in "bare mode" (no real script run
context) - noisy (missing-ScriptRunContext warnings on stderr) but
functionally harmless; every st.* call it makes at import time (file_uploader,
etc.) is a no-op without a real runtime, and fill_missing_provider itself
makes no Streamlit calls at all.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app
from schema import ListingRow

# The real UNION "by-area" header set (see tests/test_extract_spreadsheet.py's
# REAL_UNION_BY_AREA_HEADERS - reproduced here rather than imported, since
# this file's own real-header comment above explains why real provider
# header text lives directly in test files rather than committed fixtures).
_REAL_UNION_BY_AREA_HEADERS = [
    "Clerkenwell & Farringdon", "Floor", "Current spec", "Size sq.ft",
    "Minimum Term", "Monthly Rate", "Price p/sq.ft", "Brochure",
]


class FillMissingProviderTests(unittest.TestCase):
    def test_spreadsheet_source_gets_the_filename_guess(self):
        # The real Kitt's Availability format: no column states a
        # provider at all, so the filename is the only signal available.
        rows = [ListingRow(building="28 Bruton Street")]

        app.fill_missing_provider(rows, "Kitt's Availability (External).xlsx", apply_filename_guess=True)

        self.assertEqual(rows[0].provider, "Kitt's")
        self.assertEqual(rows[0].internal_ref, "Kitt's")

    def test_spreadsheet_source_never_overrides_a_real_mapped_provider(self):
        rows = [ListingRow(building="City Tower", provider="Breezblok")]

        app.fill_missing_provider(rows, "Breezblok_GPE.xlsx", apply_filename_guess=True)

        self.assertEqual(rows[0].provider, "Breezblok")

    def test_pdf_source_with_no_provider_stays_none_not_a_filename_guess(self):
        # Grounded in a real landlord-direct brochure (40 New Bond Street) -
        # Gemini genuinely extracts no provider/contacts for it, since
        # there's no presenting agent at all (see schema.ExtractedFields'
        # own provider/internal_ref comments). apply_filename_guess=False
        # for PDF/email must leave that alone, not fabricate an agent name
        # from the filename.
        rows = [ListingRow(building="40 New Bond Street", provider=None, internal_ref=None)]

        app.fill_missing_provider(rows, "40 New Bond Street Brochure.pdf", apply_filename_guess=False)

        self.assertIsNone(rows[0].provider)
        self.assertIsNone(rows[0].internal_ref)

    def test_email_source_with_no_provider_also_stays_none(self):
        rows = [ListingRow(building="City Fringe", provider=None)]

        app.fill_missing_provider(rows, "Fw__Property_of_The_Week_-_City_Fringe.eml", apply_filename_guess=False)

        self.assertIsNone(rows[0].provider)

    def test_pdf_source_with_a_real_extracted_provider_is_left_untouched(self):
        # apply_filename_guess=False must not touch ANY row's fields, not
        # just leave a blank one blank - a real extracted provider must
        # obviously survive too.
        rows = [ListingRow(building="City Tower", provider="Savills", internal_ref="Savills")]

        app.fill_missing_provider(rows, "City Tower Brochure.pdf", apply_filename_guess=False)

        self.assertEqual(rows[0].provider, "Savills")
        self.assertEqual(rows[0].internal_ref, "Savills")


class FillMissingAddressFromBuildingTests(unittest.TestCase):
    def test_spreadsheet_source_copies_an_address_like_building_value(self):
        # Real Kitt's Availability values - both a plain address and a
        # "Building Name, real address" compound, copied verbatim either
        # way (never split/cleaned - see the function's own docstring).
        rows = [
            ListingRow(building="28 Bruton Street"),
            ListingRow(building="Bridge House, 22 Newman Street"),
        ]

        app.fill_missing_address_from_building(rows, apply_building_fallback=True)

        self.assertEqual(rows[0].address_1, "28 Bruton Street")
        self.assertEqual(rows[1].address_1, "Bridge House, 22 Newman Street")

    def test_spreadsheet_source_leaves_a_name_only_building_unfilled(self):
        # Real Kitt's Availability values with no digit at all - not
        # address-like, must stay blank exactly as today.
        rows = [ListingRow(building="Albion Mills"), ListingRow(building="Flat Iron")]

        app.fill_missing_address_from_building(rows, apply_building_fallback=True)

        self.assertIsNone(rows[0].address_1)
        self.assertIsNone(rows[1].address_1)

    def test_never_overrides_a_real_already_populated_address_1(self):
        # Whether from a mapped column or from geocode_rows' own lookup -
        # this is purely a last-resort fallback for whatever's still
        # missing, never a source of truth in its own right.
        rows = [ListingRow(building="28 Bruton Street", address_1="1 Real Street")]

        app.fill_missing_address_from_building(rows, apply_building_fallback=True)

        self.assertEqual(rows[0].address_1, "1 Real Street")

    def test_pdf_source_with_an_address_like_building_is_not_filled(self):
        # A blank address_1 on a PDF/email row is frequently Gemini's own
        # deliberate answer (see schema.ExtractedFields' address_1 comment)
        # - apply_building_fallback=False must leave it alone even when
        # building would otherwise qualify.
        rows = [ListingRow(building="28 Bruton Street", address_1=None)]

        app.fill_missing_address_from_building(rows, apply_building_fallback=False)

        self.assertIsNone(rows[0].address_1)

    def test_a_building_with_no_digit_at_all_is_simply_skipped_not_a_crash(self):
        rows = [ListingRow(building="The Pavilion")]

        app.fill_missing_address_from_building(rows, apply_building_fallback=True)

        self.assertIsNone(rows[0].address_1)


class FillMissingSubmarketFromStructuralHeaderTests(unittest.TestCase):
    def test_real_union_by_area_format_fills_submarket_from_its_own_header(self):
        # The real rows this was built for - Google's own geocoded-
        # neighbourhood fallback (see geocode.py) has no data at all for
        # this specific area, so this file-level constant is the only
        # reliable source for submarket here.
        rows = [
            ListingRow(building="55 Goswell Road"),
            ListingRow(building="13 Northburgh Street"),
        ]

        app.fill_missing_submarket_from_structural_header(
            rows, _REAL_UNION_BY_AREA_HEADERS,
            "UNION - Availability - June 26 - Clerkenwell & Farringdon.xlsx",
        )

        self.assertEqual(rows[0].submarket, "Clerkenwell & Farringdon")
        self.assertEqual(rows[1].submarket, "Clerkenwell & Farringdon")

    def test_never_overrides_a_genuinely_extracted_submarket(self):
        rows = [ListingRow(building="55 Goswell Road", submarket="Farringdon")]

        app.fill_missing_submarket_from_structural_header(
            rows, _REAL_UNION_BY_AREA_HEADERS,
            "UNION - Availability - June 26 - Clerkenwell & Farringdon.xlsx",
        )

        self.assertEqual(rows[0].submarket, "Farringdon")

    def test_other_providers_formats_are_unaffected(self):
        # Kitt's already has a real Area column mapped straight through -
        # this fallback must be a complete no-op for it, not just harmless.
        kitts_headers = ["Area", "Building", "Floor/Unit"]
        rows = [ListingRow(building="28 Bruton Street", submarket=None)]

        app.fill_missing_submarket_from_structural_header(
            rows, kitts_headers, "Kitt's Availability (External).xlsx"
        )

        self.assertIsNone(rows[0].submarket)

    def test_the_full_union_format_with_a_real_building_header_is_unaffected(self):
        # Has a genuine "Area" column of its own (mapped separately by
        # suggest_mapping) - this structural fallback must never fire for
        # it at all, since _structural_building_header only matches the
        # by-area format's fixed fingerprint, not this one.
        full_union_headers = ["Area", "Building", "Floor/Unit", "Brochure PDF"]
        rows = [ListingRow(building="16 Dufour's Place", submarket=None)]

        app.fill_missing_submarket_from_structural_header(
            rows, full_union_headers, "UNION - Shoreditch_2026-07-14.xlsx"
        )

        self.assertIsNone(rows[0].submarket)


if __name__ == "__main__":
    unittest.main()
