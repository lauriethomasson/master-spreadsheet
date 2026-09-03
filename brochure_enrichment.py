"""
brochure_enrichment.py

Secondary enrichment for spreadsheet- and email-extracted ListingRows -
fetches a row's OWN brochure_link and reads it (reusing extract.py's exact
PDF-vision extraction pipeline via extract_raw_units) purely to fill fields
the original source left genuinely blank. The original upload is always
the source of truth: enrichment only ever fills a field that is currently
blank, never overwrites a populated one - special_features is the one
exception (see _apply_units_to_row's own docstring): a non-blank value is
kept as the first, most-specific segment of a combined value, never
discarded or overwritten outright - and any fetch/parse failure simply
leaves the row exactly as extracted - it must never fail the surrounding
upload.

Deliberately scoped to spreadsheet AND email rows only, never a PDF upload -
a PDF is already extracted from the actual brochure itself (enriching it
from itself would be circular and pointless). Every field-scope/matching
rule below (see needs_enrichment/_match_unit/_apply_units_to_row) operates
purely on a ListingRow's own field values - it has no notion of which
upload type a row came from at all; app.py's own upload loop is the only
place that decides WHICH rows this module ever gets called on.

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
(see _apply_units_to_row's own docstring for the exact precedence).
special_features is the one exception: rather than the narrowest present
scope winning outright and the wider ones being discarded, every scope
that's genuinely present is combined into one value (see that same
docstring for why).
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

Runs automatically, immediately after a spreadsheet OR email upload's base
rows are staged (see app.py's own is_spreadsheet_source/is_email_source and
save_staging_file) - NOT a separate, later, user-triggered action any
more. The base rows are staged
FIRST, with zero brochure/Gemini calls, specifically so that if enrichment
then crashes, times out, or is interrupted, the original extraction already
exists safely on disk; enrichment only ever REWRITES that same staging file
afterward (see storage.file_store.update_staging_rows), incrementally, as
results come in - never something the base extraction's own success depends
on.
"""

import base64
import concurrent.futures
import ctypes
import functools
import os
import platform
import re
import sys
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import streamlit as st

import extract
from brochure_link_resolver import (
    REQUEST_TIMEOUT, USER_AGENT, is_canva_view_link, is_floorplan_not_brochure_url, is_generic_link,
    is_gpe_flipbook_link, is_kitt_brochure_preview_link, is_pitch_view_link, looks_like_url, resolve_brochure_link,
)
import geocode
from house_number import LEADING_HOUSE_NUMBER_RE, house_numbers_conflict, leading_house_number
from master_merge import _STREET_SUFFIX_EXPANSIONS, normalize_key
from schema import ListingRow
from storage.file_store import set_staging_enrichment_progress, set_staging_enrichment_summary, update_staging_rows

# --- Document processing status vocabulary ---------------------------------
#
# Distinguishes WHY a brochure/floorplan document produced no enrichment -
# previously collapsed into a single "unavailable" (see processed_urls
# elsewhere in this module), which could mean a 404, a corrupt PDF, a
# Gemini failure, or a document that read fine but had nothing usable, with
# no way for a user to tell which. A constant-style status string (never a
# raised exception, never surfaced as a stack trace) is attached per
# document (see _StatusCapture/_record_status) and, for a specific row,
# possibly refined further (extracted_but_ambiguous - see
# _row_had_ambiguous_match) purely for diagnostics; it never changes
# whether/what gets enriched, only what the caller can report about why.
STATUS_NO_DOCUMENT = "no_document"
STATUS_INVALID_PLACEHOLDER = "invalid_placeholder"
STATUS_UNSUPPORTED_LINK_TYPE = "unsupported_link_type"
STATUS_FETCH_FAILED = "fetch_failed"
STATUS_RENDER_FAILED = "render_failed"
STATUS_EXTRACTION_FAILED = "extraction_failed"
STATUS_EXTRACTED_NO_USEFUL_DATA = "extracted_no_useful_data"
STATUS_EXTRACTED_BUT_AMBIGUOUS = "extracted_but_ambiguous"
STATUS_EXTRACTED_SUCCESSFULLY = "extracted_successfully"

# The subset of statuses worth surfacing to a user as a "document issue" -
# no_document/invalid_placeholder are completely normal (this property
# simply has no brochure/floorplan - not a failure of anything), and
# extracted_no_useful_data/extracted_successfully both mean the document
# itself was read without any error at all. Only a genuine failure or
# unresolved ambiguity belongs in a "these need a look" list.
ISSUE_STATUSES = (
    STATUS_UNSUPPORTED_LINK_TYPE, STATUS_FETCH_FAILED, STATUS_RENDER_FAILED,
    STATUS_EXTRACTION_FAILED, STATUS_EXTRACTED_BUT_AMBIGUOUS,
)

# Friendly, user-facing wording per issue status - never a raw exception
# message, HTTP status code, stack trace, or URL (which may carry a
# signed/tokenized query string - see this module's own point on never
# logging sensitive query parameters to a user-facing surface). Kept
# deliberately generic rather than naming a specific website/provider (e.g.
# never "Canva" by name) - a webpage that doesn't expose a downloadable
# document reads the same to this pipeline regardless of which site it is,
# and hardcoding one host's name here would be exactly the kind of
# provider-specific special case this whole module avoids elsewhere.
_ISSUE_LABELS = {
    STATUS_UNSUPPORTED_LINK_TYPE: "This type of link can't currently be read",
    STATUS_FETCH_FAILED: "The document couldn't be opened",
    STATUS_RENDER_FAILED: "The document couldn't be read",
    STATUS_EXTRACTION_FAILED: "Information couldn't be extracted from this document",
    STATUS_EXTRACTED_BUT_AMBIGUOUS: (
        "This document covers multiple floors — we couldn't confirm which one matches this listing, "
        "so your data wasn't changed."
    ),
}

# render_canva_page_async's own navigation-status check (canva_renderer/
# app.py) raises with this exact substring when Canva itself answered the
# initial page load with a non-2xx status - a genuinely CONFIRMED dead/
# expired/access-revoked link, never a one-off navigation hiccup. That
# reason string flows verbatim into this app's own _record_status(STATUS_
# RENDER_FAILED, ...) detail at _fetch_canva_rendered_page's generic
# failure branch (f"Canva render failed: {reason}") - matched here as
# plain substring text, deliberately, since this app and canva_renderer
# are two independently deployed services with no shared import; the
# renderer's own reason string IS the contract between them. A bare
# navigation timeout/exception (canva_renderer's own "navigation failed or
# timed out (...)" message) never contains this substring - weak, one-off-
# glitch-shaped evidence ListingRow.brochure_link_broken must never be set
# from (see that field's own docstring).
_CONFIRMED_DEAD_CANVA_LINK_MARKER = "navigation returned HTTP"


def _is_confirmed_dead_canva_link(detail) -> bool:
    return bool(detail) and _CONFIRMED_DEAD_CANVA_LINK_MARKER in detail


def issue_label(status: str) -> str:
    """User-facing wording for `status` - see _ISSUE_LABELS. Falls back to
    the raw status string (never raises) for any status not in that map,
    which should only ever happen for a non-issue status a caller mistakenly
    passed here."""
    return _ISSUE_LABELS.get(status, status)


# Name of the env var pointing at the separate Canva-rendering service
# (see canva_renderer/README.md) - e.g. "https://canva-renderer-xyz.run.
# app". Unset (the default for any environment that hasn't deployed that
# service) means Canva support stays exactly as it was under commit
# a67e337: a public Canva "view" link is still recognized on sight, but
# still never fetched at all - see _canva_renderer_configured's own call
# sites in classify_link_eligibility/_is_eligible_brochure_url/_is_
# eligible_floorplan_url. Deliberately a plain env var check, not a
# network call - these three functions are all documented as "purely from
# the URL's own text, no network activity", and checking whether a
# separate service has been deployed at all is a local, static fact, not
# a probe of that service's own health.
CANVA_RENDERER_URL_ENV_VAR = "CANVA_RENDERER_URL"


def _canva_renderer_configured() -> bool:
    return bool(os.environ.get(CANVA_RENDERER_URL_ENV_VAR))


def _render_platform_label(url: str) -> str:
    """Which of the four URL shapes canva_renderer/ supports `url`
    matches, as the exact string used throughout this module's own log
    lines/diagnostics ("Canva"/"Pitch"/"GPE Flipbook"/"Kitt") - factored
    out so every caller that needs this label (there are several - grep
    this module for its own call sites) shares ONE check, rather than each
    hand-rolling its own "Pitch" if is_pitch_view_link(url) else
    "Canva"-style ternary that would need updating individually every time
    a new platform is added. GPE checked before Pitch: is_gpe_flipbook_
    link's own regex is disjoint from is_pitch_view_link's (different
    host), so the order between them doesn't actually matter for
    correctness, but checking the newer, narrower shape first reads
    slightly more naturally than the reverse; same reasoning for checking
    Kitt before the Canva fallback. Callers are expected to only ever call
    this once one of the four has already been confirmed True (e.g. via
    classify_link_eligibility) - returns "Canva" as a fallback for a URL
    matching none of the four, never raises, but that fallback is not
    expected to be reached in practice."""
    if is_gpe_flipbook_link(url):
        return "GPE Flipbook"
    if is_pitch_view_link(url):
        return "Pitch"
    if is_kitt_brochure_preview_link(url):
        return "Kitt"
    return "Canva"


def classify_link_eligibility(url, reject_floorplan_shaped: bool = True):
    """
    None if `url` is eligible for a real fetch attempt (the existing
    _is_eligible_brochure_url/_is_eligible_floorplan_url gate - both share
    this same underlying logic, see _is_eligible_brochure_url's own
    docstring) - otherwise the STATUS_* constant explaining why not, purely
    from the URL's own text, no network activity:
    - STATUS_NO_DOCUMENT: blank/nothing stated at all;
    - STATUS_INVALID_PLACEHOLDER: non-blank but not genuinely URL-shaped
      (see brochure_link_resolver.looks_like_url) - "TBC"/"N/A"/"-"/
      "Coming Soon" and similar provider placeholders meaning "none yet";
    - STATUS_UNSUPPORTED_LINK_TYPE: a real URL, but one of the shapes this
      pipeline already knows it can't use (a bare generic homepage/known
      social-profile domain, a video link, a Canva public "view" link, a
      Pitch.com public "view" link, or Kitt's own brochure-preview app
      link - see brochure_link_resolver.is_canva_view_link/is_pitch_view_
      link/is_kitt_brochure_preview_link's own docstrings on why a plain
      HTTP fetch can never retrieve real content from any of them,
      confirmed directly against real examples rather than assumed,
      UNLESS _canva_renderer_configured (that service now handles all
      three) - a Google Drive FOLDER share link, see _is_google_drive_
      folder_link's own docstring,
      confirmed real against Kitt's Availability file's own floorplan_link
      values - or, only when reject_floorplan_shaped, brochure_link's own
      rule, see _is_eligible_brochure_url - a floor-plan-labeled link where
      a brochure was expected; floorplan_link's own check, _is_eligible_
      floorplan_url, passes reject_floorplan_shaped=False since that shape
      is exactly what's expected there, never a rejection reason).
    """
    if _is_blank(url):
        return STATUS_NO_DOCUMENT
    if not looks_like_url(url):
        return STATUS_INVALID_PLACEHOLDER
    if urlparse(url).scheme not in ("http", "https"):
        return STATUS_UNSUPPORTED_LINK_TYPE
    if is_generic_link(url):
        return STATUS_UNSUPPORTED_LINK_TYPE
    if (
        is_canva_view_link(url) or is_pitch_view_link(url) or is_gpe_flipbook_link(url)
        or is_kitt_brochure_preview_link(url)
    ) and not _canva_renderer_configured():
        return STATUS_UNSUPPORTED_LINK_TYPE
    if _is_google_drive_folder_link(url):
        return STATUS_UNSUPPORTED_LINK_TYPE
    if reject_floorplan_shaped and is_floorplan_not_brochure_url(url):
        return STATUS_UNSUPPORTED_LINK_TYPE
    lowered = url.lower()
    if any(bad in lowered for bad in _REJECTED_URL_KEYWORDS):
        return STATUS_UNSUPPORTED_LINK_TYPE
    return None


def _ineligible_link_issues(rows: list, needs_fn, url_attr: str, reject_floorplan_shaped: bool) -> list:
    """
    One {"building", "floor_unit", "status"} entry per row that needs_fn(row)
    (needs_enrichment/needs_floorplan_enrichment) but whose own url_attr
    link ("brochure_link"/"floorplan_link") isn't even eligible for a fetch
    attempt (see classify_link_eligibility) - these rows never reach
    enrich_rows_grouped's/_enrich_rows_from_floorplans' own real fetch loop
    at all (see eligible_rows_and_brochures/eligible_rows_and_floorplans),
    so without this they'd be silently, indistinguishably absent from any
    diagnostic - previously "no brochure worked" and "no brochure existed"
    looked identical to a caller.

    Deliberately excludes a row that doesn't need enrichment at all
    (nothing wrong, nothing to report) and STATUS_NO_DOCUMENT/STATUS_
    INVALID_PLACEHOLDER (see ISSUE_STATUSES's own docstring - a genuinely
    blank/placeholder link is completely normal, never an "issue").

    An entry whose STATUS_UNSUPPORTED_LINK_TYPE reason is specifically a
    Canva public "view" link (see is_canva_view_link) also carries an
    additive "unsupported_reason": "canva" key - not a new status, just an
    extra fact the Review page's own compact summary uses to say "most of
    these are Canva links" ONLY when that's actually true (see pages/2_
    Review_and_Master.py's own document-issues summary), never guessed or
    assumed. Every OTHER unsupported-link-type cause (a bare homepage, a
    known social-profile domain, a video link) omits this key entirely, so
    a caller that only ever checked "status" before this is completely
    unaffected.
    """
    issues = []
    for row in rows:
        if not needs_fn(row):
            continue
        url = getattr(row, url_attr)
        status = classify_link_eligibility(url, reject_floorplan_shaped=reject_floorplan_shaped)
        if status in ISSUE_STATUSES:
            issue = {"building": row.building, "floor_unit": row.floor_unit, "status": status}
            if status == STATUS_UNSUPPORTED_LINK_TYPE and is_canva_view_link(url):
                issue["unsupported_reason"] = "canva"
            issues.append(issue)
    return issues


# Thread-local so concurrent enrich_rows_grouped workers (see that
# function's own bounded ThreadPoolExecutor) never cross-contaminate each
# other's status - each worker thread calls _fetch_one, which sets up its
# OWN sink via _StatusCapture before calling _extract_brochure_units, so
# whichever thread actually executes a given fetch/render/Gemini call is
# also the one whose sink _record_status writes into.
_status_sink_local = threading.local()


def _record_status(status: str, detail: str = None) -> None:
    """
    Records `status` (plus an optional short, non-sensitive `detail`
    string) into whatever sink the CURRENT thread's innermost active
    _StatusCapture set up - a complete no-op when no capture is active,
    which is the case for every existing caller/test that never opts into
    this (see _StatusCapture's own docstring) - so adding this call
    anywhere in the existing fetch/render/extract functions changes
    NOTHING about their behavior or return value for any caller that
    doesn't use _StatusCapture. Only the FIRST call for a given capture
    wins - a function that fails at an early stage (e.g. fetch) never has
    a later, unrelated call (there shouldn't be one, but this stays
    defensive) overwrite that with a less specific status.
    """
    sink = getattr(_status_sink_local, "sink", None)
    if sink is not None and "status" not in sink:
        sink["status"] = status
        if detail:
            sink["detail"] = detail


class _StatusCapture:
    """
    Context manager - while active on the CURRENT thread, any _record_status
    call (from _fetch_pdf_bytes/_fetch_box_shared_pdf/_extract_brochure_
    units/_extract_floorplan_units - none of which take a new parameter or
    change their return value for this) writes into `sink` (a plain dict
    the caller owns and reads afterward) instead of being a no-op. Restores
    whatever capture (if any) was active before, so nesting is safe even
    though nothing in this module currently nests captures.

    Deliberately NOT a new parameter threaded through _extract_brochure_
    units/_extract_floorplan_units - both are @functools.lru_cache'd on
    `url` alone, and a mutable dict argument would break that (dicts aren't
    hashable) - and NOT a change to _fetch_pdf_bytes's own signature either,
    so every existing call site/test/mock of any of these functions is
    completely unaffected by this existing.

    On a CROSS-RUN cache hit (see _extract_brochure_units's own docstring -
    the SAME url was already fetched in an earlier, different enrichment
    run and is still in the 64-slot cache), the real function body does not
    execute this time, so no status is recorded for this particular call -
    the caller (see enrich_rows_grouped) falls back to a generic status
    derived from the returned units alone in that case. This is an
    accepted, pre-existing limitation of that cache (already documented as
    "a cross-run optimization only... nothing within one run depends on it
    still holding an old entry") - the original detailed reason was already
    printed to stderr the first time it happened; this only affects how
    much CAN be re-surfaced for a stale cache hit days later, never
    behavior.
    """

    def __init__(self, sink: dict):
        self.sink = sink

    def __enter__(self):
        self._previous = getattr(_status_sink_local, "sink", None)
        _status_sink_local.sink = self.sink
        return self.sink

    def __exit__(self, *exc_info):
        _status_sink_local.sink = self._previous


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

# Generous relative tolerance for _rent_values_consistent below - wide enough
# to absorb ordinary real-world rounding (an agent quoting "£33 psf" for a
# figure that's really £32.80, or a size rounded to the nearest 10 sqft),
# narrow enough that a genuine mislabeling (e.g. a per-square-foot figure
# read into rent_pcm) is still caught: a real confirmed production case had
# rent_pcm=33/size=2762 against an actual rent_psf=33 for the same unit,
# implying annual figures of 396 vs 91,146 - a ~230x divergence, nowhere
# close to this tolerance regardless of how generous it is.
_RENT_CONSISTENCY_TOLERANCE_FRACTION = 0.15


def _rent_values_consistent(size_sqft, rent_pcm, rent_psf) -> bool:
    """
    True whenever there isn't enough data to check at all (any of the three
    is missing, or non-positive - not this check's job to judge a zero/
    negative value), OR whenever what IS available is internally consistent
    within _RENT_CONSISTENCY_TOLERANCE_FRACTION: the annual rent implied by
    rent_pcm (×12) and the annual rent implied by rent_psf×size_sqft must be
    close to each other. Purely a generic mathematical relationship - no
    property-specific thresholds, no notion of what a "plausible" rent looks
    like for any particular market - so a genuinely unusual but internally
    consistent rent (very cheap or very expensive) is never rejected on that
    basis; only a mismatch between how the SAME rent is being described two
    different ways is treated as suspicious.
    """
    if size_sqft is None or rent_pcm is None or rent_psf is None:
        return True
    if size_sqft <= 0 or rent_pcm <= 0 or rent_psf <= 0:
        return True
    annual_from_pcm = rent_pcm * 12
    annual_from_psf = rent_psf * size_sqft
    lo, hi = sorted((annual_from_pcm, annual_from_psf))
    if lo == 0:
        return hi == 0
    return (hi / lo) - 1 <= _RENT_CONSISTENCY_TOLERANCE_FRACTION


def _rent_check_values(row: ListingRow, unit: dict):
    """
    (size_sqft, rent_pcm, rent_psf) to feed _rent_values_consistent when
    deciding whether `unit`'s own rent figures are safe to apply to `row` -
    each is the row's OWN existing value when it already has one (a trusted,
    already-protected value from whatever source originally supplied it,
    never touched regardless of this check's outcome - see the blank-only
    guard in _apply_units_to_row's own loop), otherwise `unit`'s own
    candidate value for that same field. This mixing is deliberate: the
    real, confirmed production case this guards against is exactly a row
    that already has a trustworthy rent_psf/size_sqft from its original
    source, with only rent_pcm left blank for a brochure to fill - checking
    the brochure's OWN candidate pair against each other alone would miss
    it entirely whenever Gemini's own two numbers happen to already agree
    with each other (both wrong, but mutually "consistent" - a residual risk
    no purely internal check can rule out; the two candidate pairs are only
    combined with the row's own existing values BECAUSE the row's own values
    are independently anchored - see this module's own docstring on why
    fields other than the one being checked are never invented here).
    """
    size = row.size_sqft if not _is_blank(row.size_sqft) else _coerced_unit_value("size_sqft", unit.get("size_sqft"))
    pcm = row.rent_pcm if not _is_blank(row.rent_pcm) else _coerced_unit_value("rent_pcm", unit.get("rent_pcm"))
    psf = row.rent_psf if not _is_blank(row.rent_psf) else _coerced_unit_value("rent_psf", unit.get("rent_psf"))
    return size, pcm, psf


