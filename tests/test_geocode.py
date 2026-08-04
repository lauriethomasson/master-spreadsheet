"""
Regression test for geocode.py's early-return when a row already has real
coordinates (e.g. a provider spreadsheet's own Lat/Lng columns, mapped
straight through by extract_spreadsheet.py) - added alongside xlsx/csv
upload support, since calling out to the paid Geocoding/Places APIs for a
row that's already correctly geocoded would be wasted at best and a
regression (a worse guess overwriting a correct source-provided coordinate)
at worst.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_geocode -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema import ListingRow

import geocode


class SkipAlreadyGeocodedRowsTests(unittest.TestCase):
    def test_row_with_existing_lat_lng_never_calls_any_geocoding_api(self):
        row = ListingRow(building="City Tower", provider="Breezblok", lat=51.5, lng=-0.1)

        with patch("geocode.call_geocoding_api") as mock_geocoding, \
             patch("geocode.call_places_text_search") as mock_places:
            result = geocode.geocode_row(row)

        mock_geocoding.assert_not_called()
        mock_places.assert_not_called()
        self.assertEqual(result.lat, 51.5)
        self.assertEqual(result.lng, -0.1)

    def test_row_missing_lat_still_goes_through_the_normal_path(self):
        row = ListingRow(building="City Tower", provider="Breezblok", lat=None, lng=-0.1)

        with patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}) as mock_places:
            geocode.geocode_row(row)

        mock_places.assert_called_once()


class SplitCompoundBuildingTests(unittest.TestCase):
    def test_name_comma_address_is_compound(self):
        # The real Kitt's Availability building whose compound value
        # broke geocoding entirely (see CompoundBuildingGeocodingTests).
        self.assertEqual(
            geocode.split_compound_building("Bridge House, 22 Newman Street"),
            ("Bridge House", "22 Newman Street"),
        )

    def test_name_dash_address_is_compound(self):
        # The real UNION Clerkenwell & Farringdon building-name convention.
        self.assertEqual(
            geocode.split_compound_building("Straus Haus - 50 Great Sutton Street"),
            ("Straus Haus", "50 Great Sutton Street"),
        )

    def test_a_name_with_no_numbered_street_after_it_is_not_compound(self):
        # Real Kitt's value - "Rochester Mews" has no digit at all, so
        # there's no address portion to extract in the first place.
        self.assertIsNone(geocode.split_compound_building("The Rochester, Rochester Mews"))

    def test_a_plain_address_with_no_separator_is_not_compound(self):
        self.assertIsNone(geocode.split_compound_building("28 Bruton Street"))

    def test_a_plain_name_with_no_separator_is_not_compound(self):
        self.assertIsNone(geocode.split_compound_building("Kent House"))

    def test_a_digit_in_the_name_part_too_is_not_compound(self):
        # Ambiguous which part is really "the name" - stay conservative
        # rather than guess.
        self.assertIsNone(geocode.split_compound_building("Unit 5, 22 Newman Street"))


class CompoundBuildingGeocodingTests(unittest.TestCase):
    def setUp(self):
        geocode.FAILURES.clear()

    def test_compound_building_uses_the_address_only_result_when_it_succeeds(self):
        # Real Kitt's Availability case: the address portion alone
        # resolves correctly via the real Places API - the full compound
        # value ("Bridge House, 22 Newman Street, Fitzrovia, London, UK")
        # must never even be queried once the address-only attempt works.
        row = ListingRow(building="Bridge House, 22 Newman Street", submarket="Fitzrovia")

        def fake_places(query):
            if query == "22 Newman Street, Fitzrovia, London, UK":
                return {"status": "OK", "lat": 51.5176665, "lng": -0.1354706, "address_components": []}
            raise AssertionError(f"must not query the full compound value: {query!r}")

        with patch("geocode.call_places_text_search", side_effect=fake_places):
            geocode.geocode_row(row)

        self.assertEqual(row.lat, 51.5176665)
        self.assertEqual(row.lng, -0.1354706)

    def test_compound_building_falls_back_to_the_full_value_when_address_only_fails(self):
        row = ListingRow(building="Bridge House, 22 Newman Street", submarket="Fitzrovia")
        queried = []

        def fake_places(query):
            queried.append(query)
            if query == "22 Newman Street, Fitzrovia, London, UK":
                return {"status": "ZERO_RESULTS"}
            return {"status": "OK", "lat": 51.52, "lng": -0.09, "address_components": []}

        with patch("geocode.call_places_text_search", side_effect=fake_places):
            geocode.geocode_row(row)

        self.assertEqual(
            queried,
            ["22 Newman Street, Fitzrovia, London, UK", "Bridge House, 22 Newman Street, Fitzrovia, London, UK"],
        )
        self.assertEqual(row.lat, 51.52)
        self.assertEqual(row.lng, -0.09)

    def test_compound_building_logs_one_failure_when_both_attempts_fail(self):
        row = ListingRow(building="Bridge House, 22 Newman Street", submarket="Fitzrovia")

        with patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        self.assertIsNone(row.lat)
        self.assertEqual(len(geocode.FAILURES), 1)

    def test_non_compound_building_is_queried_only_once_not_twice(self):
        # No regression for the ordinary case - a plain building name
        # never triggers the address-only/fallback machinery at all.
        row = ListingRow(building="Kent House", submarket="Fitzrovia")

        with patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}) as mock_places:
            geocode.geocode_row(row)

        mock_places.assert_called_once_with("Kent House, Fitzrovia, London, UK")

    def test_address_only_success_still_backfills_address_1_and_postcode(self):
        # The compound-vs-not distinction only changes WHICH query wins -
        # everything downstream (address/postcode backfill from whichever
        # result is actually used) is unchanged.
        row = ListingRow(building="Whitfield Court, 30-32 Whitfield Street", submarket="Fitzrovia")
        components = [
            {"longText": "30-32", "types": ["street_number"]},
            {"longText": "Whitfield Street", "types": ["route"]},
            {"longText": "W1T 2RG", "types": ["postal_code"]},
        ]

        with patch("geocode.call_places_text_search", return_value={
            "status": "OK", "lat": 51.5200939, "lng": -0.1345432, "address_components": components,
        }):
            geocode.geocode_row(row)

        self.assertEqual(row.address_1, "30-32 Whitfield Street")
        self.assertEqual(row.postcode, "W1T 2RG")


if __name__ == "__main__":
    unittest.main()
