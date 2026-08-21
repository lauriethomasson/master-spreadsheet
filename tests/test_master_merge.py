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


class DedupKeyTests(unittest.TestCase):
    def test_same_postcode_uses_primary_key(self):
        a = {"building": "77 Gracechurch Street", "provider": "Workplace Plus",
             "floor_unit": "6th Floor", "postcode": "EC3V 0AS"}
        b = dict(a)
        self.assertEqual(master_merge._dedup_key(a), master_merge._dedup_key(b))
        self.assertEqual(master_merge._dedup_key(a), master_merge._primary_key(a))

    def test_different_postcode_never_grouped(self):
        # Same building/provider/floor_unit, genuinely different postcode -
        # treated as real signal against merging, not missing data.
        a = {"building": "1 Example Street", "provider": "Test Provider",
             "floor_unit": "1st Floor", "postcode": "EC1A 1AA"}
        b = {"building": "1 Example Street", "provider": "Test Provider",
             "floor_unit": "1st Floor", "postcode": "SW1A 1AA"}
        self.assertNotEqual(master_merge._dedup_key(a), master_merge._dedup_key(b))

    def test_both_blank_postcode_falls_back_to_three_field_key(self):
        a = {"building": "1 Example Street", "provider": "Test Provider", "floor_unit": "1st Floor"}
        b = dict(a)
        self.assertEqual(master_merge._dedup_key(a), master_merge._dedup_key(b))
        self.assertEqual(master_merge._dedup_key(a), master_merge._fallback_key(a))


class SuggestSimilarTests(unittest.TestCase):
    def _master_records(self, buildings):
        return [{"building": b, "provider": "X", "floor_unit": "1st", "postcode": "EC1A 1AA"} for b in buildings]

    def test_numbered_address_never_suggested_anything(self):
        # Real reported false positives - none share anything meaningful
        # with "77 Gracechurch Street", all three cleared the old 0.6
        # cutoff purely from generic "digit + word + Street" structure.
        master_records = self._master_records(
            ["27 Greville Street", "55 Grosvenor Street", "141 Fenchurch Street (Monument)"]
        )
        new_dict = {"building": "77 Gracechurch Street", "provider": "X"}
        self.assertEqual(master_merge._suggest_similar(new_dict, master_records), [])

    def test_genuine_name_only_typo_still_suggested(self):
        # Same real pair FuzzyBuildingMatchTests validates for the actual
        # matching tier (0.938) - the raised threshold must not lose this.
        master_records = self._master_records(["Thirty Lighterman"])
        new_dict = {"building": "Thirty Lightman", "provider": "X"}
        self.assertEqual(len(master_merge._suggest_similar(new_dict, master_records)), 1)

    def test_unrelated_name_only_pair_not_suggested(self):
        # Real confirmed false-positive risk for the matching tier (0.762) -
        # below the 0.85 threshold reused here.
        master_records = self._master_records(["Orion House"])
        new_dict = {"building": "York House", "provider": "X"}
        self.assertEqual(master_merge._suggest_similar(new_dict, master_records), [])

    def test_blank_building_suggests_nothing(self):
        self.assertEqual(master_merge._suggest_similar({"building": None}, self._master_records(["Kent House"])), [])

    def test_different_provider_similar_building_is_never_suggested(self):
        # The real confirmed case: incoming "Clerkenwell Road" (MetSpace)
        # must never suggest an existing "80 Clerkenwell Road" (UNION) as a
        # possible near-miss - provider is part of listing identity, and a
        # different provider is never the same listing, even as a hint.
        master_records = [{"building": "80 Clerkenwell Road", "provider": "UNION", "floor_unit": "2nd"}]
        new_dict = {"building": "Clerkenwell Road", "provider": "MetSpace"}
        self.assertEqual(master_merge._suggest_similar(new_dict, master_records), [])

    def test_same_provider_similar_building_is_still_suggested(self):
        master_records = [{"building": "Thirty Lighterman", "provider": "MetSpace", "floor_unit": "2nd"}]
        new_dict = {"building": "Thirty Lightman", "provider": "MetSpace"}
        self.assertEqual(master_merge._suggest_similar(new_dict, master_records), master_records)

    def test_provider_filtering_applies_before_the_fuzzy_cutoff_not_after(self):
        # A different-provider candidate must be excluded from the
        # candidate pool entirely, not merely scored and then discarded -
        # confirmed here with a genuinely close typo pair that would
        # otherwise clear BUILDING_FUZZY_MATCH_THRESHOLD easily.
        master_records = [{"building": "Thirty Lighterman", "provider": "UNION", "floor_unit": "1st"}]
        new_dict = {"building": "Thirty Lightman", "provider": "MetSpace"}
        self.assertEqual(master_merge._suggest_similar(new_dict, master_records), [])

    def test_same_name_same_provider_but_conflicting_real_address_is_never_suggested(self):
        # Real confirmed false positive: master's "City Tower"/GPE record
        # is a real, unrelated residential building at 3 Limeharbour,
        # Canary Wharf (E14) - a completely different real "City Tower" at
        # 40 Basinghall Street, EC2V (GPE's own actual managed office
        # building) must never be suggested against it, despite the
        # identical building-name string and shared provider.
        master_records = [{
            "building": "City Tower", "provider": "GPE", "floor_unit": "2nd",
            "address_1": "3 Limeharbour", "postcode": "E14 9SH",
        }]
        new_dict = {
            "building": "City Tower", "provider": "GPE",
            "address_1": "40 Basinghall Street", "postcode": "EC2V 5DE",
        }
        self.assertEqual(master_merge._suggest_similar(new_dict, master_records), [])

    def test_same_name_same_provider_matching_addresses_still_suggested(self):
        # The opposite of the conflicting-address case above - a genuine
        # near-miss where both sides agree on location (same postcode
        # district) must still be suggested, same as before this fix.
        master_records = [{
            "building": "City Towr", "provider": "GPE", "floor_unit": "2nd",
            "address_1": "40 Basinghall Street", "postcode": "EC2V 5DE",
        }]
        new_dict = {
            "building": "City Tower", "provider": "GPE",
            "address_1": "40 Basinghall Street", "postcode": "EC2V 5DE",
        }
        self.assertEqual(master_merge._suggest_similar(new_dict, master_records), master_records)

    def test_same_name_same_provider_blank_addresses_on_both_sides_still_suggested(self):
        # No address on either side at all - nothing to compare, so this
        # new guard must never block a genuine near-miss just because
        # neither side happens to state a location (see
        # test_genuine_name_only_typo_still_suggested for the same
        # principle with an actual typo pair).
        master_records = [{"building": "City Tower", "provider": "GPE", "floor_unit": "2nd"}]
        new_dict = {"building": "City Towr", "provider": "GPE"}
        self.assertEqual(len(master_merge._suggest_similar(new_dict, master_records)), 1)


class ValuesEqualTests(unittest.TestCase):
    """
    _values_equal's own field-specific tolerance - real Kitt's
    Availability case: rent_pcm/rent_psf routinely come from two
    different sheets computed to different precision (a live division
    vs the same figure pre-rounded), both displaying as the identical
    "£243" (see listing_summary_lines' own f"{value:,.0f}" rounding) but
    failing the default 1e-6 tolerance as if they genuinely disagreed.
    """

    def test_rent_psf_rounding_only_difference_is_equal_when_field_name_given(self):
        self.assertTrue(master_merge._values_equal(243.108108, 243.0, "rent_psf"))

    def test_rent_pcm_rounding_only_difference_is_equal_when_field_name_given(self):
        self.assertTrue(master_merge._values_equal(18700.4, 18700.0, "rent_pcm"))

    def test_rent_psf_genuinely_different_pounds_still_unequal(self):
        # The tolerance is whole-POUND, not unlimited.
        self.assertFalse(master_merge._values_equal(243.4, 244.0, "rent_psf"))

    def test_rent_psf_without_field_name_keeps_the_strict_default_tolerance(self):
        # Backward compatibility: an existing caller that doesn't pass
        # field_name at all must see the exact prior behavior, even for
        # a field that WOULD get the widened tolerance if named.
        self.assertFalse(master_merge._values_equal(243.108108, 243.0))

    def test_size_sqft_is_not_widened_even_when_named(self):
        # Deliberately scoped to rent_pcm/rent_psf only.
        self.assertFalse(master_merge._values_equal(1000.3, 1000.0, "size_sqft"))

    def test_desks_max_is_not_widened_even_when_named(self):
        self.assertFalse(master_merge._values_equal(12.4, 12.0, "desks_max"))

    def test_text_fields_are_unaffected_by_field_name(self):
        self.assertTrue(master_merge._values_equal("METSPACE", "Metspace", "provider"))
        self.assertFalse(master_merge._values_equal("Fitted", "CAT A", "state_of_space"))

    def test_exact_match_still_equal_with_or_without_field_name(self):
        self.assertTrue(master_merge._values_equal(243.0, 243.0))
        self.assertTrue(master_merge._values_equal(243.0, 243.0, "rent_psf"))


class MergeFieldChoiceTests(unittest.TestCase):
    def test_all_equal_needs_no_choice(self):
        self.assertEqual(master_merge.merge_field_choice(["Fully Managed", "Fully Managed"]), (False, "Fully Managed"))

    def test_tolerant_equal_needs_no_choice(self):
        # Case-only difference - same principle as _values_equal elsewhere.
        self.assertEqual(master_merge.merge_field_choice(["MetSpace", "METSPACE"]), (False, "MetSpace"))

    def test_all_blank_needs_no_choice(self):
        self.assertEqual(master_merge.merge_field_choice([None, ""]), (False, None))

    def test_one_blank_one_filled_still_needs_a_choice(self):
        # A blank vs. a real value IS a disagreement worth a reviewer's eyes
        # (see default_merge_choice_index for the smart default) - the point
        # is a reviewer can deliberately choose blank if the one value
        # present is wrong for the merged property, not that this gets
        # silently decided for them just because only one side has data.
        needs_choice, resolved = master_merge.merge_field_choice([None, "Fully Managed"])
        self.assertTrue(needs_choice)
        self.assertIsNone(resolved)

    def test_genuinely_different_values_need_a_choice(self):
        needs_choice, resolved = master_merge.merge_field_choice(["Fully Managed", "Fully Fitted"])
        self.assertTrue(needs_choice)
        self.assertIsNone(resolved)

    def test_field_name_gives_rent_psf_the_widened_rounding_tolerance(self):
        needs_choice, resolved = master_merge.merge_field_choice([243.108108, 243.0], "rent_psf")
        self.assertFalse(needs_choice)
        self.assertEqual(resolved, 243.108108)

    def test_field_name_not_passed_keeps_the_strict_tolerance(self):
        # Backward compatibility: no field_name means the exact prior
        # behavior, even for a field that would otherwise get the
        # widened tolerance.
        needs_choice, resolved = master_merge.merge_field_choice([243.108108, 243.0])
        self.assertTrue(needs_choice)
        self.assertIsNone(resolved)


class DefaultMergeChoiceIndexTests(unittest.TestCase):
    def test_single_non_blank_is_preselected(self):
        self.assertEqual(master_merge.default_merge_choice_index([None, "value"]), 1)

    def test_all_non_blank_defaults_to_first(self):
        self.assertEqual(master_merge.default_merge_choice_index(["a", "b"]), 0)

    def test_all_blank_defaults_to_first(self):
        self.assertEqual(master_merge.default_merge_choice_index([None, None]), 0)


class MatchedCollisionFieldChoiceTests(unittest.TestCase):
    """Unlike merge_field_choice (used for unmatched_collisions' brand-new-
    property merge, where a blank source IS a meaningful choice to show -
    see that class's own test_one_blank_one_filled_still_needs_a_choice),
    a MATCHED collision always has master's own current value as a
    fallback, so a colliding row that's silently blank on a field has no
    opinion on it at all - it must never force a choice just because a
    sibling row does propose something."""

    def test_all_agree_no_choice_needed(self):
        self.assertEqual(
            master_merge.matched_collision_field_choice(["Fitted", "Fitted", "Fitted"]), (False, "Fitted"),
        )

    def test_tolerant_equal_values_agree(self):
        self.assertEqual(master_merge.matched_collision_field_choice(["METSPACE", "Metspace"]), (False, "METSPACE"))

    def test_one_non_blank_among_blanks_is_not_a_disagreement(self):
        # The real point of departure from merge_field_choice: a colliding
        # row that's blank on this field (silent - no opinion, exactly what
        # it means for a field to be absent from THAT row's own diffs) must
        # NOT force a manual choice just because a sibling row DOES propose
        # a value for it.
        needs_choice, value = master_merge.matched_collision_field_choice([None, "22 desks", ""])
        self.assertFalse(needs_choice)
        self.assertEqual(value, "22 desks")

    def test_genuine_disagreement_needs_choice(self):
        needs_choice, value = master_merge.matched_collision_field_choice(["Fitted", "CAT A"])
        self.assertTrue(needs_choice)
        self.assertIsNone(value)

    def test_all_blank_resolves_to_none_no_choice(self):
        needs_choice, value = master_merge.matched_collision_field_choice([None, ""])
        self.assertFalse(needs_choice)
        self.assertIsNone(value)

    def test_field_name_not_passed_keeps_prior_behavior_for_text_fields(self):
        # Backward compatibility: an existing caller that doesn't pass
        # field_name gets the exact prior behavior (always a real choice
        # on any textual disagreement, even a reworded-but-compatible one)
        # - the new RISKY_TEXT_FIELDS tolerance is opt-in per call site.
        needs_choice, value = master_merge.matched_collision_field_choice(
            ["Fully fitted, meeting rooms, kitchen", "Meeting rooms and a kitchen"],
        )
        self.assertTrue(needs_choice)
        self.assertIsNone(value)

    def test_reworded_but_compatible_special_features_auto_resolves_to_richest(self):
        # Real scenario this exists for: the SAME real brochure enriched
        # independently across separate uploads/runs, where Gemini's own
        # extraction non-determinism can reword the same fact slightly -
        # never a genuine conflict, so this must not force a click.
        needs_choice, value = master_merge.matched_collision_field_choice(
            ["Fully fitted, meeting rooms, kitchen", "Meeting rooms and a kitchen"], "special_features",
        )
        self.assertFalse(needs_choice)
        self.assertEqual(value, "Fully fitted, meeting rooms, kitchen")

    def test_genuinely_conflicting_special_features_still_needs_a_choice(self):
        # Real content disagreement (not just rewording) - is_detail_loss
        # correctly sees an item on each side missing from the other.
        needs_choice, value = master_merge.matched_collision_field_choice(
            ["Meeting rooms; kitchen", "Gym; roof terrace"], "special_features",
        )
        self.assertTrue(needs_choice)
        self.assertIsNone(value)

    def test_tolerance_is_scoped_to_risky_text_fields_only(self):
        # The same reworded-looking pair on a NON-risky field (e.g.
        # provider) must still be treated as a genuine disagreement - the
        # tolerance is specifically about detail-loss on free-text lists,
        # never a general "these look similar" fuzzy match.
        needs_choice, value = master_merge.matched_collision_field_choice(["UNION", "Union Ltd"], "provider")
        self.assertTrue(needs_choice)
        self.assertIsNone(value)

    def test_rent_psf_rounding_only_difference_needs_no_choice(self):
        # Real Kitt's Availability shape (see ValuesEqualTests) - one
        # sheet's live division vs the other's pre-rounded figure, both
        # displaying as the same "£243".
        needs_choice, value = master_merge.matched_collision_field_choice([243.108108, 243.0], "rent_psf")
        self.assertFalse(needs_choice)
        self.assertEqual(value, 243.108108)

    def test_rent_pcm_genuinely_different_pounds_still_needs_a_choice(self):
        needs_choice, value = master_merge.matched_collision_field_choice([18700.0, 18750.0], "rent_pcm")
        self.assertTrue(needs_choice)
        self.assertIsNone(value)

    def test_rent_psf_rounding_tolerance_requires_field_name(self):
        # Backward compatibility: a caller that doesn't pass field_name
        # keeps the exact prior strict comparison for rent_psf too.
        needs_choice, value = master_merge.matched_collision_field_choice([243.108108, 243.0])
        self.assertTrue(needs_choice)
        self.assertIsNone(value)


class GroupHasGenuineConflictTests(unittest.TestCase):
    def test_identical_rows_have_no_conflict(self):
        dicts = [
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "size_sqft": 1000.0},
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "size_sqft": 1000.0},
        ]
        self.assertFalse(master_merge._group_has_genuine_conflict(dicts))

    def test_complementary_blank_and_filled_special_features_has_no_conflict(self):
        dicts = [
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "special_features": None},
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "special_features": "Roof terrace"},
        ]
        self.assertFalse(master_merge._group_has_genuine_conflict(dicts))

    def test_conflicting_size_is_a_genuine_conflict(self):
        dicts = [
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "size_sqft": 1000.0},
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "size_sqft": 5000.0},
        ]
        self.assertTrue(master_merge._group_has_genuine_conflict(dicts))

    def test_conflicting_rent_is_a_genuine_conflict(self):
        dicts = [
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "rent_pcm": 10000.0},
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "rent_pcm": 15000.0},
        ]
        self.assertTrue(master_merge._group_has_genuine_conflict(dicts))

    def test_conflicting_state_of_space_is_a_genuine_conflict(self):
        dicts = [
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "state_of_space": "Cat A"},
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "state_of_space": "Shell & Core"},
        ]
        self.assertTrue(master_merge._group_has_genuine_conflict(dicts))

    def test_reworded_compatible_special_features_across_three_copies_has_no_conflict(self):
        # The real "same brochure enriched 3 times" pattern - three
        # slightly different phrasings of the same underlying fact.
        dicts = [
            {"building": "107 Cannon Street", "provider": "UNION", "floor_unit": "4th", "special_features": "Fully fitted, meeting rooms, kitchen"},
            {"building": "107 Cannon Street", "provider": "UNION", "floor_unit": "4th", "special_features": "Meeting rooms and a kitchen"},
            {"building": "107 Cannon Street", "provider": "UNION", "floor_unit": "4th", "special_features": None},
        ]
        self.assertFalse(master_merge._group_has_genuine_conflict(dicts))


class GroupHasGenuineConflictBrochureLinkOverrideTests(unittest.TestCase):
    """
    Direct unit tests for _group_has_genuine_conflict's postcode/address_1
    exception (see _brochure_link_identity_override) - the conflict-stage
    half of the real Nexus Place fix (see BrochureLinkOverridesPostcode
    ConflictTests for the grouping-stage half and the full real-world
    story). These call _group_has_genuine_conflict directly, same style
    as GroupHasGenuineConflictTests above, rather than going through the
    whole build-a-plan/consolidate pipeline.
    """

    def test_postcode_conflict_with_shared_brochure_is_not_genuine(self):
        dicts = [
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "postcode": "EC4M 4AB", "brochure_link": "https://example.com/b.pdf"},
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "postcode": "EC1M 3HA", "brochure_link": "https://example.com/b.pdf"},
        ]
        self.assertFalse(master_merge._group_has_genuine_conflict(dicts))

    def test_address_1_conflict_with_shared_brochure_is_not_genuine(self):
        # address_1 is backfilled from the exact same geocode.py API
        # result as postcode (see _group_has_genuine_conflict's own
        # docstring) - the real Nexus Place pair disagrees on both for
        # that reason, so the override must cover both, not postcode alone.
        dicts = [
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "address_1": "25 Farringdon Street", "brochure_link": "https://example.com/b.pdf"},
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "address_1": "25-27 Farringdon Road", "brochure_link": "https://example.com/b.pdf"},
        ]
        self.assertFalse(master_merge._group_has_genuine_conflict(dicts))

    def test_postcode_and_address_1_both_conflicting_with_shared_brochure_is_not_genuine(self):
        # The real Nexus Place shape: both geocoded fields disagree at
        # once, still excused by the one shared brochure_link.
        dicts = [
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "postcode": "EC4M 4AB", "address_1": "25 Farringdon Street",
             "brochure_link": "https://example.com/b.pdf"},
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "postcode": "EC1M 3HA", "address_1": "25-27 Farringdon Road",
             "brochure_link": "https://example.com/b.pdf"},
        ]
        self.assertFalse(master_merge._group_has_genuine_conflict(dicts))

    def test_postcode_conflict_with_one_blank_brochure_and_agreeing_nonblank_is_not_genuine(self):
        dicts = [
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "postcode": "EC4M 4AB", "brochure_link": "https://example.com/b.pdf"},
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "postcode": "EC1M 3HA", "brochure_link": None},
        ]
        self.assertFalse(master_merge._group_has_genuine_conflict(dicts))

    def test_postcode_conflict_with_different_brochures_is_still_genuine(self):
        dicts = [
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "postcode": "EC4M 4AB", "brochure_link": "https://example.com/a.pdf"},
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "postcode": "EC1M 3HA", "brochure_link": "https://example.com/b.pdf"},
        ]
        self.assertTrue(master_merge._group_has_genuine_conflict(dicts))

    def test_postcode_conflict_with_no_brochure_at_all_is_still_genuine(self):
        # The ordinary, unrelated-properties case - no brochure evidence
        # of any kind, so a real postcode disagreement still requires
        # manual review exactly as it always has.
        dicts = [
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th", "postcode": "EC4M 4AB"},
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th", "postcode": "EC1M 3HA"},
        ]
        self.assertTrue(master_merge._group_has_genuine_conflict(dicts))

    def test_building_conflict_with_shared_brochure_is_still_genuine(self):
        # The override is scoped to postcode/address_1 only - a
        # genuinely different building (clearly different property
        # identity) must still block auto-merge even with a shared
        # brochure_link (e.g. a provider data-entry error linking the
        # wrong document to two unrelated rows).
        dicts = [
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "brochure_link": "https://example.com/b.pdf"},
            {"building": "Totally Different Building", "provider": "UNION", "floor_unit": "5th",
             "brochure_link": "https://example.com/b.pdf"},
        ]
        self.assertTrue(master_merge._group_has_genuine_conflict(dicts))

    def test_size_conflict_with_shared_brochure_and_postcode_conflict_is_still_genuine(self):
        # A brochure_link match excuses ONLY the postcode/address_1
        # disagreement it exists for - any other genuine field conflict in
        # the same group (here size_sqft) must still force manual review.
        dicts = [
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "postcode": "EC4M 4AB", "size_sqft": 1000.0, "brochure_link": "https://example.com/b.pdf"},
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "postcode": "EC1M 3HA", "size_sqft": 5000.0, "brochure_link": "https://example.com/b.pdf"},
        ]
        self.assertTrue(master_merge._group_has_genuine_conflict(dicts))


