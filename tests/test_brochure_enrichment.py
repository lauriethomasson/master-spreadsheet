"""
Regression tests for brochure_enrichment.py - secondary enrichment of
spreadsheet-extracted ListingRows from their own linked brochure. Never
calls the real network or the real Gemini API - httpx.get and extract.
extract_raw_units are mocked throughout (same principle as test_extract_
spreadsheet_gemini.py mocking call_gemini).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_brochure_enrichment -v
"""

import base64
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brochure_enrichment
from schema import ListingRow


def _brochure_units(units, property_features=None, contacts=None, building_features=None, provider=None):
    """A _BrochureUnits (see brochure_enrichment.py's own class) built from
    plain test data - the real shape _extract_brochure_units returns,
    used here so tests exercising the property/building-level fallbacks
    (which only ever read these as extra attributes, never as list
    elements) match what the real function actually hands to
    _apply_units_to_row/enrich_row/enrich_rows_grouped."""
    result = brochure_enrichment._BrochureUnits(units)
    result.property_features = property_features
    result.contacts = contacts
    result.building_features = building_features or []
    result.provider = provider
    return result


def _response(status_code=200, content=b"%PDF-1.4 fake pdf bytes", content_type="application/pdf"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    resp.headers = {"content-type": content_type}
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError("error", request=None, response=resp)
    else:
        resp.raise_for_status.side_effect = None
    return resp


class EnrichmentTestCase(unittest.TestCase):
    def setUp(self):
        brochure_enrichment._extract_brochure_units.cache_clear()

    def tearDown(self):
        brochure_enrichment._extract_brochure_units.cache_clear()


class TrimMemoryTests(unittest.TestCase):
    def test_no_op_on_non_linux(self):
        with patch("brochure_enrichment.platform.system", return_value="Windows"), \
             patch("brochure_enrichment.ctypes.CDLL") as mock_cdll:
            brochure_enrichment._trim_memory()

        mock_cdll.assert_not_called()

    def test_calls_malloc_trim_on_linux(self):
        mock_libc = MagicMock()
        with patch("brochure_enrichment.platform.system", return_value="Linux"), \
             patch("brochure_enrichment.ctypes.CDLL", return_value=mock_libc) as mock_cdll:
            brochure_enrichment._trim_memory()

        mock_cdll.assert_called_once_with("libc.so.6")
        mock_libc.malloc_trim.assert_called_once_with(0)

    def test_never_raises_even_if_the_symbol_is_missing(self):
        with patch("brochure_enrichment.platform.system", return_value="Linux"), \
             patch("brochure_enrichment.ctypes.CDLL", side_effect=OSError("not found")):
            brochure_enrichment._trim_memory()  # must not raise


class NeedsEnrichmentTests(EnrichmentTestCase):
    def test_blank_special_features_needs_enrichment(self):
        row = ListingRow(building="A", special_features=None, state_of_space="Cat A")
        self.assertTrue(brochure_enrichment.needs_enrichment(row))

    def test_blank_state_of_space_needs_enrichment(self):
        row = ListingRow(building="A", special_features="Nice", state_of_space=None)
        self.assertTrue(brochure_enrichment.needs_enrichment(row))

    def test_every_enrichable_field_populated_does_not_need_enrichment(self):
        row = ListingRow(
            building="A", address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
            floor_unit="1st", size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
            special_features="Nice", state_of_space="Cat A", contacts="Jane, jane@x.com",
        )
        self.assertFalse(brochure_enrichment.needs_enrichment(row))

    def test_blank_address_1_needs_enrichment(self):
        row = ListingRow(
            building="A", postcode="EC1A 1AA", submarket="City", floor_unit="1st", size_sqft=1000,
            desks_max=20, rent_pcm=5000, rent_psf=60, special_features="Nice", state_of_space="Cat A",
            contacts="Jane, jane@x.com", address_1=None,
        )
        self.assertTrue(brochure_enrichment.needs_enrichment(row))

    def test_blank_rent_fields_need_enrichment(self):
        row = ListingRow(
            building="A", address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
            floor_unit="1st", size_sqft=1000, desks_max=20, special_features="Nice", state_of_space="Cat A",
            contacts="Jane, jane@x.com", rent_pcm=None, rent_psf=None,
        )
        self.assertTrue(brochure_enrichment.needs_enrichment(row))

    def test_blank_contacts_needs_enrichment(self):
        row = ListingRow(building="A", special_features="Nice", state_of_space="Cat A", contacts=None)
        self.assertTrue(brochure_enrichment.needs_enrichment(row))


class EligibleBrochureUrlTests(unittest.TestCase):
    def test_direct_pdf_url_is_eligible(self):
        self.assertTrue(brochure_enrichment._is_eligible_brochure_url("https://example.com/brochure.pdf"))

    def test_blank_is_not_eligible(self):
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url(None))
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url(""))

    def test_non_url_text_is_not_eligible(self):
        # e.g. a literal "Coming Soon" placeholder instead of a real link.
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url("Coming Soon"))

    def test_floorplan_url_is_not_eligible(self):
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url("https://sharepoint.com/floorplans/a.pdf"))

    def test_combined_brochure_and_floorplan_url_is_still_eligible(self):
        # A genuine brochure whose own filename also mentions floor plans
        # (a real, common combined-document naming pattern) must remain
        # eligible for enrichment - never discarded merely because its
        # name contains the word "floorplan" too.
        self.assertTrue(
            brochure_enrichment._is_eligible_brochure_url("https://example.com/Building-Brochure-and-Floorplans.pdf"),
        )

    def test_youtube_url_is_not_eligible(self):
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url("https://youtube.com/watch?v=abc123"))
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url("https://youtu.be/abc123"))

    def test_generic_homepage_is_not_eligible(self):
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url("https://www.someprovider.co.uk"))

    def test_social_profile_is_not_eligible(self):
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url("https://www.linkedin.com/company/x"))

    def test_google_drive_folder_link_is_not_eligible(self):
        # Real Kitt's Availability shape - a folder has no single document
        # to fetch at all, just Google's own HTML folder-listing page, so
        # a fetch attempt is structurally doomed the same way a Canva
        # link's is.
        self.assertFalse(
            brochure_enrichment._is_eligible_brochure_url(
                "https://drive.google.com/drive/folders/1Q9q4PTgZcJvhTMW3rVAzND3rVCQ6b9ra?usp=drive_link",
            )
        )

    def test_google_drive_single_file_link_is_still_eligible(self):
        # The real, supported shape (drive.google.com/file/d/{id}/...)
        # must be completely unaffected by the new folder-link rejection.
        self.assertTrue(
            brochure_enrichment._is_eligible_brochure_url(
                "https://drive.google.com/file/d/1EPO_g2beNdCQKTgHqVIVlaIZPvSjbudJ/view?usp=sharing",
            )
        )


class EligibleFloorplanUrlTests(unittest.TestCase):
    """_is_eligible_floorplan_url - mirrors EligibleBrochureUrlTests above,
    for floorplan_link (see that function's own docstring for the one
    real difference: a floorplan-shaped URL is expected here, never
    rejected)."""

    def test_direct_pdf_url_is_eligible(self):
        self.assertTrue(brochure_enrichment._is_eligible_floorplan_url("https://example.com/floorplan.pdf"))

    def test_blank_is_not_eligible(self):
        self.assertFalse(brochure_enrichment._is_eligible_floorplan_url(None))
        self.assertFalse(brochure_enrichment._is_eligible_floorplan_url(""))

    def test_generic_homepage_is_not_eligible(self):
        self.assertFalse(brochure_enrichment._is_eligible_floorplan_url("https://www.someprovider.co.uk"))

    def test_youtube_url_is_not_eligible(self):
        self.assertFalse(brochure_enrichment._is_eligible_floorplan_url("https://youtube.com/watch?v=abc123"))

    def test_google_drive_folder_link_is_not_eligible(self):
        # The real, confirmed gap: 20+ distinct Google Drive folder links
        # used as floorplan_link across Kitt's Availability file's real
        # rows previously fell straight through to a real, structurally-
        # doomed fetch attempt (surfacing as a misleading "document
        # couldn't be opened") rather than the accurate "unsupported link
        # type" this now gives instead.
        self.assertFalse(
            brochure_enrichment._is_eligible_floorplan_url(
                "https://drive.google.com/drive/folders/1Q9q4PTgZcJvhTMW3rVAzND3rVCQ6b9ra?usp=drive_link",
            )
        )
        self.assertFalse(
            brochure_enrichment._is_eligible_floorplan_url(
                "https://drive.google.com/drive/u/0/folders/1P2Fwz5PCfJBu1tEc7ZJSPzLD-dtskanc",
            )
        )

    def test_google_drive_single_file_link_is_still_eligible(self):
        self.assertTrue(
            brochure_enrichment._is_eligible_floorplan_url(
                "https://drive.google.com/file/d/1UjN-Sdoz5H7AkEgf1RudQUsU-_Pu8Z2o/view?usp=drive_link",
            )
        )

    def test_floorplan_shaped_url_is_still_eligible(self):
        # Unlike brochure_link, a floorplan-shaped URL text is expected
        # here, never a rejection reason - must remain unaffected.
        self.assertTrue(brochure_enrichment._is_eligible_floorplan_url("https://app.box.com/s/my-floorplan"))


class GoogleDriveFolderLinkTests(unittest.TestCase):
    def test_bare_folder_link_matches(self):
        self.assertTrue(
            brochure_enrichment._is_google_drive_folder_link("https://drive.google.com/drive/folders/abc123"),
        )

    def test_folder_link_with_query_string_matches(self):
        self.assertTrue(
            brochure_enrichment._is_google_drive_folder_link(
                "https://drive.google.com/drive/folders/abc123?usp=drive_link",
            )
        )

    def test_signed_in_account_variant_matches(self):
        self.assertTrue(
            brochure_enrichment._is_google_drive_folder_link("https://drive.google.com/drive/u/0/folders/abc123"),
        )

    def test_case_insensitive(self):
        self.assertTrue(
            brochure_enrichment._is_google_drive_folder_link("HTTPS://DRIVE.GOOGLE.COM/drive/folders/abc123"),
        )

    def test_single_file_link_does_not_match(self):
        self.assertFalse(
            brochure_enrichment._is_google_drive_folder_link(
                "https://drive.google.com/file/d/abc123/view?usp=sharing",
            )
        )

    def test_blank_does_not_match(self):
        self.assertFalse(brochure_enrichment._is_google_drive_folder_link(None))
        self.assertFalse(brochure_enrichment._is_google_drive_folder_link(""))

    def test_unrelated_url_does_not_match(self):
        self.assertFalse(brochure_enrichment._is_google_drive_folder_link("https://example.com/brochure.pdf"))


class FetchAndExtractTests(EnrichmentTestCase):
    # render_pages/render_and_extract, not extract_raw_units - _extract_
    # brochure_units renders directly from the fetched bytes now (see its
    # own docstring on why: no temp file at all), so the fake PDF content
    # _response() returns is never actually valid enough for fitz to parse
    # as a real PDF - these mock the two split steps instead, exactly as
    # production code now calls them.
    def test_direct_pdf_url_skips_landing_page_resolution(self):
        with patch("brochure_enrichment.resolve_brochure_link") as mock_resolve, \
             patch("brochure_enrichment.httpx.get", return_value=_response()) as mock_get, \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value={"units": []}):
            brochure_enrichment._extract_brochure_units("https://example.com/brochure.pdf")

        mock_resolve.assert_not_called()
        mock_get.assert_called_once()

    def test_landing_page_is_resolved_before_fetching(self):
        with patch("brochure_enrichment.resolve_brochure_link", return_value="https://example.com/real.pdf") as mock_resolve, \
             patch("brochure_enrichment.httpx.get", return_value=_response()) as mock_get, \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value={"units": []}):
            brochure_enrichment._extract_brochure_units("https://example.com/preview")

        mock_resolve.assert_called_once_with("https://example.com/preview")
        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args[0][0], "https://example.com/real.pdf")

    def test_broken_url_returns_none_not_raise(self):
        with patch("brochure_enrichment.resolve_brochure_link", return_value="https://example.com/x.pdf"), \
             patch("brochure_enrichment.httpx.get", side_effect=httpx.ConnectError("dns failure")):
            result = brochure_enrichment._extract_brochure_units("https://example.com/x")

        self.assertIsNone(result)

    def test_timeout_returns_none_not_raise(self):
        with patch("brochure_enrichment.resolve_brochure_link", return_value="https://example.com/x.pdf"), \
             patch("brochure_enrichment.httpx.get", side_effect=httpx.TimeoutException("timed out")):
            result = brochure_enrichment._extract_brochure_units("https://example.com/x")

        self.assertIsNone(result)

    def test_non_pdf_response_returns_none(self):
        html_response = _response(content=b"<html>not a pdf</html>", content_type="text/html")
        with patch("brochure_enrichment.httpx.get", return_value=html_response):
            result = brochure_enrichment._extract_brochure_units("https://example.com/x.pdf")

        self.assertIsNone(result)

    def test_gemini_extraction_failure_returns_none(self):
        with patch("brochure_enrichment.httpx.get", return_value=_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", side_effect=RuntimeError("bad json")):
            result = brochure_enrichment._extract_brochure_units("https://example.com/x.pdf")

        self.assertIsNone(result)

    def test_a_render_failure_also_returns_none_not_raise(self):
        # A malformed/corrupt PDF - render_pages itself is what raises here,
        # never reaching render_and_extract at all.
        with patch("brochure_enrichment.httpx.get", return_value=_response()), \
             patch("brochure_enrichment.extract.render_pages", side_effect=RuntimeError("corrupt PDF")) as mock_render, \
             patch("brochure_enrichment.extract.render_and_extract") as mock_extract:
            result = brochure_enrichment._extract_brochure_units("https://example.com/x.pdf")

        self.assertIsNone(result)
        mock_render.assert_called_once()
        mock_extract.assert_not_called()

    def test_same_url_only_fetched_once(self):
        with patch("brochure_enrichment.httpx.get", return_value=_response()) as mock_get, \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value={"units": []}) as mock_extract:
            brochure_enrichment._extract_brochure_units("https://example.com/shared.pdf")
            brochure_enrichment._extract_brochure_units("https://example.com/shared.pdf")
            brochure_enrichment._extract_brochure_units("https://example.com/shared.pdf")

        mock_get.assert_called_once()
        mock_extract.assert_called_once()

    def test_raw_pdf_bytes_are_never_written_to_a_temp_file(self):
        # The whole point of rendering directly from bytes (see extract.
        # render_pages' own docstring) - confirms no tempfile.* call is
        # made anywhere in this path at all.
        import tempfile as tempfile_module

        with patch("brochure_enrichment.httpx.get", return_value=_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value={"units": []}), \
             patch.object(tempfile_module, "NamedTemporaryFile") as mock_tmp:
            brochure_enrichment._extract_brochure_units("https://example.com/brochure.pdf")

        mock_tmp.assert_not_called()


class ExtractRowsFromLinkTests(EnrichmentTestCase):
    """extract_rows_from_link - the "paste a document link directly"
    upload path's own conversion step (see app.py): every unit
    _extract_brochure_units finds becomes its own ListingRow, with every
    field the extraction actually found carried straight through."""

    def test_multi_building_document_produces_one_row_per_unit_with_every_field_intact(self):
        # The real scenario this feature exists for: one document covering
        # several UNRELATED properties - every unit becomes its own row,
        # each with its own building, not collapsed or cross-contaminated.
        units = _brochure_units(
            [
                {
                    "building": "28 Lime Street", "address_1": "28 Lime Street", "postcode": "EC3M 7HD",
                    "submarket": "City", "floor_unit": "4th Floor", "size_sqft": 1915.0,
                    "desks_min": 18, "desks_max": 24, "rent_pcm": 23200.0, "rent_psf": None,
                    "special_features": "Bike racks; showers", "state_of_space": "CAT A",
                },
                {
                    "building": "40 Fenchurch Street", "address_1": "40 Fenchurch Street", "postcode": "EC3M 4DT",
                    "submarket": "Aldgate", "floor_unit": "2nd Floor", "size_sqft": 2200.0,
                    "desks_min": 20, "desks_max": 28, "rent_pcm": None, "rent_psf": 65.0,
                    "special_features": "Roof terrace", "state_of_space": "Fitted",
                },
                {
                    "building": "160 Blackfriars Road", "address_1": "160 Blackfriars Road", "postcode": "SE1 8EZ",
                    "submarket": "Southwark", "floor_unit": "3rd Floor", "size_sqft": 11785.0,
                    "desks_min": None, "desks_max": 103, "rent_pcm": 137492.0, "rent_psf": 140.0,
                    "special_features": "Typical floor", "state_of_space": "CAT A",
                },
            ],
            property_features="WiredScore Platinum", contacts="Jane Smith, jane@agent.com",
            provider="Test Agency",
        )

        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            rows = brochure_enrichment.extract_rows_from_link("https://example.com/schedule.pdf")

        self.assertEqual(len(rows), 3)
        by_building = {r.building: r for r in rows}
        self.assertEqual(set(by_building), {"28 Lime Street", "40 Fenchurch Street", "160 Blackfriars Road"})

        lime = by_building["28 Lime Street"]
        self.assertEqual(lime.address_1, "28 Lime Street")
        self.assertEqual(lime.postcode, "EC3M 7HD")
        self.assertEqual(lime.submarket, "City")
        self.assertEqual(lime.floor_unit, "4th Floor")
        self.assertEqual(lime.size_sqft, 1915.0)
        self.assertEqual(lime.desks_min, 18)
        self.assertEqual(lime.desks_max, 24)
        self.assertEqual(lime.rent_pcm, 23200.0)
        self.assertEqual(lime.special_features, "Bike racks; showers")
        self.assertEqual(lime.state_of_space, "CAT A")

        fenchurch = by_building["40 Fenchurch Street"]
        self.assertEqual(fenchurch.rent_psf, 65.0)
        self.assertEqual(fenchurch.state_of_space, "Fitted")

        # provider/contacts are document-level, not per-unit - every row
        # gets the SAME value, exactly like extract.py's own PDF-upload
        # path already does for a document with no explicit per-unit
        # source of either.
        for r in rows:
            self.assertEqual(r.provider, "Test Agency")
            self.assertEqual(r.internal_ref, "Test Agency")
            self.assertEqual(r.contacts, "Jane Smith, jane@agent.com")

    def test_brochure_link_is_the_pasted_url_verbatim_for_every_row_never_resolved(self):
        units = _brochure_units([
            {"building": "A", "floor_unit": "1st"},
            {"building": "B", "floor_unit": "2nd"},
        ])

        with patch("brochure_enrichment._extract_brochure_units", return_value=units), \
             patch("brochure_enrichment.resolve_brochure_link") as mock_resolve:
            rows = brochure_enrichment.extract_rows_from_link("https://example.com/schedule.pdf")

        mock_resolve.assert_not_called()
        for r in rows:
            self.assertEqual(r.brochure_link, "https://example.com/schedule.pdf")

    def test_provider_is_blank_when_the_document_does_not_state_one(self):
        units = _brochure_units([{"building": "A", "floor_unit": "1st"}])  # provider left at its own default (None)

        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            rows = brochure_enrichment.extract_rows_from_link("https://example.com/schedule.pdf")

        self.assertIsNone(rows[0].provider)

    def test_a_unit_with_no_building_inherits_the_previous_units_building(self):
        units = _brochure_units([
            {"building": "28 Lime Street", "floor_unit": "4th Floor"},
            {"building": None, "floor_unit": "5th Floor"},
        ])

        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            rows = brochure_enrichment.extract_rows_from_link("https://example.com/schedule.pdf")

        self.assertEqual([r.building for r in rows], ["28 Lime Street", "28 Lime Street"])

    def test_a_leading_unit_with_no_building_and_nothing_to_inherit_is_skipped(self):
        units = _brochure_units([
            {"building": None, "floor_unit": "Ground Floor"},
            {"building": "28 Lime Street", "floor_unit": "4th Floor"},
        ])

        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            rows = brochure_enrichment.extract_rows_from_link("https://example.com/schedule.pdf")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].building, "28 Lime Street")

    def test_no_units_at_all_returns_an_empty_list_not_none_or_raise(self):
        with patch("brochure_enrichment._extract_brochure_units", return_value=None):
            rows = brochure_enrichment.extract_rows_from_link("https://example.com/schedule.pdf")

        self.assertEqual(rows, [])

    def test_source_file_defaults_to_the_url_when_no_label_is_given(self):
        units = _brochure_units([{"building": "A", "floor_unit": "1st"}])

        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            rows = brochure_enrichment.extract_rows_from_link("https://example.com/schedule.pdf")

        self.assertEqual(rows[0].source_file, "https://example.com/schedule.pdf")

    def test_source_file_uses_the_given_label_when_provided(self):
        units = _brochure_units([{"building": "A", "floor_unit": "1st"}])

        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            rows = brochure_enrichment.extract_rows_from_link(
                "https://example.com/schedule.pdf", source_label="Pasted link: schedule.pdf",
            )

        self.assertEqual(rows[0].source_file, "Pasted link: schedule.pdf")


def _box_share_html(shared_name="abc123", extension="pdf", can_download=True, name="16 Dufour's Place Brochure.pdf"):
    # A minimal excerpt of the real Box.postStreamData JSON blob Box's own
    # share page embeds in its server-rendered HTML - confirmed against 6
    # real UNION brochure links (see brochure_enrichment._fetch_box_shared_
    # pdf's own docstring). Only the exact substrings the regexes look for
    # matter; everything else is deliberately just filler.
    return (
        "<html><script>Box.postStreamData = {"
        f'"sharedName":"{shared_name}","extension":"{extension}",'
        f'"canDownload":{"true" if can_download else "false"},"name":"{name}"'
        "};</script></html>"
    )


def _html_response(html: str):
    resp = MagicMock()
    resp.status_code = 200
    resp.text = html
    resp.raise_for_status.side_effect = None
    return resp


class BoxShareTokenTests(unittest.TestCase):
    def test_plain_box_share_url_matches(self):
        self.assertEqual(brochure_enrichment._box_share_token("https://app.box.com/s/abc123"), "abc123")

    def test_bare_box_com_without_app_subdomain_matches(self):
        self.assertEqual(brochure_enrichment._box_share_token("https://box.com/s/abc123"), "abc123")

    def test_trailing_slash_and_query_string_tolerated(self):
        self.assertEqual(brochure_enrichment._box_share_token("https://app.box.com/s/abc123/?utm=x"), "abc123")

    def test_non_box_url_does_not_match(self):
        self.assertIsNone(brochure_enrichment._box_share_token("https://example.com/brochure.pdf"))

    def test_box_folder_url_shape_does_not_match(self):
        # Only the "/s/{token}" shared-FILE-link shape is handled - anything
        # else (a folder link, a different Box URL shape) is left alone.
        self.assertIsNone(brochure_enrichment._box_share_token("https://app.box.com/folder/123456"))


