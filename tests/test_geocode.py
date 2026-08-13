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
        # submarket is already set here specifically so the new submarket
        # backfill (see SubmarketBackfillTests) has nothing to do - this
        # test is only about the lat/lng skip itself.
        row = ListingRow(building="City Tower", provider="Breezblok", lat=51.5, lng=-0.1, submarket="The City")

        with patch("geocode.call_geocoding_api") as mock_geocoding, \
             patch("geocode.call_places_text_search") as mock_places, \
             patch("geocode.call_reverse_geocoding_api") as mock_reverse:
            result = geocode.geocode_row(row)

        mock_geocoding.assert_not_called()
        mock_places.assert_not_called()
        mock_reverse.assert_not_called()
        self.assertEqual(result.lat, 51.5)
        self.assertEqual(result.lng, -0.1)

    def test_row_missing_lat_still_goes_through_the_normal_path(self):
        row = ListingRow(building="City Tower", provider="Breezblok", lat=None, lng=-0.1)

        with patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}) as mock_places:
            geocode.geocode_row(row)

        mock_places.assert_called_once()


class SubmarketBackfillTests(unittest.TestCase):
    """
    Google's Geocoding/Places APIs never return neighbourhood-level detail
    in a forward lookup's own top result (confirmed against real UNION rows
    missing submarket - the most specific component there is
    postal_town="London", not useful) - but a reverse-geocode of the
    resolved coordinates pools several results together and does surface a
    "sublocality"/"sublocality_level_1" component with the real neighbourhood
    name (confirmed "Mayfair", "Fitzrovia", "Soho" for real addresses on
    Dering Street, Mortimer Street, Wardour Street respectively). See
    _submarket_from_components/_backfill_submarket_from_coords.
    """

    def setUp(self):
        geocode.FAILURES.clear()

    _SUBLOCALITY_COMPONENTS = [
        {"long_name": "Fitzrovia", "types": ["political", "sublocality", "sublocality_level_1"]},
        {"long_name": "London", "types": ["postal_town"]},
    ]

    def test_row_with_existing_lat_lng_but_no_submarket_gets_backfilled(self):
        row = ListingRow(building="City Tower", provider="Breezblok", lat=51.5188, lng=-0.1381)

        with patch(
            "geocode.call_reverse_geocoding_api",
            return_value={"status": "OK", "address_components": self._SUBLOCALITY_COMPONENTS},
        ) as mock_reverse:
            geocode.geocode_row(row)

        mock_reverse.assert_called_once_with(51.5188, -0.1381)
        self.assertEqual(row.submarket, "Fitzrovia")

    def test_never_overwrites_a_genuinely_extracted_submarket(self):
        row = ListingRow(building="City Tower", provider="Breezblok", lat=51.5188, lng=-0.1381, submarket="Noho")

        with patch("geocode.call_reverse_geocoding_api") as mock_reverse:
            geocode.geocode_row(row)

        mock_reverse.assert_not_called()
        self.assertEqual(row.submarket, "Noho")

    def test_tier_1_geocoding_api_success_backfills_submarket(self):
        row = ListingRow(building="16 Mortimer Street", address_1="16 Mortimer Street", postcode="W1T 3JL")

        with patch("geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.5188, "lng": -0.1381}), \
             patch(
                 "geocode.call_reverse_geocoding_api",
                 return_value={"status": "OK", "address_components": self._SUBLOCALITY_COMPONENTS},
             ):
            geocode.geocode_row(row)

        self.assertEqual(row.submarket, "Fitzrovia")

    def test_tier_2_places_success_backfills_submarket_with_a_single_reverse_call(self):
        # Places' own record has neither address_1/postcode nor submarket -
        # both are missing, so both must be filled from ONE shared reverse-
        # geocode call, not two separate ones.
        row = ListingRow(building="Kent House")
        reverse_components = [
            {"long_name": "16", "types": ["street_number"]},
            {"long_name": "Mortimer Street", "types": ["route"]},
            {"long_name": "W1T 3JL", "types": ["postal_code"]},
            {"long_name": "Fitzrovia", "types": ["political", "sublocality", "sublocality_level_1"]},
        ]

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.5188, "lng": -0.1381, "address_components": []},
        ), patch(
            "geocode.call_reverse_geocoding_api",
            return_value={"status": "OK", "address_components": reverse_components},
        ) as mock_reverse:
            geocode.geocode_row(row)

        mock_reverse.assert_called_once()
        self.assertEqual(row.address_1, "16 Mortimer Street")
        self.assertEqual(row.postcode, "W1T 3JL")
        self.assertEqual(row.submarket, "Fitzrovia")