class GenuinelyDifferingFieldsTests(unittest.TestCase):
    """
    genuinely_differing_fields - the field list _group_has_genuine_
    conflict is now defined in terms of (see that class's own tests above,
    unaffected by this refactor), and that pages/2_Review_and_Master.py's
    duplicate-listing review card uses directly to decide what to show a
    reviewer (see ListingSummaryLinesTests below) - confirms the two never
    disagree, which is the entire point of sharing this one function.
    """

    def test_identical_rows_have_no_differing_fields(self):
        dicts = [
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "size_sqft": 1000.0},
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "size_sqft": 1000.0},
        ]
        self.assertEqual(master_merge.genuinely_differing_fields(dicts), [])

    def test_rent_psf_only_conflict_returns_only_that_field(self):
        # The real Kitt's "28 Bruton Street" shape: every other field
        # agrees, only rent_psf genuinely disagrees (£310 vs £296) - the
        # exact real gap the fixed five-field review card used to hide
        # entirely (rent_psf was never one of the five).
        dicts = [
            {"building": "28 Bruton Street", "provider": "Kitt's", "floor_unit": "1st",
             "size_sqft": 759.0, "rent_pcm": 18700.0, "rent_psf": 310.0, "state_of_space": "Partially Fitted"},
            {"building": "28 Bruton Street", "provider": "Kitt's", "floor_unit": "1st",
             "size_sqft": 759.0, "rent_pcm": 18700.0, "rent_psf": 296.0, "state_of_space": "Partially Fitted"},
        ]
        self.assertEqual(master_merge.genuinely_differing_fields(dicts), ["rent_psf"])

    def test_rounding_only_rent_psf_difference_is_not_a_differing_field(self):
        # Real Kitt's Availability shape: one sheet states rent_psf as a
        # live rent_pcm/size_sqft division (243.108108...), the other has
        # the SAME figure pre-rounded to 243.0 - both display as the
        # identical "£243", so this must NOT be flagged, unlike the
        # genuinely different £310-vs-£296 case above.
        dicts = [
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "rent_psf": 243.108108},
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "rent_psf": 243.0},
        ]
        self.assertEqual(master_merge.genuinely_differing_fields(dicts), [])

    def test_rounding_only_rent_pcm_difference_is_not_a_differing_field(self):
        dicts = [
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "rent_pcm": 18700.4},
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "rent_pcm": 18700.0},
        ]
        self.assertEqual(master_merge.genuinely_differing_fields(dicts), [])

    def test_whole_pound_rent_psf_difference_still_counts_as_differing(self):
        # The widened tolerance is whole-POUND, not unlimited - a real
        # difference that rounds to two DIFFERENT pounds must still show.
        dicts = [
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "rent_psf": 243.4},
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "rent_psf": 244.0},
        ]
        self.assertEqual(master_merge.genuinely_differing_fields(dicts), ["rent_psf"])

    def test_size_sqft_rounding_is_not_widened(self):
        # Deliberately scoped to rent_pcm/rent_psf only - size_sqft has no
        # confirmed rounding-precision mismatch, so its existing strict
        # (1e-6) tolerance must be completely unaffected.
        dicts = [
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "size_sqft": 1000.3},
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "size_sqft": 1000.0},
        ]
        self.assertEqual(master_merge.genuinely_differing_fields(dicts), ["size_sqft"])

    def test_floor_and_size_conflict_returns_exactly_those_two_fields(self):
        dicts = [
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "size_sqft": 1000.0},
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "2nd", "size_sqft": 5000.0},
        ]
        self.assertEqual(master_merge.genuinely_differing_fields(dicts), ["floor_unit", "size_sqft"])

    def test_reworded_compatible_special_features_is_not_a_differing_field(self):
        # Mirrors GroupHasGenuineConflictTests' own equivalent case - the
        # RISKY_TEXT_FIELDS reworded-but-compatible tolerance must apply
        # here exactly as it does for _group_has_genuine_conflict, since
        # this IS that function's own per-field logic now.
        dicts = [
            {"building": "107 Cannon Street", "provider": "UNION", "floor_unit": "4th",
             "special_features": "Fully fitted, meeting rooms, kitchen"},
            {"building": "107 Cannon Street", "provider": "UNION", "floor_unit": "4th",
             "special_features": "Meeting rooms and a kitchen"},
        ]
        self.assertEqual(master_merge.genuinely_differing_fields(dicts), [])

    def test_reworded_compatible_special_features_alongside_a_genuine_rent_conflict(self):
        # Selectivity: the tolerant field is correctly excluded while a
        # genuinely different field on the SAME pair still shows up.
        dicts = [
            {"building": "107 Cannon Street", "provider": "UNION", "floor_unit": "4th", "rent_pcm": 10000.0,
             "special_features": "Fully fitted, meeting rooms, kitchen"},
            {"building": "107 Cannon Street", "provider": "UNION", "floor_unit": "4th", "rent_pcm": 15000.0,
             "special_features": "Meeting rooms and a kitchen"},
        ]
        self.assertEqual(master_merge.genuinely_differing_fields(dicts), ["rent_pcm"])

    def test_postcode_conflict_excused_by_shared_brochure_is_not_a_differing_field(self):
        # Same override GroupHasGenuineConflictBrochureLinkOverrideTests
        # covers for _group_has_genuine_conflict - it's the same check now.
        dicts = [
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "postcode": "EC4M 4AB", "brochure_link": "https://example.com/b.pdf"},
            {"building": "Nexus Place", "provider": "UNION", "floor_unit": "5th",
             "postcode": "EC1M 3HA", "brochure_link": "https://example.com/b.pdf"},
        ]
        self.assertEqual(master_merge.genuinely_differing_fields(dicts), [])

    def test_hidden_fields_are_still_reported_here_filtering_is_the_callers_job(self):
        # brochure_link_broken/brochure_link_is_floorplan/lat/lng are hidden
        # from the REVIEW CARD (see DUPLICATE_CARD_HIDDEN_FIELDS), but this
        # function itself must still report a genuine disagreement in one -
        # _group_has_genuine_conflict needs to keep acting on it regardless
        # of whether a human ever sees it named.
        dicts = [
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "lat": 51.5},
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st", "lat": 51.6},
        ]
        self.assertEqual(master_merge.genuinely_differing_fields(dicts), ["lat"])
        self.assertIn("lat", master_merge.DUPLICATE_CARD_HIDDEN_FIELDS)


class ListingSummaryLinesTests(unittest.TestCase):
    """listing_summary_lines(row_dict, fields) - formats exactly the
    requested fields, reusing this module's own pre-existing per-field
    wording where one exists (see the function's own docstring)."""

    def test_only_the_requested_field_is_shown_even_though_others_have_values(self):
        row = {
            "floor_unit": "1st", "size_sqft": 759.0, "rent_pcm": 18700.0,
            "rent_psf": 310.0, "state_of_space": "Partially Fitted",
        }
        self.assertEqual(master_merge.listing_summary_lines(row, ["rent_psf"]), ["Rent per sq ft: £310"])

    def test_floor_and_size_are_shown_using_the_pre_existing_wording(self):
        row = {"floor_unit": "1st", "size_sqft": 759.0, "rent_pcm": 18700.0, "state_of_space": "Partially Fitted"}
        lines = master_merge.listing_summary_lines(row, ["floor_unit", "size_sqft"])
        self.assertEqual(lines, ["Floor/unit: 1st", "Size: 759 sq ft"])

    def test_a_requested_field_blank_on_this_row_is_skipped(self):
        row = {"floor_unit": "1st", "rent_psf": None}
        self.assertEqual(master_merge.listing_summary_lines(row, ["floor_unit", "rent_psf"]), ["Floor/unit: 1st"])

    def test_desks_shown_as_a_range_when_either_desks_field_is_requested(self):
        row = {"desks_min": 5, "desks_max": 10}
        self.assertEqual(master_merge.listing_summary_lines(row, ["desks_max"]), ["Desks: 5–10"])
        self.assertEqual(master_merge.listing_summary_lines(row, ["desks_min"]), ["Desks: 5–10"])

    def test_desks_shown_as_a_single_value_when_only_one_side_is_non_blank(self):
        row = {"desks_min": None, "desks_max": 12}
        self.assertEqual(master_merge.listing_summary_lines(row, ["desks_max"]), ["Desks: 12"])

    def test_an_unmentioned_field_falls_back_to_a_plain_title_case_label(self):
        row = {"submarket": "Mayfair"}
        self.assertEqual(master_merge.listing_summary_lines(row, ["submarket"]), ["Submarket: Mayfair"])

    def test_zero_requested_fields_present_on_this_row_returns_an_empty_list_not_a_crash(self):
        # The page-level safeguard (pages/2_Review_and_Master.py) falls
        # back to ["floor_unit"] when nothing genuinely differs, and shows
        # a clear "these look identical" message when even THAT'S blank -
        # this is the case that triggers it: must return [], never raise.
        row = {"floor_unit": None}
        self.assertEqual(master_merge.listing_summary_lines(row, ["floor_unit"]), [])


class MergeUnmatchedGroupTests(unittest.TestCase):
    def _group(self, rows):
        return [master_merge.UnmatchedRow(r) for r in rows]

    def test_three_identical_copies_merge_into_one_row_with_no_data_loss(self):
        rows = [
            ListingRow(
                building="107 Cannon Street", provider="UNION", floor_unit="4th", size_sqft=4500.0,
                source_file=f"upload_{i}.xlsx",
            )
            for i in range(3)
        ]
        merged = master_merge._merge_unmatched_group(self._group(rows))
        self.assertEqual(merged.building, "107 Cannon Street")
        self.assertEqual(merged.floor_unit, "4th")
        self.assertEqual(merged.size_sqft, 4500.0)
        self.assertIn("upload_0.xlsx", merged.source_file)
        self.assertIn("upload_1.xlsx", merged.source_file)
        self.assertIn("upload_2.xlsx", merged.source_file)

    def test_complementary_information_is_combined_not_lost(self):
        rows = [
            ListingRow(
                building="107 Cannon Street", provider="UNION", floor_unit="4th", size_sqft=4500.0,
                special_features=None, brochure_link="https://example.com/b.pdf", source_file="a.xlsx",
            ),
            ListingRow(
                building="107 Cannon Street", provider="UNION", floor_unit="4th", size_sqft=4500.0,
                special_features="Fully fitted, meeting rooms", brochure_link="https://example.com/b.pdf",
                source_file="b.xlsx",
            ),
        ]
        merged = master_merge._merge_unmatched_group(self._group(rows))
        self.assertEqual(merged.special_features, "Fully fitted, meeting rooms")
        self.assertEqual(merged.size_sqft, 4500.0)
        self.assertEqual(merged.brochure_link, "https://example.com/b.pdf")

    def test_gets_a_fresh_property_id_distinct_from_any_source_row(self):
        rows = [
            ListingRow(building="A", provider="UNION", floor_unit="1st", property_id="orig-1"),
            ListingRow(building="A", provider="UNION", floor_unit="1st", property_id="orig-2"),
        ]
        merged = master_merge._merge_unmatched_group(self._group(rows))
        self.assertNotIn(merged.property_id, ("orig-1", "orig-2"))


class ConsolidateUnmatchedDuplicatesTests(unittest.TestCase):
    def _plan_with_unmatched(self, rows, master_records=None):
        unmatched = [master_merge.UnmatchedRow(r) for r in rows]
        groups = master_merge._group_unmatched_duplicates(unmatched)
        return master_merge.MergePlan(master_records or [], [], [], unmatched, [], groups)

    def test_three_identical_copies_become_one_unmatched_row_no_review_needed(self):
        rows = [
            ListingRow(building="107 Cannon Street", provider="UNION", floor_unit="4th", size_sqft=4500.0)
            for _ in range(3)
        ]
        plan = self._plan_with_unmatched(rows)
        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched), 1)
        self.assertEqual(consolidated.unmatched_collisions, [])

    def test_six_identical_copies_become_one_unmatched_row(self):
        rows = [
            ListingRow(building="4 Moorgate", provider="UNION", floor_unit="5th", size_sqft=2000.0)
            for _ in range(6)
        ]
        plan = self._plan_with_unmatched(rows)
        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched), 1)
        self.assertEqual(consolidated.unmatched_collisions, [])

    def test_conflicting_group_stays_in_unmatched_collisions_and_in_unmatched(self):
        rows = [
            ListingRow(building="A", provider="UNION", floor_unit="1st", size_sqft=1000.0),
            ListingRow(building="A", provider="UNION", floor_unit="1st", size_sqft=5000.0),
        ]
        plan = self._plan_with_unmatched(rows)
        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched_collisions), 1)
        self.assertEqual(len(consolidated.unmatched_collisions[0]), 2)
        # Original members remain present in `unmatched` too - the existing
        # pages-layer id() tracking excludes them from near_miss/plain_new,
        # not a removal from this list (see consolidate_unmatched_
        # duplicates' own docstring).
        self.assertEqual(len(consolidated.unmatched), 2)

    def test_same_floor_different_units_never_merged(self):
        rows = [
            ListingRow(building="A Building", provider="UNION", floor_unit="2nd Unit 1"),
            ListingRow(building="A Building", provider="UNION", floor_unit="2nd Unit 2"),
        ]
        plan = self._plan_with_unmatched(rows)
        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        # Never grouped at all - different floor_unit keys - so both stay
        # as independent, standalone unmatched rows.
        self.assertEqual(consolidated.unmatched_collisions, [])
        self.assertEqual(len(consolidated.unmatched), 2)

    def test_north_south_never_merged(self):
        rows = [
            ListingRow(building="A Building", provider="UNION", floor_unit="5th (North)"),
            ListingRow(building="A Building", provider="UNION", floor_unit="5th (South)"),
        ]
        plan = self._plan_with_unmatched(rows)
        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(consolidated.unmatched_collisions, [])
        self.assertEqual(len(consolidated.unmatched), 2)

    def test_front_rear_entire_never_merged(self):
        rows = [
            ListingRow(building="A Building", provider="UNION", floor_unit="3rd (Front)"),
            ListingRow(building="A Building", provider="UNION", floor_unit="3rd (Rear)"),
            ListingRow(building="A Building", provider="UNION", floor_unit="3rd (Entire)"),
        ]
        plan = self._plan_with_unmatched(rows)
        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(consolidated.unmatched_collisions, [])
        self.assertEqual(len(consolidated.unmatched), 3)

    def test_different_floors_never_merged(self):
        rows = [
            ListingRow(building="A Building", provider="UNION", floor_unit="4th"),
            ListingRow(building="A Building", provider="UNION", floor_unit="5th"),
        ]
        plan = self._plan_with_unmatched(rows)
        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(consolidated.unmatched_collisions, [])
        self.assertEqual(len(consolidated.unmatched), 2)

    def test_near_miss_suggestions_survive_onto_the_merged_row(self):
        master_rec = {"building": "Similar Building", "provider": "UNION", "floor_unit": "1st", "property_id": "m1"}
        rows = [
            ListingRow(building="107 Cannon Street", provider="UNION", floor_unit="4th"),
            ListingRow(building="107 Cannon Street", provider="UNION", floor_unit="4th"),
        ]
        unmatched = [master_merge.UnmatchedRow(r, suggestions=[master_rec]) for r in rows]
        groups = master_merge._group_unmatched_duplicates(unmatched)
        plan = master_merge.MergePlan([master_rec], [], [], unmatched, [], groups)
        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched), 1)
        self.assertEqual(consolidated.unmatched[0].suggestions, [master_rec])

    def test_other_providers_unaffected(self):
        rows = [
            ListingRow(building="33 Cavendish Square", provider="Kitt's", floor_unit="2nd Floor", desks_max=20),
            ListingRow(building="33 Cavendish Square", provider="Kitt's", floor_unit="2nd Floor", desks_max=20),
            ListingRow(building="Copthall House", provider="Copthall Estates", floor_unit="4th Floor"),
        ]
        plan = self._plan_with_unmatched(rows)
        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched), 2)  # the Kitt's pair merged; Copthall row untouched
        self.assertEqual(consolidated.unmatched_collisions, [])


class BrochureLinkOverridesPostcodeConflictTests(unittest.TestCase):
    """
    Real reported false negative: the real UNION "Nexus Place - 25
    Farringdon Place" / 5th floor row appears on both the "City" and
    "Clerkenwell & Farringdon" sheets of the same real workbook - byte-
    identical building/floor_unit/brochure_link - but geocode.py's own
    submarket-biased Places search (each sheet's own different area name
    used as a disambiguation hint) returned two DIFFERENT real postcodes
    for the same actual building (confirmed against the real API: EC4M
    4AB vs EC1M 3HA). The postcode-conflict guard in
    _group_unmatched_duplicates correctly-by-design refused to merge
    genuinely different postcodes - but that "conflict" was a geocoding
    artifact here, not real identity evidence, while the identical
    brochure_link (much stronger, provider-issued evidence) was never
    consulted at all. See master_merge._group_unmatched_duplicates' own
    docstring for the full explanation.
    """

    _UNSET = object()

    def _rows(self, brochure_link_b=_UNSET):
        row_a = ListingRow(
            building="Nexus Place -  25 Farringdon Place", floor_unit="5th",
            postcode="EC4M 4AB", address_1="25 Farringdon Street",
            brochure_link="https://app.box.com/s/cktz4q797wgzo5dgoi1flvrcf2d70ae2",
            source_file="UNION.xlsx — City",
        )
        row_b = ListingRow(
            building="Nexus Place -  25 Farringdon Place", floor_unit="5th",
            postcode="EC1M 3HA", address_1="25-27 Farringdon Road",
            brochure_link=row_a.brochure_link if brochure_link_b is self._UNSET else brochure_link_b,
            source_file="UNION.xlsx — Clerkenwell & Farringdon",
        )
        return row_a, row_b

    def test_real_nexus_place_pattern_now_grouped(self):
        row_a, row_b = self._rows()
        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]

        groups = master_merge._group_unmatched_duplicates(unmatched)

        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_real_nexus_place_pattern_auto_consolidates_with_no_manual_review(self):
        row_a, row_b = self._rows()
        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]
        groups = master_merge._group_unmatched_duplicates(unmatched)
        plan = master_merge.MergePlan([], [], [], unmatched, [], groups)

        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched), 1)
        self.assertEqual(consolidated.unmatched_collisions, [])
        # The postcode conflict itself must not silently vanish without a
        # trace - matched_collision_field_choice still flags it internally
        # (see _merge_unmatched_group), it's just not forced into a manual
        # duplicate-identity decision purely because of it.

    def test_different_brochure_links_do_not_override_the_postcode_conflict(self):
        # Same street/provider/floor_unit, genuinely different postcodes,
        # AND genuinely different brochures - no strong counter-evidence,
        # so the postcode conflict must still block the merge exactly as
        # before this fix.
        row_a, row_b = self._rows(brochure_link_b="https://app.box.com/s/completely-different-brochure")

        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]
        groups = master_merge._group_unmatched_duplicates(unmatched)

        self.assertEqual(groups, [])

    def test_no_brochure_link_at_all_does_not_override_the_postcode_conflict(self):
        row_a, row_b = self._rows(brochure_link_b=None)
        row_a = row_a.model_copy(update={"brochure_link": None})

        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]
        groups = master_merge._group_unmatched_duplicates(unmatched)

        self.assertEqual(groups, [])

    def test_genuinely_different_house_number_still_blocks_even_with_shared_brochure(self):
        # The override is scoped to the postcode/address_1 checks ONLY - a
        # disagreeing house number (sourced from the provider's own text,
        # not geocoding) must still block the merge even if a
        # brochure_link happens to be shared (e.g. a provider data-entry
        # error linking the wrong document) - never weakened by this fix.
        row_a = ListingRow(
            building="14 Example Street", floor_unit="5th", postcode="EC1A 1AA",
            brochure_link="https://example.com/shared.pdf",
        )
        row_b = ListingRow(
            building="18 Example Street", floor_unit="5th", postcode="EC1A 1AA",
            brochure_link="https://example.com/shared.pdf",
        )
        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]

        groups = master_merge._group_unmatched_duplicates(unmatched)

        self.assertEqual(groups, [])

    def test_unrelated_buildings_sharing_a_brochure_link_are_not_merged(self):
        # The override only ever fires WITHIN the address-tier's own
        # existing street+provider+floor_unit match - two rows for
        # genuinely different streets are never even candidates for this
        # override, regardless of brochure_link.
        row_a = ListingRow(
            building="1 Totally Different Road", floor_unit="5th", postcode="EC4M 4AB",
            brochure_link="https://example.com/shared.pdf",
        )
        row_b = ListingRow(
            building="Nexus Place -  25 Farringdon Place", floor_unit="5th", postcode="EC1M 3HA",
            brochure_link="https://example.com/shared.pdf",
        )
        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]

        groups = master_merge._group_unmatched_duplicates(unmatched)

        self.assertEqual(groups, [])

    def test_different_floor_units_never_grouped_even_with_shared_brochure(self):
        # floor_unit is part of the address-tier grouping key itself - two
        # rows for different floors of the same street are never even
        # candidates for the postcode/brochure override, regardless of how
        # strong the brochure evidence is.
        row_a, row_b = self._rows()
        row_b = row_b.model_copy(update={"floor_unit": "6th"})

        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]
        groups = master_merge._group_unmatched_duplicates(unmatched)

        self.assertEqual(groups, [])

    def test_one_blank_postcode_auto_consolidates_regardless_of_this_fix(self):
        # A blank postcode is "no opinion", not a conflict, even without
        # any brochure_link evidence at all - this already worked before
        # this fix and must keep working identically after it.
        row_a, row_b = self._rows()
        row_b = row_b.model_copy(update={"postcode": None})

        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]
        groups = master_merge._group_unmatched_duplicates(unmatched)
        plan = master_merge.MergePlan([], [], [], unmatched, [], groups)

        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched), 1)
        self.assertEqual(consolidated.unmatched_collisions, [])

    def test_one_blank_brochure_link_with_agreeing_nonblank_still_auto_consolidates(self):
        # Only one row actually carries the brochure_link, the other is
        # blank on it (no opinion) - the "blank means no opinion"
        # tolerance already applied to every other field extends to this
        # override too, so this still counts as agreement, not a
        # disagreement, and the postcode conflict is still excused.
        row_a, row_b = self._rows()
        row_b = row_b.model_copy(update={"brochure_link": None})

        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]
        groups = master_merge._group_unmatched_duplicates(unmatched)
        plan = master_merge.MergePlan([], [], [], unmatched, [], groups)

        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched), 1)
        self.assertEqual(consolidated.unmatched_collisions, [])


