"""
Regression tests for master_merge.py's pure merge/diff logic - no
Streamlit, no storage, no network. Run with:

    .venv\\Scripts\\python.exe -m unittest tests.test_master_merge -v

(from the repo root, so `master_merge`/`schema` resolve as top-level
imports the way the app itself imports them).
"""

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import master_merge
from schema import ListingRow

BOND_STREET_AMENITIES = (
    "Bike racks, passenger lifts, LED lighting, lockers, showers, "
    "roof terrace with views"
)


def _master_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([ListingRow(**r).model_dump() for r in rows])


class NormalizeKeyTests(unittest.TestCase):
    def test_case_and_punctuation_insensitive(self):
        self.assertEqual(master_merge.normalize_key("Bond St., 1st Floor"), "bond st 1st floor")

    def test_none_is_empty_string(self):
        self.assertEqual(master_merge.normalize_key(None), "")


class NormalizePostcodeTests(unittest.TestCase):
    def test_with_and_without_inward_code_space_are_equal(self):
        # Real reported case: "W1T 4PW" vs "W1T4PW" for the same 77
        # Charlotte Street unit - normalize_key alone doesn't catch this
        # (it collapses whitespace runs but doesn't remove them), unlike
        # _normalize_text which already treats these as identical for
        # diffing purposes.
        self.assertEqual(master_merge._normalize_postcode("W1T 4PW"), master_merge._normalize_postcode("W1T4PW"))

    def test_still_case_and_punctuation_insensitive(self):
        self.assertEqual(master_merge._normalize_postcode("w1t 4pw"), "w1t4pw")


class CanonicalizeProviderNameTests(unittest.TestCase):
    def test_plus_symbol_variant_is_corrected(self):
        self.assertEqual(master_merge.canonicalize_provider_name("Workplace+"), "Workplace Plus")

    def test_all_caps_plus_variant_is_corrected(self):
        # Real Gemini output, seen live on repeated extraction of the same
        # real "77 Gracechurch Street" brochure.
        self.assertEqual(master_merge.canonicalize_provider_name("WORKPLACE+"), "Workplace Plus")

    def test_already_canonical_is_unchanged(self):
        # The document's own literal text ("At Workplace Plus, we believe...").
        self.assertEqual(master_merge.canonicalize_provider_name("Workplace Plus"), "Workplace Plus")

    def test_case_only_variant_is_corrected(self):
        # Real spreadsheet data: "Metspace" vs "MetSpace" elsewhere.
        self.assertEqual(master_merge.canonicalize_provider_name("Metspace"), "MetSpace")
        self.assertEqual(master_merge.canonicalize_provider_name("metspace"), "MetSpace")

    def test_apostrophe_insensitive(self):
        self.assertEqual(master_merge.canonicalize_provider_name("Kitts"), "Kitt's")

    def test_slash_spacing_variant_is_corrected(self):
        self.assertEqual(master_merge.canonicalize_provider_name("JLL/HK London"), "JLL / HK London")

    def test_unknown_provider_passes_through_unchanged(self):
        # Not on KNOWN_PROVIDERS - never guessed at, never coerced toward
        # the nearest known name.
        self.assertEqual(master_merge.canonicalize_provider_name("Newco Realty"), "Newco Realty")

    def test_blank_passes_through_unchanged(self):
        self.assertIsNone(master_merge.canonicalize_provider_name(None))
        self.assertEqual(master_merge.canonicalize_provider_name(""), "")


class CanonicalizeProvidersTests(unittest.TestCase):
    def test_mutates_every_row_in_place(self):
        rows = [
            ListingRow(building="A", provider="Workplace+"),
            ListingRow(building="B", provider="WORKPLACE+"),
            ListingRow(building="C", provider="Newco Realty"),
            ListingRow(building="D", provider=None),
        ]

        master_merge.canonicalize_providers(rows)

        self.assertEqual([r.provider for r in rows], ["Workplace Plus", "Workplace Plus", "Newco Realty", None])


class BuildingHasNoDigitsTests(unittest.TestCase):
    def test_a_plain_name_has_no_digits(self):
        self.assertTrue(master_merge._building_has_no_digits("Kent House"))

    def test_a_numbered_address_has_digits(self):
        self.assertFalse(master_merge._building_has_no_digits("138 Cheapside"))

    def test_a_compound_values_own_address_part_still_disqualifies_it(self):
        # The digit lives in the address part, not the name - checked on
        # the WHOLE value, not just the name part, so this stays excluded
        # just like a bare numbered address (see BUILDING_FUZZY_MATCH_
        # THRESHOLD's own comment on why numbered addresses are excluded).
        self.assertFalse(master_merge._building_has_no_digits("Bridge House, 22 Newman Street"))

    def test_a_compound_value_with_no_digits_anywhere_is_eligible(self):
        # A real example: "The Rochester, Rochester Mews" has no house
        # number at all in either part.
        self.assertTrue(master_merge._building_has_no_digits("The Rochester, Rochester Mews"))

    def test_blank_is_not_eligible_either_way(self):
        self.assertFalse(master_merge._building_has_no_digits(None))


