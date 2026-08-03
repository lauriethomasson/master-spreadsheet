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
    """The core safeguard: is a free-text update dropping real information?"""

    def test_full_amenity_list_replaced_by_one_liner_is_flagged(self):
        self.assertTrue(master_merge.is_detail_loss(BOND_STREET_AMENITIES, "Available Q3 2026"))

    def test_short_new_value_flagged_purely_on_length(self):
        old = "Fully fitted, CAT A+ finish, breakout area, phone booths, kitchen"
        new = "Fitted"
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


if __name__ == "__main__":
    unittest.main()
