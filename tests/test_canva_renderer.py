"""
Regression tests for canva_renderer/app.py - the separate, isolated
Playwright/Chromium service that renders a public Canva "view" link (see
that module's own docstring for why this is a SEPARATE service, never run
inside the main app's own container, and for the real production
"greenlet.error: cannot switch to a different thread" incident this
module's async-API-plus-one-dedicated-event-loop-thread architecture
fixes). Playwright's own browser is mocked throughout - these tests never
launch a real browser, matching this repo's existing convention (test_
brochure_enrichment.py never calls the real network/Gemini API either).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_canva_renderer -v
"""

import concurrent.futures
import importlib.util
import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Loaded via an explicit file path under a unique module name, never a
# bare "import app" - the main repo's OWN top-level app.py (the Streamlit
# entrypoint, imported as a bare "app" module by several other test files,
# e.g. test_app.py) would otherwise collide in sys.modules with this
# renderer's own, unrelated app.py: whichever one is imported-by-name
# FIRST in a given test process wins that cache slot, silently handing
# every later "import app" the WRONG module. Loading by path sidesteps
# that entirely - this always loads canva_renderer/app.py specifically,
# regardless of what else has already been imported in this process.
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


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def _make_async_page(final_url="https://www.canva.com/design/x/y/view", goto_side_effect=None, screenshot=b"\x89PNG fake"):
    """A MagicMock shaped like an async Playwright Page - every method
    Playwright's own async API defines as a coroutine is an AsyncMock;
    the two plain (non-async) setters stay ordinary MagicMock attributes,
    matching the real API exactly (see canva_renderer.app's own comment on
    this - confirmed directly against the installed playwright package)."""
    page = MagicMock()
    page.url = final_url
    page.set_default_navigation_timeout = MagicMock()
    page.set_default_timeout = MagicMock()
    page.route = AsyncMock()
    page.goto = AsyncMock(side_effect=goto_side_effect) if goto_side_effect else AsyncMock()
    locator = MagicMock()
    locator.click = AsyncMock(side_effect=Exception("no cookie banner"))
    page.get_by_text = MagicMock(return_value=locator)
    page.wait_for_timeout = AsyncMock()
    page.screenshot = AsyncMock(return_value=screenshot)
    return page


def _make_async_context(page):
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    return context


def _make_async_browser(context):
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    return browser


class _ResetGlobalBrowserStateTestCase(unittest.TestCase):
    """The lazily-initialized browser/playwright-context globals are
    process-lifetime singletons by design (see _get_browser_async's own
    docstring) - reset between tests so each one gets its own mocked
    browser rather than silently reusing a previous test's. The dedicated
    event-loop thread itself is NOT reset - exactly like production, it
    starts once and is reused for the rest of the process."""

    def setUp(self):
        canva_renderer._browser = None
        canva_renderer._playwright_ctx = None
        canva_renderer._browser_init_lock = None

    def tearDown(self):
        canva_renderer._browser = None
        canva_renderer._playwright_ctx = None
        canva_renderer._browser_init_lock = None


class RenderCanvaPageAsyncTests(_ResetGlobalBrowserStateTestCase):
    """Unit-level tests against render_canva_page_async directly (run via
    asyncio.run in this test process' own thread) - the render LOGIC
    itself, independent of the sync-to-async threading bridge (see
    RenderCanvaPageThreadBridgeTests for that)."""

    def _patch_browser(self, page):
        context = _make_async_context(page)
        browser = _make_async_browser(context)
        return patch.object(canva_renderer, "_get_browser_async", AsyncMock(return_value=browser)), context

    def test_non_canva_url_raises_before_touching_the_browser(self):
        mock_get_browser = AsyncMock()
        with patch.object(canva_renderer, "_get_browser_async", mock_get_browser):
            with self.assertRaises(canva_renderer.RenderError):
                _run(canva_renderer.render_canva_page_async("https://example.com/x"))
        mock_get_browser.assert_not_called()

    def test_successful_render_returns_png_bytes(self):
        page = _make_async_page(screenshot=b"\x89PNG real bytes")
        patcher, context = self._patch_browser(page)
        with patcher:
            result = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(result, b"\x89PNG real bytes")
        context.close.assert_awaited_once()  # resources cleaned up after the request

    def test_navigation_timeout_raises_render_error_and_still_cleans_up(self):
        page = _make_async_page(goto_side_effect=Exception("Timeout 15000ms exceeded"))
        patcher, context = self._patch_browser(page)
        with patcher:
            with self.assertRaises(canva_renderer.RenderError):
                _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))
        context.close.assert_awaited_once()

    def test_redirect_off_canva_host_raises_render_error(self):
        page = _make_async_page(final_url="http://169.254.169.254/latest/meta-data/")
        patcher, context = self._patch_browser(page)
        with patcher:
            with self.assertRaises(canva_renderer.RenderError):
                _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))
        context.close.assert_awaited_once()

    def test_request_route_guard_aborts_non_canva_requests(self):
        page = _make_async_page()
        patcher, context = self._patch_browser(page)
        with patcher:
            _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        guard = page.route.call_args.args[1]
        allowed_route = MagicMock(request=MagicMock(url="https://www.canva.com/some-asset.js"))
        allowed_route.continue_ = AsyncMock()
        allowed_route.abort = AsyncMock()
        blocked_route = MagicMock(request=MagicMock(url="http://10.0.0.5/steal"))
        blocked_route.continue_ = AsyncMock()
        blocked_route.abort = AsyncMock()
        _run(guard(allowed_route))
        _run(guard(blocked_route))
        allowed_route.continue_.assert_awaited_once()
        blocked_route.abort.assert_awaited_once()
        blocked_route.continue_.assert_not_called()

    def test_browser_is_only_ever_launched_once_across_renders(self):
        page = _make_async_page()
        context = _make_async_context(page)
        browser = _make_async_browser(context)
        starter = MagicMock()
        starter.start = AsyncMock(return_value=MagicMock(chromium=MagicMock(launch=AsyncMock(return_value=browser))))

        with patch.object(canva_renderer, "async_playwright", MagicMock(return_value=starter)):
            _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))
            _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        starter.start.assert_awaited_once()  # never launched a second browser


