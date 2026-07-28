import streamlit as st

import master_writer

st.title("Master Spreadsheet")

if master_writer.master_exists():
    df = master_writer.load_master_as_dataframe()
    st.dataframe(df)

    with open(master_writer.DEFAULT_MASTER_PATH, "rb") as f:
        st.download_button(
            "Download master.xlsx",
            f,
            file_name="master.xlsx",
        )

    log = master_writer.get_master_write_log()
    if log:
        last = log[-1]
        st.caption(f"Last updated: {last['timestamp']} — {last['row_count']} rows")
else:
    st.info("No master spreadsheet yet — approve an upload to create one.")