class SourceRowIdentityPreservedTests(unittest.TestCase):
    """
    A provider's own source workbook can intentionally list the SAME
    building/floor more than once under a different area/submarket context
    (a real UNION file lists the same physical unit once per area sheet
    it's marketed under) - that is a decision about how many LISTING rows
    exist, entirely separate from whether two rows describe the same
    PHYSICAL property (see _brochure_link_identity_override's own,
    narrower postcode-only concern). _partition_by_source_submarket must
    keep such rows apart - never merged, never even offered as a manual
    duplicate decision - while still letting every other existing
    duplicate/conflict rule apply normally whenever submarket doesn't
    distinguish them. Deliberately provider-agnostic: every scenario here
    uses invented buildings/providers, proving the rule from ListingRow's
    own submarket field alone, never a hardcoded provider name or area.
    """

    def _plan_with_unmatched(self, rows):
        unmatched = [master_merge.UnmatchedRow(r) for r in rows]
        groups = master_merge._group_unmatched_duplicates(unmatched)
        return master_merge.MergePlan([], [], [], unmatched, [], groups)

    # 1. Different, meaningful, non-blank submarket -> two rows, no manual
    # duplicate decision at all.
    def test_different_meaningful_submarket_keeps_rows_separate_with_no_prompt(self):
        row_a = ListingRow(
            building="Example Tower", provider="Acme Estates", floor_unit="5th",
            submarket="North District", size_sqft=2000.0,
        )
        row_b = ListingRow(
            building="Example Tower", provider="Acme Estates", floor_unit="5th",
            submarket="South District", size_sqft=2000.0,
        )
        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]

        groups = master_merge._group_unmatched_duplicates(unmatched)
        self.assertEqual(groups, [])

        plan = self._plan_with_unmatched([row_a, row_b])
        consolidated = master_merge.consolidate_unmatched_duplicates(plan)
        self.assertEqual(len(consolidated.unmatched), 2)
        self.assertEqual(consolidated.unmatched_collisions, [])

    # 2. Same building/provider/floor AND same submarket -> existing
    # duplicate logic applies (safe auto-consolidation).
    def test_same_submarket_still_auto_consolidates_as_before(self):
        row_a = ListingRow(
            building="Example Tower", provider="Acme Estates", floor_unit="5th",
            submarket="North District", size_sqft=2000.0,
        )
        row_b = ListingRow(
            building="Example Tower", provider="Acme Estates", floor_unit="5th",
            submarket="North District", size_sqft=2000.0,
        )
        plan = self._plan_with_unmatched([row_a, row_b])

        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched), 1)
        self.assertEqual(consolidated.unmatched_collisions, [])

    # 3. One submarket blank -> do not assume distinct; fall back to
    # existing identity evidence (here, a genuine size conflict still
    # forces manual review exactly as it would with no submarket at all).
    def test_one_blank_submarket_does_not_assume_distinctness(self):
        row_a = ListingRow(
            building="Example Tower", provider="Acme Estates", floor_unit="5th",
            submarket="North District", size_sqft=2000.0,
        )
        row_b = ListingRow(
            building="Example Tower", provider="Acme Estates", floor_unit="5th",
            submarket=None, size_sqft=5000.0,
        )
        plan = self._plan_with_unmatched([row_a, row_b])

        groups = master_merge._group_unmatched_duplicates(
            [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]
        )
        self.assertEqual(len(groups), 1)  # still treated as ONE candidate-duplicate group

        consolidated = master_merge.consolidate_unmatched_duplicates(plan)
        self.assertEqual(len(consolidated.unmatched), 2)  # left for manual review
        self.assertEqual(len(consolidated.unmatched_collisions), 1)

    # 6. A pipeline-generated duplicate of one source row (identical
    # submarket, identical everything) still consolidates even in a batch
    # that ALSO contains a genuinely distinct-submarket row for the same
    # building/floor - the distinct row must not block the OTHER, genuinely
    # safe consolidation.
    def test_accidental_duplicate_still_consolidates_alongside_a_distinct_submarket_row(self):
        row_a = ListingRow(
            building="Example Tower", provider="Acme Estates", floor_unit="5th",
            submarket="North District", size_sqft=2000.0,
        )
        row_a_dupe = row_a.model_copy(update={"property_id": None})
        row_b = ListingRow(
            building="Example Tower", provider="Acme Estates", floor_unit="5th",
            submarket="South District", size_sqft=2000.0,
        )
        plan = self._plan_with_unmatched([row_a, row_a_dupe, row_b])

        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched), 2)  # merged North pair + separate South row
        self.assertEqual(consolidated.unmatched_collisions, [])
        submarkets = sorted(r.new_row.submarket for r in consolidated.unmatched)
        self.assertEqual(submarkets, ["North District", "South District"])

    # 7. A genuine source-data conflict (unrelated to submarket) still
    # requires manual review even when submarket also happens to differ -
    # a submarket difference isn't a blanket excuse for every OTHER field
    # to be flagged instead of resolved; it only ever governs whether rows
    # are grouped as duplicate candidates AT ALL. Here they aren't (this
    # covers the "genuinely conflicting duplicate" case at the row-pair
    # level - a group that never forms produces no prompt, which is
    # covered by test 1 above; this test instead confirms a SAME-submarket
    # conflict still gets manual review, unaffected by this feature).
    def test_genuine_conflict_with_same_submarket_still_needs_manual_review(self):
        row_a = ListingRow(
            building="Example Tower", provider="Acme Estates", floor_unit="5th",
            submarket="North District", size_sqft=2000.0,
        )
        row_b = ListingRow(
            building="Example Tower", provider="Acme Estates", floor_unit="5th",
            submarket="North District", size_sqft=9999.0,
        )
        plan = self._plan_with_unmatched([row_a, row_b])

        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched), 2)
        self.assertEqual(len(consolidated.unmatched_collisions), 1)

    # 8. A different provider entirely is unaffected by this rule existing.
    def test_unrelated_provider_batch_is_unaffected(self):
        row_a = ListingRow(building="Some Building", provider="Other Provider", floor_unit="2nd", size_sqft=800.0)
        row_b = ListingRow(building="Some Building", provider="Other Provider", floor_unit="2nd", size_sqft=800.0)
        plan = self._plan_with_unmatched([row_a, row_b])

        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched), 1)
        self.assertEqual(consolidated.unmatched_collisions, [])

    # Non-UNION regression proving the rule is fully generic - a different
    # invented provider/format, same mechanism.
    def test_generic_non_union_provider_with_area_distinction(self):
        row_a = ListingRow(
            building="Riverside House", provider="Meridian Workspace", floor_unit="3rd",
            submarket="Riverside", size_sqft=1500.0,
        )
        row_b = ListingRow(
            building="Riverside House", provider="Meridian Workspace", floor_unit="3rd",
            submarket="Docklands", size_sqft=1500.0,
        )
        plan = self._plan_with_unmatched([row_a, row_b])

        groups = master_merge._group_unmatched_duplicates(
            [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]
        )
        self.assertEqual(groups, [])

        consolidated = master_merge.consolidate_unmatched_duplicates(plan)
        self.assertEqual(len(consolidated.unmatched), 2)
        self.assertEqual(consolidated.unmatched_collisions, [])

    # Documents the deliberate limitation _partition_by_source_submarket's
    # own docstring describes: a future provider file could intentionally
    # list the SAME building/floor/submarket twice with a genuinely
    # different source-supplied value (desks, here) - this architecture has
    # no way to prove that's two real listings rather than one drifted
    # duplicate, so it is NOT auto-split (unlike a genuine submarket
    # difference) and NOT auto-merged (the values genuinely disagree) -
    # it falls through to the existing, already-safe manual-review
    # fallback, exactly as it would with no submarket involved at all.
    # Generic/non-UNION on purpose, proving this boundary isn't format-
    # specific either.
    def test_same_submarket_with_a_meaningful_value_difference_still_needs_manual_review(self):
        row_a = ListingRow(
            building="Riverside House", provider="Meridian Workspace", floor_unit="3rd",
            submarket="Riverside", desks_max=20,
        )
        row_b = ListingRow(
            building="Riverside House", provider="Meridian Workspace", floor_unit="3rd",
            submarket="Riverside", desks_max=45,
        )
        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]

        groups = master_merge._group_unmatched_duplicates(unmatched)
        self.assertEqual(len(groups), 1)  # NOT split apart - same submarket, still one candidate group

        plan = master_merge.MergePlan([], [], [], unmatched, [], groups)
        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched), 2)  # NOT auto-merged - values genuinely disagree
        self.assertEqual(len(consolidated.unmatched_collisions), 1)  # left for a human to decide


class NexusPlaceSourceRowVsPhysicalIdentityTests(unittest.TestCase):
    """
    The required Nexus Place regression test (see this module's own
    _partition_by_source_submarket/_group_unmatched_duplicates docstrings):
    the SAME real UNION listing intentionally appears under two different
    submarkets in the original workbook (City / Clerkenwell & Farringdon) -
    genuinely two source rows, not a pipeline duplicate - while also being
    the exact real case _brochure_link_identity_override exists for (a
    shared brochure_link overriding a geocoding-artifact postcode
    disagreement). Both protections must hold at once: 2 rows out, 0
    manual-merge prompts, submarkets preserved, shared brochure_link never
    used to collapse them into one - proving source-listing identity and
    physical-property identity are governed independently.
    """

    def _rows(self):
        row_a = ListingRow(
            submarket="City", building="Nexus Place - 25 Farringdon Place", floor_unit="5th",
            postcode="EC4M 4AB", address_1="25 Farringdon Street",
            brochure_link="https://app.box.com/s/cktz4q797wgzo5dgoi1flvrcf2d70ae2",
            source_file="UNION.xlsx — City",
        )
        row_b = ListingRow(
            submarket="Clerkenwell & Farringdon", building="Nexus Place - 25 Farringdon Place", floor_unit="5th",
            postcode="EC1M 3HA", address_1="25-27 Farringdon Road",
            brochure_link=row_a.brochure_link,
            source_file="UNION.xlsx — Clerkenwell & Farringdon",
        )
        return row_a, row_b

    def test_nexus_place_yields_two_rows_with_no_manual_merge_decision(self):
        row_a, row_b = self._rows()
        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]

        groups = master_merge._group_unmatched_duplicates(unmatched)
        self.assertEqual(groups, [])

        plan = master_merge.MergePlan([], [], [], unmatched, [], groups)
        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched), 2)
        self.assertEqual(consolidated.unmatched_collisions, [])
        submarkets = sorted(r.new_row.submarket for r in consolidated.unmatched)
        self.assertEqual(submarkets, ["City", "Clerkenwell & Farringdon"])

    def test_shared_brochure_link_does_not_collapse_the_two_submarkets(self):
        # Explicit safety check: the exact identity evidence that WOULD
        # merge two rows lacking a submarket distinction (see
        # BrochureLinkOverridesPostcodeConflictTests) must never override a
        # genuine submarket split.
        row_a, row_b = self._rows()
        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]

        self.assertTrue(master_merge._brochure_link_identity_override(
            [row_a.model_dump(), row_b.model_dump()]
        ))  # the override condition genuinely holds here...
        groups = master_merge._group_unmatched_duplicates(unmatched)
        self.assertEqual(groups, [])  # ...but still never merges them.

    def test_does_not_prevent_a_genuine_accidental_duplicate_in_the_same_submarket(self):
        # A THIRD row - an accidental re-extraction of row_a itself (same
        # submarket, same everything) - must still safely auto-consolidate
        # with row_a, without being dragged into row_b's separate identity.
        row_a, row_b = self._rows()
        row_a_dupe = row_a.model_copy(update={"source_file": "UNION.xlsx — City (re-run)"})
        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_a_dupe), master_merge.UnmatchedRow(row_b)]
        plan = master_merge.MergePlan([], [], [], unmatched, [], master_merge._group_unmatched_duplicates(unmatched))

        consolidated = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(len(consolidated.unmatched), 2)  # merged City pair + separate Clerkenwell row
        self.assertEqual(consolidated.unmatched_collisions, [])


class CollisionGroupFieldsTests(unittest.TestCase):
    def test_union_of_diffs_in_diff_fields_order(self):
        master_df = _master_df([{"building": "A", "provider": "P1", "size_sqft": 1000.0, "state_of_space": "Cat A"}])
        row_a = ListingRow(building="A", provider="P1", size_sqft=1200.0, state_of_space="Cat A")
        row_b = ListingRow(building="A", provider="P1", size_sqft=1000.0, state_of_space="Fitted")

        plan = master_merge.build_merge_plan([row_a, row_b], master_df)

        self.assertEqual(len(plan.collisions), 1)
        self.assertEqual(master_merge.collision_group_fields(plan.collisions[0]), ["size_sqft", "state_of_space"])

    def test_field_no_member_changed_is_not_included(self):
        master_df = _master_df([{"building": "A", "provider": "P1", "size_sqft": 1000.0}])
        row_a = ListingRow(building="A", provider="P1", size_sqft=1200.0)
        row_b = ListingRow(building="A", provider="P1", size_sqft=1200.0)

        plan = master_merge.build_merge_plan([row_a, row_b], master_df)

        self.assertEqual(master_merge.collision_group_fields(plan.collisions[0]), ["size_sqft"])


class BuildMergePlanIntraBatchDuplicateTests(unittest.TestCase):
    """The exact reported scenario: a PDF upload and an email upload of the
    same real property, neither matching master, must be recognized as
    duplicates of EACH OTHER - not just checked against master."""

    def test_identical_match_key_rows_are_grouped(self):
        # Real field values from the 77 Gracechurch Street PDF + email test.
        pdf_row = ListingRow(
            building="77 Gracechurch Street", provider="Workplace Plus",
            floor_unit="6th Floor", postcode="EC3V 0AS", source_file="pdf.pdf",
        )
        email_row = ListingRow(
            building="77 Gracechurch Street", provider="Workplace Plus",
            floor_unit="6th Floor", postcode="EC3V 0AS", source_file="email.eml",
        )

        plan = master_merge.build_merge_plan([pdf_row, email_row], _empty_master_df_like(pdf_row))

        self.assertEqual(len(plan.unmatched), 2)
        self.assertEqual(len(plan.unmatched_collisions), 1)
        self.assertEqual(len(plan.unmatched_collisions[0]), 2)

    def test_different_postcode_is_not_grouped(self):
        row_a = ListingRow(building="1 Example Street", provider="Test Provider",
                            floor_unit="1st Floor", postcode="EC1A 1AA")
        row_b = ListingRow(building="1 Example Street", provider="Test Provider",
                            floor_unit="1st Floor", postcode="SW1A 1AA")

        plan = master_merge.build_merge_plan([row_a, row_b], _empty_master_df_like(row_a))

        self.assertEqual(len(plan.unmatched), 2)
        self.assertEqual(len(plan.unmatched_collisions), 0)

    def test_a_single_unique_new_row_has_no_collision(self):
        row = ListingRow(building="1 Example Street", provider="Test Provider", floor_unit="1st Floor")

        plan = master_merge.build_merge_plan([row], _empty_master_df_like(row))

        self.assertEqual(len(plan.unmatched), 1)
        self.assertEqual(len(plan.unmatched_collisions), 0)


class AddressStreetKeyTests(unittest.TestCase):
    def test_house_number_and_abbreviated_suffix_match_full_street_name(self):
        # Real pair from the same uploaded Copthall Estates file: a
        # portfolio-wide rollup sheet vs that provider's own dedicated
        # per-area sheet for the SAME real building.
        self.assertEqual(
            master_merge._address_street_key("89 Charterhouse St"),
            master_merge._address_street_key("Charterhouse Street"),
        )

    def test_different_house_numbers_still_share_a_street_key(self):
        # The street-key alone doesn't disambiguate these - that's
        # _leading_house_number's job (see the grouping-level tests below),
        # not this function's.
        self.assertEqual(
            master_merge._address_street_key("27 Cannon Street"),
            master_merge._address_street_key("108 Cannon Street"),
        )

    def test_blank_building_has_no_address_key(self):
        self.assertEqual(master_merge._address_street_key(None), "")

    def test_number_only_building_has_no_address_key(self):
        self.assertEqual(master_merge._address_street_key("89"), "")


class LeadingHouseNumberTests(unittest.TestCase):
    def test_extracts_a_plain_number(self):
        self.assertEqual(master_merge._leading_house_number("89 Charterhouse St"), "89")

    def test_extracts_a_range(self):
        self.assertEqual(master_merge._leading_house_number("27-30 Lime Street"), "27-30")

    def test_no_number_returns_none(self):
        self.assertIsNone(master_merge._leading_house_number("Charterhouse Street"))

    def test_blank_returns_none(self):
        self.assertIsNone(master_merge._leading_house_number(None))


class AddressAwareDuplicateGroupingTests(unittest.TestCase):
    """The fix confirmed still outstanding after the earlier intra-batch
    merge-UI work: two pending rows for the same real building, one with a
    house number and abbreviated street suffix, the other without the
    number at all, must still be recognized as duplicates of each other."""

    def test_house_number_and_full_street_name_are_grouped(self):
        # Real pair from the same uploaded Copthall Estates file.
        rollup_row = ListingRow(
            building="89 Charterhouse St", provider="Copthall Estates",
            floor_unit="3rd Floor", source_file="x.xlsx — Portfolio",
        )
        per_area_row = ListingRow(
            building="Charterhouse Street", provider="Copthall Estates",
            floor_unit="3rd Floor", source_file="x.xlsx — Mid Town",
        )

        plan = master_merge.build_merge_plan([rollup_row, per_area_row], _empty_master_df_like(rollup_row))

        self.assertEqual(len(plan.unmatched_collisions), 1)
        self.assertEqual(len(plan.unmatched_collisions[0]), 2)

    def test_different_house_numbers_on_the_same_street_are_not_grouped(self):
        # Real confirmed-different pair (BUILDING_FUZZY_MATCH_THRESHOLD's
        # own comment) - sharing a street name must never be enough alone.
        row_a = ListingRow(building="27 Cannon Street", provider="Kitt's", floor_unit="1st")
        row_b = ListingRow(building="108 Cannon Street", provider="Kitt's", floor_unit="1st")

        plan = master_merge.build_merge_plan([row_a, row_b], _empty_master_df_like(row_a))

        self.assertEqual(len(plan.unmatched_collisions), 0)

    def test_different_floor_unit_is_not_grouped(self):
        row_a = ListingRow(building="89 Charterhouse St", provider="Copthall Estates", floor_unit="3rd Floor")
        row_b = ListingRow(building="Charterhouse Street", provider="Copthall Estates", floor_unit="4th Floor")

        plan = master_merge.build_merge_plan([row_a, row_b], _empty_master_df_like(row_a))

        self.assertEqual(len(plan.unmatched_collisions), 0)

    def test_different_provider_is_not_grouped(self):
        row_a = ListingRow(building="89 Charterhouse St", provider="Copthall Estates", floor_unit="3rd Floor")
        row_b = ListingRow(building="Charterhouse Street", provider="A Different Agent", floor_unit="3rd Floor")

        plan = master_merge.build_merge_plan([row_a, row_b], _empty_master_df_like(row_a))

        self.assertEqual(len(plan.unmatched_collisions), 0)

    def test_different_non_blank_postcodes_are_not_grouped(self):
        row_a = ListingRow(building="89 Charterhouse St", provider="Copthall Estates",
                            floor_unit="3rd Floor", postcode="EC1A 1AA")
        row_b = ListingRow(building="Charterhouse Street", provider="Copthall Estates",
                            floor_unit="3rd Floor", postcode="SW1A 1AA")

        plan = master_merge.build_merge_plan([row_a, row_b], _empty_master_df_like(row_a))

        self.assertEqual(len(plan.unmatched_collisions), 0)

    def test_one_blank_postcode_does_not_block_the_match(self):
        row_a = ListingRow(building="89 Charterhouse St", provider="Copthall Estates",
                            floor_unit="3rd Floor", postcode="EC1M 6PE")
        row_b = ListingRow(building="Charterhouse Street", provider="Copthall Estates", floor_unit="3rd Floor")

        plan = master_merge.build_merge_plan([row_a, row_b], _empty_master_df_like(row_a))

        self.assertEqual(len(plan.unmatched_collisions), 1)

    def test_exact_match_pass_and_address_aware_pass_union_into_one_group(self):
        # Three-way case: two rows share the exact key already (no number on
        # either), the third only matches via the address-aware key - all
        # three must end up in the SAME final group, not two separate ones.
        exact_a = ListingRow(building="Charterhouse Street", provider="Copthall Estates",
                              floor_unit="3rd Floor", source_file="a")
        exact_b = ListingRow(building="Charterhouse Street", provider="Copthall Estates",
                              floor_unit="3rd Floor", source_file="b")
        address_only = ListingRow(building="89 Charterhouse St", provider="Copthall Estates",
                                   floor_unit="3rd Floor", source_file="c")

        plan = master_merge.build_merge_plan(
            [exact_a, exact_b, address_only], _empty_master_df_like(exact_a)
        )

        self.assertEqual(len(plan.unmatched_collisions), 1)
        self.assertEqual(len(plan.unmatched_collisions[0]), 3)


