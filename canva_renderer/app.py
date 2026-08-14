"""
canva_renderer/app.py

Small, isolated HTTP service whose only job is: given a public Canva
"view" URL, render it in a real headless Chromium (a plain HTTP fetch of
this URL returns Canva's own "Unsupported client" shell instead of the
actual design - confirmed directly, see the main app's brochure_link_
resolver.is_canva_view_link docstring) and return a single PNG screenshot
of whatever page that URL lands on.

Deliberately NEVER runs inside the main spreadsheet app's own container/
process - a stuck/misbehaving Canva page or a Chromium OOM here must never
be able to affect the main app's own memory budget or uptime. This is the
ONLY reason this service exists as a separate deployable: everything else
(URL recognition, matching, enrichment rules) stays in the main app.

Deliberately NOT a general-purpose URL renderer - this is the main SSRF
defense. Every request is validated against a small Canva-only hostname
allow-list (_ALLOWED_HOST_SUFFIXES) TWICE: once on the caller-supplied URL
before a browser page is ever created, and again at the network-request
level for every single request a loaded page tries to make (navigation,
redirect, or sub-resource - see _install_host_guard). Rather than trying
to enumerate every private IP range/scheme a malicious or redirected URL
could point at (file://, localhost, 169.254.169.254, custom schemes,
...), this service simply never lets the browser talk to anything that
isn't canva.com/canva.link in the first place - those all fail the same
one allow-list check, by construction, with no separate denylist logic to
keep in sync.

Only ever captures the FIRST page a URL lands on - see render_canva_page's
own "KNOWN LIMITATION" docstring for why a genuinely multi-page brochure's
other pages are not currently reachable.

--- Playwright thread-affinity (real production incident) -----------------

This service used Playwright's SYNC API (playwright.sync_api) under
ThreadingHTTPServer, which hands each incoming request to a NEW OS thread.
Playwright's sync API is built on greenlet, and greenlet's own switching
mechanism is tied to the specific OS thread that first created the
Playwright/Browser/Page objects - calling browser.new_context()/page.
goto() etc. from a DIFFERENT thread than the one that created them raises
"greenlet.error: cannot switch to a different thread", confirmed directly
in production (every /render request after the first, on a fresh request
thread, crashed this way and returned 503).

Fixed by switching to Playwright's ASYNC API (playwright.async_api) driven
by ONE dedicated background thread running its own asyncio event loop for
the lifetime of this process - every Playwright object (Browser, the lazy
launch, every render's own context/page) is created AND used exclusively
from coroutines scheduled onto that SAME loop, via asyncio.
run_coroutine_threadsafe from whichever HTTP request thread is handling a
given call. This preserves genuine concurrency (multiple renders really
can be in flight at once, up to MAX_CONCURRENT_RENDERS, interleaved via
asyncio's own cooperative scheduling) without ever touching a Playwright
object from more than one OS thread - the actual root cause, not merely a
symptom to catch/retry around.

Run locally:
    pip install -r requirements.txt
    playwright install chromium
    python app.py

See README.md for deployment (a separate Cloud Run service, never the
main app's own container) and required environment variables.
"""

import asyncio
import concurrent.futures
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from playwright.async_api import async_playwright

# Mirrors brochure_link_resolver.is_canva_view_link's own narrow shape in
# the main app (never a bare "canva.com" substring) - kept as an
# independent copy here deliberately: this is a separately deployed
# service with its own repo/container boundary, not a shared import, so
# the two are only ever coupled by Canva's own URL shape, never by an
# unrelated change on either side.
_CANVA_VIEW_URL_RE = re.compile(
    r"^https?://(?:[\w-]+\.)*canva\.com/design/[^/\s]+/[^/\s]+/view(?:[/?#].*)?$", re.IGNORECASE
)
_CANVA_SHORT_LINK_RE = re.compile(r"^https?://canva\.link/[^/\s?#]+(?:[/?#].*)?$", re.IGNORECASE)

# The ONLY hostnames this service's browser is ever allowed to talk to -
# see the module's own SSRF docstring above. Exact-or-subdomain match
# only, same principle as the main app's own is_generic_link
# KNOWN_NON_BROCHURE_DOMAINS check - "canva.com.evil.com" does NOT match
# ".canva.com".
_ALLOWED_HOST_SUFFIXES = ("canva.com", "canva.link")

