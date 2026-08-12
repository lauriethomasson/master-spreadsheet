"""
display_utils.py

Display-only helpers for the Review/Master pages. These never affect what
gets written to the staging/master .xlsx files — only what's rendered.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from master_merge import field_kind, row_label  # noqa: F401 - row_label re-exported for display_utils.row_label(...) call sites
from schema import ListingRow
from staging_writer import LINK_DISPLAY_TEXT, title_case_label

LONDON_TZ = ZoneInfo("Europe/London")


def render_before_after(old_val, new_val) -> None:
    """
    Side-by-side Before/After display for one changed field's old and new
    value - a small muted "BEFORE" label over the old value, a small
    accent-colored "AFTER" label over the new value, each in its own
    bordered half-width box (st.columns(..., border=True), native to
    Streamlit - no custom CSS/HTML needed) so long free-text values
    (special_features, contacts) wrap onto as many lines as needed rather
    than being squished onto one, and old vs new stay directly comparable
    at a glance. Read-only - see render_before_after_editable for the
    manual per-field review UI, where "After" is an input, not static text.
    """
    old_col, after_col = st.columns(2, border=True)
    with old_col:
        st.caption("BEFORE")
        st.write("—" if old_val in (None, "") else old_val)
    with after_col:
        st.caption(":red[AFTER]")
        st.write("—" if new_val in (None, "") else new_val)


def render_before_after_editable(old_val, new_val, kind: str, key: str, multiline: bool = False):
    """
    Like render_before_after, but "After" is an editable input
    (st.number_input for an int/float field, st.text_area/st.text_input
    otherwise) rather than static text - used by the manual per-field
    review UI, where a reviewer can correct the incoming value, not just
    accept/reject it verbatim. Returns the input's current value (int/float
    per kind, or the text - None if left blank - for a str field).

    multiline should be True for long free-text fields (see
    display_utils.WIDE_TEXT_COLUMNS) - st.text_input is a single-line box
    that truncates rather than wraps, which would defeat the whole point of
    the side-by-side Before/After layout for exactly the fields (special_
    features, contacts) where comparing full text at a glance matters most.
    """
    old_col, after_col = st.columns(2, border=True)
    with old_col:
        st.caption("BEFORE")
        st.write("—" if old_val in (None, "") else old_val)
    with after_col:
        st.caption(":red[AFTER]")
        if kind in ("int", "float"):
            default = float(new_val) if new_val is not None else 0.0
            edited = st.number_input(
                "New value", value=default, step=(1.0 if kind == "int" else 0.01),
                key=key, label_visibility="collapsed",
            )
            return int(edited) if kind == "int" else edited
        if multiline:
            edited = st.text_area(
                "New value", value="" if new_val is None else str(new_val),
                key=key, label_visibility="collapsed",
            )
            return edited if edited != "" else None
        edited = st.text_input(
            "New value", value="" if new_val is None else str(new_val),
            key=key, label_visibility="collapsed",
        )
        return edited if edited != "" else None

# Columns holding a URL - rendered as a clickable link with a fixed label
# instead of the raw URL (which would otherwise make the table unreadable).
LINK_COLUMNS = [
    "brochure_link",
    "floorplan_link",
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
    "desks_min",
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


def label_column_config(df: pd.DataFrame) -> dict:
    """
    Base column_config giving every column in df a Title Case display label
    (e.g. "internal_ref" -> "Internal Ref") via title_case_label - purely
    cosmetic, the underlying column name a caller matches/edits by never
    changes. Meant to be spread into a column_config dict FIRST, ahead of
    link_column_config/wide_text_column_config/numeric_column_config below -
    each of those also sets its own label for the columns it covers, so
    spreading them afterward overrides this generic entry for those columns
    while still ending up with a real label everywhere.
    """
    return {col: st.column_config.Column(label=title_case_label(col)) for col in df.columns}


def link_column_config(df: pd.DataFrame) -> dict:
    """column_config for st.dataframe/st.data_editor: renders every column in
    LINK_COLUMNS that's actually present in df as a clickable link showing a
    short fixed label rather than the raw URL. Editing still works exactly as
    before (LinkColumn behaves like a text input when edited) - this only
    changes how a cell is displayed, not what's stored."""
    return {
        col: st.column_config.LinkColumn(
            label=title_case_label(col), display_text=LINK_DISPLAY_TEXT.get(col, "Open link"),
        )
        for col in LINK_COLUMNS
        if col in df.columns
    }


def wide_text_column_config(df: pd.DataFrame) -> dict:
    """column_config widening every column in WIDE_TEXT_COLUMNS that's
    present in df, so long descriptive text is genuinely more readable
    without scrolling - see WIDE_TEXT_COLUMNS. Very long values may still be
    visually truncated within the grid itself; the full text is always
    available by downloading the .xlsx (which has its own wrap-text/row-
    height formatting - see staging_writer.write_rows_to_xlsx)."""
    return {
        col: st.column_config.TextColumn(label=title_case_label(col), width="large")
        for col in WIDE_TEXT_COLUMNS
        if col in df.columns
    }


def numeric_column_config(df: pd.DataFrame) -> dict:
    """column_config forcing every int/float ListingRow field present in df
    into an explicit NumberColumn, for the Master default view's direct
    cell-editing grid - without this, an editable numeric column's edit
    widget depends on pandas' inferred dtype for that column, which silently
    degrades to a free-text input if inference doesn't land on a numeric
    dtype (e.g. a column that happens to be all-null this load). An explicit
    NumberColumn always gives a real numeric input and, on edit, a real
    Python int/float back - never a string needing separate parsing."""
    config = {}
    for col in df.columns:
        if col not in ListingRow.model_fields:
            continue
        kind = field_kind(col)
        if kind == "int":
            config[col] = st.column_config.NumberColumn(label=title_case_label(col), step=1)
        elif kind == "float":
            config[col] = st.column_config.NumberColumn(label=title_case_label(col), step=0.01)
    return config


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
