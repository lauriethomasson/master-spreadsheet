"""
Regression tests for the "missing location" note on brand-new properties
(see pages/2_Review_and_Master.py's "New properties" section and
_render_new_property_let_status_decision, and master_merge.
new_property_missing_location).

Purely informational: a new property with no address_1, postcode, lat, AND
lng at all still gets added exactly as before - the note only flags it,
never blocks it. A row missing just SOME of those fields (a partial case)
must not trigger the note at all.

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_page_restructure.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_new_property_missing_location -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import master_writer
from schema import ListingRow
from storage.file_store import save_staging_file
from streamlit.testing.v1 import AppTest

BASE = Path(__file__).resolve().parent.parent

_NOTE = "📍 Address, postcode & map location not found — added anyway"


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


class PlainNewPropertyMissingLocationTests(IsolatedCwdTestCase):
    def test_note_shown_when_address_postcode_lat_lng_all_blank(self):
        save_staging_file(
            [ListingRow(building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor")],
            "new_no_location.xlsx", content_hash="new-no-location-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        self.assertIn("📄 New properties", [s.value for s in at.subheader])
        caption_text = "".join(c.value for c in at.caption)
        self.assertIn(_NOTE, caption_text)

    def test_no_note_when_address_1_is_present(self):
        save_staging_file(
            [ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor",
                address_1="9 Example Yard",
            )],
            "new_partial_location.xlsx", content_hash="new-partial-location-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertNotIn(_NOTE, caption_text)

    def test_no_note_when_only_lat_lng_present(self):
        # Partial case: lat/lng found but address_1/postcode weren't - the
        # note is specifically for "nothing geographic came through at all".
        save_staging_file(
            [ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor",
                lat=51.5, lng=-0.1,
            )],
            "new_coords_only.xlsx", content_hash="new-coords-only-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertNotIn(_NOTE, caption_text)

    def test_flagged_property_still_gets_added_on_approve(self):
        save_staging_file(
            [ListingRow(building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor")],
            "new_no_location.xlsx", content_hash="new-no-location-approve-hash",
        )
        at = _run_review_page()
        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)
        self.assertEqual(master_df.iloc[0]["building"], "9 Example Yard")


class NewPropertyLetStatusMissingLocationTests(IsolatedCwdTestCase):
    def test_note_also_shown_on_the_let_status_decision_card(self):
        # Same missing-location signal, applied to the OTHER new-property
        # rendering path (decision_new_property_let_status), for consistency.
        save_staging_file(
            [ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor",
                special_features="Under Offer",
            )],
            "new_uo_no_location.xlsx", content_hash="new-uo-no-location-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        self.assertIn("⚠️ Needs your decision", [s.value for s in at.subheader])
        caption_text = "".join(c.value for c in at.caption)
        self.assertIn(_NOTE, caption_text)

    def test_no_note_on_let_status_card_when_address_present(self):
        save_staging_file(
            [ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor",
                special_features="Under Offer", address_1="9 Example Yard",
            )],
            "new_uo_with_location.xlsx", content_hash="new-uo-with-location-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertNotIn(_NOTE, caption_text)


if __name__ == "__main__":
    unittest.main()
