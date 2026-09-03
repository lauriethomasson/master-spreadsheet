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


def _brochure_units(units, property_features=None, contacts=None, building_features=None):
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


class IsPlaceholderAddressTests(unittest.TestCase):
    """_is_placeholder_address - True when address_1 is blank OR just a
    duplicate of building in disguise (the confirmed real "Nineteen Wells
    St" shape: a source with no separate numbered address states, gets
    address_1 filled in as a plain copy of building rather than left
    blank)."""

    def test_blank_address_is_a_placeholder(self):
        self.assertTrue(brochure_enrichment._is_placeholder_address(None, "Nineteen Wells St"))
        self.assertTrue(brochure_enrichment._is_placeholder_address("", "Nineteen Wells St"))

    def test_exact_duplicate_of_building_is_a_placeholder(self):
        self.assertTrue(brochure_enrichment._is_placeholder_address("Nineteen Wells St", "Nineteen Wells St"))

    def test_street_suffix_abbreviation_difference_is_still_a_placeholder(self):
        # "St" (address_1) vs "Street" (building) - a street-suffix
        # abbreviation difference alone must never make a placeholder look
        # like a genuine, independent address.
        self.assertTrue(brochure_enrichment._is_placeholder_address("Nineteen Wells St", "Nineteen Wells Street"))
        self.assertTrue(brochure_enrichment._is_placeholder_address("Nineteen Wells Street", "Nineteen Wells St"))

    def test_genuinely_different_address_is_not_a_placeholder(self):
        self.assertFalse(brochure_enrichment._is_placeholder_address("22 Newman Street", "Kent House"))

    def test_blank_building_with_non_blank_address_is_not_a_placeholder(self):
        # Nothing to compare address_1 against - a real, stated address_1
        # is never treated as a placeholder just because building happens
        # to be blank too.
        self.assertFalse(brochure_enrichment._is_placeholder_address("22 Newman Street", None))


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

    def test_placeholder_address_1_alone_needs_enrichment(self):
        # The confirmed real "Nineteen Wells St" shape: every OTHER
        # ENRICHABLE_FIELDS is genuinely filled - address_1 being just a
        # copy of building is the ONLY reason this row is eligible at all.
        row = ListingRow(
            building="Nineteen Wells St", address_1="Nineteen Wells St", postcode="EC1A 1AA", submarket="City",
            floor_unit="1st", size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
            special_features="Nice", state_of_space="Cat A", contacts="Jane, jane@x.com",
        )
        self.assertTrue(brochure_enrichment.needs_enrichment(row))

    def test_placeholder_address_1_with_street_suffix_abbreviation_needs_enrichment(self):
        row = ListingRow(
            building="Nineteen Wells Street", address_1="Nineteen Wells St", postcode="EC1A 1AA", submarket="City",
            floor_unit="1st", size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
            special_features="Nice", state_of_space="Cat A", contacts="Jane, jane@x.com",
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

    def test_every_field_filled_including_special_features_but_has_a_brochure_link_still_needs_enrichment(self):
        # Confirmed real bug: all 41 real Colliers rows already have SOME
        # value in every single ENRICHABLE_FIELDS field, special_features
        # included (e.g. "1 meeting room; Term 24 months") - but special_
        # features's own combine logic (see _apply_units_to_row) is never
        # gated on it already being non-blank, so this row must stay
        # eligible on that basis alone whenever a brochure_link exists to
        # check, unlike the other 8 fields which keep the plain "only if
        # blank" rule.
        row = ListingRow(
            building="A", address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
            floor_unit="1st", size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
            special_features="Nice", state_of_space="Cat A", contacts="Jane, jane@x.com",
            brochure_link="https://example.com/brochure.pdf",
        )
        self.assertTrue(brochure_enrichment.needs_enrichment(row))

    def test_every_field_filled_with_no_brochure_link_still_does_not_need_enrichment(self):
        # Same fully-filled row, but nothing to check at all (no
        # brochure_link) - special_features's decoupling must not make
        # this True unconditionally; there has to be a real link behind it.
        row = ListingRow(
            building="A", address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
            floor_unit="1st", size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
            special_features="Nice", state_of_space="Cat A", contacts="Jane, jane@x.com",
            brochure_link=None,
        )
        self.assertFalse(brochure_enrichment.needs_enrichment(row))


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

    def test_fetch_pdf_bytes_accepts_a_reachable_html_page_when_flag_set(self):
        # The confirmed real gap: a genuine per-unit listing webpage (e.g.
        # colliers.com/en-gb/properties/...) returns real HTML, not a PDF -
        # accept_any_reachable_page=True must keep it rather than treating
        # a non-PDF response as a fetch failure. The URL isn't a direct
        # .pdf, so _fetch_pdf_bytes tries resolve_brochure_link(url) FIRST
        # (same httpx.get, same mocked response) - .text/.url must be real
        # strings, not the MagicMock default, or its own BeautifulSoup
        # parse/redirect check chokes on them before ever reaching the
        # accept_any_reachable_page check this test is actually about.
        url = "https://www.colliers.com/en-gb/properties/kingsland-house"
        html_response = _response(content=b"<html>Kingsland House</html>", content_type="text/html")
        html_response.text = "<html>Kingsland House</html>"
        html_response.url = url
        with patch("brochure_enrichment.httpx.get", return_value=html_response):
            result = brochure_enrichment._fetch_pdf_bytes(url, accept_any_reachable_page=True)

        self.assertEqual(result, b"<html>Kingsland House</html>")

    def test_fetch_pdf_bytes_still_rejects_html_by_default(self):
        # accept_any_reachable_page defaults to False - every OTHER caller
        # (enrichment/floorplan fetch, which parses these bytes as a
        # document) keeps today's exact PDF/image-only behavior.
        url = "https://www.colliers.com/en-gb/properties/kingsland-house"
        html_response = _response(content=b"<html>Kingsland House</html>", content_type="text/html")
        html_response.text = "<html>Kingsland House</html>"
        html_response.url = url
        with patch("brochure_enrichment.httpx.get", return_value=html_response):
            result = brochure_enrichment._fetch_pdf_bytes(url)

        self.assertIsNone(result)

    def test_fetch_pdf_bytes_still_returns_none_on_a_genuine_fetch_failure_even_with_the_flag(self):
        # accept_any_reachable_page only widens what counts as a valid
        # RESPONSE - it can never rescue a request that never got one at
        # all (a real HTTP error, a timeout, a dead link) - raise_for_
        # status already fails before _looks_like_fetchable_document is
        # ever reached, exactly as before.
        forbidden_response = _response(status_code=403, content=b"Forbidden", content_type="text/plain")
        with patch("brochure_enrichment.httpx.get", return_value=forbidden_response):
            result = brochure_enrichment._fetch_pdf_bytes(
                "https://www.colliers.com/en-gb/properties/blocked", accept_any_reachable_page=True,
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


class GoogleDriveConfirmParamsTests(unittest.TestCase):
    """_google_drive_confirm_params - the confirmation token(s) needed to
    get past Google Drive's "can't scan this file for viruses"
    interstitial for a large file, in the two real shapes Google has
    shipped (see that function's own docstring)."""

    def test_plain_link_shape(self):
        html = b'<html><a href="/uc?export=download&confirm=t7xK&id=ABC123">Download anyway</a></html>'
        self.assertEqual(brochure_enrichment._google_drive_confirm_params(html), {"confirm": "t7xK"})

    def test_hidden_form_shape_without_uuid(self):
        html = (
            b'<form id="download-form" action="https://drive.usercontent.google.com/download" method="get">'
            b'<input type="hidden" name="id" value="ABC123">'
            b'<input type="hidden" name="export" value="download">'
            b'<input type="hidden" name="confirm" value="t">'
            b"</form>"
        )
        self.assertEqual(brochure_enrichment._google_drive_confirm_params(html), {"confirm": "t"})

    def test_hidden_form_shape_with_uuid(self):
        html = (
            b'<form id="download-form" action="https://drive.usercontent.google.com/download" method="get">'
            b'<input type="hidden" name="id" value="ABC123">'
            b'<input type="hidden" name="confirm" value="t">'
            b'<input type="hidden" name="uuid" value="12345678-90ab-cdef-1234-567890abcdef">'
            b"</form>"
        )
        self.assertEqual(
            brochure_enrichment._google_drive_confirm_params(html),
            {"confirm": "t", "uuid": "12345678-90ab-cdef-1234-567890abcdef"},
        )

    def test_hidden_form_preferred_over_a_coincidental_link_match(self):
        # Both shapes present on the same page - the hidden form (the
        # newer, more complete shape) must win, never the plain link.
        html = (
            b'<input type="hidden" name="confirm" value="real-token">'
            b'<input type="hidden" name="uuid" value="real-uuid">'
            b'<a href="/some-other-link?confirm=stale-token">unrelated</a>'
        )
        self.assertEqual(
            brochure_enrichment._google_drive_confirm_params(html),
            {"confirm": "real-token", "uuid": "real-uuid"},
        )

    def test_no_token_found_returns_empty_dict(self):
        html = b"<html>Google Drive can't scan this file for viruses...</html>"
        self.assertEqual(brochure_enrichment._google_drive_confirm_params(html), {})

    def test_non_utf8_content_returns_empty_dict_without_raising(self):
        self.assertEqual(brochure_enrichment._google_drive_confirm_params(b"\xff\xfe\x00\x01binary junk"), {})

    def test_blank_content_returns_empty_dict(self):
        self.assertEqual(brochure_enrichment._google_drive_confirm_params(b""), {})

    def test_a_visible_non_hidden_input_named_confirm_is_not_used(self):
        # Only type="hidden" inputs count for the form shape - a visible
        # input happening to share the name "confirm" (unconfirmed to ever
        # occur, but costs nothing to guard against) falls through to the
        # plain-link check instead, same as if no hidden input existed.
        html = (
            b'<input type="text" name="confirm" value="not-a-real-token">'
            b'<a href="/uc?export=download&confirm=t7xK&id=ABC123">Download anyway</a>'
        )
        self.assertEqual(brochure_enrichment._google_drive_confirm_params(html), {"confirm": "t7xK"})


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
        # mis-extracted from the interstitial's own HTML content. This
        # particular interstitial has no parseable confirmation token at
        # all (see GoogleDriveConfirmParamsTests/FetchPdfBytesGoogleDrive
        # ConfirmRetryTests below for the token-found retry-success path),
        # so httpx.get here is called exactly once - no retry is even
        # attempted when there's no token to retry with.
        interstitial = _response(
            content=b"<html>Google Drive can't scan this file for viruses...</html>", content_type="text/html",
        )
        with patch("brochure_enrichment.httpx.get", return_value=interstitial) as mock_get:
            result = brochure_enrichment._fetch_pdf_bytes(
                "https://drive.google.com/file/d/1AbCdEfGh_IJK-lmno/view?usp=sharing"
            )

        self.assertIsNone(result)
        mock_get.assert_called_once()

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


class FetchPdfBytesGoogleDriveConfirmRetryTests(EnrichmentTestCase):
    """_fetch_pdf_bytes's one-time Google Drive confirm-token retry - the
    real confirmed case this exists for: a MetSpace listing's brochure
    link resolved to a large Google Drive file ("67 Clerkenwell Rd - 4th
    Floor - Brochure.pdf") that was unreadable through the old single-
    attempt fetch, leaving geocoding nothing but a bare street name to
    guess an address from."""

    def _drive_url(self):
        return "https://drive.google.com/file/d/1AbCdEfGh_IJK-lmno/view?usp=sharing"

    def test_plain_link_shape_interstitial_succeeds_via_retry(self):
        interstitial = _response(
            content=b'<a href="/uc?export=download&confirm=t7xK&id=1AbCdEfGh_IJK-lmno">Download anyway</a>',
            content_type="text/html",
        )
        real_pdf = _response(content=b"%PDF-1.4 real brochure bytes", content_type="application/pdf")
        with patch("brochure_enrichment.httpx.get", side_effect=[interstitial, real_pdf]) as mock_get:
            result = brochure_enrichment._fetch_pdf_bytes(self._drive_url())

        self.assertEqual(result, b"%PDF-1.4 real brochure bytes")
        self.assertEqual(mock_get.call_count, 2)
        retry_call_url = mock_get.call_args_list[1].args[0]
        self.assertEqual(
            retry_call_url, "https://drive.google.com/uc?export=download&id=1AbCdEfGh_IJK-lmno&confirm=t7xK",
        )

    def test_hidden_form_shape_interstitial_succeeds_via_retry(self):
        interstitial = _response(
            content=(
                b'<input type="hidden" name="confirm" value="t">'
                b'<input type="hidden" name="uuid" value="u-1234">'
            ),
            content_type="text/html",
        )
        real_pdf = _response(content=b"%PDF-1.4 real brochure bytes", content_type="application/pdf")
        with patch("brochure_enrichment.httpx.get", side_effect=[interstitial, real_pdf]) as mock_get:
            result = brochure_enrichment._fetch_pdf_bytes(self._drive_url())

        self.assertEqual(result, b"%PDF-1.4 real brochure bytes")
        retry_call_url = mock_get.call_args_list[1].args[0]
        self.assertIn("confirm=t", retry_call_url)
        self.assertIn("uuid=u-1234", retry_call_url)

    def test_retry_itself_still_unreadable_fails_safe(self):
        # The retry found a token and tried it, but the second response
        # ALSO isn't a readable document - falls through to the normal
        # failure path, never a second retry attempt.
        interstitial = _response(
            content=b'<a href="/uc?export=download&confirm=t7xK&id=X">Download anyway</a>', content_type="text/html",
        )
        still_html = _response(content=b"<html>still not a pdf</html>", content_type="text/html")
        with patch("brochure_enrichment.httpx.get", side_effect=[interstitial, still_html]) as mock_get:
            with brochure_enrichment._StatusCapture({}) as sink:
                result = brochure_enrichment._fetch_pdf_bytes(self._drive_url())

        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 2)  # exactly one retry, never a third attempt
        self.assertEqual(sink["status"], brochure_enrichment.STATUS_FETCH_FAILED)

    def test_retry_request_itself_raising_fails_safe(self):
        interstitial = _response(
            content=b'<a href="/uc?export=download&confirm=t7xK&id=X">Download anyway</a>', content_type="text/html",
        )
        with patch(
            "brochure_enrichment.httpx.get", side_effect=[interstitial, httpx.ConnectError("dns failure")],
        ) as mock_get:
            with brochure_enrichment._StatusCapture({}) as sink:
                result = brochure_enrichment._fetch_pdf_bytes(self._drive_url())

        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(sink["status"], brochure_enrichment.STATUS_FETCH_FAILED)
        self.assertIn("confirm-token retry failed", sink["detail"])

    def test_no_token_found_never_retries(self):
        # No confirmation token anywhere on the page - nothing to retry
        # with, so this must fail safe on the FIRST attempt alone.
        interstitial = _response(
            content=b"<html>Google Drive can't scan this file for viruses...</html>", content_type="text/html",
        )
        with patch("brochure_enrichment.httpx.get", return_value=interstitial) as mock_get:
            result = brochure_enrichment._fetch_pdf_bytes(self._drive_url())

        self.assertIsNone(result)
        mock_get.assert_called_once()

    def test_non_drive_url_returning_html_with_a_confirm_looking_string_is_never_retried(self):
        # Scoped strictly to a genuine Google Drive URL (drive_file_id
        # truthy) - a completely unrelated host that happens to return
        # HTML containing something shaped like "confirm=..." must never
        # trigger this retry path at all.
        coincidental_html = _response(
            content=b'<a href="/download?confirm=abc123">Some other confirm link</a>', content_type="text/html",
        )
        with patch("brochure_enrichment.httpx.get", return_value=coincidental_html) as mock_get, \
                patch("brochure_enrichment.resolve_brochure_link", return_value="https://example.com/brochure"):
            result = brochure_enrichment._fetch_pdf_bytes("https://example.com/brochure.pdf")

        self.assertIsNone(result)
        mock_get.assert_called_once()

    def test_successful_first_fetch_never_attempts_a_retry(self):
        real_pdf = _response(content=b"%PDF-1.4 real bytes", content_type="application/pdf")
        with patch("brochure_enrichment.httpx.get", return_value=real_pdf) as mock_get:
            result = brochure_enrichment._fetch_pdf_bytes(self._drive_url())

        self.assertEqual(result, b"%PDF-1.4 real bytes")
        mock_get.assert_called_once()


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


_PITCH_URL = "https://pitch.com/v/1-finsbury-brochure-4jnj9d"


class PitchRendererConfiguredEligibilityTests(EnrichmentTestCase):
    """Mirrors CanvaRendererConfiguredEligibilityTests exactly, for a Pitch
    view link - same CANVA_RENDERER_URL env var gates both, since it's the
    same deployed service (see _canva_renderer_configured's own
    docstring)."""

    def test_pitch_stays_ineligible_without_the_env_var(self):
        self.assertEqual(
            brochure_enrichment.classify_link_eligibility(_PITCH_URL), brochure_enrichment.STATUS_UNSUPPORTED_LINK_TYPE,
        )
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url(_PITCH_URL))
        self.assertFalse(brochure_enrichment._is_eligible_floorplan_url(_PITCH_URL))

    def test_pitch_becomes_eligible_when_renderer_is_configured(self):
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}):
            self.assertIsNone(brochure_enrichment.classify_link_eligibility(_PITCH_URL))
            self.assertTrue(brochure_enrichment._is_eligible_brochure_url(_PITCH_URL))
            self.assertTrue(brochure_enrichment._is_eligible_floorplan_url(_PITCH_URL))


_GPE_FLIPBOOK_URL = "https://fm.gpe.co.uk/v/gpe-nineteen-wells-street-6hqnfd"


class GpeFlipbookRendererConfiguredEligibilityTests(EnrichmentTestCase):
    """Mirrors CanvaRendererConfiguredEligibilityTests/PitchRendererConfigured
    EligibilityTests exactly, for GPE's own branded fm.gpe.co.uk flipbook
    link - same CANVA_RENDERER_URL env var gates all three, since fm.gpe.
    co.uk is confirmed to be the same deployed Pitch mechanism (see is_gpe_
    flipbook_link's own docstring)."""

    def test_gpe_flipbook_stays_ineligible_without_the_env_var(self):
        self.assertEqual(
            brochure_enrichment.classify_link_eligibility(_GPE_FLIPBOOK_URL),
            brochure_enrichment.STATUS_UNSUPPORTED_LINK_TYPE,
        )
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url(_GPE_FLIPBOOK_URL))
        self.assertFalse(brochure_enrichment._is_eligible_floorplan_url(_GPE_FLIPBOOK_URL))

    def test_gpe_flipbook_becomes_eligible_when_renderer_is_configured(self):
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}):
            self.assertIsNone(brochure_enrichment.classify_link_eligibility(_GPE_FLIPBOOK_URL))
            self.assertTrue(brochure_enrichment._is_eligible_brochure_url(_GPE_FLIPBOOK_URL))
            self.assertTrue(brochure_enrichment._is_eligible_floorplan_url(_GPE_FLIPBOOK_URL))


