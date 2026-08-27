"""
Regression tests for geocode.py's postcode-district validation - added after
a real, reproduced failure against the actual beem Live Flex Availability.xlsx
file: the source's own building text ("New Derwent House WC1") states a
postcode-district hint, but geocode_row's Tier 2 (Places Text Search
fallback) only ever checked within_london_bbox - a coarse rectangle covering
all of Greater London - before accepting whatever candidate Places returned.
The real API returned a same/similarly-named place at "25 Savile Row, London
W1S 2ER" (area W1, nowhere near the source's own WC1), comfortably inside the
bbox, and it was accepted with no way to catch the contradiction.

extract_postcode_hint/_source_location_hint/_postcode_hint_conflicts parse
this generically via the UK postcode's own outward/inward code grammar
(area letters + district digits + optional subdivision letter) - never a
hardcoded list of specific areas/places/providers - so the same check
protects any future file naming any other UK postcode district.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_geocode_location_validation -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema import ListingRow

import geocode


class ExtractPostcodeHintTests(unittest.TestCase):
    """Pure parsing - no API calls involved."""

    def test_trailing_district_token_on_a_building_name(self):
        self.assertEqual(
            geocode.extract_postcode_hint("New Derwent House WC1"),
            {"full": None, "district": ("WC", "1")},
        )

    def test_trailing_district_token_after_a_newline(self):
        # The real Beem convention - a literal newline between the building
        # name/area text and the trailing postcode district, never stripped
        # anywhere upstream (see storage.file_store.dataframe_to_listing_rows).
        self.assertEqual(
            geocode.extract_postcode_hint("Clove \nLondon Bridge SE1"),
            {"full": None, "district": ("SE", "1")},
        )

    def test_full_postcode_is_parsed_as_both_full_and_district(self):
        self.assertEqual(
            geocode.extract_postcode_hint("22 Newman Street W1T 4PX"),
            {"full": "W1T 4PX", "district": ("W", "1")},
        )

    def test_a_plain_building_name_with_nothing_appended_has_no_hint(self):
        self.assertIsNone(geocode.extract_postcode_hint("Kent House"))

    def test_a_coincidental_non_postcode_trailing_word_has_no_hint(self):
        self.assertIsNone(geocode.extract_postcode_hint("Bridge House, 22 Newman Street"))

    def test_lowercase_district_is_still_recognized(self):
        self.assertEqual(
            geocode.extract_postcode_hint("New Derwent House wc1"),
            {"full": None, "district": ("WC", "1")},
        )

    def test_blank_text_has_no_hint(self):
        self.assertIsNone(geocode.extract_postcode_hint(""))
        self.assertIsNone(geocode.extract_postcode_hint(None))


class ExtractAreaHintTests(unittest.TestCase):
    """extract_area_hint - pure parsing, no API calls involved."""

    def test_newline_separated_locality_before_a_district_hint(self):
        # The real Beem convention (see ExtractPostcodeHintTests above).
        self.assertEqual(geocode.extract_area_hint("Clove \nLondon Bridge SE1"), "London Bridge")

    def test_comma_separated_locality_before_a_full_postcode(self):
        self.assertEqual(geocode.extract_area_hint("Nutmeg House, London Bridge, SE1 2AA"), "London Bridge")

    def test_comma_separated_locality_before_a_bare_district(self):
        self.assertEqual(geocode.extract_area_hint("Nutmeg House, London Bridge, SE1"), "London Bridge")

    def test_a_single_unbroken_line_has_no_safe_area_hint(self):
        # No separator at all - too ambiguous to guess where the building
        # name ends and a locality would begin.
        self.assertIsNone(geocode.extract_area_hint("New Derwent House WC1"))

    def test_a_street_address_segment_is_never_mistaken_for_a_locality(self):
        self.assertIsNone(geocode.extract_area_hint("Bridge House, 22 Newman Street WC1"))
        self.assertIsNone(geocode.extract_area_hint("22 Newman Street, WC1"))

    def test_no_postcode_hint_at_all_has_no_area_hint_either(self):
        self.assertIsNone(geocode.extract_area_hint("Nutmeg House, London Bridge"))

    def test_blank_text_has_no_area_hint(self):
        self.assertIsNone(geocode.extract_area_hint(""))
        self.assertIsNone(geocode.extract_area_hint(None))


class DistrictConflictTests(unittest.TestCase):
    """_postcode_hint_conflicts - the actual accept/reject decision."""

    def test_same_district_different_subdivision_letter_is_not_a_conflict(self):
        # "EC1" (source) and "EC1V" (candidate) are the same numbered
        # district - the trailing letter is a finer subdivision, not a
        # genuinely different location.
        source = geocode.extract_postcode_hint("Red Lion Studios EC1")
        self.assertFalse(geocode._postcode_hint_conflicts(source, "EC1V 4NJ"))

    def test_different_area_letters_is_a_conflict(self):
        # The confirmed real failure's own shape: WC1 vs W1S.
        source = geocode.extract_postcode_hint("New Derwent House WC1")
        self.assertTrue(geocode._postcode_hint_conflicts(source, "W1S 2ER"))

    def test_different_area_letters_entirely_is_a_conflict(self):
        # The user's own illustrative pattern: SE1 vs EC1V.
        source = geocode.extract_postcode_hint("XYZ Building SE1")
        self.assertTrue(geocode._postcode_hint_conflicts(source, "EC1V 4NJ"))

    def test_same_area_different_district_number_is_a_conflict(self):
        source = geocode.extract_postcode_hint("Some Building SE1")
        self.assertTrue(geocode._postcode_hint_conflicts(source, "SE11 5HY"))

    def test_no_source_hint_never_conflicts(self):
        self.assertFalse(geocode._postcode_hint_conflicts(None, "EC1V 4NJ"))

    def test_unparseable_candidate_postcode_never_conflicts(self):
        source = geocode.extract_postcode_hint("New Derwent House WC1")
        self.assertFalse(geocode._postcode_hint_conflicts(source, None))
        self.assertFalse(geocode._postcode_hint_conflicts(source, "not a postcode"))


class GeocodeRowLocationValidationTests(unittest.TestCase):
    """geocode_row's own end-to-end acceptance decision, mocking the API."""

    def setUp(self):
        geocode.FAILURES.clear()

    _SAVILE_ROW_COMPONENTS = [
        {"longText": "25", "types": ["street_number"]},
        {"longText": "Savile Row", "types": ["route"]},
        {"longText": "W1S 2ER", "types": ["postal_code"]},
    ]

    # Same real place, but shaped like a legacy Geocoding API (reverse-geocode)
    # response - "long_name" keys, not "longText" (see _address_line1_and_
    # postcode's own name_key param) - used only where a test mocks
    # call_reverse_geocoding_api rather than call_places_text_search.
    _SAVILE_ROW_COMPONENTS_REVERSE = [
        {"long_name": "25", "types": ["street_number"]},
        {"long_name": "Savile Row", "types": ["route"]},
        {"long_name": "W1S 2ER", "types": ["postal_code"]},
    ]

    def test_the_confirmed_real_failure_is_now_rejected_not_accepted(self):
        # Real building text from beem Live Flex Availability.xlsx, real
        # (previously-observed) Places response shape. Now also exercises
        # the fallback-retry this module adds (see geocode.py's own
        # _fallback_query_texts): "New Derwent House WC1" has no comma/
        # newline separator, so the ONLY extra variant available is the
        # postcode-hint-as-its-own-segment one ("New Derwent House WC1,
        # WC1") - genuinely a different, differently-phrased query, tried
        # here since the first one conflicts, and correctly rejected too
        # (the mock returns the same wrong place for every query) rather
        # than ever accepting it just because it's a later attempt.
        row = ListingRow(building="New Derwent House WC1", provider="beem")

        with patch(
            "geocode.call_places_text_search",
            return_value={
                "status": "OK", "lat": 51.5118097, "lng": -0.1414146,
                "address_components": self._SAVILE_ROW_COMPONENTS,
            },
        ) as mock_places:
            geocode.geocode_row(row)

        self.assertEqual(mock_places.call_count, 2)
        self.assertIsNone(row.lat)
        self.assertIsNone(row.lng)
        self.assertIsNone(row.address_1)
        self.assertIsNone(row.postcode)
        self.assertEqual(len(geocode.FAILURES), 1)
        self.assertIn("contradicts", geocode.FAILURES[0]["reason"])

    def test_a_matching_district_is_accepted_exactly_as_before(self):
        # No regression: when the candidate's own postcode genuinely agrees
        # with the source's stated district, behavior is unchanged.
        row = ListingRow(building="Clove \nLondon Bridge SE1", provider="beem")
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

    def test_a_building_with_no_postcode_hint_is_unaffected(self):
        # No regression for the ordinary case with nothing to validate
        # against - identical to this module's own pre-existing tests.
        row = ListingRow(building="Kent House", provider="UNION")

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.52, "lng": -0.1, "address_components": []},
        ):
            geocode.geocode_row(row)

        self.assertEqual(row.lat, 51.52)
        self.assertEqual(geocode.FAILURES, [])

    def test_a_candidate_with_no_postcode_component_is_accepted_on_trust(self):
        # No evidence to contradict (Places' own record has no postal_code
        # component at all, e.g. a "premise"-only match) must never block a
        # match the same way a genuine contradiction does.
        row = ListingRow(building="New Derwent House WC1", provider="beem")

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.52, "lng": -0.13, "address_components": []},
        ), patch("geocode.call_reverse_geocoding_api", return_value={"status": "ZERO_RESULTS"}):
            geocode.geocode_row(row)

        self.assertEqual(row.lat, 51.52)

    def test_source_postcode_field_outranks_a_building_text_hint(self):
        # row.postcode (if the source stated one directly, separately from
        # building) is the strongest evidence - checked ahead of anything
        # merely embedded in building text.
        row = ListingRow(building="Ambiguous House WC1", provider="beem", postcode="EC1V 4NJ")

        with patch(
            "geocode.call_places_text_search",
            return_value={
                "status": "OK", "lat": 51.5118097, "lng": -0.1414146,
                "address_components": self._SAVILE_ROW_COMPONENTS,
            },
        ):
            geocode.geocode_row(row)

        # EC1V (source postcode) vs W1S (candidate) - still a conflict,
        # proving row.postcode (not the building text's WC1) drove the check.
        self.assertIsNone(row.lat)

    def test_address_1_hint_is_also_checked_when_no_postcode_field_exists(self):
        # address_1 deliberately has NO leading house number of its own -
        # geocode_row's own Tier 1 now also runs for a NUMBERED address_1
        # with no postcode yet (see the module docstring's own "Tier 1
        # also runs for a row with a genuinely numbered address_1..."
        # paragraph), which would otherwise intercept this row before Tier
        # 2 (this test's own actual point) is ever reached - "Ambiguous
        # House" alone has no house number either, so this stays a genuine
        # zero-hint-for-Tier-1 row, reaching Tier 2 exactly as intended.
        row = ListingRow(building="Ambiguous House", provider="beem", address_1="Somewhere Lane SE1")

        with patch(
            "geocode.call_places_text_search",
            return_value={
                "status": "OK", "lat": 51.5118097, "lng": -0.1414146,
                "address_components": self._SAVILE_ROW_COMPONENTS,
            },
        ):
            geocode.geocode_row(row)

        self.assertIsNone(row.lat)

    def test_compound_building_conflict_falls_through_to_the_full_value(self):
        # "Bridge House, 22 Newman Street WC1" - source hint WC1 comes from
        # the OVERALL building text even though the address-only candidate
        # ("22 Newman Street WC1") is what's actually queried first; if that
        # candidate conflicts, the loop still tries the full building value
        # before giving up, exactly like the existing zero-results fallback.
        row = ListingRow(building="Bridge House, 22 Newman Street WC1", provider="Kitt's")
        queried = []

        def fake_places(query):
            queried.append(query)
            if query.startswith("22 Newman Street"):
                return {
                    "status": "OK", "lat": 51.5118097, "lng": -0.1414146,
                    "address_components": self._SAVILE_ROW_COMPONENTS,
                }
            return {
                "status": "OK", "lat": 51.52, "lng": -0.12,
                "address_components": [{"longText": "WC1 4PX", "types": ["postal_code"]}],
            }

        with patch("geocode.call_places_text_search", side_effect=fake_places):
            geocode.geocode_row(row)

        self.assertEqual(len(queried), 2)
        self.assertEqual(row.lat, 51.52)
        self.assertEqual(row.postcode, "WC1 4PX")

    def test_all_candidates_conflicting_leaves_the_row_blank(self):
        row = ListingRow(building="New Derwent House WC1", provider="beem")

        with patch(
            "geocode.call_places_text_search",
            return_value={
                "status": "OK", "lat": 51.5118097, "lng": -0.1414146,
                "address_components": self._SAVILE_ROW_COMPONENTS,
            },
        ):
            geocode.geocode_row(row)

        self.assertIsNone(row.lat)
        self.assertIsNone(row.lng)
        self.assertEqual(len(geocode.FAILURES), 1)

    def test_reverse_geocode_backfill_conflict_is_also_rejected(self):
        # The accepted candidate itself has no postal_code component (so it
        # passed accept-time validation on trust), but the reverse-geocode
        # used to fill in the missing address/postcode surfaces one that
        # DOES contradict the source's own evidence - must still be
        # rejected, leaving address_1/postcode blank rather than writing a
        # confident-but-wrong value; lat/lng (already accepted) are kept.
        row = ListingRow(building="New Derwent House WC1", provider="beem")

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.5118097, "lng": -0.1414146, "address_components": []},
        ), patch(
            "geocode.call_reverse_geocoding_api",
            return_value={"status": "OK", "address_components": self._SAVILE_ROW_COMPONENTS_REVERSE},
        ):
            geocode.geocode_row(row)

        self.assertEqual(row.lat, 51.5118097)
        self.assertIsNone(row.address_1)
        self.assertIsNone(row.postcode)
        self.assertTrue(any("contradicts" in f["reason"] for f in geocode.FAILURES))

    def test_reverse_geocode_backfill_with_no_conflict_still_works(self):
        # No regression: the existing Kent-House-style backfill (no
        # conflict at all) is unaffected.
        row = ListingRow(building="Kent House", provider="UNION")
        components = [
            {"long_name": "16", "types": ["street_number"]},
            {"long_name": "Mortimer Street", "types": ["route"]},
            {"long_name": "W1T 3JL", "types": ["postal_code"]},
        ]

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", "lat": 51.5188, "lng": -0.1381, "address_components": []},
        ), patch("geocode.call_reverse_geocoding_api", return_value={"status": "OK", "address_components": components}):
            geocode.geocode_row(row)

        self.assertEqual(row.address_1, "16 Mortimer Street")
        self.assertEqual(row.postcode, "W1T 3JL")