class FuzzyBuildingMatchTests(unittest.TestCase):
    def _rows(self, buildings_and_providers_and_floors):
        return [
            {"building": b, "provider": p, "floor_unit": f}
            for b, p, f in buildings_and_providers_and_floors
        ]

    def test_a_real_typo_matches(self):
        # The actual real-data pair: same provider/floor_unit, building
        # name reworded by extraction non-determinism between two uploads.
        master_records = self._rows([("Thirty Lighterman", "Kitt's", "3rd")])
        new_dict = {"building": "Thirty Lightman", "provider": "Kitt's", "floor_unit": "3rd"}

        idx = master_merge._fuzzy_building_match(new_dict, [0], master_records)

        self.assertEqual(idx, 0)

    def test_a_numbered_address_near_miss_never_matches(self):
        # Confirmed against real master data: a genuinely different real
        # property routinely scores just as high or higher than an actual
        # typo (see BUILDING_FUZZY_MATCH_THRESHOLD's own comment) - a
        # numbered address must never reach the fuzzy tier at all.
        master_records = self._rows([("138 Cheapside", "Kitt's", "1st")])
        new_dict = {"building": "139 Cheapside", "provider": "Kitt's", "floor_unit": "1st"}

        self.assertIsNone(master_merge._fuzzy_building_match(new_dict, [0], master_records))

    def test_the_real_confirmed_false_positive_risk_never_matches(self):
        # "20 St James's Square" vs "30 St James's Square" - confirmed
        # different real properties (different postcodes) in real master
        # data, scoring HIGHER than either genuine typo pair.
        master_records = self._rows([("20 St James's Square", "Kitt's", "1st")])
        new_dict = {"building": "30 St James's Square", "provider": "Kitt's", "floor_unit": "1st"}

        self.assertIsNone(master_merge._fuzzy_building_match(new_dict, [0], master_records))

    def test_ambiguous_when_more_than_one_candidate_clears_the_threshold(self):
        master_records = self._rows([
            ("Thirty Lighterman", "Kitt's", "3rd"),
            ("Thirty Lightman", "Kitt's", "3rd"),
        ])
        new_dict = {"building": "Thirty Lightmann", "provider": "Kitt's", "floor_unit": "3rd"}

        self.assertIsNone(master_merge._fuzzy_building_match(new_dict, [0, 1], master_records))

    def test_a_candidate_outside_the_anchor_never_gets_considered(self):
        # build_merge_plan only ever passes candidates sharing the exact
        # provider+floor_unit anchor (see _fuzzy_anchor_key) - this directly
        # tests that a mismatched real-data pair (different provider AND
        # postcode, "Conran Building" vs "Cowan Building") is never even
        # offered as a candidate once anchored correctly.
        master_records = self._rows([("Conran Building", "Kitt's", "3rd")])
        new_dict = {"building": "Cowan Building", "provider": "", "floor_unit": "3rd"}

        # Simulating build_merge_plan's own anchor lookup: candidate_indices
        # would be empty here since the anchors (provider) differ.
        self.assertEqual(master_merge._fuzzy_anchor_key(master_records[0]), ("kitts", "3rd"))
        self.assertNotEqual(master_merge._fuzzy_anchor_key(master_records[0]), master_merge._fuzzy_anchor_key(new_dict))

    def test_unrelated_pure_name_buildings_stay_below_threshold(self):
        # The closest unrelated real pair found across the whole project's
        # real building-name data.
        master_records = self._rows([("Orion House", "Kitt's", "1st")])
        new_dict = {"building": "York House", "provider": "Kitt's", "floor_unit": "1st"}

        self.assertIsNone(master_merge._fuzzy_building_match(new_dict, [0], master_records))


