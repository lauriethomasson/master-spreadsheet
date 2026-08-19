"""
Regression test for app.py's skipped-sheet message wording when Gemini
extraction legitimately returns zero units for a sheet - distinguishing a
sheet where a building was recognized but has nothing currently available
(its own mini-table just says "Fully Occupied", per extract_spreadsheet_
gemini.PROMPT shape (b)) from one where no listing data was recognized on
the sheet at all (e.g. a portfolio-wide index page with no per-unit rows).

Runs the real app.py end-to-end via Streamlit's AppTest, with extract_
spreadsheet_gemini's Gemini call mocked to reproduce each response shape -
never calls the real API (same principle as test_app_upload_rescue_removal.py).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_upload_fully_occupied_sheet -v
"""

import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.file_store import list_pending_staging_files

BASE = Path(__file__).resolve().parent.parent


def _clear_pending():
    for p in list_pending_staging_files():
        (BASE / p).unlink(missing_ok=True)
        (BASE / p).with_suffix(".meta.json").unlink(missing_ok=True)


def _build_xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


class FullyOccupiedSheetMessageTests(unittest.TestCase):
    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_fully_occupied_sheet_shows_recognized_building_message(self):
        xlsx = _build_xlsx([
            [None, "Riverside Building - Southwark"],
            [None, "A modern glass-fronted office building overlooking the Thames."],
            [None, "15 Riverside Walk, SE1 9EZ", None, None, None, "Download Floorplans"],
            [None, "Office", "Sq.Ft", "Rent PCM", "Available From"],
            [None, "Fully Occupied"],
        ])
        raw = {"provider": "Copthall Estates", "contacts": None, "units": []}
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()

            at.file_uploader[0].upload(
                "fully_occupied.xlsx", xlsx,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()

            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        info_text = "".join(i.value for i in at.info)
        self.assertIn("nothing available in this building right now", info_text)
        self.assertNotIn("no listings found on this sheet", info_text)

    def test_no_recognizable_data_sheet_shows_original_message(self):
        xlsx = _build_xlsx([
            [None, "This is a commission and incentive structure explanation, not a listings page."],
        ])
        raw = {"provider": "Copthall Estates", "contacts": None, "units": []}
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()

            at.file_uploader[0].upload(
                "no_data.xlsx", xlsx,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()

            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        info_text = "".join(i.value for i in at.info)
        self.assertIn("no listings found on this sheet", info_text)
        self.assertNotIn("nothing available in this building right now", info_text)


if __name__ == "__main__":
    unittest.main()
