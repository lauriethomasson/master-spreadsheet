"""
brochure_link_resolver.py

finalize_brochure_link is the entry point extract.py/extract_email.py call
per unit, applying the full brochure_link priority order (see its
docstring): a genuine per-unit link, resolved through one landing-page hop
if needed (confirmed: GPE, whose per-property link is a landing page
containing the real brochure PDF's link, not the PDF itself) > discard
anything generic (a bare company homepage with no listing-specific path) >
default to the uploaded PDF's own persisted-file URL (or its bare filename,
in local-disk dev mode where there's nothing to persist it to) > null for
emails.

resolve_brochure_link never discards a working link on its own - on any
failure (network error, timeout, no document link found), the original URL
is kept as-is, since a working landing page is still better than nothing.
"""

import functools
import json
import re
import sys
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

REQUEST_TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (compatible; MasterSpreadsheetBot/1.0)"

# Matched against both the link text and the href itself, case-insensitively.
# "brochure" is checked separately from the rest since a landing page often links
# to several unrelated PDFs (annual reports, ESG strategy docs, etc.) alongside
# the one that's actually the property brochure - text/href literally saying
# "brochure" is a much stronger signal than a bare .pdf extension or "download".
OTHER_LINK_KEYWORDS = ("download", "floorplan", "floor plan")


def _clean_path(href: str) -> str:
    return href.lower().split("?")[0].split("#")[0]


def _normalize_url(url: str) -> str:
    """Add an https:// scheme if the URL is missing one entirely - seen from
    Gemini occasionally (e.g. 'gpe.co.uk/portfolio/city-tower' with no
    protocol). A bare domain/path string isn't fetchable, and may not even
    render as a clickable link once written out to Excel.
    """
    if not urlparse(url).scheme:
        return f"https://{url}"
    return url


# Social/professional profile platforms that turn up in every email signature
# block (company LinkedIn page, RICS membership badge, etc.) - never a
# brochure host, so treated as generic regardless of path, to avoid wasting
# a fetch confirming what's already obvious.
KNOWN_NON_BROCHURE_DOMAINS = (
    "linkedin.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "rics.org",
)


# Checked against a candidate URL's own text only (never a fetch) - a floor
# plan is a genuinely different document from a brochure regardless of what
# it turns out to contain, so its own URL/link-text shape is already enough
# to rule it out, without spending a network round trip to confirm it. Also
# reused by brochure_enrichment.py's own pre-fetch eligibility filter, so
# both places share one definition rather than two independently-drifting
# keyword lists.
FLOORPLAN_URL_KEYWORDS = ("floorplan", "floor-plan", "floor plan")


def is_floorplan_not_brochure_url(url: str) -> bool:
    """
    True when `url`'s own text (URL or filename) clearly identifies it as a
    floor plan AND gives no competing signal that it's also (or instead) a
    brochure - see FLOORPLAN_URL_KEYWORDS. A URL/filename that mentions BOTH
    (e.g. ".../Building-Brochure-and-Floorplans.pdf" - a real, common
    combined-document naming pattern) is deliberately left alone here:
    discarding a genuine brochure link just because its own name also
    happens to mention floor plans would be exactly the wrong side of
    "incorrect enrichment is worse than a blank field" to err on, when the
    safer reading of an ambiguous name is to keep the link rather than null
    out a real one. Never a fetch - matched against the URL's own text only,
    same as FLOORPLAN_URL_KEYWORDS' other use sites.
    """
    if not url:
        return False
    lowered = url.lower()
    return "brochure" not in lowered and any(kw in lowered for kw in FLOORPLAN_URL_KEYWORDS)