class FetchBoxSharedPdfTests(EnrichmentTestCase):
    def test_successful_box_fetch_returns_pdf_bytes(self):
        share_html = _box_share_html(shared_name="whntw3tqip6cnjeu88d055o3rfbdr019", extension="pdf")
        with patch(
            "brochure_enrichment.httpx.get",
            side_effect=[_html_response(share_html), _response(content=b"%PDF-1.4 real bytes")],
        ) as mock_get:
            result = brochure_enrichment._fetch_box_shared_pdf("https://app.box.com/s/whntw3tqip6cnjeu88d055o3rfbdr019")

        self.assertEqual(result, b"%PDF-1.4 real bytes")
        self.assertEqual(mock_get.call_count, 2)
        static_url_called = mock_get.call_args_list[1].args[0]
        self.assertEqual(static_url_called, "https://app.box.com/shared/static/whntw3tqip6cnjeu88d055o3rfbdr019.pdf")

    def test_downloads_disabled_returns_none_without_a_second_fetch(self):
        share_html = _box_share_html(can_download=False)
        with patch("brochure_enrichment.httpx.get", return_value=_html_response(share_html)) as mock_get:
            result = brochure_enrichment._fetch_box_shared_pdf("https://app.box.com/s/abc123")

        self.assertIsNone(result)
        mock_get.assert_called_once()

    def test_non_pdf_extension_returns_none_without_a_second_fetch(self):
        share_html = _box_share_html(extension="docx")
        with patch("brochure_enrichment.httpx.get", return_value=_html_response(share_html)) as mock_get:
            result = brochure_enrichment._fetch_box_shared_pdf("https://app.box.com/s/abc123")

        self.assertIsNone(result)
        mock_get.assert_called_once()

    def test_png_extension_rejected_by_default_brochure_fetch(self):
        # A floor plan is routinely delivered as a scanned/exported image
        # rather than a PDF (two real UNION examples confirmed directly) -
        # but the BROCHURE fetch path (accept_image_formats defaults to
        # False) must stay PDF-only; 0 of 9 real brochure documents traced
        # were images.
        share_html = _box_share_html(extension="png")
        with patch("brochure_enrichment.httpx.get", return_value=_html_response(share_html)) as mock_get:
            result = brochure_enrichment._fetch_box_shared_pdf("https://app.box.com/s/abc123")

        self.assertIsNone(result)
        mock_get.assert_called_once()

    def test_png_extension_accepted_when_fetching_a_floorplan(self):
        share_html = _box_share_html(extension="png", name="7th Floor - Fitted.png")
        png_response = _response(content=b"\x89PNG\r\n\x1a\nfake png bytes", content_type="image/png")
        with patch(
            "brochure_enrichment.httpx.get", side_effect=[_html_response(share_html), png_response],
        ):
            result = brochure_enrichment._fetch_box_shared_pdf(
                "https://app.box.com/s/abc123", reject_floorplan_filename=False, accept_image_formats=True,
            )

        self.assertEqual(result, b"\x89PNG\r\n\x1a\nfake png bytes")

    def test_jpg_extension_accepted_when_fetching_a_floorplan(self):
        share_html = _box_share_html(extension="jpg")
        jpg_response = _response(content=b"\xff\xd8\xfffake jpeg bytes", content_type="image/jpeg")
        with patch(
            "brochure_enrichment.httpx.get", side_effect=[_html_response(share_html), jpg_response],
        ):
            result = brochure_enrichment._fetch_box_shared_pdf(
                "https://app.box.com/s/abc123", reject_floorplan_filename=False, accept_image_formats=True,
            )

        self.assertEqual(result, b"\xff\xd8\xfffake jpeg bytes")

    def test_docx_extension_still_rejected_even_when_fetching_a_floorplan(self):
        # accept_image_formats never opens the floodgates to any non-PDF
        # content - only the two specific raster formats a floor plan is
        # actually confirmed to be delivered as.
        share_html = _box_share_html(extension="docx")
        with patch("brochure_enrichment.httpx.get", return_value=_html_response(share_html)) as mock_get:
            result = brochure_enrichment._fetch_box_shared_pdf(
                "https://app.box.com/s/abc123", accept_image_formats=True,
            )

        self.assertIsNone(result)
        mock_get.assert_called_once()

    def test_floorplan_named_file_is_rejected(self):
        # The real, confirmed case: a UNION row's own "Brochure" column
        # pointed at a Box file literally named "3rd floor - Grosvenor
        # Street - PLAN #1.pdf" - the provider's own spreadsheet mislabeled
        # a floor plan as its brochure link.
        share_html = _box_share_html(name="3rd floor - Grosvenor Street - PLAN #1.pdf")
        with patch("brochure_enrichment.httpx.get", return_value=_html_response(share_html)) as mock_get:
            result = brochure_enrichment._fetch_box_shared_pdf("https://app.box.com/s/abc123")

        self.assertIsNone(result)
        mock_get.assert_called_once()

    def test_genuine_brochure_filename_is_not_rejected(self):
        share_html = _box_share_html(name="16 Dufour's Place Brochure.pdf")
        with patch(
            "brochure_enrichment.httpx.get", side_effect=[_html_response(share_html), _response()],
        ):
            result = brochure_enrichment._fetch_box_shared_pdf("https://app.box.com/s/abc123")

        self.assertIsNotNone(result)

    def test_missing_metadata_returns_none(self):
        with patch("brochure_enrichment.httpx.get", return_value=_html_response("<html>no box data here</html>")) as mock_get:
            result = brochure_enrichment._fetch_box_shared_pdf("https://app.box.com/s/abc123")

        self.assertIsNone(result)
        mock_get.assert_called_once()

    def test_share_page_fetch_failure_returns_none(self):
        with patch("brochure_enrichment.httpx.get", side_effect=httpx.ConnectError("dns failure")):
            result = brochure_enrichment._fetch_box_shared_pdf("https://app.box.com/s/abc123")

        self.assertIsNone(result)

    def test_static_download_failure_returns_none(self):
        share_html = _box_share_html()
        with patch(
            "brochure_enrichment.httpx.get",
            side_effect=[_html_response(share_html), httpx.TimeoutException("timed out")],
        ):
            result = brochure_enrichment._fetch_box_shared_pdf("https://app.box.com/s/abc123")

        self.assertIsNone(result)

    def test_static_download_non_pdf_content_returns_none(self):
        share_html = _box_share_html()
        non_pdf_response = _response(content=b"<html>error page</html>", content_type="text/html")
        with patch(
            "brochure_enrichment.httpx.get", side_effect=[_html_response(share_html), non_pdf_response],
        ):
            result = brochure_enrichment._fetch_box_shared_pdf("https://app.box.com/s/abc123")

        self.assertIsNone(result)

    def test_fetch_pdf_bytes_routes_box_urls_through_the_box_specific_path(self):
        share_html = _box_share_html(shared_name="abc123", extension="pdf")
        with patch(
            "brochure_enrichment.httpx.get", side_effect=[_html_response(share_html), _response()],
        ) as mock_get, patch("brochure_enrichment.resolve_brochure_link") as mock_resolve:
            result = brochure_enrichment._fetch_pdf_bytes("https://app.box.com/s/abc123")

        self.assertIsNotNone(result)
        mock_resolve.assert_not_called()
        self.assertEqual(mock_get.call_count, 2)

    def test_fetch_pdf_bytes_non_box_url_unaffected(self):
        with patch("brochure_enrichment.httpx.get", return_value=_response()) as mock_get:
            result = brochure_enrichment._fetch_pdf_bytes("https://example.com/brochure.pdf")

        self.assertIsNotNone(result)
        mock_get.assert_called_once()

    def test_fetch_pdf_bytes_generic_path_rejects_image_content_by_default(self):
        image_response = _response(content=b"\x89PNG\r\n\x1a\nfake png", content_type="image/png")
        with patch("brochure_enrichment.httpx.get", return_value=image_response):
            result = brochure_enrichment._fetch_pdf_bytes("https://example.com/floorplan.png")

        self.assertIsNone(result)

    def test_fetch_pdf_bytes_generic_path_accepts_image_content_for_a_floorplan(self):
        image_response = _response(content=b"\x89PNG\r\n\x1a\nfake png", content_type="image/png")
        with patch("brochure_enrichment.httpx.get", return_value=image_response) as mock_get:
            result = brochure_enrichment._fetch_pdf_bytes(
                "https://example.com/floorplan.png", accept_image_formats=True,
            )

        self.assertEqual(result, b"\x89PNG\r\n\x1a\nfake png")
        # A direct .png URL is treated as already-direct, same as .pdf -
        # resolve_brochure_link's own one-hop landing-page resolution is
        # skipped, confirmed by there being exactly one httpx.get call.
        mock_get.assert_called_once_with(
            "https://example.com/floorplan.png", timeout=brochure_enrichment.REQUEST_TIMEOUT,
            headers={"User-Agent": brochure_enrichment.USER_AGENT}, follow_redirects=True,
        )

    def test_fetch_pdf_bytes_generic_path_still_rejects_docx_for_a_floorplan(self):
        docx_response = _response(
            content=b"not an image or pdf",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        with patch("brochure_enrichment.httpx.get", return_value=docx_response):
            result = brochure_enrichment._fetch_pdf_bytes(
                "https://example.com/floorplan.docx", accept_image_formats=True,
            )

        self.assertIsNone(result)


class DropboxAndGoogleDriveShareUrlTests(unittest.TestCase):
    def test_dropbox_s_share_url_matches(self):
        self.assertTrue(brochure_enrichment._is_dropbox_share_url("https://www.dropbox.com/s/abc123/Brochure.pdf?dl=0"))

    def test_dropbox_scl_fi_share_url_matches(self):
        self.assertTrue(
            brochure_enrichment._is_dropbox_share_url(
                "https://www.dropbox.com/scl/fi/abc123/Brochure.pdf?rlkey=xyz&dl=0"
            )
        )

    def test_bare_dropbox_com_without_www_matches(self):
        self.assertTrue(brochure_enrichment._is_dropbox_share_url("https://dropbox.com/s/abc123/Brochure.pdf"))

    def test_dropbox_non_share_path_does_not_match(self):
        self.assertFalse(brochure_enrichment._is_dropbox_share_url("https://www.dropbox.com/home"))

    def test_non_dropbox_url_does_not_match(self):
        self.assertFalse(brochure_enrichment._is_dropbox_share_url("https://example.com/s/abc123"))

    def test_dropbox_direct_download_url_forces_dl_1(self):
        result = brochure_enrichment._dropbox_direct_download_url(
            "https://www.dropbox.com/s/abc123/Brochure.pdf?dl=0"
        )
        self.assertEqual(result, "https://www.dropbox.com/s/abc123/Brochure.pdf?dl=1")

    def test_dropbox_direct_download_url_adds_dl_when_missing(self):
        result = brochure_enrichment._dropbox_direct_download_url("https://www.dropbox.com/s/abc123/Brochure.pdf")
        self.assertEqual(result, "https://www.dropbox.com/s/abc123/Brochure.pdf?dl=1")

    def test_google_drive_file_id_extracted_from_view_link(self):
        result = brochure_enrichment._google_drive_file_id(
            "https://drive.google.com/file/d/1AbCdEfGh_IJK-lmno/view?usp=sharing"
        )
        self.assertEqual(result, "1AbCdEfGh_IJK-lmno")

    def test_non_drive_url_returns_none(self):
        self.assertIsNone(
            brochure_enrichment._google_drive_file_id("https://example.com/file/d/1AbCdEfGh_IJK-lmno/view")
        )

    def test_drive_url_of_a_different_shape_returns_none(self):
        # docs.google.com (Sheets/Docs), or drive.google.com/drive/folders/...
        # are deliberately NOT handled - only the common "share a file"
        # /file/d/{id}/ shape, to stay small and avoid guessing at shapes
        # never confirmed against a real example.
        self.assertIsNone(brochure_enrichment._google_drive_file_id("https://drive.google.com/drive/folders/abc123"))


class FetchPdfBytesDropboxAndGoogleDriveTests(EnrichmentTestCase):
    def test_dropbox_share_link_is_fetched_via_its_direct_download_url(self):
        with patch("brochure_enrichment.httpx.get", return_value=_response()) as mock_get, \
                patch("brochure_enrichment.resolve_brochure_link") as mock_resolve:
            result = brochure_enrichment._fetch_pdf_bytes("https://www.dropbox.com/s/abc123/Brochure.pdf?dl=0")

        self.assertEqual(result, b"%PDF-1.4 fake pdf bytes")
        mock_resolve.assert_not_called()
        mock_get.assert_called_once_with(
            "https://www.dropbox.com/s/abc123/Brochure.pdf?dl=1", timeout=brochure_enrichment.REQUEST_TIMEOUT,
            headers={"User-Agent": brochure_enrichment.USER_AGENT}, follow_redirects=True,
        )

    def test_google_drive_share_link_is_fetched_via_its_direct_download_url(self):
        with patch("brochure_enrichment.httpx.get", return_value=_response()) as mock_get, \
                patch("brochure_enrichment.resolve_brochure_link") as mock_resolve:
            result = brochure_enrichment._fetch_pdf_bytes(
                "https://drive.google.com/file/d/1AbCdEfGh_IJK-lmno/view?usp=sharing"
            )

        self.assertEqual(result, b"%PDF-1.4 fake pdf bytes")
        mock_resolve.assert_not_called()
        mock_get.assert_called_once_with(
            "https://drive.google.com/uc?export=download&id=1AbCdEfGh_IJK-lmno",
            timeout=brochure_enrichment.REQUEST_TIMEOUT,
            headers={"User-Agent": brochure_enrichment.USER_AGENT}, follow_redirects=True,
        )

    def test_google_drive_large_file_virus_scan_interstitial_fails_safe(self):
        # A file that triggers Drive's "can't scan this file for viruses"
        # confirmation page returns HTML, not the PDF - this must be
        # treated exactly like any other unreadable source (None), never
        # mis-extracted from the interstitial's own HTML content.
        interstitial = _response(
            content=b"<html>Google Drive can't scan this file for viruses...</html>", content_type="text/html",
        )
        with patch("brochure_enrichment.httpx.get", return_value=interstitial):
            result = brochure_enrichment._fetch_pdf_bytes(
                "https://drive.google.com/file/d/1AbCdEfGh_IJK-lmno/view?usp=sharing"
            )

        self.assertIsNone(result)

    def test_dropbox_fetch_failure_returns_none_and_is_recorded(self):
        with patch("brochure_enrichment.httpx.get", side_effect=httpx.ConnectError("dns failure")):
            with brochure_enrichment._StatusCapture({}) as sink:
                result = brochure_enrichment._fetch_pdf_bytes("https://www.dropbox.com/s/abc123/Brochure.pdf?dl=0")

        self.assertIsNone(result)
        self.assertEqual(sink["status"], brochure_enrichment.STATUS_FETCH_FAILED)

    def test_a_failed_dropbox_document_does_not_prevent_a_later_box_document_from_succeeding(self):
        # One resolver failing must never affect an unrelated document
        # handled by a different resolver in the same run.
        with patch("brochure_enrichment.httpx.get", side_effect=httpx.ConnectError("dns failure")):
            dropbox_result = brochure_enrichment._fetch_pdf_bytes(
                "https://www.dropbox.com/s/abc123/Brochure.pdf?dl=0"
            )
        self.assertIsNone(dropbox_result)

        share_html = _box_share_html(shared_name="abc123", extension="pdf")
        with patch(
            "brochure_enrichment.httpx.get", side_effect=[_html_response(share_html), _response()],
        ):
            box_result = brochure_enrichment._fetch_pdf_bytes("https://app.box.com/s/abc123")
        self.assertIsNotNone(box_result)

    def test_canva_view_link_is_still_never_attempted_as_dropbox_or_drive(self):
        # Regression guard for commit a67e337's Canva-unsupported behavior -
        # a Canva view link must stay rejected pre-fetch by classify_link_
        # eligibility/the _is_eligible_* checks, never reach _fetch_pdf_
        # bytes's own resolver dispatch at all. Confirmed here at the
        # eligibility layer, since a Canva URL's own shape would never
        # match the Dropbox/Drive checks anyway - this only guards against
        # a future change accidentally routing it through _fetch_pdf_bytes.
        canva_url = "https://www.canva.com/design/DAGzsWW-Yp8/s8tPVTQe6HUQa939xX0XQw/view"
        self.assertEqual(
            brochure_enrichment.classify_link_eligibility(canva_url),
            brochure_enrichment.STATUS_UNSUPPORTED_LINK_TYPE,
        )
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url(canva_url))


_CANVA_URL = "https://www.canva.com/design/DAGzsWW-Yp8/s8tPVTQe6HUQa939xX0XQw/view"


class CanvaRendererConfiguredEligibilityTests(EnrichmentTestCase):
    """
    With CANVA_RENDERER_URL configured, a Canva view link becomes eligible
    again - the opposite of CanvaViewLinkRejectedTests' own unconfigured-
    environment behavior above (every existing test, including that one,
    runs with no such env var set, so none of them are affected by this).
    """

    def test_canva_becomes_eligible_when_renderer_is_configured(self):
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}):
            self.assertIsNone(brochure_enrichment.classify_link_eligibility(_CANVA_URL))
            self.assertTrue(brochure_enrichment._is_eligible_brochure_url(_CANVA_URL))
            self.assertTrue(brochure_enrichment._is_eligible_floorplan_url(_CANVA_URL))

    def test_canva_stays_ineligible_without_the_env_var(self):
        self.assertEqual(
            brochure_enrichment.classify_link_eligibility(_CANVA_URL), brochure_enrichment.STATUS_UNSUPPORTED_LINK_TYPE,
        )
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url(_CANVA_URL))


def _canva_pages_response(pages, page_count_detected=None, status_code=200):
    """A MagicMock httpx.Response shaped like the renderer's own new JSON
    multi-page format (see canva_renderer/app.py's Handler.do_POST) -
    {"pages": [base64 PNG, ...], "page_count_detected": N|None}."""
    payload = {
        "pages": [base64.b64encode(p).decode("ascii") for p in pages],
        "page_count_detected": page_count_detected,
    }
    return MagicMock(
        status_code=status_code, headers={"content-type": "application/json"}, json=MagicMock(return_value=payload),
    )


class FetchCanvaRenderedPageTests(EnrichmentTestCase):
    def test_successful_render_returns_png_bytes(self):
        response = _canva_pages_response([b"\x89PNG real bytes"], page_count_detected=1)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response) as mock_post, \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            result = brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        self.assertEqual(result, [b"\x89PNG real bytes"])
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.args[0], "https://canva-renderer.example.run.app/render")
        self.assertEqual(mock_post.call_args.kwargs["json"], {"url": _CANVA_URL})

    def test_successful_render_with_multiple_pages_preserves_order(self):
        pages = [b"\x89PNG p1", b"\x89PNG p2", b"\x89PNG p3"]
        response = _canva_pages_response(pages, page_count_detected=3)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            result = brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        self.assertEqual(result, pages)

    def test_response_with_more_pages_than_the_main_apps_own_cap_is_truncated(self):
        # Defense-in-depth: this app never trusts the (separately deployed,
        # separately versioned) renderer's own MAX_CANVA_PAGES cap at face
        # value - a response claiming more is simply truncated here too.
        pages = [f"\x89PNG p{i}".encode() for i in range(1, 30)]
        response = _canva_pages_response(pages, page_count_detected=29)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}), \
                patch.object(brochure_enrichment, "_CANVA_MAX_PAGES_ACCEPTED", 5):
            result = brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        self.assertEqual(len(result), 5)
        self.assertEqual(result, pages[:5])

    def test_malformed_pages_payload_returns_none_and_records_render_failed(self):
        response = MagicMock(
            status_code=200, headers={"content-type": "application/json"},
            json=MagicMock(return_value={"not_pages": []}),
        )
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            with brochure_enrichment._StatusCapture({}) as sink:
                result = brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        self.assertIsNone(result)
        self.assertEqual(sink["status"], brochure_enrichment.STATUS_RENDER_FAILED)

    def test_successful_render_logs_a_single_unambiguous_success_line(self):
        # The ONE clear, greppable Cloud Run log line confirming the whole
        # authenticated round trip worked - distinct from every failure
        # message (renderer unreachable/auth failed/render failed), so an
        # operator can tell "rendering itself worked" apart from "Gemini
        # then found nothing useful" without ambiguity.
        response = _canva_pages_response([b"\x89PNG real bytes"], page_count_detected=1)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}), \
                patch("brochure_enrichment.sys.stderr") as mock_stderr:
            brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        logged = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        self.assertIn("Canva render succeeded", logged)

    def test_renderer_unreachable_returns_none_and_records_fetch_failed(self):
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", side_effect=httpx.ConnectError("dns failure")) as mock_post:
            with brochure_enrichment._StatusCapture({}) as sink:
                result = brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        self.assertIsNone(result)
        self.assertEqual(sink["status"], brochure_enrichment.STATUS_FETCH_FAILED)
        # A raw connection-level exception is NEVER retried (unlike an
        # actual transient 502/503 HTTP response - see
        # TransientRendererRetryTests) - it already burned a full
        # _CANVA_RENDERER_TIMEOUT once, so retrying it blindly would only
        # add latency for a URL very unlikely to succeed on retry anyway.
        mock_post.assert_called_once()

    def test_renderer_reports_safe_failure_returns_none_and_records_render_failed(self):
        response = MagicMock(
            status_code=422, headers={"content-type": "application/json"},
            json=MagicMock(return_value={"error": "render_failed", "reason": "private design"}),
        )
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response):
            with brochure_enrichment._StatusCapture({}) as sink:
                result = brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        self.assertIsNone(result)
        self.assertEqual(sink["status"], brochure_enrichment.STATUS_RENDER_FAILED)
        self.assertIn("private design", sink["detail"])

    def test_renderer_failure_reason_is_logged_instead_of_bare_http_status(self):
        # Regression guard for the real production diagnostic gap: this app
        # previously only ever logged "HTTP 500"/"HTTP 422" for a renderer
        # failure, hiding whatever the renderer itself actually saw (a
        # browser launch failure, a navigation timeout, ...). The renderer
        # now always includes a "reason" (see canva_renderer/app.py's
        # _safe_reason), and this app must surface it, not just the code.
        # 500, not one of _CANVA_RENDERER_TRANSIENT_STATUS_CODES (502/503/
        # 504) - this test is about reason-surfacing for a NON-transient
        # failure specifically; see TransientRendererRetryTests for the
        # transient-504-style retry behavior itself.
        response = MagicMock(
            status_code=500, headers={"content-type": "application/json"},
            json=MagicMock(return_value={"error": "render_failed", "reason": "browser launch failed"}),
        )
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment.sys.stderr") as mock_stderr:
            result = brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        self.assertIsNone(result)
        logged = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        self.assertIn("Canva renderer failed for", logged)
        self.assertIn("browser launch failed", logged)
        self.assertNotIn("HTTP 500", logged)

    def test_renderer_422_reason_is_also_logged_by_name(self):
        response = MagicMock(
            status_code=422, headers={"content-type": "application/json"},
            json=MagicMock(return_value={"error": "render_failed", "reason": "page navigation timed out"}),
        )
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment.sys.stderr") as mock_stderr:
            brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        logged = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        self.assertIn("page navigation timed out", logged)

    def test_long_or_secret_bearing_reason_is_truncated_before_logging(self):
        # Defense-in-depth on this app's OWN side - never assumes the
        # renderer (a separately deployed, separately versioned service)
        # actually enforced its own reason-safety guarantees.
        huge_reason = "leaked-token-abc123 " + ("x" * 5000)
        response = MagicMock(
            status_code=500, headers={"content-type": "application/json"},
            json=MagicMock(return_value={"error": "internal_error", "reason": huge_reason}),
        )
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment.sys.stderr") as mock_stderr:
            brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        logged = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        # The exact secret-shaped substring may still appear (this app has
        # no way to recognize an arbitrary token shape it wasn't told
        # about) - what's actually guaranteed is boundedness: the log line
        # is never allowed to balloon to the full 5000+ char raw reason.
        self.assertLess(len(logged), 2000)

    def test_401_from_renderer_is_distinguished_as_an_auth_failure(self):
        # Real, confirmed diagnostic gap this closes: a 401/403 from the
        # renderer (Cloud Run's own IAM check rejecting the call BEFORE it
        # ever reaches the renderer's own code) previously looked
        # identical to "the renderer just couldn't render this page" -
        # this is distinguished so a real production auth problem (a
        # missing Cloud Run Invoker binding, a wrong audience) is never
        # masked as a generic render failure.
        response = MagicMock(status_code=401, headers={"content-type": "text/html"})
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response):
            with brochure_enrichment._StatusCapture({}) as sink:
                result = brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        self.assertIsNone(result)
        self.assertEqual(sink["status"], brochure_enrichment.STATUS_FETCH_FAILED)
        self.assertIn("authentication failed", sink["detail"])

    def test_403_from_renderer_is_also_distinguished_as_an_auth_failure(self):
        response = MagicMock(status_code=403, headers={"content-type": "text/html"})
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response):
            with brochure_enrichment._StatusCapture({}) as sink:
                brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        self.assertIn("authentication failed", sink["detail"])

    def test_auth_header_minting_failure_is_logged_not_silent(self):
        with patch("google.oauth2.id_token.fetch_id_token", side_effect=Exception("no credentials")), \
                patch("brochure_enrichment.sys.stderr") as mock_stderr:
            headers = brochure_enrichment._canva_renderer_auth_headers("https://canva-renderer.example.run.app")

        self.assertEqual(headers, {})
        logged = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        self.assertIn("Could not mint an ID token", logged)

    def test_auth_headers_included_in_request(self):
        response = _canva_pages_response([b"\x89PNG"])
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response) as mock_post, \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={"Authorization": "Bearer tok"}):
            brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        self.assertEqual(mock_post.call_args.kwargs["headers"], {"Authorization": "Bearer tok"})

    def test_auth_header_minting_failure_falls_back_to_no_header(self):
        # google.oauth2.id_token.fetch_id_token raising (e.g. no ADC
        # available - local dev outside GCP) must never crash the call.
        with patch("google.oauth2.id_token.fetch_id_token", side_effect=Exception("no credentials")):
            headers = brochure_enrichment._canva_renderer_auth_headers("https://canva-renderer.example.run.app")
        self.assertEqual(headers, {})

    def test_old_format_image_response_is_distinguished_as_a_deployment_skew(self):
        # Regression guard for the real diagnostic question this was added
        # to answer: "multi-page support was implemented but production
        # still shows one page" - one likely cause is the canva-renderer
        # Cloud Run service simply not having been redeployed yet, still
        # returning its OLD raw image/png body instead of the new JSON
        # {"pages": [...]}  format. This must be logged as a DISTINCT,
        # actionable message - never conflated with a generic render
        # failure - since a real render failure never returns 200 with
        # image bytes at all.
        response = MagicMock(status_code=200, content=b"\x89PNG old format bytes", headers={"content-type": "image/png"})
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}), \
                patch("brochure_enrichment.sys.stderr") as mock_stderr:
            with brochure_enrichment._StatusCapture({}) as sink:
                result = brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        self.assertIsNone(result)
        self.assertEqual(sink["status"], brochure_enrichment.STATUS_RENDER_FAILED)
        self.assertIn("outdated", sink["detail"])
        logged = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        self.assertIn("OLD-FORMAT", logged)
        self.assertIn("needs redeploying", logged)


class TransientRendererRetryTests(EnrichmentTestCase):
    """
    Real production evidence this covers: two otherwise-healthy Canva
    URLs both failing with a bare HTTP 504, while canva_renderer's OWN
    logs showed the SAME render continuing to run in the background and
    later succeeding (partially or fully) - see canva_renderer/app.py's
    Handler.do_POST, which never sends a 504 itself (only 200/400/401/
    404/422/500/503), proving a 504 is Cloud Run's OWN proxy giving up on
    the request, never the renderer's own code failing or stopping. A
    502/503 IS retried (Cloud Run's own infrastructure or the renderer's
    busy-semaphore giving up BEFORE any work started for that attempt -
    see _CANVA_RENDERER_TRANSIENT_STATUS_CODES' own docstring), a SMALL,
    bounded number of times with a short fixed backoff. 504 is NEVER
    retried this way - see TransientRendererRetryTests' own 504-specific
    tests below for why firing a second, fully independent render would
    duplicate expensive Chromium work for a render that may already be
    succeeding.
    """

    def test_a_single_504_does_not_trigger_a_second_render(self):
        # The core fix: a 504 must never cause this app to fire a second,
        # independent Canva render (a new browser context/page, no
        # relation to the first) - see this class' own docstring for why
        # that duplicates expensive work for a render that may already be
        # succeeding server-side.
        response_504 = MagicMock(status_code=504, headers={"content-type": "text/html"}, json=MagicMock(side_effect=ValueError))
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response_504) as mock_post, \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}), \
                patch("brochure_enrichment.time.sleep") as mock_sleep, \
                patch("brochure_enrichment.sys.stderr") as mock_stderr:
            with brochure_enrichment._StatusCapture({}) as sink:
                result = brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        self.assertIsNone(result)
        mock_post.assert_called_once()  # exactly ONE request - never a duplicate render
        mock_sleep.assert_not_called()  # never backs off/retries a 504 at all
        self.assertEqual(sink["status"], brochure_enrichment.STATUS_RENDER_FAILED)
        logged = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        self.assertIn("HTTP 504", logged)
        self.assertNotIn("retrying", logged)  # never claims a retry that didn't happen

    def test_504_is_not_in_the_transient_retry_set(self):
        # Direct regression guard on the actual constant, not just the
        # end-to-end behavior above - a future edit that accidentally
        # re-adds 504 here would silently reintroduce the duplicate-
        # render bug this fix closes.
        self.assertNotIn(504, brochure_enrichment._CANVA_RENDERER_TRANSIENT_STATUS_CODES)
        self.assertIn(502, brochure_enrichment._CANVA_RENDERER_TRANSIENT_STATUS_CODES)
        self.assertIn(503, brochure_enrichment._CANVA_RENDERER_TRANSIENT_STATUS_CODES)

    def test_a_502_and_a_503_are_both_treated_as_transient(self):
        for status in (502, 503):
            response_bad = MagicMock(status_code=status, headers={"content-type": "text/html"})
            response_ok = _canva_pages_response([b"\x89PNG ok"], page_count_detected=1)
            with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                    patch("brochure_enrichment.httpx.post", side_effect=[response_bad, response_ok]) as mock_post, \
                    patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}), \
                    patch("brochure_enrichment.time.sleep"):
                result = brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

            self.assertEqual(result, [b"\x89PNG ok"], f"status {status} should have retried and recovered")
            self.assertEqual(mock_post.call_count, 2)

    def test_retry_backoff_is_short_and_bounded_not_unbounded_hang(self):
        # Directly protects against "do not cause a large spreadsheet
        # upload to hang for an unreasonable amount of time" - the backoff
        # itself must stay a small, fixed number of seconds, not scale
        # with anything unbounded.
        self.assertLessEqual(brochure_enrichment._CANVA_RENDERER_RETRY_BACKOFF_SECONDS, 5)
        self.assertLessEqual(brochure_enrichment._CANVA_RENDERER_MAX_ATTEMPTS, 3)

    def test_a_real_renderer_reason_e_g_422_is_never_retried(self):
        # A 422 is the renderer's OWN code reporting a clean, deliberate
        # failure (see canva_renderer/app.py's RenderError) - retrying
        # that immediately would almost certainly hit the exact same
        # wall again; only an infrastructure-level 502/503 is retried (see
        # TransientRendererRetryTests' own docstring on why 504 - despite
        # ALSO being infrastructure-level - is deliberately excluded).
        response_422 = MagicMock(
            status_code=422, headers={"content-type": "application/json"},
            json=MagicMock(return_value={"error": "render_failed", "reason": "not a recognized public Canva URL"}),
        )
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response_422) as mock_post, \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            result = brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        self.assertIsNone(result)
        mock_post.assert_called_once()


