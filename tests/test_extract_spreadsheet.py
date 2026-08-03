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
from pathlib import Path

import pandas as pd

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


if __name__ == "__main__":
    unittest.main()