class GeocodeRowsGroupingIdentityTests(unittest.TestCase):
    """
    Confirms _physical_identity_key's existing exact-match (never fuzzy)
    tuple already provides the regression guarantee this task's own
    requirement #10 asks for - two rows with merely SIMILAR (not identical,
    normalize_key-tolerant) building names must never collapse into one
    shared geocoding group and risk cross-contaminating each other's
    location. No code change needed here; this test only verifies the
    existing guarantee still holds after the validation change above.
    """

    def setUp(self):
        geocode.FAILURES.clear()

    def test_similar_but_distinct_building_names_are_never_grouped(self):
        row_a = ListingRow(building="New Derwent House WC1", provider="beem")
        row_b = ListingRow(building="New Derwent House", provider="beem")

        def fake_places(query):
            if "WC1" in query:
                return {"status": "OK", "lat": 51.52, "lng": -0.12, "address_components": []}
            return {"status": "OK", "lat": 51.50, "lng": -0.09, "address_components": []}

        with patch("geocode.call_places_text_search", side_effect=fake_places) as mock_places:
            geocode.geocode_rows([row_a, row_b])

        self.assertEqual(mock_places.call_count, 2)
        self.assertNotEqual((row_a.lat, row_a.lng), (row_b.lat, row_b.lng))


