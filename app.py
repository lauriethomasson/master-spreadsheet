import hashlib
import json
import re
import tempfile
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import fitz  # PyMuPDF
import streamlit as st
from openpyxl import load_workbook

import brochure_enrichment
import brochure_link_resolver
import extract
import extract_email
import extract_spreadsheet
import extract_spreadsheet_gemini
import page_flow
import page_setup
from brochure_link_resolver import looks_like_url
from display_utils import LONDON_TZ
from gemini_client import QuotaExceededError
import geocode
from geocode import geocode_rows
from master_merge import RISKY_TEXT_FIELDS, canonicalize_providers, draft_merge_text, normalize_key, richest_listing_index
from schema import ListingRow
from storage.file_store import (
    dataframe_to_listing_rows,
    find_previous_upload_by_hash,
    get_saved_critical_field_rescue,
    get_staging_enrichment_summary,
    get_staging_fully_occupied_buildings,
    load_staging_as_dataframe,
    save_original_pdf,
    save_staging_file,
)

SPREADSHEET_SUFFIXES = (".xlsx", ".csv")

# Column-header mapping (extract_spreadsheet.py) has no Gemini call and is
# fully deterministic - the only way its cached result could ever go stale
# is a change to that module's own mapping/guessing logic itself (suggest_
# mapping, guess_provider_name, FIELD_SYNONYMS, etc.), never anything the
# PDF/email path's own version counter (since removed - see
# _PDF_EMAIL_LOGIC_FINGERPRINT below) was ever meant to track. Confirmed to
# have actually gone stale this way: a real fix to that logic landed without
# that counter being bumped (it had no reason to know spreadsheet logic even
# changed), so a byte-identical re-upload of an already-staged spreadsheet
# kept silently reusing its pre-fix cached rows - dedup working exactly as
# designed, just against the wrong invalidation signal for this source
# type. Hashing the source of both spreadsheet-path modules directly instead
# makes invalidation automatic and self-maintaining - no version number to
# remember, ever, for this source type specifically. extract_spreadsheet_
# gemini.py is included here too even though IT does call Gemini (and so
# isn't "fully deterministic" the way the comment above once meant) -
# folding it into the same fingerprint is still correct: a prompt/logic
# change there must invalidate a cached result exactly like a mapping-logic
# change does, for the same reason, even though a fresh (non-cached) call to
# that module was never guaranteed byte-identical to begin with.
# brochure_enrichment.py is included for the exact same reason: it now runs
# automatically, unconditionally, right after a fresh spreadsheet
# extraction (see _run_automatic_brochure_enrichment/the spreadsheet branch
# below), so a change to its matching/field rules must invalidate an
# already-staged result too, not silently keep serving rows enriched (or
# not enriched) under the OLD logic.
#
# geocode.py is included for the same reason again, confirmed via a real
# gap: geocode_rows() runs unconditionally right after a fresh extraction
# for BOTH source types (see the spreadsheet and PDF/email branches below),
# but wasn't part of either invalidation mechanism at all - neither this
# fingerprint nor the PDF/email path's own version counter (since removed -
# see _PDF_EMAIL_LOGIC_FINGERPRINT below). A real geocoding-validation fix
# (rejecting a Places candidate that contradicts the source's own postcode
# evidence - see geocode.py's own module docstring) landed without either
# being touched, so re-uploading an already-staged file (e.g. the real beem
# Live Flex Availability.xlsx) kept silently reusing its pre-fix cached
# rows/coordinates - dedup working exactly as designed, just blind to this
# one dependency. Folding geocode.py's own source in here (see also its
# addition to the PDF/email versioned_content below) closes that gap the
# same automatic, self-maintaining way as the other three modules.
#
# brochure_link_resolver.py is included for the same reason once more, and
# was found to have the exact same gap: extract_spreadsheet_gemini.py (and,
# on the PDF/email side below, extract.py/extract_email.py) all call
# finalize_brochure_link, but that function lives in its own separate
# module, whose source was never folded into either fingerprint at all - a
# real fix to finalize_brochure_link itself (the rule-3 PDF-fallback
# removal) landed without either fingerprint changing, so a byte-identical
# re-upload of an already-staged file kept silently reusing its pre-fix
# cached brochure_link values, exactly the same failure shape as the
# geocode.py gap above.
_SPREADSHEET_LOGIC_FINGERPRINT = hashlib.sha256(
    Path(extract_spreadsheet.__file__).read_bytes()
    + Path(extract_spreadsheet_gemini.__file__).read_bytes()
    + Path(brochure_enrichment.__file__).read_bytes()
    + Path(geocode.__file__).read_bytes()
    + Path(brochure_link_resolver.__file__).read_bytes()
).hexdigest()

# extract.py's/extract_email.py's own source, folded into
# _pdf_or_email_content_hash for a PDF/email upload the exact same
# automatic, self-maintaining way _SPREADSHEET_LOGIC_FINGERPRINT above
# already does for extract_spreadsheet.py/extract_spreadsheet_gemini.py -
# replacing the EXTRACTION_VERSION human-maintained counter this used to
# rely on instead. Confirmed to have already gone stale exactly the same
# way the spreadsheet-mapping gap above did: EXTRACTION_VERSION stayed "3"
# across two real extract.py fixes since it was introduced (the single-
# unit-page PDF link attachment fix, and the bulk-upload-fallback-
# suppression fix), so a PDF re-uploaded after either fix kept silently
# reusing its pre-fix cached extraction result - the fix was sitting in the
# repo but never actually took effect for anything already uploaded once.
#
# brochure_link_resolver.py's own source is folded in here too, same as
# _SPREADSHEET_LOGIC_FINGERPRINT above and for the same confirmed gap: both
# extract.py and extract_email.py call finalize_brochure_link (unlike
# geocode.py/brochure_enrichment.py below, which only apply to some PDF/
# email paths, this one applies to both unconditionally, so it belongs in
# the fingerprint itself rather than in versioned_content).
_PDF_EMAIL_LOGIC_FINGERPRINT = hashlib.sha256(
    Path(extract.__file__).read_bytes()
    + Path(extract_email.__file__).read_bytes()
    + Path(brochure_link_resolver.__file__).read_bytes()
).hexdigest()

# The neutral, un-decided option in an ambiguous-sheet decision radio (see
# _render_ambiguous_sheet_decision) - deliberately never pre-selecting
# either real choice, so _pending_sheet_decisions can tell "hasn't been
# looked at yet" apart from a real decision.
SHEET_DECISION_PLACEHOLDER = "— Please choose —"
SHEET_DECISION_SKIP = "Skip this sheet"
SHEET_DECISION_INCLUDE = "Include this sheet"

HIDDEN_SHEET_EXPLAINER = (
    "Excel workbooks can contain hidden sheets. Hidden sheets may not appear as tabs in Excel, "
    "but they are still part of the workbook and can contain data."
)


def fill_missing_provider(rows: list[ListingRow], filename: str, apply_filename_guess: bool) -> None:
    """
    Fill provider/internal_ref from a filename-derived guess (see
    extract_spreadsheet.guess_provider_name) without overwriting extracted
    values - but only when apply_filename_guess is True, which the caller
    sets for spreadsheet-sourced rows only, never PDF/email.

    A spreadsheet has no equivalent judgment call to make: if no column
    states a provider, nothing else in the data ever will, so the filename
    is the best signal available. A PDF/email brochure is different -
    Gemini's own extraction already decided whether a provider genuinely
    exists, and a blank provider there is frequently a deliberate,
    meaningful answer, not a missed extraction (see schema.ExtractedFields'
    provider/internal_ref comments - a landlord-direct brochure has no
    presenting agent at all, e.g. a real "40 New Bond Street" brochure with
    no contacts). Applying a filename-derived guess there would fabricate
    an agent for a listing that genuinely has none - misrepresenting a
    landlord-direct listing as agent-represented. So for PDF/email,
    apply_filename_guess is always False and this function leaves
    provider/internal_ref exactly as extraction produced them, same as
    before guess_provider_name existed at all.
    """
    if not apply_filename_guess:
        return

    fallback_provider = extract_spreadsheet.guess_provider_name(filename)

    for row in rows:
        if not row.provider:
            row.provider = fallback_provider

        if not row.internal_ref:
            row.internal_ref = row.provider or fallback_provider


def fill_missing_submarket_from_structural_header(rows: list[ListingRow], headers: list, filename: str) -> None:
    """
    Fill submarket from a recognized provider's own structural fallback
    (see extract_spreadsheet.structural_submarket_fallback) without
    overwriting a genuinely-extracted value - a no-op for any format that
    fallback doesn't apply to. Runs before geocode_rows' own geocoded-
    neighbourhood fallback (see geocode.py) - this constant, file-level
    value is 100% reliable when it applies at all, whereas that one is only
    ever a secondary, best-effort attempt for whatever this doesn't cover
    (confirmed necessary: Google has no neighbourhood-polygon data at all
    for the real Clerkenwell & Farringdon addresses this was built for).
    """
    submarket = extract_spreadsheet.structural_submarket_fallback(headers, filename)
    if not submarket:
        return

    for row in rows:
        if not row.submarket:
            row.submarket = submarket


# A house/building number is a strong, simple signal that `building` holds
# a real street address rather than just a proper name - confirmed against
# the real Kitt's Availability file (35 of 42 real building values contain
# a digit - "28 Bruton Street", "33 Cavendish Square", compound ones like
# "Bridge House, 22 Newman Street" - and all 7 without one are genuine
# name-only buildings - "Albion Mills", "Flat Iron", "Orion House" - not a
# single false positive/negative found) and both real UNION "by-area"
# files (9/11 and 14/14 respectively, same clean split).
_ADDRESS_LIKE_RE = re.compile(r"\d")


