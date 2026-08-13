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

Run locally:
    pip install -r requirements.txt
    playwright install chromium
    python app.py

See README.md for deployment (a separate Cloud Run service, never the
main app's own container) and required environment variables.
"""

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

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
# Optional defense-in-depth on top of Cloud Run's own IAM-based invoker
# check (see README.md) - never the primary access control, since a
# static secret has to be stored/rotated somewhere, but cheap insurance
# against this service accidentally being deployed with --allow-
# unauthenticated.
SHARED_SECRET = os.environ.get("RENDERER_SHARED_SECRET")

_render_semaphore = threading.Semaphore(MAX_CONCURRENT_RENDERS)
_playwright_lock = threading.Lock()
_playwright_ctx = None
_browser = None


def _host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_HOST_SUFFIXES)


def _is_recognized_canva_url(url: str) -> bool:
    url = url.strip()
    return bool(_CANVA_VIEW_URL_RE.match(url)) or bool(_CANVA_SHORT_LINK_RE.match(url))


def _get_browser():
    """
    One Chromium process, launched lazily on first use and reused for
    every subsequent request (each request gets its own fresh browser
    CONTEXT + page - see render_canva_page - so requests never share
    cookies/storage/state) - never a new browser process per request,
    which would be far slower and heavier for no benefit.
    """
    global _playwright_ctx, _browser
    with _playwright_lock:
        if _browser is None:
            _playwright_ctx = sync_playwright().start()
            _browser = _playwright_ctx.chromium.launch(headless=True)
        return _browser


class RenderError(Exception):
    """A clean, expected failure - a malformed/disallowed URL, a
    navigation timeout, or a page that never rendered real content.
    Always carries a short, non-sensitive reason string, never a raw
    Playwright exception or stack trace."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def render_canva_page(url: str) -> bytes:
    """
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

    browser = _get_browser()
    context = browser.new_context(viewport=VIEWPORT)
    try:
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        page.set_default_timeout(NAV_TIMEOUT_MS)

        def _guard(route):
            if _host_allowed(route.request.url):
                route.continue_()
            else:
                route.abort()

        # Applies to EVERY request this page makes, not just the initial
        # navigation - a redirect or sub-resource load reaching a
        # non-Canva host is aborted at the network layer, regardless of
        # how the page got there (see the module's own SSRF docstring).
        page.route("**/*", _guard)

        try:
            page.goto(url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
        except Exception as e:
            raise RenderError(f"navigation failed or timed out ({e!r})")

        if not _host_allowed(page.url):
            raise RenderError("navigation left the allowed Canva host")

        page.wait_for_timeout(SETTLE_MS)

        # Best-effort only - a cookie-consent banner is common but not
        # guaranteed present, and its own exact wording is Canva's choice,
        # not a documented contract - never treated as a failure if this
        # doesn't find anything within its own short, bounded timeout.
        for label in ("Reject all cookies", "Accept all cookies"):
            try:
                page.get_by_text(label, exact=True).click(timeout=COOKIE_DISMISS_TIMEOUT_MS)
                page.wait_for_timeout(500)
                break
            except Exception:
                continue

        return page.screenshot(type="png")
    finally:
        # Always closed, success or failure - a leaked context/page would
        # otherwise accumulate memory across requests indefinitely.
        context.close()


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