class FetchPdfBytesCanvaRoutingTests(EnrichmentTestCase):
    def test_fetch_pdf_bytes_routes_canva_urls_through_the_renderer(self):
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_canva_rendered_page", return_value=[b"\x89PNG bytes"]) as mock_render:
            result = brochure_enrichment._fetch_pdf_bytes(_CANVA_URL)

        self.assertEqual(result, [b"\x89PNG bytes"])
        mock_render.assert_called_once_with(_CANVA_URL)

    def test_non_canva_url_never_calls_the_canva_renderer(self):
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_canva_rendered_page") as mock_render, \
                patch("brochure_enrichment.httpx.get", return_value=_response()):
            brochure_enrichment._fetch_pdf_bytes("https://example.com/brochure.pdf")

        mock_render.assert_not_called()


class CanvaEndToEndEnrichmentTests(EnrichmentTestCase):
    """
    End-to-end through enrich_rows_grouped with the renderer configured -
    confirms a rendered Canva page feeds the EXISTING extraction pipeline
    (extract.render_pages/render_and_extract), never a separate Canva-only
    extraction path, and that existing blank-only/source-first rules and
    document-issue handling still apply exactly as for any other document.
    """

    def test_successful_canva_render_feeds_the_existing_extraction_pipeline(self):
        rows = [ListingRow(building="Metropolitan Wharf", brochure_link=_CANVA_URL, special_features=None)]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_canva_rendered_page", return_value=[b"\x89PNG fake render"]), \
                patch(
                    "brochure_enrichment.extract.images_from_png_pages", return_value=["img"],
                ) as mock_images_from_pages, \
                patch(
                    "brochure_enrichment.extract.render_and_extract",
                    return_value={"units": [{"building": "Metropolitan Wharf", "special_features": "Roof terrace"}]},
                ) as mock_extract:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(enriched[0].special_features, "Roof terrace")
        mock_images_from_pages.assert_called_once_with([b"\x89PNG fake render"])
        mock_extract.assert_called_once()
        self.assertEqual(stats["document_issues"], [])

    def test_multiple_canva_pages_all_reach_gemini_in_order(self):
        # The whole point of multi-page capture: every page returned by the
        # renderer must reach the SAME Gemini call, in the same order the
        # renderer captured them - never dropped, never reordered.
        pages = [b"\x89PNG cover", b"\x89PNG features", b"\x89PNG units", b"\x89PNG contacts"]
        rows = [ListingRow(building="Metropolitan Wharf", brochure_link=_CANVA_URL, special_features=None)]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_canva_rendered_page", return_value=pages), \
                patch(
                    "brochure_enrichment.extract.render_and_extract",
                    return_value={"units": [{"building": "Metropolitan Wharf", "special_features": "Roof terrace"}]},
                ) as mock_extract:
            brochure_enrichment.enrich_rows_grouped(rows)

        # extract.images_from_png_pages is NOT mocked here - the real
        # function runs, so the images actually passed to render_and_extract
        # are real types.Part objects built from these exact page bytes, in
        # this exact order (see extract.images_from_png_pages' own docstring).
        images_passed = mock_extract.call_args.args[0]
        self.assertEqual(len(images_passed), len(pages))
        for part, original_bytes in zip(images_passed, pages):
            self.assertEqual(part.inline_data.data, original_bytes)

    def test_field_from_a_later_page_fills_a_blank_property_field(self):
        # Proves the actual bug this feature fixes: a field that's blank on
        # the original row (special_features) gets filled from content that
        # - in a real brochure - only exists on a LATER page (e.g. page 3's
        # "Key Features"), not the cover page. The renderer/page-count is
        # irrelevant to this unit-level test (extract.render_and_extract is
        # mocked) - what matters is that whatever Gemini returns from
        # however many pages it saw reaches _apply_units_to_row and fills
        # the blank field, exactly as it would for a multi-page PDF.
        rows = [ListingRow(building="Metropolitan Wharf", brochure_link=_CANVA_URL, special_features=None, contacts=None)]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch(
                    "brochure_enrichment._fetch_canva_rendered_page",
                    return_value=[b"\x89PNG cover", b"\x89PNG features page", b"\x89PNG contacts page"],
                ), \
                patch("brochure_enrichment.extract.images_from_png_pages", return_value=["img1", "img2", "img3"]), \
                patch(
                    "brochure_enrichment.extract.render_and_extract",
                    return_value={
                        "units": [{"building": "Metropolitan Wharf", "special_features": "24/7 access, roof terrace"}],
                        "contacts": "Harry James - hj@theworkplacecompany.co.uk",
                    },
                ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(enriched[0].special_features, "24/7 access, roof terrace")
        self.assertEqual(enriched[0].contacts, "Harry James - hj@theworkplacecompany.co.uk")

    def test_successful_canva_enrichment_logs_exactly_which_fields_were_added(self):
        # The "brochure enrichment succeeded and which fields were added"
        # diagnostic - distinct from "Canva render succeeded" (which only
        # confirms rendering, not that Gemini found anything usable, let
        # alone that it actually changed a real row).
        rows = [ListingRow(building="Metropolitan Wharf", floor_unit="3rd", brochure_link=_CANVA_URL, special_features=None)]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_canva_rendered_page", return_value=[b"\x89PNG fake render"]), \
                patch("brochure_enrichment.extract.images_from_png_pages", return_value=["img"]), \
                patch(
                    "brochure_enrichment.extract.render_and_extract",
                    return_value={"units": [{"building": "Metropolitan Wharf", "special_features": "Roof terrace"}]},
                ), \
                patch("brochure_enrichment.sys.stderr") as mock_stderr:
            brochure_enrichment.enrich_rows_grouped(rows)

        logged = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        self.assertIn("Canva enrichment applied", logged)
        self.assertIn("special_features", logged)
        self.assertIn("Metropolitan Wharf", logged)

    def test_renderer_failure_leaves_row_unchanged_and_records_an_issue(self):
        rows = [ListingRow(building="Metropolitan Wharf", brochure_link=_CANVA_URL, special_features=None)]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", side_effect=httpx.ConnectError("renderer down")):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(enriched[0].brochure_link, _CANVA_URL)  # original link preserved
        self.assertEqual(len(stats["document_issues"]), 1)
        self.assertEqual(stats["document_issues"][0]["status"], brochure_enrichment.STATUS_FETCH_FAILED)

    def test_canva_failure_does_not_block_other_documents_in_the_same_run(self):
        rows = [
            ListingRow(building="Metropolitan Wharf", brochure_link=_CANVA_URL, special_features=None),
            ListingRow(building="Good Co", brochure_link="https://example.com/good.pdf", special_features=None),
        ]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_canva_rendered_page", return_value=None), \
                patch("brochure_enrichment.httpx.get", return_value=_response()), \
                patch("brochure_enrichment.extract.render_pages", return_value=["img"]), \
                patch(
                    "brochure_enrichment.extract.render_and_extract",
                    return_value={"units": [{"building": "Good Co", "special_features": "Nice"}]},
                ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(enriched[1].special_features, "Nice")

    def test_duplicate_canva_url_across_rows_is_only_rendered_once(self):
        rows = [
            ListingRow(building="Metropolitan Wharf", floor_unit="1st", brochure_link=_CANVA_URL, special_features=None),
            ListingRow(building="Metropolitan Wharf", floor_unit="2nd", brochure_link=_CANVA_URL, special_features=None),
        ]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch(
                    "brochure_enrichment._fetch_canva_rendered_page", return_value=[b"\x89PNG fake"],
                ) as mock_render, \
                patch("brochure_enrichment.extract.images_from_png_pages", return_value=["img"]), \
                patch(
                    "brochure_enrichment.extract.render_and_extract",
                    return_value={"units": [{"building": "Metropolitan Wharf", "special_features": "Nice"}]},
                ):
            brochure_enrichment.enrich_rows_grouped(rows)

        mock_render.assert_called_once()  # one unique URL shared by 2 rows -> rendered once, not twice

    def test_metropolitan_wharf_inconsistent_canva_rent_is_not_applied(self):
        # Regression test for the real confirmed production bug: a Canva
        # render of Metropolitan Wharf's page returned rent_pcm=33/
        # rent_psf=0.14 for a 2762 sqft unit whose real rent_psf (from the
        # original spreadsheet source, already on the row and protected) is
        # 33 - implying a genuine rent_pcm around 7595.5, not 33. The
        # candidate rent_pcm must be rejected, not stored, and the row's own
        # trusted rent_psf must stay untouched.
        rows = [ListingRow(
            building="Metropolitan Wharf", floor_unit="1st", brochure_link=_CANVA_URL,
            size_sqft=2762, rent_psf=33, rent_pcm=None,
        )]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_canva_rendered_page", return_value=[b"\x89PNG fake render"]), \
                patch("brochure_enrichment.extract.images_from_png_pages", return_value=["img"]), \
                patch(
                    "brochure_enrichment.extract.render_and_extract",
                    return_value={"units": [{
                        "building": "Metropolitan Wharf", "floor_unit": "1st", "size_sqft": 2762,
                        "rent_pcm": 33, "rent_psf": 0.14,
                    }]},
                ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].rent_pcm)
        self.assertNotEqual(enriched[0].rent_pcm, 33)
        self.assertEqual(enriched[0].rent_psf, 33)  # the trusted source value, never overwritten

    def test_canva_rent_conflict_is_recorded_as_a_document_issue(self):
        rows = [ListingRow(
            building="Metropolitan Wharf", floor_unit="1st", brochure_link=_CANVA_URL,
            size_sqft=2762, rent_psf=33, rent_pcm=None,
        )]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_canva_rendered_page", return_value=[b"\x89PNG fake render"]), \
                patch("brochure_enrichment.extract.images_from_png_pages", return_value=["img"]), \
                patch(
                    "brochure_enrichment.extract.render_and_extract",
                    return_value={"units": [{
                        "building": "Metropolitan Wharf", "floor_unit": "1st", "size_sqft": 2762, "rent_pcm": 33,
                    }]},
                ):
            _, _, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(len(stats["document_issues"]), 1)
        self.assertEqual(stats["document_issues"][0]["status"], brochure_enrichment.STATUS_EXTRACTED_BUT_AMBIGUOUS)

    def test_consistent_canva_rent_still_fills_when_genuinely_blank(self):
        # The gate must not become so strict that a genuinely good Canva
        # render can no longer fill a real gap - a fresh, fully consistent
        # rent trio (nothing pre-existing to check against, but internally
        # self-consistent) still applies.
        rows = [ListingRow(
            building="Metropolitan Wharf", floor_unit="1st", brochure_link=_CANVA_URL,
            size_sqft=None, rent_psf=None, rent_pcm=None,
        )]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_canva_rendered_page", return_value=[b"\x89PNG fake render"]), \
                patch("brochure_enrichment.extract.images_from_png_pages", return_value=["img"]), \
                patch(
                    "brochure_enrichment.extract.render_and_extract",
                    return_value={"units": [{
                        "building": "Metropolitan Wharf", "floor_unit": "1st", "size_sqft": 2762,
                        "rent_pcm": 7595.5, "rent_psf": 33,
                    }]},
                ):
            enriched, _, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(enriched[0].size_sqft, 2762)
        self.assertEqual(enriched[0].rent_pcm, 7595.5)
        self.assertEqual(enriched[0].rent_psf, 33)
        self.assertEqual(stats["document_issues"], [])

    def test_canva_extraction_summary_is_logged(self):
        # The "what did Gemini actually return" diagnostic - lets a
        # production log distinguish "Gemini found nothing useful" from
        # "Gemini found something but matching/apply rejected it", which
        # were previously indistinguishable from the logs alone.
        rows = [ListingRow(building="Metropolitan Wharf", brochure_link=_CANVA_URL, contacts=None)]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_canva_rendered_page", return_value=[b"\x89PNG fake render"]), \
                patch("brochure_enrichment.extract.images_from_png_pages", return_value=["img"]), \
                patch(
                    "brochure_enrichment.extract.render_and_extract",
                    return_value={
                        "units": [{"building": "Metropolitan Wharf"}],
                        "contacts": "Harry James - hj@theworkplacecompany.co.uk",
                    },
                ), \
                patch("brochure_enrichment.sys.stderr") as mock_stderr:
            brochure_enrichment.enrich_rows_grouped(rows)

        logged = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        self.assertIn("Canva extraction for", logged)
        self.assertIn("contacts=present", logged)

    def test_canva_extraction_summary_is_never_logged_for_non_canva_brochures(self):
        # Scoping guard - this new diagnostic must never add log noise to
        # the PDF/Box/Dropbox/GDrive paths, which never had it before.
        rows = [ListingRow(building="Good Co", brochure_link="https://example.com/good.pdf", contacts=None)]
        with patch("brochure_enrichment.httpx.get", return_value=_response()), \
                patch("brochure_enrichment.extract.render_pages", return_value=["img"]), \
                patch(
                    "brochure_enrichment.extract.render_and_extract",
                    return_value={"units": [{"building": "Good Co"}], "contacts": "Someone - x@example.com"},
                ), \
                patch("brochure_enrichment.sys.stderr") as mock_stderr:
            brochure_enrichment.enrich_rows_grouped(rows)

        logged = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        self.assertNotIn("Canva extraction for", logged)

    def test_blank_fields_after_successful_canva_extraction_are_logged_with_a_reason(self):
        # The "matched/rejected" diagnostic - the document read fine (see
        # the extraction-summary log) but THIS row still has blank fields
        # afterward; this names them and says why, rather than staying
        # silently indistinguishable from "the document had nothing at all".
        rows = [ListingRow(building="Unrelated Building", floor_unit="9th", brochure_link=_CANVA_URL, special_features=None)]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_canva_rendered_page", return_value=[b"\x89PNG fake render"]), \
                patch("brochure_enrichment.extract.images_from_png_pages", return_value=["img"]), \
                patch(
                    "brochure_enrichment.extract.render_and_extract",
                    return_value={"units": [{"building": "Metropolitan Wharf", "special_features": "Roof terrace"}]},
                ), \
                patch("brochure_enrichment.sys.stderr") as mock_stderr:
            brochure_enrichment.enrich_rows_grouped(rows)

        logged = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        self.assertIn("left", logged)
        self.assertIn("special_features", logged)
        self.assertIn("no confident match for this row", logged)


class LooksLikeFetchableDocumentTests(unittest.TestCase):
    def test_pdf_content_type_always_accepted(self):
        self.assertTrue(brochure_enrichment._looks_like_fetchable_document("application/pdf", b"anything"))

    def test_pdf_magic_bytes_always_accepted(self):
        self.assertTrue(brochure_enrichment._looks_like_fetchable_document(None, b"%PDF-1.4 ..."))

    def test_png_rejected_by_default(self):
        self.assertFalse(brochure_enrichment._looks_like_fetchable_document("image/png", b"\x89PNG\r\n\x1a\n..."))

    def test_png_accepted_with_accept_image_formats(self):
        self.assertTrue(
            brochure_enrichment._looks_like_fetchable_document(
                "image/png", b"\x89PNG\r\n\x1a\n...", accept_image_formats=True,
            ),
        )

    def test_jpeg_magic_bytes_accepted_with_accept_image_formats(self):
        self.assertTrue(
            brochure_enrichment._looks_like_fetchable_document(
                None, b"\xff\xd8\xfffake jpeg", accept_image_formats=True,
            ),
        )

    def test_docx_never_accepted_even_with_accept_image_formats(self):
        self.assertFalse(
            brochure_enrichment._looks_like_fetchable_document(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK\x03\x04...",
                accept_image_formats=True,
            ),
        )

    def test_html_error_page_never_accepted(self):
        self.assertFalse(brochure_enrichment._looks_like_fetchable_document("text/html", b"<html>404</html>"))


class MatchUnitTests(unittest.TestCase):
    def test_single_unit_matches_regardless_of_floor(self):
        row = ListingRow(building="28 Lime Street", floor_unit="4th Floor")
        units = [{"building": "28 Lime Street", "floor_unit": None, "special_features": "Reception; showers"}]

        matched = brochure_enrichment._match_unit(row, units)

        self.assertIs(matched, units[0])

    def test_floor_match_disambiguates_multiple_units(self):
        row = ListingRow(building="28 Lime Street", floor_unit="4th Floor")
        units = [
            {"building": "28 Lime Street", "floor_unit": "2nd Floor", "special_features": "Wrong floor"},
            {"building": "28 Lime Street", "floor_unit": "4th Floor", "special_features": "Right floor"},
        ]

        matched = brochure_enrichment._match_unit(row, units)

        self.assertEqual(matched["special_features"], "Right floor")

    def test_real_friars_yard_shape_matches_via_address_1_fallback(self):
        # Synthetic reproduction of the real, confirmed production case:
        # UNION's own spreadsheet states this property's plain street
        # address as its ENTIRE building field (no name at all), while the
        # real Friars Yard brochure is branded under a marketing name that
        # shares no words with the address - building-to-building matching
        # (tiers 1-3) can never bridge this; the row must fall through to
        # the address_1 fallback (tier 4) to reach a confident single-unit
        # match at all, at which point floor disambiguates normally.
        row = ListingRow(building="160 Blackfriars Road", floor_unit="4th Floor")
        units = [
            {
                "building": "Friars Yard", "address_1": "160 Blackfriars Road",
                "floor_unit": "4th Floor", "state_of_space": "Partially Fitted",
            },
            {
                "building": "Friars Yard", "address_1": "160 Blackfriars Road",
                "floor_unit": "3rd Floor", "state_of_space": "CAT A",
            },
        ]

        matched = brochure_enrichment._match_unit(row, units)

        self.assertEqual(matched["state_of_space"], "Partially Fitted")

    def test_size_match_disambiguates_when_floor_label_differs(self):
        row = ListingRow(building="28 Lime Street", floor_unit="Suite A", size_sqft=2000)
        units = [
            {"building": "28 Lime Street", "floor_unit": "1st Floor", "size_sqft": 1000},
            {"building": "28 Lime Street", "floor_unit": "2nd Floor", "size_sqft": 2005},
        ]

        matched = brochure_enrichment._match_unit(row, units)

        self.assertEqual(matched["size_sqft"], 2005)

    def test_ambiguous_floor_and_size_returns_none(self):
        row = ListingRow(building="28 Lime Street", floor_unit="Suite Z", size_sqft=9999)
        units = [
            {"building": "28 Lime Street", "floor_unit": "1st Floor", "size_sqft": 1000},
            {"building": "28 Lime Street", "floor_unit": "2nd Floor", "size_sqft": 2000},
        ]

        self.assertIsNone(brochure_enrichment._match_unit(row, units))

    def test_short_floor_number_matches_the_full_brochure_label(self):
        # Real confirmed gap: a provider spreadsheet's own floor_unit is
        # routinely just "5th" (or a bare "5") where the brochure itself
        # says "5th Floor" - normalize_key alone never reconciles these,
        # even though there's exactly one "5"-numbered floor and zero real
        # ambiguity (traced against a real sample brochure in this repo's
        # own tests/sample_docs/40_New_Bond_Street_Brochure.pdf).
        units = [
            {"building": "40 New Bond Street", "floor_unit": "5th Floor", "special_features": "Terrace"},
            {"building": "40 New Bond Street", "floor_unit": "4th Floor", "special_features": "Under offer"},
            {"building": "40 New Bond Street", "floor_unit": "3rd Floor", "special_features": "Available"},
        ]

        for floor_label in ("5th", "5", "Floor 5", "5th fl"):
            row = ListingRow(building="40 New Bond Street", floor_unit=floor_label)
            matched = brochure_enrichment._match_unit(row, units)
            self.assertEqual(matched["special_features"], "Terrace", msg=f"floor_unit={floor_label!r}")

    def test_floor_number_fallback_never_fires_when_exact_text_already_resolved(self):
        # The floor-number tier only runs when the exact-text tier didn't
        # already resolve to exactly one - a row whose floor_unit exactly
        # matches one brochure unit must keep matching that one even if a
        # DIFFERENT unit would coincidentally also share its floor number.
        row = ListingRow(building="A", floor_unit="5th Floor West")
        units = [
            {"building": "A", "floor_unit": "5th Floor West", "special_features": "Correct"},
            {"building": "A", "floor_unit": "5th Floor East", "special_features": "Wrong"},
        ]

        matched = brochure_enrichment._match_unit(row, units)

        self.assertEqual(matched["special_features"], "Correct")

    def test_two_units_sharing_the_same_floor_number_stay_ambiguous(self):
        # "5th Floor" and "5B Suite" both extract floor number 5 - the
        # fallback must still return None rather than guess between them,
        # same principle as the exact-text and size tiers already apply.
        row = ListingRow(building="A", floor_unit="5")
        units = [
            {"building": "A", "floor_unit": "5th Floor", "special_features": "One"},
            {"building": "A", "floor_unit": "5B Suite", "special_features": "Two"},
        ]

        self.assertIsNone(brochure_enrichment._match_unit(row, units))

    def test_floor_number_fallback_does_not_apply_when_row_floor_has_no_digit(self):
        row = ListingRow(building="A", floor_unit="Ground Floor")
        units = [
            {"building": "A", "floor_unit": "1st Floor", "special_features": "One"},
            {"building": "A", "floor_unit": "2nd Floor", "special_features": "Two"},
        ]

        self.assertIsNone(brochure_enrichment._match_unit(row, units))

    def test_spelled_out_ordinal_brochure_label_resolves_a_numeral_row(self):
        # The real Copthall Estates "28 King Street" case this fallback was
        # extended for: the brochure spells every floor as a full word with
        # no digit at all, so the exact-text tier can't match "3rd Floor"
        # and the ORIGINAL digit-only floor-number tier couldn't either -
        # now it can, via the spelled-out-ordinal fallback.
        row = ListingRow(building="28 King Street", floor_unit="3rd Floor", size_sqft=927)
        units = [
            {"building": "28 King Street", "floor_unit": "Second Floor", "size_sqft": 915},
            {"building": "28 King Street", "floor_unit": "Third Floor", "size_sqft": 927},
            {"building": "28 King Street", "floor_unit": "Fourth Floor", "size_sqft": 930},
        ]

        matched = brochure_enrichment._match_unit(row, units)

        self.assertIsNotNone(matched)
        self.assertEqual(matched["floor_unit"], "Third Floor")

    def test_mixed_digit_and_word_ordinal_units_still_resolve_correctly(self):
        # A single building whose own brochure labels some floors with a
        # digit and others spelled out (a real possible shape, e.g. a
        # renovated top floor labeled differently from the rest) - both
        # forms must resolve through the same tier without interfering.
        row_word = ListingRow(building="A", floor_unit="3rd Floor")
        row_digit = ListingRow(building="A", floor_unit="5th Floor")
        units = [
            {"building": "A", "floor_unit": "Third Floor", "special_features": "Word-labeled"},
            {"building": "A", "floor_unit": "5th Floor", "special_features": "Digit-labeled"},
        ]

        self.assertEqual(brochure_enrichment._match_unit(row_word, units)["special_features"], "Word-labeled")
        self.assertEqual(brochure_enrichment._match_unit(row_digit, units)["special_features"], "Digit-labeled")

    def test_two_units_sharing_the_same_spelled_out_ordinal_stay_ambiguous(self):
        # Defensive case: two genuinely different units that both spell out
        # the SAME word-number (e.g. "Third Floor" and "Third Floor Annex")
        # must still stay an unresolved tie, never guessed - same
        # conservative principle test_two_units_sharing_the_same_floor_
        # number_stay_ambiguous already covers for the digit form.
        row = ListingRow(building="A", floor_unit="3rd Floor")
        units = [
            {"building": "A", "floor_unit": "Third Floor", "special_features": "One"},
            {"building": "A", "floor_unit": "Third Floor Annex", "special_features": "Two"},
        ]

        self.assertIsNone(brochure_enrichment._match_unit(row, units))

    def test_no_building_match_returns_none(self):
        row = ListingRow(building="Somewhere Else")
        units = [{"building": "28 Lime Street", "floor_unit": "4th Floor"}]

        self.assertIsNone(brochure_enrichment._match_unit(row, units))

    def test_different_buildings_in_same_brochure_never_cross_match(self):
        row_a = ListingRow(building="Building A", floor_unit="1st Floor")
        row_b = ListingRow(building="Building B", floor_unit="1st Floor")
        units = [
            {"building": "Building A", "floor_unit": "1st Floor", "special_features": "A features"},
            {"building": "Building B", "floor_unit": "1st Floor", "special_features": "B features"},
        ]

        matched_a = brochure_enrichment._match_unit(row_a, units)
        matched_b = brochure_enrichment._match_unit(row_b, units)

        self.assertEqual(matched_a["special_features"], "A features")
        self.assertEqual(matched_b["special_features"], "B features")

    def test_blank_building_never_matches(self):
        row = ListingRow(building="   ")
        units = [{"building": "28 Lime Street", "floor_unit": "4th Floor"}]

        self.assertIsNone(brochure_enrichment._match_unit(row, units))

    def test_address_suffixed_row_matches_the_bare_brochure_building_name(self):
        # Real confirmed shape: a provider spreadsheet's own building column
        # bakes the street address into the same field a brochure's own
        # cover only ever states as the bare building name.
        row = ListingRow(building="Nash House - 13a St George St", floor_unit="2nd Floor")
        units = [{"building": "Nash House", "floor_unit": "2nd Floor", "special_features": "Roof terrace"}]

        matched = brochure_enrichment._match_unit(row, units)

        self.assertEqual(matched["special_features"], "Roof terrace")

    def test_two_different_addresses_sharing_a_brand_prefix_never_cross_match(self):
        # The regression this module's own address-suffix stripping must
        # never reintroduce: a portfolio brochure listing two GENUINELY
        # different physical properties that merely share a brand/prefix
        # ("Example House") must never be treated as the same building just
        # because both strip down to "Example House".
        row_alpha = ListingRow(building="Example House - 10 Alpha Street", floor_unit="1st Floor")
        row_beta = ListingRow(building="Example House - 50 Beta Street", floor_unit="1st Floor")
        units = [
            {"building": "Example House - 10 Alpha Street", "floor_unit": "1st Floor", "special_features": "Alpha only"},
            {"building": "Example House - 50 Beta Street", "floor_unit": "1st Floor", "special_features": "Beta only"},
        ]

        matched_alpha = brochure_enrichment._match_unit(row_alpha, units)
        matched_beta = brochure_enrichment._match_unit(row_beta, units)

        self.assertEqual(matched_alpha["special_features"], "Alpha only")
        self.assertEqual(matched_beta["special_features"], "Beta only")

    def test_address_suffixed_row_against_two_different_bare_prefixed_buildings_stays_ambiguous(self):
        # Same regression as above, but the ROW itself only ever states the
        # bare brand ("Example House") with no address of its own - it can
        # never be told apart from either brochure entry, so this must stay
        # unresolved (never guessed) rather than silently picking one.
        row = ListingRow(building="Example House", floor_unit="1st Floor")
        units = [
            {"building": "Example House - 10 Alpha Street", "floor_unit": "1st Floor", "special_features": "Alpha only"},
            {"building": "Example House - 50 Beta Street", "floor_unit": "1st Floor", "special_features": "Beta only"},
        ]

        self.assertIsNone(brochure_enrichment._match_unit(row, units))

    def test_exact_match_is_preferred_over_an_ambiguous_stripped_match(self):
        # An EXACT name match is always sufficient identity evidence on its
        # own, even alongside another candidate that would otherwise make
        # the stripped-suffix tier ambiguous.
        row = ListingRow(building="Example House - 10 Alpha Street", floor_unit="1st Floor")
        units = [
            {"building": "Example House - 10 Alpha Street", "floor_unit": "1st Floor", "special_features": "Exact match"},
            {"building": "Example House - 50 Beta Street", "floor_unit": "1st Floor", "special_features": "Unrelated"},
        ]

        matched = brochure_enrichment._match_unit(row, units)

        self.assertEqual(matched["special_features"], "Exact match")


