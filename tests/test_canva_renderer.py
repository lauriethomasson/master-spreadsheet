"""
Regression tests for canva_renderer/app.py - the separate, isolated
Playwright/Chromium service that renders a public Canva "view" link (see
that module's own docstring for why this is a SEPARATE service, never run
inside the main app's own container). Playwright's own browser is mocked
throughout - these tests never launch a real browser, matching this
repo's existing convention (test_brochure_enrichment.py never calls the
real network/Gemini API either).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_canva_renderer -v
"""

import importlib.util
import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Loaded via an explicit file path under a unique module name, never a
# bare "import app" - the main repo's OWN top-level app.py (the Streamlit
# entrypoint, imported as a bare "app" module by several other test files,
# e.g. test_app.py) would otherwise collide in sys.modules with this
# renderer's own, unrelated app.py: whichever one is imported-by-name
# FIRST in a given test process wins that cache slot, silently handing
# every later "import app" the WRONG module. Loading by path sidesteps
# that entirely - this always loads canva_renderer/app.py specifically,
# regardless of what else has already been imported in this process (see
# the full-suite discovery run that first caught this exact collision).
_spec = importlib.util.spec_from_file_location(
    "canva_renderer_app", Path(__file__).resolve().parent.parent / "canva_renderer" / "app.py",
)
canva_renderer = importlib.util.module_from_spec(_spec)
sys.modules["canva_renderer_app"] = canva_renderer
_spec.loader.exec_module(canva_renderer)


class IsRecognizedCanvaUrlTests(unittest.TestCase):
    def test_real_view_link_is_recognized(self):
        self.assertTrue(canva_renderer._is_recognized_canva_url(
            "https://www.canva.com/design/DAGzsWW-Yp8/s8tPVTQe6HUQa939xX0XQw/view"
        ))

    def test_short_link_is_recognized(self):
        self.assertTrue(canva_renderer._is_recognized_canva_url("https://canva.link/abc123"))

    def test_non_canva_url_is_rejected(self):
        self.assertFalse(canva_renderer._is_recognized_canva_url("https://example.com/brochure.pdf"))

    def test_canva_homepage_is_rejected(self):
        self.assertFalse(canva_renderer._is_recognized_canva_url("https://www.canva.com/"))


class HostAllowedSsrfTests(unittest.TestCase):
    """
    The renderer's core SSRF defense - a strict allow-list, not a
    denylist. Rejecting everything that isn't canva.com/canva.link
    inherently rejects localhost/private IPs/file:///custom schemes too,
    with no separate IP-range logic to keep correct.
    """

    def test_canva_com_is_allowed(self):
        self.assertTrue(canva_renderer._host_allowed("https://www.canva.com/design/x/y/view"))

    def test_canva_link_is_allowed(self):
        self.assertTrue(canva_renderer._host_allowed("https://canva.link/abc"))

    def test_lookalike_domain_is_rejected(self):
        # "canva.com.evil.com" must NOT match ".canva.com".
        self.assertFalse(canva_renderer._host_allowed("https://canva.com.evil.com/design/x/y/view"))

    def test_localhost_is_rejected(self):
        self.assertFalse(canva_renderer._host_allowed("http://localhost:8080/"))

    def test_private_ip_is_rejected(self):
        self.assertFalse(canva_renderer._host_allowed("http://169.254.169.254/latest/meta-data/"))
        self.assertFalse(canva_renderer._host_allowed("http://10.0.0.5/"))

    def test_file_scheme_is_rejected(self):
        self.assertFalse(canva_renderer._host_allowed("file:///etc/passwd"))

    def test_arbitrary_external_site_is_rejected(self):
        self.assertFalse(canva_renderer._host_allowed("https://example.com/"))


def _mock_page(final_url="https://www.canva.com/design/x/y/view", goto_side_effect=None, screenshot=b"\x89PNG fake"):
    page = MagicMock()
    page.url = final_url
    if goto_side_effect:
        page.goto.side_effect = goto_side_effect
    page.get_by_text.return_value.click.side_effect = Exception("no cookie banner")
    page.screenshot.return_value = screenshot
    return page


