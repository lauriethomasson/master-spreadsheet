"""
Regression tests for pasting a link on the Upload page (app.py) - adds an
entry to the SAME "ready to extract" list as an uploaded file, each with
its own icon (📄 file / 🔗 link) and its own remove control; one Extract
press processes everything in that list together.

Mocks brochure_enrichment._fetch_pdf_bytes - the one network boundary
app._fetch_pasted_link's own precedence (direct PDF / Canva-Pitch render /
resolve_brochure_link fallback) is built on - rather than app._fetch_
pasted_link itself: app.py is executed by AppTest as its own independent
script, not imported through the normal module system, so patching an
attribute on a separately-`import app`-ed module object here has no
effect on AppTest's own run (confirmed directly - see git history/PR
notes). Patching brochure_enrichment (a real, sys.modules-cached
dependency app.py's own exec'd script imports the normal way, exactly
like every other test in this suite already does for extract.extract/
extract_spreadsheet_gemini.call_gemini) exercises the REAL _fetch_pasted_
link/_pdf_bytes_from_png_pages/_filename_from_url logic end-to-end, never
just a mocked stand-in for it.

The real fetch precedence itself was verified separately against a real
Canva link, a real direct-PDF link, and a real gated link (see the PR
description) - not repeated here as a mocked unit test.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_upload_paste_a_link -v
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app
from schema import ListingRow
from storage.file_store import list_pending_staging_files

BASE = Path(__file__).resolve().parent.parent


def _clear_pending():
    for p in list_pending_staging_files():
        (BASE / p).unlink(missing_ok=True)
        (BASE / p).with_suffix(".meta.json").unlink(missing_ok=True)


def _run_upload_page():
    at = AppTest.from_file(str(BASE / "app.py"), default_timeout=30)
    at.run()
    return at


def _add_link(at, url):
    at.text_input[0].set_value(url)
    add_buttons = [b for b in at.button if b.label == "Add link"]
    add_buttons[0].click().run()
    return at


def _make_png(color) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=200, height=100)
    page.draw_rect(fitz.Rect(0, 0, 200, 100), color=color, fill=color)
    png_bytes = page.get_pixmap().tobytes("png")
    doc.close()
    return png_bytes


class FetchPastedLinkUnitTests(unittest.TestCase):
    """
    Direct, non-AppTest unit tests of app._fetch_pasted_link itself -
    calling it plainly works (unlike patching it from inside an AppTest
    run - see this file's own module docstring) since it makes no
    Streamlit calls of its own; this is what actually verifies the
    assembled PDF's real page count/content, which the AppTest-level
    tests above can't inspect (the temp file is deleted right after
    extract.extract() returns).
    """

    def test_direct_pdf_bytes_pass_through_unchanged(self):
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=b"%PDF-1.4 fake pdf bytes"):
            result = app._fetch_pasted_link("https://example.com/brochure.pdf")

        self.assertIsInstance(result, app._PastedLinkFile)
        self.assertEqual(result.name, "brochure.pdf")
        self.assertEqual(result.getvalue(), b"%PDF-1.4 fake pdf bytes")

    def test_png_pages_are_assembled_into_a_real_multi_page_pdf(self):
        # A Canva/Pitch link is now intercepted BEFORE _fetch_pdf_bytes
        # (see app._fetch_pasted_link's own docstring) specifically so
        # each page's own real link candidates can be captured alongside
        # the pages themselves - mocking brochure_enrichment.fetch_
        # rendered_page_with_links, not _fetch_pdf_bytes, for this shape.
        png_pages = [_make_png((1, 0, 0)), _make_png((0, 1, 0)), _make_png((0, 0, 1))]
        page_links = [[], [], []]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch(
                    "brochure_enrichment.fetch_rendered_page_with_links",
                    return_value=(png_pages, page_links),
                ) as mock_fetch:
            result = app._fetch_pasted_link("https://canva.link/cvkmdcet1gpz149")

        mock_fetch.assert_called_once_with("https://canva.link/cvkmdcet1gpz149")
        self.assertIsInstance(result, app._PastedLinkFile)
        self.assertEqual(result.png_pages, png_pages)
        self.assertEqual(result.page_links, page_links)
        doc = fitz.open("pdf", result.getvalue())
        try:
            self.assertEqual(doc.page_count, 3)
        finally:
            doc.close()

    def test_canva_url_without_the_renderer_configured_falls_through_to_the_generic_path(self):
        # No CANVA_RENDERER_URL set - _canva_renderer_configured() is
        # False, so this must fall through to the plain _fetch_pdf_bytes
        # path exactly like before this feature existed (which itself
        # already knows not to attempt an unconfigured canva/pitch render
        # - see that function's own canva/pitch branches).
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=None) as mock_fetch, \
                patch("brochure_enrichment.fetch_rendered_page_with_links") as mock_with_links:
            result = app._fetch_pasted_link("https://canva.link/cvkmdcet1gpz149")

        self.assertIsNone(result)
        mock_fetch.assert_called_once_with("https://canva.link/cvkmdcet1gpz149")
        mock_with_links.assert_not_called()

    def test_total_fetch_failure_returns_none(self):
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=None):
            result = app._fetch_pasted_link("https://drive.google.com/file/d/doesnotexist/view")

        self.assertIsNone(result)


class ValidatePastedLinkBrochureLinksTests(unittest.TestCase):
    """Direct, non-AppTest unit tests of app._validate_pasted_link_
    brochure_links - the per-property link validation/fallback step for
    a pasted Canva/Pitch link's own rows (see the Extract loop's own
    png_pages branch)."""

    SHARED_URL = "https://storage.example.com/pasted-deck.pdf"

    def _row(self, brochure_link, brochure_link_is_floorplan=None):
        return ListingRow(
            building="Test Building", provider="Colliers",
            brochure_link=brochure_link, brochure_link_is_floorplan=brochure_link_is_floorplan,
        )

    def test_a_link_that_fetches_as_a_real_document_is_kept(self):
        row = self._row("https://blob.example.com/gloucester.pdf")
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=b"%PDF-1.4 real") as mock_fetch:
            app._validate_pasted_link_brochure_links([row], self.SHARED_URL)

        mock_fetch.assert_called_once_with("https://blob.example.com/gloucester.pdf")
        self.assertEqual(row.brochure_link, "https://blob.example.com/gloucester.pdf")

    def test_a_link_that_fails_to_fetch_falls_back_to_the_shared_link(self):
        row = self._row("https://www.colliers.com/en-gb/properties/kingsland-house")
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=None):
            app._validate_pasted_link_brochure_links([row], self.SHARED_URL)

        self.assertEqual(row.brochure_link, self.SHARED_URL)

    def test_a_link_already_equal_to_the_shared_fallback_is_never_fetched(self):
        row = self._row(self.SHARED_URL)
        with patch("brochure_enrichment._fetch_pdf_bytes") as mock_fetch:
            app._validate_pasted_link_brochure_links([row], self.SHARED_URL)

        mock_fetch.assert_not_called()
        self.assertEqual(row.brochure_link, self.SHARED_URL)

    def test_a_blank_brochure_link_is_never_fetched(self):
        row = self._row(None)
        with patch("brochure_enrichment._fetch_pdf_bytes") as mock_fetch:
            app._validate_pasted_link_brochure_links([row], self.SHARED_URL)

        mock_fetch.assert_not_called()
        self.assertIsNone(row.brochure_link)

    def test_a_floorplan_substituted_link_is_left_alone_even_if_it_would_fail(self):
        # brochure_link_is_floorplan is a separate, pre-existing mechanism
        # this feature doesn't touch - never validated/replaced here.
        row = self._row("https://app.box.com/s/some-floorplan", brochure_link_is_floorplan=True)
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=None) as mock_fetch:
            app._validate_pasted_link_brochure_links([row], self.SHARED_URL)

        mock_fetch.assert_not_called()
        self.assertEqual(row.brochure_link, "https://app.box.com/s/some-floorplan")

    def test_mixed_batch_only_the_failing_row_falls_back(self):
        good_row = self._row("https://blob.example.com/good.pdf")
        bad_row = self._row("https://www.colliers.com/en-gb/properties/blocked")

        def fake_fetch(url):
            return b"%PDF-1.4" if url == "https://blob.example.com/good.pdf" else None

        with patch("brochure_enrichment._fetch_pdf_bytes", side_effect=fake_fetch):
            app._validate_pasted_link_brochure_links([good_row, bad_row], self.SHARED_URL)

        self.assertEqual(good_row.brochure_link, "https://blob.example.com/good.pdf")
        self.assertEqual(bad_row.brochure_link, self.SHARED_URL)


class InvalidLinkTests(unittest.TestCase):
    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_blank_input_shows_a_warning_and_adds_nothing(self):
        at = _run_upload_page()
        add_buttons = [b for b in at.button if b.label == "Add link"]
        add_buttons[0].click().run()
        self.assertFalse(at.exception)

        self.assertEqual(at.session_state["pasted_links"], [])
        self.assertIn("Paste a link first.", "".join(w.value for w in at.warning))

    def test_non_url_text_shows_a_warning_and_adds_nothing(self):
        at = _run_upload_page()
        _add_link(at, "not a link at all")
        self.assertFalse(at.exception)

        self.assertEqual(at.session_state["pasted_links"], [])
        self.assertIn("doesn't look like a valid link", "".join(w.value for w in at.warning))


class SuccessfulDirectPdfLinkTests(unittest.TestCase):
    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_successful_link_appears_with_link_icon_and_no_warning(self):
        url = "https://example.com/brochure.pdf"
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=b"%PDF-1.4 fake pdf bytes") as mock_fetch:
            at = _run_upload_page()
            _add_link(at, url)
            self.assertFalse(at.exception)

        mock_fetch.assert_called_once_with(url)
        markdown_text = "".join(m.value for m in at.markdown)
        self.assertIn(f"🔗 {app._filename_from_url(url)}", markdown_text)
        self.assertEqual([w.value for w in at.warning], [])

    def test_successful_link_is_processed_by_extract(self):
        url = "https://example.com/brochure.pdf"
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=b"%PDF-1.4 fake pdf bytes"), \
             patch("extract.extract", return_value=[]) as mock_pdf_extract:
            at = _run_upload_page()
            _add_link(at, url)

            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        mock_pdf_extract.assert_called_once()
        self.assertEqual(mock_pdf_extract.call_args.kwargs["original_filename"], "brochure.pdf")
        self.assertEqual(len(list_pending_staging_files()), 1)


class SuccessfulCanvaStyleLinkTests(unittest.TestCase):
    """The Canva/Pitch render path - a Canva/Pitch link is intercepted
    before _fetch_pdf_bytes (see app._fetch_pasted_link's own docstring)
    so each page's own real link candidates can be captured alongside
    the pages themselves, and extracted via extract.extract_from_png_
    pages directly (never by re-rasterizing the assembled PDF bytes back
    into images - see that function's own docstring)."""

    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_png_pages_are_assembled_and_extracted_via_extract_from_png_pages(self):
        url = "https://canva.link/cvkmdcet1gpz149"
        png_pages = [_make_png((1, 0, 0)), _make_png((0, 1, 0)), _make_png((0, 0, 1))]
        page_links = [[], [], []]
        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch(
                    "brochure_enrichment.fetch_rendered_page_with_links",
                    return_value=(png_pages, page_links),
                ), \
                patch("extract.extract_from_png_pages", return_value=[]) as mock_extract, \
                patch("extract.extract") as mock_plain_extract:
            at = _run_upload_page()
            _add_link(at, url)
            self.assertFalse(at.exception)

            markdown_text = "".join(m.value for m in at.markdown)
            self.assertIn(f"🔗 {app._filename_from_url(url)}", markdown_text)

            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        mock_extract.assert_called_once()
        self.assertEqual(mock_extract.call_args.args[0], png_pages)
        self.assertEqual(mock_extract.call_args.kwargs["original_filename"], app._filename_from_url(url))
        self.assertEqual(mock_extract.call_args.kwargs["page_links"], page_links)
        mock_plain_extract.assert_not_called()  # never the re-rasterizing PDF-file path for this source
        self.assertEqual(len(list_pending_staging_files()), 1)


class FailedLinkTests(unittest.TestCase):
    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_failed_link_shows_the_exact_inline_message(self):
        url = "https://drive.google.com/file/d/doesnotexist/view"
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=None) as mock_fetch:
            at = _run_upload_page()
            _add_link(at, url)
            self.assertFalse(at.exception)

        mock_fetch.assert_called_once()
        warnings = "".join(w.value for w in at.warning)
        # AppTest's own .value strips a message's LEADING emoji (Streamlit
        # treats it as the widget's icon, rendered separately - see
        # PASTED_LINK_UNREADABLE_MESSAGE's own leading "⚠️" in app.py) - the
        # remaining text is still asserted verbatim.
        self.assertIn(
            "Couldn't read this link — it looks like it needs a sign-in or email first. "
            "Save it as a PDF and upload that instead.",
            warnings,
        )
        markdown_text = "".join(m.value for m in at.markdown)
        self.assertIn(f"🔗 {url}", markdown_text)

    def test_failed_link_is_excluded_from_extract_with_nothing_to_process(self):
        url = "https://drive.google.com/file/d/doesnotexist/view"
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=None), \
             patch("extract.extract") as mock_pdf_extract:
            at = _run_upload_page()
            _add_link(at, url)

            # Nothing ready at all - Extract shouldn't even be clickable/present.
            extract_buttons = [b for b in at.button if b.label == "Extract"]
            self.assertEqual(extract_buttons, [])

        mock_pdf_extract.assert_not_called()
        self.assertEqual(list_pending_staging_files(), [])

    def test_removing_a_failed_link_removes_it_from_the_list(self):
        url = "https://drive.google.com/file/d/doesnotexist/view"
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=None):
            at = _run_upload_page()
            _add_link(at, url)
            self.assertEqual(len(at.session_state["pasted_links"]), 1)

            entry_id = at.session_state["pasted_links"][0]["id"]
            remove_button = at.button(key=f"remove_link_{entry_id}")
            remove_button.click().run()
            self.assertFalse(at.exception)

        self.assertEqual(at.session_state["pasted_links"], [])
        self.assertEqual([w.value for w in at.warning], [])


