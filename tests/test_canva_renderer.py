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

import base64
import concurrent.futures
import contextlib
import importlib.util
import inspect
import io
import json
import random
import sys
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brochure_enrichment

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


class IsRecognizedPitchUrlTests(unittest.TestCase):
    def test_real_view_link_is_recognized(self):
        self.assertTrue(canva_renderer._is_recognized_pitch_url("https://pitch.com/v/1-finsbury-brochure-4jnj9d"))
        self.assertTrue(canva_renderer._is_recognized_pitch_url("https://pitch.com/v/hallmark-6th-floor-jdfuuc"))

    def test_non_pitch_url_is_rejected(self):
        self.assertFalse(canva_renderer._is_recognized_pitch_url("https://example.com/brochure.pdf"))

    def test_canva_url_is_not_recognized_as_pitch(self):
        self.assertFalse(canva_renderer._is_recognized_pitch_url("https://www.canva.com/design/x/y/view"))

    def test_pitch_homepage_is_rejected(self):
        self.assertFalse(canva_renderer._is_recognized_pitch_url("https://pitch.com/"))


class IsRecognizedGpeFlipbookUrlTests(unittest.TestCase):
    """GPE's own branded "fm.gpe.co.uk" custom domain for Pitch's Managed
    Links feature - confirmed real via a throwaway Playwright recon script
    against a real GPE managed-link URL (see render_pitch_page_async's own
    docstring): every asset/API call the page makes loads from pitch.com/
    *.services.pitch.com, so this is Pitch itself, not a separate
    platform - routed into render_pitch_page directly (see render_page)."""

    def test_real_flipbook_link_is_recognized(self):
        self.assertTrue(
            canva_renderer._is_recognized_gpe_flipbook_url("https://fm.gpe.co.uk/v/gpe-nineteen-wells-street-6hqnfd")
        )

    def test_trailing_uuid_segment_variant_is_recognized(self):
        # Real second shape confirmed in tests/sample_docs/GPE.eml.
        self.assertTrue(canva_renderer._is_recognized_gpe_flipbook_url(
            "https://fm.gpe.co.uk/v/gpe-availability-schedule-zu7yk2/b812cdcc-7bbb-429b-8af5-d000b8032853"
        ))

    def test_non_gpe_url_is_rejected(self):
        self.assertFalse(canva_renderer._is_recognized_gpe_flipbook_url("https://example.com/brochure.pdf"))

    def test_plain_pitch_com_url_is_not_recognized_as_gpe(self):
        self.assertFalse(canva_renderer._is_recognized_gpe_flipbook_url("https://pitch.com/v/1-finsbury-brochure-4jnj9d"))

    def test_gpe_flipbook_homepage_is_rejected(self):
        self.assertFalse(canva_renderer._is_recognized_gpe_flipbook_url("https://fm.gpe.co.uk/"))


class IsRecognizedKittUrlTests(unittest.TestCase):
    def test_real_preview_link_is_recognized(self):
        self.assertTrue(canva_renderer._is_recognized_kitt_url(
            "https://brochures.kittoffices.com/brochures/preview?entity%5B9e40cdea-02a1-44a5-9599-"
            "c3ed1567c117%5D=unit&display_label=Open+brochure"
        ))

    def test_variant_with_empty_template_param_is_recognized(self):
        self.assertTrue(canva_renderer._is_recognized_kitt_url(
            "https://brochures.kittoffices.com/brochures/preview?entity%5B7c50678c-7d16-4a8b-8f85-"
            "16dcd98b9a99%5D=unit&template=&display_label=Open+brochure"
        ))

    def test_unrelated_url_is_rejected(self):
        self.assertFalse(canva_renderer._is_recognized_kitt_url("https://example.com/brochure.pdf"))

    def test_pitch_url_is_rejected(self):
        self.assertFalse(canva_renderer._is_recognized_kitt_url("https://pitch.com/v/1-finsbury-brochure-4jnj9d"))

    def test_kitt_marketing_site_without_the_preview_path_is_rejected(self):
        # The allow-list entry is exactly "/brochures/preview" - Kitt's own
        # ordinary marketing site is a completely different, unrelated page
        # this service has no reason to ever navigate to.
        self.assertFalse(canva_renderer._is_recognized_kitt_url("https://www.kittoffices.com/"))

    def test_bare_preview_path_with_no_entity_query_is_still_recognized(self):
        # The path shape alone is what's matched - see _KITT_BROCHURE_
        # PREVIEW_URL_RE's own docstring on why the query string isn't
        # parsed precisely (too fragile across encoding variations).
        self.assertTrue(canva_renderer._is_recognized_kitt_url("https://brochures.kittoffices.com/brochures/preview"))


class HostAllowedSsrfTests(unittest.TestCase):
    """
    The renderer's core SSRF defense - a strict allow-list, not a
    denylist. Rejecting everything that isn't canva.com/canva.link/
    pitch.com inherently rejects localhost/private IPs/file:///custom
    schemes too, with no separate IP-range logic to keep correct.
    """

    def test_canva_com_is_allowed(self):
        self.assertTrue(canva_renderer._host_allowed("https://www.canva.com/design/x/y/view"))

    def test_canva_link_is_allowed(self):
        self.assertTrue(canva_renderer._host_allowed("https://canva.link/abc"))

    def test_pitch_com_is_allowed(self):
        self.assertTrue(canva_renderer._host_allowed("https://pitch.com/v/abc"))

    def test_pitch_lookalike_domain_is_rejected(self):
        self.assertFalse(canva_renderer._host_allowed("https://pitch.com.evil.com/v/abc"))

    def test_fm_gpe_co_uk_is_allowed(self):
        self.assertTrue(canva_renderer._host_allowed("https://fm.gpe.co.uk/v/abc"))

    def test_fm_gpe_co_uk_lookalike_domain_is_rejected(self):
        self.assertFalse(canva_renderer._host_allowed("https://fm.gpe.co.uk.evil.com/v/abc"))

    def test_plain_gpe_co_uk_without_the_fm_subdomain_is_rejected(self):
        # The allow-list entry is exactly "fm.gpe.co.uk", never the bare
        # "gpe.co.uk" - GPE's own ordinary corporate site is a completely
        # different, unrelated host this service has no reason to ever
        # navigate to.
        self.assertFalse(canva_renderer._host_allowed("https://gpe.co.uk/portfolio/city-tower"))

    def test_lookalike_domain_is_rejected(self):
        # "canva.com.evil.com" must NOT match ".canva.com".
        self.assertFalse(canva_renderer._host_allowed("https://canva.com.evil.com/design/x/y/view"))

    def test_kittoffices_com_is_allowed(self):
        self.assertTrue(canva_renderer._host_allowed("https://brochures.kittoffices.com/brochures/preview"))

    def test_kittoffices_storage_subdomain_is_also_allowed(self):
        # The allow-list entry is the BASE domain "kittoffices.com", not
        # just "brochures.kittoffices.com" - confirmed real, this page's
        # own building photos/floor plans load from a DIFFERENT
        # kittoffices.com subdomain (storage.kittoffices.com), which must
        # also pass or every image on the page would be blocked.
        self.assertTrue(canva_renderer._host_allowed("https://storage.kittoffices.com/photos/4-millbank.jpg"))

    def test_kittoffices_lookalike_domain_is_rejected(self):
        self.assertFalse(canva_renderer._host_allowed("https://kittoffices.com.evil.com/brochures/preview"))

    def test_unrelated_google_maps_host_is_rejected(self):
        # Confirmed real: a live Kitt preview page also contacts
        # maps.googleapis.com for a small "Transport Links" thumbnail -
        # deliberately NOT allow-listed (see _ALLOWED_HOST_SUFFIXES' own
        # docstring on why), so this must still be blocked.
        self.assertFalse(canva_renderer._host_allowed("https://maps.googleapis.com/maps/api/staticmap"))

    def test_localhost_is_rejected(self):
        self.assertFalse(canva_renderer._host_allowed("http://localhost:8080/"))

    def test_private_ip_is_rejected(self):
        self.assertFalse(canva_renderer._host_allowed("http://169.254.169.254/latest/meta-data/"))
        self.assertFalse(canva_renderer._host_allowed("http://10.0.0.5/"))

    def test_file_scheme_is_rejected(self):
        self.assertFalse(canva_renderer._host_allowed("file:///etc/passwd"))

    def test_arbitrary_external_site_is_rejected(self):
        self.assertFalse(canva_renderer._host_allowed("https://example.com/"))


