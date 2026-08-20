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
    REQUEST_TIMEOUT, USER_AGENT, finalize_floorplan_link, is_canva_view_link, is_floorplan_not_brochure_url,
    is_generic_link, looks_like_url, resolve_brochure_link,
)
import geocode
from gemini_client import compute_rent
from house_number import leading_house_number
from master_merge import normalize_key
from schema import ExtractedFields, ListingRow
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
      social-profile domain, a video link, a Canva public "view" link - see
      brochure_link_resolver.is_canva_view_link's own docstring on why a
      plain HTTP fetch can never retrieve real content from one, confirmed
      directly against a real example rather than assumed - a Google Drive
      FOLDER share link, see _is_google_drive_folder_link's own docstring,
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
    if is_canva_view_link(url) and not _canva_renderer_configured():
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
# and a wrong guess (e.g. treating "Ground" as floor 0) risks a false match
# against a genuinely different numbered floor.
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


def _floor_number(floor_unit):
    """
    The leading digit run in `floor_unit` as an int (e.g. 5 from "5th
    Floor"), or - when there's no digit at all - a recognized spelled-out
    numbered-floor ordinal word (e.g. 3 from "Third Floor", case-
    insensitive; see _ORDINAL_WORD_TO_NUMBER), or None if `floor_unit` is
    blank or matches neither form (e.g. "Ground Floor", "Reception") -
    those never participate in this fallback tier, exactly as if it didn't
    exist for them (falls through to the existing size-based tier, or no
    match, same as before either form of this existed). The digit form is
    checked first and wins if present - a label with both a digit and a
    coincidental word match is not a real case this needs to handle
    specially.
    """
    if _is_blank(floor_unit):
        return None
    text = str(floor_unit)
    digit_match = _FLOOR_NUMBER_RE.search(text)
    if digit_match:
        return int(digit_match.group())
    word_match = _ORDINAL_WORD_RE.search(text)
    return _ORDINAL_WORD_TO_NUMBER[word_match.group(1).lower()] if word_match else None


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
    "parade",
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
       the identical key. Only ever accepted when it is the SOLE candidate
       sharing that stripped key - two or more candidates sharing it is
       exactly as ambiguous as two or more exact matches would be if this
       fell back to guessing between them, so it stays unresolved (empty),
       same "incorrect enrichment is worse than a blank field" philosophy
       as every other tier in this module. This is what makes the stripped
       tier only ever a WEAK, corroborated signal - unique-within-this-
       comparison is the corroboration, never the bare shortened name alone.
    3. TRAILING-STREET-SUFFIX-STRIPPED (see _strip_trailing_street_suffix_
       word) - e.g. "35a Westminster Bridge" (a row) vs "35A Westminster
       Bridge Road" (a brochure's own fuller name) - confirmed against a
       real production case. Same weak-signal treatment as tier 2 and for
       the identical reason: dropping a generic trailing word can coincide
       two genuinely different streets that merely share everything before
       their own street-type word (e.g. "Kings Road" and "Kings Street" in
       the same portfolio brochure both drop to "kings") - only ever
       accepted when it is the SOLE candidate sharing that key. Tried
       independently of tier 2, on the ORIGINAL (non-address-suffix-
       stripped) keys - the two gaps are unrelated (one is a spreadsheet
       baking a full address onto a building name, the other is one side
       simply omitting a trailing street-type word) and neither building's
       real text needs both stripped at once for any case seen so far.
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
    if row_stripped_key:
        stripped = [
            i for i, c in enumerate(candidate_buildings)
            if normalize_key(_strip_building_address_suffix(c)) == row_stripped_key
        ]
        if len(stripped) == 1:
            return stripped

    row_street_key = _strip_trailing_street_suffix_word(row_key)
    street_suffix = [
        i for i, c in enumerate(candidate_buildings)
        if _strip_trailing_street_suffix_word(normalize_key(c)) == row_street_key
    ]
    if len(street_suffix) == 1:
        return street_suffix

    if candidate_addresses is None:
        return []

    addr_exact = _distinct_building_group(
        [i for i, a in enumerate(candidate_addresses) if normalize_key(a) == row_key],
        candidate_buildings,
    )
    if addr_exact:
        return addr_exact

    row_house_number = leading_house_number(row_building)
    if row_house_number is None:
        return []
    addr_street_suffix = _distinct_building_group(
        [
            i for i, a in enumerate(candidate_addresses)
            if _strip_trailing_street_suffix_word(normalize_key(a)) == row_street_key
            and leading_house_number(a) == row_house_number
        ],
        candidate_buildings,
    )
    return addr_street_suffix


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
    brochure_link_resolver.is_generic_link), a Canva public "view" link
    (see is_canva_view_link's own docstring - confirmed, not assumed, that
    a plain fetch can never retrieve real content from one), a Google
    Drive FOLDER share link (see _is_google_drive_folder_link - Google's
    own HTML folder-listing page, not a document, so a fetch attempt is
    structurally doomed the same way it is for a Canva link), and a URL
    whose own text already identifies it as a floor plan or a video rather
    than a document - never a fetch-then-guess; these are excluded by the
    URL alone, exactly like a human skimming a link list would.
    """
    if _is_blank(url):
        return False
    if urlparse(url).scheme not in ("http", "https"):
        return False
    if is_generic_link(url):
        return False
    if is_canva_view_link(url) and not _canva_renderer_configured():
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
# externally for over a decade). Deliberately does NOT attempt Drive's
# separate "Google can't scan this file for viruses" HTML interstitial/
# confirmation-token flow that appears for large files - extracting a
# token out of that HTML would be exactly the kind of brittle,
# undocumented scraping this task explicitly avoids. A file that triggers
# that interstitial instead of a direct download simply fails
# _looks_like_fetchable_document below (an HTML page, not a PDF) and is
# skipped exactly like any other unreadable source - never mis-extracted,
# just not one this pipeline can reach.
_GOOGLE_DRIVE_FILE_ID_RE = re.compile(r"^https?://drive\.google\.com/file/d/([\w-]+)", re.IGNORECASE)


def _google_drive_file_id(url: str):
    """The file ID from a "drive.google.com/file/d/{id}/..." share URL, or
    None if `url` isn't shaped like one - a pure URL-shape check, no fetch,
    mirroring _box_share_token's own convention."""
    match = _GOOGLE_DRIVE_FILE_ID_RE.match(url)
    return match.group(1) if match else None


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


