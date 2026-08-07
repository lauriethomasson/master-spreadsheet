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

    Belt-and-suspenders guard: regardless of what Gemini decided, a link
    that is itself shaped like an unsubscribe/preferences-center URL (see
    ADMIN_LINK_KEYWORDS) is never treated as genuine and never followed -
    in practice Gemini should never surface one of these in the first
    place, since resolve_email_tracking_links already strips them out of
    the text before extraction, but this is a second, independent check
    for the case where Gemini is given a raw link directly (e.g. a PDF
    brochure with an embedded unsubscribe link) or otherwise still
    surfaces one.
    """
    if raw_link and _is_admin_link(raw_link):
        print(
            f"[finalize_brochure_link] {raw_link!r} looks like an unsubscribe/preferences link — "
            "discarding rather than following it.",
            file=sys.stderr,
        )
        raw_link = None

    if raw_link and not is_generic_link(raw_link):
        return resolve_brochure_link(raw_link)

    if is_pdf:
        return pdf_fallback_link

    return None
