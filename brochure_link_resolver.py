"""
brochure_link_resolver.py

Some documents (confirmed: GPE) give a landing-page URL per listing rather
than a direct link to the brochure/floorplan document itself — the landing
page contains a "Download brochure" link pointing to the actual document.
This resolves that one level deep: fetch the URL Gemini extracted, look for
an actual document link on that page, and use it instead if found.

Never discards a working link — on any failure (network error, timeout, no
document link found), the original URL is kept as-is, since a working
landing page is still better than nothing.
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