NAV_TIMEOUT_MS = 15_000
SETTLE_MS = 3_000
COOKIE_DISMISS_TIMEOUT_MS = 2_000
VIEWPORT = {"width": 1200, "height": 1600}
MAX_CONCURRENT_RENDERS = int(os.environ.get("MAX_CONCURRENT_RENDERS", "2"))
# Overall budget for one render's whole async round trip (navigation +
# settle + cookie-dismiss + screenshot), enforced from the OUTSIDE via
# concurrent.futures' own timeout - comfortably above NAV_TIMEOUT_MS+
# SETTLE_MS so a render that's genuinely progressing is never cut off
# before its own internal timeouts would have ended it anyway; this is
# the backstop for a hang neither of those internal timeouts catches.
RENDER_TIMEOUT_SECONDS = (NAV_TIMEOUT_MS + SETTLE_MS) / 1000 + 10
# Optional defense-in-depth on top of Cloud Run's own IAM-based invoker
# check (see README.md) - never the primary access control, since a
# static secret has to be stored/rotated somewhere, but cheap insurance
# against this service accidentally being deployed with --allow-
# unauthenticated.
SHARED_SECRET = os.environ.get("RENDERER_SHARED_SECRET")

_render_semaphore = threading.Semaphore(MAX_CONCURRENT_RENDERS)

# The single dedicated thread + event loop that owns EVERY Playwright
# object for this process' whole lifetime - see the module's own "Playwright
# thread-affinity" docstring above for why this exists at all. _loop_ready
# gates _ensure_loop() so concurrent first-callers block until the loop
# thread has genuinely started (never a race constructing it twice).
_loop = None
_loop_ready = threading.Event()
_loop_start_lock = threading.Lock()

_playwright_ctx = None  # set once, on the loop thread only
_browser = None  # set once, on the loop thread only
_browser_init_lock = None  # an asyncio.Lock, created lazily ON the loop thread


def _host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_HOST_SUFFIXES)


def _is_recognized_canva_url(url: str) -> bool:
    url = url.strip()
    return bool(_CANVA_VIEW_URL_RE.match(url)) or bool(_CANVA_SHORT_LINK_RE.match(url))


def _run_loop_forever():
    global _loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop = loop
    _loop_ready.set()
    loop.run_forever()


def _ensure_loop():
    """
    Starts the single dedicated Playwright event-loop thread on first use
    (idempotent, thread-safe across however many HTTP request threads race
    to call this first) and returns its loop. Every Playwright-touching
    coroutine in this module is scheduled onto THIS loop via asyncio.
    run_coroutine_threadsafe, never run directly on a request thread -
    that's the actual fix (see this module's own top-level docstring).
    """
    if not _loop_ready.is_set():
        with _loop_start_lock:
            if not _loop_ready.is_set():
                threading.Thread(target=_run_loop_forever, name="canva-playwright-loop", daemon=True).start()
                _loop_ready.wait()
    return _loop


async def _get_browser_async():
    """
    One Chromium process, launched lazily on first use and reused for
    every subsequent request (each request gets its own fresh browser
    CONTEXT + page - see render_canva_page_async - so requests never share
    cookies/storage/state) - never a new browser process per request,
    which would be far slower and heavier for no benefit. Only ever
    awaited from a coroutine already running on the single dedicated
    Playwright loop (see _ensure_loop) - an asyncio.Lock (not a threading
    one) guards the lazy-init check itself, since two render coroutines on
    the SAME loop could otherwise both see _browser as None and each start
    their own browser if either awaited between the check and the launch.
    """
    global _playwright_ctx, _browser, _browser_init_lock
    if _browser_init_lock is None:
        _browser_init_lock = asyncio.Lock()
    async with _browser_init_lock:
        if _browser is None:
            _playwright_ctx = await async_playwright().start()
            _browser = await _playwright_ctx.chromium.launch(headless=True)
        return _browser