class BuildingAddressSuffixTests(unittest.TestCase):
    """_strip_building_address_suffix - the identity-corroboration helper
    behind _building_identity_matches (see its own docstring)."""

    def test_strips_a_dash_separated_address(self):
        self.assertEqual(
            brochure_enrichment._strip_building_address_suffix("Discovery House - 28-42 Banner St"),
            "Discovery House",
        )

    def test_strips_a_comma_separated_address(self):
        self.assertEqual(
            brochure_enrichment._strip_building_address_suffix("Nash House, 13a St George St"), "Nash House",
        )

    def test_leaves_a_non_address_shaped_second_half_alone(self):
        self.assertEqual(
            brochure_enrichment._strip_building_address_suffix("Discovery House - East Wing"),
            "Discovery House - East Wing",
        )

    def test_blank_returns_unchanged(self):
        self.assertIsNone(brochure_enrichment._strip_building_address_suffix(None))
        self.assertEqual(brochure_enrichment._strip_building_address_suffix(""), "")

    def test_a_building_whose_own_name_is_an_unnamed_address_range_is_never_mis_split(self):
        # Confirmed real gap: a bare `\d.+` check alone happily strips this
        # down to a nonsense "27" - house_number.leading_house_number
        # recognizes the HEAD itself ("27") as already being a house number,
        # not a genuine building name, so the split is rejected entirely.
        self.assertEqual(
            brochure_enrichment._strip_building_address_suffix("27-30 Lime Street"), "27-30 Lime Street",
        )

    def test_a_tail_that_is_not_a_real_house_number_is_never_split(self):
        self.assertEqual(
            brochure_enrichment._strip_building_address_suffix("Discovery House - Reception"),
            "Discovery House - Reception",
        )


class BuildingIdentityMatchesTests(unittest.TestCase):
    """_building_identity_matches - see its own docstring for the three-tier
    exact/address-suffix-stripped/street-suffix-stripped, all corroborated-
    by-uniqueness-past-tier-1, rule."""

    def test_exact_match_returns_every_exact_index(self):
        indices = brochure_enrichment._building_identity_matches(
            "28 Lime Street", ["28 Lime Street", "28 Lime Street", "Somewhere Else"],
        )
        self.assertEqual(indices, [0, 1])

    def test_unique_stripped_match_is_accepted(self):
        indices = brochure_enrichment._building_identity_matches(
            "Nash House - 13a St George St", ["Nash House"],
        )
        self.assertEqual(indices, [0])

    def test_ambiguous_stripped_match_is_rejected(self):
        indices = brochure_enrichment._building_identity_matches(
            "Example House", ["Example House - 10 Alpha Street", "Example House - 50 Beta Street"],
        )
        self.assertEqual(indices, [])

    def test_blank_row_building_never_matches_anything(self):
        self.assertEqual(brochure_enrichment._building_identity_matches("   ", ["Anything"]), [])

    def test_no_candidates_returns_empty(self):
        self.assertEqual(brochure_enrichment._building_identity_matches("28 Lime Street", []), [])

    def test_unique_street_suffix_stripped_match_is_accepted(self):
        # Real production case: a row's own building text lacks the
        # trailing street-type word a brochure's fuller name states -
        # "35a Westminister Bridge" (sic - the row's own real typo,
        # unrelated to this fix) vs Gemini's own extracted "35A
        # Westminster Bridge Road". This test uses the correctly-spelled
        # row text - the typo itself is a source-data problem, fixed in
        # the spreadsheet, not something this matcher is meant to paper
        # over (see BUILDING_FUZZY_MATCH_THRESHOLD's own module elsewhere
        # for why a similarity-score fuzzy match is deliberately NOT used
        # here).
        indices = brochure_enrichment._building_identity_matches(
            "35a Westminster Bridge", ["35A Westminster Bridge Road"],
        )
        self.assertEqual(indices, [0])

    def test_street_suffix_stripped_match_works_in_either_direction(self):
        # The row side carrying the extra street-type word instead of the
        # brochure side - the strip must be symmetric, not a one-way rule.
        indices = brochure_enrichment._building_identity_matches(
            "35 Example Road", ["35 Example"],
        )
        self.assertEqual(indices, [0])

    def test_ambiguous_street_suffix_stripped_match_is_rejected(self):
        # Two genuinely different streets that merely share everything
        # before their own street-type word - incorrect enrichment is
        # worse than a blank field, same philosophy as the address-suffix
        # tier's own ambiguous case.
        indices = brochure_enrichment._building_identity_matches(
            "Kings", ["Kings Road", "Kings Street"],
        )
        self.assertEqual(indices, [])

    def test_two_different_one_word_street_type_building_names_never_collide(self):
        # The real risk _strip_trailing_street_suffix_word's own
        # len(words) > 1 guard closes: a building's own genuine one-word
        # name that happens to BE a street-type word (e.g. "Court",
        # "Road") must never strip down to an empty, universally-matching
        # key - without that guard, "Court" and "Road" (two genuinely
        # different, unrelated buildings) would both reduce to "" and
        # incorrectly match each other.
        indices = brochure_enrichment._building_identity_matches("Court", ["Road"])
        self.assertEqual(indices, [])

    def test_tier_4a_exact_address_match_is_accepted(self):
        # The real, confirmed gap this tier closes: a provider's own
        # spreadsheet states a property's plain street address as its
        # ENTIRE building field (no name at all) while the real brochure
        # for that property (the actual "Friars Yard" / "160 Blackfriars
        # Road" document) is branded under a marketing name sharing no
        # words with the address at all - tiers 1-3 (building-to-building)
        # can never bridge that, no matter how much suffix-stripping is
        # tried. Both the 3rd and 4th floor units in the real document
        # share this identical address_1 - both indices must be returned,
        # not treated as ambiguous (see _distinct_building_group).
        indices = brochure_enrichment._building_identity_matches(
            "160 Blackfriars Road",
            ["Friars Yard", "Friars Yard"],
            ["160 Blackfriars Road", "160 Blackfriars Road"],
        )
        self.assertEqual(indices, [0, 1])

    def test_tier_4a_rejects_when_the_address_is_shared_by_different_buildings(self):
        # Unlike the same-building case above, matching indices naming
        # TWO DIFFERENT buildings for the same address text is a genuine,
        # unresolvable conflict - _distinct_building_group must reject it,
        # not just count raw indices.
        indices = brochure_enrichment._building_identity_matches(
            "160 Blackfriars Road",
            ["Friars Yard", "Other Tower"],
            ["160 Blackfriars Road", "160 Blackfriars Road"],
        )
        self.assertEqual(indices, [])

    def test_tier_4b_suffix_stripped_address_with_matching_house_number_is_accepted(self):
        # row_building omits the trailing street-type word its own address
        # states - same class of gap tier 3 already closes for building-to-
        # building text, extended here to building-vs-address.
        indices = brochure_enrichment._building_identity_matches(
            "160 Blackfriars", ["Friars Yard"], ["160 Blackfriars Road"],
        )
        self.assertEqual(indices, [0])

    def test_tier_4b_rejects_when_the_house_number_disagrees(self):
        # The stripped text alone ("blackfriars") is never sufficient for
        # tier 4b - a DIFFERENT house number on the same street is a real,
        # confirmed-different address, not the same building.
        indices = brochure_enrichment._building_identity_matches(
            "162 Blackfriars", ["Friars Yard"], ["160 Blackfriars Road"],
        )
        self.assertEqual(indices, [])

    def test_tier_4_is_never_tried_once_an_earlier_tier_already_resolved(self):
        # A real building-name match (tier 1 here) is always more specific
        # evidence than a cross-field address match - tier 4 must never
        # even be consulted once an earlier tier already found something,
        # regardless of what candidate_addresses says.
        indices = brochure_enrichment._building_identity_matches(
            "Friars Yard", ["Friars Yard"], ["Something Entirely Unrelated"],
        )
        self.assertEqual(indices, [0])

    def test_tier_4_is_skipped_entirely_when_no_address_data_is_available(self):
        # _match_building_feature's own building_features entries carry no
        # address_1 at all - candidate_addresses defaults to None, and tier
        # 4 must never be attempted (never raise, never guess) when there's
        # nothing to compare against.
        indices = brochure_enrichment._building_identity_matches("160 Blackfriars Road", ["Friars Yard"])
        self.assertEqual(indices, [])

    def test_tier_4b_never_applies_when_row_building_has_no_leading_house_number(self):
        # A name-only row_building (no house number of its own to
        # corroborate against) must never match via the suffix-stripped
        # address tier - there's nothing to guard the comparison with.
        indices = brochure_enrichment._building_identity_matches(
            "Blackfriars", ["Friars Yard"], ["160 Blackfriars Road"],
        )
        self.assertEqual(indices, [])


class MatchBuildingFeatureTests(unittest.TestCase):
    """_match_building_feature - the building-level (level B) counterpart
    to _match_unit, same exact-match-only philosophy."""

    def test_matches_by_exact_building_name(self):
        row = ListingRow(building="The Canal Building")
        units = _brochure_units(
            [],
            building_features=[
                {"building": "The Canal Building", "features": "Exposed beams; canalside frontage"},
                {"building": "The Mill", "features": "Exposed brickwork"},
            ],
        )

        self.assertEqual(
            brochure_enrichment._match_building_feature(row, units), "Exposed beams; canalside frontage",
        )

    def test_no_matching_building_returns_none(self):
        row = ListingRow(building="Somewhere Else")
        units = _brochure_units([], building_features=[{"building": "The Canal Building", "features": "Beams"}])

        self.assertIsNone(brochure_enrichment._match_building_feature(row, units))

    def test_blank_building_features_returns_none(self):
        row = ListingRow(building="The Canal Building")
        units = _brochure_units([])

        self.assertIsNone(brochure_enrichment._match_building_feature(row, units))

    def test_duplicate_building_entries_stay_ambiguous(self):
        row = ListingRow(building="The Canal Building")
        units = _brochure_units(
            [],
            building_features=[
                {"building": "The Canal Building", "features": "One"},
                {"building": "The Canal Building", "features": "Two"},
            ],
        )

        self.assertIsNone(brochure_enrichment._match_building_feature(row, units))

    def test_address_suffixed_row_matches_the_bare_brochure_building_name(self):
        row = ListingRow(building="Nash House - 13a St George St")
        units = _brochure_units(
            [], building_features=[{"building": "Nash House", "features": "Roof terrace"}],
        )

        self.assertEqual(brochure_enrichment._match_building_feature(row, units), "Roof terrace")

    def test_two_different_addresses_sharing_a_brand_prefix_never_cross_contaminate(self):
        row_alpha = ListingRow(building="Example House - 10 Alpha Street")
        row_beta = ListingRow(building="Example House - 50 Beta Street")
        units = _brochure_units(
            [],
            building_features=[
                {"building": "Example House - 10 Alpha Street", "features": "Alpha only"},
                {"building": "Example House - 50 Beta Street", "features": "Beta only"},
            ],
        )

        self.assertEqual(brochure_enrichment._match_building_feature(row_alpha, units), "Alpha only")
        self.assertEqual(brochure_enrichment._match_building_feature(row_beta, units), "Beta only")


class FloorNumberTests(unittest.TestCase):
    def test_ordinal_with_word(self):
        self.assertEqual(brochure_enrichment._floor_number("5th Floor"), 5)

    def test_bare_ordinal(self):
        self.assertEqual(brochure_enrichment._floor_number("5th"), 5)

    def test_bare_number(self):
        self.assertEqual(brochure_enrichment._floor_number("5"), 5)

    def test_number_before_word(self):
        self.assertEqual(brochure_enrichment._floor_number("Floor 5"), 5)

    def test_no_digit_returns_none(self):
        self.assertIsNone(brochure_enrichment._floor_number("Ground Floor"))

    def test_blank_returns_none(self):
        self.assertIsNone(brochure_enrichment._floor_number(None))
        self.assertIsNone(brochure_enrichment._floor_number(""))

    def test_two_digit_number_not_confused_with_a_single_digit(self):
        self.assertEqual(brochure_enrichment._floor_number("15th Floor"), 15)
        self.assertNotEqual(brochure_enrichment._floor_number("15th Floor"), brochure_enrichment._floor_number("5th"))

    def test_spelled_out_ordinal_word(self):
        # Real confirmed gap: real Copthall Estates brochures ("28-King-
        # Street-EC2-2026-March.pdf", "Copthall-House-Office-Feb-2026.pdf")
        # spell every floor out as a full word with no digit at all.
        self.assertEqual(brochure_enrichment._floor_number("Third Floor"), 3)
        self.assertEqual(brochure_enrichment._floor_number("Fourth Floor"), 4)
        self.assertEqual(brochure_enrichment._floor_number("First Floor"), 1)

    def test_spelled_out_ordinal_is_case_insensitive(self):
        for variant in ("THIRD FLOOR", "third floor", "Third floor", "ThIrD FlOoR"):
            self.assertEqual(brochure_enrichment._floor_number(variant), 3, msg=f"variant={variant!r}")

    def test_full_range_first_through_twentieth(self):
        words = [
            "First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth", "Ninth", "Tenth",
            "Eleventh", "Twelfth", "Thirteenth", "Fourteenth", "Fifteenth", "Sixteenth", "Seventeenth",
            "Eighteenth", "Nineteenth", "Twentieth",
        ]
        for expected, word in enumerate(words, start=1):
            self.assertEqual(brochure_enrichment._floor_number(f"{word} Floor"), expected, msg=f"word={word!r}")

    def test_bare_ordinal_word_with_no_floor_suffix(self):
        self.assertEqual(brochure_enrichment._floor_number("Third"), 3)

    def test_digit_form_wins_when_a_label_has_a_conflicting_word(self):
        # Not a real expected shape, just confirms the digit tier is
        # checked first and a differing word match never overrides it.
        self.assertEqual(brochure_enrichment._floor_number("5th Floor (Third Suite)"), 5)

    def test_ground_and_reception_style_labels_still_return_none(self):
        # Deliberately NOT given an invented numeric mapping - see
        # _ORDINAL_WORD_TO_NUMBER's own comment on why guessing "Ground" is
        # floor 0 (or similar) risks a false match against a genuinely
        # different numbered floor.
        for label in ("Ground Floor", "Lower Ground Floor", "Basement", "Mezzanine", "Reception"):
            self.assertIsNone(brochure_enrichment._floor_number(label), msg=f"label={label!r}")


class ApplyUnitsToRowNonStringGuardTests(unittest.TestCase):
    """
    _apply_units_to_row's units come from raw Gemini JSON (see
    _extract_brochure_units), never validated against ExtractedFields the
    way the primary upload path's own units are (see extract.extract()) -
    and model_copy(update=...) does not re-validate field types the way
    constructing a ListingRow directly would. A non-str value for an
    ENRICHABLE_FIELDS field must be treated exactly like a blank one
    (skipped), never silently written into a ListingRow field that every
    other reader assumes is a plain string.
    """

    def test_list_value_is_not_applied(self):
        row = ListingRow(building="A", floor_unit="1st", special_features=None)
        units = [{"building": "A", "floor_unit": "1st", "special_features": ["Kitchen", "Showers"]}]

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.special_features)
        self.assertEqual(fields, [])
        self.assertIs(new_row, row)

    def test_numeric_value_is_not_applied(self):
        row = ListingRow(building="A", floor_unit="1st", state_of_space=None)
        units = [{"building": "A", "floor_unit": "1st", "state_of_space": 42}]

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.state_of_space)
        self.assertEqual(fields, [])

    def test_other_valid_string_field_still_applies_alongside_a_bad_one(self):
        row = ListingRow(building="A", floor_unit="1st", special_features=None, state_of_space=None)
        units = [{
            "building": "A", "floor_unit": "1st",
            "special_features": ["not", "a", "string"], "state_of_space": "Cat A",
        }]

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.special_features)
        self.assertEqual(new_row.state_of_space, "Cat A")
        self.assertEqual(fields, ["state_of_space"])


class MalformedUnitEntryTests(unittest.TestCase):
    """
    Confirmed real, reproducible production bug: units is raw Gemini JSON,
    never schema-validated for this enrichment path (unlike the direct-
    PDF-upload path's own ExtractedFields validation) - a single malformed
    entry (a stray non-dict item, or a non-numeric size_sqft) previously
    made _match_unit raise, uncaught, in enrich_rows_grouped's own main
    thread loop, entirely outside _fetch_one's try/except - crashing the
    WHOLE batch over one bad brochure. _match_unit now filters non-dict
    entries out before matching, and enrich_rows_grouped's own per-row
    apply step has its own try/except too (belt and braces).
    """

    def test_match_unit_skips_a_non_dict_entry_instead_of_raising(self):
        row = ListingRow(building="A", floor_unit="1st")
        units = [None, {"building": "A", "floor_unit": "1st", "special_features": "Fine"}]

        matched = brochure_enrichment._match_unit(row, units)

        self.assertEqual(matched["special_features"], "Fine")

    def test_match_unit_returns_none_when_every_entry_is_malformed(self):
        row = ListingRow(building="A", floor_unit="1st")

        self.assertIsNone(brochure_enrichment._match_unit(row, [None, "not a dict either", 42]))

    def test_non_numeric_size_sqft_is_ignored_not_raised(self):
        row = ListingRow(building="A", floor_unit="Suite Z", size_sqft=2000)
        units = [
            {"building": "A", "floor_unit": "1st Floor", "size_sqft": "not a number"},
            {"building": "A", "floor_unit": "2nd Floor", "size_sqft": 2005},
        ]

        matched = brochure_enrichment._match_unit(row, units)

        self.assertEqual(matched["size_sqft"], 2005)

    def test_enrich_rows_grouped_never_crashes_on_a_malformed_unit_entry(self):
        rows = [
            ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None),
            ListingRow(building="B", brochure_link="https://example.com/b.pdf", special_features=None),
            ListingRow(building="C", brochure_link="https://example.com/c.pdf", special_features=None),
        ]

        def _fake(url):
            if "b.pdf" in url:
                return [None, {"building": "B", "floor_unit": None, "special_features": "Recovered"}]
            return [{"building": url, "floor_unit": None, "special_features": "fine"}]

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(enriched[1].special_features, "Recovered")
        self.assertEqual(stats["unique_brochures_considered"], 3)
        self.assertEqual(stats["processed_urls"]["https://example.com/b.pdf"], "ok")

    def test_a_unit_that_raises_when_applied_leaves_only_that_row_unchanged(self):
        # A stray non-dict entry is the confirmed real case, but the
        # per-row try/except in enrich_rows_grouped is belt-and-braces on
        # top of _match_unit's own filtering - this proves the outer guard
        # independently, by making _apply_units_to_row itself raise.
        rows = [ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None)]

        with patch("brochure_enrichment._extract_brochure_units", return_value=[{"building": "A"}]), \
             patch("brochure_enrichment._apply_units_to_row", side_effect=RuntimeError("boom")):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(log, [])


