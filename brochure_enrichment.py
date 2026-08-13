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

Field scope is deliberately explicit and categorized (see PROPERTY_LEVEL_
FIELDS/BUILDING_LEVEL_FIELDS/UNIT_LEVEL_FIELDS/HIGH_RISK_UNIT_LEVEL_FIELDS
below, together forming ENRICHABLE_FIELDS) rather than one unrestricted
list - different fields need different confidence requirements before a
blank one may be filled, not just "is it currently blank". Every field
NOT in one of those categories (provider, internal_ref, building,
brochure_link, floorplan_link, lat, lng, property_id, source_file) is
never touched by this module at all - not "protected by a priority rule
that could have a bug", genuinely absent from any code path here, which is
safer than a runtime overwrite-guard for the same reason
finalize_brochure_link's own rules are enforced in code rather than left to
a model to decide (see that module's own docstring). lat/lng in particular
are NEVER read from brochure text - even when a brochure enrichment run
supplies a trustworthy blank address_1/postcode, coordinates are still only
ever produced by the existing geocoder (geocode.py), never invented or
read from a document here; this module has no second geocoding path.

Brochure information is applied at the WIDEST level the brochure itself
clearly supports, never wider - three scopes, each narrower than the last
(see _apply_units_to_row's own docstring for the exact precedence):
1. Property/document-wide (PROPERTY_LEVEL_FIELDS: property_features,
   contacts - see _extract_brochure_units) - genuinely true of every row
   sharing this brochure_link, applied regardless of whether a specific
   building or floor/unit can be matched at all.
2. Building-wide (BUILDING_LEVEL_FIELDS: special_features's building_
   features fallback - see _match_building_feature - plus address_1/
   postcode/submarket - see _match_building_value) - applied whenever the
   row's own building exactly (normalize_key, see _building_identity_
   matches) matches brochure content with its own distinct building-level
   value, again regardless of whether a specific floor/unit within it can
   be matched. address_1/postcode/submarket are building-wide facts, never
   floor-specific, so they deliberately never require a unit match either -
   see point 8 in the task this was built for: an ambiguous/unresolved
   floor must never block a building-level fact this confident from filling.
3. Floor/unit-specific (UNIT_LEVEL_FIELDS: special_features/state_of_space/
   floor_unit/size_sqft/desks_max, plus HIGH_RISK_UNIT_LEVEL_FIELDS: rent_
   pcm/rent_psf - see _match_unit) - only ever applied to the one row a
   specific floor/unit was confidently matched to; never leaks to a
   different floor in the same building, because it only ever comes from
   _match_unit's own conservative, exact-match-only building+floor
   identification (see that function's own docstring) - there is no fuzzy/
   similarity matching anywhere in this module, at any of these scopes.
   HIGH_RISK_UNIT_LEVEL_FIELDS uses the exact same matching mechanism as
   UNIT_LEVEL_FIELDS (never a stricter/different one - there is no fuzzy
   matching to strengthen it with) but is kept in its own category since a
   wrong rent value is materially costlier than a wrong amenity note - see
   that constant's own docstring. desks_min is deliberately never included
   in either unit-level category - the brochure-extraction PROMPT (extract.
   py) only ever asks for desks_max, the same existing project convention
   already used for the direct-PDF-upload path; there is no per-unit
   desks_min in a raw brochure unit dict to backfill from at all.

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
import ctypes
import functools
import platform
import re
import sys
from urllib.parse import urlparse

import httpx
import streamlit as st

import extract
from brochure_link_resolver import (
    REQUEST_TIMEOUT, USER_AGENT, is_floorplan_not_brochure_url, is_generic_link, resolve_brochure_link,
)
import geocode
from house_number import leading_house_number
from master_merge import normalize_key
from schema import ListingRow
from storage.file_store import set_staging_enrichment_progress, set_staging_enrichment_summary, update_staging_rows


def _trim_memory() -> None:
    """
    Best-effort hint to the OS allocator to return freed-but-unreturned
    heap pages back to the OS, called once per completed brochure (see
    enrich_rows_grouped's own main loop) - a direct response to a
    confirmed, measured finding, not a speculative addition: rendering the
    same real sample PDF 30x with fitz.TOOLS.store_shrink(100) called
    after every render (exactly what render_pages already does) still grew
    RSS by ~22MB, even with gc.collect() run immediately before each
    measurement - i.e. not live Python objects waiting to be collected,
    and not MuPDF's own store (which store_shrink's own docstring
    confirms IS being fully freed - "Free 'percent' of current store
    size"). The remaining explanation is glibc's own allocator not handing
    genuinely-freed pages back to the OS - normal allocator behavior, but
    one that still counts against a container's cgroup memory limit (RSS
    doesn't distinguish "live object" from "freed but retained by the
    allocator for reuse"), and directly relevant here: a prior real Cloud
    Run OOM already occurred at 2062 MiB against a 2048 MiB limit, a
    margin this kind of per-brochure residual could plausibly close over
    enough brochures.

    glibc-only (Linux) - malloc_trim(0) has no equivalent, and no such
    accumulation risk worth chasing, on this project's Windows dev
    environment, so this is a safe no-op there (and anywhere else the
    symbol isn't found) rather than something that needs its own
    Linux-only test path to stay correct.
    """
    if platform.system() != "Linux":
        return
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass

# Property/document-wide fields - see _apply_units_to_row's DOCUMENT-level
# step. Genuinely true of every row sharing this brochure_link; contacts is
# ALWAYS sourced this way (a document-level value, never a per-unit one -
# see _apply_units_to_row's own docstring). special_features also has a
# building-level and unit-level source (see BUILDING_LEVEL_FIELDS/UNIT_
# LEVEL_FIELDS below) - property_features is only its widest, weakest one.
PROPERTY_LEVEL_FIELDS = ("special_features", "contacts")

# Building-wide fallback fields - see _apply_units_to_row's BUILDING-level
# step / _match_building_value. Filled only when row.building confidently
# identifies exactly one brochure building (_building_identity_matches) AND
# every one of that building's own raw units agrees on a single, non-blank
# value for the field - two or more conflicting values (or zero) leaves it
# blank, same "incorrect enrichment is worse than missing" philosophy as
# every other tier. Deliberately never requires a floor/unit match - an
# address/postcode/submarket is true of the whole building regardless of
# which floor a row describes (see the module docstring's own point 8).
# special_features's own building-level source (_match_building_feature,
# reading units.building_features - a SEPARATE, dedicated per-building text
# list the extraction prompt already produces) is handled inline in
# _apply_units_to_row, not via _match_building_value - kept out of this
# tuple's own per-field loop for that reason, but still a genuine building-
# level fallback for that field.
BUILDING_LEVEL_FIELDS = ("address_1", "postcode", "submarket")

# Unit-level fallback fields - see _apply_units_to_row's UNIT-level step /
# _match_unit. Only ever applied to the one row a specific floor/unit was
# confidently matched to - never a building- or property-wide fallback,
# since none of these is safe to assume applies beyond the one unit it was
# actually stated for. contacts is deliberately excluded here even though
# it's in ENRICHABLE_FIELDS overall - see PROPERTY_LEVEL_FIELDS above.
UNIT_LEVEL_FIELDS = ("special_features", "state_of_space", "floor_unit", "size_sqft", "desks_max")

# Same exact-unit-match mechanism and confidence bar as UNIT_LEVEL_FIELDS
# (there is no fuzzy matching in this module to make a field "even more
# confident" with) - kept as its own category purely for auditability: a
# wrong rent value quoted from a stale brochure is materially costlier than
# a wrong amenity note, so this list exists as the one place to tighten
# further later if a real failure ever motivates it, without touching the
# lower-risk fields' own rule. Never derives one from the other (e.g. via
# gemini_client.compute_rent) - each raw brochure unit already states its
# own rent_pcm/rent_psf directly per the extraction PROMPT's "ONLY if
# explicitly stated... do not calculate this yourself" instruction, so a
# blank one here is filled only from that same unit's own explicit value,
# never derived from its sibling field.
HIGH_RISK_UNIT_LEVEL_FIELDS = ("rent_pcm", "rent_psf")

# The full set of fields this module can ever fill, for the single top-
# level "is there anything at all worth fetching a brochure for" gate (see
# needs_enrichment) - dict.fromkeys dedupes special_features (present in
# both PROPERTY_LEVEL_FIELDS and UNIT_LEVEL_FIELDS) while preserving order.
ENRICHABLE_FIELDS = tuple(dict.fromkeys(
    PROPERTY_LEVEL_FIELDS + BUILDING_LEVEL_FIELDS + UNIT_LEVEL_FIELDS + HIGH_RISK_UNIT_LEVEL_FIELDS
))

# Matched against the URL only (never a fetch) - a video is never a
# brochure regardless of what it turns out to contain, and fetching one
# would waste a network round-trip for a URL shape that's already
# unambiguous from its own text. is_generic_link (bare company homepage,
# known social/professional domains) is checked separately - see
# _is_eligible_brochure_url. A floor-plan-shaped URL is rejected too, but
# via is_floorplan_not_brochure_url (see its own docstring) rather than a
# bare FLOORPLAN_URL_KEYWORDS substring check - a combined "Brochure and
# Floorplans.pdf" document must still be eligible.
_REJECTED_URL_KEYWORDS = ("youtube.com", "youtu.be")

# Numeric tolerance for matching a row's own size_sqft against a candidate
# brochure unit's - a plain percentage band, never a similarity score. Wide
# enough to absorb rounding between a spreadsheet's and a brochure's own
# figure for the SAME unit, narrow enough that two genuinely different
# floors of comparable size are never confused for one another.
_SIZE_MATCH_TOLERANCE_FRACTION = 0.02
_SIZE_MATCH_MIN_TOLERANCE_SQFT = 1.0

# The leading digit run in a floor_unit label, e.g. "5" from "5th Floor",
# "5th", "Floor 5", or a bare "5" - used by _match_unit as a fallback when an
# exact normalize_key floor_unit match (the primary tier) fails to resolve to
# exactly one candidate. A provider's own spreadsheet and Gemini's brochure
# extraction routinely label the SAME floor differently in ways normalize_key
# alone never reconciles ("5th" vs "5th Floor" vs "Floor 5" - confirmed: none
# of these three normalize_key-equal each other) even though a human reading
# both would recognize them as unambiguously the same floor. Digit-only
# (never "fifth"/word ordinals) - deliberately narrow, same "start
# conservative" precedent as brochure_link_resolver.py's own.
_FLOOR_NUMBER_RE = re.compile(r"\d+")


def _floor_number(floor_unit):
    """
    The leading digit run in `floor_unit` as an int (e.g. 5 from "5th
    Floor"), or None if it's blank or has no digit at all (e.g. "Ground
    Floor", "Reception") - those never participate in this fallback tier,
    exactly as if it didn't exist for them (falls through to the existing
    size-based tier, or no match, same as before this existed).
    """
    if _is_blank(floor_unit):
        return None
    match = _FLOOR_NUMBER_RE.search(str(floor_unit))
    return int(match.group()) if match else None


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


# Matches a " - "/","/"—" separator followed by a clause starting with a
# house number - e.g. "Discovery House - 28-42 Banner St" or "Nash House,
# 13a St George St". Real, confirmed shape: a provider's own spreadsheet
# building column routinely bakes the street address into the SAME field a
# brochure's own cover/title only ever states as the bare building name -
# "Discovery House" (brochure) vs "Discovery House - 28-42 Banner St" (row).
_BUILDING_ADDRESS_SUFFIX_RE = re.compile(r"^(.+?)\s*[-,–—]\s*(\d.+)$")


def _strip_building_address_suffix(building):
    """
    `building` with a trailing address-shaped suffix removed, when one is
    present - "Discovery House - 28-42 Banner St" -> "Discovery House",
    "Nash House, 13a St George St" -> "Nash House". Returns `building`
    unchanged (never None/"") whenever the shape isn't genuinely "a name,
    then a real house-number-led address": no separator at all, what
    follows the separator isn't itself a real house number per house_
    number.leading_house_number (the same authoritative parser master_
    merge.py/extract_spreadsheet_gemini.py already use, rather than a
    second, independently-drifting digit check - confirmed gap: a bare
    `\\d.+` check alone would happily strip "27-30 Lime Street" itself down
    to a bogus "27"), or what remains BEFORE the separator is itself
    already a house number (the real "27-30 Lime Street" case - splitting
    at its own internal "-" would otherwise produce a nonsense "27" head,
    not a genuine building name). Deliberately conservative, same "start
    conservative" precedent as this module's other matching tiers: a
    building's own genuinely distinct second half that isn't address-shaped
    (e.g. "Discovery House - East Wing") is left alone rather than guessed
    at.
    """
    if not building:
        return building
    match = _BUILDING_ADDRESS_SUFFIX_RE.match(str(building).strip())
    if not match:
        return building
    head, tail = match.group(1).strip(), match.group(2).strip()
    if not head or leading_house_number(head) is not None or leading_house_number(tail) is None:
        return building
    return head


def _building_identity_matches(row_building, candidate_buildings: list) -> list:
    """
    Indices into `candidate_buildings` (a list of raw building-name strings,
    e.g. one per brochure unit/building_features entry) that confidently
    identify the SAME building as `row_building` - never a fuzzy/similarity
    match, only exact-string comparisons at two tiers:

    1. EXACT (both sides' own normalize_key, no suffix stripped) - always
       sufficient identity evidence by itself. Every exact match is
       returned, even several (e.g. a real schedule-of-areas brochure with
       many units for the SAME building) - disambiguating between several
       exact matches is left to the caller's own further narrowing (floor/
       size), exactly as before this function existed.
    2. ADDRESS-SUFFIX-STRIPPED (see _strip_building_address_suffix) - e.g.
       "Nash House - 13a St George St" (a row) vs "Nash House" (a brochure).
       This is explicitly weaker evidence: a shortened/stripped name alone
       does not prove identity, since two GENUINELY DIFFERENT buildings that
       happen to share a brand/prefix (e.g. "WeWork - 10 Fenchurch St" and
       "WeWork - 20 Old Broad St" in the same portfolio brochure) strip to
       the identical key. Only ever accepted when it is the SOLE candidate
       sharing that stripped key - two or more candidates sharing it is
       exactly as ambiguous as two or more exact matches would be if this
       fell back to guessing between them, so it stays unresolved (empty),
       same "incorrect enrichment is worse than a blank field" philosophy
       as every other tier in this module. This is what makes the stripped
       tier only ever a WEAK, corroborated signal - unique-within-this-
       comparison is the corroboration, never the bare shortened name alone.

    Returns [] when row_building has no genuine key at all (blank/
    whitespace-only).
    """
    row_key = normalize_key(row_building)
    if not row_key:
        return []

    exact = [i for i, c in enumerate(candidate_buildings) if normalize_key(c) == row_key]
    if exact:
        return exact

    row_stripped_key = normalize_key(_strip_building_address_suffix(row_building))
    if not row_stripped_key:
        return []
    stripped = [
        i for i, c in enumerate(candidate_buildings)
        if normalize_key(_strip_building_address_suffix(c)) == row_stripped_key
    ]
    return stripped if len(stripped) == 1 else []


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
    never a judgment about what's actually AT that URL (see _looks_like_
    fetchable_document for the one made after fetching). Rejects: blank/
    non-URL text, a bare
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
    if is_floorplan_not_brochure_url(url):
        return False
    lowered = url.lower()
    return not any(bad in lowered for bad in _REJECTED_URL_KEYWORDS)


# Magic bytes for the two raster formats a floor plan drawing is actually
# exported as (see _looks_like_fetchable_document's own docstring) - never
# widened beyond these two specific, confirmed-real formats.
_IMAGE_MAGIC_BYTES = (
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"\xff\xd8\xff",  # JPEG
)


def _looks_like_fetchable_document(content_type, data: bytes, accept_image_formats: bool = False) -> bool:
    """
    True when `data` is genuinely readable by extract.render_pages/render_
    and_extract - a real PDF always, and (only when accept_image_formats is
    True - set only for the floorplan fetch path, see _extract_floorplan_
    units) a real PNG/JPEG too.

    Confirmed real gap this second case fixes: a floor plan is routinely
    delivered as a scanned/exported image rather than a vector PDF (two
    real UNION examples confirmed directly: Box reports both as a plain
    .png) - extract.render_pages already opens and renders a raster image
    correctly, exactly like a one-page PDF (PyMuPDF's own filetype hint is
    advisory, not enforced - fitz.open(stream=data, filetype="pdf") still
    opens genuine PNG/JPEG bytes correctly, confirmed directly), so nothing
    downstream of this check needs to change - only this fetch-time gate,
    which used to reject the bytes outright before they ever reached
    render_pages.

    Never widened to "any non-PDF content" even with accept_image_formats
    True - a .docx, an HTML error page, or a truncated download must still
    be rejected exactly as before; only these two specific raster formats
    are accepted, and only for the floorplan fetch path, since 0 of 9 real
    brochure documents traced were images while 2 of 2 real floorplan
    documents traced were.
    """
    if content_type and "pdf" in content_type.lower():
        return True
    if data[:5] == b"%PDF-":
        return True
    if not accept_image_formats:
        return False
    if content_type and "image/" in content_type.lower():
        return True
    return any(data[:len(magic)] == magic for magic in _IMAGE_MAGIC_BYTES)


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


_BOX_PDF_ONLY_EXTENSIONS = ("pdf",)
# Only ever used for the floorplan fetch path (accept_image_formats=True -
# see _fetch_box_shared_pdf's own docstring) - a real, confirmed shape: two
# real UNION floor plans are stored on Box as a plain .png, not a PDF, and
# extract.render_pages already renders that correctly with zero changes
# once the bytes are actually let through (see _looks_like_fetchable_
# document's own docstring). Never widened to a brochure fetch - 0 of 9
# real brochure documents traced were images.
_BOX_PDF_OR_IMAGE_EXTENSIONS = ("pdf", "png", "jpg", "jpeg")


def _fetch_box_shared_pdf(share_url: str, reject_floorplan_filename: bool = True, accept_image_formats: bool = False):
    """
    Document bytes (a PDF, or - only when accept_image_formats, see below -
    a PNG/JPEG) for a Box "shared link" URL, via Box's own "direct link"
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
    - the file's own extension isn't one this call accepts - "pdf" only by
      default, or "pdf"/"png"/"jpg"/"jpeg" when accept_image_formats is
      True (see _BOX_PDF_OR_IMAGE_EXTENSIONS's own docstring on why this is
      never widened beyond those two specific raster formats, and only for
      the floorplan fetch path);
    - the file's own name looks like a floor plan (see
      _FLOORPLAN_FILENAME_RE) - see that pattern's own docstring for the
      real confirmed case this guards against. reject_floorplan_filename=
      False (see brochure_enrichment._extract_floorplan_units, the one
      caller that fetches a row's OWN floorplan_link rather than its
      brochure_link) skips this specific check - a floor plan IS the
      expected, correct content there, not a mislabeling to guard against.
      accept_image_formats is passed by that same caller, for the same
      reason.
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
    allowed_extensions = _BOX_PDF_OR_IMAGE_EXTENSIONS if accept_image_formats else _BOX_PDF_ONLY_EXTENSIONS
    if extension not in allowed_extensions:
        print(
            f"[brochure_enrichment] Box share {share_url!r} is a .{extension or '?'} file, not a "
            f"{'PDF/image' if accept_image_formats else 'PDF'} — skipping enrichment.",
            file=sys.stderr,
        )
        return None

    name_match = _BOX_FILE_NAME_RE.search(html)
    file_name = name_match.group(1) if name_match else ""
    if reject_floorplan_filename and _FLOORPLAN_FILENAME_RE.search(file_name):
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

    if not _looks_like_fetchable_document(
        response.headers.get("content-type"), response.content, accept_image_formats=accept_image_formats,
    ):
        print(
            f"[brochure_enrichment] Box static download for {share_url!r} did not resolve to a "
            f"{'PDF/image' if accept_image_formats else 'PDF'} — skipping enrichment.",
            file=sys.stderr,
        )
        return None
    return response.content


def _fetch_pdf_bytes(url: str, reject_floorplan_filename: bool = True, accept_image_formats: bool = False):
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
    is tried first for anything else not already a direct .pdf (or, when
    accept_image_formats, .png/.jpg/.jpeg) URL, covering a provider
    brochure-preview page or a landing page that links to the real
    document; something that resolves to a Google Drive/SharePoint share
    page instead (never a raw document byte stream from a plain GET)
    simply fails the _looks_like_fetchable_document check below and
    enrichment is skipped for it - not a source this version can read, not
    something worth raising over.

    reject_floorplan_filename/accept_image_formats are passed straight
    through to _fetch_box_shared_pdf (see its own docstring) - both True
    only when the caller is deliberately fetching a floor plan (see
    _extract_floorplan_units), where a floorplan-shaped Box file name is
    the expected content, not a mislabeling to guard against, and a raster
    image is a real, confirmed document shape, not a fetch failure.
    """
    if _box_share_token(url):
        return _fetch_box_shared_pdf(
            url, reject_floorplan_filename=reject_floorplan_filename, accept_image_formats=accept_image_formats,
        )

    direct_extensions = (".pdf", ".png", ".jpg", ".jpeg") if accept_image_formats else (".pdf",)
    try:
        clean_path = url.lower().split("?")[0]
        target = url if clean_path.endswith(direct_extensions) else resolve_brochure_link(url)
        response = httpx.get(
            target, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True,
        )
        response.raise_for_status()
    except Exception as e:
        print(f"[brochure_enrichment] Could not fetch {url!r} ({e!r}) — skipping enrichment.", file=sys.stderr)
        return None

    if not _looks_like_fetchable_document(
        response.headers.get("content-type"), response.content, accept_image_formats=accept_image_formats,
    ):
        print(
            f"[brochure_enrichment] {url!r} did not resolve to a "
            f"{'PDF/image' if accept_image_formats else 'PDF'} — skipping enrichment.",
            file=sys.stderr,
        )
        return None
    return response.content


class _BrochureUnits(list):
    """
    A plain list of unit dicts (see _extract_brochure_units's own docstring
    below) that ALSO carries this brochure's own document-level
    property_features/contacts, and its building-level building_features,
    as extra attributes - never as extra list elements. Every existing
    caller/test that already treats this as a plain list (truthiness,
    len(), iteration, indexing) is completely unaffected, since it
    genuinely IS one; only code that explicitly asks for
    .property_features/.contacts/.building_features (see
    _apply_units_to_row/_match_building_feature) sees them.

    This is deliberately NOT a new return shape (e.g. a tuple or dict) -
    dozens of existing tests mock _extract_brochure_units with a bare
    return_value=[...]/None, and a plain `list`/None still satisfies every
    getattr(units, "property_features", None) check below with None,
    exactly as if that brochure had stated nothing at that level - so none
    of those mocks needed to change for this.
    """

    property_features = None
    contacts = None
    building_features = None


@functools.lru_cache(maxsize=64)
def _extract_brochure_units(url: str):
    """
    The raw brochure units (see extract.extract_raw_units) for `url` - each
    a dict with its own building/floor_unit/size_sqft/special_features/
    state_of_space, exactly as extract.py's own PROMPT already extracts them
    for an uploaded PDF. Returns a _BrochureUnits (see above), so this
    brochure's own document-level property_features/contacts - genuinely
    true of the WHOLE document, never one specific floor - travel alongside
    the per-unit list without requiring every existing caller to unpack a
    different shape.

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

    try:
        # Rendered directly from the in-memory bytes - never written to a
        # temp file at all (see render_pages' own docstring on why: a
        # tmpfs-backed container /tmp, e.g. Cloud Run's default, would
        # count that file's bytes against the SAME memory budget as `data`
        # itself, a real doubling of a payload that can be up to ~32MB).
        with extract._RENDER_LOCK:
            images = extract.render_pages(data)
        # Dropped HERE, between the two calls - not in a finally at the end
        # of this function - specifically so it's freed BEFORE the slower
        # Gemini call below runs, not merely before this function returns.
        # A caller-side reference to an argument stays alive for a callee's
        # entire execution regardless of what that callee does internally,
        # so this only works because the render step and the Gemini call
        # are two SEPARATE calls with this line in between, not one.
        data = None
        raw = extract.render_and_extract(images)
    except Exception as e:
        print(
            f"[brochure_enrichment] Could not read {url!r} as a brochure ({e!r}) — skipping enrichment.",
            file=sys.stderr,
        )
        return None

    units = _BrochureUnits(raw.get("units", []))
    property_features = raw.get("property_features")
    units.property_features = property_features if isinstance(property_features, str) else None
    contacts = raw.get("contacts")
    units.contacts = contacts if isinstance(contacts, str) else None
    # Raw Gemini JSON, same as units above - never schema-validated before
    # reaching here, so a malformed entry (missing/non-string "building" or
    # "features") is simply excluded rather than raising (see _match_unit's
    # own docstring for the identical reasoning about a stray bad unit).
    units.building_features = [
        bf for bf in raw.get("building_features") or []
        if isinstance(bf, dict) and isinstance(bf.get("building"), str) and isinstance(bf.get("features"), str)
        and not _is_blank(bf.get("building")) and not _is_blank(bf.get("features"))
    ]
    return units


def _match_unit(row: ListingRow, units: list):
    """
    The single brochure unit confidently identified as describing `row`'s
    own property, or None when there isn't one - never a fuzzy/similarity
    match, only exact building-name matching (see _building_identity_
    matches - still exact-string, and only weakly, corroborated-by-
    uniqueness tolerant of a redundant address suffix baked into one side's
    own building field) and then, in order, an exact floor_unit
    match, a floor NUMBER match (see
    _floor_number), or a size_sqft match within a small numeric tolerance
    (see _SIZE_MATCH_TOLERANCE_FRACTION).

    A building with only ONE matching brochure unit is treated as a
    confident match regardless of its own floor_unit/size_sqft - a brochure
    describing just one whole building's space (no floor breakdown at all)
    is exactly the "building-level" case genuine building-wide features
    (manned reception, showers, bike storage) legitimately apply to every
    matching row for; there is nothing else in the brochure to prefer over
    it. A building with SEVERAL matching units (a real schedule of areas)
    only ever resolves when floor_unit (exact text, then leading floor
    number) or size_sqft narrows it to exactly one - two or more still
    matching after every tier is ambiguous and returns None, same as zero
    matching: incorrect enrichment is worse than a blank field, so an
    unresolved tie is never broken by guessing.

    The floor-NUMBER tier (between the exact-text and size tiers) exists
    because a provider's own spreadsheet and Gemini's own brochure
    extraction routinely label the identical floor differently in ways
    normalize_key alone never reconciles - confirmed against real brochures
    in this repo's own tests/sample_docs/: a spreadsheet row commonly says
    just "5th" or a bare "5" where the brochure itself says "5th Floor",
    which fails the exact-text tier outright even though there is exactly
    one "5"-numbered floor in the whole building and zero real ambiguity.
    Still just as conservative as the tiers around it: only ever resolves
    when exactly one building_match shares that row's own leading floor
    number, never when two units coincidentally share one (e.g. "5th
    Floor" and "5B Suite" both extracting 5) - that stays an unresolved
    tie, same as the exact-text and size tiers already treat one.
    """
    row_building_key = normalize_key(row.building)
    if not row_building_key:
        return None
    # units is raw Gemini JSON (see _extract_brochure_units's own
    # docstring - never validated against ExtractedFields the way the
    # direct-PDF-upload path's units are) - a stray non-dict entry (a
    # confirmed real failure mode: a malformed JSON array item) is simply
    # excluded here rather than raising on it, so ONE bad entry degrades
    # to "one less candidate to match against", never a crash that
    # would otherwise take the entire batch down with it (see this
    # function's own caller in enrich_rows_grouped for the belt-and-
    # braces try/except around this too).
    units = [u for u in units if isinstance(u, dict)]
    match_indices = _building_identity_matches(row.building, [u.get("building") for u in units])
    building_matches = [units[i] for i in match_indices]
    if not building_matches:
        return None
    if len(building_matches) == 1:
        return building_matches[0]

    if row.floor_unit:
        row_floor_key = normalize_key(row.floor_unit)
        floor_matches = [u for u in building_matches if normalize_key(u.get("floor_unit")) == row_floor_key]
        if len(floor_matches) == 1:
            return floor_matches[0]

        row_floor_number = _floor_number(row.floor_unit)
        if row_floor_number is not None:
            number_matches = [u for u in building_matches if _floor_number(u.get("floor_unit")) == row_floor_number]
            if len(number_matches) == 1:
                return number_matches[0]

    if row.size_sqft:
        tolerance = max(_SIZE_MATCH_MIN_TOLERANCE_SQFT, row.size_sqft * _SIZE_MATCH_TOLERANCE_FRACTION)
        size_matches = [
            u for u in building_matches
            if _safe_float(u.get("size_sqft")) is not None
            and abs(_safe_float(u["size_sqft"]) - row.size_sqft) <= tolerance
        ]
        if len(size_matches) == 1:
            return size_matches[0]

    return None


def _match_building_feature(row: ListingRow, units):
    """
    The building-wide features text (a plain str) confidently identified as
    describing `row`'s own building, or None - the level-B counterpart to
    _match_unit's floor-level matching and units.property_features's
    document-level fallback (see _apply_units_to_row's own docstring for how
    the three combine). Sourced from units.building_features (see
    _extract_brochure_units), one {"building", "features"} entry per
    building the brochure itself gave distinct building-level text for.

    Same exact-match-only philosophy as _match_unit: exact building-name
    matching via _building_identity_matches (still exact-string, and only
    weakly, corroborated-by-uniqueness tolerant of a redundant address
    suffix - see that function's own docstring), never fuzzy/similarity
    matching. Two entries that happen to match the row's own building
    (shouldn't occur - the prompt asks for one entry per distinct building -
    but never assumed) is treated as ambiguous and returns None, same as
    zero matches.
    """
    building_features = getattr(units, "building_features", None)
    if not building_features:
        return None
    if not normalize_key(row.building):
        return None
    match_indices = _building_identity_matches(row.building, [bf.get("building") for bf in building_features])
    if len(match_indices) == 1:
        return building_features[match_indices[0]]["features"]
    return None


def _source_postcode_conflict(row: ListingRow, candidate_postcode: str) -> bool:
    """
    Reuses geocode.py's own source-postcode-evidence check (see that
    module's _source_location_hint/_postcode_hint_conflicts) rather than a
    second, independently-drifting implementation - True only when the row
    ALREADY has its own postcode-district evidence (in row.postcode/
    address_1/building) that genuinely disagrees with `candidate_postcode`
    (e.g. row.building states "...WC1" but the brochure's matched building
    states an "SE1" postcode) - never true when the row has no such
    evidence at all, same permissive default as geocode.py's own check.
    """
    source_hint = geocode._source_location_hint(row)
    if not source_hint:
        return False
    return geocode._postcode_hint_conflicts(source_hint, candidate_postcode)


def _match_building_value(row: ListingRow, units, field: str):
    """
    The single, unambiguous value for `field` (one of BUILDING_LEVEL_FIELDS
    - address_1/postcode/submarket) among every raw brochure unit whose own
    "building" confidently identifies the SAME building as row.building
    (see _building_identity_matches) - None when there's nothing to offer,
    or when the matching units disagree on this field (two or more distinct
    non-blank values - ambiguous, "incorrect enrichment is worse than
    missing" applies here exactly as it does to _match_unit/_match_building_
    feature), or (postcode only) when the row already has its own
    conflicting postcode evidence (see _source_postcode_conflict, reusing
    geocode.py's own validation rather than a parallel implementation).

    Deliberately building-scoped, never floor-specific - correct for a
    field genuinely true of the whole building regardless of which floor a
    row describes (see the module docstring's own point 8: an unresolved
    floor must never block a building-level fact this confident).
    """
    if not normalize_key(row.building):
        return None
    plain_units = [u for u in units if isinstance(u, dict)]
    match_indices = _building_identity_matches(row.building, [u.get("building") for u in plain_units])
    if not match_indices:
        return None

    values = set()
    for i in match_indices:
        value = plain_units[i].get(field)
        if isinstance(value, str) and not _is_blank(value):
            values.add(value.strip())
    if len(values) != 1:
        return None

    value = next(iter(values))
    if field == "postcode" and _source_postcode_conflict(row, value):
        return None
    return value


def _safe_float(value):
    """float(value), or None for anything that isn't genuinely numeric -
    a raw Gemini unit's own size_sqft is supposed to be "a plain number"
    per the extraction prompt, but is never schema-validated for this
    enrichment path (see _match_unit's own docstring), so a stray non-
    numeric value (e.g. a range string the prompt's own instructions say
    not to produce, but nothing here enforces) must not raise - treated
    as "doesn't match", never a crash."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ListingRow's own declared numeric type per field (see schema.py) - int
# for desks_max, float for the other two. Used by _coerced_unit_value so a
# raw Gemini number lands in `updates` as the SAME type ListingRow itself
# declares, rather than whatever bare type happened to survive JSON
# decoding (Gemini's JSON has no int/float distinction the way Python
# does - a whole-number desk count can decode as either).
_NUMERIC_UNIT_FIELD_TYPES = {"size_sqft": float, "desks_max": int, "rent_pcm": float, "rent_psf": float}


def _coerced_unit_value(field: str, value):
    """
    `value` (raw Gemini JSON for one matched unit's own field) coerced to
    the type ListingRow actually declares for `field`, or None when it
    isn't a genuine, usable value - never raises, same "one bad value
    degrades to no match" philosophy as _safe_float/_match_unit throughout
    this module.

    A string field (floor_unit/state_of_space/special_features) must be a
    real, non-blank str - see _apply_units_to_row's own docstring on why
    raw Gemini JSON is never trusted to already be the right type. A
    numeric field (size_sqft/desks_max/rent_pcm/rent_psf - see
    _NUMERIC_UNIT_FIELD_TYPES) must parse as a real number (see
    _safe_float); desks_max is additionally rounded to the nearest int,
    since ListingRow declares it Optional[int], not float.
    """
    if field in _NUMERIC_UNIT_FIELD_TYPES:
        number = _safe_float(value)
        if number is None:
            return None
        return round(number) if _NUMERIC_UNIT_FIELD_TYPES[field] is int else number
    if isinstance(value, str) and not _is_blank(value):
        return value
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

    Three independent sources, applied at three different scopes, from
    widest to narrowest - see the module docstring for the full "widest
    safe level" rationale and PROPERTY_LEVEL_FIELDS/BUILDING_LEVEL_FIELDS/
    UNIT_LEVEL_FIELDS/HIGH_RISK_UNIT_LEVEL_FIELDS for exactly which fields
    each scope covers. Each later, narrower source OVERWRITES the same key
    in `updates` set by an earlier, wider one when both apply, since a more
    specific value is always preferred over a less specific one:

    1. DOCUMENT-level (PROPERTY_LEVEL_FIELDS - contacts, special_features's
       property_features fallback - see units.property_features/units.
       contacts, set by _extract_brochure_units) - genuinely true of every
       row sharing this brochure_link regardless of which floor/unit or
       even which building it is, so both are applied even when neither of
       the two steps below finds anything. contacts is ALWAYS sourced this
       way, never from a per-unit dict - the extraction prompt only ever
       asks for contacts once, for the whole document (see extract.py's
       own PROMPT), so no per-unit dict ever carries its own "contacts" key.
    2. BUILDING-level (BUILDING_LEVEL_FIELDS - address_1/postcode/submarket,
       via _match_building_value - plus special_features's building_
       features fallback, via _match_building_feature) - applied whenever
       the row's own building exactly matches brochure content with
       distinct building-wide text/values, again regardless of whether a
       specific floor/unit can be matched - a building-wide fact is still
       true of every floor in that building even when this particular
       row's own floor can't be pinned down (see the module docstring's
       own point 8).
    3. UNIT-level (UNIT_LEVEL_FIELDS - state_of_space, floor_unit,
       size_sqft, desks_max, and special_features's own value when present;
       HIGH_RISK_UNIT_LEVEL_FIELDS - rent_pcm, rent_psf - see _match_unit)
       - only ever applied when a SPECIFIC floor/unit is confidently
       matched, since none of these is safe to assume applies beyond the
       one unit it was actually stated for.

    Every value taken from `units`/`unit` is coerced to ListingRow's own
    declared type for that field before being trusted (see _coerced_unit_
    value for numeric fields, plain isinstance(str) for text ones) - fields
    are typed on ListingRow, but `units`/`unit` are raw Gemini JSON that,
    unlike the primary upload path's own units (validated through
    ExtractedFields - see extract.extract()), never passes through any
    schema validation at all before reaching here. model_copy(update=...)
    below does NOT re-validate its update dict (unlike constructing a
    ListingRow directly) - it would otherwise silently accept whatever type
    Gemini's JSON happened to produce (e.g. a list, if a future prompt
    tweak ever led it to return special_features as an array instead of the
    prompt's own requested semicolon-separated string, or a numeric string
    for size_sqft) directly into a ListingRow field that every other reader
    (Excel writing, text diffing in master_merge.py) assumes is the
    declared type. Treated exactly like a blank value in that case -
    nothing to enrich from this field, not a reason to fail the whole row
    or the run.
    """
    updates = {}

    if units is not None:
        contacts = getattr(units, "contacts", None)
        if _is_blank(row.contacts) and isinstance(contacts, str) and not _is_blank(contacts):
            updates["contacts"] = contacts

        property_features = getattr(units, "property_features", None)
        if _is_blank(row.special_features) and isinstance(property_features, str) and not _is_blank(property_features):
            updates["special_features"] = property_features

        building_features = _match_building_feature(row, units)
        if _is_blank(row.special_features) and isinstance(building_features, str) and not _is_blank(building_features):
            updates["special_features"] = building_features  # more specific than property_features above

        for field in BUILDING_LEVEL_FIELDS:
            if not _is_blank(getattr(row, field)):
                continue
            value = _match_building_value(row, units, field)
            if value is not None:
                updates[field] = value

    if units:
        unit = _match_unit(row, units)
        if unit is not None:
            for field in UNIT_LEVEL_FIELDS + HIGH_RISK_UNIT_LEVEL_FIELDS:
                if not _is_blank(getattr(row, field)):
                    continue
                value = _coerced_unit_value(field, unit.get(field))
                if value is not None:
                    updates[field] = value  # a genuine unit match beats both fallbacks above

    if not updates:
        return row, []

    return row.model_copy(update=updates), list(updates.keys())


# --- Secondary enrichment source: a row's own floorplan_link ---
#
# A floor plan is a genuinely different document from a brochure (see
# schema.py's own floorplan_link docstring and brochure_link_resolver.
# finalize_brochure_link's floor-plan backstop) - never a marketing
# document, so it is never trusted for a property-wide or building-wide
# fact the way units.property_features/building_features are (see
# _apply_units_to_row above). It's a SECONDARY source only, ever
# considered after brochure-link enrichment has already been applied (see
# enrich_row/enrich_rows_grouped) and only for whatever that left blank -
# and only ever fills special_features, only at the one floor a floor plan
# document can be confidently matched to.

# The only field a floor plan is ever trusted to fill - never state_of_
# space or contacts, neither of which a floor plan drawing states at all.
FLOORPLAN_ENRICHABLE_FIELDS = ("special_features",)

FLOORPLAN_PROMPT = """You are extracting ONLY the explicit layout/feature annotations from a floor
plan drawing - this is NOT a marketing brochure. Do not infer amenities, certifications, or any fact
that applies to the whole property or building; only extract what is literally labeled or drawn on
this floor plan itself for the floor(s) it shows (e.g. desk counts, meeting rooms, a boardroom, phone
booths, a private office, a kitchen/breakout area, reception, WCs). Never invent or assume a feature
that isn't actually labeled on the drawing.

Identify each distinct floor shown (usually just one). For each one:
- floor_unit: the floor's own label if stated on the drawing (e.g. "3rd Floor"), otherwise null.
- special_features: every explicit layout annotation for that floor, semicolon-separated (e.g. "12
  desks; 10-person boardroom; 4-person meeting room; private office; soft seating; kitchen/breakout;
  dedicated WCs"). Leave null if nothing is explicitly labeled at all - never guess.

Return your answer as a single JSON object with this exact structure:

{
  "units": [
    {"floor_unit": "..." or null, "special_features": "..." or null}
  ]
}

Return ONLY this JSON object. No preamble, no explanation, no markdown code fences - just the raw JSON.
"""


def needs_floorplan_enrichment(row: ListingRow) -> bool:
    """True when at least one of FLOORPLAN_ENRICHABLE_FIELDS is genuinely
    blank on `row` - the floorplan-specific counterpart to needs_
    enrichment, checked before any network/Gemini activity."""
    return any(_is_blank(getattr(row, field)) for field in FLOORPLAN_ENRICHABLE_FIELDS)


def _is_eligible_floorplan_url(url) -> bool:
    """
    Like _is_eligible_brochure_url, but for floorplan_link - deliberately
    does NOT reject a floorplan-shaped URL (FLOORPLAN_URL_KEYWORDS), since
    that shape is exactly what's EXPECTED here, unlike for brochure_link
    where it's a rejection reason. Still requires an explicit http(s)
    scheme and rejects a bare company homepage/known social-profile domain
    (see is_generic_link) and a video link - a floor plan is never hosted
    at either of those either.
    """
    if _is_blank(url):
        return False
    if urlparse(url).scheme not in ("http", "https"):
        return False
    if is_generic_link(url):
        return False
    lowered = url.lower()
    return not any(bad in lowered for bad in ("youtube.com", "youtu.be"))


def eligible_rows_and_floorplans(rows: list):
    """
    Mirrors eligible_rows_and_brochures, for floorplan_link - (eligible_
    rows, unique_urls), pure, no network/Gemini call. A row only ever
    counts as floorplan-eligible by needs_floorplan_enrichment (special_
    features specifically - see that function's own docstring), not the
    wider needs_enrichment used for brochure_link, since floorplan
    enrichment is deliberately narrower in scope than brochure enrichment.
    """
    eligible = [r for r in rows if needs_floorplan_enrichment(r) and _is_eligible_floorplan_url(r.floorplan_link)]
    seen = set()
    unique_urls = []
    for r in eligible:
        if r.floorplan_link not in seen:
            seen.add(r.floorplan_link)
            unique_urls.append(r.floorplan_link)
    return eligible, unique_urls


@functools.lru_cache(maxsize=64)
def _extract_floorplan_units(url: str):
    """
    The raw floor-plan units (see FLOORPLAN_PROMPT) for `url` - a plain
    list of {"floor_unit", "special_features"} dicts, or None on any fetch/
    render/Gemini failure (identical failure semantics to _extract_
    brochure_units - see its own docstring). Deliberately returns a plain
    list, never a _BrochureUnits - a floor plan drawing has no concept of a
    document-wide or building-wide fact, so there is nothing for those
    extra attributes to ever hold; only unit-level (floor-matched)
    annotations are ever extracted from one (see _apply_floorplan_units_
    to_row).

    reject_floorplan_filename=False is passed to the fetch step (see
    _fetch_pdf_bytes) - the Box-share floorplan-filename rejection exists
    specifically to catch a BROCHURE link that's actually a mislabeled
    floor plan; here the floor plan IS the expected, correct content, so
    that same filename shape must never be rejected. accept_image_formats=
    True is passed for the same reason: a floor plan is routinely delivered
    as a scanned/exported PNG/JPEG rather than a vector PDF (see _looks_
    like_fetchable_document's own docstring for the confirmed real case) -
    never passed for the brochure fetch path, which stays PDF-only.

    Cross-run lru_cache, same "not the per-run dedup mechanism" caveat as
    _extract_brochure_units - see that function's own docstring.
    """
    data = _fetch_pdf_bytes(url, reject_floorplan_filename=False, accept_image_formats=True)
    if data is None:
        return None

    try:
        with extract._RENDER_LOCK:
            images = extract.render_pages(data)
        data = None
        raw = extract.render_and_extract(images, prompt=FLOORPLAN_PROMPT)
    except Exception as e:
        print(
            f"[brochure_enrichment] Could not read {url!r} as a floor plan ({e!r}) — skipping enrichment.",
            file=sys.stderr,
        )
        return None

    units = raw.get("units")
    return [u for u in units if isinstance(u, dict)] if isinstance(units, list) else []


def _match_floorplan_unit(row: ListingRow, units: list):
    """
    The single floor plan unit describing `row`'s own floor, or None.
    Unlike _match_unit, a floor plan unit carries no "building" field at
    all - every unit here already necessarily describes the SAME property
    this floorplan_link belongs to (rows are grouped by their own
    floorplan_link before this is ever called - see _enrich_rows_from_
    floorplans - exactly like brochure_link's own per-URL grouping), so no
    building comparison is needed or possible.

    If the floor plan names only one floor at all (len(units) == 1 - the
    overwhelming common real case: one document, one floor) AND that one
    unit either states no floor identity of its own, or the row itself
    states none, or the two agree (exact text or leading floor number),
    that unit applies - there's nothing else in the document to prefer over
    it, same "single match is a confident match" precedent as _match_unit's
    own building-level case. But a single unit whose OWN stated floor
    genuinely conflicts with the row's own (e.g. row floor_unit="2nd Floor"
    against a floor plan unit stating "3rd Floor") is never applied merely
    because it's the only unit in the document - a floor plan naming a
    different floor than the row is exactly as unsafe a match as no match
    at all, same "incorrect enrichment is worse than a blank field"
    philosophy as every other tier in this module. If it names SEVERAL
    distinct floors, only resolves via an exact floor_unit or floor-number
    match (see _floor_number) - an unresolved tie stays None, same
    conservative philosophy as _match_unit throughout.
    """
    units = [u for u in units if isinstance(u, dict)]
    if not units:
        return None

    if len(units) == 1:
        unit = units[0]
        unit_floor = unit.get("floor_unit")
        if _is_blank(unit_floor) or _is_blank(row.floor_unit):
            return unit
        if normalize_key(unit_floor) == normalize_key(row.floor_unit):
            return unit
        unit_floor_number = _floor_number(unit_floor)
        if unit_floor_number is not None and unit_floor_number == _floor_number(row.floor_unit):
            return unit
        return None  # the floor plan's own stated floor conflicts with the row's - never guess

    if row.floor_unit:
        row_floor_key = normalize_key(row.floor_unit)
        floor_matches = [u for u in units if normalize_key(u.get("floor_unit")) == row_floor_key]
        if len(floor_matches) == 1:
            return floor_matches[0]

        row_floor_number = _floor_number(row.floor_unit)
        if row_floor_number is not None:
            number_matches = [u for u in units if _floor_number(u.get("floor_unit")) == row_floor_number]
            if len(number_matches) == 1:
                return number_matches[0]

    return None


def _apply_floorplan_units_to_row(row: ListingRow, units):
    """
    (row_or_new_row, enriched_fields) - the floorplan-specific counterpart
    to _apply_units_to_row, deliberately much narrower: ONLY ever fills
    special_features (see FLOORPLAN_ENRICHABLE_FIELDS), and ONLY at the
    floor-matched level (see _match_floorplan_unit) - never a property- or
    building-wide fallback, since a floor plan drawing states neither.
    Returns `row` unchanged (same object) whenever there's nothing to
    apply: units is None/empty, special_features is already populated
    (never overwritten), no confident floor match, or the matched value
    isn't a genuine non-blank string (see _apply_units_to_row's own
    docstring on why raw Gemini JSON is never trusted to already be the
    right type).
    """
    if not units or not _is_blank(row.special_features):
        return row, []
    unit = _match_floorplan_unit(row, units)
    if unit is None:
        return row, []
    value = unit.get("special_features")
    if not isinstance(value, str) or _is_blank(value):
        return row, []
    return row.model_copy(update={"special_features": value}), ["special_features"]


def _enrich_rows_from_floorplans(
    rows: list, checkpoint_callback=None, already_processed: dict = None, url_checkpoint_callback=None,
) -> tuple:
    """
    (rows, log, stats) - secondary enrichment pass, run AFTER brochure-link
    enrichment (see enrich_row/enrich_rows_grouped, both of which call this
    only once the brochure-link pass has already applied) - fills ONLY
    FLOORPLAN_ENRICHABLE_FIELDS, ONLY at the floor-matched level (see
    _apply_floorplan_units_to_row), from each row's OWN floorplan_link, for
    whichever rows still need it.

    Sequential, not concurrent - unlike enrich_rows_grouped's own bounded
    worker pool (tuned against a real 126-brochure UNION file), a floor
    plan is only ever reached as a secondary source for whatever brochure
    enrichment already left blank, expected to be a much smaller/rarer
    worklist; the added complexity of a second worker pool isn't justified
    for this first version. Never raises - a fetch/Gemini failure for one
    floor plan simply leaves every row sharing it unchanged, same "no
    confident result is indistinguishable from a blank field" philosophy
    as the brochure path, with the identical per-row try/except belt-and-
    braces guard against a malformed unit entry.

    already_processed/checkpoint_callback/url_checkpoint_callback mirror
    enrich_rows_grouped's own identically-named parameters (see its own
    docstring): a URL already marked "ok" is never re-fetched, and both
    callbacks fire after EVERY floor plan (never batched - this worklist is
    already expected to be small/rare, see above, so there's no CHECKPOINT_
    EVERY-style interval to tune) so an interruption partway through this
    pass - which used to lose every row update made so far, since nothing
    here was ever persisted until the whole pass returned - now has exactly
    the same recoverable, per-URL checkpoint/resume behavior the brochure
    pass has always had, instead of silently discarding already-matched
    floor plan work or leaving no record that this pass was even reached.
    """
    already_processed = already_processed or {}
    eligible, unique_urls = eligible_rows_and_floorplans(rows)
    urls_to_fetch = [u for u in unique_urls if already_processed.get(u) != "ok"]
    current = list(rows)
    log = []
    floorplans_read_ok = 0
    floorplans_unavailable = 0
    processed_urls = {}
    stats = {
        "unique_floorplans_considered": len(unique_urls), "floorplans_read_ok": 0,
        "floorplans_unavailable": 0, "processed_urls": processed_urls,
    }
    if not urls_to_fetch:
        return current, log, stats

    indices_by_url = {}
    for i, row in enumerate(rows):
        if needs_floorplan_enrichment(row) and _is_eligible_floorplan_url(row.floorplan_link):
            indices_by_url.setdefault(row.floorplan_link, []).append(i)

    for url in urls_to_fetch:
        try:
            units = _extract_floorplan_units(url)
        except Exception as e:
            print(
                f"[brochure_enrichment] Unexpected error reading {url!r} as a floor plan ({e!r}) — skipping.",
                file=sys.stderr,
            )
            units = None

        if units is not None:
            floorplans_read_ok += 1
            processed_urls[url] = "ok"
        else:
            floorplans_unavailable += 1
            processed_urls[url] = "unavailable"

        for i in indices_by_url[url]:
            try:
                new_row, fields = _apply_floorplan_units_to_row(current[i], units)
            except Exception as e:
                print(
                    f"[brochure_enrichment] Could not apply floor plan units from {url!r} to "
                    f"{current[i].building!r} ({e!r}) — leaving this row unchanged.",
                    file=sys.stderr,
                )
                new_row, fields = current[i], []
            current[i] = new_row
            if fields:
                log.append({
                    "building": new_row.building,
                    "floor_unit": new_row.floor_unit,
                    "fields": fields,
                    "brochure_link": new_row.brochure_link,
                    "floorplan_link": url,
                })

        if checkpoint_callback:
            checkpoint_callback(list(current))
        if url_checkpoint_callback:
            # Unlike the brochure pass' own url_checkpoint_callback (see
            # enrich_rows_grouped's own docstring), the TOTAL is passed
            # here too, not just the cumulative dict - the caller can't
            # know len(unique_urls) upfront the way it does for brochures,
            # since floorplan eligibility depends on what the brochure pass
            # already filled in (see this function's own docstring).
            url_checkpoint_callback({**already_processed, **processed_urls}, len(unique_urls))

    stats["floorplans_read_ok"] = floorplans_read_ok
    stats["floorplans_unavailable"] = floorplans_unavailable
    return current, log, stats


def enrich_row(row: ListingRow):
    """
    (row, enriched_fields) - row is `row` unchanged (the exact same object)
    whenever nothing was enriched (nothing missing, no eligible brochure
    link, fetch/parse failure, or no confident match - all indistinguishable
    to the caller by design, since incorrect enrichment is worse than a
    blank field and none of these cases should behave differently from any
    other), or a NEW ListingRow (model_copy) with one or more of
    ENRICHABLE_FIELDS filled in from a genuinely applicable, non-blank
    brochure value for a field that was blank - see _apply_units_to_row's
    own docstring for exactly which fields require a specific unit match
    vs. which apply from the whole document. enriched_fields lists exactly
    which fields changed - [] whenever row is returned as-is.

    Fetches row's own brochure_link every call (through the cross-run
    lru_cache, see _extract_brochure_units) - correct for a single row in
    isolation, but a caller enriching many rows at once should use
    enrich_rows_grouped instead, which fetches each distinct brochure link
    exactly once regardless of how many rows share it (see that function's
    own docstring).

    Never mutates the input row, never raises - see _extract_brochure_units/
    _fetch_pdf_bytes for where every real failure mode is caught.

    After the brochure-link pass above, also tries the row's own
    floorplan_link (see _apply_floorplan_units_to_row) for whatever's still
    blank - a secondary, narrower source, never competing with a genuine
    brochure-sourced value (see FLOORPLAN_ENRICHABLE_FIELDS's own
    docstring). enriched_fields reflects fields changed by EITHER pass,
    still with no duplicate entries.
    """
    working_row, fields = row, []
    if needs_enrichment(row) and _is_eligible_brochure_url(row.brochure_link):
        units = _extract_brochure_units(row.brochure_link)
        working_row, fields = _apply_units_to_row(row, units)

    if needs_floorplan_enrichment(working_row) and _is_eligible_floorplan_url(row.floorplan_link):
        floorplan_units = _extract_floorplan_units(row.floorplan_link)
        working_row, floorplan_fields = _apply_floorplan_units_to_row(working_row, floorplan_units)
        fields = fields + [f for f in floorplan_fields if f not in fields]

    return working_row, fields


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


def enrich_rows_grouped(
    rows: list, progress_callback=None, checkpoint_callback=None, max_workers: int = DEFAULT_MAX_WORKERS,
    already_processed: dict = None, url_checkpoint_callback=None,
    floorplan_already_processed: dict = None, floorplan_checkpoint_callback=None,
    floorplan_url_checkpoint_callback=None,
):
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

    already_processed ({url: "ok" | "unavailable"}, from a PRIOR call of
    this same function against this same staging file - see storage.
    file_store's own processed_urls persistence) lets a caller RESUME an
    interrupted run: a URL already marked "ok" here is skipped entirely -
    never re-fetched, never re-sent to Gemini, full stop - since "blank
    special_features" alone can never tell a caller whether a brochure was
    already successfully checked and genuinely had nothing to contribute,
    or was never checked at all (see this module's own ENRICHABLE_FIELDS
    docstring on why a blank value is never itself evidence of anything).
    A URL marked "unavailable" is NOT skipped - retried exactly like a
    never-seen URL, since a fetch/Gemini failure may well have been
    transient; this is bounded by the caller only ever resuming in
    response to an explicit action (see pages/2_Review_and_Master.py's own
    "Continue enrichment"), never an automatic unbounded retry loop.
    Defaults to {} (nothing previously processed - identical to every
    prior behavior before this parameter existed).

    stats["processed_urls"] ({url: "ok" | "unavailable"}) reports the
    outcome for every URL actually FETCHED during THIS call only (never
    includes a URL skipped via already_processed) - the caller merges this
    into its own persisted cumulative record; deliberately not merged with
    already_processed internally, so a caller can always tell "what did
    THIS call itself just learn" apart from "what was already known
    coming in". url_checkpoint_callback(processed_urls_so_far), if given,
    fires at the exact same points as checkpoint_callback (see its own
    docstring) with the CUMULATIVE dict (already_processed merged with
    every outcome learned so far this call) - a separate callback, not a
    second argument added to checkpoint_callback, so every existing caller
    that only ever passes checkpoint_callback is completely unaffected.

    floorplan_already_processed/floorplan_checkpoint_callback/floorplan_
    url_checkpoint_callback are the SAME resume/checkpoint contract as
    already_processed/checkpoint_callback/url_checkpoint_callback above,
    applied to the secondary floorplan-link pass (see _enrich_rows_from_
    floorplans) instead of the brochure-link pass - deliberately separate
    parameters, never a shared/reused callback, since a floorplan URL's own
    processed-state must never be conflated with a brochure URL's (see
    run_brochure_enrichment, the one real caller that wires both passes to
    two distinct persisted keys). Without these, an interruption during the
    floorplan pass - which only ever starts once every brochure is already
    marked "done" - silently lost whatever it had already matched and left
    the caller's own "how much remains" count blind to it entirely (see
    this function's own returned stats: unique_floorplans_considered/
    floorplans_read_ok/floorplans_unavailable/floorplan_processed_urls).
    """
    progress_callback = progress_callback or (lambda done, total, label: None)
    already_processed = already_processed or {}
    eligible, unique_urls = eligible_rows_and_brochures(rows)
    urls_to_fetch = [u for u in unique_urls if already_processed.get(u) != "ok"]

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
    processed_urls = {}

    if not unique_urls or not urls_to_fetch:
        progress_callback(0, 0, None)
        current, floorplan_log, floorplan_stats = _enrich_rows_from_floorplans(
            current, checkpoint_callback=floorplan_checkpoint_callback,
            already_processed=floorplan_already_processed,
            url_checkpoint_callback=floorplan_url_checkpoint_callback,
        )
        log = log + floorplan_log
        return current, log, {
            "unique_brochures_considered": len(unique_urls), "brochures_read_ok": 0,
            "brochures_unavailable": 0, "rows_eligible": len(eligible), "rows_enriched": len(log),
            "processed_urls": processed_urls,
            "unique_floorplans_considered": floorplan_stats["unique_floorplans_considered"],
            "floorplans_read_ok": floorplan_stats["floorplans_read_ok"],
            "floorplans_unavailable": floorplan_stats["floorplans_unavailable"],
            "floorplan_processed_urls": floorplan_stats["processed_urls"],
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
        futures = [executor.submit(_fetch_one, url) for url in urls_to_fetch]
        for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            url, units = future.result()
            _trim_memory()

            # is not None, not plain truthiness - _extract_brochure_units
            # returns [] (falsy) for a brochure that was successfully
            # fetched and read but genuinely has no units in it, which is
            # a real "checked, nothing there" outcome, never the same as
            # None's "couldn't read this at all" (see that function's own
            # docstring). Conflating the two here would wrongly mark a
            # perfectly good, already-checked brochure "unavailable" -
            # exactly the ambiguity per-URL status tracking exists to
            # avoid (a caller resuming later must retry a genuine failure
            # but must never re-fetch/re-bill Gemini for one that simply
            # had nothing to contribute).
            if units is not None:
                brochures_read_ok += 1
                processed_urls[url] = "ok"
            else:
                brochures_unavailable += 1
                processed_urls[url] = "unavailable"

            for i in indices_by_url[url]:
                # Confirmed real, reproducible bug this guards against: raw
                # units come straight from Gemini's own JSON (see
                # _extract_brochure_units's own docstring - never validated
                # against ExtractedFields the way the direct-PDF-upload
                # path's units are), so a single malformed entry (e.g. a
                # stray non-dict item, or a non-numeric size_sqft) makes
                # _match_unit raise - previously UNCAUGHT here, in the main
                # thread, entirely outside _fetch_one's own try/except,
                # which crashed this whole function (and the surrounding
                # Streamlit script) over ONE bad brochure - every remaining
                # brochure lost, not just that one. Treated exactly like
                # "no confident match" on failure: the row is left
                # unchanged, never guessed at from data that couldn't even
                # be read correctly.
                try:
                    new_row, fields = _apply_units_to_row(rows[i], units)
                except Exception as e:
                    print(
                        f"[brochure_enrichment] Could not apply units from {url!r} to "
                        f"{rows[i].building!r} ({e!r}) — leaving this row unchanged.",
                        file=sys.stderr,
                    )
                    new_row, fields = rows[i], []
                current[i] = new_row
                if fields:
                    log.append({
                        "building": new_row.building,
                        "floor_unit": new_row.floor_unit,
                        "fields": fields,
                        "brochure_link": new_row.brochure_link,
                    })

            progress_callback(done, len(urls_to_fetch), first_label.get(url))

            since_checkpoint += 1
            is_last = done == len(urls_to_fetch)
            if (checkpoint_callback or url_checkpoint_callback) and (since_checkpoint >= CHECKPOINT_EVERY or is_last):
                if checkpoint_callback:
                    checkpoint_callback(list(current))
                if url_checkpoint_callback:
                    url_checkpoint_callback({**already_processed, **processed_urls})
                since_checkpoint = 0

    # Secondary pass: whatever brochure-link enrichment above still left
    # blank, a row's OWN floorplan_link (see FLOORPLAN_ENRICHABLE_FIELDS)
    # may still be able to fill - run only after the brochure pass has
    # fully applied, since floorplan enrichment is deliberately narrower
    # and never meant to compete with a genuine brochure-sourced value.
    current, floorplan_log, floorplan_stats = _enrich_rows_from_floorplans(
        current, checkpoint_callback=floorplan_checkpoint_callback,
        already_processed=floorplan_already_processed,
        url_checkpoint_callback=floorplan_url_checkpoint_callback,
    )
    log = log + floorplan_log

    stats = {
        "unique_brochures_considered": len(unique_urls),
        "brochures_read_ok": brochures_read_ok,
        "brochures_unavailable": brochures_unavailable,
        "rows_eligible": len(eligible),
        "rows_enriched": len(log),
        "processed_urls": processed_urls,
        "unique_floorplans_considered": floorplan_stats["unique_floorplans_considered"],
        "floorplans_read_ok": floorplan_stats["floorplans_read_ok"],
        "floorplans_unavailable": floorplan_stats["floorplans_unavailable"],
        "floorplan_processed_urls": floorplan_stats["processed_urls"],
    }
    return current, log, stats


def run_brochure_enrichment(
    rows: list, staging_path: str, already_processed: dict, floorplan_already_processed: dict = None,
) -> list:
    """
    The Streamlit-aware orchestration shared by BOTH callers that ever run
    enrich_rows_grouped against a real staging file: app.py's automatic
    run right after a fresh upload (already_processed={} - nothing to
    resume yet) and pages/2_Review_and_Master.py's "Continue enrichment"
    action on an interrupted one (already_processed/floorplan_already_
    processed=whatever get_staging_enrichment_summary's own "processed_
    urls"/"floorplan_processed_urls" already recorded). enrich_rows_grouped
    itself stays the pure, callback-driven core with no Streamlit/storage
    dependency of its own (see its own docstring) - this wrapper is the one
    place that actually renders a progress bar and persists both row-level
    progress and per-URL state, for BOTH the brochure and floorplan passes,
    so the two callers share identical checkpointing/persistence behavior
    rather than two independently-maintained copies of it.

    Renders its own progress bar (caller prints any introductory caption
    first, e.g. "N row(s) have missing information..." vs. "Resuming...",
    since that wording legitimately differs per caller) and a final
    "Brochure enrichment complete: ..." caption once every remaining
    brochure AND floorplan has been processed. Returns the enriched rows so
    the caller can reassign its own `rows` variable to the final state.

    Persists incrementally exactly like the automatic run always has:
    update_staging_rows + set_staging_enrichment_progress (status=
    "in_progress", the FULL cumulative processed_urls so far, for both
    passes) at every enrich_rows_grouped checkpoint, and set_staging_
    enrichment_summary (status="complete") only once this call's own
    remaining brochures AND floorplans are entirely done - so an
    interruption partway through a RESUME, during EITHER pass, leaves
    exactly the same kind of recoverable, explicit "in_progress" record a
    first run's own interruption would, never silently reverting to
    looking complete or looking like nothing ever ran, and never losing
    whatever the OTHER pass had already recorded (see known_floorplan_*
    below - every progress write here always supplies its own full current
    knowledge of BOTH passes, never just whichever one just changed, since
    set_staging_enrichment_progress fully replaces the persisted record on
    every call).
    """
    floorplan_already_processed = floorplan_already_processed or {}
    eligible, unique_urls = eligible_rows_and_brochures(rows)
    urls_to_fetch = [u for u in unique_urls if already_processed.get(u) != "ok"]

    # progress bar + the "keep this page open" caption share ONE placeholder
    # (progress_slot.container(), not two separate st.empty() calls) so the
    # single progress_slot.empty() below clears both together - the caption
    # must disappear at exactly the same moment the progress bar does,
    # never lingering or vanishing separately.
    progress_slot = st.empty()
    with progress_slot.container():
        bar = st.progress(0.0, text=f"Enriching from brochures — 0 / {len(urls_to_fetch)}")
        st.caption(
            "Please keep this page open and avoid clicking or navigating while brochures are being "
            "enriched. Your progress is saved if the process is interrupted."
        )

    # The floorplan pass' own true total is only known once it actually
    # runs (floorplan eligibility depends on what the brochure pass already
    # filled in - see _enrich_rows_from_floorplans' own docstring), so this
    # starts as a lower bound (at least this many are already known from a
    # prior resume) and is corrected the moment the floorplan pass' own
    # checkpoint first fires - never left at a stale/guessed value that
    # could otherwise make "remaining" go negative.
    known_floorplan_processed_urls = dict(floorplan_already_processed)
    known_unique_floorplans_considered = len(known_floorplan_processed_urls)

    # Written BEFORE the run starts (not just at checkpoints) so even an
    # interruption in the first few seconds - before a single brochure has
    # completed, let alone reached CHECKPOINT_EVERY - still leaves an
    # "in_progress" record in meta.json rather than none at all (see
    # set_staging_enrichment_progress's own docstring on why a missing
    # record is otherwise indistinguishable from "nothing was eligible").
    set_staging_enrichment_progress(
        staging_path, already_processed, len(unique_urls),
        floorplan_processed_urls=known_floorplan_processed_urls,
        unique_floorplans_considered=known_unique_floorplans_considered,
    )

    def on_progress(done, total, label):
        if not total:
            return
        text = f"Enriching from brochures — {done} / {total}"
        if label:
            text += f" ({label})"
        bar.progress(done / total, text=text)

    def on_checkpoint(rows_so_far):
        update_staging_rows(staging_path, rows_so_far)

    def on_url_checkpoint(processed_urls_so_far):
        set_staging_enrichment_progress(
            staging_path, processed_urls_so_far, len(unique_urls),
            floorplan_processed_urls=known_floorplan_processed_urls,
            unique_floorplans_considered=known_unique_floorplans_considered,
        )

    def on_floorplan_url_checkpoint(floorplan_processed_urls_so_far, unique_floorplans_considered):
        nonlocal known_floorplan_processed_urls, known_unique_floorplans_considered
        known_floorplan_processed_urls = floorplan_processed_urls_so_far
        known_unique_floorplans_considered = unique_floorplans_considered
        # Brochures are always fully done by the time the floorplan pass
        # even starts (see enrich_rows_grouped's own docstring) - already_
        # processed/len(unique_urls) here is therefore still this call's
        # own correct, complete brochure-side record, not stale.
        set_staging_enrichment_progress(
            staging_path, already_processed, len(unique_urls),
            floorplan_processed_urls=known_floorplan_processed_urls,
            unique_floorplans_considered=known_unique_floorplans_considered,
        )

    enriched_rows, _log, stats = enrich_rows_grouped(
        rows, progress_callback=on_progress, checkpoint_callback=on_checkpoint,
        url_checkpoint_callback=on_url_checkpoint, already_processed=already_processed,
        floorplan_already_processed=floorplan_already_processed,
        floorplan_checkpoint_callback=on_checkpoint,
        floorplan_url_checkpoint_callback=on_floorplan_url_checkpoint,
    )
    progress_slot.empty()

    cumulative_processed_urls = {**already_processed, **stats["processed_urls"]}
    cumulative_floorplan_processed_urls = {**floorplan_already_processed, **stats["floorplan_processed_urls"]}
    update_staging_rows(staging_path, enriched_rows)
    set_staging_enrichment_summary(
        staging_path, stats, cumulative_processed_urls,
        floorplan_processed_urls=cumulative_floorplan_processed_urls,
        unique_floorplans_considered=stats["unique_floorplans_considered"],
    )

    counts = _derive_cumulative_counts(cumulative_processed_urls)
    summary = (
        f"{len(unique_urls)} unique brochure(s) considered, "
        f"{counts['ok']} read successfully, {stats['rows_enriched']} row(s) enriched this run."
    )
    if counts["unavailable"]:
        summary += f" {counts['unavailable']} brochure(s) could not be processed."
    if stats["unique_floorplans_considered"]:
        floorplan_counts = _derive_cumulative_counts(cumulative_floorplan_processed_urls)
        summary += (
            f" {stats['unique_floorplans_considered']} unique floor plan(s) also considered, "
            f"{floorplan_counts['ok']} read successfully."
        )
    st.caption(f"Brochure enrichment complete: {summary}")

    return enriched_rows


def _derive_cumulative_counts(processed_urls: dict) -> dict:
    return {
        "ok": sum(1 for v in processed_urls.values() if v == "ok"),
        "unavailable": sum(1 for v in processed_urls.values() if v == "unavailable"),
    }