class MixedBatchTests(unittest.TestCase):
    """The real scope-defining case: one uploaded file, one good link, and
    one bad link in the SAME batch - the good ones must still extract
    normally and the bad one's failure must never block or crash the rest."""

    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_good_file_and_good_link_both_extract_bad_link_excluded(self):
        good_url = "https://example.com/good.pdf"
        bad_url = "https://drive.google.com/file/d/doesnotexist/view"

        def fake_fetch(url):
            return b"%PDF-1.4 fake pdf bytes" if url == good_url else None

        with patch("brochure_enrichment._fetch_pdf_bytes", side_effect=fake_fetch), \
             patch("extract.extract", return_value=[]) as mock_pdf_extract:
            at = _run_upload_page()
            at.file_uploader[0].upload("uploaded.pdf", b"%PDF-1.4 fake", "application/pdf")
            at.run()

            _add_link(at, good_url)
            _add_link(at, bad_url)
            self.assertFalse(at.exception)

            # Both the failed AND the succeeded link show up, each labeled.
            markdown_text = "".join(m.value for m in at.markdown)
            self.assertIn("📄 uploaded.pdf", markdown_text)
            self.assertIn(f"🔗 {app._filename_from_url(good_url)}", markdown_text)
            self.assertIn(f"🔗 {bad_url}", markdown_text)
            warnings = "".join(w.value for w in at.warning)
            self.assertIn("Couldn't read this link", warnings)

            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        # Only the uploaded file + the good link were actually processed.
        self.assertEqual(mock_pdf_extract.call_count, 2)
        processed_names = {kwargs["original_filename"] for _, kwargs in mock_pdf_extract.call_args_list}
        self.assertEqual(processed_names, {"uploaded.pdf", "good.pdf"})
        self.assertEqual(len(list_pending_staging_files()), 2)

    def test_removing_the_uploaded_file_leaves_only_the_link_to_extract(self):
        url = "https://example.com/good.pdf"
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=b"%PDF-1.4 fake pdf bytes"), \
             patch("extract.extract", return_value=[]) as mock_pdf_extract:
            at = _run_upload_page()
            at.file_uploader[0].upload("uploaded.pdf", b"%PDF-1.4 fake", "application/pdf")
            at.run()
            _add_link(at, url)

            file_id = at.file_uploader[0].value[0].file_id
            remove_button = at.button(key=f"remove_upload_{file_id}")
            remove_button.click().run()
            self.assertFalse(at.exception)

            markdown_text = "".join(m.value for m in at.markdown)
            self.assertNotIn("📄 uploaded.pdf", markdown_text)
            self.assertIn(f"🔗 {app._filename_from_url(url)}", markdown_text)

            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        mock_pdf_extract.assert_called_once()
        self.assertEqual(mock_pdf_extract.call_args.kwargs["original_filename"], "good.pdf")
        self.assertEqual(len(list_pending_staging_files()), 1)


