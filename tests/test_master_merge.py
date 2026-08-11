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
        new_dict = {"building": "77 Gracechurch Street"}
        self.assertEqual(master_merge._suggest_similar(new_dict, master_records), [])

    def test_genuine_name_only_typo_still_suggested(self):
        # Same real pair FuzzyBuildingMatchTests validates for the actual
        # matching tier (0.938) - the raised threshold must not lose this.
        master_records = self._master_records(["Thirty Lighterman"])
        new_dict = {"building": "Thirty Lightman"}
        self.assertEqual(len(master_merge._suggest_similar(new_dict, master_records)), 1)

    def test_unrelated_name_only_pair_not_suggested(self):
        # Real confirmed false-positive risk for the matching tier (0.762) -
        # below the 0.85 threshold reused here.
        master_records = self._master_records(["Orion House"])
        new_dict = {"building": "York House"}
        self.assertEqual(master_merge._suggest_similar(new_dict, master_records), [])

    def test_blank_building_suggests_nothing(self):
        self.assertEqual(master_merge._suggest_similar({"building": None}, self._master_records(["Kent House"])), [])


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


if __name__ == "__main__":
    unittest.main()
