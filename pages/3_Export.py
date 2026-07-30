from io import BytesIO

import streamlit as st

import display_utils
import master_writer
import page_flow
import page_setup
from staging_writer import write_rows_to_xlsx
from storage.file_store import dataframe_to_listing_rows

with page_setup.setup_page("export"):
    st.title("Export Selected Properties")
    st.caption("Pick specific properties from the master spreadsheet and export just those to their own file.")

    if not master_writer.master_exists():
        st.info("No master spreadsheet yet — approve an upload first.")
    else:
        with st.spinner("Loading..."):
            master_df = master_writer.load_master_as_dataframe()

        visible = display_utils.visible_columns(master_df)
        display_df = master_df[visible].copy()
        display_df.insert(0, "Select", False)

        edited = st.data_editor(
            display_df,
            column_config={
                "Select": st.column_config.CheckboxColumn(required=True),
                **display_utils.link_column_config(display_df),
            },
            disabled=[c for c in display_df.columns if c != "Select"],
            width="stretch",
            height=600,
            key="export_selector",
        )

        selected_positions = edited.index[edited["Select"]].tolist()
        st.caption(f"{len(selected_positions)} of {len(master_df)} selected")

        # A stale export from a previous selection would be misleading to
        # hand out silently - clear it the moment the selection changes, so
        # the download button never lags behind what's actually checked.
        if selected_positions != st.session_state.get("export_selected_positions"):
            st.session_state.pop("export_bytes", None)
        st.session_state["export_selected_positions"] = selected_positions

        if st.button("Export selected", type="primary", disabled=not selected_positions):
            selected_full = master_df.loc[selected_positions]
            rows = dataframe_to_listing_rows(selected_full)
            buffer = BytesIO()
            write_rows_to_xlsx(rows, buffer)
            st.session_state["export_bytes"] = buffer.getvalue()
            st.session_state["export_count"] = len(rows)

        if st.session_state.get("export_bytes"):
            st.success(f"Ready — {st.session_state['export_count']} row(s).")
            st.download_button(
                "Download export.xlsx",
                st.session_state["export_bytes"],
                file_name="export.xlsx",
                key="download_export",
            )

    page_flow.render_nav_buttons("pages/3_Export.py")
