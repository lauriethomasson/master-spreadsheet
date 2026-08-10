"""
Regression test for extract._RENDER_LOCK (see extract_raw_units's own
docstring) - PyMuPDF/MuPDF's page rendering keeps process-wide, not
per-Document, internal state, so this serializes just the render step
across threads, added specifically to make brochure_enrichment.
enrich_rows_grouped's bounded worker pool safe. Confirms render_pages calls
never overlap even when extract_raw_units is invoked from several threads
at once, while the (mocked) Gemini call itself is free to overlap.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_extract_render_lock -v
"""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract


def _make_minimal_pdf() -> Path:
    doc = fitz.open()
    page = doc.new_page(width=200, height=100)
    page.insert_text((10, 20), "Test brochure page", fontsize=11)
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    doc.save(tmp_path)
    doc.close()
    return tmp_path


class RenderLockTests(unittest.TestCase):
    def test_render_pages_never_overlaps_across_threads(self):
        pdf_path = _make_minimal_pdf()
        lock = threading.Lock()
        state = {"current": 0, "max_seen": 0}
        real_render_pages = extract.render_pages

        def _tracked_render_pages(path):
            with lock:
                state["current"] += 1
                state["max_seen"] = max(state["max_seen"], state["current"])
            time.sleep(0.05)  # widens the window a concurrency bug would need to land in
            result = real_render_pages(path)
            with lock:
                state["current"] -= 1
            return result

        try:
            with patch("extract.render_pages", side_effect=_tracked_render_pages), \
                 patch("extract.get_client", return_value="fake-client"), \
                 patch("extract.call_gemini", return_value={"units": []}):
                threads = [threading.Thread(target=extract.extract_raw_units, args=(pdf_path,)) for _ in range(5)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
        finally:
            pdf_path.unlink(missing_ok=True)

        self.assertEqual(state["max_seen"], 1)

    def test_gemini_call_itself_is_not_serialized_by_the_render_lock(self):
        # The lock guards render_pages only - concurrent calls to
        # call_gemini (the actual network round trip, by far the more
        # expensive part) must still be able to genuinely overlap.
        pdf_path = _make_minimal_pdf()
        lock = threading.Lock()
        state = {"current": 0, "max_seen": 0}

        def _tracked_call_gemini(client, prompt, parts):
            with lock:
                state["current"] += 1
                state["max_seen"] = max(state["max_seen"], state["current"])
            time.sleep(0.05)
            with lock:
                state["current"] -= 1
            return {"units": []}

        try:
            with patch("extract.get_client", return_value="fake-client"), \
                 patch("extract.call_gemini", side_effect=_tracked_call_gemini):
                threads = [threading.Thread(target=extract.extract_raw_units, args=(pdf_path,)) for _ in range(5)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
        finally:
            pdf_path.unlink(missing_ok=True)

        self.assertGreater(state["max_seen"], 1)


if __name__ == "__main__":
    unittest.main()