def _row_had_rent_conflict(row: ListingRow, units) -> bool:
    """
    True when the SAME unit _match_unit would confidently match for `row`
    stated a rent_pcm/rent_psf figure that failed _rent_values_consistent
    once paired against whatever size_sqft/sibling rent value is already
    trustworthy for this row (see _rent_check_values) - i.e. this row's rent
    field(s) stayed blank because the brochure's own stated number didn't
    add up, not because the document had nothing to offer. Purely a
    diagnostic signal (see STATUS_EXTRACTED_BUT_AMBIGUOUS), mirroring _row_
    had_ambiguous_match's own role alongside it in enrich_rows_grouped's
    main loop - never changes what gets enriched, only what's reported.
    """
    if not units:
        return False
    unit = _match_unit(row, units)
    if unit is None:
        return False
    size, pcm, psf = _rent_check_values(row, unit)
    return not _rent_values_consistent(size, pcm, psf)

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
# both would recognize them as unambiguously the same floor.
_FLOOR_NUMBER_RE = re.compile(r"\d+")

# Spelled-out numbered-floor ordinals, checked (case-insensitively, whole
# word only) when _FLOOR_NUMBER_RE finds no digit at all - confirmed real
# gap: real Copthall Estates brochures ("28-King-Street-EC2-2026-March.pdf",
# "Copthall-House-Office-Feb-2026.pdf") spell every floor out as a full word
# ("Third Floor", "Fourth Floor") with no digit anywhere in the label, which
# defeated this fallback entirely even though the spreadsheet's own "3rd
# Floor"/"4th Floor" would otherwise have resolved to a unique match. Scoped
# narrowly to NUMBERED floors only, First through Twentieth - "Ground Floor",
# "Lower Ground Floor", "Basement", "Mezzanine", "Reception" etc. are
# deliberately NOT given an invented numeric mapping here (they still return
# None, exactly as before) since there's no real confirmed case needing one
# and a wrong guess risks a false match against a genuinely different
# numbered floor. Ground itself is the one exception - see _GROUND_FLOOR_RE
# below, added once a real confirmed case existed for it specifically
# (unlike the others, still unmapped).
_ORDINAL_WORD_TO_NUMBER = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20,
}
_ORDINAL_WORD_RE = re.compile(
    r"\b(" + "|".join(_ORDINAL_WORD_TO_NUMBER) + r")\b", re.IGNORECASE
)

# Ground-floor label variants, checked (on normalize_key'd text) ONLY after
# both the digit and ordinal-word tiers above find nothing - confirmed real
# gap: a real Ivybridge House row's own floor_unit "G - Strand" (Gemini's
# own brochure extraction for the same floor: "Ground Floor") never matched
# via either existing tier (no digit, no ordinal word), permanently
# excluding this floor's State Of Space/Special Features from ever being
# applied, on every re-upload of the same real document. Anchored at the
# START with a word boundary, so this matches ONLY when "g"/"ground"/
# "ground floor" is itself a complete leading TOKEN - "G - Strand" and
# "Ground Floor - Part" both match (the trailing description is simply
# never examined), but "Gallery Floor"/"Garden Level" never do (there is no
# word boundary directly after "g" in either). Deliberately does NOT match
# "Lower Ground"/"LG"/"Basement"/"Mezzanine" - those start with a different
# leading word/token entirely and remain unmapped, exactly as the comment
# above still documents for them: Lower Ground is a genuinely DIFFERENT
# space from Ground, confirmed by the same real Ivybridge House brochure
# stating both "LG" and "G - Strand" as separate floors - conflating them
# would be a real, confirmed false match, not a hypothetical risk.
_GROUND_FLOOR_RE = re.compile(r"^(ground floor|ground|g)\b")


def _floor_number(floor_unit):
    """
    The leading digit run in `floor_unit` as an int (e.g. 5 from "5th
    Floor", or 6 from "6th Floor - Part"/"6th Floor West" - the digit
    search runs anywhere in the text, so a trailing description never
    blocks it), or - when there's no digit at all - a recognized spelled-
    out numbered-floor ordinal word (e.g. 3 from "Third Floor", case-
    insensitive; see _ORDINAL_WORD_TO_NUMBER), or 0 for a Ground-floor
    label (e.g. "G", "Ground Floor", "G - Strand" - see _GROUND_FLOOR_RE's
    own docstring for exactly which shapes this does and does NOT match),
    or None if `floor_unit` is blank or matches none of these forms (e.g.
    "Lower Ground Floor", "Reception") - those never participate in this
    fallback tier, exactly as if it didn't exist for them (falls through
    to the existing size-based tier, or no match, same as before any of
    these forms existed). Digit, then ordinal word, then Ground are
    checked in that order and the first match wins - a label matching more
    than one form at once is not a real case this needs to handle specially.
    """
    if _is_blank(floor_unit):
        return None
    text = str(floor_unit)
    digit_match = _FLOOR_NUMBER_RE.search(text)
    if digit_match:
        return int(digit_match.group())
    word_match = _ORDINAL_WORD_RE.search(text)
    if word_match:
        return _ORDINAL_WORD_TO_NUMBER[word_match.group(1).lower()]
    if _GROUND_FLOOR_RE.match(normalize_key(text)):
        return 0
    return None


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


# Generic UK street-type words that can legitimately appear as the LAST
# word of a building/address string - both full forms and common
# abbreviations. Deliberately a small, explicit list (same "conservative,
# a human catches a case this misses" philosophy as normalize_key itself
# and master_merge._STREET_SUFFIX_EXPANSIONS), not a general gazetteer.
_TRAILING_STREET_SUFFIX_WORDS = frozenset({
    "road", "rd", "street", "st", "avenue", "ave", "av", "lane", "ln",
    "place", "pl", "court", "ct", "crescent", "cres", "gardens", "gdns",
    "terrace", "ter", "square", "sq", "drive", "dr", "way", "close",
    "walk", "row", "grove", "hill", "rise", "mews", "boulevard", "blvd",
    "parade", "yard",
})


def _strip_trailing_street_suffix_word(key: str) -> str:
    """
    `key` (an ALREADY normalize_key'd string) with its own trailing
    generic street-type word dropped, when the last word is one - e.g.
    "35a westminster bridge road" -> "35a westminster bridge". Confirmed
    real gap this closes: a spreadsheet row's own building text ("35a
    Westminster Bridge") against the SAME building's fuller name as a
    brochure itself states it ("35A Westminster Bridge Road") - genuinely
    the same building, but sharing no normalize_key() overlap at all
    since one side simply never carries the trailing street-type word the
    other does.

    Deliberately a DROP, not master_merge._STREET_SUFFIX_EXPANSIONS' own
    abbreviation-to-full-form EXPANSION (that module's own docstring scopes
    _STREET_SUFFIX_EXPANSIONS to intra-batch duplicate grouping only, and
    expanding "Rd" to "Road" would never make "bridge" equal "bridge
    road" anyway - only dropping the extra word closes THIS gap, one side
    having the word at all, not the two sides merely abbreviating it
    differently).

    Never strips when there's only one word (e.g. a building genuinely
    just named "Court" or "Row") or the last word isn't a recognized
    street-type word - a building's own genuine one-word name is left
    alone rather than guessed at.
    """
    words = key.split()
    if len(words) > 1 and words[-1] in _TRAILING_STREET_SUFFIX_WORDS:
        return " ".join(words[:-1])
    return key


def _strip_leading_the(key: str) -> str:
    """
    `key` (an ALREADY normalize_key'd string) with a leading "the " word
    dropped, when present - e.g. "the canal building" -> "canal building".
    Confirmed real gap this closes: a real Regent's Wharf brochure's own
    Gemini-extracted building names ("The Canal Building", "The Packing
    House") carry a leading definite article the provider's own
    spreadsheet omits from those same buildings' own row text ("Canal
    Building", "Packing House") - genuinely the same buildings, but
    sharing no normalize_key() overlap at all purely because of this one
    word. normalize_key's own docstring already documents deliberately
    NOT stripping this by default (a near-miss should surface as "no
    match" for tier 1's exact comparison to catch, not be silently
    guessed away) - this stays scoped to its own weak-signal tier below
    instead, same as every other stripped-variant tier in this function;
    normalize_key and tier 1 itself are both untouched.

    Never strips when "the" is the ONLY word (e.g. a building genuinely
    just named "The") - stripping down to an empty key would make this
    tier match literally any other single-word candidate.
    """
    words = key.split()
    if len(words) > 1 and words[0] == "the":
        return " ".join(words[1:])
    return key


# A trailing "(...)" segment on a row's own building text - see
# _strip_trailing_parenthetical's own docstring for the real MetSpace
# convention this exists for.
_TRAILING_PARENTHETICAL_RE = re.compile(r"\s*\([^()]*\)\s*$")

# A " - " (space-dash-space) split point in a row's own building text -
# see _building_identity_matches's own tier 3f docstring for the two real
# confirmed cases this exists for. Deliberately requires whitespace on
# BOTH sides of the dash (never a bare hyphen inside a single compound
# word like "Co-working" or a house-number range like "27-29"), and only
# ever splits on the FIRST such occurrence (maxsplit=1) - a row_building
# with more than one " - " (none seen in any real case so far) is left
# with its second half still attached to the suffix piece, never guessed
# at further.
_DASH_SEPARATOR_RE = re.compile(r"\s+-\s+")


def _strip_trailing_parenthetical(building):
    """
    `building` with a trailing "(...)" segment removed, when present -
    e.g. "141 Fenchurch Street (Monument)" -> "141 Fenchurch Street".
    Returns `building` unchanged (never None/"") when there's no trailing
    parenthetical at all, or stripping it would leave nothing behind.

    Confirmed real gap this closes: a real MetSpace email's own building
    text routinely appends a nearby Tube/landmark station name in
    parentheses purely for the READER's own area orientation ("141
    Fenchurch Street (Monument)" - Monument being the nearest Underground
    station) - a convention with nothing to do with the building's own
    real identity or address. The SAME real building's own brochure
    (Gemini-extracted) never carries this at all ("141 Fenchurch
    Street") - confirmed against three real floors of this exact
    building, all three of which enriched with ZERO fields changed
    despite each one's own brochure containing real, matchable unit
    content, purely because none of tiers 1-3's own building-to-building
    text comparisons could bridge the parenthetical.
    """
    if not building:
        return building
    stripped = _TRAILING_PARENTHETICAL_RE.sub("", str(building)).strip()
    return stripped or building


def _is_descriptive_parenthetical(inner: str) -> bool:
    """
    True when `inner` (a trailing parenthetical's own already-extracted
    content, e.g. "splitable" from "160 Blackfriars Yard (splitable)" - see
    _trailing_parenthetical_content) reads as a descriptive note about the
    unit rather than a plausible alternate building/sub-building/landmark
    name - i.e. it's written in lowercase, the way a person writes an
    ordinary descriptive word or phrase ("splitable", "subject to
    availability"), never the way a real proper noun is written ("Monument",
    "The Mill", "Fora Enterprise") regardless of how short it is. Digits-led
    content (e.g. a unit number) is never treated as descriptive either -
    only actual lowercase-led alphabetic text counts.

    Deliberately a narrow, conservative test - only the FIRST character
    decides it, so a rare descriptive note that happens to start with a
    proper noun of its own is never caught here (left for a human to widen
    this if a real case like that ever surfaces), but every real descriptive
    case confirmed so far ("splitable") is caught by it.
    """
    inner = (inner or "").strip()
    return bool(inner) and inner[0].isalpha() and inner[0].islower()


def _strip_descriptive_trailing_parenthetical(building):
    """
    `building` with its own trailing "(...)" segment removed ONLY when that
    segment's content is descriptive (see _is_descriptive_parenthetical) -
    i.e. never a plausible alternate building name, so there is no
    building-identity information to weigh at all, unlike the "(Monument)"/
    "(The Mill)"-style parentheticals tiers 3b/3c/3e already handle as weak,
    corroborated-by-uniqueness evidence. Confirmed real gap this closes: a
    real UNION "160 Blackfriars Yard (splitable)" row (splitable meaning the
    floor CAN be split into smaller units, not an alternate name for the
    building) never matched its own real brochure at all - not because no
    tier could bridge the parenthetical, but because leaving "splitable" as
    the row building's own trailing word (after normalize_key flattens the
    parentheses away) pushed the real trailing street-suffix word ("Yard")
    out of trailing position, which silently broke tier 4b's own address-
    based fallback match too (that tier needs the street-suffix word at the
    very end to strip it) - a single-purpose parenthetical-stripping tier
    could never have fixed this, since the breakage wasn't in a building-vs-
    building tier at all. Run once, unconditionally, on row_building itself
    before any tier runs, so every tier downstream (not just the ones that
    explicitly know about parentheses) sees the same clean text a person
    would read off this row with the purely descriptive aside removed.

    Returns `building` completely unchanged whenever its own trailing
    parenthetical (if any) looks like a plausible name instead - that case
    is left entirely to tiers 3b/3c/3e's own existing weak-signal handling,
    never touched here.
    """
    inner = _trailing_parenthetical_content(building)
    if inner is None or not _is_descriptive_parenthetical(inner):
        return building
    return _strip_trailing_parenthetical(building)


def _trailing_parenthetical_content(building):
    """
    The INNER text of `building`'s own trailing "(...)" segment, when
    present - e.g. "Regents Wharf (The Mill)" -> "The Mill" - or None when
    there's no trailing parenthetical at all. The mirror image of
    _strip_trailing_parenthetical (which DISCARDS this same content and
    keeps everything before it): confirmed real, distinct case this exists
    for - a real UNION Regents Wharf portfolio spreadsheet states each row's
    OWN building as "Regents Wharf (<sub-building>)" ("Regents Wharf (The
    Mill)", "Regents Wharf (The Canal Building)", "Regents Wharf (Thorley
    Works)", "Regents Wharf (The Packing House)"), while that SAME real
    brochure's own Gemini extraction (and every OTHER row referencing the
    same sub-buildings) uses the bare sub-building name alone ("The Mill",
    "The Canal Building", ...) - here "Regents Wharf" is a shared
    DEVELOPMENT/portfolio wrapper, not part of any one sub-building's own
    identity, and the parenthetical content is the real, distinct name -
    the OPPOSITE of _strip_trailing_parenthetical's own MetSpace case
    ("141 Fenchurch Street (Monument)"), where the parenthetical is
    disposable reader-orientation text and everything BEFORE it is the
    real name. Returns None (never the bare building text) when there's
    nothing to extract, so a caller can tell "no parenthetical" apart from
    "a parenthetical that stripped to nothing" without a second check.
    """
    if not building:
        return None
    match = _TRAILING_PARENTHETICAL_RE.search(str(building))
    if not match:
        return None
    inner = match.group().strip()[1:-1].strip()
    return inner or None


def _is_placeholder_address(address_1, building) -> bool:
    """
    True when `address_1` is either genuinely blank OR just a duplicate of
    `building` in disguise - confirmed real shape: a listing whose source
    document never states a separate numbered street address at all gets
    address_1 filled in as a plain copy of building ("Nineteen Wells St"
    for both), rather than left blank, since extraction has nothing better
    to put there. That still isn't a real address - it carries zero
    information address_1 wouldn't already carry via building - so it must
    be treated the same as blank for address_1's own enrichment-
    eligibility/backfill/re-geocode-trigger purposes (see needs_enrichment,
    _apply_units_to_row's BUILDING_LEVEL_FIELDS step, and
    _regeocode_rows_with_newly_backfilled_addresses, all below) - but ONLY
    for address_1 specifically; this is never generalized to any other
    field.

    Reuses _strip_trailing_street_suffix_word (this module's own existing
    building-identity comparison, already used to match a row's building
    against a brochure's own building/unit candidates elsewhere in this
    file) rather than a plain exact-string check, so "Nineteen Wells St"
    (address_1) vs "Nineteen Wells Street" (building) still counts as a
    duplicate - a street-suffix abbreviation difference alone must never
    make a placeholder look like a genuine, independent address.
    """
    if _is_blank(address_1):
        return True
    if _is_blank(building):
        return False
    address_key = _strip_trailing_street_suffix_word(normalize_key(address_1))
    building_key = _strip_trailing_street_suffix_word(normalize_key(building))
    return address_key == building_key


def _distinct_building_group(indices: list, candidate_buildings: list) -> list:
    """
    `indices` unchanged if every one of them shares the SAME candidate
    building identity (normalize_key(candidate_buildings[i]) is identical
    across all of them), else [] - the right granularity for tier 4's own
    uniqueness check (see _building_identity_matches), where several
    matching INDICES sharing one real building (e.g. that building's own
    several floors, each its own unit entry, all carrying the identical
    address_1) is the expected, unambiguous case - not a conflict the way
    two matching indices naming two DIFFERENT buildings would be. Mirrors
    tier 1's own "every exact match returned, even several, for the SAME
    building" allowance, just checked explicitly here since tier 4 matches
    on a DIFFERENT field (address_1) than the one being grouped by
    (building), so several matching indices no longer implies they share
    one building the way it did for tiers 1-3's own building-to-building
    comparisons.
    """
    if not indices:
        return []
    building_keys = {normalize_key(candidate_buildings[i]) for i in indices}
    return indices if len(building_keys) == 1 else []


