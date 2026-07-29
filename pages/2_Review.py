import streamlit as st

import display_utils
import master_writer
from storage.file_store import (
    dataframe_to_listing_rows,
    list_pending_staging_files,
    load_staging_as_dataframe,
    mark_as_approved,
    save_staging_dataframe,
)

st.set_page_config(page_title="Master Spreadsheet Pipeline", page_icon="📋", layout="wide")

st.title("Review Staged Uploads")

pending = list_pending_staging_files()

if not pending:
    st.info("No pending uploads to review.")
else:
    selected = st.selectbox("Choose an upload to review", pending)

    df = load_staging_as_dataframe(selected)
    visible = display_utils.visible_columns(df)
    edited_visible = st.data_editor(df[visible], num_rows="dynamic", width="stretch", height=600)
    edited_df = display_utils.restore_hidden_columns(edited_visible, df)

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Save edits"):
            save_staging_dataframe(selected, edited_df)
            st.success("Edits saved.")
    with col2:
        if st.button("Approve → Master", type="primary"):
            try:
                rows = dataframe_to_listing_rows(edited_df)
                master_writer.write_master(rows)
                mark_as_approved(selected)
                st.success("Master spreadsheet updated.")
            except Exception as e:
                st.error(f"Approval failed, master was not changed: {e}")
