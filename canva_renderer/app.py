"""
canva_renderer/app.py

Small, isolated HTTP service whose only job is: given a public Canva
"view" URL OR a public Pitch.com "view" URL, render it in a real headless
Chromium (a plain HTTP fetch of either URL shape returns only an empty/
"Unsupported client" shell instead of the actual content - confirmed
directly, see the main app's brochure_link_resolver.is_canva_view_link/
is_pitch_view_link docstrings) and return one PNG screenshot per page.
Pitch support was added second, reusing this exact same architecture
(browser lifecycle, SSRF allow-list, pagination-capture loop shape) - see
render_pitch_page_async's own docstring for the two genuine, confirmed
differences from Canva's own render_canva_page_async.

Deliberately NEVER runs inside the main spreadsheet app's own container/
process - a stuck/misbehaving page or a Chromium OOM here must never be
able to affect the main app's own memory budget or uptime. This is the
ONLY reason this service exists as a separate deployable: everything else
(URL recognition, matching, enrichment rules) stays in the main app.

Deliberately NOT a general-purpose URL renderer - this is the main SSRF
defense. Every request is validated against a small hostname allow-list
(_ALLOWED_HOST_SUFFIXES: canva.com, canva.link, pitch.com) TWICE: once on
the caller-supplied URL before a browser page is ever created, and again
at the network-request level for every single request a loaded page
tries to make (navigation, redirect, or sub-resource - see
_install_host_guard). Rather than trying to enumerate every private IP
range/scheme a malicious or redirected URL could point at (file://,
localhost, 169.254.169.254, custom schemes, ...), this service simply
never lets the browser talk to anything outside that allow-list in the
first place - those all fail the same one allow-list check, by
construction, with no separate denylist logic to keep in sync.

Captures EVERY page of a multi-page brochure/deck, up to MAX_CANVA_PAGES/
MAX_PITCH_PAGES - see render_canva_page_async's/render_pitch_page_async's
own docstrings for how (each platform's own accessible Next-page control
and its own disabled-state signal, confirmed directly against a real
multi-page example - never a CSS-class-dependent scrape), with a bounded
retry against a freshly re-acquired button locator on a transient click
failure (see MAX_NEXT_CLICK_ATTEMPTS) before giving up on pagination and
keeping whatever was already captured.

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
import base64
import concurrent.futures
import io
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from PIL import Image

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

# Mirrors brochure_link_resolver.is_pitch_view_link's own narrow shape in
# the main app - a Pitch.com public "view" link (e.g. "https://pitch.com/
# v/1-finsbury-brochure-4jnj9d"), confirmed directly against real GPE/
# Knotel/MetSpace brochure links via a throwaway Playwright recon script:
# a plain HTTP GET returns only an empty client-side-rendered shell (same
# category of problem as Canva's own "Unsupported client" page - see
# _CANVA_VIEW_URL_RE's own docstring), but real content DOES render
# correctly in a real headless browser, with no login/email gate. Kept as
# an independent copy here for the same reason _CANVA_VIEW_URL_RE is: this
# is a separately deployed service with its own repo/container boundary,
# not a shared import.
_PITCH_VIEW_URL_RE = re.compile(r"^https?://(?:[\w-]+\.)*pitch\.com/v/[^/\s?#]+(?:[/?#].*)?$", re.IGNORECASE)

# The ONLY hostnames this service's browser is ever allowed to talk to -
# see the module's own SSRF docstring above. Exact-or-subdomain match
# only, same principle as the main app's own is_generic_link
# KNOWN_NON_BROCHURE_DOMAINS check - "canva.com.evil.com" does NOT match
# ".canva.com". pitch.com added alongside canva.com/canva.link - same
# service, same allow-list mechanism, one more recognized platform.
_ALLOWED_HOST_SUFFIXES = ("canva.com", "canva.link", "pitch.com")

# Real production evidence this covers: `Page.goto: Timeout 15000ms
# exceeded` on a genuine public Canva "view" link that DOES eventually
# render - 15s was too tight for Canva's own initial bundle/design load
# on a cold request, not a sign the page was actually broken. Applies
# ONLY to the initial navigation (see page.goto below) - kept
# deliberately separate from NEXT_PAGE_CLICK_TIMEOUT_MS, since a page
# transition on an already-loaded, warm design has no reason to need
# anywhere near this long (see that constant's own docstring).
NAV_TIMEOUT_MS = 30_000
SETTLE_MS = 3_000
COOKIE_DISMISS_TIMEOUT_MS = 2_000
VIEWPORT = {"width": 1200, "height": 1600}
MAX_CONCURRENT_RENDERS = int(os.environ.get("MAX_CONCURRENT_RENDERS", "2"))

# Hard cap on how many pages of ONE Canva design this service will ever
# capture, regardless of how many the design actually has - a malformed or
# deliberately huge public design must never be able to make one /render
# call consume unbounded time/memory. Raised from 20 to 30: a real
# production brochure (Risborough) had its contact info on page 29 of 29,
# lost outright by the old cap - confirmed as a real truncation, not an
# extraction failure. Must be kept in lockstep with the main app's own
# independent _CANVA_MAX_PAGES_ACCEPTED (brochure_enrichment.py) - that
# defense-in-depth cap truncates whatever this service returns, so raising
# this one alone would silently keep losing page 29.
MAX_CANVA_PAGES = int(os.environ.get("MAX_CANVA_PAGES", "30"))
# Same cap concept, for a Pitch presentation - see render_pitch_page_async.
# No production evidence yet of a Pitch deck anywhere near this size (the
# real ones checked during recon were 13-14 pages), but the SAME "a
# malformed/huge public deck must never make one /render call consume
# unbounded time/memory" reasoning applies regardless of platform, so this
# gets its own bounded cap rather than an implicit assumption. Same default
# as MAX_CANVA_PAGES - no evidence yet a different value is warranted.
MAX_PITCH_PAGES = int(os.environ.get("MAX_PITCH_PAGES", "30"))
# Settle time after each Next-page click, before that page's own
# screenshot - shorter than the initial-load SETTLE_MS above since the
# Canva app/design is already warm; still enough for that page's own
# images to paint (confirmed directly against a real 7-page brochure).
PAGE_NAV_SETTLE_MS = 1_200
# Deliberately SEPARATE from NAV_TIMEOUT_MS (see that constant's own
# docstring on why it was raised to 30s) - a "Next page" click advances
# an ALREADY-loaded, warm design in place (confirmed: no new navigation/
# network round trip, just a DOM/canvas transition - see render_canva_
# page_async's own docstring), so it has no reason to need anywhere near
# as long as the initial cold navigation does. Kept at the ORIGINAL
# NAV_TIMEOUT_MS value (15s) on purpose: raising this too would make
# every one of the bounded MAX_NEXT_CLICK_ATTEMPTS retries take
# proportionally longer, working against the whole point of a FAST,
# bounded retry - real production evidence this covers: `Locator.click:
# Timeout 15000ms exceeded` on the "Next page" button after already
# capturing several pages successfully.
NEXT_PAGE_CLICK_TIMEOUT_MS = 15_000
# One real click failure can be transient (an in-flight CSS transition, a
# frame Canva's own JS hadn't finished attaching a handler to yet) -
# confirmed production symptom this guards against: a "Next page" click
# whose OWN actionability checks reported the element visible, enabled,
# and stable, yet the click itself still timed out. A bounded retry, each
# with a FRESHLY re-acquired button locator (never the same handle reused
# across attempts - Canva's own SPA can re-render, and therefore detach,
# this exact button between attempts), rides through that without
# turning a single hiccup into a lost page; still bounded, so a genuinely
# broken "Next page" control gives up exactly like before this constant
# existed, never an infinite retry loop.
MAX_NEXT_CLICK_ATTEMPTS = 3
# Timeout for the "Go to page" indicator read used to VERIFY a click
# actually advanced the page (see _page_advance_signature) - deliberately
# much shorter than _detect_page_count's own one-time-per-render 2000ms
# budget for the SAME read, since this one runs up to twice per attempt,
# up to MAX_NEXT_CLICK_ATTEMPTS times per page transition: a design with
# no such indicator at all would otherwise pay the full timeout on every
# single check, silently slowing down the common (indicator-less) case
# rather than just the one genuinely-optional diagnostic read this was
# originally sized for. Short enough to stay cheap when absent, still
# comfortably enough time to read a real, already-rendered element when
# present.
_PAGE_ADVANCE_INDICATOR_TIMEOUT_MS = 500
# Timeout for the OUTER "does a 'Next page' control exist at all" check at
# the top of each pagination loop iteration (see render_canva_page_async's
# own while loop) - deliberately explicit and short, NEVER left to
# implicitly inherit page.set_default_timeout's own NAV_TIMEOUT_MS (30s).
# Real production evidence this fixes: a genuinely single-page design (or
# a dead/expired Canva link - see the navigation-status check above) has
# no such control at all, so Playwright's own auto-wait polls for up to
# whatever timeout is in effect hoping it appears - silently costing a
# full 30 EXTRA wasted seconds, holding this request's own Chromium
# context (and therefore memory) open the whole time, on every single-
# page render before this constant existed. Short enough to stay cheap
# on the (common) no-button case, still comfortably enough time to read a
# real, already-rendered element when one genuinely exists.
_NEXT_BUTTON_PRESENCE_TIMEOUT_MS = 3_000
# Per-page-transition timeout budget used only to size RENDER_TIMEOUT_
# SECONDS below - the click itself now uses its own explicit
# NEXT_PAGE_CLICK_TIMEOUT_MS (see that constant's own docstring), not
# NAV_TIMEOUT_MS; this is a generous per-page allowance (click + settle
# + screenshot + margin for an occasional retry - see MAX_NEXT_CLICK_
# ATTEMPTS) for sizing the OUTER backstop only. Deliberately NOT sized to
# every page hitting its own worst-case retry simultaneously (that would
# make this backstop enormous, the exact "just increase the timeout to
# something huge" anti-pattern this fix avoids) - a retry is the
# exception, not the per-page norm.
_PER_PAGE_TIMEOUT_BUDGET_S = 8
# Overall budget for one render's whole async round trip (navigation +
# settle + cookie-dismiss + screenshot + up to max_pages-1 further page
# transitions), enforced from the OUTSIDE via concurrent.futures' own
# timeout - comfortably above every internal timeout combined so a render
# that's genuinely progressing is never cut off before its own internal
# timeouts would have ended it anyway; this is the backstop for a hang none
# of those internal timeouts catches. Factored out so both platforms'
# render_*_page sync wrappers size their own future.result(timeout=...)
# from the SAME formula, just against their own respective page cap -
# never two independently-drifting copies of this arithmetic.
def _render_timeout_seconds(max_pages: int) -> float:
    return (NAV_TIMEOUT_MS + SETTLE_MS) / 1000 + 10 + (max_pages - 1) * _PER_PAGE_TIMEOUT_BUDGET_S


RENDER_TIMEOUT_SECONDS = _render_timeout_seconds(MAX_CANVA_PAGES)
PITCH_RENDER_TIMEOUT_SECONDS = _render_timeout_seconds(MAX_PITCH_PAGES)

# Cloud Run's own hard ceiling on a single HTTP response body - confirmed
# directly via real production evidence: Cloud Run's own platform-level
# "Response size was too large. Please consider reducing response size."
# WARNING (never this app's own logging), immediately followed by an HTTP
# 500 the main app received - the FIRST time a 29-page capture (Risborough,
# right after MAX_CANVA_PAGES was raised from 20 to 30) ever actually
# produced a response this large. Not a Cloud Run *setting* - --memory/
# --timeout/--concurrency have no effect on this at all; the only lever on
# this side of the boundary is the response's own byte size. See do_POST's
# own adaptive JPEG re-encoding below, which exists specifically to stay
# under this.
CLOUD_RUN_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
# Re-encode every page as JPEG (see JPEG_QUALITY) instead of sending the
# lossless PNG Chromium actually produced, but ONLY when the estimated
# base64+JSON payload would come within this margin of CLOUD_RUN_MAX_
# RESPONSE_BYTES - most renders (the large majority: small/medium decks)
# never get anywhere near this and keep full lossless PNG quality
# completely unchanged; only a genuinely large and/or photo-dense capture
# pays JPEG's lossy-compression cost, and only when actually needed to
# avoid a hard platform failure. Deliberately NOT set right up against
# CLOUD_RUN_MAX_RESPONSE_BYTES itself - the estimate below (raw-bytes-
# times-4/3) approximates base64's own exact blow-up but ignores the
# JSON envelope's own small fixed overhead, so this margin covers that
# approximation being imprecise, not just the platform limit itself.
RESPONSE_SIZE_SAFETY_THRESHOLD_BYTES = 24 * 1024 * 1024
# Confirmed directly against a real production capture (Risborough, 29
# pages): 25.68MB raw PNG total -> 5.25MB JPEG total at this exact
# quality - a ~4.9x reduction with no visible quality loss for a real
# photo-heavy brochure page, since PNG (lossless) is a poor fit for
# photographic content in the first place. Not tuned further than this
# one confirmed data point - revisit with real evidence if a future
# capture's own text/floor-plan legibility is ever actually reported as
# degraded, never preemptively.
JPEG_QUALITY = 85

# How long ONE request will wait for a free render slot (MAX_CONCURRENT_
# RENDERS already in flight) before giving up - confirmed real production
# root cause of "some Canva properties render fine, others get an
# immediate 503 for no visible reason": the semaphore acquire used to be
# non-blocking (blocking=False), so the (MAX_CONCURRENT_RENDERS + 1)th
# concurrent /render call was rejected INSTANTLY, even though a slot was
# often about to free up seconds later. A bulk spreadsheet upload with
# many Canva-linked rows genuinely dispatches several brochures
# concurrently (see brochure_enrichment.enrich_rows_grouped's own
# DEFAULT_MAX_WORKERS=5) - with MAX_CONCURRENT_RENDERS=2 it only takes 3
# simultaneous Canva requests for the rest to be dropped outright,
# independent of whether that specific design would have rendered fine
# (confirmed directly: a real failing production URL rendered perfectly
# in isolation, with zero contention). This is deliberately a WAIT budget
# ONLY, kept entirely separate from RENDER_TIMEOUT_SECONDS above - the
# render clock (see render_canva_page) only starts counting once a slot is
# actually acquired (see do_POST), so a request queued behind a burst is
# never penalized for time spent merely waiting for capacity.
SEMAPHORE_WAIT_TIMEOUT_SECONDS = int(os.environ.get("SEMAPHORE_WAIT_TIMEOUT_SECONDS", "90"))

# Best-effort parse of Canva's own "current / total" page-count text (e.g.
# "1 / 7") from the accessible "Go to page" button's own inner text -
# purely a diagnostic ("Canva design detected: N pages" logging), never the
# mechanism this service relies on to know when to stop (that's the Next-
# page button's own aria-disabled state - see render_canva_page_async).
# Collapses internal whitespace/newlines first since Canva renders this as
# several separate text nodes ("1", "/", "7"), not one plain string.
_PAGE_COUNT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
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


def _is_recognized_pitch_url(url: str) -> bool:
    return bool(_PITCH_VIEW_URL_RE.match(url.strip()))


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

    Also relaunches if the cached `_browser` has DISCONNECTED (a real crash
    or an OOM kill under load, distinct from a genuinely slow/hanging
    render - see RENDER_TIMEOUT_SECONDS/SEMAPHORE_WAIT_TIMEOUT_SECONDS for
    that) - `Browser.is_connected()` is a cheap, synchronous, purely local
    state check (no I/O), so this costs nothing on the far more common
    still-healthy path. Without this, a single Chromium crash would leave
    every SUBSEQUENT request in this process' whole lifetime trying to use
    the same dead Browser object forever (every render failing the same
    way until Cloud Run eventually recycles the container) - this recovers
    within the very next request instead.
    """
    global _playwright_ctx, _browser, _browser_init_lock
    if _browser_init_lock is None:
        _browser_init_lock = asyncio.Lock()
    async with _browser_init_lock:
        if _browser is not None and not _browser.is_connected():
            print(
                "[canva_renderer] Cached browser was disconnected (crashed or OOM-killed) - relaunching.",
                file=sys.stderr,
            )
            try:
                await _playwright_ctx.stop()
            except Exception:
                pass  # already gone - nothing more to clean up
            _browser = None
        if _browser is None:
            _playwright_ctx = await async_playwright().start()
            _browser = await _playwright_ctx.chromium.launch(headless=True)
        return _browser


