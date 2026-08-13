"""
Streamlit-level regression tests for the brochure-enrichment summary on
pages/2_Review_and_Master.py's pending-review screen (see
_render_brochure_enrichment_summary) - purely a read-only caption for a
file that finished normally (enrichment itself runs automatically during
Extract, see test_app_upload_brochure_enrichment.py, never from a button
for that case), but a "Continue enrichment" button DOES appear for a file
whose enrichment was interrupted partway through - the one and only
action this page offers, and only ever to recover from that specific
situation, never to make ordinary enrichment optional.

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
from storage.file_store import (
    get_staging_enrichment_summary,
    load_staging_as_dataframe,
    save_staging_file,
    set_staging_enrichment_progress,
    set_staging_enrichment_summary,
)

BASE = Path(__file__).resolve().parent.parent


class BrochureEnrichmentSummaryReviewUiTests(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def test_no_enrich_button_for_a_file_with_no_interrupted_run(self):
        # No enrichment ever ran for this file at all (get_staging_
        # enrichment_summary returns None) - "Continue enrichment" only
        # ever appears for a file whose OWN interim marker says
        # status="in_progress" (see test_interrupted_run_offers_a_
        # continue_button_with_the_remaining_count), never as a general
        # optional trigger.
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
        set_staging_enrichment_summary(
            path, {"unique_brochures_considered": 1, "rows_eligible": 1, "rows_enriched": 1},
            {"https://example.com/brochure.pdf": "ok"},
        )

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("Brochure enrichment", caption_text)
        self.assertIn("1 row(s) enriched", caption_text)

    def test_completed_run_never_shows_a_continue_button(self):
        # Continue enrichment exists ONLY as recovery for an interrupted
        # (status="in_progress") run - a genuinely complete file must
        # never offer it, or a reviewer could click it expecting to
        # resume something that already finished.
        path = save_staging_file(
            [ListingRow(
                building="16 Dufour's Place", floor_unit="3rd Floor",
                brochure_link="https://example.com/brochure.pdf",
                special_features="Roof terrace",
            )],
            "Union.xlsx", content_hash="hash-complete-no-continue",
        )
        set_staging_enrichment_summary(
            path, {"unique_brochures_considered": 1, "rows_eligible": 1, "rows_enriched": 1},
            {"https://example.com/brochure.pdf": "ok"},
        )

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        continue_buttons = [b for b in at.button if "Continue enrichment" in (b.label or "")]
        self.assertEqual(continue_buttons, [])

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
        processed = {f"https://example.com/{i}.pdf": ("ok" if i < 3 else "unavailable") for i in range(5)}
        set_staging_enrichment_summary(
            path, {"unique_brochures_considered": 5, "rows_eligible": 5, "rows_enriched": 3}, processed,
        )

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("2 brochure(s) could not be processed", caption_text)

    def test_interrupted_run_shows_a_warning_not_a_normal_caption(self):
        # A run that never reached its own final set_staging_enrichment_
        # summary call (killed process, crashed Cloud Run instance,
        # cancelled Streamlit rerun) leaves only the interim progress
        # marker behind (see set_staging_enrichment_progress) - this file's
        # rows are a genuine mix of enriched and never-attempted, so this
        # must render as an explicit warning, never the same quiet caption
        # a genuinely finished run gets.
        path = save_staging_file(
            [ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None)],
            "Union.xlsx", content_hash="hash-interrupted",
        )
        processed = {f"https://example.com/{i}.pdf": "ok" for i in range(4)}
        set_staging_enrichment_progress(path, processed, 10)

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        warning_text = "".join(w.value for w in at.warning)
        self.assertIn("4/10", warning_text)
        self.assertIn("stopped", warning_text)
        caption_text = "".join(c.value for c in at.caption)
        self.assertNotIn("Brochure enrichment", caption_text)

    def test_interrupted_run_offers_a_continue_button_with_the_remaining_count(self):
        path = save_staging_file(
            [ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None)],
            "Union.xlsx", content_hash="hash-interrupted-2",
        )
        processed = {f"https://example.com/{i}.pdf": "ok" for i in range(4)}
        set_staging_enrichment_progress(path, processed, 10)

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        continue_buttons = [b for b in at.button if "Continue enrichment" in (b.label or "")]
        self.assertEqual(len(continue_buttons), 1)
        self.assertIn("6 remaining", continue_buttons[0].label)

    def test_clicking_continue_resumes_only_the_remaining_brochures(self):
        path = save_staging_file(
            [
                ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None),
                ListingRow(building="B", brochure_link="https://example.com/b.pdf", special_features=None),
            ],
            "Union.xlsx", content_hash="hash-interrupted-3",
        )
        set_staging_enrichment_progress(path, {"https://example.com/a.pdf": "ok"}, 2)

        with patch(
            "brochure_enrichment._extract_brochure_units",
            return_value=[{"building": "B", "floor_unit": None, "special_features": "Recovered"}],
        ) as mock_extract:
            at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
            at.run()
            continue_button = next(b for b in at.button if "Continue enrichment" in (b.label or ""))
            continue_button.click().run()
            self.assertFalse(at.exception)

        mock_extract.assert_called_once_with("https://example.com/b.pdf")
        df = load_staging_as_dataframe(path)
        self.assertEqual(df.loc[df["building"] == "B", "special_features"].iloc[0], "Recovered")
        stats = get_staging_enrichment_summary(path)
        self.assertEqual(stats["status"], "complete")


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

    def test_document_issues_expander_shown_when_issues_exist(self):
        path = save_staging_file(
            [ListingRow(building="Clove", brochure_link="https://example.com/broken.pdf", special_features=None)],
            "Union.xlsx", content_hash="hash-issues",
        )
        set_staging_enrichment_summary(
            path, {"unique_brochures_considered": 1, "rows_eligible": 1, "rows_enriched": 0},
            {"https://example.com/broken.pdf": "unavailable"},
            document_issues=[{"building": "Clove", "floor_unit": None, "status": "fetch_failed"}],
        )

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("1 document(s) need a look", caption_text)

        expanders = [e for e in at.expander if e.label == "View document issues"]
        self.assertEqual(len(expanders), 1)
        markdown_text = "".join(m.value for m in expanders[0].markdown)
        self.assertIn("Clove", markdown_text)
        self.assertIn("could not be accessed", markdown_text)

    def test_no_issues_expander_when_nothing_wrong(self):
        path = save_staging_file(
            [ListingRow(building="Good Co", brochure_link="https://example.com/good.pdf", special_features="Nice")],
            "Union.xlsx", content_hash="hash-no-issues",
        )
        set_staging_enrichment_summary(
            path, {"unique_brochures_considered": 1, "rows_eligible": 1, "rows_enriched": 1},
            {"https://example.com/good.pdf": "ok"},
            document_issues=[],
        )

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        self.assertEqual([e for e in at.expander if e.label == "View document issues"], [])


if __name__ == "__main__":
    unittest.main()
