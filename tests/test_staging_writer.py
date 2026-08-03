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
        self.assertEqual(title_case_label("rent_psf_min"), "Rent Psf Min")
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
        self.assertIn("Rent Psf Min", header_values)
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


if __name__ == "__main__":
    unittest.main()
