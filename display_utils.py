"""
display_utils.py

Display-only helpers for the Review/Master pages. These never affect what
gets written to the staging/master .xlsx files — only what's rendered.
"""

import re
from datetime import datetime, timezone
from urllib.parse import quote_plus
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
# and brochure_link_is_floorplan are both pipeline diagnostics (see their own
# schema.py docstrings) - same reasoning as staging_writer.HIDDEN_COLUMNS,
# which hides them from the exported file for the identical reason; kept as
# its own separate list here since this one governs the on-screen review
# grid, not the .xlsx.
# floorplan_link is a pure visibility change, not a data/logic one - the
# field, its data, and its own enrichment (FLOORPLAN_PROMPT etc. in
# brochure_enrichment.py) are all completely untouched; it's just no
# longer shown as its own column here.
ALWAYS_HIDDEN_COLUMNS = [
    "source_file",
    "property_id",
    "brochure_link_broken",
    "brochure_link_is_floorplan",
    "floorplan_link",
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
    short label rather than the raw URL. Editing still works exactly as
    before (LinkColumn behaves like a text input when edited) - this only
    changes how a cell is displayed, not what's stored.

    brochure_link is the one exception: its own display_text is a regex
    (see _DISPLAY_LABEL_CAPTURE_RE), not a fixed string, so it varies per
    row - see with_brochure_link_display_labels's own docstring for why
    and how. Every other LINK_COLUMNS entry (floorplan_link) keeps a
    single fixed label for the whole column, unchanged."""
    config = {}
    for col in LINK_COLUMNS:
        if col not in df.columns:
            continue
        display_text = _DISPLAY_LABEL_CAPTURE_RE if col == "brochure_link" else LINK_DISPLAY_TEXT.get(col, "Open link")
        config[col] = st.column_config.LinkColumn(label=title_case_label(col), display_text=display_text)
    return config


# brochure_link's own per-row display label, encoded as an extra query
# parameter on the URL itself (see with_brochure_link_display_labels) -
# never a separate column. st.column_config.LinkColumn's display_text
# cannot vary per row from a fixed string or a value in a DIFFERENT
# column, but it CAN be a regex capturing a piece of the URL's OWN text
# (confirmed directly against the installed Streamlit 1.60.0, via a real
# headless-browser render - not assumed: a genuine "?display_label=Open+
# brochure"/"&display_label=Broken+link" query value renders as real text
# with a real space, "+" and "%20" both correctly decoded by the
# frontend). This regex is deliberately anchored to "display_label="
# specifically (never a bare generic pattern) so it can never accidentally
# capture text from utm_content/utlId or any other real query parameter
# Canva's own share links already carry.
_DISPLAY_LABEL_PARAM = "display_label"
_DISPLAY_LABEL_CAPTURE_RE = r"[?&]display_label=([^&]+)"
_DISPLAY_LABEL_STRIP_RE = re.compile(r"[?&]display_label=[^&]*")

# Identical wording to the exported .xlsx (staging_writer.BROKEN_LINK_
# DISPLAY_TEXT, imported above, never a separate literal) - the on-screen
# grid and the downloaded file must always describe a confirmed-dead
# brochure_link the same way, never two independently-drifting terms for
# the same underlying brochure_link_broken fact.
BROCHURE_LINK_WORKING_LABEL = LINK_DISPLAY_TEXT.get("brochure_link", "Open brochure")
BROCHURE_LINK_BROKEN_LABEL = BROKEN_LINK_DISPLAY_TEXT
# Reuses floorplan_link's OWN existing label text (never a separate
# literal) for a row where brochure_link_is_floorplan is confirmed True -
# see that field's own schema.py docstring. A brochure_link that's really
# a floor plan under the hood must never read as "Open brochure", the
# same misleading-label problem BROCHURE_LINK_BROKEN_LABEL exists to avoid.
BROCHURE_LINK_FLOORPLAN_LABEL = LINK_DISPLAY_TEXT.get("floorplan_link", "Open floor plan")


def _with_display_label(url, label: str) -> str:
    """Appends `&display_label=<label>` (or `?display_label=<label>` if
    `url` has no query string at all yet) - urllib.parse.quote_plus so a
    space survives as the SAME "+" encoding confirmed to render correctly
    (see _DISPLAY_LABEL_CAPTURE_RE's own comment), not raw/unescaped."""
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{_DISPLAY_LABEL_PARAM}={quote_plus(label)}"


def with_brochure_link_display_labels(df: pd.DataFrame) -> pd.DataFrame:
    """
    Returns a COPY of df where every non-blank brochure_link value has a
    display_label query parameter appended - BROCHURE_LINK_BROKEN_LABEL for
    a row whose brochure_link_broken is confirmed True, else BROCHURE_LINK_
    FLOORPLAN_LABEL for a row whose brochure_link_is_floorplan is confirmed
    True, else BROCHURE_LINK_WORKING_LABEL (today's existing default) for
    every other row, UNCONDITIONALLY - never left blank/absent, even when
    neither column is present in df at all. That unconditional part matters:
    confirmed directly (real headless-browser render) that a URL with NO
    display_label param at all falls back to showing the RAW URL, not a
    blank cell or an error - a real regression from today's clean "Open
    brochure" label if any row were ever left untransformed. broken takes
    priority over is_floorplan when (in principle) both were ever true at
    once - a confirmed-dead link is the more urgent fact to surface.

    A no-op (returns df completely UNCHANGED, not even a copy) when
    brochure_link isn't a column in df at all - nothing to transform.

    Never touches the real, persisted brochure_link value - this is a
    display-only transformation of a COPY, exactly like the with_
    brochure_link_status helper this replaces. The one caller that also
    WRITES its own edited grid back out (pages/3_Export.py, via st.
    data_editor) must call strip_display_label on brochure_link before
    ever reaching dataframe_to_listing_rows - unlike the old synthetic-
    column approach, this transformation now lives INSIDE the real
    brochure_link column's own value, so it does not get dropped
    automatically the way a synthetic extra column did.
    """
    if "brochure_link" not in df.columns:
        return df
    df = df.copy()
    broken = df["brochure_link_broken"] if "brochure_link_broken" in df.columns else pd.Series(False, index=df.index)
    is_floorplan = (
        df["brochure_link_is_floorplan"] if "brochure_link_is_floorplan" in df.columns
        else pd.Series(False, index=df.index)
    )

    def _label_for(url, is_broken, is_floorplan_fallback):
        if not url or (isinstance(url, float) and pd.isna(url)):
            return url
        if is_broken is True:
            label = BROCHURE_LINK_BROKEN_LABEL
        elif is_floorplan_fallback is True:
            label = BROCHURE_LINK_FLOORPLAN_LABEL
        else:
            label = BROCHURE_LINK_WORKING_LABEL
        return _with_display_label(url, label)

    df["brochure_link"] = [
        _label_for(url, is_broken, is_floorplan_fallback)
        for url, is_broken, is_floorplan_fallback in zip(df["brochure_link"], broken, is_floorplan)
    ]
    return df


def strip_display_label(url):
    """Reverses _with_display_label - removes exactly the display_label
    query parameter with_brochure_link_display_labels added (wherever it
    sits, "?"- or "&"-prefixed), leaving everything else about `url`
    untouched. Safe to call on a value that never had one at all (a no-op)
    or on a genuinely reviewer-edited value (only ever strips OUR OWN
    marker, never touches a real edit to any other part of the URL).
    Blank/None passes through unchanged."""
    if not url or (isinstance(url, float) and pd.isna(url)):
        return url
    return _DISPLAY_LABEL_STRIP_RE.sub("", url, count=1)


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