class PerPropertyLinkAttributionEndToEndTests(unittest.TestCase):
    """
    Full pipeline, only Gemini and the validation fetch mocked (real
    extract.extract_from_png_pages, real images_from_png_pages, real
    app._validate_pasted_link_brochure_links) - the real Colliers deck
    shape: one page describing a single property (Kingsland House) whose
    only real link is a Colliers.com webpage that turns out blocked, and
    another page whose building name IS the link, pointing directly at a
    real PDF.
    """

    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_kingsland_house_falls_back_gloucester_place_keeps_its_own_link(self):
        url = "https://canva.link/cvkmdcet1gpz149"
        png_pages = [_make_png((1, 0, 0)), _make_png((0, 1, 0))]
        page_links = [
            [{"href": "https://www.colliers.com/en-gb/properties/kingsland-house", "text": "LINK TO BROCHURE"}],
            [{"href": "https://blob.example.com/gloucester.pdf", "text": "27-29 Gloucester Place"}],
        ]
        raw = {
            "provider": "Colliers", "contacts": None,
            "units": [
                {
                    "building": "Kingsland House", "floor_unit": "Ground", "page_index": 0,
                    "brochure_link": "https://www.colliers.com/en-gb/properties/kingsland-house",
                },
                {
                    "building": "27-29 Gloucester Place", "floor_unit": "2nd", "page_index": 1,
                    "brochure_link": "https://blob.example.com/gloucester.pdf",
                },
            ],
        }

        def fake_validate_fetch(fetch_url):
            # Kingsland's own Colliers.com page is blocked to a plain
            # fetch (real, confirmed HTTP 403); Gloucester's own Azure
            # blob URL is a real, directly-fetchable PDF (also real,
            # confirmed) - see the PR's own recon findings.
            return b"%PDF-1.4 real" if fetch_url == "https://blob.example.com/gloucester.pdf" else None

        with patch.dict(os.environ, {"CANVA_RENDERER_URL": "https://canva-renderer.example.run.app"}), \
                patch(
                    "brochure_enrichment.fetch_rendered_page_with_links",
                    return_value=(png_pages, page_links),
                ), \
                patch("extract.get_client", return_value="fake-client"), \
                patch("extract.call_gemini", return_value=raw), \
                patch("brochure_enrichment._fetch_pdf_bytes", side_effect=fake_validate_fetch), \
                patch("geocode.geocode_rows"):
            at = _run_upload_page()
            _add_link(at, url)
            self.assertFalse(at.exception)

            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        from storage.file_store import load_staging_as_dataframe

        staged = list_pending_staging_files()
        self.assertEqual(len(staged), 1)
        df = load_staging_as_dataframe(staged[0])
        by_building = {row["building"]: row["brochure_link"] for _, row in df.iterrows()}

        # Kingsland House's only real candidate is blocked - must NOT end
        # up with the dead Colliers.com link; falls back to the shared
        # document-level link instead.
        self.assertNotEqual(
            by_building["Kingsland House"], "https://www.colliers.com/en-gb/properties/kingsland-house",
        )
        # 27-29 Gloucester Place's own real PDF link survives validation -
        # its own genuine per-property link, not the shared fallback.
        self.assertEqual(by_building["27-29 Gloucester Place"], "https://blob.example.com/gloucester.pdf")
        self.assertNotEqual(by_building["27-29 Gloucester Place"], by_building["Kingsland House"])