# Matches specifically the real Canva public-share "view" URL shape (e.g.
# "https://www.canva.com/design/DAGzsWW-Yp8/s8tPVTQe6HUQa939xX0XQw/view?...
# #7") - deliberately narrow (requires /design/{id}/{share-token}/view, not
# a bare "canva.com" substring) so an unrelated canva.com URL (a user
# profile, a template gallery page, canva.com used as a generic homepage)
# never enters this check. Confirmed by directly fetching the exact real
# example URL above with a plain, unauthenticated GET (no browser/JS
# execution, no cookies) - Canva's server itself returns HTTP 200 but an
# "Unsupported client — Canva" HTML shell (noindex, no design content, no
# embedded document/PDF/image reference of any kind at all) rather than the
# real design - i.e. Canva actively distinguishes a real browser from a
# plain HTTP client and never serves usable content to the latter, for a
# genuinely public design or otherwise. There is no stable, documented,
# unauthenticated mechanism a plain HTTP client could use to obtain real
# PDF/page content from a link shaped like this. A URL matching this shape
# is still treated as a known-unsupported link type by default (never
# attempted as a fetchable document at all - see is_canva_view_link's own
# call sites in brochure_enrichment.py's classify_link_eligibility) UNLESS
# a separate, isolated browser-rendering service has been deployed and
# configured (see canva_renderer/README.md and brochure_enrichment.
# _canva_renderer_configured) - real headless Chromium rendering DOES
# obtain the actual design content correctly (confirmed directly), it is
# just never run inside THIS app's own process/container, for the reasons
# that service's own README explains.
_CANVA_VIEW_URL_RE = re.compile(
    r"^https?://(?:[\w-]+\.)*canva\.com/design/[^/\s]+/[^/\s]+/view(?:[/?#].*)?$", re.IGNORECASE
)

# Canva's own short-link redirector for a shared design (e.g.
# "https://canva.link/45k34aansogxr2a") - confirmed a REAL, common shape
# across dozens of real links in this project's own Workplace Company
# fixture, every one of which redirects straight to a real canva.com/
# design/{id}/{token}/view URL (the exact shape _CANVA_VIEW_URL_RE
# matches). Missing this shape was a real, confirmed gap: is_canva_view_
# link previously only matched the fully-expanded canva.com URL, so a
# short link never even got detected as Canva at all - it fell through to
# the ordinary generic fetch/landing-page-scan path (see resolve_brochure_
# link), which follows the redirect via a plain httpx GET and hits the
# exact same "Unsupported client" shell _CANVA_VIEW_URL_RE's own docstring
# describes, just reported as a generic fetch failure rather than being
# routed to the Canva renderer (see canva_renderer/) when one is
# configured. The renderer's own app.py recognizes this exact shape too
# (see its own _CANVA_SHORT_LINK_RE) - a real headless browser follows the
# redirect itself and lands on the real design, same as any other
# navigation.
_CANVA_SHORT_LINK_RE = re.compile(r"^https?://canva\.link/[^/\s?#]+(?:[/?#].*)?$", re.IGNORECASE)


def is_canva_view_link(url: str) -> bool:
    """True for a real Canva public-share "view" link, OR a Canva short
    link that redirects to one (see _CANVA_SHORT_LINK_RE's own docstring)
    - see _CANVA_VIEW_URL_RE's own docstring for why either shape is
    unsupported by default, and how canva_renderer/ opts a deployment into
    real support. Never a fetch - matched against the URL's own text only,
    same as is_floorplan_not_brochure_url's other use sites."""
    if not url:
        return False
    url = url.strip()
    return bool(_CANVA_VIEW_URL_RE.match(url)) or bool(_CANVA_SHORT_LINK_RE.match(url))


# A Pitch.com public "view" link (e.g. "https://pitch.com/v/1-finsbury-
# brochure-4jnj9d") - real, confirmed shape used by GPE, Knotel, and
# MetSpace to share brochures/availability decks. Same category of
# problem as Canva's own "view" link (see _CANVA_VIEW_URL_RE's own
# docstring): a plain HTTP GET returns only an empty client-side-rendered
# shell (Pitch's own SPA, confirmed directly via a throwaway Playwright
# recon script - no login/email gate, real content renders correctly in
# a real browser), never the actual content. canva_renderer/ (see that
# service's own README/module docstring) now handles Pitch too, reusing
# the exact same architecture it already had for Canva.
_PITCH_VIEW_URL_RE = re.compile(r"^https?://(?:[\w-]+\.)*pitch\.com/v/[^/\s?#]+(?:[/?#].*)?$", re.IGNORECASE)


