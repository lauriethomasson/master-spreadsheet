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

import pandas as pd

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


class SourceIdentityHashFallbackDedupTests(IsolatedCwdTestCase):
    """
    find_previous_upload_by_hash's own real, confirmed gap: content_hash
    ALONE bakes in the current code's own extraction-logic fingerprint
    (_SPREADSHEET_LOGIC_FINGERPRINT/_PDF_EMAIL_LOGIC_FINGERPRINT + geocode.py, see
    app.py), so re-uploading the exact same source file after ANY change
    to that logic produced a genuinely different content_hash and this
    lookup wrongly returned None - the file was re-extracted from scratch
    instead of reusing the cached rows. group_pending_by_content_hash/
    active_and_superseded_staging_files (see ActiveAndSupersededStaging
    FilesTests above) already had this exact fallback via _grouping_hash;
    these mirror that class's own "different content_hash, same source_
    identity_hash" tests, but for THIS lookup specifically.
    """

    def test_different_content_hash_but_same_source_identity_hash_still_matches(self):
        rows = [ListingRow(building="A", provider="P1")]
        staging_path = file_store.save_staging_file(
            rows, "a.pdf", content_hash="fingerprint-1-bytes", source_identity_hash="same-real-file",
        )

        # Simulates a real fix landing in extract_spreadsheet.py/geocode.py
        # between the two uploads - the fingerprint-dependent content_hash
        # is now genuinely different, but the real file bytes (and
        # therefore source_identity_hash) haven't changed at all.
        found = file_store.find_previous_upload_by_hash("fingerprint-2-bytes", "same-real-file")

        self.assertEqual(found, staging_path)

    def test_three_reuploads_across_three_fingerprints_all_resolve_to_the_latest(self):
        rows = [ListingRow(building="A", provider="P1")]
        file_store.save_staging_file(rows, "a1.pdf", content_hash="fingerprint-1", source_identity_hash="same-real-file")
        file_store.save_staging_file(rows, "a2.pdf", content_hash="fingerprint-2", source_identity_hash="same-real-file")
        third = file_store.save_staging_file(
            rows, "a3.pdf", content_hash="fingerprint-3", source_identity_hash="same-real-file",
        )

        found = file_store.find_previous_upload_by_hash("fingerprint-4", "same-real-file")

        self.assertEqual(found, third)

    def test_different_source_identity_hash_never_matches_even_with_no_content_hash_overlap_either(self):
        # Two genuinely different real files must never be treated as the
        # same upload merely because BOTH happen to have gone through the
        # source_identity_hash path.
        file_store.save_staging_file(
            [ListingRow(building="A")], "a.pdf", content_hash="hash-a", source_identity_hash="file-a-bytes",
        )

        found = file_store.find_previous_upload_by_hash("hash-b", "file-b-bytes")

        self.assertIsNone(found)

    def test_omitting_source_identity_hash_preserves_the_exact_prior_content_hash_only_behavior(self):
        # Backward compatibility for any caller not yet updated to pass
        # source_identity_hash (the default is None) - must behave
        # EXACTLY like the pre-fix content_hash-only lookup.
        staging_path = file_store.save_staging_file(
            [ListingRow(building="A")], "a.pdf", content_hash="hash-a", source_identity_hash="file-a-bytes",
        )

        found = file_store.find_previous_upload_by_hash("hash-a")  # no source_identity_hash passed at all

        self.assertEqual(found, staging_path)

    def test_legacy_entry_with_no_source_identity_hash_still_falls_back_to_content_hash(self):
        # A staging entry written before source_identity_hash existed at
        # all (never recorded) must keep matching on content_hash alone,
        # exactly as it always did - no backfill needed for old history.
        staging_path = file_store.save_staging_file(
            [ListingRow(building="A")], "a.pdf", content_hash="hash-a-legacy",
        )

        # A caller that HAS started passing source_identity_hash for new
        # uploads must still find this pre-existing legacy entry by its
        # own content_hash, since it never recorded a source_identity_hash
        # to compare against in the first place.
        found = file_store.find_previous_upload_by_hash("hash-a-legacy", "some-new-uploads-source-hash")

        self.assertEqual(found, staging_path)


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


