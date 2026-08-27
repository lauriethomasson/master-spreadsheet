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
from unittest.mock import MagicMock, patch

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
        # This run never actually checked anything - it must leave
        # geocode_unverified completely untouched (still None, never
        # explicit False), so a real prior True from another row/upload
        # sharing this same building is never silently cleared by a run
        # that did no verification work at all.
        self.assertIsNone(result.geocode_unverified)

    def test_row_missing_lat_still_goes_through_the_normal_path(self):
        row = ListingRow(building="City Tower", provider="Breezblok", lat=None, lng=-0.1)

        with patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}) as mock_places:
            geocode.geocode_row(row)

        mock_places.assert_called_once()


class IsBareStreetReferenceTests(unittest.TestCase):
    """
    _is_bare_street_reference - the unit-level check behind Tier 2's own
    bare-street-name guard (see BareStreetReferenceSkipsTier2Tests below
    for the full geocode_row integration). Confirmed real case: a MetSpace
    email listing whose only location text was "Clerkenwell Road" - no
    house number, no building name at all.
    """

    def test_bare_street_name_is_a_bare_reference(self):
        self.assertTrue(geocode._is_bare_street_reference(ListingRow(building="Clerkenwell Road")))

    def test_abbreviated_street_suffix_is_also_a_bare_reference(self):
        self.assertTrue(geocode._is_bare_street_reference(ListingRow(building="Clerkenwell Rd")))

    def test_real_building_name_is_not_a_bare_reference(self):
        self.assertFalse(geocode._is_bare_street_reference(ListingRow(building="Kent House")))

    def test_building_name_ending_in_a_generic_word_is_not_a_bare_reference(self):
        # "Mill"/"Building"/etc. are not street-suffix words at all.
        self.assertFalse(geocode._is_bare_street_reference(ListingRow(building="Discovery Mill")))

    def test_building_name_ending_in_place_square_court_gardens_or_terrace_is_not_a_bare_reference(self):
        # Deliberately narrower than master_merge._STREET_SUFFIX_
        # EXPANSIONS' own full vocabulary (see _RELIABLE_STREET_SUFFIX_
        # WORDS' own docstring) - "Place"/"Square"/"Court"/"Gardens"/
        # "Terrace" are also common, genuine standalone UK BUILDING names
        # in their own right (confirmed real: this project's own "Nexus
        # Place" fixture, used throughout this test file as a real
        # building), unlike street/road/avenue/lane. Using the full
        # vocabulary here would have silently skipped Tier 2 geocoding
        # for a genuine building like this.
        for name in ("Nexus Place", "Fitzroy Square", "Clifford's Court", "Bedford Gardens", "Nash Terrace"):
            with self.subTest(name=name):
                self.assertFalse(geocode._is_bare_street_reference(ListingRow(building=name)))

    def test_numbered_street_address_is_not_a_bare_reference(self):
        # A leading house number ANYWHERE in building - via house_number.
        # leading_house_number, reused unmodified - excludes this
        # regardless of the last word.
        self.assertFalse(geocode._is_bare_street_reference(ListingRow(building="27-30 Lime Street")))

    def test_compound_name_and_numbered_address_is_not_a_bare_reference(self):
        # A real confirmed regression this specific check exists for: a
        # compound "Name, Address" building value (see split_compound_
        # building, tried FIRST by Tier 2's own address-only query) has
        # its own house number AFTER the separator, not at position 0 of
        # the raw string - "Bridge House, 22 Newman Street" has no digit
        # at the very start, but its own address part genuinely does.
        # Caught directly by this module's own pre-existing
        # CompoundBuildingGeocodingTests the moment this check was missing.
        row = ListingRow(building="Bridge House, 22 Newman Street")
        self.assertFalse(geocode._is_bare_street_reference(row))

    def test_compound_name_and_bare_street_with_no_number_is_still_a_bare_reference(self):
        # The mirror case: a compound value whose OWN address part also
        # has no house number at all - still genuinely nothing to
        # identify a specific building with, so this must still be
        # treated as a bare reference.
        row = ListingRow(building="Some Development, Newman Street")
        self.assertTrue(geocode._is_bare_street_reference(row))

    def test_development_name_present_excludes_even_a_genuine_bare_street_shape(self):
        # A stated development name is real extra identifying evidence
        # (see this module's own "Canal Building"/Regent's Wharf case) -
        # must never be blocked by this guard just because building alone
        # looks like a bare street.
        row = ListingRow(building="Clerkenwell Road", development_name="Some Development")
        self.assertFalse(geocode._is_bare_street_reference(row))

    def test_blank_building_is_not_a_bare_reference(self):
        self.assertFalse(geocode._is_bare_street_reference(ListingRow(building="")))