def is_pitch_view_link(url: str) -> bool:
    """True for a real Pitch.com public-share "view" link - see
    _PITCH_VIEW_URL_RE's own docstring for why this is unsupported by
    default, and how canva_renderer/ opts a deployment into real support.
    Never a fetch - matched against the URL's own text only, same as
    is_canva_view_link."""
    if not url:
        return False
    return bool(_PITCH_VIEW_URL_RE.match(url.strip()))


def looks_like_url(value) -> bool:
    """
    True only when `value` is genuinely shaped like a URL, as opposed to a
    placeholder a provider uses to mean "no brochure yet" - "TBC", "Coming
    Soon", "N/A", "None", "-", or blank. This is a narrower, EARLIER gate
    than is_generic_link (which assumes its input already IS a real URL and
    only asks whether it's merely a bare, non-listing-specific homepage) -
    is_generic_link alone doesn't reliably catch every placeholder shape
    (e.g. "N/A" normalizes to "https://N/A", parsed as netloc="N",
    path="/A" - a non-empty path, so is_generic_link would NOT flag it).

    True for anything with an explicit http(s):// scheme already - a real
    hyperlink target (e.g. openpyxl's cell.hyperlink.target, or a URL
    Gemini extracted from actual document text) always has one, and text
    this explicit is never a placeholder. Otherwise requires a domain-
    shaped string with no whitespace and a dotted, letters-only suffix,
    optionally followed by a port and/or a path/query/fragment (e.g.
    "app.box.com/s/abc123", "app.box.com:8443/s/abc123",
    "example.com?ref=1", matching _normalize_url's own "add a scheme if
    missing" treatment elsewhere in this module) - a placeholder like
    "TBC"/"N/A"/"-"/"None" has no dotted-domain shape at all and is
    correctly rejected regardless. Confirmed real gap this widened shape
    fixes: a genuine hyperlink target recovered from a staging file this
    app itself already wrote (see staging_writer.read_xlsx_with_hyperlinks)
    that happens to include an explicit port, or a bare query string with
    no path segment, was previously indistinguishable from a placeholder
    and silently nulled on reload (see storage.file_store._sanitize_url_
    like_fields) even though nothing about the link had changed.
    """
    if not value or not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if re.match(r"^https?://", text, re.IGNORECASE):
        return True
    if re.search(r"\s", text):
        return False
    return bool(re.match(r"^[^\s/]+\.[a-zA-Z]{2,}(?::\d+)?(?:[/?#]\S*)?$", text))


def is_generic_link(url: str) -> bool:
    """
    True for a bare company homepage or top-level marketing domain with no
    listing-specific path at all (e.g. 'workplaceplus.co.uk',
    'https://www.workspace.co.uk/') - the exact shape of link the extraction
    prompt is supposed to suppress on its own. This is a deterministic
    code-level backstop for that judgment call, not a replacement for it:
    the prompt is better placed to judge fuzzier cases (a "contact us" page,
    a generic multi-property portfolio index) since those still have SOME
    path. This only catches the unambiguous case - empty or "/" path, no
    query string - so it never second-guesses a link that has any specific
    path segment, however it eventually resolves (see resolve_brochure_link:
    a genuine per-unit landing page that fails to resolve to a document is
    still kept, not treated as generic).

    Also true for a handful of known social/professional profile domains
    regardless of path - see KNOWN_NON_BROCHURE_DOMAINS.
    """
    parsed = urlparse(_normalize_url(url))
    # netloc == d (exact) or netloc.endswith("." + d) (a real subdomain) -
    # NEVER a bare .endswith(d) substring check, which is a real, confirmed
    # false positive: "app.box.com".endswith("x.com") is True purely because
    # "box.com" happens to end in the same 5 characters as "x.com", wrongly
    # rejecting every real UNION brochure link (Box-hosted) as if it were a
    # Twitter/X profile. A domain is only ever "the same site or a subdomain
    # of it", never "any domain whose name happens to end with the same
    # letters".
    if any(parsed.netloc == d or parsed.netloc.endswith(f".{d}") for d in KNOWN_NON_BROCHURE_DOMAINS):
        return True
    path = parsed.path.rstrip("/")
    return not path and not parsed.query


