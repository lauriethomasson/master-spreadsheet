import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import streamlit as st
from openpyxl import load_workbook

import extract
import extract_email
import extract_spreadsheet
import extract_spreadsheet_gemini
import page_flow
import page_setup
from display_utils import LONDON_TZ
from gemini_client import QuotaExceededError
from geocode import geocode_rows
from master_merge import canonicalize_providers
from schema import ListingRow
from storage.file_store import (
    dataframe_to_listing_rows,
    find_previous_upload_by_hash,
    get_saved_critical_field_rescue,
    get_staging_fully_occupied_buildings,
    load_staging_as_dataframe,
    save_original_pdf,
    save_staging_file,
)

SPREADSHEET_SUFFIXES = (".xlsx", ".csv")

# Increase this whenever PDF/email extraction logic changes. This prevents
# results created by older extraction code from being reused.
EXTRACTION_VERSION = "3"

# Column-header mapping (extract_spreadsheet.py) has no Gemini call and is
# fully deterministic - the only way its cached result could ever go stale
# is a change to that module's own mapping/guessing logic itself (suggest_
# mapping, guess_provider_name, FIELD_SYNONYMS, etc.), never anything
# EXTRACTION_VERSION above is meant to track. Confirmed to have actually
# gone stale this way: a real fix to that logic landed without EXTRACTION_
# VERSION being bumped (it has no reason to know spreadsheet logic even
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
_SPREADSHEET_LOGIC_FINGERPRINT = hashlib.sha256(
    Path(extract_spreadsheet.__file__).read_bytes() + Path(extract_spreadsheet_gemini.__file__).read_bytes()
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
    units = [{"building": r.building, "brochure_link": r.brochure_link} for r in rows]
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


with page_setup.setup_page("upload"):
    st.title("Upload Brochure")

    st.session_state.setdefault("recent_uploads", [])

    uploaded_files = st.file_uploader(
        "Upload one or more PDF brochures, .eml emails, or provider .xlsx/.csv spreadsheets",
        type=["pdf", "eml", "xlsx", "csv"],
        accept_multiple_files=True,
    )

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

    any_hidden_sheet_mentioned = False
    pending_decision_labels = []
    for file_hash, (filename, plans) in scanned_files.items():
        for plan in plans:
            classification = plan["classification"]
            if not classification:
                continue

            if classification["outcome"] == "auto_skip":
                any_hidden_sheet_mentioned = any_hidden_sheet_mentioned or classification["sheet_state"] in (
                    "hidden", "veryHidden",
                )
                st.info(f"Skipped hidden non-authoritative sheet: {plan['sheet_name']} ({filename})")
                with st.expander(f"Why was {plan['sheet_name']!r} skipped?"):
                    st.caption(HIDDEN_SHEET_EXPLAINER)
                    st.caption(
                        f"This sheet is {'hidden' if classification['sheet_state'] in ('hidden', 'veryHidden') else 'visible'} "
                        "in the workbook and does not contain the normal availability/download "
                        "structure, so it was not treated as current availability."
                    )

            elif classification["outcome"] == "ambiguous":
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
                    # PDF/email, EXTRACTION_VERSION (a human-maintained
                    # counter - see its own comment for why spreadsheets use
                    # a different mechanism); for a spreadsheet,
                    # _SPREADSHEET_LOGIC_FINGERPRINT instead (a hash of
                    # extract_spreadsheet.py's own source - automatic,
                    # nothing to remember to bump).
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
                    else:
                        versioned_content = (
                            EXTRACTION_VERSION.encode("utf-8")
                            + b"\0"
                            + file_bytes
                        )
                        content_hash = hashlib.sha256(versioned_content).hexdigest()

                    previous_staging_path = find_previous_upload_by_hash(content_hash)
                    fully_occupied_buildings = []

                    if previous_staging_path:
                        rows = dataframe_to_listing_rows(load_staging_as_dataframe(previous_staging_path))
                        reused = True
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
                        is_spreadsheet_source = suffix in SPREADSHEET_SUFFIXES
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
                                        f"{sheet_label}: hidden, non-authoritative rollup sheet — skipped."
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
                                                f"{sheet_label}: recognized a building, but nothing currently "
                                                "available — skipped."
                                            )
                                        else:
                                            st.info(f"{sheet_label}: no listing data recognized on this sheet — skipped.")
                                    else:
                                        _warn_if_extraction_looks_garbled(sheet_rows, sheet_label)
                                        _warn_if_units_look_undercounted(sheet_rows, ws, sheet_label)
                                        _warn_if_brochure_link_missing(sheet_rows, ws, sheet_label)
                                gemini_rows.extend(sheet_rows)
                            else:
                                sheet_rows = extract_spreadsheet.build_rows(df, mapping, source_file=sheet_label)
                                fill_missing_submarket_from_structural_header(sheet_rows, headers, uploaded_file.name)
                                header_mapped_rows.extend(sheet_rows)

                            rows.extend(sheet_rows)

                        sheet_progress.empty()
                        geocode_rows(rows)
                        # apply_filename_guess/apply_building_fallback=True
                        # only for header-mapped rows - a Gemini-extracted
                        # sheet has already made its own genuine judgment
                        # call about whether a provider/address genuinely
                        # exists (same reasoning as PDF/email, see both
                        # functions' own docstrings), so guessing on top of
                        # that would risk exactly the "misrepresenting a
                        # landlord-direct listing as agent-represented"
                        # problem those docstrings warn about.
                        fill_missing_provider(header_mapped_rows, uploaded_file.name, apply_filename_guess=True)
                        fill_missing_address_from_building(header_mapped_rows, apply_building_fallback=True)
                        reused = False
                    else:
                        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                            tmp.write(uploaded_file.getvalue())
                            tmp_path = Path(tmp.name)

                        try:
                            if suffix == ".pdf":
                                # Persisted before extraction (from the bytes
                                # we already have in memory, not the temp
                                # file about to be deleted below) so the
                                # PDF-fallback brochure_link rule has a real,
                                # permanent URL to point at instead of just
                                # the bare filename - see
                                # finalize_brochure_link's rule 3.
                                brochure_url = save_original_pdf(uploaded_file.getvalue(), uploaded_file.name)
                                rows = extract.extract(
                                    tmp_path, original_filename=uploaded_file.name, brochure_url=brochure_url
                                )
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
