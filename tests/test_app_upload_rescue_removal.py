"""
Regression test for the removed pre-Extract rescue-prompt UI (app.py): a
spreadsheet sheet whose building column can't be resolved by header-mapping
must now fall straight through to extract_spreadsheet_gemini.extract_sheet,
automatically, with no expander/selectbox/confirm-button ever rendered - a
real Copthall Estates file's row 1 being just a title string (no real header
row at all) meant that prompt's own dropdown offered nonsense options
("nan", "1", the title text itself), not a legitimate manual answer.

Runs the real app.py end-to-end via Streamlit's AppTest, with extract_
spreadsheet_gemini's Gemini call mocked (never calls the real API - same
principle as test_extract_spreadsheet_gemini.py).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_upload_rescue_removal -v
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


def _build_no_header_row_xlsx() -> bytes:
    # Same real shape as Copthall Estates: row 1 is a title, not a header -
    # no column named "building" (or anything mappable to it) exists at all.
    wb = Workbook()
    ws = wb.active
    ws.append(["Copthall Estates City Availability - Updated 03/08/2026"])
    ws.append(["28 Lime Street - Fenchurch St / Bank"])
    ws.append(["Office", "Sq.Ft", "Monthly List Price"])
    ws.append(["4th Floor", 1358, 19805])
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


class UnresolvedBuildingSkipsRescuePromptTests(unittest.TestCase):
    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_no_rescue_prompt_is_rendered_and_gemini_fallback_runs_automatically(self):
        raw = {
            "provider": "Copthall Estates",
            "contacts": None,
            "units": [{"building": "28 Lime Street", "floor_unit": "4th Floor", "size_sqft": 1358, "rent_pcm": 19805}],
        }
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw) as mock_call_gemini:
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            self.assertFalse(at.exception)

            at.file_uploader[0].upload(
                "no_header_row.xlsx", _build_no_header_row_xlsx(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()
            self.assertFalse(at.exception)

            # The old rescue prompt showed an expander titled "We need your
            # help..." with a selectbox and a "Confirm" button - none of that
            # exists anywhere in the tree at this point, before Extract is
            # even clicked (that's exactly when the old prompt used to
            # appear).
            self.assertEqual(list(at.selectbox), [])
            self.assertNotIn("We need your help", "".join(e.label or "" for e in at.expander))
            self.assertNotIn("Confirm", [b.label for b in at.button if b.label != "Extract"])

            extract_buttons = [b for b in at.button if b.label == "Extract"]
            self.assertEqual(len(extract_buttons), 1)
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

            # Still no selectbox/rescue expander after Extract - it went
            # straight through, not "prompt appeared but auto-resolved".
            self.assertEqual(list(at.selectbox), [])
            self.assertNotIn("We need your help", "".join(e.label or "" for e in at.expander))

        mock_call_gemini.assert_called_once()
        pending = list_pending_staging_files()
        self.assertEqual(len(pending), 1)


if __name__ == "__main__":
    unittest.main()