def _decode_known_tracking_wrapper(url: str):
    """
    Some tracking platforms embed the real destination directly in a query
    parameter instead of performing an actual HTTP redirect - confirmed for
    Microsoft Dynamics 365 Marketing's msdynmkt_target: a plain HTTP GET
    with follow_redirects=True does NOT redirect through it at all, it just
    returns 200 with no further link on the page. Decoding this directly
    avoids relying on a fetch mechanism that doesn't work for this platform.

    Returns the decoded destination URL, or None if `url` isn't a
    recognized wrapper of this kind (the generic fetch-and-scan path in
    resolve_brochure_link is the fallback for everything else, including
    genuinely opaque click-trackers like Mailchimp's, which DO redirect
    over plain HTTP and don't need special-casing).
    """
    parsed = urlparse(url)
    if "dynamics.com" not in parsed.netloc:
        return None

    params = parse_qs(parsed.query)
    target = params.get("msdynmkt_target")
    if not target:
        return None

    try:
        decoded = json.loads(unquote(target[0]))
        return unquote(decoded.get("TargetUrl", "")) or None
    except Exception:
        return None


def _score_candidate(href: str, text: str) -> int:
    is_pdf = _clean_path(href).endswith(".pdf")
    has_brochure = "brochure" in text or "brochure" in _clean_path(href)
    has_other_keyword = any(kw in text for kw in OTHER_LINK_KEYWORDS) or any(
        kw in _clean_path(href) for kw in OTHER_LINK_KEYWORDS
    )

    if is_pdf and has_brochure:
        return 3  # strongest signal: a PDF explicitly labeled as the brochure
    if is_pdf:
        return 2  # a document, but not confirmed to be THIS listing's brochure
    if has_brochure:
        return 1  # labeled "brochure" but not a direct file (e.g. another landing page)
    if has_other_keyword:
        return 1
    return 0


@functools.lru_cache(maxsize=256)
def resolve_brochure_link(url: str) -> str:
    """
    If `url` looks like a landing page rather than a document itself, fetch
    it and look for a link to an actual brochure/floorplan document on that
    page. Returns the resolved document URL if found, otherwise returns the
    original URL unchanged (but with an https:// scheme added if it was
    missing one). Cached per-URL so a link shared across many units in one
    document (or across documents in one process) is only fetched once.
    """
    url = _normalize_url(url)
    if _clean_path(url).endswith(".pdf"):
        return url  # already a direct document link, nothing to resolve

    decoded_target = _decode_known_tracking_wrapper(url)
    if decoded_target:
        if decoded_target.startswith("mailto:"):
            print(
                f"[brochure_link_resolver] {url!r} decodes to a mailto: link, not a webpage — keeping original.",
                file=sys.stderr,
            )
            return url
        print(
            f"[brochure_link_resolver] Decoded tracking wrapper {url!r} -> {decoded_target!r}, resolving that instead",
            file=sys.stderr,
        )
        return resolve_brochure_link(decoded_target)

    try:
        response = httpx.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        response.raise_for_status()
    except Exception as e:
        print(
            f"[brochure_link_resolver] Could not fetch {url!r} ({e!r}) — keeping original link.",
            file=sys.stderr,
        )
        return url

    soup = BeautifulSoup(response.text, "html.parser")
    candidates = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if href.startswith("#"):
            continue  # a same-page anchor is never itself a distinct document
        text = link.get_text(strip=True).lower()
        score = _score_candidate(href, text)
        if score > 0:
            candidates.append((score, href))

    final_url = str(response.url)
    if not candidates:
        if final_url != url:
            print(
                f"[brochure_link_resolver] {url!r} redirected to {final_url!r} but no document link "
                "found there — keeping the redirected page (still more useful than the raw redirect link).",
                file=sys.stderr,
            )
        else:
            print(
                f"[brochure_link_resolver] No document link found on landing page {url!r} — keeping original link.",
                file=sys.stderr,
            )
        return final_url

    candidates.sort(key=lambda c: c[0], reverse=True)
    best_href = candidates[0][1]
    resolved = urljoin(str(response.url), best_href)
    print(
        f"[brochure_link_resolver] Resolved landing page {url!r} -> {resolved!r}",
        file=sys.stderr,
    )
    return resolved


