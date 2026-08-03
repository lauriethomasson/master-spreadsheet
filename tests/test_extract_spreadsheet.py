"""
Regression tests for extract_spreadsheet.py - the header-mapping path for
uploaded provider .xlsx/.csv spreadsheets (see the module docstring for the
overall flow: read_spreadsheet -> header_hash -> suggest_mapping/confirm ->
build_rows).

Header strings used here for the "real UNION format" cases are taken
verbatim from the actual current-format UNION export files (checked
directly, not guessed) - not committed into this repo (real provider data
kept out of git-tracked fixtures, same principle as never using the real
data/master.xlsx in tests), just reproduced as plain header strings so the
synonym table is tested against the real thing it needs to handle.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_extract_spreadsheet -v
"""

import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract_spreadsheet

REAL_UNION_HEADERS = [
    "External Ref", "Assigned Agents", "Property Address 1", "Property Postcode",
    "Lat", "Lng", "For Sale", "To Let", "Area", "Building", "Floor/Unit",
    "Size (sq ft)", "Desks (max)", "Marketing Price (Based on Min Term) PCM",
    "Marketing Price (Based on Min Term) PSF", "Brochure PDF", "Min. Term",
    "Special Features", "State of Space", "Legal Structure", "Broker Fee",
    "Contacts", "Floor Plan", "High Res Images",
]


class HeaderHashTests(unittest.TestCase):
    def test_same_headers_same_order_hash_identically(self):
        a = extract_spreadsheet.header_hash(["Building", "Floor/Unit"])
        b = extract_spreadsheet.header_hash(["Building", "Floor/Unit"])
        self.assertEqual(a, b)

    def test_different_order_hashes_differently(self):
        a = extract_spreadsheet.header_hash(["Building", "Floor/Unit"])
        b = extract_spreadsheet.header_hash(["Floor/Unit", "Building"])
        self.assertNotEqual(a, b)


class ProviderFilenameInferenceTests(unittest.TestCase):
    def test_kitts_external_availability_export(self):
        self.assertEqual(
            extract_spreadsheet.infer_provider_from_filename(
                "Kitt's Availability (External).xlsx"
            ),
            "Kitt's",
        )

    def test_workplace_plus_dated_availability_export(self):
        self.assertEqual(
            extract_spreadsheet.infer_provider_from_filename(
                "Workplace Plus - Availability 14th July.xlsx"
            ),
            "Workplace Plus",
        )

    def test_knotel_underscore_export(self):
        self.assertEqual(
            extract_spreadsheet.infer_provider_from_filename(
                "Knotel_Availability_30_06_2026.xlsx"
            ),
            "Knotel",
        )

    def test_union_location_and_date_export(self):
        self.assertEqual(
            extract_spreadsheet.infer_provider_from_filename(
                "UNION - London Bridge & Southbank_2026-07-14 (original).xlsx"
            ),
            "UNION",
        )

    def test_different_headers_hash_differently(self):
        a = extract_spreadsheet.header_hash(["Building"])
        b = extract_spreadsheet.header_hash(["Building "])
        self.assertNotEqual(a, b)