class BuildMergePlanFuzzyBuildingTests(unittest.TestCase):
    def test_a_typo_only_building_name_matches_via_the_fuzzy_tier(self):
        master_df = _master_df([{"building": "Thirty Lighterman", "provider": "Kitt's", "floor_unit": "3rd"}])
        new_row = ListingRow(building="Thirty Lightman", provider="Kitt's", floor_unit="3rd")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed) + len(plan.matched_unchanged), 1)
        matched = (plan.matched_changed + plan.matched_unchanged)[0]
        self.assertEqual(matched.match_tier, "fuzzy_building")
        self.assertEqual(matched.master_index, 0)

    def test_an_exact_match_never_falls_through_to_the_fuzzy_tier(self):
        master_df = _master_df([{"building": "Kent House", "provider": "Kitt's", "floor_unit": "3rd"}])
        new_row = ListingRow(building="Kent House", provider="Kitt's", floor_unit="3rd")

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = (plan.matched_changed + plan.matched_unchanged)[0]
        self.assertIn(matched.match_tier, ("postcode", "fallback"))

    def test_postcode_whitespace_variant_still_matches_via_postcode_tier(self):
        # Same real case as NormalizePostcodeTests, at the build_merge_plan
        # level: a numbered address (fuzzy tier barred, see
        # BuildingHasNoDigitsTests) whose postcode differs only by the
        # inward-code space must still hit the postcode tier, not fall
        # through to fallback/unmatched.
        master_df = _master_df([{
            "building": "The Sevens, 77 Charlotte Street", "provider": "Kitt's",
            "floor_unit": "1st", "postcode": "W1T 4PW",
        }])
        new_row = ListingRow(
            building="The Sevens, 77 Charlotte Street", provider="Kitt's",
            floor_unit="1st", postcode="W1T4PW",
        )

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed) + len(plan.matched_unchanged), 1)
        matched = (plan.matched_changed + plan.matched_unchanged)[0]
        self.assertEqual(matched.match_tier, "postcode")

    def test_a_numbered_address_near_miss_is_unmatched_not_fuzzy_matched(self):
        master_df = _master_df([{"building": "138 Cheapside", "provider": "Kitt's", "floor_unit": "1st"}])
        new_row = ListingRow(building="139 Cheapside", provider="Kitt's", floor_unit="1st")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.unmatched), 1)
        self.assertEqual(len(plan.matched_changed) + len(plan.matched_unchanged), 0)

    def test_different_provider_never_reaches_the_fuzzy_tier(self):
        # The real "Conran Building"/"Cowan Building" case - provider also
        # differs (a separate, pre-existing duplicate from the provider-name
        # bug, not something this tier is meant to fix) - correctly stays
        # unmatched rather than guessing.
        master_df = _master_df([{"building": "Conran Building", "provider": "Kitt's", "floor_unit": "3rd"}])
        new_row = ListingRow(building="Cowan Building", provider=None, floor_unit="3rd")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.unmatched), 1)


class DiffFieldsTests(unittest.TestCase):
    def test_changed_value_is_a_diff(self):
        diffs = master_merge.diff_fields({"building": "A"}, {"building": "B"})
        self.assertEqual(diffs, {"building": ("A", "B")})

    def test_blank_new_value_never_overwrites_old(self):
        diffs = master_merge.diff_fields({"building": "A"}, {"building": None})
        self.assertEqual(diffs, {})

    def test_first_time_value_is_a_diff(self):
        diffs = master_merge.diff_fields({"building": None}, {"building": "A"})
        self.assertEqual(diffs, {"building": (None, "A")})

    def test_tolerant_formatting_difference_is_not_a_diff(self):
        diffs = master_merge.diff_fields({"provider": "Metspace"}, {"provider": "METSPACE"})
        self.assertEqual(diffs, {})


class SilentFieldUpdatesTests(unittest.TestCase):
    def test_case_only_change_is_silent(self):
        updates = master_merge.silent_field_updates({"provider": "Metspace"}, {"provider": "METSPACE"})
        self.assertEqual(updates, {"provider": "METSPACE"})

    def test_real_change_is_not_silent(self):
        updates = master_merge.silent_field_updates({"provider": "A"}, {"provider": "B"})
        self.assertEqual(updates, {})