def _empty_master_df_like(row: ListingRow) -> pd.DataFrame:
    return pd.DataFrame(columns=list(row.model_dump().keys()))


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


class CanonicalizeProvidersInternalRefSyncTests(unittest.TestCase):
    """
    Real confirmed bug: internal_ref is documented (schema.ExtractedFields'
    own "internal_ref ... mirrors provider" comment) to mirror provider,
    but canonicalize_providers only ever corrected row.provider - a real
    "Business Cube.pdf" upload's own Gemini extraction (extract.py's
    "internal_ref": raw.get("provider"), a verbatim copy at extraction
    time) produced internal_ref="business cube" while provider got
    corrected to "Business Cube" via KNOWN_PROVIDERS, leaving internal_ref
    stranded at the old lowercase text despite representing the identical
    real fact. "Business Cube" is a real KNOWN_PROVIDERS entry, so this
    exercises the actual correction, not a synthetic stand-in.
    """

    def test_business_cube_lowercase_internal_ref_is_corrected_alongside_provider(self):
        row = ListingRow(building="A", provider="business cube", internal_ref="business cube")

        master_merge.canonicalize_providers([row])

        self.assertEqual(row.provider, "Business Cube")
        self.assertEqual(row.internal_ref, "Business Cube")

    def test_internal_ref_already_correctly_cased_is_unaffected(self):
        row = ListingRow(building="A", provider="business cube", internal_ref="Business Cube")

        master_merge.canonicalize_providers([row])

        self.assertEqual(row.internal_ref, "Business Cube")

    def test_a_genuinely_different_internal_ref_is_never_touched(self):
        # extract_spreadsheet.py's own "External Ref" column mapping - a
        # real, provider-specific reference code with nothing to do with
        # the provider's own name. Never case-insensitively matched
        # provider in the first place, so this must be left completely
        # alone rather than guessing the two were meant to become equal.
        row = ListingRow(building="A", provider="business cube", internal_ref="REF-12345")

        master_merge.canonicalize_providers([row])

        self.assertEqual(row.provider, "Business Cube")
        self.assertEqual(row.internal_ref, "REF-12345")

    def test_blank_internal_ref_is_never_forced_to_match_provider(self):
        # fill_missing_provider (app.py) already owns filling a genuinely
        # blank internal_ref from provider - canonicalize_providers must
        # never duplicate or race that by inventing a value here.
        row = ListingRow(building="A", provider="business cube", internal_ref=None)

        master_merge.canonicalize_providers([row])

        self.assertIsNone(row.internal_ref)

    def test_blank_provider_never_crashes_or_touches_internal_ref(self):
        row = ListingRow(building="A", provider=None, internal_ref="Some Ref")

        master_merge.canonicalize_providers([row])

        self.assertIsNone(row.provider)
        self.assertEqual(row.internal_ref, "Some Ref")

    def test_both_blank_is_a_complete_no_op(self):
        row = ListingRow(building="A", provider=None, internal_ref=None)

        master_merge.canonicalize_providers([row])

        self.assertIsNone(row.provider)
        self.assertIsNone(row.internal_ref)

    def test_suffix_stripped_provider_still_syncs_a_matching_internal_ref(self):
        # internal_ref was copied from the RAW pre-strip provider text
        # (e.g. extract_email.py's own "internal_ref": raw.get("provider"))
        # - the case-insensitive match must compare against that SAME raw
        # value, before either stage of correction, not the already-
        # stripped/canonicalized one.
        row = ListingRow(building="A", provider="Copthall Estates Availability", internal_ref="copthall estates availability")

        master_merge.canonicalize_providers([row])

        self.assertEqual(row.provider, "Copthall Estates")
        self.assertEqual(row.internal_ref, "Copthall Estates")

    def test_mismatched_case_and_whitespace_still_counts_as_a_match(self):
        row = ListingRow(building="A", provider="  business cube  ", internal_ref="BUSINESS CUBE")

        master_merge.canonicalize_providers([row])

        self.assertEqual(row.internal_ref, "Business Cube")


class ProviderPurposeSuffixTests(unittest.TestCase):
    """Real reported bug: a single uploaded Copthall Estates workbook's
    per-sheet provider extraction (see extract_spreadsheet_gemini.PROMPT -
    each sheet's own title/branding text is transcribed verbatim) produced
    "Copthall Estates" from one sheet and "Copthall Estates Availibility"
    (that exact misspelling) from another for the SAME real Throgmorton
    Avenue 2nd Floor unit - two separate "no match found" new-property rows
    instead of one flagged batch duplicate, because provider is part of
    every matching/dedup key."""

    def test_strips_availability_and_the_real_misspelling(self):
        self.assertEqual(
            master_merge._strip_provider_purpose_suffix("Copthall Estates Availability"), "Copthall Estates",
        )
        self.assertEqual(
            master_merge._strip_provider_purpose_suffix("Copthall Estates Availibility"), "Copthall Estates",
        )

    def test_bare_suffix_word_alone_is_left_unchanged(self):
        # Nothing meaningful would be left after stripping - a value this
        # bare was never a confident provider extraction to begin with.
        self.assertEqual(master_merge._strip_provider_purpose_suffix("Availability"), "Availability")

    def test_unrelated_word_is_untouched(self):
        self.assertEqual(master_merge._strip_provider_purpose_suffix("Business Cube"), "Business Cube")

    def test_blank_passes_through_unchanged(self):
        self.assertIsNone(master_merge._strip_provider_purpose_suffix(None))
        self.assertEqual(master_merge._strip_provider_purpose_suffix(""), "")

    def test_unrelated_provider_ending_in_a_different_word_is_not_merged(self):
        # Proves the strip is narrowly scoped to the confirmed sheet-purpose
        # words only, not a general "strip the trailing word and see if it
        # matches" heuristic - two genuinely DIFFERENT real providers for
        # the same unit (e.g. two different agents both listing it) must
        # never collapse into the same match key just because one string
        # happens to be a prefix of the other.
        row_a = ListingRow(
            building="1 Example Street", provider="Copthall Estates",
            floor_unit="1st Floor", postcode="EC1A 1AA",
        )
        row_b = ListingRow(
            building="1 Example Street", provider="Copthall Estates Group",
            floor_unit="1st Floor", postcode="EC1A 1AA",
        )
        master_merge.canonicalize_providers([row_a, row_b])
        self.assertNotEqual(row_a.provider, row_b.provider)
        plan = master_merge.build_merge_plan([row_a, row_b], _master_df([]))
        self.assertEqual(len(plan.unmatched_collisions), 0)

    def test_availability_suffix_variant_recognized_as_same_provider(self):
        # The actual reported bug: same real Throgmorton Avenue 2nd Floor
        # unit, one sheet's provider "Copthall Estates", another sheet's
        # "Copthall Estates Availibility" - must now be flagged as one
        # batch duplicate instead of two separate "no match" new rows.
        row_a = ListingRow(building="Throgmorton Avenue", provider="Copthall Estates", floor_unit="2nd Floor")
        row_b = ListingRow(
            building="Throgmorton Avenue", provider="Copthall Estates Availibility", floor_unit="2nd Floor",
        )
        rows = [row_a, row_b]
        master_merge.canonicalize_providers(rows)
        self.assertEqual(row_a.provider, row_b.provider)
        plan = master_merge.build_merge_plan(rows, _master_df([]))
        self.assertEqual(len(plan.unmatched_collisions), 1)
        self.assertEqual(len(plan.unmatched_collisions[0]), 2)


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


class BuildMergePlanNearMatchSuggestionProviderScopeTests(unittest.TestCase):
    """End-to-end (build_merge_plan) coverage for the near-match SUGGESTION
    hint's own provider scoping - distinct from the real matching tiers
    (already correctly provider-scoped, see BuildMergePlanFuzzyBuildingTests)
    which only decide auto-update vs. unmatched. This is specifically about
    what UnmatchedRow.suggestions - the "possible near-misses" list a human
    reviewer sees - contains once a row is already unmatched."""

    def test_metspace_clerkenwell_road_is_not_blocked_by_union_80_clerkenwell_road(self):
        # The real confirmed case: a MetSpace listing must land as a
        # genuinely new property, with no "possible near-miss" pointing at
        # an unrelated UNION listing.
        master_df = _master_df([{"building": "80 Clerkenwell Road", "provider": "UNION", "floor_unit": "2nd"}])
        new_row = ListingRow(building="Clerkenwell Road", provider="MetSpace", floor_unit="4th Floor")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed) + len(plan.matched_unchanged), 0)
        self.assertEqual(len(plan.unmatched), 1)
        self.assertEqual(plan.unmatched[0].suggestions, [])

    def test_same_provider_genuine_near_match_still_suggested_for_review(self):
        # Provider scoping must never remove a genuinely useful SAME-
        # provider hint - only cross-provider ones.
        master_df = _master_df([{"building": "Thirty Lighterman", "provider": "MetSpace", "floor_unit": "9th"}])
        new_row = ListingRow(building="Thirty Lightman", provider="MetSpace", floor_unit="2nd Floor")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.unmatched), 1)
        self.assertEqual(len(plan.unmatched[0].suggestions), 1)

    def test_same_building_two_different_providers_becomes_two_master_rows(self):
        # Provider is part of listing identity: the same physical building
        # listed by two different providers must never be merged into one
        # row - each stays (or becomes) its own separate listing.
        master_df = _master_df([
            {"building": "Adler House", "provider": "MetSpace", "floor_unit": "1st"},
        ])
        new_row = ListingRow(building="Adler House", provider="UNION", floor_unit="1st")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed) + len(plan.matched_unchanged), 0)
        self.assertEqual(len(plan.unmatched), 1)
        self.assertEqual(plan.unmatched[0].suggestions, [])


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

    def test_rent_psf_rounding_only_difference_is_not_a_diff(self):
        # Real Kitt's Availability shape - diff_fields now passes the
        # field name through to _values_equal, so this gets the same
        # whole-pound tolerance as matched_collision_field_choice does.
        diffs = master_merge.diff_fields({"rent_psf": 243.0}, {"rent_psf": 243.108108})
        self.assertEqual(diffs, {})

    def test_rent_pcm_rounding_only_difference_is_not_a_diff(self):
        diffs = master_merge.diff_fields({"rent_pcm": 18700.0}, {"rent_pcm": 18700.4})
        self.assertEqual(diffs, {})

    def test_rent_psf_genuinely_different_pounds_is_still_a_diff(self):
        diffs = master_merge.diff_fields({"rent_psf": 243.0}, {"rent_psf": 296.0})
        self.assertEqual(diffs, {"rent_psf": (243.0, 296.0)})

    def test_size_sqft_rounding_is_not_widened(self):
        diffs = master_merge.diff_fields({"size_sqft": 1000.0}, {"size_sqft": 1000.3})
        self.assertEqual(diffs, {"size_sqft": (1000.0, 1000.3)})


class SilentFieldUpdatesTests(unittest.TestCase):
    def test_case_only_change_is_silent(self):
        updates = master_merge.silent_field_updates({"provider": "Metspace"}, {"provider": "METSPACE"})
        self.assertEqual(updates, {"provider": "METSPACE"})

    def test_real_change_is_not_silent(self):
        updates = master_merge.silent_field_updates({"provider": "A"}, {"provider": "B"})
        self.assertEqual(updates, {})


class BuildMergePlanBrochureLinkBrokenTests(unittest.TestCase):
    """
    ListingRow.brochure_link_broken - diagnostic pipeline metadata, never
    a reviewable field change (see build_merge_plan's own comment on why
    it's moved into silent_updates), but still a genuine value that must
    reach master on Approve without a human click, and must never be
    disturbed by a fresh row that simply didn't recompute it this run.
    """

    def test_a_genuine_change_is_routed_to_silent_never_to_diffs(self):
        master_df = _master_df([{"building": "Dead Design House", "brochure_link_broken": True}])
        new_row = ListingRow(building="Dead Design House", brochure_link_broken=False)

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = (plan.matched_changed + plan.matched_unchanged)[0]
        self.assertNotIn("brochure_link_broken", matched.diffs)
        self.assertEqual(matched.silent_updates.get("brochure_link_broken"), False)

    def test_a_fresh_row_that_never_rechecked_the_link_never_disturbs_master(self):
        # The real "fixed link fixes itself on the NEXT genuine re-check"
        # guarantee, from the other side: a row that simply wasn't re-
        # enriched this run (brochure_link_broken defaults to None) must
        # never accidentally clear master's own already-confirmed True -
        # diff_fields' own blank-new-value-skip rule is what gives this
        # for free (None is blank, so this field never even enters diffs
        # or silent at all here).
        master_df = _master_df([{"building": "Dead Design House", "brochure_link_broken": True}])
        new_row = ListingRow(building="Dead Design House")  # brochure_link_broken defaults to None

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = (plan.matched_changed + plan.matched_unchanged)[0]
        self.assertNotIn("brochure_link_broken", matched.diffs)
        self.assertNotIn("brochure_link_broken", matched.silent_updates)

    def test_applying_the_silent_update_actually_reaches_the_merged_row(self):
        # End-to-end through apply_merge itself (the same call pages/2_
        # Review_and_Master.py makes with silent_by_index folded in) -
        # confirms a fixed-and-reverified link genuinely un-stalls, not
        # just that build_merge_plan computed the right dict.
        master_records = [ListingRow(building="Dead Design House", brochure_link_broken=True).model_dump()]

        merged = master_merge.apply_merge(master_records, {0: {"brochure_link_broken": False}}, [])

        self.assertIs(merged[0].brochure_link_broken, False)


class IsBarePostcodeDistrictTests(unittest.TestCase):
    def test_bare_district_is_recognized(self):
        self.assertTrue(master_merge._is_bare_postcode_district("SE1"))
        self.assertTrue(master_merge._is_bare_postcode_district("ec1v"))  # case-insensitive

    def test_a_real_place_name_is_not_a_bare_district(self):
        self.assertFalse(master_merge._is_bare_postcode_district("Borough"))
        self.assertFalse(master_merge._is_bare_postcode_district("London Bridge"))

    def test_a_full_postcode_is_not_bare(self):
        self.assertFalse(master_merge._is_bare_postcode_district("SE1 8QH"))

    def test_blank_is_not_bare(self):
        self.assertFalse(master_merge._is_bare_postcode_district(None))
        self.assertFalse(master_merge._is_bare_postcode_district(""))


class OutwardCodeFromPostcodeTests(unittest.TestCase):
    def test_full_postcode_extracts_the_outward_code(self):
        self.assertEqual(master_merge._outward_code_from_postcode("SE1 8QH"), "SE1")

    def test_bare_postcode_returns_itself_normalized(self):
        self.assertEqual(master_merge._outward_code_from_postcode("se1"), "SE1")

    def test_subdivision_letter_is_preserved_not_collapsed(self):
        # Deliberately different from geocode.py's own _district_parts,
        # which WOULD collapse "EC1V" to ("EC", "1") - see this function's
        # own docstring for why that's wrong for naming specifically.
        self.assertEqual(master_merge._outward_code_from_postcode("EC1V 9RT"), "EC1V")

    def test_non_postcode_text_returns_none(self):
        self.assertIsNone(master_merge._outward_code_from_postcode("Borough"))
        self.assertIsNone(master_merge._outward_code_from_postcode(None))


class BuildPostcodeSubmarketLookupTests(unittest.TestCase):
    """
    Takes build_merge_plan's own already-clean_value'd master_records
    (plain dicts with real None, never a raw DataFrame) - see that
    function's own docstring for why a raw master_df.to_dict() (float(
    'nan') for a missing value, truthy in Python) would silently corrupt
    this. test_a_raw_dataframe_nan_is_never_mistaken_for_a_confirmed_value
    below exercises that exact real bug directly, not just via a plain
    dict input.
    """

    def test_a_single_confirmed_row_populates_the_lookup(self):
        lookup = master_merge.build_postcode_submarket_lookup(
            [{"postcode": "SE1 9RT", "submarket": "Bankside"}],
        )
        self.assertEqual(lookup.get("SE1"), "Bankside")

    def test_unanimous_agreement_across_several_rows_still_resolves(self):
        lookup = master_merge.build_postcode_submarket_lookup([
            {"postcode": "SE1 9RT", "submarket": "Bankside"},
            {"postcode": "SE1 0AA", "submarket": "Bankside"},
        ])
        self.assertEqual(lookup.get("SE1"), "Bankside")

    def test_genuine_disagreement_resolves_to_nothing_never_a_guess(self):
        # The real SE1 case: South Bank, Borough, Bankside, and Waterloo
        # are all genuinely correct for different SE1 buildings - picking
        # any ONE of several already-observed real answers is still a
        # guess, so this must stay unresolved, not pick a majority/latest.
        lookup = master_merge.build_postcode_submarket_lookup([
            {"postcode": "SE1 9RT", "submarket": "Bankside"},
            {"postcode": "SE1 0AA", "submarket": "Borough"},
        ])
        self.assertNotIn("SE1", lookup)

    def test_a_bare_postcode_submarket_is_never_itself_treated_as_confirmed(self):
        lookup = master_merge.build_postcode_submarket_lookup(
            [{"postcode": "SE1 9RT", "submarket": "SE1"}],
        )
        self.assertNotIn("SE1", lookup)

    def test_a_row_with_no_postcode_never_contributes(self):
        lookup = master_merge.build_postcode_submarket_lookup([{"submarket": "Bankside"}])
        self.assertEqual(lookup, {})

    def test_different_districts_never_interfere_with_each_other(self):
        lookup = master_merge.build_postcode_submarket_lookup([
            {"postcode": "SE1 9RT", "submarket": "Bankside"},
            {"postcode": "EC1V 4PW", "submarket": "Clerkenwell"},
        ])
        self.assertEqual(lookup.get("SE1"), "Bankside")
        self.assertEqual(lookup.get("EC1V"), "Clerkenwell")

    def test_a_row_with_no_submarket_at_all_never_contributes(self):
        # A real regression this guards against: a raw master_df.to_dict()
        # record's missing submarket comes back as float('nan') - truthy
        # in Python, so a naive `not submarket` check alone doesn't catch
        # it, which would otherwise look like a SECOND, conflicting
        # confirmed value for the same district and wrongly blank out a
        # real one (see this function's own docstring). None (what a
        # properly clean_value'd record actually holds) must be excluded
        # just as cleanly as an empty string.
        lookup = master_merge.build_postcode_submarket_lookup([
            {"postcode": "SE1 9RT", "submarket": "Bankside"},
            {"postcode": "SE1 0AA", "submarket": None},
        ])
        self.assertEqual(lookup.get("SE1"), "Bankside")


class BackfillPostcodeSubmarketsTests(unittest.TestCase):
    """
    backfill_postcode_submarkets - the SEPARATE, explicit retroactive
    action (see its own docstring for why this is never bundled into
    every ordinary approve the way the forward-only fresh-row correction
    in build_merge_plan is).
    """

    def test_a_bare_row_is_corrected_when_a_confirmed_name_exists_elsewhere(self):
        records = [
            {"building": "A", "postcode": "SE1 0AA", "submarket": "Bankside"},
            {"building": "B", "postcode": "SE1 9RT", "submarket": "SE1"},
        ]
        updated, changes = master_merge.backfill_postcode_submarkets(records)
        self.assertEqual(updated[1]["submarket"], "Bankside")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["old_submarket"], "SE1")
        self.assertEqual(changes[0]["new_submarket"], "Bankside")
        self.assertEqual(changes[0]["building"], "B")

    def test_original_records_list_is_never_mutated(self):
        records = [
            {"building": "A", "postcode": "SE1 0AA", "submarket": "Bankside"},
            {"building": "B", "postcode": "SE1 9RT", "submarket": "SE1"},
        ]
        master_merge.backfill_postcode_submarkets(records)
        self.assertEqual(records[1]["submarket"], "SE1")  # untouched

    def test_no_confirmed_name_leaves_the_row_completely_unchanged(self):
        records = [{"building": "B", "postcode": "SE1 9RT", "submarket": "SE1"}]
        updated, changes = master_merge.backfill_postcode_submarkets(records)
        self.assertEqual(updated, records)
        self.assertEqual(changes, [])

    def test_a_genuine_place_name_is_never_touched_or_reported_as_a_change(self):
        records = [
            {"building": "A", "postcode": "SE1 0AA", "submarket": "Bankside"},
            {"building": "B", "postcode": "SE1 9RT", "submarket": "Borough"},
        ]
        updated, changes = master_merge.backfill_postcode_submarkets(records)
        self.assertEqual(updated, records)
        self.assertEqual(changes, [])

    def test_disagreement_across_master_leaves_every_bare_row_unchanged(self):
        records = [
            {"building": "A", "postcode": "SE1 0AA", "submarket": "Bankside"},
            {"building": "B", "postcode": "SE1 1AA", "submarket": "Borough"},
            {"building": "C", "postcode": "SE1 9RT", "submarket": "SE1"},
        ]
        updated, changes = master_merge.backfill_postcode_submarkets(records)
        self.assertEqual(updated[2]["submarket"], "SE1")
        self.assertEqual(changes, [])


