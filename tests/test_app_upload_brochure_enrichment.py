"""
App-level regression tests for AUTOMATIC brochure enrichment on spreadsheet
Extract (see app.py's _run_automatic_brochure_enrichment and brochure_
enrichment.py's own module docstring) - enrichment now runs on its own,
immediately after a fresh spreadsheet upload's base rows are staged, with
no separate "Enrich from brochures" button anywhere. Confirms: it only
fires when there's genuinely something to enrich, the base rows are staged
BEFORE any brochure/Gemini call happens, a broken brochure never fails the
whole upload, and the fingerprint invalidation this depends on is correct
again now that brochure_enrichment.py runs unconditionally during
extraction (see app.py's own _SPREADSHEET_LOGIC_FINGERPRINT comment).

Runs the real app.py end-to-end via Streamlit's AppTest, with httpx and
extract.extract_raw_units mocked - never touches the real network or the
real Gemini API.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_upload_brochure_enrichment -v
"""

import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app
import brochure_enrichment
from storage.file_store import (
    get_staging_enrichment_summary,
    list_pending_staging_files,
    load_staging_as_dataframe,
)

BASE = Path(__file__).resolve().parent.parent


def _clear_pending():
    # Clears file_store's own st.cache_data-backed lookup caches FIRST -
    # this file's tests share the real repo's staging/ directory (never an
    # isolated cwd), so a stale cached listing/hash-lookup from an
    # immediately-preceding test in a DIFFERENT class could otherwise
    # either miss a file that needs deleting here, or hand a LATER test a
    # stale path/result that doesn't reflect what THIS test just did.
    from storage import file_store as _file_store
    _file_store._list_pending_staging_files_cached.clear()
    _file_store._find_previous_upload_by_hash_cached.clear()
    _file_store._load_staging_as_dataframe_cached.clear()
    for p in list_pending_staging_files():
        (BASE / p).unlink(missing_ok=True)
        (BASE / p).with_suffix(".meta.json").unlink(missing_ok=True)


def _union_style_workbook(n_rows=1) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Availability"
    ws.append(["Building", "Floor/Unit", "Size (sq ft)", "Monthly Rate", "Brochure"])
    for i in range(n_rows):
        ws.append([f"Building {i}", f"{i}th Floor", 1200, 15000, "https://example.com/brochure.pdf"])
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _no_brochure_workbook() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Availability"
    ws.append(["Building", "Floor/Unit", "Size (sq ft)", "Monthly Rate"])
    ws.append(["40 New Bond Street", "3rd Floor", 2000, 15000])
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _pdf_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"%PDF-1.4 fake"
    resp.headers = {"content-type": "application/pdf"}
    resp.raise_for_status.side_effect = None
    return resp


