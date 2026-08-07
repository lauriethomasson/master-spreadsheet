"""
App-level regression tests for wiring brochure_enrichment.py into app.py's
spreadsheet upload path - confirms the feature actually reaches staged
rows end to end, is scoped to spreadsheet uploads only, and that the
existing content-hash cache-busting fingerprint mechanism now also covers
brochure_enrichment.py's own source.

Runs the real app.py end-to-end via Streamlit's AppTest, with httpx and
extract.extract_raw_units mocked - never touches the real network or the
real Gemini API.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_upload_brochure_enrichment -v
"""

import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app
import brochure_enrichment
from storage.file_store import list_pending_staging_files, load_staging_as_dataframe

BASE = Path(__file__).resolve().parent.parent


def _clear_pending():
    for p in list_pending_staging_files():
        (BASE / p).unlink(missing_ok=True)
        (BASE / p).with_suffix(".meta.json").unlink(missing_ok=True)


def _union_style_workbook() -> bytes:
    # Real documented UNION "by-area" header wording (see extract_
    # spreadsheet.py's own EXTRA_SYNONYMS comment) - a plain single-table
    # sheet, resolved entirely by header-mapping, no Gemini call at all.
    wb = Workbook()
    ws = wb.active
    ws.title = "Availability"
    ws.append(["Building", "Floor/Unit", "Size (sq ft)", "Monthly Rate", "Brochure"])
    ws.append(["16 Dufour's Place", "3rd Floor", 1200, 15000, "https://example.com/brochure.pdf"])
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _pdf_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"%PDF-1.4 fake"
    resp.headers = {"content-type": "application/pdf"}
    resp.raise_for_status.side_effect = None
    return resp


class BrochureEnrichmentUploadTests(unittest.TestCase):
    def setUp(self):
        _clear_pending()
        brochure_enrichment._extract_brochure_units.cache_clear()

    def tearDown(self):
        _clear_pending()
        brochure_enrichment._extract_brochure_units.cache_clear()

    def test_union_style_row_gets_enriched_from_its_linked_brochure(self):
        raw_units = {
            "units": [{
                "building": "16 Dufour's Place", "floor_unit": "3rd Floor",
                "special_features": "Private terrace; showers; cycle storage",
            }],
        }
        with patch("brochure_enrichment.httpx.get", return_value=_pdf_response()) as mock_get, \
             patch("brochure_enrichment.extract.extract_raw_units", return_value=raw_units) as mock_extract:
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Union.xlsx", _union_style_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()

            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        # geocode.py ALSO calls httpx.get (same shared module-level
        # attribute), so mock_get's own call count isn't specific to
        # brochure fetching - filter to calls actually targeting the
        # brochure URL instead.
        brochure_calls = [c for c in mock_get.call_args_list if c.args and c.args[0] == "https://example.com/brochure.pdf"]
        self.assertEqual(len(brochure_calls), 1)
        mock_extract.assert_called_once()

        info_text = "".join(i.value for i in at.info)
        self.assertIn("Enriched", info_text)
        self.assertIn("16 Dufour's Place", info_text)

        pending = list_pending_staging_files()
        df = load_staging_as_dataframe(pending[0])
        self.assertEqual(df.iloc[0]["special_features"], "Private terrace; showers; cycle storage")
        # The structured fields the spreadsheet already provided must
        # survive completely untouched.
        self.assertEqual(df.iloc[0]["size_sqft"], 1200)
        self.assertEqual(df.iloc[0]["rent_pcm"], 15000)

    def test_row_with_no_brochure_link_never_touches_the_network(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "Availability"
        ws.append(["Building", "Floor/Unit", "Size (sq ft)", "Monthly Rate"])
        ws.append(["40 New Bond Street", "3rd Floor", 2000, 15000])
        buffer = BytesIO()
        wb.save(buffer)

        with patch("brochure_enrichment.extract.extract_raw_units") as mock_extract:
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Kitts.xlsx", buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()

            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        # No brochure_link at all on this row - _is_eligible_brochure_url
        # rejects it before any fetch/Gemini attempt, regardless of
        # needs_enrichment (special_features is also blank here).
        mock_extract.assert_not_called()
        info_text = "".join(i.value for i in at.info)
        self.assertNotIn("Enriched", info_text)


class FingerprintIncludesBrochureEnrichmentTests(unittest.TestCase):
    def test_fingerprint_matches_recomputation_including_brochure_enrichment(self):
        # A change to brochure_enrichment.py's own logic must invalidate an
        # already-staged spreadsheet result exactly like a change to
        # extract_spreadsheet(_gemini).py already does - see app.py's own
        # _SPREADSHEET_LOGIC_FINGERPRINT comment. Recomputes the exact same
        # formula independently and compares, so a future refactor that
        # forgets to fold brochure_enrichment.py back in fails this test.
        import hashlib

        import extract_spreadsheet
        import extract_spreadsheet_gemini

        expected = hashlib.sha256(
            Path(extract_spreadsheet.__file__).read_bytes()
            + Path(extract_spreadsheet_gemini.__file__).read_bytes()
            + Path(brochure_enrichment.__file__).read_bytes()
        ).hexdigest()

        self.assertEqual(app._SPREADSHEET_LOGIC_FINGERPRINT, expected)


if __name__ == "__main__":
    unittest.main()
