"""
Regression tests for a real production report: 2 pending uploads, both
copies of the same 275-row UNION workbook (one interrupted at 30/126
brochures, one completed at 126/126) - Review & Master combined both into
a false 550-row/hundreds-of-conflicts batch, and the only discard action
available ("Discard all pending uploads") could not remove just the
obsolete copy without also losing the completed one.

Fix: pages/2_Review_and_Master.py now splits `pending` into active vs
superseded staging entries by content_hash identity (see storage.
file_store.active_and_superseded_staging_files) BEFORE combining rows into
the merge plan, and every pending entry gets its own individually-
targeted "Discard this upload" button (see _render_single_file_discard) -
never the whole-batch-only action that used to be the only option.

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_master_table.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_pending_staging_management -v
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
from storage import file_store

BASE = Path(__file__).resolve().parent.parent


class PendingStagingManagementTests(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp.name)
        self._counter = 0

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def _rows(self, n, building_prefix="Building"):
        return [
            ListingRow(
                building=f"{building_prefix} {i}", provider="UNION", floor_unit=f"{i}th",
                property_id=f"{building_prefix}-{i}",
            )
            for i in range(n)
        ]

    def _staged(self, rows, content_hash, status=None, processed_urls=None, unique=1, filename="UNION.xlsx"):
        # Distinct filename per save_staging_file call (see test_file_
        # store.py's own ActiveAndSupersededStagingFilesTests._staged for
        # why - a real, narrow, pre-existing second-resolution timestamp
        # collision risk in save_staging_file itself), with the meta.json
        # rewritten afterward so the VISIBLE filename is still exactly
        # `filename` - tests that need two entries to genuinely share one
        # filename still can.
        self._counter += 1
        stem, suffix = Path(filename).stem, Path(filename).suffix
        path = file_store.save_staging_file(rows, f"{stem}__{self._counter}{suffix}", content_hash=content_hash)
        meta = file_store._read_meta(path)
        meta["filename"] = filename
        file_store._write_meta(path, meta)
        if status == "complete":
            file_store.set_staging_enrichment_summary(
                path, {"unique_brochures_considered": unique, "rows_eligible": len(rows), "rows_enriched": 0},
                processed_urls or {f"https://{i}.pdf": "ok" for i in range(unique)},
            )
        elif status == "in_progress":
            file_store.set_staging_enrichment_progress(path, processed_urls or {}, unique)
        return path

    def _open_review(self):
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)
        return at

    # 2/3/19/20. The exact reported scenario: 30/126 incomplete + 126/126
    # complete copies of the same file -> complete copy is active, the
    # incomplete one is excluded from counts/conflicts, no hundreds of
    # duplicate warnings.
    def test_incomplete_and_complete_copies_do_not_double_count_or_conflict(self):
        incomplete = self._staged(
            self._rows(275), "hash-union",
            status="in_progress", processed_urls={f"https://{i}.pdf": "ok" for i in range(30)}, unique=126,
        )
        complete = self._staged(self._rows(275), "hash-union", status="complete", unique=126)

        at = self._open_review()

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("1 upload pending", caption_text)  # truthful count, not 2
        self.assertIn("275", caption_text)
        self.assertNotIn("550", caption_text)
        # No artificial duplicate/conflict warnings at all - only ONE
        # copy's rows ever reached the merge plan, so there was nothing
        # for master_merge's own duplicate detection to reconcile.
        self.assertNotIn("conflict(s) need your review", caption_text)

    # 1/4/7. Both entries individually identifiable, even sharing a
    # filename - never confused with each other.
    def test_both_entries_individually_identifiable_in_staging_management(self):
        incomplete = self._staged(
            self._rows(275), "hash-union",
            status="in_progress", processed_urls={f"https://{i}.pdf": "ok" for i in range(30)}, unique=126,
        )
        complete = self._staged(self._rows(275), "hash-union", status="complete", unique=126)

        at = self._open_review()

        warning_text = "".join(w.value for w in at.warning)
        self.assertIn("30/126", warning_text)
        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("126/126", caption_text)
        discard_buttons = [b for b in at.button if b.label == "Discard this upload"]
        self.assertEqual(len(discard_buttons), 2)

    # 5. Discarding the incomplete copy deletes only that copy.
    def test_discarding_incomplete_copy_deletes_only_that_copy(self):
        incomplete = self._staged(self._rows(275), "hash-union", status="in_progress", unique=126)
        complete = self._staged(self._rows(275), "hash-union", status="complete", unique=126)

        at = self._open_review()
        discard_btn = next(b for b in at.button if b.key == f"discard_single_{incomplete}")
        discard_btn.click().run()
        confirm_btn = next(b for b in at.button if b.key == f"discard_single_confirm_btn_{incomplete}")
        confirm_btn.click().run()
        self.assertFalse(at.exception)

        pending = file_store.list_pending_staging_files()
        self.assertEqual(pending, [complete])
        self.assertIsNotNone(file_store.get_staging_enrichment_summary(complete))

    # 6. Discarding the complete copy deletes only that copy.
    def test_discarding_complete_copy_deletes_only_that_copy(self):
        incomplete = self._staged(self._rows(275), "hash-union", status="in_progress", unique=126)
        complete = self._staged(self._rows(275), "hash-union", status="complete", unique=126)

        at = self._open_review()
        discard_btn = next(b for b in at.button if b.key == f"discard_single_{complete}")
        discard_btn.click().run()
        confirm_btn = next(b for b in at.button if b.key == f"discard_single_confirm_btn_{complete}")
        confirm_btn.click().run()
        self.assertFalse(at.exception)

        pending = file_store.list_pending_staging_files()
        self.assertEqual(pending, [incomplete])
        self.assertIsNotNone(file_store.get_staging_enrichment_summary(incomplete))

    # 7. Same filename never causes the wrong deletion.
    def test_same_filename_discard_targets_the_correct_path(self):
        a = self._staged(self._rows(3), "hash-a", status="complete", unique=1, filename="Report.xlsx")
        b = self._staged(self._rows(3), "hash-b", status="complete", unique=1, filename="Report.xlsx")
        self.assertEqual(file_store.get_staging_filename(a), file_store.get_staging_filename(b))
        self.assertNotEqual(a, b)

        at = self._open_review()
        discard_btn = next(b for b in at.button if b.key == f"discard_single_{a}")
        discard_btn.click().run()
        confirm_btn = next(b for b in at.button if b.key == f"discard_single_confirm_btn_{a}")
        confirm_btn.click().run()
        self.assertFalse(at.exception)

        pending = file_store.list_pending_staging_files()
        self.assertEqual(pending, [b])

    # 10/11. Incomplete/complete identical re-uploads never create
    # competing Review rows (both already covered structurally by the
    # active/superseded split - proven here at the Review-page level).
    def test_identical_content_never_produces_competing_review_rows(self):
        self._staged(self._rows(50), "hash-x", status="in_progress", unique=20)
        self._staged(self._rows(50), "hash-x", status="complete", unique=20)

        at = self._open_review()

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("50", caption_text)
        self.assertNotIn("100", caption_text)

    # 12/13. Genuinely different uploads (different content) still both
    # participate - June vs July, or any two unrelated files, are never
    # incorrectly superseded against each other.
    def test_genuinely_different_uploads_both_participate(self):
        self._staged(self._rows(5, "June Building"), "hash-june", status="complete", unique=1, filename="June.xlsx")
        self._staged(self._rows(5, "July Building"), "hash-july", status="complete", unique=1, filename="July.xlsx")

        at = self._open_review()

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("2 uploads pending", caption_text)
        self.assertIn("10", caption_text)  # both files' rows genuinely counted

    # 14. Master remains untouched by staging discard.
    def test_master_untouched_by_discard(self):
        incomplete = self._staged(self._rows(275), "hash-union", status="in_progress", unique=126)
        self._staged(self._rows(275), "hash-union", status="complete", unique=126)
        self.assertFalse(master_writer.master_exists())

        at = self._open_review()
        discard_btn = next(b for b in at.button if b.key == f"discard_single_{incomplete}")
        discard_btn.click().run()
        confirm_btn = next(b for b in at.button if b.key == f"discard_single_confirm_btn_{incomplete}")
        confirm_btn.click().run()

        self.assertFalse(master_writer.master_exists())

    # 15. Enrichment metadata is removed only with its own staging entry.
    def test_enrichment_metadata_removed_only_with_its_own_entry(self):
        incomplete = self._staged(self._rows(275), "hash-union", status="in_progress", unique=126)
        complete = self._staged(self._rows(275), "hash-union", status="complete", unique=126)

        at = self._open_review()
        discard_btn = next(b for b in at.button if b.key == f"discard_single_{incomplete}")
        discard_btn.click().run()
        confirm_btn = next(b for b in at.button if b.key == f"discard_single_confirm_btn_{incomplete}")
        confirm_btn.click().run()

        self.assertEqual(file_store.get_staging_enrichment_summary(complete)["status"], "complete")

    # 16. Version history remains unaffected by a staging discard (no
    # master write happens at all).
    def test_version_history_unaffected_by_discard(self):
        incomplete = self._staged(self._rows(275), "hash-union", status="in_progress", unique=126)
        self._staged(self._rows(275), "hash-union", status="complete", unique=126)
        versions_before = master_writer.list_versions()

        at = self._open_review()
        discard_btn = next(b for b in at.button if b.key == f"discard_single_{incomplete}")
        discard_btn.click().run()
        confirm_btn = next(b for b in at.button if b.key == f"discard_single_confirm_btn_{incomplete}")
        confirm_btn.click().run()

        self.assertEqual(master_writer.list_versions(), versions_before)


class SinglePendingUploadDiscardControlTests(unittest.TestCase):
    """
    Regression tests for a second, separate real production report: with
    EXACTLY ONE pending upload, the page showed two near-identical discard
    buttons for the same effective action - the per-file "Discard this
    upload" (_render_single_file_discard) AND the whole-batch "Discard this
    pending upload" (the old len==1 label branch of _render_discard_pending).

    Fix: with exactly one pending upload, _render_discard_pending now
    renders nothing at all, and the per-file button is relabeled "Discard
    upload" (see _render_brochure_enrichment_summary) so it stands alone as
    the ONE discard control on the page. With 2+ pending uploads, nothing
    changes: each file's own "Discard this upload" button plus a single
    "Discard all pending uploads" bulk button, as already covered by
    PendingStagingManagementTests above.
    """

    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp.name)
        self._counter = 0

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def _rows(self, n, building_prefix="Building"):
        return [
            ListingRow(
                building=f"{building_prefix} {i}", provider="UNION", floor_unit=f"{i}th",
                property_id=f"{building_prefix}-{i}",
            )
            for i in range(n)
        ]

    def _staged(self, rows, content_hash, status=None, processed_urls=None, unique=1, filename="UNION.xlsx"):
        self._counter += 1
        stem, suffix = Path(filename).stem, Path(filename).suffix
        path = file_store.save_staging_file(rows, f"{stem}__{self._counter}{suffix}", content_hash=content_hash)
        meta = file_store._read_meta(path)
        meta["filename"] = filename
        file_store._write_meta(path, meta)
        if status == "complete":
            file_store.set_staging_enrichment_summary(
                path, {"unique_brochures_considered": unique, "rows_eligible": len(rows), "rows_enriched": 0},
                processed_urls or {f"https://{i}.pdf": "ok" for i in range(unique)},
            )
        elif status == "in_progress":
            file_store.set_staging_enrichment_progress(path, processed_urls or {}, unique)
        return path

    def _open_review(self):
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)
        return at

    def test_one_pending_upload_shows_exactly_one_discard_button_labeled_discard_upload(self):
        self._staged(self._rows(5), "hash-solo", status="complete", unique=1)

        at = self._open_review()

        discard_buttons = [
            b for b in at.button
            if b.label in ("Discard upload", "Discard this upload", "Discard all pending uploads")
        ]
        self.assertEqual(len(discard_buttons), 1)
        self.assertEqual(discard_buttons[0].label, "Discard upload")

    def test_one_pending_upload_confirming_discard_deletes_only_that_staging_entry(self):
        solo = self._staged(self._rows(5), "hash-solo", status="complete", unique=1)

        at = self._open_review()
        discard_btn = next(b for b in at.button if b.key == f"discard_single_{solo}")
        discard_btn.click().run()
        confirm_btn = next(b for b in at.button if b.key == f"discard_single_confirm_btn_{solo}")
        confirm_btn.click().run()
        self.assertFalse(at.exception)

        self.assertEqual(file_store.list_pending_staging_files(), [])

    def test_one_pending_upload_cancel_deletes_nothing(self):
        solo = self._staged(self._rows(5), "hash-solo", status="complete", unique=1)

        at = self._open_review()
        discard_btn = next(b for b in at.button if b.key == f"discard_single_{solo}")
        discard_btn.click().run()
        cancel_btn = next(b for b in at.button if b.key == f"discard_single_cancel_{solo}")
        cancel_btn.click().run()
        self.assertFalse(at.exception)

        self.assertEqual(file_store.list_pending_staging_files(), [solo])
        self.assertIsNotNone(file_store.get_staging_enrichment_summary(solo))

    def test_two_pending_uploads_show_per_file_and_bulk_buttons_unchanged(self):
        self._staged(self._rows(5, "June Building"), "hash-june", status="complete", unique=1, filename="June.xlsx")
        self._staged(self._rows(5, "July Building"), "hash-july", status="complete", unique=1, filename="July.xlsx")

        at = self._open_review()

        per_file = [b for b in at.button if b.label == "Discard this upload"]
        bulk = [b for b in at.button if b.label == "Discard all pending uploads"]
        solo_label = [b for b in at.button if b.label == "Discard upload"]
        self.assertEqual(len(per_file), 2)
        self.assertEqual(len(bulk), 1)
        self.assertEqual(len(solo_label), 0)

    def test_two_pending_uploads_individual_discard_removes_only_that_one(self):
        june = self._staged(self._rows(5, "June Building"), "hash-june", status="complete", unique=1, filename="June.xlsx")
        july = self._staged(self._rows(5, "July Building"), "hash-july", status="complete", unique=1, filename="July.xlsx")

        at = self._open_review()
        discard_btn = next(b for b in at.button if b.key == f"discard_single_{june}")
        discard_btn.click().run()
        confirm_btn = next(b for b in at.button if b.key == f"discard_single_confirm_btn_{june}")
        confirm_btn.click().run()
        self.assertFalse(at.exception)

        self.assertEqual(file_store.list_pending_staging_files(), [july])

    def test_two_pending_uploads_bulk_discard_removes_both(self):
        self._staged(self._rows(5, "June Building"), "hash-june", status="complete", unique=1, filename="June.xlsx")
        self._staged(self._rows(5, "July Building"), "hash-july", status="complete", unique=1, filename="July.xlsx")

        at = self._open_review()
        discard_btn = next(b for b in at.button if b.key == "discard_pending")
        discard_btn.click().run()
        confirm_btn = next(b for b in at.button if b.key == "discard_pending_confirm_btn")
        confirm_btn.click().run()
        self.assertFalse(at.exception)

        self.assertEqual(file_store.list_pending_staging_files(), [])

    def test_rerun_after_dropping_from_two_to_one_pending_does_not_leak_bulk_confirm_state(self):
        # A reviewer opens the confirm prompt on the (now-removed) bulk
        # "Discard all pending uploads" control while 2 uploads are still
        # pending, then - via any path - ends up back on a rerun where only
        # one upload remains pending. _render_discard_pending must not
        # leave discard_pending_confirm=True armed against a control that
        # no longer renders, which could otherwise surface stale state if a
        # future rerun ever brought the bulk control back.
        june = self._staged(self._rows(5, "June Building"), "hash-june", status="complete", unique=1, filename="June.xlsx")
        self._staged(self._rows(5, "July Building"), "hash-july", status="complete", unique=1, filename="July.xlsx")

        at = self._open_review()
        bulk_btn = next(b for b in at.button if b.key == "discard_pending")
        at = bulk_btn.click().run()
        self.assertTrue(at.session_state["discard_pending_confirm"])

        # Now collapse to a single pending upload behind the scenes (as if
        # the other upload were discarded via its own per-file control in a
        # separate browser tab/session) and rerun.
        file_store.discard_pending_staging_files(
            [p for p in file_store.list_pending_staging_files() if p != june]
        )
        at.run()

        self.assertFalse(at.exception)
        self.assertNotIn("discard_pending_confirm", at.session_state)
        bulk_buttons = [b for b in at.button if b.label == "Discard all pending uploads"]
        self.assertEqual(len(bulk_buttons), 0)
        solo_buttons = [b for b in at.button if b.label == "Discard upload"]
        self.assertEqual(len(solo_buttons), 1)

    def test_approval_flow_unaffected_by_single_upload_discard_relabeling(self):
        self._staged(self._rows(3), "hash-solo", status="complete", unique=1)

        at = self._open_review()
        approve_buttons = [b for b in at.button if "master" in b.label.lower() or "approve" in b.label.lower()]
        self.assertTrue(approve_buttons, "expected an approve/apply-to-master button to still be present")

    def test_existing_staging_display_unaffected_by_single_upload_relabeling(self):
        self._staged(self._rows(5), "hash-solo", status="complete", unique=1)

        at = self._open_review()
        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("1 upload pending", caption_text)
        self.assertIn("5", caption_text)


if __name__ == "__main__":
    unittest.main()