class IsDetailLossTests(unittest.TestCase):
    """The core safeguard: is a free-text update dropping a genuine item,
    as opposed to rewording/compressing the same fact into fewer words?"""

    def test_full_amenity_list_replaced_by_one_liner_is_flagged(self):
        self.assertTrue(master_merge.is_detail_loss(BOND_STREET_AMENITIES, "Available Q3 2026"))

    def test_long_single_fact_reworded_much_shorter_is_not_flagged(self):
        # The actual reported false positive: a much shorter value that's
        # still the same underlying fact, just reworded/compressed - must
        # not be flagged purely for being short.
        old = "Benefits from a large private terrace landscaped with plants, trees and premium Italian outdoor furniture"
        new = "Private landscaped terrace"
        self.assertFalse(master_merge.is_detail_loss(old, new))

    def test_comma_only_rewording_with_no_semicolons_is_not_flagged(self):
        # Neither value has a ";" to itemize on, so this is compared as one
        # whole-value "item" rather than several - a real, if coarse, trade-
        # off (see _detail_items' docstring): a legitimate compression like
        # this is never flagged, at the cost of not being able to tell
        # "genuinely dropped unrelated amenities" apart from "reworded" when
        # the source text was never itemized with semicolons to begin with.
        old = "Fully fitted, CAT A+ finish, breakout area, phone booths, kitchen"
        new = "Fitted"
        self.assertFalse(master_merge.is_detail_loss(old, new))

    def test_semicolon_itemized_drop_with_nothing_replacing_it_is_flagged(self):
        # With real items (";"-delimited, matching the documented extraction
        # format), dropping one item entirely - not rewording it, just
        # removing it - is caught at the individual-item level.
        old = "Bike racks; passenger lifts; LED lighting; lockers; showers; roof terrace"
        new = "Bike racks; passenger lifts; LED lighting; lockers; showers"
        self.assertTrue(master_merge.is_detail_loss(old, new))

    def test_semicolon_itemized_reword_of_every_item_is_not_flagged(self):
        old = "Bike racks; passenger lifts; roof terrace with panoramic views"
        new = "Racks for bikes; lifts for passengers; a terrace with panoramic views"
        self.assertFalse(master_merge.is_detail_loss(old, new))

    def test_comma_within_a_single_semicolon_item_is_not_split(self):
        # "deposit £36,000 required" is ONE item per the documented format
        # (extract.py's own example) - splitting on comma would shred it
        # into "deposit £36" / "000 required", corrupting the comparison.
        old = "2 meeting rooms; deposit £36,000 required; 50Mb dedicated bandwidth"
        new = "2 meeting rooms; a deposit of £36,000 is required; 50Mb dedicated bandwidth"
        self.assertFalse(master_merge.is_detail_loss(old, new))

    def test_contacts_dropping_a_whole_person_is_flagged(self):
        old = "Jane Smith, jane@example.com, 020 1234 5678; John Doe, john@example.com, 020 8765 4321"
        new = "Jane Smith, jane@example.com, 020 1234 5678"
        self.assertTrue(master_merge.is_detail_loss(old, new))

    def test_similar_length_but_mostly_different_content_is_flagged(self):
        old = "Bike racks, passenger lifts, LED lighting, lockers, showers, terrace"
        new = "Under offer, viewings by appointment only, contact agent for details"
        self.assertTrue(master_merge.is_detail_loss(old, new))

    def test_dropping_one_of_six_items_is_not_flagged(self):
        old = "Bike racks, passenger lifts, LED lighting, lockers, showers, roof terrace"
        new = "Bike racks, passenger lifts, LED lighting, lockers, showers"
        self.assertFalse(master_merge.is_detail_loss(old, new))

    def test_genuinely_shorter_but_still_mostly_overlapping_is_not_flagged(self):
        old = "Bike racks, lockers, showers"
        new = "Bike racks, lockers, showers, and a new rooftop terrace"
        self.assertFalse(master_merge.is_detail_loss(old, new))

    def test_blank_values_never_flagged(self):
        self.assertFalse(master_merge.is_detail_loss(None, "Available now"))
        self.assertFalse(master_merge.is_detail_loss(BOND_STREET_AMENITIES, None))