def _building_identity_matches(row_building, candidate_buildings: list, candidate_addresses: list = None) -> list:
    """
    Indices into `candidate_buildings` (a list of raw building-name strings,
    e.g. one per brochure unit/building_features entry) that confidently
    identify the SAME building as `row_building` - never a fuzzy/similarity
    match, only exact-string comparisons, tried in order until one tier
    finds something:

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
       the identical key. Only ever accepted when every candidate sharing
       that stripped key names the SAME real building (see
       _distinct_building_group - several indices for the identical
       building, e.g. its own several floors, is the expected schedule-of-
       areas case, not an ambiguity) - two candidates sharing the stripped
       key but naming DIFFERENT buildings is exactly as ambiguous as two or
       more exact matches would be if this fell back to guessing between
       them, so it stays unresolved (empty), same "incorrect enrichment is
       worse than a blank field" philosophy as every other tier in this
       module. This is what makes the stripped tier only ever a WEAK,
       corroborated signal - uniqueness of the underlying building is the
       corroboration, never the bare shortened name alone.
    3. TRAILING-STREET-SUFFIX-STRIPPED (see _strip_trailing_street_suffix_
       word) - e.g. "35a Westminster Bridge" (a row) vs "35A Westminster
       Bridge Road" (a brochure's own fuller name) - confirmed against a
       real production case. Same weak-signal treatment as tier 2 and for
       the identical reason: dropping a generic trailing word can coincide
       two genuinely different streets that merely share everything before
       their own street-type word (e.g. "Kings Road" and "Kings Street" in
       the same portfolio brochure both drop to "kings") - only ever
       accepted when every candidate sharing that key names the SAME real
       building (see _distinct_building_group, same discipline as tier 2
       above). Tried independently of tier 2, on the ORIGINAL (non-address-suffix-
       stripped) keys - the two gaps are unrelated (one is a spreadsheet
       baking a full address onto a building name, the other is one side
       simply omitting a trailing street-type word) and neither building's
       real text needs both stripped at once for any case seen so far.
    3b. TRAILING-PARENTHETICAL-STRIPPED (see _strip_trailing_parenthetical)
       - e.g. "141 Fenchurch Street (Monument)" (a row) vs "141 Fenchurch
       Street" (a brochure's own extraction) - confirmed against three
       real floors of this exact real building, ALL THREE of which
       enriched with zero fields changed despite each one's own brochure
       containing real, matchable unit content. A real MetSpace email's
       own building text routinely appends a nearby Tube/landmark station
       name in parentheses purely for the reader's own area orientation -
       a convention with nothing to do with the building's own real
       identity, which its brochure (Gemini-extracted) never carries at
       all. Same weak-signal, corroborated-by-uniqueness treatment as
       tiers 2/3 - only row_building's own side is stripped here (a
       candidate carrying this kind of suffix itself is handled
       separately by tier 3c below, not assumed away).
    3c. CANDIDATE-SIDE TRAILING-PARENTHETICAL-STRIPPED - the mirror image
       of 3b, e.g. row_building "210 Euston Road" (no parenthetical at
       all) vs a brochure's own extracted unit building "210 Euston Road
       (Fora Enterprise)" - confirmed against a real Colliers brochure
       whose own Gemini extraction appends the floor's own tenant/
       operator name in parentheses, the same area-orientation-style
       convention as tier 3b's MetSpace case but on the OTHER side: here
       it's the brochure's own text carrying the suffix, not the row's.
       Tier 3b's own original assumption - that a candidate carrying this
       kind of suffix is already real evidence of a genuinely distinct
       name, since a truly shared one would already be caught by tier 1's
       exact comparison - turns out not to hold: the row's own building
       text can be the shorter, suffix-free one instead, in which case
       nothing upstream of this tier ever bridges the gap at all. Same
       weak-signal, corroborated-by-uniqueness discipline as every tier
       above - tried against row_building's own UNSTRIPPED key (if
       row_building itself also carried a parenthetical suffix, tier 3b
       above already tried stripping it and either matched or didn't;
       this tier is strictly about the suffix appearing on the candidate
       side), and only ever accepted when every candidate whose own
       stripped key lands on row_key names the SAME real building (see
       _distinct_building_group) - two or more candidates collapsing to
       the same stripped key but naming DIFFERENT buildings (e.g. two
       different floors of two DIFFERENT buildings that each happen to
       append a parenthetical to the same base text) stays unresolved,
       never guessed, identically to every other tier's own ambiguity
       guard.
    3d. LEADING-ARTICLE-STRIPPED (see _strip_leading_the) - e.g.
       row_building "Canal Building"/"Packing House" (a row) vs a real
       Regent's Wharf brochure's own Gemini-extracted building text "The
       Canal Building"/"The Packing House" - confirmed against that real
       document, whose own building_features/units both consistently
       carry the leading article a provider's own spreadsheet just as
       consistently omits. normalize_key deliberately never strips a
       leading "the" by default (see its own docstring) so a genuine
       near-miss still surfaces as "no match" for tier 1 rather than
       being silently guessed away there - this tier is where that
       tolerance actually lives instead, same as every other stripped-
       variant tier here. Stripped from BOTH sides at once (unlike tiers
       3b/3c's own deliberate row-side-only/candidate-side-only split) -
       there's no real-world reason a leading article's omission would
       only ever happen in one direction, so one symmetric comparison
       covers either side carrying it. Same weak-signal, corroborated-
       by-uniqueness discipline as every tier above - only accepted when
       every candidate sharing the stripped key names the SAME real
       building (see _distinct_building_group - e.g. a real Regent's
       Wharf brochure's own several floors per building, all sharing the
       identical "The Packing House"/"The Canal Building" text, is the
       expected schedule-of-areas case here too, not an ambiguity).
    3e. ROW'S OWN TRAILING-PARENTHETICAL CONTENT AS THE REAL NAME (see
       _trailing_parenthetical_content) - the OPPOSITE of tier 3b: e.g.
       row_building "Regents Wharf (The Mill)" (a real UNION portfolio
       spreadsheet's own row text) vs that SAME real brochure's own
       Gemini-extracted building text "The Mill" - "Regents Wharf" is a
       shared development/portfolio wrapper with no identity of its own,
       and the parenthetical's own INNER content is the real, distinct
       sub-building name, not disposable reader-orientation text the way
       tier 3b's own MetSpace "(Monument)" case is. Tried against the
       extracted content's own key and its leading-article-stripped form
       together (see _strip_leading_the) - never fires when row_building
       has no trailing parenthetical at all. Same weak-signal,
       corroborated-by-uniqueness discipline as every tier above.
    4. BUILDING-VS-ADDRESS (row_building compared against each candidate's
       own address_1, via `candidate_addresses` - a parallel list, one
       entry per candidate_buildings, or None from a caller with no address
       data available at all, e.g. _match_building_feature's building_
       features entries, which skips this tier entirely). Real, confirmed
       gap this closes: a provider's own spreadsheet sometimes states a
       property's plain street address as its ENTIRE building field (no
       name at all - confirmed common shape, see _ADDRESS_LIKE_RE's own
       comment), while a brochure for that same property is branded under
       a marketing name that shares no words with the address at all (e.g.
       row_building "160 Blackfriars Road" vs the real Friars Yard
       brochure's own building "Friars Yard", address_1 "160 Blackfriars
       Road") - tiers 1-3 all compare building-to-building text and can
       never bridge that, no matter how much suffix-stripping is tried,
       since the two sides share no vocabulary whatsoever.
       a. EXACT address (row_building's own key against candidate_
          addresses[i]'s key) - always sufficient alone, same as tier 1:
          two full address strings landing on the byte-identical
          normalized text is already strong, specific evidence.
       b. TRAILING-STREET-SUFFIX-STRIPPED address (e.g. row_building "160
          Blackfriars" vs address_1 "160 Blackfriars Road") - weaker,
          same coincidental-collision risk tier 3's own stripping has, so
          ALSO requires row_building's own leading house number (see
          house_number.leading_house_number) to equal the candidate
          address's leading house number, a second, independent, already-
          trusted corroboration signal (the same parser _strip_building_
          address_suffix already relies on) - never accepted on the
          stripped text match alone.
       Both of tier 4's own sub-tiers use _distinct_building_group, not a
       raw index count, for their own uniqueness check: several matching
       indices that all name the SAME candidate building (e.g. that
       building's own several floors, each restating the identical
       address_1) is the expected case, not an ambiguity - only 2+ matching
       indices naming DIFFERENT buildings is rejected as genuinely
       unresolvable. Tried strictly after tiers 1-3 both find nothing at
       all - a real building-name match is always more specific evidence
       than a cross-field address match, so tier 4 never competes with or
       overrides one.
    5. BARE STREET REFERENCE (row_building has no leading house number of
       its own AT ALL - see house_number.leading_house_number). Confirmed
       real gap: a MetSpace email listing whose only location text was
       "Clerkenwell Road" (no house number, no building name) never
       matched its own uniquely-linked brochure's real numbered building
       ("67 Clerkenwell Road") - tier 4b's own sub-tier requires row_
       building's OWN leading house number to corroborate a candidate
       address's, which a genuinely bare street reference has none of, by
       definition, to corroborate WITH. Compares STREET-NAME WORD SETS
       (_expanded_street_name_words, defined below - builds on the same
       word-overlap corroboration _address_conflict_note already uses
       elsewhere in this file, PLUS street-suffix-abbreviation expansion -
       a real brochure's own Gemini extraction routinely abbreviates "67
       Clerkenwell Rd" even when the row's own source spells it in full)
       against candidate_buildings itself (never candidate_addresses - a
       brochure's own extracted unit "building" text is exactly where a
       real numbered name like "67 Clerkenwell Road" actually shows up),
       requiring EXACT set equality, not mere overlap - "Kings" ({"kings"})
       must never equal "Kings Road" ({"kings", "road"}), an extra word is
       real evidence of a more specific, different street reference. Same
       _distinct_building_group
       uniqueness guard as tier 4: two DIFFERENT numbered buildings on the
       same street among the candidates stays unresolved, never guessed.
       Available regardless of whether candidate_addresses was even
       provided at all - unlike tier 4, this tier never reads it. Tried
       strictly last (weakest evidence: a bare street name alone identifies
       a specific building only when there's exactly one on it among the
       candidates).

    Returns [] when row_building has no genuine key at all (blank/
    whitespace-only).
    """
    # 0. DESCRIPTIVE TRAILING PARENTHETICAL STRIPPED (see
    # _strip_descriptive_trailing_parenthetical) - run once, unconditionally,
    # before any tier below, so a purely descriptive aside like "160
    # Blackfriars Yard (splitable)" never has to be bridged tier-by-tier at
    # all: it's simply never part of row_building's own working text again.
    # A parenthetical that instead looks like a plausible name (e.g.
    # "(Monument)", "(The Mill)") is left completely untouched here - that
    # case stays exactly tiers 3b/3c/3e's own weak-signal territory below.
    row_building = _strip_descriptive_trailing_parenthetical(row_building)

    row_key = normalize_key(row_building)
    if not row_key:
        return []

    exact = [i for i, c in enumerate(candidate_buildings) if normalize_key(c) == row_key]
    if exact:
        return exact

    row_stripped_key = normalize_key(_strip_building_address_suffix(row_building))
    if row_stripped_key:
        stripped = _distinct_building_group(
            [
                i for i, c in enumerate(candidate_buildings)
                if normalize_key(_strip_building_address_suffix(c)) == row_stripped_key
            ],
            candidate_buildings,
        )
        if stripped:
            return stripped

    row_street_key = _strip_trailing_street_suffix_word(row_key)
    street_suffix = _distinct_building_group(
        [
            i for i, c in enumerate(candidate_buildings)
            if _strip_trailing_street_suffix_word(normalize_key(c)) == row_street_key
        ],
        candidate_buildings,
    )
    if street_suffix:
        return street_suffix

    # 3b. TRAILING PARENTHETICAL STRIPPED (see _strip_trailing_
    # parenthetical's own docstring for the real MetSpace "141 Fenchurch
    # Street (Monument)" case) - only row_building's own side is ever
    # stripped; a candidate's own text is never expected to carry this
    # kind of area-disambiguation suffix at all (if it genuinely did,
    # that's real evidence of an actually distinct name, not the same
    # building loosely restated, and tier 1 would already have matched
    # it via an exact comparison). Same weak-signal, corroborated-by-
    # uniqueness treatment as tiers 2/3 - only accepted when it's the
    # SOLE candidate sharing the stripped key. Tried independently of
    # tiers 2/3 above, on the ORIGINAL (non-address-suffix-stripped,
    # non-street-suffix-stripped) key - the gaps are unrelated and no
    # real case seen so far needs more than one of them stripped at once.
    row_parenthetical_key = normalize_key(_strip_trailing_parenthetical(row_building))
    if row_parenthetical_key and row_parenthetical_key != row_key:
        parenthetical_stripped = _distinct_building_group(
            [i for i, c in enumerate(candidate_buildings) if normalize_key(c) == row_parenthetical_key],
            candidate_buildings,
        )
        if parenthetical_stripped:
            return parenthetical_stripped

    # 3c. CANDIDATE-SIDE TRAILING-PARENTHETICAL-STRIPPED (see this
    # function's own docstring, tier 3c) - the mirror of 3b above: strips
    # the same suffix from each CANDIDATE's own text instead, compared
    # against row_key unstripped. Any candidate whose raw key already
    # equalled row_key would already have been returned by tier 1's exact
    # comparison above, so no extra guard against that is needed here.
    candidate_parenthetical_stripped = _distinct_building_group(
        [
            i for i, c in enumerate(candidate_buildings)
            if normalize_key(_strip_trailing_parenthetical(c)) == row_key
        ],
        candidate_buildings,
    )
    if candidate_parenthetical_stripped:
        return candidate_parenthetical_stripped

    # 3d. LEADING-ARTICLE-STRIPPED (see _strip_leading_the's own docstring
    # for the real Regent's Wharf case: row_building "Canal Building"/
    # "Packing House" vs the brochure's own extracted building_features
    # text "The Canal Building"/"The Packing House"). Stripped from BOTH
    # sides' own keys here, unlike tiers 3b/3c's own deliberate row-side-
    # only/candidate-side-only split - a leading article omission has no
    # real-world reason to only ever happen in one direction the way the
    # parenthetical tiers' own MetSpace/Colliers provenance did, so one
    # symmetric comparison covers either side carrying it. Tried on the
    # ORIGINAL (non-address-suffix-stripped, non-street-suffix-stripped,
    # non-parenthetical-stripped) key, same as tiers 3b/3c - independent
    # of those gaps, no real case seen so far needs more than one kind of
    # stripping at once. Same weak-signal, corroborated-by-uniqueness
    # treatment as every tier above - only accepted when it's the SOLE
    # candidate sharing the stripped key; two candidates that both
    # collapse to the same leading-article-stripped key stays unresolved,
    # never guessed.
    row_no_the_key = _strip_leading_the(row_key)
    leading_article_stripped = _distinct_building_group(
        [
            i for i, c in enumerate(candidate_buildings)
            if _strip_leading_the(normalize_key(c)) == row_no_the_key
        ],
        candidate_buildings,
    )
    if leading_article_stripped:
        return leading_article_stripped

    # 3e. ROW'S OWN PARENTHETICAL CONTENT AS THE REAL NAME (see
    # _trailing_parenthetical_content's own docstring for the real, confirmed
    # UNION Regents Wharf case: row_building "Regents Wharf (The Mill)"/
    # "Regents Wharf (The Canal Building)"/"Regents Wharf (Thorley Works)"/
    # "Regents Wharf (The Packing House)" vs that SAME real brochure's own
    # Gemini-extracted building text for each sub-building, the bare name
    # alone ("The Mill", "The Canal Building", ...). The exact OPPOSITE of
    # tier 3b above: there the parenthetical is disposable reader-
    # orientation text and the part BEFORE it is the real name; here
    # "Regents Wharf" is a shared development/portfolio wrapper with no
    # identity of its own, and the parenthetical's own INNER content is
    # the real, distinct sub-building name. Tried against both the
    # extracted content's own key AND its leading-article-stripped form
    # (see tier 3d above) in one pass, since a real portfolio's own
    # sub-building name routinely carries a leading "The" the wrapped row
    # text still states in full ("Regents Wharf (The Mill)") while the
    # brochure's own extraction sometimes omits it - no real case seen so
    # far needs the reverse (a candidate-side "the" that the row's own
    # parenthetical content lacks), so this stays one-directional, unlike
    # tier 3d's own deliberately symmetric strip. Same weak-signal,
    # corroborated-by-uniqueness discipline as every tier above (see
    # _distinct_building_group) - only accepted when every candidate
    # sharing the matched key names the SAME real building; two candidates
    # colliding here stays unresolved, never guessed. Never fires at all
    # when row_building has no trailing parenthetical (returns None, which
    # can never equal any real candidate key).
    row_parenthetical_content = _trailing_parenthetical_content(row_building)
    if row_parenthetical_content:
        content_key = normalize_key(row_parenthetical_content)
        content_no_the_key = _strip_leading_the(content_key)
        parenthetical_content_matches = _distinct_building_group(
            [
                i for i, c in enumerate(candidate_buildings)
                if normalize_key(c) == content_key or _strip_leading_the(normalize_key(c)) == content_no_the_key
            ],
            candidate_buildings,
        )
        if parenthetical_content_matches:
            return parenthetical_content_matches

    # 3f. DASH-SEPARATED WRAPPER, EITHER SIDE MAY BE THE REAL NAME (see
    # _DASH_SEPARATOR_RE's own docstring for the two real confirmed cases
    # this closes: row_building "Southbank Central - ALTO"/"Southbank
    # Central - VIVO" vs that brochure's own bare "Alto"/"Vivo" (the
    # SUFFIX is the real sub-building name, "Southbank Central" a
    # disposable development wrapper - the dash-separated mirror of tier
    # 3e's own parenthetical-wrapper case), and row_building "210 Euston
    # Road - Fora Enterprise" vs that brochure's own bare "210 Euston
    # Road" (here the PREFIX is the real name, "Fora Enterprise" a
    # disposable operator/tenant suffix - tier 2's own _strip_building_
    # address_suffix never catches this, since it only ever strips a
    # dash-suffix that's itself address-SHAPED, i.e. starts with a house
    # number; "Fora Enterprise" doesn't). Tries BOTH resulting pieces as
    # independent candidate identities in one pass, since there's no
    # reliable way to tell in advance which side (if either) is the real
    # name - same weak-signal, corroborated-by-uniqueness discipline as
    # every tier above (see _distinct_building_group): if prefix and
    # suffix happen to match two DIFFERENT real candidates, that stays
    # genuinely ambiguous, never guessed between. Never fires at all when
    # row_building has no " - " separator.
    dash_parts = _DASH_SEPARATOR_RE.split(row_building, maxsplit=1) if row_building else [row_building]
    if len(dash_parts) == 2:
        dash_prefix_key = normalize_key(dash_parts[0])
        dash_suffix_key = normalize_key(dash_parts[1])
        dash_side_matches = _distinct_building_group(
            [
                i for i, c in enumerate(candidate_buildings)
                if (dash_prefix_key and normalize_key(c) == dash_prefix_key)
                or (dash_suffix_key and normalize_key(c) == dash_suffix_key)
            ],
            candidate_buildings,
        )
        if dash_side_matches:
            return dash_side_matches

    # 3g. PERIOD-AS-WORD-SEPARATOR - e.g. row_building "TBC London - 224
    # Tower Bridge Rd" (already reduced to "TBC London" by tier 2's own
    # _strip_building_address_suffix, its "224 Tower Bridge Rd" suffix
    # genuinely address-shaped) vs a real brochure's own extracted
    # building text "TBC.London" - confirmed real, growing branding
    # convention (a domain-style name used AS the building name).
    # normalize_key itself simply DROPS "." entirely rather than treating
    # it as a word boundary (confirmed directly: normalize_key("TBC.
    # London") -> "tbclondon", never "tbc london"), so tier 1's own exact
    # comparison - and every tier above needing an EXACT normalize_key
    # match on at least one side - can never bridge this gap no matter how
    # much else is stripped first. Deliberately narrow: replaces "." with
    # a space BEFORE normalize_key runs, on both the row's own address-
    # suffix-stripped text and each candidate's raw text, tried as an
    # ADDITIONAL exact comparison - never a change to normalize_key
    # itself (which countless OTHER real building names rely on treating
    # "." as pure noise, e.g. an address abbreviation like "St." never
    # meant to gain a spurious extra token). Same weak-signal,
    # corroborated-by-uniqueness discipline as every tier above.
    row_period_base = _strip_building_address_suffix(row_building)
    row_period_key = normalize_key((row_period_base or "").replace(".", " "))
    if row_period_key:
        period_matches = _distinct_building_group(
            [i for i, c in enumerate(candidate_buildings) if normalize_key((c or "").replace(".", " ")) == row_period_key],
            candidate_buildings,
        )
        if period_matches:
            return period_matches

    row_house_number = leading_house_number(row_building)

    # 3h. HOUSE-NUMBER-RANGE OVERLAP - e.g. row_building "27-29 Gloucester
    # Place" (a provider's own spreadsheet stating the FULL numbered range
    # a building spans) vs a real brochure's own extracted building text
    # "29 Gloucester Place" (Gemini choosing to state just the one number
    # actually printed on that page) - confirmed real case. Requires BOTH:
    # the text AFTER each side's own leading house number is byte-
    # identical once normalize_key'd ("gloucester place" == "gloucester
    # place" - genuinely the same street/name, not a coincidental number
    # overlap on a different street), AND the two house-number tokens
    # don't genuinely conflict (see house_number.house_numbers_conflict -
    # reused rather than a second, independently-drifting range-overlap
    # check; "27-29" and "29" overlap, so this is never a conflict, the
    # same real UK convention that function's own docstring already
    # documents). Never fires when either side has no leading house
    # number at all - tier 5 below already covers a row/candidate with no
    # number of its own, on a different, weaker signal (bare street-name
    # word overlap), and mixing the two would only weaken both.
    if row_house_number is not None:
        row_number_match = LEADING_HOUSE_NUMBER_RE.match(row_building)
        row_number_remainder_key = normalize_key(row_building[row_number_match.end():])
        if row_number_remainder_key:
            range_matches = []
            for i, c in enumerate(candidate_buildings):
                c = c or ""
                candidate_number_match = LEADING_HOUSE_NUMBER_RE.match(c)
                if candidate_number_match is None:
                    continue
                if normalize_key(c[candidate_number_match.end():]) != row_number_remainder_key:
                    continue
                if house_numbers_conflict(row_house_number, candidate_number_match.group(1)):
                    continue
                range_matches.append(i)
            range_matches = _distinct_building_group(range_matches, candidate_buildings)
            if range_matches:
                return range_matches

    if candidate_addresses is not None:
        addr_exact = _distinct_building_group(
            [i for i, a in enumerate(candidate_addresses) if normalize_key(a) == row_key],
            candidate_buildings,
        )
        if addr_exact:
            return addr_exact

        if row_house_number is not None:
            addr_street_suffix = _distinct_building_group(
                [
                    i for i, a in enumerate(candidate_addresses)
                    if _strip_trailing_street_suffix_word(normalize_key(a)) == row_street_key
                    and leading_house_number(a) == row_house_number
                ],
                candidate_buildings,
            )
            if addr_street_suffix:
                return addr_street_suffix

    if row_house_number is None:
        # 5. BARE STREET REFERENCE (row_building has no house number of its
        #    own at all). Confirmed real gap: a MetSpace email listing
        #    whose only location text was "Clerkenwell Road" (no house
        #    number, no building name - see extract_email.py's own
        #    extraction) never matched its own uniquely-linked brochure
        #    ("67 Clerkenwell Rd - 4th Floor - Brochure.pdf", building
        #    "67 Clerkenwell Road" once Gemini extracts it) - address_1/
        #    postcode/lat/lng all stayed blank, even though the real
        #    numbered address was sitting right there in the one document
        #    this row points to. Tier 4b above can never bridge this shape
        #    at all: it requires row_building's OWN leading house number to
        #    corroborate a candidate address's (see its own comment) -
        #    there's nothing here to corroborate WITH by definition.
        #
        #    Compares STREET-NAME WORD SETS (_street_name_words - the same
        #    word-overlap corroboration _address_conflict_note already uses
        #    elsewhere in this file, which already drops a pure-digit token
        #    like a leading house number) against candidate_buildings
        #    itself, not candidate_addresses - a brochure's own extracted
        #    unit "building" text is exactly where a real numbered name
        #    like "67 Clerkenwell Road" actually shows up. "Clerkenwell
        #    Road" -> {"clerkenwell", "road"} equals "67 Clerkenwell Road"
        #    -> {"clerkenwell", "road"} (its own "67" already dropped) -
        #    genuinely the same street, not just a coincidental prefix.
        #    Deliberately EXACT set equality, not mere overlap - "Kings" ->
        #    {"kings"} must never equal "Kings Road" -> {"kings", "road"},
        #    an extra word is real evidence of a DIFFERENT, more specific
        #    street reference, not the same one loosely restated. Uses
        #    _expanded_street_name_words, not the bare _street_name_words -
        #    a real brochure's own Gemini-extracted text routinely
        #    abbreviates the street-suffix word ("67 Clerkenwell Rd") even
        #    when the row's own source text spells it in full
        #    ("Clerkenwell Road") or vice versa - see that function's own
        #    docstring for the confirmed real case. Reuses
        #    _distinct_building_group for the same uniqueness guard tier 4
        #    already applies: two DIFFERENT numbered buildings on the same
        #    street among the candidates (a portfolio brochure spanning one
        #    street) stays unresolved, never guessed - only a SINGLE
        #    distinct building sharing this exact street-word set is ever
        #    accepted. Available regardless of whether candidate_addresses
        #    was even provided (unlike tier 4) - this tier never reads it.
        row_street_words = _expanded_street_name_words(row_building)
        if row_street_words:
            return _distinct_building_group(
                [i for i, c in enumerate(candidate_buildings) if _expanded_street_name_words(c) == row_street_words],
                candidate_buildings,
            )

    return []


