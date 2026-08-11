"""
Regression tests for brochure_enrichment.py - secondary enrichment of
spreadsheet-extracted ListingRows from their own linked brochure. Never
calls the real network or the real Gemini API - httpx.get and extract.
extract_raw_units are mocked throughout (same principle as test_extract_
spreadsheet_gemini.py mocking call_gemini).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_brochure_enrichment -v
"""

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

    def test_both_populated_does_not_need_enrichment(self):
        row = ListingRow(building="A", special_features="Nice", state_of_space="Cat A")
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

    def test_youtube_url_is_not_eligible(self):
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url("https://youtube.com/watch?v=abc123"))
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url("https://youtu.be/abc123"))

    def test_generic_homepage_is_not_eligible(self):
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url("https://www.someprovider.co.uk"))

    def test_social_profile_is_not_eligible(self):
        self.assertFalse(brochure_enrichment._is_eligible_brochure_url("https://www.linkedin.com/company/x"))


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

    def test_populated_special_features_not_overwritten(self):
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

        self.assertEqual(new_row.special_features, "Existing genuine description")
        self.assertEqual(fields, [])
        self.assertIs(new_row, row)

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
        row = ListingRow(
            building="A", floor_unit="1st", brochure_link="https://example.com/brochure.pdf",
            special_features="Already have this", state_of_space="Cat A",
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
            ListingRow(building="B", floor_unit="1st", brochure_link="https://example.com/b.pdf", special_features="Already there", state_of_space="Cat A"),
            ListingRow(building="C", floor_unit="1st", brochure_link=None, special_features=None),
        ]
        eligible, urls = brochure_enrichment.eligible_rows_and_brochures(rows)
        self.assertEqual(len(eligible), 2)
        self.assertEqual(urls, ["https://example.com/a.pdf"])

    def test_no_eligible_rows_returns_empty(self):
        rows = [ListingRow(building="A", special_features="x", state_of_space="y")]
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
        rows = [ListingRow(
            building="A", brochure_link="https://example.com/a.pdf", special_features="Done", state_of_space="Cat A",
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


if __name__ == "__main__":
    unittest.main()
