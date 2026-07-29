"""
display_utils.py

Display-only helpers for the Review/Master pages. These never affect what
gets written to the staging/master .xlsx files — only what's rendered.
"""

import pandas as pd
import streamlit as st

# Columns holding a URL - rendered as a clickable link with a fixed label
# instead of the raw URL (which would otherwise make the table unreadable).
LINK_COLUMNS = [
    "brochure_link",
]

RANGE_COLUMNS = [
    "size_sqft_min",
    "size_sqft_max",
    "desks_min",
    "rent_psf_min",
    "rent_psf_max",
    "rent_pcm_min",
    "rent_pcm_max",
]

# Unlike RANGE_COLUMNS (hidden only when every row is empty), these are
# internal/traceability-only columns that never belong in front of Mark/Laurie
# regardless of whether they have a value - still written to the underlying
# .xlsx (see write_rows_to_xlsx), just never rendered here.
ALWAYS_HIDDEN_COLUMNS = [
    "source_file",
]


def visible_columns(df: pd.DataFrame) -> list:
    """All of df's columns, minus RANGE_COLUMNS when no row has a value in any
    of them, minus ALWAYS_HIDDEN_COLUMNS unconditionally."""
    present = [c for c in RANGE_COLUMNS if c in df.columns]
    if present and df[present].notna().any().any():
        columns = list(df.columns)
    else:
        columns = [c for c in df.columns if c not in RANGE_COLUMNS]
    return [c for c in columns if c not in ALWAYS_HIDDEN_COLUMNS]


def link_column_config(df: pd.DataFrame) -> dict:
    """column_config for st.dataframe/st.data_editor: renders every column in
    LINK_COLUMNS that's actually present in df as a clickable link showing a
    short fixed label rather than the raw URL. Editing still works exactly as
    before (LinkColumn behaves like a text input when edited) - this only
    changes how a cell is displayed, not what's stored."""
    return {
        col: st.column_config.LinkColumn(display_text="Open brochure")
        for col in LINK_COLUMNS
        if col in df.columns
    }


def restore_hidden_columns(edited_df: pd.DataFrame, original_df: pd.DataFrame) -> pd.DataFrame:
    """
    Re-attach columns that were hidden from the data_editor (present in
    original_df but not in edited_df), aligning by index. Rows added in the
    editor have no corresponding row in original_df, so they get null values
    for those columns — there was no input for them to have been set through.
    """
    all_columns = list(original_df.columns)
    hidden = [c for c in all_columns if c not in edited_df.columns]
    if not hidden:
        return edited_df
    merged = edited_df.join(original_df[hidden], how="left")
    return merged[all_columns]
