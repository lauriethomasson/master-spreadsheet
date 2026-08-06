"""
Regression test for app.py's _warn_if_units_look_undercounted - a live
Gemini call against the real Copthall Estates "Mid Town" sheet's Cursitor
Street mini-table (G/LG East, 1st Floor, 4th Floor) returned only 1 unit on
a real production run, a silent, validly-parsed but short response that
_warn_if_extraction_looks_garbled's own row-content check can't see (the
surviving row's own size_sqft/rent_pcm were completely normal).

Runs the real app.py end-to-end via Streamlit's AppTest, with extract_
spreadsheet_gemini's Gemini call mocked to reproduce that exact failure -
never calls the real API (same principle as test_app_upload_rescue_removal.py).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_upload_undercounted_units -v
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


def _build_mid_town_xlsx() -> bytes:
    # Same real shape as the actual Copthall Estates "Mid Town" sheet: row 1
    # is a title (no real header row), a blank leading column on every row,
    # and a single building block with a 3-floor mini-table (Cursitor
    # Street - G/LG East, 1st Floor, 4th Floor).
    wb = Workbook()
    ws = wb.active
    ws.append([None, "Copthall Estates Mid-Town Availability - Updated 03/08/2026"])
    ws.append([None, "Cursitor Street - Chancery Lane"])
    ws.append([None, "Cursitor Street offers fully furnished, self-contained office spaces."])
    ws.append([None, "11 Cursitor Street, EC4A 1LL", None, None, None, "Download Floorplans"])
    ws.append([
        None, "Office", "Sq.Ft", "Price Per Sq.Ft", "Monthly List Price",
        "Office Description", "Minimum Term", "Available From", "Commission",
    ])
    ws.append([
        None, "G/LG East", 1800.0, 143.0, 21379.0,
        "24+ desks on the ground floor and a dedicated kitchen, and a breakout area in the LG floor.",
        "24 Months", "Now", "10% on the first 12 months.",
    ])
    ws.append([
        None, "1st Floor", 1696.0, 160.0, 22614.0,
        "18 desks, 1 exec office, 10 person boardroom, dedicated kitchen and breakout space",
        "24 Months", "1st October 2026",
    ])
    ws.append([
        None, "4th Floor", 706.0, 160.0, 9413.0,
        "12 desks, dedicated kitchen, dedicated 8 person boardroom",
        "24 Months", "Now",
    ])
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


class UndercountedUnitsWarningTests(unittest.TestCase):
    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_partial_extraction_shows_undercount_warning(self):
        # The exact real failure: only G/LG East survives, 1st Floor and
        # 4th Floor silently vanish from an otherwise validly-parsed response.
        raw = {
            "provider": "Copthall Estates",
            "contacts": None,
            "units": [
                {
                    "building": "Cursitor Street", "floor_unit": "G/LG East",
                    "size_sqft": 1800, "rent_pcm": 21379,
                },
            ],
        }
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            self.assertFalse(at.exception)

            at.file_uploader[0].upload(
                "mid_town.xlsx", _build_mid_town_xlsx(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()

            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        warnings = "".join(w.value for w in at.warning)
        self.assertIn("Cursitor Street", warnings)
        self.assertIn("3 unit(s)", warnings)
        self.assertIn("only 1 were extracted", warnings)

    def test_complete_extraction_shows_no_undercount_warning(self):
        raw = {
            "provider": "Copthall Estates",
            "contacts": None,
            "units": [
                {"building": "Cursitor Street", "floor_unit": "G/LG East", "size_sqft": 1800, "rent_pcm": 21379},
                {"building": "Cursitor Street", "floor_unit": "1st Floor", "size_sqft": 1696, "rent_pcm": 22614},
                {"building": "Cursitor Street", "floor_unit": "4th Floor", "size_sqft": 706, "rent_pcm": 9413},
            ],
        }
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()

            at.file_uploader[0].upload(
                "mid_town.xlsx", _build_mid_town_xlsx(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()

            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        warnings = "".join(w.value for w in at.warning)
        self.assertNotIn("looks like it may have", warnings)


if __name__ == "__main__":
    unittest.main()
