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

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app
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
        png_pages = [_make_png((1, 0, 0)), _make_png((0, 1, 0)), _make_png((0, 0, 1))]
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=png_pages):
            result = app._fetch_pasted_link("https://canva.link/cvkmdcet1gpz149")

        self.assertIsInstance(result, app._PastedLinkFile)
        doc = fitz.open("pdf", result.getvalue())
        try:
            self.assertEqual(doc.page_count, 3)
        finally:
            doc.close()

    def test_total_fetch_failure_returns_none(self):
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=None):
            result = app._fetch_pasted_link("https://drive.google.com/file/d/doesnotexist/view")

        self.assertIsNone(result)


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
    """The Canva/Pitch render path - brochure_enrichment._fetch_pdf_bytes
    returns list[bytes] (PNG pages), never a PDF directly - exercises the
    real _pdf_bytes_from_png_pages assembly, not just a stubbed shortcut."""

    def setUp(self):
        _clear_pending()

    def tearDown(self):
        _clear_pending()

    def test_png_pages_are_assembled_into_one_multi_page_pdf(self):
        url = "https://canva.link/cvkmdcet1gpz149"
        png_pages = [_make_png((1, 0, 0)), _make_png((0, 1, 0)), _make_png((0, 0, 1))]
        with patch("brochure_enrichment._fetch_pdf_bytes", return_value=png_pages), \
             patch("extract.extract", return_value=[]) as mock_pdf_extract:
            at = _run_upload_page()
            _add_link(at, url)
            self.assertFalse(at.exception)

            markdown_text = "".join(m.value for m in at.markdown)
            self.assertIn(f"🔗 {app._filename_from_url(url)}", markdown_text)

            extract_buttons = [b for b in at.button if b.label == "Extract"]
            extract_buttons[0].click().run()
            self.assertFalse(at.exception)

        mock_pdf_extract.assert_called_once()
        self.assertEqual(mock_pdf_extract.call_args.kwargs["original_filename"], app._filename_from_url(url))
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


if __name__ == "__main__":
    unittest.main()
