


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

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema import ListingRow
from storage import blob_store, file_store

SAMPLE_DOCS = Path(__file__).resolve().parent / "sample_docs"


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


class RealPdfContentHashDedupTests(IsolatedCwdTestCase):
    """
    The tests above all use a literal synthetic content_hash string (e.g.
    "hash-a"), never a real SHA256 of actual file bytes - covering the
    ledger/lookup logic itself, but never exercising real PDF byte-hashing
    end-to-end (see the earlier investigation this closes the gap on).
    These hash an actual sample PDF exactly as app.py does
    (hashlib.sha256(file_bytes).hexdigest()), proving the mechanism against
    a real file's real content, not an arbitrary stand-in string.
    """

    def _real_pdf_bytes(self) -> bytes:
        return (SAMPLE_DOCS / "Breezblok.pdf").read_bytes()

    def test_identical_real_pdf_bytes_are_detected_as_a_duplicate(self):
        original_bytes = self._real_pdf_bytes()
        original_hash = hashlib.sha256(original_bytes).hexdigest()
        staging_path = file_store.save_staging_file(
            [ListingRow(building="Breezblok", provider="Breezblok")], "Breezblok.pdf", content_hash=original_hash,
        )

        # A re-upload of the exact same bytes (any filename - app.py hashes
        # raw bytes before ever looking at the name) must hash identically
        # and resolve to the same previously-staged file.
        reupload_bytes = self._real_pdf_bytes()
        reupload_hash = hashlib.sha256(reupload_bytes).hexdigest()

        self.assertEqual(original_hash, reupload_hash)
        self.assertEqual(file_store.find_previous_upload_by_hash(reupload_hash), staging_path)

    def test_a_single_changed_byte_is_correctly_not_a_duplicate(self):
        original_bytes = self._real_pdf_bytes()
        original_hash = hashlib.sha256(original_bytes).hexdigest()
        file_store.save_staging_file(
            [ListingRow(building="Breezblok", provider="Breezblok")], "Breezblok.pdf", content_hash=original_hash,
        )

        # Simulates a re-exported/re-saved copy with the same visual content
        # but different underlying bytes (e.g. rewritten producer metadata) -
        # exactly the documented limitation of a byte-exact hash scheme.
        modified_bytes = original_bytes[:-1] + bytes([original_bytes[-1] ^ 0x01])
        modified_hash = hashlib.sha256(modified_bytes).hexdigest()

        self.assertNotEqual(original_hash, modified_hash)
        self.assertIsNone(file_store.find_previous_upload_by_hash(modified_hash))


class DiscardPendingStagingFilesTests(IsolatedCwdTestCase):
    def test_discarded_file_no_longer_appears_as_pending(self):
        staging_path = file_store.save_staging_file(
            [ListingRow(building="A", provider="P1")], "a.pdf", content_hash="hash-a",
        )
        self.assertIn(staging_path, file_store.list_pending_staging_files())

        file_store.discard_pending_staging_files([staging_path])

        self.assertNotIn(staging_path, file_store.list_pending_staging_files())

    def test_discarded_file_is_forgotten_by_the_hash_ledger(self):
        # A discard is a real rejection, not a status change - a later
        # re-upload of the exact same bytes must be treated as genuinely
        # new (re-extracted), not silently reused from the discarded run.
        staging_path = file_store.save_staging_file(
            [ListingRow(building="A", provider="P1")], "a.pdf", content_hash="hash-a",
        )
        file_store.discard_pending_staging_files([staging_path])

        self.assertIsNone(file_store.find_previous_upload_by_hash("hash-a"))

    def test_the_underlying_xlsx_and_meta_are_both_actually_gone(self):
        staging_path = file_store.save_staging_file(
            [ListingRow(building="A", provider="P1")], "a.pdf", content_hash="hash-a",
        )
        file_store.discard_pending_staging_files([staging_path])

        self.assertFalse(blob_store.exists(staging_path))
        self.assertFalse(blob_store.exists(file_store._meta_path(staging_path)))

    def test_discarding_one_file_leaves_other_pending_files_untouched(self):
        first = file_store.save_staging_file([ListingRow(building="A", provider="P1")], "a.pdf", content_hash="hash-a")
        second = file_store.save_staging_file([ListingRow(building="B", provider="P2")], "b.pdf", content_hash="hash-b")

        file_store.discard_pending_staging_files([first])

        remaining = file_store.list_pending_staging_files()
        self.assertNotIn(first, remaining)
        self.assertIn(second, remaining)
        self.assertEqual(file_store.find_previous_upload_by_hash("hash-b"), second)

    def test_discarding_an_already_approved_file_does_not_error(self):
        # Not a path the UI takes (discard only ever targets currently-
        # pending files - see list_pending_staging_files), but the
        # underlying deletion itself has no reason to care about status,
        # and blob_store.delete is already a safe no-op on a missing path.
        staging_path = file_store.save_staging_file(
            [ListingRow(building="A", provider="P1")], "a.pdf", content_hash="hash-a",
        )
        file_store.mark_as_approved(staging_path)

        file_store.discard_pending_staging_files([staging_path])

        self.assertFalse(blob_store.exists(staging_path))

    def test_discarding_an_empty_list_does_nothing(self):
        staging_path = file_store.save_staging_file(
            [ListingRow(building="A", provider="P1")], "a.pdf", content_hash="hash-a",
        )
        file_store.discard_pending_staging_files([])
        self.assertIn(staging_path, file_store.list_pending_staging_files())


class HeaderMappingPersistenceTests(IsolatedCwdTestCase):
    def test_no_mapping_saved_yet_returns_none(self):
        self.assertIsNone(file_store.get_saved_header_mapping("some-hash"))

    def test_saved_mapping_round_trips(self):
        headers = ["Building", "Floor/Unit"]
        mapping = {"Building": "building", "Floor/Unit": "floor_unit"}
        file_store.save_header_mapping("hash-a", headers, mapping)

        saved = file_store.get_saved_header_mapping("hash-a")

        self.assertEqual(saved["headers"], headers)
        self.assertEqual(saved["mapping"], mapping)

    def test_saved_provider_round_trips_with_the_mapping(self):
        file_store.save_header_mapping(
            "hash-a", ["Building"], {"Building": "building"}, provider="Kitt's"
        )

        saved = file_store.get_saved_header_mapping("hash-a")

        self.assertEqual(saved["provider"], "Kitt's")

    def test_provider_defaults_to_none_for_existing_callers(self):
        file_store.save_header_mapping("hash-a", ["Building"], {"Building": "building"})

        saved = file_store.get_saved_header_mapping("hash-a")

        self.assertIsNone(saved["provider"])

    def test_different_hash_never_matches_a_saved_mapping(self):
        file_store.save_header_mapping("hash-a", ["Building"], {"Building": "building"})

        self.assertIsNone(file_store.get_saved_header_mapping("hash-b"))


if __name__ == "__main__":
    unittest.main()