def fill_missing_address_from_building(rows: list[ListingRow], apply_building_fallback: bool) -> None:
    """
    Copies building's value into address_1 when address_1 has no value at
    all AND building looks address-like (see _ADDRESS_LIKE_RE) - never
    overwrites a real, already-populated address_1, from a mapped column
    or from geocode_rows' own (more accurate, API-verified) address lookup;
    this is only ever a last-resort fallback for whatever's still missing
    after that, which is exactly why the caller must run this AFTER
    geocode_rows, not before - filling address_1 first would make
    geocode_row see it as already present and skip its own lookup.

    apply_building_fallback is True for HEADER-MAPPED spreadsheet rows only,
    same scoping as fill_missing_provider's apply_filename_guess and for the
    same reason: a blank address_1 on a PDF/email row is frequently
    Gemini's own deliberate, meaningful answer, not a missed extraction
    (see schema.ExtractedFields' own address_1 comment - "not every source,
    e.g. email listings, states a street address" - never fabricate).
    building holding an address-like string doesn't change that judgment
    call for those sources - and a spreadsheet row extracted via Gemini
    (see extract_spreadsheet_gemini.py) has made that exact same judgment
    call already, so it's scoped out here too. A HEADER-MAPPED spreadsheet
    row has no equivalent judgment call: if no column states an address and
    geocoding couldn't resolve one either, building's own value is the best
    signal left.
    """
    if not apply_building_fallback:
        return

    for row in rows:
        if row.address_1:
            continue
        if row.building and _ADDRESS_LIKE_RE.search(row.building):
            row.address_1 = row.building


# At least this fraction of the SMALLER of two header-mapped sheets' own
# rows must share an identity with the OTHER sheet before _rows_dropped_
# as_duplicate_sheet_extractions treats them as duplicative at all - see
# that function's own docstring for why this is deliberately a large-
# majority-overlap signal, not a per-row one.
_SHEET_OVERLAP_DUPLICATE_THRESHOLD = 0.5


def _row_identity_key(row: ListingRow):
    """(building, floor_unit) normalize_key-tolerant identity - the signal
    _rows_dropped_as_duplicate_sheet_extractions uses to compare two
    header-mapped sheets in the same uploaded file for real content
    overlap. None (never groupable) for a row with no building at all -
    normalize_key("") is already falsy, but spelled out here so two blank-
    building rows are never treated as sharing an identity purely from
    both being blank."""
    building_key = normalize_key(row.building)
    if not building_key:
        return None
    return building_key, normalize_key(row.floor_unit)


def _rows_dropped_as_duplicate_sheet_extractions(header_mapped_sheets: list) -> set:
    """
    id()s of rows to drop as redundant re-extractions of the SAME real
    listing from ANOTHER header-mapped sheet in this same uploaded file.

    Confirmed real case: a real Kitt's "Kitts_Availability_External.xlsx"
    has two sheets - "Source_Availability" (950 rows, pulling live from a
    Google Sheet via IMPORTRANGE formulas, an internal working tracker)
    and "Live Availability" (1000 rows, clean static values, the polished
    published-for-external-use view) - that independently list the exact
    same real units. Every sheet in a multi-sheet workbook is already
    processed unconditionally and independently (see the Extract loop
    above - deliberately so, a real Copthall Estates file genuinely has 4
    different-area sheets that must all merge), so both Kitt's sheets got
    extracted as if they were separate real data, producing two rows per
    shared unit. For most of that file's own real units this went
    completely unnoticed - both sheets' own text happened to be byte-
    identical, so the two rows silently matched with no visible conflict
    at Review & Master's own intra-batch duplicate-detection stage (see
    master_merge._group_unmatched_duplicates) - but "8 Laurence Pountney
    Hill" had a real difference (one sheet's own "Space video" cell has a
    genuine embedded YouTube hyperlink, the other doesn't), which
    correctly, but unnecessarily, tripped that same stage's "possible
    duplicate listings, can't safely tell" human-review card - unnecessary
    because these were never two independently-entered listings needing a
    human's judgment call at all, just the SAME row counted twice by this
    app's own sheet processing.

    Deliberately a SHEET-vs-SHEET overlap signal, not a per-row one - a
    single coincidentally-shared (building, floor_unit) pair between two
    otherwise-unrelated sheets is nowhere near enough evidence the SHEETS
    themselves are duplicative (that's exactly what master_merge's own
    row-level duplicate detection already exists to judge safely, case by
    case - see _SHEET_OVERLAP_DUPLICATE_THRESHOLD), so this only ever acts
    when a large majority of one sheet's own rows are also found on
    another - strong, structural evidence the two sheets describe the same
    underlying data, not just two listings that happen to share an
    address. A real Copthall Estates file (4 genuinely different per-area
    sheets: City, Mid Town, Westend Soho, Blackfriars) has NO cross-sheet
    overlap at all under this signal, since each sheet covers different
    buildings - a pure no-op for it, same as for any other multi-sheet
    file whose sheets genuinely aren't duplicates of each other.
    Deliberately NOT a sheet-NAME heuristic (e.g. preferring a sheet named
    "Live"/"External" over "Source") - naming conventions are provider-
    specific and won't generalize; actual row overlap does.

    Only ever compares header-mapped sheets against each other, never
    against a Gemini-fallback sheet - a repeating-block layout (e.g. the
    real Copthall "Portfolio" rollup tab) is a structurally different kind
    of duplicate risk already handled by is_non_authoritative_rollup_
    sheet/classify_sheet_for_extraction upstream of this (see the Extract
    loop's own unresolved branch).

    For each pair of sheets whose overlap clears _SHEET_OVERLAP_DUPLICATE_
    THRESHOLD, only the rows that ACTUALLY share an identity with the
    other sheet are collapsed to one - the base row is whichever of the
    two is richer (master_merge.richest_listing_index, the same tie-break
    the Review & Master page's own "Same listing — merge" choice already
    trusts, reused here rather than a second, separately-invented one),
    but every RISKY_TEXT_FIELDS field (special_features, contacts) that
    genuinely differs between the two is combined via master_merge.
    draft_merge_text FIRST and written onto that base row, exactly like a
    reviewer's own "merge" choice would produce - never silently dropped
    by richest_listing_index alone, which only ever weighs _LISTING_
    DIFFERENCE_FIELDS (size/desks/rent) and has no notion of free text at
    all. This is what actually closes the confirmed 8 Laurence Pountney
    Hill case: richness alone ties (identical size/rent on both sheets),
    so without this, which sheet's own special_features text happened to
    survive would be arbitrary - draft_merge_text's own "uncleaned
    concatenation, never assumed final" caveat (see its own docstring) is
    a non-issue here specifically, since two sheets independently re-
    extracting the SAME source document are never a genuine two-sided
    disagreement the way two DIFFERENT providers' own listings could be -
    one side simply has strictly more text than the other (e.g. an
    embedded hyperlink one extraction pass happened to pick up), so
    joining them is lossless, not a values judgment a human still needs to
    make. A row with no counterpart on the other sheet is real, non-
    redundant data either way and is never touched, even between two
    sheets confidently judged duplicative overall.
    """
    if len(header_mapped_sheets) < 2:
        return set()

    dropped_ids = set()
    for i in range(len(header_mapped_sheets)):
        for j in range(i + 1, len(header_mapped_sheets)):
            sheet_a, sheet_b = header_mapped_sheets[i], header_mapped_sheets[j]
            if not sheet_a or not sheet_b:
                continue

            keys_a = {_row_identity_key(r) for r in sheet_a} - {None}
            keys_b = {_row_identity_key(r) for r in sheet_b} - {None}
            shared_keys = keys_a & keys_b
            if not shared_keys:
                continue

            overlap_ratio = len(shared_keys) / min(len(sheet_a), len(sheet_b))
            if overlap_ratio < _SHEET_OVERLAP_DUPLICATE_THRESHOLD:
                continue

            rows_by_key_a, rows_by_key_b = {}, {}
            for r in sheet_a:
                rows_by_key_a.setdefault(_row_identity_key(r), []).append(r)
            for r in sheet_b:
                rows_by_key_b.setdefault(_row_identity_key(r), []).append(r)

            for key in shared_keys:
                candidates = rows_by_key_a[key] + rows_by_key_b[key]
                if len(candidates) < 2:
                    continue
                dicts = [r.model_dump() for r in candidates]
                keep_row = candidates[richest_listing_index(dicts)]
                for field in RISKY_TEXT_FIELDS:
                    merged = draft_merge_text([d.get(field) for d in dicts])
                    if merged:
                        setattr(keep_row, field, merged)
                dropped_ids.update(id(r) for r in candidates if r is not keep_row)

    return dropped_ids


def _run_automatic_brochure_enrichment(
    rows: list[ListingRow], staging_path: str, already_processed: dict = None,
    floorplan_already_processed: dict = None, special_features_matched: dict = None, row_count: int = None,
) -> list[ListingRow]:
    """
    Runs immediately after a FRESH spreadsheet OR email upload's base rows
    are already staged at staging_path (see save_staging_file, called by
    the caller strictly BEFORE this) - automatic, not a separate action the
    user has to remember to trigger. That ordering is what keeps the base
    extraction safe even if this crashes, times out, or is interrupted (see
    brochure_enrichment.py's own module docstring): the rows already exist
    on disk, byte-identical to what was just extracted, before this ever
    downloads or sends a single brochure to Gemini.

    already_processed/floorplan_already_processed ({url: "ok" |
    "unavailable"}), when given, are a PRIOR upload's own persisted
    brochure/floorplan enrichment progress for this exact content (see the
    caller's own "reused but incomplete" branch below) - already-"ok"
    brochures/floorplans are never re-fetched/re-sent to Gemini just
    because a re-upload of the identical file happened to land on a NEW
    staging entry rather than the original one. special_features_matched
    ({str(row_index): True}) is the same PRIOR upload's own persisted
    special-features-resume sidecar (see brochure_enrichment.
    enrich_rows_grouped's own param docstring) - all three default to {}
    (nothing to resume) for the ordinary fresh-upload case, identical to
    every prior behavior before any of them existed.

    row_count, when given (the caller's freshly-extracted row count - never
    passed for the "reused but incomplete" resume case below, since that
    path never announced a row count of its own before this parameter
    existed either), is folded into the caption this shows so the caller no
    longer needs its OWN separate "N row(s) saved" caption alongside this
    one - see this function's own caption logic below for the combined
    wording, including the case where there's nothing left to check.

    Has no UI at all beyond that combined "row(s) saved" caption (none, if
    row_count isn't given) when NEITHER brochure NOR floorplan enrichment
    has anything eligible to do (see brochure_enrichment.
    eligible_rows_and_brochures/eligible_rows_and_floorplans) - the common
    case for a provider whose spreadsheet already states everything
    ENRICHABLE_FIELDS covers, and the ONLY case that should ever feel
    instant; the point of showing real progress below is precisely for the
    case where it isn't. Confirmed real gap this guards against: a
    spreadsheet with zero brochure-eligible rows but at least one genuinely
    eligible floorplan_link used to skip run_brochure_enrichment (and
    therefore the floorplan pass) entirely, silently losing floorplan-
    sourced enrichment that had nothing to do with brochures at all.

    Persists partial progress incrementally and the final result (see
    brochure_enrichment.run_brochure_enrichment, the shared Streamlit-aware
    orchestration this delegates to - also used by pages/2_Review_and_
    Master.py's own "Continue enrichment" action to resume an interrupted
    run) - so an interruption partway through loses at most a handful of
    brochures'/floorplans' worth of work, never the base extraction, which
    was already durably staged before this function was even called.

    Returns the (possibly enriched) rows so the caller can reassign its own
    `rows` variable to the final state - enrichment never changes row
    COUNT, only field values, but keeping the caller's own variable
    accurate is one less place a future change could accidentally read the
    stale pre-enrichment list.
    """
    eligible, unique_urls = brochure_enrichment.eligible_rows_and_brochures(rows)
    eligible_floorplans, unique_floorplan_urls = brochure_enrichment.eligible_rows_and_floorplans(rows)
    if not unique_urls and not unique_floorplan_urls:
        if row_count is not None:
            st.caption(f"{row_count} row{'s' if row_count != 1 else ''} saved.")
        return rows

    brochure_count = f"{len(unique_urls)} brochure{'s' if len(unique_urls) != 1 else ''}"
    if row_count is not None:
        st.caption(
            f"{row_count} row{'s' if row_count != 1 else ''} saved — now checking "
            f"{brochure_count} for extra details, your data's safe either way."
        )
    else:
        st.caption(
            f"Now checking {brochure_count} to fill in missing "
            "details — your data's already saved either way."
        )
    return brochure_enrichment.run_brochure_enrichment(
        rows, staging_path, already_processed=already_processed or {},
        floorplan_already_processed=floorplan_already_processed or {},
        special_features_matched=special_features_matched or {},
    )


