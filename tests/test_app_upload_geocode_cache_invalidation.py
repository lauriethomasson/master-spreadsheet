"""
Regression tests for a real reported gap: geocode.py's own source was not
part of either content-hash cache-invalidation mechanism app.py uses to
decide whether a re-uploaded file is "identical to a previous upload,
reused rather than re-extracted" - _SPREADSHEET_LOGIC_FINGERPRINT for
spreadsheets, _PDF_EMAIL_LOGIC_FINGERPRINT (extract.py's/extract_email.py's
own source + now geocode.py bytes too) for PDF/email.
geocode_rows() runs unconditionally right after a fresh extraction
for BOTH source types, so a geocoding-logic change must invalidate an
already-staged result exactly like an extract_spreadsheet.py/
extract_spreadsheet_gemini.py/brochure_enrichment.py change already does -
it simply never did.

Also covers a second, later gap of the exact same shape: once email
uploads started running automatic brochure enrichment too (see app.py's
own is_email_source), a brochure_enrichment.py change had to start
invalidating an already-staged EMAIL result the same way it already does
for a spreadsheet upload - see PdfEmailContentHashCompositionTests below.
Deliberately never folded in for a PDF upload, which still never runs
automatic enrichment at all.

Confirmed against a real report: after landing the geocoding postcode-
validation fix (see geocode.py's own module docstring - rejecting a Places
candidate that contradicts the source's own postcode evidence), re-
uploading the real beem Live Flex Availability.xlsx kept showing "identical
to a previous upload, reused rather than re-extracted" and the old, pre-fix
coordinates - dedup working exactly as designed, just blind to this one
dependency (extract_spreadsheet.py/extract_spreadsheet_gemini.py/
brochure_enrichment.py changes DID correctly invalidate the cache before,
which is why earlier fixes to those files were picked up on re-upload).

Fix: fold geocode.py's own source bytes into both existing mechanisms - no
new version-tracking system, matching the existing automatic pattern
already used for extract_spreadsheet_gemini.py/brochure_enrichment.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_upload_geocode_cache_invalidation -v
"""

import hashlib
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app
import geocode
from storage.file_store import list_pending_staging_files, load_staging_as_dataframe

# The bare `import app` above executes app.py's top-level page code once
# with no active Streamlit runtime. With no runtime, st.form (the paste-a-
# link input's own form - see app.py's own comment there) mutates the
# process-wide main DeltaGenerator singleton's _form_data IN PLACE instead
# of creating a genuinely separate child block - confirmed directly against
# a minimal repro. Left uncleared, this taints every subsequent AppTest run
# of app.py in this process (this file's own AND any other test module's)
# with a spurious "Forms cannot be nested in other forms." the first time
# it reaches that same st.form, even though nothing is actually nested. See
# tests/test_app_upload_paste_a_link.py's own copy of this same fix for the
# full explanation - duplicated here (no shared conftest.py exists yet)
# since this file independently imports app and drives AppTest against it.
from streamlit.delta_generator_singletons import get_dg_singleton_instance

get_dg_singleton_instance().main_dg._form_data = None

BASE = Path(__file__).resolve().parent.parent


def _clear_pending():
    # Shares the real repo's staging/ directory (never an isolated cwd,
    # same convention as test_app_upload_brochure_enrichment.py) - clears
    # file_store's own st.cache_data-backed lookups first so a stale cached
    # hash-lookup from an earlier test can't leak into this one.
    from storage import file_store as _file_store
    _file_store._list_pending_staging_files_cached.clear()
    _file_store._find_previous_upload_by_hash_cached.clear()
    _file_store._load_staging_as_dataframe_cached.clear()
    for p in list_pending_staging_files():
        (BASE / p).unlink(missing_ok=True)
        (BASE / p).with_suffix(".meta.json").unlink(missing_ok=True)


