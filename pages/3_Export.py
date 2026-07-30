from io import BytesIO

import streamlit as st

import display_utils
import page_flow
import page_setup
from staging_writer import write_rows_to_xlsx
from storage.file_store import dataframe_to_listing_rows

with page_setup.setup_page("export"):
    st.title("Export Selected Properties")
    st.caption(
        "These are the properties selected on the Review & Master page. "
        "Make any last corrections, then download."
    )

    selected_df = st.session_state.get("export_selected_df")

    if selected_df is None or selected_df.empty:
        st.info(
            "No rows selected — go back to Review & Master, check the rows you "
            "want, then come back here."
        )
    else:
        selected_df = display_utils.sort_by_provider(selected_df)
        visible = display_utils.visible_columns(selected_df)
        edited_visible = st.data_editor(
            selected_df[visible],
            num_rows="fixed",
            width="stretch",
            column_config={
                **display_utils.link_column_config(selected_df[visible]),
                **display_utils.wide_text_column_config(selected_df[visible]),
            },
            key="export_editor",
        )
        edited_full = display_utils.restore_hidden_columns(edited_visible, selected_df)

        rows = dataframe_to_listing_rows(edited_full)

        buffer = BytesIO()
        write_rows_to_xlsx(rows, buffer)

        st.download_button(
            "Download export.xlsx",
            buffer.getvalue(),
            file_name="export.xlsx",
            key="download_export",
        )

    page_flow.render_nav_buttons("pages/3_Export.py")
