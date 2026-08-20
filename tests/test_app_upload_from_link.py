"""
Regression tests for app.py's "paste a document link" upload path (see
brochure_enrichment.extract_rows_from_link) - a new input alongside the
existing file uploader, feeding the exact same staging/Review pipeline a
file upload already goes through.

Runs the real app.py end-to-end via Streamlit's AppTest, with brochure_
enrichment.extract_rows_from_link mocked (never calls the real API), same
principle as test_app_upload_missing_brochure_link.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_upload_from_link -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema import ListingRow
from storage.file_store import list_pending_staging_files

BASE = Path(__file__).resolve().parent.parent


def _clear_pending():
    for p in list_pending_staging_files():
        (BASE / p).unlink(missing_ok=True)
        (BASE / p).with_suffix(".meta.json").unlink(missing_ok=True)


def _run_upload_page():
    at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
    at.run()
    return at


def _paste_link_and_extract(at, url):
    link_input = next(t for t in at.text_input if t.key == "pasted_brochure_link")
    link_input.set_value(url).run()
    extract_button = next(b for b in at.button if b.label == "Extract from link")
    extract_button.click().run()
    return at


class PasteLinkExtractionTests(unittest.TestCase):
    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_multi_building_extraction_stages_one_row_per_unit(self):
        rows = [
            ListingRow(
                building="28 Lime Street", floor_unit="4th Floor", size_sqft=1915.0,
                special_features="Bike racks", brochure_link="https://example.com/schedule.pdf",
                source_file="https://example.com/schedule.pdf",
            ),
            ListingRow(
                building="40 Fenchurch Street", floor_unit="2nd Floor", size_sqft=2200.0,
                state_of_space="Fitted", brochure_link="https://example.com/schedule.pdf",
                source_file="https://example.com/schedule.pdf",
            ),
        ]
        with patch("brochure_enrichment.extract_rows_from_link", return_value=rows) as mock_extract, \
             patch("app.geocode_rows"):
            at = _run_upload_page()
            _paste_link_and_extract(at, "https://example.com/schedule.pdf")
            self.assertFalse(at.exception)

        mock_extract.assert_called_once_with("https://example.com/schedule.pdf")
        success_text = "".join(s.value for s in at.success)
        self.assertIn("Extracted and staged 2 row(s)", success_text)

        pending = list_pending_staging_files()
        self.assertEqual(len(pending), 1)

    def test_text_that_does_not_look_like_a_url_shows_an_error_no_staging(self):
        with patch("brochure_enrichment.extract_rows_from_link") as mock_extract:
            at = _run_upload_page()
            _paste_link_and_extract(at, "not a real link")
            self.assertFalse(at.exception)

        mock_extract.assert_not_called()
        error_text = "".join(e.value for e in at.error)
        self.assertIn("doesn't look like a real link", error_text)
        self.assertEqual(list_pending_staging_files(), [])

    def test_nothing_extracted_shows_an_error_no_staging(self):
        with patch("brochure_enrichment.extract_rows_from_link", return_value=[]):
            at = _run_upload_page()
            _paste_link_and_extract(at, "https://example.com/empty.pdf")
            self.assertFalse(at.exception)

        error_text = "".join(e.value for e in at.error)
        self.assertIn("Nothing could be extracted", error_text)
        self.assertEqual(list_pending_staging_files(), [])

    def test_extract_from_link_button_is_disabled_with_no_text_entered(self):
        at = _run_upload_page()
        extract_button = next(b for b in at.button if b.label == "Extract from link")
        self.assertTrue(extract_button.proto.disabled)


if __name__ == "__main__":
    unittest.main()
