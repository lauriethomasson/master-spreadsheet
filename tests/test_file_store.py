"""
Regression tests for storage/file_store.py's content-hash dedup mechanism
(find_previous_upload_by_hash / save_staging_file's content_hash) - the
fix for a byte-identical re-upload triggering a real (Gemini-called) re-
extraction, which risks a spurious diff purely from wording nondeterminism.

Runs from an isolated temporary working directory for every test (never the
real repo) - storage/blob_store.py's local-disk paths (here, "staging/...")
are plain relative strings resolved against the process's cwd, same
approach already verified safe in prior live-testing sessions for this
repo. Also explicitly clears file_store's st.cache_data caches in setUp -
without that, a cache key that happens to collide across two different
tests' isolated directories (same content_hash + same directory signature
tuple, e.g. both empty before any staging file exists) could return a
stale/wrong-context cached result rather than a fresh lookup against THIS
test's directory. Run with:

    .venv\\Scripts\\python.exe -m unittest tests.test_file_store -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema import ListingRow
from storage import file_store


class IsolatedCwdTestCase(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        file_store._find_previous_upload_by_hash_cached.clear()
        file_store._list_pending_staging_files_cached.clear()
        file_store._load_staging_as_dataframe_cached.clear()

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()


class ContentHashDedupTests(IsolatedCwdTestCase):
    def test_no_match_when_nothing_uploaded_yet(self):
        self.assertIsNone(file_store.find_previous_upload_by_hash("abc123"))

    def test_matches_a_previously_staged_upload_with_the_same_hash(self):
        rows = [ListingRow(building="40 New Bond Street", provider="Workplace Plus")]
        staging_path = file_store.save_staging_file(rows, "brochure.pdf", content_hash="hash-a")

        found = file_store.find_previous_upload_by_hash("hash-a")

        self.assertEqual(found, staging_path)

    def test_different_hash_does_not_match(self):
        file_store.save_staging_file(
            [ListingRow(building="A", provider="P1")], "a.pdf", content_hash="hash-a",
        )

        self.assertIsNone(file_store.find_previous_upload_by_hash("hash-b"))

    def test_matches_regardless_of_filename(self):
        # Same content, uploaded under a different filename - content_hash
        # is computed from raw bytes by the caller (app.py), not derived
        # from the filename here, so this must still match.
        rows = [ListingRow(building="A", provider="P1")]
        staging_path = file_store.save_staging_file(rows, "original_name.pdf", content_hash="hash-a")

        found = file_store.find_previous_upload_by_hash("hash-a")

        self.assertEqual(found, staging_path)
        # Sanity: really is a different filename, not a coincidence.
        meta_filename = file_store._read_meta(staging_path)["filename"]
        self.assertEqual(meta_filename, "original_name.pdf")

    def test_matches_an_already_approved_upload_not_just_pending_ones(self):
        # Staging .xlsx files are never deleted after approval (see module
        # docstring) - the hash ledger has to search that whole history,
        # not just what's still awaiting review.
        staging_path = file_store.save_staging_file(
            [ListingRow(building="A", provider="P1")], "a.pdf", content_hash="hash-a",
        )
        file_store.mark_as_approved(staging_path)

        found = file_store.find_previous_upload_by_hash("hash-a")

        self.assertEqual(found, staging_path)

    def test_pre_existing_entries_with_no_content_hash_never_match(self):
        # Simulates staging history written before this feature existed -
        # save_staging_file always wrote a meta.json, just never with a
        # content_hash key at all.
        file_store.save_staging_file([ListingRow(building="A", provider="P1")], "a.pdf")

        self.assertIsNone(file_store.find_previous_upload_by_hash("hash-a"))

    def test_blank_hash_never_matches_anything(self):
        file_store.save_staging_file(
            [ListingRow(building="A", provider="P1")], "a.pdf", content_hash=None,
        )
        self.assertIsNone(file_store.find_previous_upload_by_hash(None))
        self.assertIsNone(file_store.find_previous_upload_by_hash(""))

    def test_most_recent_match_wins_when_uploaded_more_than_once(self):
        rows = [ListingRow(building="A", provider="P1")]
        file_store.save_staging_file(rows, "a.pdf", content_hash="hash-a")
        second = file_store.save_staging_file(rows, "a_again.pdf", content_hash="hash-a")

        found = file_store.find_previous_upload_by_hash("hash-a")

        self.assertEqual(found, second)


if __name__ == "__main__":
    unittest.main()