class SourceAreaHintSubmarketTests(unittest.TestCase):
    """
    Regression tests for the confirmed Part C gap: submarket previously
    only ever came from Google's reverse-geocode sublocality/neighborhood
    component, even when the source's own text already stated a locality
    (e.g. "Nutmeg House \\nLondon Bridge SE1", the same real newline
    convention extract_postcode_hint already trusts) that Google's own
    coverage simply doesn't reach for every real address.
    """

    def setUp(self):
        geocode.FAILURES.clear()

    def test_explicit_source_submarket_survives_unchanged(self):
        row = ListingRow(
            building="Nutmeg House \nLondon Bridge SE1", provider="beem",
            lat=51.505, lng=-0.087, submarket="Bermondsey",
        )

        with patch("geocode.call_reverse_geocoding_api") as mock_reverse:
            geocode.geocode_row(row)

        mock_reverse.assert_not_called()
        self.assertEqual(row.submarket, "Bermondsey")

    def test_source_locality_hint_backfills_blank_submarket_with_no_api_call(self):
        row = ListingRow(building="Nutmeg House \nLondon Bridge SE1", provider="beem", lat=51.505, lng=-0.087)

        with patch("geocode.call_reverse_geocoding_api") as mock_reverse:
            geocode.geocode_row(row)

        mock_reverse.assert_not_called()
        self.assertEqual(row.submarket, "London Bridge")

    def test_google_neighbourhood_still_backfills_when_source_has_no_locality_hint(self):
        # "City Tower" has no separator/locality hint at all - unaffected,
        # Google's own reverse-geocode result is still used exactly as
        # before this change.
        row = ListingRow(building="City Tower", provider="Breezblok", lat=51.5188, lng=-0.1381)
        components = [{"long_name": "Fitzrovia", "types": ["political", "sublocality", "sublocality_level_1"]}]

        with patch(
            "geocode.call_reverse_geocoding_api", return_value={"status": "OK", "address_components": components},
        ) as mock_reverse:
            geocode.geocode_row(row)

        mock_reverse.assert_called_once()
        self.assertEqual(row.submarket, "Fitzrovia")

    def test_a_source_locality_hint_outranks_a_blank_google_sublocality(self):
        # Confirmed real shape: valid lat/lng, but Google's own reverse-
        # geocode simply has no sublocality/neighborhood component for this
        # address - the source's own locality text must still win, and the
        # (would-be-empty) Google call is never even made.
        row = ListingRow(building="Nutmeg House \nLondon Bridge SE1", provider="beem", lat=51.505, lng=-0.087)

        with patch(
            "geocode.call_reverse_geocoding_api",
            return_value={"status": "OK", "address_components": [{"long_name": "London", "types": ["postal_town"]}]},
        ) as mock_reverse:
            geocode.geocode_row(row)

        mock_reverse.assert_not_called()
        self.assertEqual(row.submarket, "London Bridge")

    def test_ambiguous_free_text_with_no_separator_never_becomes_a_guessed_submarket(self):
        # "New Derwent House WC1" has no comma/newline - extract_area_hint
        # stays conservative and returns nothing, so this must fall through
        # to Google's own reverse-geocode exactly as before, never guess a
        # word out of the unbroken building text.
        row = ListingRow(building="New Derwent House WC1", provider="beem", lat=51.52, lng=-0.12)
        components = [{"long_name": "Holborn", "types": ["political", "sublocality", "sublocality_level_1"]}]

        with patch(
            "geocode.call_reverse_geocoding_api", return_value={"status": "OK", "address_components": components},
        ) as mock_reverse:
            geocode.geocode_row(row)

        mock_reverse.assert_called_once()
        self.assertEqual(row.submarket, "Holborn")

    def test_no_provider_specific_code_is_involved(self):
        # Same locality-hint backfill for a provider never seen before -
        # extract_area_hint/_source_area_hint have no provider branching at
        # all (see geocode.py's own module docstring on this).
        row = ListingRow(building="Any Building \nSoho W1", provider="A Brand New Agent", lat=51.51, lng=-0.13)

        with patch("geocode.call_reverse_geocoding_api") as mock_reverse:
            geocode.geocode_row(row)

        mock_reverse.assert_not_called()
        self.assertEqual(row.submarket, "Soho")


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