_KITT_URL = (
    "https://brochures.kittoffices.com/brochures/preview?entity%5B9e40cdea-02a1-44a5-9599-"
    "c3ed1567c117%5D=unit&display_label=Open+brochure"
)


class KittRendererConfiguredEligibilityTests(EnrichmentTestCase):
    """Mirrors CanvaRendererConfiguredEligibilityTests/PitchRendererConfigured
    EligibilityTests/GpeFlipbookRendererConfiguredEligibilityTests exactly,
    for Kitt's own brochure-preview app link - same CANVA_RENDERER_URL env
    var gates all four (Kitt confirmed to need its own dedicated render
    function, but it's still the SAME deployed renderer service - see
    is_kitt_brochure_preview_link's own docstring)."""

    def test_kitt_preview_link_stays_ineligible_without_the_env_var(self):
        self.assertEqual(
            brochure_enrichment.classify_link_eligibility(_KITT_URL),
            brochure_enrichment.STATUS_UNSUPPORTED_LINK_TYPE,
        )
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url(_KITT_URL))
        self.assertFalse(brochure_enrichment._is_eligible_floorplan_url(_KITT_URL))

    def test_kitt_preview_link_becomes_eligible_when_renderer_is_configured(self):
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}):
            self.assertIsNone(brochure_enrichment.classify_link_eligibility(_KITT_URL))
            self.assertTrue(brochure_enrichment._is_eligible_brochure_url(_KITT_URL))
            self.assertTrue(brochure_enrichment._is_eligible_floorplan_url(_KITT_URL))


class RenderPlatformLabelTests(unittest.TestCase):
    """_render_platform_label - the single shared check every
    platform_label call site now uses, replacing what used to be
    separately hand-rolled "Pitch" if is_pitch_view_link(url) else
    "Canva" ternaries."""

    def test_canva_url(self):
        self.assertEqual(brochure_enrichment._render_platform_label(_CANVA_URL), "Canva")

    def test_pitch_url(self):
        self.assertEqual(brochure_enrichment._render_platform_label(_PITCH_URL), "Pitch")

    def test_gpe_flipbook_url(self):
        self.assertEqual(brochure_enrichment._render_platform_label(_GPE_FLIPBOOK_URL), "GPE Flipbook")

    def test_kitt_url(self):
        self.assertEqual(brochure_enrichment._render_platform_label(_KITT_URL), "Kitt")


def _canva_pages_response(pages, page_count_detected=None, status_code=200, links=None):
    """A MagicMock httpx.Response shaped like the renderer's own new JSON
    multi-page format (see canva_renderer/app.py's Handler.do_POST) -
    {"pages": [base64 PNG, ...], "page_count_detected": N|None}.

    `links` (default None, meaning "omit the key entirely") mocks an
    older renderer deploy that predates that field, alongside the
    genuine current shape - a list of per-page link-candidate lists, one
    per entry in `pages`, only ever added to the payload when explicitly
    given so every pre-existing call site of this helper (testing before
    this field existed) is completely unaffected."""
    payload = {
        "pages": [base64.b64encode(p).decode("ascii") for p in pages],
        "page_count_detected": page_count_detected,
    }
    if links is not None:
        payload["links"] = links
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


class FetchPitchRenderedPageTests(EnrichmentTestCase):
    """_fetch_pitch_rendered_page - a thin wrapper over the exact same
    _fetch_rendered_page implementation _fetch_canva_rendered_page calls
    (see that shared function's own docstring) - this class only re-
    checks the pieces that could plausibly differ per platform
    (platform_label in log text, its own max_pages_accepted cap); every
    retry/error-handling rule already covered by FetchCanvaRenderedPageTests
    applies identically here since it's the same code path."""

    def test_successful_render_returns_png_bytes(self):
        response = _canva_pages_response([b"\x89PNG real bytes"], page_count_detected=1)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response) as mock_post, \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            result = brochure_enrichment._fetch_pitch_rendered_page(_PITCH_URL)

        self.assertEqual(result, [b"\x89PNG real bytes"])
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.kwargs["json"], {"url": _PITCH_URL})

    def test_successful_render_with_multiple_pages_preserves_order(self):
        pages = [b"\x89PNG p1", b"\x89PNG p2", b"\x89PNG p3"]
        response = _canva_pages_response(pages, page_count_detected=3)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            result = brochure_enrichment._fetch_pitch_rendered_page(_PITCH_URL)

        self.assertEqual(result, pages)

    def test_response_with_more_pages_than_the_main_apps_own_cap_is_truncated(self):
        # Uses _PITCH_MAX_PAGES_ACCEPTED specifically, NOT _CANVA_MAX_
        # PAGES_ACCEPTED - the two are tracked independently (see that
        # constant's own docstring), so this proves the Pitch wrapper
        # passes its own cap through, not Canva's.
        pages = [f"\x89PNG p{i}".encode() for i in range(1, 30)]
        response = _canva_pages_response(pages, page_count_detected=29)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}), \
                patch.object(brochure_enrichment, "_PITCH_MAX_PAGES_ACCEPTED", 5), \
                patch.object(brochure_enrichment, "_CANVA_MAX_PAGES_ACCEPTED", 999):
            result = brochure_enrichment._fetch_pitch_rendered_page(_PITCH_URL)

        self.assertEqual(len(result), 5)
        self.assertEqual(result, pages[:5])

    def test_successful_render_logs_pitch_not_canva_in_the_success_line(self):
        response = _canva_pages_response([b"\x89PNG real bytes"], page_count_detected=1)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}), \
                patch("brochure_enrichment.sys.stderr") as mock_stderr:
            brochure_enrichment._fetch_pitch_rendered_page(_PITCH_URL)

        logged = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        self.assertIn("Pitch render succeeded", logged)
        self.assertNotIn("Canva render succeeded", logged)

    def test_renderer_unreachable_returns_none_and_records_fetch_failed(self):
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", side_effect=httpx.ConnectError("dns failure")):
            with brochure_enrichment._StatusCapture({}) as sink:
                result = brochure_enrichment._fetch_pitch_rendered_page(_PITCH_URL)

        self.assertIsNone(result)
        self.assertEqual(sink["status"], brochure_enrichment.STATUS_FETCH_FAILED)
        self.assertIn("Pitch renderer unreachable", sink["detail"])

    def test_renderer_reports_safe_failure_returns_none_with_pitch_labeled_reason(self):
        response = MagicMock(
            status_code=422, headers={"content-type": "application/json"},
            json=MagicMock(return_value={"error": "render_failed", "reason": "private design"}),
        )
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response):
            with brochure_enrichment._StatusCapture({}) as sink:
                result = brochure_enrichment._fetch_pitch_rendered_page(_PITCH_URL)

        self.assertIsNone(result)
        self.assertEqual(sink["status"], brochure_enrichment.STATUS_RENDER_FAILED)
        self.assertIn("Pitch render failed", sink["detail"])
        self.assertIn("private design", sink["detail"])

    def test_malformed_pages_payload_returns_none_and_records_render_failed(self):
        response = MagicMock(
            status_code=200, headers={"content-type": "application/json"},
            json=MagicMock(return_value={"not_pages": []}),
        )
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            with brochure_enrichment._StatusCapture({}) as sink:
                result = brochure_enrichment._fetch_pitch_rendered_page(_PITCH_URL)

        self.assertIsNone(result)
        self.assertEqual(sink["status"], brochure_enrichment.STATUS_RENDER_FAILED)


class FetchGpeFlipbookRenderedPageTests(EnrichmentTestCase):
    """_fetch_gpe_flipbook_rendered_page - a thin wrapper over the exact
    same _fetch_rendered_page implementation _fetch_canva_rendered_page/
    _fetch_pitch_rendered_page call, and it shares Pitch's OWN max_pages_
    accepted cap rather than a separate one (see _PITCH_MAX_PAGES_
    ACCEPTED's own docstring on why - fm.gpe.co.uk is confirmed to be the
    identical underlying Pitch mechanism). Only re-checks the pieces that
    could plausibly differ (platform_label in log text, its use of Pitch's
    cap) - every retry/error-handling rule is already covered by
    FetchCanvaRenderedPageTests/FetchPitchRenderedPageTests, same code
    path."""

    def test_successful_render_returns_png_bytes(self):
        response = _canva_pages_response([b"\x89PNG real bytes"], page_count_detected=1)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response) as mock_post, \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            result = brochure_enrichment._fetch_gpe_flipbook_rendered_page(_GPE_FLIPBOOK_URL)

        self.assertEqual(result, [b"\x89PNG real bytes"])
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.kwargs["json"], {"url": _GPE_FLIPBOOK_URL})

    def test_shares_pitchs_own_max_pages_accepted_cap_not_canvas(self):
        pages = [f"\x89PNG p{i}".encode() for i in range(1, 30)]
        response = _canva_pages_response(pages, page_count_detected=29)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}), \
                patch.object(brochure_enrichment, "_PITCH_MAX_PAGES_ACCEPTED", 5), \
                patch.object(brochure_enrichment, "_CANVA_MAX_PAGES_ACCEPTED", 999):
            result = brochure_enrichment._fetch_gpe_flipbook_rendered_page(_GPE_FLIPBOOK_URL)

        self.assertEqual(len(result), 5)
        self.assertEqual(result, pages[:5])

    def test_successful_render_logs_gpe_flipbook_not_canva_or_pitch(self):
        response = _canva_pages_response([b"\x89PNG real bytes"], page_count_detected=1)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}), \
                patch("brochure_enrichment.sys.stderr") as mock_stderr:
            brochure_enrichment._fetch_gpe_flipbook_rendered_page(_GPE_FLIPBOOK_URL)

        logged = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        self.assertIn("GPE Flipbook render succeeded", logged)
        self.assertNotIn("Canva render succeeded", logged)
        self.assertNotIn("Pitch render succeeded", logged)

    def test_renderer_reports_email_gate_failure_returns_none_with_reason(self):
        # The email-gate case (see canva_renderer/app.py's own render_
        # pitch_page_async docstring) surfaces here exactly like any other
        # clean RenderError - no special-casing needed on this side.
        response = MagicMock(
            status_code=422, headers={"content-type": "application/json"},
            json=MagicMock(return_value={
                "error": "render_failed",
                "reason": "presentation requires an email to open (access-gated, not publicly viewable)",
            }),
        )
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response):
            with brochure_enrichment._StatusCapture({}) as sink:
                result = brochure_enrichment._fetch_gpe_flipbook_rendered_page(_GPE_FLIPBOOK_URL)

        self.assertIsNone(result)
        self.assertEqual(sink["status"], brochure_enrichment.STATUS_RENDER_FAILED)
        self.assertIn("requires an email to open", sink["detail"])


class FetchKittRenderedPageTests(EnrichmentTestCase):
    """_fetch_kitt_rendered_page - a thin wrapper over the exact same
    _fetch_rendered_page implementation _fetch_canva_rendered_page/
    _fetch_pitch_rendered_page/_fetch_gpe_flipbook_rendered_page call,
    but with its own dedicated _KITT_MAX_PAGES_ACCEPTED cap (Kitt's
    render_kitt_page_async is a genuinely new render function - see
    is_kitt_brochure_preview_link's own docstring - not routed through
    Canva's or Pitch's own renderer-side function the way GPE is). Only
    re-checks the pieces that could plausibly differ (platform_label in
    log text, its use of its own cap) - every retry/error-handling rule
    is already covered by FetchCanvaRenderedPageTests/
    FetchPitchRenderedPageTests, same code path."""

    def test_successful_render_returns_png_bytes(self):
        response = _canva_pages_response([b"\x89PNG real bytes"], page_count_detected=1)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response) as mock_post, \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            result = brochure_enrichment._fetch_kitt_rendered_page(_KITT_URL)

        self.assertEqual(result, [b"\x89PNG real bytes"])
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.kwargs["json"], {"url": _KITT_URL})

    def test_uses_its_own_max_pages_accepted_cap_not_canvas_or_pitchs(self):
        pages = [f"\x89PNG p{i}".encode() for i in range(1, 25)]
        response = _canva_pages_response(pages, page_count_detected=24)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}), \
                patch.object(brochure_enrichment, "_KITT_MAX_PAGES_ACCEPTED", 4), \
                patch.object(brochure_enrichment, "_CANVA_MAX_PAGES_ACCEPTED", 999), \
                patch.object(brochure_enrichment, "_PITCH_MAX_PAGES_ACCEPTED", 999):
            result = brochure_enrichment._fetch_kitt_rendered_page(_KITT_URL)

        self.assertEqual(len(result), 4)
        self.assertEqual(result, pages[:4])

    def test_successful_render_logs_kitt_not_canva_pitch_or_gpe(self):
        response = _canva_pages_response([b"\x89PNG real bytes"], page_count_detected=1)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}), \
                patch("brochure_enrichment.sys.stderr") as mock_stderr:
            brochure_enrichment._fetch_kitt_rendered_page(_KITT_URL)

        logged = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        self.assertIn("Kitt render succeeded", logged)
        self.assertNotIn("Canva render succeeded", logged)
        self.assertNotIn("Pitch render succeeded", logged)
        self.assertNotIn("GPE Flipbook render succeeded", logged)

    def test_renderer_reports_safe_failure_returns_none_with_kitt_labeled_reason(self):
        response = MagicMock(
            status_code=422, headers={"content-type": "application/json"},
            json=MagicMock(return_value={"error": "render_failed", "reason": "no recognized scroll container"}),
        )
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response):
            with brochure_enrichment._StatusCapture({}) as sink:
                result = brochure_enrichment._fetch_kitt_rendered_page(_KITT_URL)

        self.assertIsNone(result)
        self.assertEqual(sink["status"], brochure_enrichment.STATUS_RENDER_FAILED)
        self.assertIn("Kitt render failed", sink["detail"])


class FetchRenderedPageWithLinksTests(EnrichmentTestCase):
    """fetch_rendered_page_with_links - the new, additive entry point for
    the paste-a-link flow (see app.py's own _fetch_pasted_link). Never
    called by the existing per-unit enrichment path - that coverage
    (FetchCanvaRenderedPageTests/FetchPitchRenderedPageTests above)
    already confirms _fetch_canva_rendered_page/_fetch_pitch_rendered_
    page keep their own plain list[bytes]-or-None contract unaffected by
    this function's addition."""

    def test_canva_url_returns_pages_and_links_together(self):
        pages = [b"\x89PNG p1", b"\x89PNG p2"]
        links = [
            [{"href": "https://colliers.com/kingsland-house", "text": "LINK TO BROCHURE"}],
            [{"href": "https://blob.example.com/gloucester.pdf", "text": "27-29 Gloucester Place"}],
        ]
        response = _canva_pages_response(pages, page_count_detected=2, links=links)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            result_pages, result_links = brochure_enrichment.fetch_rendered_page_with_links(_CANVA_URL)

        self.assertEqual(result_pages, pages)
        self.assertEqual(result_links, links)

    def test_pitch_url_returns_pages_and_links_together(self):
        pages = [b"\x89PNG deck"]
        links = [[{"href": "https://example.com/deck.pdf", "text": "View the deck"}]]
        response = _canva_pages_response(pages, page_count_detected=1, links=links)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response):
            result_pages, result_links = brochure_enrichment.fetch_rendered_page_with_links(_PITCH_URL)

        self.assertEqual(result_pages, pages)
        self.assertEqual(result_links, links)

    def test_an_older_renderer_response_with_no_links_field_at_all_yields_empty_lists(self):
        # Backward compatibility with a renderer deploy that predates this
        # field entirely - never a KeyError, [] per page instead.
        pages = [b"\x89PNG p1", b"\x89PNG p2"]
        response = _canva_pages_response(pages, page_count_detected=2)  # links=None (omitted)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            result_pages, result_links = brochure_enrichment.fetch_rendered_page_with_links(_CANVA_URL)

        self.assertEqual(result_pages, pages)
        self.assertEqual(result_links, [[], []])

    def test_links_are_truncated_in_lockstep_with_a_capped_pages_list(self):
        pages = [f"\x89PNG p{i}".encode() for i in range(1, 8)]
        links = [[{"href": f"https://example.com/{i}.pdf", "text": str(i)}] for i in range(1, 8)]
        response = _canva_pages_response(pages, page_count_detected=7, links=links)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}), \
                patch.object(brochure_enrichment, "_CANVA_MAX_PAGES_ACCEPTED", 3):
            result_pages, result_links = brochure_enrichment.fetch_rendered_page_with_links(_CANVA_URL)

        self.assertEqual(len(result_pages), 3)
        self.assertEqual(result_links, links[:3])

    def test_render_failure_returns_none_none_not_a_partial_tuple(self):
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", side_effect=Exception("connection refused")):
            result_pages, result_links = brochure_enrichment.fetch_rendered_page_with_links(_CANVA_URL)

        self.assertIsNone(result_pages)
        self.assertIsNone(result_links)

    def test_a_url_matching_neither_platform_returns_none_none_without_raising(self):
        result_pages, result_links = brochure_enrichment.fetch_rendered_page_with_links(
            "https://example.com/not-canva-or-pitch"
        )
        self.assertIsNone(result_pages)
        self.assertIsNone(result_links)

    def test_gpe_flipbook_url_returns_pages_and_links_together(self):
        pages = [b"\x89PNG deck"]
        links = [[{"href": "https://example.com/deck.pdf", "text": "View the deck"}]]
        response = _canva_pages_response(pages, page_count_detected=1, links=links)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response):
            result_pages, result_links = brochure_enrichment.fetch_rendered_page_with_links(_GPE_FLIPBOOK_URL)

        self.assertEqual(result_pages, pages)
        self.assertEqual(result_links, links)

    def test_kitt_url_returns_pages_and_links_together(self):
        pages = [b"\x89PNG chunk1", b"\x89PNG chunk2"]
        links = [
            [{"href": "https://my.matterport.com/show/?m=uhTx33agohq", "text": "View virtual tour"}],
            [],
        ]
        response = _canva_pages_response(pages, page_count_detected=None, links=links)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            result_pages, result_links = brochure_enrichment.fetch_rendered_page_with_links(_KITT_URL)

        self.assertEqual(result_pages, pages)
        self.assertEqual(result_links, links)

    def test_existing_canva_wrapper_still_returns_plain_list_unaffected(self):
        # The exact same underlying call, but through the OLD/existing
        # entry point - confirms it's still untouched by this addition.
        pages = [b"\x89PNG p1"]
        links = [[{"href": "https://example.com/a.pdf", "text": "A"}]]
        response = _canva_pages_response(pages, page_count_detected=1, links=links)
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment.httpx.post", return_value=response), \
                patch("brochure_enrichment._canva_renderer_auth_headers", return_value={}):
            result = brochure_enrichment._fetch_canva_rendered_page(_CANVA_URL)

        self.assertEqual(result, pages)  # plain list[bytes], never a tuple