# Generous enough to keep a genuinely useful failure description (e.g.
# "navigation failed or timed out (TimeoutError('Timeout 15000ms exceeded
# ... Call log: ...'))") readable, short enough that this service's own
# 422/500 JSON body can never balloon into something resembling a full
# stack trace dump - see _safe_reason below, this service's own diagnostic-
# propagation contract with the main app (never tokens/credentials/env
# vars/stack traces/arbitrary HTML, always capped).
_MAX_REASON_LENGTH = 200


def _safe_reason(raw) -> str:
    """
    `raw` (a RenderError's own reason string, or repr(exc) for a bare
    uncaught exception - see Handler.do_POST's 500 branch) collapsed to a
    single line (Playwright's own TimeoutError repr routinely embeds a
    multi-line "Call log:" dump - never returned to a caller as-is) and
    capped to _MAX_REASON_LENGTH, so the main app's own diagnostic log line
    (see brochure_enrichment._fetch_canva_rendered_page) always gets
    something short and safe to print, never a raw stack trace or an
    unbounded string. Also redacts this service's own SHARED_SECRET if it
    were ever somehow present (defense-in-depth only - a genuine Playwright/
    Chromium navigation or launch failure never actually references this
    service's own env vars, but this costs nothing to guard against).

    This is NOT a claim that the input was already safe - repr(exc) for an
    arbitrary caught exception is NOT guaranteed to exclude request URLs or
    other caller-supplied text embedded in the exception's own message by
    whatever library raised it; this only bounds LENGTH and collapses
    newlines, the two properties a caller (the main app's log line) can't
    otherwise protect itself against. It does not attempt to redact
    arbitrary secret-shaped substrings it has no way to recognize.
    """
    text = " ".join(str(raw).split())
    if SHARED_SECRET and SHARED_SECRET in text:
        text = text.replace(SHARED_SECRET, "[redacted]")
    if len(text) > _MAX_REASON_LENGTH:
        text = text[:_MAX_REASON_LENGTH].rstrip() + "…"
    return text


