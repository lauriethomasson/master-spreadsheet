"""
Regression tests for master_writer.py's write/version/log mechanism,
specifically the source="manual_edit"/fields_changed support added for the
Master default view's direct cell-editing feature.

Runs from an isolated temporary working directory for every test (never the
real repo) - master_writer.py's paths (data/master.xlsx, data/
master_write_log.jsonl, versions/) are plain relative strings resolved
against the process's cwd, so this fully isolates data/versions/ from the
real repo with zero code changes - same approach already verified safe in
prior live-testing sessions for this repo. Run with:

    .venv\\Scripts\\python.exe -m unittest tests.test_master_writer -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import master_writer
from schema import ListingRow


class IsolatedCwdTestCase(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()


class ManualEditVersioningTests(IsolatedCwdTestCase):
    def test_manual_edit_write_logs_source_and_fields_changed(self):
        master_writer.write_master(
            [ListingRow(building="A", provider="P1", size_sqft=1000.0)],
            source="manual_edit",
            fields_changed=1,
        )

        log = master_writer.get_master_write_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["source"], "manual_edit")
        self.assertEqual(log[0]["fields_changed"], 1)
        self.assertTrue(log[0]["success"])

    def test_manual_edit_version_label_is_singular_for_one_field(self):
        master_writer.write_master(
            [ListingRow(building="A", provider="P1", size_sqft=1000.0)],
            source="manual_edit",
            fields_changed=1,
        )

        versions = master_writer.list_versions()
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["label"], "Manual edit: 1 field changed")

    def test_manual_edit_version_label_is_plural_for_several_fields(self):
        master_writer.write_master(
            [ListingRow(building="A", provider="P1", size_sqft=1000.0)],
            source="manual_edit",
            fields_changed=3,
        )

        versions = master_writer.list_versions()
        self.assertEqual(versions[0]["label"], "Manual edit: 3 fields changed")

    def test_regeocode_version_label_is_singular_for_one_field(self):
        master_writer.write_master(
            [ListingRow(building="A", provider="P1", lat=51.5, lng=-0.1)],
            source="re-geocode",
            fields_changed=1,
        )

        versions = master_writer.list_versions()
        self.assertEqual(versions[0]["label"], "Re-geocoded: 1 field changed")

    def test_regeocode_version_label_is_plural_for_several_fields(self):
        master_writer.write_master(
            [ListingRow(building="A", provider="P1", lat=51.5, lng=-0.1)],
            source="re-geocode",
            fields_changed=4,
        )

        versions = master_writer.list_versions()
        self.assertEqual(versions[0]["label"], "Re-geocoded: 4 fields changed")

    def test_normal_approve_label_is_unaffected(self):
        master_writer.write_master(
            [ListingRow(building="A", provider="P1")],
            new_count=1,
            updated_count=0,
        )

        versions = master_writer.list_versions()
        self.assertEqual(versions[0]["label"], "0 updated, 1 new")

    def test_removed_count_appears_in_the_label_when_non_zero(self):
        master_writer.write_master(
            [ListingRow(building="A", provider="P1")],
            new_count=1,
            updated_count=2,
            removed_count=1,
        )

        versions = master_writer.list_versions()
        self.assertEqual(versions[0]["label"], "2 updated, 1 new, 1 removed")

    def test_removed_count_omitted_from_the_label_when_zero(self):
        # Matches the existing "0 updated, 1 new" convention - removed_count
        # is a newer, less common dimension, so a normal approve's label
        # shouldn't grow a ", 0 removed" suffix just because the param exists.
        master_writer.write_master(
            [ListingRow(building="A", provider="P1")],
            new_count=1,
            updated_count=0,
            removed_count=0,
        )

        versions = master_writer.list_versions()
        self.assertEqual(versions[0]["label"], "0 updated, 1 new")

    def test_manual_edit_creates_a_restorable_version_like_an_approve(self):
        master_writer.write_master([ListingRow(building="Original", provider="P1")])
        first_version = master_writer.list_versions(limit=1)[0]["path"]

        master_writer.write_master(
            [ListingRow(building="Edited", provider="P1")],
            source="manual_edit",
            fields_changed=1,
        )
        self.assertEqual(master_writer.load_master_as_dataframe()["building"].iloc[0], "Edited")

        master_writer.restore_version(first_version)
        self.assertEqual(master_writer.load_master_as_dataframe()["building"].iloc[0], "Original")

        # Restoring is itself a new, undoable version - never a one-way trip.
        versions = master_writer.list_versions()
        self.assertEqual(len(versions), 3)
        self.assertTrue(versions[0]["label"].startswith("Restored from"))

    def test_failed_manual_edit_logs_its_real_source_not_approve(self):
        # write_rows_to_xlsx accepts anything with the right attributes, so
        # force the row-count validation to fail by writing directly to a
        # bogus master_path parent that can't be created, keeping this a
        # pure "does the failure log use the right source" check.
        bad_path = "/this/path/cannot/possibly/be/created/master.xlsx" if os.name != "nt" else "Z:\\nonexistent\\master.xlsx"
        with self.assertRaises(Exception):
            master_writer.write_master(
                [ListingRow(building="A", provider="P1")],
                master_path=bad_path,
                source="manual_edit",
                fields_changed=1,
            )
        log = master_writer.get_master_write_log()
        self.assertEqual(len(log), 1)
        self.assertFalse(log[0]["success"])
        self.assertEqual(log[0]["source"], "manual_edit")


if __name__ == "__main__":
    unittest.main()