class FetchPdfBytesPitchDispatchTests(EnrichmentTestCase):
    """_fetch_pdf_bytes's own Pitch dispatch branch - mirrors whatever
    coverage exists for its Canva branch (see FetchPdfBytesCanvaRoutingTests
    if present) at the one new call site this feature adds."""

    def test_pitch_url_is_routed_to_the_pitch_renderer_when_configured(self):
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_pitch_rendered_page", return_value=[b"\x89PNG"]) as mock_fetch, \
                patch("brochure_enrichment._fetch_canva_rendered_page") as mock_canva_fetch:
            result = brochure_enrichment._fetch_pdf_bytes(_PITCH_URL)

        self.assertEqual(result, [b"\x89PNG"])
        mock_fetch.assert_called_once_with(_PITCH_URL)
        mock_canva_fetch.assert_not_called()

    def test_pitch_url_falls_through_to_generic_fetch_when_unconfigured(self):
        # Same "correct independent of configuration" contract Canva's own
        # branch already has (see _fetch_pdf_bytes' own docstring) - never
        # attempts a renderer call this deployment was never told about.
        with patch.dict(os.environ, {}, clear=True), \
                patch("brochure_enrichment._fetch_pitch_rendered_page") as mock_fetch, \
                patch("brochure_enrichment.resolve_brochure_link", return_value=_PITCH_URL), \
                patch("brochure_enrichment.httpx.get", side_effect=httpx.ConnectError("dns failure")):
            brochure_enrichment._fetch_pdf_bytes(_PITCH_URL)

        mock_fetch.assert_not_called()


