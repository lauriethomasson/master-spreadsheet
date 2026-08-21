"""
Regression tests for two small wording changes in pages/2_Review_and_
Master.py's _render_field_rows:

1. _risky_field_reason now has a dedicated reason for special_features/
   contacts (master_merge.RISKY_TEXT_FIELDS), instead of falling into the
   generic "Existing value differs from the new upload" catch-all.
2. The "N other safe changes" bundled-fields caption now names a few of
   the actual bundled field labels, not just a bare count.

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_page_restructure.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_risky_reason_and_bundle_names -v
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


class RiskyTextFieldReasonTests(IsolatedCwdTestCase):
    def test_special_features_detail_loss_gets_its_own_reason(self):
        # Same real detail-loss pattern as test_app_review_duplicate_
        # consolidation.py's test_master_row_plus_genuine_incoming_
        # conflict_needs_review - a re-upload's special_features drops
        # real amenities master already had.
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
            "update.xlsx", content_hash="risky-text-field-hash",
        )

        at = _run_review_page()
        self.assertFalse(at.exception)
        self.assertIn("⚠️ Needs your decision", [s.value for s in at.subheader])

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn(
            "New text looks shorter than what's there now — may be missing detail, not just an update.",
            caption_text,
        )
        # The old generic catch-all must not also appear for this field.
        self.assertNotIn("Existing value differs from the new upload", caption_text)


class BundledFieldNamesTests(IsolatedCwdTestCase):
    def test_bundled_safe_changes_line_names_the_actual_fields(self):
        # Same shape as test_app_review_page_restructure.py's Uncommon
        # Liverpool St fixture - one risky address change forces a
        # decision, while size/rent/desks are safe and bundled.
        master_writer.write_master([
            ListingRow(
                building="Uncommon Liverpool St", provider="Uncommon", floor_unit="5th Floor",
                address_1="Uncommon Liverpool St",
            ),
        ])
        save_staging_file(
            [ListingRow(
                building="Uncommon Liverpool St", provider="Uncommon", floor_unit="5th Floor",
                address_1="34-37 Liverpool Street", size_sqft=1638.0, rent_pcm=49500.0, desks_max=30,
            )],
            "uncommon.xlsx", content_hash="bundled-field-names-hash",
        )

        at = _run_review_page()
        self.assertFalse(at.exception)

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("3 other changes (Size, Maximum desks, Rent PCM) will apply automatically.", caption_text)

        # Approving must still apply every bundled field, unchanged.
        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        self.assertEqual(master_df.iloc[0]["size_sqft"], 1638.0)
        self.assertEqual(master_df.iloc[0]["rent_pcm"], 49500.0)
        self.assertEqual(master_df.iloc[0]["desks_max"], 30)


if __name__ == "__main__":
    unittest.main()