class RenderError(Exception):
    """A clean, expected failure - a malformed/disallowed URL, a
    navigation timeout, or a page that never rendered real content.
    Always carries a short, non-sensitive reason string, never a raw
    Playwright exception or stack trace - reason is run through
    _safe_reason at construction time (not left to each raise site to
    remember), so EVERY RenderError anywhere in this module - including
    ones built from a raw caught exception's own repr, e.g. render_canva_
    page_async's own navigation-timeout branch - is safe by construction."""

    def __init__(self, reason: str):
        reason = _safe_reason(reason)
        super().__init__(reason)
        self.reason = reason


async def _read_current_and_total_pages(page, timeout_ms: int = 2_000) -> tuple:
    """
    Best-effort "current / total" page indicator - the SAME read
    _detect_page_count has always used for its own diagnostic-only total,
    generalized here to also expose the CURRENT page number, which both
    render_canva_page_async's and render_pitch_page_async's own pagination
    loops use (when available) to verify a "Next page" click actually
    advanced anything, rather than inventing a second, differently-shaped
    selector just for that check.

    Tries Canva's own accessible "Go to page" button first (its exact
    accessible name, e.g. inner text "1 / 7"); if that isn't present/
    readable at all, falls back to a plain text-content match for the same
    "current / total" shape anywhere on the page - confirmed directly via
    a throwaway Playwright recon script that this is exactly how Pitch's
    own equivalent counter renders (plain text in its bottom bar, e.g.
    "1 / 13", not inside a button by any particular accessible name).
    Either strategy degrades the exact same safe way: (None, None) when
    neither is found - a normal outcome for many real designs/decks (see
    _detect_page_count's own docstring), not a failure.

    `timeout_ms` defaults to _detect_page_count's own original one-time-
    per-render budget, but _page_advance_signature (called up to twice
    PER PAGINATION ATTEMPT, unlike the one-time initial detection) passes
    a much smaller value - a design with no such indicator at all would
    otherwise pay this full timeout, twice, on every single attempt,
    which would silently slow down the common case rather than only the
    one-time initial check this constant was originally sized for.

    Returns (None, None) - never raises - whenever this indicator isn't
    present/readable at all; a caller checking `current is not None`
    before trusting it is what makes this safe to treat as optional
    everywhere it's used.
    """
    text = None
    try:
        text = await page.get_by_role("button", name="Go to page").inner_text(timeout=timeout_ms)
    except Exception:
        try:
            text = await page.get_by_text(_PAGE_COUNT_RE).first.inner_text(timeout=timeout_ms)
        except Exception:
            return None, None
    match = _PAGE_COUNT_RE.search(re.sub(r"\s+", " ", text))
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