class SuggestMappingTests(unittest.TestCase):
    def test_maps_the_real_union_header_format(self):
        guess = extract_spreadsheet.suggest_mapping(REAL_UNION_HEADERS)

        self.assertEqual(guess["Building"], "building")
        self.assertEqual(guess["Floor/Unit"], "floor_unit")
        self.assertEqual(guess["Size (sq ft)"], "size_sqft")
        self.assertEqual(guess["Desks (max)"], "desks_max")
        self.assertEqual(guess["Marketing Price (Based on Min Term) PCM"], "rent_pcm")
        self.assertEqual(guess["Marketing Price (Based on Min Term) PSF"], "rent_psf")
        self.assertEqual(guess["Brochure PDF"], "brochure_link")
        self.assertEqual(guess["Special Features"], "special_features")
        self.assertEqual(guess["State of Space"], "state_of_space")
        self.assertEqual(guess["Contacts"], "contacts")
        self.assertEqual(guess["Property Address 1"], "address_1")
        self.assertEqual(guess["Property Postcode"], "postcode")
        self.assertEqual(guess["Lat"], "lat")
        self.assertEqual(guess["Lng"], "lng")
        self.assertEqual(guess["Area"], "submarket")
        self.assertEqual(guess["External Ref"], "internal_ref")
        self.assertEqual(guess["Assigned Agents"], "provider")

    def test_columns_with_no_known_synonym_are_left_unmapped(self):
        guess = extract_spreadsheet.suggest_mapping(REAL_UNION_HEADERS)

        # These have no ListingRow equivalent at all - must never be forced
        # onto some unrelated field.
        for header in ("For Sale", "To Let", "Min. Term", "Legal Structure", "Broker Fee", "Floor Plan", "High Res Images"):
            self.assertIsNone(guess[header], f"{header!r} should have no guess")

    def test_our_own_staging_xlsx_header_format_round_trips(self):
        # staging_writer.write_rows_to_xlsx's own Title-Case header labels
        # (e.g. "Size Sqft" for the size_sqft field) should map straight
        # back onto themselves via the field-name/title-case auto-synonym,
        # with no extra synonym table entry needed.
        guess = extract_spreadsheet.suggest_mapping(["Building", "Floor Unit", "Size Sqft", "Contacts"])
        self.assertEqual(guess["Building"], "building")
        self.assertEqual(guess["Size Sqft"], "size_sqft")
        self.assertEqual(guess["Contacts"], "contacts")

    def test_never_maps_two_headers_onto_the_same_field(self):
        # Both "Brochure PDF" and "Link to File" are known brochure_link
        # synonyms - only the first (in header order) should win; the
        # second must be left for a human to resolve rather than silently
        # dropped or double-mapped.
        guess = extract_spreadsheet.suggest_mapping(["Brochure PDF", "Link to File"])
        self.assertEqual(guess["Brochure PDF"], "brochure_link")
        self.assertIsNone(guess["Link to File"])

    def test_maps_the_real_kitts_brochure_link_header_and_close_variants(self):
        # "Link to Brochure" is the real header used by the actual Kitt's
        # Availability (External).xlsx export - previously unmapped,
        # defaulting to "(ignore)" in the confirm-mapping UI on first
        # upload of that format.
        guess = extract_spreadsheet.suggest_mapping(["Link to Brochure", "Brochure Link", "Link to Brochure PDF"])
        self.assertEqual(guess["Link to Brochure"], "brochure_link")
        self.assertEqual(guess["Brochure Link"], None)  # already used by "Link to Brochure" above
        self.assertEqual(guess["Link to Brochure PDF"], None)  # same

    def test_brochure_link_close_variant_maps_alone(self):
        for header in ("Link to Brochure", "Brochure Link", "Link to Brochure PDF"):
            with self.subTest(header=header):
                guess = extract_spreadsheet.suggest_mapping([header])
                self.assertEqual(guess[header], "brochure_link")

    def test_maps_the_real_kitts_key_features_header(self):
        # "Key Features" (note: trailing space in the real header cell,
        # stripped by normalize_key) is the real column used by the actual
        # Kitt's Availability (External).xlsx export for descriptive
        # amenity/notes text - the same concept as special_features, just
        # different wording from "Special Features".
        guess = extract_spreadsheet.suggest_mapping(["Key Features "])
        self.assertEqual(guess["Key Features "], "special_features")

    def test_real_kitts_header_set_maps_every_genuinely_mappable_column_and_nothing_else(self):
        # The full real header set from Kitt's Availability (External).xlsx
        # - confirms the confirm-mapping UI would show a fully correct
        # mapping on first upload (10 real fields, nothing guessed wrong)
        # with every genuinely unmappable column still correctly left for
        # a human to see as "(ignore)", not silently guessed.
        headers = [
            "Area", "Building", "Floor/Unit", "Size \n(sq ft)", "Desks \n(max)",
            "Marketing Price \n(Based on Min Term)\nPCM", "Marketing Price \n(Based on Min Term)\nPSF",
            "Link to Brochure", "Min. term", "Key Features ", "State of Space", "Legal Structure",
            "Broker Fee", "Marketing Permission", "Commercial Model", "Patch?",
            "Unit Lead - Viewings go to this person initially", "Unit Support - back up cover for viewings",
            "Who Onboarded?", "Landlord/Agent Onboarded", "Other Info", "Access Information",
            "Link to Floorplan", "Link to High Res Images", "Matterport Link",
        ]
        guess = extract_spreadsheet.suggest_mapping(headers)

        expected_mapped = {
            "Area": "submarket",
            "Building": "building",
            "Floor/Unit": "floor_unit",
            "Size \n(sq ft)": "size_sqft",
            "Desks \n(max)": "desks_max",
            "Marketing Price \n(Based on Min Term)\nPCM": "rent_pcm",
            "Marketing Price \n(Based on Min Term)\nPSF": "rent_psf",
            "Link to Brochure": "brochure_link",
            "Key Features ": "special_features",
            "State of Space": "state_of_space",
        }
        for header, field in expected_mapped.items():
            self.assertEqual(guess[header], field, f"{header!r} should map to {field!r}")

        for header in set(headers) - set(expected_mapped):
            self.assertIsNone(guess[header], f"{header!r} has no real field and must not be guessed")

    def test_never_suggests_an_unmappable_field(self):
        guess = extract_spreadsheet.suggest_mapping(["Property Id", "Source File"])
        self.assertIsNone(guess["Property Id"])
        self.assertIsNone(guess["Source File"])