class PageAdvanceVerificationTests(unittest.TestCase):
    """
    Unit-level tests for _page_has_advanced/_page_advance_signature - the
    pagination-verification logic itself (see render_canva_page_async's
    own pagination docstring for the real production symptom this
    closes: a "Next page" click that Playwright reports as completed can
    still land on Canva's own debounced no-op). Exercised directly
    against mocked Page objects, independent of the full render loop -
    RenderCanvaPageAsyncTests below covers the integration.
    """

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def _page(self, go_to_page_text=None, fingerprint="same"):
        page = MagicMock()
        if go_to_page_text is None:
            button = MagicMock()
            button.inner_text = AsyncMock(side_effect=Exception("no indicator"))
        else:
            button = MagicMock()
            button.inner_text = AsyncMock(return_value=go_to_page_text)
        page.get_by_role = MagicMock(return_value=button)
        page.evaluate = AsyncMock(return_value=fingerprint)
        return page

    def test_prefers_the_numeric_indicator_when_both_sides_have_it(self):
        page_before = self._page(go_to_page_text="2 / 7")
        page_after = self._page(go_to_page_text="3 / 7")
        before = self._run(canva_renderer._page_advance_signature(page_before))
        after = self._run(canva_renderer._page_advance_signature(page_after))
        self.assertTrue(canva_renderer._page_has_advanced(before, after))

    def test_numeric_indicator_unchanged_is_not_advanced_even_if_fingerprint_differs(self):
        # The numeric indicator, when both sides have it, is authoritative
        # over the fingerprint fallback - a real "current/total" match
        # takes precedence, never overridden by an unrelated text change
        # elsewhere on the page (e.g. an animation, a loading spinner).
        page_before = self._page(go_to_page_text="2 / 7", fingerprint="fingerprint-a")
        page_after = self._page(go_to_page_text="2 / 7", fingerprint="fingerprint-b")
        before = self._run(canva_renderer._page_advance_signature(page_before))
        after = self._run(canva_renderer._page_advance_signature(page_after))
        self.assertFalse(canva_renderer._page_has_advanced(before, after))

    def test_falls_back_to_content_fingerprint_when_no_numeric_indicator(self):
        page_before = self._page(go_to_page_text=None, fingerprint="fingerprint-a")
        page_after = self._page(go_to_page_text=None, fingerprint="fingerprint-b")
        before = self._run(canva_renderer._page_advance_signature(page_before))
        after = self._run(canva_renderer._page_advance_signature(page_after))
        self.assertTrue(canva_renderer._page_has_advanced(before, after))

    def test_unchanged_fingerprint_is_not_advanced(self):
        page_before = self._page(go_to_page_text=None, fingerprint="same-content")
        page_after = self._page(go_to_page_text=None, fingerprint="same-content")
        before = self._run(canva_renderer._page_advance_signature(page_before))
        after = self._run(canva_renderer._page_advance_signature(page_after))
        self.assertFalse(canva_renderer._page_has_advanced(before, after))

    def test_two_empty_fingerprints_are_never_a_false_positive(self):
        # Both signals unavailable (no indicator, evaluate itself fails)
        # must never be treated as "confirmed changed" - only a genuine,
        # non-empty difference counts.
        page_before = self._page(go_to_page_text=None)
        page_before.evaluate = AsyncMock(side_effect=Exception("evaluate failed"))
        page_after = self._page(go_to_page_text=None)
        page_after.evaluate = AsyncMock(side_effect=Exception("evaluate failed"))
        before = self._run(canva_renderer._page_advance_signature(page_before))
        after = self._run(canva_renderer._page_advance_signature(page_after))
        self.assertFalse(canva_renderer._page_has_advanced(before, after))


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class WaitForPageContentToStabilizeTests(unittest.TestCase):
    """
    Unit-level tests for _wait_for_page_content_to_stabilize - the real,
    confirmed production case this closes: the SAME Canva design
    (DAGbhpjThxc) produced full building-amenities text in one extraction
    and only sparse per-unit text (no amenities at all) in another, for
    the identical document. A bare numbers-table slide paints instantly,
    but a content-heavy slide (a bullet-list "BUILDING FEATURES"/"OFFICE
    FEATURES" layout) can still be mid-paint at the fixed PAGE_NAV_
    SETTLE_MS wait under load, so a screenshot taken then alone can
    capture only part of that page's own text.
    """

    def _page(self, fingerprints):
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        page.evaluate = AsyncMock(side_effect=list(fingerprints))
        return page

    def test_stops_polling_once_two_consecutive_reads_match(self):
        # First poll ("v1") differs from the caller-supplied initial
        # fingerprint ("v0") - content was still changing at the fixed
        # settle wait, exactly the real reported failure shape. Second
        # poll ("v1" again) matches the first, direct evidence painting
        # has genuinely stopped - polling must end there, not keep going
        # to the cap.
        page = self._page(["v1", "v1"])

        _run(canva_renderer._wait_for_page_content_to_stabilize(page, "v0"))

        self.assertEqual(page.evaluate.call_count, 2)
        self.assertEqual(page.wait_for_timeout.call_count, 2)
        page.wait_for_timeout.assert_called_with(canva_renderer.PAGE_STABILITY_POLL_INTERVAL_MS)

    def test_stabilizes_immediately_when_the_first_poll_already_matches(self):
        page = self._page(["v0"])

        _run(canva_renderer._wait_for_page_content_to_stabilize(page, "v0"))

        self.assertEqual(page.evaluate.call_count, 1)
        self.assertEqual(page.wait_for_timeout.call_count, 1)

    def test_never_exceeds_the_cap_when_content_keeps_changing(self):
        # A page whose content never settles (a live clock, a subtly
        # animating background) must still return - never hang the
        # render indefinitely - once max_extra_wait_ms elapses, at
        # PAGE_STABILITY_POLL_INTERVAL_MS per poll. max_extra_wait_ms is
        # an exact multiple of the poll interval here so the expected
        # poll count (elapsed_ms reaches the cap exactly, on the last
        # iteration's own boundary check) is unambiguous.
        poll_ms = canva_renderer.PAGE_STABILITY_POLL_INTERVAL_MS
        expected_polls = 3
        max_extra_wait_ms = poll_ms * expected_polls
        ever_changing = [f"v{i}" for i in range(expected_polls + 5)]  # never repeats consecutively
        page = self._page(ever_changing)

        _run(canva_renderer._wait_for_page_content_to_stabilize(
            page, "v-initial", max_extra_wait_ms=max_extra_wait_ms,
        ))

        self.assertEqual(page.evaluate.call_count, expected_polls)
        self.assertEqual(page.wait_for_timeout.call_count, expected_polls)

    def test_no_initial_fingerprint_reads_one_fresh_before_polling(self):
        # render_kitt_page_async's own scroll-step loop has no prior
        # signature_after to reuse (unlike render_canva_page_async's/
        # render_pitch_page_async's own pagination loop) - omitting
        # initial_fingerprint must read one itself first, THEN poll,
        # rather than treating the omission as "already stable".
        page = self._page(["fresh", "fresh"])

        _run(canva_renderer._wait_for_page_content_to_stabilize(page))

        # One evaluate() for the initial read, one more for the single
        # poll that confirms it (both return "fresh" - stable immediately).
        self.assertEqual(page.evaluate.call_count, 2)
        self.assertEqual(page.wait_for_timeout.call_count, 1)

    def test_never_raises_when_the_page_cannot_be_read_at_all(self):
        # _page_content_fingerprint's own "" fallback on any evaluate()
        # failure degrades safely here too - two "" reads in a row
        # (the first poll differs from the real, non-empty initial
        # fingerprint, but the second matches the first "" and stops
        # there) is correct: nothing further polling could do would help
        # a page that isn't readable at all.
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        page.evaluate = AsyncMock(side_effect=Exception("evaluate failed"))

        _run(canva_renderer._wait_for_page_content_to_stabilize(page, "v0"))

        self.assertEqual(page.wait_for_timeout.call_count, 2)


def _make_async_page(
    final_url="https://www.canva.com/design/x/y/view",
    goto_side_effect=None,
    goto_response_status=200,
    screenshots=(b"\x89PNG fake",),
    next_disabled_sequence=("true",),
    next_button_raises=False,
    next_button_count=1,
    go_to_page_text="1 / 1",
    go_to_page_raises=True,
    click_advances_page=True,
    page_links=None,
):
    """
    A MagicMock shaped like an async Playwright Page - every method
    Playwright's own async API defines as a coroutine is an AsyncMock;
    the two plain (non-async) setters stay ordinary MagicMock attributes,
    matching the real API exactly (see canva_renderer.app's own comment on
    this - confirmed directly against the installed playwright package).

    `screenshots` is consumed in order, one per page.screenshot() call - the
    FIRST is always the cover-page screenshot; each further one corresponds
    to one successful Next-page loop iteration (see `next_disabled_sequence`
    below), so callers must supply exactly as many as the sequence implies.

    `next_disabled_sequence` mocks the "Next page" button's own aria-
    disabled attribute across successive render_canva_page_async loop
    iterations - "true" stops the loop (single-page default: stops after
    just the cover page, matching every existing pre-multi-page test's own
    expectations unchanged); None/"false" lets it click through to another
    page. Exhausting the sequence keeps returning "true" (a safety net, not
    something any real test here should actually rely on hitting).
    `next_button_raises` simulates the button not existing at all (a Canva
    frontend change, or a genuinely single-page design with no pagination
    UI) - the loop stops on the very first attempt either way.

    `go_to_page_text`/`go_to_page_raises` mock the accessible "Go to page"
    total-page-count indicator used only for diagnostics (_detect_page_
    count) AND, when NOT raising, as the PREFERRED page-advance-
    verification signal (see canva_renderer.app._page_advance_signature) -
    raising by default so most tests don't need to care about either.
    `go_to_page_text` supplies the TOTAL half of "N / M"; the CURRENT half
    is always read live from this page's own advance state below, so a
    test exercising real pagination sees a genuinely changing indicator
    (e.g. "1 / 3" -> "2 / 3"), never a value frozen at whatever string was
    passed in.

    `click_advances_page` (default True, matching every pre-existing
    test's own implicit assumption that a click just works) controls
    whether the DEFAULT "Next page" click - and any custom click side
    effect that calls back into it via `real_click`, see existing tests'
    own "_flaky_click" pattern - advances this page's own shared advance-
    verification state (both the live "Go to page" current half above
    AND the content-fingerprint fallback _page_content_fingerprint reads
    via page.evaluate). Set False to simulate the real production
    "debounced no-op" symptom this verification exists to catch: a click
    that Playwright reports as completed, yet nothing about the visible
    page actually changed.

    `next_button_count` mocks the "Next page" locator's own .count() -
    used only by the post-cap "is there genuinely more beyond
    MAX_CANVA_PAGES" check (0 simulates the button having been removed
    from the DOM entirely, e.g. a design whose last page coincides with
    the cap).

    `page_links` mocks _page_link_candidates' own page.evaluate(
    _PAGE_LINK_CANDIDATES_JS) call - a list of per-page link-candidate
    lists, cycled the same way `screenshots` is (so a test exercising
    several pages can supply exactly as many entries, or fewer and let it
    repeat). Defaults to an empty list of links for every page, matching
    every pre-existing test's own implicit assumption that link data is
    irrelevant to it. page.evaluate is itself shared with the completely
    UNRELATED _page_content_fingerprint call the advance-verification
    logic below also makes (see `advance_state`) - both go through this
    SAME mocked method, so _evaluate below dispatches on the script text
    itself to answer with the right one, never a single fixed value for
    every call regardless of which script was actually passed.
    """
    page = MagicMock()
    page.url = final_url
    page.set_default_navigation_timeout = MagicMock()
    page.set_default_timeout = MagicMock()
    page.route = AsyncMock()
    # A real Playwright navigation Response, not a bare MagicMock() whose
    # own .status would itself be an unrelated auto-generated mock -
    # render_canva_page_async checks this status explicitly (see its own
    # docstring on why: a dead/expired Canva link still loads successfully
    # as Canva's own 404 error page). goto_response_status defaults to 200
    # so every existing test's implicit "navigation succeeded" assumption
    # keeps holding without needing to know about this at all.
    page.goto = (
        AsyncMock(side_effect=goto_side_effect) if goto_side_effect
        else AsyncMock(return_value=MagicMock(status=goto_response_status))
    )
    page.close = AsyncMock()
    cookie_locator = MagicMock()
    cookie_locator.click = AsyncMock(side_effect=Exception("no cookie banner"))
    page.get_by_text = MagicMock(return_value=cookie_locator)
    page.wait_for_timeout = AsyncMock()

    # itertools.cycle, not a one-shot iter - several tests below (sequential/
    # concurrent renders) deliberately reuse the SAME mocked page across
    # multiple independent render_canva_page calls (mirroring context.
    # new_page() returning the same page every time in these tests), so
    # screenshot()/get_attribute() must keep serving values indefinitely
    # rather than raising StopIteration on a second, unrelated render.
    import itertools
    screenshot_iter = itertools.cycle(screenshots)

    async def _screenshot(*args, **kwargs):
        return next(screenshot_iter)

    page.screenshot = AsyncMock(side_effect=_screenshot)

    # Shared "current page" advance state - a pure counter, read (never
    # mutated) by page.evaluate/the "Go to page" indicator below, and
    # advanced ONLY by a successful default click (see next_button.click
    # below) when click_advances_page is True. Reading it any number of
    # times without an intervening successful click correctly reports
    # "unchanged", exactly like a real Canva viewer's own DOM would.
    advance_state = {"page": 1}
    link_candidates_iter = itertools.cycle(page_links if page_links is not None else [[]])

    async def _evaluate(script):
        if script == canva_renderer._PAGE_LINK_CANDIDATES_JS:
            return next(link_candidates_iter)
        return f"content-page-{advance_state['page']}"

    page.evaluate = AsyncMock(side_effect=_evaluate)

    next_button = MagicMock()
    next_button.count = AsyncMock(return_value=next_button_count)
    if next_button_raises:
        next_button.get_attribute = AsyncMock(side_effect=Exception("Next page button not found"))
    else:
        disabled_iter = itertools.cycle(next_disabled_sequence)

        async def _get_attribute(_name, timeout=None):
            return next(disabled_iter)

        next_button.get_attribute = AsyncMock(side_effect=_get_attribute)

    async def _default_click(*args, **kwargs):
        if click_advances_page:
            advance_state["page"] += 1

    next_button.click = AsyncMock(side_effect=_default_click)

    go_to_page_button = MagicMock()
    if go_to_page_raises:
        go_to_page_button.inner_text = AsyncMock(side_effect=Exception("Go to page button not found"))
    else:
        _match = canva_renderer._PAGE_COUNT_RE.search(go_to_page_text)
        _total_text = _match.group(2) if _match else go_to_page_text

        async def _inner_text(timeout=None):
            return f"{advance_state['page']} / {_total_text}"

        go_to_page_button.inner_text = AsyncMock(side_effect=_inner_text)

    def _get_by_role(role, name=None, **kwargs):
        if name == "Next page":
            return next_button
        if name == "Go to page":
            return go_to_page_button
        return MagicMock()

    page.get_by_role = MagicMock(side_effect=_get_by_role)
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


