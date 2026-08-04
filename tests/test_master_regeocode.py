"""
Regression tests for master_regeocode.py - finding master rows worth
re-checking against the current geocode_row logic (compound building
values, or rows with no coordinates at all) and re-running geocoding for a
chosen selection, in the same (merged_rows, diff_rows, fields_changed)
shape master_merge.build_manual_edit produces.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_master_regeocode -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import master_regeocode


def _record(building=None, lat=None, lng=None, address_1=None, postcode=None, provider="P"):
    return {
        "building": building, "provider": provider, "lat": lat, "lng": lng,
        "address_1": address_1, "postcode": postcode, "floor_unit": None,
        "submarket": None, "property_id": "id-1",
    }


class FindSuspectRowsTests(unittest.TestCase):
    def test_a_compound_building_is_flagged(self):
        records = [_record(building="Bridge House, 22 Newman Street", lat=51.5, lng=-0.1)]

        suspects = master_regeocode.find_suspect_rows(records)

        self.assertEqual(len(suspects), 1)
        self.assertIn("compound building", suspects[0]["reason"])

    def test_missing_coordinates_is_flagged_even_for_a_plain_building_name(self):
        records = [_record(building="Kent House", lat=None, lng=None)]

        suspects = master_regeocode.find_suspect_rows(records)

        self.assertEqual(len(suspects), 1)
        self.assertIn("no coordinates on file", suspects[0]["reason"])

    def test_a_clean_row_with_a_plain_building_and_real_coordinates_is_not_flagged(self):
        records = [_record(building="Kent House", lat=51.5, lng=-0.1)]

        self.assertEqual(master_regeocode.find_suspect_rows(records), [])

    def test_both_reasons_are_combined_for_a_compound_building_missing_coordinates(self):
        records = [_record(building="Bridge House, 22 Newman Street", lat=None, lng=None)]

        suspects = master_regeocode.find_suspect_rows(records)

        self.assertEqual(len(suspects), 1)
        self.assertIn("compound building", suspects[0]["reason"])
        self.assertIn("no coordinates on file", suspects[0]["reason"])

    def test_index_matches_position_in_the_records_list(self):
        records = [
            _record(building="Kent House", lat=51.5, lng=-0.1),
            _record(building="Bridge House, 22 Newman Street", lat=51.5, lng=-0.1),
        ]

        suspects = master_regeocode.find_suspect_rows(records)

        self.assertEqual(suspects[0]["index"], 1)


class RegeocodeRowsTests(unittest.TestCase):
    def test_a_successful_regeocode_updates_lat_lng_address_and_postcode(self):
        records = [_record(building="Bridge House, 22 Newman Street", lat=None, lng=None)]

        def fake_geocode_row(row):
            row.lat = 51.5176665
            row.lng = -0.1354706
            row.address_1 = "22 Newman Street"
            row.postcode = "W1T 1PG"
            return row

        with patch("geocode.geocode_row", side_effect=fake_geocode_row):
            merged_rows, diff_rows, fields_changed = master_regeocode.regeocode_rows(records, [0])

        self.assertEqual(fields_changed, 4)
        self.assertEqual(merged_rows[0].lat, 51.5176665)
        self.assertEqual(merged_rows[0].address_1, "22 Newman Street")
        self.assertEqual({d["field"] for d in diff_rows}, {"lat", "lng", "address_1", "postcode"})

    def test_lat_lng_are_cleared_before_calling_geocode_row(self):
        # geocode_row short-circuits immediately if lat/lng are already
        # populated (see its own early-return) - a re-geocode pass MUST
        # clear them first, or every row would be a permanent no-op.
        records = [_record(building="Kent House", lat=51.52, lng=-0.09)]
        seen_lat_lng = []

        def fake_geocode_row(row):
            seen_lat_lng.append((row.lat, row.lng))
            row.lat, row.lng = 51.5, -0.1
            return row

        with patch("geocode.geocode_row", side_effect=fake_geocode_row):
            master_regeocode.regeocode_rows(records, [0])

        self.assertEqual(seen_lat_lng, [(None, None)])

    def test_a_row_where_nothing_actually_changes_produces_no_diff_or_update(self):
        # Idempotency: re-running this pass on a row the fix doesn't
        # affect must never manufacture a spurious diff/version.
        records = [_record(building="28 Bruton Street", lat=51.51, lng=-0.14, address_1="28 Bruton Street", postcode="W1J 6QP")]

        def fake_geocode_row(row):
            row.lat, row.lng = 51.51, -0.14  # identical to before
            row.address_1, row.postcode = "28 Bruton Street", "W1J 6QP"
            return row

        with patch("geocode.geocode_row", side_effect=fake_geocode_row):
            merged_rows, diff_rows, fields_changed = master_regeocode.regeocode_rows(records, [0])

        self.assertEqual(fields_changed, 0)
        self.assertEqual(diff_rows, [])

    def test_unrelated_fields_are_never_touched(self):
        records = [_record(building="Bridge House, 22 Newman Street", provider="Kitt's", lat=None, lng=None)]

        def fake_geocode_row(row):
            row.lat, row.lng = 51.5176665, -0.1354706
            return row

        with patch("geocode.geocode_row", side_effect=fake_geocode_row):
            merged_rows, _, _ = master_regeocode.regeocode_rows(records, [0])

        self.assertEqual(merged_rows[0].provider, "Kitt's")
        self.assertEqual(merged_rows[0].building, "Bridge House, 22 Newman Street")

    def test_only_the_selected_indices_are_regeocoded(self):
        records = [
            _record(building="Kent House", lat=None, lng=None),
            _record(building="Bridge House, 22 Newman Street", lat=None, lng=None),
        ]

        with patch("geocode.geocode_row") as mock_geocode:
            master_regeocode.regeocode_rows(records, [1])

        mock_geocode.assert_called_once()

    def test_a_compound_buildings_stale_address_never_lets_tier_1_reconfirm_it(self):
        # Regression for a real bug found via live testing against the
        # actual API: leaving a compound building's existing (possibly
        # wrong) address_1/postcode in place let the REAL geocode_row's
        # Tier 1 (Geocoding API, used whenever address_1+postcode are
        # both already present) just re-confirm that same stale address,
        # never reaching Tier 2's fixed compound-address-first logic at
        # all - so this deliberately exercises the real geocode_row (only
        # the two low-level API-call functions are mocked), not a mocked
        # geocode_row, since that's the only way this bug was catchable.
        records = [_record(
            building="Imperial House, 8 Kean Street", provider="Kitt's",
            lat=51.52, lng=-0.1, address_1="16 Baldwin's Gardens", postcode="EC1N 7RJ",
        )]
        records[0]["submarket"] = "Holborn"

        with patch("geocode.call_geocoding_api") as mock_tier1, \
             patch("geocode.call_places_text_search", return_value={
                 "status": "OK", "lat": 51.5137627, "lng": -0.1182998,
                 "address_components": [
                     {"longText": "8", "types": ["street_number"]},
                     {"longText": "Kean Street", "types": ["route"]},
                     {"longText": "WC2B 4AS", "types": ["postal_code"]},
                 ],
             }) as mock_tier2:
            merged_rows, diff_rows, fields_changed = master_regeocode.regeocode_rows(records, [0])

        mock_tier1.assert_not_called()
        mock_tier2.assert_called_once_with("8 Kean Street, Holborn, London, UK")
        self.assertEqual(merged_rows[0].address_1, "8 Kean Street")
        self.assertEqual(merged_rows[0].postcode, "WC2B 4AS")

    def test_a_plain_buildings_genuine_address_is_never_cleared_before_regeocoding(self):
        # Only a COMPOUND building's address_1/postcode is suspect enough
        # to clear first - a plain building name missing just its
        # coordinates (e.g. a PDF-sourced row where Gemini read a real
        # address straight off the page) has no reason to distrust its
        # existing address_1/postcode, and Tier 1 correctly reusing them
        # is the normal, desired path, not a bug to work around.
        records = [_record(building="Kent House", lat=None, lng=None, address_1="1 Real Street", postcode="W1A 1AA")]

        with patch("geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.5, "lng": -0.1}) as mock_tier1, \
             patch("geocode.call_places_text_search") as mock_tier2:
            master_regeocode.regeocode_rows(records, [0])

        mock_tier1.assert_called_once_with("1 Real Street, W1A 1AA, UK")
        mock_tier2.assert_not_called()

    def test_rows_not_selected_pass_through_unchanged_in_merged_rows(self):
        records = [
            _record(building="Untouched Building", lat=51.5, lng=-0.1),
            _record(building="Bridge House, 22 Newman Street", lat=None, lng=None),
        ]

        def fake_geocode_row(row):
            row.lat, row.lng = 51.5176665, -0.1354706
            return row

        with patch("geocode.geocode_row", side_effect=fake_geocode_row):
            merged_rows, _, _ = master_regeocode.regeocode_rows(records, [1])

        self.assertEqual(merged_rows[0].building, "Untouched Building")
        self.assertEqual(merged_rows[0].lat, 51.5)


if __name__ == "__main__":
    unittest.main()
