"""
Regression tests for the upload-flow reorder: extract -> save -> brochure-
check -> geocode -> fill_missing_address_from_building, for EVERY upload
type now (spreadsheet, email, PDF, pasted link alike) - no more per-source
gating on automatic brochure enrichment at all (is_spreadsheet_source/
is_email_source/is_pdf_source used to matter for this; none do any more -
see app.py's own upload-flow comment, right after save_staging_file).

Real, confirmed incident this closes: a Colliers "Prospect House" row had
no street address of its own in the raw source - only its own brochure
genuinely stated one ("148-150 Great Portland Street") - but under the OLD
order (geocode_rows first, brochure-check after, spreadsheet/email only)
geocode_rows ran with nothing to go on but the bare name, landed on a
same-named but genuinely different building via geocode.py's own weaker
Tier 2 (Places name-only search), and nothing downstream ever revisited
it. Checking the brochure FIRST means geocode_rows usually has a real
address to work with already, so its own far more reliable Tier 1 (a real
Geocoding API address lookup) fires instead.

Deliberately a SEPARATE file from tests/test_app_upload_brochure_
enrichment.py/tests/test_app_upload_email_brochure_enrichment.py (which
cover brochure enrichment's own triggering/eligibility rules, unaffected
by this reorder) - this file is specifically about WHERE in the pipeline
that enrichment now runs relative to geocoding, for every source type.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_upload_brochure_before_geocode -v
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
from storage.file_store import list_pending_staging_files, load_staging_as_dataframe

# The bare `import app` above executes app.py's top-level page code once
# with no active Streamlit runtime - same fix this suite's other files
# already carry independently (see tests/test_app_upload_paste_a_link.py's
# own copy for the full explanation of the "Forms cannot be nested in
# other forms." taint this avoids).
from streamlit.delta_generator_singletons import get_dg_singleton_instance

get_dg_singleton_instance().main_dg._form_data = None

BASE = Path(__file__).resolve().parent.parent


def _clear_pending():
    from storage import file_store as _file_store
    _file_store._list_pending_staging_files_cached.clear()
    _file_store._find_previous_upload_by_hash_cached.clear()
    _file_store._load_staging_as_dataframe_cached.clear()
    for p in list_pending_staging_files():
        (BASE / p).unlink(missing_ok=True)
        (BASE / p).with_suffix(".meta.json").unlink(missing_ok=True)


def _workbook(building="Prospect House", brochure_link="https://example.com/prospect-house.pdf") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Availability"
    if brochure_link:
        ws.append(["Building", "Floor/Unit", "Size (sq ft)", "Monthly Rate", "Brochure"])
        ws.append([building, "1st Floor", 1200, 15000, brochure_link])
    else:
        ws.append(["Building", "Floor/Unit", "Size (sq ft)", "Monthly Rate"])
        ws.append([building, "1st Floor", 1200, 15000])
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


class BrochureAddressBackfillBeforeGeocodingTests(unittest.TestCase):
    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_blank_address_1_is_backfilled_from_brochure_and_geocoded_via_tier1_not_tier2(self):
        # The real Prospect House shape: no street address in the raw
        # source, but the brochure itself states one plainly.
        brochure_units = {"units": [{
            "building": "Prospect House", "address_1": "148-150 Great Portland Street", "postcode": "W1W 5QQ",
        }]}
        with patch("brochure_enrichment.httpx.get", return_value=_pdf_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value=brochure_units), \
             patch("geocode.call_geocoding_api", return_value={
                 "status": "OK", "lat": 51.5203, "lng": -0.1438, "address_components": [],
             }) as mock_tier1, \
             patch("geocode.call_places_text_search") as mock_tier2:
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Prospect.xlsx", _workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        pending = list_pending_staging_files()
        df = load_staging_as_dataframe(pending[0])
        self.assertEqual(df.iloc[0]["address_1"], "148-150 Great Portland Street")
        self.assertEqual(df.iloc[0]["postcode"], "W1W 5QQ")
        # The core behavior change this reorder exists for: Tier 1 (a real,
        # specific address lookup) fired because the brochure supplied a
        # real address BEFORE geocoding ever ran - Tier 2's weak name-only
        # search, which can't tell a same-named but genuinely different
        # building apart, was never even reached.
        mock_tier1.assert_called_once()
        mock_tier2.assert_not_called()
        self.assertFalse(bool(df.iloc[0]["geocode_unverified"]))

    def test_row_with_no_eligible_brochure_still_falls_through_to_tier2_guess_unchanged(self):
        # No Brochure column at all - genuinely nothing for enrichment to
        # check, so today's exact Tier 2 guess-and-flag behavior must be
        # completely unaffected by this reorder.
        with patch("geocode.call_places_text_search", return_value={
            "status": "OK", "lat": 51.5, "lng": -0.13, "address_components": [],
        }) as mock_tier2:
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Prospect.xlsx", _workbook(brochure_link=None),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        mock_tier2.assert_called()
        pending = list_pending_staging_files()
        df = load_staging_as_dataframe(pending[0])
        self.assertTrue(bool(df.iloc[0]["geocode_unverified"]))


class SaveStagingFileRunsBeforeGeocodeRowsTests(unittest.TestCase):
    """
    Confirms the literal call ORDER (not just the end result) for both
    branches that used to call geocode_rows internally, right after
    extraction - save_staging_file must now come first, geocode_rows
    second, for a fresh spreadsheet upload AND a fresh PDF/email upload
    alike. Patches storage.file_store.save_staging_file/geocode.
    geocode_rows (the modules app.py's own exec'd copy re-imports FROM on
    every AppTest run), never app.save_staging_file/app.geocode_rows -
    patching the bare-imported test-file copy has no effect on AppTest's
    own separately-exec'd script (see this suite's own established
    convention, e.g. tests/test_app_upload_email_brochure_enrichment.py's
    own _upload_email docstring).
    """

    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_spreadsheet_branch_saves_before_geocoding(self):
        import geocode as geocode_module
        from storage import file_store as file_store_module

        call_order = []
        real_save = file_store_module.save_staging_file
        real_geocode_rows = geocode_module.geocode_rows

        def _tracking_save(*args, **kwargs):
            call_order.append("save")
            return real_save(*args, **kwargs)

        def _tracking_geocode_rows(*args, **kwargs):
            call_order.append("geocode")
            return real_geocode_rows(*args, **kwargs)

        with patch("storage.file_store.save_staging_file", side_effect=_tracking_save), \
             patch("geocode.geocode_rows", side_effect=_tracking_geocode_rows), \
             patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Plain.xlsx", _workbook(brochure_link=None),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        self.assertEqual(call_order, ["save", "geocode"])

    def test_pdf_email_branch_saves_before_geocoding(self):
        import geocode as geocode_module
        from storage import file_store as file_store_module

        call_order = []
        real_save = file_store_module.save_staging_file
        real_geocode_rows = geocode_module.geocode_rows

        def _tracking_save(*args, **kwargs):
            call_order.append("save")
            return real_save(*args, **kwargs)

        def _tracking_geocode_rows(*args, **kwargs):
            call_order.append("geocode")
            return real_geocode_rows(*args, **kwargs)

        email_raw = {
            "provider": "MetSpace", "contacts": None,
            "units": [{"building": "Prospect House", "floor_unit": "1st Floor", "brochure_link": None}],
        }
        with patch("storage.file_store.save_staging_file", side_effect=_tracking_save), \
             patch("geocode.geocode_rows", side_effect=_tracking_geocode_rows), \
             patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}), \
             patch("extract_email.get_client"), \
             patch("extract_email.load_eml_body", return_value="email body"), \
             patch("extract_email.call_gemini", return_value=email_raw):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload(
                "Prospect.eml",
                b"From: agent@example.com\nTo: ops@example.com\nSubject: Availability\n\nBody",
                "message/rfc822",
            )
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        self.assertEqual(call_order, ["save", "geocode"])


if __name__ == "__main__":
    unittest.main()