def _make_async_pitch_page(
    final_url="https://pitch.com/v/1-finsbury-brochure-4jnj9d",
    goto_side_effect=None,
    goto_response_status=200,
    screenshots=(b"\x89PNG fake",),
    next_disabled_sequence=(True,),
    next_button_raises=False,
    next_button_count=1,
    click_advances_page=True,
    page_links=None,
    email_gate=False,
):
    """
    Pitch's own counterpart to _make_async_page (see that fixture's own
    docstring for the full shape this mirrors, including `page_links`) -
    the one real difference is next_disabled_sequence's own values: a
    plain bool (True = the `disabled` attribute is present, False =
    absent/None) rather than Canva's own literal "true"/"false" ARIA
    string values, matching render_pitch_page_async's own `get_attribute(
    "disabled", ...) is not None` check (never Canva's own `== "true"`
    string comparison).

    `email_gate=True` makes the body-innerText read (the SAME page.
    evaluate call both _page_shows_email_gate and _page_content_
    fingerprint use - see render_pitch_page_async's own docstring on the
    real GPE managed-link case this covers) return real gate wording
    instead of the default "content-page-N" fingerprint text, so a test
    can exercise the gate-detection branch without needing its own
    separate mock plumbing.
    """
    page = MagicMock()
    page.url = final_url
    page.set_default_navigation_timeout = MagicMock()
    page.set_default_timeout = MagicMock()
    page.route = AsyncMock()
    page.goto = (
        AsyncMock(side_effect=goto_side_effect) if goto_side_effect
        else AsyncMock(return_value=MagicMock(status=goto_response_status))
    )
    page.close = AsyncMock()
    cookie_locator = MagicMock()
    cookie_locator.click = AsyncMock(side_effect=Exception("no cookie banner"))
    page.get_by_text = MagicMock(return_value=cookie_locator)
    page.wait_for_timeout = AsyncMock()

    import itertools
    screenshot_iter = itertools.cycle(screenshots)

    async def _screenshot(*args, **kwargs):
        return next(screenshot_iter)

    page.screenshot = AsyncMock(side_effect=_screenshot)

    advance_state = {"page": 1}
    link_candidates_iter = itertools.cycle(page_links if page_links is not None else [[]])

    async def _evaluate(script):
        if script == canva_renderer._PAGE_LINK_CANDIDATES_JS:
            return next(link_candidates_iter)
        if email_gate:
            return "This presentation requires you to enter an email to open"
        return f"content-page-{advance_state['page']}"

    page.evaluate = AsyncMock(side_effect=_evaluate)

    next_button = MagicMock()
    next_button.count = AsyncMock(return_value=next_button_count)
    if next_button_raises:
        next_button.get_attribute = AsyncMock(side_effect=Exception("Next button not found"))
    else:
        disabled_iter = itertools.cycle(next_disabled_sequence)

        async def _get_attribute(name, timeout=None):
            # Mirrors the real HTML boolean attribute: present (an empty
            # string, never None) when disabled, absent (None) otherwise -
            # never a literal "true"/"false" string the way Canva's own
            # aria-disabled is.
            return "" if next(disabled_iter) else None

        next_button.get_attribute = AsyncMock(side_effect=_get_attribute)

    async def _default_click(*args, **kwargs):
        if click_advances_page:
            advance_state["page"] += 1

    next_button.click = AsyncMock(side_effect=_default_click)

    def _get_by_role(role, name=None, **kwargs):
        if name == "Next":
            return next_button
        return MagicMock()

    page.get_by_role = MagicMock(side_effect=_get_by_role)
    return page


