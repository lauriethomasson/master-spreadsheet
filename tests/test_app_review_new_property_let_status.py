"""
Regression tests for the new-property let-status decision (see pages/
2_Review_and_Master.py's _render_new_property_let_status_decision and
master_merge.UnmatchedRow.let_status_fields).

Real, confirmed gap this closes: master_merge.mentions_let_status (via
MatchedRow.let_status_fields) was only ever checked for a row that matched
an EXISTING master record and changed - a brand-new property (no master
match at all) whose own special_features/state_of_space text ALREADY says
"Under Offer"/"Let"/etc. previously sailed straight into "New properties"
with zero decision prompt at all.

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_page_restructure.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_new_property_let_status -v
"""

import os
import sys
import tempfile
import unittest
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


class NewPropertyLetStatusDecisionTests(IsolatedCwdTestCase):
    def _staged_under_offer_new_property(self):
        # No master.xlsx at all - this row can never match anything, the
        # exact plain_new shape (no master, no near-miss, no batch
        # collision) this fix targets.
        save_staging_file(
            [ListingRow(
                building="9 Example Yard", provider="Test Provider", floor_unit="2nd Floor",
                special_features="Under Offer",
            )],
            "new_under_offer.xlsx", content_hash="new-property-let-status-hash",
        )
        return _run_review_page()

    def test_needs_a_decision_instead_of_silently_landing_in_new_properties(self):
        at = self._staged_under_offer_new_property()
        self.assertFalse(at.exception)

        self.assertIn("⚠️ Needs your decision", [s.value for s in at.subheader])
        # Never silently added - "New properties" must not claim this row.
        self.assertNotIn("📄 New properties", [s.value for s in at.subheader])

    def test_warning_names_the_property_and_its_own_status_text(self):
        at = self._staged_under_offer_new_property()
        warning_text = "".join(w.value for w in at.warning)
        self.assertIn("9 Example Yard", warning_text)
        self.assertIn("Under Offer", warning_text)

    def test_only_the_trigger_phrase_is_shown_not_the_whole_field(self):
        # Real Workplace Plus shape: "U/O" buried in a long amenity list -
        # same fix as the matched-row prompt (see master_merge.
        # let_status_display_text), applied consistently here too.
        save_staging_file(
            [ListingRow(
                building="11 Example Yard", provider="Test Provider",
                special_features="52 + 2 MR + 3 PB + BR; U/O; term 2 - 5 years",
            )],
            "new_uo.xlsx", content_hash="new-property-buried-phrase-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        warning_text = "".join(w.value for w in at.warning)
        self.assertIn("U/O", warning_text)
        self.assertNotIn("52 + 2 MR + 3 PB + BR", warning_text)
        self.assertNotIn("term 2 - 5 years", warning_text)

    def test_two_choices_offered_not_the_matched_row_three(self):
        # "Remove property"/"Keep current information" both presuppose an
        # existing master record - neither applies to a brand-new property.
        at = self._staged_under_offer_new_property()
        radios = [r for r in at.radio if r.label == "What would you like to do?"]
        self.assertEqual(len(radios), 1)
        options = radios[0].options
        self.assertEqual(len(options), 2)
        self.assertTrue(any(o.startswith("Add anyway") for o in options))
        self.assertTrue(any(o.startswith("Don't add") for o in options))

    def test_approving_without_touching_the_radio_adds_it(self):
        # Same "must not default to the destructive choice" principle as
        # the matched-row decision - clicking Approve without ever
        # touching the radio must add the property, not silently drop it.
        at = self._staged_under_offer_new_property()
        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)
        self.assertEqual(master_df.iloc[0]["building"], "9 Example Yard")
        self.assertEqual(master_df.iloc[0]["special_features"], "Under Offer")

    def test_choosing_dont_add_skips_the_property_entirely(self):
        at = self._staged_under_offer_new_property()
        radio = next(r for r in at.radio if r.label == "What would you like to do?")
        skip_option = next(o for o in radio.options if o.startswith("Don't add"))
        radio.set_value(skip_option)
        at.run()
        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 0)

    def test_plain_new_row_without_let_status_wording_still_auto_adds(self):
        # Control case - an ordinary new property (no let-status wording
        # at all) must be completely unaffected by this fix: no decision
        # prompt, straight into "New properties", auto-added on approve
        # exactly as before.
        save_staging_file(
            [ListingRow(building="10 Example Yard", provider="Test Provider", special_features="Bike racks")],
            "new_ordinary.xlsx", content_hash="new-property-ordinary-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        self.assertNotIn("⚠️ Needs your decision", [s.value for s in at.subheader])
        self.assertIn("📄 New properties", [s.value for s in at.subheader])

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(len(master_df), 1)
        self.assertEqual(master_df.iloc[0]["building"], "10 Example Yard")


if __name__ == "__main__":
    unittest.main()