# Checked against a window of text preceding a wrapped link, and against the
# URL itself. These links can perform a real action (unsubscribing, opening
# a preference center) rather than just returning content, so they're never
# followed - domain/URL shape alone isn't a safe way to rule this out, since
# an unsubscribe link is often on the exact same tracking domain/path shape
# as a genuine content link in the same email.
ADMIN_LINK_KEYWORDS = (
    "unsubscribe",
    "preferences",
    "view in browser",
    "opt out",
    "opt-out",
    "manage your subscription",
    "subscribed to our list",
    "update your",
)

# How much preceding text to inspect for admin-link keywords. Generous enough
# to catch e.g. "You can update your preferences <url> or unsubscribe <url>"
# where the keyword sits well before the link itself.
ADMIN_LINK_CONTEXT_CHARS = 100


def _is_admin_link(nearby_text: str) -> bool:
    lowered = nearby_text.lower()
    return any(kw in lowered for kw in ADMIN_LINK_KEYWORDS)


def resolve_email_tracking_links(text: str) -> str:
    """
    Emails often wrap real hyperlinks - including genuine per-listing
    brochure/floorplan links - in click-tracking redirects, rendered in the
    plain-text body as '<https://tracking.example.com/...>' right after the
    visible text. Blindly deleting these (the old behavior) threw away
    genuine per-unit links along with harmless boilerplate (logos, social
    icons).

    Replaces each wrapped link with its resolved destination, so Gemini can
    see and attribute a real URL during extraction exactly as it already
    does for links that were never wrapped - except:

    - a link whose nearby text suggests it's an unsubscribe/manage-
      preferences/view-in-browser link is left deleted, as before, and is
      NEVER followed, since these can perform a real action rather than
      just return content;
    - an already-generic link (a bare company homepage/social profile, no
      specific path - see is_generic_link) is deleted without a fetch,
      since it was never going to be a listing-specific document either.

    Anything else is resolved via resolve_brochure_link, which follows
    redirects and scans the final page for an actual document link - so a
    tracking wrapper around a genuine per-unit landing page (e.g. a Google
    Drive or Canva view link) ends up exactly as well-resolved as a plain,
    unwrapped link would have been.
    """

    def _replace(match: re.Match) -> str:
        url = match.group(1)
        preceding = text[max(0, match.start() - ADMIN_LINK_CONTEXT_CHARS):match.start()]

        if _is_admin_link(preceding) or _is_admin_link(url):
            print(
                f"[email_tracking_links] Skipping likely admin/unsubscribe link "
                f"(context: {preceding[-40:]!r}): {url}",
                file=sys.stderr,
            )
            return ""

        if is_generic_link(url):
            return ""  # matches the old stripped-boilerplate behavior

        resolved = resolve_brochure_link(url)
        if is_generic_link(resolved):
            # The wrapper itself had a real path/query (so wasn't caught above),
            # but it resolves to the sender's own bare homepage - e.g. a footer
            # "website" icon. Still boilerplate, just not detectable before the
            # fetch; drop it rather than inserting a homepage link into the text.
            print(f"[email_tracking_links] {url!r} resolved to generic {resolved!r} — dropping.", file=sys.stderr)
            return ""

        print(f"[email_tracking_links] {url!r} -> {resolved!r}", file=sys.stderr)
        return resolved

    return re.sub(r"<(https?://\S+?)>", _replace, text)


