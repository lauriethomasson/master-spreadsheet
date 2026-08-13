"""
Regression tests for the Review page's presentation/interaction restructure
(see pages/2_Review_and_Master.py's _render_pending_review): manual property-
level decisions grouped together under one "Needs your decision" heading at
the top, an "Automatic updates" summary (+ "View changes" expander) instead
of field-by-field Apply buttons for changes already considered safe, a "New
properties" summary (+ optional expander), and a "No changes" line - in that
order, always, never interleaved. The manual_review_toggle field-by-field
review mode is gone entirely - safe changes never show a per-field checkbox.

Also covers the reframed let-status decision (see _render_let_status_
decision): three explicit choices (apply the new status / remove the
property / keep current information and ignore this update), never exposed
as a raw special_features/state_of_space before/after field diff, and
"Under Offer"-style wording must NOT default to removing the property.

Runs from an isolated temporary working directory (never the real repo).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_page_restructure -v
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
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()


def _run_review_page():
    at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
    at.run()
    return at


class SectionOrderingTests(IsolatedCwdTestCase):
    """All four sections present at once, in the mandated order, with
    manual decisions never interleaved among the automatic ones."""

    def _mixed_batch(self):
        # 1 property needing a decision (let-status), 1 auto-updated
        # property, 1 new property, 1 unchanged property - all four
        # sections present simultaneously.
        master_writer.write_master([
            ListingRow(
                building="15 Hatfields", provider="Knotel", floor_unit="3rd Floor",
                special_features="Available", property_id=str(uuid.uuid4()),
            ),
            ListingRow(
                building="1 Finsbury Market", provider="Knotel", floor_unit="3rd Floor",
                size_sqft=1000.0, property_id=str(uuid.uuid4()),
            ),
            ListingRow(
                building="Unchanged House", provider="Knotel", floor_unit="1st Floor",
                property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [
                ListingRow(
                    building="15 Hatfields", provider="Knotel", floor_unit="3rd Floor",
                    special_features="Under Offer",
                ),
                ListingRow(
                    building="1 Finsbury Market", provider="Knotel", floor_unit="3rd Floor",
                    size_sqft=1200.0,
                ),
                ListingRow(building="Unchanged House", provider="Knotel", floor_unit="1st Floor"),
                ListingRow(building="Brand New House", provider="Knotel", floor_unit="2nd Floor"),
            ],
            "mixed.xlsx", content_hash="mixed-sections-hash",
        )
        return _run_review_page()

    def test_all_four_sections_appear_in_order(self):
        at = self._mixed_batch()
        self.assertFalse(at.exception)

        headings = [s.value for s in at.subheader]
        expected_present = ["⚠️ Needs your decision", "✅ Automatic updates", "📄 New properties"]
        for heading in expected_present:
            self.assertIn(heading, headings)

        # Strict relative order - decisions first, then automatic updates,
        # then new properties.
        indices = [headings.index(h) for h in expected_present]
        self.assertEqual(indices, sorted(indices))

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("1 property matched with no changes.", caption_text)

    def test_no_field_by_field_apply_checkboxes_for_the_safe_update(self):
        at = self._mixed_batch()
        # The old field-by-field UI rendered an "Apply this change"
        # checkbox per field - none should exist anywhere now for a change
        # already considered safe (only genuinely risky/collision fields
        # would ever show one, and there are none in this batch).
        apply_checkboxes = [c for c in at.checkbox if c.label == "Apply this change"]
        self.assertEqual(apply_checkboxes, [])

    def test_manual_review_toggle_no_longer_exists(self):
        at = self._mixed_batch()
        self.assertEqual([t for t in at.toggle if t.key == "manual_review_toggle"], [])


class PentonvilleStyleAutomaticUpdateTests(IsolatedCwdTestCase):
    """The real confirmed case: a real brochure_link change alongside a
    special_features update whose only real difference is an updated
    availability statement plus a restated short feature item must land
    under Automatic updates, never Needs your decision."""

    def test_44_pentonville_style_row_lands_in_automatic_updates_not_needs_a_decision(self):
        master_writer.write_master([
            ListingRow(
                building="44 Pentonville Road", provider="MetSpace",
                brochure_link="https://drive.google.com/file/d/OLDID/view",
                special_features="4 MR + 3 PB; Available: Now",
                property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [ListingRow(
                building="44 Pentonville Road", provider="MetSpace",
                brochure_link="https://drive.google.com/file/d/NEWID/view",
                special_features="4 MR + 3 PB; Available: December",
            )],
            "pentonville.xlsx", content_hash="pentonville-hash",
        )

        at = _run_review_page()
        self.assertFalse(at.exception)

        headings = [s.value for s in at.subheader]
        self.assertIn("✅ Automatic updates", headings)
        self.assertNotIn("⚠️ Needs your decision", headings)

        view_changes = [e for e in at.expander if e.label == "View changes"]
        self.assertEqual(len(view_changes), 1)
        expander_text = "".join(m.value for m in view_changes[0].markdown)
        self.assertIn("44 Pentonville Road", expander_text)
        self.assertIn("Brochure Link", expander_text)  # friendly display label, not the raw field name
        self.assertIn("Special Features", expander_text)

        markdown_text = "".join(m.value for m in at.markdown)
        # No duplicated "4 MR + 3 PB" in whatever gets rendered as the new value.
        self.assertNotIn("4 MR + 3 PB; Available: December; 4 MR + 3 PB", markdown_text)


class UncommonLiverpoolStStyleCompactDecisionTests(IsolatedCwdTestCase):
    """
    The real reported case: an address change (house_number_changed - see
    master_merge.HOUSE_NUMBER_FIELDS) forces a property into manual review,
    but three other ordinary blank-field additions on that SAME property
    (size_sqft/rent_pcm/desks_max) are not themselves risky - they must
    never get their own full before/after card + checkbox; only the
    genuinely risky field does, with a plain-English reason, and the rest
    are bundled into a single "N other safe changes" line while STILL
    being applied automatically on Approve.
    """

    def _staged_uncommon_liverpool_st(self):
        # building/provider/floor_unit stay IDENTICAL between master and
        # the upload (a genuine matched-row update, not a new property) -
        # only address_1 changes, from a value with no leading house
        # number to one with a real, different leading number ("34"),
        # which is exactly the shape master_merge.house_number_changed
        # flags as risky.
        master_writer.write_master([
            ListingRow(
                building="Uncommon Liverpool St", provider="Uncommon", floor_unit="5th Floor",
                address_1="Uncommon Liverpool St", property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [ListingRow(
                building="Uncommon Liverpool St", provider="Uncommon", floor_unit="5th Floor",
                address_1="34-37 Liverpool Street", size_sqft=1638.0, rent_pcm=49500.0, desks_max=30,
            )],
            "uncommon.xlsx", content_hash="uncommon-liverpool-st-hash",
        )
        return _run_review_page()

    def test_only_one_decision_needed_for_the_risky_field(self):
        at = self._staged_uncommon_liverpool_st()
        self.assertFalse(at.exception)

        self.assertIn("⚠️ Needs your decision", [s.value for s in at.subheader])
        expanders = [e for e in at.expander if "1 decision needed" in (e.label or "")]
        self.assertEqual(len(expanders), 1)

    def test_reason_and_friendly_label_shown_for_the_risky_field(self):
        at = self._staged_uncommon_liverpool_st()
        markdown_text = "".join(m.value for m in at.markdown)
        self.assertIn("Address", markdown_text)  # friendly label, not "address_1"
        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("Existing address would be replaced", caption_text)

    def test_safe_fields_are_bundled_not_individually_rendered(self):
        at = self._staged_uncommon_liverpool_st()
        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("3 other safe changes will be applied automatically.", caption_text)
        # No individual "Apply" checkbox for the 3 safe fields - only the
        # one risky field (address) gets a checkbox at all.
        apply_checkboxes = [c for c in at.checkbox if c.label == "Apply"]
        self.assertEqual(len(apply_checkboxes), 1)

    def test_leaving_the_risky_checkbox_unchecked_still_applies_the_safe_fields(self):
        at = self._staged_uncommon_liverpool_st()
        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()

        master_df = master_writer.load_master_as_dataframe()
        row = master_df.iloc[0]
        self.assertEqual(row["address_1"], "Uncommon Liverpool St")  # unchanged - the risky field was never approved
        self.assertEqual(row["size_sqft"], 1638.0)
        self.assertEqual(row["rent_pcm"], 49500.0)
        self.assertEqual(row["desks_max"], 30)

    def test_checking_the_risky_checkbox_also_applies_the_address_change(self):
        at = self._staged_uncommon_liverpool_st()
        apply_checkboxes = [c for c in at.checkbox if c.label == "Apply"]
        apply_checkboxes[0].set_value(True).run()

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()

        master_df = master_writer.load_master_as_dataframe()
        row = master_df.iloc[0]
        self.assertEqual(row["address_1"], "34-37 Liverpool Street")
        self.assertEqual(row["size_sqft"], 1638.0)
        self.assertEqual(row["rent_pcm"], 49500.0)
        self.assertEqual(row["desks_max"], 30)


class LetStatusThreeChoiceTests(IsolatedCwdTestCase):
    def _staged_under_offer(self):
        master_writer.write_master([
            ListingRow(
                building="15 Hatfields", provider="Knotel", floor_unit="3rd Floor",
                special_features="Available",
                # Explicit, stable property_id - build_merge_plan backfills
                # a FRESH random uuid for any blank one on every single
                # render pass, which would make the let-status radio's own
                # widget key (keyed on property_id) change out from under a
                # simulated selection on the very next rerun. A real
                # master.xlsx never has a blank property_id (every row gets
                # one for real at approve-time) - this only matters for a
                # test fixture built directly via write_master rather than
                # through the app's own approve flow (see test_app_review_
                # unmatched_sections.py's own identical note).
                property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [ListingRow(
                building="15 Hatfields", provider="Knotel", floor_unit="3rd Floor",
                special_features="Under Offer",
            )],
            "knotel.xlsx", content_hash="let-status-hash",
        )
        return _run_review_page()

    def test_property_level_framing_not_a_raw_field_diff(self):
        at = self._staged_under_offer()
        self.assertFalse(at.exception)

        warning_text = "".join(w.value for w in at.warning)
        self.assertIn("15 Hatfields", warning_text)
        self.assertIn("now lists this space as", warning_text)
        self.assertIn("Under Offer", warning_text)
        # Never the raw field name as a plain label the way an ordinary
        # field diff would show it.
        self.assertNotIn("**special_features**", warning_text)

    def test_three_choices_offered(self):
        at = self._staged_under_offer()
        radios = [r for r in at.radio if r.label == "What would you like to do?"]
        self.assertEqual(len(radios), 1)
        options = radios[0].options
        self.assertEqual(len(options), 3)
        self.assertTrue(any(o.startswith("Keep as Under Offer") for o in options))
        self.assertTrue(any(o.startswith("Remove property") for o in options))
        self.assertTrue(any(o.startswith("Keep current information") for o in options))

    def test_under_offer_does_not_default_to_remove(self):
        # Approving without ever touching the radio must apply the new
        # status, never silently remove the property.
        at = self._staged_under_offer()
        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)
        self.assertEqual(master_df.iloc[0]["special_features"], "Under Offer")

    def test_remove_property_choice_removes_it(self):
        at = self._staged_under_offer()
        radio = next(r for r in at.radio if r.label == "What would you like to do?")
        remove_option = next(o for o in radio.options if o.startswith("Remove property"))
        radio.set_value(remove_option)
        at.run()
        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 0)

    def test_keep_current_information_choice_leaves_the_record_untouched(self):
        at = self._staged_under_offer()
        radio = next(r for r in at.radio if r.label == "What would you like to do?")
        ignore_option = next(o for o in radio.options if o.startswith("Keep current information"))
        radio.set_value(ignore_option)
        at.run()
        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)
        # Still "Available" - this update contributed nothing at all.
        self.assertEqual(master_df.iloc[0]["special_features"], "Available")


if __name__ == "__main__":
    unittest.main()