class BareStreetReferenceSkipsTier2Tests(unittest.TestCase):
    """
    geocode_row's own wiring of _is_bare_street_reference - skipped BEFORE
    Tier 2 ever queries Places at all (never merely flagged geocode_
    unverified afterward, which is too easy to miss/click through in
    review). Real motivating failure: Places never returns "no result"
    for a bare-street-name query; it hands back SOME plausible-looking
    address on that street instead (confirmed: "156 Clerkenwell Road" for
    a listing that was actually 67 Clerkenwell Road, a real but wrong
    building).
    """

    def setUp(self):
        geocode.FAILURES.clear()

    def test_bare_street_name_never_calls_places_and_leaves_the_row_untouched(self):
        row = ListingRow(building="Clerkenwell Road", provider="MetSpace")

        with patch("geocode.call_places_text_search") as mock_places:
            result = geocode.geocode_row(row)

        mock_places.assert_not_called()
        self.assertIsNone(result.lat)
        self.assertIsNone(result.lng)
        self.assertIsNone(result.address_1)
        self.assertIsNone(result.postcode)
        self.assertIsNone(result.geocode_unverified)

    def test_bare_street_name_is_logged_with_its_own_distinct_reason(self):
        row = ListingRow(building="Clerkenwell Road", provider="MetSpace")

        with patch("geocode.call_places_text_search"):
            geocode.geocode_row(row)

        self.assertEqual(len(geocode.FAILURES), 1)
        self.assertIn("bare street name", geocode.FAILURES[0]["reason"])
        self.assertIn("no house number", geocode.FAILURES[0]["reason"])

    def test_real_building_name_still_queries_places_normally(self):
        row = ListingRow(building="Kent House", provider="UNION")

        with patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}) as mock_places:
            geocode.geocode_row(row)

        mock_places.assert_called()

    def test_street_suffix_building_with_development_name_still_queries_places(self):
        row = ListingRow(building="Clerkenwell Road", provider="MetSpace", development_name="Some Development")

        with patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}) as mock_places:
            geocode.geocode_row(row)

        mock_places.assert_called()

    def test_numbered_street_address_as_building_still_queries_places_normally(self):
        row = ListingRow(building="27-30 Lime Street", provider="UNION")

        with patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}) as mock_places:
            geocode.geocode_row(row)

        mock_places.assert_called()


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


class IsBarePostcodeDistrictTests(unittest.TestCase):
    def test_bare_district_is_recognized(self):
        for value in ("SE1", "EC2", "W1S", "ec1v", " NW3 "):
            self.assertTrue(geocode._is_bare_postcode_district(value), value)

    def test_real_place_names_are_not_postcode_districts(self):
        for value in ("Shoreditch", "Clerkenwell", "City", "Mayfair", "London Bridge", "Borough", "City Fringe"):
            self.assertFalse(geocode._is_bare_postcode_district(value), value)

    def test_full_postcode_is_not_a_bare_district(self):
        self.assertFalse(geocode._is_bare_postcode_district("SE1 8QH"))

    def test_blank_is_not_a_bare_district(self):
        self.assertFalse(geocode._is_bare_postcode_district(""))
        self.assertFalse(geocode._is_bare_postcode_district(None))