async def _detect_page_count(page) -> int:
    """
    Best-effort TOTAL page count only - see _read_current_and_total_pages
    for the shared read this wraps. Purely for the caller's own logging
    (see Handler.do_POST's "Canva design detected: N pages" line), never
    used to decide when to stop capturing pages (see render_canva_page_
    async's own docstring on why the Next-page button's aria-disabled
    state is the real stopping mechanism). Returns None on any failure -
    a design with no such indicator at all (or one Canva has changed the
    shape of) simply logs without a detected total, never a render
    failure.
    """
    _current, total = await _read_current_and_total_pages(page)
    return total


async def _page_content_fingerprint(page) -> str:
    """
    Fallback page-advance signal for when the "Go to page" indicator
    isn't present/readable at all (see _read_current_and_total_pages) -
    a cheap snapshot of the page's own visible text, which differs
    between any two genuinely different Canva slides in a real brochure
    (address, unit numbers, floor plan labels, ...). Deliberately NOT a
    Canva-specific CSS selector or DOM structure guess - just the same
    kind of accessible-text read this module already relies on elsewhere
    (see _read_current_and_total_pages), applied to the whole page
    rather than one specific button, so it degrades the same safe way:
    returns "" (never raises) on any failure, and callers treat two ""
    results as "still don't know", never as a false confirmation of
    change.
    """
    try:
        return await page.evaluate("() => document.body.innerText.slice(0, 500)")
    except Exception:
        return ""


async def _page_advance_signature(page) -> tuple:
    """
    Everything render_canva_page_async's pagination loop needs to later
    decide "did the page actually change" (see _page_has_advanced) -
    captured the SAME way both before and after a click so the two sides
    of that comparison are never taken via different mechanisms. Always
    reads BOTH signals (never either/or) - the numeric "current/total"
    indicator when available (see _read_current_and_total_pages) AND the
    content fingerprint fallback (see _page_content_fingerprint) - so a
    comparison is never left stuck with a "before" value from one
    mechanism and an "after" value from the other, e.g. if the indicator
    happens to be readable at one moment but not the other.
    """
    page_number, _total = await _read_current_and_total_pages(page, timeout_ms=_PAGE_ADVANCE_INDICATOR_TIMEOUT_MS)
    fingerprint = await _page_content_fingerprint(page)
    return page_number, fingerprint


def _page_has_advanced(before: tuple, after: tuple) -> bool:
    """
    Confirms a "Next page" click actually changed the visible page,
    rather than trusting a click that merely didn't raise (see
    render_canva_page_async's own pagination docstring for the real
    production symptom this closes: a click that Playwright reports as
    completed can still land on Canva's own debounced no-op).

    `before`/`after` are both _page_advance_signature(page) results.
    Prefers the numeric "current/total" indicator when BOTH sides have
    it - an exact, unambiguous comparison; falls back to the content-
    fingerprint comparison otherwise (see _page_content_fingerprint's
    own "least brittle alternative" reasoning) - e.g. a design with no
    such indicator at all, or one that was briefly unreadable on just
    one side of this specific comparison.
    """
    page_before, fingerprint_before = before
    page_after, fingerprint_after = after
    if page_before is not None and page_after is not None:
        return page_after != page_before
    return bool(fingerprint_after) and fingerprint_after != fingerprint_before


# A page's every real <a href> anchor, evaluated directly against the live
# DOM (never the screenshot) - confirmed directly, via a throwaway
# Playwright recon script against a real Colliers-on-Canva deck, that a
# per-property brochure/document link is a genuine <a href>, not a JS-only
# click handler with no href at all - this is a straightforward DOM read,
# not a click-simulation problem. Deduped by (href, text) - a real design
# renders the SAME link element TWICE (once wrapped in Canva's own
# canva.com/link?target=... click-tracking redirector, once more as an
# already-unwrapped duplicate carrying the plain real destination
# directly) - resolving the wrapper's own "target" query param first
# means both forms collapse into one clean entry rather than two, cutting
# needless noise/duplicate validation work downstream. Any OTHER href
# (including whatever Pitch's own sharing surface may or may not wrap
# links in - not directly confirmed the way Canva's shape is here) is
# passed through completely unresolved; it is still a perfectly usable
# href either way, since a later real fetch of it already follows
# redirects on its own regardless of whether this stripped a wrapper
# first.
_PAGE_LINK_CANDIDATES_JS = """
() => {
  function resolveHref(raw) {
    try {
      const u = new URL(raw, window.location.href);
      if (u.hostname === 'www.canva.com' && u.pathname === '/link' && u.searchParams.has('target')) {
        return u.searchParams.get('target');
      }
      return u.href;
    } catch (e) {
      return raw;
    }
  }
  const seen = new Set();
  const results = [];
  for (const el of document.querySelectorAll('a[href]')) {
    const href = resolveHref(el.getAttribute('href'));
    const text = (el.innerText || el.textContent || '').trim();
    if (!href || !text) continue;
    const key = href + '|' + text;
    if (seen.has(key)) continue;
    seen.add(key);
    results.push({href, text});
  }
  return results;
}
"""


async def _page_link_candidates(page) -> list:
    """
    [{"href", "text"}, ...] for every real anchor currently on `page` (see
    _PAGE_LINK_CANDIDATES_JS) - called once per captured screenshot, right
    alongside it, so a page's own links are always exactly as of the
    moment its own image was captured (Canva/Pitch's pagination advances
    the SAME loaded design in place - see render_canva_page_async's own
    docstring - so this must be read fresh per page, never once up
    front). Best-effort: [] (never a hard failure) if the DOM read itself
    ever raises, since a page that failed to yield a screenshot at all
    already stopped this loop long before this would run, and a page
    with genuinely zero links is a normal, expected result too.
    """
    try:
        return await page.evaluate(_PAGE_LINK_CANDIDATES_JS)
    except Exception as e:
        print(f"[canva_renderer] Could not read link candidates from a page: {e!r}", file=sys.stderr)
        return []


