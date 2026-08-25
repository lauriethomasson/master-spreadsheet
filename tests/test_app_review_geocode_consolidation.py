"""
Regression tests for the Review page's consolidated geocode decision card
(see pages/2_Review_and_Master.py's _render_geocode_consolidated_decision
and master_merge.geocode_consolidation_groups) - real confirmed problem:
pasting the Colliers Canva deck produced 6 separate, byte-identical
"This address couldn't be independently verified" decisions for Ivybridge
House's 6 floors (3 for Hatchers Yard, 2 for Henly House), all repeating
the exact same address/postcode/lat/lng change, because geocode_rows' own
grouping (geocode.py) already guarantees these fields are identical across
every row sharing the same building+provider. Several floors/units of the
same building now get ONE consolidated card instead of one per row.

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_page_restructure.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_geocode_consolidation -v
"""

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import master_writer
from schema import ListingRow
from storage.file_store import save_staging_file

BASE = Path(__file__).resolve().parent.parent

FLOORS = [f"{n}{'st' if n == 1 else 'nd' if n == 2 else 'rd' if n == 3 else 'th'} Floor" for n in range(1, 7)]


class IsolatedCwdTestCase(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()


def _run_review_page():
    at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
    at.run()
    return at


class IvybridgeHouseConsolidatedCardTests(IsolatedCwdTestCase):
    """The real Ivybridge House shape: 6 existing master floors with no
    address on file, re-uploaded with an identical zero-hint-Tier-2
    geocode result (see geocode.py's geocode_row) shared across all six."""

    def _staged_ivybridge_house(self):
        master_writer.write_master([
            ListingRow(
                building="Ivybridge House", provider="Workplace Plus", floor_unit=floor,
                size_sqft=1000.0 + i,
                # Explicit, stable property_id - build_merge_plan backfills a
                # fresh random uuid for any blank one on every render pass,
                # which would make this test's own repeated at.run() calls
                # unstable if it mattered here (see test_app_review_page_
                # restructure.py's identical note) - kept for safety even
                # though this feature's own checkbox keys don't depend on it.
                property_id=str(uuid.uuid4()),
            )
            for i, floor in enumerate(FLOORS)
        ])
        save_staging_file(
            [
                ListingRow(
                    building="Ivybridge House", provider="Workplace Plus", floor_unit=floor,
                    address_1="1 Ivybridge Terrace", postcode="TW1 1AA", lat=51.45, lng=-0.32,
                    geocode_unverified=True,
                )
                for floor in FLOORS
            ],
            "ivybridge_colliers.xlsx", content_hash="ivybridge-consolidation-hash",
        )
        return _run_review_page()

    def test_one_consolidated_card_renders_instead_of_six(self):
        at = self._staged_ivybridge_house()
        self.assertFalse(at.exception)

        expander_labels = [e.label or "" for e in at.expander]
        consolidated = [l for l in expander_labels if "Ivybridge House" in l and "shared by" in l]
        self.assertEqual(len(consolidated), 1)
        self.assertIn("6 properties", consolidated[0])

        # No per-floor individual card at all - every one of the six rows'
        # own diff was entirely the (now consolidated) geocode fields, so
        # there is nothing left for an individual card to show.
        individual_floor_cards = [l for l in expander_labels if "Ivybridge House" in l and "decision" in l]
        self.assertEqual(individual_floor_cards, [])

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("This address couldn't be independently verified", caption_text)
        # _render_field_rows shows this caption once per RISKY FIELD (see
        # its own docstring) - 4 here (address_1/postcode/lat/lng), from
        # the ONE consolidated card. Without consolidation this would be
        # up to 6 rows x 4 fields = 24 - the count that matters is "once
        # per shared field", never "once per row sharing that field".
        self.assertEqual(caption_text.count("This address couldn't be independently verified"), 4)

    def test_approving_the_consolidated_card_applies_to_every_row_in_the_group(self):
        at = self._staged_ivybridge_house()

        geo_checkboxes = [c for c in at.checkbox if c.key and c.key.startswith("geo_consolidated_0_")]
        self.assertEqual(len(geo_checkboxes), 4)  # address_1, postcode, lat, lng
        for cb in geo_checkboxes:
            cb.set_value(True)
        at.run()
        self.assertFalse(at.exception)

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        ivybridge_rows = master_df[master_df["building"] == "Ivybridge House"]
        self.assertEqual(len(ivybridge_rows), 6)
        self.assertTrue((ivybridge_rows["address_1"] == "1 Ivybridge Terrace").all())
        self.assertTrue((ivybridge_rows["postcode"] == "TW1 1AA").all())
        self.assertTrue((ivybridge_rows["lat"] == 51.45).all())
        self.assertTrue((ivybridge_rows["lng"] == -0.32).all())

    def test_leaving_the_consolidated_card_unchecked_leaves_every_row_untouched(self):
        at = self._staged_ivybridge_house()

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        ivybridge_rows = master_df[master_df["building"] == "Ivybridge House"]
        self.assertEqual(len(ivybridge_rows), 6)
        self.assertTrue(ivybridge_rows["address_1"].isna().all())
        self.assertTrue(ivybridge_rows["postcode"].isna().all())


class OtherFieldsRenderPerRowUnaffectedTests(IsolatedCwdTestCase):
    """One floor of the group ALSO has its own, unrelated risky change
    (Special Features detail loss) - that field must still get its own
    per-row card, unaffected by the consolidation of the shared geocode
    fields (see this module's own docstring: "only the shared geocode
    fields get consolidated")."""

    def test_a_floor_with_an_extra_risky_field_still_gets_its_own_card_for_that_field_alone(self):
        master_writer.write_master([
            ListingRow(
                building="Ivybridge House", provider="Workplace Plus", floor_unit="1st Floor",
                special_features="Roof terrace; showers; bike storage; meeting rooms",
                property_id=str(uuid.uuid4()),
            ),
            ListingRow(
                building="Ivybridge House", provider="Workplace Plus", floor_unit="2nd Floor",
                property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [
                ListingRow(
                    building="Ivybridge House", provider="Workplace Plus", floor_unit="1st Floor",
                    address_1="1 Ivybridge Terrace", postcode="TW1 1AA", lat=51.45, lng=-0.32,
                    geocode_unverified=True, special_features="Available now",
                ),
                ListingRow(
                    building="Ivybridge House", provider="Workplace Plus", floor_unit="2nd Floor",
                    address_1="1 Ivybridge Terrace", postcode="TW1 1AA", lat=51.45, lng=-0.32,
                    geocode_unverified=True,
                ),
            ],
            "ivybridge_mixed.xlsx", content_hash="ivybridge-mixed-hash",
        )

        at = _run_review_page()
        self.assertFalse(at.exception)

        expander_labels = [e.label or "" for e in at.expander]
        consolidated = [l for l in expander_labels if "shared by" in l]
        self.assertEqual(len(consolidated), 1)
        self.assertIn("2 properties", consolidated[0])

        # The 1st Floor's own special_features change is NOT part of the
        # consolidated card - it still gets its own individual card, with
        # exactly its own 1 remaining decision (special_features alone,
        # the geocode fields having already been claimed above).
        individual = [l for l in expander_labels if "1st Floor" in l and "decision" in l]
        self.assertEqual(len(individual), 1)
        self.assertIn("1 decision needed", individual[0])

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn(
            "New text looks shorter than what's there now — may be missing detail, not just an update.",
            caption_text,
        )


class NoRiskyFieldsLeftAfterConsolidationTests(IsolatedCwdTestCase):
    """
    Real Hatchers Yard shape this regression-tests: once a row's ONLY
    risky field (its shared geocode address) is claimed by the
    consolidated card, this row's OWN leftover diffs (brochure_link,
    size_sqft) are both ordinary safe fields - risky_fields is empty.
    Nothing here needs a deliberate look, so this must NOT render its own
    empty "no decisions needed" card under "⚠️ Needs your decision" at
    all - these safe fields belong in the "✅ Automatic updates" summary,
    the exact same bucket auto_matched rows already use.
    """

    def test_leftover_safe_only_fields_skip_the_decision_section_and_land_in_automatic_updates(self):
        master_writer.write_master([
            ListingRow(
                building="Hatchers Yard", provider="Colliers", floor_unit="1st Floor",
                brochure_link="https://example.com/old-brochure.pdf", size_sqft=1000.0,
                property_id=str(uuid.uuid4()),
            ),
            ListingRow(
                building="Hatchers Yard", provider="Colliers", floor_unit="2nd Floor",
                brochure_link="https://example.com/old-brochure.pdf", size_sqft=1000.0,
                property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [
                ListingRow(
                    building="Hatchers Yard", provider="Colliers", floor_unit="1st Floor",
                    address_1="30 Barkston Gardens", postcode="SW5 0EW", lat=51.49, lng=-0.19,
                    geocode_unverified=True,
                    brochure_link="https://example.com/new-brochure.pdf", size_sqft=1100.0,
                ),
                ListingRow(
                    building="Hatchers Yard", provider="Colliers", floor_unit="2nd Floor",
                    address_1="30 Barkston Gardens", postcode="SW5 0EW", lat=51.49, lng=-0.19,
                    geocode_unverified=True,
                    brochure_link="https://example.com/new-brochure.pdf", size_sqft=1200.0,
                ),
            ],
            "hatchers_yard.xlsx", content_hash="hatchers-yard-no-decisions-hash",
        )

        at = _run_review_page()
        self.assertFalse(at.exception)

        expander_labels = [e.label or "" for e in at.expander]
        consolidated = [l for l in expander_labels if "shared by" in l]
        self.assertEqual(len(consolidated), 1)
        self.assertIn("2 properties", consolidated[0])

        # Neither floor gets its own leftover card at all - there was
        # never a real decision left once the address moved into the
        # consolidated card above (see the removed "no decisions needed"
        # shape this replaces).
        leftover = [l for l in expander_labels if "Hatchers Yard" in l and "Floor" in l]
        self.assertEqual(leftover, [])
        self.assertNotIn("no decisions needed", " ".join(expander_labels))

        # Both floors' safe fields (brochure_link, size_sqft) show up in
        # the Automatic updates section instead.
        self.assertIn("✅ Automatic updates", [s.value for s in at.subheader])
        info_text = "".join(i.value for i in at.info)
        self.assertIn("2 existing properties will be updated automatically.", info_text)

        markdown_text = "".join(m.value or "" for m in at.markdown)
        self.assertEqual(
            markdown_text.count('<td colspan="3">Hatchers Yard — Colliers — 1st Floor</td>'), 1,
        )
        self.assertEqual(
            markdown_text.count('<td colspan="3">Hatchers Yard — Colliers — 2nd Floor</td>'), 1,
        )
        self.assertIn("<td>Brochure Link</td>", markdown_text)
        self.assertIn("<td>Size</td>", markdown_text)


if __name__ == "__main__":
    unittest.main()
