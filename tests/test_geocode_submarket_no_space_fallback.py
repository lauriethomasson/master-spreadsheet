"""
Regression test for geocode.py's submarket no-space fallback query (see
_submarket_query_variants) - added after a real, confirmed failure: a real
MetSpace email lists "Adler House" under its own "MID TOWN" section
header, with no address_1/postcode stated anywhere in the source at all,
so geocode_row's Tier 2 (Places Text Search fallback) is the only path
available, using submarket="Mid Town" (the correct, unaltered extraction).

Querying the real Google Places API (New) Text Search directly (not
mocked - a one-off manual check, reproduced here as a mocked regression
test using the exact real response shapes observed):
- "Adler House, Mid Town, London, UK" (the stated, correct two-word
  submarket) returns 9 ambiguous candidates, including two different
  wrong buildings - "Arundel St, Temple, London WC2R 3DX" (candidate #0)
  and "2 Electric Blvd, Nine Elms, London SW11 8BQ" (candidate #1), both
  comfortably inside the London bbox with no source hint to reject either
  one, so the FIRST of these gets accepted, wrongly, today.
- "Adler House, MidTown, London, UK" (no space - what row.submarket.
  replace(" ", "") actually produces, confirmed to behave identically to
  a manually-typed lowercase "Midtown") returns exactly ONE candidate:
  the real Adler House in Holborn, WC1R 4AQ.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_geocode_submarket_no_space_fallback -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema import ListingRow

import geocode

# Real candidates observed from the actual Places API for the spaced query -
# trimmed to the two real wrong buildings actually seen (candidates #0/#1)
# plus one filler, since the exact remaining count doesn't matter here -
# what matters is that NEITHER is the real Adler House, and the first one
# gets accepted today with nothing to reject it.
_SPACED_QUERY_CANDIDATES = [
    {
        "lat": 51.5114873, "lng": -0.1136097,
        "formatted_address": "Arundel St, Temple, London WC2R 3DX, UK",
        "address_components": [{"longText": "WC2R 3DX", "types": ["postal_code"]}],
    },
    {
        "lat": 51.4808625, "lng": -0.1452928,
        "formatted_address": "2 Electric Blvd, Nine Elms, London SW11 8BQ, UK",
        "address_components": [{"longText": "SW11 8BQ", "types": ["postal_code"]}],
    },
    {
        "lat": 51.5090205, "lng": -0.1324229,
        "formatted_address": "57-59 Haymarket, London SW1Y 4QX, UK",
        "address_components": [{"longText": "SW1Y 4QX", "types": ["postal_code"]}],
    },
]

_NO_SPACE_QUERY_CANDIDATE = {
    "lat": 51.5187707, "lng": -0.1171297,
    "formatted_address": "Adler House, London WC1R 4AQ, UK",
    "address_components": [{"longText": "WC1R 4AQ", "types": ["postal_code"]}],
}


def _fake_places(query: str) -> dict:
    if "MidTown" in query:
        candidates = [_NO_SPACE_QUERY_CANDIDATE]
    elif "Mid Town" in query:
        candidates = _SPACED_QUERY_CANDIDATES
    else:
        return {"status": "ZERO_RESULTS"}
    return {"status": "OK", "candidates": candidates, **candidates[0]}


class AdlerHouseRealCaseTests(unittest.TestCase):
    """The exact real case: no address_1/postcode stated, building="Adler
    House", submarket="Mid Town" (as genuinely extracted from the real
    MetSpace email) - must now resolve to the real Holborn location."""

    def setUp(self):
        geocode.FAILURES.clear()

    def test_resolves_to_the_real_holborn_location_not_one_of_the_wrong_candidates(self):
        row = ListingRow(building="Adler House", provider="MetSpace", submarket="Mid Town")

        # The real Places match has no street_number/route component (a
        # premise-only record, like the module's own documented "Kent
        # House" case) - address_1 stays blank, which would otherwise
        # trigger a real reverse-geocode network call; mocked here to
        # keep this test offline, same as every other test in this suite.
        with patch("geocode.call_places_text_search", side_effect=_fake_places) as mock_places, \
             patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        self.assertEqual(row.lat, 51.5187707)
        self.assertEqual(row.lng, -0.1171297)
        self.assertEqual(row.postcode, "WC1R 4AQ")
        # Definitely not either of the two real wrong answers previously
        # observed (master's own "Arundel Street", and a fresh extraction's
        # own "2 Electric Boulevard").
        self.assertNotEqual((row.lat, row.lng), (51.5114873, -0.1136097))
        self.assertNotEqual((row.lat, row.lng), (51.4808625, -0.1452928))

        # The no-space query is tried FIRST and succeeds immediately - the
        # spaced (stated) query is never even needed for this exact row.
        mock_places.assert_called_once()
        self.assertIn("MidTown", mock_places.call_args.args[0])

    def test_submarket_itself_is_left_completely_unaltered(self):
        # This fix only changes which STRING gets sent to Places - the
        # row's own extracted submarket value is never touched.
        row = ListingRow(building="Adler House", provider="MetSpace", submarket="Mid Town")

        with patch("geocode.call_places_text_search", side_effect=_fake_places), \
             patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        self.assertEqual(row.submarket, "Mid Town")


class SubmarketQueryVariantsUnitTests(unittest.TestCase):
    """Pure unit tests of _submarket_query_variants - no API calls."""

    def test_multi_word_submarket_tries_no_space_first_then_the_original(self):
        self.assertEqual(geocode._submarket_query_variants("Mid Town"), ["MidTown", "Mid Town"])

    def test_single_word_submarket_has_no_second_variant(self):
        self.assertEqual(geocode._submarket_query_variants("Fitzrovia"), ["Fitzrovia"])

    def test_no_submarket_at_all_returns_none_placeholder(self):
        self.assertEqual(geocode._submarket_query_variants(None), [None])
        self.assertEqual(geocode._submarket_query_variants(""), [None])

    def test_general_not_hardcoded_to_mid_town(self):
        # Confirms this is a generic space-stripping transform, not a
        # special case for this one building/submarket.
        self.assertEqual(
            geocode._submarket_query_variants("Old Street"), ["OldStreet", "Old Street"],
        )
        self.assertEqual(
            geocode._submarket_query_variants("Clerkenwell & Farringdon"),
            ["Clerkenwell&Farringdon", "Clerkenwell & Farringdon"],
        )


class ExistingBehaviorUnaffectedTests(unittest.TestCase):
    """No regression for a row whose spaced submarket query already
    resolves correctly today - the no-space attempt is tried first, but a
    query-agnostic mock (matching how several existing tests in this
    suite are already written) proves the eventual accepted result is
    identical either way."""

    def setUp(self):
        geocode.FAILURES.clear()

    def test_a_matching_district_is_still_accepted_the_same_way(self):
        row = ListingRow(building="Clove \nLondon Bridge SE1", provider="beem", submarket="London Bridge")
        components = [
            {"longText": "4", "types": ["street_number"]},
            {"longText": "Maguire Street", "types": ["route"]},
            {"longText": "SE1 2NQ", "types": ["postal_code"]},
        ]

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.5023335, "lng": -0.0723225, "address_components": components},
        ):
            geocode.geocode_row(row)

        self.assertEqual(row.lat, 51.5023335)
        self.assertEqual(row.postcode, "SE1 2NQ")
        self.assertEqual(geocode.FAILURES, [])


if __name__ == "__main__":
    unittest.main()
