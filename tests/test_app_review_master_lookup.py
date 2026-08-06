"""
Streamlit-level regression test for the read-only master-lookup section
added to pages/2_Review_and_Master.py's pending-review screen (see
_render_master_lookup) - previously, whenever a batch was pending, there was
no way to see the current master spreadsheet at all, even though several of
the review screen's own decisions (a "possible near-miss" link-or-new
choice, in particular) need comparing an incoming row against an existing
master record.

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_master_writer.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_master_lookup -v
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


class MasterLookupWhilePendingTests(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def _stage_pending_and_master(self):
        master_writer.write_master([
            ListingRow(building="28 Gresham Street", provider="Kitt's", floor_unit="3rd Floor"),
            ListingRow(building="Wells Street", provider="UNION", floor_unit="2nd Floor"),
        ])
        save_staging_file(
            [ListingRow(building="Unrelated New Building", provider="Newco")],
            "unrelated.xlsx", content_hash="test-hash",
        )

    def _lookup_expander(self, at):
        return next(e for e in at.expander if e.label == "🔍 View current master")

    def test_existing_master_row_visible_once_filter_matches_it(self):
        self._stage_pending_and_master()

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        lookup = self._lookup_expander(at)
        lookup.text_input(key="pending_master_lookup_filter").set_value("Gresham").run()
        self.assertFalse(at.exception)

        shown = self._lookup_expander(at).dataframe[0].value
        self.assertIn("28 Gresham Street", shown["building"].tolist())

    def test_filter_narrows_out_unrelated_rows(self):
        self._stage_pending_and_master()

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self._lookup_expander(at).text_input(key="pending_master_lookup_filter").set_value("Gresham").run()

        shown = self._lookup_expander(at).dataframe[0].value
        self.assertNotIn("Wells Street", shown["building"].tolist())

    def test_no_editable_widget_or_save_mechanism_in_this_section(self):
        self._stage_pending_and_master()

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()

        lookup = self._lookup_expander(at)
        self.assertEqual(len(lookup.button), 0)
        self.assertEqual(len(lookup.checkbox), 0)
        self.assertEqual(len(lookup.dataframe), 1)

    def test_empty_master_shows_plain_info_not_an_error(self):
        save_staging_file(
            [ListingRow(building="Unrelated New Building", provider="Newco")],
            "unrelated.xlsx", content_hash="test-hash-empty",
        )

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        lookup = self._lookup_expander(at)
        self.assertIn("No master spreadsheet yet", "".join(i.value for i in lookup.info))


if __name__ == "__main__":
    unittest.main()