class DataframeToListingRowsTests(IsolatedCwdTestCase):
    def test_a_row_with_no_building_but_a_non_blank_other_field_is_skipped_not_crashed(self):
        # Grounded in a real Kitt's Availability row: a spreadsheet-
        # author's own section-header/note, every column blank except one
        # unrelated mapped field. Not all-blank, so the old "skip only if
        # every field is None" check let it through to
        # ListingRow(building=None, ...), which raised and aborted the
        # WHOLE file's extraction over this one non-property row.
        df = pd.DataFrame([
            {"building": "City Tower", "provider": "Breezblok"},
            {"building": None, "provider": None, "submarket": "COMING SOON / ADDITIONAL OPTIONS TO SHARE"},
        ])

        rows = file_store.dataframe_to_listing_rows(df)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].building, "City Tower")

    def test_a_whitespace_only_building_is_also_skipped(self):
        df = pd.DataFrame([{"building": "   ", "provider": "Breezblok"}])

        rows = file_store.dataframe_to_listing_rows(df)

        self.assertEqual(len(rows), 0)

    def test_a_genuinely_all_blank_row_is_still_skipped(self):
        df = pd.DataFrame([
            {"building": "City Tower", "provider": "Breezblok"},
            {"building": None, "provider": None},
        ])

        rows = file_store.dataframe_to_listing_rows(df)

        self.assertEqual(len(rows), 1)

    def test_a_raw_number_in_a_string_field_is_coerced_not_a_crash(self):
        # Real confirmed case: a beem Live Flex Availability.xlsx row's own
        # Floor cell held the raw number 2.3, not text like "2nd" - this
        # previously failed ListingRow's own str validation and aborted the
        # WHOLE file's extraction over that one cell.
        df = pd.DataFrame([{"building": "City Tower", "floor_unit": 2.3}])

        rows = file_store.dataframe_to_listing_rows(df)

        self.assertEqual(rows[0].floor_unit, "2.3")

    def test_a_whole_number_float_in_a_string_field_drops_the_trailing_zero(self):
        df = pd.DataFrame([{"building": "City Tower", "floor_unit": 2.0}])

        rows = file_store.dataframe_to_listing_rows(df)

        self.assertEqual(rows[0].floor_unit, "2")

    def test_numeric_fields_are_never_coerced_to_strings(self):
        df = pd.DataFrame([{"building": "City Tower", "size_sqft": 1000.0}])

        rows = file_store.dataframe_to_listing_rows(df)

        self.assertEqual(rows[0].size_sqft, 1000.0)
        self.assertIsInstance(rows[0].size_sqft, float)

    def test_tbc_placeholder_brochure_link_becomes_blank(self):
        # Real confirmed case: a UNION row's own Brochure cell reads "TBC"
        # (the provider's own way of saying "no brochure yet") - this must
        # never survive as a real, broken, clickable link.
        df = pd.DataFrame([{"building": "City Tower", "brochure_link": "TBC"}])

        rows = file_store.dataframe_to_listing_rows(df)

        self.assertIsNone(rows[0].brochure_link)

    def test_coming_soon_placeholder_floorplan_link_becomes_blank(self):
        df = pd.DataFrame([{"building": "City Tower", "floorplan_link": "Coming Soon"}])

        rows = file_store.dataframe_to_listing_rows(df)

        self.assertIsNone(rows[0].floorplan_link)

    def test_a_genuinely_missing_brochure_stays_blank(self):
        df = pd.DataFrame([{"building": "City Tower", "brochure_link": None}])

        rows = file_store.dataframe_to_listing_rows(df)

        self.assertIsNone(rows[0].brochure_link)

    def test_scheme_less_link_with_a_port_survives(self):
        # Confirmed real gap: a genuine hyperlink target that happens to
        # include an explicit port used to fail looks_like_url's old,
        # narrower shape check and be silently nulled here.
        df = pd.DataFrame([{"building": "City Tower", "brochure_link": "app.box.com:8443/s/abc123"}])

        rows = file_store.dataframe_to_listing_rows(df)

        self.assertEqual(rows[0].brochure_link, "app.box.com:8443/s/abc123")

    def test_genuine_scheme_link_round_trips_through_staging_write_and_reload(self):
        # source -> staging write -> staging reload - a valid brochure/
        # floorplan link must survive unchanged, never erased by the
        # sanitizer that exists purely to catch a placeholder.
        original = [ListingRow(
            building="City Tower", brochure_link="https://app.box.com/s/abc123",
            floorplan_link="https://app.box.com/s/floorplan456",
        )]
        staging_path = file_store.save_staging_file(original, "test.xlsx")

        reloaded = file_store.dataframe_to_listing_rows(file_store.load_staging_as_dataframe(staging_path))

        self.assertEqual(reloaded[0].brochure_link, "https://app.box.com/s/abc123")
        self.assertEqual(reloaded[0].floorplan_link, "https://app.box.com/s/floorplan456")

    def test_a_source_with_genuinely_no_brochure_stays_blank_through_the_round_trip(self):
        original = [ListingRow(building="City Tower", brochure_link=None)]
        staging_path = file_store.save_staging_file(original, "test.xlsx")

        reloaded = file_store.dataframe_to_listing_rows(file_store.load_staging_as_dataframe(staging_path))

        self.assertIsNone(reloaded[0].brochure_link)