class MultiCandidateFallbackTests(unittest.TestCase):
    """
    Regression tests for the confirmed Part B gap: geocode_row's Tier 2
    previously gave up the moment its one candidate conflicted, even when a
    differently-phrased query (see geocode.py's own _fallback_query_texts)
    or a different candidate from the SAME Places response (see
    call_places_text_search's own "candidates" list / _best_places_result)
    would have found a safe, non-conflicting match.
    """

    def setUp(self):
        geocode.FAILURES.clear()

    _WRONG_PLACE = {
        "lat": 51.5118097, "lng": -0.1414146,
        "address_components": [{"longText": "W1S 2ER", "types": ["postal_code"]}],
    }
    _RIGHT_PLACE = {
        "lat": 51.5230, "lng": -0.1130,
        "address_components": [{"longText": "WC1X 8NP", "types": ["postal_code"]}],
    }

    def test_a_conflicting_candidate_causes_a_differently_phrased_fallback_query(self):
        # "New Derwent House WC1" has no comma/newline, so the only base
        # candidate is the mashed full-text query - the fallback variant
        # (postcode hint as its own explicit segment) must still be tried
        # once that one conflicts.
        row = ListingRow(building="New Derwent House WC1", provider="beem")
        queried = []

        def fake_places(query):
            queried.append(query)
            if query == "New Derwent House WC1, London, UK":
                return {"status": "OK", **self._WRONG_PLACE}
            return {"status": "OK", **self._RIGHT_PLACE}

        with patch("geocode.call_places_text_search", side_effect=fake_places):
            geocode.geocode_row(row)

        self.assertEqual(queried, ["New Derwent House WC1, London, UK", "New Derwent House WC1, WC1, London, UK"])
        self.assertEqual(row.postcode, "WC1X 8NP")
        self.assertEqual(geocode.FAILURES, [])

    def test_a_source_area_hint_also_produces_a_fallback_query(self):
        # "Nutmeg House \nLondon Bridge SE1" - the newline-separated area
        # hint (see extract_area_hint) becomes its own explicit query
        # variant, tried after the plain building-text attempt conflicts.
        row = ListingRow(building="Nutmeg House \nLondon Bridge SE1", provider="beem")
        queried = []
        area_hint_query = "Nutmeg House \nLondon Bridge SE1, London Bridge, London, UK"

        def fake_places(query):
            queried.append(query)
            if query == area_hint_query:
                return {
                    "status": "OK", "lat": 51.505, "lng": -0.087,
                    "address_components": [{"longText": "SE1 2AA", "types": ["postal_code"]}],
                }
            return {
                "status": "OK", "lat": 51.5118097, "lng": -0.1414146,
                "address_components": [{"longText": "EC1V 4NJ", "types": ["postal_code"]}],
            }

        with patch("geocode.call_places_text_search", side_effect=fake_places):
            geocode.geocode_row(row)

        self.assertEqual(
            queried,
            [
                "Nutmeg House \nLondon Bridge SE1, London, UK",
                "Nutmeg House \nLondon Bridge SE1, SE1, London, UK",
                area_hint_query,
            ],
        )
        self.assertEqual(row.postcode, "SE1 2AA")

    def test_a_second_candidate_from_the_same_places_response_is_accepted(self):
        # call_places_text_search now exposes every candidate a search
        # returns, not just the top one - a conflicting FIRST candidate
        # must not block a SECOND, non-conflicting candidate from the exact
        # same query/response.
        row = ListingRow(building="New Derwent House WC1", provider="beem")

        with patch(
            "geocode.call_places_text_search",
            return_value={
                "status": "OK",
                "candidates": [self._WRONG_PLACE, self._RIGHT_PLACE],
                **self._WRONG_PLACE,
            },
        ) as mock_places:
            geocode.geocode_row(row)

        mock_places.assert_called_once()
        self.assertEqual(row.postcode, "WC1X 8NP")
        self.assertEqual(geocode.FAILURES, [])

    def test_lat_lng_are_never_written_before_a_candidate_passes_validation(self):
        row = ListingRow(building="New Derwent House WC1", provider="beem")

        with patch(
            "geocode.call_places_text_search",
            return_value={"status": "OK", **self._WRONG_PLACE},
        ):
            geocode.geocode_row(row)

        self.assertIsNone(row.lat)
        self.assertIsNone(row.lng)


if __name__ == "__main__":
    unittest.main()