def _workbook(building="New Derwent House WC1") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Availability"
    ws.append(["Building", "Floor/Unit", "Size (sq ft)", "Monthly Rate"])
    ws.append([building, "4th Floor", 2000, 15000])
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _upload_and_extract(at, filename, file_bytes):
    at.file_uploader[0].upload(
        filename, file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    at.run()
    extract_buttons = [b for b in at.button if b.label == "Extract"]
    extract_buttons[0].click().run()
    return at


class FingerprintCompositionTests(unittest.TestCase):
    """Pure, no upload involved - proves geocode.py is genuinely part of
    the formula, not just mentioned in a comment."""

    def test_spreadsheet_fingerprint_is_computed_from_geocode_py_bytes_too(self):
        expected = hashlib.sha256(
            Path(app.extract_spreadsheet.__file__).read_bytes()
            + Path(app.extract_spreadsheet_gemini.__file__).read_bytes()
            + Path(app.brochure_enrichment.__file__).read_bytes()
            + Path(geocode.__file__).read_bytes()
        ).hexdigest()
        self.assertEqual(app._SPREADSHEET_LOGIC_FINGERPRINT, expected)

    def test_a_hash_computed_under_a_different_fingerprint_never_matches_current(self):
        # Simulates "this cached entry was produced by older geocoding
        # code" - a hash computed under a DIFFERENT (older) fingerprint must
        # never be treated as identical by the CURRENT _spreadsheet_content_
        # hash, i.e. an old processing-version result can never masquerade
        # as current.
        file_bytes = _workbook()
        old_hash = hashlib.sha256(
            b"pretend-old-fingerprint-before-the-geocode-fix" + b"\0" + file_bytes + b"\0" + b"{}"
        ).hexdigest()
        current_hash = app._spreadsheet_content_hash(file_bytes, {})
        self.assertNotEqual(old_hash, current_hash)

    def test_same_bytes_and_decisions_hash_identically_every_call(self):
        # Same source + same (current) processing version still reuses -
        # the positive case this fix must not break.
        file_bytes = _workbook()
        self.assertEqual(
            app._spreadsheet_content_hash(file_bytes, {}),
            app._spreadsheet_content_hash(file_bytes, {}),
        )

    def test_different_bytes_hash_differently(self):
        self.assertNotEqual(
            app._spreadsheet_content_hash(_workbook("Building A"), {}),
            app._spreadsheet_content_hash(_workbook("Building B"), {}),
        )

    def test_filename_never_participates_in_the_hash(self):
        # content_hash is purely byte-based, filename-independent (see
        # _spreadsheet_content_hash's own signature - it takes no filename
        # at all) - a different filename + identical bytes + same version
        # CAN reuse, the existing, intended behavior, confirmed end-to-end
        # in UploadReuseAcrossAFingerprintChangeTests below (which uses two
        # different filenames for the same bytes throughout).
        import inspect
        self.assertNotIn("filename", inspect.signature(app._spreadsheet_content_hash).parameters)


class PdfEmailContentHashCompositionTests(unittest.TestCase):
    """
    Pure, no upload involved - app._pdf_or_email_content_hash, the PDF/
    email counterpart to _spreadsheet_content_hash above. Confirms
    brochure_enrichment.py's own source is folded in for an email upload
    (which now runs automatic brochure enrichment too - see app.py's own
    is_email_source) but deliberately NOT for a PDF upload (which still
    never runs it at all - see brochure_enrichment.py's own module
    docstring), exactly mirroring why _SPREADSHEET_LOGIC_FINGERPRINT
    already includes it unconditionally for every spreadsheet upload.
    """

    def test_email_hash_is_computed_from_brochure_enrichment_py_bytes_too(self):
        file_bytes = b"pretend .eml bytes"
        expected = hashlib.sha256(
            app._PDF_EMAIL_LOGIC_FINGERPRINT.encode("utf-8") + b"\0" + file_bytes + b"\0"
            + Path(geocode.__file__).read_bytes() + b"\0" + Path(app.brochure_enrichment.__file__).read_bytes()
        ).hexdigest()
        self.assertEqual(app._pdf_or_email_content_hash(".eml", file_bytes), expected)

    def test_pdf_hash_never_includes_brochure_enrichment_py(self):
        file_bytes = b"pretend .pdf bytes"
        expected = hashlib.sha256(
            app._PDF_EMAIL_LOGIC_FINGERPRINT.encode("utf-8") + b"\0" + file_bytes + b"\0"
            + Path(geocode.__file__).read_bytes()
        ).hexdigest()
        self.assertEqual(app._pdf_or_email_content_hash(".pdf", file_bytes), expected)

    def test_pdf_email_fingerprint_is_computed_from_extract_and_extract_email_bytes(self):
        # Proves _PDF_EMAIL_LOGIC_FINGERPRINT is genuinely a hash of
        # extract.py's/extract_email.py's own source, not just mentioned in
        # a comment - mirrors FingerprintCompositionTests' own spreadsheet
        # counterpart above.
        expected = hashlib.sha256(
            Path(app.extract.__file__).read_bytes()
            + Path(app.extract_email.__file__).read_bytes()
        ).hexdigest()
        self.assertEqual(app._PDF_EMAIL_LOGIC_FINGERPRINT, expected)

    def test_content_hash_is_sensitive_to_the_pdf_email_fingerprint(self):
        # _PDF_EMAIL_LOGIC_FINGERPRINT is a module-level constant computed
        # ONCE at import time from extract.py's/extract_email.py's own
        # source bytes (see test_pdf_email_fingerprint_is_computed_from_
        # extract_and_extract_email_bytes above, which proves that
        # composition directly) - so patching pathlib.Path.read_bytes after
        # app.py is already imported can never reach it (the constant is
        # already baked in, unlike geocode.py's fingerprint-free inclusion
        # below, which IS re-read on every call). What CAN be tested purely
        # is that _pdf_or_email_content_hash's result genuinely depends on
        # that constant's current value - patching it directly here stands
        # in for "if extract.py's/extract_email.py's bytes had been
        # different at import time, _PDF_EMAIL_LOGIC_FINGERPRINT would have
        # been different too" (already proven above), so together these two
        # tests confirm a change to either file's own content changes the
        # resulting content_hash.
        file_bytes = b"pretend .pdf bytes"
        original_hash = app._pdf_or_email_content_hash(".pdf", file_bytes)

        with patch.object(app, "_PDF_EMAIL_LOGIC_FINGERPRINT", "a-different-fingerprint"):
            changed_hash = app._pdf_or_email_content_hash(".pdf", file_bytes)

        self.assertNotEqual(original_hash, changed_hash)

    def test_current_content_hash_differs_from_old_extraction_version_formula(self):
        # Proves this fix actually unsticks the real, confirmed stale-cache
        # bug it was written to close (see this module's own docstring and
        # _PDF_EMAIL_LOGIC_FINGERPRINT's own comment in app.py):
        # EXTRACTION_VERSION stayed "3" across two real extract.py fixes
        # that landed after it was introduced, so a PDF/email content_hash
        # computed under the CURRENT source-hash formula must differ from
        # what the OLD EXTRACTION_VERSION = "3" formula would have produced
        # for the exact same bytes - every already-cached PDF/email result
        # becomes stale exactly once on next upload, not just future ones.
        file_bytes = b"pretend .pdf bytes"
        old_formula_hash = hashlib.sha256(
            "3".encode("utf-8") + b"\0" + file_bytes + b"\0" + Path(geocode.__file__).read_bytes()
        ).hexdigest()
        current_hash = app._pdf_or_email_content_hash(".pdf", file_bytes)
        self.assertNotEqual(old_formula_hash, current_hash)

    def test_pdf_and_email_hash_differently_for_the_exact_same_bytes(self):
        # Since brochure_enrichment.py's bytes are only folded in for one
        # of the two, the same underlying content must never collide
        # between a PDF and an email upload.
        file_bytes = b"identical bytes, different upload type"
        self.assertNotEqual(
            app._pdf_or_email_content_hash(".pdf", file_bytes),
            app._pdf_or_email_content_hash(".eml", file_bytes),
        )

    def test_same_suffix_and_bytes_hash_identically_every_call(self):
        file_bytes = b"pretend .eml bytes"
        self.assertEqual(
            app._pdf_or_email_content_hash(".eml", file_bytes),
            app._pdf_or_email_content_hash(".eml", file_bytes),
        )

    def test_different_bytes_hash_differently(self):
        self.assertNotEqual(
            app._pdf_or_email_content_hash(".eml", b"one email"),
            app._pdf_or_email_content_hash(".eml", b"a different email"),
        )


class UploadReuseAcrossAFingerprintChangeTests(unittest.TestCase):
    """
    End-to-end via the real app.py upload flow. geocode.call_places_text_
    search is mocked (ZERO_RESULTS) purely to keep this fast/deterministic
    and avoid real network calls - the actual geocoding OUTCOME is
    irrelevant here, only whether it runs at all (call count) vs is skipped
    entirely by the reuse path.
    """

    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_same_bytes_same_fingerprint_reuses_and_skips_geocoding(self):
        # "Kent House" (no postcode/district hint of its own) rather than
        # this file's default "New Derwent House WC1" - deliberately a
        # building geocode.py's own multi-candidate fallback (see its own
        # module docstring) never adds extra Places attempts for, so this
        # test's call-count assertions stay a pure "did geocoding run at
        # all" check, decoupled from however many fallback tiers that
        # module happens to try internally.
        file_bytes = _workbook(building="Kent House")

        with patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}) as mock_places:
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            _upload_and_extract(at, "test.xlsx", file_bytes)
            self.assertFalse(at.exception)
            self.assertEqual(mock_places.call_count, 1)

            at2 = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at2.run()
            _upload_and_extract(at2, "test-reupload.xlsx", file_bytes)
            self.assertFalse(at2.exception)

            # Reused - geocode was never called a second time.
            self.assertEqual(mock_places.call_count, 1)

        markdown_text = "".join(m.value for m in at2.markdown)
        self.assertIn("identical to a previous upload, reused rather than re-extracted", markdown_text)

    def test_same_bytes_but_fingerprint_changed_does_not_reuse_and_regeocodes(self):
        # Simulates exactly the real Beem report: the source bytes are
        # unchanged, but geocode.py's own logic (folded into
        # _SPREADSHEET_LOGIC_FINGERPRINT) has changed since the first
        # upload - the second upload must NOT reuse the stale cached rows,
        # and must run geocoding again under the current code.
        #
        # AppTest.from_file re-execs app.py fresh (exec(code, module.
        # __dict__)) in its OWN namespace on every .run() - patching the
        # `app` module THIS test file imported has no effect on that exec'd
        # copy at all, they're different module objects entirely. What both
        # genuinely share is the same running Python process, so patching
        # pathlib.Path.read_bytes itself (real, unpatched, for every path
        # except geocode.py's own file) reaches the exec'd script's own
        # Path(geocode.__file__).read_bytes() call just as it would a real
        # source change to geocode.py on disk - without touching the real
        # file.
        file_bytes = _workbook(building="Kent House")
        real_read_bytes = Path.read_bytes
        geocode_path = Path(geocode.__file__).resolve()

        def _fake_read_bytes(self):
            data = real_read_bytes(self)
            if self.resolve() == geocode_path:
                return data + b"\0-simulated-geocode-py-change"
            return data

        with patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}) as mock_places:
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            _upload_and_extract(at, "test.xlsx", file_bytes)
            self.assertFalse(at.exception)
            self.assertEqual(mock_places.call_count, 1)

            with patch.object(Path, "read_bytes", _fake_read_bytes):
                at2 = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
                at2.run()
                _upload_and_extract(at2, "test-reupload.xlsx", file_bytes)
                self.assertFalse(at2.exception)

            # NOT reused - geocoding actually ran again under the "new" code.
            self.assertEqual(mock_places.call_count, 2)

        markdown_text = "".join(m.value for m in at2.markdown)
        self.assertNotIn("identical to a previous upload, reused rather than re-extracted", markdown_text)

        # Both uploads produced their own staging entry (see save_staging_
        # file's own "reused or freshly extracted alike" docstring) - the
        # second one's rows came from genuinely re-running the current
        # pipeline, not a stale copy.
        pending = list_pending_staging_files()
        self.assertEqual(len(pending), 2)
        for path in pending:
            df = load_staging_as_dataframe(path)
            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["building"], "Kent House")


if __name__ == "__main__":
    unittest.main()
