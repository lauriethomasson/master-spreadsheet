"""
Regression tests for staging_writer.py's Title Case header-row display
(title_case_label) - the header row written to master.xlsx/staging/
export.xlsx should read as human-friendly labels ("Internal Ref") while the
underlying column identity used for every round-trip read stays the real
snake_case field name ("internal_ref"), since master_merge.py and friends
match/edit fields by that exact name.

Run with:

    .venv\\Scripts\\python.exe -m unittest tests.test_staging_writer -v
"""

import sys
import unittest
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema import ListingRow
from staging_writer import read_xlsx_with_hyperlinks, title_case_label, write_rows_to_xlsx


class TitleCaseLabelTests(unittest.TestCase):
    def test_documented_examples(self):
        self.assertEqual(title_case_label("internal_ref"), "Internal Ref")
        self.assertEqual(title_case_label("brochure_link"), "Brochure Link")
        self.assertEqual(title_case_label("desks_min"), "Desks Min")
        self.assertEqual(title_case_label("size_sqft"), "Size Sqft")

    def test_single_word_field(self):
        self.assertEqual(title_case_label("building"), "Building")


class WriteRowsToXlsxHeaderTests(unittest.TestCase):
    def _write_and_load_workbook(self, rows):
        buffer = BytesIO()
        write_rows_to_xlsx(rows, buffer)
        buffer.seek(0)
        return load_workbook(buffer)

    def test_header_row_is_title_case(self):
        wb = self._write_and_load_workbook([ListingRow(building="A", provider="P1")])
        ws = wb.active
        header_values = [cell.value for cell in ws[1]]

        self.assertIn("Internal Ref", header_values)
        self.assertIn("Brochure Link", header_values)
        self.assertIn("Desks Min", header_values)
        self.assertIn("Size Sqft", header_values)
        # None of the raw snake_case names should appear as header text.
        self.assertNotIn("internal_ref", header_values)
        self.assertNotIn("brochure_link", header_values)

    def test_header_order_still_matches_schema_field_order(self):
        # read_xlsx_with_hyperlinks relies on this positional match, not on
        # parsing the (now cosmetic) header text - see its docstring.
        wb = self._write_and_load_workbook([ListingRow(building="A", provider="P1")])
        ws = wb.active
        header_values = [cell.value for cell in ws[1]]
        expected = [title_case_label(f) for f in ListingRow.model_fields.keys()]

        self.assertEqual(header_values, expected)


class RoundTripTests(unittest.TestCase):
    """The critical invariant: Title Case is display-only - reading a
    written file back must still yield real snake_case column names and
    unchanged values, or every downstream field lookup breaks."""

    def _round_trip(self, rows):
        buffer = BytesIO()
        write_rows_to_xlsx(rows, buffer)
        return read_xlsx_with_hyperlinks(buffer.getvalue())

    def test_column_names_are_snake_case_not_title_case(self):
        df = self._round_trip([ListingRow(building="A", provider="P1")])

        self.assertIn("internal_ref", df.columns)
        self.assertIn("brochure_link", df.columns)
        self.assertNotIn("Internal Ref", df.columns)
        self.assertNotIn("Brochure Link", df.columns)

    def test_column_names_match_listingrow_fields_exactly(self):
        df = self._round_trip([ListingRow(building="A", provider="P1")])
        self.assertEqual(list(df.columns), list(ListingRow.model_fields.keys()))

    def test_values_round_trip_correctly(self):
        row = ListingRow(building="40 New Bond Street", provider="Workplace Plus", size_sqft=5000.0)
        df = self._round_trip([row])

        self.assertEqual(df.iloc[0]["building"], "40 New Bond Street")
        self.assertEqual(df.iloc[0]["provider"], "Workplace Plus")
        self.assertEqual(df.iloc[0]["size_sqft"], 5000.0)

    def test_hyperlink_column_still_round_trips_the_real_url_not_display_text(self):
        row = ListingRow(building="A", provider="P1", brochure_link="https://example.com/brochure.pdf")
        df = self._round_trip([row])

        self.assertEqual(df.iloc[0]["brochure_link"], "https://example.com/brochure.pdf")

    def test_hidden_columns_still_present_in_the_round_tripped_data(self):
        # Hidden in Excel (column width), never dropped from the data.
        row = ListingRow(building="A", provider="P1", property_id="prop-123", source_file="a.pdf")
        df = self._round_trip([row])

        self.assertEqual(df.iloc[0]["property_id"], "prop-123")
        self.assertEqual(df.iloc[0]["source_file"], "a.pdf")