def _fetch_canva_rendered_page(url: str):
    """
    PNG bytes for every page `url` (a public Canva "view" link) renders to,
    in page order, obtained from the separate Canva-rendering service (see
    canva_renderer/README.md) - never attempted at all unless
    CANVA_RENDERER_URL is configured (see _canva_renderer_configured's own
    docstring); classify_link_eligibility/_is_eligible_brochure_url/_is_
    eligible_floorplan_url already reject a Canva URL before it ever
    reaches this function in that case, exactly like before this feature
    existed. Chromium itself never runs in this app's own process/
    container - see that service's own README for why this separation
    exists (a stuck/misbehaving Canva page or a Chromium OOM must never be
    able to affect this app's own memory budget or uptime).

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

    Returns None (never raises) whenever:
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
      Canva URL, a private/login-required design, a page that never
      finished loading - recorded as STATUS_RENDER_FAILED with the
      renderer's own short, non-sensitive reason string (never a raw
      exception, stack trace, or URL with a query string).

    Otherwise returns a non-empty list[bytes], ALWAYS at least one page (the
    renderer itself never returns an empty "pages" list on a 200 - see its
    own app.py) - a genuinely multi-page Canva brochure's OTHER pages are
    now included too (see canva_renderer/README.md's "Multi-page capture"),
    up to whatever that service's own MAX_CANVA_PAGES allowed, further
    capped here at _CANVA_MAX_PAGES_ACCEPTED as defense-in-depth against a
    misbehaving/compromised renderer response (that service is a separately
    deployed, separately versioned service - this app never assumes its own
    cap is what actually ran on the other side). This list feeds into
    extract.images_from_png_pages/render_and_extract exactly like a multi-
    page PDF's own per-page images already do (see _fetch_pdf_bytes' own
    Canva branch below) - no separate Canva extraction system, no change to
    matching/enrichment rules.
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
                f"[brochure_enrichment] Canva renderer returned a transient HTTP {response.status_code} for "
                f"{url!r} on attempt {attempt}/{_CANVA_RENDERER_MAX_ATTEMPTS} - retrying "
                f"after {_CANVA_RENDERER_RETRY_BACKOFF_SECONDS}s.",
                file=sys.stderr,
            )
            time.sleep(_CANVA_RENDERER_RETRY_BACKOFF_SECONDS)

    if response is None:
        print(
            f"[brochure_enrichment] Canva renderer unreachable for {url!r} ({connect_exception!r}) — "
            "skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_FETCH_FAILED, f"Canva renderer unreachable ({connect_exception!r})")
        return None

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
            f"[brochure_enrichment] Canva renderer rejected the request for {url!r} with "
            f"HTTP {response.status_code} (authentication failed - check the main app's service account has "
            f"the Cloud Run Invoker role on the renderer, and that CANVA_RENDERER_URL exactly matches its "
            f"own URL) — skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_FETCH_FAILED, "Canva renderer authentication failed")
        return None

    content_type = response.headers.get("content-type", "")
    if response.status_code == 200 and "application/json" not in content_type and "image/" in content_type:
        # Distinguished from every other failure shape below - a 200 with
        # an image/* content-type is exactly what the OLD, single-page-only
        # renderer returned (see this repo's history before multi-page
        # capture: canva_renderer/app.py used to respond with a raw PNG
        # body, not the {"pages": [...]} JSON this app now expects). This
        # is the one failure shape that means "the two services are on
        # mismatched versions", not "Canva/the render itself failed" - a
        # genuine render failure never returns 200 with image bytes at all.
        # Loud and specific on purpose: this is the single most likely
        # cause of "Canva enrichment silently does nothing in production
        # despite the multi-page code being merged" - the canva-renderer
        # Cloud Run service simply hasn't been redeployed with it yet.
        print(
            f"[brochure_enrichment] Canva renderer for {url!r} returned an OLD-FORMAT single-image "
            f"response (content-type {content_type!r}) instead of the expected JSON {{'pages': [...]}} "
            "body — the canva-renderer Cloud Run service needs redeploying with the current multi-page "
            "code (see canva_renderer/README.md) — skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_RENDER_FAILED, "Canva renderer is running an outdated single-page image response")
        return None

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
        print(f"[brochure_enrichment] Canva renderer failed for {url!r}: {reason}", file=sys.stderr)
        _record_status(STATUS_RENDER_FAILED, f"Canva render failed: {reason}")
        return None

    try:
        payload = response.json()
        raw_pages = payload["pages"]
        pages = [base64.b64decode(p) for p in raw_pages]
        if not pages:
            raise ValueError("empty pages list")
    except Exception as e:
        print(
            f"[brochure_enrichment] Canva renderer returned a malformed response for {url!r} ({e!r}) — "
            "skipping enrichment.",
            file=sys.stderr,
        )
        _record_status(STATUS_RENDER_FAILED, f"malformed renderer response ({e!r})")
        return None

    detected_total = payload.get("page_count_detected")
    if len(pages) > _CANVA_MAX_PAGES_ACCEPTED:
        print(
            f"[brochure_enrichment] Canva renderer returned {len(pages)} pages for {url!r}, "
            f"truncating to this app's own cap of {_CANVA_MAX_PAGES_ACCEPTED}.",
            file=sys.stderr,
        )
        pages = pages[:_CANVA_MAX_PAGES_ACCEPTED]

    # The ONE clear, positive confirmation the whole authenticated round
    # trip actually worked - main app -> ID token -> Cloud Run IAM ->
    # renderer -> Chromium -> pages back. Every failure mode above already
    # prints its own distinct message; this is deliberately the only
    # SUCCESS line for Canva specifically, so grepping Cloud Run logs for
    # "Canva render succeeded" is a single, unambiguous way to confirm
    # rendering itself worked for a given URL, separate from whether the
    # subsequent Gemini extraction (see _extract_brochure_units) then
    # found anything useful in it.
    detected_str = f"{detected_total} detected" if detected_total else "total unknown"
    print(
        f"[brochure_enrichment] Canva render succeeded for {url!r}: {len(pages)} page(s) captured "
        f"({detected_str}) — handing off to the existing extraction pipeline.",
        file=sys.stderr,
    )
    return pages


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

    A Dropbox share link (see _is_dropbox_share_url) or a Google Drive
    "file/d/{id}" share link (see _google_drive_file_id) is turned into
    that host's own direct-download URL before fetching, same idea as the
    Box branch above but via a simple, documented URL transform rather
    than a second metadata fetch - neither host requires one. Anything
    else keeps the exact same behavior as before: a direct document
    extension is fetched as-is, otherwise resolve_brochure_link's one-hop
    landing-page scan is tried first.

    A Canva "view" link (see is_canva_view_link) is read via the separate
    Canva-rendering service instead (see _fetch_canva_rendered_page), but
    ONLY when CANVA_RENDERER_URL is actually configured (see
    _canva_renderer_configured) - classify_link_eligibility/_is_eligible_
    brochure_url/_is_eligible_floorplan_url already reject a Canva URL
    before it gets here at all in that case, so this check is normally
    redundant, but this function's own direct callers (e.g. a diagnostic
    script, or a test exercising this layer directly) don't necessarily
    go through that eligibility gate first - falling through to the exact
    same generic fetch path as any other URL when unconfigured, rather
    than attempting a renderer call this deployment was never told about,
    keeps this function's own behavior correct independent of whether a
    caller already checked eligibility.

    The Canva branch is the ONE case this function returns list[bytes]
    (one PNG per page, see _fetch_canva_rendered_page) rather than a single
    bytes object - every other branch/URL shape is unaffected. Both callers
    (_extract_brochure_units/_extract_floorplan_units) branch on this via
    _images_from_fetched_document, so neither needs its own isinstance
    check duplicated.
    """
    if _box_share_token(url):
        return _fetch_box_shared_pdf(
            url, reject_floorplan_filename=reject_floorplan_filename, accept_image_formats=accept_image_formats,
        )

    if is_canva_view_link(url) and _canva_renderer_configured():
        return _fetch_canva_rendered_page(url)

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
        response.headers.get("content-type"), response.content, accept_image_formats=accept_image_formats,
    ):
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

    provider is read the exact same way - extract.py's own PROMPT already
    asks for it at the document level (raw.get("provider") - see
    extract.extract()'s own identical read for the uploaded-PDF path), but
    _extract_brochure_units previously never carried it past this point at
    all, since ordinary brochure ENRICHMENT (an existing row's own
    brochure_link, read to fill in blanks) never needed a document-level
    provider - the row already has one from its real source. extract_
    rows_from_link (a "paste a document link directly" upload, with no
    other source to have a provider from) does need it.
    """

    property_features = None
    contacts = None
    building_features = None
    provider = None


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
    provider = raw.get("provider")
    units.provider = provider if isinstance(provider, str) else None
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
    if is_canva_view_link(url):
        # Canva-specific diagnostic only (never printed for the PDF/Box/
        # Dropbox/GDrive paths, which already have years of production
        # history without this) - shows exactly what Gemini's raw JSON
        # contained for this brochure, one level BEFORE matching/
        # _apply_units_to_row ever runs, so "Gemini extracted nothing
        # useful" and "matching/apply rejected what Gemini found" are never
        # ambiguous with each other in the logs. Field NAMES only, never
        # the actual extracted text (which could be long/PII-bearing).
        print(
            f"[brochure_enrichment] Canva extraction for {url!r}: {len(units)} unit(s), "
            f"property_features={'present' if units.property_features else 'absent'}, "
            f"contacts={'present' if units.contacts else 'absent'}, "
            f"building_features={len(units.building_features)}.",
            file=sys.stderr,
        )
    return units


def extract_rows_from_link(url: str, source_label: str = None) -> list[ListingRow]:
    """
    Every unit _extract_brochure_units finds at `url`, converted directly
    into ListingRow objects - the "paste a document link" upload path's
    own conversion step (see app.py), reusing the exact same real-
    document-reading pipeline brochure ENRICHMENT already uses for an
    existing row's own brochure_link, rather than a second,
    independently-drifting extraction implementation. Works for every link
    shape _extract_brochure_units already handles (a real PDF, Box/
    Dropbox, a Canva "view" link) - deliberately NOT the Pitch/GPE viewer
    shape, which needs real headless-browser rendering support that
    doesn't exist yet (see brochure_link_resolver/canva_renderer - this
    function only ever sees what _extract_brochure_units can already
    fetch).

    Field conversion mirrors extract.py's own extract() (the uploaded-PDF
    path) exactly: ExtractedFields(**brochure, **unit).model_dump() then
    compute_rent(...) - the SAME schema-driven spread, so every field a
    unit's own dict actually has (floor_unit/size_sqft/special_features/
    state_of_space/desks_min/desks_max/rent_pcm/rent_psf/etc.) carries
    straight through with no separately-maintained field list here that
    could drift out of sync with ExtractedFields' own declared fields.

    brochure_link is set to `url` itself, verbatim, for EVERY row this
    returns - deliberately never run through finalize_brochure_link's
    generic-link/landing-page-resolution rules the way a per-unit link
    Gemini merely read out of unrelated text elsewhere in this module is:
    the user explicitly pasted this exact URL as THIS document's own
    link, so there is nothing to second-guess or resolve.

    provider/internal_ref come from whatever this document's own
    extraction found at the document level (see _BrochureUnits.provider) -
    left blank for the reviewer to fill in at review time when the
    document doesn't state one, exactly like extract.py's own PDF-upload
    path already leaves a landlord-direct brochure's blank provider
    alone rather than guessing one.

    A unit with no building at all inherits the previous unit's own
    building (same rule extract.py's own extract() already applies for a
    document covering several buildings/units); a unit with NEITHER its
    own building NOR a prior one to inherit (only possible for the very
    first unit) is skipped with a stderr warning - the one case with
    nothing safe to attach it to at all. Every other unit always becomes
    its own row - never silently dropped for any other reason.

    Returns [] (never raises) when _extract_brochure_units found nothing
    at all (a fetch/parse failure, or a document with no real units) -
    app.py is responsible for telling the user nothing was extracted.
    """
    units = _extract_brochure_units(url)
    if not units:
        return []

    label = source_label or url
    brochure = {
        "internal_ref": units.provider,
        "provider": units.provider,
        "contacts": units.contacts,
    }

    rows = []
    last_building = None
    for i, unit in enumerate(units):
        if not isinstance(unit, dict):
            continue
        unit = dict(unit)
        if not unit.get("building"):
            if not last_building:
                print(
                    f"Warning: {label} unit {i} has no building and no prior "
                    "unit to inherit one from — skipping this unit.",
                    file=sys.stderr,
                )
                continue
            unit["building"] = last_building
        last_building = unit["building"]

        unit["brochure_link"] = url
        unit["floorplan_link"] = finalize_floorplan_link(unit.get("floorplan_link"))

        fields = ExtractedFields(**brochure, **unit).model_dump()
        fields = compute_rent(fields)
        rows.append(ListingRow(**fields, lat=None, lng=None, source_file=label))
    return rows


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
    No deduplication between tiers (a unit's own text may legitimately
    restate a building-wide detail Gemini already saw) - deliberately not
    attempted here; a bad dedup rule risks stripping something real, a
    worse failure mode than an occasional repeated phrase.

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
        # because that cell wasn't genuinely blank. No deduplication against
        # overlapping phrasing between tiers (e.g. a unit's own text already
        # echoing a building-wide amenity) - deliberately out of scope, see
        # this change's own commit message for why a dedup rule is its own,
        # separate risk (wrongly stripping something real) not worth taking
        # on here.
        row_features = row.special_features if isinstance(row.special_features, str) and not _is_blank(row.special_features) else None
        unit_features = _coerced_unit_value("special_features", unit.get("special_features")) if unit else None
        building_features = _match_building_feature(row, units)
        if not (isinstance(building_features, str) and not _is_blank(building_features)):
            building_features = None
        property_features = getattr(units, "property_features", None)
        if not (isinstance(property_features, str) and not _is_blank(property_features)):
            property_features = None

        combined = "; ".join(seg for seg in (row_features, unit_features, building_features, property_features) if seg)
        if combined and combined != row.special_features:
            updates["special_features"] = combined

        for field in BUILDING_LEVEL_FIELDS:
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
    (see is_generic_link), a Canva public "view" link (see is_canva_view_
    link's own docstring - a floor plan is never retrievable from one
    either, for the same reason a brochure isn't), a Google Drive FOLDER
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
    if is_canva_view_link(url) and not _canva_renderer_configured():
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
    # See enrich_rows_grouped's own document_issues docstring - additive
    # diagnostics alongside processed_urls, never replacing it.
    document_issues = _ineligible_link_issues(
        rows, needs_floorplan_enrichment, "floorplan_link", reject_floorplan_shaped=False,
    )
    stats = {
        "unique_floorplans_considered": len(unique_urls), "floorplans_read_ok": 0,
        "floorplans_unavailable": 0, "processed_urls": processed_urls, "document_issues": document_issues,
    }
    if not urls_to_fetch:
        return current, log, stats

    indices_by_url = {}
    for i, row in enumerate(rows):
        if needs_floorplan_enrichment(row) and _is_eligible_floorplan_url(row.floorplan_link):
            indices_by_url.setdefault(row.floorplan_link, []).append(i)

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
                if link_broken_update is not None:
                    new_row = new_row.model_copy(update={"brochure_link_broken": link_broken_update})
                current[i] = new_row
                if fields:
                    if is_canva_view_link(url):
                        # Distinct, greppable confirmation that a Canva-
                        # sourced render made it all the way through the
                        # existing extraction pipeline AND actually
                        # produced usable field values on a real row - the
                        # final link in the chain _fetch_canva_rendered_
                        # page's own "Canva render succeeded" log confirms
                        # only the rendering half of (see this module's own
                        # "prove Canva end-to-end" verification).
                        print(
                            f"[brochure_enrichment] Canva enrichment applied {fields} to "
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
                    if is_canva_view_link(url):
                        # Canva-specific diagnostic only - the document read
                        # fine (see the Canva extraction log above) but this
                        # ROW still has blank fields afterward; names the
                        # exact fields and whether an ambiguous/rent-conflict
                        # match was the reason, so "Gemini found nothing for
                        # this row" and "Gemini found something but matching/
                        # the rent guard rejected it" are never conflated.
                        still_blank = [f for f in ENRICHABLE_FIELDS if _is_blank(getattr(new_row, f))]
                        reason = "ambiguous/rent-conflicting match" if ambiguous else "no confident match for this row"
                        print(
                            f"[brochure_enrichment] Canva extraction read fine for {new_row.building!r} "
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
        "document_issues": document_issues + floorplan_stats["document_issues"],
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
        document_issues=stats["document_issues"],
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
