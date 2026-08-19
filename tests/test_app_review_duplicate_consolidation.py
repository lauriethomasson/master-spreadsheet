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


class MergeDuplicateListingsTests(IsolatedCwdTestCase):
    """
    The "Same listing — merge" choice on the duplicate-listing review card
    (pages/2_Review_and_Master.py's _render_intra_batch_duplicate_group) -
    for a group whose only genuine disagreement is a RISKY_TEXT_FIELDS
    field, lets a reviewer combine both texts into one edited value instead
    of being forced to discard one wholesale.
    """

    def test_special_features_disagreement_offers_merge_and_produces_combined_row(self):
        # The real confirmed Kitt's case ("8 Laurence Pountney Hill — G"): a
        # bare YouTube link vs prose mentioning "Space video" share no
        # actual text at all, so the existing auto-merge check
        # (master_merge._text_variants_compatible) correctly refuses to
        # silently combine them - but a human can tell they're the same
        # video, so this needs a way to combine the two texts rather than
        # forcing a wholesale pick of one over the other.
        save_staging_file(
            [
                ListingRow(
                    building="8 Laurence Pountney Hill", provider="Kitt's", floor_unit="G",
                    special_features="https://www.youtube.com/watch?v=DbjHrR1fQF4",
                ),
                ListingRow(
                    building="8 Laurence Pountney Hill", provider="Kitt's", floor_unit="G",
                    special_features="Free communal meeting rooms and business lounge; Space video",
                ),
            ],
            "kitts.xlsx", content_hash="kitts-hash",
        )

        at = _run_review_page()
        self.assertIn("⚠️ Needs your decision", [s.value for s in at.subheader])

        radio = next(r for r in at.radio if r.label == "What are these?")
        self.assertIn("Same listing — merge the two", radio.options)
        radio.set_value("Same listing — merge the two")
        at.run()
        self.assertFalse(at.exception)

        # Pre-filled with a plain, uncleaned join of both listings' own
        # values - never auto-deduplicated - the reviewer edits it in.
        text_areas = [t for t in at.text_area if t.label == "Special Features"]
        self.assertEqual(len(text_areas), 1)
        self.assertEqual(
            text_areas[0].value,
            "https://www.youtube.com/watch?v=DbjHrR1fQF4; "
            "Free communal meeting rooms and business lounge; Space video",
        )
        text_areas[0].set_value(
            "Free communal meeting rooms and business lounge "
            "(https://www.youtube.com/watch?v=DbjHrR1fQF4)"
        )
        at.run()
        self.assertFalse(at.exception)

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)
        self.assertEqual(
            master_df.iloc[0]["special_features"],
            "Free communal meeting rooms and business lounge (https://www.youtube.com/watch?v=DbjHrR1fQF4)",
        )
        self.assertEqual(master_df.iloc[0]["building"], "8 Laurence Pountney Hill")

    def test_non_text_field_disagreement_offers_no_merge_option(self):
        # rent_psf disagreeing is a genuine conflict (see master_merge.
        # genuinely_differing_fields), but it isn't a RISKY_TEXT_FIELDS
        # field - merging free text isn't a meaningful operation for it, so
        # the merge choice must never even be offered here.
        save_staging_file(
            [
                ListingRow(building="28 Bruton Street", provider="Kitt's", floor_unit="4th", rent_psf=45.0),
                ListingRow(building="28 Bruton Street", provider="Kitt's", floor_unit="4th", rent_psf=60.0),
            ],
            "kitts_rent.xlsx", content_hash="kitts-rent-hash",
        )

        at = _run_review_page()
        self.assertIn("⚠️ Needs your decision", [s.value for s in at.subheader])

        radio = next(r for r in at.radio if r.label == "What are these?")
        self.assertEqual(radio.options, [
            "Keep both — separate listings", "Same listing — use Listing A", "Same listing — use Listing B",
        ])
        self.assertEqual([t for t in at.text_area if t.label in ("Special Features", "Contacts")], [])

    def test_two_differing_text_fields_show_two_separate_boxes(self):
        save_staging_file(
            [
                ListingRow(
                    building="1 Example Street", provider="UNION", floor_unit="1st",
                    special_features="Rooftop terrace with panoramic views",
                    contacts="Alice Smith, alice@firmone.co.uk, 07111 111111",
                ),
                ListingRow(
                    building="1 Example Street", provider="UNION", floor_unit="1st",
                    special_features="Bike storage and shower facilities",
                    contacts="Bob Jones, bob@differentco.com, 07222 222222",
                ),
            ],
            "two_fields.xlsx", content_hash="two-fields-hash",
        )

        at = _run_review_page()
        self.assertIn("⚠️ Needs your decision", [s.value for s in at.subheader])

        radio = next(r for r in at.radio if r.label == "What are these?")
        self.assertIn("Same listing — merge the two", radio.options)
        radio.set_value("Same listing — merge the two")
        at.run()
        self.assertFalse(at.exception)

        self.assertEqual(len([t for t in at.text_area if t.label == "Special Features"]), 1)
        self.assertEqual(len([t for t in at.text_area if t.label == "Contacts"]), 1)
        special_box = next(t for t in at.text_area if t.label == "Special Features")
        contacts_box = next(t for t in at.text_area if t.label == "Contacts")
        self.assertEqual(
            special_box.value, "Rooftop terrace with panoramic views; Bike storage and shower facilities",
        )
        self.assertEqual(
            contacts_box.value,
            "Alice Smith, alice@firmone.co.uk, 07111 111111; Bob Jones, bob@differentco.com, 07222 222222",
        )


if __name__ == "__main__":
    unittest.main()