class PostcodeDistrictSubmarketFallbackTests(unittest.TestCase):
    """
    The real confirmed report: a provider's own "SE1" section heading
    (grouping several buildings under one postcode-district label) gets
    faithfully extracted as submarket="SE1" - a real, non-blank value, but
    not a useful named locality the way "Shoreditch"/"Mayfair" already
    are. Only a value shaped exactly like a bare postcode district (see
    IsBarePostcodeDistrictTests) is ever reconsidered - a genuine named
    submarket is never touched (see test_good_named_submarket_survives_
    unchanged), and only sufficiently specific, confident evidence (a
    source-text locality hint, or Google's own sublocality/neighborhood
    reverse-geocode component) is ever used to replace it - never a
    guessed or broad value (see the "do not guess" tests below).
    """

    def setUp(self):
        geocode.FAILURES.clear()

    def test_good_named_submarket_survives_unchanged(self):
        for value in ("Shoreditch", "Clerkenwell", "City", "Mayfair", "London Bridge", "Borough", "City Fringe"):
            row = ListingRow(building="Some Building", lat=51.5, lng=-0.1, submarket=value)
            with patch("geocode.call_reverse_geocoding_api") as mock_reverse:
                geocode.geocode_row(row)
            mock_reverse.assert_not_called()
            self.assertEqual(row.submarket, value)

    def test_postcode_like_submarket_is_replaced_by_a_confident_google_sublocality(self):
        # The real "1 Valentine Place" shape: Google's reverse-geocode of
        # the ALREADY-resolved coordinates genuinely has a sublocality
        # component (confirmed directly against the real address).
        row = ListingRow(
            building="1 Valentine Place", address_1="1 Valentine Place, London", postcode="SE1 8QH",
            lat=51.5016158, lng=-0.1049473, submarket="SE1",
        )
        components = [
            {"long_name": "South Bank", "types": ["political", "sublocality", "sublocality_level_1"]},
            {"long_name": "London", "types": ["locality", "political"]},
        ]

        with patch(
            "geocode.call_reverse_geocoding_api", return_value={"status": "OK", "address_components": components},
        ) as mock_reverse:
            geocode.geocode_row(row)

        mock_reverse.assert_called_once_with(51.5016158, -0.1049473)
        self.assertEqual(row.submarket, "South Bank")

    def test_postcode_like_submarket_is_replaced_by_a_confident_source_text_hint_with_no_api_call(self):
        # A source-text locality hint (see extract_area_hint) is preferred
        # over a Google API call, same priority as the existing blank-
        # submarket case - never a provider/postcode-specific rule, the
        # exact same generic newline/comma-adjacent-to-postcode parsing
        # already used elsewhere in this module.
        row = ListingRow(
            building="Nutmeg House \nLondon Bridge SE1", provider="beem", lat=51.505, lng=-0.087, submarket="SE1",
        )

        with patch("geocode.call_reverse_geocoding_api") as mock_reverse:
            geocode.geocode_row(row)

        mock_reverse.assert_not_called()
        self.assertEqual(row.submarket, "London Bridge")

    def test_blank_submarket_with_a_confident_locality_is_filled(self):
        row = ListingRow(building="Some Building", lat=51.5016158, lng=-0.1049473)
        components = [{"long_name": "South Bank", "types": ["political", "sublocality", "sublocality_level_1"]}]

        with patch(
            "geocode.call_reverse_geocoding_api", return_value={"status": "OK", "address_components": components},
        ):
            geocode.geocode_row(row)

        self.assertEqual(row.submarket, "South Bank")

    def test_only_broad_locality_never_replaces_the_postcode_like_value(self):
        # The real "44 Copperfield Street" shape: Google's reverse-geocode
        # has NO sublocality/neighborhood component at all - only the
        # broad locality="London"/administrative_area="Greater London"/
        # "England" and borough-level "London Borough of Southwark" - none
        # of which _submarket_from_components ever treats as a useful
        # submarket. "SE1" must survive unchanged rather than being
        # replaced with something no more useful than what it already had.
        row = ListingRow(
            building="44 Copperfield Street", address_1="44 Copperfield Street, London", postcode="SE1 0DY",
            lat=51.5031582, lng=-0.1001583, submarket="SE1",
        )
        components = [
            {"long_name": "London", "types": ["postal_town"]},
            {"long_name": "Greater London", "types": ["administrative_area_level_2", "political"]},
            {"long_name": "England", "types": ["administrative_area_level_1", "political"]},
            {"long_name": "London Borough of Southwark", "types": ["administrative_area_level_3", "political"]},
            {"long_name": "London", "types": ["locality", "political"]},
        ]

        with patch(
            "geocode.call_reverse_geocoding_api", return_value={"status": "OK", "address_components": components},
        ) as mock_reverse:
            geocode.geocode_row(row)

        mock_reverse.assert_called_once()
        self.assertEqual(row.submarket, "SE1")

    def test_ambiguous_locality_does_not_guess(self):
        # No sublocality/neighborhood AND no source-text hint at all -
        # nothing confident to replace "SE1" with, so it stays exactly as
        # extracted rather than being guessed at.
        row = ListingRow(building="Some Building", lat=51.5, lng=-0.1, submarket="EC2")

        with patch(
            "geocode.call_reverse_geocoding_api",
            return_value={"status": "OK", "address_components": [{"long_name": "London", "types": ["locality"]}]},
        ):
            geocode.geocode_row(row)

        self.assertEqual(row.submarket, "EC2")

    def test_reverse_geocode_failure_leaves_postcode_like_value_unchanged(self):
        row = ListingRow(building="Some Building", lat=51.5, lng=-0.1, submarket="SE1")

        with patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        self.assertEqual(row.submarket, "SE1")

    def test_postcode_conflict_protection_still_applies_with_a_postcode_like_submarket(self):
        # This feature only ever runs AFTER a location has already been
        # safely resolved - it must never weaken the existing postcode-
        # conflict rejection for an UNRESOLVED building-only row. The
        # confirmed real "New Derwent House WC1" failure (Places matches
        # a different, wrong postcode area) must still be rejected exactly
        # as before, regardless of what row.submarket happens to be.
        row = ListingRow(building="New Derwent House WC1", submarket="SE1")

        with patch(
            "geocode.call_places_text_search",
            return_value={
                "status": "OK", "lat": 51.5118097, "lng": -0.1414146,
                "address_components": [{"longText": "W1S 2ER", "types": ["postal_code"]}],
            },
        ):
            geocode.geocode_row(row)

        self.assertIsNone(row.lat)
        self.assertIsNone(row.lng)
        self.assertEqual(row.submarket, "SE1")  # left exactly as it was - never guessed from a rejected candidate

    def test_existing_lat_lng_address_postcode_are_never_altered_by_this_feature(self):
        row = ListingRow(
            building="1 Valentine Place", address_1="1 Valentine Place, London", postcode="SE1 8QH",
            lat=51.5016158, lng=-0.1049473, submarket="SE1",
        )
        components = [{"long_name": "South Bank", "types": ["political", "sublocality", "sublocality_level_1"]}]

        with patch(
            "geocode.call_reverse_geocoding_api", return_value={"status": "OK", "address_components": components},
        ):
            geocode.geocode_row(row)

        self.assertEqual(row.lat, 51.5016158)
        self.assertEqual(row.lng, -0.1049473)
        self.assertEqual(row.address_1, "1 Valentine Place, London")
        self.assertEqual(row.postcode, "SE1 8QH")
        self.assertEqual(row.submarket, "South Bank")  # only submarket itself changes

    def test_behavior_is_generic_across_different_postcode_districts_not_just_se1(self):
        # No provider/property/postcode-specific rule - "EC2" (a totally
        # different real district) gets the exact same treatment as "SE1".
        row = ListingRow(building="Some City Building", lat=51.5155, lng=-0.0922, submarket="EC2")
        components = [{"long_name": "Broadgate", "types": ["political", "sublocality", "sublocality_level_1"]}]

        with patch(
            "geocode.call_reverse_geocoding_api", return_value={"status": "OK", "address_components": components},
        ):
            geocode.geocode_row(row)

        self.assertEqual(row.submarket, "Broadgate")


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


