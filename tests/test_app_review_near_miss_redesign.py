"""
Regression tests for the redesigned near-miss decision card in pages/
2_Review_and_Master.py (see _render_near_miss_summary/_render_near_miss_
comparison_table/_render_near_miss_link_diff and the `if near_miss:` block
in _render_pending_review).

Purely a display/interaction change - covers the single-suggestion Yes/No
path, the multiple-suggestion dropdown path (still offered when a button
pair can't represent the choice), and the zero-diff-after-linking edge
case, confirming all three still populate decision_updates/new_rows_final
exactly like the original plain dropdown did.

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_page_restructure.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_near_miss_redesign -v
"""

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import master_writer
from schema import ListingRow
from storage.file_store import save_staging_file
from streamlit.testing.v1 import AppTest

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


class SingleSuggestionYesNoTests(IsolatedCwdTestCase):
    """Exactly one fuzzy suggestion - the common case, redesigned to a
    Yes/No button pair. One field (floor_unit) genuinely differs; the
    rest (building/provider/address_1/size_sqft) match exactly."""

    def _stage_single_near_miss(self):
        master_writer.write_master([
            ListingRow(
                building="Thirty Lighterman", provider="Kitt's", address_1="Thirty Lighterman Wharf",
                size_sqft=1000.0, property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [ListingRow(
                building="Thirty Lighterman", provider="Kitt's", address_1="Thirty Lighterman Wharf",
                size_sqft=1000.0, floor_unit="Ground Floor",
            )],
            "single_near_miss.xlsx", content_hash="single-near-miss-hash",
        )
        return _run_review_page()

    def test_summary_sentence_names_matches_and_the_one_difference(self):
        at = self._stage_single_near_miss()
        self.assertFalse(at.exception)

        markdown_text = "".join(m.value for m in at.markdown)
        self.assertIn("same building, provider, address, and size", markdown_text)
        self.assertIn("The only thing different is the floor", markdown_text)
        self.assertIn("master has none recorded", markdown_text)
        self.assertIn("this upload says 'Ground Floor'", markdown_text)

    def test_comparison_table_shows_friendly_labels_and_headers(self):
        at = self._stage_single_near_miss()
        markdown_text = "".join(m.value for m in at.markdown)
        self.assertIn("Floor / Unit", markdown_text)
        self.assertIn("Address", markdown_text)
        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("IN MASTER", caption_text)
        self.assertIn("THIS UPLOAD", caption_text)

    def test_differing_row_is_highlighted(self):
        at = self._stage_single_near_miss()
        markdown_text = "".join(m.value for m in at.markdown)
        self.assertIn(":orange-background[Floor / Unit]", markdown_text)
        self.assertIn(":orange-background[Ground Floor]", markdown_text)
        # A matching field must never be wrapped in the highlight markup.
        self.assertNotIn(":orange-background[Address]", markdown_text)

    def test_yes_no_buttons_shown_not_a_dropdown(self):
        at = self._stage_single_near_miss()
        button_labels = {b.label for b in at.button}
        self.assertIn("✓ Yes, same property", button_labels)
        self.assertIn("Keep as new property", button_labels)
        self.assertEqual([sb for sb in at.selectbox if sb.key == "near_miss_0_choice"], [])

    def test_default_without_touching_anything_adds_as_new(self):
        # Must match the OLD selectbox's own default behavior exactly.
        at = self._stage_single_near_miss()
        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 2)  # original + the new one, never linked

    def test_clicking_yes_links_instead_of_adding_a_second_row(self):
        at = self._stage_single_near_miss()
        yes_button = next(b for b in at.button if b.label == "✓ Yes, same property")
        yes_button.click().run()
        self.assertFalse(at.exception)

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)  # linked, not added as a second row
        self.assertEqual(master_df.iloc[0]["floor_unit"], "Ground Floor")

    def test_clicking_add_as_new_after_yes_reverts_to_adding_as_new(self):
        at = self._stage_single_near_miss()
        yes_button = next(b for b in at.button if b.label == "✓ Yes, same property")
        yes_button.click().run()
        no_button = next(b for b in at.button if b.label == "Keep as new property")
        no_button.click().run()
        self.assertFalse(at.exception)

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 2)  # back to two separate properties

    def test_no_caption_shown_before_any_click(self):
        # The default ("Keep as new property") is already communicated by
        # the button pair's own primary/secondary styling - an
        # unconditional caption here was confirmed confusing to reviewers.
        at = self._stage_single_near_miss()
        caption_text = "".join(c.value for c in at.caption)
        self.assertNotIn("Will be added as a new property", caption_text)
        self.assertNotIn("Will be merged into", caption_text)

    def test_clicking_keep_as_new_shows_the_new_property_caption(self):
        at = self._stage_single_near_miss()
        no_button = next(b for b in at.button if b.label == "Keep as new property")
        no_button.click().run()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("✓ Will be added as a new property", caption_text)
        self.assertNotIn("Will be merged into", caption_text)

    def test_clicking_yes_shows_the_merge_caption_naming_the_master_row(self):
        at = self._stage_single_near_miss()
        yes_button = next(b for b in at.button if b.label == "✓ Yes, same property")
        yes_button.click().run()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("🔗 Will be merged into Thirty Lighterman — Kitt's as an update", caption_text)
        self.assertNotIn("Will be added as a new property", caption_text)

    def test_caption_updates_when_switching_back_after_yes(self):
        at = self._stage_single_near_miss()
        yes_button = next(b for b in at.button if b.label == "✓ Yes, same property")
        at = yes_button.click().run()
        no_button = next(b for b in at.button if b.label == "Keep as new property")
        no_button.click().run()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("✓ Will be added as a new property", caption_text)
        self.assertNotIn("Will be merged into", caption_text)