def _make_async_kitt_page(
    final_url="https://brochures.kittoffices.com/brochures/preview?entity%5B9e40cdea-02a1-44a5-9599-"
              "c3ed1567c117%5D=unit&display_label=Open+brochure",
    goto_side_effect=None,
    goto_response_status=200,
    screenshots=(b"\x89PNG fake",),
    scroll_metrics=(3072, 1536),
    page_links=None,
):
    """
    Kitt's own counterpart to _make_async_page/_make_async_pitch_page -
    but for a genuinely different capture mechanism (see render_kitt_
    page_async's own docstring): no "Next"/"Next page" button at all,
    scroll-position-based instead.

    `scroll_metrics` is (scrollHeight, clientHeight) of Kitt's own nested
    scroll container, as read by _kitt_scroll_metrics - defaults to
    exactly TWO clientHeight-sized chunks (3072 / 1536), matching the
    real shape confirmed via live recon. Pass None to simulate the scroll
    container not being found at all (see render_kitt_page_async's own
    single-screenshot fallback for that case).

    Exposes `page._scroll_offsets_used` (a plain list, populated as the
    render loop runs) - the sequence of scrollTop offsets render_kitt_
    page_async actually requested, so a test can assert on it directly
    rather than reverse-engineering it from screenshot call counts alone.
    """
    page = MagicMock()
    page.url = final_url
    page.set_default_navigation_timeout = MagicMock()
    page.set_default_timeout = MagicMock()
    page.route = AsyncMock()
    page.goto = (
        AsyncMock(side_effect=goto_side_effect) if goto_side_effect
        else AsyncMock(return_value=MagicMock(status=goto_response_status))
    )
    page.close = AsyncMock()
    cookie_locator = MagicMock()
    cookie_locator.click = AsyncMock(side_effect=Exception("no cookie banner"))
    page.get_by_text = MagicMock(return_value=cookie_locator)
    page.wait_for_timeout = AsyncMock()

    import itertools
    screenshot_iter = itertools.cycle(screenshots)

    async def _screenshot(*args, **kwargs):
        return next(screenshot_iter)

    page.screenshot = AsyncMock(side_effect=_screenshot)

    link_candidates_iter = itertools.cycle(page_links if page_links is not None else [[]])
    scroll_offsets_used = []

    async def _evaluate(script, *args):
        if script == canva_renderer._PAGE_LINK_CANDIDATES_JS:
            return next(link_candidates_iter)
        if "scrollTop = y" in script:
            if args:
                scroll_offsets_used.append(args[0])
            return None
        if "scrollHeight" in script:
            if scroll_metrics is None:
                return None
            height, client_height = scroll_metrics
            return {"scrollHeight": height, "clientHeight": client_height}
        return None

    page.evaluate = AsyncMock(side_effect=_evaluate)
    page._scroll_offsets_used = scroll_offsets_used
    return page


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
        page = _make_async_page(screenshots=(b"\x89PNG real bytes",))
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, detected_total = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(pages, [b"\x89PNG real bytes"])
        self.assertIsNone(detected_total)
        context.close.assert_awaited_once()  # resources cleaned up after the request

    def test_initial_navigation_waits_for_load_not_networkidle(self):
        # Canva is a heavy SPA with persistent background network activity
        # (websockets, polling, analytics beacons) - "networkidle" can wait
        # out the ENTIRE navigation timeout even after the design has
        # already rendered, a real confirmed contributor to production
        # navigation timeouts. "load" fires once the page's own resources
        # are in, without waiting on unrelated ongoing network traffic.
        page = _make_async_page(screenshots=(b"\x89PNG real bytes",))
        patcher, _ = self._patch_browser(page)
        with patcher:
            _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(page.goto.call_args.kwargs["wait_until"], "load")
        self.assertEqual(page.goto.call_args.kwargs["timeout"], canva_renderer.NAV_TIMEOUT_MS)

    def test_next_page_click_uses_its_own_dedicated_timeout_not_nav_timeout(self):
        # Regression guard: a "Next page" click on an already-loaded, warm
        # design has no reason to need the same generous budget the COLD
        # initial navigation does - see NEXT_PAGE_CLICK_TIMEOUT_MS's own
        # docstring for why these two are deliberately decoupled.
        self.assertNotEqual(canva_renderer.NAV_TIMEOUT_MS, canva_renderer.NEXT_PAGE_CLICK_TIMEOUT_MS)
        page = _make_async_page(
            screenshots=(b"\x89PNG p1", b"\x89PNG p2"),
            next_disabled_sequence=(None, "true"),
        )
        next_button = page.get_by_role("button", name="Next page")
        patcher, _ = self._patch_browser(page)
        with patcher:
            _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        next_button.click.assert_awaited_once_with(timeout=canva_renderer.NEXT_PAGE_CLICK_TIMEOUT_MS)

    def test_single_page_design_with_no_pagination_ui_returns_one_page(self):
        # No "Next page" control at all - a genuinely single-page design,
        # or a future Canva frontend change; either way this must degrade
        # to exactly the one-page behavior this service always had before
        # multi-page capture existed, never a hard failure.
        page = _make_async_page(screenshots=(b"\x89PNG only page",), next_button_raises=True)
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, _ = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(pages, [b"\x89PNG only page"])
        context.close.assert_awaited_once()

    def test_multi_page_design_captures_every_page_in_order(self):
        page = _make_async_page(
            screenshots=(b"\x89PNG p1", b"\x89PNG p2", b"\x89PNG p3"),
            next_disabled_sequence=(None, None, "true"),
            go_to_page_text="1 / 3",
            go_to_page_raises=False,
        )
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, detected_total = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(pages, [b"\x89PNG p1", b"\x89PNG p2", b"\x89PNG p3"])
        self.assertEqual(detected_total, 3)
        context.close.assert_awaited_once()

    def test_page_limit_is_enforced_even_if_next_button_never_reports_disabled(self):
        # A malformed/huge design whose Next-page button never reports
        # aria-disabled="true" must still stop at MAX_CANVA_PAGES, never
        # loop unboundedly.
        screenshots = tuple(f"\x89PNG p{i}".encode() for i in range(1, 25))
        page = _make_async_page(
            screenshots=screenshots,
            next_disabled_sequence=[None] * 30,  # "never disabled"
        )
        patcher, context = self._patch_browser(page)
        with patch.object(canva_renderer, "MAX_CANVA_PAGES", 5), patcher:
            pages, _, _ = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(len(pages), 5)
        context.close.assert_awaited_once()

    def test_hitting_the_page_cap_with_next_button_still_available_logs_a_capped_warning(self):
        # Real production evidence: a genuine 29-page brochure silently
        # truncated to MAX_CANVA_PAGES=20 with nothing anywhere (log or
        # response) distinguishing it from a normal, complete render. If
        # the "Next page" control is STILL present and enabled right when
        # the cap is hit, there is genuinely more beyond it - this must be
        # logged distinctly from every other (clean) pagination stop.
        screenshots = tuple(f"\x89PNG p{i}".encode() for i in range(1, 4))
        page = _make_async_page(
            screenshots=screenshots,
            next_disabled_sequence=("false",),  # never reports disabled
            next_button_count=1,  # still present at the cap
        )
        buf = io.StringIO()
        patcher, context = self._patch_browser(page)
        with patch.object(canva_renderer, "MAX_CANVA_PAGES", 3), patcher, contextlib.redirect_stderr(buf):
            pages, _, _ = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(len(pages), 3)
        stderr_output = buf.getvalue()
        self.assertIn("Pagination capped", stderr_output)
        self.assertIn("may have more pages than were captured", stderr_output)
        context.close.assert_awaited_once()

    def test_hitting_the_page_cap_on_the_designs_genuinely_last_page_logs_no_false_warning(self):
        # The false-positive this guards against: a design whose page
        # count exactly EQUALS MAX_CANVA_PAGES is NOT truncated at all -
        # its "Next page" control has genuinely become disabled right as
        # the cap is reached, so no "may have more pages" warning should
        # ever be logged for it.
        screenshots = tuple(f"\x89PNG p{i}".encode() for i in range(1, 4))
        page = _make_async_page(
            screenshots=screenshots,
            # First two checks (before each successful click) report "not
            # disabled" so pagination proceeds to fill the cap; the THIRD
            # check - the post-cap "is there really more?" check - reports
            # "true", i.e. this genuinely was the design's last page.
            next_disabled_sequence=("false", "false", "true"),
            next_button_count=1,
        )
        buf = io.StringIO()
        patcher, context = self._patch_browser(page)
        with patch.object(canva_renderer, "MAX_CANVA_PAGES", 3), patcher, contextlib.redirect_stderr(buf):
            pages, _, _ = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(len(pages), 3)
        stderr_output = buf.getvalue()
        self.assertNotIn("Pagination capped", stderr_output)
        self.assertNotIn("may have more pages than were captured", stderr_output)
        context.close.assert_awaited_once()

    def test_hitting_the_page_cap_with_next_button_removed_logs_no_false_warning(self):
        # A second way the false-positive could show up: the "Next page"
        # control disappears from the DOM entirely (count() == 0) right as
        # the cap is reached, rather than staying present with aria-
        # disabled="true". Must be treated the same as a genuine last
        # page - no warning.
        screenshots = tuple(f"\x89PNG p{i}".encode() for i in range(1, 4))
        page = _make_async_page(
            screenshots=screenshots,
            next_disabled_sequence=("false",),
            next_button_count=0,  # gone by the time the cap is hit
        )
        buf = io.StringIO()
        patcher, context = self._patch_browser(page)
        with patch.object(canva_renderer, "MAX_CANVA_PAGES", 3), patcher, contextlib.redirect_stderr(buf):
            pages, _, _ = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(len(pages), 3)
        stderr_output = buf.getvalue()
        self.assertNotIn("Pagination capped", stderr_output)
        self.assertNotIn("may have more pages than were captured", stderr_output)
        context.close.assert_awaited_once()

    def test_a_later_page_failure_still_returns_the_pages_already_captured(self):
        # Page 3's own click/screenshot fails partway through a multi-page
        # capture - this must never lose pages 1-2 already safely captured,
        # and must never escalate into a RenderError for the whole request.
        page = _make_async_page(
            screenshots=(b"\x89PNG p1", b"\x89PNG p2"),
            next_disabled_sequence=(None, None, "true"),
        )
        call_count = {"n": 0}
        real_click = page.get_by_role("button", name="Next page").click

        async def _flaky_click(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise Exception("page 3 click timed out")
            return await real_click(*args, **kwargs)

        page.get_by_role("button", name="Next page").click = AsyncMock(side_effect=_flaky_click)
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, _ = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(pages, [b"\x89PNG p1", b"\x89PNG p2"])
        context.close.assert_awaited_once()

    def test_transient_click_failure_recovers_via_retry_with_a_fresh_locator(self):
        # The real production symptom this fixes: a "Next page" click
        # whose own actionability checks reported the element visible,
        # enabled, and stable, yet the click itself still timed out once -
        # a bounded retry with a freshly re-acquired locator must ride
        # through a single transient failure like this, never losing the
        # page it was trying to reach.
        page = _make_async_page(
            screenshots=(b"\x89PNG p1", b"\x89PNG p2", b"\x89PNG p3"),
            next_disabled_sequence=(None, None, "true"),
        )
        call_count = {"n": 0}
        real_click = page.get_by_role("button", name="Next page").click

        async def _flaky_click(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:  # only the FIRST attempt at the 2nd transition fails
                raise Exception("Timeout 15000ms exceeded")
            return await real_click(*args, **kwargs)

        page.get_by_role("button", name="Next page").click = AsyncMock(side_effect=_flaky_click)
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, _ = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(pages, [b"\x89PNG p1", b"\x89PNG p2", b"\x89PNG p3"])
        self.assertEqual(call_count["n"], 3)  # one failed attempt + one successful retry
        context.close.assert_awaited_once()

    def test_repeated_click_failure_gives_up_after_the_bounded_retry_and_keeps_partial_pages(self):
        # Every attempt at the 2nd transition fails (not just one) - must
        # give up after MAX_NEXT_CLICK_ATTEMPTS, never retry forever, while
        # still returning the pages already captured before that point.
        page = _make_async_page(
            screenshots=(b"\x89PNG p1", b"\x89PNG p2"),
            next_disabled_sequence=(None, None, "true"),
        )
        next_button = page.get_by_role("button", name="Next page")
        next_button.click = AsyncMock(side_effect=Exception("Timeout 15000ms exceeded"))
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, _ = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(pages, [b"\x89PNG p1"])
        self.assertEqual(next_button.click.await_count, canva_renderer.MAX_NEXT_CLICK_ATTEMPTS)
        context.close.assert_awaited_once()

    def test_pagination_retries_when_a_click_succeeds_but_the_page_does_not_advance(self):
        # The real production symptom Task 3 closes: a "Next page" click
        # that Playwright reports as completed (never raises) yet the
        # visible page never actually changed - a debounced no-op. Must
        # be retried via the SAME bounded mechanism as a raising click,
        # and recovers once a later attempt genuinely advances.
        page = _make_async_page(
            screenshots=(b"\x89PNG p1", b"\x89PNG p2"),
            next_disabled_sequence=(None, "true"),
        )
        call_count = {"n": 0}
        real_click = page.get_by_role("button", name="Next page").click

        async def _debounced_then_real_click(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return None  # "succeeds" per Playwright, but nothing changes
            return await real_click(*args, **kwargs)

        page.get_by_role("button", name="Next page").click = AsyncMock(side_effect=_debounced_then_real_click)
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, _ = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(pages, [b"\x89PNG p1", b"\x89PNG p2"])
        self.assertEqual(call_count["n"], 2)  # one no-op "success" + one real advance
        context.close.assert_awaited_once()

    def test_pagination_gives_up_when_the_click_never_actually_advances_the_page(self):
        # Every attempt "succeeds" (never raises) but the page never
        # changes - must still give up after the bounded retry, exactly
        # like a raising click would, never an infinite loop, and never
        # counted as a captured page.
        page = _make_async_page(
            screenshots=(b"\x89PNG p1",),
            next_disabled_sequence=(None,),
            click_advances_page=False,
        )
        next_button = page.get_by_role("button", name="Next page")
        buf = io.StringIO()
        patcher, context = self._patch_browser(page)
        with patcher, contextlib.redirect_stderr(buf):
            pages, _, _ = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(pages, [b"\x89PNG p1"])  # never advanced past the cover
        self.assertEqual(next_button.click.await_count, canva_renderer.MAX_NEXT_CLICK_ATTEMPTS)
        context.close.assert_awaited_once()
        logged = buf.getvalue()
        self.assertIn("did not advance", logged)
        self.assertIn("attempt", logged)
        self.assertIn("gave up after 3 attempts", logged)

    def test_page_advance_verification_uses_the_go_to_page_indicator_when_available(self):
        # Integration-level confirmation that a design WITH a readable
        # "Go to page" indicator drives verification off it correctly
        # end to end, not just in the PageAdvanceVerificationTests unit
        # tests above.
        page = _make_async_page(
            screenshots=(b"\x89PNG p1", b"\x89PNG p2"),
            next_disabled_sequence=(None, "true"),
            go_to_page_text="1 / 2",
            go_to_page_raises=False,
        )
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, detected_total = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(pages, [b"\x89PNG p1", b"\x89PNG p2"])
        self.assertEqual(detected_total, 2)
        context.close.assert_awaited_once()

    def test_page_is_closed_before_the_context_on_a_successful_multi_page_render(self):
        page = _make_async_page(
            screenshots=(b"\x89PNG p1", b"\x89PNG p2"),
            next_disabled_sequence=(None, "true"),
        )
        patcher, context = self._patch_browser(page)
        with patcher:
            _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        page.close.assert_awaited_once()
        context.close.assert_awaited_once()

    def test_navigation_timeout_raises_render_error_and_still_cleans_up(self):
        page = _make_async_page(goto_side_effect=Exception("Timeout 15000ms exceeded"))
        patcher, context = self._patch_browser(page)
        with patcher:
            with self.assertRaises(canva_renderer.RenderError):
                _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))
        context.close.assert_awaited_once()

    def test_a_404_navigation_response_raises_render_error_and_captures_no_pages(self):
        # Real production evidence this fixes: a dead/expired/access-
        # revoked Canva link still loads successfully AS FAR AS
        # PLAYWRIGHT IS CONCERNED - it's Canva's own "Looks like we hit a
        # roadblock" 404 error page, confirmed directly against three
        # real production URLs (both canva.link short links and a plain
        # www.canva.com/design/.../view URL) - so without this check, that
        # error page's own screenshot was silently captured and reported
        # as a normal "1 page(s) captured" success.
        page = _make_async_page(goto_response_status=404)
        patcher, context = self._patch_browser(page)
        with patcher:
            with self.assertRaises(canva_renderer.RenderError) as ctx:
                _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertIn("404", ctx.exception.reason)
        page.screenshot.assert_not_called()  # never even tried to capture the error page
        context.close.assert_awaited_once()

    def test_a_200_navigation_response_is_unaffected_regression_guard(self):
        # Regression guard for the 404 check above - a genuinely
        # successful navigation (the overwhelming common case) must be
        # completely unaffected by it.
        page = _make_async_page(
            goto_response_status=200,
            screenshots=(b"\x89PNG p1", b"\x89PNG p2"),
            next_disabled_sequence=(None, "true"),
        )
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, _ = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(pages, [b"\x89PNG p1", b"\x89PNG p2"])
        context.close.assert_awaited_once()

    def test_aria_disabled_presence_check_uses_its_own_bounded_timeout(self):
        # Regression guard: the outer "does a Next-page control exist at
        # all" check must use its OWN short, explicit timeout - never
        # silently inherit page.set_default_timeout's own NAV_TIMEOUT_MS
        # (30s). Real production evidence this fixes: a genuinely single-
        # page design (or a dead link - see the 404 test above) has no
        # such control at all, so Playwright's own auto-wait would
        # otherwise poll for the full 30s, holding this request's own
        # Chromium context open the whole time, before this constant
        # existed.
        self.assertLess(canva_renderer._NEXT_BUTTON_PRESENCE_TIMEOUT_MS, canva_renderer.NAV_TIMEOUT_MS)
        page = _make_async_page(screenshots=(b"\x89PNG only page",))
        next_button = page.get_by_role("button", name="Next page")
        patcher, _ = self._patch_browser(page)
        with patcher:
            _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        next_button.get_attribute.assert_awaited_once_with(
            "aria-disabled", timeout=canva_renderer._NEXT_BUTTON_PRESENCE_TIMEOUT_MS,
        )

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


class PageLinkCandidatesTests(unittest.TestCase):
    """Unit tests for _page_link_candidates in isolation - no browser/
    render loop involved, just the DOM-eval wrapper's own contract."""

    def test_returns_whatever_the_dom_eval_yields(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value=[{"href": "https://example.com/a.pdf", "text": "Link"}])

        result = _run(canva_renderer._page_link_candidates(page))

        self.assertEqual(result, [{"href": "https://example.com/a.pdf", "text": "Link"}])
        page.evaluate.assert_awaited_once_with(canva_renderer._PAGE_LINK_CANDIDATES_JS)

    def test_a_failed_dom_eval_returns_an_empty_list_not_a_raise(self):
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=Exception("evaluate failed"))

        result = _run(canva_renderer._page_link_candidates(page))

        self.assertEqual(result, [])