class DevelopmentNameDisambiguationTests(unittest.TestCase):
    """
    Regression coverage for row.development_name (see schema.ListingRow's
    own docstring) - the real confirmed case: "Canal Building" (Colliers),
    part of the real "Regent's Wharf" campus on All Saints Street, King's
    Cross (alongside "Thorley Works", "The Mill", "The Packing House"),
    states no street number anywhere in its own brochure. A plain "Canal
    Building, King's Cross, London, UK" query (building name + coarse
    submarket alone) is not unique enough - it can land on a genuinely
    different "Canal Reach" street also in King's Cross. development_name
    ("Regent's Wharf") is a more specific disambiguator, tried BEFORE the
    submarket-based variants (see geocode_row's own Tier 2 block).
    """

    def setUp(self):
        geocode.FAILURES.clear()

    def test_development_name_variant_is_tried_before_the_submarket_variant(self):
        row = ListingRow(
            building="Canal Building", provider="Colliers", submarket="King's Cross",
            development_name="Regent's Wharf",
        )

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.5363, "lng": -0.1201, "address_components": []},
        ) as mock_places, patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        mock_places.assert_called_once_with("Canal Building, Regent's Wharf, London, UK")

    def test_development_name_disambiguates_a_real_same_area_collision(self):
        # The plain submarket-only query would resolve to the WRONG real
        # place (a decoy "Canal Reach" street, also in King's Cross, also
        # comfortably in-bbox) - the development_name variant, tried
        # first, must resolve to the actual Regent's Wharf building
        # instead, without ever falling through to the decoy.
        row = ListingRow(
            building="Canal Building", provider="Colliers", submarket="King's Cross",
            development_name="Regent's Wharf",
        )

        def fake_places(query):
            if query == "Canal Building, Regent's Wharf, London, UK":
                return {"status": "OK", "lat": 51.5363, "lng": -0.1201, "address_components": []}
            if query == "Canal Building, King's Cross, London, UK":
                # The real, wrong "Canal Reach" collision this feature exists for.
                return {"status": "OK", "lat": 51.5395, "lng": -0.1180, "address_components": []}
            raise AssertionError(f"unexpected query: {query!r}")

        with patch("geocode.call_places_text_search", side_effect=fake_places), \
             patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        self.assertEqual(row.lat, 51.5363)
        self.assertEqual(row.lng, -0.1201)

    def test_development_name_query_failing_falls_back_to_the_submarket_variant(self):
        row = ListingRow(
            building="Canal Building", provider="Colliers", submarket="King's Cross",
            development_name="Regent's Wharf",
        )
        queried = []

        def fake_places(query):
            queried.append(query)
            if query == "Canal Building, Regent's Wharf, London, UK":
                return {"status": "ZERO_RESULTS"}
            return {"status": "OK", "lat": 51.5363, "lng": -0.1201, "address_components": []}

        with patch("geocode.call_places_text_search", side_effect=fake_places), \
             patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        # The submarket fallback itself still tries its own no-space
        # variant first (see _submarket_query_variants' own docstring) -
        # unchanged by this feature, just tried AFTER development_name.
        self.assertEqual(
            queried,
            ["Canal Building, Regent's Wharf, London, UK", "Canal Building, King'sCross, London, UK"],
        )
        self.assertEqual(row.lat, 51.5363)

    def test_no_development_name_behaves_exactly_as_before(self):
        # Regression: a row with no development_name at all must query
        # exactly the same submarket-based text as before this feature -
        # never a "None" artifact from the new location_texts list.
        row = ListingRow(building="Canal Building", provider="Colliers", submarket="King's Cross")

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.5363, "lng": -0.1201, "address_components": []},
        ) as mock_places, patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        # The no-space submarket variant is tried first (see
        # _submarket_query_variants' own docstring) - unchanged by this
        # feature for a row with no development_name at all.
        mock_places.assert_called_once_with("Canal Building, King'sCross, London, UK")

    def test_development_name_never_marks_the_result_as_unverified(self):
        # A development-name-assisted match is a better GUESS, not
        # independent evidence - it must be flagged for manual
        # confirmation exactly like any other zero-hint Tier 2 result,
        # never treated as verified just because it used development_name.
        row = ListingRow(
            building="Canal Building", provider="Colliers", development_name="Regent's Wharf",
        )

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.5363, "lng": -0.1201, "address_components": []},
        ), patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        self.assertTrue(row.geocode_unverified)