class BuildRowsTests(unittest.TestCase):
    def test_applies_mapping_and_sets_source_file(self):
        df = pd.DataFrame([
            {"Building": "40 New Bond Street", "Floor/Unit": "3rd Floor", "Size (sq ft)": 5000, "Ignore Me": "x"},
        ])
        mapping = {"Building": "building", "Floor/Unit": "floor_unit", "Size (sq ft)": "size_sqft", "Ignore Me": None}

        rows = extract_spreadsheet.build_rows(df, mapping, source_file="union_export.xlsx")

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.building, "40 New Bond Street")
        self.assertEqual(row.floor_unit, "3rd Floor")
        self.assertEqual(row.size_sqft, 5000.0)
        self.assertEqual(row.source_file, "union_export.xlsx")

    def test_all_blank_row_is_skipped(self):
        df = pd.DataFrame([
            {"Building": "City Tower", "Floor/Unit": "5th Floor"},
            {"Building": None, "Floor/Unit": None},
        ])
        mapping = {"Building": "building", "Floor/Unit": "floor_unit"}

        rows = extract_spreadsheet.build_rows(df, mapping, source_file="a.xlsx")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].building, "City Tower")

    def test_non_numeric_placeholder_in_a_numeric_column_becomes_none_not_a_crash(self):
        # Grounded in a real current-format UNION file: its Lat/Lng columns
        # hold the literal text "Needs manual lookup" for rows the provider
        # hasn't geocoded yet themselves - must not blow up the entire
        # file's extraction over one placeholder cell in one column.
        df = pd.DataFrame([
            {"Building": "111 Wardour Street", "Lat": "Needs manual lookup", "Lng": "Needs manual lookup"},
        ])
        mapping = {"Building": "building", "Lat": "lat", "Lng": "lng"}

        rows = extract_spreadsheet.build_rows(df, mapping, source_file="union.xlsx")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].building, "111 Wardour Street")
        self.assertIsNone(rows[0].lat)
        self.assertIsNone(rows[0].lng)

    def test_real_numeric_lat_lng_still_come_through_correctly(self):
        df = pd.DataFrame([{"Building": "City Tower", "Lat": 51.5, "Lng": -0.1}])
        mapping = {"Building": "building", "Lat": "lat", "Lng": "lng"}

        rows = extract_spreadsheet.build_rows(df, mapping, source_file="union.xlsx")

        self.assertEqual(rows[0].lat, 51.5)
        self.assertEqual(rows[0].lng, -0.1)

    def test_unmapped_columns_never_reach_listingrow(self):
        # A column mapped to None (or simply absent from the mapping dict)
        # must never surface as an unexpected kwarg - ListingRow's own
        # extra="ignore" would tolerate it anyway, but build_rows should
        # not even pass it through.
        df = pd.DataFrame([{"Building": "City Tower", "Legal Structure": "Freehold"}])
        mapping = {"Building": "building", "Legal Structure": None}

        rows = extract_spreadsheet.build_rows(df, mapping, source_file="a.xlsx")

        self.assertEqual(rows[0].building, "City Tower")


class ParseXludfFallbackTests(unittest.TestCase):
    """
    Formula strings here are taken verbatim from a real Google Sheets/
    IMPORTRANGE .xlsx export (Kitt's Availability (External).xlsx, not
    committed into this repo) - checked directly, not guessed.
    """

    def test_quoted_string_fallback_with_nested_importrange_call(self):
        formula = (
            '=IFERROR(__xludf.DUMMYFUNCTION("IMPORTRANGE(""https://docs.google.com/spreadsheets/d/abc/edit#gid=1"",'
            '""\'Availability\'!A:ab"")"),"Area")'
        )
        self.assertEqual(extract_spreadsheet._parse_xludf_fallback(formula), "Area")

    def test_quoted_string_fallback_with_embedded_newline(self):
        formula = '=IFERROR(__xludf.DUMMYFUNCTION("""COMPUTED_VALUE"""),"Size \n(sq ft)")'
        self.assertEqual(extract_spreadsheet._parse_xludf_fallback(formula), "Size \n(sq ft)")

    def test_numeric_fallback(self):
        formula = '=IFERROR(__xludf.DUMMYFUNCTION("""COMPUTED_VALUE"""),759.0)'
        self.assertEqual(extract_spreadsheet._parse_xludf_fallback(formula), 759.0)

    def test_quoted_string_fallback_with_a_pound_sign(self):
        formula = '=IFERROR(__xludf.DUMMYFUNCTION("""COMPUTED_VALUE"""),"£296")'
        self.assertEqual(extract_spreadsheet._parse_xludf_fallback(formula), "£296")

    def test_not_a_formula_at_all_returns_none(self):
        self.assertIsNone(extract_spreadsheet._parse_xludf_fallback("just plain text"))
        self.assertIsNone(extract_spreadsheet._parse_xludf_fallback(None))
        self.assertIsNone(extract_spreadsheet._parse_xludf_fallback(759.0))

    def test_a_different_kind_of_formula_returns_none_rather_than_guessing(self):
        self.assertIsNone(extract_spreadsheet._parse_xludf_fallback("=SUM(A1:A10)"))