class RenderCanvaPageAsyncLinkCaptureTests(_ResetGlobalBrowserStateTestCase):
    """render_canva_page_async's own new third return value - link data
    captured alongside each screenshot, never in place of it."""

    def _patch_browser(self, page):
        context = _make_async_context(page)
        browser = _make_async_browser(context)
        return patch.object(canva_renderer, "_get_browser_async", AsyncMock(return_value=browser))

    def test_single_page_link_data_is_returned_alongside_the_screenshot(self):
        page_one_links = [{"href": "https://colliers.com/kingsland-house", "text": "LINK TO BROCHURE"}]
        page = _make_async_page(screenshots=(b"\x89PNG p1",), page_links=[page_one_links])
        with self._patch_browser(page):
            pages, page_links, _ = _run(
                canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view")
            )

        self.assertEqual(pages, [b"\x89PNG p1"])
        self.assertEqual(page_links, [page_one_links])

    def test_multi_page_link_data_stays_aligned_with_its_own_page(self):
        links_p1 = [{"href": "https://example.com/shared.pdf", "text": "Brochure"}]
        links_p2 = [{"href": "https://blob.example.com/gloucester.pdf", "text": "27-29 Gloucester Place"}]
        links_p3 = []  # a page with genuinely no links at all
        page = _make_async_page(
            screenshots=(b"\x89PNG p1", b"\x89PNG p2", b"\x89PNG p3"),
            next_disabled_sequence=("false", "false", "true"),
            page_links=[links_p1, links_p2, links_p3],
        )
        with self._patch_browser(page):
            pages, page_links, _ = _run(
                canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view")
            )

        self.assertEqual(len(pages), 3)
        self.assertEqual(page_links, [links_p1, links_p2, links_p3])

    def test_default_page_links_is_an_empty_list_per_page_when_unspecified(self):
        # Every pre-existing test in this file relies on this default -
        # confirms it explicitly rather than only implicitly via those.
        page = _make_async_page(screenshots=(b"\x89PNG p1", b"\x89PNG p2"), next_disabled_sequence=("false", "true"))
        with self._patch_browser(page):
            pages, page_links, _ = _run(
                canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view")
            )

        self.assertEqual(page_links, [[], []])


class RenderPitchPageAsyncTests(_ResetGlobalBrowserStateTestCase):
    """Unit-level tests against render_pitch_page_async directly - mirrors
    RenderCanvaPageAsyncTests' own coverage for the shared loop shape, plus
    the two genuine differences confirmed via recon (see that function's
    own docstring): the `disabled` HTML attribute instead of aria-disabled,
    and the "Next" button name instead of "Next page"."""

    def _patch_browser(self, page):
        context = _make_async_context(page)
        browser = _make_async_browser(context)
        return patch.object(canva_renderer, "_get_browser_async", AsyncMock(return_value=browser)), context

    def test_non_pitch_url_raises_before_touching_the_browser(self):
        mock_get_browser = AsyncMock()
        with patch.object(canva_renderer, "_get_browser_async", mock_get_browser):
            with self.assertRaises(canva_renderer.RenderError):
                _run(canva_renderer.render_pitch_page_async("https://example.com/x"))
        mock_get_browser.assert_not_called()

    def test_successful_render_returns_png_bytes(self):
        page = _make_async_pitch_page(screenshots=(b"\x89PNG real bytes",))
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, detected_total = _run(
                canva_renderer.render_pitch_page_async("https://pitch.com/v/1-finsbury-brochure-4jnj9d")
            )

        self.assertEqual(pages, [b"\x89PNG real bytes"])
        self.assertIsNone(detected_total)
        context.close.assert_awaited_once()

    def test_initial_navigation_waits_for_networkidle_not_load(self):
        # The one deliberate divergence from Canva - see render_pitch_
        # page_async's own docstring point 1 for why (Pitch's own initial
        # HTML is a genuinely empty shell with no persistent background
        # traffic problem the way Canva's heavy SPA has).
        page = _make_async_pitch_page(screenshots=(b"\x89PNG real bytes",))
        patcher, _ = self._patch_browser(page)
        with patcher:
            _run(canva_renderer.render_pitch_page_async("https://pitch.com/v/1-finsbury-brochure-4jnj9d"))

        self.assertEqual(page.goto.call_args.kwargs["wait_until"], "networkidle")

    def test_single_page_deck_with_no_pagination_ui_returns_one_page(self):
        page = _make_async_pitch_page(screenshots=(b"\x89PNG only page",), next_button_raises=True)
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, _ = _run(
                canva_renderer.render_pitch_page_async("https://pitch.com/v/1-finsbury-brochure-4jnj9d")
            )

        self.assertEqual(pages, [b"\x89PNG only page"])
        context.close.assert_awaited_once()

    def test_multi_page_deck_captures_every_page_in_order(self):
        page = _make_async_pitch_page(
            screenshots=(b"\x89PNG p1", b"\x89PNG p2", b"\x89PNG p3"),
            next_disabled_sequence=(False, False, True),
        )
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, _ = _run(
                canva_renderer.render_pitch_page_async("https://pitch.com/v/1-finsbury-brochure-4jnj9d")
            )

        self.assertEqual(pages, [b"\x89PNG p1", b"\x89PNG p2", b"\x89PNG p3"])
        context.close.assert_awaited_once()

    def test_disabled_attribute_present_as_empty_string_still_stops_the_loop(self):
        # The real, confirmed HTML boolean attribute semantics (see recon):
        # `disabled=""` (present, even with an empty value) means disabled -
        # never Canva's own literal aria-disabled="true" STRING comparison.
        # _make_async_pitch_page's own get_attribute fake already returns
        # "" (not None) for a disabled state - this test exists specifically
        # to pin that "" must be treated as disabled, not accidentally
        # falsy-compared against a bare boolean the way a lazy `if value:`
        # check would get wrong (`if "":` is falsy in Python).
        page = _make_async_pitch_page(screenshots=(b"\x89PNG p1",), next_disabled_sequence=(True,))
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, _ = _run(
                canva_renderer.render_pitch_page_async("https://pitch.com/v/1-finsbury-brochure-4jnj9d")
            )

        self.assertEqual(len(pages), 1)
        context.close.assert_awaited_once()

    def test_page_limit_is_enforced_even_if_next_button_never_reports_disabled(self):
        screenshots = tuple(f"\x89PNG p{i}".encode() for i in range(1, 8))
        page = _make_async_pitch_page(screenshots=screenshots, next_disabled_sequence=[False] * 10)
        patcher, context = self._patch_browser(page)
        with patch.object(canva_renderer, "MAX_PITCH_PAGES", 5), patcher:
            pages, _, _ = _run(
                canva_renderer.render_pitch_page_async("https://pitch.com/v/1-finsbury-brochure-4jnj9d")
            )

        self.assertEqual(len(pages), 5)
        context.close.assert_awaited_once()

    def test_navigation_failure_raises_render_error(self):
        page = _make_async_pitch_page(goto_side_effect=Exception("Timeout 30000ms exceeded"))
        patcher, context = self._patch_browser(page)
        with patcher:
            with self.assertRaises(canva_renderer.RenderError):
                _run(canva_renderer.render_pitch_page_async("https://pitch.com/v/1-finsbury-brochure-4jnj9d"))

    def test_dead_link_status_raises_render_error_with_navigation_returned_http_marker(self):
        # Same marker text as Canva's own identical check - the main app's
        # _is_confirmed_dead_canva_link matches on this literal substring
        # regardless of which platform produced it (see render_pitch_page_
        # async's own docstring).
        page = _make_async_pitch_page(goto_response_status=404)
        patcher, _ = self._patch_browser(page)
        with patcher:
            with self.assertRaises(canva_renderer.RenderError) as ctx:
                _run(canva_renderer.render_pitch_page_async("https://pitch.com/v/1-finsbury-brochure-4jnj9d"))
        self.assertIn("navigation returned HTTP", str(ctx.exception))

    def test_link_data_is_captured_alongside_the_screenshot(self):
        page_links = [{"href": "https://example.com/deck.pdf", "text": "View deck"}]
        page = _make_async_pitch_page(screenshots=(b"\x89PNG p1",), page_links=[page_links])
        patcher, _ = self._patch_browser(page)
        with patcher:
            pages, links, _ = _run(
                canva_renderer.render_pitch_page_async("https://pitch.com/v/1-finsbury-brochure-4jnj9d")
            )

        self.assertEqual(pages, [b"\x89PNG p1"])
        self.assertEqual(links, [page_links])

    def test_gpe_flipbook_url_is_accepted_by_the_same_pitch_render_function(self):
        # fm.gpe.co.uk is confirmed to be Pitch's own player on GPE's own
        # branded domain (see this function's own docstring) - a
        # recognized GPE URL must be accepted here directly, never
        # rejected as "not a recognized public Pitch URL".
        page = _make_async_pitch_page(
            final_url="https://fm.gpe.co.uk/v/gpe-nineteen-wells-street-6hqnfd",
            screenshots=(b"\x89PNG real bytes",),
        )
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, _ = _run(
                canva_renderer.render_pitch_page_async("https://fm.gpe.co.uk/v/gpe-nineteen-wells-street-6hqnfd")
            )

        self.assertEqual(pages, [b"\x89PNG real bytes"])
        context.close.assert_awaited_once()

    def test_email_gated_document_raises_render_error_instead_of_a_false_success(self):
        # Real confirmed case (see this function's own docstring): a
        # Pitch Managed Link with its owner's email-capture gate enabled
        # returns real HTTP 200 and has no "Next"/"Previous" controls at
        # all - before this check existed, that would have been silently
        # captured as a normal one-page "successful" render (the gate
        # screen itself handed to Gemini as if it were brochure content).
        page = _make_async_pitch_page(email_gate=True, next_button_raises=True)
        patcher, context = self._patch_browser(page)
        with patcher:
            with self.assertRaises(canva_renderer.RenderError) as ctx:
                _run(canva_renderer.render_pitch_page_async("https://pitch.com/v/1-finsbury-brochure-4jnj9d"))

        self.assertIn("email", str(ctx.exception).lower())
        page.screenshot.assert_not_called()
        context.close.assert_awaited_once()

    def test_email_gate_check_also_applies_to_a_gpe_flipbook_url(self):
        # Checked in the SHARED function, not a GPE special case - applies
        # identically regardless of which URL shape got here.
        page = _make_async_pitch_page(email_gate=True, next_button_raises=True)
        patcher, _ = self._patch_browser(page)
        with patcher:
            with self.assertRaises(canva_renderer.RenderError):
                _run(canva_renderer.render_pitch_page_async("https://fm.gpe.co.uk/v/gpe-nineteen-wells-street-6hqnfd"))

    def test_a_normal_non_gated_render_is_unaffected_by_the_gate_check(self):
        # The gate check must never produce a false positive on an
        # ordinary deck's own real content.
        page = _make_async_pitch_page(email_gate=False, screenshots=(b"\x89PNG real bytes",))
        patcher, _ = self._patch_browser(page)
        with patcher:
            pages, _, _ = _run(
                canva_renderer.render_pitch_page_async("https://pitch.com/v/1-finsbury-brochure-4jnj9d")
            )

        self.assertEqual(pages, [b"\x89PNG real bytes"])