class CriticalFieldRescuePersistenceTests(IsolatedCwdTestCase):
    def test_no_rescue_saved_yet_returns_none(self):
        self.assertIsNone(file_store.get_saved_critical_field_rescue("some-hash"))

    def test_saved_rescue_round_trips(self):
        headers = ["Clerkenwell & Farringdon", "Floor"]
        assignments = {"building": "Clerkenwell & Farringdon"}
        file_store.save_critical_field_rescue("hash-a", headers, assignments)

        saved = file_store.get_saved_critical_field_rescue("hash-a")

        self.assertEqual(saved["headers"], headers)
        self.assertEqual(saved["assignments"], assignments)

    def test_different_hash_never_matches_a_saved_rescue(self):
        file_store.save_critical_field_rescue("hash-a", ["Floor"], {"building": "Floor"})

        self.assertIsNone(file_store.get_saved_critical_field_rescue("hash-b"))

    def test_a_none_assignment_round_trips_as_none_not_dropped(self):
        # A confirmed "genuinely no such column" answer must survive the
        # round trip as None, not be silently omitted from the saved JSON -
        # unresolved_critical_fields relies on the key being present at all
        # to tell "confirmed blank" apart from "never asked".
        file_store.save_critical_field_rescue("hash-a", ["Floor"], {"building": None})

        saved = file_store.get_saved_critical_field_rescue("hash-a")

        self.assertIn("building", saved["assignments"])
        self.assertIsNone(saved["assignments"]["building"])


class FullyOccupiedBuildingsMetadataTests(IsolatedCwdTestCase):
    """save_staging_file/get_staging_fully_occupied_buildings - the sidecar
    meta.json field carrying extract_spreadsheet_gemini's fully_occupied_
    buildings signal from upload time through to review time (see master_
    merge.find_stale_candidates)."""

    def test_round_trips_through_the_sidecar(self):
        fully_occupied = [{"provider": "Copthall Estates", "building": "27 Lime Street"}]
        staging_path = file_store.save_staging_file(
            [ListingRow(building="A", provider="Copthall Estates")], "copthall.xlsx",
            fully_occupied_buildings=fully_occupied,
        )

        self.assertEqual(file_store.get_staging_fully_occupied_buildings(staging_path), fully_occupied)

    def test_omitted_defaults_to_empty_list_not_none(self):
        staging_path = file_store.save_staging_file([ListingRow(building="A", provider="P1")], "a.pdf")

        self.assertEqual(file_store.get_staging_fully_occupied_buildings(staging_path), [])

    def test_pre_existing_entry_with_no_such_key_at_all_defaults_to_empty_list(self):
        # A staging file written before this field existed has no key for it
        # at all in its meta.json - must not KeyError, same "old entries
        # just don't have it" tolerance as content_hash.
        staging_path = file_store.save_staging_file([ListingRow(building="A", provider="P1")], "a.pdf")
        meta = file_store._read_meta(staging_path)
        del meta["fully_occupied_buildings"]
        file_store._write_meta(staging_path, meta)

        self.assertEqual(file_store.get_staging_fully_occupied_buildings(staging_path), [])