class IsRichnessRegressionTests(unittest.TestCase):
    """The backstop for is_detail_loss's own documented blind spot: a value
    with no ";"/newline to itemize on collapses to one "item" for item-loss,
    so a hard compression of un-itemized text can pass item-loss cleanly
    even though real content vanished. See RICHNESS_RATIO_THRESHOLD's own
    comment for the real cases these thresholds were checked against."""

    def test_full_amenity_list_replaced_by_one_liner_is_flagged(self):
        # Same real pair as IsDetailLossTests - already caught by item-loss
        # too, but richness must also independently recognize it (ratio 0.25).
        self.assertTrue(master_merge.is_richness_regression(BOND_STREET_AMENITIES, "Available Q3 2026"))

    def test_comma_only_compression_is_flagged_where_item_loss_misses_it(self):
        # The actual documented item-loss blind spot (see
        # test_comma_only_rewording_with_no_semicolons_is_not_flagged) -
        # ratio 0.10, exactly what this check exists to catch.
        old = "Fully fitted, CAT A+ finish, breakout area, phone booths, kitchen"
        new = "Fitted"
        self.assertFalse(master_merge.is_detail_loss(old, new))
        self.assertTrue(master_merge.is_richness_regression(old, new))

    def test_long_single_fact_reworded_much_shorter_is_a_known_accepted_false_positive(self):
        # Same real pair is_detail_loss correctly does NOT flag (a genuine
        # compressed paraphrase of one fact, not a loss) - richness flags it
        # anyway (ratio 0.20, even lower than the amenity-loss case above),
        # because no pure length measure can tell these two cases apart (see
        # RICHNESS_RATIO_THRESHOLD's comment). Accepted: this only forces a
        # review a human can immediately dismiss, never a silent discard.
        old = "Benefits from a large private terrace landscaped with plants, trees and premium Italian outdoor furniture"
        new = "Private landscaped terrace"
        self.assertFalse(master_merge.is_detail_loss(old, new))
        self.assertTrue(master_merge.is_richness_regression(old, new))

    def test_semicolon_reword_with_growth_is_not_flagged(self):
        old = "Bike racks; passenger lifts; roof terrace with panoramic views"
        new = "Racks for bikes; lifts for passengers; a terrace with panoramic views"
        self.assertFalse(master_merge.is_richness_regression(old, new))

    def test_comma_within_a_single_item_with_growth_is_not_flagged(self):
        old = "2 meeting rooms; deposit £36,000 required; 50Mb dedicated bandwidth"
        new = "2 meeting rooms; a deposit of £36,000 is required; 50Mb dedicated bandwidth"
        self.assertFalse(master_merge.is_richness_regression(old, new))

    def test_dropping_one_of_six_items_is_not_flagged(self):
        # Ratio 0.80 - a minor trim, not "meaningfully shorter".
        old = "Bike racks, passenger lifts, LED lighting, lockers, showers, roof terrace"
        new = "Bike racks, passenger lifts, LED lighting, lockers, showers"
        self.assertFalse(master_merge.is_richness_regression(old, new))

    def test_exactly_half_is_not_flagged(self):
        # Strictly under the threshold, not at-or-under - a clean halving
        # sits right at the boundary rather than past it.
        self.assertFalse(master_merge.is_richness_regression("one two three four", "five six"))

    def test_real_gracechurch_street_reword_pair_is_not_flagged(self):
        # Real data: two listings of the same 77 Gracechurch Street unit,
        # special_features reworded ("Fully Managed" vs "Managed lease" etc.)
        # but not shortened (ratio 1.03).
        old = (
            "+ 3 meeting rooms + boardroom + executive office + collaboration space; 3-5 years term; "
            "Fully Managed; Building Concierge; Air Conditioning; Secure Bicycle Storage; Passenger Lifts; "
            "24/7 Access; Shower & Locker Facilities"
        )
        new = (
            "+ 3 meeting rooms + boardroom + executive office + collaboration space; 3-5 years lease term; "
            "Managed lease; Building concierge; Air conditioning; Secure bicycle storage; Passenger lifts; "
            "24/7 access; Shower & locker facilities"
        )
        self.assertFalse(master_merge.is_richness_regression(old, new))

    def test_blank_values_never_flagged(self):
        self.assertFalse(master_merge.is_richness_regression(None, "Available now"))
        self.assertFalse(master_merge.is_richness_regression(BOND_STREET_AMENITIES, None))


class BuildMergePlanRiskyFieldsTests(unittest.TestCase):
    """The exact reported scenario: a re-upload of an existing floor whose
    special_features shrinks from a full amenity list to a one-line
    availability status must not be silently auto-appliable."""

    def test_special_features_detail_loss_is_flagged_and_still_diffed(self):
        master_df = _master_df([{
            "building": "40 New Bond Street",
            "provider": "Workplace Plus",
            "floor_unit": "3rd Floor",
            "postcode": "W1S 2SP",
            "special_features": BOND_STREET_AMENITIES,
        }])
        new_row = ListingRow(
            building="40 New Bond Street",
            provider="Workplace Plus",
            floor_unit="3rd Floor",
            postcode="W1S 2SP",
            special_features="Available Q3 2026",
        )

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed), 1)
        matched = plan.matched_changed[0]
        # Still a real diff - the safeguard must not make the change disappear,
        # only stop it from being auto-applied.
        self.assertIn("special_features", matched.diffs)
        self.assertEqual(matched.diffs["special_features"], (BOND_STREET_AMENITIES, "Available Q3 2026"))
        # This is the actual gate pages/2_Review_and_Master.py uses to skip
        # auto-accept - see `auto_accept and not is_collision and not is_risky`.
        self.assertIn("special_features", matched.risky_fields)
        self.assertFalse(plan.collisions)  # not a collision - the safeguard is independent of that path

    def test_richness_regression_flags_at_plan_level_where_item_loss_alone_would_not(self):
        # item-loss's own documented blind spot (no ";" to itemize on) - at
        # the build_merge_plan level, still ends up in risky_fields (and so
        # still excluded from auto-accept) because is_richness_regression is
        # ORed into the same computation, not a separate gate.
        master_df = _master_df([{
            "building": "1 Example Street",
            "provider": "Test Provider",
            "floor_unit": "1st Floor",
            "postcode": "EC1A 1AA",
            "special_features": "Fully fitted, CAT A+ finish, breakout area, phone booths, kitchen",
        }])
        new_row = ListingRow(
            building="1 Example Street",
            provider="Test Provider",
            floor_unit="1st Floor",
            postcode="EC1A 1AA",
            special_features="Fitted",
        )

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = plan.matched_changed[0]
        self.assertIn("special_features", matched.diffs)
        self.assertIn("special_features", matched.risky_fields)

    def test_normal_special_features_update_is_not_flagged(self):
        master_df = _master_df([{
            "building": "1 Example Street",
            "provider": "Test Provider",
            "floor_unit": "1st Floor",
            "postcode": "EC1A 1AA",
            "special_features": "Bike racks, showers",
        }])
        new_row = ListingRow(
            building="1 Example Street",
            provider="Test Provider",
            floor_unit="1st Floor",
            postcode="EC1A 1AA",
            special_features="Bike racks, showers, new rooftop terrace",
        )

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = plan.matched_changed[0]
        self.assertIn("special_features", matched.diffs)
        self.assertEqual(matched.risky_fields, frozenset())

    def test_non_risky_field_shrinking_is_never_flagged(self):
        # address_1 isn't in RISKY_TEXT_FIELDS - a much shorter new value
        # there is a normal diff, not a detail-loss caution. (building/
        # provider/floor_unit/postcode are excluded from this test because
        # they're match-key fields - changing them would stop the row from
        # matching at all, rather than producing a diff to inspect.)
        master_df = _master_df([{
            "building": "1 Example Street",
            "provider": "Test Provider",
            "floor_unit": "1st Floor",
            "postcode": "EC1A 1AA",
            "address_1": "123 Long Address Street Which Is Quite Verbose Indeed",
        }])
        new_row = ListingRow(
            building="1 Example Street",
            provider="Test Provider",
            floor_unit="1st Floor",
            postcode="EC1A 1AA",
            address_1="123 L St",
        )

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = plan.matched_changed[0]
        self.assertIn("address_1", matched.diffs)
        self.assertEqual(matched.risky_fields, frozenset())