class RenderKittPageAsyncTests(_ResetGlobalBrowserStateTestCase):
    """Unit-level tests against render_kitt_page_async directly - the
    scroll-position-based capture logic (see that function's own
    docstring), genuinely different from Canva's/Pitch's own click-based
    pagination."""

    _URL = (
        "https://brochures.kittoffices.com/brochures/preview?entity%5B9e40cdea-02a1-44a5-9599-"
        "c3ed1567c117%5D=unit&display_label=Open+brochure"
    )

    def _patch_browser(self, page):
        context = _make_async_context(page)
        browser = _make_async_browser(context)
        return patch.object(canva_renderer, "_get_browser_async", AsyncMock(return_value=browser)), context

    def test_non_kitt_url_raises_before_touching_the_browser(self):
        mock_get_browser = AsyncMock()
        with patch.object(canva_renderer, "_get_browser_async", mock_get_browser):
            with self.assertRaises(canva_renderer.RenderError):
                _run(canva_renderer.render_kitt_page_async("https://example.com/x"))
        mock_get_browser.assert_not_called()

    def test_initial_navigation_waits_for_networkidle_not_load(self):
        # Kitt's own initial HTML is a genuinely empty Next.js shell (see
        # render_kitt_page_async's own docstring) - same reasoning as
        # Pitch's own choice, the opposite of Canva's.
        page = _make_async_kitt_page(screenshots=(b"\x89PNG p1", b"\x89PNG p2"))
        patcher, _ = self._patch_browser(page)
        with patcher:
            _run(canva_renderer.render_kitt_page_async(self._URL))

        self.assertEqual(page.goto.call_args.kwargs["wait_until"], "networkidle")
        self.assertEqual(page.goto.call_args.kwargs["timeout"], canva_renderer.NAV_TIMEOUT_MS)

    def test_dead_link_http_status_raises_render_error(self):
        page = _make_async_kitt_page(goto_response_status=404)
        patcher, context = self._patch_browser(page)
        with patcher:
            with self.assertRaises(canva_renderer.RenderError) as ctx:
                _run(canva_renderer.render_kitt_page_async(self._URL))

        self.assertIn("HTTP 404", str(ctx.exception))
        context.close.assert_awaited_once()

    def test_navigation_leaving_the_allowed_host_raises(self):
        page = _make_async_kitt_page(final_url="https://evil.example.com/redirected")
        patcher, _ = self._patch_browser(page)
        with patcher:
            with self.assertRaises(canva_renderer.RenderError) as ctx:
                _run(canva_renderer.render_kitt_page_async(self._URL))

        self.assertIn("host", str(ctx.exception).lower())

    def test_short_single_chunk_page_returns_exactly_one_page(self):
        # scrollHeight <= clientHeight - the whole page fits in one
        # screenshot, no further scrolling needed.
        page = _make_async_kitt_page(screenshots=(b"\x89PNG only chunk",), scroll_metrics=(1200, 1536))
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, detected_total = _run(canva_renderer.render_kitt_page_async(self._URL))

        self.assertEqual(pages, [b"\x89PNG only chunk"])
        self.assertIsNone(detected_total)
        self.assertEqual(page._scroll_offsets_used, [0])
        context.close.assert_awaited_once()

    def test_multi_chunk_page_captures_every_scroll_position_in_order(self):
        # Real confirmed shape (33 Cavendish Square): scrollHeight=10752,
        # clientHeight=1536 -> exactly 7 chunks. Scaled down here to 3 for
        # a readable test.
        screenshots = (b"\x89PNG chunk1", b"\x89PNG chunk2", b"\x89PNG chunk3")
        page = _make_async_kitt_page(screenshots=screenshots, scroll_metrics=(4608, 1536))
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, page_links, _ = _run(canva_renderer.render_kitt_page_async(self._URL))

        self.assertEqual(pages, list(screenshots))
        self.assertEqual(page._scroll_offsets_used, [0, 1536, 3072])
        self.assertEqual(len(page_links), 3)
        context.close.assert_awaited_once()

    def test_no_scroll_container_falls_back_to_a_single_plain_screenshot(self):
        # A page shape this hasn't seen before (see render_kitt_page_
        # async's own docstring) - must still return something real
        # rather than failing outright.
        page = _make_async_kitt_page(screenshots=(b"\x89PNG fallback",), scroll_metrics=None)
        patcher, context = self._patch_browser(page)
        with patcher:
            pages, _, detected_total = _run(canva_renderer.render_kitt_page_async(self._URL))

        self.assertEqual(pages, [b"\x89PNG fallback"])
        self.assertIsNone(detected_total)
        self.assertEqual(page._scroll_offsets_used, [])  # never entered the scroll loop at all
        context.close.assert_awaited_once()

    def test_page_cap_is_enforced_even_when_scroll_height_implies_more(self):
        # A malformed/huge preview must still stop at MAX_KITT_PAGES,
        # never loop unboundedly - same reasoning as Canva's/Pitch's own
        # page caps.
        screenshots = tuple(f"\x89PNG p{i}".encode() for i in range(1, 10))
        page = _make_async_kitt_page(screenshots=screenshots, scroll_metrics=(1_000_000, 1536))
        patcher, context = self._patch_browser(page)
        buf = io.StringIO()
        with patch.object(canva_renderer, "MAX_KITT_PAGES", 4), patcher, contextlib.redirect_stderr(buf):
            pages, _, _ = _run(canva_renderer.render_kitt_page_async(self._URL))

        self.assertEqual(len(pages), 4)
        self.assertIn("Kitt pagination capped", buf.getvalue())
        context.close.assert_awaited_once()

    def test_hitting_the_cap_on_the_pages_genuinely_last_chunk_logs_no_false_warning(self):
        # scrollHeight is an EXACT multiple of clientHeight matching the
        # cap - the loop's own natural break fires on the last iteration,
        # so no "capped" warning should be logged.
        screenshots = (b"\x89PNG p1", b"\x89PNG p2", b"\x89PNG p3")
        page = _make_async_kitt_page(screenshots=screenshots, scroll_metrics=(4608, 1536))
        patcher, context = self._patch_browser(page)
        buf = io.StringIO()
        with patch.object(canva_renderer, "MAX_KITT_PAGES", 3), patcher, contextlib.redirect_stderr(buf):
            pages, _, _ = _run(canva_renderer.render_kitt_page_async(self._URL))

        self.assertEqual(len(pages), 3)
        self.assertNotIn("Kitt pagination capped", buf.getvalue())
        context.close.assert_awaited_once()


class RenderPageDispatchTests(unittest.TestCase):
    """render_page - the one call site Handler.do_POST uses, dispatching
    to whichever platform's own renderer a URL shape recognizes."""

    def test_canva_url_dispatches_to_render_canva_page(self):
        with patch.object(canva_renderer, "render_canva_page", return_value=([b"x"], [], 1)) as mock_canva, \
             patch.object(canva_renderer, "render_pitch_page") as mock_pitch:
            result = canva_renderer.render_page("https://www.canva.com/design/x/y/view")

        self.assertEqual(result, ([b"x"], [], 1))
        mock_canva.assert_called_once_with("https://www.canva.com/design/x/y/view")
        mock_pitch.assert_not_called()

    def test_pitch_url_dispatches_to_render_pitch_page(self):
        with patch.object(canva_renderer, "render_canva_page") as mock_canva, \
             patch.object(canva_renderer, "render_pitch_page", return_value=([b"x"], [], 1)) as mock_pitch:
            result = canva_renderer.render_page("https://pitch.com/v/1-finsbury-brochure-4jnj9d")

        self.assertEqual(result, ([b"x"], [], 1))
        mock_pitch.assert_called_once_with("https://pitch.com/v/1-finsbury-brochure-4jnj9d")
        mock_canva.assert_not_called()

    def test_unrecognized_url_raises_render_error_without_calling_either(self):
        with patch.object(canva_renderer, "render_canva_page") as mock_canva, \
             patch.object(canva_renderer, "render_pitch_page") as mock_pitch:
            with self.assertRaises(canva_renderer.RenderError):
                canva_renderer.render_page("https://example.com/brochure.pdf")

        mock_canva.assert_not_called()
        mock_pitch.assert_not_called()

    def test_gpe_flipbook_url_dispatches_to_the_same_render_pitch_page(self):
        # Confirmed to be the identical underlying Pitch player (see
        # _GPE_FLIPBOOK_VIEW_URL_RE's own docstring) - routed into the
        # SAME render_pitch_page, never a separate render function.
        with patch.object(canva_renderer, "render_canva_page") as mock_canva, \
             patch.object(canva_renderer, "render_pitch_page", return_value=([b"x"], [], 1)) as mock_pitch:
            result = canva_renderer.render_page("https://fm.gpe.co.uk/v/gpe-nineteen-wells-street-6hqnfd")

        self.assertEqual(result, ([b"x"], [], 1))
        mock_pitch.assert_called_once_with("https://fm.gpe.co.uk/v/gpe-nineteen-wells-street-6hqnfd")
        mock_canva.assert_not_called()

    def test_kitt_url_dispatches_to_its_own_render_kitt_page(self):
        # Confirmed NOT to be Canva or Pitch under the hood (see
        # render_kitt_page_async's own docstring) - routed into its OWN
        # dedicated render function, never one of the other two.
        url = (
            "https://brochures.kittoffices.com/brochures/preview?entity%5B9e40cdea-02a1-44a5-9599-"
            "c3ed1567c117%5D=unit&display_label=Open+brochure"
        )
        with patch.object(canva_renderer, "render_canva_page") as mock_canva, \
             patch.object(canva_renderer, "render_pitch_page") as mock_pitch, \
             patch.object(canva_renderer, "render_kitt_page", return_value=([b"x"], [], None)) as mock_kitt:
            result = canva_renderer.render_page(url)

        self.assertEqual(result, ([b"x"], [], None))
        mock_kitt.assert_called_once_with(url)
        mock_canva.assert_not_called()
        mock_pitch.assert_not_called()


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
        page = _make_async_page(screenshots=(b"\x89PNG single",))
        with self._patch_browser(page):
            pages, _, _ = canva_renderer.render_canva_page("https://www.canva.com/design/x/y/view")
        self.assertEqual(pages, [b"\x89PNG single"])

    def test_sequential_renders_all_succeed(self):
        page = _make_async_page(screenshots=(b"\x89PNG seq",))
        with self._patch_browser(page):
            results = [
                canva_renderer.render_canva_page("https://www.canva.com/design/x/y/view") for _ in range(5)
            ]
        self.assertEqual([pages for pages, _, _ in results], [[b"\x89PNG seq"]] * 5)

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
        page = _make_async_page(screenshots=(b"\x89PNG concurrent",))
        with self._patch_browser(page):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futures = [
                    pool.submit(canva_renderer.render_canva_page, "https://www.canva.com/design/x/y/view")
                    for _ in range(8)
                ]
                results = [f.result(timeout=10) for f in futures]

        self.assertEqual([pages for pages, _, _ in results], [[b"\x89PNG concurrent"]] * 8)

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