class ThreeLevelEnrichmentTests(unittest.TestCase):
    """
    Regression tests for a real, confirmed gap: a real Regent's Wharf
    brochure clearly states property-wide amenities (WiredScore Platinum,
    BREEAM Excellent, showers/lockers, cycle spaces) on one page and
    building-specific descriptions (The Canal Building's own exposed
    beams/canalside frontage, The Packing House's own rooftop terrace) next
    to each sub-building's own schedule of areas - none of which ever
    reached any row's special_features, because _apply_units_to_row only
    ever considered a SPECIFIC matched floor/unit's own text, with no
    concept of "document-wide" or "building-wide" at all. Traced against
    the real PDF: raw Gemini extraction correctly captured all of this once
    the extraction prompt was asked for property_features/building_features
    (see extract.py's own PROMPT) - the gap was that _apply_units_to_row
    never looked for or applied either.

    Fix: brochure information is now applied at the WIDEST level the
    brochure itself clearly supports - property/document-wide (property_
    features, contacts), then building-wide (building_features), then
    floor/unit-specific (a matched unit's own fields) - each narrower level
    overwriting the same field only when it's actually available, never
    fabricated. See _apply_units_to_row's own docstring for the exact
    precedence and brochure_enrichment.py's own module docstring for the
    overall rationale.
    """

    def test_property_wide_feature_survives_with_no_exact_floor_match(self):
        # No floor_unit at all on the row, but the row's building IS one of
        # the brochure's own buildings - the document-wide property_
        # features must still apply. The single matching brochure unit ALSO
        # confidently identifies this row (see _match_unit's own "a building
        # with only one matching unit is a confident match" rule), so
        # floor_unit itself is now also backfilled (UNIT_LEVEL_FIELDS) -
        # a second, independently-correct fill, not a regression.
        row = ListingRow(building="The Canal Building", floor_unit=None, special_features=None)
        units = _brochure_units(
            [{"building": "The Canal Building", "floor_unit": "5th Floor", "special_features": None}],
            property_features="WiredScore Platinum; BREEAM Excellent; 16 showers & 108 lockers",
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.special_features, "WiredScore Platinum; BREEAM Excellent; 16 showers & 108 lockers")
        self.assertEqual(new_row.floor_unit, "5th Floor")
        self.assertEqual(set(fields), {"special_features", "floor_unit"})

    def test_property_wide_feature_applies_even_when_building_does_not_match_any_unit(self):
        # Property-wide facts are true of the WHOLE document, regardless of
        # building - this row's own building isn't even one of the
        # brochure's buildings, but it shares this SAME brochure_link (the
        # only way _apply_units_to_row is ever called for it), so the
        # document-wide fallback still applies.
        row = ListingRow(building="A Building Not In This Brochure", special_features=None)
        units = _brochure_units(
            [{"building": "The Canal Building", "floor_unit": "5th Floor"}],
            property_features="Gas free buildings; natural ventilation",
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.special_features, "Gas free buildings; natural ventilation")

    def test_building_specific_feature_only_reaches_its_own_building(self):
        canal_row = ListingRow(building="The Canal Building", floor_unit=None, special_features=None)
        mill_row = ListingRow(building="The Mill", floor_unit=None, special_features=None)
        units = _brochure_units(
            [
                {"building": "The Canal Building", "floor_unit": "5th Floor"},
                {"building": "The Mill", "floor_unit": "5th Floor"},
            ],
            building_features=[
                {"building": "The Canal Building", "features": "Exposed beams; canalside frontage"},
                {"building": "The Mill", "features": "Exposed brickwork; courtyard balconies"},
            ],
        )

        canal_new, _ = brochure_enrichment._apply_units_to_row(canal_row, units)
        mill_new, _ = brochure_enrichment._apply_units_to_row(mill_row, units)

        self.assertEqual(canal_new.special_features, "Exposed beams; canalside frontage")
        self.assertEqual(mill_new.special_features, "Exposed brickwork; courtyard balconies")

    def test_building_specific_feature_does_not_reach_an_unrelated_building(self):
        # special_features must NOT leak from "The Canal Building"'s own
        # building_features entry - that's this test's own point, and still
        # holds. The lone brochure unit for "The Packing House" itself IS a
        # confident single-unit match for this row (see _match_unit), so
        # floor_unit is separately, correctly backfilled from it - a genuine
        # unit-level fill, unrelated to the building_features leak this test
        # guards against.
        other_row = ListingRow(building="The Packing House", floor_unit=None, special_features=None)
        units = _brochure_units(
            [{"building": "The Packing House", "floor_unit": "Ground Floor"}],
            building_features=[{"building": "The Canal Building", "features": "Exposed beams; canalside frontage"}],
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(other_row, units)

        self.assertIsNone(new_row.special_features)
        self.assertEqual(new_row.floor_unit, "Ground Floor")
        self.assertEqual(fields, ["floor_unit"])

    def test_address_only_building_field_still_fills_state_of_space_end_to_end(self):
        # End-to-end (through _apply_units_to_row, not just _match_unit in
        # isolation) reproduction of the real Friars Yard case: row.building
        # is plain address text with no name at all, the brochure's own
        # building is a marketing name sharing no words with it - the new
        # address_1 fallback tier is what lets state_of_space (a UNIT_LEVEL_
        # FIELD, only ever filled via a confident _match_unit result) reach
        # the row at all.
        row = ListingRow(building="160 Blackfriars Road", floor_unit="4th Floor", state_of_space=None)
        units = _brochure_units([
            {
                "building": "Friars Yard", "address_1": "160 Blackfriars Road",
                "floor_unit": "4th Floor", "state_of_space": "Partially Fitted",
            },
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.state_of_space, "Partially Fitted")
        self.assertIn("state_of_space", fields)

    def test_floor_specific_feature_never_leaks_to_a_different_floor(self):
        row_5th = ListingRow(building="The Canal Building", floor_unit="5th Floor", special_features=None)
        row_4th = ListingRow(building="The Canal Building", floor_unit="4th Floor", special_features=None)
        units = _brochure_units([
            {"building": "The Canal Building", "floor_unit": "5th Floor", "special_features": "Includes mezzanine"},
            {"building": "The Canal Building", "floor_unit": "4th Floor", "special_features": None},
        ])

        new_5th, _ = brochure_enrichment._apply_units_to_row(row_5th, units)
        new_4th, _ = brochure_enrichment._apply_units_to_row(row_4th, units)

        self.assertEqual(new_5th.special_features, "Includes mezzanine")
        self.assertIsNone(new_4th.special_features)  # never inherits the 5th floor's own mezzanine

    def test_unmatched_floor_still_gets_building_level_fallback(self):
        # A real confirmed Regent's Wharf shape: several floors within one
        # building have NO floor-specific special_features of their own at
        # all - a failure to find unit-specific text must not throw away
        # the building-wide fact this row can still safely receive.
        row = ListingRow(building="The Canal Building", floor_unit="4th Floor", special_features=None)
        units = _brochure_units(
            [{"building": "The Canal Building", "floor_unit": "4th Floor", "special_features": None}],
            building_features=[{"building": "The Canal Building", "features": "Exposed beams; canalside frontage"}],
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.special_features, "Exposed beams; canalside frontage")
        self.assertEqual(fields, ["special_features"])

    def test_all_three_levels_present_are_combined_specific_to_general(self):
        # Real gap this closes: the three tiers used to overwrite each
        # other (narrowest present wins outright, the wider ones simply
        # discarded) - now every genuinely present tier is combined into
        # one value, unit first, then building, then property.
        row = ListingRow(building="The Canal Building", floor_unit="5th Floor", special_features=None)
        units = _brochure_units(
            [{"building": "The Canal Building", "floor_unit": "5th Floor", "special_features": "Includes mezzanine"}],
            property_features="WiredScore Platinum",
            building_features=[{"building": "The Canal Building", "features": "Exposed beams"}],
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.special_features, "Includes mezzanine; Exposed beams; WiredScore Platinum")
        self.assertEqual(fields, ["special_features"])

    def test_unit_only_special_features_is_unchanged(self):
        # No wider tier present at all - a single segment, byte-identical
        # to the pre-combining behavior.
        row = ListingRow(building="The Canal Building", floor_unit="5th Floor", special_features=None)
        units = _brochure_units(
            [{"building": "The Canal Building", "floor_unit": "5th Floor", "special_features": "Includes mezzanine"}],
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.special_features, "Includes mezzanine")

    def test_unit_and_building_features_are_combined(self):
        # Real gap this closes, specifically: previously the building-wide
        # text would have OVERWRITTEN the unit's own text (a narrower
        # source always won outright) - now both are kept, unit first.
        row = ListingRow(building="The Canal Building", floor_unit="5th Floor", special_features=None)
        units = _brochure_units(
            [{"building": "The Canal Building", "floor_unit": "5th Floor", "special_features": "Includes mezzanine"}],
            building_features=[{"building": "The Canal Building", "features": "Exposed beams"}],
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.special_features, "Includes mezzanine; Exposed beams")

    def test_blank_unit_text_still_falls_back_to_the_building_level_alone(self):
        # No unit-specific text to combine with (the unit itself IS
        # matched, but its own special_features is blank) - the existing
        # single-tier building-level fallback must still work exactly as
        # before this change.
        row = ListingRow(building="The Canal Building", floor_unit="5th Floor", special_features=None)
        units = _brochure_units(
            [{"building": "The Canal Building", "floor_unit": "5th Floor", "special_features": None}],
            building_features=[{"building": "The Canal Building", "features": "Exposed beams"}],
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.special_features, "Exposed beams")

    def test_existing_special_features_is_kept_as_the_first_segment_when_combined(self):
        # A row whose own spreadsheet already put something in special_
        # features (e.g. a bare "U/O" status marker, confirmed present in
        # real Workplace Plus rows) must still get the brochure's real
        # amenity info appended - the combine is attempted regardless of
        # whether this value was blank to begin with, with the row's own
        # value kept as the first, most-specific segment, never discarded.
        row = ListingRow(
            building="The Canal Building", floor_unit="5th Floor", special_features="Genuine provider text",
        )
        units = _brochure_units(
            [{"building": "The Canal Building", "floor_unit": "5th Floor", "special_features": "Includes mezzanine"}],
            property_features="WiredScore Platinum",
            building_features=[{"building": "The Canal Building", "features": "Exposed beams"}],
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(
            new_row.special_features, "Genuine provider text; Includes mezzanine; Exposed beams; WiredScore Platinum",
        )
        self.assertEqual(fields, ["special_features"])

    def test_existing_special_features_unchanged_when_no_wider_tier_applies(self):
        # Same starting value, but nothing new to combine with (no unit
        # match, no building_features, no property_features) - the combine
        # is still attempted, but produces the exact same value, so this
        # must be recognized as a no-op: no field listed as changed, and
        # the exact same row object returned (never a needless model_copy).
        row = ListingRow(
            building="The Canal Building", floor_unit="5th Floor", special_features="Genuine provider text",
        )
        units = _brochure_units([])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.special_features, "Genuine provider text")
        self.assertEqual(fields, [])
        self.assertIs(new_row, row)

    def test_contacts_retained_without_requiring_a_floor_match(self):
        # contacts is this test's own point (document-level, no floor match
        # needed). The lone brochure unit is also a confident single-unit
        # match for this row, so floor_unit is separately backfilled too -
        # a genuine, independent unit-level fill.
        row = ListingRow(building="The Canal Building", floor_unit=None, contacts=None)
        units = _brochure_units(
            [{"building": "The Canal Building", "floor_unit": "5th Floor"}],
            contacts="Jane Smith, jane@agent.com, 020 7946 0000",
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.contacts, "Jane Smith, jane@agent.com, 020 7946 0000")
        self.assertEqual(new_row.floor_unit, "5th Floor")
        self.assertEqual(set(fields), {"contacts", "floor_unit"})

    def test_missing_contacts_stay_blank_never_invented(self):
        row = ListingRow(building="The Canal Building", floor_unit="5th Floor", contacts=None)
        units = _brochure_units(
            [{"building": "The Canal Building", "floor_unit": "5th Floor", "special_features": "Includes mezzanine"}],
            contacts=None,
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.contacts)
        self.assertNotIn("contacts", fields)

    def test_existing_contacts_never_overwritten(self):
        row = ListingRow(building="The Canal Building", contacts="Existing Agent, existing@agent.com")
        units = _brochure_units([], contacts="Different Agent, different@agent.com")

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.contacts, "Existing Agent, existing@agent.com")
        self.assertEqual(fields, [])

    def test_ambiguous_unit_match_stays_conservative_property_wide_still_applies(self):
        # Two floors, neither identifiable from the row's own vague label -
        # _match_unit correctly returns None (no unit-level guess), but the
        # document-wide property_features fallback is unaffected by that.
        row = ListingRow(building="The Canal Building", floor_unit="Suite Z", special_features=None)
        units = _brochure_units(
            [
                {"building": "The Canal Building", "floor_unit": "1st Floor", "special_features": "One"},
                {"building": "The Canal Building", "floor_unit": "2nd Floor", "special_features": "Two"},
            ],
            property_features="WiredScore Platinum",
        )

        self.assertIsNone(brochure_enrichment._match_unit(row, units))
        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)
        self.assertEqual(new_row.special_features, "WiredScore Platinum")

    def test_regents_wharf_style_campus_enriches_applicable_rows_at_the_right_level(self):
        # End-to-end shape mirroring the real confirmed brochure: a
        # property-wide amenities blurb, per-building descriptive text, and
        # a schedule of areas where several floors have no floor-specific
        # text of their own at all.
        units = _brochure_units(
            [
                {"building": "Thorley Works", "floor_unit": "5th Floor", "special_features": "1,851 sq ft outdoor space"},
                {"building": "The Canal Building", "floor_unit": "5th Floor", "special_features": "Includes mezzanine"},
                {"building": "The Canal Building", "floor_unit": "4th Floor", "special_features": None},
                {"building": "The Packing House", "floor_unit": "Ground Floor", "special_features": None},
            ],
            property_features="WiredScore Platinum; BREEAM Excellent; 16 showers & 108 lockers",
            building_features=[
                {"building": "The Canal Building", "features": "Exposed beams; canalside frontage"},
                {"building": "The Packing House", "features": "Stunning rooftop terrace"},
            ],
        )

        thorley, _ = brochure_enrichment._apply_units_to_row(
            ListingRow(building="Thorley Works", floor_unit="5th Floor", special_features=None), units,
        )
        canal_5th, _ = brochure_enrichment._apply_units_to_row(
            ListingRow(building="The Canal Building", floor_unit="5th Floor", special_features=None), units,
        )
        canal_4th, _ = brochure_enrichment._apply_units_to_row(
            ListingRow(building="The Canal Building", floor_unit="4th Floor", special_features=None), units,
        )
        packing_ground, _ = brochure_enrichment._apply_units_to_row(
            ListingRow(building="The Packing House", floor_unit="Ground Floor", special_features=None), units,
        )

        # Every genuinely present tier combines, specific-to-general -
        # property_features is document-wide, so it reaches every row here
        # regardless of which narrower tiers also applied.
        self.assertEqual(
            thorley.special_features,
            "1,851 sq ft outdoor space; WiredScore Platinum; BREEAM Excellent; 16 showers & 108 lockers",
        )
        self.assertEqual(
            canal_5th.special_features,
            "Includes mezzanine; Exposed beams; canalside frontage; "
            "WiredScore Platinum; BREEAM Excellent; 16 showers & 108 lockers",
        )
        # No floor-specific text -> building-level + property-level combine, never blank.
        self.assertEqual(
            canal_4th.special_features,
            "Exposed beams; canalside frontage; WiredScore Platinum; BREEAM Excellent; 16 showers & 108 lockers",
        )
        self.assertEqual(
            packing_ground.special_features,
            "Stunning rooftop terrace; WiredScore Platinum; BREEAM Excellent; 16 showers & 108 lockers",
        )

    def test_a_brochures_own_floorplan_pages_contribute_floor_specific_content(self):
        # A brochure PDF that also contains floorplan pages is still ONE
        # brochure document (see this module's own docstring) - Gemini's
        # single extraction call over the whole document already reads
        # those pages as part of `units`, so a unit's own special_features
        # can legitimately be explicit floorplan-derived content ("72
        # desks; 10-person boardroom") sitting right alongside a property-
        # wide marketing blurb from the SAME document - both usable, at
        # their own correct scope, with no special-casing needed.
        units = _brochure_units(
            [
                {"building": "40 New Bond Street", "floor_unit": "5th Floor", "special_features": "72 desks; 10-person boardroom; phone booths; kitchen/breakout"},
                {"building": "40 New Bond Street", "floor_unit": "3rd Floor", "special_features": None},
            ],
            property_features="WiredScore Platinum; BREEAM Excellent; manned reception",
        )

        floor_matched, _ = brochure_enrichment._apply_units_to_row(
            ListingRow(building="40 New Bond Street", floor_unit="5th Floor", special_features=None), units,
        )
        # 9th Floor matches neither of the two real units in this document -
        # _match_unit correctly returns no confident match, but that must
        # not throw away the document-wide fact this row can still safely
        # receive.
        no_floor_match, _ = brochure_enrichment._apply_units_to_row(
            ListingRow(building="40 New Bond Street", floor_unit="9th Floor", special_features=None), units,
        )

        # The floorplan-page-derived, floor-specific text combines with the
        # SAME document's own property-wide blurb - both usable, unit text
        # first (most specific).
        self.assertEqual(
            floor_matched.special_features,
            "72 desks; 10-person boardroom; phone booths; kitchen/breakout; "
            "WiredScore Platinum; BREEAM Excellent; manned reception",
        )
        # A DIFFERENT floor in the same document, with no floor-specific
        # match, still safely falls back to the property-wide fact - never
        # blank just because this document also happens to contain floor
        # plan pages.
        self.assertEqual(no_floor_match.special_features, "WiredScore Platinum; BREEAM Excellent; manned reception")