class GeocodeUnverifiedFlagTests(unittest.TestCase):
    """
    Regression coverage for row.geocode_unverified (see schema.ListingRow's
    own docstring) - confirmed real cases: "Henly House" (not indexed under
    that exact spelling by Places) and "Ivybridge House" (Places resolves it
    to a stale/mislabeled POI), neither fixable by better query phrasing or
    a name-similarity check, since the returned candidate itself is wrong.
    What geocode_row CAN do is stop treating a zero-hint Tier 2 acceptance
    with the same confidence as any other result - see geocode_row's own
    Tier 2 block.
    """

    def setUp(self):
        geocode.FAILURES.clear()

    def test_henly_house_zero_hint_tier2_acceptance_is_flagged_unverified(self):
        # No address_1/postcode, and "Henly House" has no trailing postcode-
        # district token either - _source_location_hint has nothing at all
        # to offer, so this is exactly the zero-hint Tier 2 shape.
        row = ListingRow(building="Henly House", provider="MetSpace")

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.52, "lng": -0.09, "address_components": []},
        ), patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        self.assertTrue(row.geocode_unverified)

    def test_ivybridge_house_zero_hint_tier2_acceptance_is_flagged_unverified(self):
        row = ListingRow(building="Ivybridge House", provider="Workplace Plus")

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.5, "lng": -0.12, "address_components": []},
        ), patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        self.assertTrue(row.geocode_unverified)

    def test_tier_1_geocoding_api_success_is_never_flagged_unverified(self):
        row = ListingRow(building="16 Mortimer Street", address_1="16 Mortimer Street", postcode="W1T 3JL")

        with patch("geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.5188, "lng": -0.1381}), \
             patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        # Explicit False, not merely "not True" - a Tier 1 success is real
        # evidence, so it must positively clear a stale True a prior
        # upload's own zero-hint fallback may have left on this row (see
        # schema.ListingRow.geocode_unverified's own docstring).
        self.assertIs(row.geocode_unverified, False)

    def test_tier_2_with_a_source_postcode_hint_is_never_flagged_unverified(self):
        # A trailing postcode-district token on `building` IS a source hint
        # (see _source_location_hint) - the Places candidate here agrees
        # with it (both district ("WC", "1")), so this is corroborated, not
        # a zero-hint acceptance, even though it went through Tier 2.
        row = ListingRow(building="New Derwent House WC1", provider="beem")
        components = [
            {"longText": "1", "types": ["street_number"]},
            {"longText": "New Derwent House", "types": ["route"]},
            {"longText": "WC1B 3ES", "types": ["postal_code"]},
        ]

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.52, "lng": -0.12, "address_components": components},
        ), patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        # Same explicit-False reasoning as Tier 1 above - a hint-
        # corroborated Tier 2 match is real evidence too.
        self.assertIs(row.geocode_unverified, False)


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

    def test_geocode_unverified_flag_is_copied_to_every_group_member(self):
        # Confirmed real-world symptom this regression-tests: Hatchers Yard
        # (3 rows) and Ivybridge House (6 rows) each only flagged the ONE
        # representative row actually sent through geocode_row, leaving
        # every other member silently sharing the identical unverified
        # address with no flag/caution shown at all - because the group
        # copy-down loop copied lat/lng/address_1/postcode but not
        # geocode_unverified alongside them.
        hatchers_yard = [
            ListingRow(building="Hatchers Yard", provider="Beam", floor_unit="1st Floor"),
            ListingRow(building="Hatchers Yard", provider="Beam", floor_unit="2nd Floor"),
            ListingRow(building="Hatchers Yard", provider="Beam", floor_unit="3rd Floor"),
        ]
        ivybridge_house = [
            ListingRow(building="Ivybridge House", provider="Workplace Plus", floor_unit=f"Suite {i}")
            for i in range(1, 7)
        ]

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.5, "lng": -0.12, "address_components": []},
        ), patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_rows(hatchers_yard + ivybridge_house)

        for row in hatchers_yard + ivybridge_house:
            self.assertTrue(row.geocode_unverified, f"{row.building} {row.floor_unit} was not flagged unverified")

    def test_explicit_false_geocode_unverified_is_also_copied_to_every_group_member(self):
        # A different, later-introduced regression than the one above:
        # once Tier 1/a hint-corroborated Tier 2 match started writing an
        # explicit False (see schema.ListingRow.geocode_unverified's own
        # docstring), the group copy-down's own truthiness check (`if not
        # row.geocode_unverified and representative.geocode_unverified`)
        # treated that False as "nothing to propagate" (False is falsy
        # too) and silently left every OTHER member's own value at None -
        # confirmed directly with a plain Tier 1 success shared by a
        # 2-row group.
        row_a = ListingRow(building="Test Building", provider="UNION", address_1="1 Test St", postcode="EC1A 1AA")
        row_b = ListingRow(building="Test Building", provider="UNION", address_1="1 Test St", postcode="EC1A 1AA")

        with patch("geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.5, "lng": -0.1}):
            geocode.geocode_rows([row_a, row_b])

        self.assertIs(row_a.geocode_unverified, False)
        self.assertIs(row_b.geocode_unverified, False)

    def test_blank_building_rows_are_never_grouped_with_anything(self):
        row_a = ListingRow(building="", provider="UNION")
        row_b = ListingRow(building="", provider="UNION")

        with patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}) as mock_places:
            geocode.geocode_rows([row_a, row_b])

        # Blank building means geocode_row's own Tier 2 branch never even
        # runs (see geocode_row's own "if row.building:" guard) - confirms
        # this grouping wrapper doesn't change that for either row.
        mock_places.assert_not_called()


