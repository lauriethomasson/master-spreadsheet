"""
Streamlit-level regression tests for the READ-ONLY brochure-enrichment
summary on pages/2_Review_and_Master.py's pending-review screen (see
_render_brochure_enrichment_summary) - enrichment itself now runs
automatically during Extract (see test_app_upload_brochure_enrichment.py),
never from a button here. This page only ever shows a caption reporting
what already happened.

Runs from an isolated temporary working directory (never the real repo),
same approach as test_app_review_master_lookup.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_brochure_enrichment -v
"""

import os
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import master_writer
from schema import ListingRow
from storage.file_store import save_staging_file, set_staging_enrichment_summary

BASE = Path(__file__).resolve().parent.parent


class BrochureEnrichmentSummaryReviewUiTests(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def test_no_manual_enrich_button_exists_anywhere_on_the_page(self):
        save_staging_file(
            [ListingRow(
                building="16 Dufour's Place", floor_unit="3rd Floor",
                brochure_link="https://example.com/brochure.pdf", special_features=None,
            )],
            "Union.xlsx", content_hash="hash-1",
        )

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        self.assertEqual([b.label for b in at.button if "nrich" in (b.label or "")], [])

    def test_read_only_summary_shown_for_a_file_that_was_already_enriched(self):
        path = save_staging_file(
            [ListingRow(
                building="16 Dufour's Place", floor_unit="3rd Floor",
                brochure_link="https://example.com/brochure.pdf",
                special_features="Roof terrace",
            )],
            "Union.xlsx", content_hash="hash-2",
        )
        set_staging_enrichment_summary(path, {
            "unique_brochures_considered": 1, "brochures_read_ok": 1,
            "brochures_unavailable": 0, "rows_eligible": 1, "rows_enriched": 1,
        })

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("Brochure enrichment", caption_text)
        self.assertIn("1 row(s) enriched", caption_text)

    def test_no_summary_shown_when_enrichment_never_ran_for_this_file(self):
        save_staging_file(
            [ListingRow(building="16 Dufour's Place", floor_unit="3rd Floor", special_features="Already there")],
            "Kitts.xlsx", content_hash="hash-3",
        )

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertNotIn("Brochure enrichment", caption_text)

    def test_summary_mentions_unavailable_brochures_when_present(self):
        path = save_staging_file(
            [ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features="x")],
            "Union.xlsx", content_hash="hash-4",
        )
        set_staging_enrichment_summary(path, {
            "unique_brochures_considered": 5, "brochures_read_ok": 3,
            "brochures_unavailable": 2, "rows_eligible": 5, "rows_enriched": 3,
        })

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("2 brochure(s) could not be processed", caption_text)


def _pdf_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"%PDF-1.4 fake"
    resp.headers = {"content-type": "application/pdf"}
    resp.raise_for_status.side_effect = None
    return resp


class MasterUntouchedBeforeApprovalTests(unittest.TestCase):
    """
    Runs BOTH app.py (upload + automatic Extract-time enrichment) and
    pages/2_Review_and_Master.py (Approve) inside one isolated temp cwd, so
    master_writer.write_master never touches the real repo's data/
    master.xlsx (same isolation approach as test_app_review_master_lookup.py)
    even though this exercises the real Approve flow.
    """

    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def test_master_is_not_created_until_approve_is_clicked(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "Availability"
        ws.append(["Building", "Floor/Unit", "Size (sq ft)", "Monthly Rate", "Brochure"])
        ws.append(["16 Dufour's Place", "3rd Floor", 1200, 15000, "https://example.com/brochure.pdf"])
        buffer = BytesIO()
        wb.save(buffer)

        raw_units = {"units": [{
            "building": "16 Dufour's Place", "floor_unit": "3rd Floor", "special_features": "Roof terrace",
        }]}
        with patch("brochure_enrichment.httpx.get", return_value=_pdf_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value=raw_units):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Union.xlsx", buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        # Automatic enrichment already ran (as part of Extract) - master
        # must still not exist at all yet, regardless.
        self.assertFalse(master_writer.master_exists())

        review = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        review.run()
        self.assertFalse(review.exception)
        self.assertFalse(master_writer.master_exists())

        approve_buttons = [b for b in review.button if b.label == "Approve → Master"]
        self.assertEqual(len(approve_buttons), 1)
        approve_buttons[0].click().run()
        self.assertFalse(review.exception)

        self.assertTrue(master_writer.master_exists())
        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(master_df.iloc[0]["special_features"], "Roof terrace")


if __name__ == "__main__":
    unittest.main()