class PropagateValidatedLinksWithinPageTests(unittest.TestCase):
    """
    app._propagate_validated_links_within_page - the deterministic backfill
    that runs right after _validate_pasted_link_brochure_links. Real,
    confirmed gap: against the real Colliers deck, Gemini sometimes only
    attributes a page's own real link to the FIRST of several rows sharing
    the same building on the same page, leaving the rest on the shared
    document-level fallback even though they're unambiguously the same
    property - not the multi-building-per-page case that justifies trusting
    Gemini's own per-row pick in the first place. Uses the real building
    name/link from that deck's own "27-29 Gloucester Place" page.
    """

    FALLBACK = "https://storage.example/colliers-deck.pdf"
    REAL_LINK = (
        "https://listingsprod.blob.core.windows.net/ourlistings-gbr/"
        "530d3baf-4d77-4824-be42-adb15270c44b/0d33a8d7-3b7c-4ef5-b1a0-c314926f1605"
    )

    def _row(self, building, floor_unit, brochure_link, brochure_link_is_floorplan=None):
        return ListingRow(
            building=building, floor_unit=floor_unit, brochure_link=brochure_link,
            brochure_link_is_floorplan=brochure_link_is_floorplan,
        )

    def test_backfills_the_other_four_rows_when_only_one_of_five_got_the_real_link(self):
        rows = [
            self._row("27-29 Gloucester Place", "2nd (Office 6)", self.REAL_LINK),
            self._row("27-29 Gloucester Place", "2nd (Office 7)", self.FALLBACK),
            self._row("27-29 Gloucester Place", "3rd (Office 8)", self.FALLBACK),
            self._row("27-29 Gloucester Place", "3rd (Office 9)", self.FALLBACK),
            self._row("27-29 Gloucester Place", "4th (Office 11)", self.FALLBACK),
        ]
        page_indices = [4, 4, 4, 4, 4]

        app._propagate_validated_links_within_page(rows, page_indices, self.FALLBACK)

        self.assertTrue(all(row.brochure_link == self.REAL_LINK for row in rows))

    def test_never_propagates_across_different_pages(self):
        rows = [
            self._row("27-29 Gloucester Place", "2nd", self.REAL_LINK),
            self._row("27-29 Gloucester Place", "3rd", self.FALLBACK),
        ]
        page_indices = [4, 9]  # same building, but NOT the same page - unrelated to each other

        app._propagate_validated_links_within_page(rows, page_indices, self.FALLBACK)

        self.assertEqual(rows[1].brochure_link, self.FALLBACK)

    def test_never_propagates_across_different_buildings_on_the_same_page(self):
        rows = [
            self._row("27-29 Gloucester Place", "2nd", self.REAL_LINK),
            self._row("3 Fitzhardinge Street", "3rd", self.FALLBACK),
        ]
        page_indices = [4, 4]  # same page, but a genuinely different building

        app._propagate_validated_links_within_page(rows, page_indices, self.FALLBACK)

        self.assertEqual(rows[1].brochure_link, self.FALLBACK)

    def test_ambiguous_group_with_two_different_validated_links_is_left_alone(self):
        other_real_link = "https://blob.example.com/other-office.pdf"
        rows = [
            self._row("27-29 Gloucester Place", "2nd", self.REAL_LINK),
            self._row("27-29 Gloucester Place", "3rd", other_real_link),
            self._row("27-29 Gloucester Place", "4th", self.FALLBACK),
        ]
        page_indices = [4, 4, 4]

        app._propagate_validated_links_within_page(rows, page_indices, self.FALLBACK)

        self.assertEqual(rows[0].brochure_link, self.REAL_LINK)
        self.assertEqual(rows[1].brochure_link, other_real_link)
        self.assertEqual(rows[2].brochure_link, self.FALLBACK)

    def test_floorplan_substituted_link_is_excluded_from_both_sides(self):
        rows = [
            self._row("27-29 Gloucester Place", "2nd", self.REAL_LINK),
            self._row(
                "27-29 Gloucester Place", "3rd", "https://blob.example.com/floorplan.pdf",
                brochure_link_is_floorplan=True,
            ),
        ]
        page_indices = [4, 4]

        app._propagate_validated_links_within_page(rows, page_indices, self.FALLBACK)

        # Never overwritten - a floorplan substitution is a different kind
        # of fact, not a row still "on the fallback" waiting to be filled.
        self.assertEqual(rows[1].brochure_link, "https://blob.example.com/floorplan.pdf")

    def test_no_page_indices_is_a_no_op(self):
        rows = [
            self._row("27-29 Gloucester Place", "2nd", self.REAL_LINK),
            self._row("27-29 Gloucester Place", "3rd", self.FALLBACK),
        ]

        app._propagate_validated_links_within_page(rows, None, self.FALLBACK)

        self.assertEqual(rows[1].brochure_link, self.FALLBACK)


if __name__ == "__main__":
    unittest.main()
