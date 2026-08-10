"""
brochure_enrichment.py

Secondary enrichment for spreadsheet-extracted ListingRows - fetches a
row's OWN brochure_link and reads it (reusing extract.py's exact PDF-vision
extraction pipeline via extract_raw_units) purely to fill fields the
original spreadsheet source left genuinely blank. The original upload is
always the source of truth: enrichment only ever fills a field that is
currently blank, never overwrites a populated one, and any fetch/parse
failure simply leaves the row exactly as extracted - it must never fail the
surrounding upload.

Deliberately scoped to spreadsheet rows only - a PDF upload is already
extracted from the actual brochure (enriching it from itself would be
circular and pointless), and email rows are left for a later change.

Field scope is deliberately narrow: ENRICHABLE_FIELDS below is the ONLY set
of fields this module ever assigns to. Every other ListingRow field
(provider, internal_ref, building, floor_unit, size_sqft, rent_pcm,
rent_psf, brochure_link, postcode, address_1, desks_min, desks_max,
contacts) is never touched by this module at all - not "protected by a
priority rule that could have a bug", genuinely absent from any code path
here, which is safer than a runtime overwrite-guard for the same reason
finalize_brochure_link's own rules are enforced in code rather than left to
a model to decide (see that module's own docstring).

No new ListingRow field is added for provenance - enrich_rows/enrich_rows_
grouped return a separate log (see their own docstrings) instead, so a
caller can report "these rows were enriched, from these fields" without a
schema change, while the code path that computed a value stays visibly
distinct from the one that extracted the original row.

Two entry points, for two different callers:
- enrich_row/enrich_rows: one row (or a plain list of rows) at a time - the
  original API, still correct, still used directly by callers that don't
  need per-run duplicate-call guarantees (e.g. a single already-known row).
- enrich_rows_grouped: the one a real spreadsheet upload uses (see app.py's
  spreadsheet branch) - groups rows by their OWN distinct brochure_link
  FIRST and processes each unique link exactly once, regardless of the
  cross-run lru_cache's own eviction behavior (see _extract_brochure_units's
  own docstring - a real UNION file with 126 unique brochures against a
  64-slot cache confirmed eviction can otherwise force a second real Gemini
  call for a link two sheets share) AND regardless of how many worker
  threads process that unique-URL worklist concurrently (see its own
  docstring on bounded concurrency) - the dedup guarantee comes from
  building the worklist itself as a set of distinct URLs before any work is
  dispatched, never from relying on either the cache or a lock to catch a
  race after the fact.

Runs automatically, immediately after a spreadsheet upload's base rows are
staged (see app.py's spreadsheet branch and save_staging_file) - NOT a
separate, later, user-triggered action any more. The base rows are staged
FIRST, with zero brochure/Gemini calls, specifically so that if enrichment
then crashes, times out, or is interrupted, the original extraction already
exists safely on disk; enrichment only ever REWRITES that same staging file
afterward (see storage.file_store.update_staging_rows), incrementally, as
results come in - never something the base extraction's own success depends
on.
"""

import concurrent.futures
import functools
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

import extract
from brochure_link_resolver import REQUEST_TIMEOUT, USER_AGENT, is_generic_link, resolve_brochure_link
from master_merge import normalize_key
from schema import ListingRow

# The only fields enrich_row ever assigns to, and only when the row's own
# value is blank - see the module docstring for why this list alone is the
# safety mechanism, not a separate overwrite-guard. Deliberately narrow for
# this first version: desks_min/desks_max/address_1/postcode/contacts are
# all plausible future candidates (see brochure_link_resolver.py's own
# "start conservative" precedent) but are not attempted here.
ENRICHABLE_FIELDS = ("special_features", "state_of_space")