class FetchPdfBytesGpeFlipbookDispatchTests(EnrichmentTestCase):
    """_fetch_pdf_bytes's own GPE flipbook dispatch branch - mirrors
    FetchPdfBytesPitchDispatchTests exactly, at the one new call site this
    feature adds."""

    def test_gpe_flipbook_url_is_routed_to_the_gpe_flipbook_fetch_when_configured(self):
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch(
                    "brochure_enrichment._fetch_gpe_flipbook_rendered_page", return_value=[b"\x89PNG"],
                ) as mock_fetch, \
                patch("brochure_enrichment._fetch_canva_rendered_page") as mock_canva_fetch, \
                patch("brochure_enrichment._fetch_pitch_rendered_page") as mock_pitch_fetch:
            result = brochure_enrichment._fetch_pdf_bytes(_GPE_FLIPBOOK_URL)

        self.assertEqual(result, [b"\x89PNG"])
        mock_fetch.assert_called_once_with(_GPE_FLIPBOOK_URL)
        mock_canva_fetch.assert_not_called()
        mock_pitch_fetch.assert_not_called()

    def test_gpe_flipbook_url_falls_through_to_generic_fetch_when_unconfigured(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("brochure_enrichment._fetch_gpe_flipbook_rendered_page") as mock_fetch, \
                patch("brochure_enrichment.resolve_brochure_link", return_value=_GPE_FLIPBOOK_URL), \
                patch("brochure_enrichment.httpx.get", side_effect=httpx.ConnectError("dns failure")):
            brochure_enrichment._fetch_pdf_bytes(_GPE_FLIPBOOK_URL)

        mock_fetch.assert_not_called()

    def test_a_gpe_rows_plain_pdf_link_never_triggers_the_gpe_flipbook_fetch(self):
        # Detection is purely URL-shape-based, never inferred from
        # row.provider - a GPE upload with an ordinary direct .pdf link
        # must go through the completely unaffected, ordinary direct-
        # fetch path, never anywhere near the flipbook renderer.
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_gpe_flipbook_rendered_page") as mock_gpe_fetch, \
                patch("brochure_enrichment._fetch_canva_rendered_page") as mock_canva_fetch, \
                patch("brochure_enrichment._fetch_pitch_rendered_page") as mock_pitch_fetch, \
                patch("brochure_enrichment.httpx.get", return_value=_response()):
            brochure_enrichment._fetch_pdf_bytes("https://example.com/GPE-brochure.pdf")

        mock_gpe_fetch.assert_not_called()
        mock_canva_fetch.assert_not_called()
        mock_pitch_fetch.assert_not_called()

    def test_a_gpe_rows_plain_pitch_link_is_routed_to_pitch_never_gpe_flipbook(self):
        # A GPE row genuinely using a plain pitch.com/v/... link (not the
        # fm.gpe.co.uk custom domain) must go through the ordinary Pitch
        # path unaffected - the two are distinguished purely by host.
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_pitch_rendered_page", return_value=[b"\x89PNG"]) as mock_pitch_fetch, \
                patch("brochure_enrichment._fetch_gpe_flipbook_rendered_page") as mock_gpe_fetch:
            result = brochure_enrichment._fetch_pdf_bytes(_PITCH_URL)

        self.assertEqual(result, [b"\x89PNG"])
        mock_pitch_fetch.assert_called_once_with(_PITCH_URL)
        mock_gpe_fetch.assert_not_called()


class FetchPdfBytesKittDispatchTests(EnrichmentTestCase):
    """_fetch_pdf_bytes's own Kitt dispatch branch - mirrors
    FetchPdfBytesPitchDispatchTests/FetchPdfBytesGpeFlipbookDispatchTests
    exactly, at the one new call site this feature adds."""

    def test_kitt_url_is_routed_to_the_kitt_fetch_when_configured(self):
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch("brochure_enrichment._fetch_kitt_rendered_page", return_value=[b"\x89PNG"]) as mock_fetch, \
                patch("brochure_enrichment._fetch_canva_rendered_page") as mock_canva_fetch, \
                patch("brochure_enrichment._fetch_pitch_rendered_page") as mock_pitch_fetch, \
                patch("brochure_enrichment._fetch_gpe_flipbook_rendered_page") as mock_gpe_fetch:
            result = brochure_enrichment._fetch_pdf_bytes(_KITT_URL)

        self.assertEqual(result, [b"\x89PNG"])
        mock_fetch.assert_called_once_with(_KITT_URL)
        mock_canva_fetch.assert_not_called()
        mock_pitch_fetch.assert_not_called()
        mock_gpe_fetch.assert_not_called()

    def test_kitt_url_falls_through_to_generic_fetch_when_unconfigured(self):
        # Same "correct independent of configuration" contract Canva's/
        # Pitch's/GPE's own branches already have (see _fetch_pdf_bytes'
        # own docstring) - never attempts a renderer call this deployment
        # was never told about.
        with patch.dict(os.environ, {}, clear=True), \
                patch("brochure_enrichment._fetch_kitt_rendered_page") as mock_fetch, \
                patch("brochure_enrichment.resolve_brochure_link", return_value=_KITT_URL), \
                patch("brochure_enrichment.httpx.get", side_effect=httpx.ConnectError("dns failure")):
            brochure_enrichment._fetch_pdf_bytes(_KITT_URL)

        mock_fetch.assert_not_called()


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

    def test_html_page_accepted_with_accept_any_reachable_page(self):
        # A genuine per-unit listing webpage (e.g. a real colliers.com
        # property page) - the confirmed gap this param closes - is real
        # HTML, not a PDF/image, but must still count as valid here.
        self.assertTrue(
            brochure_enrichment._looks_like_fetchable_document(
                "text/html", b"<html>Kingsland House</html>", accept_any_reachable_page=True,
            ),
        )

    def test_accept_any_reachable_page_overrides_every_other_check(self):
        # Even content that would otherwise be rejected outright (a docx,
        # an explicit 404 page) counts as reachable - this param is
        # deliberately about reachability alone, not content type at all.
        self.assertTrue(
            brochure_enrichment._looks_like_fetchable_document(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK\x03\x04...",
                accept_any_reachable_page=True,
            ),
        )

    def test_pdf_still_accepted_when_accept_any_reachable_page_is_false(self):
        # The new param's default (False) leaves every existing behavior
        # in this class completely unchanged.
        self.assertTrue(brochure_enrichment._looks_like_fetchable_document("application/pdf", b"anything"))


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

    def test_real_regents_wharf_wrapped_building_name_matches_via_parenthetical_content(self):
        # Real, confirmed production case: a UNION Regents Wharf row states
        # its own building as "Regents Wharf (The Mill)" - a shared
        # development wrapper around the real sub-building name - while
        # that SAME brochure's own Gemini extraction (23+ units across 4
        # sub-buildings, several floors each) states each unit's building
        # as the bare sub-building name alone. Before tier 3e existed, NO
        # building-identity tier ever matched this shape at all, so
        # State Of Space stayed permanently blank for every Regents Wharf
        # row regardless of floor_unit - confirming this is a genuine
        # building-match gap, not a floor-matching one.
        row = ListingRow(building="Regents Wharf (The Mill)", floor_unit="1st Floor")
        units = [
            {"building": "The Mill", "floor_unit": "1st Floor", "state_of_space": "Fully Fitted"},
            {"building": "The Mill", "floor_unit": "2nd Floor", "state_of_space": "Fully Fitted"},
            {"building": "The Canal Building", "floor_unit": "1st Floor", "state_of_space": "CAT A"},
        ]

        matched = brochure_enrichment._match_unit(row, units)

        self.assertEqual(matched["state_of_space"], "Fully Fitted")
        self.assertEqual(matched["building"], "The Mill")

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

    def test_floor_number_fallback_does_not_apply_when_row_floor_has_no_digit_or_recognized_word(self):
        row = ListingRow(building="A", floor_unit="Basement")
        units = [
            {"building": "A", "floor_unit": "1st Floor", "special_features": "One"},
            {"building": "A", "floor_unit": "2nd Floor", "special_features": "Two"},
        ]

        self.assertIsNone(brochure_enrichment._match_unit(row, units))

    def test_ground_floor_label_variant_resolves_against_the_brochures_own_ground_floor_unit(self):
        # Real confirmed gap: a real Ivybridge House row's own floor_unit
        # "G - Strand" never matched its own brochure's "Ground Floor"
        # unit at all (no digit, no ordinal word on either side) - State
        # Of Space/Special Features stayed permanently blank for this
        # floor on every re-upload of the same real document.
        units = [
            {"building": "Ivybridge House", "floor_unit": "Ground Floor", "state_of_space": "Fully Fitted"},
            {"building": "Ivybridge House", "floor_unit": "1st Floor", "state_of_space": "CAT A"},
        ]
        row = ListingRow(building="Ivybridge House", floor_unit="G - Strand")

        matched = brochure_enrichment._match_unit(row, units)

        self.assertEqual(matched["state_of_space"], "Fully Fitted")

    def test_ground_floor_never_confused_with_lower_ground(self):
        # The genuinely different real-world pair confirmed in the same
        # Ivybridge House brochure: "LG"/"Lower Ground" must never match
        # against a brochure's own separate "Ground Floor" unit.
        units = [
            {"building": "Ivybridge House", "floor_unit": "Ground Floor", "state_of_space": "Fully Fitted"},
            {"building": "Ivybridge House", "floor_unit": "Lower Ground Floor", "state_of_space": "Shell & Core"},
        ]
        row = ListingRow(building="Ivybridge House", floor_unit="LG")

        # Neither the exact-text nor the floor-number tier resolves this
        # (LG has no digit and doesn't match the Ground-floor pattern) -
        # falls through to no match, same as before Ground floor existed,
        # rather than guessing between two genuinely different spaces.
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

    def test_tier_5_bare_street_reference_matches_its_own_numbered_building(self):
        # Real confirmed case: a MetSpace email listing's only location
        # text was "Clerkenwell Road" (no house number, no building name -
        # extract_email.py correctly extracted it as such), but its own
        # uniquely-linked brochure states the real numbered building "67
        # Clerkenwell Road" - tiers 1-4 can never bridge this (tier 4b
        # requires row_building's OWN house number to corroborate with,
        # which a bare street reference has none of).
        indices = brochure_enrichment._building_identity_matches("Clerkenwell Road", ["67 Clerkenwell Road"])
        self.assertEqual(indices, [0])

    def test_tier_5_rejects_two_different_numbered_buildings_on_the_same_street(self):
        # A portfolio brochure spanning one street - two GENUINELY
        # different real buildings, both on Clerkenwell Road - must stay
        # unresolved, same "wrong guess is worse than blank" rule every
        # other tier already follows.
        indices = brochure_enrichment._building_identity_matches(
            "Clerkenwell Road", ["67 Clerkenwell Road", "82 Clerkenwell Road"],
        )
        self.assertEqual(indices, [])

    def test_tier_5_never_fires_when_row_building_has_its_own_house_number(self):
        # row_building already states its own house number - not a bare
        # street reference at all, so this must go through the existing
        # tiers only, completely unchanged. "44 Clerkenwell Road" is a
        # DIFFERENT real building from "67 Clerkenwell Road" - tier 5
        # firing here (ignoring the disagreeing house numbers) would be a
        # real, dangerous regression, not just an unnecessary path.
        indices = brochure_enrichment._building_identity_matches("44 Clerkenwell Road", ["67 Clerkenwell Road"])
        self.assertEqual(indices, [])

    def test_tier_5_accepts_several_floors_of_the_same_numbered_building(self):
        # Several units of the SAME real building (its own several floors,
        # each independently restating "67 Clerkenwell Road") is the
        # expected case, not an ambiguity - same _distinct_building_group
        # allowance tier 4 already has.
        indices = brochure_enrichment._building_identity_matches(
            "Clerkenwell Road", ["67 Clerkenwell Road", "67 Clerkenwell Road"],
        )
        self.assertEqual(indices, [0, 1])

    def test_tier_5_requires_the_full_street_name_not_just_a_partial_overlap(self):
        # "Clerkenwell" ({"clerkenwell"}) must never equal "67 Clerkenwell
        # Road" ({"clerkenwell", "road"}) - an extra word (never stripped
        # by _street_name_words, which only drops digit/range-connector
        # tokens, not a genuine street-suffix word) is real evidence of a
        # more specific, different street reference, not the same one
        # loosely restated. ("Kings"/"Kings Road" isn't usable for this
        # case - tier 3's own trailing-street-suffix-word stripping already
        # resolves that pair before tier 5 is ever reached; "Clerkenwell"
        # has only one word, so tier 3's own len>1 guard leaves it
        # untouched, genuinely reaching tier 5.)
        indices = brochure_enrichment._building_identity_matches("Clerkenwell", ["67 Clerkenwell Road"])
        self.assertEqual(indices, [])

    def test_tier_5_available_even_with_no_address_data_at_all(self):
        # Unlike tier 4, tier 5 never reads candidate_addresses - a
        # building_features-shaped caller (candidate_addresses=None, see
        # MatchBuildingFeatureTests) must still benefit from it.
        indices = brochure_enrichment._building_identity_matches("Clerkenwell Road", ["67 Clerkenwell Road"], None)
        self.assertEqual(indices, [0])

    def test_tier_3b_trailing_parenthetical_is_stripped(self):
        # Real confirmed case: a real MetSpace email's own "141 Fenchurch
        # Street (Monument)" (Monument being the nearest Underground
        # station, appended purely for the reader's own area orientation)
        # never matched its own real brochure's building text ("141
        # Fenchurch Street") at all - three real floors of this exact
        # building enriched with ZERO fields changed despite each one's
        # own brochure containing real, matchable unit content.
        indices = brochure_enrichment._building_identity_matches(
            "141 Fenchurch Street (Monument)", ["141 Fenchurch Street"],
        )
        self.assertEqual(indices, [0])

    def test_tier_3b_two_candidates_naming_the_same_building_both_match(self):
        # Uses _distinct_building_group (same allowance tier 1's own exact
        # match already has for a real schedule-of-areas brochure with
        # several units for the SAME building) - two candidates that both
        # stripped-match AND share the identical raw building text are the
        # expected multi-floor case, not an ambiguity: every index is
        # returned, disambiguated further by the caller's own floor/size
        # narrowing, same as tier 1. (Tier 3b's own stripping is one-sided -
        # only row_building's side is ever stripped, candidates are compared
        # as-is - so two candidates sharing this tier's own matching key
        # necessarily share the identical raw text too; a genuine
        # different-building collision under one-sided stripping is tier
        # 3c's own mirror-image case, covered separately below.)
        indices = brochure_enrichment._building_identity_matches(
            "141 Fenchurch Street (Monument)", ["141 Fenchurch Street", "141 Fenchurch Street"],
        )
        self.assertEqual(indices, [0, 1])

    def test_tier_3b_ambiguous_match_is_rejected(self):
        # Two DIFFERENT buildings that both happen to strip to the same
        # parenthetical-stripped key - same "incorrect enrichment is worse
        # than a blank field" rejection every other weak tier already
        # applies.
        indices = brochure_enrichment._building_identity_matches(
            "Example House (Monument)", ["Example House - 10 Alpha Street", "Example House - 50 Beta Street"],
        )
        self.assertEqual(indices, [])

    def test_tier_3b_never_fires_when_row_building_has_no_parenthetical(self):
        # A plain row_building with nothing to strip must fall through to
        # whatever tier (if any) genuinely applies, never be affected by
        # this tier at all.
        indices = brochure_enrichment._building_identity_matches("141 Fenchurch Street", ["Somewhere Else"])
        self.assertEqual(indices, [])

    def test_tier_3c_matches_when_only_the_candidate_side_carries_the_suffix(self):
        # The mirror of tier 3b (see this test class's own docstring,
        # tier 3c) - confirmed against a real Colliers brochure whose own
        # Gemini extraction appended a floor's tenant/operator name in
        # parentheses to a building text the row's own side never carried
        # at all: row.building "210 Euston Road" vs the brochure's own
        # extracted unit building "210 Euston Road (Fora Enterprise)".
        # Tier 3b's own original assumption (a candidate carrying this
        # kind of suffix must already be a genuinely distinct name, since
        # tier 1's exact comparison would already have caught a real
        # match) turns out not to hold when it's the CANDIDATE, not the
        # row, that's the one carrying the extra text.
        indices = brochure_enrichment._building_identity_matches(
            "210 Euston Road", ["210 Euston Road (Fora Enterprise)"],
        )
        self.assertEqual(indices, [0])

    def test_tier_3c_ambiguous_match_is_rejected(self):
        # Two DIFFERENT candidate buildings that both happen to strip to
        # the same parenthetical-stripped key - same "incorrect
        # enrichment is worse than a blank field" rejection every other
        # weak tier already applies, now also enforced in this new
        # direction.
        indices = brochure_enrichment._building_identity_matches(
            "210 Euston Road", ["210 Euston Road (Fora Enterprise)", "210 Euston Road (WeWork)"],
        )
        self.assertEqual(indices, [])

    def test_tier_3d_matches_when_only_the_candidate_side_carries_a_leading_the(self):
        # The real Regent's Wharf case (see this test class's own
        # docstring, tier 3d): a provider's own spreadsheet row named
        # just "Canal Building"/"Packing House", the same real brochure's
        # own Gemini-extracted building text carrying a leading "The" the
        # row's own side never had at all.
        indices = brochure_enrichment._building_identity_matches(
            "Canal Building", ["Thorley Works", "The Canal Building", "The Mill", "The Packing House"],
        )
        self.assertEqual(indices, [1])

        indices = brochure_enrichment._building_identity_matches(
            "Packing House", ["Thorley Works", "The Canal Building", "The Mill", "The Packing House"],
        )
        self.assertEqual(indices, [3])

    def test_tier_3d_matches_when_only_the_row_side_carries_a_leading_the(self):
        # The mirror direction - unlike tiers 3b/3c's own deliberate row-
        # side-only/candidate-side-only split, tier 3d strips both sides
        # at once, so a row carrying the leading article the candidate
        # lacks must match too.
        indices = brochure_enrichment._building_identity_matches("The Mill", ["Mill"])
        self.assertEqual(indices, [0])

    def test_tier_3d_two_candidates_naming_the_same_building_both_match(self):
        # Two candidate entries sharing the identical leading-article-
        # stripped key AND the identical raw text - the real Regent's
        # Wharf schedule-of-areas shape (several floors, each entry
        # repeating "The Packing House" verbatim) - are the expected
        # multi-floor case via _distinct_building_group, not an ambiguity;
        # see test_tier_3b_two_candidates_naming_the_same_building_both_
        # match above for the identical philosophy. Neither candidate here
        # is "Point" itself, so tier 1's own exact comparison never
        # intercepts either of them first.
        indices = brochure_enrichment._building_identity_matches("Point", ["The Point", "The Point"])
        self.assertEqual(indices, [0, 1])

    # No standalone "tier 3d ambiguous match is rejected, different raw
    # candidates" test exists here, unlike tiers 2/3/3b/3c above - this
    # tier's own stripping (a single leading "the " token, symmetric on
    # both sides) has no realistic shape where two DIFFERENT raw building
    # names collide onto the same stripped key the way an address suffix
    # or a street-type word can; the only strings that ever collide under
    # it are "The X" and "X" for the same X, which is precisely two
    # phrasings of the SAME building, not a genuine ambiguity - see
    # test_tier_3d_two_candidates_naming_the_same_building_both_match
    # above. test_tier_3d_does_not_introduce_false_positives_between_two_
    # the_prefixed_buildings below covers the real risk this tier could
    # introduce instead: two buildings that both happen to start with
    # "The" must still resolve independently.

    def test_tier_3d_does_not_introduce_false_positives_between_two_the_prefixed_buildings(self):
        # Two genuinely different buildings that both happen to start
        # with "The" must still resolve independently, not collide with
        # each other just because they share the leading article.
        indices = brochure_enrichment._building_identity_matches(
            "Point", ["The Mill", "The Point"],
        )
        self.assertEqual(indices, [1])

    def test_tier_3e_parenthetical_content_is_the_real_name(self):
        # Real, confirmed UNION Regents Wharf case: a provider's own
        # spreadsheet states each row's building as "Regents Wharf
        # (<sub-building>)" - "Regents Wharf" is a shared development
        # wrapper with no identity of its own, while that SAME real
        # brochure's own Gemini extraction (and every sibling row
        # referencing the same sub-building) uses the bare name alone.
        # Confirmed real production gap this closes: State Of Space stayed
        # permanently blank for every "Regents Wharf (...)" row because
        # _match_unit's own building match returned nothing at all for
        # this shape - no existing tier (row-side-only or candidate-side-
        # only trailing-parenthetical-stripped) ever tries treating the
        # row's own parenthetical CONTENT as the real match target.
        indices = brochure_enrichment._building_identity_matches(
            "Regents Wharf (The Mill)", ["Thorley Works", "The Canal Building", "The Mill", "The Packing House"],
        )
        self.assertEqual(indices, [2])

    def test_tier_3e_matches_several_floors_of_the_same_sub_building(self):
        # Same _distinct_building_group allowance every other stripped tier
        # gets - a real schedule of areas with several floors sharing the
        # identical bare sub-building name is the expected case, not an
        # ambiguity.
        indices = brochure_enrichment._building_identity_matches(
            "Regents Wharf (The Packing House)", ["The Packing House"] * 7,
        )
        self.assertEqual(indices, list(range(7)))

    def test_tier_3e_tolerates_a_leading_the_mismatch_either_way(self):
        # The row's own parenthetical content routinely still carries the
        # leading article the brochure's own extraction sometimes omits
        # (or vice versa) - tolerated via the same _strip_leading_the
        # comparison tier 3d already uses, tried alongside the exact
        # comparison in one pass.
        self.assertEqual(
            brochure_enrichment._building_identity_matches("Regents Wharf (The Mill)", ["Mill"]),
            [0],
        )
        self.assertEqual(
            brochure_enrichment._building_identity_matches("Regents Wharf (Mill)", ["The Mill"]),
            [0],
        )

    def test_tier_3e_never_fires_when_row_building_has_no_parenthetical(self):
        indices = brochure_enrichment._building_identity_matches("The Mill", ["Somewhere Else"])
        self.assertEqual(indices, [])

    def test_tier_3e_does_not_cross_contaminate_different_sub_buildings(self):
        # Only the ONE candidate genuinely naming the row's own
        # parenthetical content is ever matched - a different real
        # sub-building sharing the same portfolio wrapper must never be
        # confused with it.
        indices = brochure_enrichment._building_identity_matches(
            "Regents Wharf (The Mill)", ["The Mill", "The Canal Building"],
        )
        self.assertEqual(indices, [0])

    def test_yard_is_a_recognized_trailing_street_suffix_word(self):
        # Real, confirmed case: row_building "160 Blackfriars Yard" vs a
        # real Friars Yard brochure's own bare building text "Friars
        # Yard" and address_1 "160 Blackfriars Road" - tier 4b (address-
        # suffix-stripped + house-number corroboration) needs "yard"
        # recognized as a trailing street-type word (same as "road") for
        # "160 blackfriars yard"/"160 blackfriars road" to both strip down
        # to the identical "160 blackfriars".
        indices = brochure_enrichment._building_identity_matches(
            "160 Blackfriars Yard", ["Friars Yard", "Friars Yard"],
            ["160 Blackfriars Road", "160 Blackfriars Road"],
        )
        self.assertEqual(indices, [0, 1])

    def test_tier_3f_dash_wrapper_suffix_is_the_real_name(self):
        # Real, confirmed case: row_building "Southbank Central - ALTO"/
        # "Southbank Central - VIVO" vs that brochure's own bare "Alto"/
        # "Vivo" - "Southbank Central" is a disposable development
        # wrapper, the SUFFIX after the dash is the real sub-building name.
        candidates = ["Vivo", "Vivo", "Alto"]
        self.assertEqual(
            brochure_enrichment._building_identity_matches("Southbank Central - ALTO", candidates), [2],
        )
        self.assertEqual(
            brochure_enrichment._building_identity_matches("Southbank Central - VIVO", candidates), [0, 1],
        )

    def test_tier_3f_dash_wrapper_prefix_is_the_real_name(self):
        # Real, confirmed case: row_building "210 Euston Road - Fora
        # Enterprise" vs that brochure's own bare "210 Euston Road" - here
        # the PREFIX is the real name and "Fora Enterprise" (an
        # operator/tenant name) is disposable - never caught by tier 2's
        # own _strip_building_address_suffix, which only strips a dash-
        # suffix that's itself address-shaped (starts with a house
        # number); "Fora Enterprise" doesn't.
        indices = brochure_enrichment._building_identity_matches(
            "210 Euston Road - Fora Enterprise", ["210 Euston Road", "210 Euston Road"],
        )
        self.assertEqual(indices, [0, 1])

    def test_tier_3f_never_fires_without_a_dash_separator(self):
        indices = brochure_enrichment._building_identity_matches("Plain Building Name", ["Something Else"])
        self.assertEqual(indices, [])

    def test_tier_3f_stays_unresolved_when_neither_side_matches(self):
        indices = brochure_enrichment._building_identity_matches("Foo Bar - Baz Qux", ["Something Unrelated"])
        self.assertEqual(indices, [])

    def test_tier_3g_period_as_word_separator(self):
        # Real, confirmed case: row_building "TBC London - 224 Tower
        # Bridge Rd" (reduced to "TBC London" by tier 2's own address-
        # suffix strip) vs that brochure's own extracted building text
        # "TBC.London" - normalize_key itself simply drops "." rather than
        # treating it as a word boundary (normalize_key("TBC.London") ->
        # "tbclondon", never "tbc london"), so no EXACT-comparison tier
        # above this one can ever bridge the gap no matter what else gets
        # stripped first.
        indices = brochure_enrichment._building_identity_matches(
            "TBC London - 224 Tower Bridge Rd", ["TBC.London", "TBC.London"],
        )
        self.assertEqual(indices, [0, 1])

    def test_tier_3g_never_fires_for_an_unrelated_period_containing_name(self):
        indices = brochure_enrichment._building_identity_matches("St. Mary Axe House", ["Something Unrelated"])
        self.assertEqual(indices, [])

    def test_tier_3h_house_number_range_overlaps_a_single_candidate_number(self):
        # Real, confirmed case: row_building "27-29 Gloucester Place" (a
        # provider's own spreadsheet stating the full numbered range a
        # building spans) vs a real brochure's own extracted building text
        # "29 Gloucester Place" (Gemini stating just the one number
        # actually printed on that page) - 29 falls within 27-29, so this
        # is never a genuine conflict (see house_number.house_numbers_
        # conflict), and the remaining text ("Gloucester Place") matches
        # exactly on both sides.
        indices = brochure_enrichment._building_identity_matches(
            "27-29 Gloucester Place", ["29 Gloucester Place"] * 3,
        )
        self.assertEqual(indices, [0, 1, 2])

    def test_tier_3h_rejects_a_genuinely_disjoint_house_number(self):
        # True-negative guard: a candidate number that does NOT fall
        # within the row's own range is a genuinely different numbered
        # building on the same street, never guessed at.
        indices = brochure_enrichment._building_identity_matches("27-29 Gloucester Place", ["45 Gloucester Place"])
        self.assertEqual(indices, [])

    def test_tier_3h_never_fires_when_the_remaining_text_differs(self):
        # Same leading house number, but a genuinely different street -
        # must never match on the number alone.
        indices = brochure_enrichment._building_identity_matches("27-29 Gloucester Place", ["27-29 Baker Street"])
        self.assertEqual(indices, [])

    def test_tier_3h_never_fires_when_row_has_no_leading_house_number(self):
        # A row with no leading house number of its own at all skips tier
        # 3h entirely (guarded on row_house_number is not None) - this
        # candidate still resolves, but via the pre-existing, unrelated
        # tier 5 (bare street reference), not tier 3h's own house-number-
        # range-overlap logic.
        indices = brochure_enrichment._building_identity_matches("Gloucester Place", ["29 Gloucester Place"])
        self.assertEqual(indices, [0])


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
        self.assertIsNone(brochure_enrichment._floor_number("Mezzanine"))

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

    def test_non_ground_unnumbered_labels_still_return_none(self):
        # Deliberately NOT given an invented numeric mapping - see
        # _ORDINAL_WORD_TO_NUMBER's own comment on why. Ground itself is
        # the one exception (see test_ground_floor_variants below) - these
        # remain unmapped, including Lower Ground specifically, which a
        # real Ivybridge House brochure confirms is a genuinely DIFFERENT
        # space from Ground, not the same floor loosely restated.
        for label in ("Lower Ground Floor", "Lower Ground", "LG", "Basement", "Mezzanine", "Reception"):
            self.assertIsNone(brochure_enrichment._floor_number(label), msg=f"label={label!r}")

    def test_ground_floor_variants(self):
        # Real confirmed gap: a real Ivybridge House row's own floor_unit
        # "G - Strand" never matched its own brochure's "Ground Floor"
        # unit via any existing tier, permanently excluding that floor
        # from enrichment on every re-upload of the same real document.
        for label in ("G", "Ground", "Ground Floor", "ground floor", "G - Strand", "Ground Floor - Part"):
            self.assertEqual(brochure_enrichment._floor_number(label), 0, msg=f"label={label!r}")

    def test_ground_floor_never_matches_a_word_merely_starting_with_g(self):
        # Anchored at the START with a word boundary - "g" must be its own
        # complete leading token, never a false positive against an
        # unrelated label that merely happens to start with the letter.
        for label in ("Gallery Floor", "Garden Level", "Grand Hall"):
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

    def test_row_and_unit_features_verbatim_duplicate_is_not_repeated(self):
        # Real confirmed case: Kitt's "The Sevens, 77 Charlotte Street"
        # (floors G, 1st, 3rd) - the spreadsheet's own special_features
        # cell and the brochure's own unit-level text for that same floor
        # are the same short blurb verbatim, so this combine used to
        # produce "<blurb>; <blurb again>; Manned reception; showers; bike
        # storage" - a visible duplicate that also tripped master_merge.
        # is_richness_regression hard enough to skip its own auto-merge.
        # The row's own value (kept as the first, most-specific segment)
        # survives; the identical unit-level echo is dropped; a genuinely
        # DISTINCT building_features segment is completely unaffected.
        row = ListingRow(
            building="The Sevens", floor_unit="Ground", special_features="Exposed brick, high ceilings",
        )
        units = _brochure_units(
            [{"building": "The Sevens", "floor_unit": "Ground", "special_features": "Exposed brick, high ceilings"}],
            building_features=[{"building": "The Sevens", "features": "Manned reception; showers; bike storage"}],
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(
            new_row.special_features, "Exposed brick, high ceilings; Manned reception; showers; bike storage",
        )
        self.assertNotIn("Exposed brick, high ceilings; Exposed brick, high ceilings", new_row.special_features)

    def test_duplicate_check_is_case_and_whitespace_tolerant(self):
        row = ListingRow(building="The Sevens", floor_unit="1st", special_features="  Exposed BRICK,  high ceilings ")
        units = _brochure_units(
            [{"building": "The Sevens", "floor_unit": "1st", "special_features": "exposed brick, high ceilings"}],
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.special_features, "  Exposed BRICK,  high ceilings ")
        self.assertEqual(fields, [])

    def test_genuinely_different_tiers_sharing_some_words_are_both_kept(self):
        # Deliberately NOT deduped via a partial-overlap/fuzzy check - two
        # tiers sharing SOME words but stating genuinely different facts
        # overall must never have one silently dropped, only a WHOLE-tier
        # exact match should ever trigger this.
        row = ListingRow(building="The Sevens", floor_unit="3rd", special_features="2 meeting rooms available")
        units = _brochure_units(
            [{"building": "The Sevens", "floor_unit": "3rd", "special_features": "2 meeting rooms and a kitchen"}],
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.special_features, "2 meeting rooms available; 2 meeting rooms and a kitchen")

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

    def test_real_metspace_fenchurch_street_parenthetical_now_enriches(self):
        # Real confirmed case (see BuildingIdentityMatchesTests' own tier
        # 3b tests): a real MetSpace email row's building text is "141
        # Fenchurch Street (Monument)" (Monument being the nearest
        # Underground station, appended purely for the reader's own area
        # orientation) - its own real brochure (Gemini-extracted, never
        # carrying this parenthetical) states building "141 Fenchurch
        # Street" with real, matchable special_features/state_of_space -
        # confirmed to previously enrich with ZERO fields changed across
        # all three real floors of this building traced.
        row = ListingRow(
            building="141 Fenchurch Street (Monument)", floor_unit="Ground Floor", special_features="Available: Now",
        )
        units = _brochure_units(
            [{
                "building": "141 Fenchurch Street", "floor_unit": "Ground Floor",
                "special_features": "1 meeting room; bike racks; showers", "state_of_space": "Fully Fitted",
            }],
            building_features=[{"building": "141 Fenchurch Street", "features": "Multi storey office building"}],
        )

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertIn("special_features", fields)
        self.assertEqual(
            new_row.special_features,
            "Available: Now; 1 meeting room; bike racks; showers; Multi storey office building",
        )
        self.assertEqual(new_row.state_of_space, "Fully Fitted")


class RealWorldSelfDuplicationRegressionTests(unittest.TestCase):
    """
    Real-world regression coverage for the root cause of the "CURRENT
    DESCRIPTION; CURRENT DESCRIPTION; new structured details..." shape
    reported across many unrelated real buildings - confirmed root cause:
    row.special_features (this upload's own short descriptive blurb, e.g.
    from the provider's own spreadsheet cell) and the brochure's own
    Gemini-extracted unit_features (which independently restates that same
    blurb as a PREFIX before its own genuinely new facts) are different
    WHOLE-TIER strings, so the prior whole-tier-only dedup in
    _apply_units_to_row never caught it. Each case here reproduces the
    real row_features/unit_features shape (row_features = the short blurb
    alone, unit_features = the real, previously-malformed brochure
    extraction) and asserts the fix's own two guarantees: no duplicated
    item survives, and no genuinely new item is lost.
    """

    CASES = [
        (
            "138 Cheapside 1st",
            "High end new fit out with sit stand desks",
            "High end new fit out with sit stand desks; High end new fit out with sit stand desks; Min term 36 months; "
            "2x teapoints; 6x meeting rooms; 4x phone booths; 100x current sit/stand desks; Short-Form All-Inclusive "
            "Lease; Distinctive hallmark 1950s façade; Plentiful windows offering natural light and views of St. "
            "Paul's Cathedral; Manned reception; Secure bike racks; End-of-trip facilities; DDA compliant",
            ["Short-Form All-Inclusive Lease", "100x current sit/stand desks", "DDA compliant"],
        ),
        (
            "138 Cheapside 6th",
            "High end new fit out with sit stand desks, fantastic views over St Pauls Cathedral",
            "High end new fit out with sit stand desks, fantastic views over St Pauls Cathedral; High end new fit out "
            "with sit stand desks, fantastic views over St Pauls Cathedral; Min term 36 months; 1x teapoint; 6x "
            "meeting rooms; 4x phone booths; 92x current sit/stand desks; Short-Form All-Inclusive Lease; "
            "Distinctive hallmark 1950s façade; Plentiful windows offering natural light and views of St. Paul's "
            "Cathedral; Manned reception; Secure bike racks; End-of-trip facilities; DDA compliant",
            ["Short-Form All-Inclusive Lease", "92x current sit/stand desks", "DDA compliant"],
        ),
        (
            "108 Cannon Street",
            "Prominent building in a fantastic location on Cannon Street with sharerd roof terrace",
            "Prominent building in a fantastic location on Cannon Street with sharerd roof terrace; Prominent "
            "building in a fantastic location on Cannon Street with sharerd roof terrace; 36 month term; Short-Form "
            "All-Inclusive Lease; Communal roof terrace; Manned reception; Secure bike racks; Hireable ground-floor "
            "event space; Shower and end-of-trip facilities; DDA compliant",
            ["Communal roof terrace", "Hireable ground-floor event space", "DDA compliant"],
        ),
        (
            "120 Cannon Street",
            "BREEAM excellent and manned reception",
            "BREEAM excellent and manned reception; BREEAM excellent and manned reception; Min. Term: 36 months; "
            "Legal structure: Lease + MSA; 1 teapoint; An elegant, recently-refurbished office in a prime location "
            "on bustling Cannon Street. 120 Cannon Street boasts modern and spacious offices, exposed ceilings and "
            "fantastic natural light. Clients enter via a sleek reception with high-end finishes.; BREEAM "
            "(Excellent); DDA compliant; Lift; Manned reception; Bike storage; Shower",
            ["Legal structure: Lease + MSA", "BREEAM (Excellent)", "Lift", "Shower"],
        ),
        (
            "4 Moorgate 2nd",
            "Well connected, brand new fitted fitted floor, excellent natural light. ",
            "Well connected, brand new fitted fitted floor, excellent natural light. ; Well connected, brand new "
            "fitted fitted floor, excellent natural light. ; Minimum term 36 months; 2 meeting rooms; 1 tea point; "
            "30 current desks; 1 phone booth; Manned reception; Secure bicycle storage; Shower facilities",
            ["30 current desks", "Secure bicycle storage"],
        ),
        (
            "4 Moorgate 4th",
            "Well connected, brand new fitted fitted floor, excellent natural light. ",
            "Well connected, brand new fitted fitted floor, excellent natural light. ; Well connected, brand new "
            "fitted fitted floor, excellent natural light. ; Minimum term 36 months; 2 meeting rooms; 1 breakout "
            "area; 30 current desks; 1 phone booth; Manned reception; Secure bicycle storage; Shower facilities",
            ["30 current desks", "1 breakout area"],
        ),
        (
            "4 Moorgate 5th",
            "Well connected, brand new fitted fitted floor, excellent natural light. ",
            "Well connected, brand new fitted fitted floor, excellent natural light. ; Well connected, brand new "
            "fitted fitted floor, excellent natural light. ; Minimum term 36 months; 3 meeting rooms; 1 tea point; "
            "30 current desks; 1 phone booth; Manned reception; Secure bicycle storage; Shower facilities",
            ["30 current desks", "3 meeting rooms"],
        ),
        (
            "44 Paul Street G",
            "Newly rennovated space with exposed brick",
            "Newly rennovated space with exposed brick; Newly rennovated space with exposed brick; Min. Term 24 "
            "months; Short form all-inclusive lease; 2 meeting rooms; 1 tea point; Manned reception; Shower; Bike "
            "storage; DDA compliant; Great natural light; Air Conditioning",
            ["Short form all-inclusive lease", "Air Conditioning"],
        ),
        (
            "44 Paul Street 5th",
            "New fitout, bright floor, well connected location",
            "New fitout, bright floor, well connected location; New fitout, bright floor, well connected location; "
            "18 current desks; 2 meeting rooms; 1 tea point; Min. term 24 months; Short form all-inclusive lease; "
            "Located at the intersection of Old Street, Shoreditch and Moorgate, 44 Paul Street offers a generous "
            "open plan layout. Perfect for collaborative and social teams who will benefit from the multifunctional "
            "space – from the exposed brick boardroom to the generous kitchen breakout.; Manned reception; Shower; "
            "Bike storage; DDA compliant; Great natural light; Air Conditioning",
            ["18 current desks", "generous kitchen breakout"],
        ),
        (
            "26 Finsbury Square 1st",
            "Bright, cost effective. Prominent corner position overlooking Finsbury Square",
            "Bright, cost effective. Prominent corner position overlooking Finsbury Square; Bright, cost effective. "
            "Prominent corner position overlooking Finsbury Square; Min. Term 36 months; Legal structure: Lease + "
            "MSA; Striking corner building overlooking one of the City's most recognisable garden squares with "
            "double-height reception, 24-hour commissionaire, floor-to-ceiling glazing, and modern end-of-trip "
            "facilities.; Manned reception; Shower; Bike storage; BREEAM Very Good; Air Conditioning",
            ["Legal structure: Lease + MSA", "BREEAM Very Good"],
        ),
        (
            "26 Finsbury Square 3rd",
            "Bright, cost effective. Prominent corner position overlooking Finsbury Square",
            "Bright, cost effective. Prominent corner position overlooking Finsbury Square; Bright, cost effective. "
            "Prominent corner position overlooking Finsbury Square; Minimum term 36 months; Lease + MSA legal "
            "structure; 52 current desks; 6 meeting rooms; 2 exec offices; 1 teapoint; 1 phone booth; Striking "
            "corner building overlooking one of the City's most recognisable garden squares with an impressive "
            "double-height reception, 24-hour commissionaire, floor-to-ceiling glazing, and modern end-of-trip "
            "facilities.; 24-hour commissionaire; Manned reception; Shower; Bike storage; BREEAM Very Good; Air "
            "Conditioning",
            ["52 current desks", "2 exec offices", "BREEAM Very Good"],
        ),
        (
            "Albion Mills",
            "Manned reception, converted warehouse with exposed brick",
            "Manned reception, converted warehouse with exposed brick; Manned reception, converted warehouse with "
            "exposed brick; 1 meeting room; 1 breakout area; Excellent natural light; Minimum term 12 months; "
            "Originally built in 1905 as a warehouse for Israel Hyman & Sons and later home to a thriving clothing "
            "factory; factory-style windows, cast-iron columns and exposed original brickwork thoughtfully restored "
            "for modern working life.; Manned reception; Showers; Bike storage",
            ["1 breakout area", "Minimum term 12 months"],
        ),
        (
            "Conran Building",
            "Located directly on the river, fit out underway due to complete Summer 2026",
            "Located directly on the river, fit out underway due to complete Summer 2026; Located directly on the "
            "river, fit out underway due to complete Summer 2026; 4 meeting rooms; 1 tea point; 36 current desks; "
            "views overlooking the river; minimum term 36 months; Staffed reception; Dedicated reception desk; "
            "Panoramic river views; Fully accessible (DDA compliant); Secure bicycle storage; Modern shower "
            "facilities; Pet-friendly policy",
            ["36 current desks", "Staffed reception"],
        ),
        (
            "95 Southwark Street",
            "New fitout, bright floors. Conveniently located. ",
            "New fitout, bright floors. Conveniently located. ; New fitout, bright floors. Conveniently located. ; "
            "2 meeting rooms; 2 phone booths; Minimum term 36 months; Short-Form All Inclusive Lease; Showers; Bike "
            "storage; Pet-friendly",
            ["Short-Form All Inclusive Lease", "Pet-friendly"],
        ),
        (
            "The Rochester, Rochester Mews",
            "Fully fitted, shared landscaped garden, onsite cafe. Currently undergoing works to create a split unit.",
            "Fully fitted, shared landscaped garden, onsite cafe. Currently undergoing works to create a split unit. "
            "; Fully fitted, shared landscaped garden, onsite cafe. Currently undergoing works to create a split "
            "unit.",
            [],
        ),
    ]

    def test_no_duplicate_survives_and_no_new_information_is_lost(self):
        for name, row_features, unit_features, expected_new_fragments in self.CASES:
            with self.subTest(name=name):
                row = ListingRow(building=name, floor_unit="1st", special_features=row_features)
                units = _brochure_units([{"building": name, "floor_unit": "1st", "special_features": unit_features}])

                new_row, fields = brochure_enrichment._apply_units_to_row(row, units)
                combined = new_row.special_features

                # The row's own blurb survives EXACTLY once, not twice.
                self.assertEqual(
                    combined.lower().count(row_features.strip().lower()), 1,
                    f"{name}: blurb should appear exactly once, got: {combined!r}",
                )
                for fragment in expected_new_fragments:
                    self.assertIn(fragment, combined, f"{name}: missing new info {fragment!r} in {combined!r}")

    def test_pure_duplication_with_no_new_content_collapses_to_one_copy(self):
        # The Rochester - a pure duplicate with NOTHING new at all.
        name, row_features, unit_features, _ = self.CASES[-1]
        row = ListingRow(building=name, floor_unit="1st", special_features=row_features)
        units = _brochure_units([{"building": name, "floor_unit": "1st", "special_features": unit_features}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.special_features, row_features)
        self.assertEqual(fields, [])
        self.assertIs(new_row, row)

    def test_prime_west_end_location_reported_shape_dedups_the_opening_sentence(self):
        # The exact reported shape: row_features is Kitt's own spreadsheet
        # cell (just the opening description sentence); unit_features is
        # Gemini's own fresh extraction, restating that same sentence as a
        # PREFIX before its own genuinely new structured facts.
        row_features = "Prime West End location. Impressive views across London."
        unit_features = (
            "Prime West End location. Impressive views across London.; Min. Term 36 months; 5 meeting rooms"
        )
        row = ListingRow(building="X", floor_unit="1st", special_features=row_features)
        units = _brochure_units([{"building": "X", "floor_unit": "1st", "special_features": unit_features}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)
        combined = new_row.special_features

        self.assertEqual(combined.lower().count(row_features.lower()), 1)
        self.assertIn("Min. Term 36 months", combined)
        self.assertIn("5 meeting rooms", combined)

    def test_partial_overlap_between_tiers_keeps_all_distinct_content_from_both(self):
        # A shared item, identically phrased, alongside OTHER genuinely
        # different facts on each side - the fix must drop only the exact
        # repeated item, never anything else, from either side.
        row_features = "Bike storage; Great natural light"
        unit_features = "Bike storage; 5 meeting rooms; Manned reception"
        row = ListingRow(building="X", floor_unit="1st", special_features=row_features)
        units = _brochure_units([{"building": "X", "floor_unit": "1st", "special_features": unit_features}])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)
        combined = new_row.special_features

        self.assertEqual(combined.lower().count("bike storage"), 1)
        self.assertIn("Great natural light", combined)
        self.assertIn("5 meeting rooms", combined)
        self.assertIn("Manned reception", combined)


class FullPipelineExtractionToMergePlanTests(unittest.TestCase):
    """
    extraction -> brochure enrichment -> ListingRow -> build_merge_plan,
    end to end, for three of the real cases above - proves the combine-
    layer fix (RealWorldSelfDuplicationRegressionTests) and the merge-
    layer safeguards (master_merge.SpecialFeaturesMergeTests/
    SpecialFeaturesAutoMergePermissivenessTests) work TOGETHER correctly
    as one pipeline, not just each in isolation.
    """

    def test_138_cheapside_1st_enriches_clean_and_auto_applies(self):
        # The combine-layer fix alone already prevents the duplicate from
        # ever being produced, so this genuinely new information (a clean,
        # non-duplicated enrichment) is safe to auto-apply - no review
        # needed for THIS specific real case, once fixed at the root.
        import pandas as pd

        import master_merge

        row_features = "High end new fit out with sit stand desks"
        unit_features = (
            "High end new fit out with sit stand desks; High end new fit out with sit stand desks; Min term 36 "
            "months; 2x teapoints; 6x meeting rooms; 4x phone booths; 100x current sit/stand desks; Short-Form "
            "All-Inclusive Lease; Distinctive hallmark 1950s façade; Plentiful windows offering natural light and "
            "views of St. Paul's Cathedral; Manned reception; Secure bike racks; End-of-trip facilities; DDA "
            "compliant"
        )
        row = ListingRow(building="138 Cheapside", provider="UNION", floor_unit="1st", special_features=row_features)
        units = _brochure_units(
            [{"building": "138 Cheapside", "floor_unit": "1st", "special_features": unit_features}],
        )
        enriched_row, fields = brochure_enrichment._apply_units_to_row(row, units)
        self.assertEqual(enriched_row.special_features.lower().count(row_features.lower()), 1)

        master_df = pd.DataFrame(
            [ListingRow(building="138 Cheapside", provider="UNION", floor_unit="1st", special_features=row_features).model_dump()]
        )
        plan = master_merge.build_merge_plan([enriched_row], master_df)
        matched = (plan.matched_changed + plan.matched_unchanged)[0]

        self.assertNotIn("special_features", matched.risky_fields)
        self.assertNotIn("High end new fit out with sit stand desks; High end", matched.diffs["special_features"][1])

    def test_thirty_lighterman_real_loss_stays_a_review_decision(self):
        import pandas as pd

        import master_merge

        old_val = (
            "Penthouse suite, private terrace; 3 meeting rooms; 1 tea point; minimum term 24 months; legal "
            "structure: co-lease; excellent natural light; Thirty Lighterman offers a fresh alternative to the "
            "traditional office. With flexible spaces and room to make it your own, it's designed to support "
            "collaboration, creativity, and growth as your needs evolve.; private outdoor spaces; high-spec showers"
        )
        # The genuinely malformed extraction actually observed - a real
        # extraction-quality issue (not a combine-layer duplicate this
        # session's own fix addresses at the source), so this simulates
        # what reaches build_merge_plan when extraction itself is bad.
        new_val = (
            "Penthouse suite, private terrace; Penthouse suite, private terrace; 3 meeting rooms; 1 tea point; "
            "minimum term 24 months; excellent natural light; private outdoor spaces; high-spec showers"
        )
        master_df = pd.DataFrame(
            [ListingRow(building="Thirty Lighterman", provider="UNION", special_features=old_val).model_dump()]
        )
        new_row = ListingRow(building="Thirty Lighterman", provider="UNION", special_features=new_val)

        plan = master_merge.build_merge_plan([new_row], master_df)
        matched = (plan.matched_changed + plan.matched_unchanged)[0]

        self.assertIn("special_features", matched.risky_fields)
        # Master's own richer value is never silently discarded - it's
        # still sitting right there as the "Current" side of the diff,
        # completely untouched, waiting for a reviewer's own decision.
        self.assertEqual(matched.diffs["special_features"][0], old_val)

        # A reviewer can still explicitly approve the real extraction.
        merged = master_merge.apply_merge(master_df.to_dict("records"), {0: {"special_features": new_val}}, [])
        self.assertEqual(merged[0].special_features, new_val)

    def test_the_rochester_pure_duplicate_enriches_to_a_no_op(self):
        import pandas as pd

        import master_merge

        blurb = "Fully fitted, shared landscaped garden, onsite cafe. Currently undergoing works to create a split unit."
        row = ListingRow(building="The Rochester", provider="UNION", floor_unit="1st", special_features=blurb)
        units = _brochure_units(
            [{"building": "The Rochester", "floor_unit": "1st", "special_features": f"{blurb} ; {blurb}"}],
        )
        enriched_row, fields = brochure_enrichment._apply_units_to_row(row, units)
        self.assertEqual(enriched_row.special_features, blurb)

        master_df = pd.DataFrame(
            [ListingRow(building="The Rochester", provider="UNION", floor_unit="1st", special_features=blurb).model_dump()]
        )
        plan = master_merge.build_merge_plan([enriched_row], master_df)
        matched = (plan.matched_changed + plan.matched_unchanged)[0]

        self.assertNotIn("special_features", matched.diffs)
        self.assertNotIn("special_features", matched.risky_fields)


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
        # address_1 here also happens to disagree with the brochure's own
        # value ("Already Stated Address" vs "13a St George St") - this
        # now correctly raises address_conflict (see AddressConflictNote
        # Tests/AddressConflictWiringTests), a real, intentional flag, not
        # a regression. This test's own point stands unaffected: every
        # field's own VALUE - address_1 included - is never overwritten.
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

        self.assertEqual(fields, ["address_conflict"])
        self.assertEqual(new_row.address_1, "Already Stated Address")
        self.assertEqual(new_row.postcode, "EC1A 1AA")
        self.assertEqual(new_row.submarket, "Already Stated Area")
        self.assertEqual(new_row.rent_pcm, 4000)
        self.assertEqual(new_row.rent_psf, 50)
        self.assertEqual(new_row.desks_max, 12)

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

    def test_placeholder_address_duplicate_of_building_is_overwritten(self):
        # The confirmed real "Nineteen Wells St" shape: address_1 is a
        # plain copy of building, not a real address - a genuine brochure-
        # derived address_1 must still be allowed to overwrite it, exactly
        # as it would a literally blank one.
        row = ListingRow(building="Nineteen Wells St", address_1="Nineteen Wells St", postcode=None)
        units = _brochure_units([
            {"building": "Nineteen Wells St", "address_1": "19 Wells Street", "postcode": "W1T 3PA"},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.address_1, "19 Wells Street")
        self.assertEqual(new_row.postcode, "W1T 3PA")
        self.assertIn("address_1", fields)

    def test_placeholder_address_with_street_suffix_abbreviation_is_overwritten(self):
        # address_1 "Nineteen Wells St" vs building "Nineteen Wells Street"
        # - a street-suffix abbreviation difference alone must still count
        # as a placeholder, not a genuine, independent address.
        row = ListingRow(building="Nineteen Wells Street", address_1="Nineteen Wells St")
        units = _brochure_units([
            {"building": "Nineteen Wells Street", "address_1": "19 Wells Street"},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.address_1, "19 Wells Street")
        self.assertIn("address_1", fields)

    def test_genuine_non_placeholder_address_is_never_overwritten(self):
        # A row whose address_1 is already genuine (not blank, not a
        # duplicate of building) must have that VALUE left exactly as it
        # is today - never overwritten, even by a confidently-matched
        # brochure value. This particular pair also happens to disagree
        # (see AddressConflictNoteTests/AddressConflictWiringTests below) -
        # confirmed as a real, intentional flag now (address_conflict),
        # not a regression: the original point of this test (address_1
        # ITSELF is never silently replaced) still holds completely.
        row = ListingRow(building="Nineteen Wells St", address_1="1 Some Other Road")
        units = _brochure_units([
            {"building": "Nineteen Wells St", "address_1": "19 Wells Street"},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertNotIn("address_1", fields)
        self.assertEqual(new_row.address_1, "1 Some Other Road")

    def test_genuine_non_placeholder_address_with_no_conflict_is_completely_unaffected(self):
        # The ORIGINAL, still-valid shape this test class's own name
        # promises: a genuine address_1 that also happens to agree with
        # the brochure - nothing at all happens, not even an address_
        # conflict flag.
        row = ListingRow(building="Nineteen Wells St", address_1="19 Wells Street")
        units = _brochure_units([
            {"building": "Nineteen Wells St", "address_1": "19 Wells Street"},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(fields, [])
        self.assertIs(new_row, row)
        self.assertEqual(new_row.address_1, "19 Wells Street")

    def test_placeholder_address_with_no_real_brochure_address_falls_through_cleanly(self):
        # The brochure itself doesn't state a real address either - must
        # fall through to today's existing "nothing to enrich" behavior,
        # no error, address_1 left exactly as the placeholder it was.
        row = ListingRow(building="Nineteen Wells St", address_1="Nineteen Wells St")
        units = _brochure_units([
            {"building": "Nineteen Wells St", "floor_unit": "1st"},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertNotIn("address_1", fields)
        self.assertEqual(new_row.address_1, "Nineteen Wells St")

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


class AddressConflictNoteTests(unittest.TestCase):
    """_address_conflict_note - the confirmed real case: an Ivybridge
    House row's own address_1 read "1 John Adam Street", but its own
    brochure states "1 to 5 Adam Street" on its cover page and every floor
    plan - no "John" anywhere. Conservative by design: only flags a
    genuine disagreeing house number or a street-name word-set mismatch,
    never a plain formatting difference."""

    def test_exact_match_is_not_a_conflict(self):
        self.assertIsNone(brochure_enrichment._address_conflict_note("19 Wells Street", "19 Wells Street"))

    def test_case_and_punctuation_only_difference_is_not_a_conflict(self):
        self.assertIsNone(brochure_enrichment._address_conflict_note("19 Wells St.", "19 wells st"))

    def test_genuinely_different_house_number_is_a_conflict(self):
        note = brochure_enrichment._address_conflict_note("27 Cannon Street", "108 Cannon Street")
        self.assertIsNotNone(note)
        self.assertIn("108 Cannon Street", note)
        self.assertIn("27 Cannon Street", note)

    def test_ivybridge_house_real_case_extra_word_is_a_conflict(self):
        # The confirmed real shape: leading house numbers agree ("1" both
        # sides), but the file's own text has a real extra word ("John")
        # the brochure's own version has nothing corresponding to.
        note = brochure_enrichment._address_conflict_note("1 John Adam Street", "1 to 5 Adam Street")
        self.assertIsNotNone(note)
        self.assertEqual(note, "Brochure states '1 to 5 Adam Street', file has '1 John Adam Street'")

    def test_no_house_number_shaped_brochure_text_is_not_a_conflict(self):
        # The brochure's own address text has nothing number-shaped to
        # compare against at all - not evidence of a conflict, just a
        # brochure that didn't state a number.
        self.assertIsNone(brochure_enrichment._address_conflict_note("1 John Adam Street", "Adam Street"))

    def test_blank_file_address_is_not_a_conflict(self):
        # Nothing on file yet to conflict with - BUILDING_LEVEL_FIELDS'
        # own ordinary blank-backfill handles this shape, not this check.
        self.assertIsNone(brochure_enrichment._address_conflict_note(None, "1 Adam Street"))
        self.assertIsNone(brochure_enrichment._address_conflict_note("", "1 Adam Street"))

    def test_blank_brochure_address_is_not_a_conflict(self):
        self.assertIsNone(brochure_enrichment._address_conflict_note("1 John Adam Street", None))
        self.assertIsNone(brochure_enrichment._address_conflict_note("1 John Adam Street", ""))

    def test_a_range_vs_its_own_single_endpoint_is_still_a_conflict(self):
        # Matches master_merge.house_number_changed's own established
        # precedent for this exact shape ("18" vs "14-18" - see that
        # function's own docstring): a plain number vs. a hyphenated range
        # sharing an endpoint is treated as a REAL disagreement, since the
        # range covers a different, wider set of units than the plain
        # number alone - never silently treated as "close enough".
        note = brochure_enrichment._address_conflict_note("27-30 Lime Street", "27 Lime Street")
        self.assertIsNotNone(note)

    def test_street_suffix_abbreviation_alone_is_flagged_as_a_word_mismatch(self):
        # Known, deliberate limitation: this check does its own plain
        # word-token comparison (via normalize_key), with no separate
        # abbreviation-expansion tolerance the way master_merge.
        # _building_match_key has for BUILDING-name matching - "St" and
        # "Street" are two different words here, so this flags a false
        # positive for a source that merely abbreviates the street suffix
        # differently than the file already does. Accepted rather than
        # solved: the cost of a false positive here is one extra manual
        # glance, never silent data corruption, and this wasn't part of
        # what was asked for this check - locked in as documented,
        # observed behavior rather than a silent gap.
        note = brochure_enrichment._address_conflict_note("19 Wells St", "19 Wells Street")
        self.assertIsNotNone(note)

    def test_saint_vs_st_as_a_non_last_word_is_not_a_conflict(self):
        # Real confirmed Review & Master case: the file's own address
        # ("26 Saint James's Square") flagged a false conflict against its
        # brochure's own spelling ("26 St James's Square") - genuinely the
        # same address, "St"/"Saint" simply spelled two different real
        # ways, with an "Apply" button that changed nothing at all
        # (Current and New already identical text).
        self.assertIsNone(
            brochure_enrichment._address_conflict_note("26 Saint James's Square", "26 St James's Square"),
        )
        # Symmetric - whichever side abbreviates, the other spells in
        # full, must be tolerated either way.
        self.assertIsNone(
            brochure_enrichment._address_conflict_note("26 St James's Square", "26 Saint James's Square"),
        )

    def test_genuine_conflict_still_flagged_when_saint_st_is_also_present(self):
        # The Saint/St tolerance must never mask an ACTUAL disagreement
        # elsewhere in the same address - a real different house number,
        # here, alongside the exact same "Saint"/"St" spelling difference
        # that alone would be tolerated.
        note = brochure_enrichment._address_conflict_note("26 Saint James's Square", "44 St James's Square")
        self.assertIsNotNone(note)
        self.assertIn("44 St James's Square", note)
        self.assertIn("26 Saint James's Square", note)

    def test_trailing_st_is_never_treated_as_saint(self):
        # The Saint/St tolerance is deliberately position-aware - only a
        # NON-last "st" means "Saint" ("St James's"); the LAST word is
        # left completely untouched (mirrors master_merge._expand_street_
        # suffix's own opposite convention: only the LAST word can mean
        # the street-type suffix "Street"). Two genuinely different real
        # streets that merely happen to share a "St"/"Street" TRAILING
        # spelling difference (see test_street_suffix_abbreviation_alone_
        # is_flagged_as_a_word_mismatch above - a pre-existing, documented
        # limitation this fix must not silently paper over) must still be
        # flagged as different, never conflated via this new tolerance.
        note = brochure_enrichment._address_conflict_note("1 Kings St", "1 Kings Street")
        self.assertIsNotNone(note)


class AddressConflictWiringTests(EnrichmentTestCase):
    """_apply_units_to_row's own wiring of _address_conflict_note into the
    BUILDING_LEVEL_FIELDS address_1 step - runs regardless of whether
    address_1 is blank, but NEVER writes address_1 itself; only ever sets
    address_conflict."""

    def test_conflicting_brochure_address_sets_address_conflict_never_touches_address_1(self):
        row = ListingRow(building="Ivybridge House", address_1="1 John Adam Street")
        units = _brochure_units([
            {"building": "Ivybridge House", "address_1": "1 to 5 Adam Street"},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(fields, ["address_conflict"])
        self.assertEqual(new_row.address_1, "1 John Adam Street")
        self.assertEqual(
            new_row.address_conflict, "Brochure states '1 to 5 Adam Street', file has '1 John Adam Street'",
        )

    def test_agreeing_brochure_address_never_sets_address_conflict(self):
        row = ListingRow(building="Ivybridge House", address_1="1 to 5 Adam Street")
        units = _brochure_units([
            {"building": "Ivybridge House", "address_1": "1 to 5 Adam Street"},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(fields, [])
        self.assertIsNone(new_row.address_conflict)

    def test_blank_address_1_is_backfilled_normally_never_flagged_as_a_conflict(self):
        # Nothing on file to compare against at all - the ordinary
        # placeholder/blank backfill path handles this, completely
        # unrelated to the conflict check.
        row = ListingRow(building="Ivybridge House", address_1=None)
        units = _brochure_units([
            {"building": "Ivybridge House", "address_1": "1 to 5 Adam Street"},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertEqual(new_row.address_1, "1 to 5 Adam Street")
        self.assertIn("address_1", fields)
        self.assertNotIn("address_conflict", fields)
        self.assertIsNone(new_row.address_conflict)

    def test_no_brochure_address_at_all_is_not_flagged_and_does_not_crash(self):
        row = ListingRow(building="Ivybridge House", address_1="1 John Adam Street")
        units = _brochure_units([
            {"building": "Ivybridge House", "floor_unit": "2nd"},
        ])

        new_row, fields = brochure_enrichment._apply_units_to_row(row, units)

        self.assertNotIn("address_conflict", fields)
        self.assertIsNone(new_row.address_conflict)
        self.assertEqual(new_row.address_1, "1 John Adam Street")


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

    def test_row_with_nothing_missing_and_no_brochure_link_never_triggers_extraction(self):
        # Every field in ENRICHABLE_FIELDS populated, not just the original
        # three - a row missing even one of the newer fields (address_1,
        # postcode, submarket, size_sqft, desks_max, rent_pcm, rent_psf)
        # now correctly needs_enrichment (see NeedsEnrichmentTests). With no
        # brochure_link at all, there's nothing special_features's own
        # decoupled check could check either, so this stays excluded.
        row = ListingRow(
            building="A", floor_unit="1st", brochure_link=None,
            address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
            size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
            special_features="Already have this", state_of_space="Cat A", contacts="Jane, jane@x.com",
        )
        with patch("brochure_enrichment._extract_brochure_units") as mock_extract:
            new_row, fields = brochure_enrichment.enrich_row(row)

        mock_extract.assert_not_called()
        self.assertIs(new_row, row)
        self.assertEqual(fields, [])

    def test_row_with_nothing_missing_but_a_real_brochure_link_still_checks_special_features(self):
        # Same fully-filled row, but WITH a real brochure_link - special_
        # features's own decoupled check (see NeedsEnrichmentTests) means
        # extraction now DOES run, but since the brochure genuinely has
        # nothing new for this building, the row still comes back unchanged.
        row = ListingRow(
            building="A", floor_unit="1st", brochure_link="https://example.com/brochure.pdf",
            address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
            size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
            special_features="Already have this", state_of_space="Cat A", contacts="Jane, jane@x.com",
        )
        units = _brochure_units([{"building": "A", "floor_unit": "1st"}])
        with patch("brochure_enrichment._extract_brochure_units", return_value=units) as mock_extract:
            new_row, fields = brochure_enrichment.enrich_row(row)

        mock_extract.assert_called_once()
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
            # Every OTHER ENRICHABLE_FIELDS entry populated, special_
            # features included - but a real, unexplored brochure_link
            # still makes this row eligible on special_features's own
            # account alone (see NeedsEnrichmentTests), so this must now
            # be INCLUDED, not excluded.
            ListingRow(
                building="B", floor_unit="1st", brochure_link="https://example.com/b.pdf",
                address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
                size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
                special_features="Already there", state_of_space="Cat A", contacts="Jane, jane@x.com",
            ),
            # No brochure_link at all - nothing to check, so this one
            # genuinely does stay excluded regardless of special_features.
            ListingRow(building="C", floor_unit="1st", brochure_link=None, special_features=None),
        ]
        eligible, urls = brochure_enrichment.eligible_rows_and_brochures(rows)
        self.assertEqual(len(eligible), 3)
        self.assertEqual(urls, ["https://example.com/a.pdf", "https://example.com/b.pdf"])

    def test_no_eligible_rows_returns_empty(self):
        rows = [ListingRow(building="A", special_features="x", state_of_space="y", contacts="z")]
        eligible, urls = brochure_enrichment.eligible_rows_and_brochures(rows)
        self.assertEqual(eligible, [])
        self.assertEqual(urls, [])


class PropagateSharedBrochureLinkWithinBuildingTests(unittest.TestCase):
    """
    _propagate_shared_brochure_link_within_building - the real, confirmed
    Henly House gap: a schedule-of-areas brochure covering several floors
    of ONE building only ever had brochure_link genuinely stated for ONE
    of those floors' own rows, structurally excluding every sibling floor
    from brochure enrichment entirely (see eligible_rows_and_brochures/
    needs_enrichment, both keyed on a row's OWN brochure_link field with
    no sibling awareness at all).
    """

    def test_blank_sibling_rows_inherit_the_sole_distinct_link(self):
        rows = [
            ListingRow(building="Henly House", floor_unit="4th", provider="Colliers", brochure_link="https://example.com/henly.pdf"),
            ListingRow(building="Henly House", floor_unit="1st", provider="Colliers", brochure_link=None),
            ListingRow(building="Henly House", floor_unit="2nd", provider="Colliers", brochure_link=None),
        ]
        updated = brochure_enrichment._propagate_shared_brochure_link_within_building(rows)
        self.assertEqual([r.brochure_link for r in updated], ["https://example.com/henly.pdf"] * 3)

    def test_two_distinct_links_in_one_building_group_is_never_resolved(self):
        # A genuine multi-listing/portfolio shape (or two unrelated
        # listings sharing a building name+provider) - never guessed,
        # every row's own existing value (including blank) is left alone.
        rows = [
            ListingRow(building="Point House", floor_unit="1st", provider="Colliers", brochure_link="https://example.com/a.pdf"),
            ListingRow(building="Point House", floor_unit="2nd", provider="Colliers", brochure_link="https://example.com/b.pdf"),
            ListingRow(building="Point House", floor_unit="3rd", provider="Colliers", brochure_link=None),
        ]
        updated = brochure_enrichment._propagate_shared_brochure_link_within_building(rows)
        self.assertEqual(
            [r.brochure_link for r in updated],
            ["https://example.com/a.pdf", "https://example.com/b.pdf", None],
        )

    def test_never_overwrites_a_row_that_already_has_its_own_link(self):
        rows = [
            ListingRow(building="Henly House", floor_unit="4th", provider="Colliers", brochure_link="https://example.com/henly.pdf"),
            ListingRow(building="Henly House", floor_unit="1st", provider="Colliers", brochure_link="https://example.com/own-link.pdf"),
        ]
        updated = brochure_enrichment._propagate_shared_brochure_link_within_building(rows)
        self.assertEqual(updated[1].brochure_link, "https://example.com/own-link.pdf")

    def test_different_buildings_never_cross_propagate(self):
        rows = [
            ListingRow(building="Henly House", floor_unit="4th", provider="Colliers", brochure_link="https://example.com/henly.pdf"),
            ListingRow(building="Ivybridge House", floor_unit="LG", provider="Colliers", brochure_link=None),
        ]
        updated = brochure_enrichment._propagate_shared_brochure_link_within_building(rows)
        self.assertIsNone(updated[1].brochure_link)

    def test_same_building_different_provider_never_cross_propagates(self):
        # Same building name, genuinely different provider/source - not
        # safe to assume the same listing (mirrors master_merge._fallback_
        # key's own building+provider identity pairing).
        rows = [
            ListingRow(building="Henly House", floor_unit="4th", provider="Colliers", brochure_link="https://example.com/henly.pdf"),
            ListingRow(building="Henly House", floor_unit="1st", provider="UNION", brochure_link=None),
        ]
        updated = brochure_enrichment._propagate_shared_brochure_link_within_building(rows)
        self.assertIsNone(updated[1].brochure_link)

    def test_no_non_blank_link_at_all_in_the_group_leaves_everything_blank(self):
        rows = [
            ListingRow(building="Henly House", floor_unit="4th", provider="Colliers", brochure_link=None),
            ListingRow(building="Henly House", floor_unit="1st", provider="Colliers", brochure_link=None),
        ]
        updated = brochure_enrichment._propagate_shared_brochure_link_within_building(rows)
        self.assertEqual([r.brochure_link for r in updated], [None, None])

    def test_end_to_end_a_previously_ineligible_sibling_now_gets_enriched(self):
        # The real fix, proven at the level enrich_rows_grouped itself
        # operates - mirrors run_brochure_enrichment's own wiring (see its
        # docstring): propagate first, THEN compute eligibility/enrich.
        rows = [
            ListingRow(building="Henly House", floor_unit="4th", provider="Colliers", brochure_link="https://example.com/henly.pdf", special_features=None),
            ListingRow(building="Henly House", floor_unit="1st", provider="Colliers", brochure_link=None, special_features=None, state_of_space=None),
        ]
        propagated = brochure_enrichment._propagate_shared_brochure_link_within_building(rows)

        # Before the fix, this second row would never even be considered:
        # eligible_rows_and_brochures keys eligibility on brochure_link
        # alone, and the ORIGINAL (unpropagated) row has none.
        eligible_before, _ = brochure_enrichment.eligible_rows_and_brochures(rows)
        self.assertEqual(len(eligible_before), 1)
        eligible_after, urls_after = brochure_enrichment.eligible_rows_and_brochures(propagated)
        self.assertEqual(len(eligible_after), 2)
        self.assertEqual(urls_after, ["https://example.com/henly.pdf"])

        units = [{"building": "Henly House", "floor_unit": None, "state_of_space": "Fully Fitted", "special_features": "Shared feature"}]
        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            enriched, _log, _stats = brochure_enrichment.enrich_rows_grouped(propagated)

        self.assertEqual(enriched[1].state_of_space, "Fully Fitted")
        self.assertEqual(enriched[1].special_features, "Shared feature")


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

    def test_rows_with_nothing_missing_and_no_brochure_link_are_excluded_from_the_run(self):
        # Every ENRICHABLE_FIELDS entry populated - see EnrichRowTests' own
        # identical update for why this now needs more than the original
        # three fields to be genuinely "nothing missing". No brochure_link
        # at all, so special_features's own decoupled check (see
        # NeedsEnrichmentTests) has nothing to check either.
        rows = [ListingRow(
            building="A", brochure_link=None,
            address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
            floor_unit="1st", size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
            special_features="Done", state_of_space="Cat A", contacts="Jane, jane@x.com",
        )]
        with patch("brochure_enrichment._extract_brochure_units") as mock_units:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        mock_units.assert_not_called()
        self.assertEqual(stats["unique_brochures_considered"], 0)
        self.assertEqual(stats["rows_eligible"], 0)

    def test_rows_with_nothing_missing_but_a_real_brochure_link_are_still_included(self):
        # Same fully-filled row, but WITH a real brochure_link - special_
        # features's own decoupled check means this row IS included in the
        # run (unique_brochures_considered/rows_eligible both reflect it),
        # even though the brochure genuinely has nothing new to add.
        rows = [ListingRow(
            building="A", brochure_link="https://example.com/a.pdf",
            address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
            floor_unit="1st", size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
            special_features="Done", state_of_space="Cat A", contacts="Jane, jane@x.com",
        )]
        units = _brochure_units([{"building": "A", "floor_unit": "1st"}])
        with patch("brochure_enrichment._extract_brochure_units", return_value=units) as mock_units:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows)

        mock_units.assert_called_once()
        self.assertEqual(stats["unique_brochures_considered"], 1)
        self.assertEqual(stats["rows_eligible"], 1)
        self.assertEqual(enriched[0].special_features, "Done")

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

    def test_partial_fill_among_rows_sharing_one_url_forces_a_refetch(self):
        # Confirmed real root cause of rows staying blank on special_
        # features indefinitely (the Fetter Lane/High Holborn case): two
        # rows SHARE one brochure_link (e.g. several floors of the same
        # building all pointing at one PDF). _apply_units_to_row does its
        # OWN per-row matching and can legitimately fill SOME of the
        # sharing rows while leaving others blank (no confident match for
        # that specific row) - the url still gets marked "ok" because the
        # FETCH itself succeeded, not because every row got a value. A
        # url-only skip on resume (the old behavior) would strand row B
        # blank forever, indistinguishable from a row correctly left blank
        # because the document genuinely had nothing for it at all - see
        # enrich_rows_grouped's own already_processed docstring. Row A's
        # own prior value must survive completely untouched throughout.
        # special_features_matched={"0": True} marks row A (index 0) as
        # genuinely resolved by that prior run (see enrich_rows_grouped's
        # own special_features_matched param docstring - a per-staging-file
        # sidecar record, never a ListingRow field) - the actual evidence
        # still_blank_counts now uses, since a plain non-blank special_
        # features value alone is no longer sufficient (see the resume-skip
        # regression tests below).
        row_a = ListingRow(
            building="A", address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
            floor_unit="1st", size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
            special_features="Already filled by a prior run", state_of_space="Cat A",
            contacts="Jane, jane@x.com", brochure_link="https://example.com/shared.pdf",
        )
        row_b = ListingRow(building="B", brochure_link="https://example.com/shared.pdf", special_features=None)
        already_processed = {"https://example.com/shared.pdf": "ok"}
        special_features_matched = {"0": True}

        def _fake_apply(row, units):
            if row.building == "B":
                return row.model_copy(update={"special_features": "Recovered for B"}), ["special_features"]
            return row, []

        with patch("brochure_enrichment._extract_brochure_units", return_value=[{"building": "B"}]) as mock_extract, \
             patch("brochure_enrichment._apply_units_to_row", side_effect=_fake_apply):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(
                [row_a, row_b], already_processed=already_processed,
                special_features_matched=special_features_matched,
            )

        mock_extract.assert_called_once_with("https://example.com/shared.pdf")
        self.assertEqual(enriched[0].special_features, "Already filled by a prior run")
        self.assertEqual(enriched[1].special_features, "Recovered for B")
        self.assertEqual(stats["processed_urls"], {"https://example.com/shared.pdf": "ok"})

    def test_short_boilerplate_special_features_with_everything_else_filled_forces_a_refetch(self):
        # Confirmed real root cause of the "210 Euston Road" shape: a row
        # can have SOME non-blank special_features text (boilerplate from
        # the original source extraction, never actually brochure-sourced)
        # while every other ENRICHABLE_FIELDS value is already filled -
        # _row_has_a_genuinely_blank_enrichable_field used to treat that as
        # "nothing left to do here" on the strength of special_features'
        # plain non-blankness alone, permanently stranding it once its
        # shared URL was marked "ok" by an already-resolved sibling row.
        # special_features_matched={"0": True} (see enrich_rows_grouped's
        # own param docstring - a per-staging-file sidecar record, never a
        # ListingRow field) is the fix: still_blank_counts now uses it
        # instead of a blank check, so row B (index 1, absent from the
        # incoming sidecar - never matched) keeps forcing a refetch until a
        # genuine combine actually lands.
        row_a = ListingRow(
            building="A", address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
            floor_unit="1st", size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
            special_features="Genuinely resolved by a prior run", state_of_space="Cat A",
            contacts="Jane, jane@x.com", brochure_link="https://example.com/shared.pdf",
        )
        row_b = ListingRow(
            building="B", address_1="2 Example Street", postcode="EC1A 1AB", submarket="City",
            floor_unit="2nd", size_sqft=2000, desks_max=30, rent_pcm=6000, rent_psf=65,
            special_features="5 meeting rooms; 12 month term", state_of_space="Cat A",
            contacts="Jane, jane@x.com", brochure_link="https://example.com/shared.pdf",
        )
        already_processed = {"https://example.com/shared.pdf": "ok"}
        special_features_matched = {"0": True}

        def _fake_apply(row, units):
            if row.building == "B":
                return row.model_copy(update={"special_features": "Recovered for B"}), ["special_features"]
            return row, []

        with patch("brochure_enrichment._extract_brochure_units", return_value=[{"building": "B"}]) as mock_extract, \
             patch("brochure_enrichment._apply_units_to_row", side_effect=_fake_apply):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(
                [row_a, row_b], already_processed=already_processed,
                special_features_matched=special_features_matched,
            )

        mock_extract.assert_called_once_with("https://example.com/shared.pdf")
        self.assertEqual(enriched[0].special_features, "Genuinely resolved by a prior run")
        self.assertEqual(enriched[1].special_features, "Recovered for B")
        self.assertEqual(stats["special_features_matched"], {"1": True})
        self.assertEqual(stats["processed_urls"], {"https://example.com/shared.pdf": "ok"})

    def test_url_where_every_sharing_row_is_already_matched_is_not_refetched(self):
        # The other side of the fix above - once every row sharing a URL
        # genuinely has an entry in the incoming special_features_matched
        # sidecar, the existing "don't waste a refetch" optimization must
        # still apply unchanged, even though special_features' own text is
        # non-blank on both (the same shape a plain blank check would
        # already have gotten right - this confirms the new sidecar-based
        # check does too).
        row_a = ListingRow(
            building="A", address_1="1 Example Street", postcode="EC1A 1AA", submarket="City",
            floor_unit="1st", size_sqft=1000, desks_max=20, rent_pcm=5000, rent_psf=60,
            special_features="Resolved A", state_of_space="Cat A", contacts="Jane, jane@x.com",
            brochure_link="https://example.com/shared.pdf",
        )
        row_b = ListingRow(
            building="B", address_1="2 Example Street", postcode="EC1A 1AB", submarket="City",
            floor_unit="2nd", size_sqft=2000, desks_max=30, rent_pcm=6000, rent_psf=65,
            special_features="Resolved B", state_of_space="Cat A", contacts="Jane, jane@x.com",
            brochure_link="https://example.com/shared.pdf",
        )
        already_processed = {"https://example.com/shared.pdf": "ok"}
        special_features_matched = {"0": True, "1": True}

        with patch("brochure_enrichment._extract_brochure_units") as mock_extract:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(
                [row_a, row_b], already_processed=already_processed,
                special_features_matched=special_features_matched,
            )

        mock_extract.assert_not_called()
        self.assertEqual(stats["processed_urls"], {})

    def test_skipped_rows_are_left_completely_unchanged(self):
        # Row 0 must be genuinely, fully resolved (every ENRICHABLE_FIELDS
        # value present, AND special_features_matched carrying its own
        # index) for its url to stay skipped on resume - a row missing
        # even one other field (address_1, state_of_space, ...) is still
        # "genuinely blank" per _row_has_a_genuinely_blank_enrichable_field
        # and must now force a refetch, same as an all-blank row does (see
        # test_urls_marked_ok_but_still_genuinely_blank_are_refetched).
        rows = self._rows(2)
        rows[0] = rows[0].model_copy(update={
            "address_1": "1 Example Street", "postcode": "EC1A 1AA", "submarket": "City",
            "floor_unit": "1st", "size_sqft": 1000, "desks_max": 20, "rent_pcm": 5000, "rent_psf": 60,
            "special_features": "Already checked, genuinely nothing more", "state_of_space": "Cat A",
            "contacts": "Jane, jane@x.com",
        })
        already_processed = {"https://example.com/B0.pdf": "ok"}
        special_features_matched = {"0": True}

        with patch("brochure_enrichment._extract_brochure_units", return_value=[
            {"building": "B1", "floor_unit": None, "special_features": "New"},
        ]):
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(
                rows, already_processed=already_processed, special_features_matched=special_features_matched,
            )

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
        # Every row must be genuinely, fully resolved (see test_skipped_
        # rows_are_left_completely_unchanged's own comment) for "already
        # ok" to be a true no-op - a row still missing a field forces a
        # refetch regardless of the url's own "ok" mark (see test_urls_
        # marked_ok_but_still_genuinely_blank_are_refetched below).
        rows = self._rows(2)
        full_fields = {
            "address_1": "1 Example Street", "postcode": "EC1A 1AA", "submarket": "City",
            "floor_unit": "1st", "size_sqft": 1000, "desks_max": 20, "rent_pcm": 5000, "rent_psf": 60,
            "special_features": "Resolved", "state_of_space": "Cat A", "contacts": "Jane, jane@x.com",
        }
        rows = [r.model_copy(update=full_fields) for r in rows]
        already_processed = {r.brochure_link: "ok" for r in rows}
        special_features_matched = {"0": True, "1": True}

        with patch("brochure_enrichment._extract_brochure_units") as mock_extract:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(
                rows, already_processed=already_processed, special_features_matched=special_features_matched,
            )

        mock_extract.assert_not_called()
        self.assertEqual(stats["unique_brochures_considered"], 2)
        self.assertEqual(stats["processed_urls"], {})
        self.assertEqual(log, [])

    def test_resume_after_interruption_processes_only_the_remaining_brochures(self):
        # Simulates the exact reported scenario: 30 of 126 already checked
        # AND genuinely, fully resolved, a resume must only touch the other
        # 96 - proven here at smaller scale (2 of 5 already done). B0/B1
        # must be fully resolved (see test_skipped_rows_are_left_
        # completely_unchanged's own comment) for their url's "ok" mark to
        # actually stick - a merely-blank "ok" row no longer counts (see
        # test_urls_marked_ok_but_still_genuinely_blank_are_refetched).
        rows = self._rows(5)
        full_fields = {
            "address_1": "1 Example Street", "postcode": "EC1A 1AA", "submarket": "City",
            "floor_unit": "1st", "size_sqft": 1000, "desks_max": 20, "rent_pcm": 5000, "rent_psf": 60,
            "special_features": "Resolved", "state_of_space": "Cat A", "contacts": "Jane, jane@x.com",
        }
        rows[0] = rows[0].model_copy(update=full_fields)
        rows[1] = rows[1].model_copy(update=full_fields)
        already_processed = {
            "https://example.com/B0.pdf": "ok", "https://example.com/B1.pdf": "ok",
        }
        special_features_matched = {"0": True, "1": True}

        with patch("brochure_enrichment._extract_brochure_units", return_value=[
            {"building": "x", "floor_unit": None, "special_features": "f"},
        ]) as mock_extract:
            _, _, stats = brochure_enrichment.enrich_rows_grouped(
                rows, already_processed=already_processed, special_features_matched=special_features_matched,
            )

        self.assertEqual(mock_extract.call_count, 3)
        called_urls = {c.args[0] for c in mock_extract.call_args_list}
        self.assertEqual(called_urls, {"https://example.com/B2.pdf", "https://example.com/B3.pdf", "https://example.com/B4.pdf"})
        self.assertEqual(len(stats["processed_urls"]), 3)

    def test_urls_marked_ok_but_still_genuinely_blank_are_refetched(self):
        # The fix confirmed against the real regentswharf.co.uk Colliers
        # brochure (see enrich_rows_grouped's own already_processed
        # docstring): a url marked "ok" where EVERY sharing row is still
        # genuinely blank is no longer treated as reliable evidence the
        # document "has nothing here" - that shape is equally consistent
        # with a matching bug that failed every row identically, so it's
        # retried on every resume/re-upload exactly like a partial-fill
        # url already was. Renamed/inverted from this class's own former
        # test_urls_already_marked_ok_are_never_refetched, which asserted
        # the old (buggy) behavior this fix removes.
        rows = self._rows(3)
        already_processed = {"https://example.com/B0.pdf": "ok", "https://example.com/B1.pdf": "ok"}

        def _fake(url):
            return [{"building": url, "floor_unit": None, "special_features": "checked"}]

        with patch("brochure_enrichment._extract_brochure_units", side_effect=_fake) as mock_extract:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(rows, already_processed=already_processed)

        self.assertEqual(mock_extract.call_count, 3)
        called_urls = {c.args[0] for c in mock_extract.call_args_list}
        self.assertEqual(
            called_urls,
            {"https://example.com/B0.pdf", "https://example.com/B1.pdf", "https://example.com/B2.pdf"},
        )
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
        # Row 0 must already be genuinely resolved (special_features
        # non-blank - the only FLOORPLAN_ENRICHABLE_FIELDS entry) for its
        # url's "ok" mark to actually stick - a row still blank forces a
        # refetch regardless (see test_urls_marked_ok_but_still_blank_
        # floorplans_are_refetched below).
        rows = self._rows(2)
        rows[0] = rows[0].model_copy(update={"special_features": "Already checked, genuinely nothing more"})
        already_processed = {"https://example.com/0.pdf": "ok"}

        with patch(
            "brochure_enrichment._extract_floorplan_units",
            return_value=[{"floor_unit": None, "special_features": "New"}],
        ) as mock_extract:
            current, log, stats = brochure_enrichment._enrich_rows_from_floorplans(
                rows, already_processed=already_processed,
            )

        mock_extract.assert_called_once_with("https://example.com/1.pdf")
        self.assertEqual(current[0].special_features, "Already checked, genuinely nothing more")  # skipped - left exactly as before
        self.assertEqual(current[1].special_features, "New")
        self.assertEqual(stats["processed_urls"], {"https://example.com/1.pdf": "ok"})

    def test_urls_marked_ok_but_still_blank_floorplans_are_refetched(self):
        # The floorplan-pass counterpart of EnrichRowsGroupedResumeTests'
        # own test_urls_marked_ok_but_still_genuinely_blank_are_refetched:
        # a url marked "ok" where every sharing row is still genuinely
        # blank is no longer treated as reliable "nothing here" evidence,
        # since that shape is equally consistent with a matching bug that
        # failed every row identically.
        rows = self._rows(2)
        already_processed = {"https://example.com/0.pdf": "ok"}

        with patch(
            "brochure_enrichment._extract_floorplan_units",
            return_value=[{"floor_unit": None, "special_features": "New"}],
        ) as mock_extract:
            current, log, stats = brochure_enrichment._enrich_rows_from_floorplans(
                rows, already_processed=already_processed,
            )

        self.assertEqual(mock_extract.call_count, 2)
        called_urls = {c.args[0] for c in mock_extract.call_args_list}
        self.assertEqual(called_urls, {"https://example.com/0.pdf", "https://example.com/1.pdf"})

    def test_partial_fill_among_rows_sharing_one_floorplan_forces_a_refetch(self):
        # Same shape as enrich_rows_grouped's own equivalent regression
        # test (see EnrichRowsGroupedResumeTests) for the brochure-link
        # pass, mirrored here for the floorplan-link pass:
        # _apply_floorplan_units_to_row does its own per-row matching and
        # can fill some of several rows sharing one floorplan_link while
        # leaving others blank - the url still gets marked "ok" for the
        # document as a whole, so a url-only skip on resume would strand
        # the still-blank row(s) forever. Row A's own prior value must
        # survive completely untouched.
        row_a = ListingRow(
            building="A", floor_unit="1st", floorplan_link="https://example.com/shared.pdf",
            special_features="Already filled by a prior run",
        )
        row_b = ListingRow(
            building="B", floor_unit="2nd", floorplan_link="https://example.com/shared.pdf", special_features=None,
        )
        already_processed = {"https://example.com/shared.pdf": "ok"}

        def _fake_apply(row, units):
            if row.building == "B":
                return row.model_copy(update={"special_features": "Recovered for B"}), ["special_features"]
            return row, []

        with patch(
            "brochure_enrichment._extract_floorplan_units", return_value=[{"floor_unit": "2nd"}],
        ) as mock_extract, patch("brochure_enrichment._apply_floorplan_units_to_row", side_effect=_fake_apply):
            current, log, stats = brochure_enrichment._enrich_rows_from_floorplans(
                [row_a, row_b], already_processed=already_processed,
            )

        mock_extract.assert_called_once_with("https://example.com/shared.pdf")
        self.assertEqual(current[0].special_features, "Already filled by a prior run")
        self.assertEqual(current[1].special_features, "Recovered for B")
        self.assertEqual(stats["processed_urls"], {"https://example.com/shared.pdf": "ok"})

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
        # The row must already be genuinely resolved (special_features
        # non-blank) for its floorplan url's "ok" mark to actually stick -
        # see _enrich_rows_from_floorplans' own test_already_ok_floorplan_
        # is_never_refetched for why a still-blank row now forces a
        # refetch regardless of the url's own "ok" mark.
        rows = [
            ListingRow(
                building="A", floor_unit="1st", floorplan_link="https://example.com/fp.pdf",
                special_features="Already resolved",
            ),
        ]
        with patch("brochure_enrichment._extract_floorplan_units") as mock_extract:
            enriched, log, stats = brochure_enrichment.enrich_rows_grouped(
                rows, floorplan_already_processed={"https://example.com/fp.pdf": "ok"},
            )

        mock_extract.assert_not_called()
        self.assertEqual(enriched[0].special_features, "Already resolved")

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

    def test_kitt_brochure_preview_link_is_pre_emptively_rejected(self):
        # Same reasoning as the Canva case above, confirmed via live
        # Playwright recon rather than assumed (see brochure_link_
        # resolver.is_kitt_brochure_preview_link's own docstring) - a
        # plain, unauthenticated fetch of this link shape never returns
        # usable document content, so it's excluded before ever wasting a
        # real fetch attempt.
        self.assertEqual(
            brochure_enrichment.classify_link_eligibility(_KITT_URL),
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


class RegeocodeRowsWithNewlyBackfilledAddressesTests(unittest.TestCase):
    """
    _regeocode_rows_with_newly_backfilled_addresses - run_brochure_
    enrichment's own post-enrichment step: a row whose address_1/postcode
    was blank at geocode time (so geocode.py's own Tier 2 zero-hint
    fallback had nothing to check itself against - see schema.ListingRow.
    geocode_unverified's own docstring) may have just had a real address
    backfilled from its own brochure (see BUILDING_LEVEL_FIELDS/
    _apply_units_to_row above) - this re-geocodes it against that fresh
    address, letting a stale, unverified guess self-correct. Paired by
    index (see that function's own docstring on why, not property_id or
    object identity), so every test here passes single-element lists in
    the same order.
    """

    def _row(self, **kwargs):
        defaults = {"building": "Henly House", "provider": "Colliers"}
        defaults.update(kwargs)
        return ListingRow(**defaults)

    def test_backfilled_address_clears_a_stale_unverified_guess(self):
        original = self._row()
        enriched = self._row(
            address_1="Bolsover St", postcode="NW1 3AU", lat=51.5, lng=-0.09, geocode_unverified=True,
        )

        with patch(
            "geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.5237, "lng": -0.1436},
        ) as mock_geocoding:
            brochure_enrichment._regeocode_rows_with_newly_backfilled_addresses([original], [enriched])

        mock_geocoding.assert_called_once_with("Bolsover St, NW1 3AU, UK")
        self.assertEqual(enriched.lat, 51.5237)
        self.assertEqual(enriched.lng, -0.1436)
        self.assertIs(enriched.geocode_unverified, False)

    def test_backfilled_address_with_no_prior_geocode_result_gets_geocoded_for_the_first_time(self):
        original = self._row()
        enriched = self._row(address_1="Bolsover St", postcode="NW1 3AU")

        with patch("geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.5237, "lng": -0.1436}):
            brochure_enrichment._regeocode_rows_with_newly_backfilled_addresses([original], [enriched])

        self.assertEqual(enriched.lat, 51.5237)
        self.assertEqual(enriched.lng, -0.1436)
        self.assertIs(enriched.geocode_unverified, False)

    def test_a_row_whose_address_was_already_present_is_left_completely_untouched(self):
        original = self._row(address_1="Bolsover St", postcode="NW1 3AU", lat=51.5, lng=-0.09)
        enriched = self._row(address_1="Bolsover St", postcode="NW1 3AU", lat=51.5, lng=-0.09)

        with patch("geocode.call_geocoding_api") as mock_geocoding:
            brochure_enrichment._regeocode_rows_with_newly_backfilled_addresses([original], [enriched])

        mock_geocoding.assert_not_called()
        self.assertEqual(enriched.lat, 51.5)
        self.assertEqual(enriched.lng, -0.09)

    def test_tier_1_failure_on_the_backfilled_address_falls_through_to_existing_tier_2_behavior(self):
        original = self._row()
        enriched = self._row(
            address_1="Bolsover St", postcode="NW1 3AU", lat=51.5, lng=-0.09, geocode_unverified=True,
        )

        with patch("geocode.call_geocoding_api", return_value={"status": "ZERO_RESULTS"}), \
             patch("geocode.call_places_text_search", return_value={"status": "ZERO_RESULTS"}):
            brochure_enrichment._regeocode_rows_with_newly_backfilled_addresses([original], [enriched])

        # geocode_row's own Tier 1 branch failed, then its own Tier 2
        # (building-name-only, no source hint since address_1/postcode
        # were only just backfilled and haven't produced a hint here) also
        # found nothing - exactly today's existing "no match" outcome, not
        # a new special case. The stale guess was already cleared before
        # this attempt (see this function's own docstring on why) and is
        # never silently restored just because the retry didn't pan out;
        # geocode_unverified itself is left exactly as this function set
        # it going in (True), still correctly describing this row's own
        # now-blank lat/lng as unverified, never flipped to False by a
        # call that resolved nothing.
        self.assertIsNone(enriched.lat)
        self.assertIsNone(enriched.lng)
        self.assertIs(enriched.geocode_unverified, True)

    def test_address_1_alone_backfilled_is_still_enough_to_trigger_a_regeocode(self):
        original = self._row(postcode="NW1 3AU")
        enriched = self._row(address_1="Bolsover St", postcode="NW1 3AU")

        with patch(
            "geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.5237, "lng": -0.1436},
        ) as mock_geocoding:
            brochure_enrichment._regeocode_rows_with_newly_backfilled_addresses([original], [enriched])

        mock_geocoding.assert_called_once()

    def test_postcode_alone_backfilled_is_still_enough_to_trigger_a_regeocode(self):
        original = self._row(address_1="Bolsover St")
        enriched = self._row(address_1="Bolsover St", postcode="NW1 3AU")

        with patch(
            "geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.5237, "lng": -0.1436},
        ) as mock_geocoding:
            brochure_enrichment._regeocode_rows_with_newly_backfilled_addresses([original], [enriched])

        mock_geocoding.assert_called_once()

    def test_bare_street_building_row_now_resolves_via_tier_1s_relaxed_path(self):
        # Real confirmed case: a MetSpace email row whose only location
        # text was "Clerkenwell Road" (bare street, no house number - see
        # _building_identity_matches' own tier 5) gets address_1 backfilled
        # to "67 Clerkenwell Rd" from its own brochure, but that SAME
        # brochure states no postcode for this floor - postcode stays
        # blank even after enrichment. This function's own trigger still
        # correctly fires (address_1 went from blank to genuinely non-
        # placeholder), calling geocode_row - which now (see geocode.py's
        # own module docstring, "Tier 1 also runs for a row with a
        # genuinely numbered address_1... but no postcode yet") resolves
        # real coordinates via address_1 alone and backfills postcode
        # itself via a reverse-geocode, even though row.building is still
        # the unchanged bare "Clerkenwell Road" Tier 2 alone could never
        # have resolved. Verified directly against the real Geocoding API
        # with this exact real address in tests/test_geocode.py's own
        # Tier1AddressWithoutPostcodeTests.
        original = self._row(building="Clerkenwell Road")
        enriched = self._row(building="Clerkenwell Road", address_1="67 Clerkenwell Rd", submarket="Clerkenwell")

        with patch(
            "geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.5219197, "lng": -0.1077003},
        ) as mock_geocoding_api, patch(
            "geocode.call_reverse_geocoding_api",
            return_value={"status": "OK", "address_components": [
                {"long_name": "EC1R 5BL", "types": ["postal_code"]},
            ]},
        ):
            brochure_enrichment._regeocode_rows_with_newly_backfilled_addresses([original], [enriched])

        mock_geocoding_api.assert_called_once_with("67 Clerkenwell Rd, London, UK")
        self.assertEqual(enriched.lat, 51.5219197)
        self.assertEqual(enriched.lng, -0.1077003)
        self.assertEqual(enriched.postcode, "EC1R 5BL")
        self.assertIs(enriched.geocode_unverified, False)

    def test_placeholder_to_real_address_transition_triggers_a_regeocode(self):
        # The confirmed real "Nineteen Wells St" shape: address_1 wasn't
        # blank, just a copy of building - the OLD address_1_backfilled
        # check (blank -> non-blank) would never have fired here at all.
        original = self._row(
            building="Nineteen Wells St", address_1="Nineteen Wells St", postcode="W1T 3PA",
            lat=51.0, lng=-0.1, geocode_unverified=True,
        )
        enriched = self._row(
            building="Nineteen Wells St", address_1="19 Wells Street", postcode="W1T 3PA",
            lat=51.0, lng=-0.1, geocode_unverified=True,
        )

        with patch(
            "geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.5177, "lng": -0.1416},
        ) as mock_geocoding:
            brochure_enrichment._regeocode_rows_with_newly_backfilled_addresses([original], [enriched])

        mock_geocoding.assert_called_once_with("19 Wells Street, W1T 3PA, UK")
        self.assertEqual(enriched.lat, 51.5177)
        self.assertEqual(enriched.lng, -0.1416)
        self.assertIs(enriched.geocode_unverified, False)

    def test_placeholder_to_real_with_street_suffix_abbreviation_still_triggers(self):
        # A backfilled address that only differs from the placeholder by a
        # street-suffix abbreviation ("St" -> "Street") must still count
        # as a genuine transition, not be mistaken for "already real".
        original = self._row(building="Nineteen Wells Street", address_1="Nineteen Wells St", postcode="W1T 3PA")
        enriched = self._row(building="Nineteen Wells Street", address_1="19 Wells Street", postcode="W1T 3PA")

        with patch(
            "geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.5177, "lng": -0.1416},
        ) as mock_geocoding:
            brochure_enrichment._regeocode_rows_with_newly_backfilled_addresses([original], [enriched])

        mock_geocoding.assert_called_once()

    def test_still_placeholder_after_enrichment_never_triggers(self):
        # The brochure had no real address either - address_1 is still a
        # placeholder on both sides, so no re-geocode call is warranted.
        original = self._row(building="Nineteen Wells St", address_1="Nineteen Wells St")
        enriched = self._row(building="Nineteen Wells St", address_1="Nineteen Wells St")

        with patch("geocode.call_geocoding_api") as mock_geocoding:
            brochure_enrichment._regeocode_rows_with_newly_backfilled_addresses([original], [enriched])

        mock_geocoding.assert_not_called()


class NineteenWellsStPlaceholderAddressEndToEndTests(EnrichmentTestCase):
    """
    The exact confirmed real case, end-to-end: a row whose address_1 is
    just a copy of its own building ("Nineteen Wells St" for both) - (1)
    becomes enrichment-eligible purely because of that placeholder address
    (see NeedsEnrichmentTests.test_placeholder_address_1_alone_needs_
    enrichment), (2) a matching brochure unit's real address_1 backfills
    it (enrich_rows_grouped -> _apply_units_to_row's BUILDING_LEVEL_FIELDS
    step), and (3) the existing re-geocode trigger then picks up that
    placeholder-to-real transition and clears a stale geocode_unverified
    guess on success - three separately-testable pieces, chained together
    here exactly as run_brochure_enrichment itself chains them (enrich_
    rows_grouped, then _regeocode_rows_with_newly_backfilled_addresses).
    """

    def _row(self, **kwargs):
        defaults = {
            "building": "Nineteen Wells St", "provider": "GPE", "floor_unit": "1st",
            "address_1": "Nineteen Wells St", "postcode": "W1T 3PA",
            "brochure_link": "https://example.com/brochure.pdf",
            "lat": 51.0, "lng": -0.1, "geocode_unverified": True,
            # Every OTHER ENRICHABLE_FIELDS already filled, so the
            # placeholder address_1 is A reason this row is eligible -
            # proves point (1) above, not just (2)/(3). (Since the special_
            # features decoupling fix, a non-blank brochure_link alone is
            # now also sufficient on its own - see NeedsEnrichmentTests -
            # but this row's placeholder address_1 makes it eligible either
            # way, so that doesn't weaken what this test demonstrates.)
            "submarket": "Fitzrovia", "size_sqft": 1000, "desks_max": 20, "rent_pcm": 5000, "rent_psf": 60,
            "special_features": "Nice", "state_of_space": "Cat A", "contacts": "Jane, jane@x.com",
        }
        defaults.update(kwargs)
        return ListingRow(**defaults)

    def test_placeholder_address_backfilled_then_regeocoded_and_unverified_cleared(self):
        row = self._row()
        self.assertTrue(brochure_enrichment.needs_enrichment(row))  # (1)

        units = [{"building": "Nineteen Wells St", "floor_unit": "1st", "address_1": "19 Wells Street"}]
        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            enriched_rows, _, _ = brochure_enrichment.enrich_rows_grouped([row])

        enriched = enriched_rows[0]
        self.assertEqual(enriched.address_1, "19 Wells Street")  # (2)
        # geocode_unverified/lat/lng are untouched by enrichment itself -
        # still the stale Tier 2 guess, exactly as _regeocode_rows_with_
        # newly_backfilled_addresses' own docstring expects going in.
        self.assertIs(enriched.geocode_unverified, True)

        with patch(
            "geocode.call_geocoding_api", return_value={"status": "OK", "lat": 51.5177, "lng": -0.1416},
        ) as mock_geocoding:
            brochure_enrichment._regeocode_rows_with_newly_backfilled_addresses([row], enriched_rows)

        mock_geocoding.assert_called_once_with("19 Wells Street, W1T 3PA, UK")  # (3)
        self.assertEqual(enriched.lat, 51.5177)
        self.assertEqual(enriched.lng, -0.1416)
        self.assertIs(enriched.geocode_unverified, False)

    def test_brochure_with_no_real_address_falls_through_with_no_error(self):
        # The brochure itself doesn't state a real address either - the
        # row stays eligible (nothing wrong with checking), but nothing
        # changes and no re-geocode is triggered; no exception anywhere in
        # the chain.
        row = self._row()

        units = [{"building": "Nineteen Wells St", "floor_unit": "1st"}]
        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            enriched_rows, _, _ = brochure_enrichment.enrich_rows_grouped([row])

        enriched = enriched_rows[0]
        self.assertEqual(enriched.address_1, "Nineteen Wells St")

        with patch("geocode.call_geocoding_api") as mock_geocoding:
            brochure_enrichment._regeocode_rows_with_newly_backfilled_addresses([row], enriched_rows)

        mock_geocoding.assert_not_called()
        self.assertIs(enriched.geocode_unverified, True)  # left exactly as it was


class FullyFilledRowSpecialFeaturesGateEndToEndTests(EnrichmentTestCase):
    """
    The confirmed real bug, end-to-end: a row where every single
    ENRICHABLE_FIELDS value - special_features itself included - is
    already filled (the real shape of all 41 real Colliers rows) used to
    be excluded from enrichment entirely by needs_enrichment, so _apply_
    units_to_row's own "combine, never gated on special_features already
    being non-blank" logic (see its own docstring) never even got a
    chance to run for it.
    """

    def _row(self, **kwargs):
        defaults = {
            "building": "The Canal Building", "address_1": "1 Example Street", "postcode": "EC1A 1AA",
            "submarket": "City", "floor_unit": "5th Floor", "size_sqft": 1000, "desks_max": 20,
            "rent_pcm": 5000, "rent_psf": 60, "special_features": "1 meeting room; Term 24 months",
            "state_of_space": "Cat A", "contacts": "Jane, jane@x.com",
            "brochure_link": "https://example.com/brochure.pdf",
        }
        defaults.update(kwargs)
        return ListingRow(**defaults)

    def test_fully_filled_row_still_gets_building_level_amenities_appended(self):
        row = self._row()
        units = _brochure_units(
            [{"building": "The Canal Building", "floor_unit": "5th Floor"}],
            building_features=[{"building": "The Canal Building", "features": "Exposed beams; canalside frontage"}],
        )
        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            enriched_rows, _, _ = brochure_enrichment.enrich_rows_grouped([row])

        enriched = enriched_rows[0]
        self.assertEqual(
            enriched.special_features, "1 meeting room; Term 24 months; Exposed beams; canalside frontage",
        )
        # Every OTHER already-filled field is completely untouched - proves
        # this is genuinely a special_features-only gating change, not a
        # general re-enrichment of an already-complete row (_apply_units_
        # to_row's own per-field blank check, unaffected by this fix,
        # already guarantees this independently of needs_enrichment).
        self.assertEqual(enriched.address_1, "1 Example Street")
        self.assertEqual(enriched.postcode, "EC1A 1AA")
        self.assertEqual(enriched.submarket, "City")
        self.assertEqual(enriched.size_sqft, 1000)
        self.assertEqual(enriched.desks_max, 20)
        self.assertEqual(enriched.rent_pcm, 5000)
        self.assertEqual(enriched.rent_psf, 60)
        self.assertEqual(enriched.state_of_space, "Cat A")
        self.assertEqual(enriched.contacts, "Jane, jane@x.com")

    def test_fully_filled_row_with_genuinely_nothing_new_is_unaffected(self):
        # The real Kingsland House shape: enrichment now runs at all
        # (unlike before this fix), but the brochure genuinely has no
        # building/property-level text for this building - the row must
        # come back with special_features byte-identical to before.
        row = self._row()
        units = _brochure_units([{"building": "The Canal Building", "floor_unit": "5th Floor"}])

        with patch("brochure_enrichment._extract_brochure_units", return_value=units):
            enriched_rows, _, _ = brochure_enrichment.enrich_rows_grouped([row])

        self.assertEqual(enriched_rows[0].special_features, "1 meeting room; Term 24 months")

    def test_fully_filled_row_with_no_brochure_link_is_never_even_attempted(self):
        # No brochure_link at all - needs_enrichment's own decoupled
        # special_features check requires one (see NeedsEnrichmentTests),
        # so this row is correctly excluded from the worklist entirely,
        # exactly as before this fix.
        row = self._row(brochure_link=None)

        with patch("brochure_enrichment._extract_brochure_units") as mock_extract:
            enriched_rows, _, _ = brochure_enrichment.enrich_rows_grouped([row])

        mock_extract.assert_not_called()
        self.assertEqual(enriched_rows[0].special_features, "1 meeting room; Term 24 months")


if __name__ == "__main__":
    unittest.main()