class CollisionTests(unittest.TestCase):
    def test_two_new_rows_matching_same_master_row_are_a_collision(self):
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "Test Provider",
            "floor_unit": "1st Floor", "postcode": "EC1A 1AA",
        }])
        rows = [
            ListingRow(building="1 Example Street", provider="Test Provider",
                       floor_unit="1st Floor", postcode="EC1A 1AA", size_sqft=1000.0),
            ListingRow(building="1 Example Street", provider="Test Provider",
                       floor_unit="1st Floor", postcode="EC1A 1AA", size_sqft=2000.0),
        ]

        plan = master_merge.build_merge_plan(rows, master_df)

        self.assertEqual(len(plan.collisions), 1)
        self.assertEqual(len(plan.collisions[0]), 2)


class BuildManualEditTests(unittest.TestCase):
    """The Master default view's direct cell-editing feature - a data_editor
    'edited_rows' delta turned into a full row list + a diff summary, via
    the exact same shape/mechanism as a normal approve's apply_merge."""

    def _master_records(self, rows: list[dict]) -> list[dict]:
        return [ListingRow(**r).model_dump() for r in rows]

    def test_single_cell_edit(self):
        master_records = self._master_records([
            {"building": "40 New Bond Street", "provider": "Workplace Plus", "size_sqft": 5000.0},
        ])
        edited_rows = {0: {"size_sqft": 6000.0}}

        merged_rows, diff_rows, fields_changed = master_merge.build_manual_edit(master_records, edited_rows)

        self.assertEqual(fields_changed, 1)
        self.assertEqual(merged_rows[0].size_sqft, 6000.0)
        self.assertEqual(len(diff_rows), 1)
        self.assertEqual(diff_rows[0]["field"], "size_sqft")
        self.assertEqual(diff_rows[0]["old"], 5000.0)
        self.assertEqual(diff_rows[0]["new"], 6000.0)

    def test_lat_lng_are_editable_like_any_other_field(self):
        master_records = self._master_records([
            {"building": "40 New Bond Street", "provider": "Workplace Plus", "lat": None, "lng": None},
        ])
        edited_rows = {0: {"lat": 51.5142, "lng": -0.1494}}

        merged_rows, diff_rows, fields_changed = master_merge.build_manual_edit(master_records, edited_rows)

        self.assertEqual(fields_changed, 2)
        self.assertEqual(merged_rows[0].lat, 51.5142)
        self.assertEqual(merged_rows[0].lng, -0.1494)

    def test_unrelated_rows_pass_through_unchanged(self):
        master_records = self._master_records([
            {"building": "A", "provider": "P1", "size_sqft": 1000.0},
            {"building": "B", "provider": "P2", "size_sqft": 2000.0},
        ])
        edited_rows = {1: {"size_sqft": 2500.0}}

        merged_rows, diff_rows, fields_changed = master_merge.build_manual_edit(master_records, edited_rows)

        self.assertEqual(merged_rows[0].size_sqft, 1000.0)  # untouched
        self.assertEqual(merged_rows[1].size_sqft, 2500.0)
        self.assertEqual(fields_changed, 1)

    def test_ui_only_select_checkbox_column_is_ignored(self):
        # "Select" is bolted onto the grid for row-selection/export - never a
        # ListingRow field, so toggling it alone must never trigger a save.
        master_records = self._master_records([
            {"building": "A", "provider": "P1"},
        ])
        edited_rows = {0: {"Select": True}}

        merged_rows, diff_rows, fields_changed = master_merge.build_manual_edit(master_records, edited_rows)

        self.assertEqual(fields_changed, 0)
        self.assertEqual(diff_rows, [])
        self.assertEqual(merged_rows[0].building, "A")  # unchanged

    def test_select_toggle_bundled_with_a_real_edit_only_counts_the_real_edit(self):
        master_records = self._master_records([
            {"building": "A", "provider": "P1", "size_sqft": 1000.0},
        ])
        edited_rows = {0: {"Select": True, "size_sqft": 1500.0}}

        merged_rows, diff_rows, fields_changed = master_merge.build_manual_edit(master_records, edited_rows)

        self.assertEqual(fields_changed, 1)
        self.assertEqual(merged_rows[0].size_sqft, 1500.0)

    def test_multi_cell_batch_is_a_single_result_not_per_cell(self):
        # A multi-cell paste lands as several changed cells across rows in
        # one edited_rows dict - this must produce one combined result
        # (the caller then does exactly one write_master()/version for it),
        # not something the caller would need to split into several saves.
        master_records = self._master_records([
            {"building": "A", "provider": "P1", "size_sqft": 1000.0, "state_of_space": "Cat A"},
            {"building": "B", "provider": "P2", "size_sqft": 2000.0},
        ])
        edited_rows = {
            0: {"size_sqft": 1100.0, "state_of_space": "Fully Fitted"},
            1: {"size_sqft": 2200.0},
        }

        merged_rows, diff_rows, fields_changed = master_merge.build_manual_edit(master_records, edited_rows)

        self.assertEqual(fields_changed, 3)
        self.assertEqual(len(diff_rows), 3)
        self.assertEqual(merged_rows[0].size_sqft, 1100.0)
        self.assertEqual(merged_rows[0].state_of_space, "Fully Fitted")
        self.assertEqual(merged_rows[1].size_sqft, 2200.0)

    def test_string_row_position_keys_are_handled(self):
        # Some Streamlit versions have reported edited_rows keys as strings
        # rather than ints after JSON round-tripping - build_manual_edit
        # must not silently drop or crash on these.
        master_records = self._master_records([{"building": "A", "provider": "P1", "size_sqft": 1000.0}])
        edited_rows = {"0": {"size_sqft": 1200.0}}

        merged_rows, diff_rows, fields_changed = master_merge.build_manual_edit(master_records, edited_rows)

        self.assertEqual(fields_changed, 1)
        self.assertEqual(merged_rows[0].size_sqft, 1200.0)