# Matched against the URL only (never a fetch) - a floor plan or a video is
# never a brochure regardless of what it turns out to contain, and fetching
# either would waste a network round-trip for a URL shape that's already
# unambiguous from its own text. is_generic_link (bare company homepage,
# known social/professional domains) is checked separately - see
# _is_eligible_brochure_url.
_REJECTED_URL_KEYWORDS = ("floorplan", "floor-plan", "youtube.com", "youtu.be")

# Numeric tolerance for matching a row's own size_sqft against a candidate
# brochure unit's - a plain percentage band, never a similarity score. Wide
# enough to absorb rounding between a spreadsheet's and a brochure's own
# figure for the SAME unit, narrow enough that two genuinely different
# floors of comparable size are never confused for one another.
_SIZE_MATCH_TOLERANCE_FRACTION = 0.02
_SIZE_MATCH_MIN_TOLERANCE_SQFT = 1.0


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def needs_enrichment(row: ListingRow) -> bool:
    """
    True when at least one of ENRICHABLE_FIELDS is genuinely blank on `row`
    - checked BEFORE any network/Gemini activity is even considered (see
    enrich_row), so a row with nothing missing never costs a fetch or a
    Gemini call at all.
    """
    return any(_is_blank(getattr(row, field)) for field in ENRICHABLE_FIELDS)


def eligible_rows_and_brochures(rows: list):
    """
    (eligible_rows, unique_urls) - pure, no network/Gemini call of any kind
    - which of `rows` are genuinely worth attempting (needs_enrichment AND
    an eligible brochure URL, see _is_eligible_brochure_url), and the
    distinct brochure_link values among them, in first-encounter order.

    Used two ways: as a cheap PREVIEW of what an enrichment run would do
    (see pages/2_Review_and_Master.py's own "N rows / M brochures" banner,
    shown before the user opts in at all) and internally by
    enrich_rows_grouped to build its own per-run worklist - both need
    exactly this same computation, so it's factored out rather than
    duplicated.
    """
    eligible = [r for r in rows if needs_enrichment(r) and _is_eligible_brochure_url(r.brochure_link)]
    seen = set()
    unique_urls = []
    for r in eligible:
        if r.brochure_link not in seen:
            seen.add(r.brochure_link)
            unique_urls.append(r.brochure_link)
    return eligible, unique_urls


def _is_eligible_brochure_url(url) -> bool:
    """
    True only for a URL shape worth even attempting to fetch as a brochure -
    never a judgment about what's actually AT that URL (see _looks_like_pdf
    for the one made after fetching). Rejects: blank/non-URL text, a bare
    company homepage or known social/professional profile domain (see
    brochure_link_resolver.is_generic_link), and a URL whose own text
    already identifies it as a floor plan or a video rather than a
    document - never a fetch-then-guess; these are excluded by the URL
    alone, exactly like a human skimming a link list would.
    """
    if _is_blank(url):
        return False
    if urlparse(url).scheme not in ("http", "https"):
        return False
    if is_generic_link(url):
        return False
    lowered = url.lower()
    return not any(bad in lowered for bad in _REJECTED_URL_KEYWORDS)


def _looks_like_pdf(content_type, data: bytes) -> bool:
    if content_type and "pdf" in content_type.lower():
        return True
    return data[:5] == b"%PDF-"


# UNION's real Availability files (checked directly, across 7 real
# spreadsheets/80 distinct links) use app.box.com "shared link" URLs for
# 100% of their brochure_link values - a plain GET on one of these returns
# only an HTML shell for Box's own JS single-page viewer, never the PDF
# bytes themselves (confirmed: content-type text/html, no document link
# discoverable anywhere in the server-rendered markup). BOX_SHARE_URL_RE/
# _fetch_box_shared_pdf below read the file straight from Box's own "direct
# link" static-download URL scheme instead - see that function's own
# docstring for exactly what makes this reliable without a headless browser
# or Box API credentials.
_BOX_SHARE_URL_RE = re.compile(r"^https?://(?:app\.)?box\.com/s/([A-Za-z0-9]+)/?$")