def needs_enrichment(row: ListingRow) -> bool:
    """
    True when at least one of ENRICHABLE_FIELDS is genuinely blank on `row`
    - checked BEFORE any network/Gemini activity is even considered (see
    enrich_row), so a row with nothing missing never costs a fetch or a
    Gemini call at all.

    address_1 is checked via _is_placeholder_address rather than a plain
    blank check - a row whose only "problem" is an address_1 that's really
    just a copy of its own building (see that function's own docstring)
    still has nothing genuinely useful there, and must remain eligible for
    enrichment on that basis alone, exactly as if address_1 were blank.

    special_features is handled separately from the other 8 fields, which
    all keep the plain "only if blank" rule. enrich_row's own
    special_features handling is never gated on row.special_features
    already being non-blank - it always attempts to append building-/
    property-level text pulled from the brochure on top of whatever's
    already there. So a row stays eligible on special_features's account
    alone whenever it has ANY brochure_link to check (URL-shape validity
    is _is_eligible_brochure_url's job, same as for every other field -
    not duplicated here), even when every other field - including
    special_features itself - is already filled. Without this, that
    combine logic would never get a chance to run for any row whose
    initial extraction already filled every field, which in practice is
    most real rows with a data-rich source document.
    """
    for field in ENRICHABLE_FIELDS:
        if field == "special_features":
            continue
        if field == "address_1":
            if _is_placeholder_address(row.address_1, row.building):
                return True
            continue
        if _is_blank(getattr(row, field)):
            return True
    return _is_blank(row.special_features) or bool(row.brochure_link)


def _row_has_a_genuinely_blank_enrichable_field(row: ListingRow, special_features_matched: bool = False) -> bool:
    """
    True if row is missing a real value for at least one of
    ENRICHABLE_FIELDS, judged STRICTLY - unlike needs_enrichment, a truthy
    brochure_link is never itself a reason special_features counts as
    "still needed" here (contrast needs_enrichment's own final line -
    that override is deliberate THERE, so the additive special_features
    combine keeps getting a chance to run even once a row already has
    real content, but it makes needs_enrichment permanently True for any
    row with an eligible brochure_link regardless of whether special_
    features already has a genuine value, which is exactly why it can't
    ALSO serve as evidence a row's own value is still missing).

    Used only by enrich_rows_grouped's own already_processed resume logic
    (see its own still_blank_counts comment) to tell whether a url ALREADY
    marked "ok" still has any row genuinely unresolved - whether that's
    every row sharing it (a document that was checked and genuinely had
    nothing for anyone, OR one where a matching bug failed every row
    identically - the two are indistinguishable from here, see enrich_
    rows_grouped's own already_processed docstring) or only SOME of them
    (a sibling row already has a real value here, even though needs_
    enrichment would still call
    it "eligible" for another pass) - never used for the per-row
    application loop itself, which keeps using needs_enrichment/
    indices_by_url completely unchanged, preserving its own deliberate
    always-eligible-for-recombine behavior.

    special_features is checked differently from every other field here,
    using the caller-supplied `special_features_matched` flag (see enrich_
    rows_grouped's own special_features_matched param docstring - a per-
    staging-file sidecar record, NOT a ListingRow field, since a row's own
    text is not reliable evidence of this) rather than a plain blank check
    on row.special_features itself - a confirmed real gap a plain blank
    check can't close: a row's special_features can be non-blank (short
    boilerplate carried over from the original source extraction, never
    actually brochure-sourced) while STILL never having received a genuine
    unit-/building-/property-level combine. A plain blank check would call
    such a row "not genuinely blank" - exactly wrong for THIS function's
    own purpose, since it's what let a row like this get silently,
    permanently skipped on a resumed run once its shared brochure URL was
    already marked "ok" by an earlier pass, even though the combine that
    would have actually resolved it never once ran. special_features_
    matched is the evidence needed to ask the real question ("did a
    genuine combine ever land here", not "is there any text here at all")
    that this function's own docstring above already explains needs_
    enrichment's blunt brochure_link-based override can't answer on its
    own - still gated on an eligible brochure_link existing at all, since a
    row with nothing to fetch has no way to ever resolve this regardless.
    """
    for field in ENRICHABLE_FIELDS:
        if field == "address_1":
            if _is_placeholder_address(row.address_1, row.building):
                return True
            continue
        if field == "special_features":
            if not special_features_matched and bool(row.brochure_link):
                return True
            continue
        if _is_blank(getattr(row, field)):
            return True
    return False


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
    brochure_link_resolver.is_generic_link), a Canva OR Pitch.com public
    "view" link (see is_canva_view_link/is_pitch_view_link's own
    docstrings - confirmed, not assumed, that a plain fetch can never
    retrieve real content from either - UNLESS _canva_renderer_configured,
    which now handles both), a Google
    Drive FOLDER share link (see _is_google_drive_folder_link - Google's
    own HTML folder-listing page, not a document, so a fetch attempt is
    structurally doomed the same way it is for a Canva/Pitch link), and a
    URL whose own text already identifies it as a floor plan or a video
    rather
    than a document - never a fetch-then-guess; these are excluded by the
    URL alone, exactly like a human skimming a link list would.
    """
    if _is_blank(url):
        return False
    if urlparse(url).scheme not in ("http", "https"):
        return False
    if is_generic_link(url):
        return False
    if (
        is_canva_view_link(url) or is_pitch_view_link(url) or is_gpe_flipbook_link(url)
        or is_kitt_brochure_preview_link(url)
    ) and not _canva_renderer_configured():
        return False
    if _is_google_drive_folder_link(url):
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


def _looks_like_fetchable_document(
    content_type, data: bytes, accept_image_formats: bool = False, accept_any_reachable_page: bool = False,
) -> bool:
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

    accept_any_reachable_page (only ever set by app.py's own _validate_
    pasted_link_brochure_links, via its _fetch_pdf_bytes call - see that
    function's own docstring) is a genuinely different question from the
    two checks above: True for ANY successfully-fetched response, real
    document or not. That caller never opens/parses these bytes at all - it
    only needs to confirm a per-unit link Gemini attributed from a page's
    own real link candidates (see extract.py's own page_links mechanism)
    resolves to a live page rather than a dead/blocked one, before trusting
    it over the shared document-level fallback. Confirmed real gap this
    closes: a genuine Colliers per-unit listing webpage (colliers.com/
    en-gb/properties/...) - exactly the case the extraction PROMPT's own
    brochure_link instructions already treat as valid ("a URL for this
    specific unit/listing... if one is clearly given for it", never
    restricted to a literal PDF/document URL) - was being discarded here
    for the shared whole-document fallback purely because a webpage isn't a
    PDF/image, even though it was genuinely reachable, real, and exactly
    what was extracted. Every OTHER caller (enrichment/floorplan fetch,
    which DOES go on to parse these bytes as a document) never sets this,
    so their own PDF/image-only requirement is completely unaffected.
    """
    if accept_any_reachable_page:
        return True
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


# Dropbox's own documented "force download" mechanism (see Dropbox's help
# center: sharing a file normally serves an HTML preview page, but forcing
# dl=1 in the share link's query string switches it to a direct, byte-for-
# byte download of exactly what the owner shared) - no OAuth/app
# credentials, no reverse engineering, just the public query parameter
# Dropbox itself designed for this. Covers both the older "/s/{id}/..." and
# current "/scl/fi/{id}/..." share-link path shapes; declined for any other
# dropbox.com path (a login page, the folder-browser UI, a team admin page)
# this mechanism was never meant for and isn't confirmed to work on.
def _is_dropbox_share_url(url: str) -> bool:
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc != "dropbox.com" and not netloc.endswith(".dropbox.com"):
        return False
    return parsed.path.startswith("/s/") or parsed.path.startswith("/scl/fi/")


def _dropbox_direct_download_url(url: str) -> str:
    """`url` (already confirmed by _is_dropbox_share_url) with dl forced to
    1 - see the comment above for why this is Dropbox's own documented
    mechanism, not a hack."""
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    query["dl"] = "1"
    return urlunparse(parsed._replace(query=urlencode(query)))


# Google Drive's own long-standing "uc?export=download" endpoint for a
# PUBLICLY shared file's direct bytes - not Drive's authenticated REST API
# (no OAuth/app credentials) and not a reverse-engineered internal
# endpoint (this exact URL shape has been documented and relied upon
# externally for over a decade). For a large file this endpoint alone
# returns Drive's own "can't scan this file for viruses" HTML confirmation
# page instead of the file itself - see _google_drive_confirm_params below
# for the one-time confirmation-token retry this now attempts rather than
# failing outright (real confirmed case: a MetSpace listing's brochure
# link resolved to a Google Drive file - "67 Clerkenwell Rd - 4th Floor -
# Brochure.pdf" - unreadable through a single fetch attempt, leaving
# geocoding nothing but a bare "Clerkenwell Road" street name to guess an
# address from, which produced a wrong, unrelated address on the same
# street).
_GOOGLE_DRIVE_FILE_ID_RE = re.compile(r"^https?://drive\.google\.com/file/d/([\w-]+)", re.IGNORECASE)


def _google_drive_file_id(url: str):
    """The file ID from a "drive.google.com/file/d/{id}/..." share URL, or
    None if `url` isn't shaped like one - a pure URL-shape check, no fetch,
    mirroring _box_share_token's own convention."""
    match = _GOOGLE_DRIVE_FILE_ID_RE.match(url)
    return match.group(1) if match else None


# The confirmation token(s) needed to get past Google Drive's "can't scan
# this file for viruses" interstitial for a large file - Google has
# shipped two real shapes for this over time, both handled here:
#   (a) a plain link on the interstitial page whose href contains a bare
#       "confirm=TOKEN" query parameter (the older, simpler shape);
#   (b) a hidden download <form> - <input type="hidden" name="confirm"
#       value="...">, sometimes paired with a second <input type="hidden"
#       name="uuid" value="...">, since some interstitials require both,
#       not just confirm alone (the newer, more complete shape).
# Attribute order within an <input> tag is not assumed fixed (real Google
# markup has been observed both ways) - each candidate <input> tag is
# matched as a whole first, then name="..."/value="..." are read from
# WITHIN that match, independent of which comes first.
_GOOGLE_DRIVE_HIDDEN_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_GOOGLE_DRIVE_INPUT_TYPE_RE = re.compile(r'type\s*=\s*"([^"]*)"', re.IGNORECASE)
_GOOGLE_DRIVE_INPUT_NAME_RE = re.compile(r'name\s*=\s*"([^"]*)"', re.IGNORECASE)
_GOOGLE_DRIVE_INPUT_VALUE_RE = re.compile(r'value\s*=\s*"([^"]*)"', re.IGNORECASE)
_GOOGLE_DRIVE_LINK_CONFIRM_RE = re.compile(r"confirm=([\w-]+)")


def _google_drive_confirm_params(content: bytes) -> dict:
    """
    {"confirm": token} or {"confirm": token, "uuid": token} extracted from
    `content` (a Google Drive virus-scan interstitial page's own HTML), or
    {} when no confirmation token can be found at all - including for
    binary/non-UTF8 content, which is never a real interstitial page and
    must never raise trying to decode it as one.

    The hidden-form shape (b, see the module-level regexes' own comment)
    is preferred whenever both shapes are present on the same page - it's
    the newer, more complete one, and the plain-link shape (a) alone can't
    express a required "uuid" value the newer flow sometimes needs. Only
    falls back to the plain-link shape when no hidden "confirm" input was
    found at all.
    """
    try:
        html = content.decode("utf-8")
    except UnicodeDecodeError:
        return {}

    hidden_values = {}
    for tag in _GOOGLE_DRIVE_HIDDEN_INPUT_RE.findall(html):
        type_match = _GOOGLE_DRIVE_INPUT_TYPE_RE.search(tag)
        name_match = _GOOGLE_DRIVE_INPUT_NAME_RE.search(tag)
        value_match = _GOOGLE_DRIVE_INPUT_VALUE_RE.search(tag)
        if not (type_match and name_match and value_match):
            continue
        if type_match.group(1).lower() != "hidden":
            continue
        if name_match.group(1) in ("confirm", "uuid"):
            hidden_values[name_match.group(1)] = value_match.group(1)

    if "confirm" in hidden_values:
        return hidden_values

    link_match = _GOOGLE_DRIVE_LINK_CONFIRM_RE.search(html)
    if link_match:
        return {"confirm": link_match.group(1)}

    return {}


# A Google Drive FOLDER share link ("drive.google.com/drive/folders/{id}",
# or "drive.google.com/drive/u/{n}/folders/{id}" for a specific signed-in
# account) - a genuinely different shape from the single-FILE share link
# above, confirmed real: 20+ distinct folder links used as floorplan_link
# across real rows in Kitt's Availability file. A folder has no single
# document to fetch at all - just Google's own HTML folder-listing page -
# so a plain HTTP GET can never retrieve real content from one the way it
# can for a real file link; classify_link_eligibility rejects this shape
# outright (STATUS_UNSUPPORTED_LINK_TYPE) rather than letting it fall
# through to a real fetch attempt that's structurally guaranteed to fail,
# which previously surfaced as a misleading "document couldn't be opened"
# (a fetch-failure message) for a link shape this pipeline was never
# built to read at all - same honest wording Canva links already get.
# Classification only: this does NOT add real folder support (reading
# whichever specific file inside the folder is the actual floor plan) -
# that's a separate, bigger feature, not attempted here.
_GOOGLE_DRIVE_FOLDER_RE = re.compile(r"^https?://drive\.google\.com/drive/(?:u/\d+/)?folders/", re.IGNORECASE)


