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
from staging_writer import BROKEN_LINK_DISPLAY_TEXT, LINK_DISPLAY_TEXT, title_case_label

LONDON_TZ = ZoneInfo("Europe/London")

# Friendly display labels for the fields a reviewer sees most often in a
# before/after diff - overrides title_case_label's own generic mechanical
# conversion only where that would read awkwardly ("Address 1", "Rent
# Pcm", "Desks Min") or is genuinely ambiguous out of context ("Size").
# Every other field (postcode, submarket, special_features, ...) already
# reads fine via title_case_label and has no entry here. The underlying
# schema field name is never changed - purely cosmetic (see this module's
# own docstring).
_FRIENDLY_FIELD_LABELS = {
    "address_1": "Address",
    "size_sqft": "Size",
    "rent_pcm": "Rent PCM",
    "rent_psf": "Rent PSF",
    "floor_unit": "Floor / Unit",
    "desks_min": "Minimum desks",
    "desks_max": "Maximum desks",
}


def friendly_field_label(field: str) -> str:
    """The display label for `field` in a before/after diff - see
    _FRIENDLY_FIELD_LABELS for the specific overrides; title_case_label's
    own generic conversion otherwise."""
    return _FRIENDLY_FIELD_LABELS.get(field, title_case_label(field))


def coerced_new_value(new_val, kind: str):
    """
    `new_val` coerced exactly the way render_new_value_input's own widget
    would return it if a reviewer never touched it - int()/float() per
    kind (falling back to 0/0.0 for a None new_val, mirroring that
    widget's own st.number_input default), or the string unchanged (empty
    string normalized to None, matching st.text_input's own "blank means
    nothing entered" convention there). Used ONLY for a field that's safe
    enough to skip rendering its own widget entirely (see pages/2_Review_
    and_Master.py's _render_field_rows) - this guarantees the value
    actually applied is byte-identical to what the always-rendered widget
    would have applied by default, never a shortcut that silently changes
    behavior.
    """
    if kind == "int":
        return int(new_val) if new_val is not None else 0
    if kind == "float":
        return float(new_val) if new_val is not None else 0.0
    return new_val if new_val != "" else None


def format_field_value_for_display(field: str, value) -> str:
    """
    Plain-English formatting for `field`'s own `value` in a compact
    before/after row - a comma-grouped desk count, a "sq ft" suffix for
    size, a "£" prefix for rent - purely cosmetic (see this module's own
    docstring: never changes what's actually stored/applied). "—" for a
    blank value, matching this page's existing before/after convention
    elsewhere. Falls back to a plain str() for any field/value shape this
    doesn't specifically know how to format (including a value that
    doesn't parse as a number despite the field normally being numeric -
    never raises just because a display nicety doesn't apply).
    """
    if value is None or value == "":
        return "—"
    try:
        if field == "size_sqft":
            return f"{float(value):,.0f} sq ft"
        if field in ("rent_pcm", "rent_psf"):
            amount = float(value)
            return f"£{amount:,.0f}" if amount == int(amount) else f"£{amount:,.2f}"
        if field in ("desks_min", "desks_max"):
            return f"{int(value):,}"
    except (TypeError, ValueError):
        pass
    return str(value)


def render_compact_before_after_row(field: str, old_val, new_val) -> None:
    """
    One compact, read-only "Field: before → after" line - the non-
    editable counterpart to render_new_value_input's own compact row (see
    pages/2_Review_and_Master.py's _render_field_rows), used wherever a
    diff is shown purely for confirmation (already-
    applied automatic updates, a post-approval summary, a manual cell-edit
    confirmation) rather than a decision still to be made. Deliberately no
    bordered box/bare caption pair - see render_before_after's own
    docstring for the large-card layout this replaces.
    """
    old_display = format_field_value_for_display(field, old_val)
    new_display = format_field_value_for_display(field, new_val)
    st.write(f"{friendly_field_label(field)}: {old_display} → {new_display}")


def render_before_after(old_val, new_val) -> None:
    """
    Side-by-side Before/After display for one changed field's old and new
    value - a small muted "BEFORE" label over the old value, a small
    accent-colored "AFTER" label over the new value, each in its own
    bordered half-width box (st.columns(..., border=True), native to
    Streamlit - no custom CSS/HTML needed) so long free-text values
    (special_features, contacts) wrap onto as many lines as needed rather
    than being squished onto one, and old vs new stay directly comparable
    at a glance. Read-only - see render_new_value_input for the manual
    per-field review UI's own editable "After" input, and render_compact_
    before_after_row for a compact read-only alternative to this function.
    """
    old_col, after_col = st.columns(2, border=True)
    with old_col:
        st.caption("BEFORE")
        st.write("—" if old_val in (None, "") else old_val)
    with after_col:
        st.caption(":red[AFTER]")
        st.write("—" if new_val in (None, "") else new_val)