class BuildingAndUnitFieldFallbackTests(unittest.TestCase):
    """
    Regression tests for the widened field scope - address_1/postcode/
    submarket (BUILDING_LEVEL_FIELDS), floor_unit/size_sqft/desks_max
    (UNIT_LEVEL_FIELDS), and rent_pcm/rent_psf (HIGH_RISK_UNIT_LEVEL_FIELDS)
    can now be filled from a brochure when genuinely blank, using the exact
    same confidence-scoped matching (_building_identity_matches/_match_unit)
    ThreeLevelEnrichmentTests already proves for special_features/state_of_
    space/contacts - never a new, weaker matching mechanism.
    """

    def test_non_blank_source_values_are_never_overwritten(self):
        row = ListingRow(
            building="Nash House", floor_unit="2nd", size_sqft=1524, address_1="Already Stated Address",
            postcode="EC1A 1AA", submarket="Already Stated Area", rent_pcm=4000, rent_psf=50, desks_max=12,
        )
        units = _brochure_units([{
            "building": "Nash House", "floor_unit": "2nd", "size_sqft": 1524,
            "address_1": "13a St George St", "postcode": "W1S 2FE", "submarket": "Mayfair",
            "rent_pcm": 9999, "rent_psf": 999, "desks_max": 999,
        }])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(fields, [])
        self.assertIs(new_row, row)

    def test_blank_floor_with_a_unique_compatible_unit_fills(self):
        row = ListingRow(building="Nash House", floor_unit=None, size_sqft=1524)
        units = _brochure_units([{"building": "Nash House", "floor_unit": "2nd Floor", "size_sqft": 1524}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.floor_unit, "2nd Floor")
        self.assertIn("floor_unit", fields)

    def test_ambiguous_floor_stays_blank(self):
        # Two units in the same building, neither disambiguated by floor
        # text/number or size (row.size_sqft is also blank) - an unresolved
        # tie, same "incorrect enrichment is worse than missing" rule as
        # every other tier.
        row = ListingRow(building="Nash House", floor_unit=None, size_sqft=None)
        units = _brochure_units([
            {"building": "Nash House", "floor_unit": "1st Floor", "size_sqft": 1000},
            {"building": "Nash House", "floor_unit": "2nd Floor", "size_sqft": 1500},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.floor_unit)
        self.assertEqual(fields, [])

    def test_blank_size_with_a_unique_compatible_unit_fills(self):
        row = ListingRow(building="Nash House", floor_unit="2nd Floor", size_sqft=None)
        units = _brochure_units([{"building": "Nash House", "floor_unit": "2nd Floor", "size_sqft": 1524}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.size_sqft, 1524)
        self.assertIn("size_sqft", fields)

    def test_ambiguous_size_stays_blank(self):
        row = ListingRow(building="Nash House", floor_unit=None, size_sqft=None)
        units = _brochure_units([
            {"building": "Nash House", "floor_unit": None, "size_sqft": 1000},
            {"building": "Nash House", "floor_unit": None, "size_sqft": 1500},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.size_sqft)
        self.assertEqual(fields, [])

    def test_blank_address_with_confident_building_and_explicit_brochure_address_fills(self):
        # Discovery House-style shape: the row's own building already has a
        # redundant address suffix stripped by _building_identity_matches'
        # own weaker (uniqueness-corroborated) tier.
        row = ListingRow(building="Discovery House - 28-42 Banner St", address_1=None, postcode=None)
        units = _brochure_units([
            {"building": "Discovery House", "address_1": "28-42 Banner St", "postcode": "EC1Y 8QE"},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.address_1, "28-42 Banner St")
        self.assertEqual(new_row.postcode, "EC1Y 8QE")
        self.assertIn("address_1", fields)
        self.assertIn("postcode", fields)

    def test_ambiguous_building_leaves_address_blank(self):
        # Two genuinely different buildings share a brand/prefix once the
        # address suffix is stripped - _building_identity_matches' own
        # weaker tier only ever accepts a stripped match when it is the
        # SOLE candidate (see its own docstring) - two here, so neither
        # resolves and address_1 must stay blank.
        row = ListingRow(building="WeWork - 10 Fenchurch St", address_1=None)
        units = _brochure_units([
            {"building": "WeWork", "address_1": "10 Fenchurch St"},
            {"building": "WeWork", "address_1": "20 Old Broad St"},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.address_1)
        self.assertEqual(fields, [])

    def test_explicit_brochure_postcode_fills_blank_postcode(self):
        row = ListingRow(building="Nutmeg House", postcode=None)
        units = _brochure_units([{"building": "Nutmeg House", "postcode": "SE1 2NQ"}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.postcode, "SE1 2NQ")
        self.assertIn("postcode", fields)

    def test_brochure_postcode_conflicting_with_source_evidence_stays_blank(self):
        # Reuses geocode.py's own postcode-district-conflict check - the
        # row's own building text states WC1, the brochure's matched
        # building states an SE1 postcode - a genuine contradiction, so
        # this must be rejected exactly like geocode.py rejects a
        # conflicting Places candidate.
        row = ListingRow(building="New Derwent House WC1", postcode=None)
        units = _brochure_units([{"building": "New Derwent House WC1", "postcode": "SE1 2NQ"}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.postcode)
        self.assertEqual(fields, [])

    def test_brochure_locality_fills_blank_submarket_when_safe(self):
        row = ListingRow(building="Nutmeg House", submarket=None)
        units = _brochure_units([{"building": "Nutmeg House", "submarket": "London Bridge"}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.submarket, "London Bridge")
        self.assertIn("submarket", fields)

    def test_exact_unit_desk_capacity_fills_blank_desks_max(self):
        row = ListingRow(building="Nash House", floor_unit="2nd Floor", desks_max=None)
        units = _brochure_units([{"building": "Nash House", "floor_unit": "2nd Floor", "desks_max": 12}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.desks_max, 12)
        self.assertIn("desks_max", fields)

    def test_ambiguous_unit_desk_information_stays_blank(self):
        row = ListingRow(building="Nash House", floor_unit=None, size_sqft=None, desks_max=None)
        units = _brochure_units([
            {"building": "Nash House", "floor_unit": "1st Floor", "size_sqft": 1000, "desks_max": 10},
            {"building": "Nash House", "floor_unit": "2nd Floor", "size_sqft": 1500, "desks_max": 12},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.desks_max)
        self.assertEqual(fields, [])

    def test_exact_unit_explicit_rent_fills_blank_rent(self):
        row = ListingRow(building="Nash House", floor_unit="2nd Floor", rent_pcm=None, rent_psf=None)
        units = _brochure_units([
            {"building": "Nash House", "floor_unit": "2nd Floor", "rent_pcm": 4200, "rent_psf": 55},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.rent_pcm, 4200)
        self.assertEqual(new_row.rent_psf, 55)
        self.assertIn("rent_pcm", fields)
        self.assertIn("rent_psf", fields)

    def test_ambiguous_unit_rent_stays_blank(self):
        # Two units in the same building, no way to resolve which one this
        # row describes - rent must never be guessed from either.
        row = ListingRow(building="Nash House", floor_unit=None, size_sqft=None, rent_pcm=None)
        units = _brochure_units([
            {"building": "Nash House", "floor_unit": "1st Floor", "size_sqft": 1000, "rent_pcm": 3000},
            {"building": "Nash House", "floor_unit": "2nd Floor", "size_sqft": 1500, "rent_pcm": 4200},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.rent_pcm)
        self.assertEqual(fields, [])

    def test_general_marketing_text_never_fills_rent_without_a_unit_match(self):
        # No per-unit rent stated at all (Gemini's own PROMPT already never
        # populates rent_pcm/rent_psf from vague marketing copy - this
        # confirms _apply_units_to_row adds no separate inference of its
        # own on top of that).
        row = ListingRow(building="Nash House", floor_unit="2nd Floor", rent_pcm=None)
        units = _brochure_units([{"building": "Nash House", "floor_unit": "2nd Floor", "rent_pcm": None}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.rent_pcm)
        self.assertEqual(fields, [])

    def test_building_wide_address_fill_does_not_require_a_floor_match(self):
        # Confirms point 8 - address/postcode fill even when floor_unit
        # itself would be ambiguous (two units, no size/floor to
        # disambiguate) - the two concerns are genuinely independent.
        row = ListingRow(building="Nash House", floor_unit=None, size_sqft=None, address_1=None, postcode=None)
        units = _brochure_units([
            {"building": "Nash House", "floor_unit": "1st Floor", "size_sqft": 1000, "address_1": "13a St George St", "postcode": "W1S 2FE"},
            {"building": "Nash House", "floor_unit": "2nd Floor", "size_sqft": 1500, "address_1": "13a St George St", "postcode": "W1S 2FE"},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.address_1, "13a St George St")
        self.assertEqual(new_row.postcode, "W1S 2FE")
        self.assertIsNone(new_row.floor_unit)  # still correctly unresolved

    def test_multi_building_brochure_cannot_cross_contaminate_address(self):
        # "The Packing House"'s row must only ever receive ITS OWN
        # building's address/postcode, never "The Canal Building"'s -
        # confirms _match_building_value scopes to _building_identity_
        # matches, not "any unit in this document".
        row = ListingRow(building="The Packing House", address_1=None, postcode=None)
        units = _brochure_units([
            {"building": "The Canal Building", "address_1": "1 Towpath Walk", "postcode": "SE1 1AA"},
            {"building": "The Packing House", "address_1": "2 Towpath Walk", "postcode": "SE1 1AB"},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.address_1, "2 Towpath Walk")
        self.assertEqual(new_row.postcode, "SE1 1AB")
        self.assertIn("address_1", fields)
        self.assertIn("postcode", fields)

    def test_multi_building_brochure_leaves_address_blank_when_only_a_different_building_states_one(self):
        row = ListingRow(building="The Packing House", address_1=None, postcode=None)
        units = _brochure_units([
            {"building": "The Canal Building", "address_1": "1 Towpath Walk", "postcode": "SE1 1AA"},
            {"building": "The Packing House", "address_1": None, "postcode": None},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.address_1)
        self.assertIsNone(new_row.postcode)
        self.assertEqual(fields, [])

    def test_lat_lng_provider_and_internal_ref_are_never_brochure_written(self):
        # Even a malformed/unexpected raw unit dict carrying these keys
        # (never actually produced by the real PROMPT) must never reach the
        # row - these fields are structurally absent from every category
        # this module ever loops over.
        row = ListingRow(
            building="Nash House", floor_unit="2nd Floor", lat=None, lng=None,
            provider=None, internal_ref=None,
        )
        units = _brochure_units([{
            "building": "Nash House", "floor_unit": "2nd Floor",
            "lat": 51.5, "lng": -0.1, "provider": "Some Agent", "internal_ref": "REF123",
        }])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.lat)
        self.assertIsNone(new_row.lng)
        self.assertIsNone(new_row.provider)
        self.assertIsNone(new_row.internal_ref)
        self.assertNotIn("lat", fields)
        self.assertNotIn("lng", fields)
        self.assertNotIn("provider", fields)
        self.assertNotIn("internal_ref", fields)


class RentValuesConsistentTests(unittest.TestCase):
    """
    Direct tests of _rent_values_consistent - the generic, property-agnostic
    math check (annual rent implied by rent_pcm vs by rent_psf*size_sqft)
    behind the guard against a real confirmed production bug: a Canva-
    rendered Metropolitan Wharf page produced rent_pcm=33/rent_psf=0.14 for
    a 2762 sqft unit whose real, spreadsheet-sourced rent_psf was 33
    (implying rent_pcm=7595.5, not 33) - a ~230x divergence between the
    brochure's own candidate rent_pcm and what the row's OWN trusted
    rent_psf*size_sqft implied. This pure function only ever sees whatever
    three values its caller passes it - see _rent_check_values/_row_had_
    rent_conflict/RentConsistencyApplicationTests below for where the row's
    own trusted values are actually mixed in with a brochure's candidate
    ones, which is what makes the real bug catchable at all (see this
    class's own test_wildly_inconsistent_trio_is_rejected for why the bug's
    two candidate numbers alone do NOT fail this check).
    """

    def test_missing_size_is_not_enough_data_to_judge(self):
        self.assertTrue(brochure_enrichment._rent_values_consistent(None, 33, 0.14))

    def test_missing_pcm_is_not_enough_data_to_judge(self):
        self.assertTrue(brochure_enrichment._rent_values_consistent(2762, None, 33))

    def test_missing_psf_is_not_enough_data_to_judge(self):
        self.assertTrue(brochure_enrichment._rent_values_consistent(2762, 7595.5, None))

    def test_consistent_trio_is_accepted(self):
        # 2762 sqft at £33 psf pa implies pcm = 2762*33/12 = 7595.5 exactly.
        self.assertTrue(brochure_enrichment._rent_values_consistent(2762, 7595.5, 33))

    def test_consistent_trio_within_rounding_tolerance_is_accepted(self):
        # A real agent's own rounding of the same figures - a few percent
        # off, never rejected for ordinary rounding.
        self.assertTrue(brochure_enrichment._rent_values_consistent(2762, 7700, 33))

    def test_wildly_inconsistent_trio_is_rejected(self):
        # size=2762 with rent_psf=33 implies an annual rent around 91,146
        # (2762*33) - a rent_pcm of 33 (implying just 396/year) is nowhere
        # close, exactly the divergence the user's own worked example
        # describes. NOTE: the real Metropolitan Wharf bug's own two numbers
        # (rent_pcm=33, rent_psf=0.14) do NOT fail this pure check when
        # tested against each other alone - 33*12=396 and 0.14*2762=386.68
        # are mutually consistent, because that pair was produced by the
        # same pcm<->psf arithmetic this check itself uses (see this
        # module's own docstring on why _rent_check_values always mixes in
        # the ROW's own already-trusted value rather than only ever
        # comparing a brochure's two candidate numbers against each other -
        # see test_candidate_pcm_inconsistent_with_existing_psf_and_size_is_
        # rejected below for the check that actually catches the real bug).
        self.assertFalse(brochure_enrichment._rent_values_consistent(2762, 33, 33))

    def test_non_positive_values_are_not_this_checks_job(self):
        self.assertTrue(brochure_enrichment._rent_values_consistent(2762, 0, 33))
        self.assertTrue(brochure_enrichment._rent_values_consistent(2762, -100, 33))


class RentConsistencyApplicationTests(unittest.TestCase):
    """
    _apply_units_to_row-level tests for the rent-consistency gate - see
    RentValuesConsistentTests above for the underlying math check on its
    own. Confirms the gate only ever blocks a NEWLY PROPOSED, inconsistent
    rent value, never an already-populated (protected) one, and never a
    genuinely consistent one.
    """

    def test_psf_only_figure_maps_to_rent_psf_never_rent_pcm(self):
        # No code path in brochure_enrichment.py derives one rent field from
        # the other (unlike gemini_client.compute_rent, used only by the
        # direct-upload/spreadsheet/email extraction pipelines - see this
        # module's own HIGH_RISK_UNIT_LEVEL_FIELDS docstring) - a unit that
        # explicitly states only rent_psf must fill only rent_psf.
        row = ListingRow(building="Metropolitan Wharf", floor_unit="1st", rent_pcm=None, rent_psf=None, size_sqft=2762)
        units = _brochure_units([{"building": "Metropolitan Wharf", "floor_unit": "1st", "rent_psf": 33}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.rent_psf, 33)
        self.assertIsNone(new_row.rent_pcm)
        self.assertEqual(fields, ["rent_psf"])

    def test_pcm_only_figure_maps_to_rent_pcm(self):
        row = ListingRow(building="Metropolitan Wharf", floor_unit="1st", rent_pcm=None, rent_psf=None, size_sqft=2762)
        units = _brochure_units([{"building": "Metropolitan Wharf", "floor_unit": "1st", "rent_pcm": 7595.5}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.rent_pcm, 7595.5)
        self.assertIsNone(new_row.rent_psf)
        self.assertEqual(fields, ["rent_pcm"])

    def test_non_numeric_rent_value_is_ignored_not_guessed(self):
        # A stray non-numeric value (e.g. Gemini returning a range string
        # despite the prompt's own instructions) coerces to None - never
        # guessed at, same "one bad value degrades to no match" philosophy
        # as every other coercion in this module.
        row = ListingRow(building="A", floor_unit="1st", rent_pcm=None)
        units = _brochure_units([{"building": "A", "floor_unit": "1st", "rent_pcm": "ask agent"}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.rent_pcm)
        self.assertEqual(fields, [])

    def test_consistent_new_rent_pair_with_consistent_size_is_accepted(self):
        row = ListingRow(building="A", floor_unit="1st", size_sqft=2762, rent_pcm=None, rent_psf=None)
        units = _brochure_units([
            {"building": "A", "floor_unit": "1st", "rent_pcm": 7595.5, "rent_psf": 33},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.rent_pcm, 7595.5)
        self.assertEqual(new_row.rent_psf, 33)
        self.assertIn("rent_pcm", fields)
        self.assertIn("rent_psf", fields)

    def test_candidate_pcm_inconsistent_with_existing_psf_and_size_is_rejected(self):
        # The exact real-world shape: row already has a trustworthy
        # rent_psf/size_sqft (e.g. from the original spreadsheet source),
        # only rent_pcm is blank - a brochure candidate of 33 implies an
        # annual rent of 396, wildly incompatible with the row's own
        # 2762*33=91,146 - rejected rather than stored.
        row = ListingRow(building="Metropolitan Wharf", floor_unit="1st", size_sqft=2762, rent_psf=33, rent_pcm=None)
        units = _brochure_units([{"building": "Metropolitan Wharf", "floor_unit": "1st", "rent_pcm": 33}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.rent_pcm)
        self.assertEqual(new_row.rent_psf, 33)  # untouched, still the trusted source value
        self.assertEqual(fields, [])

    def test_existing_rent_is_never_overwritten_regardless_of_brochure_consistency(self):
        row = ListingRow(building="A", floor_unit="1st", size_sqft=2762, rent_pcm=7595.5, rent_psf=33)
        units = _brochure_units([{"building": "A", "floor_unit": "1st", "rent_pcm": 33, "rent_psf": 0.14}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.rent_pcm, 7595.5)
        self.assertEqual(new_row.rent_psf, 33)
        self.assertEqual(fields, [])
        self.assertIs(new_row, row)

    def test_inconsistent_rent_does_not_block_other_unit_level_fields(self):
        # The rent gate is scoped to HIGH_RISK_UNIT_LEVEL_FIELDS only - a
        # genuinely blank, unrelated field (state_of_space) on the SAME
        # matched unit must still fill normally.
        row = ListingRow(
            building="Metropolitan Wharf", floor_unit="1st", size_sqft=2762, rent_psf=33, rent_pcm=None,
            state_of_space=None,
        )
        units = _brochure_units([{
            "building": "Metropolitan Wharf", "floor_unit": "1st", "rent_pcm": 33, "state_of_space": "Cat A",
        }])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIsNone(new_row.rent_pcm)
        self.assertEqual(new_row.state_of_space, "Cat A")
        self.assertIn("state_of_space", fields)
        self.assertNotIn("rent_pcm", fields)


class FloorplanOnlyNeverInventsWiderScopeFeaturesTests(unittest.TestCase):
    """A floorplan-only document (see FLOORPLAN_ENRICHABLE_FIELDS) must
    never be treated as a marketing brochure - even if its own raw units
    dict somehow carried extra keys, only special_features is ever read
    from it, and never at the property/building-wide level."""

    def test_only_special_features_is_ever_applied_from_a_floorplan(self):
        row = ListingRow(building="A", floor_unit="1st", contacts=None, state_of_space=None, special_features=None)
        units = [{
            "floor_unit": "1st", "special_features": "12 desks; boardroom",
            # Extra keys a malformed/unexpected Gemini response could
            # carry - never read by the floorplan path regardless.
            "contacts": "Should Never Apply, never@example.com",
            "state_of_space": "Should Never Apply",
        }]

        new_row, fields = brochure_enrichment._apply_floorplan_units_to_row(row, units)

        self.assertEqual(new_row.special_features, "12 desks; boardroom")
        self.assertIsNone(new_row.contacts)
        self.assertIsNone(new_row.state_of_space)
        self.assertEqual(fields, ["special_features"])

    def test_a_floorplan_never_has_a_property_or_building_wide_fallback(self):
        # Unlike _apply_units_to_row (brochure path), _apply_floorplan_
        # units_to_row has no property_features/building_features concept
        # at all - a floor plan drawing states neither, so there is
        # nothing to fall back to when no floor match is found.
        row = ListingRow(building="A", floor_unit="9th", special_features=None)
        units = [{"floor_unit": "1st", "special_features": "12 desks"}]  # different floor, no match

        new_row, fields = brochure_enrichment._apply_floorplan_units_to_row(row, units)

        self.assertIsNone(new_row.special_features)
        self.assertEqual(fields, [])


class ExtractFloorplanUnitsImageFormatTests(EnrichmentTestCase):
    """_extract_floorplan_units end to end - a floor plan delivered as a
    real image (confirmed real UNION shape: Box reports it as a plain
    .png) must be fetched, rendered, and read by Gemini exactly like a
    PDF, not rejected at the fetch layer."""

    def test_png_floorplan_is_fetched_rendered_and_extracted(self):
        png_response = _response(content=b"\x89PNG\r\n\x1a\nfake png bytes", content_type="image/png")
        raw = {"units": [{"floor_unit": "7th Floor", "special_features": "Meeting room; Reception; Kitchen"}]}
        with patch("brochure_enrichment.httpx.get", return_value=png_response), \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]) as mock_render, \
             patch("brochure_enrichment.extract.render_and_extract", return_value=raw) as mock_extract:
            units = brochure_enrichment._extract_floorplan_units("https://example.com/floorplan.png")

        mock_render.assert_called_once()
        mock_extract.assert_called_once()
        self.assertEqual(units[0]["special_features"], "Meeting room; Reception; Kitchen")

    def test_non_image_non_pdf_floorplan_is_still_rejected(self):
        docx_response = _response(
            content=b"not readable",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        with patch("brochure_enrichment.httpx.get", return_value=docx_response), \
             patch("brochure_enrichment.extract.render_pages") as mock_render:
            units = brochure_enrichment._extract_floorplan_units("https://example.com/floorplan.docx")

        self.assertIsNone(units)
        mock_render.assert_not_called()


class MatchFloorplanUnitTests(unittest.TestCase):
    """_match_floorplan_unit - a single-unit floor plan must never be
    applied to a row whose own stated floor genuinely conflicts with it."""

    def test_single_unit_with_matching_floor_applies(self):
        row = ListingRow(building="A", floor_unit="2nd Floor")
        units = [{"floor_unit": "2nd Floor", "special_features": "12 desks; boardroom"}]

        matched = brochure_enrichment._match_floorplan_unit(row, units)

        self.assertEqual(matched["special_features"], "12 desks; boardroom")

    def test_single_unit_with_matching_floor_number_applies(self):
        # "2nd" (row) vs "2nd Floor" (floor plan) - same floor-number
        # fallback tier _match_unit already uses.
        row = ListingRow(building="A", floor_unit="2nd")
        units = [{"floor_unit": "2nd Floor", "special_features": "12 desks"}]

        matched = brochure_enrichment._match_floorplan_unit(row, units)

        self.assertEqual(matched["special_features"], "12 desks")

    def test_single_unit_with_conflicting_floor_never_applies(self):
        # The real bug this guards against: a floor plan naming a
        # DIFFERENT floor than the row must never be applied merely
        # because it's the only unit extracted from that document.
        row = ListingRow(building="A", floor_unit="2nd Floor")
        units = [{"floor_unit": "3rd Floor", "special_features": "12 desks"}]

        self.assertIsNone(brochure_enrichment._match_floorplan_unit(row, units))

    def test_single_unit_with_no_floor_identity_of_its_own_still_applies(self):
        row = ListingRow(building="A", floor_unit="2nd Floor")
        units = [{"floor_unit": None, "special_features": "12 desks"}]

        matched = brochure_enrichment._match_floorplan_unit(row, units)

        self.assertEqual(matched["special_features"], "12 desks")

    def test_single_unit_applies_when_the_row_itself_states_no_floor(self):
        row = ListingRow(building="A", floor_unit=None)
        units = [{"floor_unit": "3rd Floor", "special_features": "12 desks"}]

        matched = brochure_enrichment._match_floorplan_unit(row, units)

        self.assertEqual(matched["special_features"], "12 desks")

    def test_several_units_still_resolve_by_exact_floor_text(self):
        row = ListingRow(building="A", floor_unit="3rd Floor")
        units = [
            {"floor_unit": "2nd Floor", "special_features": "Wrong"},
            {"floor_unit": "3rd Floor", "special_features": "Right"},
        ]

        matched = brochure_enrichment._match_floorplan_unit(row, units)

        self.assertEqual(matched["special_features"], "Right")

    def test_several_units_with_no_row_floor_match_stay_unresolved(self):
        row = ListingRow(building="A", floor_unit="9th Floor")
        units = [
            {"floor_unit": "2nd Floor", "special_features": "One"},
            {"floor_unit": "3rd Floor", "special_features": "Two"},
        ]

        self.assertIsNone(brochure_enrichment._match_floorplan_unit(row, units))

    def test_no_units_returns_none(self):
        row = ListingRow(building="A", floor_unit="2nd Floor")
        self.assertIsNone(brochure_enrichment._match_floorplan_unit(row, []))


class EnrichRowTests(EnrichmentTestCase):
    def _mock_units(self, units):
        return patch("brochure_enrichment._extract_brochure_units", return_value=units)

    def test_blank_special_features_filled_from_matching_brochure(self):
        row = ListingRow(
            building="16 Dufour's Place", provider="UNION", floor_unit="3rd Floor",
            size_sqft=1200, rent_pcm=15000, brochure_link="https://example.com/brochure.pdf",
            special_features=None,
        )
        units = [{
            "building": "16 Dufour's Place", "floor_unit": "3rd Floor",
            "special_features": "Private terrace; showers; cycle storage",
        }]

        with self._mock_units(units):
            new_row, fields = brochure_enrichment.enrich_row(row)

        self.assertEqual(new_row.special_features, "Private terrace; showers; cycle storage")
        self.assertEqual(fields, ["special_features"])

    def test_populated_special_features_gets_brochure_text_appended(self):
        # A row's pre-existing special_features (even something non-
        # descriptive, e.g. a bare "U/O" status marker in some real
        # provider files) is kept as the first, most-specific segment and
        # combined with the brochure's own text, rather than the brochure
        # combine being skipped entirely just because the field wasn't
        # blank to begin with.
        row = ListingRow(
            building="16 Dufour's Place", floor_unit="3rd Floor",
            brochure_link="https://example.com/brochure.pdf",
            special_features="Existing genuine description",
        )
        units = [{
            "building": "16 Dufour's Place", "floor_unit": "3rd Floor",
            "special_features": "Completely different brochure text",
        }]

        with self._mock_units(units):
            new_row, fields = brochure_enrichment.enrich_row(row)

        self.assertEqual(new_row.special_features, "Existing genuine description; Completely different brochure text")
        self.assertEqual(fields, ["special_features"])

    def test_blank_state_of_space_filled_from_explicit_wording(self):
        row = ListingRow(
            building="16 Dufour's Place", floor_unit="3rd Floor",
            brochure_link="https://example.com/brochure.pdf", state_of_space=None,
        )
        units = [{"building": "16 Dufour's Place", "floor_unit": "3rd Floor", "state_of_space": "Fully Furnished"}]

        with self._mock_units(units):
            new_row, fields = brochure_enrichment.enrich_row(row)

        self.assertEqual(new_row.state_of_space, "Fully Furnished")

    def test_populated_state_of_space_not_overwritten(self):
        row = ListingRow(
            building="16 Dufour's Place", floor_unit="3rd Floor",
            brochure_link="https://example.com/brochure.pdf", state_of_space="Cat A",
        )
        units = [{"building": "16 Dufour's Place", "floor_unit": "3rd Floor", "state_of_space": "Shell & Core"}]

        with self._mock_units(units):
            new_row, fields = brochure_enrichment.enrich_row(row)

        self.assertEqual(new_row.state_of_space, "Cat A")

    def test_fully_furnished_wording_preserved_verbatim(self):
        row = ListingRow(
            building="A", floor_unit="1st", brochure_link="https://example.com/brochure.pdf", state_of_space=None,
        )
        units = [{"building": "A", "floor_unit": "1st", "state_of_space": "Fully Furnished"}]

        with self._mock_units(units):
            new_row, _ = brochure_enrichment.enrich_row(row)

        self.assertEqual(new_row.state_of_space, "Fully Furnished")

    def test_no_fit_out_wording_leaves_state_of_space_blank(self):
        row = ListingRow(
            building="A", floor_unit="1st", brochure_link="https://example.com/brochure.pdf", state_of_space=None,
        )
        units = [{"building": "A", "floor_unit": "1st", "state_of_space": None, "special_features": "Desks and chairs included"}]

        with self._mock_units(units):
            new_row, fields = brochure_enrichment.enrich_row(row)

        self.assertIsNone(new_row.state_of_space)
        self.assertNotIn("state_of_space", fields)

    def test_spreadsheet_rent_never_overwritten_by_different_brochure_rent(self):
        row = ListingRow(
            building="A", floor_unit="1st", brochure_link="https://example.com/brochure.pdf",
            rent_psf=175, special_features=None,
        )
        units = [{"building": "A", "floor_unit": "1st", "rent_psf": 165, "special_features": "Kitchen"}]

        with self._mock_units(units):
            new_row, fields = brochure_enrichment.enrich_row(row)

        self.assertEqual(new_row.rent_psf, 175)
        self.assertEqual(fields, ["special_features"])

    def test_normal_pdf_brochure_with_consistent_rent_still_fills(self):
        # Confirms the new rent-consistency gate (see RentConsistencyApplic-
        # ationTests/CanvaEndToEndEnrichmentTests) doesn't regress the
        # ordinary, non-Canva PDF-brochure enrichment path this whole module
        # exists for - a genuinely consistent, freshly-stated rent trio from
        # a plain PDF brochure link still fills normally.
        row = ListingRow(
            building="A", floor_unit="1st", brochure_link="https://example.com/brochure.pdf",
            size_sqft=2762, rent_pcm=None, rent_psf=None,
        )
        units = [{"building": "A", "floor_unit": "1st", "rent_pcm": 7595.5, "rent_psf": 33}]

        with self._mock_units(units):
            new_row, fields = brochure_enrichment.enrich_row(row)

        self.assertEqual(new_row.rent_pcm, 7595.5)
        self.assertEqual(new_row.rent_psf, 33)
        self.assertIn("rent_pcm", fields)
        self.assertIn("rent_psf", fields)

    def test_spreadsheet_size_never_overwritten_by_different_brochure_size(self):
        row = ListingRow(
            building="A", floor_unit="1st", brochure_link="https://example.com/brochure.pdf",
            size_sqft=1200, special_features=None,
        )
        units = [{"building": "A", "floor_unit": "1st", "size_sqft": 5000, "special_features": "Kitchen"}]

        with self._mock_units(units):
            new_row, _ = brochure_enrichment.enrich_row(row)

        self.assertEqual(new_row.size_sqft, 1200)

    def test_spreadsheet_provider_never_overwritten_by_brochure_branding(self):
        row = ListingRow(
            building="A", provider="UNION", floor_unit="1st",
            brochure_link="https://example.com/brochure.pdf", special_features=None,
        )
        units = [{"building": "A", "floor_unit": "1st", "provider": "SomeOtherBrand", "special_features": "Kitchen"}]

        with self._mock_units(units):
            new_row, _ = brochure_enrichment.enrich_row(row)

        self.assertEqual(new_row.provider, "UNION")

    def test_ambiguous_floor_matching_produces_no_enrichment(self):
        row = ListingRow(
            building="A", floor_unit="Suite Z", brochure_link="https://example.com/brochure.pdf",
            special_features=None,
        )
        units = [
            {"building": "A", "floor_unit": "1st Floor", "special_features": "One"},
            {"building": "A", "floor_unit": "2nd Floor", "special_features": "Two"},
        ]

        with self._mock_units(units):
            new_row, fields = brochure_enrichment.enrich_row(row)

        self.assertIsNone(new_row.special_features)
        self.assertEqual(fields, [])

    def test_row_with_nothing_missing_never_triggers_extraction(self):
        # Every field in ENRICHABLE_FIELDS populated, not just the original
        # three - a row missing even one of the newer fields (address_1,
        # postcode, submarket, size_sqft, desks_max, rent_pcm, rent_psf)
        # now correctly needs_enrichment (see NeedsEnrichmentTests), so this
        # "truly nothing missing" case must give every one of them a value.
        row = ListingRow(
            building="A", floor_unit="1st", brochure_link="https://example.com/brochure.pdf",
            address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
            size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
            special_features="Already have this", state_of_space="Cat A", contacts="Jane, jane@x.com",
        )
        with patch("brochure_enrichment._extract_brochure_units") as mock_extract:
            new_row, fields = brochure_enrichment.enrich_row(row)

        mock_extract.assert_not_called()
        self.assertIs(new_row, row)
        self.assertEqual(fields, [])

    def test_ineligible_url_never_triggers_extraction(self):
        row = ListingRow(
            building="A", floor_unit="1st", brochure_link="https://sharepoint.com/floorplans/a.pdf",
            special_features=None,
        )
        with patch("brochure_enrichment._extract_brochure_units") as mock_extract:
            brochure_enrichment.enrich_row(row)

        mock_extract.assert_not_called()

    def test_no_brochure_link_never_triggers_extraction(self):
        row = ListingRow(building="A", floor_unit="1st", brochure_link=None, special_features=None)
        with patch("brochure_enrichment._extract_brochure_units") as mock_extract:
            brochure_enrichment.enrich_row(row)

        mock_extract.assert_not_called()

    def test_broken_brochure_leaves_row_unchanged(self):
        row = ListingRow(
            building="A", floor_unit="1st", brochure_link="https://example.com/brochure.pdf",
            special_features=None,
        )
        with patch("brochure_enrichment._extract_brochure_units", return_value=None):
            new_row, fields = brochure_enrichment.enrich_row(row)

        self.assertIs(new_row, row)
        self.assertEqual(fields, [])


class EnrichRowsBatchTests(EnrichmentTestCase):
    def test_several_rows_sharing_one_brochure_only_extract_once(self):
        rows = [
            ListingRow(
                building="28 Lime Street", floor_unit=f"{i}th Floor",
                brochure_link="https://example.com/shared.pdf", special_features=None,
            )
            for i in (2, 3, 4)
        ]
        units = [
            {"building": "28 Lime Street", "floor_unit": "2th Floor", "special_features": "Two"},
            {"building": "28 Lime Street", "floor_unit": "3th Floor", "special_features": "Three"},
            {"building": "28 Lime Street", "floor_unit": "4th Floor", "special_features": "Four"},
        ]

        with patch("brochure_enrichment.httpx.get", return_value=_response()) as mock_get, \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value={"units": units}) as mock_extract:
            enriched, log = brochure_enrichment.enrich_rows(rows)

        mock_get.assert_called_once()
        mock_extract.assert_called_once()
        self.assertEqual(len(log), 3)
        self.assertEqual([r.special_features for r in enriched], ["Two", "Three", "Four"])

    def test_extract_brochure_units_end_to_end_carries_property_and_building_features(self):
        # Proves the REAL _extract_brochure_units (not a test-only helper)
        # correctly turns a raw Gemini dict's new property_features/
        # building_features/contacts keys into the extra attributes
        # _apply_units_to_row reads - only render_and_extract (the actual
        # Gemini call) is mocked here, exactly like the real network path.
        rows = [
            ListingRow(building="The Canal Building", floor_unit="4th Floor", brochure_link="https://example.com/rw.pdf", special_features=None, contacts=None),
            ListingRow(building="The Packing House", floor_unit="Ground Floor", brochure_link="https://example.com/rw.pdf", special_features=None, contacts=None),
        ]
        raw = {
            "provider": None,
            "contacts": "Jane Smith, jane@agent.com, 020 7946 0000",
            "property_features": "WiredScore Platinum; BREEAM Excellent",
            "building_features": [
                {"building": "The Canal Building", "features": "Exposed beams; canalside frontage"},
                {"building": "The Packing House", "features": "Stunning rooftop terrace"},
            ],
            "units": [
                {"building": "The Canal Building", "floor_unit": "4th Floor", "special_features": None},
                {"building": "The Packing House", "floor_unit": "Ground Floor", "special_features": None},
            ],
        }

        with patch("brochure_enrichment.httpx.get", return_value=_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["fake_image"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value=raw):
            enriched, log = brochure_enrichment.enrich_rows(rows)

        self.assertEqual(enriched[0].special_features, "Exposed beams; canalside frontage; WiredScore Platinum; BREEAM Excellent")
        self.assertEqual(enriched[1].special_features, "Stunning rooftop terrace; WiredScore Platinum; BREEAM Excellent")
        self.assertEqual(enriched[0].contacts, "Jane Smith, jane@agent.com, 020 7946 0000")
        self.assertEqual(enriched[1].contacts, "Jane Smith, jane@agent.com, 020 7946 0000")

    def test_building_level_feature_enriches_multiple_matching_floors(self):
        rows = [
            ListingRow(
                building="28 Lime Street", floor_unit=f, brochure_link="https://example.com/shared.pdf",
                special_features=None,
            )
            for f in ("2nd Floor", "4th Floor")
        ]
        # Only ONE brochure unit for this building at all - a whole-building
        # description, safely shared across both matching floors.
        units = [{"building": "28 Lime Street", "floor_unit": None, "special_features": "Manned reception; showers"}]

        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            enriched, log = brochure_enrichment.enrich_rows(rows)

        self.assertEqual([r.special_features for r in enriched], ["Manned reception; showers"] * 2)
        self.assertEqual(len(log), 2)

    def test_floor_specific_feature_only_enriches_correct_floor(self):
        rows = [
            ListingRow(
                building="28 Lime Street", floor_unit=f, brochure_link="https://example.com/shared.pdf",
                special_features=None,
            )
            for f in ("2nd Floor", "4th Floor")
        ]
        units = [
            {"building": "28 Lime Street", "floor_unit": "2nd Floor", "special_features": "2nd floor only"},
            {"building": "28 Lime Street", "floor_unit": "4th Floor", "special_features": "4th floor only"},
        ]

        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            enriched, _ = brochure_enrichment.enrich_rows(rows)

        self.assertEqual(enriched[0].special_features, "2nd floor only")
        self.assertEqual(enriched[1].special_features, "4th floor only")

    def test_multi_building_brochure_does_not_cross_contaminate_rows(self):
        rows = [
            ListingRow(building="Building A", brochure_link="https://example.com/shared.pdf", special_features=None),
            ListingRow(building="Building B", brochure_link="https://example.com/shared.pdf", special_features=None),
        ]
        units = [
            {"building": "Building A", "floor_unit": None, "special_features": "A only"},
            {"building": "Building B", "floor_unit": None, "special_features": "B only"},
        ]

        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            enriched, _ = brochure_enrichment.enrich_rows(rows)

        self.assertEqual(enriched[0].special_features, "A only")
        self.assertEqual(enriched[1].special_features, "B only")

    def test_one_rows_broken_brochure_does_not_affect_other_rows(self):
        rows = [
            ListingRow(building="A", brochure_link="https://example.com/broken.pdf", special_features=None),
            ListingRow(building="B", brochure_link="https://example.com/good.pdf", special_features=None),
        ]

        def _fake_units(url):
            return None if "broken" in url else [{"building": "B", "floor_unit": None, "special_features": "Good features"}]

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake_units):
            enriched, log = brochure_enrichment.enrich_rows(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(enriched[1].special_features, "Good features")
        self.assertEqual(len(log), 1)


class EligibleRowsAndBrochuresTests(unittest.TestCase):
    def test_returns_eligible_rows_and_unique_urls_in_first_encounter_order(self):
        rows = [
            ListingRow(building="A", floor_unit="1st", brochure_link="https://example.com/a.pdf", special_features=None),
            ListingRow(building="A", floor_unit="2nd", brochure_link="https://example.com/a.pdf", special_features=None),
            # Every ENRICHABLE_FIELDS entry populated - genuinely nothing
            # missing, so this row must stay excluded (see NeedsEnrichmentTests).
            ListingRow(
                building="B", floor_unit="1st", brochure_link="https://example.com/b.pdf",
                address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
                size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
                special_features="Already there", state_of_space="Cat A", contacts="Jane, jane@x.com",
            ),
            ListingRow(building="C", floor_unit="1st", brochure_link=None, special_features=None),
        ]
        eligible, urls = brochure_enrichment.eligible_rows_and_brochures(rows)
        self.assertEqual(len(eligible), 2)
        self.assertEqual(urls, ["https://example.com/a.pdf"])

    def test_no_eligible_rows_returns_empty(self):
        rows = [ListingRow(building="A", special_features="x", state_of_space="y", contacts="z")]
        eligible, urls = brochure_enrichment.eligible_rows_and_brochures(rows)
        self.assertEqual(eligible, [])
        self.assertEqual(urls, [])


class EnrichRowsGroupedTests(EnrichmentTestCase):
    def test_ten_rows_sharing_one_brochure_extract_exactly_once(self):
        rows = [
            ListingRow(
                building="28 Lime Street", floor_unit=f"{i}th Floor",
                brochure_link="https://example.com/shared.pdf", special_features=None,
            )
            for i in range(10)
        ]
        units = [{"building": "28 Lime Street", "floor_unit": None, "special_features": "Shared feature"}]

        with patch("brochure_enrichment._extract_brochure_units", return_value=units) as mock_units:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        mock_units.assert_called_once_with("https://example.com/shared.pdf")
        self.assertEqual(len(log), 10)
        self.assertTrue(all(r.special_features == "Shared feature" for r in enriched))
        self.assertEqual(stats["unique_brochures_considered"], 1)
        self.assertEqual(stats["rows_enriched"], 10)

    def test_more_than_64_unique_brochures_still_one_extraction_each(self):
        # Directly exercises the real confirmed bug: the module-level
        # lru_cache(maxsize=64) alone would evict and re-call past the 64th
        # distinct URL. enrich_rows_grouped's own per-run worklist (the
        # FULL deduplicated URL list, built up front - see its own
        # docstring) must not depend on that cache surviving.
        n = 80
        rows = [
            ListingRow(
                building=f"Building {i}", floor_unit="1st",
                brochure_link=f"https://example.com/{i}.pdf", special_features=None,
            )
            for i in range(n)
        ]
        call_log = []

        def _fake_extract(url):
            call_log.append(url)
            idx = url.split("/")[-1].split(".")[0]
            return [{"building": f"Building {idx}", "floor_unit": None, "special_features": f"Feature {idx}"}]

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake_extract):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(len(call_log), n)
        self.assertEqual(len(set(call_log)), n)
        self.assertEqual(stats["unique_brochures_considered"], n)
        self.assertEqual(len(log), n)

    def test_cache_eviction_cannot_create_a_duplicate_call_within_one_run(self):
        # Simulates the real confirmed 200 Aldersgate scenario: the SAME
        # brochure link referenced by two rows, more than 64 OTHER unique
        # links apart. The real functools.lru_cache(maxsize=64) underneath
        # _extract_brochure_units would itself evict and re-call for the
        # second reference; enrich_rows_grouped must not, since it only
        # ever calls _extract_brochure_units once per distinct URL in its
        # own per-run worklist regardless of what the cache does.
        shared_url = "https://example.com/shared.pdf"
        rows = [ListingRow(building="Shared Building", floor_unit="1st", brochure_link=shared_url, special_features=None)]
        for i in range(70):
            rows.append(ListingRow(
                building=f"Building {i}", floor_unit="1st",
                brochure_link=f"https://example.com/{i}.pdf", special_features=None,
            ))
        rows.append(ListingRow(building="Shared Building", floor_unit="2nd", brochure_link=shared_url, special_features=None))

        call_count = {}

        def _fake_fetch(url):
            call_count[url] = call_count.get(url, 0) + 1
            if url == shared_url:
                return [{"building": "Shared Building", "floor_unit": None, "special_features": "Shared"}]
            return [{"building": url, "floor_unit": None, "special_features": "X"}]

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake_fetch):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(call_count[shared_url], 1)
        self.assertEqual(enriched[0].special_features, "Shared")
        self.assertEqual(enriched[-1].special_features, "Shared")

    def test_different_brochures_remain_separate(self):
        rows = [
            ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None),
            ListingRow(building="B", brochure_link="https://example.com/b.pdf", special_features=None),
        ]

        def _fake(url):
            if "a.pdf" in url:
                return [{"building": "A", "floor_unit": None, "special_features": "A-only"}]
            return [{"building": "B", "floor_unit": None, "special_features": "B-only"}]

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(enriched[0].special_features, "A-only")
        self.assertEqual(enriched[1].special_features, "B-only")

    def test_broken_brochure_does_not_fail_the_batch(self):
        rows = [
            ListingRow(building="A", brochure_link="https://example.com/broken.pdf", special_features=None),
            ListingRow(building="B", brochure_link="https://example.com/good.pdf", special_features=None),
        ]

        def _fake(url):
            return None if "broken" in url else [{"building": "B", "floor_unit": None, "special_features": "Good"}]

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(enriched[1].special_features, "Good")
        self.assertEqual(stats["brochures_unavailable"], 1)
        self.assertEqual(stats["brochures_read_ok"], 1)

    def test_unexpected_exception_in_one_brochure_does_not_fail_the_batch(self):
        rows = [
            ListingRow(building="A", brochure_link="https://example.com/raises.pdf", special_features=None),
            ListingRow(building="B", brochure_link="https://example.com/good.pdf", special_features=None),
        ]

        def _fake(url):
            if "raises" in url:
                raise RuntimeError("boom")
            return [{"building": "B", "floor_unit": None, "special_features": "Good"}]

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(enriched[1].special_features, "Good")

    def test_multiple_independent_failures_do_not_stop_the_batch(self):
        # Several DIFFERENT failure modes scattered through the run - a
        # raised exception, a None (unavailable) result, and a malformed
        # units entry - none of them may stop any OTHER brochure from
        # being attempted, and every genuinely good one must still
        # succeed regardless of how many bad ones surround it.
        rows = [
            ListingRow(building=f"B{i}", brochure_link=f"https://example.com/{i}.pdf", special_features=None)
            for i in range(6)
        ]

        def _fake(url):
            if "0.pdf" in url:
                raise RuntimeError("boom")
            if "2.pdf" in url:
                return None
            if "4.pdf" in url:
                return [None, "also not a dict"]
            idx = url.split("/")[-1].split(".")[0]
            return [{"building": f"B{idx}", "floor_unit": None, "special_features": f"Feature {idx}"}]

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)  # raised
        self.assertIsNone(enriched[2].special_features)  # unavailable
        self.assertIsNone(enriched[4].special_features)  # malformed, no confident match
        self.assertEqual(enriched[1].special_features, "Feature 1")
        self.assertEqual(enriched[3].special_features, "Feature 3")
        self.assertEqual(enriched[5].special_features, "Feature 5")
        self.assertEqual(stats["unique_brochures_considered"], 6)
        self.assertEqual(stats["rows_enriched"], 3)

    def test_no_confident_match_leaves_row_unchanged(self):
        rows = [ListingRow(building="A", floor_unit="Suite Z", brochure_link="https://example.com/x.pdf", special_features=None)]
        units = [
            {"building": "A", "floor_unit": "1st Floor", "special_features": "One"},
            {"building": "A", "floor_unit": "2nd Floor", "special_features": "Two"},
        ]

        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(log, [])

    def test_progress_callback_called_once_per_unique_brochure(self):
        # done starts at 1 (not 0) here - unlike the old sequential version,
        # every unique brochure's work is dispatched to the worker pool up
        # front (see enrich_rows_grouped's own docstring), so there's no
        # single well-defined "about to start #1" moment to report a 0
        # against; the first callback IS the first completion.
        rows = [
            ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None),
            ListingRow(building="B", brochure_link="https://example.com/b.pdf", special_features=None),
        ]
        calls = []
        with patch("brochure_enrichment._extract_brochure_units", return_value=[]):
            brochure_enrichment.enrich_rows_grouped(rows, progress_callback=lambda d, t, l: calls.append((d, t)))

        self.assertEqual(sorted(calls), [(1, 2), (2, 2)])

    def test_progress_count_is_based_on_unique_brochures_not_rows(self):
        rows = [
            ListingRow(
                building="A", floor_unit=str(i), brochure_link="https://example.com/shared.pdf", special_features=None,
            )
            for i in range(5)
        ]
        totals = set()
        with patch(
            "brochure_enrichment._extract_brochure_units",
            return_value=[{"building": "A", "floor_unit": None, "special_features": "F"}],
        ):
            brochure_enrichment.enrich_rows_grouped(rows, progress_callback=lambda d, t, l: totals.add(t))

        self.assertEqual(totals, {1})

    def test_checkpoint_callback_fires_on_the_final_brochure_even_below_the_interval(self):
        rows = [ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None)]
        checkpoints = []
        with patch(
            "brochure_enrichment._extract_brochure_units",
            return_value=[{"building": "A", "floor_unit": None, "special_features": "F"}],
        ):
            brochure_enrichment.enrich_rows_grouped(rows, checkpoint_callback=checkpoints.append)

        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0][0].special_features, "F")

    def test_trim_memory_is_called_once_per_completed_brochure(self):
        # Direct response to a confirmed, measured finding (see
        # _trim_memory's own docstring): store_shrink(100) alone still
        # leaves real residual RSS growth across repeated renders - this
        # confirms the mitigation is actually wired into the main loop,
        # once per brochure, not just present as an unused helper.
        rows = [
            ListingRow(building=f"B{i}", brochure_link=f"https://example.com/{i}.pdf", special_features=None)
            for i in range(4)
        ]
        with patch("brochure_enrichment._extract_brochure_units", return_value=[]), \
             patch("brochure_enrichment._trim_memory") as mock_trim:
            brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(mock_trim.call_count, 4)

    def test_rows_with_nothing_missing_are_excluded_from_the_run(self):
        # Every ENRICHABLE_FIELDS entry populated - see EnrichRowTests' own
        # identical update for why this now needs more than the original
        # three fields to be genuinely "nothing missing".
        rows = [ListingRow(
            building="A", brochure_link="https://example.com/a.pdf",
            address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
            floor_unit="1st", size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
            special_features="Done", state_of_space="Cat A", contacts="Jane, jane@x.com",
        )]
        with patch("brochure_enrichment._extract_brochure_units") as mock_units:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        mock_units.assert_not_called()
        self.assertEqual(stats["unique_brochures_considered"], 0)
        self.assertEqual(stats["rows_eligible"], 0)

    def test_summary_stats_are_accurate(self):
        rows = [
            ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None),
            ListingRow(building="B", brochure_link="https://example.com/b.pdf", special_features=None),
            ListingRow(building="C", brochure_link="https://example.com/c.pdf", special_features=None),
        ]

        def _fake(url):
            if "a.pdf" in url:
                return [{"building": "A", "floor_unit": None, "special_features": "A feature"}]
            if "b.pdf" in url:
                return [{"building": "NoMatchHere", "floor_unit": None, "special_features": "irrelevant"}]
            return None  # c.pdf unavailable

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(stats["unique_brochures_considered"], 3)
        self.assertEqual(stats["brochures_read_ok"], 2)
        self.assertEqual(stats["brochures_unavailable"], 1)
        self.assertEqual(stats["rows_eligible"], 3)
        self.assertEqual(stats["rows_enriched"], 1)


class EnrichRowsGroupedConcurrencyTests(EnrichmentTestCase):
    def test_never_exceeds_the_configured_worker_limit(self):
        max_workers = 3
        lock = threading.Lock()
        state = {"current": 0, "max_seen": 0}

        rows = [
            ListingRow(building=f"B{i}", brochure_link=f"https://example.com/{i}.pdf", special_features=None)
            for i in range(12)
        ]

        def _fake(url):
            with lock:
                state["current"] += 1
                state["max_seen"] = max(state["max_seen"], state["current"])
            time.sleep(0.05)
            with lock:
                state["current"] -= 1
            return [{"building": url, "floor_unit": None, "special_features": "x"}]

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake):
            brochure_enrichment.enrich_rows_grouped(rows, max_workers=max_workers)

        self.assertLessEqual(state["max_seen"], max_workers)
        # Also confirms real overlap actually happened (not accidentally
        # serialized down to 1) - otherwise this test would pass for the
        # wrong reason.
        self.assertGreater(state["max_seen"], 1)

    def test_progress_and_checkpoint_callbacks_never_run_on_a_worker_thread(self):
        # Streamlit's own APIs (what a real caller's progress_callback/
        # checkpoint_callback would call - see app.py) are documented as
        # unsafe to call off the main script thread. Every callback must
        # therefore run in the SAME thread that called enrich_rows_grouped,
        # never inside a worker (see that function's own docstring on why
        # this holds by construction).
        main_thread = threading.current_thread()
        seen_threads = []

        rows = [
            ListingRow(building=f"B{i}", brochure_link=f"https://example.com/{i}.pdf", special_features=None)
            for i in range(8)
        ]

        def _fake(url):
            return [{"building": url, "floor_unit": None, "special_features": "x"}]

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake):
            brochure_enrichment.enrich_rows_grouped(
                rows,
                progress_callback=lambda d, t, l: seen_threads.append(threading.current_thread()),
                checkpoint_callback=lambda r: seen_threads.append(threading.current_thread()),
                max_workers=3,
            )

        self.assertTrue(seen_threads)
        self.assertTrue(all(t is main_thread for t in seen_threads))

    def test_results_apply_correctly_regardless_of_completion_order(self):
        # Fake fetch sleeps different amounts per URL so real completion
        # order is deliberately scrambled relative to submission order -
        # per-row result correctness must never depend on which finishes
        # first (see enrich_rows_grouped's own docstring on why checkpoint/
        # result application follows completion order, not unique_urls'
        # own order).
        rows = [
            ListingRow(building="Slow", brochure_link="https://example.com/slow.pdf", special_features=None),
            ListingRow(building="Fast", brochure_link="https://example.com/fast.pdf", special_features=None),
            ListingRow(building="Medium", brochure_link="https://example.com/medium.pdf", special_features=None),
        ]

        def _fake(url):
            if "slow" in url:
                time.sleep(0.15)
                return [{"building": "Slow", "floor_unit": None, "special_features": "Slow feature"}]
            if "fast" in url:
                return [{"building": "Fast", "floor_unit": None, "special_features": "Fast feature"}]
            time.sleep(0.05)
            return [{"building": "Medium", "floor_unit": None, "special_features": "Medium feature"}]

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows, max_workers=3)

        result_map = {r.building: r.special_features for r in enriched}
        self.assertEqual(result_map, {"Slow": "Slow feature", "Fast": "Fast feature", "Medium": "Medium feature"})

    def test_progress_reaches_total_even_when_some_brochures_fail(self):
        rows = [
            ListingRow(building=f"B{i}", brochure_link=f"https://example.com/{i}.pdf", special_features=None)
            for i in range(5)
        ]

        def _fake(url):
            if "2.pdf" in url or "3.pdf" in url:
                return None
            return [{"building": url, "floor_unit": None, "special_features": "x"}]

        calls = []
        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake):
            brochure_enrichment.enrich_rows_grouped(rows, progress_callback=lambda d, t, l: calls.append((d, t)))

        self.assertIn((5, 5), calls)  # reached total despite 2 real failures

    def test_checkpoints_reflect_a_consistent_partial_state_under_concurrency(self):
        # Every checkpoint snapshot must be a fully self-consistent
        # rows list (any row whose brochure has completed already
        # reflects that; anything not yet completed is still the
        # original row) - never a half-written/torn state, which is
        # exactly what the "only the consumer thread ever mutates
        # `current`" design (see enrich_rows_grouped's own docstring)
        # is meant to guarantee.
        rows = [
            ListingRow(building=f"B{i}", brochure_link=f"https://example.com/{i}.pdf", special_features=None)
            for i in range(12)
        ]

        def _fake(url):
            return [{"building": url, "floor_unit": None, "special_features": "filled"}]

        snapshots = []
        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake):
            brochure_enrichment.enrich_rows_grouped(
                rows, checkpoint_callback=lambda r: snapshots.append(list(r)), max_workers=3,
            )

        self.assertTrue(snapshots)
        for snap in snapshots:
            for row in snap:
                self.assertIn(row.special_features, (None, "filled"))

    def test_worker_count_of_one_is_still_correct(self):
        # max_workers is a performance knob, never a correctness one -
        # confirms the degenerate case (effectively sequential) produces
        # the exact same result as any other worker count.
        rows = [
            ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None),
            ListingRow(building="B", brochure_link="https://example.com/b.pdf", special_features=None),
        ]

        def _fake(url):
            name = "A" if "a.pdf" in url else "B"
            return [{"building": name, "floor_unit": None, "special_features": f"{name} feature"}]

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows, max_workers=1)

        self.assertEqual(enriched[0].special_features, "A feature")
        self.assertEqual(enriched[1].special_features, "B feature")
        self.assertEqual(stats["unique_brochures_considered"], 2)


class EnrichRowsGroupedResumeTests(EnrichmentTestCase):
    """
    already_processed/stats["processed_urls"]/url_checkpoint_callback -
    lets a caller resume an interrupted run without re-fetching (and
    re-billing Gemini for) a brochure already successfully checked. See
    enrich_rows_grouped's own docstring: a blank special_features value can
    never itself prove a brochure was never checked, so this is tracked by
    explicit per-URL state, never inferred from row content.
    """

    def _rows(self, n, prefix="B"):
        return [
            ListingRow(building=f"{prefix}{i}", brochure_link=f"https://example.com/{prefix}{i}.pdf", special_features=None)
            for i in range(n)
        ]

    def test_urls_already_marked_ok_are_never_refetched(self):
        rows = self._rows(3)
        already_processed = {"https://example.com/B0.pdf": "ok", "https://example.com/B1.pdf": "ok"}

        def _fake(url):
            return [{"building": url, "floor_unit": None, "special_features": "checked"}]

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake) as mock_extract:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows, already_processed=already_processed)

        mock_extract.assert_called_once_with("https://example.com/B2.pdf")
        self.assertEqual(stats["processed_urls"], {"https://example.com/B2.pdf": "ok"})

    def test_skipped_rows_are_left_completely_unchanged(self):
        rows = self._rows(2)
        rows[0] = rows[0].model_copy(update={"special_features": "Already checked, genuinely nothing more"})
        already_processed = {"https://example.com/B0.pdf": "ok"}

        with patch("brochure_enrichment._extract_brochure_units", return_value=[
            {"building": "B1", "floor_unit": None, "special_features": "New"},
        ]):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows, already_processed=already_processed)

        self.assertIs(enriched[0], rows[0])
        self.assertEqual(enriched[0].special_features, "Already checked, genuinely nothing more")
        self.assertEqual(enriched[1].special_features, "New")

    def test_checked_with_no_applicable_features_is_still_recorded_as_ok(self):
        # An empty units list (or one with nothing matching) is still a
        # SUCCESSFUL check - never "unavailable" - see _extract_brochure_
        # units' own None-vs-[] distinction.
        rows = self._rows(1)

        with patch("brochure_enrichment._extract_brochure_units", return_value=[]):
            _, _, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(stats["processed_urls"], {"https://example.com/B0.pdf": "ok"})

    def test_failed_brochure_is_recorded_as_unavailable_not_ok(self):
        rows = self._rows(1)

        with patch("brochure_enrichment._extract_brochure_units", return_value=None):
            _, _, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(stats["processed_urls"], {"https://example.com/B0.pdf": "unavailable"})

    def test_previously_unavailable_url_is_retried_not_skipped(self):
        rows = self._rows(1)
        already_processed = {"https://example.com/B0.pdf": "unavailable"}

        with patch("brochure_enrichment._extract_brochure_units", return_value=[
            {"building": "B0", "floor_unit": None, "special_features": "Recovered"},
        ]) as mock_extract:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows, already_processed=already_processed)

        mock_extract.assert_called_once()
        self.assertEqual(enriched[0].special_features, "Recovered")
        self.assertEqual(stats["processed_urls"], {"https://example.com/B0.pdf": "ok"})

    def test_url_checkpoint_callback_fires_with_cumulative_state(self):
        rows = self._rows(1)
        already_processed = {"https://example.com/other.pdf": "ok"}
        seen = []

        with patch("brochure_enrichment._extract_brochure_units", return_value=[
            {"building": "B0", "floor_unit": None, "special_features": "x"},
        ]):
            brochure_enrichment.enrich_rows_grouped(
                rows, already_processed=already_processed, url_checkpoint_callback=seen.append,
            )

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], {"https://example.com/other.pdf": "ok", "https://example.com/B0.pdf": "ok"})

    def test_url_checkpoint_callback_does_not_break_callers_using_only_checkpoint_callback(self):
        rows = self._rows(1)
        row_checkpoints = []

        with patch("brochure_enrichment._extract_brochure_units", return_value=[
            {"building": "B0", "floor_unit": None, "special_features": "x"},
        ]):
            brochure_enrichment.enrich_rows_grouped(rows, checkpoint_callback=lambda r: row_checkpoints.append(list(r)))

        self.assertTrue(row_checkpoints)

    def test_everything_already_ok_is_a_no_op_with_correct_totals(self):
        rows = self._rows(2)
        already_processed = {r.brochure_link: "ok" for r in rows}

        with patch("brochure_enrichment._extract_brochure_units") as mock_extract:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows, already_processed=already_processed)

        mock_extract.assert_not_called()
        self.assertEqual(stats["unique_brochures_considered"], 2)
        self.assertEqual(stats["processed_urls"], {})
        self.assertEqual(log, [])

    def test_resume_after_interruption_processes_only_the_remaining_brochures(self):
        # Simulates the exact reported scenario: 30 of 126 already checked,
        # a resume must only touch the other 96 - proven here at smaller
        # scale (2 of 5 already done).
        rows = self._rows(5)
        already_processed = {
            "https://example.com/B0.pdf": "ok", "https://example.com/B1.pdf": "ok",
        }

        with patch("brochure_enrichment._extract_brochure_units", return_value=[
            {"building": "x", "floor_unit": None, "special_features": "f"},
        ]) as mock_extract:
            _, _, stats = brochure_enrichment.enrich_rows_grouped(rows, already_processed=already_processed)

        self.assertEqual(mock_extract.call_count, 3)
        called_urls = {c.args[0] for c in mock_extract.call_args_list}
        self.assertEqual(called_urls, {"https://example.com/B2.pdf", "https://example.com/B3.pdf", "https://example.com/B4.pdf"})
        self.assertEqual(len(stats["processed_urls"]), 3)