class RenderCanvaPageTests(unittest.TestCase):
    def setUp(self):
        canva_renderer._browser = None
        canva_renderer._playwright_ctx = None

    def tearDown(self):
        canva_renderer._browser = None
        canva_renderer._playwright_ctx = None

    def _patched_browser(self, page):
        context = MagicMock()
        context.new_page.return_value = page
        browser = MagicMock()
        browser.new_context.return_value = context
        return browser, context

    def test_non_canva_url_raises_before_launching_a_browser(self):
        with patch.object(canva_renderer, "_get_browser") as mock_get_browser:
            with self.assertRaises(canva_renderer.RenderError):
                canva_renderer.render_canva_page("https://example.com/x")
        mock_get_browser.assert_not_called()

    def test_successful_render_returns_png_bytes(self):
        page = _mock_page(screenshot=b"\x89PNG real bytes")
        browser, context = self._patched_browser(page)
        with patch.object(canva_renderer, "_get_browser", return_value=browser):
            result = canva_renderer.render_canva_page("https://www.canva.com/design/x/y/view")

        self.assertEqual(result, b"\x89PNG real bytes")
        context.close.assert_called_once()  # resources cleaned up after the request

    def test_navigation_timeout_raises_render_error(self):
        page = _mock_page(goto_side_effect=Exception("Timeout 15000ms exceeded"))
        browser, context = self._patched_browser(page)
        with patch.object(canva_renderer, "_get_browser", return_value=browser):
            with self.assertRaises(canva_renderer.RenderError):
                canva_renderer.render_canva_page("https://www.canva.com/design/x/y/view")
        context.close.assert_called_once()  # still cleaned up even on failure

    def test_redirect_off_canva_host_raises_render_error(self):
        # Simulates a malicious/broken redirect landing somewhere off
        # canva.com after navigation completes without raising on its own.
        page = _mock_page(final_url="http://169.254.169.254/latest/meta-data/")
        browser, context = self._patched_browser(page)
        with patch.object(canva_renderer, "_get_browser", return_value=browser):
            with self.assertRaises(canva_renderer.RenderError):
                canva_renderer.render_canva_page("https://www.canva.com/design/x/y/view")
        context.close.assert_called_once()

    def test_request_route_guard_aborts_non_canva_requests(self):
        page = _mock_page()
        browser, context = self._patched_browser(page)
        with patch.object(canva_renderer, "_get_browser", return_value=browser):
            canva_renderer.render_canva_page("https://www.canva.com/design/x/y/view")

        guard = page.route.call_args.args[1]
        allowed_route = MagicMock(request=MagicMock(url="https://www.canva.com/some-asset.js"))
        blocked_route = MagicMock(request=MagicMock(url="http://10.0.0.5/steal"))
        guard(allowed_route)
        guard(blocked_route)
        allowed_route.continue_.assert_called_once()
        blocked_route.abort.assert_called_once()
        blocked_route.continue_.assert_not_called()


class RenderHandlerTests(unittest.TestCase):
    """HTTP-layer behavior - auth, malformed input, busy/backpressure -
    exercised directly against the handler methods rather than a real
    socket, mocking render_canva_page itself."""

    def _make_handler(self, body: bytes, path="/render"):
        handler = canva_renderer.Handler.__new__(canva_renderer.Handler)
        handler.path = path
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def test_valid_request_returns_png(self):
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
        with patch.object(canva_renderer, "render_canva_page", return_value=b"\x89PNG bytes"):
            handler.do_POST()
        handler.send_response.assert_called_with(200)
        self.assertEqual(handler.wfile.getvalue(), b"\x89PNG bytes")

    def test_render_error_returns_422_with_reason(self):
        handler = self._make_handler(json.dumps({"url": "https://example.com/x"}).encode())
        with patch.object(
            canva_renderer, "render_canva_page", side_effect=canva_renderer.RenderError("not allowed"),
        ):
            handler.do_POST()
        handler.send_response.assert_called_with(422)
        self.assertIn(b"not allowed", handler.wfile.getvalue())

    def test_malformed_body_returns_400(self):
        handler = self._make_handler(b"not json")
        handler.do_POST()
        handler.send_response.assert_called_with(400)

    def test_shared_secret_rejects_missing_auth_header(self):
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
        with patch.object(canva_renderer, "SHARED_SECRET", "topsecret"):
            handler.do_POST()
        handler.send_response.assert_called_with(401)

    def test_shared_secret_accepts_correct_bearer_token(self):
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
        handler.headers["Authorization"] = "Bearer topsecret"
        with patch.object(canva_renderer, "SHARED_SECRET", "topsecret"), \
                patch.object(canva_renderer, "render_canva_page", return_value=b"\x89PNG bytes"):
            handler.do_POST()
        handler.send_response.assert_called_with(200)

    def test_busy_semaphore_returns_503_without_calling_render(self):
        import threading
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
        with patch.object(canva_renderer, "_render_semaphore", threading.Semaphore(0)):
            with patch.object(canva_renderer, "render_canva_page") as mock_render:
                handler.do_POST()
            mock_render.assert_not_called()
            handler.send_response.assert_called_with(503)

    def test_health_endpoint(self):
        handler = self._make_handler(b"", path="/health")
        handler.do_GET()
        handler.send_response.assert_called_with(200)


if __name__ == "__main__":
    unittest.main()
