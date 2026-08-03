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


if __name__ == "__main__":
    unittest.main()
