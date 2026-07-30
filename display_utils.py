"""
display_utils.py

Display-only helpers for the Review/Master pages. These never affect what
gets written to the staging/master .xlsx files — only what's rendered.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from master_merge import row_label  # noqa: F401 - re-exported for display_utils.row_label(...) call sites
from staging_writer import HYPERLINK_DISPLAY_TEXT

LONDON_TZ = ZoneInfo("Europe/London")

# Columns holding a URL - rendered as a clickable link with a fixed label
# instead of the raw URL (which would otherwise make the table unreadable).
LINK_COLUMNS = [
    "brochure_link",
]

# Free-text columns that regularly hold multi-sentence prose (special_features)
# or several people's contact details (contacts) - a plain data_editor grid
# truncates these to one line with no word-wrap, so they get an explicit
# wider column (see wide_text_column_config) plus the row-detail view below
# for the full untruncated text.
WIDE_TEXT_COLUMNS = [
    "special_features",
    "contacts",
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
    "property_id",
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
        col: st.column_config.LinkColumn(display_text=HYPERLINK_DISPLAY_TEXT)
        for col in LINK_COLUMNS
        if col in df.columns
    }


def wide_text_column_config(df: pd.DataFrame) -> dict:
    """column_config widening every column in WIDE_TEXT_COLUMNS that's
    present in df, so long descriptive text is genuinely more readable
    without scrolling - see WIDE_TEXT_COLUMNS. Pair with render_row_detail
    for the full untruncated text, since column width alone still can't
    word-wrap a multi-sentence value onto several lines."""
    return {
        col: st.column_config.TextColumn(width="large")
        for col in WIDE_TEXT_COLUMNS
        if col in df.columns
    }


def _blank(value) -> bool:
    """True for None and NaN - a bare `value or default` check gets this
    wrong for NaN, which is truthy in Python, so a dict built straight from
    a DataFrame row (e.g. via to_dict) needs this instead of `or`."""
    return value is None or (isinstance(value, float) and pd.isna(value))


def render_row_detail(df: pd.DataFrame, key: str) -> None:
    """
    Supplements a data_editor grid that truncates WIDE_TEXT_COLUMNS to one
    line with no word-wrap: lets a reviewer pick one row from a selectbox
    and see its complete special_features/contacts text, which the grid
    itself has no way to show without truncating.
    """
    if df.empty:
        return
    records = df.to_dict(orient="records")
    labels = [f"{i}: {row_label(record)}" for i, record in enumerate(records)]
    choice = st.selectbox("View full details for a row", labels, key=key)
    record = records[labels.index(choice)]
    with st.expander("Full text", expanded=True):
        for col in WIDE_TEXT_COLUMNS:
            if col not in df.columns:
                continue
            value = record.get(col)
            st.markdown(f"**{col}**")
            st.write("—" if _blank(value) else value)


def sort_by_provider(df: pd.DataFrame) -> pd.DataFrame:
    """
    Groups all rows from the same provider consecutively - stable (rows
    that already shared a provider keep their existing relative order) and
    case-insensitive (so "GPE" and "gpe" don't split into two groups).
    Rows with no provider at all sort last.
    """
    if "provider" not in df.columns or df.empty:
        return df
    return df.sort_values(
        by="provider",
        key=lambda col: col.str.lower(),
        na_position="last",
        kind="stable",
    ).reset_index(drop=True)


def to_london_display(stamp: str) -> str:
    """
    Parses a stored UTC timestamp - either the millisecond filename-safe
    stamp ("YYYY-MM-DD_HH-MM-SS-mmm", used for version filenames and
    master_write_log.jsonl entries) or a standard ISO 8601 string (the
    fallback log_master_write() uses when no explicit stamp is passed) -
    and formats it for display in Europe/London local time. Storage stays
    UTC throughout; this only ever affects what's shown to a person.
    Returns the raw string unchanged if it matches neither known format,
    rather than raising.
    """
    if not stamp:
        return stamp
    try:
        dt = datetime.strptime(stamp, "%Y-%m-%d_%H-%M-%S-%f").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            dt = datetime.fromisoformat(stamp)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return stamp
    return dt.astimezone(LONDON_TZ).strftime("%Y-%m-%d %H:%M %Z")


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