class BrochureEnrichmentProgressTests(IsolatedCwdTestCase):
    """
    set_staging_enrichment_progress/set_staging_enrichment_summary/
    get_staging_enrichment_summary - the interim "in_progress" marker an
    interrupted enrichment run leaves behind (see app.py's own
    _run_automatic_brochure_enrichment and brochure_enrichment.
    run_brochure_enrichment), versus the final "complete" one written once
    every remaining brochure has actually been processed. Both are now
    driven by a per-URL processed_urls map ({url: "ok" | "unavailable"}),
    not separately-tracked counters - see _derive_enrichment_counts.
    """

    def test_no_marker_at_all_returns_none(self):
        staging_path = file_store.save_staging_file([ListingRow(building="A")], "a.xlsx")

        self.assertIsNone(file_store.get_staging_enrichment_summary(staging_path))

    def test_progress_marker_is_tagged_in_progress(self):
        staging_path = file_store.save_staging_file([ListingRow(building="A")], "a.xlsx")

        file_store.set_staging_enrichment_progress(
            staging_path, {"https://a.pdf": "ok", "https://b.pdf": "ok", "https://c.pdf": "unavailable"}, 10,
        )

        stats = file_store.get_staging_enrichment_summary(staging_path)
        self.assertEqual(stats["status"], "in_progress")
        self.assertEqual(stats["brochures_done"], 3)
        self.assertEqual(stats["brochures_read_ok"], 2)
        self.assertEqual(stats["brochures_unavailable"], 1)
        self.assertEqual(stats["unique_brochures_considered"], 10)
        self.assertEqual(
            stats["processed_urls"], {"https://a.pdf": "ok", "https://b.pdf": "ok", "https://c.pdf": "unavailable"},
        )

    def test_final_summary_overwrites_an_earlier_progress_marker(self):
        staging_path = file_store.save_staging_file([ListingRow(building="A")], "a.xlsx")
        file_store.set_staging_enrichment_progress(staging_path, {"https://a.pdf": "ok"}, 10)

        processed = {f"https://{i}.pdf": "ok" for i in range(10)}
        file_store.set_staging_enrichment_summary(
            staging_path, {"unique_brochures_considered": 10, "rows_eligible": 10, "rows_enriched": 10}, processed,
        )

        stats = file_store.get_staging_enrichment_summary(staging_path)
        self.assertEqual(stats["status"], "complete")
        self.assertEqual(stats["rows_enriched"], 10)
        self.assertEqual(stats["brochures_read_ok"], 10)
        self.assertEqual(stats["processed_urls"], processed)

    def test_final_summary_always_tagged_complete_even_without_a_prior_progress_marker(self):
        staging_path = file_store.save_staging_file([ListingRow(building="A")], "a.xlsx")

        file_store.set_staging_enrichment_summary(
            staging_path, {"unique_brochures_considered": 1, "rows_eligible": 1, "rows_enriched": 1},
            {"https://a.pdf": "ok"},
        )

        self.assertEqual(file_store.get_staging_enrichment_summary(staging_path)["status"], "complete")

    def test_a_later_progress_call_can_still_overwrite_a_completed_summary(self):
        # Not a realistic call order for one run (progress calls only ever
        # precede the final summary call - see run_brochure_enrichment),
        # but confirms the two functions don't depend on call order to
        # behave correctly, only on whichever was written last.
        staging_path = file_store.save_staging_file([ListingRow(building="A")], "a.xlsx")
        file_store.set_staging_enrichment_summary(
            staging_path, {"unique_brochures_considered": 1, "rows_eligible": 1, "rows_enriched": 1},
            {"https://a.pdf": "ok"},
        )

        file_store.set_staging_enrichment_progress(staging_path, {}, 5)

        self.assertEqual(file_store.get_staging_enrichment_summary(staging_path)["status"], "in_progress")

    def test_derived_counts_never_disagree_with_processed_urls(self):
        staging_path = file_store.save_staging_file([ListingRow(building="A")], "a.xlsx")

        file_store.set_staging_enrichment_progress(
            staging_path, {"https://a.pdf": "ok", "https://b.pdf": "unavailable"}, 5,
        )
        stats = file_store.get_staging_enrichment_summary(staging_path)

        self.assertEqual(stats["brochures_done"], len(stats["processed_urls"]))
        self.assertEqual(
            stats["brochures_read_ok"],
            sum(1 for v in stats["processed_urls"].values() if v == "ok"),
        )

    def test_callers_that_omit_floorplan_args_get_zeroed_floorplan_fields(self):
        # Every pre-existing caller (brochures only) must be completely
        # unaffected by the floorplan fields' existence.
        staging_path = file_store.save_staging_file([ListingRow(building="A")], "a.xlsx")

        file_store.set_staging_enrichment_progress(staging_path, {"https://a.pdf": "ok"}, 1)
        stats = file_store.get_staging_enrichment_summary(staging_path)

        self.assertEqual(stats["unique_floorplans_considered"], 0)
        self.assertEqual(stats["floorplans_done"], 0)
        self.assertEqual(stats["floorplan_processed_urls"], {})

    def test_progress_marker_tracks_floorplan_fields_independently_of_brochure_ones(self):
        staging_path = file_store.save_staging_file([ListingRow(building="A")], "a.xlsx")

        file_store.set_staging_enrichment_progress(
            staging_path, {"https://a.pdf": "ok"}, 1,
            floorplan_processed_urls={"https://fp1.pdf": "ok", "https://fp2.pdf": "unavailable"},
            unique_floorplans_considered=3,
        )
        stats = file_store.get_staging_enrichment_summary(staging_path)

        # Brochure-side fields are untouched by the floorplan ones.
        self.assertEqual(stats["brochures_done"], 1)
        self.assertEqual(stats["unique_brochures_considered"], 1)
        # Floorplan-side fields are their own, independent record.
        self.assertEqual(stats["unique_floorplans_considered"], 3)
        self.assertEqual(stats["floorplans_done"], 2)
        self.assertEqual(stats["floorplans_read_ok"], 1)
        self.assertEqual(stats["floorplans_unavailable"], 1)

    def test_final_summary_carries_floorplan_fields_through_too(self):
        staging_path = file_store.save_staging_file([ListingRow(building="A")], "a.xlsx")

        file_store.set_staging_enrichment_summary(
            staging_path, {"unique_brochures_considered": 1, "rows_eligible": 1, "rows_enriched": 1},
            {"https://a.pdf": "ok"},
            floorplan_processed_urls={"https://fp1.pdf": "ok"}, unique_floorplans_considered=1,
        )
        stats = file_store.get_staging_enrichment_summary(staging_path)

        self.assertEqual(stats["status"], "complete")
        self.assertEqual(stats["unique_floorplans_considered"], 1)
        self.assertEqual(stats["floorplans_done"], 1)
        self.assertEqual(stats["floorplan_processed_urls"], {"https://fp1.pdf": "ok"})


