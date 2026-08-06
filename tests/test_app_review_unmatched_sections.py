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

        plain_new_expanders = [e for e in at.expander if (e.label or "").startswith("📄")]
        self.assertEqual(len(plain_new_expanders), 1)
        self.assertEqual(plain_new_expanders[0].label, "📄 3 new properties will be added — click to view")
        # Streamlit expanders default to collapsed unless expanded=True is
        # passed - confirm this one wasn't given that.
        self.assertFalse(plain_new_expanders[0].proto.expanded)

    def test_no_plain_new_section_when_there_are_no_plain_new_rows(self):
        # Everything in this batch is a genuine duplicate of everything
        # else - no plain_new rows at all.
        save_staging_file(
            [
                ListingRow(building="1 Example Street", provider="Test Provider", floor_unit="1st"),
                ListingRow(building="1 Example Street", provider="Test Provider", floor_unit="1st"),
            ],
            "dupes.xlsx", content_hash="no-plain-new-hash",
        )

        at = _run_review_page()

        self.assertEqual([e for e in at.expander if (e.label or "").startswith("📄")], [])


class NeedsADecisionHeadingTests(IsolatedCwdTestCase):
    HEADING = "⚠️ Needs a decision"
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
        # unmatched_collisions rather than near_miss.
        save_staging_file(
            [
                ListingRow(building="1 Example Street", provider="Test Provider", floor_unit="1st"),
                ListingRow(building="1 Example Street", provider="Test Provider", floor_unit="1st"),
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
        selectbox = at.selectbox(key="near_miss_0_choice")
        link_option = next(o for o in selectbox.options if o != "— add as new —")
        selectbox.select(link_option).run()
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
