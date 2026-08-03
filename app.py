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
    get_saved_header_mapping,
    load_staging_as_dataframe,
    save_header_mapping,
    save_original_pdf,
    save_staging_file,
)

SPREADSHEET_SUFFIXES = (".xlsx", ".csv")
_MAPPABLE_FIELDS = sorted(f for f in ListingRow.model_fields if f not in extract_spreadsheet.UNMAPPABLE_FIELDS)
_FIELD_OPTIONS = ["(ignore this column)"] + _MAPPABLE_FIELDS

# Increase this whenever extraction logic changes. This prevents results
# created by older extraction code from being reused.
EXTRACTION_VERSION = "4"


def fill_missing_provider(rows: list[ListingRow], fallback_provider: str = None) -> None:
    """Fill missing provider data without overwriting values from the source."""
    fallback_provider = (fallback_provider or "").strip() or None
    for row in rows:
        if not row.provider and fallback_provider:
            row.provider = fallback_provider

        if not row.internal_ref and row.provider:
            row.internal_ref = row.provider


with page_setup.setup_page("upload"):
    st.title("Upload Brochure")

    st.session_state.setdefault("recent_uploads", [])

    uploaded_files = st.file_uploader(
        "Upload one or more PDF brochures, .eml emails, or provider .xlsx/.csv spreadsheets",
        type=["pdf", "eml", "xlsx", "csv"],
        accept_multiple_files=True,
    )

    # Branding or provider columns can be unclear or incomplete. Let the
    # uploader supply a per-file fallback without guessing from filenames.
    provider_overrides = {}
    if uploaded_files:
        with st.expander("Provider overrides (optional)"):
            st.write(
                "Only fill these in when the provider cannot be identified from the file. "
                "An extracted provider always takes priority."
            )
            for uploaded_file in uploaded_files:
                provider_overrides[uploaded_file.name] = st.text_input(
                    f"Provider / internal ref — {uploaded_file.name}",
                    key=f"provider_override_{uploaded_file.name}",
                ).strip()

    # Spreadsheet uploads need a header->field mapping before they can be
    # extracted at all - unlike PDF/eml (always routed through Gemini),
    # there's no way to build a ListingRow from a spreadsheet row without
    # first knowing which column is which. Resolved (or, on first sight of a
    # given header set, confirmed by a human right here) BEFORE the Extract
    # button's loop below, since that loop has no good way to pause for
    # input mid-iteration. Keyed by header_hash, not per-file, so several
    # files sharing one provider's recurring format only need confirming
    # once - see storage.file_store.get_saved_header_mapping/save_header_mapping.
    unresolved_mappings = {}
    if uploaded_files:
        for uploaded_file in uploaded_files:
            suffix = Path(uploaded_file.name).suffix.lower()
            if suffix not in SPREADSHEET_SUFFIXES:
                continue
            df = extract_spreadsheet.read_spreadsheet(uploaded_file.getvalue(), suffix)
            headers = list(df.columns)
            h_hash = extract_spreadsheet.header_hash(headers)
            saved = get_saved_header_mapping(h_hash)
            mapping_has_provider_column = bool(
                saved and "provider" in saved.get("mapping", {}).values()
            )
            if saved is not None and (mapping_has_provider_column or saved.get("provider")):
                continue
            entry = unresolved_mappings.setdefault(
                h_hash,
                {"headers": headers, "filenames": [], "saved": saved},
            )
            entry["filenames"].append(uploaded_file.name)

    for h_hash, info in unresolved_mappings.items():
        with st.expander(f"Confirm column mapping — {', '.join(info['filenames'])}", expanded=True):
            st.write(
                "This spreadsheet's column headers haven't been seen before. Confirm which "
                "field each column belongs to (or leave it as **ignore** if it doesn't map to "
                "anything) - this is only asked once per header format; the same provider's "
                "future uploads with these exact headers will reuse this mapping automatically."
            )
            saved = info.get("saved") or {}
            guess = saved.get("mapping") or extract_spreadsheet.suggest_mapping(info["headers"])
            chosen = {}
            for header in info["headers"]:
                default = guess.get(header) or "(ignore this column)"
                default_index = _FIELD_OPTIONS.index(default) if default in _FIELD_OPTIONS else 0
                choice = st.selectbox(
                    str(header), _FIELD_OPTIONS, index=default_index, key=f"map_{h_hash}_{header}",
                )
                chosen[header] = None if choice == "(ignore this column)" else choice

            provider = st.text_input(
                "Provider / internal ref for this spreadsheet format",
                value=saved.get("provider") or "",
                key=f"provider_{h_hash}",
                help=(
                    "Saved with this column format and reused automatically for future uploads. "
                    "Leave blank only when a column above is mapped to provider."
                ),
            ).strip()

            mapped_fields = [f for f in chosen.values() if f]
            duplicate_fields = {f for f in mapped_fields if mapped_fields.count(f) > 1}
            if duplicate_fields:
                st.warning(f"Each field can only be mapped from one column - fix: {', '.join(sorted(duplicate_fields))}")
            elif "provider" not in mapped_fields and not provider:
                st.warning("Enter the provider, or map one of the spreadsheet columns to provider.")
            elif st.button("Confirm mapping", key=f"confirm_{h_hash}"):
                save_header_mapping(h_hash, info["headers"], chosen, provider=provider or None)
                st.rerun()

    if uploaded_files and st.button("Extract"):
        total = len(uploaded_files)
        succeeded = 0
        tmp_path = None
        try:
            for i, uploaded_file in enumerate(uploaded_files, start=1):
                with st.spinner(f"Processing {i} of {total}: {uploaded_file.name}..."):
                    suffix = Path(uploaded_file.name).suffix.lower()
                    source_provider = provider_overrides.get(uploaded_file.name)

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
                        # needed is the header->field mapping confirmed
                        # above, before this button could even be clicked.
                        df = extract_spreadsheet.read_spreadsheet(uploaded_file.getvalue(), suffix)
                        h_hash = extract_spreadsheet.header_hash(list(df.columns))
                        saved = get_saved_header_mapping(h_hash)
                        if saved is None:
                            raise ValueError(
                                f"{uploaded_file.name}: column mapping not confirmed yet - "
                                "confirm it above, then click Extract again."
                            )
                        rows = extract_spreadsheet.build_rows(df, saved["mapping"], source_file=uploaded_file.name)
                        source_provider = saved.get("provider") or source_provider
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
                    # Existing extracted provider values are not overwritten;
                    # internal_ref mirrors any provider when it is blank.
                    fill_missing_provider(rows, source_provider)
                    if any(not row.provider or not row.internal_ref for row in rows):
                        raise ValueError(
                            f"{uploaded_file.name}: provider could not be identified. "
                            "Enter a provider override above, then click Extract again."
                        )

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