class EnrichRowsFromFloorplansCheckpointTests(unittest.TestCase):
    """
    _enrich_rows_from_floorplans - the floorplan pass' own checkpoint/
    resume contract, mirroring enrich_rows_grouped's brochure-side one (see
    that function's own docstring). Confirmed real gap this closes: this
    pass previously had no checkpointing of its own at all - an
    interruption partway through it silently lost every floor plan already
    matched, and a resume had no way to skip already-checked floor plans.
    """

    def _rows(self, n):
        return [
            ListingRow(
                building=f"B{i}", floor_unit="1st", floorplan_link=f"https://example.com/{i}.pdf",
                special_features=None,
            )
            for i in range(n)
        ]

    def test_row_checkpoint_fires_after_every_floorplan(self):
        rows = self._rows(3)
        checkpoints = []

        with patch(
            "brochure_enrichment._extract_floorplan_units",
            return_value=[{"floor_unit": None, "special_features": "Feature"}],
        ):
            brochure_enrichment._enrich_rows_from_floorplans(rows, checkpoint_callback=checkpoints.append)

        self.assertEqual(len(checkpoints), 3)
        self.assertTrue(all(r.special_features == "Feature" for r in checkpoints[-1]))

    def test_url_checkpoint_fires_with_cumulative_state_and_total(self):
        rows = self._rows(1)
        already_processed = {"https://example.com/other.pdf": "ok"}
        seen = []

        with patch(
            "brochure_enrichment._extract_floorplan_units",
            return_value=[{"floor_unit": None, "special_features": "x"}],
        ):
            brochure_enrichment._enrich_rows_from_floorplans(
                rows, already_processed=already_processed, url_checkpoint_callback=lambda d, t: seen.append((d, t)),
            )

        self.assertEqual(len(seen), 1)
        processed, total = seen[0]
        self.assertEqual(processed, {"https://example.com/other.pdf": "ok", "https://example.com/0.pdf": "ok"})
        self.assertEqual(total, 1)

    def test_already_ok_floorplan_is_never_refetched(self):
        rows = self._rows(2)
        already_processed = {"https://example.com/0.pdf": "ok"}

        with patch(
            "brochure_enrichment._extract_floorplan_units",
            return_value=[{"floor_unit": None, "special_features": "New"}],
        ) as mock_extract:
            current, log, stats = brochure_enrichment._enrich_rows_from_floorplans(
                rows, already_processed=already_processed,
            )

        mock_extract.assert_called_once_with("https://example.com/1.pdf")
        self.assertIsNone(current[0].special_features)  # skipped - left exactly as before
        self.assertEqual(current[1].special_features, "New")
        self.assertEqual(stats["processed_urls"], {"https://example.com/1.pdf": "ok"})

    def test_stats_report_considered_ok_and_unavailable_counts(self):
        rows = self._rows(2)

        def _fake(url):
            return None if "0.pdf" in url else [{"floor_unit": None, "special_features": "x"}]

        with patch("brochure_enrichment._extract_floorplan_units", side_effect=_fake):
            _, _, stats = brochure_enrichment._enrich_rows_from_floorplans(rows)

        self.assertEqual(stats["unique_floorplans_considered"], 2)
        self.assertEqual(stats["floorplans_read_ok"], 1)
        self.assertEqual(stats["floorplans_unavailable"], 1)

    def test_no_eligible_floorplans_returns_zeroed_stats_with_no_callback_firing(self):
        rows = [ListingRow(building="A", floorplan_link=None, special_features=None)]
        checkpoints = []

        current, log, stats = brochure_enrichment._enrich_rows_from_floorplans(
            rows, checkpoint_callback=checkpoints.append,
        )

        self.assertEqual(checkpoints, [])
        self.assertEqual(stats, {
            "unique_floorplans_considered": 0, "floorplans_read_ok": 0,
            "floorplans_unavailable": 0, "processed_urls": {}, "document_issues": [],
        })


