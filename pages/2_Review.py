import pandas as pd
import streamlit as st

import display_utils
import master_writer
import page_setup
from storage.file_store import (
    dataframe_to_listing_rows,
    list_pending_staging_files,
    load_staging_as_dataframe,
    mark_as_approved,
)

with page_setup.setup_page("review"):
    st.title("Review Staged Uploads")

    pending = list_pending_staging_files()

    if not pending:
        st.info("No pending uploads to review.")
    else:
        with st.spinner("Loading..."):
            combined_df = pd.concat(
                [load_staging_as_dataframe(path) for path in pending], ignore_index=True
            )
        st.caption(f"{len(pending)} pending upload(s) combined into {len(combined_df)} rows.")

        visible = display_utils.visible_columns(combined_df)
        edited_visible = st.data_editor(combined_df[visible], num_rows="fixed", width="stretch", height=600)
        edited_df = display_utils.restore_hidden_columns(edited_visible, combined_df)

        if st.button("Approve → Master", type="primary"):
            with st.spinner("Updating master spreadsheet..."):
                try:
                    rows = dataframe_to_listing_rows(edited_df)
                    master_writer.write_master(rows)
                    for path in pending:
                        mark_as_approved(path)
                    st.success("Master spreadsheet updated.")
                except Exception as e:
                    st.error(f"Approval failed, master was not changed: {e}")