class ReencodeAsJpegTests(unittest.TestCase):
    """
    _reencode_as_jpeg - a real Pillow round-trip (no mocking), confirming
    it actually produces valid, smaller JPEG bytes from a real PNG, not
    just that it returns SOMETHING. Only ever called from do_POST when a
    render's estimated payload risks Cloud Run's own 32MB response limit
    (see RenderHandlerTests' own adaptive-re-encoding tests).
    """

    @staticmethod
    def _make_real_png(size=(200, 200), color=(255, 0, 0)) -> bytes:
        buffer = BytesIO()
        Image.new("RGB", size, color).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_produces_real_decodable_jpeg_bytes(self):
        png_bytes = self._make_real_png()
        jpeg_bytes = canva_renderer._reencode_as_jpeg(png_bytes)
        self.assertTrue(jpeg_bytes.startswith(b"\xff\xd8\xff"))
        decoded = Image.open(BytesIO(jpeg_bytes))
        self.assertEqual(decoded.format, "JPEG")
        self.assertEqual(decoded.size, (200, 200))

    def test_a_real_photo_like_image_shrinks_meaningfully(self):
        # A flat single-color PNG (the case above) is already tiny under
        # PNG's own lossless compression - not representative of a real,
        # noisy/photo-heavy brochure page, where PNG is a poor fit. Random
        # per-pixel noise is the cheap, offline stand-in that's genuinely
        # hard for PNG to compress, the same way a real photo is.
        random.seed(0)
        width, height = 200, 200
        pixels = bytes(random.randrange(256) for _ in range(width * height * 3))
        image = Image.frombytes("RGB", (width, height), pixels)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        png_bytes = buffer.getvalue()

        jpeg_bytes = canva_renderer._reencode_as_jpeg(png_bytes)

        self.assertLess(len(jpeg_bytes), len(png_bytes))

    def test_quality_uses_the_module_constant(self):
        png_bytes = self._make_real_png()
        fake_image = MagicMock()
        fake_image.convert.return_value = fake_image
        with patch.object(canva_renderer, "JPEG_QUALITY", 42), \
                patch.object(canva_renderer.Image, "open", return_value=fake_image):
            canva_renderer._reencode_as_jpeg(png_bytes)
        save_kwargs = fake_image.save.call_args.kwargs
        self.assertEqual(save_kwargs["quality"], 42)
        self.assertEqual(save_kwargs["format"], "JPEG")


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

    def test_valid_request_returns_bounded_json_page_list(self):
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
        with patch.object(canva_renderer, "render_canva_page", return_value=([b"\x89PNG bytes"], [], 1)):
            handler.do_POST()
        handler.send_response.assert_called_with(200)
        payload = json.loads(handler.wfile.getvalue())
        self.assertEqual(payload["pages"], [base64.b64encode(b"\x89PNG bytes").decode("ascii")])
        self.assertEqual(payload["page_count_detected"], 1)

    def test_response_includes_links_alongside_pages(self):
        # New, additive response field - each page's own real <a href>
        # candidates (see _page_link_candidates), never base64-encoded
        # (plain JSON-serializable dicts already).
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
        pages = [b"\x89PNG p1", b"\x89PNG p2"]
        links = [
            [{"href": "https://colliers.com/kingsland-house", "text": "LINK TO BROCHURE"}],
            [{"href": "https://blob.example.com/gloucester.pdf", "text": "27-29 Gloucester Place"}],
        ]
        with patch.object(canva_renderer, "render_canva_page", return_value=(pages, links, 2)):
            handler.do_POST()
        handler.send_response.assert_called_with(200)
        payload = json.loads(handler.wfile.getvalue())
        self.assertEqual(payload["links"], links)

    def test_valid_request_with_multiple_pages_preserves_order(self):
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
        pages = [b"\x89PNG p1", b"\x89PNG p2", b"\x89PNG p3"]
        with patch.object(canva_renderer, "render_canva_page", return_value=(pages, [], 3)):
            handler.do_POST()
        handler.send_response.assert_called_with(200)
        payload = json.loads(handler.wfile.getvalue())
        self.assertEqual(payload["pages"], [base64.b64encode(p).decode("ascii") for p in pages])

    def test_response_encoding_does_not_mutate_the_source_pages_list(self):
        # Real production evidence this fixes: "Memory limit of 1024 MiB
        # exceeded" while processing a large multi-page brochure. The fix
        # drains a COPY of `pages`, freeing each raw PNG as its own
        # base64 copy is made, rather than holding one full list of raw
        # PNGs AND one full list of base64 strings simultaneously - but it
        # must do that WITHOUT mutating whatever list render_canva_page
        # itself returned, since nothing about that return value's own
        # lifetime is this handler's to assume.
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
        pages = [b"\x89PNG p1", b"\x89PNG p2", b"\x89PNG p3"]
        with patch.object(canva_renderer, "render_canva_page", return_value=(pages, [], 3)):
            handler.do_POST()

        self.assertEqual(pages, [b"\x89PNG p1", b"\x89PNG p2", b"\x89PNG p3"])  # untouched

    def test_response_encoding_is_incremental_not_a_single_comprehension(self):
        # A structural guard, not a literal memory measurement (which
        # isn't meaningfully observable through a mock) - confirms the
        # actual technique is still "drain and free one at a time" and
        # hasn't silently regressed back to building the full base64 list
        # via one comprehension over the full raw-bytes list (which would
        # reintroduce holding both fully in memory at once).
        source = inspect.getsource(canva_renderer.Handler.do_POST)
        self.assertNotIn("[base64.b64encode(p", source)
        self.assertIn(".pop(0)", source)
        self.assertIn("del page_bytes", source)

    def test_small_render_stays_png_and_never_re_encodes(self):
        # The common case (small/medium decks): well under RESPONSE_SIZE_
        # SAFETY_THRESHOLD_BYTES at its real default, so image_format
        # stays "png" and _reencode_as_jpeg is never even called - full
        # lossless quality, completely unchanged from before this feature.
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
        with patch.object(canva_renderer, "render_canva_page", return_value=([b"\x89PNG bytes"], [], 1)), \
                patch.object(canva_renderer, "_reencode_as_jpeg") as mock_reencode:
            handler.do_POST()
        payload = json.loads(handler.wfile.getvalue())
        self.assertEqual(payload["image_format"], "png")
        self.assertEqual(payload["pages"], [base64.b64encode(b"\x89PNG bytes").decode("ascii")])
        mock_reencode.assert_not_called()

    def test_large_estimated_payload_re_encodes_every_page_as_jpeg(self):
        # RESPONSE_SIZE_SAFETY_THRESHOLD_BYTES patched down so even tiny
        # fixture pages cross it, without needing genuinely huge test
        # data here - the real default (24MB) and JPEG_QUALITY (85) were
        # sized against a real, live 29-page Risborough capture (25.68MB
        # raw PNG -> 5.25MB JPEG; the unmodified PNG+base64+JSON payload
        # measured at 34.24MB, over Cloud Run's real 32MB limit) rather
        # than a guess - see canva_renderer/README.md's own "Response
        # size" section and CLOUD_RUN_MAX_RESPONSE_BYTES's own comment.
        pages = [b"\x89PNG p1", b"\x89PNG p2"]
        jpeg_bytes = [b"\xff\xd8\xff jpeg1", b"\xff\xd8\xff jpeg2"]
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
        with patch.object(canva_renderer, "render_canva_page", return_value=(pages, [], 2)), \
                patch.object(canva_renderer, "RESPONSE_SIZE_SAFETY_THRESHOLD_BYTES", 1), \
                patch.object(canva_renderer, "_reencode_as_jpeg", side_effect=jpeg_bytes) as mock_reencode:
            handler.do_POST()
        payload = json.loads(handler.wfile.getvalue())
        self.assertEqual(payload["image_format"], "jpeg")
        self.assertEqual(payload["pages"], [base64.b64encode(p).decode("ascii") for p in jpeg_bytes])
        # Every original PNG page was actually handed to the re-encoder,
        # in order - never skipped, never reordered.
        self.assertEqual([c.args[0] for c in mock_reencode.call_args_list], pages)

    def test_re_encoding_never_mutates_the_original_pages_list(self):
        pages = [b"\x89PNG p1", b"\x89PNG p2"]
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
        with patch.object(canva_renderer, "render_canva_page", return_value=(pages, [], 2)), \
                patch.object(canva_renderer, "RESPONSE_SIZE_SAFETY_THRESHOLD_BYTES", 1), \
                patch.object(canva_renderer, "_reencode_as_jpeg", side_effect=lambda p: b"jpeg-" + p):
            handler.do_POST()
        self.assertEqual(pages, [b"\x89PNG p1", b"\x89PNG p2"])  # untouched

    def test_render_error_returns_422_with_reason(self):
        # A real Canva-shaped URL, not an arbitrary one - do_POST now
        # dispatches through render_page (see that function's own
        # docstring), which picks render_canva_page/render_pitch_page
        # based on the URL's own shape BEFORE either mock below is ever
        # reached; an unrecognized URL would raise render_page's own
        # "not a recognized public Canva or Pitch URL" error first,
        # never exercising this test's own render_canva_page mock at all.
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
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
                patch.object(canva_renderer, "render_canva_page", return_value=([b"\x89PNG bytes"], [], 1)):
            handler.do_POST()
        handler.send_response.assert_called_with(200)

    def test_busy_semaphore_returns_503_without_calling_render(self):
        # SEMAPHORE_WAIT_TIMEOUT_SECONDS patched down to a few ms - a slot
        # that's permanently unavailable (Semaphore(0), never released by
        # anything in this test) must still fail safely well within a real
        # test run, not block for the real (90s-by-default) wait budget.
        import threading
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
        with patch.object(canva_renderer, "_render_semaphore", threading.Semaphore(0)), \
                patch.object(canva_renderer, "SEMAPHORE_WAIT_TIMEOUT_SECONDS", 0.05):
            with patch.object(canva_renderer, "render_canva_page") as mock_render:
                handler.do_POST()
            mock_render.assert_not_called()
            handler.send_response.assert_called_with(503)

    def test_busy_semaphore_503_body_includes_a_reason(self):
        # The main app's own generic reason-extraction (response.json().
        # get("reason", ...)) should never need a special case for this
        # one status code - it carries a "reason" key exactly like 422/500.
        import threading
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
        with patch.object(canva_renderer, "_render_semaphore", threading.Semaphore(0)), \
                patch.object(canva_renderer, "SEMAPHORE_WAIT_TIMEOUT_SECONDS", 0.05):
            handler.do_POST()
        payload = json.loads(handler.wfile.getvalue())
        self.assertIn("reason", payload)
        self.assertTrue(payload["reason"])

    def test_internal_error_returns_500_with_a_safe_reason(self):
        # Regression guard for the real production diagnostic gap this was
        # added to close: an uncaught exception previously surfaced to the
        # main app as a bare "HTTP 500" - now it carries the actual (safety-
        # bounded) exception text, so the main app's own log line can show
        # what really failed instead of just the status code.
        handler = self._make_handler(json.dumps({"url": "https://www.canva.com/design/x/y/view"}).encode())
        with patch.object(
            canva_renderer, "render_canva_page", side_effect=RuntimeError("browser launch failed"),
        ):
            handler.do_POST()
        handler.send_response.assert_called_with(500)
        payload = json.loads(handler.wfile.getvalue())
        self.assertIn("browser launch failed", payload["reason"])

    def test_health_endpoint(self):
        handler = self._make_handler(b"", path="/health")
        handler.do_GET()
        handler.send_response.assert_called_with(200)


class SafeReasonTests(unittest.TestCase):
    """
    _safe_reason is the ONE choke point every RenderError's own reason (see
    RenderError.__init__) and the bare-exception 500 path (see Handler.
    do_POST) both flow through before ever reaching this service's JSON
    response body - these tests exercise it directly rather than through
    a real Playwright failure, matching this file's own existing convention
    of never launching a real browser.
    """

    def test_collapses_multiline_playwright_style_call_log_to_one_line(self):
        raw = (
            "TimeoutError: Timeout 15000ms exceeded.\n"
            "Call log:\n"
            "  - navigating to \"https://www.canva.com/design/x/y/view\"\n"
            "  - waiting until \"networkidle\"\n"
        )
        result = canva_renderer._safe_reason(raw)
        self.assertNotIn("\n", result)

    def test_truncates_long_text_to_the_max_reason_length(self):
        raw = "x" * 5000
        result = canva_renderer._safe_reason(raw)
        self.assertLessEqual(len(result), canva_renderer._MAX_REASON_LENGTH + 1)  # +1 for the trailing ellipsis char
        self.assertTrue(result.endswith("…"))

    def test_short_reason_is_returned_unchanged(self):
        self.assertEqual(canva_renderer._safe_reason("navigation timed out"), "navigation timed out")

    def test_shared_secret_is_redacted_if_present_in_the_text(self):
        with patch.object(canva_renderer, "SHARED_SECRET", "supersecretvalue123"):
            result = canva_renderer._safe_reason("failed while calling with token supersecretvalue123 attached")
        self.assertNotIn("supersecretvalue123", result)
        self.assertIn("[redacted]", result)

    def test_render_error_reason_is_sanitized_at_construction_time(self):
        long_multiline = "line one\nline two\n" + ("z" * 500)
        err = canva_renderer.RenderError(long_multiline)
        self.assertNotIn("\n", err.reason)
        self.assertLessEqual(len(err.reason), canva_renderer._MAX_REASON_LENGTH + 1)