class MentionsLetStatusTests(unittest.TestCase):
    """The core detection: does this free-text value's wording suggest the
    property is no longer available? Vocabulary confirmed present in this
    repo's real sample documents (tests/sample_docs) - see
    master_merge.LET_STATUS_KEYWORDS' docstring."""

    def test_users_own_starting_vocabulary(self):
        self.assertTrue(master_merge.mentions_let_status("Status: Let"))
        self.assertTrue(master_merge.mentions_let_status("This unit has now been Leased"))
        self.assertTrue(master_merge.mentions_let_status("No longer available"))
        self.assertTrue(master_merge.mentions_let_status("Withdrawn from the market"))

    def test_confirmed_present_in_real_sample_documents(self):
        # 40_New_Bond_Street_Brochure.pdf / Office_Space_by_The_Crown_Estate:
        # a floor's Availability listed as "Under Offer".
        self.assertTrue(master_merge.mentions_let_status("Under Offer"))
        # Breezblok.pdf's own brochure: "The centre is now 100% Occupied".
        self.assertTrue(master_merge.mentions_let_status("The centre is now 100% Occupied"))

    def test_case_insensitive(self):
        self.assertTrue(master_merge.mentions_let_status("UNDER OFFER"))
        self.assertTrue(master_merge.mentions_let_status("withdrawn"))

    def test_pre_let_and_similar_compounds_are_not_false_positives(self):
        # GPE.eml's real wording: "high pre-let demand" / "pre-let at
        # Elsley" describes a BUILDING's overall leasing momentum, not this
        # specific unit's own current availability.
        self.assertFalse(master_merge.mentions_let_status("Last remaining workspace after high pre-let demand."))
        self.assertFalse(master_merge.mentions_let_status("80% pre-let at Elsley"))
        self.assertFalse(master_merge.mentions_let_status("Available on a sub-let basis"))

    def test_ordinary_amenity_text_is_not_flagged(self):
        self.assertFalse(master_merge.mentions_let_status("Bike racks; showers; roof terrace"))

    def test_blank_is_not_flagged(self):
        self.assertFalse(master_merge.mentions_let_status(None))
        self.assertFalse(master_merge.mentions_let_status(""))