_BOX_SHARED_NAME_RE = re.compile(r'"sharedName":"([^"]+)"')
_BOX_EXTENSION_RE = re.compile(r'"extension":"([^"]*)"')
_BOX_CAN_DOWNLOAD_RE = re.compile(r'"canDownload":(true|false)')
_BOX_FILE_NAME_RE = re.compile(r'"name":"([^"]+)"')

# Matched against the Box item's OWN file name (from its share-page
# metadata, see _fetch_box_shared_pdf) - a real, confirmed case found
# checking the real UNION files: a row's "Brochure" column pointed at a Box
# file literally named "3rd floor - Grosvenor Street - PLAN #1.pdf" - the
# PROVIDER'S OWN spreadsheet mislabeled a floor plan as its brochure link,
# not a mistake this code made. The bare "plan" check is deliberately
# broader than "floor plan" as one phrase - that real filename has "floor"
# and "plan" nowhere near each other ("3rd floor - Grosvenor Street - PLAN
# #1"), so a phrase-adjacency check alone would have missed it. Accepts the
# (believed rare) cost of rejecting a genuine brochure whose own file name
# happens to contain the standalone word "plan" for another reason -
# incorrect enrichment is worse than a missed one.
_FLOORPLAN_FILENAME_RE = re.compile(r"\bplan\b", re.IGNORECASE)


def _box_share_token(url: str):
    """The share token from a Box "app.box.com/s/{token}" URL, or None if
    `url` isn't shaped like one - a pure URL-shape check, no fetch, so a
    non-Box URL never even attempts the Box-specific path below."""
    match = _BOX_SHARE_URL_RE.match(url.split("?")[0].rstrip("/"))
    return match.group(1) if match else None


def _fetch_box_shared_pdf(share_url: str):
    """
    PDF bytes for a Box "shared link" URL, via Box's own "direct link"
    static-download URL scheme (app.box.com/shared/static/{sharedName}.
    {extension}) - the same underlying mechanism Box's own "Allow direct
    links" sharing setting uses to give a shared file a plain, embeddable
    URL, NOT Box's authenticated REST API (no OAuth/app credentials to
    provision or store) and NOT a headless browser (no JavaScript execution
    at all - every value read below comes from the share page's own
    server-rendered initial HTML response, a JSON blob literally present in
    a <script> tag, not something loaded afterward by client-side JS).

    Confirmed reliable against 6 real UNION brochure links spanning
    different buildings and different source files - each one's own
    sharedName/extension/canDownload/name read correctly and each
    correctly downloading that unit's own real PDF (sizes ranging ~0.9MB
    to ~32MB in the real files checked).

    Returns None (never raises) whenever:
    - the share page itself can't be fetched, or its metadata can't be
      parsed at all (a genuine Box frontend change, or a network failure) -
      the exact same "can't read this source" outcome as any other
      unreadable brochure, degrading no differently than before this
      function existed;
    - the file owner has downloads disabled (canDownload: false) -
      respected as a deliberate choice, never bypassed;
    - the file's own extension isn't "pdf" - nothing else is readable by
      extract.py's PDF-vision pipeline;
    - the file's own name looks like a floor plan (see
      _FLOORPLAN_FILENAME_RE) - see that pattern's own docstring for the
      real confirmed case this guards against.
    """
    try:
        page = httpx.get(
            share_url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True,
        )
        page.raise_for_status()
    except Exception as e:
        print(
            f"[brochure_enrichment] Could not load Box share page {share_url!r} ({e!r}) — skipping enrichment.",
            file=sys.stderr,
        )
        return None

    html = page.text
    shared_name_match = _BOX_SHARED_NAME_RE.search(html)
    extension_match = _BOX_EXTENSION_RE.search(html)
    can_download_match = _BOX_CAN_DOWNLOAD_RE.search(html)
    if not (shared_name_match and extension_match and can_download_match):
        print(
            f"[brochure_enrichment] Could not read Box share metadata for {share_url!r} — skipping enrichment.",
            file=sys.stderr,
        )
        return None

    if can_download_match.group(1) != "true":
        print(
            f"[brochure_enrichment] Box share {share_url!r} has downloads disabled — skipping enrichment.",
            file=sys.stderr,
        )
        return None

    extension = extension_match.group(1).lower()
    if extension != "pdf":
        print(
            f"[brochure_enrichment] Box share {share_url!r} is a .{extension or '?'} file, not a PDF — "
            "skipping enrichment.",
            file=sys.stderr,
        )
        return None

    name_match = _BOX_FILE_NAME_RE.search(html)
    file_name = name_match.group(1) if name_match else ""
    if _FLOORPLAN_FILENAME_RE.search(file_name):
        print(
            f"[brochure_enrichment] Box share {share_url!r} looks like a floor plan ({file_name!r}) — "
            "skipping enrichment.",
            file=sys.stderr,
        )
        return None

    static_url = f"https://app.box.com/shared/static/{shared_name_match.group(1)}.{extension}"
    try:
        response = httpx.get(
            static_url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True,
        )
        response.raise_for_status()
    except Exception as e:
        print(
            f"[brochure_enrichment] Could not download Box file at {static_url!r} ({e!r}) — skipping enrichment.",
            file=sys.stderr,
        )
        return None

    if not _looks_like_pdf(response.headers.get("content-type"), response.content):
        print(
            f"[brochure_enrichment] Box static download for {share_url!r} did not resolve to a PDF — "
            "skipping enrichment.",
            file=sys.stderr,
        )
        return None
    return response.content