class GeocodeRowsGroupingTests(unittest.TestCase):
    """
    geocode_rows (the batch entry point, distinct from geocode_row) groups
    rows sharing a (building, provider) identity and geocodes each group
    ONCE, copying the result into every member's own blank fields - see its
    own docstring for the real Nexus Place case this exists for (the same
    physical building, listed as two intentionally SEPARATE source rows
    under two different submarkets - see master_merge.py's own source-row-
    identity handling - getting two DIFFERENT postcodes purely because each
    row's own submarket biased its Tier 2 Places query differently).
    """

    def setUp(self):
        geocode.FAILURES.clear()

    def test_same_building_and_provider_geocoded_only_once(self):
        row_a = ListingRow(building="Nexus Place", provider="UNION", submarket="City")
        row_b = ListingRow(building="Nexus Place", provider="UNION", submarket="Clerkenwell")

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.52, "lng": -0.1, "address_components": []},
        ) as mock_places:
            geocode.geocode_rows([row_a, row_b])

        mock_places.assert_called_once()
        self.assertEqual(row_a.lat, 51.52)
        self.assertEqual(row_b.lat, 51.52)
        self.assertEqual(row_a.lng, -0.1)
        self.assertEqual(row_b.lng, -0.1)

    def test_submarket_is_never_copied_across_the_group(self):
        # The whole point: two rows can share a physical location while
        # keeping their own, genuinely different, source-stated submarket -
        # location consistency must never homogenize that field.
        row_a = ListingRow(building="Nexus Place", provider="UNION", submarket="City")
        row_b = ListingRow(building="Nexus Place", provider="UNION", submarket="Clerkenwell & Farringdon")

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.52, "lng": -0.1, "address_components": []},
        ):
            geocode.geocode_rows([row_a, row_b])

        self.assertEqual(row_a.submarket, "City")
        self.assertEqual(row_b.submarket, "Clerkenwell & Farringdon")

    def test_address_1_and_postcode_are_also_shared(self):
        row_a = ListingRow(building="Nexus Place", provider="UNION", submarket="City")
        row_b = ListingRow(building="Nexus Place", provider="UNION", submarket="Clerkenwell")
        components = [
            {"longText": "25", "types": ["street_number"]},
            {"longText": "Farringdon Street", "types": ["route"]},
            {"longText": "EC4M 4AB", "types": ["postal_code"]},
        ]

        with patch("geocode.call_places_text_search", return_value={
            "status": "OK", "lat": 51.52, "lng": -0.1, "address_components": components,
        }):
            geocode.geocode_rows([row_a, row_b])

        self.assertEqual(row_a.address_1, "25 Farringdon Street")
        self.assertEqual(row_b.address_1, "25 Farringdon Street")
        self.assertEqual(row_a.postcode, "EC4M 4AB")
        self.assertEqual(row_b.postcode, "EC4M 4AB")

    def test_a_row_with_its_own_stated_address_becomes_the_group_representative(self):
        # Tier 1 (deterministic, from a genuinely stated address) is
        # preferred over ever running the hint-biased Tier 2 Places search
        # at all for this group.
        row_a = ListingRow(building="Nexus Place", provider="UNION", submarket="City")
        row_b = ListingRow(
            building="Nexus Place", provider="UNION", submarket="Clerkenwell",
            address_1="25 Farringdon Street", postcode="EC4M 4AB",
        )

        with patch(
            "geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.52, "lng": -0.1},
        ) as mock_geocoding, patch("geocode.call_places_text_search") as mock_places:
            geocode.geocode_rows([row_a, row_b])

        mock_geocoding.assert_called_once()
        mock_places.assert_not_called()
        self.assertEqual(row_a.address_1, "25 Farringdon Street")
        self.assertEqual(row_a.postcode, "EC4M 4AB")
        self.assertEqual(row_a.lat, 51.52)

    def test_a_row_with_existing_lat_lng_becomes_the_shared_answer_with_no_api_call(self):
        row_a = ListingRow(building="Nexus Place", provider="UNION", lat=51.52, lng=-0.1, submarket="City")
        row_b = ListingRow(building="Nexus Place", provider="UNION", submarket="Clerkenwell")

        with patch("geocode.call_geocoding_api") as mock_geocoding, \
             patch("geocode.call_places_text_search") as mock_places, \
             patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_rows([row_a, row_b])

        mock_geocoding.assert_not_called()
        mock_places.assert_not_called()
        self.assertEqual(row_b.lat, 51.52)
        self.assertEqual(row_b.lng, -0.1)

    def test_representative_failure_falls_back_to_independent_attempts(self):
        # The group's one shared attempt found nothing - rather than
        # silently failing the whole group, each OTHER member still gets
        # its own independent try (the pre-grouping behavior), and one of
        # them succeeding must not be lost.
        row_a = ListingRow(building="Nexus Place", provider="UNION", submarket="City")
        row_b = ListingRow(building="Nexus Place", provider="UNION", submarket="Clerkenwell")

        with patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}) as mock_places:
            geocode.geocode_rows([row_a, row_b])

        self.assertEqual(mock_places.call_count, 2)
        self.assertIsNone(row_a.lat)
        self.assertIsNone(row_b.lat)

    def test_different_buildings_remain_independent_groups(self):
        row_a = ListingRow(building="Nexus Place", provider="UNION")
        row_b = ListingRow(building="Totally Different Building", provider="UNION")

        def fake_places(query):
            if "Nexus" in query:
                return {"status": "OK", "lat": 51.52, "lng": -0.1, "address_components": []}
            return {"status": "OK", "lat": 51.50, "lng": -0.2, "address_components": []}

        with patch("geocode.call_places_text_search", side_effect=fake_places) as mock_places:
            geocode.geocode_rows([row_a, row_b])

        self.assertEqual(mock_places.call_count, 2)
        self.assertEqual(row_a.lat, 51.52)
        self.assertEqual(row_b.lat, 51.50)

    def test_different_providers_for_the_same_building_name_remain_independent_groups(self):
        # Same precedent master_merge.py's own _fallback_key already trusts
        # elsewhere - building alone is never enough identity evidence
        # without provider too.
        row_a = ListingRow(building="Kent House", provider="UNION")
        row_b = ListingRow(building="Kent House", provider="Some Other Agent")

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.52, "lng": -0.1, "address_components": []},
        ) as mock_places:
            geocode.geocode_rows([row_a, row_b])

        self.assertEqual(mock_places.call_count, 2)

    def test_a_rows_own_stated_field_is_never_overwritten_by_the_group(self):
        row_a = ListingRow(
            building="Nexus Place", provider="UNION", submarket="City",
            address_1="Row A's own stated address", postcode="AA1 1AA",
        )
        row_b = ListingRow(building="Nexus Place", provider="UNION", submarket="Clerkenwell")

        with patch(
            "geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.1, "lng": -0.05},
        ):
            geocode.geocode_rows([row_a, row_b])

        self.assertEqual(row_a.address_1, "Row A's own stated address")
        self.assertEqual(row_a.postcode, "AA1 1AA")

    def test_blank_building_rows_are_never_grouped_with_anything(self):
        row_a = ListingRow(building="", provider="UNION")
        row_b = ListingRow(building="", provider="UNION")

        with patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}) as mock_places:
            geocode.geocode_rows([row_a, row_b])

        # Blank building means geocode_row's own Tier 2 branch never even
        # runs (see geocode_row's own "if row.building:" guard) - confirms
        # this grouping wrapper doesn't change that for either row.
        mock_places.assert_not_called()


if __name__ == "__main__":
    unittest.main()
