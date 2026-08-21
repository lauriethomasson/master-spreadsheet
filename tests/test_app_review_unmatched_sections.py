"""
Streamlit-level regression test for the plain-new/needs-a-decision split in
pages/2_Review_and_Master.py's unmatched-rows section (previously one
"No match found — will be added as new" heading covered both a plain FYI
list and rows genuinely needing a decision, with no visual separation and
no way to collapse a long FYI list).

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_master_writer.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_unmatched_sections -v
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


class IsolatedCwdTestCase(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()


def _run_review_page():
    at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
    at.run()
    return at


class PlainNewSectionTests(IsolatedCwdTestCase):
    def test_plain_new_list_is_collapsed_with_matching_count_in_header(self):
        save_staging_file(
            [ListingRow(building=f"{i} Example Street", provider="Test Provider") for i in range(3)],
            "new_properties.xlsx", content_hash="plain-new-hash",
        )

        at = _run_review_page()
        self.assertFalse(at.exception)

        self.assertIn("📄 New properties", [s.value for s in at.subheader])
        info_text = "".join(i.value for i in at.info)
        self.assertIn("3 new properties will be added.", info_text)

        new_props_expanders = [e for e in at.expander if e.label == "View new properties"]
        self.assertEqual(len(new_props_expanders), 1)
        # Streamlit expanders default to collapsed unless expanded=True is
        # passed - confirm this one wasn't given that.
        self.assertFalse(new_props_expanders[0].proto.expanded)

    def test_no_plain_new_section_when_there_are_no_plain_new_rows(self):
        # A genuinely CONFLICTING duplicate pair (different size_sqft for
        # what's otherwise the same unit) - still needs a manual decision
        # (see master_merge.consolidate_unmatched_duplicates), so this is
        # NOT auto-merged into a plain_new row, unlike a trivial duplicate
        # would be (see test_trivial_duplicates_never_reach_plain_new_or_
        # needs_a_decision in test_app_review_duplicate_consolidation.py).
        save_staging_file(
            [
                ListingRow(building="1 Example Street", provider="Test Provider", floor_unit="1st", size_sqft=1000.0),
                ListingRow(building="1 Example Street", provider="Test Provider", floor_unit="1st", size_sqft=5000.0),
            ],
            "dupes.xlsx", content_hash="no-plain-new-hash",
        )

        at = _run_review_page()

        self.assertNotIn("📄 New properties", [s.value for s in at.subheader])
        self.assertEqual([e for e in at.expander if e.label == "View new properties"], [])


class NeedsADecisionHeadingTests(IsolatedCwdTestCase):
    HEADING = "⚠️ Needs your decision"
    EXPLAINER_TAIL = "Open each one and decide: is this the same property, or genuinely different?"

    def test_heading_and_explainer_for_near_miss(self):
        # Real, previously-validated fuzzy pair (see
        # tests.test_master_merge.FuzzyBuildingMatchTests) - a DIFFERENT
        # floor_unit here so the real matching tiers (which require an
        # exact floor_unit match) correctly fail, while _suggest_similar's
        # building-name-only check still fires.
        master_writer.write_master([
            ListingRow(
                building="Thirty Lighterman", provider="Kitt's", floor_unit="3rd Floor", size_sqft=1000.0,
                # Explicit, stable property_id - build_merge_plan backfills a
                # FRESH random uuid for any blank one on every single render
                # pass, which would make the near-miss dropdown's option
                # identity change out from under a simulated selection on
                # the very next rerun. A real master.xlsx never has a blank
                # property_id (every row gets one for real at approve-time),
                # so this only matters for a test fixture built directly via
                # write_master rather than through the app's own approve flow.
                property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [ListingRow(building="Thirty Lightman", provider="Kitt's", floor_unit="5th Floor", size_sqft=2000.0)],
            "near_miss.xlsx", content_hash="near-miss-hash",
        )

        at = _run_review_page()

        self.assertIn(self.HEADING, [s.value for s in at.subheader])
        captions = "".join(c.value for c in at.caption)
        self.assertIn("look similar to a property already in master", captions)
        self.assertIn(self.EXPLAINER_TAIL, captions)

    def test_heading_and_explainer_for_batch_duplicate(self):
        # No master at all - both rows fail to match master, but match
        # EACH OTHER (see master_merge._dedup_key), landing in
        # unmatched_collisions rather than near_miss. A genuine size
        # conflict keeps this one from auto-merging (see master_merge.
        # consolidate_unmatched_duplicates) - a trivially-identical
        # duplicate no longer reaches this heading at all (see
        # test_app_review_duplicate_consolidation.py).
        save_staging_file(
            [
                ListingRow(building="1 Example Street", provider="Test Provider", floor_unit="1st", size_sqft=1000.0),
                ListingRow(building="1 Example Street", provider="Test Provider", floor_unit="1st", size_sqft=5000.0),
            ],
            "dupes.xlsx", content_hash="dupe-heading-hash",
        )

        at = _run_review_page()

        self.assertIn(self.HEADING, [s.value for s in at.subheader])
        captions = "".join(c.value for c in at.caption)
        self.assertIn("same property may have been listed twice in this upload", captions)
        self.assertIn(self.EXPLAINER_TAIL, captions)

    def test_heading_absent_when_nothing_needs_a_decision(self):
        save_staging_file(
            [ListingRow(building="1 Example Street", provider="Test Provider")],
            "plain.xlsx", content_hash="no-decision-hash",
        )

        at = _run_review_page()

        self.assertNotIn(self.HEADING, [s.value for s in at.subheader])

    def test_different_provider_near_name_match_never_triggers_the_heading(self):
        # The exact same real fuzzy pair as test_heading_and_explainer_for_
        # near_miss above, but with the incoming row's provider changed to
        # a DIFFERENT one (MetSpace vs UNION - the real confirmed
        # "Clerkenwell Road" case's own shape) - provider is part of
        # listing identity, so this must land as a plain new property, not
        # a "possible near-miss" needing a manual decision.
        master_writer.write_master([
            ListingRow(
                building="80 Clerkenwell Road", provider="UNION", floor_unit="2nd", size_sqft=1000.0,
                property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [ListingRow(building="Clerkenwell Road", provider="MetSpace", floor_unit="4th Floor", size_sqft=2000.0)],
            "different_provider.xlsx", content_hash="different-provider-hash",
        )

        at = _run_review_page()
        self.assertFalse(at.exception)

        self.assertNotIn(self.HEADING, [s.value for s in at.subheader])
        self.assertIn("📄 New properties", [s.value for s in at.subheader])


class BehaviorUnchangedAfterRestructureTests(IsolatedCwdTestCase):
    def test_near_miss_link_choice_still_updates_the_right_master_index(self):
        master_writer.write_master([
            ListingRow(
                building="Thirty Lighterman", provider="Kitt's", floor_unit="3rd Floor", size_sqft=1000.0,
                # Explicit, stable property_id - build_merge_plan backfills a
                # FRESH random uuid for any blank one on every single render
                # pass, which would make the near-miss dropdown's option
                # identity change out from under a simulated selection on
                # the very next rerun. A real master.xlsx never has a blank
                # property_id (every row gets one for real at approve-time),
                # so this only matters for a test fixture built directly via
                # write_master rather than through the app's own approve flow.
                property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [ListingRow(building="Thirty Lightman", provider="Kitt's", floor_unit="5th Floor", size_sqft=2000.0)],
            "near_miss.xlsx", content_hash="near-miss-link-hash",
        )

        at = _run_review_page()
        # Only one master property exists at all, so this near-miss has
        # exactly one suggestion - the Yes/No button pair (see pages/2_
        # Review_and_Master.py's near-miss redesign), not the dropdown.
        yes_button = next(b for b in at.button if b.label == "✓ Yes, same property")
        yes_button.click().run()
        self.assertFalse(at.exception)

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        self.assertEqual(len(approve_buttons), 1)
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)  # linked, not added as a second row
        self.assertEqual(master_df.iloc[0]["size_sqft"], 2000.0)

    def test_duplicate_merge_confirmation_still_produces_one_merged_row(self):
        save_staging_file(
            [
                ListingRow(building="1 Example Street", provider="Test Provider", floor_unit="1st", size_sqft=1000.0),
                ListingRow(building="1 Example Street", provider="Test Provider", floor_unit="1st", size_sqft=1000.0),
            ],
            "dupes.xlsx", content_hash="dupe-merge-hash",
        )

        at = _run_review_page()
        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        self.assertEqual(len(approve_buttons), 1)
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)  # merged into one, not added as two


if __name__ == "__main__":
    unittest.main()