class BuildMergePlanPostcodeSubmarketTests(unittest.TestCase):
    """
    A fresh row's bare-postcode submarket ("SE1") is replaced with the one
    real name already confirmed elsewhere in master for that exact
    district - see build_postcode_submarket_lookup's own docstring for the
    full "never a guess" contract. Applied to new_row itself (not just a
    local dict) specifically so it reaches an UNMATCHED/brand-new-property
    row too, not only a matched row's diff.
    """

    def test_matched_row_gets_a_normal_reviewable_diff_not_a_silent_update(self):
        # old_rec's own submarket is blank (never yet resolved) while a
        # DIFFERENT confirmed row supplies the real name - the correction
        # must show up as a normal, reviewable "submarket changed" diff,
        # never routed into silent_updates the way brochure_link_broken
        # (pure diagnostics) is.
        master_df = _master_df([
            {"building": "Confirmed Elsewhere", "postcode": "SE1 0AA", "submarket": "Bankside"},
            {"building": "Existing Building", "postcode": "SE1 9RT", "submarket": None},
        ])
        new_row = ListingRow(building="Existing Building", postcode="SE1 9RT", submarket="SE1")

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = next(m for m in (plan.matched_changed + plan.matched_unchanged) if m.new_row.building == "Existing Building")
        self.assertEqual(matched.diffs.get("submarket"), (None, "Bankside"))
        self.assertNotIn("submarket", matched.silent_updates)

    def test_correction_is_sourced_from_a_different_row_than_the_one_it_matches(self):
        # The confirmed name doesn't have to come from the SAME building
        # this row matches - any confirmed row sharing the same postcode
        # district counts, per the lookup's own docstring.
        master_df = _master_df([
            {"building": "Confirmed Elsewhere", "postcode": "SE1 0AA", "submarket": "Borough"},
            {"building": "Existing Building", "postcode": "SE1 9RT", "submarket": None},
        ])
        new_row = ListingRow(building="Existing Building", postcode="SE1 9RT", submarket="SE1")

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = (plan.matched_changed + plan.matched_unchanged)[0]
        self.assertEqual(matched.diffs.get("submarket"), (None, "Borough"))

    def test_unmatched_brand_new_property_also_gets_corrected(self):
        # The real bug this guards against: correcting only a local dict
        # used for diffing would leave an UNMATCHED row's own new_row
        # object (what actually becomes the new master entry) untouched.
        master_df = _master_df([
            {"building": "Unrelated Building", "postcode": "SE1 9RT", "submarket": "Bankside"},
        ])
        new_row = ListingRow(building="Brand New Building", postcode="SE1 0AA", submarket="SE1")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.unmatched), 1)
        self.assertEqual(plan.unmatched[0].new_row.submarket, "Bankside")

    def test_disagreement_leaves_the_bare_postcode_completely_unchanged(self):
        master_df = _master_df([
            {"building": "A", "postcode": "SE1 9RT", "submarket": "Bankside"},
            {"building": "B", "postcode": "SE1 0AA", "submarket": "Borough"},
            {"building": "Existing Building", "postcode": "SE1 1AA", "submarket": "SE1"},
        ])
        new_row = ListingRow(building="Existing Building", postcode="SE1 1AA", submarket="SE1")

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = (plan.matched_changed + plan.matched_unchanged)[0]
        self.assertNotIn("submarket", matched.diffs)  # unchanged: still "SE1" on both sides

    def test_no_confirmed_mapping_at_all_leaves_the_bare_postcode_unchanged(self):
        master_df = _master_df([{"building": "Existing Building", "postcode": "SE1 9RT", "submarket": "SE1"}])
        new_row = ListingRow(building="Existing Building", postcode="SE1 9RT", submarket="SE1")

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = (plan.matched_changed + plan.matched_unchanged)[0]
        self.assertNotIn("submarket", matched.diffs)

    def test_a_genuine_place_name_submarket_is_never_touched(self):
        master_df = _master_df([
            {"building": "Existing Building", "postcode": "SE1 9RT", "submarket": "Bankside"},
        ])
        new_row = ListingRow(building="Existing Building", postcode="SE1 9RT", submarket="Shoreditch")

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = (plan.matched_changed + plan.matched_unchanged)[0]
        self.assertEqual(matched.diffs.get("submarket"), ("Bankside", "Shoreditch"))


class BuildSubmarketCasingLookupTests(unittest.TestCase):
    """
    build_submarket_casing_lookup - self-learning submarket casing
    correction, using today's real Workplace Plus data as the test
    fixtures (see that function's own docstring for the full contract).
    """

    def test_confirmed_mixed_case_wins_over_all_caps(self):
        records = [
            {"building": "A", "submarket": "MAYFAIR"},
            {"building": "B", "submarket": "Mayfair"},
        ]
        lookup = master_merge.build_submarket_casing_lookup(records)
        self.assertEqual(lookup.get("mayfair"), "Mayfair")

    def test_apostrophe_is_preserved_never_mangled_like_a_naive_title_case(self):
        # normalize_key strips the apostrophe from BOTH sides to match them
        # - the corrected VALUE itself is always copied verbatim from an
        # already-existing master row, never reconstructed via .title()
        # (which would turn "king's cross" into the wrong "King'S Cross").
        records = [
            {"building": "A", "submarket": "KING'S CROSS"},
            {"building": "B", "submarket": "King's Cross"},
        ]
        lookup = master_merge.build_submarket_casing_lookup(records)
        self.assertEqual(lookup.get("kings cross"), "King's Cross")
        self.assertNotIn("King'S Cross", lookup.values())

    def test_old_street_all_caps_resolves_to_the_confirmed_spelling(self):
        records = [
            {"building": "A", "submarket": "OLD STREET"},
            {"building": "B", "submarket": "Old Street"},
        ]
        lookup = master_merge.build_submarket_casing_lookup(records)
        self.assertEqual(lookup.get("old street"), "Old Street")

    def test_bank_monument_and_cannon_street_monument_never_collide(self):
        # Three genuinely different real areas, all from today's actual
        # data - must resolve to three distinct keys, never merged.
        records = [
            {"building": "A", "submarket": "BANK"},
            {"building": "B", "submarket": "Bank"},
            {"building": "C", "submarket": "MONUMENT"},
            {"building": "D", "submarket": "Monument"},
            {"building": "E", "submarket": "CANNON STREET/MONUMENT"},
            {"building": "F", "submarket": "Cannon Street/Monument"},
        ]
        lookup = master_merge.build_submarket_casing_lookup(records)
        self.assertEqual(lookup.get("bank"), "Bank")
        self.assertEqual(lookup.get("monument"), "Monument")
        self.assertEqual(lookup.get("cannon streetmonument"), "Cannon Street/Monument")
        # Confirms these are genuinely three separate dict entries, not
        # one collapsed key silently overwriting another.
        self.assertEqual(len({lookup["bank"], lookup["monument"], lookup["cannon streetmonument"]}), 3)

    def test_a_genuinely_new_area_with_only_all_caps_history_is_not_in_the_lookup(self):
        # This same real upload's own Manchester rows - a genuinely new
        # area never yet seen in any casing - must be absent entirely,
        # never guessed at via .title() or any other derivation.
        records = [{"building": "A", "submarket": "MANCHESTER CITY CENTRE"}]
        lookup = master_merge.build_submarket_casing_lookup(records)
        self.assertNotIn("manchester city centre", lookup)

    def test_all_lowercase_is_also_never_trusted_as_the_canonical_form(self):
        records = [{"building": "A", "submarket": "mayfair"}]
        lookup = master_merge.build_submarket_casing_lookup(records)
        self.assertNotIn("mayfair", lookup)

    def test_majority_vote_breaks_a_genuine_disagreement_between_two_good_castings(self):
        records = [
            {"building": "A", "submarket": "Kings Cross"},
            {"building": "B", "submarket": "Kings Cross"},
            {"building": "C", "submarket": "King's Cross"},
        ]
        lookup = master_merge.build_submarket_casing_lookup(records)
        self.assertEqual(lookup.get("kings cross"), "Kings Cross")

    def test_a_true_tie_breaks_alphabetically_for_a_deterministic_result(self):
        records = [
            {"building": "A", "submarket": "Kings Cross"},
            {"building": "B", "submarket": "King's Cross"},
        ]
        lookup = master_merge.build_submarket_casing_lookup(records)
        self.assertEqual(lookup.get("kings cross"), "King's Cross")  # "K" < "K" but "'" < "s" alphabetically

    def test_blank_submarket_never_contributes(self):
        records = [{"building": "A", "submarket": None}, {"building": "B", "submarket": ""}]
        lookup = master_merge.build_submarket_casing_lookup(records)
        self.assertEqual(lookup, {})


class BuildMergePlanSubmarketCasingTests(unittest.TestCase):
    """
    A fresh row's badly-cased submarket ("MAYFAIR") is corrected to
    whatever properly-cased spelling ("Mayfair") is already confirmed
    elsewhere in master - see build_submarket_casing_lookup's own
    docstring for the full contract, including why this is a SILENT
    update (formatting of an already-known fact) rather than a
    reviewable diff, unlike the postcode-driven submarket correction.
    """

    def test_matched_row_already_well_cased_in_master_is_fully_invisible(self):
        # old_rec already has the good casing - after correction the
        # fresh value is byte-identical to it, so there's no diff AND no
        # silent update at all; nothing to see, nothing changed.
        master_df = _master_df([{"building": "Existing Building", "submarket": "Mayfair"}])
        new_row = ListingRow(building="Existing Building", submarket="MAYFAIR")

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = (plan.matched_changed + plan.matched_unchanged)[0]
        self.assertNotIn("submarket", matched.diffs)
        self.assertNotIn("submarket", matched.silent_updates)

    def test_matched_row_still_badly_cased_in_master_gets_a_silent_update(self):
        # old_rec is ALSO still "MAYFAIR" (never yet fixed) - a DIFFERENT
        # master row confirms "Mayfair" - the correction must land in
        # silent_updates, never as a reviewable diff a human has to
        # approve, since this is only formatting of an already-known fact.
        master_df = _master_df([
            {"building": "Confirmed Elsewhere", "submarket": "Mayfair"},
            {"building": "Existing Building", "submarket": "MAYFAIR"},
        ])
        new_row = ListingRow(building="Existing Building", submarket="MAYFAIR")

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = next(m for m in (plan.matched_changed + plan.matched_unchanged) if m.new_row.building == "Existing Building")
        self.assertNotIn("submarket", matched.diffs)
        self.assertEqual(matched.silent_updates.get("submarket"), "Mayfair")

    def test_unmatched_brand_new_property_also_gets_corrected(self):
        master_df = _master_df([{"building": "Unrelated Building", "submarket": "Old Street"}])
        new_row = ListingRow(building="Brand New Building", submarket="OLD STREET")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.unmatched), 1)
        self.assertEqual(plan.unmatched[0].new_row.submarket, "Old Street")

    def test_a_genuinely_new_area_stays_untouched_no_crash(self):
        # Today's real Manchester rows from the same Workplace Plus file -
        # never before seen in any casing - must pass through unchanged,
        # not raise, not get guessed at.
        master_df = _master_df([{"building": "Unrelated Building", "submarket": "Mayfair"}])
        new_row = ListingRow(building="Brand New Manchester Building", submarket="MANCHESTER CITY CENTRE")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.unmatched), 1)
        self.assertEqual(plan.unmatched[0].new_row.submarket, "MANCHESTER CITY CENTRE")

    def test_cannon_street_monument_is_never_confused_with_monument_alone(self):
        # Real distinctness check through the FULL pipeline, not just the
        # lookup in isolation - a fresh "CANNON STREET/MONUMENT" upload
        # must never be corrected using "Monument"'s own confirmed casing.
        master_df = _master_df([
            {"building": "A", "submarket": "Monument"},
            {"building": "B", "submarket": "Cannon Street/Monument"},
            {"building": "Existing Building", "submarket": "CANNON STREET/MONUMENT"},
        ])
        new_row = ListingRow(building="Existing Building", submarket="CANNON STREET/MONUMENT")

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = next(m for m in (plan.matched_changed + plan.matched_unchanged) if m.new_row.building == "Existing Building")
        self.assertEqual(matched.new_row.submarket, "Cannon Street/Monument")

    def test_a_genuine_new_submarket_fact_from_the_source_is_still_a_normal_diff(self):
        # A real, substantive change the source itself states (not a
        # casing artifact) must still surface as an ordinary reviewable
        # diff - casing correction must never swallow a genuine change.
        master_df = _master_df([
            {"building": "Existing Building", "submarket": "Mayfair"},
        ])
        new_row = ListingRow(building="Existing Building", submarket="Soho")

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = (plan.matched_changed + plan.matched_unchanged)[0]
        self.assertEqual(matched.diffs.get("submarket"), ("Mayfair", "Soho"))


class ItemsSimilarTests(unittest.TestCase):
    """_items_similar's exact-match short-circuit - a short, abbreviation/
    number-heavy item (every token <= 2 chars) must still be recognized as
    similar to an identical restatement of itself, even though its own
    significant-words set is empty."""

    def test_identical_short_abbreviation_item_is_similar_to_itself(self):
        self.assertTrue(master_merge._items_similar("4 mr + 3 pb", "4 mr + 3 pb"))

    def test_identical_item_similar_regardless_of_case_and_whitespace(self):
        self.assertTrue(master_merge._items_similar("4 MR + 3 PB", "4   mr +   3 pb"))

    def test_different_short_items_are_not_similar(self):
        self.assertFalse(master_merge._items_similar("4 mr + 3 pb", "5 mr + 2 pb"))

    def test_normal_reworded_items_still_work_as_before(self):
        self.assertTrue(master_merge._items_similar(
            "large private terrace landscaped with plants", "private landscaped terrace",
        ))

    def test_unrelated_normal_length_items_are_not_similar(self):
        self.assertFalse(master_merge._items_similar("manned reception desk", "bike storage available"))


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

    def test_house_number_widened_into_a_range_is_flagged(self):
        # The reported scenario: master's plain house number silently
        # becomes an update's hyphenated range sharing that same number as
        # an endpoint - a real, easy-to-miss change in what the address
        # actually covers, not a formatting difference.
        master_df = _master_df([{
            "building": "1 Example Street",
            "provider": "Test Provider",
            "floor_unit": "1st Floor",
            "postcode": "EC1A 1AA",
            "address_1": "18 Copthall Avenue",
        }])
        new_row = ListingRow(
            building="1 Example Street",
            provider="Test Provider",
            floor_unit="1st Floor",
            postcode="EC1A 1AA",
            address_1="14-18 Copthall Avenue",
        )

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = plan.matched_changed[0]
        self.assertIn("address_1", matched.diffs)
        self.assertIn("address_1", matched.risky_fields)

    def test_house_number_narrowed_from_a_range_is_also_flagged(self):
        # Same change, opposite direction - a range collapsing down to one
        # of its own endpoints is just as much a structural change as the
        # reverse, and must not get a pass just because the reported
        # direction was widening.
        master_df = _master_df([{
            "building": "1 Example Street",
            "provider": "Test Provider",
            "floor_unit": "1st Floor",
            "postcode": "EC1A 1AA",
            "address_1": "14-18 Copthall Avenue",
        }])
        new_row = ListingRow(
            building="1 Example Street",
            provider="Test Provider",
            floor_unit="1st Floor",
            postcode="EC1A 1AA",
            address_1="18 Copthall Avenue",
        )

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = plan.matched_changed[0]
        self.assertIn("address_1", matched.diffs)
        self.assertIn("address_1", matched.risky_fields)

    def test_address_change_that_leaves_house_number_untouched_is_not_flagged(self):
        # A postcode-fix-flavored correction inside address_1 itself (the
        # house number at the front is identical) is exactly the ordinary,
        # safe update this feature must keep auto-applying - only a genuine
        # house-number change should route to manual review.
        master_df = _master_df([{
            "building": "1 Example Street",
            "provider": "Test Provider",
            "floor_unit": "1st Floor",
            "postcode": "EC1A 1AA",
            "address_1": "18 Copthall Avenue, EC2R 7DJ",
        }])
        new_row = ListingRow(
            building="1 Example Street",
            provider="Test Provider",
            floor_unit="1st Floor",
            postcode="EC1A 1AA",
            address_1="18 Copthal Avenue, EC2R 7DJ",
        )

        plan = master_merge.build_merge_plan([new_row], master_df)

        matched = plan.matched_changed[0]
        self.assertIn("address_1", matched.diffs)
        self.assertEqual(matched.risky_fields, frozenset())


class HouseNumberChangedTests(unittest.TestCase):
    def test_plain_number_vs_range_sharing_an_endpoint_differs(self):
        self.assertTrue(master_merge.house_number_changed("18 Copthall Avenue", "14-18 Copthall Avenue"))
        self.assertTrue(master_merge.house_number_changed("14-18 Copthall Avenue", "18 Copthall Avenue"))

    def test_different_number_entirely_differs(self):
        self.assertTrue(master_merge.house_number_changed("18 Copthall Avenue", "24 Copthall Avenue"))

    def test_number_appearing_where_there_was_none_differs(self):
        self.assertTrue(master_merge.house_number_changed("Copthall House", "18 Copthall House"))

    def test_same_number_is_not_flagged(self):
        self.assertFalse(master_merge.house_number_changed("18 Copthall Avenue", "18 Copthall Avenue"))

    def test_case_and_whitespace_only_differences_are_not_flagged(self):
        self.assertFalse(master_merge.house_number_changed("18 Copthall Avenue", " 18 copthall avenue"))
        self.assertFalse(master_merge.house_number_changed("56A Example Street", "56a Example Street"))

    def test_no_leading_number_on_either_side_is_not_flagged(self):
        self.assertFalse(master_merge.house_number_changed("Copthall House", "Copthall Building"))


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

    def test_byte_identical_collision_all_fields_auto_resolvable(self):
        # The real reported case: two sheets of the same Copthall Estates
        # workbook (a portfolio-wide rollup and that provider's own per-
        # building detail sheet) both extracted "Copthall House" - 4th
        # Floor - with byte-identical values for every changed field. This
        # is a genuine plan.collisions entry (write order must never
        # silently pick a winner), but every field should be auto-
        # resolvable via matched_collision_field_choice since the two
        # sources don't actually disagree on anything.
        master_df = _master_df([{"building": "Copthall House", "provider": "Copthall Estates", "floor_unit": "4th Floor"}])
        rollup_row = ListingRow(
            building="Copthall House", provider="Copthall Estates", floor_unit="4th Floor",
            address_1="1 Copthall Avenue", submarket="City", special_features="Air conditioning; 24hr access",
            contacts="Jane Doe, jane@example.com", source_file="Copthall.xlsx — Portfolio",
        )
        detail_row = ListingRow(
            building="Copthall House", provider="Copthall Estates", floor_unit="4th Floor",
            address_1="1 Copthall Avenue", submarket="City", special_features="Air conditioning; 24hr access",
            contacts="Jane Doe, jane@example.com", source_file="Copthall.xlsx — City Detail",
        )

        plan = master_merge.build_merge_plan([rollup_row, detail_row], master_df)

        self.assertEqual(len(plan.collisions), 1)
        group = plan.collisions[0]
        self.assertEqual(len(group), 2)
        for f in master_merge.collision_group_fields(group):
            values = [m.new_row.model_dump()[f] for m in group]
            needs_choice, _ = master_merge.matched_collision_field_choice(values)
            self.assertFalse(needs_choice, f"field {f!r} should not need a manual choice")

    def test_collision_with_one_genuine_disagreement_only_that_field_needs_a_choice(self):
        master_df = _master_df([{"building": "Copthall House", "provider": "Copthall Estates", "floor_unit": "4th Floor"}])
        row_a = ListingRow(
            building="Copthall House", provider="Copthall Estates", floor_unit="4th Floor",
            address_1="1 Copthall Avenue", state_of_space="Cat A",
        )
        row_b = ListingRow(
            building="Copthall House", provider="Copthall Estates", floor_unit="4th Floor",
            address_1="1 Copthall Avenue", state_of_space="Fitted",
        )

        plan = master_merge.build_merge_plan([row_a, row_b], master_df)
        group = plan.collisions[0]
        fields = master_merge.collision_group_fields(group)

        results = {
            f: master_merge.matched_collision_field_choice([m.new_row.model_dump()[f] for m in group])
            for f in fields
        }
        self.assertFalse(results["address_1"][0])
        self.assertTrue(results["state_of_space"][0])


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

    def test_displayed_positions_none_behaves_exactly_like_before(self):
        master_records = self._master_records([
            {"building": "A", "provider": "P1", "size_sqft": 1000.0},
            {"building": "B", "provider": "P2", "size_sqft": 2000.0},
        ])
        edited_rows = {1: {"size_sqft": 2500.0}}

        merged_rows, diff_rows, fields_changed = master_merge.build_manual_edit(
            master_records, edited_rows, displayed_positions=None
        )

        self.assertEqual(merged_rows[1].size_sqft, 2500.0)
        self.assertEqual(fields_changed, 1)

    def test_edit_at_a_filtered_position_updates_the_correct_real_row(self):
        # A text filter on the master table (see pages/2_Review_and_Master.
        # py's _render_master_table) has narrowed what's displayed down to
        # real rows 1 and 3 only - the widget itself only ever sees 2 rows,
        # so data_editor's own edited_rows key "1" means the SECOND VISIBLE
        # row (real position 3), never real position 1. Without this
        # translation, a naive int(row_pos) would silently edit Building B
        # (real position 1) instead of the row the reviewer actually saw
        # and edited (Building D, real position 3).
        master_records = self._master_records([
            {"building": "Building A", "provider": "P1", "size_sqft": 1000.0},
            {"building": "Building B", "provider": "P1", "size_sqft": 2000.0},
            {"building": "Building C", "provider": "P1", "size_sqft": 3000.0},
            {"building": "Building D", "provider": "P1", "size_sqft": 4000.0},
        ])
        displayed_positions = [1, 3]  # filtered view shows only real rows 1 and 3, in that order
        edited_rows = {"1": {"size_sqft": 4500.0}}  # the SECOND visible row was edited

        merged_rows, diff_rows, fields_changed = master_merge.build_manual_edit(
            master_records, edited_rows, displayed_positions=displayed_positions
        )

        self.assertEqual(fields_changed, 1)
        self.assertEqual(merged_rows[3].size_sqft, 4500.0)  # Building D - the real row that was edited
        self.assertEqual(merged_rows[1].size_sqft, 2000.0)  # Building B - untouched, despite sharing visual position 1
        self.assertEqual(diff_rows[0]["property"], master_merge.row_label(master_records[3]))

    def test_edit_at_the_first_filtered_position_still_resolves_correctly(self):
        master_records = self._master_records([
            {"building": "Building A", "provider": "P1", "size_sqft": 1000.0},
            {"building": "Building B", "provider": "P1", "size_sqft": 2000.0},
            {"building": "Building C", "provider": "P1", "size_sqft": 3000.0},
        ])
        displayed_positions = [2]  # filter matched only real row 2
        edited_rows = {"0": {"size_sqft": 3500.0}}

        merged_rows, diff_rows, fields_changed = master_merge.build_manual_edit(
            master_records, edited_rows, displayed_positions=displayed_positions
        )

        self.assertEqual(merged_rows[2].size_sqft, 3500.0)
        self.assertEqual(merged_rows[0].size_sqft, 1000.0)