class LegacyColumnCompatibilityTests(unittest.TestCase):
    """Regression coverage for a real bug found while verifying this exact
    scenario against the real data/master.xlsx: that file predates the
    property_id field (inserted mid-schema, not at the end) AND still has
    the six now-removed range columns (which sat between rent_psf and
    brochure_link, not at the end either) - reading columns by fixed
    position against the CURRENT schema silently misaligned every column
    from "lat" onward (property_id's slot) into the WRONG field, and in
    this test's case raises a pydantic ValidationError rather than reading
    correctly. read_xlsx_with_hyperlinks must read each column by its own
    header text instead, immune to reordering/missing/extra columns."""

    # The real, historical column order/header text of data/master.xlsx -
    # no property_id, desks_max before desks_min, and the six now-removed
    # range columns sitting in the middle rather than the end. Snake_case
    # header text (this predates the Title Case display feature too) -
    # _label_to_field_name must be a no-op round-trip for text that's
    # already snake_case, not just for "Title Case" text.
    _LEGACY_HEADERS = [
        "internal_ref", "provider", "address_1", "postcode", "source_file", "lat", "lng", "submarket",
        "building", "floor_unit", "size_sqft", "desks_max", "desks_min", "size_sqft_min", "size_sqft_max",
        "rent_pcm", "rent_psf", "rent_psf_min", "rent_psf_max", "rent_pcm_min", "rent_pcm_max",
        "brochure_link", "special_features", "state_of_space", "contacts",
    ]

    def _legacy_workbook_bytes(self) -> bytes:
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(self._LEGACY_HEADERS)
        row = {
            "internal_ref": "Breezblok", "provider": "Breezblok", "building": "John Stow House",
            "floor_unit": "Office 302", "size_sqft": 1750, "desks_max": 32, "rent_pcm": 18000,
            "rent_psf": 123.43, "lat": 51.5147, "lng": -0.0785, "submarket": "City of London",
        }
        ws.append([row.get(h) for h in self._LEGACY_HEADERS])
        buffer = BytesIO()
        wb.save(buffer)
        return buffer.getvalue()

    def test_reading_a_legacy_reordered_file_does_not_crash(self):
        df = read_xlsx_with_hyperlinks(self._legacy_workbook_bytes())
        self.assertEqual(len(df), 1)

    def test_legacy_columns_land_in_their_correct_field_not_shifted(self):
        # The actual bug: a fixed-position read put "lat"'s value into
        # "property_id", "submarket"'s value into "lng", etc.
        df = read_xlsx_with_hyperlinks(self._legacy_workbook_bytes())
        row = df.iloc[0]

        self.assertEqual(row["building"], "John Stow House")
        self.assertEqual(row["floor_unit"], "Office 302")
        self.assertEqual(row["size_sqft"], 1750)
        self.assertEqual(row["lat"], 51.5147)
        self.assertEqual(row["submarket"], "City of London")
        self.assertEqual(row["rent_psf"], 123.43)

    def test_legacy_removed_range_columns_are_dropped_by_listingrow_construction(self):
        # read_xlsx_with_hyperlinks itself is a generic xlsx->DataFrame
        # reader - it correctly reads "size_sqft_min" back as a real column
        # (the file genuinely has it) rather than guessing which fields are
        # "current schema". Dropping fields ListingRow no longer has is
        # ListingRow's job (pydantic's default extra="ignore"), exercised
        # via dataframe_to_listing_rows here.
        from storage.file_store import dataframe_to_listing_rows

        df = read_xlsx_with_hyperlinks(self._legacy_workbook_bytes())
        self.assertIn("size_sqft_min", df.columns)  # genuinely present in the file - read correctly

        rows = dataframe_to_listing_rows(df)
        for removed_field in ("size_sqft_min", "size_sqft_max", "rent_psf_min", "rent_psf_max", "rent_pcm_min", "rent_pcm_max"):
            self.assertFalse(hasattr(rows[0], removed_field))

    def test_legacy_row_missing_property_id_entirely_converts_cleanly(self):
        # No property_id column at all in this historical shape - must not
        # KeyError, and the resulting ListingRow just gets property_id=None.
        from storage.file_store import dataframe_to_listing_rows

        df = read_xlsx_with_hyperlinks(self._legacy_workbook_bytes())
        rows = dataframe_to_listing_rows(df)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].building, "John Stow House")
        self.assertIsNone(rows[0].property_id)

    def test_dataframe_to_listing_rows_ignores_unknown_columns(self):
        # A separate guarantee from read_xlsx_with_hyperlinks' own fix above:
        # even if some other caller hands dataframe_to_listing_rows a
        # DataFrame that genuinely still has an old/unknown column (pydantic's
        # default extra="ignore" on ListingRow(**cleaned)), it must not raise.
        import pandas as pd

        from storage.file_store import dataframe_to_listing_rows

        df = pd.DataFrame([{
            "building": "A", "provider": "P1", "rent_psf_min": 190.0, "size_sqft_min": 2123.0,
        }])
        rows = dataframe_to_listing_rows(df)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].building, "A")
        self.assertFalse(hasattr(rows[0], "rent_psf_min"))


if __name__ == "__main__":
    unittest.main()