def _fetch_pdf_bytes(url: str):
    """
    PDF bytes fetched from `url`, or None on ANY failure - a network error,
    a timeout, or content that isn't actually a PDF once fetched (a
    provider preview/landing page that never resolves to a real document, a
    generic marketing page, a dead link) - never raises. A Box "shared
    link" URL (see _box_share_token) is read via _fetch_box_shared_pdf
    instead of the generic path below - a plain GET on the share URL itself
    never returns the PDF directly (see that function's own docstring).
    resolve_brochure_link (already used for exactly this kind of one-hop
    landing-page resolution elsewhere in this repo - see its own docstring)
    is tried first for anything else not already a direct .pdf URL,
    covering a provider brochure-preview page or a landing page that links
    to the real PDF; something that resolves to a Google Drive/SharePoint
    share page instead (never a raw PDF byte stream from a plain GET)
    simply fails the _looks_like_pdf check below and enrichment is skipped
    for it - not a source this version can read, not something worth
    raising over.
    """
    if _box_share_token(url):
        return _fetch_box_shared_pdf(url)

    try:
        target = url if url.lower().split("?")[0].endswith(".pdf") else resolve_brochure_link(url)
        response = httpx.get(
            target, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True,
        )
        response.raise_for_status()
    except Exception as e:
        print(f"[brochure_enrichment] Could not fetch {url!r} ({e!r}) — skipping enrichment.", file=sys.stderr)
        return None

    if not _looks_like_pdf(response.headers.get("content-type"), response.content):
        print(
            f"[brochure_enrichment] {url!r} did not resolve to a PDF — skipping enrichment.",
            file=sys.stderr,
        )
        return None
    return response.content