def _is_google_drive_folder_link(url: str) -> bool:
    """True for a Google Drive FOLDER share link (see _GOOGLE_DRIVE_
    FOLDER_RE's own comment) - never a fetch, matched against the URL's
    own text only, same as is_canva_view_link/is_floorplan_not_brochure_
    url's other use sites."""
    if not url:
        return False
    return bool(_GOOGLE_DRIVE_FOLDER_RE.match(url.strip()))


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
        _record_status(STATUS_FETCH_FAILED, f"could not load Box share page ({e!r})")
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
        _record_status(STATUS_FETCH_FAILED, "could not read Box share metadata")
        return None

    if can_download_match.group(1) != "true":
        print(
            f"[brochure_enrichment] Box share {share_url!r} has downloads disabled — skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_FETCH_FAILED, "Box share has downloads disabled")
        return None

    extension = extension_match.group(1).lower()
    allowed_extensions = _BOX_PDF_OR_IMAGE_EXTENSIONS if accept_image_formats else _BOX_PDF_ONLY_EXTENSIONS
    if extension not in allowed_extensions:
        print(
            f"[brochure_enrichment] Box share {share_url!r} is a .{extension or '?'} file, not a "
            f"{'PDF/image' if accept_image_formats else 'PDF'} — skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_FETCH_FAILED, f"Box share is a .{extension or '?'} file, not a supported document")
        return None

    name_match = _BOX_FILE_NAME_RE.search(html)
    file_name = name_match.group(1) if name_match else ""
    if reject_floorplan_filename and _FLOORPLAN_FILENAME_RE.search(file_name):
        print(
            f"[brochure_enrichment] Box share {share_url!r} looks like a floor plan ({file_name!r}) — "
            "skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_FETCH_FAILED, f"Box share file name looks like a floor plan ({file_name!r})")
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
        _record_status(STATUS_FETCH_FAILED, f"could not download Box file ({e!r})")
        return None

    if not _looks_like_fetchable_document(
        response.headers.get("content-type"), response.content, accept_image_formats=accept_image_formats,
    ):
        print(
            f"[brochure_enrichment] Box static download for {share_url!r} did not resolve to a "
            f"{'PDF/image' if accept_image_formats else 'PDF'} — skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_FETCH_FAILED, "Box static download was not a readable document")
        return None
    return response.content


# Generous over the separate renderer's OWN internal worst-case budget -
# RENDER_TIMEOUT_SECONDS (scales with MAX_CANVA_PAGES AND NAV_TIMEOUT_MS,
# since capturing every page of a real multi-page brochure takes longer
# than a single-page-only budget would) PLUS SEMAPHORE_WAIT_TIMEOUT_
# SECONDS (a request arriving while MAX_CONCURRENT_RENDERS are already in
# flight now queues for a free slot rather than being rejected instantly -
# see that constant's own docstring in canva_renderer/app.py for the real
# bulk-upload production bug this fixes) - both summed (see that module's
# own worst-case: 275 + 90 = 365s at current defaults - raised from 285s
# when MAX_CANVA_PAGES there went from 20 to 30, see that constant's own
# docstring). This is the ceiling for the WHOLE round trip (network +
# queueing + browser launch/render of every page), so it must stay
# comfortably above that service's own worst-case combined budget rather
# than racing it - a value below that budget would make THIS app give up
# on a renderer that's still genuinely working, which looks identical to
# a real renderer failure from here.
_CANVA_RENDERER_TIMEOUT = 400

# A 502/503 from the renderer is Cloud Run's OWN infrastructure or the
# renderer's own busy-semaphore giving up on the request BEFORE any real
# Playwright work started for this attempt (a cold start, a transient
# network blip, the renderer briefly unreachable during a container
# swap, or its own "renderer busy, try again" 503 - see canva_renderer/
# app.py's SEMAPHORE_WAIT_TIMEOUT_SECONDS) - never the renderer's own
# code actually running and reporting a real render failure (that's
# always a 422 with its own "reason", handled separately below). Retried
# a SMALL, bounded number of times with a short, fixed backoff - never
# indefinitely, and never on a raw connection-level exception (a DNS
# failure/genuine unreachability already burned a full _CANVA_RENDERER_
# TIMEOUT once; retrying that blindly would only double a large
# spreadsheet upload's worst-case wait for a URL that's very unlikely to
# succeed on retry anyway - see _fetch_canva_rendered_page).
#
# 504 is deliberately EXCLUDED here, unlike 502/503 - confirmed real
# production shape: two otherwise-healthy Canva URLs both surfaced a bare
# "HTTP 504" to this app, while canva_renderer's OWN logs (Cloud Run logs,
# not visible to this app) showed the SAME render continuing to run in
# the background and later succeeding (partially or fully) - because a
# 504 is Cloud Run's OWN proxy giving up waiting on the response, a layer
# ENTIRELY OUTSIDE the renderer's Python process (confirmed: canva_
# renderer/app.py's Handler.do_POST never sends a 504 itself - only
# 200/400/401/404/422/500/503). The renderer's own Playwright work runs
# on its dedicated background event-loop thread, decoupled from the HTTP
# request thread/client socket, so it keeps running to completion
# regardless of whether the proxy already gave up - a 504 is NOT proof
# the renderer failed or that no work is in flight. Retrying it by firing
# a SECOND, fully independent render (a new browser context/page, no
# relation to the first) duplicates expensive Chromium work for a render
# that may already be finishing successfully, and produces exactly the
# confusing double "Pagination failure"/"Canva render succeeded" log
# pairs seen in production for a single logical brochure fetch. A 504
# simply surfaces as a normal STATUS_FETCH_FAILED below, same as any
# other non-2xx response this app can't recover from on ITS OWN side.
_CANVA_RENDERER_TRANSIENT_STATUS_CODES = (502, 503)
_CANVA_RENDERER_MAX_ATTEMPTS = 2
_CANVA_RENDERER_RETRY_BACKOFF_SECONDS = 2

# Defense-in-depth cap on how many pages this app will ever accept from ONE
# Canva-renderer response, independent of that service's OWN MAX_CANVA_
# PAGES cap - the renderer is a separately deployed, separately versioned
# service (see canva_renderer/README.md), so this app never assumes its
# own cap is being enforced correctly on the other side; a response
# claiming more pages than this is simply truncated (with a loud log line),
# never trusted at face value. Raised from 20 to 30 in lockstep with the
# renderer's own MAX_CANVA_PAGES (canva_renderer/app.py) - a real
# production brochure (Risborough) had its contact info on page 29 of 29;
# leaving this constant at 20 would keep truncating it right back down
# even after the renderer itself was raised to capture it.
_CANVA_MAX_PAGES_ACCEPTED = 30

# Same defense-in-depth concept, for a Pitch presentation - see
# _CANVA_MAX_PAGES_ACCEPTED's own docstring. Must be kept in lockstep with
# canva_renderer/app.py's own MAX_PITCH_PAGES, same reason as the Canva
# pair (see tests.test_canva_renderer.MaxPagesCapsStayInSyncTests for the
# analogous pairing test this constant needs too).
#
# Also the cap used for a GPE flipbook link (see _fetch_gpe_flipbook_
# rendered_page below) - deliberately NOT a separate _GPE_MAX_PAGES_
# ACCEPTED constant: fm.gpe.co.uk is confirmed to be this exact same Pitch
# player on GPE's own branded domain (see is_gpe_flipbook_link's own
# docstring), routed into canva_renderer's existing render_pitch_page on
# that side too (never a separate render function/page cap there either -
# see that service's own render_page docstring), so there is no second,
# independently-tracked cap to keep in lockstep with in the first place.
_PITCH_MAX_PAGES_ACCEPTED = 30

# Same defense-in-depth concept, for a Kitt brochure-preview page (see
# _fetch_kitt_rendered_page below) - see _CANVA_MAX_PAGES_ACCEPTED's own
# docstring. Must be kept in lockstep with canva_renderer/app.py's own
# MAX_KITT_PAGES. Kitt's own render_kitt_page_async captures one
# screenshot per scroll-container-height-sized section of ONE tall page
# (see is_kitt_brochure_preview_link's own docstring), not per Next-page
# click - genuinely different content shape (a single-unit brochure page,
# not a multi-building deck), so this is deliberately its own constant
# rather than reusing _CANVA_MAX_PAGES_ACCEPTED/_PITCH_MAX_PAGES_ACCEPTED,
# even though no real Kitt example so far has needed anywhere near this
# many. No production evidence yet of a real Kitt preview this large -
# revisit with real evidence if one is ever actually reported truncated,
# never preemptively.
_KITT_MAX_PAGES_ACCEPTED = 20

# Same idea as the renderer's own _MAX_REASON_LENGTH (canva_renderer/app.py)
# applied here too - this app never assumes the renderer's own truncation
# actually ran (again: a separately deployed, separately versioned service),
# so a "reason" string pulled out of its response is independently bounded
# before ever reaching a log line.
_MAX_LOGGED_REASON_LENGTH = 200


def _truncate_reason(text: str) -> str:
    """
    `text` (a "reason" string from the Canva renderer's own JSON response,
    or a locally built "HTTP {status}" fallback) collapsed to one line and
    capped to _MAX_LOGGED_REASON_LENGTH before it's ever printed to this
    app's own logs - defense-in-depth against a renderer response (a
    separately deployed, separately versioned service - see canva_renderer/
    README.md) that doesn't actually enforce its own reason-safety
    guarantees, whether from an outdated deploy or a future change on that
    side this app was never updated in lockstep with.
    """
    collapsed = " ".join(str(text).split())
    if len(collapsed) > _MAX_LOGGED_REASON_LENGTH:
        collapsed = collapsed[:_MAX_LOGGED_REASON_LENGTH].rstrip() + "…"
    return collapsed


def _canva_renderer_auth_headers(renderer_url: str) -> dict:
    """
    Cloud Run's own native service-to-service auth: mints a Google-signed
    ID token scoped to the renderer's own URL (the "audience"), so a
    renderer deployed WITHOUT --allow-unauthenticated only ever accepts a
    call from a caller this project's own IAM policy has explicitly
    granted the Cloud Run Invoker role to - never a static shared secret
    this codebase would need to store, rotate, or risk leaking. google-
    auth is already a transitive dependency of google-cloud-storage/
    google-genai (both already in requirements.txt), so this adds no new
    dependency.

    Returns {} (never raises) whenever ID-token minting isn't available in
    the current environment - e.g. local development outside GCP, with no
    Application Default Credentials configured. The renderer's own
    deployment is expected to allow unauthenticated calls ONLY for that
    specific local-dev scenario (see canva_renderer/README.md) - in every
    other environment, an empty headers dict here simply means Cloud Run
    itself rejects the call with 401/403 before it ever reaches the
    renderer's own code, which is the safe direction to fail in.

    The mint failure itself is still logged to stderr (never silently
    swallowed) - a real, confirmed diagnostic gap: without this, a genuine
    production auth problem (a missing IAM binding, a metadata-server
    hiccup, credentials not available for whatever reason) looked
    IDENTICAL to every other Canva render failure - the same generic
    "documents couldn't be read" - with no way for whoever operates this
    deployment to tell "the renderer is fine but we're not even sending it
    a valid token" apart from "the renderer itself couldn't render this
    particular page". This print is operator-facing only (Cloud Run's own
    logs), never shown to a reviewer in the UI.
    """
    try:
        import google.auth.transport.requests
        import google.oauth2.id_token

        token = google.oauth2.id_token.fetch_id_token(google.auth.transport.requests.Request(), renderer_url)
        return {"Authorization": f"Bearer {token}"}
    except Exception as e:
        print(
            f"[brochure_enrichment] Could not mint an ID token for the Canva renderer ({renderer_url!r}): "
            f"{e!r} — calling it without one, which will fail if it requires authentication.",
            file=sys.stderr,
        )
        return {}


def _fetch_rendered_page(url: str, *, platform_label: str, max_pages_accepted: int):
    """
    PNG bytes for every page `url` (a public Canva OR Pitch.com "view"
    link, a GPE flipbook link, or a Kitt brochure-preview link) renders
    to, in page order, obtained from the separate rendering service (see
    canva_renderer/README.md - the SAME service handles all of these
    now) - never attempted at all unless CANVA_RENDERER_URL
    is configured (see _canva_renderer_configured's own docstring);
    classify_link_eligibility/_is_eligible_brochure_url/_is_eligible_
    floorplan_url already reject a Canva/Pitch URL before it ever reaches
    this function in that case. Chromium itself never runs in this app's
    own process/container - see that service's own README for why this
    separation exists (a stuck/misbehaving page or a Chromium OOM must
    never be able to affect this app's own memory budget or uptime).

    The one shared implementation _fetch_canva_rendered_page/_fetch_
    pitch_rendered_page both call (with their own platform_label/
    max_pages_accepted) - every retry/error-handling/response-parsing
    rule below applies identically to both platforms; only the LOG TEXT
    (via platform_label) and the defense-in-depth page-count cap (via
    max_pages_accepted, since canva_renderer/app.py's own MAX_CANVA_
    PAGES/MAX_PITCH_PAGES are tracked separately - see that module's own
    docstrings) differ between them at all.

    A transient HTTP 502/503 from the renderer (Cloud Run's own
    infrastructure or its own busy-semaphore giving up on the request
    BEFORE any real work started for this attempt, not the renderer's
    own code reporting a real failure - see _CANVA_RENDERER_TRANSIENT_
    STATUS_CODES' own docstring) is retried a small, bounded number of
    times (_CANVA_RENDERER_MAX_ATTEMPTS) with a short fixed backoff
    before falling through to the same failure handling as any other bad
    response - never retried indefinitely, and never on a raw connection-
    level exception (see that constant's own docstring on why). A 504 is
    deliberately NEVER retried this way (see that constant's own
    docstring) - it means Cloud Run's proxy gave up waiting on the
    response, not that the renderer's own work stopped or failed, and
    firing a second independent render would duplicate expensive
    Chromium work for a render that may already be succeeding.

    Returns (None, None) (never raises) whenever:
    - the renderer service is unreachable or times out (a raw connection-
      level exception, no HTTP response received at all) - recorded as
      STATUS_FETCH_FAILED, the same status a real network failure gets
      for any other document host;
    - the renderer returns a non-2xx/malformed response - including a
      transient 502/503 that didn't recover within the retry budget
      above, or a 504 (never retried at all - see above) - recorded as
      STATUS_RENDER_FAILED with the HTTP status/reason (same generic
      fallback path as any other bad response, see below);
    - the renderer itself reports a clean, safe failure - a malformed
      URL, a private/login-required design, a page that never finished
      loading - recorded as STATUS_RENDER_FAILED with the renderer's own
      short, non-sensitive reason string (never a raw exception, stack
      trace, or URL with a query string).

    Otherwise returns (pages, page_links): `pages` is a non-empty
    list[bytes], ALWAYS at least one page (the renderer itself never
    returns an empty "pages" list on a 200 - see its own app.py) - a
    genuinely multi-page brochure/deck's OTHER pages are now included too
    (see canva_renderer/README.md's "Multi-page capture"), up to whatever
    that service's own page cap allowed, further capped here at
    `max_pages_accepted` as defense-in-depth against a misbehaving/
    compromised renderer response (that service is a separately deployed,
    separately versioned service - this app never assumes its own cap is
    what actually ran on the other side). This list feeds into extract.
    images_from_png_pages/render_and_extract exactly like a multi-page
    PDF's own per-page images already do (see _fetch_pdf_bytes' own
    Canva/Pitch branches below) - no separate extraction system, no change
    to matching/enrichment rules.

    `page_links` is the renderer's own "links" field (see canva_renderer/
    app.py's own _page_link_candidates) - the same length as `pages`
    (truncated in lockstep if max_pages_accepted capped `pages` shorter),
    each entry that page's own list of {"href", "text"} dicts, or [] for
    an older renderer response that predates this field entirely (see
    payload.get below - never a KeyError just because the OTHER service
    hasn't been redeployed with this yet). Every EXISTING caller of this
    function (_fetch_canva_rendered_page/_fetch_pitch_rendered_page below)
    unpacks and discards this second value, keeping their own long-
    standing `list[bytes]`-or-None contract for the per-unit brochure
    enrichment path completely unchanged - only fetch_rendered_page_with_
    links (a new, separate entry point for the paste-a-link flow, see
    app.py's own _fetch_pasted_link) actually returns it to its caller.
    """
    renderer_url = os.environ.get(CANVA_RENDERER_URL_ENV_VAR, "").rstrip("/")
    connect_exception = None
    response = None
    for attempt in range(1, _CANVA_RENDERER_MAX_ATTEMPTS + 1):
        try:
            response = httpx.post(
                f"{renderer_url}/render", json={"url": url},
                headers=_canva_renderer_auth_headers(renderer_url), timeout=_CANVA_RENDERER_TIMEOUT,
            )
        except Exception as e:
            # A raw connection-level failure (DNS, refused, read timeout)
            # already burned a full _CANVA_RENDERER_TIMEOUT once - never
            # retried (see _CANVA_RENDERER_MAX_ATTEMPTS's own docstring on
            # why), so this always ends the loop, retry or not.
            connect_exception = e
            break
        if response.status_code not in _CANVA_RENDERER_TRANSIENT_STATUS_CODES:
            break  # a real answer - success or a non-transient failure - stop retrying
        if attempt < _CANVA_RENDERER_MAX_ATTEMPTS:
            print(
                f"[brochure_enrichment] {platform_label} renderer returned a transient HTTP {response.status_code} "
                f"for {url!r} on attempt {attempt}/{_CANVA_RENDERER_MAX_ATTEMPTS} - retrying "
                f"after {_CANVA_RENDERER_RETRY_BACKOFF_SECONDS}s.",
                file=sys.stderr,
            )
            time.sleep(_CANVA_RENDERER_RETRY_BACKOFF_SECONDS)

    if response is None:
        print(
            f"[brochure_enrichment] {platform_label} renderer unreachable for {url!r} ({connect_exception!r}) — "
            "skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_FETCH_FAILED, f"{platform_label} renderer unreachable ({connect_exception!r})")
        return None, None

    if response.status_code in (401, 403):
        # Distinguished from every other failure shape (see this function's
        # own docstring) - the renderer's own code never even ran; Cloud
        # Run's platform-level IAM check rejected the call before it got
        # there. Confirmed real risk this guards against: _canva_renderer_
        # auth_headers already logs its OWN mint failures, but a token that
        # mints fine yet still gets rejected (wrong audience, a missing/not-
        # yet-propagated Cloud Run Invoker binding) would otherwise look
        # identical to "the renderer just couldn't render this page".
        print(
            f"[brochure_enrichment] {platform_label} renderer rejected the request for {url!r} with "
            f"HTTP {response.status_code} (authentication failed - check the main app's service account has "
            f"the Cloud Run Invoker role on the renderer, and that CANVA_RENDERER_URL exactly matches its "
            f"own URL) — skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_FETCH_FAILED, f"{platform_label} renderer authentication failed")
        return None, None

    content_type = response.headers.get("content-type", "")
    if response.status_code == 200 and "application/json" not in content_type and "image/" in content_type:
        # Distinguished from every other failure shape below - a 200 with
        # an image/* content-type is exactly what the OLD, single-page-only
        # Canva-only renderer returned (see this repo's history before
        # multi-page capture: canva_renderer/app.py used to respond with a
        # raw PNG body, not the {"pages": [...]} JSON this app now
        # expects). This is the one failure shape that means "the two
        # services are on mismatched versions", not "the render itself
        # failed" - a genuine render failure never returns 200 with image
        # bytes at all. Loud and specific on purpose: this is the single
        # most likely cause of "enrichment silently does nothing in
        # production despite the multi-page code being merged" - the
        # canva-renderer Cloud Run service simply hasn't been redeployed
        # with it yet.
        print(
            f"[brochure_enrichment] {platform_label} renderer for {url!r} returned an OLD-FORMAT single-image "
            f"response (content-type {content_type!r}) instead of the expected JSON {{'pages': [...]}} "
            "body — the canva-renderer Cloud Run service needs redeploying with the current multi-page "
            "code (see canva_renderer/README.md) — skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_RENDER_FAILED, f"{platform_label} renderer is running an outdated single-page image response")
        return None, None

    if response.status_code != 200 or "application/json" not in content_type:
        try:
            reason = response.json().get("reason", f"HTTP {response.status_code}")
        except Exception:
            reason = f"HTTP {response.status_code}"
        reason = _truncate_reason(reason)
        # The renderer's own 422/500/503 bodies all now carry a short,
        # already-safe "reason" straight from the caught exception that
        # actually failed (see canva_renderer/app.py's _safe_reason) -
        # previously this app could only ever log the bare HTTP status
        # (e.g. "HTTP 503"), which hid whatever the renderer itself saw
        # (a browser launch failure, a navigation timeout, ...). This is
        # deliberately the one log line to grep for that question.
        print(f"[brochure_enrichment] {platform_label} renderer failed for {url!r}: {reason}", file=sys.stderr)
        _record_status(STATUS_RENDER_FAILED, f"{platform_label} render failed: {reason}")
        return None, None

    try:
        payload = response.json()
        raw_pages = payload["pages"]
        pages = [base64.b64decode(p) for p in raw_pages]
        if not pages:
            raise ValueError("empty pages list")
    except Exception as e:
        print(
            f"[brochure_enrichment] {platform_label} renderer returned a malformed response for {url!r} ({e!r}) — "
            "skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_RENDER_FAILED, f"malformed renderer response ({e!r})")
        return None, None

    # .get, not [...] - an older renderer deploy that predates this field
    # entirely (see this function's own docstring) must never turn into a
    # KeyError here; [] per page is the correct, safe "no link data at
    # all" answer for that case, identical to a page that genuinely has
    # no real anchors on it.
    page_links = payload.get("links") or [[] for _ in pages]
    detected_total = payload.get("page_count_detected")
    if len(pages) > max_pages_accepted:
        print(
            f"[brochure_enrichment] {platform_label} renderer returned {len(pages)} pages for {url!r}, "
            f"truncating to this app's own cap of {max_pages_accepted}.",
            file=sys.stderr,
        )
        pages = pages[:max_pages_accepted]
        page_links = page_links[:max_pages_accepted]

    # The ONE clear, positive confirmation the whole authenticated round
    # trip actually worked - main app -> ID token -> Cloud Run IAM ->
    # renderer -> Chromium -> pages back. Every failure mode above already
    # prints its own distinct message; this is deliberately the only
    # SUCCESS line for this platform specifically, so grepping Cloud Run
    # logs for "<Platform> render succeeded" is a single, unambiguous way
    # to confirm rendering itself worked for a given URL, separate from
    # whether the subsequent Gemini extraction (see _extract_brochure_
    # units) then found anything useful in it.
    detected_str = f"{detected_total} detected" if detected_total else "total unknown"
    print(
        f"[brochure_enrichment] {platform_label} render succeeded for {url!r}: {len(pages)} page(s) captured "
        f"({detected_str}) — handing off to the existing extraction pipeline.",
        file=sys.stderr,
    )
    return pages, page_links


def _fetch_canva_rendered_page(url: str):
    """Canva's own thin wrapper over _fetch_rendered_page - see that
    function's own docstring for the full contract. Unpacks and discards
    the page_links half of that function's own (pages, page_links) return
    - this is the per-unit brochure enrichment path's own entry point,
    untouched by that field's addition; only fetch_rendered_page_with_
    links (below) ever returns page_links to its caller."""
    pages, _page_links = _fetch_rendered_page(
        url, platform_label="Canva", max_pages_accepted=_CANVA_MAX_PAGES_ACCEPTED,
    )
    return pages


def _fetch_pitch_rendered_page(url: str):
    """Pitch's own thin wrapper over _fetch_rendered_page - see that
    function's own docstring for the full contract. Identical mechanism
    to _fetch_canva_rendered_page, just calling the same renderer service
    for a Pitch.com "view" link instead (see canva_renderer/app.py's own
    render_pitch_page_async) and its own, separately-tracked max_pages_
    accepted cap. Unpacks and discards page_links exactly like _fetch_
    canva_rendered_page does, for the same reason."""
    pages, _page_links = _fetch_rendered_page(
        url, platform_label="Pitch", max_pages_accepted=_PITCH_MAX_PAGES_ACCEPTED,
    )
    return pages


def _fetch_gpe_flipbook_rendered_page(url: str):
    """GPE flipbook's own thin wrapper over _fetch_rendered_page - see
    that function's own docstring for the full contract. fm.gpe.co.uk is
    confirmed to be Pitch's own "Managed Links" feature on GPE's own
    branded domain (see is_gpe_flipbook_link's own docstring) - the SAME
    renderer-side function (canva_renderer/app.py's own render_pitch_
    page_async) handles it, and it shares Pitch's own max_pages_accepted
    cap rather than a separate one (see _PITCH_MAX_PAGES_ACCEPTED's own
    docstring on why). platform_label is still its own distinct "GPE
    Flipbook" string, purely so this app's own logs/diagnostics can tell
    which URL shape actually triggered a given render, independent of the
    fact that the underlying mechanism is identical to Pitch's."""
    pages, _page_links = _fetch_rendered_page(
        url, platform_label="GPE Flipbook", max_pages_accepted=_PITCH_MAX_PAGES_ACCEPTED,
    )
    return pages


def _fetch_kitt_rendered_page(url: str):
    """Kitt's own thin wrapper over _fetch_rendered_page - see that
    function's own docstring for the full contract. Identical mechanism
    to _fetch_canva_rendered_page/_fetch_pitch_rendered_page, just calling
    the same renderer service for a Kitt brochure-preview link instead
    (see canva_renderer/app.py's own render_kitt_page_async - a genuinely
    new render function, confirmed NOT to be Canva or Pitch under the
    hood, see is_kitt_brochure_preview_link's own docstring) and its own,
    separately-tracked max_pages_accepted cap. Unpacks and discards
    page_links exactly like the other two wrappers do, for the same
    reason."""
    pages, _page_links = _fetch_rendered_page(
        url, platform_label="Kitt", max_pages_accepted=_KITT_MAX_PAGES_ACCEPTED,
    )
    return pages


def fetch_rendered_page_with_links(url: str) -> tuple:
    """
    Public entry point for the paste-a-link flow (see app.py's own
    _fetch_pasted_link) - like _fetch_canva_rendered_page/_fetch_pitch_
    rendered_page, but ALSO returns each page's own real <a href> link
    candidates (see canva_renderer/app.py's own _page_link_candidates),
    needed to attribute a per-property brochure link during extraction
    rather than always falling back to one shared link for the whole
    document. Never called by the existing per-unit brochure enrichment
    path (see _fetch_pdf_bytes' own canva/pitch branches, which call the
    two plain wrappers above, untouched by this) - this is additive, a
    new capability for a different caller, never a change to those.

    Dispatches on the URL's own shape (see is_canva_view_link/is_pitch_
    view_link/is_gpe_flipbook_link/is_kitt_brochure_preview_link) exactly
    like _fetch_pdf_bytes' own canva/pitch/GPE/Kitt branches do - callers
    should still check is_canva_view_link(url) or is_pitch_view_link(url)
    or is_gpe_flipbook_link(url) or is_kitt_brochure_preview_link(url)
    themselves before calling this (see _fetch_pasted_link), same as every
    other caller of either platform-specific fetch already does; a URL
    matching none of these shapes returns (None, None) here rather than
    raising, so this stays safe to call defensively.

    Returns (pages, page_links) - pages is list[bytes] exactly like the
    plain wrappers (or None on any failure, same failure contract as
    _fetch_rendered_page's own docstring); page_links is the same length
    as pages, each entry that page's own list of {"href", "text"} dicts -
    or (None, None) whenever pages itself would be None.
    """
    if is_canva_view_link(url):
        return _fetch_rendered_page(url, platform_label="Canva", max_pages_accepted=_CANVA_MAX_PAGES_ACCEPTED)
    if is_pitch_view_link(url):
        return _fetch_rendered_page(url, platform_label="Pitch", max_pages_accepted=_PITCH_MAX_PAGES_ACCEPTED)
    if is_gpe_flipbook_link(url):
        return _fetch_rendered_page(url, platform_label="GPE Flipbook", max_pages_accepted=_PITCH_MAX_PAGES_ACCEPTED)
    if is_kitt_brochure_preview_link(url):
        return _fetch_rendered_page(url, platform_label="Kitt", max_pages_accepted=_KITT_MAX_PAGES_ACCEPTED)
    return None, None


def _fetch_pdf_bytes(
    url: str,
    reject_floorplan_filename: bool = True,
    accept_image_formats: bool = False,
    accept_any_reachable_page: bool = False,
):
    """
    PDF bytes fetched from `url`, or None on ANY failure - a network error,
    a timeout, or content that isn't actually a PDF once fetched (a
    provider preview/landing page that never resolves to a real document, a
    generic marketing page, a dead link) - never raises, UNLESS
    accept_any_reachable_page is set (see _looks_like_fetchable_document's
    own docstring - only ever set by app.py's own _validate_pasted_link_
    brochure_links), in which case any successfully-fetched response's raw
    bytes are returned regardless of content type - this caller only ever
    checks the result for None (link reachable or not), never opens these
    bytes as a document, so a non-PDF response is a perfectly valid result
    here, not a failure. Threaded through only the generic httpx fetch path
    below (never the Box/Canva/Pitch/GPE/Kitt branches, which each read
    document bytes via a mechanism of their own, not this function's
    generic PDF/image check) - not a source shape this fix's own confirmed
    case (a plain colliers.com listing-webpage URL) goes through anyway.

    A Box "shared link" URL (see _box_share_token) is read via _fetch_box_shared_pdf
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

    A Dropbox share link (see _is_dropbox_share_url) or a Google Drive
    "file/d/{id}" share link (see _google_drive_file_id) is turned into
    that host's own direct-download URL before fetching, same idea as the
    Box branch above but via a simple, documented URL transform rather
    than a second metadata fetch - neither host requires one. Anything
    else keeps the exact same behavior as before: a direct document
    extension is fetched as-is, otherwise resolve_brochure_link's one-hop
    landing-page scan is tried first.

    A Google Drive fetch that comes back as Drive's own "can't scan this
    file for viruses" HTML interstitial (routine for a large file, and
    most real brochure PDFs are large - see _google_drive_confirm_params'
    own docstring) gets ONE retry with that page's own confirmation
    token(s) appended to the download URL, never more than one - a
    genuinely unreadable source (a private/deleted file, a real network
    failure, an interstitial whose token this can't parse) still fails
    safe exactly as before, just no longer including the common "the file
    was simply too large" case. Scoped strictly to a Google Drive URL
    (drive_file_id truthy) - a non-Drive source that happens to return
    HTML is never retried this way.

    A Canva "view" link (see is_canva_view_link), a Pitch.com "view" link
    (see is_pitch_view_link), a GPE flipbook link (see is_gpe_flipbook_
    link - GPE's own branded domain for this exact same Pitch mechanism),
    OR Kitt's own brochure-preview app link (see is_kitt_brochure_preview_
    link - a genuinely distinct platform, not Canva or Pitch under the
    hood) is read via the separate rendering service instead (see _fetch_
    canva_rendered_page/_fetch_pitch_rendered_page/_fetch_gpe_flipbook_
    rendered_page/_fetch_kitt_rendered_page - the same deployed service
    handles all four), but ONLY when CANVA_RENDERER_URL is actually
    configured (see _canva_renderer_configured) - classify_link_
    eligibility/_is_eligible_brochure_url/_is_eligible_floorplan_url
    already reject any of the four before it gets here at all in that
    case, so this check is normally redundant, but this function's own
    direct callers (e.g. a diagnostic script, or a test exercising this
    layer directly) don't necessarily go through that eligibility gate
    first - falling through to the exact same generic fetch path as any
    other URL when unconfigured, rather than attempting a renderer call
    this deployment was never told about, keeps this function's own
    behavior correct independent of whether a caller already checked
    eligibility.

    The Canva/Pitch/GPE/Kitt branches are the ONE case this function
    returns list[bytes] (one PNG per page, see _fetch_canva_rendered_
    page/_fetch_pitch_rendered_page/_fetch_gpe_flipbook_rendered_page/
    _fetch_kitt_rendered_page) rather than a single bytes object - every
    other branch/URL shape is unaffected. Both callers (_extract_
    brochure_units/_extract_floorplan_units) branch on this via _images_
    from_fetched_document, so neither needs its own isinstance check
    duplicated.
    """
    if _box_share_token(url):
        return _fetch_box_shared_pdf(
            url, reject_floorplan_filename=reject_floorplan_filename, accept_image_formats=accept_image_formats,
        )

    if is_canva_view_link(url) and _canva_renderer_configured():
        return _fetch_canva_rendered_page(url)

    if is_pitch_view_link(url) and _canva_renderer_configured():
        return _fetch_pitch_rendered_page(url)

    if is_gpe_flipbook_link(url) and _canva_renderer_configured():
        return _fetch_gpe_flipbook_rendered_page(url)

    if is_kitt_brochure_preview_link(url) and _canva_renderer_configured():
        return _fetch_kitt_rendered_page(url)

    direct_extensions = (".pdf", ".png", ".jpg", ".jpeg") if accept_image_formats else (".pdf",)
    try:
        clean_path = url.lower().split("?")[0]
        drive_file_id = _google_drive_file_id(url)
        if _is_dropbox_share_url(url):
            target = _dropbox_direct_download_url(url)
        elif drive_file_id:
            target = f"https://drive.google.com/uc?export=download&id={drive_file_id}"
        else:
            target = url if clean_path.endswith(direct_extensions) else resolve_brochure_link(url)
        response = httpx.get(
            target, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True,
        )
        response.raise_for_status()
    except Exception as e:
        print(f"[brochure_enrichment] Could not fetch {url!r} ({e!r}) — skipping enrichment.", file=sys.stderr)
        _record_status(STATUS_FETCH_FAILED, f"{e!r}")
        return None

    if not _looks_like_fetchable_document(
        response.headers.get("content-type"), response.content,
        accept_image_formats=accept_image_formats, accept_any_reachable_page=accept_any_reachable_page,
    ):
        if drive_file_id:
            confirm_params = _google_drive_confirm_params(response.content)
            if confirm_params:
                retry_target = f"{target}&{urlencode(confirm_params)}"
                try:
                    retry_response = httpx.get(
                        retry_target, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT},
                        follow_redirects=True,
                    )
                    retry_response.raise_for_status()
                except Exception as e:
                    print(
                        f"[brochure_enrichment] Google Drive confirm-token retry failed for {url!r} ({e!r}) — "
                        "skipping enrichment.",
                        file=sys.stderr,
                    )
                    _record_status(STATUS_FETCH_FAILED, f"Google Drive confirm-token retry failed: {e!r}")
                    return None

                if _looks_like_fetchable_document(
                    retry_response.headers.get("content-type"), retry_response.content,
                    accept_image_formats=accept_image_formats, accept_any_reachable_page=accept_any_reachable_page,
                ):
                    return retry_response.content

                # The retry itself still didn't produce a readable document
                # (e.g. an interstitial shape this couldn't fully parse) -
                # falls through to the same failure reporting below, just
                # describing the RETRY's own response rather than the
                # original interstitial - never a second retry attempt.
                response = retry_response

        print(
            f"[brochure_enrichment] {url!r} did not resolve to a "
            f"{'PDF/image' if accept_image_formats else 'PDF'} — skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(
            STATUS_FETCH_FAILED,
            f"response was not a readable document (content-type: {response.headers.get('content-type')!r})",
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


def _images_from_fetched_document(data):
    """
    list[types.Part] for `data` (whatever _fetch_pdf_bytes returned for a
    given URL) - shared by _extract_brochure_units/_extract_floorplan_units
    so neither duplicates this branch. `data` is either:
    - bytes (a real PDF, or a single-page image) - rendered via extract.
      render_pages, serialized behind extract._RENDER_LOCK (PyMuPDF/MuPDF's
      own process-wide state isn't thread-safe - see render_pages' own
      comment on fitz.TOOLS.store_shrink) - never written to a temp file at
      all (see render_pages' own docstring on why: a tmpfs-backed container
      /tmp, e.g. Cloud Run's default, would count that file's bytes against
      the SAME memory budget as `data` itself, a real doubling of a payload
      that can be up to ~32MB);
    - list[bytes] (the ONE case: a Canva "view" link - see _fetch_canva_
      rendered_page) - already-rendered PNG pages from a real browser
      screenshot, wrapped directly via extract.images_from_png_pages, never
      re-rasterized through render_pages/fitz (there is no vector PDF here
      to rasterize - re-opening one PNG at a time as a fake one-page "PDF"
      would just be a slower, no-op round trip for identical output).
    """
    if isinstance(data, list):
        return extract.images_from_png_pages(data)
    with extract._RENDER_LOCK:
        return extract.render_pages(data)


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
        _record_status(STATUS_FETCH_FAILED, "no document bytes obtained")
        return None

    try:
        images = _images_from_fetched_document(data)
    except Exception as e:
        print(
            f"[brochure_enrichment] Could not render {url!r} as a document ({e!r}) — skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_RENDER_FAILED, f"{e!r}")
        return None

    # Dropped HERE, between the two calls - not in a finally at the end
    # of this function - specifically so it's freed BEFORE the slower
    # Gemini call below runs, not merely before this function returns.
    # A caller-side reference to an argument stays alive for a callee's
    # entire execution regardless of what that callee does internally,
    # so this only works because the render step and the Gemini call
    # are two SEPARATE calls with this line in between, not one.
    data = None
    try:
        raw = extract.render_and_extract(images)
    except Exception as e:
        print(
            f"[brochure_enrichment] Could not read {url!r} as a brochure ({e!r}) — skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_EXTRACTION_FAILED, f"{e!r}")
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
    has_data = bool(units) or bool(units.property_features) or bool(units.contacts) or bool(units.building_features)
    _record_status(STATUS_EXTRACTED_SUCCESSFULLY if has_data else STATUS_EXTRACTED_NO_USEFUL_DATA)
    if (
        is_canva_view_link(url) or is_pitch_view_link(url) or is_gpe_flipbook_link(url)
        or is_kitt_brochure_preview_link(url)
    ):
        # Canva/Pitch/Kitt-specific diagnostic only (never printed for the
        # PDF/Box/Dropbox/GDrive paths, which already have years of
        # production history without this) - shows exactly what Gemini's
        # raw JSON contained for this brochure, one level BEFORE matching/
        # _apply_units_to_row ever runs, so "Gemini extracted nothing
        # useful" and "matching/apply rejected what Gemini found" are never
        # ambiguous with each other in the logs. Field NAMES only, never
        # the actual extracted text (which could be long/PII-bearing).
        platform_label = _render_platform_label(url)
        print(
            f"[brochure_enrichment] {platform_label} extraction for {url!r}: {len(units)} unit(s), "
            f"property_features={'present' if units.property_features else 'absent'}, "
            f"contacts={'present' if units.contacts else 'absent'}, "
            f"building_features={len(units.building_features)}.",
            file=sys.stderr,
        )
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
    match_indices = _building_identity_matches(
        row.building, [u.get("building") for u in units], [u.get("address_1") for u in units],
    )
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


def _row_had_ambiguous_match(row: ListingRow, units) -> bool:
    """
    True when `row`'s own building genuinely identifies 2+ candidate
    brochure units (see _building_identity_matches) that _match_unit still
    couldn't narrow down to exactly one - i.e. this row's blank fields
    stayed blank because of a real, irreducible ambiguity IN THIS DOCUMENT
    (a schedule of areas with several floors, none of which floor_unit/
    size_sqft could disambiguate), not because the document simply had
    nothing relevant to offer at all. Purely a diagnostic signal (see
    STATUS_EXTRACTED_BUT_AMBIGUOUS) - never changes what gets enriched;
    _match_unit's own conservative "unresolved tie stays None" behavior is
    completely unaffected by this function existing.

    False whenever there's 0 or exactly 1 building match - 0 means the
    document simply doesn't describe this row's building at all (not
    ambiguous, just inapplicable); 1 means _match_unit would have already
    returned it as a confident match (see its own "single match" rule), so
    a still-blank field there means the SINGLE matched unit genuinely
    didn't state that field, not that matching itself was ambiguous.
    """
    if not units:
        return False
    plain_units = [u for u in units if isinstance(u, dict)]
    match_indices = _building_identity_matches(
        row.building, [u.get("building") for u in plain_units], [u.get("address_1") for u in plain_units],
    )
    return len(match_indices) >= 2 and _match_unit(row, units) is None


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


# Range-connector words that can legitimately sit between two house
# numbers in a real UK address ("1 to 5 Adam Street") - filtered out
# alongside pure-digit tokens (below) before the street-name word-overlap
# check in _address_conflict_note, so a range's own extra number/connector
# words are never mistaken for a genuine street-name difference. Small and
# explicit, same conservative philosophy as this file's other hand-picked
# word lists (_TRAILING_STREET_SUFFIX_WORDS, _ORDINAL_WORD_TO_NUMBER).
_ADDRESS_RANGE_CONNECTOR_WORDS = frozenset({"to", "and"})


def _street_name_words(address: str) -> frozenset:
    """The normalized word set of `address` with every pure-digit token
    (a house number, or the second half of a word-form range like "1 to
    5") and range-connector word (_ADDRESS_RANGE_CONNECTOR_WORDS) removed
    - leaving just the street-name text itself, independent of exactly how
    a leading house number/range happens to be phrased. Used for the
    word-overlap half of _address_conflict_note's own comparison (the
    house-number half is handled separately, via house_number.leading_
    house_number, never by this function) and, expanded further via
    _expanded_street_name_words below, _building_identity_matches' own
    tier 5."""
    words = normalize_key(address).split()
    return frozenset(w for w in words if w not in _ADDRESS_RANGE_CONNECTOR_WORDS and not w.isdigit())


def _expanded_street_name_words(text: str) -> frozenset:
    """
    _street_name_words(text) with every standalone street-suffix
    abbreviation (see master_merge._STREET_SUFFIX_EXPANSIONS - "rd" ->
    "road", "st" -> "street", ...) expanded to its full form - used only
    by _building_identity_matches' own tier 5 (bare street reference),
    never by _address_conflict_note (that function's own comparison is
    deliberately left exactly as it already is - this is a new, narrower
    use, not a behavior change to an existing one).

    Confirmed real gap this closes: a real MetSpace brochure's own
    Gemini-extracted unit states its building as "67 Clerkenwell Rd" (the
    abbreviated form) while the source email's own bare-street reference
    is written out in full ("Clerkenwell Road") - without expansion,
    {"clerkenwell", "rd"} would never equal {"clerkenwell", "road"}, and
    tier 5 would never fire for the exact real case it exists for.
    """
    return frozenset(_STREET_SUFFIX_EXPANSIONS.get(w, w) for w in _street_name_words(text))


def _address_conflict_street_words(address: str) -> frozenset:
    """
    _street_name_words(address)'s own filtering (pure-digit/range-
    connector tokens dropped), PLUS "st" treated as equivalent to "saint"
    whenever it appears as a NON-LAST word - the mirror case of master_
    merge._expand_street_suffix (~line 1471), which deliberately only
    expands a TRAILING "st" ("Charterhouse St" -> "Charterhouse Street")
    and leaves a leading/mid "st" alone, since there it means "Saint", not
    a trailing street-type suffix (that function's own docstring: "'st
    james's' (a name, not a trailing street suffix) is left completely
    alone"). This is the opposite position: "st" as a NON-last word is
    always "Saint" ("St James's Square"), never the street-type suffix -
    only the LAST word can ever mean that, which is exactly why this
    function leaves the last word untouched in both directions (a
    genuinely differing trailing "...St" vs "...Street" still correctly
    registers as different tokens here, same as before this existed -
    that's master_merge._expand_street_suffix's own, unrelated concern,
    not this function's).

    Confirmed real case this closes: a real Review & Master row's own
    file address ("26 Saint James's Square") flagged a false conflict
    against its brochure's own spelling ("26 St James's Square") -
    genuinely the same address, "St"/"Saint" simply spelled two different
    real ways - with the resulting "Apply" button changing nothing at all
    (Current and New already identical text), confusing UX and a wasted
    review decision.

    Used only by _address_conflict_note's own comparison below - never
    _street_name_words itself (also used by _expanded_street_name_words/
    _building_identity_matches' own tier 5, a different, narrower concern
    - same "don't widen an existing shared helper" precedent _expanded_
    street_name_words's own docstring already sets for itself).
    """
    words = [w for w in normalize_key(address).split() if w not in _ADDRESS_RANGE_CONNECTOR_WORDS and not w.isdigit()]
    if len(words) > 1:
        words[:-1] = ["saint" if w == "st" else w for w in words[:-1]]
    return frozenset(words)


def _address_conflict_note(file_address, brochure_address):
    """
    A human-readable note (see schema.ListingRow.address_conflict's own
    docstring) describing a genuine disagreement between `file_address` (a
    row's own ALREADY-STATED address_1 - right or wrong, never re-checked
    once non-blank by BUILDING_LEVEL_FIELDS' own blank-only backfill rule)
    and `brochure_address` (the SAME building's own address_1 as that
    row's own brochure independently states it) - or None when there's no
    genuine conflict, or nothing real to compare at all.

    Two independent checks, either one alone is enough to flag a conflict:
    - a disagreeing LEADING HOUSE NUMBER (via house_number.leading_house_
      number - the same authoritative parser master_merge.house_number_
      changed already uses for this exact kind of comparison, never a
      second, independently-drifting implementation) - e.g. "27 Cannon
      Street" vs "108 Cannon Street".
    - street-name text that doesn't substantially overlap (a WORD-SET
      comparison via _address_conflict_street_words, once each side's own
      house number/range has been filtered out - and "st"/"saint" folded
      together as the same non-last-word token, see that function's own
      docstring for the real "26 Saint James's Square" vs "26 St James's
      Square" false-positive it exists for) - the confirmed real Ivybridge
      House case this exists for: address_1 "1 John Adam Street" vs its
      own brochure's "1 to 5 Adam Street" - the leading house numbers
      agree ("1" both sides), but "john" is a real word on the file's own
      side with nothing corresponding on the brochure's side.

    Deliberately conservative, same "a human catches a case this misses"
    philosophy as this file's other matching tiers - returns None (no
    flag) whenever:
    - either address is blank - nothing to compare;
    - the brochure's own text has no house-number-shaped token in it AT
      ALL (leading_house_number returns None) - a bare building/street
      name with nothing number-shaped to check against is not evidence of
      a conflict, just a brochure that didn't state a number;
    - both checks above find no disagreement - a genuine match, including
      one that only differs by formatting (case, punctuation, a range vs.
      its own first number, "St"/"Saint") already tolerated by normalize_
      key/leading_house_number/_address_conflict_street_words themselves.

    Never called when file_address is blank/a placeholder (see _is_
    placeholder_address) - that shape is already handled by BUILDING_
    LEVEL_FIELDS' own ordinary backfill, which is a completely different,
    unrelated case from this cross-check.
    """
    if _is_blank(file_address) or _is_blank(brochure_address):
        return None

    brochure_number = leading_house_number(brochure_address)
    if brochure_number is None:
        return None

    file_number = leading_house_number(file_address)
    if file_number is not None and file_number != brochure_number:
        return f"Brochure states '{brochure_address}', file has '{file_address}'"

    if _address_conflict_street_words(file_address) != _address_conflict_street_words(brochure_address):
        return f"Brochure states '{brochure_address}', file has '{file_address}'"

    return None


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
    match_indices = _building_identity_matches(
        row.building, [u.get("building") for u in plain_units], [u.get("address_1") for u in plain_units],
    )
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


_SPECIAL_FEATURES_ITEM_SPLIT_RE = re.compile(r"[;\n]+")


def _special_features_items(text: str) -> list:
    """
    `text` split on ";"/newline ONLY (matching master_merge._detail_items'
    own baseline split for this field, WITHOUT that pair's own extra
    comma-splitting - see _apply_units_to_row's own combine loop comment
    for why comma-splitting doesn't belong here).

    A `text` with NO ";"/newline at all (a single item) is returned
    completely UNSTRIPPED, byte-for-byte - unlike master_merge.
    _split_list_items, which strips every item unconditionally for its own
    different purpose (building a fresh merged value from possibly-
    differently-formatted sources). This matters here specifically because
    a single-item tier's text is written straight into the combined
    special_features value verbatim when it's the first (kept) occurrence
    - a row's own value that happens to have stray leading/trailing
    whitespace around it must survive completely unchanged when nothing
    about it is actually being replaced, the same "never touch a value
    that isn't genuinely different" guarantee every other read-only
    comparison in this codebase already gives (see master_merge.
    _values_equal's own docstring for the same principle).

    A `text` that DOES genuinely split into 2+ items has each of those
    items stripped, same as master_merge._split_list_items - confirmed
    necessary, not merely cosmetic: leaving the whitespace immediately
    surrounding each ";" delimiter untouched would otherwise leave a
    stray double space at every internal join point once items are
    reassembled ("Manned reception;  showers;  bike storage").

    A too-short fragment (an empty trailing segment from a trailing ";")
    is dropped, same threshold as master_merge._detail_items.
    """
    parts = _SPECIAL_FEATURES_ITEM_SPLIT_RE.split(text)
    if len(parts) == 1:
        return [text] if len(text.strip()) >= 3 else []
    return [p.strip() for p in parts if len(p.strip()) >= 3]


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
    each scope covers. For every field EXCEPT special_features, each later,
    narrower source OVERWRITES the same key in `updates` set by an earlier,
    wider one when both apply, since a more specific value is always
    preferred over a less specific one. special_features is the one
    exception: whichever of the row's own existing value and the three
    tiers below are genuinely present are instead COMBINED into one value,
    most-specific-to-least (row's own, then unit, then building, then
    property), "; "-joined - a unit's own stated features are never
    allowed to silently discard a real building- or property-wide amenity
    just because a narrower tier also happened to say something, and the
    row's own pre-existing value (even a non-descriptive one like a bare
    status marker) is never silently discarded either, since this combine
    is attempted regardless of whether that value was blank to begin with.
    Deduplication ACROSS tiers is item-level and exact-normalized-match
    only (see the combine loop's own comment below) - a unit's own text
    may still legitimately restate a building-wide detail Gemini already
    saw in DIFFERENT wording, which this never touches; only a literal,
    byte-identical-once-normalized repeated item is ever dropped. This
    replaced an earlier, narrower whole-TIER-only dedup once a real,
    reproducible failure showed that guard alone wasn't enough - see the
    combine loop's own comment for the confirmed real cases.

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

    address_1 gets a FOURTH, independent check alongside step 2 above,
    regardless of whether row.address_1 is already genuine (non-blank,
    non-placeholder): if the brochure's own building-level address text
    disagrees with what the row already has on file, address_conflict
    (see schema.ListingRow's own docstring) is set to a note describing
    both values - address_1 itself is NEVER overwritten by this (see
    _address_conflict_note's own docstring for exactly what counts as a
    genuine disagreement, and the real confirmed Ivybridge House case this
    exists for). A blank/placeholder address_1 is unaffected by this check
    entirely - that shape is what step 2's own ordinary backfill already
    handles.

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
    unit = _match_unit(row, units) if units else None

    if units is not None:
        contacts = getattr(units, "contacts", None)
        if _is_blank(row.contacts) and isinstance(contacts, str) and not _is_blank(contacts):
            updates["contacts"] = contacts

        # Combines every tier actually present, most-specific-to-least
        # (the row's OWN existing value first, if any, then unit, then
        # building, then property) - deliberately NOT the old "narrowest
        # present tier wins outright, the wider ones are simply dropped"
        # behavior (still true for every OTHER field's own property/
        # building/unit fallback - this is a special_features-only change).
        # Always attempted, never gated on row.special_features being blank
        # first - a row whose own spreadsheet already put something non-
        # descriptive there (e.g. a bare "U/O" status marker sitting in an
        # unlabeled column, confirmed present in real Workplace Plus rows)
        # must still get the brochure's real amenity info appended, not be
        # treated as "already has special_features, nothing to add" just
        # because that cell wasn't genuinely blank.
        #
        # A tier whose ENTIRE text is normalize_key-equal to an earlier,
        # already-kept tier is dropped rather than appended again - real,
        # confirmed case: a Kitt's "The Sevens, 77 Charlotte Street" row's
        # own spreadsheet special_features cell and the brochure's own
        # unit-level text for that same floor are verbatim the same short
        # blurb, so this combine used to produce "<blurb>; <blurb again>;
        # Manned reception; showers; bike storage" - a visibly duplicated
        # value that also tripped master_merge.is_richness_regression hard
        # enough to skip its own auto-merge, surfacing raw, duplicated text
        # to a reviewer as "New"/"may be missing detail" for no real
        # reason. Deliberately WHOLE-TIER, exact-normalized equality only -
        # never master_merge._items_similar's own looser, word-overlap
        # "same underlying fact" comparison, which is designed for a single
        # ITEM, not an entire tier that may itself already be several
        # semicolon-joined facts (row_features/unit_features/building_
        # features/property_features are all effectively multi-fact
        # blobs) - applying a partial-overlap threshold at that granularity
        # risks discarding a WHOLE tier over sharing just one fact with an
        # earlier tier, silently losing every other fact that tier alone
        # stated. Exact-match-only still fully covers the confirmed real
        # case (a verbatim-duplicated tier) with none of that risk.
        row_features = row.special_features if isinstance(row.special_features, str) and not _is_blank(row.special_features) else None
        unit_features = _coerced_unit_value("special_features", unit.get("special_features")) if unit else None
        building_features = _match_building_feature(row, units)
        if not (isinstance(building_features, str) and not _is_blank(building_features)):
            building_features = None
        property_features = getattr(units, "property_features", None)
        if not (isinstance(property_features, str) and not _is_blank(property_features)):
            property_features = None

        # ITEM-level dedup (see _special_features_items above - the SAME
        # semicolon/newline split master_merge._detail_items/_split_list_
        # items already use as their OWN baseline split for this field,
        # WITHOUT that pair's own extra comma-splitting - see below for
        # why, and WITHOUT their own item-stripping - see below too), not
        # merely whole-tier equality:
        # confirmed real, reproducible failure this closes - a genuine
        # "4 Moorgate" 2nd/4th/5th floor row (and many other real
        # buildings: 138 Cheapside, 108/120 Cannon Street, 44 Paul Street,
        # 26 Finsbury Square, Albion Mills, Conran Building, 95 Southwark
        # Street, The Rochester, among others) whose OWN row_features is a
        # short descriptive blurb, and whose brochure's own Gemini-
        # extracted unit_features RESTATES THAT SAME BLURB AS A PREFIX
        # before its own genuinely new facts ("<blurb>; 30 current desks;
        # ..."), is a DIFFERENT whole-tier string from row_features alone
        # ("<blurb>") - the prior whole-tier-only dedup below never caught
        # this, silently producing "<blurb>; <blurb>; 30 current desks; ..."
        # onto every affected row. Splitting each tier into its own items
        # FIRST, then deduping across all tiers by exact normalize_key
        # equality (never a fuzzy/reworded match - see master_merge.
        # _has_suspicious_duplicate_items' own docstring for why only exact
        # duplication is safe evidence here), keeps the real new items
        # ("30 current desks" etc.) while dropping only the literal
        # repeated blurb - still fully covers the original "verbatim-
        # duplicated whole tier" case (The Sevens) this dedup already
        # existed for, since that's just the special case of every item in
        # a tier being a duplicate. Item-level, not fuzzy - a unit's own
        # text still legitimately restates a building-wide detail Gemini
        # already saw in different WORDING (only a BYTE-IDENTICAL-once-
        # normalized item is ever dropped), so this never risks stripping
        # a genuinely different fact the way a looser dedup rule would.
        # Uses _special_features_items (this module's own semicolon/
        # newline-only split, UNSTRIPPED - see its own docstring), NOT
        # master_merge._split_list_items' extra comma-splitting
        # (_MERGE_COMMA_SPLIT_FIELDS) - that extra split exists purely for
        # is_detail_loss/merge_compatible_text's own item-SIMILARITY
        # comparison, a different concern; splitting on comma HERE would
        # reformat a single descriptive sentence's own internal commas
        # into semicolons in the combined text merely because it happened
        # to pass through this loop (e.g. "New fitout, bright floor" ->
        # "New fitout; bright floor") - a needless cosmetic rewrite with no
        # bearing on the actual duplicate this exists to catch, which only
        # ever occurs at a genuine ";"/newline boundary (Gemini restates a
        # row's own full sentence as one semicolon-delimited item, prefixed
        # before its own new ones - see this loop's own confirmed real
        # cases above). Also, unlike _split_list_items, never strips a kept
        # item's own text - a row's own stray whitespace must survive
        # completely unchanged (see _special_features_items' own
        # docstring).
        kept_items = []
        kept_item_keys = set()
        for seg in (row_features, unit_features, building_features, property_features):
            if not seg:
                continue
            for item in (_special_features_items(seg) or [seg]):
                key = normalize_key(item)
                if key and key in kept_item_keys:
                    continue
                if key:
                    kept_item_keys.add(key)
                kept_items.append(item)

        combined = "; ".join(kept_items)
        if combined and combined != row.special_features:
            updates["special_features"] = combined

        for field in BUILDING_LEVEL_FIELDS:
            if field == "address_1":
                if _is_placeholder_address(row.address_1, row.building):
                    # A placeholder address_1 (blank, or just a copy of
                    # the row's own building - see _is_placeholder_
                    # address) is eligible to be overwritten by a genuine
                    # brochure-derived address here, same as a blank one
                    # always was.
                    value = _match_building_value(row, units, field)
                    if value is not None:
                        updates[field] = value
                    continue
                # A GENUINE, already-stated address_1 is never overwritten
                # here (that's what the placeholder branch above is for) -
                # but it's still worth cross-checking against what this
                # row's own brochure independently states, since being
                # non-blank has never itself been evidence the value is
                # actually correct (see _address_conflict_note's own
                # docstring for the real confirmed Ivybridge House case
                # this catches - a wrong address that was simply never re-
                # checked once address_1 stopped being blank).
                brochure_value = _match_building_value(row, units, field)
                if brochure_value is not None:
                    conflict = _address_conflict_note(row.address_1, brochure_value)
                    if conflict:
                        updates["address_conflict"] = conflict
                continue
            if not _is_blank(getattr(row, field)):
                continue
            value = _match_building_value(row, units, field)
            if value is not None:
                updates[field] = value

    if units and unit is not None:
        # Checked ONCE per unit, before the per-field loop below - see
        # _rent_check_values/_rent_values_consistent's own docstrings.
        # rent_conflict being True means this unit's own rent_pcm/
        # rent_psf don't add up against whatever size/sibling-rent value
        # is already trustworthy for this row, so neither is safe to
        # write - "incorrect enrichment is worse than a blank field",
        # same philosophy as every other tier in this module. Never
        # affects a field that's already non-blank on the row (the
        # per-field blank-only guard below still applies first), and
        # never affects UNIT_LEVEL_FIELDS other than the two rent ones.
        rent_conflict = not _rent_values_consistent(*_rent_check_values(row, unit))
        for field in UNIT_LEVEL_FIELDS + HIGH_RISK_UNIT_LEVEL_FIELDS:
            if field == "special_features":
                continue  # handled above (combined across all three tiers), never the single-value overwrite below
            if not _is_blank(getattr(row, field)):
                continue
            if field in HIGH_RISK_UNIT_LEVEL_FIELDS and rent_conflict:
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
    (see is_generic_link), a Canva OR Pitch.com public "view" link (see
    is_canva_view_link/is_pitch_view_link's own docstrings - a floor plan
    is never retrievable from either, for the same reason a brochure
    isn't, unless _canva_renderer_configured), a Google Drive FOLDER
    share link (see _is_google_drive_folder_link - confirmed real: 20+
    distinct folder links used as floorplan_link in Kitt's Availability
    file, previously falling through to a real fetch attempt structurally
    guaranteed to fail), and a video link - a floor plan is never hosted
    at either of those either.
    """
    if _is_blank(url):
        return False
    if urlparse(url).scheme not in ("http", "https"):
        return False
    if is_generic_link(url):
        return False
    if (
        is_canva_view_link(url) or is_pitch_view_link(url) or is_gpe_flipbook_link(url)
        or is_kitt_brochure_preview_link(url)
    ) and not _canva_renderer_configured():
        return False
    if _is_google_drive_folder_link(url):
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
        _record_status(STATUS_FETCH_FAILED, "no document bytes obtained")
        return None

    try:
        images = _images_from_fetched_document(data)
    except Exception as e:
        print(
            f"[brochure_enrichment] Could not render {url!r} as a floor plan ({e!r}) — skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_RENDER_FAILED, f"{e!r}")
        return None

    data = None
    try:
        raw = extract.render_and_extract(images, prompt=FLOORPLAN_PROMPT)
    except Exception as e:
        print(
            f"[brochure_enrichment] Could not read {url!r} as a floor plan ({e!r}) — skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_EXTRACTION_FAILED, f"{e!r}")
        return None

    units = raw.get("units")
    result = [u for u in units if isinstance(u, dict)] if isinstance(units, list) else []
    _record_status(STATUS_EXTRACTED_SUCCESSFULLY if result else STATUS_EXTRACTED_NO_USEFUL_DATA)
    return result


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
    docstring): a URL already marked "ok" is skipped UNLESS at least one
    row sharing it is still blank (including when every sharing row is -
    see this function's own indices_by_url comment, below, for why an
    all-still-blank url is NOT reliable evidence "nothing here", and must
    still be re-fetched exactly like a partial-fill outcome does, even
    though the url itself is "ok").
    Both callbacks fire after EVERY floor plan (never batched - this worklist is
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
    current = list(rows)
    log = []
    floorplans_read_ok = 0
    floorplans_unavailable = 0
    processed_urls = {}
    # See enrich_rows_grouped's own document_issues docstring - additive
    # diagnostics alongside processed_urls, never replacing it.
    document_issues = _ineligible_link_issues(
        rows, needs_floorplan_enrichment, "floorplan_link", reject_floorplan_shaped=False,
    )
    stats = {
        "unique_floorplans_considered": len(unique_urls), "floorplans_read_ok": 0,
        "floorplans_unavailable": 0, "processed_urls": processed_urls, "document_issues": document_issues,
    }

    # indices_by_url tracks which rows STILL need this floor plan applied
    # (needs_floorplan_enrichment - a row already filled is never in here).
    # A url already marked "ok" is re-fetched whenever ANY row sharing it
    # is still in here - including when EVERY sharing row still is. A
    # coarse per-URL "ok" is never itself reliable evidence a document
    # "genuinely has nothing" for every row sharing it: the same failure
    # shape (every sharing row still blank) is equally consistent with a
    # real matching bug silently failing 100% of the time (see brochure_
    # enrichment.py's own confirmed regentswharf.co.uk case in enrich_
    # rows_grouped's own already_processed docstring) as it is with a
    # genuinely empty document - the two are indistinguishable from the
    # outside, so re-checking is the safer default; the cost is bounded,
    # since this only ever runs in response to an explicit resume/re-
    # upload action, never an automatic loop. Confirmed real PARTIAL case
    # too, unaffected by this: several floors of the same building all
    # pointing at one shared floor plan PDF, where only some floors' own
    # unit boxes on it were legible/labeled - at least one sibling row
    # VISIBLY got a real value from this exact document already (see
    # _apply_floorplan_units_to_row's own per-row matching), which is
    # direct proof this floor plan DOES have real content, just not
    # something that matched every row sharing it.
    #
    # Raw extracted units are never persisted across runs (only the ok/
    # unavailable status itself is, via processed_urls/already_processed),
    # so a full re-fetch is the only way to recover a stranded row's value.
    indices_by_url = {}
    for i, row in enumerate(rows):
        if not _is_eligible_floorplan_url(row.floorplan_link):
            continue
        if needs_floorplan_enrichment(row):
            indices_by_url.setdefault(row.floorplan_link, []).append(i)

    urls_to_fetch = [
        u for u in unique_urls
        if already_processed.get(u) != "ok"
        or len(indices_by_url.get(u, [])) > 0
    ]
    if not urls_to_fetch:
        return current, log, stats

    for url in urls_to_fetch:
        sink = {}
        try:
            with _StatusCapture(sink):
                units = _extract_floorplan_units(url)
        except Exception as e:
            print(
                f"[brochure_enrichment] Unexpected error reading {url!r} as a floor plan ({e!r}) — skipping.",
                file=sys.stderr,
            )
            units = None
            sink.setdefault("status", STATUS_EXTRACTION_FAILED)

        if units is not None:
            floorplans_read_ok += 1
            processed_urls[url] = "ok"
        else:
            floorplans_unavailable += 1
            processed_urls[url] = "unavailable"

        # Falls back to a generic status when the real fetch/render/extract
        # body didn't run this call (a cross-run cache hit - see
        # _StatusCapture's own docstring) rather than leaving this
        # document unrepresented in diagnostics at all.
        status = sink.get("status") or (STATUS_EXTRACTED_SUCCESSFULLY if units else STATUS_FETCH_FAILED)
        if status in ISSUE_STATUSES:
            for i in indices_by_url[url]:
                document_issues.append({
                    "building": current[i].building, "floor_unit": current[i].floor_unit, "status": status,
                })

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
    floorplan_url_checkpoint_callback=None, special_features_matched: dict = None,
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
    interrupted run: a URL already marked "ok" here is skipped - never
    re-fetched, never re-sent to Gemini - PROVIDED every row sharing that
    url has already been genuinely resolved. Since "blank special_features"
    alone can never tell a caller whether a brochure was already
    successfully checked and genuinely had nothing to contribute, or was
    never checked at all (see this module's own ENRICHABLE_FIELDS docstring
    on why a blank value is never itself evidence of anything), that same
    ambiguity applies PER ROW, not just per url, once more than one row
    shares a brochure_link: _apply_units_to_row does its own per-row
    matching, and can legitimately fill some of a shared document's rows
    while leaving others blank (no confident match for that specific row) -
    the url still gets marked "ok" because the fetch/extraction itself
    succeeded, so a plain url-only skip would strand those never-actually-
    resolved rows blank forever, indistinguishable from a row correctly
    left blank because the document had nothing for it. See sharing_counts/
    still_blank_counts below (NOT indices_by_url/needs_enrichment, which
    stay reserved for the per-row application loop - needs_enrichment is
    deliberately always True for a row with an eligible brochure_link, so
    it can't also serve as evidence a row's own value is still missing, see
    _row_has_a_genuinely_blank_enrichable_field's own docstring) - the
    fetch is redone whenever ANY row sharing the url is still genuinely
    blank, including when every sharing row is (still_blank_counts ==
    sharing_counts), NOT only the strictly-partial case. Confirmed real
    gap the all-still-blank case used to leave open: a real regentswharf.co.uk
    Colliers brochure whose OWN several floors per building all share one
    brochure_link, where a (since-fixed) building-identity-matching bug
    made EVERY floor's own match fail identically - a url where every
    sharing row happens to be blank is NOT reliable evidence the document
    "genuinely has nothing here" the way it would be for a document that
    was actually read correctly; it's equally consistent with a matching
    bug silently failing 100% of the time. Re-checking an all-blank url on
    every resume/re-upload is a bounded cost (only ever triggered by an
    explicit user action, never an automatic loop - see the "unavailable"
    paragraph below), so treating "still blank" as the one signal worth
    trusting - regardless of whether it's partial or total - is the safer
    default. Confirmed real partial case too, unaffected by this change:
    several floors of the same building sharing one brochure_link, where
    only some floors' own units were legible/labeled in it.
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

    stats["document_issues"] (list of {"building", "floor_unit", "status"})
    is ADDITIVE diagnostics alongside processed_urls, never a replacement
    for it - one entry per row with a genuine problem (see ISSUE_STATUSES):
    an ineligible link (unsupported_link_type - detected before any fetch,
    see _ineligible_link_issues), a fetch/render/extraction failure for
    that row's own brochure/floorplan, or a document that read fine overall
    but couldn't be safely matched to THIS row specifically (extracted_but_
    ambiguous - see _row_had_ambiguous_match). A row with nothing wrong (no
    document at all, a placeholder, or a document that simply had nothing
    relevant) is never in this list - see storage.file_store's own
    persistence of this same list for how a caller renders it.

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

    special_features_matched ({str(row_index): True}, from a PRIOR call of
    this same function against this same staging file - see storage.
    file_store's own persistence of this alongside processed_urls) is the
    per-staging-file sidecar record of which rows have ever received a
    genuine unit-/building-/property-level special_features combine (see
    _row_has_a_genuinely_blank_enrichable_field's own docstring on why a
    row's own special_features text alone is never reliable evidence of
    this). Deliberately NOT a ListingRow field - it's pure resume
    bookkeeping for THIS function's own still_blank_counts check below,
    never a property fact, so it has no business living in master.xlsx or
    a staging file's own spreadsheet schema. Keyed by the row's plain
    positional index within `rows`, stringified (matching how a JSON
    sidecar naturally round-trips object keys) - safe because this
    function and _enrich_rows_from_floorplans both build their own
    returned list as `current = list(rows)` and only ever replace entries
    IN PLACE by index, never reorder/add/drop a row (see _regeocode_rows_
    with_newly_backfilled_addresses' own docstring, which relies on this
    same guarantee for its own index-paired zip), and storage.file_store.
    update_staging_rows persists that same full ordered list wholesale on
    every checkpoint - so a row's index stays stable across however many
    resumed calls this staging file goes through. Defaults to {} (nothing
    previously matched - identical to every row starting unmatched, the
    only possible state before this parameter existed).

    stats["special_features_matched"] ({str(row_index): True}) reports
    every row that received a genuine combine during THIS call only (never
    includes an entry already true coming in) - same "this call's own new
    knowledge, merge it yourself" contract as stats["processed_urls"]
    above, for the same reason: a caller can always tell "what did THIS
    call itself just learn" apart from "what was already known coming in".
    """
    progress_callback = progress_callback or (lambda done, total, label: None)
    already_processed = already_processed or {}
    special_features_matched = special_features_matched or {}
    special_features_matched_this_call = {}
    eligible, unique_urls = eligible_rows_and_brochures(rows)

    # indices_by_url (needs_enrichment-based, UNCHANGED by this fix) drives
    # the per-row APPLICATION loop below - see needs_enrichment's own
    # docstring for why it deliberately keeps calling a row "eligible" even
    # once every field, including special_features, already has a value
    # (so the additive combine keeps getting a chance to run on every
    # call). first_label is built alongside it for the same reason it
    # always was.
    #
    # still_blank_counts is a SEPARATE signal, used only to decide whether
    # a url ALREADY marked "ok" in already_processed is worth re-fetching
    # on resume (see that parameter's own docstring above) - needs_
    # enrichment can't serve this purpose, since it's True for every row
    # sharing an eligible brochure_link regardless of whether special_
    # features already has real content, so it could never tell a genuine
    # "nothing found for anyone" apart from "some/all rows are still
    # stranded". still_blank_counts instead uses _row_has_a_genuinely_
    # blank_enrichable_field (see its own docstring) - the plain "is
    # anything actually still missing" question (special_features's own
    # check there uses special_features_matched, never a blunt brochure_
    # link-based override the way needs_enrichment does) - which is
    # exactly what distinguishes a row that was never actually resolved
    # from one that was.
    first_label = {}
    indices_by_url = {}
    still_blank_counts = {}
    for i, row in enumerate(rows):
        if not _is_eligible_brochure_url(row.brochure_link):
            continue
        row_matched = bool(special_features_matched.get(str(i)))
        if _row_has_a_genuinely_blank_enrichable_field(row, special_features_matched=row_matched):
            still_blank_counts[row.brochure_link] = still_blank_counts.get(row.brochure_link, 0) + 1
        if needs_enrichment(row):
            indices_by_url.setdefault(row.brochure_link, []).append(i)
            first_label.setdefault(row.brochure_link, row.building)

    urls_to_fetch = [
        u for u in unique_urls
        if already_processed.get(u) != "ok"
        or still_blank_counts.get(u, 0) > 0
    ]

    current = list(rows)
    log = []
    brochures_read_ok = 0
    brochures_unavailable = 0
    processed_urls = {}
    # Additive diagnostics, alongside (never replacing) processed_urls -
    # see this function's own docstring on why processed_urls itself keeps
    # its existing "ok"/"unavailable" vocabulary unchanged (35+ existing
    # tests and the on-disk checkpoint format already depend on those exact
    # two strings for resume/retry logic - see storage.file_store's own
    # _derive_enrichment_counts). One {"building", "floor_unit", "status"}
    # entry per row with a genuine document ISSUE (see ISSUE_STATUSES) -
    # never one for a row with nothing wrong, and never one per URL when
    # many rows share it (a row-level line, matching what a reviewer
    # actually needs to go check: WHICH property, not which raw link).
    document_issues = _ineligible_link_issues(rows, needs_enrichment, "brochure_link", reject_floorplan_shaped=True)

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
            "special_features_matched": special_features_matched_this_call,
            "document_issues": document_issues + floorplan_stats["document_issues"],
            "unique_floorplans_considered": floorplan_stats["unique_floorplans_considered"],
            "floorplans_read_ok": floorplan_stats["floorplans_read_ok"],
            "floorplans_unavailable": floorplan_stats["floorplans_unavailable"],
            "floorplan_processed_urls": floorplan_stats["processed_urls"],
        }

    def _fetch_one(url):
        # Runs in a worker thread - deliberately returns its result (now
        # including the status this call's own _StatusCapture recorded, if
        # any) rather than touching any shared state itself (see the
        # function's own docstring on why that's what makes this safe
        # without a lock) - status_sink is thread-local (see _StatusCapture
        # itself), so this is safe regardless of how many workers run
        # concurrently.
        sink = {}
        try:
            with _StatusCapture(sink):
                units = _extract_brochure_units(url)
            return url, units, sink
        except Exception as e:
            print(f"[brochure_enrichment] Unexpected error reading {url!r} ({e!r}) — skipping.", file=sys.stderr)
            sink.setdefault("status", STATUS_EXTRACTION_FAILED)
            return url, None, sink

    since_checkpoint = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = [executor.submit(_fetch_one, url) for url in urls_to_fetch]
        for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            url, units, sink = future.result()
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

            # Falls back to a generic status when the real fetch/render/
            # extract body didn't run for THIS call (a cross-run cache hit
            # - see _StatusCapture's own docstring) rather than leaving
            # this document unrepresented in diagnostics at all.
            document_status = sink.get("status") or (STATUS_EXTRACTED_SUCCESSFULLY if units else STATUS_FETCH_FAILED)
            if document_status in ISSUE_STATUSES:
                for i in indices_by_url[url]:
                    document_issues.append({
                        "building": rows[i].building, "floor_unit": rows[i].floor_unit, "status": document_status,
                    })

            # brochure_link_broken update for every row sharing this URL -
            # see that field's own schema.py docstring for the full tri-
            # state contract. Computed ONCE per URL (never per row - it's
            # the same underlying document/render outcome for all of
            # them), then applied uniformly below. None here means "leave
            # whatever this row already had alone" (see master_merge.
            # diff_fields' own blank-new-value-skip rule for why that's
            # exactly what a None ends up doing on the next merge) - never
            # forced to False/True when this call has no fresh, confident
            # signal either way.
            if document_status in (STATUS_EXTRACTED_SUCCESSFULLY, STATUS_EXTRACTED_NO_USEFUL_DATA):
                link_broken_update = False
            elif document_status == STATUS_RENDER_FAILED and _is_confirmed_dead_canva_link(sink.get("detail")):
                link_broken_update = True
            else:
                link_broken_update = None

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
                update = {}
                if link_broken_update is not None:
                    update["brochure_link_broken"] = link_broken_update
                if update:
                    new_row = new_row.model_copy(update=update)
                # special_features_matched sidecar record (see enrich_rows_
                # grouped's own special_features_matched param docstring) -
                # True only when _apply_units_to_row actually changed
                # special_features THIS call (present in its own `fields`
                # list, never merely carried the row's own existing value
                # forward unchanged). Recorded here, NOT on the row itself
                # (unlike brochure_link_broken above) - this is per-staging-
                # file resume bookkeeping, never a spreadsheet column.
                # Never recorded as False here (or anywhere else) - same
                # "no confirmed-nothing-here state" reasoning as brochure_
                # link_broken's own None-vs-False distinction, since a
                # brochure that matched nothing today may still match once
                # a sibling row's own matching logic improves.
                if "special_features" in fields:
                    special_features_matched_this_call[str(i)] = True
                current[i] = new_row
                if fields:
                    if (
                        is_canva_view_link(url) or is_pitch_view_link(url) or is_gpe_flipbook_link(url)
                        or is_kitt_brochure_preview_link(url)
                    ):
                        # Distinct, greppable confirmation that a Canva/
                        # Pitch/Kitt-sourced render made it all the way
                        # through the existing extraction pipeline AND
                        # actually produced usable field values on a real
                        # row - the final link in the chain _fetch_canva_
                        # rendered_page's/_fetch_pitch_rendered_page's/
                        # _fetch_kitt_rendered_page's own "<Platform>
                        # render succeeded" log confirms only the rendering
                        # half of (see this module's own "prove end-to-end"
                        # verification).
                        platform_label = _render_platform_label(url)
                        print(
                            f"[brochure_enrichment] {platform_label} enrichment applied {fields} to "
                            f"{new_row.building!r} ({new_row.floor_unit!r}).",
                            file=sys.stderr,
                        )
                    log.append({
                        "building": new_row.building,
                        "floor_unit": new_row.floor_unit,
                        "fields": fields,
                        "brochure_link": new_row.brochure_link,
                    })
                elif document_status == STATUS_EXTRACTED_SUCCESSFULLY and needs_enrichment(new_row):
                    # The document itself read fine and DID have content,
                    # but this specific row got nothing from it - only
                    # worth flagging as an issue if that's because THIS
                    # row's own building genuinely had multiple candidate
                    # units this document couldn't disambiguate (see
                    # _row_had_ambiguous_match's own docstring) - never
                    # merely because the document had nothing relevant to
                    # this row at all, which is a completely normal, silent
                    # outcome, not an issue.
                    try:
                        ambiguous = _row_had_ambiguous_match(rows[i], units) or _row_had_rent_conflict(rows[i], units)
                    except Exception:
                        ambiguous = False
                    if ambiguous:
                        document_issues.append({
                            "building": new_row.building, "floor_unit": new_row.floor_unit,
                            "status": STATUS_EXTRACTED_BUT_AMBIGUOUS,
                        })
                    if (
                        is_canva_view_link(url) or is_pitch_view_link(url) or is_gpe_flipbook_link(url)
                        or is_kitt_brochure_preview_link(url)
                    ):
                        # Canva/Pitch/Kitt-specific diagnostic only - the
                        # document read fine (see the extraction log above)
                        # but this ROW still has blank fields afterward;
                        # names the exact fields and whether an ambiguous/
                        # rent-conflict match was the reason, so "Gemini
                        # found nothing for this row" and "Gemini found
                        # something but matching/the rent guard rejected
                        # it" are never conflated.
                        still_blank = [f for f in ENRICHABLE_FIELDS if _is_blank(getattr(new_row, f))]
                        reason = "ambiguous/rent-conflicting match" if ambiguous else "no confident match for this row"
                        platform_label = _render_platform_label(url)
                        print(
                            f"[brochure_enrichment] {platform_label} extraction read fine for {new_row.building!r} "
                            f"({new_row.floor_unit!r}) but left {still_blank} blank — {reason}.",
                            file=sys.stderr,
                        )

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
        "special_features_matched": special_features_matched_this_call,
        "document_issues": document_issues + floorplan_stats["document_issues"],
        "unique_floorplans_considered": floorplan_stats["unique_floorplans_considered"],
        "floorplans_read_ok": floorplan_stats["floorplans_read_ok"],
        "floorplans_unavailable": floorplan_stats["floorplans_unavailable"],
        "floorplan_processed_urls": floorplan_stats["processed_urls"],
    }
    return current, log, stats


def _regeocode_rows_with_newly_backfilled_addresses(original_rows: list, enriched_rows: list) -> None:
    """
    Re-geocodes any row whose address_1/postcode was genuinely BLANK, OR
    whose address_1 was only a placeholder (see _is_placeholder_address -
    blank, or just a copy of the row's own building), before this
    enrichment pass and is now filled in with something real (see
    BUILDING_LEVEL_FIELDS/_apply_units_to_row's own backfill, above) - a
    Tier 2 zero-hint guess (see schema.ListingRow.geocode_unverified's own
    docstring) made back when geocode_row had no real address at all to
    check itself against can now run through Tier 1 against this freshly-
    backfilled address instead. On success, geocode_row's own existing
    Tier 1 branch already sets geocode_unverified=False itself (the
    earlier self-correction fix - nothing new needed here); on failure, it
    falls through to geocode_row's own existing Tier 2 logic exactly as
    today - never a special case, just an ordinary call.

    row.lat/row.lng are cleared FIRST when geocode_unverified was True -
    otherwise geocode_row's own early-return guard ("already has real
    coordinates") would block Tier 1 from ever running against the new
    address at all, leaving the untrustworthy Tier 2 guess in place
    despite a real address now being on file.

    Paired by INDEX (zip), never property_id and never object identity:
    - property_id is still blank for every row at this pending/staging
      stage (see schema.ListingRow.property_id's own docstring -
      "assigned once a row lands in the master... never set by
      extraction"), so matching on it would collapse every row to the
      same key.
    - a changed row comes back as a NEW ListingRow (model_copy), never
      the same object, so identity comparison would silently skip every
      row this enrichment pass actually touched.
    enrich_rows_grouped/_enrich_rows_from_floorplans both build their own
    returned list as `current = list(rows)` and only ever replace entries
    IN PLACE by index (never reorder, add, or drop a row) - see either
    function's own docstring - so paired position is the one reliable
    identity available here.

    A row whose address_1 was already genuine (not blank, not a placeholder)
    and whose postcode was already present before this pass is completely
    untouched - no wasted re-geocode call. This adds no new network/Gemini
    cost beyond an occasional Geocoding API call: the brochure itself is
    already fetched exactly once per URL by enrich_rows_grouped for its own,
    separate enrichment purposes, regardless of whether this function ever
    runs at all.
    """
    for original, enriched in zip(original_rows, enriched_rows):
        address_1_backfilled = _is_placeholder_address(
            original.address_1, original.building
        ) and not _is_placeholder_address(enriched.address_1, enriched.building)
        postcode_backfilled = _is_blank(original.postcode) and not _is_blank(enriched.postcode)
        if not (address_1_backfilled or postcode_backfilled):
            continue
        if enriched.geocode_unverified:
            enriched.lat = None
            enriched.lng = None
        geocode.geocode_row(enriched)


def _propagate_shared_brochure_link_within_building(rows: list) -> list:
    """
    `rows` with a blank brochure_link filled in from a SIBLING row (same
    building+provider, elsewhere in this same batch) whenever exactly ONE
    distinct non-blank brochure_link exists among that group - never
    touches a row that already has its own value, and never guesses when
    the group's own non-blank links disagree.

    Real, confirmed gap this closes: a schedule-of-areas brochure covering
    several floors of ONE building routinely has its brochure_link
    genuinely stated (by the source spreadsheet, or found by Gemini) for
    only ONE of those floors' own rows - a real Henly House upload's own
    "1st"/"2nd"/"3rd" floor rows all had brochure_link blank while "4th"
    alone carried the real link, all four genuinely describing the SAME
    document. needs_enrichment/eligible_rows_and_brochures both key
    eligibility on a row's OWN brochure_link field directly (see either's
    own docstring) - a row with nothing there is simply never fetched,
    matched, or enriched at all, no matter how many times the same
    content gets re-uploaded, since a blank field is never itself
    evidence of what a sibling row already knows. This runs BEFORE
    eligibility is ever computed (see run_brochure_enrichment, the one
    caller, called at the very start) so every downstream step - fetch,
    match, apply - simply sees a row that already has the link it should
    have had from the start, no special-casing needed anywhere else.

    Grouped by (building, provider) - the same two fields _fallback_key's
    own building/provider tier in master_merge.py already treats as a
    listing's stable identity - never floor_unit, since the whole point
    is bridging rows that describe DIFFERENT floors of the SAME building.
    Two or more DISTINCT non-blank links within one group (a genuine
    multi-building portfolio brochure's own per-property links, or two
    genuinely unrelated listings that happen to share a building name/
    provider) is deliberately never resolved - "incorrect enrichment is
    worse than a blank field", the same principle every matching tier in
    this module already applies; that group is left completely
    untouched, exactly as if this function didn't exist for it.

    Paired by INDEX, never object identity or property_id - the same
    reasoning as _regeocode_rows_with_newly_backfilled_addresses' own
    docstring: property_id is still blank at this pending/staging stage,
    and a changed row is a NEW ListingRow (model_copy), never the same
    object. Returns a NEW list (rows themselves are never mutated in
    place) - callers reassign their own `rows` variable, the same
    contract every other row-list-transforming function in this module
    already has.
    """
    groups_by_key = {}
    for i, row in enumerate(rows):
        building_key = normalize_key(row.building)
        if not building_key:
            continue
        groups_by_key.setdefault((building_key, normalize_key(row.provider)), []).append(i)

    updated = list(rows)
    for indices in groups_by_key.values():
        if len(indices) < 2:
            continue
        distinct_links = {rows[i].brochure_link for i in indices if not _is_blank(rows[i].brochure_link)}
        if len(distinct_links) != 1:
            continue
        shared_link = next(iter(distinct_links))
        for i in indices:
            if _is_blank(rows[i].brochure_link):
                updated[i] = rows[i].model_copy(update={"brochure_link": shared_link})
    return updated


def run_brochure_enrichment(
    rows: list, staging_path: str, already_processed: dict, floorplan_already_processed: dict = None,
    special_features_matched: dict = None,
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
    first, e.g. "Now checking N brochures..." vs. "Resuming...", since
    that wording legitimately differs per caller) and a final "Done: ..."
    caption once every remaining brochure AND floorplan has been
    processed. Returns the enriched rows so
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

    special_features_matched, when given, is a PRIOR call's own persisted
    sidecar record (see storage.file_store.set_staging_enrichment_summary's
    own param of the same name, and brochure_enrichment.enrich_rows_
    grouped's own param/stats-key docstrings) - threaded straight through
    to enrich_rows_grouped and merged with this call's own new matches
    before being persisted again, the exact same "in, merge, persist
    cumulative" shape as already_processed/processed_urls above. Defaults
    to {} for the ordinary fresh-run case, identical to every prior
    behavior before this parameter existed.

    rows is first passed through _propagate_shared_brochure_link_within_
    building (see its own docstring) - BEFORE eligibility is computed, so
    a row whose own brochure_link was left blank by extraction, but whose
    sibling floor in the same building genuinely has one, becomes
    eligible here too, rather than being silently skipped by every
    downstream step for good. Running this here (the one shared
    orchestration both real callers - the automatic post-upload run and
    "Continue enrichment" - go through) means neither needs its own copy
    of this logic.
    """
    floorplan_already_processed = floorplan_already_processed or {}
    special_features_matched = special_features_matched or {}
    rows = _propagate_shared_brochure_link_within_building(rows)
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
        special_features_matched=special_features_matched,
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
        special_features_matched=special_features_matched,
    )
    progress_slot.empty()

    # A row that just had address_1/postcode backfilled from its own
    # brochure (see BUILDING_LEVEL_FIELDS above) may have been geocoded
    # earlier - before staging, with no address to check itself against -
    # via geocode.py's own Tier 2 zero-hint fallback. Re-geocode it now
    # that a real address is on file (see _regeocode_rows_with_newly_
    # backfilled_addresses's own docstring) - mutates enriched_rows'
    # own entries in place, so the persistence below already reflects it.
    _regeocode_rows_with_newly_backfilled_addresses(rows, enriched_rows)

    cumulative_processed_urls = {**already_processed, **stats["processed_urls"]}
    cumulative_floorplan_processed_urls = {**floorplan_already_processed, **stats["floorplan_processed_urls"]}
    cumulative_special_features_matched = {**special_features_matched, **stats["special_features_matched"]}
    update_staging_rows(staging_path, enriched_rows)
    set_staging_enrichment_summary(
        staging_path, stats, cumulative_processed_urls,
        floorplan_processed_urls=cumulative_floorplan_processed_urls,
        unique_floorplans_considered=stats["unique_floorplans_considered"],
        document_issues=stats["document_issues"],
        special_features_matched=cumulative_special_features_matched,
    )

    counts = _derive_cumulative_counts(cumulative_processed_urls)
    total_brochures = len(unique_urls)
    rows_enriched = stats["rows_enriched"]
    summary = (
        f"{counts['ok']} of {total_brochures} brochure{'s' if total_brochures != 1 else ''} read successfully, "
        f"adding details to {rows_enriched} row{'s' if rows_enriched != 1 else ''}."
    )
    if counts["unavailable"]:
        summary += f" {counts['unavailable']} couldn't be read."
    if stats["unique_floorplans_considered"]:
        floorplan_counts = _derive_cumulative_counts(cumulative_floorplan_processed_urls)
        total_floorplans = stats["unique_floorplans_considered"]
        summary += (
            f" {floorplan_counts['ok']} of {total_floorplans} "
            f"floor plan{'s' if total_floorplans != 1 else ''} also read."
        )
    st.caption(f"Done: {summary}")

    return enriched_rows


def _derive_cumulative_counts(processed_urls: dict) -> dict:
    return {
        "ok": sum(1 for v in processed_urls.values() if v == "ok"),
        "unavailable": sum(1 for v in processed_urls.values() if v == "unavailable"),
    }