class ZeroDiffAfterLinkingTests(IsolatedCwdTestCase):
    """The new row leaves floor_unit blank (no data this time) while master
    has one recorded - diff_fields' own blank-skip rule means linking
    produces ZERO field diffs, even though the row is a genuine near-miss
    (floor_unit mismatch is exactly why the real matching tiers didn't
    already match it directly). Must render and approve with no error."""

    def _stage_zero_diff_near_miss(self):
        master_writer.write_master([
            ListingRow(
                building="Thirty Lighterman", provider="Kitt's", floor_unit="3rd Floor",
                size_sqft=1000.0, property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [ListingRow(building="Thirty Lighterman", provider="Kitt's", size_sqft=1000.0)],
            "zero_diff_near_miss.xlsx", content_hash="zero-diff-near-miss-hash",
        )
        return _run_review_page()

    def test_renders_with_no_exception(self):
        at = self._stage_zero_diff_near_miss()
        self.assertFalse(at.exception)
        self.assertIn("⚠️ Needs your decision", [s.value for s in at.subheader])

    def test_zero_fields_would_change_and_no_field_rows_rendered(self):
        at = self._stage_zero_diff_near_miss()
        yes_button = next(b for b in at.button if b.label == "✓ Yes, same property")
        yes_button.click().run()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("Linked to an existing property — 0 field(s) would change.", caption_text)
        self.assertEqual([c for c in at.checkbox if c.label == "Apply"], [])

    def test_approve_after_linking_with_zero_diffs_does_not_error(self):
        at = self._stage_zero_diff_near_miss()
        yes_button = next(b for b in at.button if b.label == "✓ Yes, same property")
        yes_button.click().run()

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)  # linked, not duplicated
        self.assertEqual(master_df.iloc[0]["floor_unit"], "3rd Floor")  # untouched, never blanked out


class MultipleSuggestionsDropdownTests(IsolatedCwdTestCase):
    """Two master properties both fuzzy-match the new row's building name -
    the dropdown path must still be offered (a Yes/No pair can't represent
    a 3-way choice), and linking through it must still work exactly as
    before."""

    def _stage_multi_suggestion_near_miss(self):
        master_writer.write_master([
            ListingRow(
                building="Thirty Lighterman", provider="Kitt's", floor_unit="3rd Floor",
                size_sqft=1000.0, property_id=str(uuid.uuid4()),
            ),
            ListingRow(
                building="Thirty Lightmann", provider="Kitt's", floor_unit="4th Floor",
                size_sqft=1500.0, property_id=str(uuid.uuid4()),
            ),
        ])
        save_staging_file(
            [ListingRow(
                building="Thirty Lightman", provider="Kitt's", floor_unit="5th Floor", size_sqft=2000.0,
            )],
            "multi_near_miss.xlsx", content_hash="multi-near-miss-hash",
        )
        return _run_review_page()

    def test_dropdown_shown_not_yes_no_buttons(self):
        at = self._stage_multi_suggestion_near_miss()
        self.assertFalse(at.exception)

        selectboxes = [sb for sb in at.selectbox if sb.key == "near_miss_0_choice"]
        self.assertEqual(len(selectboxes), 1)
        button_labels = {b.label for b in at.button}
        self.assertNotIn("✓ Yes, same property", button_labels)
        self.assertNotIn("Keep as new property", button_labels)

    def test_summary_and_table_compare_against_the_closest_suggestion(self):
        # closest = suggestions[0] - "Thirty Lighterman" (master_records
        # order). Building itself is a genuine spelling difference (diff_
        # fields tolerates case/whitespace only, never a real typo), so it
        # correctly shows up as differing here, alongside floor and size -
        # only provider and address actually match between the two.
        at = self._stage_multi_suggestion_near_miss()
        markdown_text = "".join(m.value for m in at.markdown)
        self.assertIn("same provider and address", markdown_text)
        self.assertIn("The things that differ are building, floor, and size", markdown_text)

    def test_selecting_a_suggestion_from_the_dropdown_still_links_it(self):
        at = self._stage_multi_suggestion_near_miss()
        selectbox = next(sb for sb in at.selectbox if sb.key == "near_miss_0_choice")
        link_option = next(o for o in selectbox.options if "Thirty Lightmann" in o)
        selectbox.select(link_option).run()
        self.assertFalse(at.exception)

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 2)  # linked to the SECOND master row, not added as a third
        # Identified by floor_unit, not building - building itself is one
        # of the (non-risky, auto-applied) diffs, so it's overwritten to
        # the upload's own spelling ("Thirty Lightman") on approve, exactly
        # like any other safe field diff would be - unrelated to this
        # redesign, just this fixture's own genuine field difference.
        linked_row = master_df[master_df["floor_unit"] == "5th Floor"].iloc[0]
        self.assertEqual(linked_row["building"], "Thirty Lightman")
        self.assertEqual(linked_row["size_sqft"], 2000.0)

    def test_default_without_touching_the_dropdown_still_adds_as_new(self):
        at = self._stage_multi_suggestion_near_miss()
        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 3)  # both originals, plus the new one added separately


if __name__ == "__main__":
    unittest.main()