@functools.lru_cache(maxsize=64)
def _extract_brochure_units(url: str):
    """
    The raw brochure units (see extract.extract_raw_units) for `url` - each
    a dict with its own building/floor_unit/size_sqft/special_features/
    state_of_space, exactly as extract.py's own PROMPT already extracts them
    for an uploaded PDF.

    This lru_cache is a CROSS-RUN optimization only (e.g. two separate
    enrichment clicks, or two pending files sharing a brochure) - it is
    never the mechanism a single enrichment run relies on to avoid a
    duplicate Gemini call for one URL. That guarantee comes from
    enrich_rows_grouped calling this exactly once per distinct URL within
    one run, by construction (see its own docstring) - maxsize=64 is small
    enough that a real large file (the real UNION file that motivated this:
    126 unique brochures) WILL evict and re-call this function across
    separate lookups, and that's fine precisely because nothing within one
    run depends on this cache still holding an old entry.

    Returns None on any failure (see _fetch_pdf_bytes, or a Gemini/parsing
    error while reading a fetched PDF that turned out to be corrupt or
    unreadable) - callers must treat that as "nothing to enrich from" and
    never propagate a failure into the surrounding upload.

    In-process only - this cache does not survive a restart, so a prompt/
    logic change here is automatically never served a stale result (unlike
    a persisted cache would risk - see the module docstring's own
    provenance note for the parallel reasoning already applied to
    ENRICHABLE_FIELDS). No persistent (disk/cross-restart) cache exists
    anywhere in this module - if one is added later, it would need real
    invalidation keying (content hash, code-version fingerprint); an
    in-process-only cache needs none, since a restart already clears it.
    """
    data = _fetch_pdf_bytes(url)
    if data is None:
        return None

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        raw = extract.extract_raw_units(tmp_path)
    except Exception as e:
        print(
            f"[brochure_enrichment] Could not read {url!r} as a brochure ({e!r}) — skipping enrichment.",
            file=sys.stderr,
        )
        return None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return raw.get("units", [])


def _match_unit(row: ListingRow, units: list):
    """
    The single brochure unit confidently identified as describing `row`'s
    own property, or None when there isn't one - never a fuzzy/similarity
    match, only exact (normalize_key-equal) building names and either an
    exact floor_unit match or a size_sqft match within a small numeric
    tolerance (see _SIZE_MATCH_TOLERANCE_FRACTION).

    A building with only ONE matching brochure unit is treated as a
    confident match regardless of its own floor_unit/size_sqft - a brochure
    describing just one whole building's space (no floor breakdown at all)
    is exactly the "building-level" case genuine building-wide features
    (manned reception, showers, bike storage) legitimately apply to every
    matching row for; there is nothing else in the brochure to prefer over
    it. A building with SEVERAL matching units (a real schedule of areas)
    only ever resolves when floor_unit or size_sqft narrows it to exactly
    one - two or more still matching after that is ambiguous and returns
    None, same as zero matching: incorrect enrichment is worse than a blank
    field, so an unresolved tie is never broken by guessing.
    """
    row_building_key = normalize_key(row.building)
    if not row_building_key:
        return None
    building_matches = [u for u in units if normalize_key(u.get("building")) == row_building_key]
    if not building_matches:
        return None
    if len(building_matches) == 1:
        return building_matches[0]

    if row.floor_unit:
        row_floor_key = normalize_key(row.floor_unit)
        floor_matches = [u for u in building_matches if normalize_key(u.get("floor_unit")) == row_floor_key]
        if len(floor_matches) == 1:
            return floor_matches[0]

    if row.size_sqft:
        tolerance = max(_SIZE_MATCH_MIN_TOLERANCE_SQFT, row.size_sqft * _SIZE_MATCH_TOLERANCE_FRACTION)
        size_matches = [
            u for u in building_matches
            if u.get("size_sqft") and abs(float(u["size_sqft"]) - row.size_sqft) <= tolerance
        ]
        if len(size_matches) == 1:
            return size_matches[0]

    return None