class StreetNameConflictTests(unittest.TestCase):
    """
    Regression coverage for _best_places_result's own STREET_CONFLICT check
    (see geocode.py's own module docstring, "A row whose OWN building text
    states a genuine street address...") - confirmed real failure: a real
    Kitt's "44 Paul Street" (Shoreditch, EC2A), with no address_1/postcode
    of its own (so nothing for the existing postcode-district check to
    compare against - see _postcode_hint_conflicts), resolved via Places
    Text Search to a genuinely different, unrelated street (Little
    Britain, EC1A) - accepted with only a geocode_unverified flag, since
    that was the only existing safeguard for a zero-hint Tier 2 row.
    """

    def setUp(self):
        geocode.FAILURES.clear()

    def test_paul_street_resolving_to_an_unrelated_street_is_rejected(self):
        row = ListingRow(building="44 Paul Street", provider="Kitt's")
        components = [
            {"longText": "1", "types": ["street_number"]},
            {"longText": "Little Britain", "types": ["route"]},
            {"longText": "EC1A 7BU", "types": ["postal_code"]},
        ]

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.5177, "lng": -0.0977, "address_components": components},
        ):
            geocode.geocode_row(row)

        self.assertIsNone(row.lat)
        self.assertIsNone(row.lng)
        self.assertEqual(len(geocode.FAILURES), 1)
        self.assertIn("shares no words", geocode.FAILURES[0]["reason"])

    def test_paul_street_resolving_to_its_own_real_street_is_accepted(self):
        # Same shape, but the candidate's own route genuinely IS Paul
        # Street (formatting/casing tolerated via normalize_key) - must
        # still resolve normally, still flagged unverified (no OTHER
        # corroboration exists), same as before this check existed.
        row = ListingRow(building="44 Paul Street", provider="Kitt's")
        components = [
            {"longText": "44", "types": ["street_number"]},
            {"longText": "Paul Street", "types": ["route"]},
            {"longText": "EC2A 4LB", "types": ["postal_code"]},
        ]

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.5262, "lng": -0.0873, "address_components": components},
        ), patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        self.assertEqual(row.lat, 51.5262)
        self.assertEqual(row.lng, -0.0873)
        self.assertTrue(row.geocode_unverified)

    def test_bare_building_name_candidate_is_never_street_checked(self):
        # "Kent House" has no leading house number of its own - not
        # address-shaped, so the street check must never even apply,
        # regardless of what street the candidate itself resolves to
        # (Kent House's own real record famously has no street_number/
        # route at all - see this module's own docstring).
        row = ListingRow(building="Kent House", provider="MetSpace")
        components = [{"longText": "SE1 1AA", "types": ["postal_code"]}]

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.5, "lng": -0.1, "address_components": components},
        ), patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        self.assertEqual(row.lat, 51.5)
        self.assertEqual(row.lng, -0.1)

    def test_candidate_with_no_route_component_is_never_street_checked(self):
        # An address-shaped source (a real house number), but the
        # returned candidate has no route component of its own to compare
        # against at all - nothing real to compare, so this is accepted
        # exactly like before this check existed (same "nothing to
        # compare" conservatism as brochure_enrichment._address_conflict_
        # note).
        row = ListingRow(building="44 Paul Street", provider="Kitt's")
        components = [{"longText": "EC2A 4LB", "types": ["postal_code"]}]

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.5262, "lng": -0.0873, "address_components": components},
        ), patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        self.assertEqual(row.lat, 51.5262)
        self.assertEqual(row.lng, -0.0873)

    def test_falls_through_to_the_next_candidate_when_the_first_conflicts(self):
        # Same "keep trying safer variants before giving up" principle
        # _best_places_result's own docstring already promises for bbox/
        # postcode conflicts - a street conflict on the FIRST candidate a
        # query returns must not give up the whole query; the next
        # candidate in the SAME response (see call_places_text_search's
        # own "candidates" list) is tried before moving on.
        row = ListingRow(building="44 Paul Street", provider="Kitt's")
        wrong = {
            "lat": 51.5177, "lng": -0.0977, "formatted_address": "1 Little Britain, London EC1A 7BU, UK",
            "address_components": [
                {"longText": "1", "types": ["street_number"]},
                {"longText": "Little Britain", "types": ["route"]},
                {"longText": "EC1A 7BU", "types": ["postal_code"]},
            ],
        }
        right = {
            "lat": 51.5262, "lng": -0.0873, "formatted_address": "44 Paul Street, London EC2A 4LB, UK",
            "address_components": [
                {"longText": "44", "types": ["street_number"]},
                {"longText": "Paul Street", "types": ["route"]},
                {"longText": "EC2A 4LB", "types": ["postal_code"]},
            ],
        }

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "candidates": [wrong, right], **wrong},
        ), patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        self.assertEqual(row.lat, 51.5262)
        self.assertEqual(row.lng, -0.0873)


