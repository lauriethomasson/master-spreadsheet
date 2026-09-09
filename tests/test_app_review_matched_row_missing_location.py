"""
Regression tests for extending the existing brand-new-property "missing
location" lookup (see tests/test_app_review_new_property_missing_location.py,
pages/2_Review_and_Master.py's own _render_missing_location_lookup) to
MATCHED rows too - a property already in master that ends up with a
genuinely blank address_1/postcode/lat/lng (its master record never had a
location, or this run's own geocode attempt came back with nothing usable)
previously surfaced no signal to a reviewer at all.

Deliberately reuses _render_missing_location_lookup/master_merge.
new_property_missing_location completely unchanged - only the NEW
"📍 Existing properties missing a location" section (pages/2_Review_and_
Master.py) and its own _matched_row_location_dict helper are new.

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_new_property_missing_location.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_matched_row_missing_location -v
"""

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import master_writer
from schema import ListingRow
from storage.file_store import save_staging_file
from streamlit.testing.v1 import AppTest

BASE = Path(__file__).resolve().parent.parent

_SECTION_HEADER = "📍 Existing properties missing a location"


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


class MatchedRowMissingLocationShownTests(IsolatedCwdTestCase):
    """A matched row (matched_unchanged or auto_matched - no other decision
    card of its own) with a genuinely blank location now gets the same
    lookup UI a brand-new row already gets."""

    def test_matched_unchanged_row_with_blank_location_shows_the_lookup_section(self):
        # Nothing at all differs between master and this upload - diffs={},
        # so this row lands in plan.matched_unchanged, which previously
        # rendered NOTHING per-row at all (just a bare count caption).
        master_writer.write_master([
            ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor",
                property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [ListingRow(building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor")],
            "matched_unchanged_no_location.xlsx", content_hash="matched-unchanged-no-location-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        self.assertIn(_SECTION_HEADER, [s.value for s in at.subheader])
        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("📍 Missing: address, postcode, map location", caption_text)
        # The exact same lookup widgets a brand-new row's own card offers.
        self.assertTrue(any(t.key == "matched_loc_0_loc_address" for t in at.text_input))
        self.assertTrue(any(b.label == "📍 Look up" for b in at.button))

    def test_auto_matched_row_with_blank_location_shows_the_lookup_section(self):
        # A real, safe, unrelated field DOES change (special_features) -
        # this row lands in auto_matched (silently auto-applied, with no
        # per-row card of its own today) rather than matched_unchanged,
        # proving the section isn't wired to matched_unchanged alone.
        master_writer.write_master([
            ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor",
                special_features="Bike racks", property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor",
                special_features="Bike racks; Roof terrace",
            )],
            "matched_auto_no_location.xlsx", content_hash="matched-auto-no-location-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        self.assertIn(_SECTION_HEADER, [s.value for s in at.subheader])
        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("📍 Missing: address, postcode, map location", caption_text)


class MatchedRowMissingLocationApplyTests(IsolatedCwdTestCase):
    """The interactive lookup/apply flow for a matched row - real Geocoding
    API calls mocked via geocode.call_geocoding_api, same boundary the
    brand-new flow's own tests already mock."""

    def test_accepted_lookup_is_applied_to_the_matched_row_written_to_master(self):
        pid = str(uuid.uuid4())
        master_writer.write_master([
            ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor", property_id=pid,
            ),
        ])
        save_staging_file(
            [ListingRow(building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor")],
            "matched_apply_no_location.xlsx", content_hash="matched-apply-no-location-hash",
        )
        with patch(
            "geocode.call_geocoding_api",
            return_value={
                "status": "OK", "lat": 51.5219197, "lng": -0.1077003,
                "address_components": [
                    {"long_name": "67", "types": ["street_number"]},
                    {"long_name": "Clerkenwell Road", "types": ["route"]},
                    {"long_name": "EC1R 5BL", "types": ["postal_code"]},
                ],
            },
        ):
            at = _run_review_page()
            at.text_input(key="matched_loc_0_loc_address").set_value("67 Clerkenwell Rd").run()
            lookup_buttons = [b for b in at.button if b.label == "📍 Look up"]
            lookup_buttons[0].click().run()
            self.assertFalse(at.exception)

            use_buttons = [b for b in at.button if b.label == "✓ Use this location"]
            self.assertEqual(len(use_buttons), 1)
            use_buttons[0].click().run()
            self.assertFalse(at.exception)

            approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
            approve_buttons[0].click().run()
            self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)
        row = master_df.iloc[0]
        self.assertEqual(row["property_id"], pid)
        self.assertEqual(row["building"], "9 Example Yard")
        self.assertEqual(row["address_1"], "67 Clerkenwell Road")
        self.assertEqual(row["postcode"], "EC1R 5BL")
        self.assertAlmostEqual(row["lat"], 51.5219197)
        self.assertAlmostEqual(row["lng"], -0.1077003)
        self.assertEqual(row["geocode_unverified"], False)

    def test_a_lookup_never_confirmed_leaves_the_master_row_exactly_as_it_was(self):
        pid = str(uuid.uuid4())
        master_writer.write_master([
            ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor", property_id=pid,
            ),
        ])
        save_staging_file(
            [ListingRow(building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor")],
            "matched_unconfirmed_no_location.xlsx", content_hash="matched-unconfirmed-no-location-hash",
        )
        with patch(
            "geocode.call_geocoding_api",
            return_value={
                "status": "OK", "lat": 51.5219197, "lng": -0.1077003,
                "address_components": [
                    {"long_name": "67", "types": ["street_number"]},
                    {"long_name": "Clerkenwell Road", "types": ["route"]},
                    {"long_name": "EC1R 5BL", "types": ["postal_code"]},
                ],
            },
        ):
            at = _run_review_page()
            at.text_input(key="matched_loc_0_loc_address").set_value("67 Clerkenwell Rd").run()
            lookup_buttons = [b for b in at.button if b.label == "📍 Look up"]
            lookup_buttons[0].click().run()
            self.assertFalse(at.exception)
            self.assertTrue(any(b.label == "✓ Use this location" for b in at.button))

            approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
            approve_buttons[0].click().run()
            self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)
        row = master_df.iloc[0]
        self.assertEqual(row["property_id"], pid)
        self.assertTrue(row["address_1"] is None or str(row["address_1"]) == "nan" or row["address_1"] == "")


class MatchedRowLocationAlreadyPresentTests(IsolatedCwdTestCase):
    """A matched row that already has a real, complete location is
    completely unaffected - no lookup UI, exactly as today."""

    def test_matched_row_with_full_location_gets_no_lookup_section(self):
        master_writer.write_master([
            ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor",
                address_1="9 Example Yard", postcode="EC1A 1AA", lat=51.5, lng=-0.1,
                property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [ListingRow(building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor")],
            "matched_full_location.xlsx", content_hash="matched-full-location-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        self.assertNotIn(_SECTION_HEADER, [s.value for s in at.subheader])
        caption_text = "".join(c.value for c in at.caption)
        self.assertNotIn("📍 Missing", caption_text)


class MatchedRowGeocodeUnverifiedStaysSeparateTests(IsolatedCwdTestCase):
    """geocode_unverified=True (a present-but-uncertain guess) is a
    completely separate signal from "blank" - a matched row with a real,
    non-blank address_1/postcode/lat/lng must never get this treatment
    just because it's also flagged unverified."""

    def test_unverified_but_non_blank_address_does_not_get_the_lookup_section(self):
        master_writer.write_master([
            ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor",
                address_1="9 Example Yard", postcode="EC1A 1AA", lat=51.5, lng=-0.1,
                geocode_unverified=False, property_id=str(uuid.uuid4()),
            ),
        ])
        # Same address/postcode/lat/lng as master (no real change there),
        # just this run's own geocode_unverified flag flipping true - never
        # a reason to treat the row as missing its location.
        save_staging_file(
            [ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor",
                address_1="9 Example Yard", postcode="EC1A 1AA", lat=51.5, lng=-0.1,
                geocode_unverified=True,
            )],
            "matched_unverified_present.xlsx", content_hash="matched-unverified-present-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        self.assertNotIn(_SECTION_HEADER, [s.value for s in at.subheader])
        caption_text = "".join(c.value for c in at.caption)
        self.assertNotIn("📍 Missing", caption_text)


if __name__ == "__main__":
    unittest.main()
