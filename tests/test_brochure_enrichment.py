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
    def test_direct_pdf_url_skips_landing_page_resolution(self):
        with patch("brochure_enrichment.resolve_brochure_link") as mock_resolve, \
             patch("brochure_enrichment.httpx.get", return_value=_response()) as mock_get, \
             patch("brochure_enrichment.extract.extract_raw_units", return_value={"units": []}):
            brochure_enrichment._extract_brochure_units("https://example.com/brochure.pdf")

        mock_resolve.assert_not_called()
        mock_get.assert_called_once()

    def test_landing_page_is_resolved_before_fetching(self):
        with patch("brochure_enrichment.resolve_brochure_link", return_value="https://example.com/real.pdf") as mock_resolve, \
             patch("brochure_enrichment.httpx.get", return_value=_response()) as mock_get, \
             patch("brochure_enrichment.extract.extract_raw_units", return_value={"units": []}):
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
             patch("brochure_enrichment.extract.extract_raw_units", side_effect=RuntimeError("bad json")):
            result = brochure_enrichment._extract_brochure_units("https://example.com/x.pdf")

        self.assertIsNone(result)

    def test_same_url_only_fetched_once(self):
        with patch("brochure_enrichment.httpx.get", return_value=_response()) as mock_get, \
             patch("brochure_enrichment.extract.extract_raw_units", return_value={"units": []}) as mock_extract:
            brochure_enrichment._extract_brochure_units("https://example.com/shared.pdf")
            brochure_enrichment._extract_brochure_units("https://example.com/shared.pdf")
            brochure_enrichment._extract_brochure_units("https://example.com/shared.pdf")

        mock_get.assert_called_once()
        mock_extract.assert_called_once()


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
             patch("brochure_enrichment.extract.extract_raw_units", return_value={"units": units}) as mock_extract:
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


if __name__ == "__main__":
    unittest.main()