class BuildMergePlanLetStatusTests(unittest.TestCase):
    """The exact scenario this feature exists for: a re-upload's wording
    implies a matched property is no longer available."""

    def test_special_features_mentioning_let_flags_the_matched_row(self):
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "Test Provider", "floor_unit": "1st Floor",
            "postcode": "EC1A 1AA", "special_features": "Bike racks; showers",
        }])
        new_row = ListingRow(
            building="1 Example Street", provider="Test Provider", floor_unit="1st Floor",
            postcode="EC1A 1AA", special_features="Let",
        )

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed), 1)
        matched = plan.matched_changed[0]
        self.assertIn("special_features", matched.let_status_fields)
        # Still a real diff, exactly like risky_fields - the safeguard forces
        # manual review, it never makes the change disappear.
        self.assertIn("special_features", matched.diffs)

    def test_state_of_space_mentioning_withdrawn_flags_the_matched_row(self):
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "Test Provider",
            "state_of_space": "Fully Fitted",
        }])
        new_row = ListingRow(building="1 Example Street", provider="Test Provider", state_of_space="Withdrawn")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertIn("state_of_space", plan.matched_changed[0].let_status_fields)

    def test_normal_update_is_not_flagged(self):
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "Test Provider", "special_features": "Bike racks",
        }])
        new_row = ListingRow(building="1 Example Street", provider="Test Provider", special_features="Bike racks; showers")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(plan.matched_changed[0].let_status_fields, frozenset())

    def test_new_unmatched_property_is_never_flagged(self):
        # A brand-new listing has no "let status change" concept - only
        # MatchedRow carries let_status_fields at all, and this must land
        # in plan.unmatched, not plan.matched_changed.
        master_df = _master_df([{"building": "Somewhere Else", "provider": "Other Provider"}])
        new_row = ListingRow(building="1 Example Street", provider="Test Provider", special_features="Let")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed), 0)
        self.assertEqual(len(plan.unmatched), 1)
        self.assertIs(plan.unmatched[0].new_row, new_row)


class ApplyMergeRemovalTests(unittest.TestCase):
    """Delete-row support added for the "remove from master entirely"
    decision - confirmed via investigation that no such capability existed
    anywhere in the codebase before this."""

    def _records(self, rows):
        return [ListingRow(**r).model_dump() for r in rows]

    def test_removed_index_is_dropped_from_the_result(self):
        master_records = self._records([
            {"building": "A", "provider": "P1"},
            {"building": "B", "provider": "P2"},
            {"building": "C", "provider": "P3"},
        ])

        result = master_merge.apply_merge(master_records, {}, [], removed_indices=frozenset({1}))

        self.assertEqual([r.building for r in result], ["A", "C"])

    def test_other_rows_and_new_rows_are_unaffected(self):
        master_records = self._records([{"building": "A", "provider": "P1"}, {"building": "B", "provider": "P2"}])
        new_row = ListingRow(building="C", provider="P3")

        result = master_merge.apply_merge(
            master_records, {0: {"provider": "Updated"}}, [new_row], removed_indices=frozenset({1}),
        )

        self.assertEqual([r.building for r in result], ["A", "C"])
        self.assertEqual(result[0].provider, "Updated")

    def test_an_update_for_a_removed_index_is_moot_not_an_error(self):
        master_records = self._records([{"building": "A", "provider": "P1"}])

        result = master_merge.apply_merge(
            master_records, {0: {"provider": "Should never apply"}}, [], removed_indices=frozenset({0}),
        )

        self.assertEqual(result, [])

    def test_no_removed_indices_behaves_exactly_as_before(self):
        master_records = self._records([{"building": "A", "provider": "P1"}])
        result = master_merge.apply_merge(master_records, {}, [])
        self.assertEqual(len(result), 1)


class BuildApprovalSummaryRemovalTests(unittest.TestCase):
    def test_removed_labels_reflect_the_removed_property(self):
        master_df = _master_df([
            {"building": "A", "provider": "P1", "floor_unit": "1st Floor"},
            {"building": "B", "provider": "P2"},
        ])
        plan = master_merge.build_merge_plan([], master_df)

        diff_rows, new_labels, removed_labels = master_merge.build_approval_summary(
            plan, {}, [], removed_indices=frozenset({0}),
        )

        self.assertEqual(removed_labels, ["A — P1 — 1st Floor"])
        self.assertEqual(diff_rows, [])
        self.assertEqual(new_labels, [])

    def test_no_removals_gives_an_empty_list(self):
        master_df = _master_df([{"building": "A", "provider": "P1"}])
        plan = master_merge.build_merge_plan([], master_df)

        _, _, removed_labels = master_merge.build_approval_summary(plan, {}, [])

        self.assertEqual(removed_labels, [])


if __name__ == "__main__":
    unittest.main()