class MergeSelectedPropertyIdsTests(unittest.TestCase):
    """export_selected_property_ids' own update rule across a (possibly
    filtered) render of the master table - see pages/2_Review_and_Master.
    py's _render_master_table. A property_id's checkbox state is only
    authoritative while its row is actually visible; one selected before a
    filter narrowed the view must survive, not be silently dropped just
    because it isn't on screen to uncheck right now."""

    def test_visible_selection_is_added(self):
        result = master_merge.merge_selected_property_ids(set(), {"a", "b"}, {"a"})
        self.assertEqual(result, {"a"})

    def test_visible_deselection_is_removed(self):
        result = master_merge.merge_selected_property_ids({"a"}, {"a", "b"}, set())
        self.assertEqual(result, set())

    def test_selection_outside_the_current_filter_survives(self):
        # "c" was selected before the filter narrowed the view to just
        # {"a", "b"} - it must still be selected after this render, even
        # though its own checkbox isn't on screen to reaffirm it.
        result = master_merge.merge_selected_property_ids({"c"}, {"a", "b"}, set())
        self.assertEqual(result, {"c"})

    def test_visible_and_off_filter_selections_combine(self):
        result = master_merge.merge_selected_property_ids({"c"}, {"a", "b"}, {"a"})
        self.assertEqual(result, {"a", "c"})


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

    def test_workplace_plus_u_o_abbreviation(self):
        # Workplace Plus's own real spreadsheet uses the literal
        # abbreviation "U/O" instead of ever spelling "Under Offer" out
        # (confirmed present in 14 real rows) - the word-boundary match on
        # "under offer" alone never catches this on its own.
        self.assertTrue(master_merge.mentions_let_status("U/O"))
        self.assertTrue(master_merge.mentions_let_status("u/o"))
        self.assertTrue(master_merge.mentions_let_status("Status: U/O"))
        self.assertTrue(master_merge.mentions_let_status("(U/O)"))

    def test_u_o_word_boundary_does_not_match_inside_an_unrelated_token(self):
        # "u"/"o" with no real boundary on the relevant side must not
        # register as this abbreviation just because the 3-character
        # sequence appears as a substring.
        self.assertFalse(master_merge.mentions_let_status("flu/office refurbishment"))
        self.assertFalse(master_merge.mentions_let_status("u/officeplan"))

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


class MatchedLetStatusPhrasesTests(unittest.TestCase):
    """matched_let_status_phrases/let_status_display_text - the display-
    only follow-up: a decision prompt shows just the trigger phrase(s),
    never a flagged field's entire text, built on the exact same match
    logic mentions_let_status itself uses (see _let_status_matches)."""

    def test_returns_the_single_matched_phrase(self):
        self.assertEqual(master_merge.matched_let_status_phrases("Under Offer"), ["Under Offer"])

    def test_exact_source_casing_and_punctuation_is_preserved_not_lowered(self):
        # The real gap this closes: a flagged field's own trigger phrase
        # buried in a long amenity list, in its own real casing - "U/O"
        # must come back exactly as "U/O", never lowercased "u/o".
        phrases = master_merge.matched_let_status_phrases(
            "52 + 2 MR + 3 PB + BR; U/O; term 2 - 5 years",
        )
        self.assertEqual(phrases, ["U/O"])

    def test_multiple_distinct_matches_are_all_returned(self):
        phrases = master_merge.matched_let_status_phrases("Let; also U/O and Withdrawn")
        self.assertEqual(phrases, ["Let", "U/O", "Withdrawn"])

    def test_a_repeated_phrase_is_returned_once_not_duplicated(self):
        phrases = master_merge.matched_let_status_phrases("Let; some notes; Let again")
        self.assertEqual(phrases, ["Let"])

    def test_no_match_returns_an_empty_list(self):
        self.assertEqual(master_merge.matched_let_status_phrases("Bike racks; showers"), [])
        self.assertEqual(master_merge.matched_let_status_phrases(None), [])

    def test_pre_let_style_exclusions_still_apply_to_phrase_extraction(self):
        # The exact same "let" exclusion mentions_let_status itself uses -
        # a "pre-let"/"re-let"/"sub-let" compound must never be extracted
        # as a trigger phrase either.
        self.assertEqual(master_merge.matched_let_status_phrases("80% pre-let at Elsley"), [])

    def test_display_text_joins_multiple_phrases(self):
        text = master_merge.let_status_display_text("Let; also U/O and Withdrawn")
        self.assertEqual(text, "Let; U/O; Withdrawn")

    def test_display_text_shows_only_the_phrase_not_the_full_field(self):
        text = master_merge.let_status_display_text(
            "52 + 2 MR + 3 PB + BR; U/O; term 2 - 5 years",
        )
        self.assertEqual(text, "U/O")

    def test_display_text_falls_back_to_the_full_original_text_when_nothing_matches(self):
        # The safety net: display code must never surface a blank/broken
        # message even in the (should-never-happen) case of no match.
        text = master_merge.let_status_display_text("Bike racks; showers")
        self.assertEqual(text, "Bike racks; showers")


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

    def test_workplace_plus_u_o_value_flags_the_matched_row(self):
        # The exact real gap this closes: Workplace Plus's own re-upload
        # states "U/O" (never the spelled-out "Under Offer") for a property
        # already in master - this must trigger the same review prompt.
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "Workplace Plus", "floor_unit": "1st Floor",
            "postcode": "EC1A 1AA", "special_features": "Bike racks; showers",
        }])
        new_row = ListingRow(
            building="1 Example Street", provider="Workplace Plus", floor_unit="1st Floor",
            postcode="EC1A 1AA", special_features="U/O",
        )

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed), 1)
        matched = plan.matched_changed[0]
        self.assertIn("special_features", matched.let_status_fields)
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

    def test_new_unmatched_property_with_let_status_wording_is_flagged_too(self):
        # Real gap this closes: a brand-new listing (no master match at
        # all) whose own text ALREADY says "Let"/"Under Offer"/etc. is
        # exactly as much in need of a human decision as an existing
        # property just updated to say so - UnmatchedRow now carries its
        # own let_status_fields (see _new_row_let_status_fields), computed
        # directly from the row's own text since there's no before/after
        # pair to diff against. Still correctly lands in plan.unmatched,
        # never plan.matched_changed - only WHICH bucket carries the flag
        # changed, not the matching outcome itself.
        master_df = _master_df([{"building": "Somewhere Else", "provider": "Other Provider"}])
        new_row = ListingRow(building="1 Example Street", provider="Test Provider", special_features="Let")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed), 0)
        self.assertEqual(len(plan.unmatched), 1)
        self.assertIs(plan.unmatched[0].new_row, new_row)
        self.assertIn("special_features", plan.unmatched[0].let_status_fields)

    def test_new_unmatched_property_without_let_status_wording_is_not_flagged(self):
        master_df = _master_df([{"building": "Somewhere Else", "provider": "Other Provider"}])
        new_row = ListingRow(building="1 Example Street", provider="Test Provider", special_features="Bike racks")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(plan.unmatched[0].let_status_fields, frozenset())


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


class FindStaleCandidatesTests(unittest.TestCase):
    """master_merge.find_stale_candidates - surfaces existing master rows a
    complete-snapshot provider's own upload gives real, scoped evidence are
    no longer available, without ever punishing a partial (one-area/one-
    building) upload from any provider - see COMPLETE_SNAPSHOT_PROVIDERS'
    own docstring."""

    def _master_records(self, rows: list[dict]) -> list[dict]:
        return [ListingRow(**r).model_dump() for r in rows]

    def test_stale_unit_from_a_complete_snapshot_is_surfaced(self):
        # The real example given: latest file has 50 Gresham Street - 2nd
        # Floor; master still has an older 50 Gresham Street - 3rd Floor.
        master_records = self._master_records([
            {"building": "50 Gresham Street", "provider": "Copthall Estates", "floor_unit": "2nd Floor"},
            {"building": "50 Gresham Street", "provider": "Copthall Estates", "floor_unit": "3rd Floor"},
        ])
        new_rows = [
            ListingRow(building="50 Gresham Street", provider="Copthall Estates", floor_unit="2nd Floor"),
        ]

        stale = master_merge.find_stale_candidates(new_rows, master_records, matched_master_indices={0})

        self.assertEqual(stale, [1])

    def test_current_unit_is_not_marked_stale(self):
        master_records = self._master_records([
            {"building": "50 Gresham Street", "provider": "Copthall Estates", "floor_unit": "2nd Floor"},
        ])
        new_rows = [
            ListingRow(building="50 Gresham Street", provider="Copthall Estates", floor_unit="2nd Floor"),
        ]

        stale = master_merge.find_stale_candidates(new_rows, master_records, matched_master_indices={0})

        self.assertEqual(stale, [])

    def test_already_matched_master_row_is_never_flagged(self):
        # Even if it would otherwise qualify (different floor_unit reported
        # this time, say) - a row this exact batch matched obviously still
        # exists; matched_master_indices always wins.
        master_records = self._master_records([
            {"building": "50 Gresham Street", "provider": "Copthall Estates", "floor_unit": "3rd Floor"},
        ])
        new_rows = [
            ListingRow(building="50 Gresham Street", provider="Copthall Estates", floor_unit="2nd Floor"),
        ]

        stale = master_merge.find_stale_candidates(new_rows, master_records, matched_master_indices={0})

        self.assertEqual(stale, [])

    def test_building_never_mentioned_by_this_upload_is_not_flagged(self):
        # The critical safety case: this upload only covers ONE building for
        # Copthall (a partial upload even from an allow-listed provider) -
        # a completely different building's existing floor must never be
        # treated as evidence of anything.
        master_records = self._master_records([
            {"building": "11 Cursitor Street", "provider": "Copthall Estates", "floor_unit": "Ground Floor"},
        ])
        new_rows = [
            ListingRow(building="50 Gresham Street", provider="Copthall Estates", floor_unit="2nd Floor"),
        ]

        stale = master_merge.find_stale_candidates(new_rows, master_records, matched_master_indices=set())

        self.assertEqual(stale, [])

    def test_other_providers_partial_upload_never_flags_anything(self):
        # A provider not on COMPLETE_SNAPSHOT_PROVIDERS uploading a subset
        # of their portfolio must never have absent rows treated as stale,
        # even when the building IS mentioned in this batch.
        master_records = self._master_records([
            {"building": "16 Dufour's Place", "provider": "GPE", "floor_unit": "3rd Floor"},
        ])
        new_rows = [
            ListingRow(building="16 Dufour's Place", provider="GPE", floor_unit="2nd Floor"),
        ]

        stale = master_merge.find_stale_candidates(new_rows, master_records, matched_master_indices=set())

        self.assertEqual(stale, [])

    def test_fully_occupied_building_flags_every_existing_floor(self):
        # A building marked Fully Occupied produces zero ListingRows, so
        # covered_buildings/covered_units alone would never catch it - this
        # is exactly what fully_occupied_buildings exists for.
        master_records = self._master_records([
            {"building": "27 Lime Street", "provider": "Copthall Estates", "floor_unit": "Ground Floor"},
            {"building": "27 Lime Street", "provider": "Copthall Estates", "floor_unit": "1st Floor"},
        ])
        fully_occupied = [{"provider": "Copthall Estates", "building": "27 Lime Street"}]

        stale = master_merge.find_stale_candidates(
            [], master_records, matched_master_indices=set(), fully_occupied_buildings=fully_occupied,
        )

        self.assertEqual(stale, [0, 1])

    def test_fully_occupied_building_for_a_different_provider_is_not_flagged(self):
        master_records = self._master_records([
            {"building": "27 Lime Street", "provider": "GPE", "floor_unit": "Ground Floor"},
        ])
        fully_occupied = [{"provider": "Copthall Estates", "building": "27 Lime Street"}]

        stale = master_merge.find_stale_candidates(
            [], master_records, matched_master_indices=set(), fully_occupied_buildings=fully_occupied,
        )

        self.assertEqual(stale, [])

    def test_provider_name_variants_still_match_via_canonicalization(self):
        # "Copthall Estates Availability" (a real sheet-title-derived
        # provider string, see _strip_provider_purpose_suffix) must still be
        # recognized as the same allow-listed provider.
        master_records = self._master_records([
            {"building": "50 Gresham Street", "provider": "Copthall Estates", "floor_unit": "3rd Floor"},
        ])
        new_rows = [
            ListingRow(building="50 Gresham Street", provider="Copthall Estates Availability", floor_unit="2nd Floor"),
        ]

        stale = master_merge.find_stale_candidates(new_rows, master_records, matched_master_indices=set())

        self.assertEqual(stale, [0])


class ConfidentSameProviderAutoUpdateTests(unittest.TestCase):
    """
    The updated-provider workflow: for a row confidently matched to an
    existing master property/unit (same provider, same normalized building
    + floor_unit), an ordinary scalar/current-state change should already
    auto-apply with no manual click - see build_merge_plan's own
    auto_accept-eligible risky_fields computation (pages/2_Review_and_
    Master.py's auto_accept mode already applies anything NOT in
    risky_fields without a per-field decision). These tests prove that's
    true for every DIFF_FIELDS category the brief calls out, not just
    special_features/contacts - no new logic was needed for any of these,
    since matching already requires provider+building+floor_unit/postcode
    agreement and diff_fields already treats a new non-blank value as a
    genuine update, blank as "no new information".
    """

    def _matched(self, master_row: dict, new_row: ListingRow):
        master_df = _master_df([master_row])
        plan = master_merge.build_merge_plan([new_row], master_df)
        self.assertEqual(len(plan.matched_changed), 1, "row did not confidently match master")
        return plan.matched_changed[0]

    def test_new_rent_replaces_old_rent(self):
        matched = self._matched(
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor", "rent_pcm": 5000.0},
            ListingRow(building="1 Example Street", provider="UNION", floor_unit="3rd Floor", rent_pcm=5500.0),
        )
        self.assertEqual(matched.diffs["rent_pcm"], (5000.0, 5500.0))
        self.assertEqual(matched.risky_fields, frozenset())

    def test_new_size_replaces_old_size(self):
        matched = self._matched(
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor", "size_sqft": 1000.0},
            ListingRow(building="1 Example Street", provider="UNION", floor_unit="3rd Floor", size_sqft=1200.0),
        )
        self.assertEqual(matched.diffs["size_sqft"], (1000.0, 1200.0))
        self.assertEqual(matched.risky_fields, frozenset())

    def test_new_desks_replaces_old_desks(self):
        matched = self._matched(
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor", "desks_max": 10},
            ListingRow(building="1 Example Street", provider="UNION", floor_unit="3rd Floor", desks_max=14),
        )
        self.assertEqual(matched.diffs["desks_max"], (10, 14))
        self.assertEqual(matched.risky_fields, frozenset())

    def test_fully_fitted_to_cat_a_results_in_cat_a(self):
        matched = self._matched(
            {
                "building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor",
                "state_of_space": "Fully Fitted",
            },
            ListingRow(
                building="1 Example Street", provider="UNION", floor_unit="3rd Floor", state_of_space="CAT A",
            ),
        )
        self.assertEqual(matched.diffs["state_of_space"], ("Fully Fitted", "CAT A"))
        self.assertEqual(matched.risky_fields, frozenset())

    def test_cat_a_to_fully_fitted_results_in_fully_fitted(self):
        matched = self._matched(
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor", "state_of_space": "CAT A"},
            ListingRow(
                building="1 Example Street", provider="UNION", floor_unit="3rd Floor", state_of_space="Fully Fitted",
            ),
        )
        self.assertEqual(matched.diffs["state_of_space"], ("CAT A", "Fully Fitted"))
        self.assertEqual(matched.risky_fields, frozenset())

    def test_blank_new_scalar_does_not_erase_old_nonblank_value(self):
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor", "rent_pcm": 5000.0,
        }])
        new_row = ListingRow(building="1 Example Street", provider="UNION", floor_unit="3rd Floor", rent_pcm=None)

        plan = master_merge.build_merge_plan([new_row], master_df)

        # No change at all - a blank new value contributes nothing, so this
        # row lands in matched_unchanged, not matched_changed.
        self.assertEqual(len(plan.matched_changed), 0)
        self.assertEqual(len(plan.matched_unchanged), 1)

    def test_old_blank_new_nonblank_scalar_updates(self):
        matched = self._matched(
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor", "rent_pcm": None},
            ListingRow(building="1 Example Street", provider="UNION", floor_unit="3rd Floor", rent_pcm=4200.0),
        )
        self.assertEqual(matched.diffs["rent_pcm"], (None, 4200.0))
        self.assertEqual(matched.risky_fields, frozenset())


class SpecialFeaturesMergeTests(unittest.TestCase):
    """
    merge_compatible_text/build_merge_plan's new auto-merge behavior for
    special_features - preserves compatible information from BOTH sources
    instead of either a blind overwrite (loses old detail) or forcing a
    manual click for an ordinary, safely-mergeable update.
    """

    def _matched_special_features(self, old_val, new_val):
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor",
            "special_features": old_val,
        }])
        new_row = ListingRow(
            building="1 Example Street", provider="UNION", floor_unit="3rd Floor", special_features=new_val,
        )
        plan = master_merge.build_merge_plan([new_row], master_df)
        return plan.matched_changed[0]

    def test_old_blank_new_text_is_used(self):
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor", "special_features": None,
        }])
        new_row = ListingRow(
            building="1 Example Street", provider="UNION", floor_unit="3rd Floor",
            special_features="Private terrace",
        )
        plan = master_merge.build_merge_plan([new_row], master_df)
        matched = plan.matched_changed[0]
        self.assertEqual(matched.diffs["special_features"], (None, "Private terrace"))
        self.assertEqual(matched.risky_fields, frozenset())

    def test_new_blank_preserves_old(self):
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor",
            "special_features": "Private terrace",
        }])
        new_row = ListingRow(
            building="1 Example Street", provider="UNION", floor_unit="3rd Floor", special_features=None,
        )
        plan = master_merge.build_merge_plan([new_row], master_df)
        self.assertEqual(len(plan.matched_changed), 0)
        self.assertEqual(plan.matched_unchanged[0].new_row.special_features, None)

    def test_compatible_additions_are_merged_without_duplication(self):
        # The brief's own example: neither side's comma-joined phrase
        # disproves the other's, so both facts survive, and the shared
        # "Private terrace" phrase isn't repeated.
        matched = self._matched_special_features(
            "Private terrace, 10-person boardroom", "Private terrace, newly fitted kitchen",
        )
        self.assertNotIn("special_features", matched.risky_fields)
        merged = matched.diffs["special_features"][1]
        self.assertIn("newly fitted kitchen", merged)
        self.assertIn("10-person boardroom", merged)
        self.assertEqual(merged.lower().count("private terrace"), 1)

    def test_semicolon_itemized_compatible_addition_is_merged(self):
        matched = self._matched_special_features(
            "Bike racks; showers", "Bike racks; showers; new rooftop terrace",
        )
        self.assertNotIn("special_features", matched.risky_fields)
        # Already-compatible (is_detail_loss was False here to begin with -
        # see IsDetailLossTests) - new_val already stood alone correctly.
        self.assertEqual(matched.diffs["special_features"][1], "Bike racks; showers; new rooftop terrace")

    def test_shorter_generic_new_value_does_not_destroy_richer_old_detail(self):
        # is_richness_regression still gates this - a drastic compression
        # stays manual review, unmerged, exactly as before this feature
        # existed (must not silently reduce to just "Fully fitted").
        matched = self._matched_special_features(
            "12-person boardroom, 3 meeting rooms, breakout area, fitted kitchen", "Fully fitted",
        )
        self.assertIn("special_features", matched.risky_fields)
        self.assertEqual(
            matched.diffs["special_features"],
            ("12-person boardroom, 3 meeting rooms, breakout area, fitted kitchen", "Fully fitted"),
        )

    def test_genuine_contradiction_is_not_concatenated(self):
        # New explicitly negates an old fact about the same topic (shares
        # "reception" but scores nowhere near _items_similar's own
        # reword threshold) - the old claim must be dropped, not kept
        # alongside its own contradiction.
        matched = self._matched_special_features("Manned reception desk", "Reception no longer staffed")
        merged = matched.diffs["special_features"][1]
        self.assertEqual(merged, "Reception no longer staffed")
        self.assertNotIn("Manned reception desk".lower(), merged.lower())

    def test_competing_availability_status_is_not_concatenated(self):
        # Real bug found against a real Knotel availability update: an old
        # "Available: Now" and a new "Under Offer" share NO significant
        # word at all (so the topic-overlap-based negation check above
        # never fires for them), but these are still two competing claims
        # about the same thing, not two independent facts - before
        # _is_availability_statement existed, this concatenated into a
        # nonsensical "Under Offer; Available: Now".
        matched = self._matched_special_features("Available: Now", "Under Offer")
        self.assertEqual(matched.diffs["special_features"][1], "Under Offer")

    def test_new_availability_date_replacing_old_is_not_concatenated(self):
        matched = self._matched_special_features("Available: Now", "Let")
        self.assertEqual(matched.diffs["special_features"][1], "Let")

    def test_reworded_same_fact_is_not_duplicated(self):
        matched = self._matched_special_features(
            "Benefits from a large private terrace landscaped with plants, trees and premium Italian outdoor furniture",
            "Private landscaped terrace",
        )
        # is_richness_regression fires here too (ratio 0.20, a known
        # accepted case - see that constant's own docstring) - stays
        # manual review, unmerged, same as the "Fully fitted" case above.
        self.assertIn("special_features", matched.risky_fields)

    def test_available_now_to_available_december_auto_updates(self):
        # The real confirmed case: 44 Pentonville Road (MetSpace). Before
        # the _items_similar fix, "4 MR + 3 PB" (every token <= 2 chars,
        # filtered to an empty significant-words set) couldn't be
        # recognized as restating itself, wrongly triggering is_detail_loss
        # and forcing this into manual review.
        matched = self._matched_special_features(
            "4 MR + 3 PB; Available: Now", "4 MR + 3 PB; Available: December",
        )
        self.assertNotIn("special_features", matched.risky_fields)
        self.assertEqual(matched.diffs["special_features"][1], "4 MR + 3 PB; Available: December")

    def test_unchanged_short_feature_item_is_not_duplicated(self):
        # Directly proves the duplication bug is gone: the shared item must
        # appear exactly once in the result, not twice.
        matched = self._matched_special_features(
            "4 MR + 3 PB; Available: Now", "4 MR + 3 PB; Available: December",
        )
        merged = matched.diffs["special_features"][1]
        self.assertEqual(merged.lower().count("4 mr + 3 pb"), 1)

    def test_result_is_equivalent_to_the_expected_clean_value(self):
        matched = self._matched_special_features(
            "4 MR + 3 PB; Available: Now", "4 MR + 3 PB; Available: December",
        )
        self.assertEqual(matched.diffs["special_features"][1], "4 MR + 3 PB; Available: December")

    def test_brochure_link_change_is_never_risky_alongside_the_auto_update(self):
        # brochure_link is not in DETAIL_LOSS_MERGE_FIELDS at all - a real
        # link change alongside the special_features update above must
        # leave the whole row with zero risky_fields, landing it in
        # Automatic updates rather than Needs your decision.
        master_df = _master_df([{
            "building": "44 Pentonville Road", "provider": "MetSpace", "floor_unit": None,
            "brochure_link": "https://drive.google.com/file/d/OLDID/view",
            "special_features": "4 MR + 3 PB; Available: Now",
        }])
        new_row = ListingRow(
            building="44 Pentonville Road", provider="MetSpace", floor_unit=None,
            brochure_link="https://drive.google.com/file/d/NEWID/view",
            special_features="4 MR + 3 PB; Available: December",
        )
        plan = master_merge.build_merge_plan([new_row], master_df)
        matched = plan.matched_changed[0]

        self.assertEqual(matched.risky_fields, frozenset())
        self.assertEqual(
            matched.diffs["brochure_link"],
            ("https://drive.google.com/file/d/OLDID/view", "https://drive.google.com/file/d/NEWID/view"),
        )

    def test_a_genuinely_different_short_item_is_still_correctly_treated_as_a_change(self):
        # The exact-match short-circuit must never make two DIFFERENT short
        # items look similar - only literal restatement is exempted.
        self.assertFalse(master_merge._items_similar("4 mr + 3 pb", "5 mr + 2 pb"))