class SemaphoreQueueingTests(unittest.TestCase):
    """
    Regression tests for the real confirmed production bug: MAX_CONCURRENT_
    RENDERS=2 combined with a non-blocking semaphore acquire meant the 3rd+
    simultaneous /render request was rejected with an INSTANT 503, even
    though a slot would free up moments later - independent of whether that
    specific design would have rendered fine (confirmed directly: a real
    failing production URL rendered perfectly when tried in isolation, with
    zero contention). A bulk spreadsheet upload with many Canva-linked rows
    genuinely dispatches several brochures concurrently (see brochure_
    enrichment.enrich_rows_grouped's own worker pool), so this was the real
    mechanism behind "some Canva properties enrich, most don't".

    Exercises Handler.do_POST directly with REAL threads and a REAL
    threading.Semaphore (never render_canva_page_async, which has no
    concept of the concurrency cap at all - that lives here, at the HTTP
    layer) - matching this file's own existing convention for exercising
    real threading/concurrency behavior rather than mocking it away.
    """

    def _make_handler(self, url="https://www.canva.com/design/x/y/view"):
        body = json.dumps({"url": url}).encode()
        handler = canva_renderer.Handler.__new__(canva_renderer.Handler)
        handler.path = "/render"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def test_a_burst_beyond_max_concurrent_renders_queues_instead_of_instant_503(self):
        release_first = threading.Event()
        call_order = []
        order_lock = threading.Lock()

        def slow_then_fast_render(url):
            with order_lock:
                is_first = not call_order
                call_order.append("started")
            if is_first:
                release_first.wait(timeout=5)
            return ([b"\x89PNG bytes"], [], 1)

        handler_a = self._make_handler()
        handler_b = self._make_handler()

        with patch.object(canva_renderer, "_render_semaphore", threading.Semaphore(1)), \
                patch.object(canva_renderer, "SEMAPHORE_WAIT_TIMEOUT_SECONDS", 5), \
                patch.object(canva_renderer, "render_canva_page", side_effect=slow_then_fast_render):
            thread_a = threading.Thread(target=handler_a.do_POST)
            thread_a.start()
            time.sleep(0.15)  # let A acquire the one slot first

            thread_b = threading.Thread(target=handler_b.do_POST)
            thread_b.start()
            time.sleep(0.15)
            # B must be genuinely BLOCKED waiting for the slot right now,
            # never already rejected - the real bug returned 503 here
            # immediately instead of waiting at all.
            self.assertEqual(len(call_order), 1)

            release_first.set()
            thread_a.join(timeout=5)
            thread_b.join(timeout=5)

        handler_a.send_response.assert_called_with(200)
        handler_b.send_response.assert_called_with(200)  # NOT 503 - it queued and succeeded

    def test_a_request_that_never_gets_a_slot_within_the_wait_budget_still_fails_safely(self):
        # The wait is a firm ceiling, not unbounded queueing - a slot that
        # genuinely never frees up must still produce a safe 503, never
        # hang the caller forever.
        handler = self._make_handler()
        with patch.object(canva_renderer, "_render_semaphore", threading.Semaphore(0)), \
                patch.object(canva_renderer, "SEMAPHORE_WAIT_TIMEOUT_SECONDS", 0.2), \
                patch.object(canva_renderer, "render_canva_page") as mock_render:
            handler.do_POST()

        mock_render.assert_not_called()
        handler.send_response.assert_called_with(503)

    def test_render_failure_does_not_poison_the_next_render(self):
        # "One failed render poisoning subsequent renders" - a RenderError
        # on request A must still release the semaphore (see do_POST's own
        # finally block, unchanged by this fix) so request B, right after,
        # gets its own real chance rather than inheriting A's failure or
        # finding the slot still held.
        call_log = []

        def fail_then_succeed(url):
            call_log.append(url)
            if len(call_log) == 1:
                raise canva_renderer.RenderError("navigation timed out")
            return ([b"\x89PNG bytes"], [], 1)

        handler_a = self._make_handler()
        handler_b = self._make_handler()
        with patch.object(canva_renderer, "_render_semaphore", threading.Semaphore(1)), \
                patch.object(canva_renderer, "render_canva_page", side_effect=fail_then_succeed):
            handler_a.do_POST()
            handler_b.do_POST()

        handler_a.send_response.assert_called_with(422)
        handler_b.send_response.assert_called_with(200)
        self.assertEqual(len(call_log), 2)

    def test_semaphore_wait_is_not_charged_against_the_actual_render_call(self):
        # RENDER_TIMEOUT_SECONDS (the render's own budget - see render_
        # canva_page's own future.result(timeout=RENDER_TIMEOUT_SECONDS))
        # is only ever applied to the render_canva_page_async call ITSELF,
        # which do_POST only ever invokes AFTER the semaphore is already
        # acquired - so a request that spent most of SEMAPHORE_WAIT_
        # TIMEOUT_SECONDS merely queued for a slot must still succeed
        # once it gets one, even with a render budget far smaller than
        # the time it spent waiting. If the two were ever conflated (one
        # combined clock started at request-arrival), this would fail.
        handler_a = self._make_handler()
        handler_b = self._make_handler()
        release_first = threading.Event()

        def a_holds_the_slot(url):
            release_first.wait(timeout=5)
            return ([b"\x89PNG a"], [], 1)

        with patch.object(canva_renderer, "_render_semaphore", threading.Semaphore(1)), \
                patch.object(canva_renderer, "SEMAPHORE_WAIT_TIMEOUT_SECONDS", 5), \
                patch.object(canva_renderer, "RENDER_TIMEOUT_SECONDS", 0.05), \
                patch.object(canva_renderer, "render_canva_page", side_effect=a_holds_the_slot):
            thread_a = threading.Thread(target=handler_a.do_POST)
            thread_a.start()
            time.sleep(0.2)  # A now holds the only slot

            # B queues behind A for ~0.3s - far longer than the 0.05s
            # RENDER_TIMEOUT_SECONDS patched above - then, once it
            # acquires, its OWN render_canva_page call (the SAME mock,
            # which returns instantly once release_first is already set)
            # must still be allowed to run and succeed, proving the wait
            # itself was never charged against that 0.05s render budget.
            thread_b = threading.Thread(target=handler_b.do_POST)
            thread_b.start()
            time.sleep(0.3)
            release_first.set()
            thread_a.join(timeout=5)
            thread_b.join(timeout=5)

        handler_a.send_response.assert_called_with(200)
        handler_b.send_response.assert_called_with(200)


class BrowserCrashRecoveryTests(_ResetGlobalBrowserStateTestCase):
    """
    Regression tests for the other real risk multi-request load exposes: if
    the single cached Chromium browser crashes or is OOM-killed mid-load,
    every subsequent request must recover on its own, not keep trying to
    use the same dead Browser object for the rest of this container
    instance's lifetime.
    """

    def test_disconnected_browser_is_relaunched_on_next_request(self):
        crashed_browser = MagicMock()
        crashed_browser.is_connected = MagicMock(return_value=False)
        canva_renderer._browser = crashed_browser
        canva_renderer._playwright_ctx = MagicMock()
        canva_renderer._playwright_ctx.stop = AsyncMock()

        page = _make_async_page(screenshots=(b"\x89PNG recovered",))
        context = _make_async_context(page)
        fresh_browser = _make_async_browser(context)
        starter = MagicMock()
        starter.start = AsyncMock(return_value=MagicMock(chromium=MagicMock(launch=AsyncMock(return_value=fresh_browser))))

        with patch.object(canva_renderer, "async_playwright", MagicMock(return_value=starter)):
            pages, _, _ = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(pages, [b"\x89PNG recovered"])
        starter.start.assert_awaited_once()  # relaunched exactly once
        self.assertIs(canva_renderer._browser, fresh_browser)  # cache updated, never left pointing at the dead one

    def test_a_healthy_connected_browser_is_never_relaunched(self):
        page = _make_async_page(screenshots=(b"\x89PNG healthy",))
        context = _make_async_context(page)
        healthy_browser = _make_async_browser(context)
        healthy_browser.is_connected = MagicMock(return_value=True)
        canva_renderer._browser = healthy_browser
        canva_renderer._playwright_ctx = MagicMock()

        with patch.object(canva_renderer, "async_playwright") as mock_playwright:
            pages, _, _ = _run(canva_renderer.render_canva_page_async("https://www.canva.com/design/x/y/view"))

        self.assertEqual(pages, [b"\x89PNG healthy"])
        mock_playwright.assert_not_called()  # never even touched async_playwright() - no relaunch needed


class MaxPagesCapsStayInSyncTests(unittest.TestCase):
    """
    This service's own MAX_CANVA_PAGES and the main app's independent,
    separately-maintained brochure_enrichment._CANVA_MAX_PAGES_ACCEPTED
    (defense-in-depth against a misbehaving/compromised renderer response
    - see that constant's own docstring) MUST be raised together. Real
    incident this guards against: a genuine 29-page brochure (Risborough)
    had its contact info on page 29, lost by the old MAX_CANVA_PAGES=20
    cap - raising ONLY the renderer's own cap while leaving the main
    app's _CANVA_MAX_PAGES_ACCEPTED at its old value would silently
    truncate the very same page right back out on the OTHER side of the
    two services' own boundary, undoing the fix without either service's
    own test suite ever catching it (each only ever asserts its own
    constant in isolation).
    """

    def test_renderer_and_main_app_page_caps_are_equal(self):
        self.assertEqual(
            canva_renderer.MAX_CANVA_PAGES,
            brochure_enrichment._CANVA_MAX_PAGES_ACCEPTED,
            "canva_renderer.MAX_CANVA_PAGES and brochure_enrichment._CANVA_MAX_PAGES_ACCEPTED "
            "must be raised together - a mismatch means the main app silently truncates pages "
            "the renderer was just raised to capture.",
        )

    def test_pitch_renderer_and_main_app_page_caps_are_equal(self):
        # Same pairing requirement, for Pitch's own independent cap pair -
        # see brochure_enrichment._PITCH_MAX_PAGES_ACCEPTED's own docstring.
        self.assertEqual(
            canva_renderer.MAX_PITCH_PAGES,
            brochure_enrichment._PITCH_MAX_PAGES_ACCEPTED,
            "canva_renderer.MAX_PITCH_PAGES and brochure_enrichment._PITCH_MAX_PAGES_ACCEPTED "
            "must be raised together - a mismatch means the main app silently truncates pages "
            "the renderer was just raised to capture.",
        )

    def test_kitt_renderer_and_main_app_page_caps_are_equal(self):
        # Same pairing requirement, for Kitt's own independent cap pair -
        # see brochure_enrichment._KITT_MAX_PAGES_ACCEPTED's own docstring.
        self.assertEqual(
            canva_renderer.MAX_KITT_PAGES,
            brochure_enrichment._KITT_MAX_PAGES_ACCEPTED,
            "canva_renderer.MAX_KITT_PAGES and brochure_enrichment._KITT_MAX_PAGES_ACCEPTED "
            "must be raised together - a mismatch means the main app silently truncates pages "
            "the renderer was just raised to capture.",
        )


if __name__ == "__main__":
    unittest.main()