def _pre_enrichment_geocode_snapshot(rows: list[ListingRow]) -> list:
    """
    Captures, by position (never by object identity - brochure enrichment
    may replace row objects via model_copy rather than mutate them in
    place, see brochure_enrichment.py's own established pattern), exactly
    the three facts _reattempt_geocoding_for_newly_addressed_rows below
    needs to decide whether a row is a genuine candidate for its fix: was
    this row's existing location already flagged uncertain, and did it
    already have BOTH address_1 and postcode before enrichment had a
    chance to backfill either. Called by the caller strictly BEFORE
    brochure enrichment runs - _run_automatic_brochure_enrichment's own
    return value is what "after" gets compared against.
    """
    return [(row.geocode_unverified, bool(row.address_1), bool(row.postcode)) for row in rows]


def _reattempt_geocoding_for_newly_addressed_rows(rows: list[ListingRow], pre_enrichment_state: list) -> None:
    """
    Real, confirmed gap this closes: geocode_rows() runs unconditionally
    right after extraction, before brochure enrichment ever gets a chance
    to backfill address_1/postcode from the brochure itself - so a row
    whose source spreadsheet stated only a bare building name (nothing
    Tier 1's own Geocoding API lookup could use) falls through to Tier 2's
    weaker Places name-only search, which has no way to distinguish a
    same-named but genuinely different real place. Confirmed real
    incident: a fresh Colliers upload's own "Thames Court" row (4 Upper
    Thames Street, EC4V 3BJ - no street address of its own in the raw
    spreadsheet) landed on a same-named building ~29km away in Surrey via
    Tier 2, correctly flagged geocode_unverified=True, but nothing ever
    revisited it once its own brochure went on to correctly backfill both
    address_1 and postcode a few steps later - by then geocoding had
    already run and moved on for good.

    Deliberately scoped to ONLY a row that (a) already carries geocode_
    unverified=True (see schema.ListingRow's own docstring for its exact
    tri-state semantics - True specifically means "this run has real
    evidence the location IS NOT verified", the one and only state this
    fix should ever act on) - a trusted row (False) or a row geocoding
    never even attempted (None) must never be re-geocoded here, avoiding
    needless churn/API cost on rows that don't need it - and (b) didn't
    already have BOTH address_1 and postcode before this specific
    enrichment pass, so this only fires when the pass genuinely just
    supplied evidence Tier 1 didn't have the first time, never re-running
    an already-Tier-1-quality lookup for no reason.

    Clears the row's own existing (untrusted) lat/lng before calling
    geocode_row again - geocode_row's own first check returns immediately
    without attempting anything further whenever lat/lng are already
    non-None (the ordinary, correct behavior for a row with a real
    source-provided coordinate), which would otherwise make this entire
    re-attempt a silent no-op for exactly the rows it exists to fix.
    """
    for row, (was_unverified, had_address_1, had_postcode) in zip(rows, pre_enrichment_state):
        if was_unverified is not True:
            continue
        if had_address_1 and had_postcode:
            continue
        if not (row.address_1 and row.postcode):
            continue
        row.lat = None
        row.lng = None
        geocode.geocode_row(row)


def _warn_if_extraction_looks_garbled(rows: list[ListingRow], sheet_label: str) -> None:
    """
    A cheap, visible sanity check for Gemini-extracted spreadsheet rows only
    (see extract_spreadsheet_gemini.extract_sheet) - header-mapping has no
    equivalent failure mode (a mapped column either has real values or it
    doesn't; there's nothing to "garble"), but Gemini will attempt to
    produce SOME output regardless of how well it actually understood a
    malformed or unusual sheet layout, with no exception raised to signal a
    bad read. Flags - visibly, in the upload UI, not just a stderr log -
    when a large share of a sheet's extracted units are missing BOTH
    size_sqft and any rent figure: a real listing almost always states at
    least one of these, so that combination is a strong signal of a
    genuinely garbled read rather than a sheet that legitimately never
    states this data (which would instead miss just one or the other, not
    both, across most of its rows).
    """
    if not rows:
        return
    missing = sum(1 for r in rows if r.size_sqft is None and r.rent_pcm is None and r.rent_psf is None)
    if missing / len(rows) > 0.5:
        st.warning(
            f"⚠️ {sheet_label}: {missing} of {len(rows)} extracted row(s) are missing both size and "
            "rent figures - this may mean the AI misread this sheet's layout. Please check these rows "
            "carefully before approving."
        )


def _warn_if_units_look_undercounted(rows: list[ListingRow], ws, sheet_label: str) -> None:
    """
    A second, cheap sanity check for Gemini-extracted spreadsheet rows,
    alongside _warn_if_extraction_looks_garbled - that one flags a row
    whose OWN content looks garbled; this one flags when a building's own
    block in the raw sheet text visibly contains MORE data rows than were
    actually extracted for it, a failure row-level content checks can't
    see at all (see extract_spreadsheet_gemini.find_undercounted_
    buildings's own docstring for the real, confirmed production case
    this exists to catch).

    Re-renders the sheet's raw text (see render_sheet_as_text) rather
    than threading it through extract_sheet's own return value - a
    second, cheap, pure-function call, not worth widening extract_sheet's
    return type (and every existing caller/test of it) just to avoid it.
    """
    if not rows:
        return
    text = extract_spreadsheet_gemini.render_sheet_as_text(ws)
    units = [{"building": r.building} for r in rows]
    for building, apparent, actual in extract_spreadsheet_gemini.find_undercounted_buildings(text, units):
        st.warning(
            f"⚠️ {sheet_label}: {building} looks like it may have {apparent} unit(s) in the "
            f"source sheet, but only {actual} were extracted - please check this building "
            "carefully before approving."
        )


def _warn_if_brochure_link_missing(rows: list[ListingRow], ws, sheet_label: str) -> None:
    """
    A third, cheap sanity check for Gemini-extracted spreadsheet rows,
    alongside _warn_if_extraction_looks_garbled/_warn_if_units_look_
    undercounted - this one flags when a building's own block in the raw
    sheet text visibly contains a "Download Brochure" hyperlink but none of
    that building's own extracted units carry a brochure_link (see extract_
    spreadsheet_gemini.find_buildings_missing_brochure_link's own docstring
    for the real, confirmed production case this exists to catch - a real
    source link every one of these buildings genuinely has).

    Re-renders the sheet's raw text (see render_sheet_as_text) rather than
    threading it through extract_sheet's own return value - same reasoning
    as _warn_if_units_look_undercounted above.
    """
    if not rows:
        return
    text = extract_spreadsheet_gemini.render_sheet_as_text(ws)
    units = [
        {"building": r.building, "brochure_link": r.brochure_link,
         "brochure_link_is_floorplan": r.brochure_link_is_floorplan}
        for r in rows
    ]
    for building in extract_spreadsheet_gemini.find_buildings_missing_brochure_link(text, units):
        st.warning(
            f"⚠️ {sheet_label}: {building} has a Download Brochure link in the source sheet, but no "
            "brochure link was extracted for any of its units - please check this building's brochure "
            "link before approving."
        )


def _resolve_sheet_mapping(file_bytes: bytes, suffix: str, filename: str, sheet_name):
    """
    (df, headers, mapping, unresolved) for one sheet - the exact header-
    mapping resolution the Extract-time sheet loop has always done,
    factored out so the SAME logic backs both that loop and the upload-time
    ambiguous-sheet scan (_scan_spreadsheet_sheets) - the two can never
    independently disagree about which sheets even reach classification.
    """
    df = extract_spreadsheet.read_spreadsheet(file_bytes, suffix, sheet_name=sheet_name)
    headers = list(df.columns)
    h_hash = extract_spreadsheet.header_hash(headers)
    mapping = extract_spreadsheet.suggest_mapping(headers)
    mapping = extract_spreadsheet.apply_provider_structural_fallback(mapping, headers, filename)
    saved_rescue = get_saved_critical_field_rescue(h_hash)
    rescue = saved_rescue["assignments"] if saved_rescue else {}
    mapping = extract_spreadsheet.apply_critical_field_rescue(mapping, rescue)
    unresolved = extract_spreadsheet.unresolved_critical_fields(mapping, rescue)
    return df, headers, mapping, unresolved