class ActiveAndSupersededStagingFilesTests(IsolatedCwdTestCase):
    """
    active_and_superseded_staging_files/group_pending_by_content_hash - a
    real production report: the SAME UNION workbook staged twice (once
    interrupted at 30/126 brochures, once completed at 126/126) both
    pending at once, combining into a false 550-row/hundreds-of-conflicts
    Review batch. These two staging entries share one content_hash (byte-
    identical source file) - only the more enrichment-complete one should
    ever be "active" (the one Review reads rows from); the other is
    "superseded" - still a completely ordinary pending entry for every
    other purpose (staging management's own listing, its own Discard
    button), just excluded from row-combination/counts.
    """

    _staged_counter = 0

    def _staged(
        self, content_hash, status=None, processed_urls=None, unique=1, n_rows=275, filename="UNION.xlsx",
        source_identity_hash=None,
    ):
        # save_staging_file's own path is {second-resolution timestamp}_
        # {filename stem}.xlsx (see its own docstring) - a distinct suffix
        # per call here guarantees a distinct staging path regardless of
        # real wall-clock timing, since several calls in one fast test
        # method easily land within the same second otherwise (a real,
        # narrow, pre-existing collision risk in save_staging_file itself,
        # unrelated to what these tests are about). The VISIBLE filename
        # recorded in each entry's own meta.json is still exactly
        # `filename` (unsuffixed) - see get_staging_filename - so tests
        # that need two entries to genuinely SHARE one filename still can.
        type(self)._staged_counter += 1
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        unique_filename = f"{stem}__{type(self)._staged_counter}{suffix}"
        path = file_store.save_staging_file(
            [ListingRow(building="A")] * n_rows, unique_filename, content_hash=content_hash,
            source_identity_hash=source_identity_hash,
        )
        meta = file_store._read_meta(path)
        meta["filename"] = filename
        file_store._write_meta(path, meta)
        if status == "complete":
            file_store.set_staging_enrichment_summary(
                path, {"unique_brochures_considered": unique, "rows_eligible": n_rows, "rows_enriched": 0},
                processed_urls or {f"https://{i}.pdf": "ok" for i in range(unique)},
            )
        elif status == "in_progress":
            file_store.set_staging_enrichment_progress(path, processed_urls or {}, unique)
        return path

    # 1/8. Two staging entries with identical source content are uniquely
    # identifiable, and their shared identity is recognized.
    def test_identical_content_hash_groups_the_two_entries_together(self):
        incomplete = self._staged("hash-union", status="in_progress", processed_urls={"https://0.pdf": "ok"}, unique=126)
        complete = self._staged("hash-union", status="complete", unique=126)

        groups = file_store.group_pending_by_content_hash([incomplete, complete])

        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups["hash-union"]), {incomplete, complete})
        self.assertNotEqual(incomplete, complete)  # still two distinct, uniquely-identifiable paths

    # 2. One 30/126 incomplete + one 126/126 complete copy -> complete
    # copy is active for Review.
    def test_complete_copy_is_active_incomplete_copy_is_superseded(self):
        incomplete = self._staged(
            "hash-union", status="in_progress",
            processed_urls={f"https://{i}.pdf": "ok" for i in range(30)}, unique=126,
        )
        complete = self._staged("hash-union", status="complete", unique=126)

        active, superseded = file_store.active_and_superseded_staging_files([incomplete, complete])

        self.assertEqual(active, [complete])
        self.assertEqual(superseded, [incomplete])

    # 4. Both entries can still be individually identified in staging
    # management regardless of active/superseded status.
    def test_superseded_entry_is_still_a_normal_pending_file_in_every_other_respect(self):
        incomplete = self._staged("hash-union", status="in_progress", unique=126)
        complete = self._staged("hash-union", status="complete", unique=126)

        active, superseded = file_store.active_and_superseded_staging_files([incomplete, complete])

        # The superseded path is still a real, readable, individually
        # discardable staging entry - "superseded" is a Review-page
        # display/counting decision only, never a deletion.
        self.assertIsNotNone(file_store.get_staging_enrichment_summary(superseded[0]))
        self.assertEqual(file_store.get_staging_filename(superseded[0]), "UNION.xlsx")

    # 9. Same filename with genuinely different content is NOT treated as
    # identical.
    def test_same_filename_different_content_hash_are_never_grouped(self):
        path_a = self._staged("hash-june", status="complete", unique=5)
        path_b = self._staged("hash-july", status="complete", unique=5)

        groups = file_store.group_pending_by_content_hash([path_a, path_b])

        self.assertEqual(len(groups), 2)
        active, superseded = file_store.active_and_superseded_staging_files([path_a, path_b])
        self.assertEqual(set(active), {path_a, path_b})
        self.assertEqual(superseded, [])

    # 12/13. Genuinely different uploads (different content) both remain
    # active - June vs July, or any two unrelated files, are never
    # superseded against each other merely because they share a filename
    # or have overlapping properties.
    def test_genuinely_different_uploads_are_never_superseded(self):
        june = self._staged("hash-june-availability", status="complete", unique=10)
        july = self._staged("hash-july-availability", status="complete", unique=10)
        unrelated = self._staged("hash-other-provider", status="complete", unique=3)

        active, superseded = file_store.active_and_superseded_staging_files([june, july, unrelated])

        self.assertEqual(set(active), {june, july, unrelated})
        self.assertEqual(superseded, [])

    def test_a_blank_content_hash_is_never_grouped_with_anything(self):
        no_hash_a = file_store.save_staging_file([ListingRow(building="A")], "old-upload.xlsx")
        no_hash_b = file_store.save_staging_file([ListingRow(building="B")], "old-upload-2.xlsx")

        groups = file_store.group_pending_by_content_hash([no_hash_a, no_hash_b])
        self.assertEqual(groups, {})

        active, superseded = file_store.active_and_superseded_staging_files([no_hash_a, no_hash_b])
        self.assertEqual(set(active), {no_hash_a, no_hash_b})
        self.assertEqual(superseded, [])

    def test_never_touched_enrichment_loses_to_any_real_progress(self):
        never_touched = self._staged("hash-union", status=None, unique=126)
        in_progress = self._staged("hash-union", status="in_progress", processed_urls={"https://0.pdf": "ok"}, unique=126)

        active, superseded = file_store.active_and_superseded_staging_files([never_touched, in_progress])

        self.assertEqual(active, [in_progress])
        self.assertEqual(superseded, [never_touched])

    def test_a_genuine_tie_breaks_toward_the_most_recent_entry(self):
        # Both fully complete, identical enrichment completeness - recency
        # is the LAST resort tie-break here, never the primary signal (see
        # this function's own docstring), but a tie must still resolve to
        # something deterministic rather than crashing/being ambiguous.
        first = self._staged("hash-union", status="complete", unique=5)
        second = self._staged("hash-union", status="complete", unique=5)

        active, superseded = file_store.active_and_superseded_staging_files([first, second])

        self.assertEqual(len(active), 1)
        self.assertEqual(len(superseded), 1)
        self.assertEqual(set(active) | set(superseded), {first, second})

    def test_three_way_group_still_resolves_to_exactly_one_active(self):
        a = self._staged("hash-union", status="in_progress", processed_urls={"https://0.pdf": "ok"}, unique=126)
        b = self._staged(
            "hash-union", status="in_progress",
            processed_urls={f"https://{i}.pdf": "ok" for i in range(30)}, unique=126,
        )
        c = self._staged("hash-union", status="complete", unique=126)

        active, superseded = file_store.active_and_superseded_staging_files([a, b, c])

        self.assertEqual(active, [c])
        self.assertEqual(set(superseded), {a, b})

    # Real, confirmed SECOND-round production report: re-uploading the same
    # source file across a real code change (a fix landing between two
    # uploads of the same real file) still duplicated every real listing,
    # even after this class's own content_hash-based supersession existed -
    # because content_hash itself bakes in the CURRENT code's own logic
    # fingerprint, so the SAME real file re-processed under different code
    # gets a genuinely different content_hash and was never recognized as
    # the same upload at all. source_identity_hash (the real bytes alone,
    # excluding that fingerprint) closes this gap.
    def test_different_content_hash_but_same_source_identity_hash_is_still_grouped(self):
        stale = self._staged(
            "fingerprint-1-bytes", status="complete", unique=1, source_identity_hash="same-real-file",
        )
        fresh = self._staged(
            "fingerprint-2-bytes", status="complete", unique=1, source_identity_hash="same-real-file",
        )

        groups = file_store.group_pending_by_content_hash([stale, fresh])
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(next(iter(groups.values()))), {stale, fresh})

        active, superseded = file_store.active_and_superseded_staging_files([stale, fresh])
        self.assertEqual(len(active), 1)
        self.assertEqual(len(superseded), 1)
        self.assertEqual(set(active) | set(superseded), {stale, fresh})

    def test_three_reuploads_across_three_fingerprints_still_resolve_to_exactly_one_active(self):
        # The exact real shape: re-uploading the same source file across
        # THREE separate code changes (three real fixes shipped in
        # succession while the same file kept getting re-tested) must
        # still collapse to exactly one active copy - never accumulate a
        # growing pile of stale, never-superseded pending entries that all
        # keep contributing their own rows to every later Review render.
        first = self._staged(
            "fingerprint-1", status="in_progress", unique=1, source_identity_hash="same-real-file",
        )
        second = self._staged(
            "fingerprint-2", status="in_progress", unique=1, source_identity_hash="same-real-file",
        )
        third = self._staged(
            "fingerprint-3", status="complete", unique=1, source_identity_hash="same-real-file",
        )

        active, superseded = file_store.active_and_superseded_staging_files([first, second, third])

        self.assertEqual(active, [third])
        self.assertEqual(set(superseded), {first, second})

    def test_different_source_identity_hash_is_never_grouped_even_with_shared_content_hash_prefix(self):
        # Two genuinely different real files must never be treated as the
        # same upload merely by coincidence - source_identity_hash is the
        # authoritative identity signal once present.
        a = self._staged("hash-a", status="complete", unique=1, source_identity_hash="file-a-bytes")
        b = self._staged("hash-b", status="complete", unique=1, source_identity_hash="file-b-bytes")

        active, superseded = file_store.active_and_superseded_staging_files([a, b])

        self.assertEqual(set(active), {a, b})
        self.assertEqual(superseded, [])

    def test_legacy_entries_with_no_source_identity_hash_still_fall_back_to_content_hash(self):
        # A staging entry written before source_identity_hash existed
        # (None recorded) must keep working exactly as before this change -
        # grouped by content_hash alone, same as always.
        a = self._staged("hash-union-legacy", status="complete", unique=1)
        b = self._staged("hash-union-legacy", status="in_progress", unique=1)

        active, superseded = file_store.active_and_superseded_staging_files([a, b])

        self.assertEqual(active, [a])
        self.assertEqual(superseded, [b])


if __name__ == "__main__":
    unittest.main()