async def render_canva_page_async(url: str) -> tuple[list[bytes], list, int]:
    """
    The real render logic - see render_canva_page (this module's own sync-
    facing entry point HTTP requests actually call) for how this gets
    scheduled onto the single dedicated Playwright loop thread from an
    arbitrary request thread.

    Returns (pages, page_links, detected_total): `pages` is a list of PNG
    bytes, one per page actually captured, in page order, ALWAYS at least
    length 1 (the first/cover page `url` lands on) - or raises
    RenderError(reason) on any safe-failure condition preventing even that
    first page, a caller only ever needs to catch this one exception type.
    `page_links` is the same length as `pages`, each entry that page's own
    real <a href> anchors (see _page_link_candidates) - possibly an empty
    list for a page with none, never a page missing from the list
    entirely. `detected_total` is Canva's own reported page count if it
    could be read (see _detect_page_count), else None - purely
    informational, never a promise that many pages were actually captured
    (MAX_CANVA_PAGES may cap `pages` shorter).

    Captures every further page via Canva's own accessible "Next page"
    button (confirmed directly against a real multi-page public brochure:
    a stable aria-label, unaffected by Canva's own CSS class names, which
    are regenerated per build) - clicking it advances the SAME already-
    loaded design in place (confirmed: the page's own URL fragment updates,
    e.g. "...view#2", with no new navigation/network round trip), and its
    own aria-disabled="true" state on the last page (confirmed directly) is
    what stops the loop - never a fixed page count assumed up front, and
    never CSS-class-dependent selector logic. Stops early, keeping every
    page already captured, the instant this stable mechanism isn't found or
    doesn't behave as expected (a future Canva frontend change) - the same
    "start conservative" precedent as this service's other safe-failure
    paths - rather than guessing at a replacement selector.
    """
    if not _is_recognized_canva_url(url):
        raise RenderError("not a recognized public Canva URL")
    if not _host_allowed(url):
        raise RenderError("host not allowed")

    browser = await _get_browser_async()
    context = await browser.new_context(viewport=VIEWPORT)
    page = None
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
            # "load" (the browser's own load event - initial HTML/JS/CSS/
            # image resources fetched), NOT "networkidle" - Canva is a
            # heavy SPA that keeps background network activity going
            # indefinitely (websockets, polling, analytics beacons), so
            # "networkidle" can wait out the ENTIRE timeout even after the
            # design has already visually finished rendering, confirmed as
            # a real contributor to production navigation timeouts. This
            # does NOT risk screenshotting a blank/half-drawn design: "load"
            # only gets the page's own resources in; SETTLE_MS below (kept
            # unchanged) is what actually waits for Canva's OWN JS to paint
            # the design after that, exactly like it already did before
            # this change - "load" simply stops this from also waiting on
            # network traffic that has nothing to do with whether the
            # design has rendered.
            response = await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        except Exception as e:
            raise RenderError(f"navigation failed or timed out ({e!r})")

        # Real production evidence this catches: a dead/expired/access-
        # revoked Canva link (both canva.link short links and plain
        # www.canva.com/design/.../view URLs) still stays ON canva.com and
        # still loads successfully - just Canva's OWN "Looks like we hit a
        # roadblock / that link doesn't work" 404 error page, confirmed
        # directly against three real production URLs. That page has
        # neither a "Next page" nor a "Go to page" control, so - before
        # this check existed - it was silently captured as a normal 1-page
        # "successful" render (an error-page screenshot handed to Gemini
        # as if it were real brochure content) rather than the clean,
        # honest render failure it actually is. `response` can legitimately
        # be None for some navigations (e.g. a same-document navigation) -
        # never treated as a failure on its own, only a genuine non-2xx
        # status is.
        if response is not None and not (200 <= response.status < 300):
            raise RenderError(f"navigation returned HTTP {response.status} (design not found or inaccessible)")

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

        pages = [await page.screenshot(type="png")]
        page_links = [await _page_link_candidates(page)]
        detected_total = await _detect_page_count(page)

        # Every further page - see this function's own docstring above for
        # why the Next-page button's aria-disabled state (not a fixed
        # count) is what stops this loop. A transient click failure gets
        # a bounded retry with a freshly re-acquired locator (see
        # MAX_NEXT_CLICK_ATTEMPTS's own docstring); once those attempts
        # are exhausted, this simply stops the loop and keeps whatever
        # was already captured - a genuinely multi-page brochure whose
        # page 4 hiccups still yields pages 1-3 rather than losing all of
        # them, and a single-page design (no Next-page button at all)
        # yields exactly the one page it always did before this feature
        # existed.
        while len(pages) < MAX_CANVA_PAGES:
            # Checked ONCE per transition, never inside the retry loop
            # below - this reflects live DOM state at the START of this
            # transition; re-querying it again mid-retry would conflate
            # "did this transition finish" with "should we even attempt
            # another one", which are different questions. A failure here
            # (button gone, attribute read itself fails) is the same
            # "stop conservatively, keep what's captured" precedent as
            # every other pagination failure in this loop - logged
            # distinctly from a CLICK failure below (see Task 4's own
            # "button not found" vs "click timed out" distinction), since
            # this is a different failure shape: no click was ever even
            # attempted.
            try:
                next_button = page.get_by_role("button", name="Next page")
                if await next_button.get_attribute("aria-disabled", timeout=_NEXT_BUTTON_PRESENCE_TIMEOUT_MS) == "true":
                    break
            except Exception as e:
                print(
                    f"[canva_renderer] Pagination ended for {url!r}: 'Next page' button not found or not "
                    f"readable ({e!r}) - {len(pages)} page(s) captured, keeping them.",
                    file=sys.stderr,
                )
                break

            advanced = False
            last_error = None
            signature_after = (None, "")
            for attempt in range(1, MAX_NEXT_CLICK_ATTEMPTS + 1):
                signature_before = await _page_advance_signature(page)
                try:
                    # Re-acquired fresh on EVERY attempt - never the same
                    # handle reused across a retry (see MAX_NEXT_CLICK_
                    # ATTEMPTS's own docstring on why).
                    next_button = page.get_by_role("button", name="Next page")
                    await next_button.click(timeout=NEXT_PAGE_CLICK_TIMEOUT_MS)
                except Exception as e:
                    last_error = e
                    print(
                        f"[canva_renderer] Pagination click failed for {url!r} on attempt "
                        f"{attempt}/{MAX_NEXT_CLICK_ATTEMPTS} (page before: {signature_before[0]!r}) - "
                        f"{e!r}.",
                        file=sys.stderr,
                    )
                    if attempt < MAX_NEXT_CLICK_ATTEMPTS:
                        await page.wait_for_timeout(400 * attempt)  # brief backoff, then re-acquire
                    continue

                await page.wait_for_timeout(PAGE_NAV_SETTLE_MS)

                # The click itself didn't raise, but that alone is NOT
                # trusted as proof the page advanced (see _page_has_
                # advanced's own docstring for the real production
                # symptom this closes) - only counted as a successful
                # page once the page's own state is confirmed different.
                signature_after = await _page_advance_signature(page)
                if _page_has_advanced(signature_before, signature_after):
                    pages.append(await page.screenshot(type="png"))
                    page_links.append(await _page_link_candidates(page))
                    advanced = True
                    break

                last_error = "click completed but the page did not advance"
                print(
                    f"[canva_renderer] Pagination click for {url!r} on attempt "
                    f"{attempt}/{MAX_NEXT_CLICK_ATTEMPTS} completed but the page did not advance "
                    f"(page before: {signature_before[0]!r}, after: {signature_after[0]!r}).",
                    file=sys.stderr,
                )
                if attempt < MAX_NEXT_CLICK_ATTEMPTS:
                    await page.wait_for_timeout(400 * attempt)  # brief backoff, then re-acquire

            if advanced and attempt > 1:
                # Distinct from the plain "captured a page" case below -
                # confirms a retry actually recovered a page that would
                # otherwise have been lost, useful to see in Cloud Run
                # logs alongside the give-up line below (same shape:
                # named URL, attempt number, page count so far).
                print(
                    f"[canva_renderer] Pagination click recovered on attempt {attempt}/{MAX_NEXT_CLICK_ATTEMPTS} "
                    f"for {url!r} (page now: {signature_after[0]!r}) - {len(pages)} page(s) captured so far.",
                    file=sys.stderr,
                )
            if not advanced:
                print(
                    f"[canva_renderer] Pagination failure for {url!r}: gave up after "
                    f"{MAX_NEXT_CLICK_ATTEMPTS} attempts (last: {last_error!r}) - stopped at "
                    f"{len(pages)} page(s) captured (detected total: {detected_total!r}), keeping them.",
                    file=sys.stderr,
                )
                break
        else:
            # Reached ONLY when the loop's own condition (len(pages) <
            # MAX_CANVA_PAGES) became false WITHOUT a break - every other
            # exit above (button not found/not readable, gave up after
            # retries, the button's own aria-disabled="true" clean stop)
            # explicitly breaks, so landing here means this render hit its
            # own page cap - NOT, on its own, proof there's more beyond it:
            # a design with EXACTLY MAX_CANVA_PAGES pages hits this same
            # condition on its own genuinely final page. Distinguished by
            # one more cheap, bounded check of the "Next page" control's
            # OWN current state - only a control that's still genuinely
            # present and enabled means pages were actually left
            # uncaptured. Silent before this existed - real production
            # evidence: a genuine 29-page brochure silently truncated to
            # MAX_CANVA_PAGES=20 with nothing anywhere (log or response)
            # distinguishing that from this normal, complete, exact-fit
            # case.
            if len(pages) >= MAX_CANVA_PAGES:
                next_page_still_available = False
                try:
                    next_button = page.get_by_role("button", name="Next page")
                    if await next_button.count() > 0:
                        disabled = await next_button.get_attribute(
                            "aria-disabled", timeout=_NEXT_BUTTON_PRESENCE_TIMEOUT_MS,
                        )
                        next_page_still_available = disabled != "true"
                except Exception:
                    next_page_still_available = False

                if next_page_still_available:
                    print(
                        f"[canva_renderer] Pagination capped for {url!r}: reached MAX_CANVA_PAGES="
                        f"{MAX_CANVA_PAGES} with 'Next page' still available (detected total: "
                        f"{detected_total!r}) - stopping here with {len(pages)} page(s) captured; "
                        "this brochure may have more pages than were captured.",
                        file=sys.stderr,
                    )

        return pages, page_links, detected_total
    finally:
        # Page closed BEFORE its context - releases this request's own
        # renderer-process resources (every slide's DOM/canvas state
        # visited during pagination) as early as possible, rather than
        # waiting on context.close()'s own cascade. Always closed, success
        # or failure - a leaked context/page would otherwise accumulate
        # memory across requests indefinitely.
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        await context.close()


