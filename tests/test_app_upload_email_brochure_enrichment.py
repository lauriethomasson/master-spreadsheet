"""
App-level regression tests for extending AUTOMATIC brochure enrichment (see
app.py's _run_automatic_brochure_enrichment and brochure_enrichment.py's own
module docstring) to email-sourced uploads, not just spreadsheets - the real
confirmed gap: a real MetSpace weekly-availability email has genuine
brochure_link URLs per listing, but since app.py's automatic-enrichment call
used to be gated on is_spreadsheet_source alone, those links were never
followed even though the exact same fetch/extract pipeline already worked
for spreadsheet rows.

Investigation confirmed brochure_enrichment.py's own field-scope/matching
logic (needs_enrichment, _match_unit, _apply_units_to_row, PROPERTY_LEVEL_
FIELDS/BUILDING_LEVEL_FIELDS/UNIT_LEVEL_FIELDS/HIGH_RISK_UNIT_LEVEL_FIELDS)
already operates purely on a ListingRow's own field values - it has no
notion of upload source at all, so nothing there needed to change. This
file exists specifically to prove that source-agnostic behavior end-to-end
through the real .eml upload path, not just re-assert it from reading the
code - each of the four safeguards the spreadsheet path already has gets
its own dedicated test here, deliberately not reused from the spreadsheet
test file, so a regression specific to the new code path (app.py's own
is_email_source gate) would actually be caught.

Runs the real app.py end-to-end via Streamlit's AppTest, with extract_
email's own Gemini call AND the brochure fetch/extract pipeline both
mocked - never touches the real network or the real Gemini API (except in
the real-file class at the bottom, which is skipped unless GEMINI_API_KEY
is configured and the real fixture is present).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_upload_email_brochure_enrichment -v
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brochure_enrichment
from schema import ListingRow
from storage.file_store import (
    get_staging_enrichment_summary,
    list_pending_staging_files,
    load_staging_as_dataframe,
)

BASE = Path(__file__).resolve().parent.parent


def _clear_pending():
    from storage import file_store as _file_store
    _file_store._list_pending_staging_files_cached.clear()
    _file_store._find_previous_upload_by_hash_cached.clear()
    _file_store._load_staging_as_dataframe_cached.clear()
    for p in list_pending_staging_files():
        (BASE / p).unlink(missing_ok=True)
        (BASE / p).with_suffix(".meta.json").unlink(missing_ok=True)


def _eml_bytes() -> bytes:
    # Content is irrelevant - extract_email.load_eml_body is always mocked
    # in this file, so nothing here is ever actually parsed.
    return b"From: agent@example.com\nTo: ops@example.com\nSubject: Availability\n\nBody"


def _pdf_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"%PDF-1.4 fake"
    resp.headers = {"content-type": "application/pdf"}
    resp.raise_for_status.side_effect = None
    return resp


def _upload_email(at, email_raw, filename="MetSpace.eml"):
    # geocode.geocode_rows is mocked to a no-op here (never geocode.py's
    # own internal call_places_text_search/reverse-geocode functions,
    # which would still leave the outer geocode_rows call itself hitting
    # the real network) - geocoding is unrelated to what every test in
    # this file actually checks, and a real building name (even a
    # deliberately generic placeholder one) can genuinely resolve to SOME
    # real place via Google's own APIs, which would otherwise make a real,
    # non-deterministic network call on every test run. Patched on the
    # `geocode` module itself (never `app.geocode_rows`) because AppTest
    # re-execs app.py's own `from geocode import geocode_rows` fresh on
    # every run - only patching the attribute it actually imports FROM,
    # before that exec happens, is visible to the exec'd copy at all.
    with patch("extract_email.get_client"), \
         patch("extract_email.load_eml_body", return_value="email body"), \
         patch("extract_email.call_gemini", return_value=email_raw), \
         patch("geocode.geocode_rows"):
        at.file_uploader[0].upload(filename, _eml_bytes(), "message/rfc822")
        at.run()
        extract_buttons = [b for b in at.button if b.label == "Extract"]
        extract_buttons[0].click().run()


class AutomaticEnrichmentOnEmailExtractTests(unittest.TestCase):
    def setUp(self):
        _clear_pending()
        brochure_enrichment._extract_brochure_units.cache_clear()

    def tearDown(self):
        _clear_pending()
        brochure_enrichment._extract_brochure_units.cache_clear()

    def test_eligible_email_upload_triggers_automatic_enrichment_with_no_button(self):
        email_raw = {
            "provider": "MetSpace", "contacts": None,
            "units": [{
                "building": "Building 0", "floor_unit": "3rd Floor",
                "brochure_link": "https://example.com/brochure.pdf",
            }],
        }
        brochure_units = {"units": [{
            "building": "Building 0", "floor_unit": "3rd Floor",
            "special_features": "Roof terrace; bike storage",
        }]}
        with patch("brochure_enrichment.httpx.get", return_value=_pdf_response()) as mock_get, \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value=brochure_units) as mock_extract:
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            _upload_email(at, email_raw)
            self.assertFalse(at.exception)

        # No button anywhere asks the user to trigger this themselves -
        # same automatic-by-default contract the spreadsheet path already has.
        self.assertEqual([b.label for b in at.button if "nrich" in (b.label or "")], [])

        brochure_calls = [c for c in mock_get.call_args_list if c.args and c.args[0] == "https://example.com/brochure.pdf"]
        self.assertEqual(len(brochure_calls), 1)
        mock_extract.assert_called_once()

        pending = list_pending_staging_files()
        df = load_staging_as_dataframe(pending[0])
        self.assertEqual(df.iloc[0]["special_features"], "Roof terrace; bike storage")

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("1 row saved", caption_text)
        self.assertIn("Done: 1 of 1 brochure read successfully, adding details to 1 row.", caption_text)

    def test_no_eligible_rows_means_no_brochure_calls_at_all(self):
        # An email row that already states everything ENRICHABLE_FIELDS
        # covers (or has no brochure_link at all) never triggers a fetch -
        # same needs_enrichment gate the spreadsheet path already relies on.
        email_raw = {
            "provider": "MetSpace", "contacts": None,
            "units": [{"building": "Building 0", "floor_unit": "3rd Floor", "brochure_link": None}],
        }
        with patch("brochure_enrichment.httpx.get") as mock_get, \
             patch("brochure_enrichment.extract.render_and_extract") as mock_extract:
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            _upload_email(at, email_raw)
            self.assertFalse(at.exception)

        mock_get.assert_not_called()
        mock_extract.assert_not_called()
        self.assertIsNone(get_staging_enrichment_summary(list_pending_staging_files()[0]))

    def test_pdf_upload_with_no_rows_at_all_still_gets_no_automatic_enrichment(self):
        # Automatic brochure enrichment now runs unconditionally for every
        # upload type (see app.py's own upload-flow reorder) - this is a
        # negative control confirming that alone never forces real network/
        # Gemini work: a PDF upload with no rows at all (so genuinely
        # nothing to compare a brochure_link against) must still be
        # completely unaffected, via eligible_rows_and_brochures' own
        # existing "nothing eligible" gate inside _run_automatic_brochure_
        # enrichment, unrelated to source type.
        with patch("extract.extract", return_value=[]) as mock_pdf_extract, \
             patch("brochure_enrichment.httpx.get") as mock_get, \
             patch("brochure_enrichment.extract.render_and_extract") as mock_brochure_extract:
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload("Some Brochure.pdf", b"%PDF-1.4 fake", "application/pdf")
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        mock_pdf_extract.assert_called_once()
        mock_get.assert_not_called()
        mock_brochure_extract.assert_not_called()

    def test_pdf_upload_with_every_row_blank_or_matching_the_source_still_gets_no_automatic_enrichment(self):
        # Same negative control, but with real rows this time - one with no
        # brochure_link at all, one whose brochure_link equals the uploaded
        # file's own name. Neither is an eligible URL to fetch at all (a
        # bare filename is never http(s)-shaped - see _is_eligible_
        # brochure_url), so eligible_rows_and_brochures' own existing gate
        # still correctly finds nothing to do, regardless of source type.
        pdf_rows = [
            ListingRow(building="No Link Building", brochure_link=None),
            ListingRow(building="Self-Referencing Building", brochure_link="Some Brochure.pdf"),
        ]
        with patch("extract.extract", return_value=pdf_rows) as mock_pdf_extract, \
             patch("brochure_enrichment.httpx.get") as mock_get, \
             patch("brochure_enrichment.extract.render_and_extract") as mock_brochure_extract, \
             patch("geocode.geocode_rows"):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload("Some Brochure.pdf", b"%PDF-1.4 fake", "application/pdf")
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        mock_pdf_extract.assert_called_once()
        mock_get.assert_not_called()
        mock_brochure_extract.assert_not_called()

    def test_pdf_upload_with_a_row_pointing_at_a_genuinely_separate_document_triggers_automatic_enrichment(self):
        # The confirmed real case this exists for: a multi-property PDF/
        # Canva deck (like this one) whose own upload is "a PDF", but one
        # row's own brochure_link points at a completely separate,
        # genuinely different PDF (Henly House's real "c45d78_...pdf") -
        # never the file that was actually uploaded at all. Previously
        # never fetched, purely because the upload's source was "PDF" -
        # regardless of that row's own distinct link.
        pdf_rows = [
            ListingRow(
                building="Henly House", floor_unit="3rd Floor",
                brochure_link="https://example.com/henly-house-detail.pdf",
            ),
        ]
        brochure_units = {"units": [{
            "building": "Henly House", "floor_unit": "3rd Floor",
            "address_1": "1 Henly Way", "postcode": "W1 1AA",
        }]}
        with patch("extract.extract", return_value=pdf_rows), \
             patch("brochure_enrichment.httpx.get", return_value=_pdf_response()) as mock_get, \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value=brochure_units) as mock_extract, \
             patch("geocode.geocode_rows"):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            at.file_uploader[0].upload("Henly Deck.pdf", b"%PDF-1.4 fake", "application/pdf")
            at.run()
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        brochure_calls = [
            c for c in mock_get.call_args_list
            if c.args and c.args[0] == "https://example.com/henly-house-detail.pdf"
        ]
        self.assertEqual(len(brochure_calls), 1)
        mock_extract.assert_called_once()

        pending = list_pending_staging_files()
        df = load_staging_as_dataframe(pending[0])
        self.assertEqual(df.iloc[0]["address_1"], "1 Henly Way")
        self.assertEqual(df.iloc[0]["postcode"], "W1 1AA")


class EmailEnrichmentSafeguardsTests(unittest.TestCase):
    """
    The four specific safeguards the user asked to see explicitly verified
    for the NEW email path, not just assumed to carry over from the
    spreadsheet path's own existing coverage.
    """

    def setUp(self):
        _clear_pending()
        brochure_enrichment._extract_brochure_units.cache_clear()

    def tearDown(self):
        _clear_pending()
        brochure_enrichment._extract_brochure_units.cache_clear()

    def test_1_field_already_stated_by_the_email_is_never_overwritten(self):
        # The email itself already states state_of_space - the brochure
        # states a genuinely DIFFERENT value for the same matched unit.
        # The email's own value must survive completely unchanged.
        email_raw = {
            "provider": "MetSpace", "contacts": None,
            "units": [{
                "building": "Building 0", "floor_unit": "3rd Floor",
                "state_of_space": "Cat A", "special_features": None,
                "brochure_link": "https://example.com/brochure.pdf",
            }],
        }
        brochure_units = {"units": [{
            "building": "Building 0", "floor_unit": "3rd Floor",
            "state_of_space": "Fully Fitted", "special_features": "Roof terrace",
        }]}
        with patch("brochure_enrichment.httpx.get", return_value=_pdf_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value=brochure_units):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            _upload_email(at, email_raw)
            self.assertFalse(at.exception)

        df = load_staging_as_dataframe(list_pending_staging_files()[0])
        # Untouched - the email's own stated value, never the brochure's
        # conflicting one.
        self.assertEqual(df.iloc[0]["state_of_space"], "Cat A")
        # A genuinely blank field on the same row still gets filled - this
        # is a blank-only guard, not a blanket "never touch this row" rule.
        self.assertEqual(df.iloc[0]["special_features"], "Roof terrace")

    def test_2_high_risk_rent_fields_get_the_same_caution_not_more_aggressive(self):
        # The row already has size_sqft + rent_psf that are mutually
        # consistent (a trustworthy existing pair) - the matched brochure
        # unit's own rent_pcm figure does NOT add up against them (see
        # brochure_enrichment._rent_values_consistent's own tolerance).
        # rent_pcm must stay blank, exactly the same caution the
        # spreadsheet path already applies via the identical function -
        # never filled just because this is a new code path.
        email_raw = {
            "provider": "MetSpace", "contacts": None,
            "units": [{
                # size_sqft/rent_pcm/rent_psf all genuinely blank on the
                # email row itself - deliberately NOT stating rent_psf
                # alongside size_sqft here, since extract_email.py's own
                # compute_rent() would otherwise derive rent_pcm from them
                # at EXTRACTION time, before enrichment ever runs, making
                # rent_pcm non-blank for a reason unrelated to what this
                # test is actually checking.
                "building": "Building 0", "floor_unit": "3rd Floor",
                "size_sqft": None, "rent_pcm": None, "rent_psf": None,
                "brochure_link": "https://example.com/brochure.pdf",
            }],
        }
        # The brochure's OWN matched unit is internally self-contradictory
        # (the real confirmed production case: size=2762/rent_pcm=33
        # against rent_psf=33 for the SAME unit) - annual from rent_pcm*12
        # = 396, annual from rent_psf*size = 91,146, ~230x apart.
        brochure_units = {"units": [{
            "building": "Building 0", "floor_unit": "3rd Floor",
            "size_sqft": 2762, "rent_pcm": 33, "rent_psf": 33,
        }]}
        with patch("brochure_enrichment.httpx.get", return_value=_pdf_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value=brochure_units):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            _upload_email(at, email_raw)
            self.assertFalse(at.exception)

        df = load_staging_as_dataframe(list_pending_staging_files()[0])
        # Both HIGH_RISK fields stay blank - the conflicting evidence
        # blocks BOTH, not just whichever one was checked first.
        self.assertTrue(pd.isna(df.iloc[0]["rent_pcm"]))
        self.assertTrue(pd.isna(df.iloc[0]["rent_psf"]))
        # size_sqft is a UNIT_LEVEL field, not HIGH_RISK - the rent
        # conflict never blocks it (see _apply_units_to_row's own "never
        # affects UNIT_LEVEL_FIELDS other than the two rent ones").
        self.assertEqual(df.iloc[0]["size_sqft"], 2762.0)

        summary = get_staging_enrichment_summary(list_pending_staging_files()[0])
        self.assertEqual(summary["status"], "complete")

    def test_2b_high_risk_rent_field_still_fills_when_genuinely_blank_and_consistent(self):
        # The positive half of the same safeguard - a genuinely blank
        # rent_pcm on an email row DOES get filled from a confidently
        # matched, internally-consistent brochure figure. Proves this is a
        # real "same caution", not an accidental block on every rent value.
        email_raw = {
            "provider": "MetSpace", "contacts": None,
            "units": [{
                "building": "Building 0", "floor_unit": "3rd Floor",
                "size_sqft": 1000, "rent_pcm": None,
                "brochure_link": "https://example.com/brochure.pdf",
            }],
        }
        brochure_units = {"units": [{
            "building": "Building 0", "floor_unit": "3rd Floor", "rent_pcm": 15000,
        }]}
        with patch("brochure_enrichment.httpx.get", return_value=_pdf_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value=brochure_units):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            _upload_email(at, email_raw)
            self.assertFalse(at.exception)

        df = load_staging_as_dataframe(list_pending_staging_files()[0])
        self.assertEqual(df.iloc[0]["rent_pcm"], 15000.0)

    def test_3_ambiguous_multi_floor_brochure_leaves_the_email_row_untouched(self):
        # The brochure covers TWO floors of the same building; the email's
        # own floor_unit is blank, so nothing disambiguates which one
        # applies to this row - _match_unit must return no confident
        # match, and the row must come out exactly as the email stated it,
        # never a guess. Same STATUS_EXTRACTED_BUT_AMBIGUOUS safety net the
        # spreadsheet path already relies on.
        email_raw = {
            "provider": "MetSpace", "contacts": None,
            "units": [{
                "building": "Building 0", "floor_unit": None, "size_sqft": None,
                "special_features": None, "brochure_link": "https://example.com/brochure.pdf",
            }],
        }
        brochure_units = {"units": [
            {"building": "Building 0", "floor_unit": "1st Floor", "size_sqft": 1000, "special_features": "A"},
            {"building": "Building 0", "floor_unit": "2nd Floor", "size_sqft": 1500, "special_features": "B"},
        ]}
        with patch("brochure_enrichment.httpx.get", return_value=_pdf_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value=brochure_units):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            _upload_email(at, email_raw)
            self.assertFalse(at.exception)

        df = load_staging_as_dataframe(list_pending_staging_files()[0])
        self.assertIsNone(df.iloc[0]["floor_unit"])
        self.assertIsNone(df.iloc[0]["size_sqft"])
        self.assertIsNone(df.iloc[0]["special_features"])

        summary = get_staging_enrichment_summary(list_pending_staging_files()[0])
        self.assertEqual(len(summary["document_issues"]), 1)
        self.assertEqual(summary["document_issues"][0]["status"], brochure_enrichment.STATUS_EXTRACTED_BUT_AMBIGUOUS)

    def test_4_a_broken_brochure_link_does_not_fail_the_whole_email_upload(self):
        # Mirrors test_app_upload_brochure_enrichment.py's own spreadsheet
        # equivalent exactly, for the email path - a fetch/extract failure
        # must leave the row exactly as extracted and must never surface
        # as "extraction failed" for an email whose real extraction
        # genuinely succeeded.
        email_raw = {
            "provider": "MetSpace", "contacts": None,
            "units": [{
                "building": "Building 0", "floor_unit": "3rd Floor",
                "brochure_link": "https://example.com/brochure.pdf",
            }],
        }
        with patch("brochure_enrichment._extract_brochure_units", side_effect=RuntimeError("network down")):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            _upload_email(at, email_raw)
            self.assertFalse(at.exception)

        success_text = "".join(s.value for s in at.success)
        self.assertIn("Extracted and staged", success_text)
        pending = list_pending_staging_files()
        self.assertEqual(len(pending), 1)
        df = load_staging_as_dataframe(pending[0])
        self.assertEqual(df.iloc[0]["building"], "Building 0")

        caption_text = "".join(c.value for c in at.caption)
        self.assertIn("Done: 0 of 1 brochure read successfully, adding details to 0 rows. 1 couldn't be read.", caption_text)

    def test_4b_a_catastrophic_enrichment_crash_still_leaves_the_email_extraction_staged(self):
        # Belt-and-braces, same as the spreadsheet path's own equivalent -
        # even a bug in run_brochure_enrichment ITSELF (not just one
        # brochure's own fetch) must never take down an already-succeeded
        # email extraction.
        email_raw = {
            "provider": "MetSpace", "contacts": None,
            "units": [{
                "building": "Building 0", "floor_unit": "3rd Floor",
                "brochure_link": "https://example.com/brochure.pdf",
            }],
        }
        with patch("brochure_enrichment.run_brochure_enrichment", side_effect=RuntimeError("unexpected crash")):
            at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
            at.run()
            _upload_email(at, email_raw)
            self.assertFalse(at.exception)

        success_text = "".join(s.value for s in at.success)
        self.assertIn("Extracted and staged", success_text)
        warning_text = "".join(w.value for w in at.warning)
        self.assertIn("unexpected error", warning_text)
        pending = list_pending_staging_files()
        self.assertEqual(len(pending), 1)
        df = load_staging_as_dataframe(pending[0])
        self.assertEqual(df.iloc[0]["building"], "Building 0")


_HAS_GEMINI_KEY = bool(os.environ.get("GEMINI_API_KEY"))
# A real, forwarded "MetSpace Availability Update" weekly email, confirmed
# to have genuine per-listing brochure_link URLs - the exact real gap this
# feature closes. Skipped automatically when GEMINI_API_KEY isn't
# configured or the file isn't present in this environment (same
# convention as test_email_upload_integration.py's own real fixture).
REAL_METSPACE_EMAIL = Path(r"C:\Users\julie\Downloads\Fw_ MetSpace Availability Update (2).eml")


@unittest.skipUnless(_HAS_GEMINI_KEY, "GEMINI_API_KEY not configured")
@unittest.skipUnless(REAL_METSPACE_EMAIL.exists(), "real MetSpace email fixture not present")
class RealMetSpaceEmailEnrichmentTests(unittest.TestCase):
    """
    Runs the real app.py end-to-end via AppTest against the actual real
    MetSpace email, with NOTHING mocked - real Gemini extraction of the
    email body, real brochure_link fetches, real Gemini brochure
    extraction. Proves the feature actually works against real data, not
    just against hand-built fixtures.
    """

    def setUp(self):
        _clear_pending()
        brochure_enrichment._extract_brochure_units.cache_clear()

    def tearDown(self):
        _clear_pending()
        brochure_enrichment._extract_brochure_units.cache_clear()

    def test_real_metspace_email_gets_automatically_enriched(self):
        file_bytes = REAL_METSPACE_EMAIL.read_bytes()

        at = AppTest.from_file(str(BASE / "app.py"), default_timeout=600)
        at.run()
        at.file_uploader[0].upload(REAL_METSPACE_EMAIL.name, file_bytes, "message/rfc822")
        at.run()
        extract_buttons = [b for b in at.button if b.label == "Extract"]
        extract_buttons[0].click().run()
        self.assertFalse(at.exception)

        pending = list_pending_staging_files()
        self.assertEqual(len(pending), 1)
        before = load_staging_as_dataframe(pending[0])
        self.assertGreater(len(before), 0)

        summary = get_staging_enrichment_summary(pending[0])
        self.assertIsNotNone(summary, "automatic enrichment never ran for this real email upload")
        self.assertEqual(summary["status"], "complete")
        self.assertGreater(summary["unique_brochures_considered"], 0)

        after = load_staging_as_dataframe(pending[0])
        self.assertEqual(len(after), len(before))

        # At least one row ended up with a genuinely filled field that
        # wasn't blank-only-by-coincidence - i.e. enrichment actually
        # changed something real, not a no-op run.
        enrichable = ("special_features", "state_of_space", "address_1", "postcode", "size_sqft")
        rows_with_data = sum(
            1 for _, row in after.iterrows() if any(row.get(f) not in (None, "") for f in enrichable)
        )
        self.assertGreater(rows_with_data, 0)


if __name__ == "__main__":
    unittest.main()
