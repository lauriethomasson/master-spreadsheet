import pandas as pd
import streamlit as st

import display_utils
import master_writer
import page_flow
import page_setup
from storage.file_store import (
    dataframe_to_listing_rows,
    list_pending_staging_files,
    load_staging_as_dataframe,
    mark_as_approved,
)

with page_setup.setup_page("review"):
    st.title("Review & Master Spreadsheet")

    pending = list_pending_staging_files()

    if pending:
        # New pending work supersedes any confirmation from a previous approval.
        st.session_state["just_approved"] = False

    edited_df = None
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
                    st.session_state["just_approved"] = True
                except Exception as e:
                    st.session_state["just_approved"] = False
                    st.error(f"Approval failed, master was not changed: {e}")

    if st.session_state.get("just_approved"):
        st.success("Approved — master spreadsheet updated.")
        if master_writer.master_exists():
            with open(master_writer.DEFAULT_MASTER_PATH, "rb") as f:
                st.download_button(
                    "Download master.xlsx",
                    f,
                    file_name="master.xlsx",
                    key="download_after_approve",
                )

    st.divider()
    with st.expander("View / download current master.xlsx"):
        if master_writer.master_exists():
            with st.spinner("Loading..."):
                df = master_writer.load_master_as_dataframe()
            st.dataframe(df[display_utils.visible_columns(df)])

            with open(master_writer.DEFAULT_MASTER_PATH, "rb") as f:
                st.download_button(
                    "Download master.xlsx",
                    f,
                    file_name="master.xlsx",
                    key="download_master_section",
                )

            log = master_writer.get_master_write_log()
            if log:
                last = log[-1]
                st.caption(f"Last updated: {last['timestamp']} — {last['row_count']} rows")
        else:
            st.info("No master spreadsheet yet — approve an upload to create one.")

    page_flow.render_nav_buttons("pages/2_Review_and_Master.py")