def _apply_units_to_row(row: ListingRow, units):
    """
    (row_or_new_row, enriched_fields) - the pure "given already-fetched
    brochure units, does this row get anything from them" step, factored
    out of enrich_row so enrich_rows_grouped can reuse it without doing its
    own fetch per row (see that function's own docstring for why: it fetches
    each DISTINCT brochure once, then applies the same result here to every
    row sharing it).

    units may be None/[] (nothing to enrich from - a failed or empty fetch)
    - in that case, or if _match_unit finds no confident match, or matching
    values are all already non-blank, this returns `row` unchanged, the
    exact same object, so a caller can tell "nothing happened" apart from
    "something changed" with a plain identity/truthiness check. Never
    mutates `row`.
    """
    if not units:
        return row, []

    unit = _match_unit(row, units)
    if unit is None:
        return row, []

    updates = {
        field: unit[field]
        for field in ENRICHABLE_FIELDS
        if _is_blank(getattr(row, field)) and not _is_blank(unit.get(field))
    }
    if not updates:
        return row, []

    return row.model_copy(update=updates), list(updates.keys())


def enrich_row(row: ListingRow):
    """
    (row, enriched_fields) - row is `row` unchanged (the exact same object)
    whenever nothing was enriched (nothing missing, no eligible brochure
    link, fetch/parse failure, or no confident match - all indistinguishable
    to the caller by design, since incorrect enrichment is worse than a
    blank field and none of these cases should behave differently from any
    other), or a NEW ListingRow (model_copy) with one or both of
    ENRICHABLE_FIELDS filled in when a confident match provided a genuinely
    new, non-blank value for a field that was blank. enriched_fields lists
    exactly which fields changed - [] whenever row is returned as-is.

    Fetches row's own brochure_link every call (through the cross-run
    lru_cache, see _extract_brochure_units) - correct for a single row in
    isolation, but a caller enriching many rows at once should use
    enrich_rows_grouped instead, which fetches each distinct brochure link
    exactly once regardless of how many rows share it (see that function's
    own docstring).

    Never mutates the input row, never raises - see _extract_brochure_units/
    _fetch_pdf_bytes for where every real failure mode is caught.
    """
    if not needs_enrichment(row) or not _is_eligible_brochure_url(row.brochure_link):
        return row, []

    units = _extract_brochure_units(row.brochure_link)
    return _apply_units_to_row(row, units)


def enrich_rows(rows: list):
    """
    (rows, log) - rows is a new list, positionally matching the input, each
    element either the original row (untouched) or enrich_row's own
    replacement. log is [{"building", "floor_unit", "fields",
    "brochure_link"}, ...], one entry per row that was ACTUALLY changed.

    Rows sharing one brochure_link cost exactly one fetch/Gemini call
    between them PROVIDED the cross-run lru_cache (see
    _extract_brochure_units) hasn't evicted that URL's entry in between -
    true for a small batch, not guaranteed for a large one (a real UNION
    file with 126 unique brochures against a 64-slot cache confirmed
    eviction can force a real second call). A caller processing many rows
    at once - the only realistic use of this function beyond a handful of
    rows - should use enrich_rows_grouped instead, which groups by distinct
    brochure_link FIRST and is immune to this regardless of cache size.
    Kept as the simple row-at-a-time API for callers that genuinely don't
    need that guarantee.
    """
    enriched_rows = []
    log = []
    for row in rows:
        new_row, fields = enrich_row(row)
        enriched_rows.append(new_row)
        if fields:
            log.append({
                "building": new_row.building,
                "floor_unit": new_row.floor_unit,
                "fields": fields,
                "brochure_link": new_row.brochure_link,
            })
    return enriched_rows, log


# How many unique brochures enrich_rows_grouped processes between each
# incremental checkpoint_callback call - bounds how much already-completed
# enrichment work an interruption (page navigation, a killed process, a
# Streamlit rerun cancelling the in-flight script - see that function's own
# docstring) can lose, without checkpointing so often that a slow blob-store
# backend (e.g. GCS in production, unlike the fast local-disk dev default)
# turns 126 brochures into 12+ extra round trips of its own.
CHECKPOINT_EVERY = 10

