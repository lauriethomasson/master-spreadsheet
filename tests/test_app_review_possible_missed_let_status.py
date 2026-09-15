"""
Regression tests for surfacing schema.ListingRow.possible_missed_let_status
on the Review page - the deterministic (non-LLM) LET-status keyword cross-
check's own review flag (see extract.py/extract_spreadsheet_gemini.py/
brochure_enrichment.py's own detection, master_merge.build_merge_plan's
own injection into risky_fields). Confirms the note appears as its own,
distinctly-labeled caption ALONGSIDE the existing generic richness-
regression warning on the same special_features field row, never replacing
it - see pages/2_Review_and_Master.py's own _render_field_rows.

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_matched_row_missing_location.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_possible_missed_let_status -v
"""

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import master_writer
from schema import ListingRow
from storage.file_store import save_staging_file
from streamlit.testing.v1 import AppTest

BASE = Path(__file__).resolve().parent.parent

_NOTE = "This unit's own source page/row states 'LET', but the extracted data doesn't reflect it. Verify manually."


class IsolatedCwdTestCase(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()


def _run_review_page():
    at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
    at.run()
    return at


class PossibleMissedLetStatusCaptionTests(IsolatedCwdTestCase):
    def test_note_appears_as_its_own_caption_on_the_special_features_row(self):
        pid = str(uuid.uuid4())
        master_writer.write_master([
            ListingRow(
                building="Ivybridge House", provider="Colliers", floor_unit="Level 2",
                special_features="Views of the River Thames", property_id=pid,
            ),
        ])
        save_staging_file(
            [ListingRow(
                building="Ivybridge House", provider="Colliers", floor_unit="Level 2",
                special_features="Views of the River Thames",
                possible_missed_let_status=_NOTE,
            )],
            "ivybridge_missed_let_status.xlsx", content_hash="ivybridge-missed-let-status-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        self.assertIn("⚠️ Needs your decision", [s.value for s in at.subheader])
        caption_text = "".join(c.value for c in at.caption)
        self.assertIn(f"🔍 {_NOTE}", caption_text)

    def test_row_with_no_note_gets_no_such_caption(self):
        pid = str(uuid.uuid4())
        master_writer.write_master([
            ListingRow(
                building="Ivybridge House", provider="Colliers", floor_unit="Level 2",
                special_features="Views of the River Thames", property_id=pid,
            ),
        ])
        save_staging_file(
            [ListingRow(
                building="Ivybridge House", provider="Colliers", floor_unit="Level 2",
                special_features="Views of the River Thames",
            )],
            "ivybridge_no_missed_let_status.xlsx", content_hash="ivybridge-no-missed-let-status-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertNotIn("🔍", caption_text)
        # Nothing at all changed, and no other reason to review this row -
        # it correctly lands in matched_unchanged with no decision needed.
        self.assertNotIn("⚠️ Needs your decision", [s.value for s in at.subheader])

    def test_note_appears_alongside_a_genuine_richness_regression_warning(self):
        # Both signals can fire on the SAME field at once - see schema.
        # ListingRow.possible_missed_let_status's own docstring - neither
        # ever replaces the other.
        pid = str(uuid.uuid4())
        master_writer.write_master([
            ListingRow(
                building="Ivybridge House", provider="Colliers", floor_unit="Level 2",
                special_features="Views of the River Thames; Comprehensively refurbished; Air conditioning",
                property_id=pid,
            ),
        ])
        save_staging_file(
            [ListingRow(
                building="Ivybridge House", provider="Colliers", floor_unit="Level 2",
                special_features="Refurbished",
                possible_missed_let_status=_NOTE,
            )],
            "ivybridge_both_signals.xlsx", content_hash="ivybridge-both-signals-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn(f"🔍 {_NOTE}", caption_text)
        self.assertIn("may be missing detail", caption_text)


class LetStatusCheckUnavailableBannerTests(IsolatedCwdTestCase):
    """
    The one-time, page-level banner for a pasted Canva/Pitch-link upload
    (see schema.ListingRow.let_status_check_unavailable/pages/2_Review_
    and_Master.py's own _render_let_status_check_unavailable_banner) - the
    confirmed real gap this closes: extract_from_png_pages's own rows
    previously showed NO signal at all that the deterministic LET-status
    cross-check never ran for them, indistinguishable from "checked,
    nothing missed".
    """

    def test_banner_shown_when_a_row_could_not_be_checked(self):
        save_staging_file(
            [ListingRow(
                building="Ivybridge House", provider="Colliers", floor_unit="Level 2",
                special_features="Views of the River Thames",
                let_status_check_unavailable=True,
            )],
            "www.canva.com_design_x_view.xlsx", content_hash="canva-let-status-unavailable-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        warning_text = "".join(w.value for w in at.warning)
        self.assertIn("pasted Canva/Pitch link", warning_text)
        self.assertIn("could not run", warning_text)

    def test_no_banner_for_an_ordinary_upload(self):
        save_staging_file(
            [ListingRow(
                building="Ivybridge House", provider="Colliers", floor_unit="Level 2",
                special_features="Views of the River Thames",
            )],
            "ordinary_upload.xlsx", content_hash="ordinary-upload-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        warning_text = "".join(w.value for w in at.warning)
        self.assertNotIn("pasted Canva/Pitch link", warning_text)

    def test_banner_shown_once_even_when_multiple_rows_are_affected(self):
        save_staging_file(
            [
                ListingRow(
                    building="Ivybridge House", provider="Colliers", floor_unit="Level 1",
                    let_status_check_unavailable=True,
                ),
                ListingRow(
                    building="Ivybridge House", provider="Colliers", floor_unit="Level 2",
                    let_status_check_unavailable=True,
                ),
            ],
            "www.canva.com_design_y_view.xlsx", content_hash="canva-let-status-unavailable-multi-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        warning_text = [w.value for w in at.warning]
        matching = [w for w in warning_text if "pasted Canva/Pitch link" in w]
        self.assertEqual(len(matching), 1)


class DomTextCheckedRowVsUncheckedRowDistinguishabilityTests(IsolatedCwdTestCase):
    """
    End-to-end proof that a row checked via real DOM text (extract._png_
    page_let_status_matches, when the Canva/Pitch renderer captured real
    page text) and a row that genuinely couldn't be checked at all (no
    DOM text captured) render DISTINCTLY on the actual Review page -
    never confusably identical. This is the concrete UI-level guarantee
    behind the "reviewer must always be able to tell which method
    checked a row" requirement: a DOM-text-checked row behaves exactly
    like a real PDF row (the normal 🔍 caption, no page-level banner);
    an unchecked row gets the page-level banner instead, never the
    caption.
    """

    def test_dom_checked_row_gets_the_caption_not_the_banner(self):
        pid = str(uuid.uuid4())
        master_writer.write_master([
            ListingRow(
                building="Ivybridge House", provider="Colliers", floor_unit="Level 2",
                special_features="Views of the River Thames", property_id=pid,
            ),
        ])
        save_staging_file(
            [ListingRow(
                building="Ivybridge House", provider="Colliers", floor_unit="Level 2",
                special_features="Views of the River Thames",
                possible_missed_let_status=_NOTE,
                # Checked via real DOM text and something WAS found -
                # let_status_check_unavailable stays unset (None),
                # identical to a real PDF row.
            )],
            "www.canva.com_design_dom_checked_view.xlsx", content_hash="canva-dom-checked-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        warning_text = "".join(w.value for w in at.warning)
        self.assertIn(f"🔍 {_NOTE}", caption_text)
        self.assertNotIn("pasted Canva/Pitch link", warning_text)

    def test_unchecked_row_gets_the_banner_not_the_caption(self):
        save_staging_file(
            [ListingRow(
                building="New Derwent House", provider="Colliers", floor_unit="Level 3",
                special_features="Views of the West End",
                let_status_check_unavailable=True,
                # Genuinely never checked - no possible_missed_let_status
                # note at all, distinct from "checked, nothing missed".
            )],
            "www.canva.com_design_unchecked_view.xlsx", content_hash="canva-unchecked-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        warning_text = "".join(w.value for w in at.warning)
        self.assertNotIn("🔍", caption_text)
        self.assertIn("pasted Canva/Pitch link", warning_text)
        self.assertIn("could not run", warning_text)


if __name__ == "__main__":
    unittest.main()