class Tier1AddressWithoutPostcodeTests(unittest.TestCase):
    """
    Regression coverage for geocode_row's own relaxed Tier 1 branch (see
    the module docstring's own "Tier 1 also runs for a row with a
    genuinely numbered address_1... but no postcode yet" paragraph) -
    confirmed real gap: once _building_identity_matches' own bare-street-
    reference tier (brochure_enrichment.py) backfills a row's address_1
    from a bare street building name ("Clerkenwell Road") to a real
    numbered address ("67 Clerkenwell Rd") via its own linked brochure,
    the row could still never get real coordinates at all - the original
    Tier 1 refused to even attempt a lookup without a postcode already on
    file, and Tier 2 only ever reads row.building (still the bare street
    name), never address_1. Verified directly against the real Google
    Geocoding API with this exact real address: resolves to
    lat=51.5219197, lng=-0.1077003, postcode="EC1R 5BL".
    """

    def setUp(self):
        geocode.FAILURES.clear()

    def test_numbered_address_1_with_no_postcode_resolves_and_backfills_postcode(self):
        # submarket is set so _backfill_submarket_from_coords (called
        # right after, unrelated to this test's own point) doesn't ALSO
        # make its own independent reverse-geocode call for its own
        # purposes (see that function's own docstring) - keeps the
        # reverse-geocode assertion below isolated to this fix's own
        # postcode-backfill call specifically.
        row = ListingRow(
            building="Clerkenwell Road", address_1="67 Clerkenwell Rd", provider="MetSpace",
            submarket="Clerkenwell",
        )
        components = [
            {"long_name": "67", "types": ["street_number"]},
            {"long_name": "Clerkenwell Road", "types": ["route"]},
            {"long_name": "EC1R 5BL", "types": ["postal_code"]},
        ]

        with patch(
            "geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.5219197, "lng": -0.1077003},
        ) as mock_geocoding, patch(
            "geocode.call_reverse_geocoding_api", return_value={"status": "OK", "address_components": components},
        ) as mock_reverse:
            geocode.geocode_row(row)

        mock_geocoding.assert_called_once_with("67 Clerkenwell Rd, London, UK")
        mock_reverse.assert_called_once_with(51.5219197, -0.1077003)
        self.assertEqual(row.lat, 51.5219197)
        self.assertEqual(row.lng, -0.1077003)
        self.assertEqual(row.postcode, "EC1R 5BL")
        # Real, corroborated evidence on a specific numbered address - same
        # confidence level the "postcode already on file" branch already
        # gets, not a weaker zero-hint guess.
        self.assertIs(row.geocode_unverified, False)

    def test_reverse_geocode_failure_still_leaves_the_coordinate_and_just_skips_postcode(self):
        row = ListingRow(building="Clerkenwell Road", address_1="67 Clerkenwell Rd")

        with patch(
            "geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.52, "lng": -0.1},
        ), patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        self.assertEqual(row.lat, 51.52)
        self.assertEqual(row.lng, -0.1)
        self.assertIsNone(row.postcode)
        self.assertIs(row.geocode_unverified, False)

    def test_reverse_geocoded_postcode_contradicting_source_evidence_is_left_blank(self):
        # Same _postcode_hint_conflicts validation Tier 2's own success
        # path already relies on - a reverse-geocoded postcode that
        # disagrees with a trailing postcode-district token already on
        # row.building must never be written over that real evidence.
        row = ListingRow(building="67 Clerkenwell Rd EC1V", address_1="67 Clerkenwell Rd")
        components = [{"long_name": "W1S 2ER", "types": ["postal_code"]}]

        with patch(
            "geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.52, "lng": -0.1},
        ), patch("geocode.call_reverse_geocoding_api", return_value={"status": "OK", "address_components": components}):
            geocode.geocode_row(row)

        self.assertEqual(row.lat, 51.52)
        self.assertIsNone(row.postcode)
        self.assertEqual(len(geocode.FAILURES), 1)
        self.assertIn("contradicts source location", geocode.FAILURES[0]["reason"])

    def test_bare_address_1_with_no_house_number_never_triggers_the_relaxed_path(self):
        # address_1 with no house number of its own has no more
        # identifying power than the bare-street BUILDING case Tier 2
        # already exists to handle cautiously - must fall through to that
        # existing path unchanged, never attempt Tier 1 at all.
        row = ListingRow(building="Clerkenwell Road", address_1="Clerkenwell Road")

        with patch("geocode.call_geocoding_api") as mock_geocoding, \
             patch("geocode.call_places_text_search") as mock_places:
            geocode.geocode_row(row)

        mock_geocoding.assert_not_called()
        # Falls all the way through to _is_bare_street_reference's own
        # skip - row.building is still a bare street reference too, so
        # Tier 2 never queries Places either.
        mock_places.assert_not_called()
        self.assertIsNone(row.lat)
        self.assertIsNone(row.postcode)

    def test_address_1_and_postcode_both_already_present_is_completely_unchanged(self):
        # The ORIGINAL Tier 1 branch - both fields already present - must
        # be untouched by this change: same query shape, same single
        # call_geocoding_api call, no reverse-geocode attempted for
        # address/postcode (nothing missing to backfill there). submarket
        # is set so _backfill_submarket_from_coords doesn't make its own
        # independent reverse-geocode call for its own, unrelated purpose
        # (see that function's own docstring) - keeps this assertion
        # isolated to whether THIS fix changed the original branch.
        row = ListingRow(
            building="Clerkenwell Road", address_1="67 Clerkenwell Rd", postcode="EC1R 5BL",
            submarket="Clerkenwell",
        )

        with patch(
            "geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.5219197, "lng": -0.1077003},
        ) as mock_geocoding, patch("geocode.call_reverse_geocoding_api") as mock_reverse:
            geocode.geocode_row(row)

        mock_geocoding.assert_called_once_with("67 Clerkenwell Rd, EC1R 5BL, UK")
        mock_reverse.assert_not_called()
        self.assertEqual(row.lat, 51.5219197)
        self.assertEqual(row.postcode, "EC1R 5BL")
        self.assertIs(row.geocode_unverified, False)

    def test_geocoding_api_failure_on_the_relaxed_path_falls_through_to_tier_2(self):
        # Same "fall through to Places if Geocoding fails" principle the
        # original branch already documents - a genuinely numbered
        # address_1 whose Tier 1 attempt fails must still get a Tier 2
        # attempt, exactly like the original branch already gets. Uses a
        # building WITHOUT a trailing street-suffix word so it isn't
        # caught by _is_bare_street_reference's own skip, confirming Tier
        # 2 genuinely runs (not just "correctly skipped again").
        row = ListingRow(building="Kent House", address_1="67 Clerkenwell Rd")

        with patch("geocode.call_geocoding_api", return_value={"status": "ZERO_RESULTS"}), \
             patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}) as mock_places:
            geocode.geocode_row(row)

        mock_places.assert_called()