class RenderError(Exception):
    """A clean, expected failure - a malformed/disallowed URL, a
    navigation timeout, or a page that never rendered real content.
    Always carries a short, non-sensitive reason string, never a raw
    Playwright exception or stack trace."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def render_canva_page_async(url: str) -> bytes:
    """
    The real render logic - see render_canva_page (this module's own sync-
    facing entry point HTTP requests actually call) for how this gets
    scheduled onto the single dedicated Playwright loop thread from an
    arbitrary request thread.

    Returns PNG bytes for the first page `url` lands on, or raises
    RenderError(reason) on any safe-failure condition - a caller only
    ever needs to catch this one exception type.

    KNOWN LIMITATION: only ever captures ONE page. A real public Canva
    "view" link renders as a single-page-at-a-time viewer (confirmed
    directly against a real example - the loaded page's own
    document.body.scrollHeight equals the viewport height, not a tall,
    continuously-scrollable list of every page stacked vertically), so a
    genuinely multi-page brochure's remaining pages are not reachable
    without interacting with Canva's own pagination controls - private,
    unversioned frontend implementation detail with no stable, documented
    contract to hook into (no public API, no stable "page N of M" URL
    scheme). Deliberately not attempted here rather than building brittle
    DOM-selector-dependent page-clicking logic that could silently break
    on any future Canva frontend change.
    """
    if not _is_recognized_canva_url(url):
        raise RenderError("not a recognized public Canva URL")
    if not _host_allowed(url):
        raise RenderError("host not allowed")

    browser = await _get_browser_async()
    context = await browser.new_context(viewport=VIEWPORT)
    try:
        page = await context.new_page()
        # Plain (non-async) setters even on the async API - never awaited.
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        page.set_default_timeout(NAV_TIMEOUT_MS)

        async def _guard(route):
            if _host_allowed(route.request.url):
                await route.continue_()
            else:
                await route.abort()

        # Applies to EVERY request this page makes, not just the initial
        # navigation - a redirect or sub-resource load reaching a
        # non-Canva host is aborted at the network layer, regardless of
        # how the page got there (see the module's own SSRF docstring).
        await page.route("**/*", _guard)

        try:
            await page.goto(url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
        except Exception as e:
            raise RenderError(f"navigation failed or timed out ({e!r})")

        if not _host_allowed(page.url):
            raise RenderError("navigation left the allowed Canva host")

        await page.wait_for_timeout(SETTLE_MS)

        # Best-effort only - a cookie-consent banner is common but not
        # guaranteed present, and its own exact wording is Canva's choice,
        # not a documented contract - never treated as a failure if this
        # doesn't find anything within its own short, bounded timeout.
        for label in ("Reject all cookies", "Accept all cookies"):
            try:
                await page.get_by_text(label, exact=True).click(timeout=COOKIE_DISMISS_TIMEOUT_MS)
                await page.wait_for_timeout(500)
                break
            except Exception:
                continue

        return await page.screenshot(type="png")
    finally:
        # Always closed, success or failure - a leaked context/page would
        # otherwise accumulate memory across requests indefinitely.
        await context.close()


def render_canva_page(url: str) -> bytes:
    """
    Sync-facing entry point - what the HTTP handler actually calls, on
    whichever request thread is handling this call. Schedules render_
    canva_page_async(url) onto the single dedicated Playwright loop thread
    (see _ensure_loop) via asyncio.run_coroutine_threadsafe, then blocks
    THIS thread (not the loop thread) waiting for the result - the
    standard, safe way to bridge a sync caller to work that must run on a
    specific already-running event loop. A RenderError raised inside the
    coroutine propagates through .result() unchanged; a render that never
    finishes within RENDER_TIMEOUT_SECONDS is converted into one too,
    rather than leaking a bare concurrent.futures.TimeoutError to the
    HTTP handler's own RenderError-only except clause.
    """
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(render_canva_page_async(url), loop)
    try:
        return future.result(timeout=RENDER_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise RenderError("render timed out")


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not SHARED_SECRET:
            return True  # Cloud Run's own IAM invoker check is the primary gate - see README.md
        return self.headers.get("Authorization") == f"Bearer {SHARED_SECRET}"

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/render":
            self._send_json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            url = payload["url"]
        except Exception:
            self._send_json(400, {"error": "invalid request body"})
            return

        if not _render_semaphore.acquire(blocking=False):
            # A hard concurrency cap, not a queue - one broken/slow Canva
            # page must never be able to pile up unbounded work here; the
            # caller (see brochure_enrichment._fetch_canva_rendered_page)
            # already treats any non-2xx response as a normal, safe
            # per-document failure.
            self._send_json(503, {"error": "renderer busy, try again"})
            return
        try:
            try:
                png_bytes = render_canva_page(url)
            except RenderError as e:
                self._send_json(422, {"error": "render_failed", "reason": e.reason})
                return
            except Exception as e:
                self._send_json(500, {"error": "internal_error", "reason": repr(e)})
                return

            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png_bytes)))
            self.end_headers()
            self.wfile.write(png_bytes)
        finally:
            _render_semaphore.release()

    def log_message(self, format, *args):
        # Cloud Run's own request logging already records method/path/
        # status for every call - this stays quiet by default so a
        # request's own URL (never sensitive, but no need to duplicate)
        # isn't logged twice.
        pass


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[canva_renderer] listening on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
