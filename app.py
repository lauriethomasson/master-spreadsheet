import hashlib
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
    load_staging_as_dataframe,
    save_critical_field_rescue,
    save_original_pdf,
    save_staging_file,
)

SPREADSHEET_SUFFIXES = (".xlsx", ".csv")
_NO_SUCH_COLUMN = "(no such column)"

# Plain-language label for each extract_spreadsheet.CRITICAL_FIELDS entry,
# used in the rescue prompt's wording - falls back to the raw field name
# for anything not listed here, so a future CRITICAL_FIELDS addition still
# renders (awkwardly) rather than crashing.
_CRITICAL_FIELD_LABELS = {"building": "the building name"}

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


with page_setup.setup_page("upload"):
    st.title("Upload Brochure")

    st.session_state.setdefault("recent_uploads", [])

    uploaded_files = st.file_uploader(
        "Upload one or more PDF brochures, .eml emails, or provider .xlsx/.csv spreadsheets",
        type=["pdf", "eml", "xlsx", "csv"],
        accept_multiple_files=True,
    )

    # Column mapping itself is fully automatic (see suggest_mapping) - but a
    # genuinely CRITICAL field (extract_spreadsheet.CRITICAL_FIELDS) going
    # unmapped isn't safe to drop silently the way an ordinary column is:
    # building is schema-required, so a spreadsheet whose building column
    # goes unmapped produces ZERO rows outright, not just one blank field
    # per row (confirmed against two real UNION "by-area" export files,
    # whose building-name column is headered with the area's own name -
    # text that's different per file and shares no vocabulary with
    # "building" that any synonym or fuzzy match could ever catch).
    # Resolved (or, on first sight of a given header format still missing
    # one, confirmed by a human right here) BEFORE the Extract button's
    # loop below - every OTHER column keeps mapping automatically either
    # way, and this is only ever asked once per header format, not once per
    # file - see storage.file_store.get_saved_critical_field_rescue/
    # save_critical_field_rescue.
    pending_rescues = {}
    if uploaded_files:
        for uploaded_file in uploaded_files:
            suffix = Path(uploaded_file.name).suffix.lower()
            if suffix not in SPREADSHEET_SUFFIXES:
                continue
            df = extract_spreadsheet.read_spreadsheet(uploaded_file.getvalue(), suffix)
            headers = list(df.columns)
            h_hash = extract_spreadsheet.header_hash(headers)
            mapping = extract_spreadsheet.suggest_mapping(headers)
            # Before ever reaching a human rescue prompt: a known provider's
            # OWN recognizable format (e.g. UNION's "by-area" exports) may
            # structurally hide a critical field from any header-text-based
            # match at all - see apply_provider_structural_fallback's own
            # docstring. Runs first so a first-ever upload of a brand new
            # area never prompts in the first place, not just formats
            # already rescued once before.
            mapping = extract_spreadsheet.apply_provider_structural_fallback(mapping, headers, uploaded_file.name)
            saved_rescue = get_saved_critical_field_rescue(h_hash)
            rescue = saved_rescue["assignments"] if saved_rescue else {}
            mapping = extract_spreadsheet.apply_critical_field_rescue(mapping, rescue)
            unresolved = extract_spreadsheet.unresolved_critical_fields(mapping, rescue)
            if not unresolved:
                continue
            entry = pending_rescues.setdefault(
                h_hash, {"headers": headers, "filenames": [], "unresolved": unresolved}
            )
            entry["filenames"].append(uploaded_file.name)

    for h_hash, info in pending_rescues.items():
        # Plain-language, non-technical wording (still references the
        # actual unresolved field name(s) via _CRITICAL_FIELD_LABELS, in
        # case CRITICAL_FIELDS ever grows beyond just "building") - the
        # "different column layouts" phrasing is deliberately explicit
        # about what "asked again" is actually keyed on (this exact header
        # set, see header_hash), since "files shaped like this one" reads
        # ambiguously as "this provider's files in general" - a real UNION
        # export has a genuinely different header set per area (Clerkenwell
        # & Farringdon vs. Fitzrovia & Marylebone), each needing its own
        # one-time answer, not one answer for "UNION" as a whole.
        field_labels = " or ".join(_CRITICAL_FIELD_LABELS.get(f, f) for f in info["unresolved"])
        with st.expander(f"We need your help with one column — {', '.join(info['filenames'])}", expanded=True):
            st.write(
                f"We couldn't automatically figure out which column has {field_labels}. Please "
                "select it below, or let us know this file doesn't have one. Everything else was "
                "matched correctly. If this provider sends other files with different column "
                "layouts (for example, a separate export per area), you may be asked this again "
                "for each new layout — but you won't be asked again for this exact one."
            )
            options = [_NO_SUCH_COLUMN] + [str(h) for h in info["headers"]]
            assignments = {}
            for field in info["unresolved"]:
                choice = st.selectbox(f'Column for "{field}"', options, key=f"rescue_{h_hash}_{field}")
                assignments[field] = None if choice == _NO_SUCH_COLUMN else choice
            if assignments.get("building") is None:
                st.warning(
                    'Confirming "(no such column)" for building means this file will be sent to '
                    "AI-based extraction instead of column-mapping (see extract_spreadsheet_gemini.py) - "
                    "slower and with a small ongoing cost per upload, but able to handle a layout with "
                    "no single building column at all."
                )
            if st.button("Confirm", key=f"confirm_rescue_{h_hash}"):
                save_critical_field_rescue(h_hash, info["headers"], assignments)
                st.rerun()

    if uploaded_files and st.button("Extract"):
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
                    if suffix in SPREADSHEET_SUFFIXES:
                        content_hash = hashlib.sha256(
                            _SPREADSHEET_LOGIC_FINGERPRINT.encode("utf-8") + b"\0" + file_bytes
                        ).hexdigest()
                    else:
                        versioned_content = (
                            EXTRACTION_VERSION.encode("utf-8")
                            + b"\0"
                            + file_bytes
                        )
                        content_hash = hashlib.sha256(versioned_content).hexdigest()

                    previous_staging_path = find_previous_upload_by_hash(content_hash)

                    if previous_staging_path:
                        rows = dataframe_to_listing_rows(load_staging_as_dataframe(previous_staging_path))
                        reused = True
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
                        wb_for_gemini = None  # lazily opened - most files never need it
                        sheet_names = extract_spreadsheet.list_sheet_names(uploaded_file.getvalue(), suffix)
                        # A multi-sheet file where every sheet needs the
                        # Gemini fallback (confirmed against the real
                        # Copthall Estates file: 6 sheets, ~150s total) gives
                        # no feedback at all for minutes at a time otherwise -
                        # the outer st.spinner's own text is fixed for the
                        # whole file, not updated per sheet.
                        sheet_progress = st.empty()
                        for sheet_idx, sheet_name in enumerate(sheet_names, start=1):
                            if len(sheet_names) > 1:
                                sheet_progress.caption(
                                    f"Reading sheet {sheet_idx} of {len(sheet_names)}: {sheet_name}..."
                                )
                            df = extract_spreadsheet.read_spreadsheet(
                                uploaded_file.getvalue(), suffix, sheet_name=sheet_name
                            )
                            headers = list(df.columns)
                            h_hash = extract_spreadsheet.header_hash(headers)
                            mapping = extract_spreadsheet.suggest_mapping(headers)
                            # Same fallback applied at upload time above (see
                            # its comment there) - must run here too, or a
                            # format that fallback resolves (suppressing the
                            # rescue prompt) would still fall back to Gemini
                            # unnecessarily the moment Extract is clicked.
                            mapping = extract_spreadsheet.apply_provider_structural_fallback(
                                mapping, headers, uploaded_file.name
                            )
                            saved_rescue = get_saved_critical_field_rescue(h_hash)
                            rescue = saved_rescue["assignments"] if saved_rescue else {}
                            mapping = extract_spreadsheet.apply_critical_field_rescue(mapping, rescue)
                            unresolved = extract_spreadsheet.unresolved_critical_fields(mapping, rescue)

                            sheet_label = f"{uploaded_file.name} — {sheet_name}" if sheet_name else uploaded_file.name

                            if unresolved:
                                if wb_for_gemini is None:
                                    wb_for_gemini = load_workbook(BytesIO(uploaded_file.getvalue()), data_only=True)
                                ws = wb_for_gemini[sheet_name] if sheet_name else wb_for_gemini.active
                                sheet_rows = extract_spreadsheet_gemini.extract_sheet(
                                    ws, sheet_label, uploaded_file.name
                                )
                                if not sheet_rows:
                                    st.info(f"{sheet_label}: no listing data recognized on this sheet — skipped.")
                                else:
                                    _warn_if_extraction_looks_garbled(sheet_rows, sheet_label)
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
                    staging_path = save_staging_file(rows, uploaded_file.name, content_hash=content_hash)
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
