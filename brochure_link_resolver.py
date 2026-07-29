"""
brochure_link_resolver.py

finalize_brochure_link is the entry point extract.py/extract_email.py call
per unit, applying the full brochure_link priority order (see its
docstring): a genuine per-unit link, resolved through one landing-page hop
if needed (confirmed: GPE, whose per-property link is a landing page
containing the real brochure PDF's link, not the PDF itself) > discard
anything generic (a bare company homepage with no listing-specific path) >
default to the uploaded PDF's own filename > null for emails.

resolve_brochure_link never discards a working link on its own - on any
failure (network error, timeout, no document link found), the original URL
is kept as-is, since a working landing page is still better than nothing.
"""

import functools
import sys
from urllib.parse import urljoin, urlparse

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
    """
    parsed = urlparse(_normalize_url(url))
    path = parsed.path.rstrip("/")
    return not path and not parsed.query


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

    if not candidates:
        print(
            f"[brochure_link_resolver] No document link found on landing page {url!r} — keeping original link.",
            file=sys.stderr,
        )
        return url

    candidates.sort(key=lambda c: c[0], reverse=True)
    best_href = candidates[0][1]
    resolved = urljoin(str(response.url), best_href)
    print(
        f"[brochure_link_resolver] Resolved landing page {url!r} -> {resolved!r}",
        file=sys.stderr,
    )
    return resolved


def finalize_brochure_link(raw_link, *, is_pdf: bool, own_filename: str):
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
       PDF - defaults to the uploaded PDF's own filename, since it
       genuinely is the brochure for the majority of PDF uploads. This is
       the expected default, not a last-resort fallback.
    4. Nothing genuine found and the source is an email - stays null; an
       email is not itself a brochure.
    """
    if raw_link and not is_generic_link(raw_link):
        return resolve_brochure_link(raw_link)

    if is_pdf:
        return own_filename

    return None