def render_canva_page(url: str) -> tuple[list[bytes], list, int]:
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


async def render_pitch_page_async(url: str) -> tuple[list[bytes], list, int]:
    """
    Pitch.com's own counterpart to render_canva_page_async - see that
    function's own docstring for the shared shape (browser/context setup,
    navigation, settle, best-effort cookie-dismiss, and a bounded
    pagination-capture loop with retry + page-cap + advance-verification,
    all confirmed to work the same way for Pitch via a throwaway Playwright
    recon script run directly against real GPE/Knotel/MetSpace brochure
    links before this was written). Two genuine, confirmed differences
    from Canva, both from that same recon:

    1. Navigation waits for "networkidle", not "load" - the OPPOSITE of
       Canva's own choice (see render_canva_page_async's own docstring on
       why Canva avoids networkidle: a heavy SPA with persistent
       background traffic that never goes idle). Pitch showed no such
       problem in either real recon run (networkidle reached in 1-4s, both
       times) - and unlike Canva, Pitch's initial HTML is a genuinely
       empty shell (`<div id="app">` plus a handful of `<script>` tags, no
       content of its own at all) that only ever gets real content once
       its OWN JS has fetched and rendered it, so waiting for network
       activity to genuinely settle is the more reliable signal here, not
       a needless risk the way it would be for Canva.
    2. The Next/Previous controls are named "Next"/"Previous" (confirmed
       via their own accessible aria-label), not "Next page", and their
       disabled state is conveyed via the plain HTML boolean `disabled`
       attribute - present (even as an empty string) when there is no
       further page, absent (None) otherwise - never Canva's own ARIA
       aria-disabled="true"/"false" string values. Confirmed directly:
       clicking "Next" advances the deck and the "disabled" attribute
       reflects this correctly on both the "Next" and "Previous" controls
       as the deck is paged through end to end.

    Every other timing budget/retry/cap constant is reused UNCHANGED from
    Canva's own (NAV_TIMEOUT_MS, SETTLE_MS, COOKIE_DISMISS_TIMEOUT_MS,
    NEXT_PAGE_CLICK_TIMEOUT_MS, MAX_NEXT_CLICK_ATTEMPTS,
    _PAGE_ADVANCE_INDICATOR_TIMEOUT_MS, _NEXT_BUTTON_PRESENCE_TIMEOUT_MS) -
    the real recon timing (1-4s networkidle, well within these existing
    budgets) gave no reason to invent new ones; only MAX_PITCH_PAGES is a
    distinct constant (see its own docstring for why, even though it
    currently shares the same default value).

    Returns/raises exactly like render_canva_page_async - see that
    function's own docstring for the full contract.
    """
    if not _is_recognized_pitch_url(url):
        raise RenderError("not a recognized public Pitch URL")
    if not _host_allowed(url):
        raise RenderError("host not allowed")

    browser = await _get_browser_async()
    context = await browser.new_context(viewport=VIEWPORT)
    page = None
    try:
        page = await context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        page.set_default_timeout(NAV_TIMEOUT_MS)

        async def _guard(route):
            if _host_allowed(route.request.url):
                await route.continue_()
            else:
                await route.abort()

        await page.route("**/*", _guard)

        try:
            # "networkidle", not "load" - see this function's own
            # docstring point 1 above for why this is the right choice
            # for Pitch specifically, unlike for Canva.
            response = await page.goto(url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
        except Exception as e:
            raise RenderError(f"navigation failed or timed out ({e!r})")

        # Same dead/expired-link detection as Canva's own (see render_
        # canva_page_async's own identical check) - kept as the exact same
        # message shape ("navigation returned HTTP {status} ...") on
        # purpose, since the main app's own _is_confirmed_dead_canva_link
        # matches on that literal substring regardless of which platform
        # produced it.
        if response is not None and not (200 <= response.status < 300):
            raise RenderError(f"navigation returned HTTP {response.status} (design not found or inaccessible)")

        if not _host_allowed(page.url):
            raise RenderError("navigation left the allowed Pitch host")

        await page.wait_for_timeout(SETTLE_MS)

        # Best-effort only, same as Canva's own - not confirmed present on
        # either real recon URL, but costs nothing to also attempt here if
        # a cookie-consent banner shows up on some Pitch deployments.
        for label in ("Reject all cookies", "Accept all cookies"):
            try:
                await page.get_by_text(label, exact=True).click(timeout=COOKIE_DISMISS_TIMEOUT_MS)
                await page.wait_for_timeout(500)
                break
            except Exception:
                continue

        pages = [await page.screenshot(type="png")]
        page_links = [await _page_link_candidates(page)]
        detected_total = await _detect_page_count(page)

        # Identical loop shape to render_canva_page_async's own (see that
        # function's own docstring for the full rationale on every piece
        # of this: bounded retry with a freshly re-acquired locator,
        # advance verification via _page_advance_signature/_page_has_
        # advanced, the page-cap "is there genuinely more" check) - only
        # the button's own accessible name and disabled-state attribute
        # differ (see this function's own docstring points 1-2 above).
        while len(pages) < MAX_PITCH_PAGES:
            try:
                next_button = page.get_by_role("button", name="Next")
                if await next_button.get_attribute("disabled", timeout=_NEXT_BUTTON_PRESENCE_TIMEOUT_MS) is not None:
                    break
            except Exception as e:
                print(
                    f"[canva_renderer] Pitch pagination ended for {url!r}: 'Next' button not found or not "
                    f"readable ({e!r}) - {len(pages)} page(s) captured, keeping them.",
                    file=sys.stderr,
                )
                break

            advanced = False
            last_error = None
            signature_after = (None, "")
            for attempt in range(1, MAX_NEXT_CLICK_ATTEMPTS + 1):
                signature_before = await _page_advance_signature(page)
                try:
                    next_button = page.get_by_role("button", name="Next")
                    await next_button.click(timeout=NEXT_PAGE_CLICK_TIMEOUT_MS)
                except Exception as e:
                    last_error = e
                    print(
                        f"[canva_renderer] Pitch pagination click failed for {url!r} on attempt "
                        f"{attempt}/{MAX_NEXT_CLICK_ATTEMPTS} (page before: {signature_before[0]!r}) - "
                        f"{e!r}.",
                        file=sys.stderr,
                    )
                    if attempt < MAX_NEXT_CLICK_ATTEMPTS:
                        await page.wait_for_timeout(400 * attempt)
                    continue

                await page.wait_for_timeout(PAGE_NAV_SETTLE_MS)

                signature_after = await _page_advance_signature(page)
                if _page_has_advanced(signature_before, signature_after):
                    pages.append(await page.screenshot(type="png"))
                    page_links.append(await _page_link_candidates(page))
                    advanced = True
                    break

                last_error = "click completed but the page did not advance"
                print(
                    f"[canva_renderer] Pitch pagination click for {url!r} on attempt "
                    f"{attempt}/{MAX_NEXT_CLICK_ATTEMPTS} completed but the page did not advance "
                    f"(page before: {signature_before[0]!r}, after: {signature_after[0]!r}).",
                    file=sys.stderr,
                )
                if attempt < MAX_NEXT_CLICK_ATTEMPTS:
                    await page.wait_for_timeout(400 * attempt)

            if advanced and attempt > 1:
                print(
                    f"[canva_renderer] Pitch pagination click recovered on attempt {attempt}/"
                    f"{MAX_NEXT_CLICK_ATTEMPTS} for {url!r} (page now: {signature_after[0]!r}) - "
                    f"{len(pages)} page(s) captured so far.",
                    file=sys.stderr,
                )
            if not advanced:
                print(
                    f"[canva_renderer] Pitch pagination failure for {url!r}: gave up after "
                    f"{MAX_NEXT_CLICK_ATTEMPTS} attempts (last: {last_error!r}) - stopped at "
                    f"{len(pages)} page(s) captured (detected total: {detected_total!r}), keeping them.",
                    file=sys.stderr,
                )
                break
        else:
            if len(pages) >= MAX_PITCH_PAGES:
                next_page_still_available = False
                try:
                    next_button = page.get_by_role("button", name="Next")
                    if await next_button.count() > 0:
                        disabled = await next_button.get_attribute(
                            "disabled", timeout=_NEXT_BUTTON_PRESENCE_TIMEOUT_MS,
                        )
                        next_page_still_available = disabled is None
                except Exception:
                    next_page_still_available = False

                if next_page_still_available:
                    print(
                        f"[canva_renderer] Pitch pagination capped for {url!r}: reached MAX_PITCH_PAGES="
                        f"{MAX_PITCH_PAGES} with 'Next' still available (detected total: "
                        f"{detected_total!r}) - stopping here with {len(pages)} page(s) captured; "
                        "this presentation may have more pages than were captured.",
                        file=sys.stderr,
                    )

        return pages, page_links, detected_total
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        await context.close()


def render_pitch_page(url: str) -> tuple[list[bytes], list, int]:
    """Sync-facing entry point for Pitch, mirroring render_canva_page's
    own thread-bridging (see that function's own docstring) - the only
    difference is its own PITCH_RENDER_TIMEOUT_SECONDS budget, sized off
    MAX_PITCH_PAGES rather than MAX_CANVA_PAGES."""
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(render_pitch_page_async(url), loop)
    try:
        return future.result(timeout=PITCH_RENDER_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise RenderError("render timed out")


def render_page(url: str) -> tuple[list[bytes], list, int]:
    """
    Dispatches to whichever platform's own renderer a `url` shape
    recognizes (see _is_recognized_canva_url/_is_recognized_pitch_url) -
    the one call site Handler.do_POST actually needs, so it never has to
    know which platform-specific render function to call itself. Raises
    RenderError (never any other exception) when `url` matches neither
    shape - the same "clean, expected failure" contract every other
    rejection path in this module already uses.
    """
    if _is_recognized_canva_url(url):
        return render_canva_page(url)
    if _is_recognized_pitch_url(url):
        return render_pitch_page(url)
    raise RenderError("not a recognized public Canva or Pitch URL")


def _reencode_as_jpeg(page_bytes: bytes) -> bytes:
    """
    Re-encodes one already-captured screenshot (PNG, from page.screenshot)
    as a JPEG at JPEG_QUALITY - only ever called from Handler.do_POST when
    the estimated response payload would otherwise risk Cloud Run's own
    hard CLOUD_RUN_MAX_RESPONSE_BYTES ceiling (see RESPONSE_SIZE_SAFETY_
    THRESHOLD_BYTES). PNG's lossless compression is a poor fit for a real
    screenshot of a photo-heavy brochure page in the first place - JPEG's
    lossy compression shrinks this kind of content roughly 5x with no
    visible quality loss, confirmed directly (see JPEG_QUALITY's own
    comment). convert("RGB") first: page.screenshot's own PNG output has
    no alpha channel to lose (the page background is always opaque), but
    Pillow's JPEG encoder rejects saving an RGBA image outright without
    this - never a lossy step of its own on an already-opaque image.
    """
    image = Image.open(io.BytesIO(page_bytes)).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


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

        if not _render_semaphore.acquire(timeout=SEMAPHORE_WAIT_TIMEOUT_SECONDS):
            # A hard concurrency cap, but a QUEUE now, not an instant-reject
            # - see SEMAPHORE_WAIT_TIMEOUT_SECONDS's own docstring above for
            # the real production bug this fixes (a burst of Canva requests
            # during a bulk upload used to drop most of them with an
            # immediate 503, regardless of whether that specific design
            # would have rendered fine). Still a firm ceiling: one broken/
            # slow Canva page must never be able to pile up UNBOUNDED work
            # here, so a slot that never frees up within this wait budget
            # still fails safely - the caller (see brochure_enrichment.
            # _fetch_canva_rendered_page) already treats any non-2xx
            # response as a normal, safe per-document failure. "reason"
            # alongside "error" here purely so the main app's own generic
            # reason-extraction (same field, same shape as the 422/500
            # bodies below) never needs a special
            # case for this one response - not a caught exception, so
            # nothing to run through _safe_reason.
            self._send_json(503, {"error": "renderer busy, try again", "reason": "renderer busy, try again"})
            return
        try:
            try:
                pages, page_links, detected_total = render_page(url)
            except RenderError as e:
                # e.reason is already _safe_reason'd at RenderError's own
                # construction time (see that class's own docstring) -
                # never re-truncated/re-collapsed here, since doing so
                # again would be a silent no-op at best.
                self._send_json(422, {"error": "render_failed", "reason": e.reason})
                return
            except Exception as e:
                # The ONE path that can carry an arbitrary, un-vetted
                # exception (anything Playwright/Chromium/asyncio itself
                # raised that this module didn't already wrap in a
                # RenderError) - always routed through _safe_reason before
                # it ever reaches a caller, exactly like every RenderError
                # reason already is.
                self._send_json(500, {"error": "internal_error", "reason": _safe_reason(repr(e))})
                return

            # A single concise line whether this was one page or several -
            # see this service's own module docstring on "<Platform> render
            # succeeded" being the one unambiguous line to grep Cloud Run
            # logs for to confirm rendering itself worked. platform_label
            # is a cheap, purely-cosmetic re-check of the URL's own shape
            # (render_page above already dispatched on the exact same
            # check) - never a second network call or render attempt.
            platform_label = "Pitch" if _is_recognized_pitch_url(url) else "Canva"
            detected_str = f"{detected_total} detected" if detected_total else "total unknown"
            page_count = len(pages)
            print(
                f"[canva_renderer] {platform_label} render succeeded for {url!r}: "
                f"{page_count} page(s) captured ({detected_str}).",
                file=sys.stderr,
            )
            # Estimated from the RAW page bytes still in hand, before any
            # encoding - base64 blows up its input by exactly 4/3, so this
            # is a close, cheap upper-bound estimate of the eventual JSON
            # payload size (the JSON envelope's own small fixed overhead
            # on top of that is exactly why RESPONSE_SIZE_SAFETY_THRESHOLD_
            # BYTES leaves real margin below CLOUD_RUN_MAX_RESPONSE_BYTES,
            # rather than comparing against it directly).
            total_raw_bytes = sum(len(p) for p in pages)
            estimated_payload_bytes = total_raw_bytes * 4 // 3
            if estimated_payload_bytes > RESPONSE_SIZE_SAFETY_THRESHOLD_BYTES:
                print(
                    f"[canva_renderer] {url!r}: estimated payload "
                    f"~{estimated_payload_bytes / (1024 * 1024):.1f}MB would risk Cloud Run's own "
                    f"{CLOUD_RUN_MAX_RESPONSE_BYTES // (1024 * 1024)}MB response-size limit - re-encoding "
                    f"{page_count} page(s) as JPEG (quality={JPEG_QUALITY}) instead of PNG.",
                    file=sys.stderr,
                )
                image_format = "jpeg"
                source_pages = [_reencode_as_jpeg(p) for p in pages]
            else:
                image_format = "png"
                source_pages = pages

            # A bounded JSON array of base64-encoded pages, never a single
            # giant concatenated image - each page stays a separate,
            # independently sized image the main app can feed straight
            # into its existing multi-image Gemini extraction (see
            # extract.render_and_extract), exactly like a multi-page PDF's
            # own per-page images already are. image_format is purely
            # informational here (see this response's own docstring in
            # extract.images_from_png_pages - the main app sniffs the
            # actual bytes' own magic number instead of trusting this
            # field, so the two services need no wire-contract
            # coordination for this at all) - included only so Cloud Run
            # logs/a manual response inspection can see which format was
            # actually used without decoding page bytes by hand.
            #
            # Encoded INCREMENTALLY - draining a SHALLOW COPY of source_
            # pages (cheap: just duplicating up to MAX_CANVA_PAGES
            # references, never the underlying image bytes themselves) one
            # page at a time, freeing each one (`del page_bytes`) the
            # instant its own base64 copy exists - rather than a single
            # list comprehension, which would keep EVERY raw page alive
            # for the ENTIRE comprehension while ALSO building the full
            # base64 list alongside it. Real production evidence this
            # matters for: "Memory limit of 1024 MiB exceeded" while
            # processing a large multi-page brochure (an 18-page deck was
            # one of this service's own successful renders) - this halves
            # the peak extra memory this specific response-construction
            # step needs, on top of whatever Chromium itself already used
            # to render it. A copy, not `pages`/source_pages itself, so
            # this never mutates a list some OTHER caller/reference might
            # still expect intact.
            remaining = list(source_pages)
            encoded_pages = []
            while remaining:
                page_bytes = remaining.pop(0)
                encoded_pages.append(base64.b64encode(page_bytes).decode("ascii"))
                del page_bytes
            self._send_json(200, {
                "pages": encoded_pages,
                "page_count_detected": detected_total,
                "image_format": image_format,
                # One list per page, in the SAME order as "pages" (see
                # _page_link_candidates) - each page's own real <a href>
                # anchors, for attributing a per-property document link
                # rather than falling back to one shared link for the
                # whole render. A NEW, additive key - any existing caller
                # reading only "pages"/"page_count_detected"/"image_format"
                # (see brochure_enrichment._fetch_rendered_page) is
                # completely unaffected by this simply being present.
                "links": page_links,
            })
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