def finalize_brochure_link(raw_link, *, is_pdf: bool, pdf_fallback_link: str):
    """
    Applies the full brochure_link priority order to whatever Gemini
    returned for one unit:

    1. A genuine, specific, per-unit link (e.g. a distinct floor-plan/
       brochure asset tied to that unit) - used as-is, resolved through one
       landing-page hop first if resolve_brochure_link finds a direct
       document. Resolving (or failing to resolve) doesn't change which
       rule applies - a genuine link stays genuine either way.
    2. A generic link (bare company homepage/top-level domain, no
       listing-specific path) - discarded entirely, treated as though
       nothing was found.
    3. Nothing genuine found (1 empty, 2 discarded) and the source is a
       PDF - defaults to pdf_fallback_link, since the PDF genuinely is the
       brochure for the majority of PDF uploads. This is the expected
       default, not a last-resort fallback. pdf_fallback_link should be a
       real, directly-fetchable URL to the uploaded PDF itself (see
       storage/file_store.save_original_pdf) whenever one is available -
       a bare filename with no scheme/host isn't a clickable link at all,
       just the closest thing callers had before original uploads started
       being persisted anywhere.
    4. Nothing genuine found and the source is an email - stays null; an
       email is not itself a brochure.

    Belt-and-suspenders guards: regardless of what Gemini decided, a link
    that is itself shaped like an unsubscribe/preferences-center URL (see
    ADMIN_LINK_KEYWORDS) or UNAMBIGUOUSLY a floor plan and nothing else (see
    is_floorplan_not_brochure_url) is never treated as genuine and never
    followed - in practice Gemini should never surface either in the first
    place (the extraction prompts already say so explicitly), but these are
    second, independent, deterministic checks for the case where Gemini is
    given a raw link directly (e.g. a PDF brochure with an embedded
    unsubscribe or floor-plan link) or otherwise still surfaces one. A link
    whose own text mentions BOTH ("...Brochure-and-Floorplans.pdf", a real
    combined-document naming pattern) is deliberately kept here - see is_
    floorplan_not_brochure_url's own docstring on why. Also never a genuine
    link at all when it doesn't even look like a URL (see looks_like_url) -
    a raw provider value that's really a placeholder ("TBC", "N/A", "-")
    rather than a real link, which is_generic_link alone doesn't reliably
    catch (see its own docstring).
    """
    if raw_link and _is_admin_link(raw_link):
        print(
            f"[finalize_brochure_link] {raw_link!r} looks like an unsubscribe/preferences link — "
            "discarding rather than following it.",
            file=sys.stderr,
        )
        raw_link = None

    if raw_link and is_floorplan_not_brochure_url(raw_link):
        print(
            f"[finalize_brochure_link] {raw_link!r} looks like a floor plan, not a brochure — "
            "discarding rather than using it as brochure_link.",
            file=sys.stderr,
        )
        raw_link = None

    if raw_link and not looks_like_url(raw_link):
        print(
            f"[finalize_brochure_link] {raw_link!r} doesn't look like a real URL — discarding rather than "
            "treating it as a genuine brochure link.",
            file=sys.stderr,
        )
        raw_link = None

    if raw_link and not is_generic_link(raw_link):
        return resolve_brochure_link(raw_link)

    if is_pdf:
        return pdf_fallback_link

    return None


def finalize_floorplan_link(raw_link):
    """
    floorplan_link's own, much narrower counterpart to finalize_brochure_
    link - a floor plan has no PDF-fallback default (rule 3) and no email-
    stays-null rule (rule 4) to apply, since neither the uploaded document
    itself nor "nothing found" is ever itself a floor plan the way a whole
    PDF upload can genuinely BE its own brochure. Only ever returns
    `raw_link` unchanged, or None when it isn't genuinely usable at all:
    an admin/unsubscribe-shaped link (see ADMIN_LINK_KEYWORDS), a generic
    bare homepage (see is_generic_link), or text that doesn't even look
    like a URL (see looks_like_url - the same placeholder-text gap that
    motivates finalize_brochure_link's own identical check). Never resolves
    through a landing-page hop the way finalize_brochure_link's rule 1
    does - a floor plan link is expected to already point directly at the
    document (or a page that IS the floor plan), not a portfolio landing
    page worth following one hop deeper.
    """
    if not raw_link:
        return None
    if _is_admin_link(raw_link):
        print(
            f"[finalize_floorplan_link] {raw_link!r} looks like an unsubscribe/preferences link — "
            "discarding rather than following it.",
            file=sys.stderr,
        )
        return None
    if not looks_like_url(raw_link):
        print(
            f"[finalize_floorplan_link] {raw_link!r} doesn't look like a real URL — discarding.",
            file=sys.stderr,
        )
        return None
    if is_generic_link(raw_link):
        return None
    return raw_link