class AutomaticEnrichmentOnExtractTests(unittest.TestCase):
    def setUp(self):
        _clear_pending()
        brochure_enrichment._extract_brochure_units.cache_clear()

    def tearDown(self):
        _clear_pending()
        brochure_enrichment._extract_brochure_units.cache_clear()

    def test_eligible_upload_triggers_automatic_enrichment_with_no_button(self):
        raw_units = {"units": [{
            "building": "Building 0", "floor_unit": "0th Floor",
            "special_features": "Private terrace; showers; cycle storage",
        }]}
        with patch("brochure_enrichment.httpx.get", return_value=_pdf_response()) as mock_get, \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value=raw_units) as mock_extract:
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Union.xlsx", _union_style_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        # No button anywhere asks the user to trigger this themselves.
        self.assertEqual([b.label for b in at.button if "nrich" in (b.label or "")], [])

        brochure_calls = [c for c in mock_get.call_args_list if c.args and c.args[0] == "https://example.com/brochure.pdf"]
        self.assertEqual(len(brochure_calls), 1)
        mock_extract.assert_called_once()

        pending = list_pending_staging_files()
        df = load_staging_as_dataframe(pending[0])
        self.assertEqual(df.iloc[0]["special_features"], "Private terrace; showers; cycle storage")
        self.assertEqual(df.iloc[0]["size_sqft"], 1200)  # primary source untouched

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("Brochure enrichment complete", caption_text)
        # The "keep this page open" caption shares run_brochure_enrichment's
        # own progress_slot placeholder with the progress bar (see that
        # function's own docstring) - both must disappear together once
        # the run has actually finished, never linger after completion.
        self.assertNotIn("keep this page open", caption_text)

    def test_no_eligible_rows_means_no_brochure_calls_at_all(self):
        with patch("brochure_enrichment.httpx.get") as mock_get, \
             patch("brochure_enrichment.extract.render_and_extract") as mock_extract:
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Kitts.xlsx", _no_brochure_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        brochure_calls = [c for c in mock_get.call_args_list if c.args and "brochure" in str(c.args[0])]
        self.assertEqual(brochure_calls, [])
        mock_extract.assert_not_called()
        self.assertIsNone(get_staging_enrichment_summary(list_pending_staging_files()[0]))

    def test_base_rows_are_staged_before_any_brochure_call_is_made(self):
        staged_already = {}

        def _check_and_return(*a, **kw):
            staged_already["yes"] = bool(list_pending_staging_files())
            return {"units": []}

        with patch("brochure_enrichment.httpx.get", return_value=_pdf_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", side_effect=_check_and_return):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Union.xlsx", _union_style_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        self.assertEqual(staged_already, {"yes": True})

    def test_ten_rows_sharing_one_brochure_costs_exactly_one_gemini_call(self):
        raw_units = {"units": [
            {"building": f"Building {i}", "floor_unit": f"{i}th Floor", "special_features": f"Feature {i}"}
            for i in range(10)
        ]}
        with patch("brochure_enrichment.httpx.get", return_value=_pdf_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value=raw_units) as mock_extract:
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Union.xlsx", _union_style_workbook(n_rows=10),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        mock_extract.assert_called_once()
        pending = list_pending_staging_files()
        df = load_staging_as_dataframe(pending[0])
        self.assertEqual(len(df), 10)
        self.assertTrue(all(df["special_features"].notna()))

    def test_a_run_that_completes_normally_is_tagged_complete_not_left_in_progress(self):
        # _run_automatic_brochure_enrichment writes an interim
        # "in_progress" marker before enrich_rows_grouped even starts (see
        # set_staging_enrichment_progress) - a run that finishes normally
        # must overwrite it with the final status="complete" summary, never
        # leave the interim marker behind to be misread later as an
        # interrupted run (see pages/2_Review_and_Master.py's own
        # _render_brochure_enrichment_summary).
        raw_units = {"units": [{
            "building": "Building 0", "floor_unit": "0th Floor", "special_features": "Roof terrace",
        }]}
        with patch("brochure_enrichment.httpx.get", return_value=_pdf_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value=raw_units):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Union.xlsx", _union_style_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        pending = list_pending_staging_files()
        stats = get_staging_enrichment_summary(pending[0])
        self.assertEqual(stats["status"], "complete")

    def test_a_broken_brochure_does_not_fail_the_whole_upload(self):
        # Patched at _extract_brochure_units, not httpx.get - that
        # attribute is shared with geocode.py's own, unrelated Google
        # Geocoding calls (see test_extract_never_fetches_or_sends_the_
        # linked_brochure_to_gemini's own comment on this same sharing),
        # and geocode.py has no exception handling of its own around it; a
        # raising httpx.get here would break geocoding, not just the thing
        # actually under test.
        with patch("brochure_enrichment._extract_brochure_units", side_effect=RuntimeError("network down")):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Union.xlsx", _union_style_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        success_text = "".join(s.value for s in at.success)
        self.assertIn("Extracted and staged", success_text)
        pending = list_pending_staging_files()
        self.assertEqual(len(pending), 1)
        df = load_staging_as_dataframe(pending[0])
        self.assertEqual(df.iloc[0]["building"], "Building 0")

    def test_base_extraction_survives_even_a_catastrophic_enrichment_crash(self):
        # Belt-and-braces on top of enrich_rows_grouped's own per-brochure
        # isolation (see MalformedUnitEntryTests in test_brochure_
        # enrichment.py) - app.py's own try/except around the WHOLE
        # run_brochure_enrichment call is what must save the day if
        # something entirely unanticipated still raises above that layer;
        # this proves that outer safety net directly, by making the whole
        # function raise, not just one brochure's own fetch.
        with patch("brochure_enrichment.run_brochure_enrichment", side_effect=RuntimeError("unexpected crash")):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Union.xlsx", _union_style_workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        success_text = "".join(s.value for s in at.success)
        self.assertIn("Extracted and staged", success_text)
        warning_text = "".join(w.value for w in at.warning)
        self.assertIn("unexpected error", warning_text)
        pending = list_pending_staging_files()
        self.assertEqual(len(pending), 1)
        df = load_staging_as_dataframe(pending[0])
        self.assertEqual(df.iloc[0]["building"], "Building 0")


class ReuploadWhileIncompleteTests(unittest.TestCase):
    """
    Content-hash dedup (see app.py's own find_previous_upload_by_hash)
    reuses an already-extracted result for a byte-identical re-upload
    rather than re-extracting - but when the matched entry's OWN brochure
    enrichment was left incomplete, the NEW (re-upload) staging entry now
    CONTINUES that progress automatically (see app.py's own
    resume_already_processed) rather than silently freezing at the same
    partial state forever with no automatic path to ever finish - a real
    production report confirmed a re-upload used to just sit there
    unfinished. Already-"ok" brochures are still never re-fetched/re-sent
    to Gemini just because this landed on a new staging path rather than
    the original one.

    Each upload event still stages its OWN file (see save_staging_file's
    own docstring - "reused or freshly extracted alike", a pre-existing,
    unrelated design choice for multi-file-batch durability), so a
    re-upload genuinely produces a SECOND pending entry - the ORIGINAL
    (first) file's own status is never retroactively changed by this (only
    the NEW entry's own progresses), and Review & Master's own content-
    hash-based supersede logic (see storage.file_store.
    active_and_superseded_staging_files) is what keeps the two from
    double-counting rows once both are pending - see
    test_app_review_pending_staging_management.py for that half.
    """

    def setUp(self):
        _clear_pending()
        brochure_enrichment._extract_brochure_units.cache_clear()

    def tearDown(self):
        _clear_pending()
        brochure_enrichment._extract_brochure_units.cache_clear()

    def test_reuploading_the_same_file_while_incomplete_continues_its_progress(self):
        file_bytes = _union_style_workbook()

        with patch("brochure_enrichment._extract_brochure_units", side_effect=RuntimeError("interrupted")):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Union.xlsx", file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        pending = list_pending_staging_files()
        self.assertEqual(len(pending), 1)
        original_path = pending[0]
        # A genuine crash-mid-run leaves an "in_progress" marker (see
        # run_brochure_enrichment) - simulated directly here since a
        # RuntimeError from _extract_brochure_units is caught and recorded
        # as "unavailable", not an uncaught crash; this test is about the
        # RE-UPLOAD behavior, not reproducing the crash itself (see
        # test_app_review_brochure_enrichment.py for that).
        from storage.file_store import set_staging_enrichment_progress
        set_staging_enrichment_progress(original_path, {}, 1)

        with patch(
            "brochure_enrichment._extract_brochure_units",
            return_value=[{"building": "Building 0", "floor_unit": "0th Floor", "special_features": "Recovered"}],
        ) as mock_extract:
            at2 = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at2.run()
            # A different filename, IDENTICAL bytes - content_hash is
            # byte-based, filename-independent (see app.py's own
            # _spreadsheet_content_hash), so this is still genuinely a
            # "re-upload of the same file" for dedup purposes; using a
            # distinct stem here only avoids save_staging_file's own
            # second-resolution timestamped filename colliding with the
            # FIRST upload's staging path if both happen within the same
            # wall-clock second (a real, narrow, pre-existing edge case in
            # save_staging_file, unrelated to what THIS test is about -
            # see this test's own module docstring).
            at2.file_uploader[0].upload(
                "Union-reupload.xlsx", file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at2.run()
            extract_buttons = [b for b in at2.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at2.exception)

        # The re-upload's OWN staging entry continued the matched entry's
        # progress - the one remaining eligible brochure WAS attempted.
        mock_extract.assert_called_once()
        pending_after = list_pending_staging_files()
        new_path = next(p for p in pending_after if p != original_path)
        new_stats = get_staging_enrichment_summary(new_path)
        self.assertEqual(new_stats["status"], "complete")
        df = load_staging_as_dataframe(new_path)
        self.assertEqual(df.iloc[0]["special_features"], "Recovered")

        # The ORIGINAL file's own status is never retroactively changed -
        # only the NEW entry progresses.
        stats = get_staging_enrichment_summary(original_path)
        self.assertEqual(stats["status"], "in_progress")

    def test_reuploading_a_complete_match_does_not_re_run_enrichment(self):
        # The counterpart case: nothing to continue when the matched
        # entry's own enrichment already finished - this must stay a pure
        # reuse, exactly as before this feature existed.
        file_bytes = _union_style_workbook()

        with patch(
            "brochure_enrichment._extract_brochure_units",
            return_value=[{"building": "Building 0", "floor_unit": "0th Floor", "special_features": "Done"}],
        ):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Union.xlsx", file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        pending = list_pending_staging_files()
        original_stats = get_staging_enrichment_summary(pending[0])
        self.assertEqual(original_stats["status"], "complete")

        with patch("brochure_enrichment._extract_brochure_units") as mock_extract:
            at2 = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at2.run()
            at2.file_uploader[0].upload(
                "Union-reupload.xlsx", file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at2.run()
            extract_buttons = [b for b in at2.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at2.exception)

        mock_extract.assert_not_called()


class FingerprintIncludesBrochureEnrichmentAgainTests(unittest.TestCase):
    def test_fingerprint_includes_brochure_enrichment_again(self):
        # brochure_enrichment.py runs unconditionally during extraction
        # again now (automatically, right after staging - see app.py's own
        # _SPREADSHEET_LOGIC_FINGERPRINT comment), so a change to its
        # matching/field rules must invalidate an already-staged result
        # exactly like a change to extract_spreadsheet(_gemini).py/
        # geocode.py already does. Recomputes the exact same formula
        # independently and compares, so a future refactor that forgets to
        # fold it back in fails this test. See tests/
        # test_app_upload_geocode_cache_invalidation.py for the equivalent,
        # dedicated coverage of geocode.py's own inclusion here.
        import hashlib

        import extract_spreadsheet
        import extract_spreadsheet_gemini
        import geocode

        expected = hashlib.sha256(
            Path(extract_spreadsheet.__file__).read_bytes()
            + Path(extract_spreadsheet_gemini.__file__).read_bytes()
            + Path(brochure_enrichment.__file__).read_bytes()
            + Path(geocode.__file__).read_bytes()
        ).hexdigest()

        self.assertEqual(app._SPREADSHEET_LOGIC_FINGERPRINT, expected)


if __name__ == "__main__":
    unittest.main()
