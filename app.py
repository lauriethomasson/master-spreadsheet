import tempfile
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

import extract
import extract_email
import page_flow
import page_setup
from display_utils import LONDON_TZ
from gemini_client import QuotaExceededError
from geocode import geocode_rows
from storage.file_store import save_original_pdf, save_staging_file

with page_setup.setup_page("upload"):
    st.title("Upload Brochure")

    st.session_state.setdefault("recent_uploads", [])

    uploaded_files = st.file_uploader(
        "Upload one or more PDF brochures or .eml emails",
        type=["pdf", "eml"],
        accept_multiple_files=True,
    )

    if uploaded_files and st.button("Extract"):
        total = len(uploaded_files)
        succeeded = 0
        tmp_path = None
        try:
            for i, uploaded_file in enumerate(uploaded_files, start=1):
                with st.spinner(f"Processing {i} of {total}: {uploaded_file.name}..."):
                    suffix = Path(uploaded_file.name).suffix.lower()

                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                        tmp.write(uploaded_file.getvalue())
                        tmp_path = Path(tmp.name)

                    try:
                        if suffix == ".pdf":
                            # Persisted before extraction (from the bytes we
                            # already have in memory, not the temp file about
                            # to be deleted below) so the PDF-fallback
                            # brochure_link rule has a real, permanent URL to
                            # point at instead of just the bare filename - see
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

                    # Geocoded and staged immediately, per file - so a failure
                    # partway through a multi-file batch (a timeout, a dropped
                    # connection, a mid-request deploy) never loses a file that
                    # already finished. Previously all files were extracted
                    # first, geocode_rows() ran once over the combined batch,
                    # and save_staging_file() was called once at the very end
                    # - meaning an interruption at any point lost everything,
                    # even files that had already been fully processed.
                    geocode_rows(rows)
                    staging_path = save_staging_file(rows, uploaded_file.name)
                    succeeded += 1

                    st.session_state["recent_uploads"].insert(
                        0,
                        {
                            "filename": uploaded_file.name,
                            "n_rows": len(rows),
                            "staging_path": staging_path,
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
            st.write(
                f"✅ **{entry['filename']}** — {entry['n_rows']} row(s), {entry['timestamp']}"
            )

    page_flow.render_nav_buttons("app.py")