class GeocodeAddressLookupTests(unittest.TestCase):
    """
    geocode_address_lookup - the reviewer-facing lookup pages/2_Review_
    and_Master.py's own missing-location UI calls (see that module's own
    _render_missing_location_lookup). httpx.get itself is never mocked
    here - geocode.call_geocoding_api is the one real network boundary,
    same convention every other test in this file already uses.
    """

    def _components(self, number="67", route="Clerkenwell Road", postcode="EC1R 5BL"):
        return [
            {"long_name": number, "types": ["street_number"]},
            {"long_name": route, "types": ["route"]},
            {"long_name": postcode, "types": ["postal_code"]},
        ]

    def test_successful_lookup_returns_a_clean_address_postcode_and_coordinates(self):
        with patch(
            "geocode.call_geocoding_api",
            return_value={
                "status": "OK", "lat": 51.5219197, "lng": -0.1077003,
                "address_components": self._components(),
            },
        ) as mock_geocoding, patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            result = geocode.geocode_address_lookup("67 Clerkenwell Rd")

        mock_geocoding.assert_called_once_with("67 Clerkenwell Rd, UK")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["address_1"], "67 Clerkenwell Road")
        self.assertEqual(result["postcode"], "EC1R 5BL")
        self.assertEqual(result["lat"], 51.5219197)
        self.assertEqual(result["lng"], -0.1077003)

    def test_postcode_argument_is_folded_into_the_query_when_given(self):
        with patch(
            "geocode.call_geocoding_api",
            return_value={"status": "OK", "lat": 51.5, "lng": -0.1, "address_components": self._components()},
        ) as mock_geocoding:
            geocode.geocode_address_lookup("67 Clerkenwell Rd", "EC1R 5BL")

        mock_geocoding.assert_called_once_with("67 Clerkenwell Rd, EC1R 5BL, UK")

    def test_submarket_is_resolved_via_the_existing_backfill_machinery(self):
        with patch(
            "geocode.call_geocoding_api",
            return_value={"status": "OK", "lat": 51.5, "lng": -0.1, "address_components": self._components()},
        ), patch(
            "geocode.call_reverse_geocoding_api",
            return_value={
                "status": "OK",
                "address_components": [{"long_name": "Farringdon", "types": ["sublocality"]}],
            },
        ):
            result = geocode.geocode_address_lookup("67 Clerkenwell Rd")

        self.assertEqual(result["submarket"], "Farringdon")

    def test_zero_results_is_passed_straight_through(self):
        with patch("geocode.call_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            result = geocode.geocode_address_lookup("complete nonsense")

        self.assertEqual(result, {"status": "ZERO_RESULTS"})

    def test_a_broad_fallback_result_with_no_address_components_still_returns_ok_with_blank_fields(self):
        # Real confirmed Google Geocoding API behavior: a nonsense query
        # can still come back "status": "OK" with a broad country-level
        # location and no address_components at all - this function
        # itself doesn't decide "no match" (that's the UI's own job, see
        # pages/2_Review_and_Master.py) - it just reports address_1/
        # postcode as genuinely blank, never fabricated.
        with patch(
            "geocode.call_geocoding_api",
            return_value={"status": "OK", "lat": 55.378051, "lng": -3.435973, "address_components": []},
        ), patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            result = geocode.geocode_address_lookup("zzzqqxxyy nonsense")

        self.assertEqual(result["status"], "OK")
        self.assertIsNone(result["address_1"])
        self.assertIsNone(result["postcode"])


class CallGeocodingApiAddressComponentsTests(unittest.TestCase):
    """call_geocoding_api's own address_components addition - purely
    additive, see that function's own docstring."""

    def test_status_lat_lng_are_unaffected(self):
        response = MagicMock()
        response.json.return_value = {
            "status": "OK",
            "results": [{"geometry": {"location": {"lat": 51.5, "lng": -0.1}}, "address_components": []}],
        }
        with patch("geocode.httpx.get", return_value=response):
            result = geocode.call_geocoding_api("67 Clerkenwell Rd, UK")

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["lat"], 51.5)
        self.assertEqual(result["lng"], -0.1)

    def test_address_components_are_now_included(self):
        components = [{"long_name": "Clerkenwell Road", "types": ["route"]}]
        response = MagicMock()
        response.json.return_value = {
            "status": "OK",
            "results": [{"geometry": {"location": {"lat": 51.5, "lng": -0.1}}, "address_components": components}],
        }
        with patch("geocode.httpx.get", return_value=response):
            result = geocode.call_geocoding_api("67 Clerkenwell Rd, UK")

        self.assertEqual(result["address_components"], components)

    def test_zero_results_is_unaffected(self):
        response = MagicMock()
        response.json.return_value = {"status": "ZERO_RESULTS", "results": []}
        with patch("geocode.httpx.get", return_value=response):
            result = geocode.call_geocoding_api("nonsense")

        self.assertEqual(result, {"status": "ZERO_RESULTS"})


if __name__ == "__main__":
    unittest.main()