def render_new_value_input(new_val, kind: str, key: str, multiline: bool = False):
    """
    The editable "new value" widget alone for one field's manual review
    row - st.number_input for an int/float field, st.text_area/st.
    text_input otherwise - used by the Review page's own compact field-row
    layout (see pages/2_Review_and_Master.py's _render_field_rows, which
    lays out the field's own label/reason, formatted "before" value, and
    Apply checkbox as sibling columns around this one) - never wrapped in
    its own bordered box/column pair (see render_before_after's own
    docstring for the large-card layout this widget-only helper replaces).
    A reviewer can correct the incoming value here, not just accept/reject
    it verbatim - returns the input's current value (int/float per kind,
    or the text - None if left blank - for a str field).

    multiline should be True for long free-text fields (see
    display_utils.WIDE_TEXT_COLUMNS) - st.text_input is a single-line box
    that truncates rather than wraps, which would defeat the whole point of
    comparing full text at a glance for exactly the fields (special_
    features, contacts) where that matters most.
    """
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
# .xlsx (see write_rows_to_xlsx), just never rendered here. brochure_link_broken
# is pipeline diagnostics (see ListingRow.brochure_link_broken's own schema.py
# docstring) - same reasoning as staging_writer.HIDDEN_COLUMNS, which hides it
# from the exported file for the identical reason; kept as its own separate
# list here since this one governs the on-screen review grid, not the .xlsx.
ALWAYS_HIDDEN_COLUMNS = [
    "source_file",
    "property_id",
    "brochure_link_broken",
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


# Synthetic column name for with_brochure_link_status/link_status_column_
# config below - never a real ListingRow field, so it can never collide with
# one, and restore_hidden_columns (see pages/3_Export.py) already drops any
# column not present in its own original_df, which this deliberately never
# is - no explicit strip-before-dataframe_to_listing_rows step needed there.
BROCHURE_LINK_STATUS_COLUMN = "brochure_link_status"

# Identical wording to the exported .xlsx (staging_writer.BROKEN_LINK_
# DISPLAY_TEXT, imported above, never a separate literal) - the on-screen
# grid and the downloaded file must always describe a confirmed-dead
# brochure_link the same way, never two independently-drifting terms for
# the same underlying brochure_link_broken fact. The "⚠️" prefix is purely
# this page's OWN display convention (every other "needs attention" signal
# already on this page uses it - see e.g. the document-issues caption), so
# it's added here only, never folded into the shared .xlsx-facing constant,
# which has no use for an emoji glyph.
BROCHURE_LINK_BROKEN_LABEL = f"⚠️ {BROKEN_LINK_DISPLAY_TEXT}"


def with_brochure_link_status(df: pd.DataFrame) -> pd.DataFrame:
    """
    Returns a COPY of df with one extra, synthetic, display-only column -
    BROCHURE_LINK_STATUS_COLUMN - inserted immediately after brochure_link
    (or appended at the end if brochure_link isn't present in df for some
    reason), showing BROCHURE_LINK_BROKEN_LABEL for a row whose brochure_
    link_broken is confirmed True, blank for False/None/anything else.

    Exists because st.column_config.LinkColumn cannot vary its own
    display_text per row - confirmed directly against the installed
    Streamlit's own LinkColumn docs: display_text is fixed once for the
    whole column, or a regex capture group extracted from the URL itself,
    never independent literal text keyed off a DIFFERENT column's value.
    brochure_link itself is therefore left completely untouched by this -
    always "Open brochure", always clickable, for every row, identical to
    today - this new column is what actually carries the distinction,
    mirroring staging_writer.write_rows_to_xlsx's own "Option A" choice
    (see BROKEN_LINK_DISPLAY_TEXT's own comment there) of never touching
    the real link, only ever changing what communicates its state.

    A no-op (returns df completely UNCHANGED, not even a copy) when
    brochure_link_broken isn't a column in df at all - same guard style as
    link_column_config/wide_text_column_config's own "if col in df.columns"
    checks, e.g. a caller that already narrowed to visible_columns(df)
    before this runs (ALWAYS_HIDDEN_COLUMNS already strips brochure_link_
    broken - see that list's own comment), so callers must run this BEFORE
    visible_columns, not after (visible_columns itself never strips this
    new synthetic column - it isn't in ALWAYS_HIDDEN_COLUMNS - so it
    survives that narrowing intact once added).
    """
    if "brochure_link_broken" not in df.columns:
        return df
    df = df.copy()
    status = df["brochure_link_broken"].apply(lambda broken: BROCHURE_LINK_BROKEN_LABEL if broken is True else "")
    if "brochure_link" in df.columns:
        df.insert(df.columns.get_loc("brochure_link") + 1, BROCHURE_LINK_STATUS_COLUMN, status)
    else:
        df[BROCHURE_LINK_STATUS_COLUMN] = status
    return df


def link_status_column_config(df: pd.DataFrame) -> dict:
    """column_config for the synthetic BROCHURE_LINK_STATUS_COLUMN added by
    with_brochure_link_status - disabled=True (read-only) even inside an
    editable st.data_editor grid (see pages/3_Export.py), since this column
    is always a computed display artifact, never real row data a reviewer
    edits. A no-op dict (same "if col in df.columns" guard as link_column_
    config) when that column was never added to this particular df."""
    if BROCHURE_LINK_STATUS_COLUMN not in df.columns:
        return {}
    return {
        BROCHURE_LINK_STATUS_COLUMN: st.column_config.TextColumn(
            label="Brochure Link Status", disabled=True, width="small",
        ),
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
