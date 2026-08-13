"""
Streamlit-level regression tests for automatic intra-batch duplicate
consolidation on pages/2_Review_and_Master.py (see master_merge.
consolidate_unmatched_duplicates) - manual review becomes the exception (a
genuine field conflict), not the default just because a property was
extracted more than once, whether within one upload, across several
pending uploads, or an existing master row plus a compatible incoming
duplicate.

Runs from an isolated temporary working directory (never the real repo),
same approach as test_app_review_master_lookup.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_duplicate_consolidation -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import master_writer
from schema import ListingRow
from storage.file_store import save_staging_file, update_staging_rows

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


class RealPatternConsolidationTests(IsolatedCwdTestCase):
    def test_107_cannon_street_three_copies_across_three_uploads_consolidates_silently(self):
        # The exact real reported pattern: "107 Cannon Street — UNION —
        # 4th" appearing 3 times, once per pending UNION upload, with
        # compatible (not conflicting) data.
        for i in range(3):
            save_staging_file(
                [ListingRow(
                    building="107 Cannon Street", provider="UNION", floor_unit="4th", size_sqft=4500.0,
                    rent_pcm=15000.0, brochure_link="https://app.box.com/s/capjtfw406d9zitn73ywohhvioti2aru",
                )],
                f"UNION_upload_{i}.xlsx", content_hash=f"union-hash-{i}",
            )

        at = _run_review_page()
        self.assertFalse(at.exception)

        # No "Possible duplicate" card at all - fully auto-consolidated.
        self.assertEqual([e for e in at.expander if "Possible duplicate" in (e.label or "")], [])
        self.assertNotIn("⚠️ Needs your decision", [s.value for s in at.subheader])

        summary_text = "".join(c.value for c in at.caption)
        self.assertIn("3 extracted row(s)", summary_text)
        self.assertIn("2 duplicate row(s) automatically consolidated", summary_text)  # 3 rows -> 1, so 2 eliminated
        self.assertIn("0 conflict(s) need your review", summary_text)

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)
        self.assertEqual(master_df.iloc[0]["size_sqft"], 4500.0)

    def test_complementary_special_features_across_uploads_merges_richer_value(self):
        save_staging_file(
            [ListingRow(
                building="107 Cannon Street", provider="UNION", floor_unit="4th", size_sqft=4500.0,
                special_features=None,
            )],
            "upload_a.xlsx", content_hash="hash-a",
        )
        save_staging_file(
            [ListingRow(
                building="107 Cannon Street", provider="UNION", floor_unit="4th", size_sqft=4500.0,
                special_features="Fully fitted, meeting rooms",
            )],
            "upload_b.xlsx", content_hash="hash-b",
        )

        at = _run_review_page()
        self.assertEqual([e for e in at.expander if "Possible duplicate" in (e.label or "")], [])

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)
        self.assertEqual(master_df.iloc[0]["special_features"], "Fully fitted, meeting rooms")

    def test_conflicting_size_across_uploads_still_needs_review(self):
        save_staging_file(
            [ListingRow(building="107 Cannon Street", provider="UNION", floor_unit="4th", size_sqft=4500.0)],
            "upload_a.xlsx", content_hash="hash-a",
        )
        save_staging_file(
            [ListingRow(building="107 Cannon Street", provider="UNION", floor_unit="4th", size_sqft=9000.0)],
            "upload_b.xlsx", content_hash="hash-b",
        )

        at = _run_review_page()
        self.assertIn("⚠️ Needs your decision", [s.value for s in at.subheader])
        self.assertEqual(len([e for e in at.expander if "Possible duplicate" in (e.label or "")]), 1)

        summary_text = "".join(c.value for c in at.caption)
        self.assertIn("1 conflict(s) need your review", summary_text)


class OtherProvidersUnaffectedTests(IsolatedCwdTestCase):
    def test_kitts_duplicate_still_consolidates_and_copthall_is_untouched(self):
        save_staging_file(
            [
                ListingRow(building="33 Cavendish Square", provider="Kitt's", floor_unit="2nd Floor", desks_max=20),
                ListingRow(building="33 Cavendish Square", provider="Kitt's", floor_unit="2nd Floor", desks_max=20),
                ListingRow(building="Copthall House", provider="Copthall Estates", floor_unit="4th Floor"),
            ],
            "mixed.xlsx", content_hash="mixed-hash",
        )

        at = _run_review_page()
        self.assertFalse(at.exception)
        self.assertEqual([e for e in at.expander if "Possible duplicate" in (e.label or "")], [])

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 2)  # the Kitt's pair merged into one + the untouched Copthall row


class ExistingMasterInteractionTests(IsolatedCwdTestCase):
    def test_master_row_plus_compatible_incoming_duplicate_pair_updates_normally(self):
        # Master already has this property; THIS upload independently
        # contains two compatible copies of an update to it - exercises
        # the matched-collision path (plan.collisions), not unmatched_
        # collisions, but confirms the two paths don't interfere.
        master_writer.write_master([
            ListingRow(building="16 Dufour's Place", provider="UNION", floor_unit="3rd Floor", size_sqft=1200.0),
        ])
        save_staging_file(
            [
                ListingRow(building="16 Dufour's Place", provider="UNION", floor_unit="3rd Floor", size_sqft=1200.0, special_features="Roof terrace"),
                ListingRow(building="16 Dufour's Place", provider="UNION", floor_unit="3rd Floor", size_sqft=1200.0, special_features="Roof terrace"),
            ],
            "update.xlsx", content_hash="update-hash",
        )

        at = _run_review_page()
        self.assertFalse(at.exception)
        self.assertEqual([e for e in at.expander if "field(s) changed" in (e.label or "")], [])

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)
        self.assertEqual(master_df.iloc[0]["special_features"], "Roof terrace")

    def test_master_row_plus_genuine_incoming_conflict_needs_review(self):
        # A real detail-loss pattern (see master_merge.is_detail_loss) - a
        # re-upload's special_features drops real amenities master already
        # had, with nothing standing in for them - forces manual review
        # via the existing risky-fields mechanism, unrelated to this
        # change but confirming it still works alongside it.
        master_writer.write_master([
            ListingRow(
                building="16 Dufour's Place", provider="UNION", floor_unit="3rd Floor", size_sqft=1200.0,
                special_features="Roof terrace; showers; bike storage; meeting rooms",
            ),
        ])
        save_staging_file(
            [ListingRow(
                building="16 Dufour's Place", provider="UNION", floor_unit="3rd Floor", size_sqft=1200.0,
                special_features="Available now",
            )],
            "update.xlsx", content_hash="update-hash",
        )

        at = _run_review_page()
        self.assertIn("⚠️ Needs your decision", [s.value for s in at.subheader])


class SummaryBannerTests(IsolatedCwdTestCase):
    def test_no_summary_line_when_nothing_to_consolidate(self):
        save_staging_file(
            [ListingRow(building="1 Example Street", provider="Test Provider")],
            "plain.xlsx", content_hash="plain-hash",
        )

        at = _run_review_page()
        summary_text = "".join(c.value for c in at.caption)
        self.assertNotIn("automatically consolidated", summary_text)

    def test_summary_counts_are_accurate(self):
        save_staging_file(
            [
                ListingRow(building="A", provider="UNION", floor_unit="1st", size_sqft=1000.0),
                ListingRow(building="A", provider="UNION", floor_unit="1st", size_sqft=1000.0),
                ListingRow(building="A", provider="UNION", floor_unit="1st", size_sqft=1000.0),
                ListingRow(building="B", provider="UNION", floor_unit="1st", size_sqft=1000.0),
                ListingRow(building="B", provider="UNION", floor_unit="1st", size_sqft=9999.0),
                ListingRow(building="C", provider="UNION", floor_unit="1st"),
            ],
            "batch.xlsx", content_hash="batch-hash",
        )

        at = _run_review_page()
        summary_text = "".join(c.value for c in at.caption)
        # 6 extracted, 2 consolidated away (A's 3->1), 1 conflict group (B), 4 unique ready (A-merged, B x2 still individually present, C)
        self.assertIn("6 extracted row(s)", summary_text)
        self.assertIn("2 duplicate row(s) automatically consolidated", summary_text)
        self.assertIn("1 conflict(s) need your review", summary_text)


class OliverYardListingIdentityTests(IsolatedCwdTestCase):
    """
    The real confirmed report: "1 Oliver's Yard" (The Workplace Company) -
    two rows sharing building/provider/blank floor_unit from the same
    upload, but with dramatically different size/desks/rent, previously
    forced a field-by-field "7282 or 42892?" manual decision. See
    master_merge.ListingIdentityConflictTests for the underlying logic
    tests - this confirms the same fix end-to-end through the real page.
    """

    def test_oliver_s_yard_style_rows_need_no_manual_decision(self):
        save_staging_file(
            [
                ListingRow(
                    building="1 Oliver's Yard", provider="The Workplace Company",
                    address_1="1 Oliver's Yard, London", postcode="EC1Y 1DT",
                    size_sqft=7282.0, desks_min=52, desks_max=68, rent_pcm=45512.0,
                    brochure_link="https://www.canva.com/design/shared-brochure/view",
                ),
                ListingRow(
                    building="1 Oliver's Yard", provider="The Workplace Company",
                    address_1="1 Oliver's Yard, London", postcode="EC1Y 1DT",
                    size_sqft=42892.0, desks_min=200, desks_max=400, rent_pcm=230811.0,
                    brochure_link="https://www.canva.com/design/shared-brochure/view",
                ),
            ],
            "The Workplace Company Availability (1).xlsx", content_hash="oliver-hash",
        )

        at = _run_review_page()

        self.assertNotIn("⚠️ Needs your decision", [s.value for s in at.subheader])
        self.assertEqual([e for e in at.expander if "Possible duplicate" in (e.label or "")], [])
        self.assertEqual([r for r in at.radio if r.label == "Keep value from:"], [])

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 2)
        self.assertEqual(sorted(master_df["size_sqft"]), [7282.0, 42892.0])

    def test_genuinely_ambiguous_duplicate_shows_listing_a_b_with_context(self):
        # Only ONE signal differs (size) - genuinely ambiguous, still needs
        # review, but the UI must show "Listing A"/"Listing B" with their
        # own summarized facts and a single 3-way identity question, never
        # per-field radios with identical source labels.
        save_staging_file(
            [
                ListingRow(building="1 Oliver's Yard", provider="The Workplace Company", size_sqft=7282.0),
                ListingRow(building="1 Oliver's Yard", provider="The Workplace Company", size_sqft=42892.0),
            ],
            "The Workplace Company Availability (1).xlsx", content_hash="oliver-ambiguous-hash",
        )

        at = _run_review_page()

        self.assertIn("⚠️ Needs your decision", [s.value for s in at.subheader])
        markdown_text = "".join(m.value for m in at.markdown)
        self.assertIn("Listing A", markdown_text)
        self.assertIn("Listing B", markdown_text)
        self.assertIn("Size: 7,282 sq ft", markdown_text)  # st.write(str) renders via markdown in AppTest

        radios = [r for r in at.radio if r.label == "What are these?"]
        self.assertEqual(len(radios), 1)
        self.assertEqual(radios[0].options, [
            "Keep both — separate listings", "Same listing — use Listing A", "Same listing — use Listing B",
        ])
        # Never the old per-field "Keep value from:" radio for this identity question.
        self.assertEqual([r for r in at.radio if r.label == "Keep value from:"], [])

    def test_reprocessed_staging_rows_are_reclassified_on_the_very_next_render(self):
        # Part 4's own "stale staging/cache" question: build_merge_plan is
        # called fresh from this file's CURRENT staged rows on every Review
        # render (pages/2_Review_and_Master.py's own _render_pending_review)
        # - there is no separate cached "these two rows are a duplicate"
        # decision stored anywhere. Confirms that directly: a batch that
        # starts with genuinely insufficient evidence (needs a manual
        # decision) is immediately, correctly reclassified as two separate
        # listings on the NEXT render once the SAME staging entry's rows are
        # rewritten with fuller evidence - simulating a corrected re-
        # extraction of the same upload - with no leftover/stuck state from
        # the first render.
        path = save_staging_file(
            [
                ListingRow(building="1 Oliver's Yard", provider="The Workplace Company", size_sqft=7282.0),
                ListingRow(building="1 Oliver's Yard", provider="The Workplace Company", size_sqft=42892.0),
            ],
            "The Workplace Company Availability (1).xlsx", content_hash="oliver-reprocessed-hash",
        )

        at = _run_review_page()
        self.assertIn("⚠️ Needs your decision", [s.value for s in at.subheader])

        update_staging_rows(path, [
            ListingRow(
                building="1 Oliver's Yard", provider="The Workplace Company",
                size_sqft=7282.0, desks_min=52, desks_max=68, rent_pcm=45512.0,
            ),
            ListingRow(
                building="1 Oliver's Yard", provider="The Workplace Company",
                size_sqft=42892.0, desks_min=200, desks_max=400, rent_pcm=230811.0,
            ),
        ])

        at = _run_review_page()

        self.assertNotIn("⚠️ Needs your decision", [s.value for s in at.subheader])
        self.assertEqual([e for e in at.expander if "Possible duplicate" in (e.label or "")], [])


if __name__ == "__main__":
    unittest.main()