class ResolveCellValueTests(unittest.TestCase):
    def _cell(self, value):
        cell = MagicMock()
        cell.value = value
        return cell

    def test_prefers_the_cached_value_when_present(self):
        value_cell = self._cell("Area")
        formula_cell = self._cell('=IFERROR(__xludf.DUMMYFUNCTION("..."),"Area")')
        self.assertEqual(extract_spreadsheet._resolve_cell_value(value_cell, formula_cell), "Area")

    def test_falls_back_to_the_formula_when_the_cache_is_missing(self):
        value_cell = self._cell(None)
        formula_cell = self._cell('=IFERROR(__xludf.DUMMYFUNCTION("""COMPUTED_VALUE"""),"Building")')
        self.assertEqual(extract_spreadsheet._resolve_cell_value(value_cell, formula_cell), "Building")

    def test_a_genuinely_blank_cell_stays_none(self):
        value_cell = self._cell(None)
        formula_cell = self._cell(None)
        self.assertIsNone(extract_spreadsheet._resolve_cell_value(value_cell, formula_cell))


class ReadSpreadsheetXludfIntegrationTests(unittest.TestCase):
    """
    Builds a workbook via openpyxl containing REAL cached-value-missing
    formula cells (assigning a formula string to a cell and saving it
    without ever letting a real spreadsheet engine compute/cache a result
    is exactly what "no cached value available" means) - end-to-end proof
    that read_spreadsheet resolves such a file's real header/data text
    correctly, not just the underlying helper functions in isolation.
    """

    def _build_xlsx_bytes(self) -> bytes:
        wb = Workbook()
        ws = wb.active
        ws["A1"] = '=IFERROR(__xludf.DUMMYFUNCTION("IMPORTRANGE(""url"",""range"")"),"Building")'
        ws["B1"] = '=IFERROR(__xludf.DUMMYFUNCTION("""COMPUTED_VALUE"""),"Size \n(sq ft)")'
        ws["A2"] = '=IFERROR(__xludf.DUMMYFUNCTION("""COMPUTED_VALUE"""),"28 Bruton Street")'
        ws["B2"] = '=IFERROR(__xludf.DUMMYFUNCTION("""COMPUTED_VALUE"""),759.0)'
        buffer = BytesIO()
        wb.save(buffer)
        return buffer.getvalue()

    def test_headers_resolve_to_real_text_not_formula_text(self):
        df = extract_spreadsheet.read_spreadsheet(self._build_xlsx_bytes(), ".xlsx")
        self.assertEqual(list(df.columns), ["Building", "Size \n(sq ft)"])

    def test_data_rows_resolve_to_real_values_not_formula_text(self):
        df = extract_spreadsheet.read_spreadsheet(self._build_xlsx_bytes(), ".xlsx")
        self.assertEqual(df.iloc[0]["Building"], "28 Bruton Street")
        self.assertEqual(df.iloc[0]["Size \n(sq ft)"], 759.0)

    def test_a_plain_non_formula_workbook_is_completely_unaffected(self):
        wb = Workbook()
        ws = wb.active
        ws["A1"] = "Building"
        ws["B1"] = "Size (sq ft)"
        ws["A2"] = "28 Bruton Street"
        ws["B2"] = 759
        buffer = BytesIO()
        wb.save(buffer)

        df = extract_spreadsheet.read_spreadsheet(buffer.getvalue(), ".xlsx")

        self.assertEqual(list(df.columns), ["Building", "Size (sq ft)"])
        self.assertEqual(df.iloc[0]["Building"], "28 Bruton Street")
        self.assertEqual(df.iloc[0]["Size (sq ft)"], 759)


if __name__ == "__main__":
    unittest.main()