# How many unique brochures enrich_rows_grouped processes concurrently.
# Each unit of work is one real Box fetch + one real Gemini vision call,
# both genuinely I/O-bound (the GIL is released during network waits, so
# real overlap happens), but a real brochure PDF can be up to ~32MB (see
# _fetch_box_shared_pdf's own docstring), so raising this trades memory
# headroom and Gemini rate-limit risk for wall-clock time.
#
# 5 - not the highest number tried - based on a real, identical-sample
# benchmark (18 real UNION brochures, deliberately including known shared-
# brochure buildings and two known-failing links) across 3/4/5/6 workers:
#   workers   elapsed    read_ok/18   peak_rss_delta   projected 126-run
#      3       116.9s        16          ~297MB            ~13.6 min
#      4        91.5s        16          ~340MB            ~10.7 min
#      5        82.2s        16          ~422MB             ~9.6 min
#      6        71.2s        15          ~429MB             ~8.3 min
# Zero Gemini 429/QuotaExceededError at any worker count tested (no
# evidence rate limiting is a concern up to 6, at least for this workload/
# tier) - the 6-worker trial's one extra failure was a transient Gemini
# 503 ("Deadline expired"), not a rate limit, and with a single occurrence
# out of 72 real calls across the full benchmark this is weak evidence on
# its own, not a confirmed concurrency effect. Chosen over 6 anyway: 5
# captures the large majority of the available speedup (13.6 -> 9.6 min,
# ~29% faster than the previous default of 3) with a clean 16/18 success
# rate matching the lower worker counts, while 6's own marginal extra speed
# (9.6 -> 8.3 min) came with both the one anomalous failure AND the
# highest observed memory of the four - not a trade worth taking for a
# further ~1.3 minutes when the evidence for its safety is exactly as thin
# as the evidence against it. Revisit only with a fresh real benchmark, not
# by raising this number on assumption.
DEFAULT_MAX_WORKERS = 5


