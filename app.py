import hashlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

import extract
import extract_email
import extract_spreadsheet
import page_flow
import page_setup
from display_utils import LONDON_TZ
from gemini_client import QuotaExceededError
from geocode import geocode_rows
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

# Increase this whenever extraction logic changes. This prevents results
# created by older extraction code from being reused.
EXTRACTION_VERSION = "3"


def fill_missing_provider(rows: list[ListingRow], filename: str) -> None:
    """
    Fill provider/internal_ref without overwriting extracted values -
    applies to PDFs, spreadsheets, emails and reused rows alike, since a
    filename-based guess is just as reasonable a fallback for any of them
    (see extract_spreadsheet.guess_provider_name). Supersedes an earlier,
    narrower infer_provider_from_filename that only recognized "kitt" as a
    hardcoded special case - guess_provider_name is the general version of
    exactly the same idea (strip boilerplate words/dates/parentheticals
    from the filename), not limited to one provider.

    guess_provider_name always returns a non-empty guess (falling back to
    the bare filename stem rather than blank) - unlike the old function,
    which returned None for anything that wasn't "kitt". That means a row
    Gemini genuinely left provider-less on purpose (a landlord-direct
    brochure with no presenting agent - see schema.ExtractedFields'
    provider/internal_ref comments) now gets a filename-derived guess
    filled in too, for PDF/email uploads specifically, not just
    spreadsheets. Applied here anyway, per explicit direction: a
    reasonable guess beats a blank field even for those sources.
    """
    fallback_provider = extract_spreadsheet.guess_provider_name(filename)

    for row in rows:
        if not row.provider:
            row.provider = fallback_provider

        if not row.internal_ref:
            row.internal_ref = row.provider or fallback_provider


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
        with st.expander(f"Missing required field(s) — {', '.join(info['filenames'])}", expanded=True):
            st.write(
                f"Couldn't automatically find a column for: **{', '.join(info['unresolved'])}**. Pick "
                "the column that holds each one, or confirm this format genuinely has none - this is "
                "only asked once per header format; the same provider's future uploads with these "
                "exact headers will reuse this answer automatically. Every other column still maps "
                "automatically either way."
            )
            options = [_NO_SUCH_COLUMN] + [str(h) for h in info["headers"]]
            assignments = {}
            for field in info["unresolved"]:
                choice = st.selectbox(f'Column for "{field}"', options, key=f"rescue_{h_hash}_{field}")
                assignments[field] = None if choice == _NO_SUCH_COLUMN else choice
            if assignments.get("building") is None:
                st.warning('Confirming "(no such column)" for building means this format will produce zero rows.')
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
                    # Include the extraction version in the hash. Therefore, identical files
                    # are reused only when they were processed by the current extraction logic.
                    file_bytes = uploaded_file.getvalue()
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
                    elif suffix in SPREADSHEET_SUFFIXES:
                        # No temp file, no Gemini call - a spreadsheet is
                        # already one row per property; the only thing
                        # needed is a header->field mapping, worked out
                        # automatically here (see suggest_mapping) with no
                        # per-column human confirmation step - a non-
                        # critical column suggest_mapping can't place is
                        # simply dropped. A CRITICAL field (building) is
                        # different - see the rescue block above, resolved
                        # (or confirmed genuinely absent) before this
                        # button could even be clicked.
                        df = extract_spreadsheet.read_spreadsheet(uploaded_file.getvalue(), suffix)
                        headers = list(df.columns)
                        h_hash = extract_spreadsheet.header_hash(headers)
                        mapping = extract_spreadsheet.suggest_mapping(headers)
                        saved_rescue = get_saved_critical_field_rescue(h_hash)
                        rescue = saved_rescue["assignments"] if saved_rescue else {}
                        mapping = extract_spreadsheet.apply_critical_field_rescue(mapping, rescue)
                        unresolved = extract_spreadsheet.unresolved_critical_fields(mapping, rescue)
                        if unresolved:
                            raise ValueError(
                                f"{uploaded_file.name}: missing required field(s) {', '.join(unresolved)} - "
                                "confirm the column above, then click Extract again."
                            )

                        rows = extract_spreadsheet.build_rows(df, mapping, source_file=uploaded_file.name)
                        geocode_rows(rows)
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
                        reused = False
                    # Applies to PDFs, spreadsheets, emails and reused rows.
                    # Existing extracted provider values are not overwritten.
                    fill_missing_provider(rows, uploaded_file.name)

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
