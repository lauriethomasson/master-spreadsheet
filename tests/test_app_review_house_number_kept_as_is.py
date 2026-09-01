"""
Regression tests for the "silently dropped house number" kept-as-is
behavior (see master_merge._house_number_silently_dropped and pages/2_
Review_and_Master.py's own kept_as_is_fields threading through _render_
field_rows/_render_matched_row) - confirmed real case: master's own
"122-124 Regent Street" re-extracted as just "Regent Street" (the leading
house-number range dropped, nothing else about the street name changed).
The OLD value is kept automatically (never applied), but this must NOT be
a fully silent no-op - it still has to render as a non-blocking FYI a
reviewer can spot-check later, a THIRD bucket alongside risky_fields
("needs a decision") and the ordinary bundled-safe-changes summary ("will
apply automatically").

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_risky_reason_and_bundle_names.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_house_number_kept_as_is -v
"""

import os
import re
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


class SilentlyDroppedHouseNumberKeptAsIsTests(IsolatedCwdTestCase):
    def _staged_regent_street_row(self):
        master_writer.write_master([
            ListingRow(
                building="122-124 Regent Street", provider="UNION", floor_unit="1st",
                address_1="122-124 Regent Street", property_id="row-regent",
            ),
        ])
        save_staging_file(
            [
                ListingRow(
                    building="122-124 Regent Street", provider="UNION", floor_unit="1st",
                    address_1="Regent Street",
                ),
            ],
            "regent_street_test.xlsx", content_hash="regent-street-test-hash",
        )
        return _run_review_page()

    def test_does_not_render_as_a_blocking_individual_decision_row(self):
        at = self._staged_regent_street_row()
        self.assertFalse(at.exception)

        # No editable address_1 input anywhere - never individually
        # rendered as a field needing a decision.
        address_boxes = [n for n in at.text_input if n.key and n.key.endswith("_address_1_value")]
        self.assertEqual(address_boxes, [])

        # The card's own label must not claim a BLOCKING decision is
        # needed for this row - nothing else changed in this upload, so
        # the expected label is "no decisions needed", not "N decisions
        # needed" (the "no decisions needed" text itself still contains
        # the substring "decision", so this checks for the digit-count
        # phrasing specifically, not a bare substring match).
        expander_labels = [e.label or "" for e in at.expander]
        blocking_labels = [
            l for l in expander_labels
            if "122-124 Regent Street" in l and re.search(r"\d+ decisions? needed", l)
        ]
        self.assertEqual(blocking_labels, [])
        self.assertTrue(
            any("122-124 Regent Street" in l and "no decisions needed" in l for l in expander_labels)
        )

    def test_a_kept_as_is_caption_is_rendered_explaining_what_happened(self):
        at = self._staged_regent_street_row()

        captions = [c.value for c in at.caption if c.value and "Kept as-is" in c.value]
        self.assertEqual(len(captions), 1)
        caption = captions[0]
        # Content, not exact final copy - must convey both what happened
        # (nothing applied) and why (house number missing from new upload).
        self.assertIn("Address", caption)
        self.assertIn("house number", caption.lower())

    def test_approving_the_row_leaves_address_1_as_the_old_value_in_master(self):
        at = self._staged_regent_street_row()

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        self.assertTrue(approve_buttons)
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        row = master_df.loc[master_df["property_id"] == "row-regent"].iloc[0]
        self.assertEqual(row["address_1"], "122-124 Regent Street")


class GenuineHouseNumberChangeStillBlocksTests(IsolatedCwdTestCase):
    """The regression check that the new kept-as-is bucket didn't
    accidentally swallow real risky house-number cases too."""

    def test_a_genuine_number_change_still_renders_as_a_normal_blocking_row(self):
        master_writer.write_master([
            ListingRow(
                building="18 Bruton Street", provider="UNION", floor_unit="1st",
                address_1="18 Bruton Street", property_id="row-bruton",
            ),
        ])
        save_staging_file(
            [
                ListingRow(
                    building="18 Bruton Street", provider="UNION", floor_unit="1st",
                    address_1="24 Bruton Street",
                ),
            ],
            "bruton_street_test.xlsx", content_hash="bruton-street-test-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        expander_labels = [e.label or "" for e in at.expander]
        decision_labels = [l for l in expander_labels if "18 Bruton Street" in l and "1 decision needed" in l]
        self.assertEqual(len(decision_labels), 1)

        self.assertEqual([c.value for c in at.caption if c.value and "Kept as-is" in c.value], [])

    def test_number_gone_and_street_also_different_still_blocks(self):
        master_writer.write_master([
            ListingRow(
                building="18 Old Street", provider="UNION", floor_unit="1st",
                address_1="18 Old Street", property_id="row-old-street",
            ),
        ])
        save_staging_file(
            [
                ListingRow(
                    building="18 Old Street", provider="UNION", floor_unit="1st",
                    address_1="Kingsland Road",
                ),
            ],
            "kingsland_road_test.xlsx", content_hash="kingsland-road-test-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        expander_labels = [e.label or "" for e in at.expander]
        decision_labels = [l for l in expander_labels if "18 Old Street" in l and "1 decision needed" in l]
        self.assertEqual(len(decision_labels), 1)

        self.assertEqual([c.value for c in at.caption if c.value and "Kept as-is" in c.value], [])


if __name__ == "__main__":
    unittest.main()