class ContactsMergeTests(unittest.TestCase):
    """
    contacts, for a confidently matched same-provider row, always resolves
    to the newest NONBLANK value verbatim - never merged/accumulated with
    master's old one (see CONTACTS_NEWEST_WINS_FIELDS' own docstring).
    Changed from the earlier union-merge behavior specifically because a
    replaced agent's own stale details must not persist forever just
    because they were once correct ("John Smith" -> "Sarah Jones" must
    become "Sarah Jones", never "John Smith; Sarah Jones").
    """

    def _matched_contacts(self, old_val, new_val):
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor", "contacts": old_val,
        }])
        new_row = ListingRow(
            building="1 Example Street", provider="UNION", floor_unit="3rd Floor", contacts=new_val,
        )
        plan = master_merge.build_merge_plan([new_row], master_df)
        return plan.matched_changed[0]

    def test_old_contact_a_new_contact_b_replaces_with_b_only(self):
        matched = self._matched_contacts("John Smith", "Sarah Jones")
        self.assertEqual(matched.diffs["contacts"], ("John Smith", "Sarah Jones"))
        self.assertNotIn("John Smith", matched.diffs["contacts"][1])

    def test_richer_restatement_of_the_same_contact_replaces_with_the_richer_value(self):
        matched = self._matched_contacts(
            "John Smith — john@example.com", "John Smith — john@example.com — 020 1234 5678",
        )
        self.assertEqual(matched.diffs["contacts"][1], "John Smith — john@example.com — 020 1234 5678")

    def test_old_one_contact_new_two_contacts_replaces_with_both_new_ones_only(self):
        matched = self._matched_contacts("John Smith", "Sarah Jones; David Brown")
        self.assertEqual(matched.diffs["contacts"][1], "Sarah Jones; David Brown")
        self.assertNotIn("John Smith", matched.diffs["contacts"][1])

    def test_new_blank_preserves_old_contact(self):
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor",
            "contacts": "John Smith",
        }])
        new_row = ListingRow(
            building="1 Example Street", provider="UNION", floor_unit="3rd Floor", contacts=None,
        )
        plan = master_merge.build_merge_plan([new_row], master_df)
        self.assertEqual(len(plan.matched_changed), 0)
        self.assertEqual(plan.matched_unchanged[0].new_row.contacts, None)

    def test_old_blank_new_contact_is_used(self):
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor", "contacts": None,
        }])
        new_row = ListingRow(
            building="1 Example Street", provider="UNION", floor_unit="3rd Floor", contacts="Sarah Jones",
        )
        plan = master_merge.build_merge_plan([new_row], master_df)
        matched = plan.matched_changed[0]
        self.assertEqual(matched.diffs["contacts"], (None, "Sarah Jones"))

    def test_identical_contacts_produce_no_change(self):
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor",
            "contacts": "John Smith, john@example.com",
        }])
        new_row = ListingRow(
            building="1 Example Street", provider="UNION", floor_unit="3rd Floor",
            contacts="John Smith, john@example.com",
        )
        plan = master_merge.build_merge_plan([new_row], master_df)
        self.assertEqual(len(plan.matched_changed), 0)
        self.assertEqual(len(plan.matched_unchanged), 1)

    def test_contact_replacement_does_not_require_manual_review(self):
        matched = self._matched_contacts("John Smith", "Sarah Jones")
        self.assertEqual(matched.risky_fields, frozenset())

    def test_repeated_provider_updates_never_accumulate_historical_contacts(self):
        # Simulates two successive updates from the same provider, each
        # applied to master in turn - the SECOND update's own diff must
        # only ever compare against the FIRST update's own result, never
        # somehow retain the very first, now-doubly-stale contact.
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor", "contacts": "John Smith",
        }])
        first_update = ListingRow(
            building="1 Example Street", provider="UNION", floor_unit="3rd Floor", contacts="Sarah Jones",
        )
        first_plan = master_merge.build_merge_plan([first_update], master_df)
        updates = {m.master_index: {"contacts": m.diffs["contacts"][1]} for m in first_plan.matched_changed}
        merged_records = master_merge.apply_merge(first_plan.master_records, updates, [])

        second_update = ListingRow(
            building="1 Example Street", provider="UNION", floor_unit="3rd Floor", contacts="David Brown",
        )
        second_plan = master_merge.build_merge_plan(
            [second_update], pd.DataFrame([r.model_dump() for r in merged_records]),
        )
        matched = second_plan.matched_changed[0]
        self.assertEqual(matched.diffs["contacts"][1], "David Brown")
        self.assertNotIn("John Smith", matched.diffs["contacts"][1])
        self.assertNotIn("Sarah Jones", matched.diffs["contacts"][1])


class BrochureFallbackDoesNotOverrideProviderTests(unittest.TestCase):
    """
    brochure_enrichment.py's own _apply_units_to_row only ever fills a
    field that's genuinely blank on the row it's enriching (see that
    module's own docstring/tests, e.g. test_populated_special_features_
    not_overwritten) - so by the time a row reaches master_merge, if a
    provider's own source stated special_features/contacts, brochure
    enrichment has already left it untouched. master_merge itself has no
    way to distinguish a value that was provider-explicit from one a
    brochure fallback filled - both are just "this row's own current
    value" - so the correct behavior at THIS layer is simply that a row's
    own non-blank value always wins over master's old one when compatible,
    and is treated as ordinary content otherwise - exactly the same rule
    already proven above, confirmed here at the master-merge boundary too.
    """

    def test_a_row_whose_value_could_only_have_come_from_brochure_fallback_still_updates_normally(self):
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor",
            "special_features": None,
        }])
        # Simulates brochure_enrichment already having filled this row's
        # own special_features (the provider's own source left it blank) -
        # master_merge treats it exactly like any other non-blank value.
        new_row = ListingRow(
            building="1 Example Street", provider="UNION", floor_unit="3rd Floor",
            special_features="Private terrace",
        )
        plan = master_merge.build_merge_plan([new_row], master_df)
        matched = plan.matched_changed[0]
        self.assertEqual(matched.diffs["special_features"], (None, "Private terrace"))
        self.assertEqual(matched.risky_fields, frozenset())

    def test_brochure_populated_current_contacts_can_update_master(self):
        # Same principle, for contacts specifically - a row whose own
        # contacts field could only have come from brochure enrichment
        # (the provider's own source left it blank) still becomes the
        # newest current contact set once it reaches master_merge.
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor", "contacts": None,
        }])
        new_row = ListingRow(
            building="1 Example Street", provider="UNION", floor_unit="3rd Floor",
            contacts="Sarah Jones, sarah@example.com",
        )
        plan = master_merge.build_merge_plan([new_row], master_df)
        matched = plan.matched_changed[0]
        self.assertEqual(matched.diffs["contacts"], (None, "Sarah Jones, sarah@example.com"))
        self.assertEqual(matched.risky_fields, frozenset())

    def test_explicit_provider_contacts_stay_protected_from_brochure_overwrite_upstream(self):
        # This protection lives one layer upstream, in brochure_enrichment.
        # py's own _apply_units_to_row (never assigns to a field that's
        # already non-blank on the row it's enriching) - NOT re-implemented
        # or re-tested here (out of scope for this change), just confirmed
        # still in force by inspecting that module's own guarantee, which
        # this task did not touch.
        import brochure_enrichment
        row = ListingRow(
            building="1 Example Street", provider="UNION", floor_unit="3rd Floor",
            contacts="Explicit Provider Contact, provider@example.com",
        )
        units = brochure_enrichment._BrochureUnits([])
        units.contacts = "Brochure Contact, brochure@example.com"
        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)
        self.assertEqual(new_row.contacts, "Explicit Provider Contact, provider@example.com")
        self.assertNotIn("contacts", fields)


_METERS_PER_DEGREE_LAT = 111320.0


def _lat_offset(base_lat, meters):
    """A pure north/south coordinate offset of approximately `meters`,
    keeping longitude fixed - avoids needing the latitude-dependent
    longitude scaling (cos(lat)) in test setup; confirmed against the real
    haversine_distance_meters (see the sanity checks in HaversineDistance
    Tests below) to be accurate to well under 1% at this scale, more than
    precise enough for a boundary test with a several-meter margin either
    side of a threshold."""
    return base_lat + meters / _METERS_PER_DEGREE_LAT


class HaversineDistanceTests(unittest.TestCase):
    def test_same_point_is_zero(self):
        self.assertEqual(master_merge.haversine_distance_meters(51.5142, -0.1494, 51.5142, -0.1494), 0.0)

    def test_known_small_offset_is_approximately_correct(self):
        # A pure ~50m north/south offset (see _lat_offset) - confirms this
        # is a real geographic distance calculation, not a raw decimal
        # comparison (a flat degree-based epsilon would treat this
        # identically to a much larger east/west offset at this latitude).
        distance = master_merge.haversine_distance_meters(51.5142, -0.1494, _lat_offset(51.5142, 50), -0.1494)
        self.assertAlmostEqual(distance, 50, delta=1)

    def test_symmetric(self):
        a = master_merge.haversine_distance_meters(51.5142, -0.1494, 51.5150, -0.1400)
        b = master_merge.haversine_distance_meters(51.5150, -0.1400, 51.5142, -0.1494)
        self.assertAlmostEqual(a, b, delta=0.001)


class GeocodeLocationRiskTests(unittest.TestCase):
    """
    lat/lng get no provenance tag distinguishing an explicit provider-
    stated coordinate from one geocode.py generated via its own API calls
    (see GEOCODE_RISK_FIELDS' own docstring). A confidence-based location
    comparison (see _is_same_location/_location_change_is_safe) replaces
    the earlier "any change always needs a look" rule: a trivially small
    movement (SAME_LOCATION_METERS) is not even a diff at all, a larger
    move is auto-applied only when corroborated by matching address_1+
    postcode (up to CORROBORATED_LOCATION_METERS), and anything beyond
    that - or without corroboration - still needs a manual look.
    """

    def _plan(self, old_lat, old_lng, new_lat, new_lng, old_extra=None, new_extra=None):
        master_df = _master_df([{
            "building": "2 Leonard Circus", "provider": "UNION", "floor_unit": "5th Floor",
            "lat": old_lat, "lng": old_lng, **(old_extra or {}),
        }])
        new_row = ListingRow(
            building="2 Leonard Circus", provider="UNION", floor_unit="5th Floor",
            lat=new_lat, lng=new_lng, **(new_extra or {}),
        )
        return master_merge.build_merge_plan([new_row], master_df)

    def test_tiny_difference_is_treated_as_equivalent(self):
        # ~10m - well under SAME_LOCATION_METERS - not even a diff, master
        # keeps its exact existing coordinate rather than being rewritten.
        plan = self._plan(51.5142, -0.1494, _lat_offset(51.5142, 10), -0.1494)
        self.assertEqual(len(plan.matched_changed), 0)
        self.assertEqual(len(plan.matched_unchanged), 1)

    def test_small_entrance_centroid_shift_does_not_require_review(self):
        # ~35m - the realistic rooftop-vs-entrance/centroid range this
        # threshold exists to absorb (see SAME_LOCATION_METERS' own
        # docstring) - same outcome as the tiny-difference case above.
        plan = self._plan(51.5142, -0.1494, _lat_offset(51.5142, 35), -0.1494)
        self.assertEqual(len(plan.matched_changed), 0)

    def test_at_the_same_location_threshold_boundary(self):
        # ~48m (under 50) - same location. ~55m (over 50) - a real diff.
        under = self._plan(51.5142, -0.1494, _lat_offset(51.5142, 48), -0.1494)
        self.assertEqual(len(under.matched_changed), 0)

        over = self._plan(51.5142, -0.1494, _lat_offset(51.5142, 55), -0.1494)
        self.assertEqual(len(over.matched_changed), 1)
        self.assertIn("lat", over.matched_changed[0].diffs)

    def test_old_valid_new_blank_preserves_old(self):
        master_df = _master_df([{
            "building": "2 Leonard Circus", "provider": "UNION", "floor_unit": "5th Floor",
            "lat": 51.5142, "lng": -0.1494,
        }])
        new_row = ListingRow(
            building="2 Leonard Circus", provider="UNION", floor_unit="5th Floor", lat=None, lng=None,
        )
        plan = master_merge.build_merge_plan([new_row], master_df)
        self.assertEqual(len(plan.matched_changed), 0)

    def test_old_blank_new_valid_fills_automatically(self):
        plan = self._plan(None, None, 51.5142, -0.1494)
        matched = plan.matched_changed[0]
        self.assertNotIn("lat", matched.risky_fields)
        self.assertNotIn("lng", matched.risky_fields)

    def test_lat_and_lng_are_judged_and_updated_as_one_pair(self):
        # Only lat actually differs (lng identical) - the pair distance is
        # still what's measured (not per-field), and BOTH fields end up
        # consistently untouched together (lng was never a diff to begin
        # with; lat's own tiny movement is removed from diffs too, not
        # left as a lone, independently-judged field).
        plan = self._plan(51.5142, -0.1494, _lat_offset(51.5142, 20), -0.1494)
        self.assertEqual(len(plan.matched_changed), 0)

    def test_confident_same_property_same_address_postcode_resolves_moderate_move(self):
        # The brief's own worked example: 2 Leonard Circus, EC2A 4LW,
        # matching address/postcode both sides, coordinates move ~120m
        # (over the same-location tier, under the corroborated ceiling).
        plan = self._plan(
            51.5142, -0.1494, _lat_offset(51.5142, 120), -0.1494,
            old_extra={"address_1": "2 Leonard Circus", "postcode": "EC2A 4LW"},
            new_extra={"address_1": "2 Leonard Circus", "postcode": "EC2A 4LW"},
        )
        matched = plan.matched_changed[0]
        self.assertIn("lat", matched.diffs)
        self.assertEqual(matched.risky_fields, frozenset())

    def test_materially_distant_with_conflicting_postcode_does_not_auto_apply(self):
        plan = self._plan(
            51.5142, -0.1494, _lat_offset(51.5142, 120), -0.1400,
            old_extra={"address_1": "2 Leonard Circus", "postcode": "EC2A 4LW"},
            new_extra={"address_1": "2 Leonard Circus", "postcode": "EC1A 9XY"},
        )
        matched = plan.matched_changed[0]
        self.assertIn("lat", matched.risky_fields)
        self.assertIn("lng", matched.risky_fields)

    def test_moderate_move_without_any_corroborating_address_stays_risky(self):
        plan = self._plan(51.5142, -0.1494, _lat_offset(51.5142, 120), -0.1494)
        matched = plan.matched_changed[0]
        self.assertIn("lat", matched.risky_fields)

    def test_at_the_corroborated_ceiling_boundary(self):
        extra = ({"address_1": "2 Leonard Circus", "postcode": "EC2A 4LW"},) * 2
        under = self._plan(51.5142, -0.1494, _lat_offset(51.5142, 148), -0.1494, *extra)
        self.assertEqual(under.matched_changed[0].risky_fields, frozenset())

        over = self._plan(51.5142, -0.1494, _lat_offset(51.5142, 152), -0.1494, *extra)
        self.assertIn("lat", over.matched_changed[0].risky_fields)

    def test_wrong_building_geocode_remains_protected_even_with_matching_key(self):
        # A confident provider+building+floor match alone is never enough
        # - a substantial, uncorroborated move (no address/postcode given
        # at all) must stay risky exactly as before this feature existed.
        plan = self._plan(51.5142, -0.1494, 51.5200, -0.1400)
        matched = plan.matched_changed[0]
        self.assertIn("lat", matched.risky_fields)
        self.assertIn("lng", matched.risky_fields)

    def test_meaningful_unsafe_movement_stays_risky_even_with_corroboration(self):
        # ~900m, well beyond CORROBORATED_LOCATION_METERS - matching
        # address/postcode never overrides a move this large.
        plan = self._plan(
            51.5142, -0.1494, _lat_offset(51.5142, 900), -0.1494,
            old_extra={"address_1": "2 Leonard Circus", "postcode": "EC2A 4LW"},
            new_extra={"address_1": "2 Leonard Circus", "postcode": "EC2A 4LW"},
        )
        matched = plan.matched_changed[0]
        self.assertIn("lat", matched.risky_fields)

    def test_not_judged_by_raw_decimal_string_equality(self):
        # Deliberately verbose, differently-formatted decimals that still
        # represent a real, tiny (~15m) movement - proves real geographic
        # distance is used, not how similar the digit strings look.
        plan = self._plan(51.5142000, -0.1494000, 51.51421347, -0.14941102)
        self.assertEqual(len(plan.matched_changed), 0)

    def test_lat_lng_cannot_become_a_mixed_old_new_pair(self):
        # A genuine, corroborated update - both fields must resolve
        # TOGETHER (neither risky), never one applied and the other left
        # pending/stale.
        plan = self._plan(
            51.5142, -0.1494, _lat_offset(51.5142, 100), -0.1400,
            old_extra={"address_1": "2 Leonard Circus", "postcode": "EC2A 4LW"},
            new_extra={"address_1": "2 Leonard Circus", "postcode": "EC2A 4LW"},
        )
        matched = plan.matched_changed[0]
        both_present = {"lat", "lng"} <= set(matched.diffs)
        both_risky = {"lat", "lng"} <= matched.risky_fields
        both_safe = not (matched.risky_fields & {"lat", "lng"})
        self.assertTrue(both_present)
        self.assertTrue(both_risky or both_safe)  # never exactly one of the two

    def test_explicit_valid_new_address_can_still_update_freely(self):
        # address_1/postcode are deliberately NOT in GEOCODE_RISK_FIELDS -
        # confirmed unaffected by this feature (see BuildMergePlanRiskyFieldsTests'
        # own pre-existing test_non_risky_field_shrinking_is_never_flagged).
        master_df = _master_df([{
            "building": "1 Example Street", "provider": "UNION", "floor_unit": "3rd Floor",
            "address_1": "1 Example Street", "postcode": "EC1A 1AA",
        }])
        new_row = ListingRow(
            building="1 Example Street", provider="UNION", floor_unit="3rd Floor",
            address_1="1 Example Street", postcode="EC1A 1BB",
        )
        plan = master_merge.build_merge_plan([new_row], master_df)
        matched = plan.matched_changed[0]
        self.assertIn("postcode", matched.diffs)
        self.assertEqual(matched.risky_fields, frozenset())