def enrich_rows_grouped(rows: list, progress_callback=None, checkpoint_callback=None, max_workers: int = DEFAULT_MAX_WORKERS):
    """
    (rows, log, stats) - like enrich_rows, but processes each DISTINCT
    eligible brochure_link (see eligible_rows_and_brochures) exactly ONCE
    per call, then applies that one result to every row sharing it. This is
    what makes per-run deduplication independent of the cross-run
    lru_cache's own maxsize/eviction (see _extract_brochure_units's own
    docstring) - within one call, nothing is ever fetched/sent to Gemini
    twice for the same URL, full stop, regardless of how many OTHER unique
    URLs this same run or a past one has already gone through, AND
    regardless of max_workers below: the guarantee comes from submitting
    exactly one task per entry in `unique_urls` (itself already
    deduplicated, see eligible_rows_and_brochures) - concurrency changes how
    many of those already-distinct tasks run at once, never how many tasks
    exist for one URL.

    Runs up to `max_workers` unique brochures' worth of fetch+Gemini work
    concurrently via a bounded ThreadPoolExecutor - each worker thread only
    ever computes a (url, units) pair (see _fetch_one, below) and returns
    it; EVERY mutation of shared state (current, log, stats, and both
    callbacks) happens back in the calling thread, one completed future at
    a time (concurrent.futures.as_completed always yields to its caller's
    own thread) - not inside a worker thread. This is what makes the whole
    function safe without any explicit lock of its own: nothing but each
    worker's own local (url, units) pair ever crosses a thread boundary, and
    progress_callback/checkpoint_callback - which may call Streamlit APIs,
    themselves not safe to call off the main script thread - are therefore
    NEVER invoked from a worker thread, only from the same thread that
    called enrich_rows_grouped in the first place.

    Safe on the extract.py side too (see extract_raw_units's own docstring)
    - get_client() makes a fresh, unshared genai.Client per call, and the
    one piece of process-wide state PyMuPDF's page rendering touches is
    serialized behind extract._RENDER_LOCK - only the actual Gemini network
    call is ever allowed to run concurrently across workers, which is also
    by far the most expensive part of each unit of work.

    progress_callback(done, total, label), if given, is called once per
    unique brochure as its result comes back (done = how many are finished
    INCLUDING this one, so the first call is (1, total, ...), not (0, ...)
    - unlike the old sequential version, work is dispatched to every worker
    up front, so there's no single well-defined "about to start #1" moment
    to report a 0 against) - purely for a caller (see app.py) to render a
    "Processing brochure N of M" progress bar; label is the building name
    of the first eligible row known to use that brochure. Called one final
    time with done == total once every brochure has been processed (a
    no-op repeat of the last real call in the normal case, but still the
    only fully reliable "done" signal if total == 0).

    checkpoint_callback(rows_so_far), if given, is called every
    CHECKPOINT_EVERY unique brochures (and always on the very last one)
    with the FULL rows list reflecting every enrichment applied so far
    (including rows whose brochure hasn't been reached yet, unchanged) -
    lets a caller persist partial progress (see storage.file_store.
    update_staging_rows) so an interruption partway through a long run
    doesn't throw away everything already successfully enriched, only
    whatever was still in flight. Checkpoint order follows COMPLETION order,
    not unique_urls' own order - with concurrency, brochure #40 can finish
    before #38 does; this is still correct (a checkpoint always reflects
    every result received so far, whichever URLs those happen to be), just
    worth knowing if you're trying to reason about exactly which brochure a
    given checkpoint "was at".

    Never raises: a real fetch/Gemini exception for one brochure is caught
    here too (belt and braces on top of _extract_brochure_units's own
    exception handling - this is the batch loop that replaces what used to
    be able to abort an entire upload over one bad brochure, so it stays
    defensive even though every currently-known failure mode is already
    handled one level down).
    """
    progress_callback = progress_callback or (lambda done, total, label: None)
    eligible, unique_urls = eligible_rows_and_brochures(rows)

    first_label = {}
    indices_by_url = {}
    for i, row in enumerate(rows):
        if needs_enrichment(row) and _is_eligible_brochure_url(row.brochure_link):
            indices_by_url.setdefault(row.brochure_link, []).append(i)
            first_label.setdefault(row.brochure_link, row.building)

    current = list(rows)
    log = []
    brochures_read_ok = 0
    brochures_unavailable = 0

    if not unique_urls:
        progress_callback(0, 0, None)
        return current, log, {
            "unique_brochures_considered": 0, "brochures_read_ok": 0,
            "brochures_unavailable": 0, "rows_eligible": len(eligible), "rows_enriched": 0,
        }

    def _fetch_one(url):
        # Runs in a worker thread - deliberately returns its result rather
        # than touching any shared state itself (see the function's own
        # docstring on why that's what makes this safe without a lock).
        try:
            return url, _extract_brochure_units(url)
        except Exception as e:
            print(f"[brochure_enrichment] Unexpected error reading {url!r} ({e!r}) — skipping.", file=sys.stderr)
            return url, None

    since_checkpoint = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = [executor.submit(_fetch_one, url) for url in unique_urls]
        for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            url, units = future.result()

            if units:
                brochures_read_ok += 1
            else:
                brochures_unavailable += 1

            for i in indices_by_url[url]:
                new_row, fields = _apply_units_to_row(rows[i], units)
                current[i] = new_row
                if fields:
                    log.append({
                        "building": new_row.building,
                        "floor_unit": new_row.floor_unit,
                        "fields": fields,
                        "brochure_link": new_row.brochure_link,
                    })

            progress_callback(done, len(unique_urls), first_label.get(url))

            since_checkpoint += 1
            is_last = done == len(unique_urls)
            if checkpoint_callback and (since_checkpoint >= CHECKPOINT_EVERY or is_last):
                checkpoint_callback(list(current))
                since_checkpoint = 0

    stats = {
        "unique_brochures_considered": len(unique_urls),
        "brochures_read_ok": brochures_read_ok,
        "brochures_unavailable": brochures_unavailable,
        "rows_eligible": len(eligible),
        "rows_enriched": len(log),
    }
    return current, log, stats