def _scan_spreadsheet_sheets(file_bytes: bytes, suffix: str, filename: str) -> list:
    """
    One entry per sheet in this spreadsheet file, in original order:
    {"sheet_name", "df", "headers", "mapping", "unresolved", "ws", "text",
    "classification"}. ws/text/classification stay None for a sheet
    header-mapping already resolved - a header-mapped sheet (Kitt's/UNION-
    style) never needed the Gemini fallback at all, so it's never a rollup/
    ambiguity candidate either (see extract_spreadsheet_gemini.
    is_non_authoritative_rollup_sheet's own scope).

    Computed once per uploaded file and reused for BOTH the upload-time
    ambiguous-sheet decision UI (rendered before Extract is even clickable)
    and the real Extract-time sheet loop - a single source of truth, so the
    two can never disagree about what any sheet classifies as.
    """
    sheet_names = extract_spreadsheet.list_sheet_names(file_bytes, suffix)
    wb = None
    plans = []
    for sheet_name in sheet_names:
        df, headers, mapping, unresolved = _resolve_sheet_mapping(file_bytes, suffix, filename, sheet_name)
        ws = None
        if unresolved:
            if wb is None:
                wb = load_workbook(BytesIO(file_bytes), data_only=True)
            ws = wb[sheet_name] if sheet_name else wb.active
        plans.append({
            "sheet_name": sheet_name, "df": df, "headers": headers, "mapping": mapping,
            "unresolved": unresolved, "ws": ws, "text": None, "classification": None,
        })

    # Cross-sheet "Updated" date comparison (see extract_spreadsheet_gemini.
    # classify_sheet_for_extraction's own sibling_dates note) needs every
    # unresolved sheet's own date known before classifying ANY of them.
    dates = {}
    for plan in plans:
        if plan["ws"] is None:
            continue
        plan["text"] = extract_spreadsheet_gemini.render_sheet_as_text(plan["ws"])
        d = extract_spreadsheet_gemini.extract_update_date(plan["text"])
        if d:
            dates[plan["sheet_name"]] = d

    for plan in plans:
        if plan["ws"] is None:
            continue
        sibling_dates = {name: d for name, d in dates.items() if name != plan["sheet_name"]}
        plan["classification"] = extract_spreadsheet_gemini.classify_sheet_for_extraction(
            plan["ws"], plan["text"], sibling_dates=sibling_dates,
        )
    return plans


def _sheet_decision_key(file_hash: str, sheet_name) -> str:
    return f"sheet_decision_{file_hash}_{sheet_name}"


def _spreadsheet_content_hash(file_bytes: bytes, decisions: dict) -> str:
    """
    content_hash for a spreadsheet upload - _SPREADSHEET_LOGIC_FINGERPRINT +
    file_bytes exactly as before, PLUS a canonical (sorted-keys) JSON
    serialization of this file's own ambiguous-sheet Include/Skip decisions
    (see classify_sheet_for_extraction's "ambiguous" outcome) folded in - so
    the exact same bytes with a DIFFERENT decision for even one sheet never
    collides with an already-cached result computed under a different
    choice (see find_previous_upload_by_hash). decisions: {sheet_name:
    SHEET_DECISION_SKIP|SHEET_DECISION_INCLUDE, ...} - empty for a file with
    no ambiguous sheet at all, in which case this is exactly the pre-
    existing formula (nothing to fold in, nothing changes for it).
    """
    decisions_repr = json.dumps(decisions, sort_keys=True).encode("utf-8")
    return hashlib.sha256(
        _SPREADSHEET_LOGIC_FINGERPRINT.encode("utf-8") + b"\0" + file_bytes + b"\0" + decisions_repr
    ).hexdigest()


def _pdf_or_email_content_hash(suffix: str, file_bytes: bytes) -> str:
    """
    content_hash for a PDF/email upload - _PDF_EMAIL_LOGIC_FINGERPRINT (a
    hash of extract.py's/extract_email.py's own source - see its own
    comment) + file_bytes + geocode.py's own source, automatically folded in
    for the same reason _SPREADSHEET_LOGIC_FINGERPRINT already folds it in
    for a spreadsheet upload (see that constant's own comment) -
    geocode_rows() runs unconditionally right after extraction for BOTH
    source types, so a geocoding-logic change must invalidate an already-
    staged PDF/email result too.

    brochure_enrichment.py's own source is ALSO folded in, but only for an
    email upload (suffix == ".eml") - an email upload now runs automatic
    brochure enrichment too (see app.py's own is_email_source), so a
    brochure_enrichment.py change must invalidate an already-staged EMAIL
    result automatically, exactly like it already does for a spreadsheet
    upload (see _SPREADSHEET_LOGIC_FINGERPRINT's own inclusion of it).
    Deliberately never folded in for a PDF upload, which still never runs
    automatic enrichment at all (see brochure_enrichment.py's own module
    docstring) - doing so anyway would only cause needless re-extraction of
    a PDF on a brochure_enrichment.py change that could never actually
    affect its result.
    """
    versioned_content = (
        _PDF_EMAIL_LOGIC_FINGERPRINT.encode("utf-8")
        + b"\0" + file_bytes + b"\0" + Path(geocode.__file__).read_bytes()
    )
    if suffix == ".eml":
        versioned_content += b"\0" + Path(brochure_enrichment.__file__).read_bytes()
    return hashlib.sha256(versioned_content).hexdigest()


def _render_ambiguous_sheet_decision(filename: str, plan: dict, file_hash: str) -> None:
    """
    Renders the required decision control + explanatory copy for one
    ambiguous sheet (see extract_spreadsheet_gemini.classify_sheet_for_
    extraction) - a radio defaulting to the neutral SHEET_DECISION_
    PLACEHOLDER (never pre-selecting Skip or Include), so _pending_sheet_
    decisions can tell "not yet decided" apart from a real choice.

    The widget's own key IS the decision's storage (Streamlit persists a
    widget's value in st.session_state under its key across reruns) - keyed
    by file_hash (the exact uploaded bytes, see _sheet_decision_key), never
    by filename or sheet name alone, so the choice is scoped to exactly
    this uploaded file for this session only: a different file (even one
    with the same name) or a new session starts with no decision recorded,
    and nothing here is ever written to disk.
    """
    sheet_name = plan["sheet_name"]
    hidden = plan["classification"]["sheet_state"] in ("hidden", "veryHidden")
    key = _sheet_decision_key(file_hash, sheet_name)

    with st.container(border=True):
        st.markdown(f"**Possible summary or old sheet detected: {sheet_name}**")
        st.write(
            f"This workbook (**{filename}**) contains a sheet named `{sheet_name}` that may be a "
            "summary, older rollup, or non-authoritative source, rather than genuine current "
            "availability."
        )
        st.caption(f"ℹ️ {HIDDEN_SHEET_EXPLAINER}")
        st.caption(f"Sheet visibility: **{'Hidden' if hidden else 'Visible'}**")

        st.radio(
            f'What should we do with "{sheet_name}"?',
            [SHEET_DECISION_PLACEHOLDER, SHEET_DECISION_SKIP, SHEET_DECISION_INCLUDE],
            key=key,
        )
        st.caption(
            f"**{SHEET_DECISION_SKIP}** — recommended if it's an old summary/rollup. "
            f"**{SHEET_DECISION_INCLUDE}** — use if it contains genuine current availability."
        )


# Shown inline under a pasted link's own entry in the "ready to extract"
# list whenever none of _fetch_pasted_link's three strategies (direct PDF,
# Canva/Pitch render, resolve_brochure_link's landing-page scan) produced
# real document bytes - deliberately ONE generic message covering every
# such failure (a dead link, a private/unshared file, a page gated behind
# a sign-in/email form) rather than a separate detection mechanism per
# cause: none of those three strategies can ever distinguish "this is a
# login form" from "this just isn't a readable document" without actually
# filling in and submitting a form, which this deliberately never does.
PASTED_LINK_UNREADABLE_MESSAGE = (
    "⚠️ Couldn't read this link — it looks like it needs a sign-in or email first. "
    "Save it as a PDF and upload that instead."
)


class _PastedLinkFile:
    """
    Minimal file-like stand-in for a pasted link's own fetched PDF bytes -
    exposes exactly .name/.getvalue(), the only two attributes hashing/
    staging/brochure enrichment ever read off an uploaded_file, so a
    successfully-fetched link can sit in the exact same uploaded_files
    list as a real st.file_uploader UploadedFile and flow through all of
    that completely unchanged.

    png_pages/page_links (both None for a direct-PDF or resolved-landing-
    page link - see _fetch_pasted_link) are the ONE piece the Extract
    loop below does treat specially: when present, they're the ORIGINAL
    Canva/Pitch render pages and each page's own real link candidates
    (see brochure_enrichment.fetch_rendered_page_with_links) - extracted
    via extract.extract_from_png_pages directly (never by re-rasterizing
    .getvalue()'s own assembled PDF bytes back into images - png_pages
    already ARE real screenshots, so that round trip would just be a
    slower, lossier no-op for identical content) so Gemini can attribute
    a per-property brochure_link from each page's own real anchors,
    rather than every unit falling back to one shared document link.
    .getvalue()'s own assembled-PDF bytes are still what gets persisted
    as this pasted link's own "whole document" fallback copy either way -
    only which BYTES Gemini actually sees differs.
    """

    def __init__(self, name: str, data: bytes, png_pages: list = None, page_links: list = None):
        self.name = name
        self._data = data
        self.png_pages = png_pages
        self.page_links = page_links

    def getvalue(self) -> bytes:
        return self._data


def _filename_from_url(url: str) -> str:
    """A sensible, always-.pdf-suffixed filename derived from `url` - the
    URL's own last path segment when it already looks like a real
    filename (the common case: a direct document link, or a resolved
    landing-page link), otherwise the host plus the FULL path, sanitized
    (a Canva/Pitch "view" link's own last path segment is never a
    filename - every real Canva "view" link ends in the literal, fixed
    "/view" segment, e.g. "/design/{design_id}/{share_token}/view" - see
    brochure_link_resolver._CANVA_VIEW_URL_RE's own docstring for the
    exact shape - so using ONLY that last segment collapsed EVERY
    distinct Canva design down to the byte-identical stem
    "www.canva.com_view.pdf" regardless of its own unique design_id/
    share_token, both of which sit in EARLIER path segments this used to
    discard entirely. Confirmed a real bug this way - not just a
    theoretical risk: save_original_pdf's own filename only gets a
    second-resolution timestamp prefix, so any two paste-a-link calls
    landing in the same wall-clock second (plausible for a fast multi-
    link paste/extract batch) silently overwrote each other's GCS object
    under this identical stem, leaving several genuinely unrelated
    buildings' own rows pointing at the SAME saved brochure_link even
    though each was pasted from its own distinct Canva design. Using the
    full path (not just its last segment) keeps every distinct URL's own
    distinguishing segments in the generated filename."""
    parsed = urlparse(url)
    base = Path(parsed.path).name
    if base.lower().endswith(".pdf"):
        return base
    host = (parsed.netloc or "link").replace(":", "_")
    path_slug = re.sub(r"[^A-Za-z0-9]+", "_", parsed.path).strip("_")
    return f"{host}_{path_slug}.pdf" if path_slug else f"{host}.pdf"