class HallmarkStyleFloorUnitMatchingTests(unittest.TestCase):
    """
    _floor_unit_key - the redundant-building-name-prefix fix. Confidently
    matches once the redundant prefix is normalized away, but never
    weakens genuine floor/unit distinctions (6th vs 7th, North vs South,
    different buildings) - see BuildMergePlanFuzzyBuildingTests/MatchUnit-
    style tests elsewhere in this file for the pre-existing safeguards
    this must not loosen.
    """

    def test_redundant_building_name_prefix_still_matches(self):
        master_df = _master_df([{"building": "Hallmark", "provider": "UNION", "floor_unit": "6th Floor"}])
        new_row = ListingRow(building="Hallmark", provider="UNION", floor_unit="Hallmark 6th Floor")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed) + len(plan.matched_unchanged), 1)
        self.assertEqual(len(plan.unmatched), 0)

    def test_reverse_direction_also_matches(self):
        master_df = _master_df([{"building": "Hallmark", "provider": "UNION", "floor_unit": "Hallmark 6th Floor"}])
        new_row = ListingRow(building="Hallmark", provider="UNION", floor_unit="6th Floor")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed) + len(plan.matched_unchanged), 1)
        self.assertEqual(len(plan.unmatched), 0)

    def test_different_floor_numbers_still_kept_separate(self):
        master_df = _master_df([{"building": "Hallmark", "provider": "UNION", "floor_unit": "Hallmark 6th Floor"}])
        new_row = ListingRow(building="Hallmark", provider="UNION", floor_unit="Hallmark 7th Floor")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed), 0)
        self.assertEqual(len(plan.unmatched), 1)

    def test_north_and_south_still_kept_separate(self):
        master_df = _master_df([{"building": "Hallmark", "provider": "UNION", "floor_unit": "Hallmark North Wing"}])
        new_row = ListingRow(building="Hallmark", provider="UNION", floor_unit="Hallmark South Wing")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed), 0)
        self.assertEqual(len(plan.unmatched), 1)

    def test_a_different_building_that_happens_to_start_the_same_is_not_stripped(self):
        # "Hallmark House" must never be treated as building "Hallmark"
        # plus a floor label - the character right after the shared prefix
        # isn't a word boundary, so no stripping happens at all here.
        master_df = _master_df([{"building": "Hallmark", "provider": "UNION", "floor_unit": "Hallmark House 2nd Floor"}])
        new_row = ListingRow(building="Hallmark", provider="UNION", floor_unit="2nd Floor")

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed), 0)
        self.assertEqual(len(plan.unmatched), 1)


class AmbiguousIdentityStillManualTests(unittest.TestCase):
    """Confirms the updated-provider auto-apply behavior never reaches a
    row whose identity/unit match is genuinely ambiguous - unchanged
    pre-existing safeguards, not new logic, but worth proving explicitly
    alongside the new auto-update behavior."""

    def test_ambiguous_floor_match_stays_unmatched_not_auto_applied(self):
        master_df = _master_df([
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "1st Floor", "rent_pcm": 1000.0},
            {"building": "1 Example Street", "provider": "UNION", "floor_unit": "2nd Floor", "rent_pcm": 2000.0},
        ])
        # No postcode, floor_unit doesn't exact-match either existing row -
        # ambiguous, must not guess.
        new_row = ListingRow(building="1 Example Street", provider="UNION", floor_unit="Suite Z", rent_pcm=3000.0)

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed), 0)
        self.assertEqual(len(plan.unmatched), 1)

    def test_different_submarket_source_rows_remain_separate_new_properties(self):
        # Two intentionally-separate source listings (see
        # _partition_by_source_submarket) sharing no existing master row -
        # both remain genuinely new, never silently merged.
        new_rows = [
            ListingRow(building="Nexus Place", provider="UNION", floor_unit="5th Floor", submarket="City"),
            ListingRow(
                building="Nexus Place", provider="UNION", floor_unit="5th Floor", submarket="Clerkenwell & Farringdon",
            ),
        ]
        plan = master_merge.build_merge_plan(new_rows, _master_df([]))

        self.assertEqual(len(plan.unmatched), 2)
        self.assertEqual(len(plan.unmatched_collisions), 0)


class ListingIdentityConflictTests(unittest.TestCase):
    """
    The real confirmed "1 Oliver's Yard" (The Workplace Company) report:
    two rows sharing (building, provider, blank floor_unit) - the same
    identity key _dedup_key/_fallback_key already used - but with
    dramatically different size/desks/rent, previously forced into a
    field-by-field "7282 or 42892?" manual decision merely because
    building+provider+blank-floor happened to agree. See master_merge's
    own _listing_evidence_conflicts/_partition_by_listing_evidence/
    _resolve_listing_evidence_conflicts docstrings for the full rule.
    """

    def _oliver_rows(self):
        row_a = ListingRow(
            building="1 Oliver's Yard", provider="The Workplace Company", address_1="1 Oliver's Yard, London",
            postcode="EC1Y 1DT", size_sqft=7282, desks_min=52, desks_max=68, rent_pcm=45512,
            brochure_link="https://www.canva.com/design/shared-brochure/view",
            source_file="The Workplace Company Availability (1).xlsx — Sheet1",
        )
        row_b = ListingRow(
            building="1 Oliver's Yard", provider="The Workplace Company", address_1="1 Oliver's Yard, London",
            postcode="EC1Y 1DT", size_sqft=42892, desks_min=200, desks_max=400, rent_pcm=230811,
            brochure_link="https://www.canva.com/design/shared-brochure/view",
            source_file="The Workplace Company Availability (1).xlsx — Sheet1",
        )
        return row_a, row_b

    def test_materially_different_requires_a_real_ratio_not_just_inequality(self):
        self.assertTrue(master_merge._materially_different(7282, 42892))
        self.assertFalse(master_merge._materially_different(45000, 46000))  # ordinary rent revision
        self.assertFalse(master_merge._materially_different(None, 42892))  # blank is never evidence
        self.assertFalse(master_merge._materially_different(0, 42892))  # non-positive is never evidence

    def test_single_signal_alone_never_counts_as_a_listing_conflict(self):
        # "size differs" alone must never mean different listing - a
        # provider may simply have corrected/updated one field.
        a = {"size_sqft": 4500.0, "desks_min": None, "desks_max": None, "rent_pcm": None, "rent_psf": None}
        b = {"size_sqft": 9000.0, "desks_min": None, "desks_max": None, "rent_pcm": None, "rent_psf": None}
        self.assertFalse(master_merge._listing_evidence_conflicts(a, b))

    def test_oliver_s_yard_size_desks_rent_together_is_a_confident_conflict(self):
        row_a, row_b = self._oliver_rows()
        self.assertTrue(master_merge._listing_evidence_conflicts(row_a.model_dump(), row_b.model_dump()))

    def test_oliver_s_yard_fresh_upload_needs_no_manual_decision(self):
        # Same building/provider/blank floor_unit, no existing master row -
        # both must become independent new properties automatically, with
        # NO unmatched_collisions group and no unmatched_collisions prompt.
        row_a, row_b = self._oliver_rows()
        plan = master_merge.build_merge_plan([row_a, row_b], _master_df([]))

        self.assertEqual(len(plan.unmatched), 2)
        self.assertEqual(plan.unmatched_collisions, [])
        self.assertEqual(plan.collisions, [])
        sizes = sorted(u.new_row.size_sqft for u in plan.unmatched)
        self.assertEqual(sizes, [7282.0, 42892.0])

    def test_oliver_s_yard_shared_brochure_link_does_not_force_a_merge(self):
        # Part 6: a shared brochure_link is building-level evidence, never
        # listing-level - both rows here already share one identical
        # brochure_link (see _oliver_rows) and are still kept separate.
        row_a, row_b = self._oliver_rows()
        self.assertEqual(row_a.brochure_link, row_b.brochure_link)

        plan = master_merge.build_merge_plan([row_a, row_b], _master_df([]))

        self.assertEqual(plan.unmatched_collisions, [])
        self.assertEqual(len(plan.unmatched), 2)

    def test_oliver_s_yard_against_an_existing_master_row_splits_rather_than_collides(self):
        # One existing master row already matches BOTH incoming rows via
        # building+provider+blank-floor - closer one (row_a, size 7282 vs
        # master's 7000) keeps updating master automatically; row_b is
        # demoted to a fresh, independent new property instead of forcing
        # a "7282 or 42892?" collision prompt.
        row_a, row_b = self._oliver_rows()
        master_df = _master_df([{
            "building": "1 Oliver's Yard", "provider": "The Workplace Company",
            "address_1": "1 Oliver's Yard, London", "postcode": "EC1Y 1DT",
            "size_sqft": 7000.0, "desks_min": 50, "desks_max": 65, "rent_pcm": 44000.0,
        }])

        plan = master_merge.build_merge_plan([row_a, row_b], master_df)

        self.assertEqual(plan.collisions, [])
        self.assertEqual(len(plan.matched_changed), 1)
        self.assertEqual(plan.matched_changed[0].new_row.size_sqft, 7282.0)
        self.assertEqual(len(plan.unmatched), 1)
        self.assertEqual(plan.unmatched[0].new_row.size_sqft, 42892.0)

    def test_different_floor_unit_already_separates_listings_with_no_new_logic_needed(self):
        # Confirms the pre-existing floor_unit-based identity split (not
        # this task's own new logic) still keeps genuinely different
        # floors apart - never grouped, regardless of listing-evidence.
        row_a = ListingRow(building="1 Oliver's Yard", provider="The Workplace Company", floor_unit="1st Floor")
        row_b = ListingRow(building="1 Oliver's Yard", provider="The Workplace Company", floor_unit="2nd Floor")

        plan = master_merge.build_merge_plan([row_a, row_b], _master_df([]))

        self.assertEqual(len(plan.unmatched), 2)
        self.assertEqual(plan.unmatched_collisions, [])

    def test_minor_update_does_not_get_split_into_a_new_listing(self):
        # A genuine, ordinary update to an existing listing (rent revised
        # upward, same size/desks) must still auto-apply as one property,
        # never split into "separate listings" just because SOME field
        # moved.
        master_df = _master_df([{
            "building": "1 Oliver's Yard", "provider": "The Workplace Company",
            "size_sqft": 7282.0, "desks_min": 52, "desks_max": 68, "rent_pcm": 44000.0,
        }])
        new_row = ListingRow(
            building="1 Oliver's Yard", provider="The Workplace Company",
            size_sqft=7282.0, desks_min=52, desks_max=68, rent_pcm=45512.0,
        )

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed), 1)
        self.assertEqual(len(plan.unmatched), 0)

    def test_large_single_field_rent_change_still_stays_the_same_listing(self):
        # Part 3's own explicit example: rent moving from £10,000 to
        # £12,000 (a real, large 20% change) must remain a normal update -
        # a single differing field is never enough on its own, no matter
        # how large the change, since _MIN_INDEPENDENT_SIGNALS_FOR_
        # SEPARATE_LISTINGS requires 3+ independent signals.
        master_df = _master_df([{
            "building": "1 Oliver's Yard", "provider": "The Workplace Company",
            "size_sqft": 7282.0, "desks_min": 52, "desks_max": 68, "rent_pcm": 10000.0,
        }])
        new_row = ListingRow(
            building="1 Oliver's Yard", provider="The Workplace Company",
            size_sqft=7282.0, desks_min=52, desks_max=68, rent_pcm=12000.0,
        )

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed), 1)
        self.assertEqual(len(plan.unmatched), 0)

    def test_single_corrected_desk_field_still_stays_the_same_listing(self):
        # A provider correcting just its own desk count (e.g. a typo fix)
        # must never be treated as a second, separate listing either - same
        # single-signal-is-never-enough rule as the rent case above.
        master_df = _master_df([{
            "building": "1 Oliver's Yard", "provider": "The Workplace Company",
            "size_sqft": 7282.0, "desks_min": 52, "desks_max": 68, "rent_pcm": 45512.0,
        }])
        new_row = ListingRow(
            building="1 Oliver's Yard", provider="The Workplace Company",
            size_sqft=7282.0, desks_min=52, desks_max=90, rent_pcm=45512.0,
        )

        plan = master_merge.build_merge_plan([new_row], master_df)

        self.assertEqual(len(plan.matched_changed), 1)
        self.assertEqual(len(plan.unmatched), 0)

    def test_richer_duplicate_of_the_same_listing_still_consolidates(self):
        # Two rows with essentially the SAME numeric evidence (no material
        # difference at all) but one carries extra descriptive text - must
        # still consolidate as one property, never split.
        row_a = ListingRow(
            building="1 Oliver's Yard", provider="The Workplace Company",
            size_sqft=7282.0, desks_min=52, desks_max=68, rent_pcm=45512.0, special_features=None,
        )
        row_b = ListingRow(
            building="1 Oliver's Yard", provider="The Workplace Company",
            size_sqft=7282.0, desks_min=52, desks_max=68, rent_pcm=45512.0,
            special_features="Roof terrace; showers",
        )
        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]

        groups = master_merge._group_unmatched_duplicates(unmatched)

        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_genuinely_ambiguous_same_building_rows_still_need_manual_review(self):
        # Only ONE signal differs materially (size) - below the multi-
        # signal bar - so this must still resolve exactly as before this
        # task: a genuine field conflict needing a human decision, never
        # silently merged and never silently split.
        row_a = ListingRow(building="1 Oliver's Yard", provider="The Workplace Company", size_sqft=4500.0)
        row_b = ListingRow(building="1 Oliver's Yard", provider="The Workplace Company", size_sqft=9000.0)
        unmatched = [master_merge.UnmatchedRow(row_a), master_merge.UnmatchedRow(row_b)]

        groups = master_merge._group_unmatched_duplicates(unmatched)

        self.assertEqual(len(groups), 1)
        self.assertTrue(master_merge._group_has_genuine_conflict([row_a.model_dump(), row_b.model_dump()]))

    def test_different_providers_at_the_same_building_remain_separate(self):
        row_a = ListingRow(building="1 Oliver's Yard", provider="The Workplace Company", size_sqft=7282.0)
        row_b = ListingRow(building="1 Oliver's Yard", provider="A Different Agent", size_sqft=42892.0)

        plan = master_merge.build_merge_plan([row_a, row_b], _master_df([]))

        self.assertEqual(len(plan.unmatched), 2)
        self.assertEqual(plan.unmatched_collisions, [])


class ReuploadDuplicationRegressionTests(unittest.TestCase):
    """
    The real confirmed report: re-uploading "The Workplace Company
    Availability" produced FOUR Oliver's Yard rows instead of two. Root
    cause traced to _partition_by_listing_evidence's own greedy first-fit
    clustering: comparing every candidate only against whichever row
    happened to be encountered FIRST let a pre-enrichment "desks only"
    pending copy (blank size/rent - insufficient evidence on its own to
    tell the two real listings apart, only 2 of the 3 required signals
    available) bridge two genuinely different, fully-evidenced listings
    together into one messy 4-row group. Fixed by processing candidates in
    DESCENDING evidence-richness order and joining the BEST-fitting
    existing cluster (fewest conflicting signals), not just the first
    non-conflicting one - see _partition_by_listing_evidence/_best_
    listing_evidence_match's own docstrings. The analogous fix also
    applies to build_merge_plan's own master-matching tiers, for the case
    where the two listings are already IN master from an earlier approval.
    """

    def _oliver_a(self, size=None, rent=None):
        return ListingRow(
            building="1 Oliver's Yard", provider="The Workplace Company", postcode="EC1Y 1DT",
            address_1="1 Oliver's Yard", submarket="City Fringe",
            size_sqft=size, desks_min=52, desks_max=68, rent_pcm=rent,
        )

    def _oliver_b(self, size=None, rent=None):
        return ListingRow(
            building="1 Oliver's Yard", provider="The Workplace Company", postcode="EC1Y 1DT",
            address_1="1 Oliver's Yard", submarket="City Fringe",
            size_sqft=size, desks_min=200, desks_max=400, rent_pcm=rent,
        )

    def test_first_upload_of_two_distinct_same_building_listings_yields_two_rows(self):
        row_a = self._oliver_a(7282.0, 45512.0)
        row_b = self._oliver_b(42892.0, 230811.0)

        plan = master_merge.build_merge_plan([row_a, row_b], _master_df([]))

        self.assertEqual(len(plan.unmatched), 2)
        self.assertEqual(plan.unmatched_collisions, [])

    def test_stale_sparse_pending_copy_alongside_a_fresh_fully_enriched_one_still_yields_two_rows(self):
        # The exact real shape: an old, not-yet-superseded pending upload
        # (blank size/rent - e.g. never brochure-enriched, or staged under
        # an earlier code fingerprint) combined into the SAME merge plan
        # as a freshly re-uploaded, fully-enriched copy of the same file -
        # four rows total, sharing one identity key, must still resolve to
        # exactly two, correctly paired, richly-populated listings.
        old_a = self._oliver_a()
        old_b = self._oliver_b()
        new_a = self._oliver_a(7282.0, 45512.0)
        new_b = self._oliver_b(42892.0, 230811.0)

        unmatched = [master_merge.UnmatchedRow(r) for r in [old_a, old_b, new_a, new_b]]
        groups = master_merge._group_unmatched_duplicates(unmatched)

        self.assertEqual(len(groups), 2)
        sizes = sorted(
            {u.new_row.size_sqft for u in group if u.new_row.size_sqft is not None}
            for group in groups
        )
        self.assertEqual(sizes, [{7282.0}, {42892.0}])

    def test_stale_and_fresh_copies_consolidate_with_no_manual_decision_needed(self):
        old_a = self._oliver_a()
        old_b = self._oliver_b()
        new_a = self._oliver_a(7282.0, 45512.0)
        new_b = self._oliver_b(42892.0, 230811.0)

        plan = master_merge.build_merge_plan([old_a, old_b, new_a, new_b], _master_df([]))
        plan = master_merge.consolidate_unmatched_duplicates(plan)

        self.assertEqual(plan.unmatched_collisions, [])
        self.assertEqual(len(plan.unmatched), 2)
        final_sizes = sorted(u.new_row.size_sqft for u in plan.unmatched)
        final_rents = sorted(u.new_row.rent_pcm for u in plan.unmatched)
        self.assertEqual(final_sizes, [7282.0, 42892.0])
        self.assertEqual(final_rents, [45512.0, 230811.0])

    def test_reupload_against_an_already_approved_master_updates_the_matching_listing_not_a_new_one(self):
        # Both listings already exist in master (a prior approval), each
        # with sparse (blank size/rent) data - a re-upload with fuller
        # data for both must UPDATE each corresponding master row, never
        # create additional properties, and must never merge the two
        # distinct listings into one.
        master_df = _master_df([self._oliver_a().model_dump(), self._oliver_b().model_dump()])
        new_a = self._oliver_a(7282.0, 45512.0)
        new_b = self._oliver_b(42892.0, 230811.0)

        plan = master_merge.build_merge_plan([new_a, new_b], master_df)

        self.assertEqual(len(plan.matched_changed), 2)
        self.assertEqual(plan.unmatched, [])
        self.assertEqual(plan.collisions, [])
        matched_sizes = sorted(m.new_row.size_sqft for m in plan.matched_changed)
        self.assertEqual(matched_sizes, [7282.0, 42892.0])

    def test_reupload_against_an_already_populated_master_still_keeps_listings_separate(self):
        # Both master rows ALREADY have their own distinguishing size/
        # rent (a normal, non-sparse case) - re-uploading the same two
        # listings again (e.g. an ordinary refresh, values unchanged) must
        # still update each correctly and never merge A into B or vice
        # versa.
        master_df = _master_df([
            self._oliver_a(7282.0, 45512.0).model_dump(), self._oliver_b(42892.0, 230811.0).model_dump(),
        ])
        new_a = self._oliver_a(7282.0, 46000.0)  # a small, ordinary rent revision
        new_b = self._oliver_b(42892.0, 230811.0)

        plan = master_merge.build_merge_plan([new_a, new_b], master_df)

        self.assertEqual(len(plan.matched_changed), 1)
        self.assertEqual(plan.matched_changed[0].new_row.size_sqft, 7282.0)
        self.assertEqual(len(plan.matched_unchanged), 1)
        self.assertEqual(plan.unmatched, [])
        self.assertEqual(plan.collisions, [])

    def test_one_changed_rent_alone_does_not_create_a_new_listing(self):
        # A single-field update against an UNAMBIGUOUS (only one master
        # candidate at all) match was already safe before this task - this
        # confirms the new multi-candidate disambiguation logic doesn't
        # accidentally make an ordinary single-listing update any less
        # safe or any more likely to be treated as ambiguous.
        master_df = _master_df([self._oliver_a(7282.0, 45512.0).model_dump()])
        new_a = self._oliver_a(7282.0, 46000.0)

        plan = master_merge.build_merge_plan([new_a], master_df)

        self.assertEqual(len(plan.matched_changed), 1)
        self.assertEqual(plan.unmatched, [])


class BestListingEvidenceMatchTests(unittest.TestCase):
    def test_uniquely_closest_candidate_is_selected(self):
        new_dict = {"size_sqft": 7282.0, "desks_min": 52, "desks_max": 68, "rent_pcm": 45512.0, "rent_psf": None}
        candidates = [
            {"size_sqft": None, "desks_min": 52, "desks_max": 68, "rent_pcm": None, "rent_psf": None},
            {"size_sqft": 42892.0, "desks_min": 200, "desks_max": 400, "rent_pcm": 230811.0, "rent_psf": None},
        ]
        self.assertEqual(master_merge._best_listing_evidence_match(new_dict, candidates), 0)

    def test_no_candidate_within_range_returns_none(self):
        new_dict = {"size_sqft": 7282.0, "desks_min": 52, "desks_max": 68, "rent_pcm": 45512.0, "rent_psf": None}
        candidates = [{"size_sqft": 42892.0, "desks_min": 200, "desks_max": 400, "rent_pcm": 230811.0, "rent_psf": None}]
        self.assertIsNone(master_merge._best_listing_evidence_match(new_dict, candidates))

    def test_a_tie_for_best_returns_none_rather_than_guessing(self):
        new_dict = {"size_sqft": None, "desks_min": 52, "desks_max": 68, "rent_pcm": None, "rent_psf": None}
        candidates = [
            {"size_sqft": None, "desks_min": 52, "desks_max": 68, "rent_pcm": None, "rent_psf": None},
            {"size_sqft": None, "desks_min": 52, "desks_max": 68, "rent_pcm": None, "rent_psf": None},
        ]
        self.assertIsNone(master_merge._best_listing_evidence_match(new_dict, candidates))


if __name__ == "__main__":
    unittest.main()