class RenderCanvaPageThreadBridgeTests(_ResetGlobalBrowserStateTestCase):
    """
    Exercises the REAL sync-to-async bridge (render_canva_page -> the
    single dedicated Playwright event-loop thread, via asyncio.
    run_coroutine_threadsafe) - the actual fix for the real production
    "greenlet.error: cannot switch to a different thread" crash. Still
    mocks the browser/page tree itself (no real Chromium needed), but the
    THREADING/event-loop machinery here is 100% real, run from this test's
    own thread exactly like a real HTTP request thread would.
    """

    def _patch_browser(self, page):
        context = _make_async_context(page)
        browser = _make_async_browser(context)
        return patch.object(canva_renderer, "_get_browser_async", AsyncMock(return_value=browser))

    def test_single_render_succeeds_through_the_real_thread_bridge(self):
        page = _make_async_page(screenshot=b"\x89PNG single")
        with self._patch_browser(page):
            result = canva_renderer.render_canva_page("https://www.canva.com/design/x/y/view")
        self.assertEqual(result, b"\x89PNG single")

    def test_sequential_renders_all_succeed(self):
        page = _make_async_page(screenshot=b"\x89PNG seq")
        with self._patch_browser(page):
            results = [
                canva_renderer.render_canva_page("https://www.canva.com/design/x/y/view") for _ in range(5)
            ]
        self.assertEqual(results, [b"\x89PNG seq"] * 5)

    def test_concurrent_renders_from_multiple_threads_never_hit_a_cross_thread_playwright_error(self):
        # The exact real production shape: several /render requests
        # arriving on DIFFERENT OS threads (ThreadingHTTPServer hands each
        # connection its own thread) at the same time. Before the fix,
        # this - or even a single request on any thread OTHER than the one
        # that happened to launch the browser - raised "greenlet.error:
        # cannot switch to a different thread". Every Playwright object
        # here is only ever touched from the one dedicated loop thread
        # (see _ensure_loop/render_canva_page's own docstrings), so this
        # must succeed regardless of which OS thread called render_canva_
        # page - genuinely exercising cross-thread calls via a real
        # ThreadPoolExecutor, not just sequential calls from one thread.
        page = _make_async_page(screenshot=b"\x89PNG concurrent")
        with self._patch_browser(page):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futures = [
                    pool.submit(canva_renderer.render_canva_page, "https://www.canva.com/design/x/y/view")
                    for _ in range(8)
                ]
                results = [f.result(timeout=10) for f in futures]

        self.assertEqual(results, [b"\x89PNG concurrent"] * 8)

    def test_context_is_cleaned_up_even_under_concurrent_load(self):
        page = _make_async_page()
        context = _make_async_context(page)
        browser = _make_async_browser(context)
        with patch.object(canva_renderer, "_get_browser_async", AsyncMock(return_value=browser)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(canva_renderer.render_canva_page, "https://www.canva.com/design/x/y/view")
                    for _ in range(4)
                ]
                [f.result(timeout=10) for f in futures]

        self.assertEqual(context.close.await_count, 4)  # once per request, none leaked

    def test_a_hanging_render_times_out_as_a_safe_render_error(self):
        page = _make_async_page()

        async def _hang(*a, **kw):
            import asyncio as _asyncio
            await _asyncio.sleep(3600)

        page.goto = AsyncMock(side_effect=_hang)
        with self._patch_browser(page), \
                patch.object(canva_renderer, "RENDER_TIMEOUT_SECONDS", 0.2):
            with self.assertRaises(canva_renderer.RenderError):
                canva_renderer.render_canva_page("https://www.canva.com/design/x/y/view")


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
