"""
Regression tests for the "missing location" note on brand-new properties
(see pages/2_Review_and_Master.py's "New properties" section and
_render_new_property_let_status_decision, and master_merge.
new_property_missing_location/missing_location_labels).

Purely informational: a new property missing ANY of address_1/postcode/
lat/lng still gets added exactly as before - the note only flags it, never
blocks it. The note now names specifically what's missing (lat/lng
collapse into one "map location" label) rather than a fixed phrase, since
it can now fire for a single missing field, not just all four at once.

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


def _note(missing: str) -> str:
    return f"📍 Missing: {missing} — added anyway"


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


def _stage_new_property(**fields):
    # Each test runs in its own fresh isolated temp directory (see
    # IsolatedCwdTestCase), so a fixed content_hash never collides across
    # test methods here.
    save_staging_file(
        [ListingRow(building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor", **fields)],
        "new_property.xlsx", content_hash="new-property-missing-location-hash",
    )


class PlainNewPropertyMissingLocationCombinationsTests(IsolatedCwdTestCase):
    """Every realistic combination of address_1/postcode/lat/lng presence,
    not just the one-field and all-four boundary cases."""

    def _caption_text(self, **fields):
        _stage_new_property(**fields)
        at = _run_review_page()
        self.assertFalse(at.exception)
        self.assertIn("📄 New properties", [s.value for s in at.subheader])
        return "".join(c.value for c in at.caption)

    def test_all_four_blank(self):
        caption_text = self._caption_text()
        self.assertIn(_note("address, postcode, map location"), caption_text)

    def test_only_address_1_blank(self):
        caption_text = self._caption_text(postcode="EC1A 1AA", lat=51.5, lng=-0.1)
        self.assertIn(_note("address"), caption_text)

    def test_only_postcode_blank(self):
        caption_text = self._caption_text(address_1="9 Example Yard", lat=51.5, lng=-0.1)
        self.assertIn(_note("postcode"), caption_text)

    def test_only_lat_blank(self):
        caption_text = self._caption_text(address_1="9 Example Yard", postcode="EC1A 1AA", lng=-0.1)
        self.assertIn(_note("map location"), caption_text)

    def test_only_lng_blank(self):
        caption_text = self._caption_text(address_1="9 Example Yard", postcode="EC1A 1AA", lat=51.5)
        self.assertIn(_note("map location"), caption_text)

    def test_both_lat_and_lng_blank_still_one_map_location_label(self):
        # Both halves missing collapses to the SAME single label as just
        # one half missing - never "map location, map location".
        caption_text = self._caption_text(address_1="9 Example Yard", postcode="EC1A 1AA")
        self.assertIn(_note("map location"), caption_text)
        self.assertNotIn("map location, map location", caption_text)

    def test_address_1_and_postcode_both_blank(self):
        caption_text = self._caption_text(lat=51.5, lng=-0.1)
        self.assertIn(_note("address, postcode"), caption_text)

    def test_postcode_and_map_location_blank_address_present(self):
        # The exact combination named in the task itself.
        caption_text = self._caption_text(address_1="9 Example Yard")
        self.assertIn(_note("postcode, map location"), caption_text)

    def test_address_and_map_location_blank_postcode_present(self):
        caption_text = self._caption_text(postcode="EC1A 1AA")
        self.assertIn(_note("address, map location"), caption_text)

    def test_nothing_missing_no_note_at_all(self):
        caption_text = self._caption_text(address_1="9 Example Yard", postcode="EC1A 1AA", lat=51.5, lng=-0.1)
        self.assertNotIn("📍", caption_text)

    def test_flagged_property_still_gets_added_on_approve(self):
        _stage_new_property()
        at = _run_review_page()
        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)
        self.assertEqual(master_df.iloc[0]["building"], "9 Example Yard")


class NewPropertyLetStatusMissingLocationTests(IsolatedCwdTestCase):
    """Same missing-location signal, applied to the OTHER new-property
    rendering path (decision_new_property_let_status), for consistency -
    a full combination sweep isn't repeated here (already covered above
    against the shared master_merge logic); just confirms this call site
    wires up the same specific-field wording correctly."""

    def test_note_names_specific_missing_fields_on_the_let_status_card(self):
        save_staging_file(
            [ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor",
                special_features="Under Offer", postcode="EC1A 1AA",
            )],
            "new_uo_partial_location.xlsx", content_hash="new-uo-partial-location-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        self.assertIn("⚠️ Needs your decision", [s.value for s in at.subheader])
        caption_text = "".join(c.value for c in at.caption)
        self.assertIn(_note("address, map location"), caption_text)

    def test_no_note_on_let_status_card_when_nothing_missing(self):
        save_staging_file(
            [ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor",
                special_features="Under Offer", address_1="9 Example Yard", postcode="EC1A 1AA",
                lat=51.5, lng=-0.1,
            )],
            "new_uo_full_location.xlsx", content_hash="new-uo-full-location-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertNotIn("📍", caption_text)


if __name__ == "__main__":
    unittest.main()