def _pdf_bytes_from_png_pages(png_pages: list) -> bytes:
    """
    Assembles `png_pages` (page-ordered PNG bytes, e.g. from a Canva/Pitch
    render - see brochure_enrichment._fetch_canva_rendered_page/_fetch_
    pitch_rendered_page) into a single in-memory PDF, one page per image,
    each page sized to match its own image - so a multi-page design
    becomes one ordinary multi-page PDF, indistinguishable downstream from
    any other uploaded PDF.
    """
    doc = fitz.open()
    try:
        for png_bytes in png_pages:
            img_doc = fitz.open(stream=png_bytes, filetype="png")
            try:
                img_pdf = fitz.open("pdf", img_doc.convert_to_pdf())
                try:
                    doc.insert_pdf(img_pdf)
                finally:
                    img_pdf.close()
            finally:
                img_doc.close()
        return doc.tobytes()
    finally:
        doc.close()


def _fetch_pasted_link(url: str):
    """
    Real PDF bytes for a pasted link, wrapped as a _PastedLinkFile, or None
    on any failure - the exact precedence a brochure_link fetch already
    uses: a direct document link fetched as-is; a Canva/Pitch "view" link
    read via the existing render service, when configured; anything else
    resolved one hop via resolve_brochure_link's landing-page scan.

    The Canva/Pitch case is checked HERE, first, rather than delegated to
    brochure_enrichment._fetch_pdf_bytes (which also has its own,
    untouched canva/pitch branches - see FetchPdfBytesCanvaRoutingTests/
    FetchPdfBytesPitchDispatchTests) specifically so this can ALSO get
    each page's own real link candidates (see brochure_enrichment.
    fetch_rendered_page_with_links) for per-property attribution during
    extraction - _fetch_pdf_bytes's own canva/pitch branches only ever
    return the bare page images, discarding link data, since that's the
    correct, unchanged contract for its OTHER caller (the per-unit
    brochure enrichment path, never touched by this). Both checks use
    the exact same condition (is_canva_view_link(url) or is_pitch_view_
    link(url)) AND _canva_renderer_configured()) _fetch_pdf_bytes's own
    branches require, so nothing about the overall precedence for any
    OTHER url shape changes - a canva/pitch url with the renderer NOT
    configured still falls through to the generic path below exactly as
    before, correctly failing the same clean way a generic unreadable
    link would (see this feature's own original scoping note on the
    Pitch renderer not needing to exist yet for this to degrade safely).
    """
    if (
        brochure_enrichment.is_canva_view_link(url) or brochure_enrichment.is_pitch_view_link(url)
    ) and brochure_enrichment._canva_renderer_configured():
        pages, page_links = brochure_enrichment.fetch_rendered_page_with_links(url)
        if pages is None:
            return None
        try:
            data = _pdf_bytes_from_png_pages(pages)
        except Exception:
            return None
        return _PastedLinkFile(_filename_from_url(url), data, png_pages=pages, page_links=page_links)

    data = brochure_enrichment._fetch_pdf_bytes(url)
    if data is None:
        return None
    return _PastedLinkFile(_filename_from_url(url), data)


def _validate_pasted_link_brochure_links(rows: list, shared_fallback_link: str) -> None:
    """
    Mutates each row's own brochure_link in place - only ever called for
    a pasted Canva/Pitch link's own rows (see the Extract loop's own
    png_pages branch). A per-unit link Gemini attributed from a page's
    real link candidates (see extract.extract_from_png_pages/images_
    from_png_pages's own page_links param) is trusted by LABEL alone up
    to this point, never by having actually been fetched - this is where
    that happens, reusing brochure_enrichment._fetch_pdf_bytes wholesale
    (the exact same direct-vs-landing-page-scan precedence any other
    brochure_link fetch already uses) rather than a second, differently-
    tuned validation. A link that fails - blocked to a plain fetch, a dead
    link, anything that doesn't even resolve to a live response - loses to
    the shared document-level fallback link instead of staging a dead one.

    accept_any_reachable_page=True (see _looks_like_fetchable_document's
    own docstring) - this call only needs to confirm the link is genuinely
    reachable, never that it's literally a PDF/image: the extraction
    PROMPT's own brochure_link instructions already treat a real per-unit
    listing webpage (not just a document URL) as valid, so requiring an
    actually-fetchable DOCUMENT here was strictly narrower than what was
    ever extracted, and confirmed to silently discard a genuine, live
    per-unit link (a real colliers.com listing page) in favor of the
    shared fallback purely for not being a PDF. The bytes themselves are
    never used below - only whether the fetch returned None at all.

    A row whose brochure_link already equals shared_fallback_link (rare
    now that finalize_brochure_link no longer has any PDF-fallback default
    of its own to produce this coincidentally - see its own docstring on
    why that was removed; still guarded against here defensively, e.g. if
    Gemini's own per-unit pick happens to equal it) has nothing to
    validate, and nothing to fall back FROM either. A row flagged
    brochure_link_is_floorplan (a floorplan link substituted in because no
    genuine brochure link existed at all) is left alone too - a separate,
    pre-existing mechanism this feature doesn't touch.
    """
    for row in rows:
        if not row.brochure_link or row.brochure_link == shared_fallback_link or row.brochure_link_is_floorplan:
            continue
        if brochure_enrichment._fetch_pdf_bytes(row.brochure_link, accept_any_reachable_page=True) is None:
            row.brochure_link = shared_fallback_link


def _propagate_validated_links_within_page(rows: list, page_indices: list, shared_fallback_link: str) -> None:
    """
    Mutates each row's own brochure_link in place - only ever called right
    after _validate_pasted_link_brochure_links, on the same rows. Gemini's
    own per-row link attribution (see extract.extract_from_png_pages/
    images_from_png_pages's own page_links param) sometimes only tags the
    FIRST row of a multi-row same-building group that all share one page,
    leaving every other row on that page/building on the shared document-
    level fallback even though they're unambiguously the same real
    property - not the multi-building-per-page case that justifies
    trusting Gemini's own per-row pick over a plain page/building grouping
    in the first place. This backfills that gap: for every group of rows
    sharing the SAME page_index and the SAME building (see normalize_key),
    if exactly one of them ends up with a genuine, validated per-unit link
    - i.e. its own brochure_link survived _validate_pasted_link_brochure_
    links rather than being replaced by shared_fallback_link - that same
    link is copied onto the other rows in that group still sitting on the
    fallback.

    page_indices is extract_from_png_pages's own `.page_indices` attribute
    (a parallel list, same order/length as rows, of each row's own raw
    unit's page_index - see extract._rows_from_raw) - None (no page_index
    data at all, e.g. rows that didn't come through extract_from_png_pages)
    makes this a no-op. A row whose own page_index couldn't be determined,
    or whose building is blank, never groups with anything else, including
    another such row - a blank is never evidence of being the same page/
    building. A row with brochure_link_is_floorplan set is excluded from
    both sides of the group - a floorplan-substituted link is a different
    kind of fact (see finalize_brochure_link/schema.py's own docstring),
    never something to propagate FROM, and never something to overwrite.

    Deliberately conservative like every other tier in this feature:
    two or more rows in the same group with their own DIFFERENT validated
    links is left alone entirely (genuinely ambiguous - never guessed at),
    same as zero rows with one.
    """
    if not page_indices:
        return

    groups = {}
    for row, page_index in zip(rows, page_indices):
        if page_index is None or not row.building:
            continue
        groups.setdefault((page_index, normalize_key(row.building)), []).append(row)

    for group in groups.values():
        if len(group) < 2:
            continue
        validated = [
            row for row in group
            if row.brochure_link and row.brochure_link != shared_fallback_link and not row.brochure_link_is_floorplan
        ]
        if len(validated) != 1:
            continue
        real_link = validated[0].brochure_link
        for row in group:
            if row.brochure_link == shared_fallback_link and not row.brochure_link_is_floorplan:
                row.brochure_link = real_link


