"""
Regression tests for extract_spreadsheet_gemini.py - the Gemini text-
extraction fallback for spreadsheet sheets with no single consistent header
row (see the module docstring). Never calls the real Gemini API - get_client/
call_gemini are mocked throughout, same principle as test_brochure_link.py
mocking GCS: this dev environment has no live-model determinism to test
against, only the deterministic code around it (rendering, row construction,
brochure-link resolution, building inheritance).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_extract_spreadsheet_gemini -v
"""

import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract_spreadsheet_gemini


class RenderSheetAsTextTests(unittest.TestCase):
    def _sheet(self, rows):
        wb = Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        return ws

    def test_blank_rows_are_skipped_entirely(self):
        ws = self._sheet([["Building"], [None], ["28 Lime Street"]])
        text = extract_spreadsheet_gemini.render_sheet_as_text(ws)
        self.assertEqual(text, "Row 1: Building\nRow 3: 28 Lime Street")

    def test_merged_continuation_blank_cells_are_preserved_not_dropped(self):
        # Real Copthall Portfolio-sheet shape: Area/Building blank on a
        # continuation row must render as an empty slot between pipes, not
        # be silently compacted away - column position is the only signal
        # Gemini has that this row inherits the value above it.
        ws = self._sheet([
            ["Area", "Building", "Floor"],
            ["City", "Copthall Avenue", "LG Floor"],
            [None, None, "2nd Floor"],
        ])
        text = extract_spreadsheet_gemini.render_sheet_as_text(ws)
        lines = text.splitlines()
        self.assertEqual(lines[2], "Row 3:  |  | 2nd Floor")

    def test_trailing_blank_cells_are_trimmed(self):
        ws = self._sheet([["Building", None, None]])
        text = extract_spreadsheet_gemini.render_sheet_as_text(ws)
        self.assertEqual(text, "Row 1: Building")

    def test_hyperlink_target_is_inlined_after_display_text(self):
        ws = self._sheet([["Download Brochure"]])
        ws["A1"].hyperlink = "https://example.com/brochure.pdf"
        text = extract_spreadsheet_gemini.render_sheet_as_text(ws)
        self.assertEqual(text, "Row 1: Download Brochure (https://example.com/brochure.pdf)")

    def test_hyperlink_with_no_display_text_uses_bare_url(self):
        ws = self._sheet([[None]])
        ws["A1"].hyperlink = "https://example.com/brochure.pdf"
        # openpyxl's hyperlink setter auto-populates .value to match when the
        # cell had none - reset it to actually construct "a hyperlink with no
        # separate display text" rather than "display text that happens to
        # equal the URL".
        ws["A1"].value = None
        text = extract_spreadsheet_gemini.render_sheet_as_text(ws)
        self.assertEqual(text, "Row 1: https://example.com/brochure.pdf")

    def test_all_blank_sheet_renders_to_empty_string(self):
        ws = self._sheet([[None, None], [None]])
        self.assertEqual(extract_spreadsheet_gemini.render_sheet_as_text(ws), "")


class ExtractSheetTests(unittest.TestCase):
    def _sheet(self):
        wb = Workbook()
        ws = wb.active
        ws["A1"] = "Office"
        ws["A2"] = "4th Floor"
        return ws

    def test_empty_sheet_never_calls_gemini(self):
        wb = Workbook()
        ws = wb.active  # genuinely blank
        with patch("extract_spreadsheet_gemini.get_client") as mock_get_client, \
             patch("extract_spreadsheet_gemini.call_gemini") as mock_call_gemini:
            rows = extract_spreadsheet_gemini.extract_sheet(ws, "file.xlsx — Sheet1", "file.xlsx")

        self.assertEqual(rows, [])
        mock_get_client.assert_not_called()
        mock_call_gemini.assert_not_called()

    def test_builds_listing_rows_from_gemini_output(self):
        raw = {
            "provider": "Copthall Estates",
            "contacts": "Kiri Norton-Brennan, 0795 811 8382",
            "units": [
                {"building": "28 Lime Street", "floor_unit": "4th Floor", "size_sqft": 1358,
                 "rent_pcm": 19805, "rent_psf": 175, "special_features": "22 desks"},
            ],
        }
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            rows = extract_spreadsheet_gemini.extract_sheet(self._sheet(), "file.xlsx — City", "file.xlsx")

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.building, "28 Lime Street")
        self.assertEqual(row.provider, "Copthall Estates")
        self.assertEqual(row.contacts, "Kiri Norton-Brennan, 0795 811 8382")
        self.assertEqual(row.size_sqft, 1358)
        self.assertEqual(row.source_file, "file.xlsx — City")

    def test_unit_with_blank_building_inherits_from_prior_unit(self):
        raw = {
            "provider": None, "contacts": None,
            "units": [
                {"building": "111 Wardour Street", "floor_unit": "4th"},
                {"building": None, "floor_unit": "5th"},
            ],
        }
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            rows = extract_spreadsheet_gemini.extract_sheet(self._sheet(), "file.xlsx — Sheet1", "file.xlsx")

        self.assertEqual([r.building for r in rows], ["111 Wardour Street", "111 Wardour Street"])

    def test_unit_with_blank_building_and_no_prior_is_skipped(self):
        raw = {"provider": None, "contacts": None, "units": [{"building": None, "floor_unit": "4th"}]}
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            rows = extract_spreadsheet_gemini.extract_sheet(self._sheet(), "file.xlsx — Sheet1", "file.xlsx")

        self.assertEqual(rows, [])

    def test_no_units_returns_empty_list(self):
        raw = {"provider": "Copthall Estates", "contacts": None, "units": []}
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            rows = extract_spreadsheet_gemini.extract_sheet(self._sheet(), "file.xlsx — Incentives", "file.xlsx")

        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
