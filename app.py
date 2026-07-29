import tempfile
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

import extract
import extract_email
import page_flow
import page_setup
from gemini_client import QuotaExceededError
from geocode import geocode_rows
from storage.file_store import save_staging_file

with page_setup.setup_page("upload"):
    st.title("Upload Brochure")

    st.session_state.setdefault("recent_uploads", [])

    uploaded_files = st.file_uploader(
        "Upload one or more PDF brochures or .eml emails",
        type=["pdf", "eml"],
        accept_multiple_files=True,
    )

    if uploaded_files and st.button("Extract"):
        with st.spinner("Extracting data from your file..."):
            tmp_path = None
            try:
                all_rows = []
                for uploaded_file in uploaded_files:
                    suffix = Path(uploaded_file.name).suffix.lower()

                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                        tmp.write(uploaded_file.getvalue())
                        tmp_path = Path(tmp.name)

                    try:
                        if suffix == ".pdf":
                            rows = extract.extract(tmp_path, original_filename=uploaded_file.name)
                        elif suffix == ".eml":
                            rows = extract_email.extract(tmp_path, original_filename=uploaded_file.name)
                        else:
                            raise ValueError(f"Unsupported file type: {suffix}")
                    finally:
                        tmp_path.unlink(missing_ok=True)
                        tmp_path = None

                    all_rows.extend(rows)

                geocode_rows(all_rows)

                if len(uploaded_files) == 1:
                    batch_name = uploaded_files[0].name
                else:
                    stems = "_".join(Path(f.name).stem for f in uploaded_files)
                    batch_name = stems if len(stems) < 60 else f"batch_of_{len(uploaded_files)}_files"

                staging_path = save_staging_file(all_rows, batch_name)

                st.session_state["recent_uploads"].insert(
                    0,
                    {
                        "batch_name": batch_name,
                        "n_files": len(uploaded_files),
                        "n_rows": len(all_rows),
                        "staging_path": staging_path,
                        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    },
                )

                st.success(
                    f"Extracted {len(all_rows)} rows from {len(uploaded_files)} file(s). "
                    "Go to Review & Master to check them."
                )
            except QuotaExceededError:
                st.error("Daily extraction limit reached. Try again tomorrow.")
            except Exception as e:
                st.error(f"Extraction failed: {e}")
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)

    if st.session_state["recent_uploads"]:
        st.divider()
        st.subheader("Uploaded this session")
        st.caption(
            "Stays visible across page navigation even though the file picker above "
            "resets — each of these has already been extracted and staged."
        )
        for entry in st.session_state["recent_uploads"]:
            st.write(
                f"✅ **{entry['batch_name']}** — {entry['n_rows']} row(s) from "
                f"{entry['n_files']} file(s), {entry['timestamp']}"
            )

    page_flow.render_nav_buttons("app.py")
