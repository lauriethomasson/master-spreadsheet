"""
Streamlit-level regression test for pages/2_Review_and_Master.py's
"No longer present in latest availability" section (see master_merge.
find_stale_candidates and _render_stale_candidate_decision in the page
itself).

Real reported scenario: master still has an old Copthall Estates floor
("27 Lime Street" - 4th Floor) that the LATEST upload of that same complete-
snapshot provider's workbook no longer mentions at all, while a different
floor of the same building ("27 Lime Street" - 2nd Floor) IS still present
and simply matches with no changes. The stale floor must be surfaced for an
explicit human keep/remove decision - defaulting to "keep" - and choosing
"remove" there must actually drop it from master on Approve, reusing the
same removed_indices/apply_merge mechanism as everything else.

Runs from an isolated temporary working directory (never the real repo) -
same isolation approach as tests/test_app_review_collision_ui.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_stale_candidates_ui -v
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
from storage.file_store import save_staging_file

BASE = Path(__file__).resolve().parent.parent


class StaleCandidateUiTests(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def test_stale_floor_is_surfaced_and_removable_on_approve(self):
        master_writer.write_master([
            ListingRow(building="27 Lime Street", provider="Copthall Estates", floor_unit="2nd Floor"),
            ListingRow(building="27 Lime Street", provider="Copthall Estates", floor_unit="4th Floor"),
        ])
        save_staging_file(
            [ListingRow(building="27 Lime Street", provider="Copthall Estates", floor_unit="2nd Floor")],
            "Copthall.xlsx", content_hash="test-hash-stale",
        )

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        warnings = "".join(w.value for w in at.warning)
        self.assertIn("No longer present", "".join(h.value for h in at.subheader) + warnings)
        self.assertIn("27 Lime Street", warnings)

        stale_radios = [r for r in at.radio if r.label == "What should happen to this property?"]
        self.assertEqual(len(stale_radios), 1)
        stale_radios[0].set_value("Remove this property from master entirely")
        at.run()
        self.assertFalse(at.exception)

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        floor_units = set(master_df["floor_unit"])
        self.assertIn("2nd Floor", floor_units)
        self.assertNotIn("4th Floor", floor_units)

    def test_default_decision_is_keep_when_approved_without_touching_the_radio(self):
        master_writer.write_master([
            ListingRow(building="27 Lime Street", provider="Copthall Estates", floor_unit="2nd Floor"),
            ListingRow(building="27 Lime Street", provider="Copthall Estates", floor_unit="4th Floor"),
        ])
        save_staging_file(
            [ListingRow(building="27 Lime Street", provider="Copthall Estates", floor_unit="2nd Floor")],
            "Copthall.xlsx", content_hash="test-hash-stale-default",
        )

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        floor_units = set(master_df["floor_unit"])
        self.assertIn("2nd Floor", floor_units)
        self.assertIn("4th Floor", floor_units)


if __name__ == "__main__":
    unittest.main()
