import tempfile
from pathlib import Path

import streamlit as st

import extract
import extract_email
from gemini_client import QuotaExceededError
from geocode import geocode_rows
from storage.file_store import save_staging_file

st.title("Upload Brochure")

uploaded_file = st.file_uploader(
    "Upload a PDF brochure or .eml email",
    type=["pdf", "eml"],
)

if uploaded_file and st.button("Extract"):
    suffix = Path(uploaded_file.name).suffix.lower()

    with st.spinner("Extracting data..."):
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(uploaded_file.getvalue())
                tmp_path = Path(tmp.name)

            if suffix == ".pdf":
                rows = extract.extract(tmp_path)
            elif suffix == ".eml":
                rows = extract_email.extract(tmp_path)
            else:
                raise ValueError(f"Unsupported file type: {suffix}")

            geocode_rows(rows)
            save_staging_file(rows, uploaded_file.name)

            st.success(f"Extracted {len(rows)} rows. Go to Review to check them.")
        except QuotaExceededError:
            st.error("Daily extraction limit reached. Try again tomorrow.")
        except Exception as e:
            st.error(f"Extraction failed: {e}")
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