class EnrichRowsGroupedFloorplanParamsTests(EnrichmentTestCase):
    """enrich_rows_grouped's own floorplan_already_processed/floorplan_
    checkpoint_callback/floorplan_url_checkpoint_callback plumbing through
    to _enrich_rows_from_floorplans, and the floorplan stats surfaced on
    its own return value - both the early-return (no brochures to fetch at
    all) and the normal (brochures processed first) paths."""

    def test_floorplan_stats_present_even_with_zero_brochures(self):
        rows = [
            ListingRow(
                building="A", floor_unit="1st", floorplan_link="https://example.com/fp.pdf", special_features=None,
            ),
        ]
        with patch(
            "brochure_enrichment._extract_floorplan_units",
            return_value=[{"floor_unit": None, "special_features": "From floorplan"}],
        ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(enriched[0].special_features, "From floorplan")
        self.assertEqual(stats["unique_floorplans_considered"], 1)
        self.assertEqual(stats["floorplans_read_ok"], 1)
        self.assertEqual(stats["floorplan_processed_urls"], {"https://example.com/fp.pdf": "ok"})

    def test_floorplan_stats_present_after_a_real_brochure_run(self):
        rows = [
            ListingRow(
                building="A", floor_unit="1st", brochure_link="https://example.com/b.pdf",
                floorplan_link="https://example.com/fp.pdf", special_features=None,
            ),
        ]
        with patch("brochure_enrichment._extract_brochure_units", return_value=[]), \
             patch(
                 "brochure_enrichment._extract_floorplan_units",
                 return_value=[{"floor_unit": None, "special_features": "From floorplan"}],
             ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(enriched[0].special_features, "From floorplan")
        self.assertEqual(stats["unique_floorplans_considered"], 1)
        self.assertEqual(stats["floorplan_processed_urls"], {"https://example.com/fp.pdf": "ok"})

    def test_floorplan_already_processed_is_never_refetched(self):
        rows = [
            ListingRow(
                building="A", floor_unit="1st", floorplan_link="https://example.com/fp.pdf", special_features=None,
            ),
        ]
        with patch("brochure_enrichment._extract_floorplan_units") as mock_extract:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(
                rows, floorplan_already_processed={"https://example.com/fp.pdf": "ok"},
            )

        mock_extract.assert_not_called()
        self.assertIsNone(enriched[0].special_features)

    def test_floorplan_checkpoint_callbacks_fire_separately_from_brochure_ones(self):
        rows = [
            ListingRow(
                building="A", floor_unit="1st", brochure_link="https://example.com/b.pdf",
                floorplan_link="https://example.com/fp.pdf", special_features=None,
            ),
        ]
        brochure_checkpoints, floorplan_checkpoints, floorplan_url_checkpoints = [], [], []

        with patch("brochure_enrichment._extract_brochure_units", return_value=[]), \
             patch(
                 "brochure_enrichment._extract_floorplan_units",
                 return_value=[{"floor_unit": None, "special_features": "x"}],
             ):
            brochure_enrichment.enrich_rows_grouped(
                rows,
                url_checkpoint_callback=lambda d: brochure_checkpoints.append(d),
                floorplan_checkpoint_callback=lambda r: floorplan_checkpoints.append(list(r)),
                floorplan_url_checkpoint_callback=lambda d, t: floorplan_url_checkpoints.append((d, t)),
            )

        self.assertEqual(len(brochure_checkpoints), 1)
        self.assertEqual(len(floorplan_checkpoints), 1)
        self.assertEqual(floorplan_url_checkpoints, [({"https://example.com/fp.pdf": "ok"}, 1)])


class ClassifyLinkEligibilityTests(unittest.TestCase):
    """Pure classification, no network activity - the pre-fetch status."""

    def test_blank_is_no_document(self):
        self.assertEqual(brochure_enrichment.classify_link_eligibility(None), brochure_enrichment.STATUS_NO_DOCUMENT)
        self.assertEqual(brochure_enrichment.classify_link_eligibility(""), brochure_enrichment.STATUS_NO_DOCUMENT)

    def test_placeholder_text_is_invalid_placeholder(self):
        for placeholder in ("TBC", "N/A", "-", "Coming Soon", "None"):
            self.assertEqual(
                brochure_enrichment.classify_link_eligibility(placeholder),
                brochure_enrichment.STATUS_INVALID_PLACEHOLDER,
                placeholder,
            )

    def test_generic_homepage_is_unsupported_link_type(self):
        self.assertEqual(
            brochure_enrichment.classify_link_eligibility("https://www.workspace.co.uk/"),
            brochure_enrichment.STATUS_UNSUPPORTED_LINK_TYPE,
        )

    def test_floorplan_shaped_link_is_unsupported_for_a_brochure(self):
        self.assertEqual(
            brochure_enrichment.classify_link_eligibility("https://app.box.com/s/my-floorplan"),
            brochure_enrichment.STATUS_UNSUPPORTED_LINK_TYPE,
        )

    def test_floorplan_shaped_link_is_eligible_for_a_floorplan(self):
        self.assertIsNone(
            brochure_enrichment.classify_link_eligibility(
                "https://app.box.com/s/my-floorplan", reject_floorplan_shaped=False,
            )
        )

    def test_canva_view_link_is_pre_emptively_rejected(self):
        # Confirmed directly (see brochure_link_resolver.is_canva_view_
        # link's own docstring) - a plain, unauthenticated fetch of a real
        # public Canva "view" link never returns usable document content,
        # for ANY such link, not just this one example - so it's excluded
        # here, before ever wasting a real fetch attempt, exactly like a
        # placeholder or a known generic homepage already are. This is
        # evidence-based, not a guess: the alternative (only discovering
        # this after a real fetch) is still safe (see
        # DocumentStatusIntegrationTests), just wasteful.
        self.assertEqual(
            brochure_enrichment.classify_link_eligibility(
                "https://www.canva.com/design/DAGzsWW-Yp8/s8tPVTQe6HUQa939xX0XQw/view"
            ),
            brochure_enrichment.STATUS_UNSUPPORTED_LINK_TYPE,
        )

    def test_a_normal_pdf_link_is_eligible(self):
        self.assertIsNone(brochure_enrichment.classify_link_eligibility("https://example.com/brochure.pdf"))

    def test_google_drive_folder_link_is_unsupported_link_type(self):
        # Real Kitt's Availability shape - previously fell through to a
        # real, structurally-doomed fetch attempt (see EligibleFloorplanUrlTests/
        # EligibleBrochureUrlTests' own equivalent tests for the actual
        # fetch-gate half of this fix).
        self.assertEqual(
            brochure_enrichment.classify_link_eligibility(
                "https://drive.google.com/drive/folders/1Q9q4PTgZcJvhTMW3rVAzND3rVCQ6b9ra?usp=drive_link",
                reject_floorplan_shaped=False,
            ),
            brochure_enrichment.STATUS_UNSUPPORTED_LINK_TYPE,
        )

    def test_google_drive_single_file_link_is_unaffected(self):
        self.assertIsNone(
            brochure_enrichment.classify_link_eligibility(
                "https://drive.google.com/file/d/1EPO_g2beNdCQKTgHqVIVlaIZPvSjbudJ/view?usp=sharing",
            )
        )


class IneligibleLinkIssuesCanvaReasonTests(unittest.TestCase):
    """
    _ineligible_link_issues' own additive "unsupported_reason": "canva" key
    (see its docstring) - lets the Review page's compact summary correctly
    say "most of these are Canva links" ONLY when the data actually
    establishes that, without inventing a new status or touching Canva's
    own safe, pre-fetch-rejected behavior from commit a67e337 at all.
    """

    def test_canva_view_link_is_tagged_with_the_canva_reason(self):
        rows = [ListingRow(
            building="Canva House",
            brochure_link="https://www.canva.com/design/DAGzsWW-Yp8/s8tPVTQe6HUQa939xX0XQw/view",
            special_features=None,
        )]
        issues = brochure_enrichment._ineligible_link_issues(
            rows, brochure_enrichment.needs_enrichment, "brochure_link", reject_floorplan_shaped=True,
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["unsupported_reason"], "canva")

    def test_a_non_canva_unsupported_link_has_no_reason_key(self):
        rows = [ListingRow(
            building="Example House", brochure_link="https://www.workspace.co.uk/", special_features=None,
        )]
        issues = brochure_enrichment._ineligible_link_issues(
            rows, brochure_enrichment.needs_enrichment, "brochure_link", reject_floorplan_shaped=True,
        )
        self.assertEqual(len(issues), 1)
        self.assertNotIn("unsupported_reason", issues[0])

    def test_a_fetch_failure_status_never_gets_a_canva_reason_key(self):
        # unsupported_reason is only ever attached to STATUS_UNSUPPORTED_
        # LINK_TYPE - a different issue status (e.g. a blank/placeholder,
        # not an issue at all here) must never carry it.
        rows = [ListingRow(building="A", brochure_link="TBC", special_features=None)]
        issues = brochure_enrichment._ineligible_link_issues(
            rows, brochure_enrichment.needs_enrichment, "brochure_link", reject_floorplan_shaped=True,
        )
        self.assertEqual(issues, [])  # TBC is a placeholder, not an issue at all


class RowHadAmbiguousMatchTests(unittest.TestCase):
    def test_two_candidate_units_with_no_disambiguator_is_ambiguous(self):
        row = ListingRow(building="Nash House", floor_unit=None, size_sqft=None)
        units = _brochure_units([
            {"building": "Nash House", "floor_unit": "1st Floor", "size_sqft": 1000},
            {"building": "Nash House", "floor_unit": "2nd Floor", "size_sqft": 1500},
        ])
        self.assertTrue(brochure_enrichment._row_had_ambiguous_match(row, units))

    def test_a_single_matching_unit_is_never_ambiguous(self):
        row = ListingRow(building="Nash House", floor_unit=None)
        units = _brochure_units([{"building": "Nash House", "floor_unit": "2nd Floor"}])
        self.assertFalse(brochure_enrichment._row_had_ambiguous_match(row, units))

    def test_zero_matching_units_is_never_ambiguous(self):
        row = ListingRow(building="A Building Not In This Document")
        units = _brochure_units([{"building": "Nash House", "floor_unit": "2nd Floor"}])
        self.assertFalse(brochure_enrichment._row_had_ambiguous_match(row, units))

    def test_a_disambiguated_match_is_never_ambiguous(self):
        row = ListingRow(building="Nash House", floor_unit="2nd Floor")
        units = _brochure_units([
            {"building": "Nash House", "floor_unit": "1st Floor"},
            {"building": "Nash House", "floor_unit": "2nd Floor"},
        ])
        self.assertFalse(brochure_enrichment._row_had_ambiguous_match(row, units))


class RowHadRentConflictTests(unittest.TestCase):
    def test_inconsistent_candidate_rent_is_a_conflict(self):
        row = ListingRow(building="Metropolitan Wharf", floor_unit="1st", size_sqft=2762, rent_psf=33, rent_pcm=None)
        units = _brochure_units([{"building": "Metropolitan Wharf", "floor_unit": "1st", "rent_pcm": 33}])
        self.assertTrue(brochure_enrichment._row_had_rent_conflict(row, units))

    def test_consistent_candidate_rent_is_not_a_conflict(self):
        row = ListingRow(building="Metropolitan Wharf", floor_unit="1st", size_sqft=2762, rent_pcm=None, rent_psf=None)
        units = _brochure_units([{"building": "Metropolitan Wharf", "floor_unit": "1st", "rent_pcm": 7595.5, "rent_psf": 33}])
        self.assertFalse(brochure_enrichment._row_had_rent_conflict(row, units))

    def test_no_matched_unit_is_never_a_conflict(self):
        row = ListingRow(building="A Building Not In This Document", rent_pcm=None)
        units = _brochure_units([{"building": "Nash House", "floor_unit": "2nd Floor", "rent_pcm": 33}])
        self.assertFalse(brochure_enrichment._row_had_rent_conflict(row, units))

    def test_no_units_at_all_is_never_a_conflict(self):
        row = ListingRow(building="Nash House", rent_pcm=None)
        self.assertFalse(brochure_enrichment._row_had_rent_conflict(row, None))


class DocumentStatusIntegrationTests(EnrichmentTestCase):
    """
    End-to-end status classification through enrich_rows_grouped's real
    fetch/render/extract path - only httpx.get and extract.render_pages/
    render_and_extract are mocked (never brochure_enrichment's own
    fetch/extract functions), so this exercises the REAL _record_status
    call sites, not a shortcut around them.
    """

    def test_valid_pdf_extracts_successfully(self):
        rows = [ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None)]
        with patch("brochure_enrichment.httpx.get", return_value=_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["img"]), \
             patch(
                 "brochure_enrichment.extract.render_and_extract",
                 return_value={"units": [{"building": "A", "special_features": "Nice"}]},
             ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(enriched[0].special_features, "Nice")
        self.assertEqual(stats["document_issues"], [])

    def test_placeholder_link_produces_no_fake_extraction_and_no_issue(self):
        rows = [ListingRow(building="A", brochure_link="TBC", special_features=None)]
        with patch("brochure_enrichment.httpx.get") as mock_get:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        mock_get.assert_not_called()
        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(stats["document_issues"], [])
        self.assertEqual(enriched[0].brochure_link, "TBC")

    def test_unsupported_link_is_recorded_as_an_issue_and_link_is_preserved(self):
        rows = [ListingRow(
            building="Example House", brochure_link="https://www.workspace.co.uk/", special_features=None,
        )]
        with patch("brochure_enrichment.httpx.get") as mock_get:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        mock_get.assert_not_called()
        self.assertEqual(enriched[0].brochure_link, "https://www.workspace.co.uk/")
        self.assertEqual(stats["document_issues"], [
            {"building": "Example House", "floor_unit": None, "status": brochure_enrichment.STATUS_UNSUPPORTED_LINK_TYPE},
        ])

    def test_canva_view_link_is_rejected_before_any_fetch_and_link_is_preserved(self):
        # The one focused Canva scenario this task asks for - confirmed
        # directly (see is_canva_view_link's own docstring) that a plain,
        # unauthenticated fetch of a real public Canva "view" link never
        # returns usable document content (Canva's own server returns an
        # "Unsupported client" HTML shell to any non-browser client) - so
        # this link is rejected at the SAME pre-fetch stage as a placeholder
        # or a generic homepage, never wasting a real network round trip.
        # No separate Canva extraction system exists; this is the same
        # existing unsupported-link-type outcome any other unreadable link
        # shape already gets.
        rows = [ListingRow(
            building="Canva House",
            brochure_link="https://www.canva.com/design/DAGzsWW-Yp8/s8tPVTQe6HUQa939xX0XQw/view?utm_content=x#7",
            special_features=None,
        )]
        with patch("brochure_enrichment.httpx.get") as mock_get:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        mock_get.assert_not_called()
        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(
            enriched[0].brochure_link,
            "https://www.canva.com/design/DAGzsWW-Yp8/s8tPVTQe6HUQa939xX0XQw/view?utm_content=x#7",
        )
        self.assertEqual(stats["document_issues"], [
            {
                "building": "Canva House", "floor_unit": None,
                "status": brochure_enrichment.STATUS_UNSUPPORTED_LINK_TYPE, "unsupported_reason": "canva",
            },
        ])

    def test_canva_failure_does_not_stop_other_documents_from_processing(self):
        rows = [
            ListingRow(
                building="Canva House",
                brochure_link="https://www.canva.com/design/DAGzsWW-Yp8/s8tPVTQe6HUQa939xX0XQw/view",
                special_features=None,
            ),
            ListingRow(building="Good Co", brochure_link="https://example.com/good.pdf", special_features=None),
        ]
        with patch("brochure_enrichment.httpx.get", return_value=_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["img"]), \
             patch(
                 "brochure_enrichment.extract.render_and_extract",
                 return_value={"units": [{"building": "Good Co", "special_features": "Nice"}]},
             ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(enriched[1].special_features, "Nice")
        self.assertEqual(len(stats["document_issues"]), 1)
        self.assertEqual(stats["document_issues"][0]["building"], "Canva House")

    def test_404_is_recorded_as_fetch_failed_and_upload_continues(self):
        rows = [
            ListingRow(building="Broken", brochure_link="https://example.com/broken.pdf", special_features=None),
            ListingRow(building="Good", brochure_link="https://example.com/good.pdf", special_features=None),
        ]

        def fake_get(url, **kwargs):
            if "broken" in url:
                return _response(status_code=404, content=b"Not Found", content_type="text/plain")
            return _response()

        with patch("brochure_enrichment.httpx.get", side_effect=fake_get), \
             patch("brochure_enrichment.extract.render_pages", return_value=["img"]), \
             patch(
                 "brochure_enrichment.extract.render_and_extract",
                 return_value={"units": [{"building": "Good", "special_features": "Nice"}]},
             ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(enriched[1].special_features, "Nice")
        self.assertEqual(enriched[0].brochure_link, "https://example.com/broken.pdf")
        self.assertEqual(len(stats["document_issues"]), 1)
        self.assertEqual(stats["document_issues"][0]["building"], "Broken")
        self.assertEqual(stats["document_issues"][0]["status"], brochure_enrichment.STATUS_FETCH_FAILED)

    def test_403_private_link_is_recorded_as_fetch_failed(self):
        rows = [ListingRow(building="Private Co", brochure_link="https://example.com/private.pdf", special_features=None)]
        with patch(
            "brochure_enrichment.httpx.get",
            return_value=_response(status_code=403, content=b"Forbidden", content_type="text/plain"),
        ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(stats["document_issues"][0]["status"], brochure_enrichment.STATUS_FETCH_FAILED)

    def test_timeout_exception_is_recorded_and_upload_continues(self):
        rows = [
            ListingRow(building="Slow Co", brochure_link="https://example.com/slow.pdf", special_features=None),
            ListingRow(building="Good Co", brochure_link="https://example.com/good.pdf", special_features=None),
        ]

        def fake_get(url, **kwargs):
            if "slow" in url:
                raise httpx.TimeoutException("timed out")
            return _response()

        with patch("brochure_enrichment.httpx.get", side_effect=fake_get), \
             patch("brochure_enrichment.extract.render_pages", return_value=["img"]), \
             patch(
                 "brochure_enrichment.extract.render_and_extract",
                 return_value={"units": [{"building": "Good Co", "special_features": "Nice"}]},
             ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(enriched[1].special_features, "Nice")
        self.assertEqual(stats["document_issues"][0]["status"], brochure_enrichment.STATUS_FETCH_FAILED)

    def test_html_returned_instead_of_a_document_is_rejected_safely(self):
        rows = [ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None)]
        with patch(
            "brochure_enrichment.httpx.get",
            return_value=_response(content=b"<html>not a pdf</html>", content_type="text/html"),
        ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(stats["document_issues"][0]["status"], brochure_enrichment.STATUS_FETCH_FAILED)

    def test_corrupt_document_is_recorded_as_render_failed(self):
        rows = [ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None)]
        with patch("brochure_enrichment.httpx.get", return_value=_response()), \
             patch("brochure_enrichment.extract.render_pages", side_effect=RuntimeError("cannot render")):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(stats["document_issues"][0]["status"], brochure_enrichment.STATUS_RENDER_FAILED)

    def test_gemini_failure_is_recorded_as_extraction_failed(self):
        rows = [ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None)]
        with patch("brochure_enrichment.httpx.get", return_value=_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["img"]), \
             patch("brochure_enrichment.extract.render_and_extract", side_effect=RuntimeError("gemini boom")):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(stats["document_issues"][0]["status"], brochure_enrichment.STATUS_EXTRACTION_FAILED)

    def test_ambiguous_unit_match_is_a_distinct_status(self):
        rows = [ListingRow(
            building="Nash House", brochure_link="https://example.com/a.pdf",
            floor_unit=None, size_sqft=None, special_features=None,
        )]
        with patch("brochure_enrichment.httpx.get", return_value=_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["img"]), \
             patch(
                 "brochure_enrichment.extract.render_and_extract",
                 return_value={"units": [
                     {"building": "Nash House", "floor_unit": "1st Floor", "size_sqft": 1000},
                     {"building": "Nash House", "floor_unit": "2nd Floor", "size_sqft": 1500},
                 ]},
             ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(stats["document_issues"][0]["status"], brochure_enrichment.STATUS_EXTRACTED_BUT_AMBIGUOUS)

    def test_extraction_with_no_useful_data_is_not_reported_as_an_issue(self):
        rows = [ListingRow(building="A", brochure_link="https://example.com/a.pdf", special_features=None)]
        with patch("brochure_enrichment.httpx.get", return_value=_response()), \
             patch("brochure_enrichment.extract.render_pages", return_value=["img"]), \
             patch("brochure_enrichment.extract.render_and_extract", return_value={"units": []}):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertIsNone(enriched[0].special_features)
        self.assertEqual(stats["document_issues"], [])

    def test_box_share_success_is_still_recorded_as_extracted_successfully(self):
        # Box resolver behaviour itself must keep working - see
        # FetchBoxSharedPdfTests for its own dedicated coverage; this just
        # confirms status tracking doesn't regress that path.
        rows = [ListingRow(building="A", brochure_link="https://app.box.com/s/abc123", special_features=None)]
        share_html = _box_share_html(shared_name="xyz", extension="pdf")
        with patch(
            "brochure_enrichment.httpx.get",
            side_effect=[_html_response(share_html), _response()],
        ), patch("brochure_enrichment.extract.render_pages", return_value=["img"]), patch(
            "brochure_enrichment.extract.render_and_extract",
            return_value={"units": [{"building": "A", "special_features": "Nice"}]},
        ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(enriched[0].special_features, "Nice")
        self.assertEqual(stats["document_issues"], [])

    def test_checkpoint_and_summary_reach_a_completed_state_despite_a_failure(self):
        rows = [ListingRow(building="Broken", brochure_link="https://example.com/broken.pdf", special_features=None)]
        with patch(
            "brochure_enrichment.httpx.get",
            return_value=_response(status_code=500, content=b"error", content_type="text/plain"),
        ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        # A failed document must still end in a KNOWN terminal outcome for
        # this run - never left looking like it's still "in progress".
        self.assertEqual(stats["brochures_done"] if "brochures_done" in stats else len(stats["processed_urls"]), 1)
        self.assertEqual(stats["processed_urls"], {"https://example.com/broken.pdf": "unavailable"})
        self.assertEqual(len(stats["document_issues"]), 1)


class IsConfirmedDeadCanvaLinkTests(unittest.TestCase):
    """_is_confirmed_dead_canva_link - the substring match on canva_
    renderer's own navigation-status reason text."""

    def test_a_confirmed_non_2xx_navigation_status_matches(self):
        self.assertTrue(brochure_enrichment._is_confirmed_dead_canva_link(
            "Canva render failed: navigation returned HTTP 404 (design not found or inaccessible)"
        ))

    def test_a_navigation_timeout_does_not_match(self):
        self.assertFalse(brochure_enrichment._is_confirmed_dead_canva_link(
            "Canva render failed: navigation failed or timed out (TimeoutError('Timeout 30000ms exceeded'))"
        ))

    def test_none_detail_does_not_match(self):
        self.assertFalse(brochure_enrichment._is_confirmed_dead_canva_link(None))

    def test_unrelated_detail_does_not_match(self):
        self.assertFalse(brochure_enrichment._is_confirmed_dead_canva_link("Canva render failed: private design"))


class BrochureLinkBrokenFlagTests(EnrichmentTestCase):
    """
    ListingRow.brochure_link_broken - set from enrich_rows_grouped's own
    per-URL document_status, end-to-end through the real Canva HTTP call
    (only httpx.post is mocked, never this app's own fetch/render
    functions - same "exercise the real _record_status call sites"
    principle as DocumentStatusIntegrationTests above).

    Real requirement this covers: True only for a CONFIRMED dead link
    (Canva's own navigation-status check returning a non-2xx - see
    canva_renderer/app.py) - a bare navigation timeout/exception is much
    weaker evidence (could easily be a one-off glitch on an otherwise-
    working link) and must leave the flag at None (not yet confirmed),
    never jump straight to True from that alone.
    """

    def test_a_confirmed_non_2xx_navigation_status_sets_the_flag_true(self):
        response = MagicMock(
            status_code=422, headers={"content-type": "application/json"},
            json=MagicMock(return_value={
                "error": "render_failed",
                "reason": "navigation returned HTTP 404 (design not found or inaccessible)",
            }),
        )
        rows = [ListingRow(building="Dead Design", brochure_link=_CANVA_URL, special_features=None)]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(stats["document_issues"][0]["status"], brochure_enrichment.STATUS_RENDER_FAILED)
        self.assertIs(enriched[0].brochure_link_broken, True)

    def test_a_navigation_timeout_leaves_the_flag_unconfirmed(self):
        response = MagicMock(
            status_code=422, headers={"content-type": "application/json"},
            json=MagicMock(return_value={
                "error": "render_failed",
                "reason": "navigation failed or timed out (TimeoutError('Timeout 30000ms exceeded'))",
            }),
        )
        rows = [ListingRow(building="Flaky Design", brochure_link=_CANVA_URL, special_features=None)]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(stats["document_issues"][0]["status"], brochure_enrichment.STATUS_RENDER_FAILED)
        self.assertIsNone(enriched[0].brochure_link_broken)

    def test_a_genuinely_successful_render_sets_the_flag_false(self):
        # Confirms the flag also self-heals: a link that previously failed
        # and is now read fine must clear back to False, not just stay
        # unset - see master_merge.py's own silent-merge handling of this
        # field for why False (a genuine confirmed value, never treated
        # as blank) is what lets that self-healing actually reach master.
        response = _canva_pages_response([b"\x89PNG p1"], page_count_detected=1)
        rows = [ListingRow(building="Fixed Design", brochure_link=_CANVA_URL, special_features=None)]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}), \
                patch(
                    "brochure_enrichment.extract.render_and_extract",
                    return_value={"units": [{"building": "Fixed Design", "special_features": "Nice"}]},
                ):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        self.assertEqual(stats["document_issues"], [])
        self.assertIs(enriched[0].brochure_link_broken, False)


REAL_CANVA_VIEW_URL = (
    "https://www.canva.com/design/DAGzsWW-Yp8/s8tPVTQe6HUQa939xX0XQw/view"
    "?utm_content=DAGzsWW-Yp8&utm_campaign=designshare&utm_medium=link&utm_source=publishsharelink#7"
)


class RealCanvaLiveIntegrationTests(unittest.TestCase):
    """
    ONE optional live test (no mocking at all) against the real public
    Canva example URL this task was built for - makes a genuine, real
    network request. Skipped automatically when the network/Canva itself
    is unreachable, so it never breaks the suite for someone offline.

    Deliberately calls _fetch_pdf_bytes directly rather than going through
    enrich_rows_grouped - the pre-fetch is_canva_view_link gate (see
    ClassifyLinkEligibilityTests) means the real pipeline never even
    attempts a fetch for this URL at all now, so a live test THROUGH that
    path would make no real request and prove nothing. This one instead
    directly re-verifies the underlying, evidence-based premise that
    classification itself is built on: a plain, unauthenticated fetch of
    this exact real link genuinely does not yield usable document bytes.
    If Canva ever changes this (starts serving real content to a non-
    browser client), THIS test starts failing - a deliberate regression
    guard on the assumption, not just on this module's own code.
    """

    @classmethod
    def setUpClass(cls):
        try:
            import httpx as _httpx
            _httpx.get(REAL_CANVA_VIEW_URL, timeout=10)
        except Exception as e:
            raise unittest.SkipTest(f"network/Canva unreachable in this environment: {e!r}")

    def test_real_canva_view_link_still_does_not_yield_a_fetchable_document(self):
        result = brochure_enrichment._fetch_pdf_bytes(REAL_CANVA_VIEW_URL)
        self.assertIsNone(result)


# --- Real-brochure integration test for the widened field scope ---
#
# Makes a REAL Gemini call against the real Nash House brochure (only
# _fetch_pdf_bytes is faked, to read the real local file instead of a real
# Box URL - render_pages/render_and_extract/Gemini itself are all genuine) -
# unlike every other test above, skipped automatically when GEMINI_API_KEY
# isn't configured or the fixture isn't present, same convention as
# test_email_upload_integration.py's own real-API test.
import os  # noqa: E402 - kept local to this section, matching test_email_upload_integration.py's own layout

from env_utils import load_dotenv  # noqa: E402

load_dotenv()
_HAS_GEMINI_KEY = bool(os.environ.get("GEMINI_API_KEY"))
REAL_NASH_HOUSE_BROCHURE = Path(r"C:\Users\julie\Downloads\Nash House - Brochure.pdf")


@unittest.skipUnless(_HAS_GEMINI_KEY, "GEMINI_API_KEY not configured")
@unittest.skipUnless(REAL_NASH_HOUSE_BROCHURE.exists(), "real Nash House brochure not present in this environment")
class RealNashHouseBrochureFieldFallbackTests(EnrichmentTestCase):
    """
    Real, confirmed report: a spreadsheet row for Nash House had a brochure
    link but blank special_features despite the brochure clearly stating
    applicable text. Traced to the extraction/matching path working
    correctly once the building identity matches (see MatchUnitTests) - the
    gap this task closes is that the SAME real brochure also explicitly
    states address_1/postcode/submarket/desks_max/rent_psf/state_of_space
    for this unit, none of which were ever backfilled before this change.
    """

    @classmethod
    def setUpClass(cls):
        with patch(
            "brochure_enrichment._fetch_pdf_bytes", return_value=REAL_NASH_HOUSE_BROCHURE.read_bytes(),
        ):
            cls.units = brochure_enrichment._extract_brochure_units("https://example.com/nash-house-brochure.pdf")

    def test_real_brochure_units_were_actually_extracted(self):
        # Fails loudly (rather than every test below silently matching
        # nothing) if a real Gemini/prompt change ever stops finding any
        # unit at all for this real document.
        self.assertTrue(self.units)

    def test_blank_fields_are_backfilled_from_the_real_brochure(self):
        row = ListingRow(building="Nash House", floor_unit="2nd Floor")

        new_row, fields = brochure_enrichment._apply_units_to_row(row, self.units)

        # Real, previously-reported blank - now filled.
        self.assertTrue(new_row.special_features)
        self.assertIn("special_features", fields)
        # Newly-widened fields this task adds - all explicitly stated in
        # the real document for this exact unit.
        self.assertTrue(new_row.address_1)
        self.assertTrue(new_row.postcode)
        self.assertTrue(new_row.submarket)
        self.assertTrue(new_row.desks_max)
        self.assertTrue(new_row.state_of_space)

    def test_already_populated_fields_are_never_overwritten(self):
        # address_1/postcode are never-overwrite fields, full stop -
        # special_features is the one deliberate exception (see
        # _apply_units_to_row's own docstring): its existing value is kept,
        # never discarded, but the real brochure's own text for this exact
        # unit is still combined onto it rather than the whole combine step
        # being skipped just because the field wasn't blank to begin with -
        # covered separately below since it's governed by a different rule.
        row = ListingRow(
            building="Nash House", floor_unit="2nd Floor",
            address_1="Provider-stated address", postcode="EC1A 1AA", special_features="Provider-stated text",
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, self.units)

        self.assertEqual(new_row.address_1, "Provider-stated address")
        self.assertEqual(new_row.postcode, "EC1A 1AA")
        self.assertNotIn("address_1", fields)
        self.assertNotIn("postcode", fields)

    def test_already_populated_special_features_gets_the_real_brochure_text_appended(self):
        row = ListingRow(
            building="Nash House", floor_unit="2nd Floor", special_features="Provider-stated text",
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, self.units)

        # The row's own pre-existing value is kept as the first segment,
        # never discarded or overwritten outright...
        self.assertTrue(new_row.special_features.startswith("Provider-stated text; "))
        # ...but the real brochure's own text for this exact unit is no
        # longer silently dropped just because the field wasn't blank.
        self.assertGreater(len(new_row.special_features), len("Provider-stated text"))
        self.assertIn("special_features", fields)


if __name__ == "__main__":
    unittest.main()