with page_setup.setup_page("upload"):
    st.title("Upload Brochure")

    st.session_state.setdefault("recent_uploads", [])

    uploaded_files = st.file_uploader(
        "Upload one or more PDF brochures, .eml emails, or provider .xlsx/.csv spreadsheets",
        type=["pdf", "eml", "xlsx", "csv"],
        accept_multiple_files=True,
    )

    # Pasted links live in their own session_state list, independent of
    # uploaded_files (a real st.file_uploader value, which Streamlit itself
    # owns and re-supplies every rerun) - each entry is fetched EAGERLY,
    # right when "Add link" is clicked, not deferred to Extract time, so a
    # failure (see PASTED_LINK_UNREADABLE_MESSAGE) shows inline in the
    # "ready to extract" list below well before Extract is ever clicked.
    # excluded_upload_file_ids is the parallel mechanism for an UPLOADED
    # file's own "remove" button - st.file_uploader has no API to drop one
    # file from an existing multi-file selection, so a removed file simply
    # stays selected in the widget itself but is filtered out everywhere
    # below by its own stable .file_id.
    st.session_state.setdefault("pasted_links", [])
    st.session_state.setdefault("excluded_upload_file_ids", set())
    st.session_state.setdefault("paste_link_input_epoch", 0)

    # st.form is what makes pressing Enter in the text_input below add the
    # link, not just clicking the button - a bare st.text_input never
    # submits on Enter at all, only a form (via st.form_submit_button)
    # does. clear_on_submit is deliberately left at its default (False):
    # clearing the input is already handled below by bumping the epoch key
    # ONLY on a successful add, so a failed validation (see the "Paste a
    # link first"/"doesn't look like a valid link" warnings) still leaves
    # what the reviewer typed visible to fix, exactly as before this existed.
    with st.form(key="paste_link_form", clear_on_submit=False):
        link_input_col, add_link_col = st.columns([5, 1])
        with link_input_col:
            pasted_link_text = st.text_input(
                "Or paste a link to a brochure (a PDF, or a Canva/Pitch view link)",
                key=f"paste_link_input_{st.session_state['paste_link_input_epoch']}",
            )
        with add_link_col:
            st.write("")  # vertical alignment with the text_input's own label row
            add_link_clicked = st.form_submit_button("Add link")

    if add_link_clicked:
        candidate = pasted_link_text.strip()
        if not candidate:
            st.warning("Paste a link first.")
        elif not looks_like_url(candidate):
            st.warning("That doesn't look like a valid link.")
        else:
            with st.spinner(f"Fetching {candidate}..."):
                fetched = _fetch_pasted_link(candidate)
            st.session_state["pasted_links"].append({"id": str(uuid.uuid4()), "url": candidate, "wrapper": fetched})
            # A fresh widget key next render clears the input - see
            # _selection_epoch_key in pages/2_Review_and_Master.py for the
            # same "bump a counter to force a new key" idiom already used
            # elsewhere in this app for exactly this reason.
            st.session_state["paste_link_input_epoch"] += 1
            st.rerun()

    # The unified "ready to extract" list - every uploaded file not
    # excluded, plus every pasted link (successful or not - a failed one
    # still shows here with its own warning, per PASTED_LINK_UNREADABLE_
    # MESSAGE, purely so a reviewer can see it and remove it; it never
    # contributes to ready_link_files below). One remove control per
    # entry, regardless of which kind it is.
    ready_uploaded_files = [
        f for f in (uploaded_files or []) if f.file_id not in st.session_state["excluded_upload_file_ids"]
    ]
    ready_link_files = [
        entry["wrapper"] for entry in st.session_state["pasted_links"] if entry["wrapper"] is not None
    ]

    if ready_uploaded_files or st.session_state["pasted_links"]:
        st.markdown("**Ready to extract:**")
        for f in ready_uploaded_files:
            entry_col, remove_col = st.columns([6, 1])
            with entry_col:
                st.write(f"📄 {f.name}")
            with remove_col:
                if st.button("✕", key=f"remove_upload_{f.file_id}"):
                    st.session_state["excluded_upload_file_ids"].add(f.file_id)
                    st.rerun()
        for entry in st.session_state["pasted_links"]:
            entry_col, remove_col = st.columns([6, 1])
            with entry_col:
                label = entry["wrapper"].name if entry["wrapper"] is not None else entry["url"]
                st.write(f"🔗 {label}")
                if entry["wrapper"] is None:
                    st.warning(PASTED_LINK_UNREADABLE_MESSAGE)
            with remove_col:
                if st.button("✕", key=f"remove_link_{entry['id']}"):
                    st.session_state["pasted_links"] = [
                        e for e in st.session_state["pasted_links"] if e["id"] != entry["id"]
                    ]
                    st.rerun()

    # From here on, uploaded_files IS the combined, filtered "ready" list -
    # every existing use below (spreadsheet-sheet scanning, the Extract
    # button's own disabled/total-count logic, the Extract loop itself)
    # needs no further change at all, since a pasted link's own
    # _PastedLinkFile already exposes exactly what an UploadedFile does.
    uploaded_files = ready_uploaded_files + ready_link_files

    # Column mapping itself is fully automatic (see suggest_mapping). A
    # genuinely CRITICAL field (extract_spreadsheet.CRITICAL_FIELDS) going
    # unmapped used to stop here for a human to manually pick which column
    # has it - removed: a sheet with no single consistent header row at all
    # (confirmed against a real Copthall Estates file, whose row 1 is just a
    # title string) has no legitimate column for a human to pick in the
    # first place, so that prompt's own dropdown offered nonsense options
    # ("nan", "1", the title text itself) rather than a real choice. Building
    # unresolved this way now falls straight through to extract_spreadsheet_
    # gemini.extract_sheet in the Extract-time loop below, unconditionally -
    # no prompt, no confirmation, no interruption. A PREVIOUSLY saved rescue
    # answer (from before this change) is still read and applied there (see
    # get_saved_critical_field_rescue) - only creating a NEW one via a human
    # prompt is gone, not the ability to honor an existing one.

    # A DIFFERENT decision from the column-mapping rescue above: whether a
    # whole SHEET (one that already needed the Gemini fallback) is even a
    # genuine source of current availability at all - see extract_
    # spreadsheet_gemini.classify_sheet_for_extraction. Scanned here, before
    # Extract is even clickable, so an ambiguous sheet's decision is made
    # BEFORE extraction the same way any other required pre-extraction
    # choice would be - never silently decided one way or the other.
    # Content-keyed (sha256 of the raw bytes alone, not filename/sheet name)
    # so two uploads that happen to share a filename are never confused with
    # each other, and re-scanning the exact same bytes within one session
    # (e.g. after an unrelated widget interaction triggers a rerun) reuses
    # the same scan rather than recomputing it under a different identity.
    scanned_files = {}  # file_hash -> (filename, [plan, ...])
    if uploaded_files:
        for uploaded_file in uploaded_files:
            suffix = Path(uploaded_file.name).suffix.lower()
            if suffix not in SPREADSHEET_SUFFIXES:
                continue
            file_bytes = uploaded_file.getvalue()
            file_hash = hashlib.sha256(file_bytes).hexdigest()
            if file_hash not in scanned_files:
                scanned_files[file_hash] = (
                    uploaded_file.name, _scan_spreadsheet_sheets(file_bytes, suffix, uploaded_file.name),
                )

    # auto_skip sheets (see extract_spreadsheet_gemini.is_non_authoritative_
    # rollup_sheet) are intentionally silent here - confidently non-
    # authoritative, so nothing to ask or even mention before Extract; the
    # existing Extract-time "hidden, non-authoritative rollup sheet —
    # skipped" info message (see the sheet loop below) is the only surfacing
    # they get. Only genuinely ambiguous sheets need a human's attention
    # here at all.
    any_hidden_sheet_mentioned = False
    pending_decision_labels = []
    for file_hash, (filename, plans) in scanned_files.items():
        for plan in plans:
            classification = plan["classification"]
            if not classification or classification["outcome"] != "ambiguous":
                continue

            any_hidden_sheet_mentioned = True
            _render_ambiguous_sheet_decision(filename, plan, file_hash)
            decision = st.session_state.get(_sheet_decision_key(file_hash, plan["sheet_name"]))
            if decision in (None, SHEET_DECISION_PLACEHOLDER):
                pending_decision_labels.append(f"{filename} — {plan['sheet_name']}")

    if any_hidden_sheet_mentioned:
        st.caption(f"ℹ️ {HIDDEN_SHEET_EXPLAINER}")

    extract_disabled = bool(pending_decision_labels)
    if pending_decision_labels:
        st.warning(
            "Please decide what to do with the following sheet(s) before extracting: "
            + ", ".join(pending_decision_labels)
        )

    if uploaded_files and st.button("Extract", disabled=extract_disabled):
        total = len(uploaded_files)
        succeeded = 0
        tmp_path = None
        try:
            for i, uploaded_file in enumerate(uploaded_files, start=1):
                with st.spinner(f"Processing {i} of {total}: {uploaded_file.name}..."):
                    suffix = Path(uploaded_file.name).suffix.lower()
                    # Computed once per file, up here, rather than separately
                    # inside the reused-vs-fresh branches below - both the
                    # reused branch's fill_missing_*/apply_*_fallback calls
                    # AND automatic brochure enrichment (fresh extraction
                    # only, see below) need to know this.
                    is_spreadsheet_source = suffix in SPREADSHEET_SUFFIXES
                    # Automatic brochure enrichment now also runs for an
                    # email upload (see the row_count/enrichment gates
                    # below and brochure_enrichment.py's own module
                    # docstring) - deliberately NOT a PDF upload, which is
                    # already extracted from the actual brochure itself
                    # (enriching it from itself would be circular).
                    is_email_source = suffix == ".eml"

                    # Hashed before anything else, from the bytes already in
                    # memory - a byte-identical re-upload (same content, any
                    # filename) skips extraction/geocoding entirely and
                    # reuses the prior run's rows verbatim, rather than
                    # calling Gemini again on a document that hasn't
                    # actually changed. This matters beyond just the wasted
                    # API call: Gemini's extraction isn't perfectly
                    # deterministic, so a repeat call on an unchanged
                    # document can genuinely reword a prose field
                    # (special_features, contacts) - a spurious diff with no
                    # real change behind it. Note this only catches an
                    # exact byte-for-byte duplicate (e.g. the same download
                    # re-uploaded, or renamed) - a resaved/re-exported copy
                    # with identical visual content still hashes differently,
                    # since PDF containers embed producer metadata that
                    # changes across a re-save even when the rendered pages
                    # don't.
                    #
                    # Folded into the hash alongside the file bytes, so a
                    # logic change invalidates any already-staged result: for
                    # PDF/email, _PDF_EMAIL_LOGIC_FINGERPRINT (a hash of
                    # extract.py's/extract_email.py's own source); for a
                    # spreadsheet, _SPREADSHEET_LOGIC_FINGERPRINT instead (a
                    # hash of extract_spreadsheet.py's own source) - both
                    # automatic, nothing to remember to bump.
                    file_bytes = uploaded_file.getvalue()
                    sheet_plans = None
                    decisions_for_this_file = {}
                    if suffix in SPREADSHEET_SUFFIXES:
                        file_hash = hashlib.sha256(file_bytes).hexdigest()
                        _, sheet_plans = scanned_files[file_hash]
                        # Folded into content_hash below - see _spreadsheet_
                        # content_hash's own docstring for why the exact same
                        # bytes with a different ambiguous-sheet decision must
                        # never collide with a result cached under a
                        # different choice.
                        decisions_for_this_file = {
                            p["sheet_name"]: st.session_state.get(_sheet_decision_key(file_hash, p["sheet_name"]))
                            for p in sheet_plans
                            if p["classification"] and p["classification"]["outcome"] == "ambiguous"
                        }
                        content_hash = _spreadsheet_content_hash(file_bytes, decisions_for_this_file)
                        # The real bytes + decisions ALONE, no code-logic
                        # fingerprint - see save_staging_file's own
                        # source_identity_hash docstring for the real,
                        # confirmed gap this closes (re-uploading the same
                        # source across a code change getting a different
                        # content_hash, so a stale pending copy was never
                        # recognized as superseded). Reuses file_hash (the
                        # same raw-bytes hash already computed above) rather
                        # than hashing file_bytes a second time.
                        decisions_repr = json.dumps(decisions_for_this_file, sort_keys=True).encode("utf-8")
                        source_identity_hash = hashlib.sha256(
                            file_hash.encode("utf-8") + b"\0" + decisions_repr
                        ).hexdigest()
                    else:
                        content_hash = _pdf_or_email_content_hash(suffix, file_bytes)
                        # Same idea as the spreadsheet branch above - the
                        # real bytes alone, no _PDF_EMAIL_LOGIC_FINGERPRINT/
                        # geocode.py fingerprint, so a re-upload of the same
                        # PDF/email across a code change is still recognized
                        # as superseding an earlier, stale pending copy.
                        source_identity_hash = hashlib.sha256(file_bytes).hexdigest()

                    # content_hash ONLY here - deliberately NOT source_
                    # identity_hash, despite find_previous_upload_by_hash's
                    # own docstring inviting a caller to pass it through.
                    # source_identity_hash is the real bytes ALONE, with no
                    # code-logic fingerprint - passing it here means find_
                    # previous_upload_by_hash's own preferred_target becomes
                    # source_identity_hash (see its "source_identity_hash or
                    # content_hash" precedence), so a stale entry from
                    # BEFORE a real extraction/geocoding fix still matches
                    # on raw bytes alone and gets reused forever, regardless
                    # of the fix - defeating _SPREADSHEET_LOGIC_FINGERPRINT/
                    # _PDF_EMAIL_LOGIC_FINGERPRINT's entire purpose. Real,
                    # confirmed regression (see tests/test_app_upload_
                    # geocode_cache_invalidation.py's own UploadReuseAcross
                    # AFingerprintChangeTests::test_same_bytes_but_
                    # fingerprint_changed_does_not_reuse_and_regeocodes,
                    # which was failing before this fix): a fingerprint
                    # change alone (e.g. the real Beem geocoding-validation
                    # fix) must force a fresh re-extraction on the very next
                    # re-upload of an already-staged file, not silently keep
                    # serving pre-fix rows. content_hash alone still does
                    # exactly that - it's the ONE hash that's provably
                    # sensitive to the current code, by construction (see
                    # _spreadsheet_content_hash/_pdf_or_email_content_hash).
                    #
                    # source_identity_hash is still computed above and still
                    # passed to save_staging_file below completely
                    # unaffected by this - that's a SEPARATE concern
                    # (avoiding a duplicate/stale PENDING copy piling up in
                    # a merge plan across a code change, the original
                    # Oliver's Yard duplication bug - see active_and_
                    # superseded_staging_files/_grouping_hash's own
                    # docstrings), which never goes through find_previous_
                    # upload_by_hash at all.
                    previous_staging_path = find_previous_upload_by_hash(content_hash)
                    fully_occupied_buildings = []
                    # Set below ONLY when previous_staging_path's own
                    # enrichment was left incomplete - see its own use at
                    # the automatic-enrichment call site further down.
                    resume_already_processed = None
                    resume_floorplan_already_processed = None
                    resume_special_features_matched = None

                    if previous_staging_path:
                        rows = dataframe_to_listing_rows(load_staging_as_dataframe(previous_staging_path))
                        reused = True
                        # A byte-identical re-upload while the ORIGINAL
                        # match's own brochure enrichment was still
                        # incomplete (see brochure_enrichment.run_brochure_
                        # enrichment's own status="in_progress") must not
                        # just freeze this new copy at that same partial
                        # state forever, with no automatic path to ever
                        # finish it short of the reviewer manually clicking
                        # Continue on some OTHER staging entry - continuing
                        # its own progress here means already-"ok"
                        # brochures are still never re-fetched/re-billed to
                        # Gemini (see enrich_rows_grouped's own
                        # already_processed param), this upload just picks
                        # up the remaining ones automatically instead of
                        # silently going nowhere.
                        previous_enrichment = get_staging_enrichment_summary(previous_staging_path)
                        if previous_enrichment and previous_enrichment.get("status") == "in_progress":
                            resume_already_processed = previous_enrichment.get("processed_urls", {})
                            resume_floorplan_already_processed = previous_enrichment.get(
                                "floorplan_processed_urls", {}
                            )
                            resume_special_features_matched = previous_enrichment.get(
                                "special_features_matched", {}
                            )
                        # A reused result's own fully_occupied_buildings (see
                        # extract_spreadsheet_gemini.extract_sheet_with_
                        # metadata) lives in the ORIGINAL staging run's own
                        # meta.json, not recoverable from its rows - carried
                        # forward here so a byte-identical re-upload doesn't
                        # silently lose the signal that let it through the
                        # first time.
                        fully_occupied_buildings = get_staging_fully_occupied_buildings(previous_staging_path)
                        # Preserves the exact pre-existing behavior for a
                        # cached result: origin (header-mapped vs Gemini-
                        # extracted) isn't recoverable from a reloaded
                        # staging file, so this can't apply the origin-aware
                        # scoping the fresh-extraction branches below use -
                        # a properly-processed cached row should already
                        # have gone through its own real fallback once,
                        # during the original run that produced it, making
                        # this a no-op in the overwhelmingly common case
                        # (both functions only ever fill an actually-blank
                        # value).
                        fill_missing_provider(rows, uploaded_file.name, apply_filename_guess=is_spreadsheet_source)
                        fill_missing_address_from_building(rows, apply_building_fallback=is_spreadsheet_source)
                    elif suffix in SPREADSHEET_SUFFIXES:
                        # Column mapping (see suggest_mapping) is tried first,
                        # per sheet - free and instant, no Gemini call, for
                        # any sheet shaped like a single consistent table
                        # (Kitt's/UNION exports). A sheet whose CRITICAL field
                        # (building) still can't be resolved this way - no
                        # single column states it at all, e.g. a repeating
                        # per-building block layout (real Copthall Estates
                        # files) rather than one table - falls back to
                        # Gemini text extraction (extract_spreadsheet_gemini)
                        # for THAT SHEET ONLY, the same text-extraction
                        # machinery extract_email.py already uses. Each
                        # sheet in a multi-sheet .xlsx is judged and
                        # processed independently; a .csv has exactly one
                        # (see list_sheet_names).
                        rows = []
                        header_mapped_rows = []
                        gemini_rows = []
                        # Same rows already being accumulated into header_
                        # mapped_rows above, ALSO kept grouped per sheet -
                        # never a second extraction - so the redundant-sheet
                        # overlap check below (_rows_dropped_as_duplicate_
                        # sheet_extractions) can compare one sheet's own
                        # rows against another's.
                        header_mapped_sheets = []
                        # sheet_plans (see _scan_spreadsheet_sheets) was
                        # already computed above, before Extract was even
                        # clickable, for the ambiguous-sheet decision UI - the
                        # SAME plans (mapping/unresolved/ws/classification)
                        # are reused here rather than recomputed, so this loop
                        # can never disagree with what the decision UI showed.
                        # A multi-sheet file where every sheet needs the
                        # Gemini fallback (confirmed against the real
                        # Copthall Estates file: 6 sheets, ~150s total) gives
                        # no feedback at all for minutes at a time otherwise -
                        # the outer st.spinner's own text is fixed for the
                        # whole file, not updated per sheet.
                        sheet_progress = st.empty()
                        for sheet_idx, plan in enumerate(sheet_plans, start=1):
                            sheet_name = plan["sheet_name"]
                            if len(sheet_plans) > 1:
                                sheet_progress.caption(
                                    f"Reading sheet {sheet_idx} of {len(sheet_plans)}: {sheet_name}..."
                                )
                            df, headers, mapping, unresolved = plan["df"], plan["headers"], plan["mapping"], plan["unresolved"]
                            sheet_label = f"{uploaded_file.name} — {sheet_name}" if sheet_name else uploaded_file.name

                            if unresolved:
                                ws = plan["ws"]
                                classification = plan["classification"]
                                decision = decisions_for_this_file.get(sheet_name)

                                # Checked BEFORE ever calling Gemini. Two ways a
                                # sheet never reaches extraction: confidently
                                # non-authoritative on its own (see is_non_
                                # authoritative_rollup_sheet, e.g. the real
                                # Copthall Estates Availability.xlsx's hidden
                                # "Portfolio" sheet - UNCHANGED from before this
                                # classification existed), or ambiguous with an
                                # explicit human "skip" decision recorded for
                                # it (see classify_sheet_for_extraction). Either
                                # way the sheet contributes nothing at all - no
                                # rows, no fully_occupied signal - so it can
                                # never produce a duplicate/stale row alongside
                                # its sibling sheets' own genuinely current data.
                                if classification["outcome"] == "auto_skip":
                                    st.info(
                                        f"{sheet_label}: a summary tab, not original data — skipped."
                                    )
                                    sheet_rows = []
                                elif classification["outcome"] == "ambiguous" and decision == SHEET_DECISION_SKIP:
                                    st.info(f"{sheet_label}: skipped per your decision for this sheet.")
                                    sheet_rows = []
                                else:
                                    sheet_rows, sheet_fully_occupied = extract_spreadsheet_gemini.extract_sheet_with_metadata(
                                        ws, sheet_label, uploaded_file.name
                                    )
                                    fully_occupied_buildings.extend(sheet_fully_occupied)
                                    if not sheet_rows:
                                        text = plan["text"]
                                        if extract_spreadsheet_gemini.sheet_shows_fully_occupied_building(text):
                                            st.info(
                                                f"{sheet_label}: nothing available in this building right now — skipped."
                                            )
                                        else:
                                            st.info(f"{sheet_label}: no listings found on this sheet — skipped.")
                                    else:
                                        _warn_if_extraction_looks_garbled(sheet_rows, sheet_label)
                                        _warn_if_units_look_undercounted(sheet_rows, ws, sheet_label)
                                        _warn_if_brochure_link_missing(sheet_rows, ws, sheet_label)
                                gemini_rows.extend(sheet_rows)
                            else:
                                sheet_rows = extract_spreadsheet.build_rows(df, mapping, source_file=sheet_label)
                                fill_missing_submarket_from_structural_header(sheet_rows, headers, uploaded_file.name)
                                header_mapped_rows.extend(sheet_rows)
                                header_mapped_sheets.append(sheet_rows)

                            rows.extend(sheet_rows)

                        sheet_progress.empty()

                        # Drops a row wherever it's a redundant re-
                        # extraction of the SAME real listing from ANOTHER
                        # header-mapped sheet in this same file - see
                        # _rows_dropped_as_duplicate_sheet_extractions' own
                        # docstring for the real confirmed Kitt's Kitts_
                        # Availability_External.xlsx case. A no-op (empty
                        # set) for the overwhelmingly common case of one
                        # sheet, or several genuinely different-content
                        # sheets (e.g. Copthall Estates' own 4 per-area
                        # sheets) - filters both rows and header_mapped_rows
                        # by identity so the dropped row's own object never
                        # reaches geocoding/staging via EITHER list.
                        dropped_ids = _rows_dropped_as_duplicate_sheet_extractions(header_mapped_sheets)
                        if dropped_ids:
                            rows = [r for r in rows if id(r) not in dropped_ids]
                            header_mapped_rows = [r for r in header_mapped_rows if id(r) not in dropped_ids]

                        geocode_rows(rows)
                        # fill_missing_provider applies to EVERY spreadsheet
                        # row here, header-mapped or Gemini-fallback alike -
                        # apply_filename_guess=True is scoped by SOURCE TYPE
                        # (spreadsheet vs PDF/email, see that function's own
                        # docstring), never by which extraction METHOD this
                        # particular sheet happened to need internally. A
                        # spreadsheet that needed the Gemini fallback (e.g.
                        # a repeating per-building block layout header-
                        # mapping can't resolve) is still a spreadsheet
                        # source - it has no column stating a provider
                        # either way, so the filename is exactly as reliable
                        # a fallback for it as for a header-mapped sheet.
                        # Confirmed real gap this closes: a real beem Live
                        # Flex Availability.xlsx sheet that fell back to
                        # Gemini extraction (see classify_sheet_for_
                        # extraction) ended up with a genuinely blank
                        # provider, even though guess_provider_name(
                        # uploaded_file.name) resolves to a real, correct
                        # "beem" - previously only ever applied to
                        # header_mapped_rows, never gemini_rows, based on
                        # reasoning that actually belongs to PDF/email (see
                        # extract.py/extract_email.py's OWN Gemini-decided
                        # provider case, which fill_missing_address_from_
                        # building below is correctly still scoped away
                        # from - a spreadsheet row's blank address_1 is a
                        # separate, deliberate judgment call this file's own
                        # docstring already reasons through independently,
                        # untouched here).
                        fill_missing_provider(rows, uploaded_file.name, apply_filename_guess=True)
                        fill_missing_address_from_building(header_mapped_rows, apply_building_fallback=True)

                        # Brochure enrichment deliberately does NOT run here
                        # any more - it used to (synchronously, per row),
                        # which meant a real UNION file's 100+ unique
                        # brochures blocked this ENTIRE upload behind
                        # sequential Box-fetch-then-Gemini-vision calls, with
                        # the rows below not yet staged anywhere - an
                        # interruption mid-enrichment lost the whole
                        # extraction, not just the enrichment. Extraction now
                        # stages its rows immediately (see save_staging_file
                        # below) with no brochure/Gemini call at all;
                        # enrichment is a separate, explicit, later action
                        # (see pages/2_Review_and_Master.py and brochure_
                        # enrichment.enrich_rows_grouped) that reads these
                        # already-staged rows back and writes the enriched
                        # result to the same staging file.
                        reused = False
                    else:
                        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                            tmp.write(uploaded_file.getvalue())
                            tmp_path = Path(tmp.name)

                        try:
                            if suffix == ".pdf":
                                png_pages = getattr(uploaded_file, "png_pages", None)
                                if png_pages is not None:
                                    # A pasted Canva/Pitch link (see
                                    # app.py's own _fetch_pasted_link/
                                    # _PastedLinkFile) - extracted straight
                                    # from the ORIGINAL render pages, never
                                    # by re-rasterizing the temp file's own
                                    # assembled-PDF bytes back into images
                                    # (that would just be a slower, lossier
                                    # no-op round trip for identical
                                    # content - see extract_from_png_
                                    # pages's own docstring). page_links
                                    # (each page's own real <a href>
                                    # candidates) lets Gemini attribute a
                                    # genuine per-property brochure_link.
                                    #
                                    # Persisted here (from the bytes already
                                    # in memory, not the temp file about to
                                    # be deleted below) ONLY for this
                                    # branch's own validation/propagation
                                    # just below, which compares each row's
                                    # own brochure_link against this exact
                                    # persisted URL - no longer for
                                    # finalize_brochure_link's own PDF-
                                    # fallback default, which was removed
                                    # entirely (see extract.py's own
                                    # docstring on why) - a plain PDF
                                    # upload (the else branch below) has no
                                    # remaining use for this persisted copy
                                    # at all, so it's no longer computed
                                    # there.
                                    brochure_url = save_original_pdf(uploaded_file.getvalue(), uploaded_file.name)
                                    rows = extract.extract_from_png_pages(
                                        png_pages, original_filename=uploaded_file.name,
                                        page_links=uploaded_file.page_links,
                                    )
                                    # Gemini's own per-unit pick is trusted
                                    # by LABEL alone up to this point, never
                                    # by having actually been fetched - a
                                    # link genuinely labeled as the
                                    # brochure that turns out to be a
                                    # blocked/unreadable page must lose to
                                    # the working shared link instead of
                                    # silently staging a dead one.
                                    _validate_pasted_link_brochure_links(rows, brochure_url)
                                    # Gemini's own per-row attribution
                                    # sometimes only tags the FIRST row of
                                    # a multi-row same-building group
                                    # sharing one page, leaving the rest on
                                    # the shared fallback even though
                                    # they're unambiguously the same real
                                    # link - backfill that here, now that
                                    # each row's own pick has already been
                                    # validated above.
                                    _propagate_validated_links_within_page(
                                        rows, getattr(rows, "page_indices", None), brochure_url,
                                    )
                                else:
                                    rows = extract.extract(tmp_path, original_filename=uploaded_file.name)
                            elif suffix == ".eml":
                                rows = extract_email.extract(tmp_path, original_filename=uploaded_file.name)
                            else:
                                raise ValueError(f"Unsupported file type: {suffix}")
                        finally:
                            tmp_path.unlink(missing_ok=True)
                            tmp_path = None

                        geocode_rows(rows)
                        # Existing extracted provider/address values are
                        # never overwritten - see fill_missing_provider's own
                        # docstring for why PDF/email must never get either
                        # guess applied on top of Gemini's own judgment call.
                        fill_missing_provider(rows, uploaded_file.name, apply_filename_guess=False)
                        fill_missing_address_from_building(rows, apply_building_fallback=False)
                        reused = False

                    # Fixes known spelling/capitalization drift (e.g. Gemini's
                    # own extraction non-determinism on "Workplace Plus" vs
                    # "Workplace+" vs "WORKPLACE+") before these rows ever
                    # reach matching/diffing - see canonicalize_provider_name.
                    # Unconditional and idempotent, so it's safe to run once
                    # here regardless of which branch above produced rows.
                    canonicalize_providers(rows)

                    # Staged immediately, per file (reused or freshly
                    # extracted alike) - so a failure partway through a
                    # multi-file batch (a timeout, a dropped connection, a
                    # mid-request deploy) never loses a file that already
                    # finished. Previously all files were extracted first,
                    # geocode_rows() ran once over the combined batch, and
                    # save_staging_file() was called once at the very end -
                    # meaning an interruption at any point lost everything,
                    # even files that had already been fully processed.
                    staging_path = save_staging_file(
                        rows, uploaded_file.name, content_hash=content_hash,
                        fully_occupied_buildings=fully_occupied_buildings,
                        source_identity_hash=source_identity_hash,
                    )

                    # Computed here (never for a "reused but incomplete"
                    # resume, which never announced a row count of its own
                    # either) so _run_automatic_brochure_enrichment below
                    # can fold it into ITS OWN caption - confirming,
                    # immediately, that the row count is already real and
                    # saved, before any further (potentially slow) step
                    # runs, without a second, separate caption alongside it.
                    row_count = len(rows) if (is_spreadsheet_source or is_email_source) and not reused else None

                    # Automatic - for a fresh spreadsheet OR email extraction
                    # always, and ALSO for a reused (byte-identical previous
                    # upload) result whose own matched entry's enrichment
                    # was left incomplete (resume_already_processed is then
                    # non-None - see its own assignment above), so THIS
                    # staging entry continues that progress rather than
                    # staying frozen at it forever. A reused result whose
                    # match was already complete still skips this entirely -
                    # nothing left to do. Never for PDF (see brochure_
                    # enrichment.py's own module docstring on why that stays
                    # out of scope - already extracted from the actual
                    # brochure itself). Wrapped in its own try/except, on
                    # top of enrich_rows_grouped's own internal per-brochure
                    # exception handling - the base extraction above is
                    # ALREADY staged by this point, so an unexpected bug
                    # here must never surface as "extraction failed" for a
                    # file whose real extraction genuinely succeeded.
                    if (is_spreadsheet_source or is_email_source) and (not reused or resume_already_processed is not None):
                        try:
                            # See _reattempt_geocoding_for_newly_addressed_
                            # rows's own docstring - captured strictly
                            # BEFORE enrichment runs, since that's the only
                            # point "did this row already have an address"
                            # can still be answered.
                            pre_enrichment_geocode_state = _pre_enrichment_geocode_snapshot(rows)
                            rows = _run_automatic_brochure_enrichment(
                                rows, staging_path, already_processed=resume_already_processed,
                                floorplan_already_processed=resume_floorplan_already_processed,
                                special_features_matched=resume_special_features_matched,
                                row_count=row_count,
                            )
                            _reattempt_geocoding_for_newly_addressed_rows(rows, pre_enrichment_geocode_state)
                        except Exception as e:
                            st.warning(
                                f"{uploaded_file.name}: brochure enrichment hit an unexpected error "
                                f"({e}) and was skipped for this file — the extraction above is "
                                "unaffected and already staged."
                            )

                    succeeded += 1

                    st.session_state["recent_uploads"].insert(
                        0,
                        {
                            "filename": uploaded_file.name,
                            "n_rows": len(rows),
                            "staging_path": staging_path,
                            "reused": reused,
                            "timestamp": datetime.now(timezone.utc).astimezone(LONDON_TZ).strftime("%Y-%m-%d %H:%M %Z"),
                        },
                    )

            st.success(
                f"Extracted and staged {succeeded} of {total} file(s). "
                "Go to Review & Master to check them."
            )
        except QuotaExceededError:
            st.error(
                f"Daily extraction limit reached after {succeeded} of {total} file(s)."
                + (f" The first {succeeded} file(s) are already staged and ready to review." if succeeded else "")
                + " Try the rest again tomorrow."
            )
        except Exception as e:
            st.error(
                f"Extraction failed on file {succeeded + 1} of {total}: {e}"
                + (f" The first {succeeded} file(s) were already staged successfully." if succeeded else "")
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    if st.session_state["recent_uploads"]:
        st.divider()
        for entry in st.session_state["recent_uploads"]:
            reused_note = " — identical to a previous upload, reused rather than re-extracted" if entry.get("reused") else ""
            st.write(
                f"✅ **{entry['filename']}** — {entry['n_rows']} row(s), {entry['timestamp']}{reused_note}"
            )

    page_flow.render_nav_buttons("app.py")
