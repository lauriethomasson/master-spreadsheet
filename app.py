import streamlit as st

import page_setup

with page_setup.setup_page("home"):
    st.title("Master Spreadsheet Pipeline")
    st.write(
        "Use the sidebar to navigate:\n\n"
        "1. **Upload** — submit a PDF brochure or .eml email for extraction\n"
        "2. **Review** — check and edit extracted rows before approving\n"
        "3. **Master** — view and download the approved master spreadsheet"
    )
