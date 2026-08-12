import hashlib
import uuid

import pandas as pd
import streamlit as st

import brochure_enrichment
import display_utils
import master_merge
import master_writer
import page_flow
import page_setup
from schema import ListingRow
from storage import blob_store
from storage.file_store import (
    active_and_superseded_staging_files,
    clean_value,
    dataframe_to_listing_rows,
    discard_pending_staging_files,
    get_staging_enrichment_summary,
    get_staging_filename,
    get_staging_fully_occupied_buildings,
    get_staging_row_count,
    list_pending_staging_files,
    load_staging_as_dataframe,
    mark_as_approved,
)


def _empty_master_df() -> pd.DataFrame:
    return pd.DataFrame(columns=list(ListingRow.model_fields.keys()))


def _render_field_rows(diffs: dict, key_prefix: str, default_checked: bool, risky_fields: frozenset = frozenset()) -> dict:
    """
    Renders one line per changed field: name, old value, editable new value,
    and its OWN apply/skip checkbox - field-level, not row-level, so a
    reviewer can accept some of a row's changes and reject others rather
    than an all-or-nothing choice for the whole row. Returns only the
    fields whose checkbox is checked, using whatever value is currently
    entered (letting a reviewer correct a value, not just accept/reject it).

    risky_fields (see master_merge.is_detail_loss) starts unchecked
    regardless of default_checked, and gets an explicit warning - the point
    is a reviewer has to notice and opt in, not just uncheck something that
    would otherwise apply silently.
    """
    approved = {}
    for f, (old_val, new_val) in diffs.items():
        is_risky = f in risky_fields
        st.markdown(f"**{f}**")
        kind = master_merge.field_kind(f)
        value = display_utils.render_before_after_editable(
            old_val, new_val, kind, key=f"{key_prefix}_{f}_value", multiline=f in display_utils.WIDE_TEXT_COLUMNS,
        )
        apply_field = st.checkbox(
            "Apply this change", value=default_checked and not is_risky, key=f"{key_prefix}_{f}_apply",
        )
        if is_risky:
            st.caption(
                "⚠️ This update looks like it may be missing information from the current record — "
                "review carefully before applying."
            )
        if apply_field:
            approved[f] = value
        st.divider()
    return approved


def _render_merge_field_choice(field_name: str, values: list, labels: list, key: str):
    """
    One field's peer-value comparison for the intra-batch duplicate merge UI
    - same bordered side-by-side visual language as display_utils.
    render_before_after, generalized from 2 columns to however many pending
    rows are in the duplicate group, plus a radio to pick which source's
    value survives into the merged row. Only called when master_merge.
    merge_field_choice has already established the values genuinely differ
    - a field where every source agrees never reaches this function at all.
    """
    st.markdown(f"**{field_name}**")
    cols = st.columns(len(values), border=True)
    for col, label, val in zip(cols, labels, values):
        with col:
            st.caption(label.upper())
            st.write("—" if val in (None, "") else val)
    default_index = master_merge.default_merge_choice_index(values)
    choice_index = st.radio(
        "Keep value from:", range(len(values)), index=default_index,
        format_func=lambda i: labels[i], key=f"{key}_choice", horizontal=True,
    )
    st.divider()
    return values[choice_index]


def _render_intra_batch_duplicate_group(group: list, key_prefix: str, new_rows_final: list) -> None:
    """
    group: list[master_merge.UnmatchedRow] sharing the same dedup key (see
    master_merge._dedup_key) - the same real property uploaded more than
    once in this batch, each copy independently failing to match master.
    Defaults to treating them as the same property (merge), since that's
    what a shared match key means by construction - but never silently
    forces it: an explicit "these are different" escape hatch stays
    available in case the match key coincidentally collided.

    On merge, every field where the sources agree (see merge_field_choice)
    carries straight through with no choice needed; a field where they
    genuinely differ gets its own radio (see _render_merge_field_choice).
    Confirming produces exactly ONE new ListingRow with a fresh property_id
    - never two, and never a value silently discarded without the reviewer
    seeing it was an option.
    """
    dicts = [u.new_row.model_dump() for u in group]
    labels = [d.get("source_file") or f"Row {i + 1}" for i, d in enumerate(dicts)]

    with st.expander(
        f"⚠️ Possible duplicate — {len(group)} rows look like the same property: "
        f"{display_utils.row_label(dicts[0])}",
        key=f"{key_prefix}_expander",
    ):
        same_property = st.radio(
            "Are these the same property?",
            ["Yes — merge into one property", "No — these are genuinely different properties"],
            key=f"{key_prefix}_same",
        )

        if same_property.startswith("Yes"):
            merged = {}
            for f in master_merge.DIFF_FIELDS:
                values = [d.get(f) for d in dicts]
                needs_choice, resolved = master_merge.merge_field_choice(values)
                merged[f] = (
                    _render_merge_field_choice(f, values, labels, f"{key_prefix}_{f}") if needs_choice else resolved
                )

            confirm_merge = st.checkbox(
                "Confirm — add as one merged property", value=True, key=f"{key_prefix}_confirm"
            )
            if confirm_merge:
                merged["property_id"] = str(uuid.uuid4())
                merged["source_file"] = " + ".join(labels)
                new_rows_final.append(ListingRow(**merged))
        else:
            for d in dicts:
                st.write(display_utils.row_label(d))
            new_rows_final.extend(
                u.new_row.model_copy(update={"property_id": str(uuid.uuid4())}) for u in group
            )


def _render_matched_row(m, key_prefix: str, prefix: str, default_checked: bool, updates: dict) -> None:
    """
    Single-row expander for an ordinary (non-collision, or a collision
    group that shrank to size 1 after its let-status member was pulled out
    - see the matched_changed loop below) matched-row diff - factored out
    of that loop so the rare group-of-one edge case can reuse it instead of
    duplicating the expander/checkbox rendering.
    """
    label = f"{prefix}{display_utils.row_label(m.new_row.model_dump())} — {len(m.diffs)} field(s) changed"
    with st.expander(label, key=f"{key_prefix}_expander"):
        approved_fields = _render_field_rows(
            m.diffs, key_prefix, default_checked=default_checked, risky_fields=m.risky_fields
        )
    if approved_fields:
        entry = updates.setdefault(m.master_index, {})
        entry.update(approved_fields)
        entry["source_file"] = m.new_row.source_file


def _render_collision_group(group: list, idx: int, plan, updates: dict, auto_accept: bool) -> None:
    """
    group: list[master_merge.MatchedRow] (len >= 2, non-let-status members
    only), all sharing one master_index (see master_merge.build_merge_
    plan's plan.collisions) - multiple rows in THIS SAME upload
    independently matched the same existing master property. Real reported
    case this replaces: two sheets of the same Copthall Estates workbook
    both extracting "Copthall House" - 4th Floor, with byte-identical
    values for all 6 changed fields - rendered as two separate, fully-
    expanded diff blocks, forcing the same 6 fields to be reviewed and
    approved twice.

    Compares the group's own proposed values against EACH OTHER first (see
    master_merge.matched_collision_field_choice) rather than diffing each
    member against master independently: a field every member agrees on
    (or where only one member has an opinion at all) is treated exactly
    like an ordinary non-colliding matched-row change - no manual click
    needed, silently auto-applied when auto_accept and nothing else about
    this group forces a look. Only a field where colliding sources
    genuinely disagree gets its own single decision, reusing the same
    merge-choice UI already built for unmatched_collisions' brand-new-
    property merge (see _render_merge_field_choice) - never one decision
    per field per member.

    A field is still flagged risky (see master_merge.is_detail_loss/
    is_richness_regression/house_number_changed) if ANY member's own diff
    triggered it - agreeing on a value doesn't make a detail-loss pattern
    any less worth a human's attention.
    """
    master_index = group[0].master_index
    old_rec = plan.master_records[master_index]
    dicts = [m.new_row.model_dump() for m in group]
    labels = [d.get("source_file") or f"Row {i + 1}" for i, d in enumerate(dicts)]
    risky_fields = frozenset().union(*(m.risky_fields for m in group))

    agree_diffs = {}    # {field: (old_val, resolved_val)} - agree, or only one has an opinion
    choice_fields = []  # [(field, values)] - genuine disagreement, needs a human pick
    for f in master_merge.collision_group_fields(group):
        values = [d.get(f) for d in dicts]
        needs_choice, resolved = master_merge.matched_collision_field_choice(values, f)
        if needs_choice:
            choice_fields.append((f, values))
        else:
            agree_diffs[f] = (old_rec.get(f), resolved)

    if auto_accept and not choice_fields and not risky_fields:
        entry = updates.setdefault(master_index, {})
        entry.update({f: v for f, (_, v) in agree_diffs.items()})
        entry["source_file"] = " + ".join(labels)
        return

    total_fields = len(agree_diffs) + len(choice_fields)
    label = f"⚠️ {display_utils.row_label(old_rec)} — {total_fields} field(s) changed ({len(group)} sources)"
    key_prefix = f"collision_{idx}_{master_index}"
    approved = {}
    with st.expander(label, key=f"{key_prefix}_expander"):
        if choice_fields:
            st.caption(
                f"{len(group)} rows in this upload matched this same existing property. "
                f"{len(choice_fields)} field(s) disagree between sources — pick which value is correct below; "
                "the rest already agree and are shown below that."
            )
            for f, values in choice_fields:
                old_val = old_rec.get(f)
                st.caption(f"Current master value: {'—' if old_val in (None, '') else old_val}")
                value = _render_merge_field_choice(f, values, labels, f"{key_prefix}_{f}")
                is_risky = f in risky_fields
                apply_field = st.checkbox(
                    "Apply this change", value=not is_risky, key=f"{key_prefix}_{f}_apply",
                )
                if is_risky:
                    st.caption(
                        "⚠️ This update looks like it may be missing information from the current record — "
                        "review carefully before applying."
                    )
                if apply_field:
                    approved[f] = value
                st.divider()
        if agree_diffs:
            approved.update(
                _render_field_rows(agree_diffs, f"{key_prefix}_agree", default_checked=True, risky_fields=risky_fields)
            )

    if approved:
        entry = updates.setdefault(master_index, {})
        entry.update(approved)
        entry["source_file"] = " + ".join(labels)


def _render_let_status_decision(m, key_prefix: str) -> str:
    """
    Prominently shown at the top of the "Needs your decision" section (see
    _render_pending_review) whenever a matched row's update contains
    wording suggesting the property is no longer available (see
    master_merge.mentions_let_status) - reframed as the actual property-
    level decision a reviewer needs to make, never exposed as a raw
    special_features/state_of_space before/after field diff: whether this
    property still belongs in master at all is a more fundamental question
    than "which field value to accept", so the whole row's update is
    presented as one status-change event, not a field to tick or skip.

    Three explicit choices, defaulting to "apply" (accept the new status,
    keep the property) if the reviewer never touches the radio and just
    clicks Approve - a status like "Under Offer" must NOT automatically
    mean remove; a reviewer who wants that has to say so explicitly.
    "Keep current information" is a genuine third option, distinct from
    "apply" - it leaves the existing master record completely untouched
    (this update contributes nothing at all for this row), for a reviewer
    who believes the status change isn't actually accurate/current, without
    forcing them into an all-or-nothing accept/remove choice.
    """
    label = display_utils.row_label(m.new_row.model_dump())
    provider = m.new_row.provider or "The latest update"
    # The new status text(s) that actually triggered this - see
    # LET_STATUS_FIELDS - shown verbatim, never invented; normally just one.
    status_text = "; ".join(m.diffs[f][1] for f in m.let_status_fields)

    st.warning(f"**{label}**\n\n{provider} now lists this space as **{status_text}**.")
    for f in m.let_status_fields:
        old_val, new_val = m.diffs[f]
        display_utils.render_before_after(old_val, new_val)

    choice = st.radio(
        "What would you like to do?",
        [
            f"Keep as {status_text} — keep the property in master and apply the new status.",
            "Remove property — remove it from master.",
            "Keep current information — ignore this status update and leave the existing record unchanged.",
        ],
        key=f"{key_prefix}_let_decision",
    )
    if choice.startswith("Remove property"):
        return "remove"
    if choice.startswith("Keep current information"):
        return "ignore"
    return "apply"


def _render_stale_candidate_decision(rec: dict, provider_label: str, key_prefix: str) -> str:
    """
    Like _render_let_status_decision, for a different signal - see
    master_merge.find_stale_candidates - a master row this upload's own
    provider gave real, scoped evidence is no longer available, rather than
    a matched row whose own wording says so. Defaults to "keep" (the
    non-destructive option) exactly like that function - a review trigger
    forcing a human to look, never a trap that silently deletes anything.
    """
    st.warning(
        f"🕳️ **{display_utils.row_label(rec)}** — no longer present in the latest "
        f"{provider_label} availability."
    )
    choice = st.radio(
        "What should happen to this property?",
        ["Keep in master", "Remove this property from master entirely"],
        key=f"{key_prefix}_stale_decision",
    )
    return "remove" if choice.startswith("Remove") else "keep"


# Search fields for the MAIN editable table's own search bar - unchanged
# from before this section had a name of its own (see _search_mask).
_MASTER_SEARCH_COLUMNS = ("building", "address_1", "provider", "floor_unit")

# Search fields for the Remove-rows expander's OWN, separate search bar -
# adds postcode on top of the master search's own fields, per real request:
# a duplicate-row cleanup is often easier to spot by postcode than by
# address text alone.
_REMOVAL_SEARCH_COLUMNS = ("provider", "building", "address_1", "floor_unit", "postcode")


def _search_mask(df: pd.DataFrame, query: str, columns: tuple) -> pd.Series:
    """Boolean mask, aligned to df's own index, of every row where ANY of
    `columns` contains `query` case-insensitively - shared by the Remove-
    rows and master-table search bars so the two stay behaviorally
    identical (same case-insensitive substring semantics) despite searching
    different field sets and feeding two completely independent widgets."""
    search_cols = [c for c in columns if c in df.columns]
    mask = pd.Series(False, index=df.index)
    for c in search_cols:
        mask = mask | df[c].fillna("").astype(str).str.contains(query.strip(), case=False)
    return mask


def _render_master_table(df: pd.DataFrame, key: str) -> bool:
    """
    Full master, browsable, directly editable, and row-selectable (for the
    export step and for bulk removal).

    Selection and editing are TWO INDEPENDENT WIDGETS, not one - see
    _render_row_selector below for why. Every column data_editor shows is a
    real edit target - hidden columns (property_id, source_file) are simply
    absent from display_df, so they can't be edited through this UI at all,
    same as before.

    Row selection is tracked by property_id in session_state, not by either
    widget's own positional state - a saved edit reloads the master (freshly
    re-sorted; see sort_by_provider), which can shift a row's position, and
    Streamlit's own widget state is keyed by position. Trusting that
    position across a reload risks silently reapplying a stale selection
    (or edit - see _process_manual_edits) to the wrong row. property_id is
    immune to re-sorting, so it survives that reload intact.

    The row-selector (search bar + selector table + selection status/Remove
    button) is rendered directly in the main table flow, always visible -
    NOT tucked inside a collapsed expander (a prior design; see git history
    for "Move row removal into a collapsed Remove rows expander" and this
    module's own now-updated test suite). Confirmed real regression report
    that reverted it: selection feeds the Export step just as much as it
    feeds removal, but hiding it behind a control literally labeled "Remove
    rows" made the export workflow look like it had disappeared entirely -
    a reviewer who only wants to export a few rows, never remove anything,
    had no reason to ever open that control. Selection and removal still
    each have their OWN search bar, over their OWN widget key - deliberately
    never shared, so searching to find a row to select/remove can never
    narrow what the main editable table shows, or vice versa (a real
    request: the two are different tasks a person does at different times,
    and conflating their search state was confusing) - this independence is
    unaffected by no longer being wrapped in an expander; only the
    visibility changed, not the underlying search/selection mechanism. Both
    still narrow only what's DISPLAYED to their own widget, never what df/
    master_records themselves contain - each widget's own positional state
    is always relative to whatever (possibly filtered) subset was actually
    passed to IT this render, so real_positions[i] (the real position in df
    of whatever row sits at the MASTER table's filtered position i) is
    threaded through to _process_manual_edits/build_manual_edit below, and
    row selection stays keyed by property_id exactly as it already was for
    the re-sort case above - a filter is just another way a row's position
    can shift out from under a stale positional reference.

    Returns True if a real field edit was saved this render - the caller
    should st.rerun() so the rest of the page reflects the fresh master
    (download button bytes, write-log caption, Version history) rather than
    the pre-edit snapshot already in hand.
    """
    visible = display_utils.visible_columns(df)
    display_df = df[visible].copy()

    removal_query = st.text_input("Search rows to select", key=f"{key}_removal_filter")
    removal_filtered_df = display_df
    if removal_query.strip():
        removal_filtered_df = display_df[_search_mask(df, removal_query, _REMOVAL_SEARCH_COLUMNS)]

    selected_positions = _render_row_selector(df, removal_filtered_df, key)
    st.session_state["export_selected_df"] = df.loc[selected_positions].reset_index(drop=True)
    _render_selection_actions(df, removal_filtered_df, selected_positions, key)

    st.divider()

    query = st.text_input("Search master spreadsheet", key=f"{key}_filter")
    filtered_df = display_df
    real_positions = list(range(len(df)))
    if query.strip():
        mask = _search_mask(df, query, _MASTER_SEARCH_COLUMNS)
        filtered_df = display_df[mask]
        real_positions = [i for i, keep in enumerate(mask) if keep]

    edited_df = st.data_editor(
        filtered_df,
        column_config={
            **display_utils.label_column_config(filtered_df),
            **display_utils.link_column_config(filtered_df),
            **display_utils.wide_text_column_config(filtered_df),
            **display_utils.numeric_column_config(filtered_df),
        },
        width="stretch",
        height=600,
        key=key,
    )
    st.caption(f"{len(edited_df)} of {len(df)} row(s) shown.")

    return _process_manual_edits(df, real_positions, key)


# The narrow, identifying column set _render_row_selector shows - enough to
# tell rows apart for a bulk-removal decision without duplicating the full
# editable table's own width. Deliberately not configurable per call site -
# every caller of _render_master_table wants the same at-a-glance identity.
_SELECTOR_COLUMNS = ("provider", "building", "floor_unit", "address_1")


def _selector_widget_key(df: pd.DataFrame, filtered_df: pd.DataFrame, key: str) -> str:
    """
    The removal selector's own st.dataframe widget key, suffixed with a
    short fingerprint of the property_id sequence CURRENTLY backing it
    (filtered_df's own row order, resolved to df's property_id column).

    Position (selection["selection"]["rows"], returned by that widget) is
    only ever meaningful relative to whatever exact rows/order it was
    rendered against - a stale widget instance (same key, but its
    frontend-cached selection state computed against an EARLIER df/
    filtered_df) has no reliable way to translate its remembered positions
    onto a genuinely different row set. Confirmed real failure modes this
    causes when the key stays constant across such a change: an out-of-
    range position after master shrinks (a removal - IndexError), and a
    stale row set/order lingering on screen after a removal until an
    unrelated rerun or a manual browser refresh happens to reset it.
    Folding this fingerprint into the key instead means Streamlit mounts a
    genuinely NEW widget instance - with no inherited frontend state at
    all - exactly when the row set or its order changes (a removal, an
    edit-triggered re-sort via sort_by_provider, a removal-search filter
    change), and _render_row_selector's own selection_default (computed
    fresh from export_selected_property_ids every render) becomes the
    widget's real starting state again rather than being silently ignored
    in favor of a carried-over one.

    Deliberately UNCHANGED across a rerun that doesn't alter which
    property_ids are visible or their order (typing in an unrelated
    widget elsewhere on the page, an unrelated button click) - remounting
    the widget on every such rerun would itself be a bug: a genuine
    in-flight browser click needs its target widget instance to survive
    from the click to the next rerun uninterrupted to be reliably
    recorded at all, and gratuitous remounts are exactly the kind of race
    a real user's light/quick trackpad tap can lose (see this module's
    prior, already-fixed report of a similar race - _render_row_selector's
    own docstring).
    """
    if "property_id" in df.columns:
        ids = tuple(df.loc[filtered_df.index, "property_id"])
    else:
        ids = tuple(filtered_df.index)
    fingerprint = hashlib.sha256(repr(ids).encode("utf-8")).hexdigest()[:16]
    return f"{key}_selector_{fingerprint}"


def _render_row_selector(df: pd.DataFrame, filtered_df: pd.DataFrame, key: str) -> list:
    """
    Renders a compact, READ-ONLY, natively row-selectable table (st.
    dataframe's own on_select/selection_mode, added specifically for this
    "select rows, then act on the selection" pattern) and returns
    selected_positions - real positions in df (immune to the Remove-rows
    expander's OWN search bar narrowing what filtered_df contains here -
    see _render_master_table's own docstring), kept in session_state[
    "export_selected_property_ids"] by property_id exactly as before.

    A real-browser report confirmed the "Remove N selected row(s)" button
    (see _render_selection_actions) sometimes needed two clicks to
    register. That was rigorously proven (via direct session_state
    manipulation reproducing both a single combined rerun and two genuinely
    sequential ones, in both the unfiltered AND filtered case) to NOT be a
    stale-state bug anywhere in this file's own logic - every scenario
    tested computed the correct selection given synchronized widget state.
    The remaining, unfixable-in-pure-Python explanation is a browser-level
    commit-timing race specific to an EDITABLE grid cell (the previous
    design: a manually-added "Select" CheckboxColumn living inside the SAME
    st.data_editor as real field edits) committing its own value to
    Streamlit's backend while racing against a separate st.button's click -
    something no amount of restructuring which session_state key gets read
    can fix, since the value genuinely hasn't arrived yet at the moment
    the race is lost. st.form() would fix this outright (it forces the
    browser to flush every contained widget atomically with the submit
    click) but wrapping the WHOLE data_editor in one would also defer
    ordinary field edits until an explicit submit, breaking today's
    auto-save-without-a-click behavior for those - not something this
    change may do.

    Splitting selection into its own widget, backed by a fundamentally
    different interaction (native row selection, never an in-grid EDIT at
    all) removes it from that specific race entirely, while leaving the
    real editable data_editor (and its auto-save behavior) completely
    untouched - the two widgets never share one commit pathway again.

    The widget's own key is fingerprinted on the CURRENT filtered_df's
    property_id sequence (see _selector_widget_key) rather than being a
    plain constant - see that function's own docstring for the stale-
    position IndexError/stuck-stale-row bugs this exists to prevent, and
    why it deliberately does NOT remount on every ordinary rerun. Even so,
    selected_row_positions is still defensively clamped to filtered_df's
    current length below - belt and braces alongside the key fix, never a
    substitute for it: this alone would silently avoid a crash without
    fixing the underlying "which row does this position actually mean"
    problem, so it exists purely to guarantee this function can never
    raise, not as the real fix.
    """
    selector_columns = [c for c in _SELECTOR_COLUMNS if c in filtered_df.columns]
    selector_df = filtered_df[selector_columns] if selector_columns else filtered_df

    selected_ids = st.session_state.get("export_selected_property_ids", set())
    default_rows = []
    if "property_id" in df.columns and selected_ids:
        filtered_property_ids = df.loc[filtered_df.index, "property_id"]
        default_rows = [i for i, pid in enumerate(filtered_property_ids) if pid in selected_ids]

    st.caption("Select rows (for export or removal):")
    selection = st.dataframe(
        selector_df,
        column_config=display_utils.label_column_config(selector_df),
        width="stretch",
        height=200,
        hide_index=True,
        on_select="rerun",
        selection_mode="multi-row",
        selection_default={"selection": {"rows": default_rows}},
        key=_selector_widget_key(df, filtered_df, key),
    )
    selected_row_positions = [p for p in selection["selection"]["rows"] if 0 <= p < len(filtered_df)]

    if "property_id" in df.columns:
        visible_ids = set(df.loc[filtered_df.index, "property_id"])
        now_selected_visible = set(df.loc[filtered_df.index[selected_row_positions], "property_id"])
        st.session_state["export_selected_property_ids"] = master_merge.merge_selected_property_ids(
            selected_ids, visible_ids, now_selected_visible
        )
        return df.index[df["property_id"].isin(st.session_state["export_selected_property_ids"])].tolist()
    return filtered_df.index[selected_row_positions].tolist()


def _clear_row_selection(df: pd.DataFrame, filtered_df: pd.DataFrame, key: str) -> None:
    """Shared by "Clear selection" and a successful removal - resets the
    tracked selection AND the selector widget's own stale state (otherwise
    its cached selection from before this click would just reapply itself
    on the next render, on top of the now-empty export_selected_property_
    ids, leaving rows looking still selected despite the tracked set
    genuinely being empty).

    filtered_df is whatever the CURRENT render just passed to
    _render_row_selector - needed to compute the exact same fingerprinted
    key that widget instance was actually mounted under (see
    _selector_widget_key), since a plain constant key no longer matches
    what's really in st.session_state. For the "Clear selection" case, df/
    filtered_df are unchanged, so this key is the same one the very next
    render will recompute and reuse - deleting it here is what makes that
    next render see a genuinely fresh widget rather than one whose cached
    positions still reflect the pre-clear selection. For the successful-
    removal case, master itself is about to shrink, so the NEXT render
    will compute a different fingerprint (and therefore a different key)
    regardless - deleting this now-orphaned entry is just hygiene, not
    load-bearing for that case, but costs nothing to do uniformly."""
    st.session_state["export_selected_property_ids"] = set()
    st.session_state["export_selected_df"] = df.iloc[0:0].reset_index(drop=True)
    selector_key = _selector_widget_key(df, filtered_df, key)
    if selector_key in st.session_state:
        del st.session_state[selector_key]


def _render_selection_actions(df: pd.DataFrame, filtered_df: pd.DataFrame, selected_positions: list, key: str) -> None:
    with st.container(horizontal=True):
        st.caption(f"{len(selected_positions)} of {len(df)} row(s) selected.")

        # Same st.switch_page(...) call page_flow.render_nav_buttons already
        # uses elsewhere in this app for page-to-page navigation - no new
        # mechanism, and no second export-selection state: this only ever
        # switches to a page that already reads export_selected_df/
        # export_selected_property_ids (see pages/3_Export.py), both of
        # which _render_row_selector above keeps current every render
        # regardless of whether this button is ever clicked - previously
        # the ONLY way there was navigating away manually (via the sidebar
        # or the Back/Next buttons at the page's own bottom) and hoping that
        # state was still populated, with no direct affordance for the
        # export workflow this selection exists to feed sitting next to the
        # selection itself.
        if st.button(
            "Export selected →", key=f"{key}_export_selected",
            disabled=not selected_positions, type="primary",
        ):
            st.switch_page("pages/3_Export.py")

        # Reuses the exact same apply_merge/write_master path a let-status
        # removal (during upload review, see removed_indices above) already
        # rides - same versioning/undo/write-log, just triggered directly
        # from the master table's own row selection instead of from an
        # upload-merge diff. Added for one-time duplicate cleanup (e.g. rows
        # left behind by a provider-name fix that changed the match key -
        # see master_merge.py's own module docstring on why provider is
        # part of the key at all) - no separate delete mechanism invented.
        remove_clicked = st.button(
            f"Remove {len(selected_positions)} selected row(s)",
            key=f"{key}_remove_selected",
        )

        if st.button("Clear selection", key=f"{key}_clear_selection", disabled=not selected_positions):
            _clear_row_selection(df, filtered_df, key)
            st.rerun()

        # Feedback for this action lives right here, inline in the same row
        # as the button, rather than as a separate banner above the table -
        # a removal is a small, frequent, low-ceremony action (unlike an
        # upload approval's own multi-row diff), so its own confirmation
        # stays equally lightweight rather than pushing the table down.
        if remove_clicked and not selected_positions:
            st.info("Select at least one row first.")
        elif remove_clicked:
            with st.spinner("Removing..."):
                master_records = [{k: clean_value(v) for k, v in rec.items()} for rec in df.to_dict(orient="records")]
                removed_indices = frozenset(selected_positions)

                # The version to offer for Undo is whatever was newest BEFORE
                # this write creates a new one - same reasoning as the approve/
                # manual-edit flows' own previous_version_path.
                previous_versions = master_writer.list_versions(limit=1)
                previous_version_path = previous_versions[0]["path"] if previous_versions else None

                merged_rows = master_merge.apply_merge(master_records, {}, [], removed_indices=removed_indices)
                try:
                    master_writer.write_master(
                        merged_rows, source="manual_removal", removed_count=len(removed_indices)
                    )
                except Exception as e:
                    write_failed = e
                else:
                    write_failed = None

            if write_failed:
                st.error(f"Removal failed, master was not changed: {write_failed}")
            else:
                _clear_row_selection(df, filtered_df, key)
                st.session_state["last_removal"] = {
                    "count": len(removed_indices),
                    "version_path": previous_version_path,
                }
                st.rerun()
        else:
            # Deliberately read, not popped: popping here would consume
            # last_removal on the very rerun that clicking "Undo" itself
            # triggers - by the time that rerun's script runs, last_removal
            # would already be gone, so the "if last_removal:" guard below
            # would be False, st.button("Undo", ...) would never even be
            # called THIS run, and the click landing on it would be
            # silently dropped (confirmed with a minimal repro - a button
            # whose own visibility depends on a pop()'d value can never be
            # clicked, since the pop already fired on the render that first
            # displayed it, one render before the click can land). Cleared
            # explicitly (not via pop-into-a-throwaway-local) only once
            # Undo has actually run, immediately below.
            last_removal = st.session_state.get("last_removal")
            if last_removal:
                n = last_removal["count"]
                st.markdown(f":red[✓ {n} row{'s' if n != 1 else ''} removed]")
                if last_removal.get("version_path") and st.button("Undo", key=f"{key}_undo_manual_removal"):
                    with st.spinner("Undoing..."):
                        master_writer.restore_version(last_removal["version_path"])
                    del st.session_state["last_removal"]
                    st.session_state["just_restored"] = last_removal["version_path"]
                    st.rerun()


def _process_manual_edits(df: pd.DataFrame, displayed_positions: list, key: str) -> bool:
    """
    Checks the data_editor's own edit-tracking state (st.session_state[key])
    for real field edits since it was last reset, and if there are any,
    saves them to master.xlsx immediately - see build_manual_edit for how
    the delta becomes a full row list. Returns True iff a save happened.

    displayed_positions[i] is the real position in df of whatever row sat
    at position i in the (possibly filtered) subset _render_master_table
    actually passed to data_editor this render - identity (0, 1, 2, ...)
    when its own filter is empty. edited_rows's own keys are always
    positions within THAT displayed subset, never directly meaningful
    against df/master_records once the filter has narrowed what's shown -
    see build_manual_edit's own handling of this same parameter for the
    real failure mode it prevents.

    Deliberately processes the WHOLE delta in one shot rather than assuming
    "one cell changed" - a multi-cell paste lands as several changed cells
    in a single edited_rows dict on one rerun, and that whole batch becomes
    exactly one save/one version, not one per cell. Combined with
    data_editor only committing a text edit on blur/Enter (never per
    keystroke), this is what keeps a burst of typing or a paste from
    spawning a version per character/cell.
    """
    state = st.session_state.get(key)
    edited_rows = state.get("edited_rows", {}) if state else {}
    if not edited_rows:
        return False

    master_records = [{k: clean_value(v) for k, v in rec.items()} for rec in df.to_dict(orient="records")]
    merged_rows, diff_rows, fields_changed = master_merge.build_manual_edit(
        master_records, edited_rows, displayed_positions=displayed_positions
    )
    if fields_changed == 0:
        return False  # only "Select" checkboxes changed this render - not a data edit

    # The version to offer for Undo is whatever was newest BEFORE this edit
    # writes a new one - same reasoning as the approve flow's previous_version_path.
    previous_versions = master_writer.list_versions(limit=1)
    previous_version_path = previous_versions[0]["path"] if previous_versions else None

    try:
        master_writer.write_master(merged_rows, source="manual_edit", fields_changed=fields_changed)
    except Exception as e:
        st.error(f"Edit failed, master was not changed: {e}")
        return False

    st.session_state["last_manual_edit"] = {
        "fields_changed": fields_changed,
        "diff_rows": diff_rows,
        "version_path": previous_version_path,
    }
    # Resets the widget's own edited_rows/added_rows/deleted_rows tracking -
    # the freshly-reloaded, re-sorted master on the next render is the sole
    # source of truth from here; reapplying this positional delta onto it
    # risks landing on the wrong row if this edit touched the sort key
    # itself (provider) and shifted row order. Select state isn't lost by
    # this - it's already been captured into export_selected_property_ids
    # (keyed by property_id, immune to the same reordering) above.
    del st.session_state[key]
    return True


def _render_approval_confirmation(approval: dict):
    updated_count = approval["updated_count"]
    new_count = approval["new_count"]
    removed_count = approval.get("removed_count", 0)
    parts = []
    if updated_count:
        parts.append(f"updated {updated_count} propert{'y' if updated_count == 1 else 'ies'}")
    if new_count:
        parts.append(f"added {new_count} new propert{'y' if new_count == 1 else 'ies'}")
    if removed_count:
        parts.append(f"removed {removed_count} propert{'y' if removed_count == 1 else 'ies'}")
    summary = ("Approved — " + " and ".join(parts) + ".") if parts else "Approved — no changes were applied."
    st.success(summary)

    # Removals get their own red/danger confirmation, always visible (not
    # gated behind "View what changed") - a property leaving master
    # entirely is a materially different, higher-stakes event than a
    # normal field update or a new property being added, and shouldn't be
    # lost in the same green "Approved" styling.
    for label in approval.get("removed_labels", []):
        st.error(f"Removed: {label}")

    show_details = st.session_state.get("show_approval_details", False)
    # horizontal=True sizes each button to its own content instead of
    # stretching across equal-width st.columns - that's what kept "Undo this
    # update" stranded far to the right of "View what changed" rather than
    # sitting right beside it.
    with st.container(horizontal=True):
        if st.button("Hide details" if show_details else "View what changed", key="toggle_approval_details"):
            st.session_state["show_approval_details"] = not show_details
            st.rerun()

        if approval.get("version_path"):
            if st.button("Undo this update", key="undo_last_approval"):
                with st.spinner("Undoing..."):
                    master_writer.restore_version(approval["version_path"])
                st.session_state.pop("last_approval", None)
                st.session_state["just_restored"] = approval["version_path"]
                st.rerun()

    if show_details:
        diff_rows = approval["diff_rows"]
        new_labels = approval["new_labels"]
        removed_labels = approval.get("removed_labels", [])
        if diff_rows or new_labels or removed_labels:
            for d in diff_rows:
                st.markdown(f"**{d['property']}** — {d['field']}")
                display_utils.render_before_after(d["old"], d["new"])
            for label in new_labels:
                st.write(f"🆕 {label} — new property")
            for label in removed_labels:
                st.write(f"🗑️ {label} — removed from master")
        else:
            st.caption("No field-level changes to show.")


def _render_manual_edit_confirmation(edit: dict):
    """
    Pinned above the table (called before _render_master_table) so it stays
    visible regardless of which row was edited - unlike the approval
    confirmation, this always shows its diff inline rather than behind a
    "View what changed" toggle, since there's normally just one or a
    handful of changed fields to show, not a whole batch of approvals.
    """
    n = edit["fields_changed"]
    st.success(f"Cell updated — {n} field{'s' if n != 1 else ''} changed.")

    if edit.get("version_path"):
        if st.button("Undo", key="undo_manual_edit"):
            with st.spinner("Undoing..."):
                master_writer.restore_version(edit["version_path"])
            st.session_state.pop("last_manual_edit", None)
            st.session_state["just_restored"] = edit["version_path"]
            st.rerun()

    for d in edit["diff_rows"]:
        st.markdown(f"**{d['property']}** — {d['field']}")
        display_utils.render_before_after(d["old"], d["new"])


def _render_full_master_view():
    # Popped, not just read - a flash confirmation shown once right after its
    # own write, same convention already used by just_discarded/just_restored
    # below. Previously these two used .get(), so nothing ever cleared them
    # except their own "Undo" button - session state is shared across every
    # page in this app, so a confirmation kept reappearing on every future
    # visit to this view, long after the write it described, with no
    # relation to what's actually happened since. (Removal's own equivalent
    # confirmation is rendered inline next to its button - see
    # _render_master_table - not as a banner here.)
    last_approval = st.session_state.pop("last_approval", None)
    if last_approval:
        _render_approval_confirmation(last_approval)

    last_manual_edit = st.session_state.pop("last_manual_edit", None)
    if last_manual_edit:
        _render_manual_edit_confirmation(last_manual_edit)

    if not master_writer.master_exists():
        st.info("No master spreadsheet yet — approve an upload to create one.")
        return

    with st.spinner("Loading..."):
        df = display_utils.sort_by_provider(master_writer.load_master_as_dataframe())

    if _render_master_table(df, key="master_table_default_view"):
        st.rerun()

    st.download_button(
        "Download master.xlsx",
        blob_store.read_bytes(master_writer.DEFAULT_MASTER_PATH),
        file_name="master.xlsx",
        # See pages/3_Export.py's download_button for why this is required -
        # without it, raw bytes always infer "application/octet-stream"
        # regardless of file_name's extension.
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="download_master_default_view",
    )

    log = master_writer.get_master_write_log()
    if log:
        last = log[-1]
        st.caption(
            f"Last updated: {display_utils.to_london_display(last['timestamp'])} — {last['row_count']} rows"
        )


def _render_discard_pending(pending: list, new_rows: list):
    """
    Lets a reviewer walk away from a pending batch entirely rather than
    being stuck reviewing something they never meant to act on (e.g. the
    wrong file was uploaded) - deliberately still whole-batch (discards
    every entry in `pending`, active and superseded alike): the "Approve ->
    Master" flow above is itself whole-batch (one diff/plan, one button),
    so "discard everything currently pending" stays available at that same
    granularity. A reviewer who wants to discard just ONE specific staging
    entry (e.g. an obsolete, superseded copy of a file also pending, or
    simply the wrong one of several unrelated uploads) uses the per-file
    "Discard this upload" button in the staging-management section instead
    (see _render_single_file_discard/_render_brochure_enrichment_summary) -
    that one, not this one, is what a real production report confirmed was
    genuinely missing: with 2+ pending uploads, this whole-batch action was
    previously the ONLY discard option at all, with no way to remove just
    one without losing every other pending upload too.

    Two-click confirm, same pattern as "Restore this version" in the
    Version history section below - this is a real, permanent deletion
    with no undo (unlike a master.xlsx change, which is always versioned),
    so a single click isn't enough.
    """
    if st.button("Discard this pending upload" if len(pending) == 1 else "Discard all pending uploads", key="discard_pending"):
        st.session_state["discard_pending_confirm"] = True

    if st.session_state.get("discard_pending_confirm"):
        n = len(new_rows)
        st.warning(
            f"Are you sure? This will permanently discard {n} pending propert{'y' if n == 1 else 'ies'} "
            f"across {len(pending)} file{'s' if len(pending) != 1 else ''} — no changes will be applied to "
            "master, and this cannot be undone (nothing was ever written to master.xlsx, so there's no "
            "version to restore)."
        )
        confirm_cols = st.columns(2)
        if confirm_cols[0].button("Confirm discard", key="discard_pending_confirm_btn", type="primary"):
            discard_pending_staging_files(pending)
            st.session_state.pop("discard_pending_confirm", None)
            st.session_state["just_discarded"] = n
            st.rerun()
        if confirm_cols[1].button("Cancel", key="discard_pending_cancel"):
            st.session_state.pop("discard_pending_confirm", None)
            st.rerun()
    st.divider()


def _render_master_lookup(master_df: pd.DataFrame) -> None:
    """
    Read-only, filterable view of the CURRENT master spreadsheet, rendered
    inside the pending-review screen itself - several of that screen's own
    decisions (the "possible near-miss" link-or-new choice, in particular)
    require comparing an incoming row against an existing master record,
    but _render_pending_review replaces _render_full_master_view entirely
    while anything is pending, so there was previously no way to look at
    master at all until the batch was approved or discarded.

    st.dataframe, never st.data_editor - genuinely read-only, no possible
    edit can reach master.xlsx through this view - under its own widget
    key namespace (pending_master_lookup_*) so it can never collide with
    _render_master_table's own data_editor state (key=
    "master_table_default_view") on a later, nothing-pending visit. Takes
    the SAME master_df _render_pending_review already loaded for
    build_merge_plan - no second load - and reads/writes no session state
    of its own beyond the filter text box, so this can never affect
    updates/new_rows_final/removed_indices or trigger master_writer.
    write_master().
    """
    with st.expander("🔍 View current master", expanded=False):
        if not master_writer.master_exists():
            st.info("No master spreadsheet yet — approve an upload to create one.")
            return

        query = st.text_input(
            "Filter (building, address, provider, or floor/unit)",
            key="pending_master_lookup_filter",
        )

        df = display_utils.sort_by_provider(master_df)
        if query.strip():
            search_cols = [c for c in ("building", "address_1", "provider", "floor_unit") if c in df.columns]
            mask = pd.Series(False, index=df.index)
            for c in search_cols:
                mask = mask | df[c].fillna("").astype(str).str.contains(query.strip(), case=False)
            df = df[mask]

        visible = display_utils.visible_columns(df)
        display_df = df[visible]
        st.dataframe(
            display_df,
            width="stretch",
            column_config={
                **display_utils.label_column_config(display_df),
                **display_utils.link_column_config(display_df),
                **display_utils.wide_text_column_config(display_df),
                **display_utils.numeric_column_config(display_df),
            },
            key="pending_master_lookup_table",
        )
        st.caption(f"{len(df)} of {len(master_df)} row(s) shown.")


def _render_single_file_discard(path: str) -> None:
    """
    A "Discard this upload" button (two-click confirm, same pattern as
    _render_discard_pending's own whole-batch action and "Restore this
    version" below) that targets EXACTLY this one staging path - never the
    whole `pending` list. discard_pending_staging_files(paths) itself
    already only ever deletes exactly the paths it's given; the real
    production bug this fixes is upstream of that function entirely - the
    OLD UI never called it with anything narrower than the full pending
    list, so a reviewer with 2+ pending uploads had no way to discard just
    one (see this module's own real report: an incomplete run and a
    completed run of the SAME source file, both pending, and clicking the
    only discard button available would have deleted both).

    Session-state keys are suffixed with `path` itself (a real, unique
    staging path, e.g. "staging/20260811_..._UNION.xlsx") - never the
    filename, which two entries here can share - so confirming discard on
    one entry can never leave a stale confirm flag armed against a
    DIFFERENT entry, and clicking Confirm always targets whichever single
    path this specific render call was for.
    """
    confirm_key = f"discard_single_confirm_{path}"
    if st.button("Discard this upload", key=f"discard_single_{path}"):
        st.session_state[confirm_key] = True

    if st.session_state.get(confirm_key):
        st.warning(
            "Are you sure? This permanently discards ONLY this one staging entry — no changes will be "
            "applied to master, and this cannot be undone (nothing was ever written to master.xlsx, so "
            "there's no version to restore)."
        )
        confirm_cols = st.columns(2)
        if confirm_cols[0].button("Confirm discard", key=f"discard_single_confirm_btn_{path}", type="primary"):
            n = get_staging_row_count(path)
            discard_pending_staging_files([path])
            st.session_state.pop(confirm_key, None)
            st.session_state["just_discarded"] = n
            st.rerun()
        if confirm_cols[1].button("Cancel", key=f"discard_single_cancel_{path}"):
            st.session_state.pop(confirm_key, None)
            st.rerun()


def _render_brochure_enrichment_summary(pending: list, superseded: list = ()) -> None:
    """
    Staging management: one block per pending upload - filename, its OWN
    row count, its OWN brochure-enrichment status, and its OWN individually-
    targeted Discard button (see _render_single_file_discard) - so two
    entries that happen to share a filename (the real, motivating case: the
    same source workbook re-uploaded while an earlier run's enrichment was
    still incomplete, leaving one "30/126" entry and one "126/126" entry
    both pending under the identical name) are never confused with each
    other, and discarding one can never accidentally remove the other, or
    master rows, or a DIFFERENT entry's own enrichment metadata.

    superseded (see active_and_superseded_staging_files, computed once by
    _render_pending_review and passed to both this function and the merge
    plan above) marks entries whose rows were excluded from that merge
    plan and from the counts above it - not hidden, just labeled, since the
    reviewer must still be able to find and explicitly discard the
    leftover copy rather than have it silently vanish or silently keep
    contributing duplicate rows forever.

    get_staging_enrichment_summary returns None for a file enrichment never
    touched at all (no eligible rows, or an upload predating this
    feature) - shown with no enrichment caption at all in that case, same
    as before this staging-management section existed, just still gets its
    own identity/row-count line and Discard button now.

    stats["status"] == "in_progress" means the run that wrote this never
    reached its own final set_staging_enrichment_summary call - an
    interruption (killed process, crashed Cloud Run instance, cancelled
    Streamlit rerun) partway through (see set_staging_enrichment_progress's
    own docstring). Surfaced as a prominent warning PLUS a "Continue
    enrichment" button, never the same quiet caption a finished run gets:
    this file's rows are a genuine mix of enriched and never-attempted, and
    a blank special_features/state_of_space cell here is NOT confirmation
    the brochure had nothing - it may simply not have been checked yet.
    Clicking Continue resumes brochure_enrichment.run_brochure_enrichment
    with already_processed=stats["processed_urls"] (see enrich_rows_
    grouped's own docstring) - already-"ok" brochures are never re-fetched/
    re-sent to Gemini, so this is genuinely a resume, not a restart. Not an
    automatic retry loop: it only ever runs again when this exact button is
    clicked, exactly once per click. Re-uploading the identical file
    instead would NOT help it directly (see app.py's own content-hash
    dedup and its own "continue the matched entry's progress" handling),
    so Continue here remains the explicit recovery action.
    """
    superseded_set = set(superseded)
    for path in pending:
        filename = get_staging_filename(path)
        n_rows = get_staging_row_count(path)
        stats = get_staging_enrichment_summary(path)

        with st.container(border=True):
            label = f"**{filename}** — {n_rows} row(s)"
            if path in superseded_set:
                label += " — _superseded by a more complete copy of this same file, pending above_"
            st.markdown(label)

            if stats and stats.get("status") == "in_progress":
                # max(0, ...) - a resumed run only ever has a lower-bound
                # guess of unique_floorplans_considered until the floorplan
                # pass itself actually starts (see run_brochure_enrichment's
                # own docstring) - never let a stale/optimistic guess make
                # this go negative. .get(..., 0) - an entry written before
                # the floorplan pass existed at all has neither key.
                brochures_remaining = max(
                    0, stats["unique_brochures_considered"] - stats["brochures_done"],
                )
                floorplans_remaining = max(
                    0, stats.get("unique_floorplans_considered", 0) - stats.get("floorplans_done", 0),
                )
                # "0 remaining" must mean there is genuinely no required
                # enrichment work left - never just "0 brochures remain
                # while the floorplan pass hasn't even started yet" (see
                # brochure_enrichment.run_brochure_enrichment's own
                # docstring on why status stays "in_progress" until BOTH
                # passes are done).
                remaining = brochures_remaining + floorplans_remaining
                warning_text = (
                    f"⚠️ Brochure enrichment incomplete: {stats['brochures_done']}/"
                    f"{stats['unique_brochures_considered']} unique brochure(s) checked before this run "
                    "stopped (the app was likely interrupted or restarted mid-run). Blank descriptive "
                    f"fields on this file's rows may simply be unchecked, not confirmed blank."
                )
                if stats.get("unique_floorplans_considered"):
                    warning_text += (
                        f" {stats.get('floorplans_done', 0)}/{stats['unique_floorplans_considered']} "
                        "unique floor plan(s) also checked before this run stopped."
                    )
                warning_text += f" {remaining} enrichment source(s) remain."
                st.warning(warning_text)
                if st.button(f"Continue enrichment ({remaining} remaining)", key=f"continue_enrichment_{path}"):
                    rows = dataframe_to_listing_rows(load_staging_as_dataframe(path))
                    with st.spinner("Resuming brochure enrichment..."):
                        brochure_enrichment.run_brochure_enrichment(
                            rows, path, already_processed=stats["processed_urls"],
                            floorplan_already_processed=stats.get("floorplan_processed_urls", {}),
                        )
                    st.rerun()
            elif stats:
                summary = (
                    f"Brochure enrichment: Complete — {stats['unique_brochures_considered']}/"
                    f"{stats['unique_brochures_considered']} checked, {stats['rows_enriched']} row(s) enriched."
                )
                if stats["brochures_unavailable"]:
                    summary += f" {stats['brochures_unavailable']} brochure(s) could not be processed."
                if stats.get("unique_floorplans_considered"):
                    summary += (
                        f" {stats['unique_floorplans_considered']} unique floor plan(s) also checked."
                    )
                st.caption(summary)

            _render_single_file_discard(path)


def _render_pending_review(pending: list):
    # Splits `pending` into active vs superseded BEFORE anything else reads
    # rows from it - see active_and_superseded_staging_files' own docstring
    # for the real problem this exists to prevent: the SAME source file
    # re-uploaded (byte-identical, hence a shared content_hash) while an
    # earlier run's brochure enrichment was still incomplete leaves TWO
    # staging entries for one real upload. Only `active` (the more
    # enrichment-complete member of each content_hash group, or the lone
    # member of a group of one) ever contributes rows to the merge plan or
    # counts below - a `superseded` entry is still a completely normal
    # member of `pending` for every other purpose (staging management's
    # own per-file listing/discard button, see _render_brochure_
    # enrichment_summary), it just never gets to double-count its own
    # building/floor rows against its more-complete twin.
    active, superseded = active_and_superseded_staging_files(pending)

    with st.spinner("Loading..."):
        combined_df = display_utils.sort_by_provider(pd.concat(
            [load_staging_as_dataframe(path) for path in active], ignore_index=True
        ))
        new_rows = dataframe_to_listing_rows(combined_df)
        master_df = master_writer.load_master_as_dataframe() if master_writer.master_exists() else _empty_master_df()
        plan = master_merge.build_merge_plan(new_rows, master_df)
        # Intra-batch duplicates (see build_merge_plan's own unmatched_
        # collisions) are auto-merged here, BEFORE any of the rendering
        # below - manual review becomes the exception (a genuine field
        # conflict, see master_merge.consolidate_unmatched_duplicates's own
        # docstring), not the default just because a property happened to
        # be extracted more than once. total_unmatched_before is captured
        # for the summary line below, since plan.unmatched itself shrinks
        # once safe groups collapse into one row each.
        total_unmatched_before = len(plan.unmatched)
        plan = master_merge.consolidate_unmatched_duplicates(plan)
        fully_occupied_buildings = [
            fo for path in active for fo in get_staging_fully_occupied_buildings(path)
        ]

    st.caption(master_merge.pending_status_line(len(active), plan))
    auto_consolidated_rows = total_unmatched_before - len(plan.unmatched)
    if auto_consolidated_rows or plan.unmatched_collisions:
        st.caption(
            f"{total_unmatched_before} extracted row(s) with no existing master match — "
            f"{auto_consolidated_rows} duplicate row(s) automatically consolidated, "
            f"{len(plan.unmatched_collisions)} conflict(s) need your review, "
            f"{len(plan.unmatched)} unique row(s) ready."
        )
    if superseded:
        st.caption(
            f"{len(superseded)} pending staging file(s) are an earlier/less-complete processing run of "
            "a file also pending above and are excluded from these counts — see below to discard them."
        )
    _render_master_lookup(master_df)
    _render_brochure_enrichment_summary(pending, superseded)

    colliding_changed_ids = {id(m) for group in plan.collisions for m in group}
    colliding_unmatched_ids = {id(u) for group in plan.unmatched_collisions for u in group}

    # See master_merge.is_detail_loss - a matched row whose special_features/
    # contacts update looks like it dropped real information needs a manual
    # look rather than being auto-appliable, exactly like a same-batch
    # collision.
    risky_changed_ids = {id(m) for m in plan.matched_changed if m.risky_fields}

    # See master_merge.mentions_let_status - wording suggesting a property
    # is no longer available always forces an explicit decision, same
    # principle as a same-batch collision never being auto-resolved.
    let_status_ids = {id(m) for m in plan.matched_changed if m.let_status_fields}

    _render_discard_pending(pending, new_rows)

    # auto_updates is populated purely by the safe, already-considered-safe
    # path below, with no click of any kind. decision_updates/new_rows_
    # final/removed_indices are populated ONLY by something a reviewer
    # explicitly decided in the "Needs your decision" section (a radio/
    # selectbox/checkbox click). Kept as two separate update dicts (merged
    # into one only right before the Approve button, see `updates` below)
    # specifically so the "Automatic updates" -> "View changes" summary can
    # show EXACTLY the auto-applied set, never conflated with something a
    # reviewer just decided - each decision card already shows its own
    # before/after inline, so repeating it in that summary would just be
    # noise.
    auto_updates = {}
    decision_updates = {}
    silent_by_index = {}  # master_index -> {field: value} - tolerant-formatting fixes, never shown in any diff UI
    new_rows_final = []   # ListingRow objects confirmed as genuinely new
    removed_indices = set()  # master_index values confirmed no longer available - see _render_let_status_decision

    def _apply_silent(m):
        # Applies regardless of whether the row's real diff (if any) needed
        # a decision - a tolerant-formatting fix (case/whitespace only, see
        # master_merge.silent_field_updates) is independent of that and
        # never itself needs review.
        if m.silent_updates:
            entry = silent_by_index.setdefault(m.master_index, {})
            entry.update(m.silent_updates)
            entry["source_file"] = m.new_row.source_file

    # ---- Classify every matched-row change into exactly one bucket -
    # never rendered here, just sorted; see the sections below for where
    # each bucket actually appears on screen.
    auto_matched = []
    decision_let_status = []
    decision_collision_groups = []
    decision_solo_collision = []  # a collision group that shrank to exactly one non-let-status member - see below
    decision_risky = []

    collision_groups_by_index = {group[0].master_index: group for group in plan.collisions}
    queued_collision_indices = set()

    for m in plan.matched_changed:
        _apply_silent(m)
        if id(m) in let_status_ids:
            decision_let_status.append(m)
            continue
        if id(m) in colliding_changed_ids:
            if m.master_index in queued_collision_indices:
                continue  # this group's peer already queued the whole group
            queued_collision_indices.add(m.master_index)
            # A collision group's own let-status members (if any) were
            # already pulled out above - only the remaining, non-let-status
            # members are compared against each other as a group. If that
            # leaves exactly one (a sibling was pulled into its own let-
            # status decision), there's nothing left to compare it AGAINST
            # as a group - _render_collision_group expects 2+ members - so
            # it falls through to the ordinary single-row rendering
            # instead, still forced into a deliberate accept (default_
            # checked=False) since it did collide with something.
            group = [g for g in collision_groups_by_index[m.master_index] if id(g) not in let_status_ids]
            if len(group) >= 2:
                # Same "does this genuinely need a look" check _render_
                # collision_group makes internally (see its own auto_accept
                # short-circuit) - computed here too, BEFORE deciding which
                # bucket this group belongs in, so a fully-agreeing group
                # (every field already agrees, nothing risky) lands in
                # auto_matched-equivalent territory (auto_updates directly)
                # instead of triggering "Needs your decision" with nothing
                # actually rendered under it for this group.
                dicts = [g.new_row.model_dump() for g in group]
                group_risky = frozenset().union(*(g.risky_fields for g in group))
                agree_diffs = {}
                needs_choice_any = False
                for f in master_merge.collision_group_fields(group):
                    values = [d.get(f) for d in dicts]
                    needs_choice, resolved = master_merge.matched_collision_field_choice(values, f)
                    if needs_choice:
                        needs_choice_any = True
                    else:
                        agree_diffs[f] = resolved
                if needs_choice_any or group_risky:
                    decision_collision_groups.append(group)
                elif agree_diffs:
                    labels = [d.get("source_file") or f"Row {gi + 1}" for gi, d in enumerate(dicts)]
                    entry = auto_updates.setdefault(m.master_index, {})
                    entry.update(agree_diffs)
                    entry["source_file"] = " + ".join(labels)
            elif group:
                decision_solo_collision.append(group[0])
            continue
        if id(m) in risky_changed_ids:
            decision_risky.append(m)
            continue
        auto_matched.append(m)

    for m in plan.matched_unchanged:
        _apply_silent(m)

    for m in auto_matched:
        entry = {f: new_val for f, (old_val, new_val) in m.diffs.items()}
        entry["source_file"] = m.new_row.source_file
        auto_updates[m.master_index] = entry

    collision_ids = {id(u) for group in plan.unmatched_collisions for u in group}
    near_miss = [u for u in plan.unmatched if id(u) not in collision_ids and u.suggestions]
    plain_new = [u for u in plan.unmatched if id(u) not in collision_ids and not u.suggestions]

    matched_master_indices = {m.master_index for m in plan.matched_changed} | {
        m.master_index for m in plan.matched_unchanged
    }
    stale_indices = master_merge.find_stale_candidates(
        new_rows, plan.master_records, matched_master_indices, fully_occupied_buildings=fully_occupied_buildings,
    )

    any_decisions = bool(
        decision_let_status or decision_collision_groups or decision_solo_collision or decision_risky
        or near_miss or plan.unmatched_collisions or stale_indices
    )

    # ==== 1. Needs your decision - every genuinely manual property-level
    # decision, together, at the top - never interleaved with the ordinary,
    # already-safe changes rendered further down. ====
    if any_decisions:
        st.subheader("⚠️ Needs your decision")

        for i, m in enumerate(decision_let_status):
            decision = _render_let_status_decision(m, f"let_status_{i}_{m.property_id}")
            if decision == "remove":
                removed_indices.add(m.master_index)
            elif decision == "apply":
                entry = decision_updates.setdefault(m.master_index, {})
                entry.update({f: new_val for f, (old_val, new_val) in m.diffs.items()})
                entry["source_file"] = m.new_row.source_file
            # "ignore" - this update contributes nothing for this row at all.
            st.divider()

        for i, group in enumerate(decision_collision_groups):
            _render_collision_group(group, i, plan, decision_updates, auto_accept=True)

        for i, m in enumerate(decision_solo_collision):
            _render_matched_row(m, f"solo_collision_{i}_{m.property_id}", "⚠️ ", False, decision_updates)

        for i, m in enumerate(decision_risky):
            _render_matched_row(m, f"risky_{i}_{m.property_id}", "⚠️ ", True, decision_updates)

        # near_miss (against an existing master property) and
        # unmatched_collisions (against another row in this same upload)
        # both genuinely need a human decision - one shared explainer
        # covering both, since a reviewer sees whichever mix this batch
        # happens to have.
        if near_miss or plan.unmatched_collisions:
            st.caption(
                "Some of these look similar to a property already in master; others look "
                "like the same property may have been listed twice in this upload. Open "
                "each one and decide: is this the same property, or genuinely different?"
            )

        # Near-misses against an EXISTING master property (not a batch
        # duplicate) - a genuine decision (is this actually that property,
        # reworded/typo'd?).
        if near_miss:
            master_options = {"— add as new —": None}
            for rec in plan.master_records:
                master_options[f"{display_utils.row_label(rec)} ({rec['property_id'][:8]})"] = rec["property_id"]

            for i, u in enumerate(near_miss):
                row_dict = u.new_row.model_dump()
                key_prefix = f"near_miss_{i}"
                with st.expander(f"⚠️ {display_utils.row_label(row_dict)}", key=f"{key_prefix}_expander"):
                    st.caption(
                        "Possible near-misses already in the master: "
                        + ", ".join(display_utils.row_label(s) for s in u.suggestions)
                    )

                    choice_label = st.selectbox(
                        "What should happen with this row?",
                        list(master_options.keys()),
                        key=f"{key_prefix}_choice",
                    )
                    linked_property_id = master_options[choice_label]

                    if linked_property_id is None:
                        # "— add as new —" is the default selectbox choice, so this is
                        # already the no-action outcome - no extra checkbox needed to
                        # confirm it. A reviewer who believes this IS the near-miss
                        # property says so by picking it from the dropdown above, which
                        # routes into the `else` branch instead.
                        new_rows_final.append(
                            u.new_row.model_copy(update={"property_id": str(uuid.uuid4())})
                        )
                    else:
                        target_index = next(
                            idx for idx, rec in enumerate(plan.master_records)
                            if rec["property_id"] == linked_property_id
                        )
                        old_rec = plan.master_records[target_index]
                        diffs = master_merge.diff_fields(old_rec, row_dict)
                        st.caption(f"Linked to an existing property — {len(diffs)} field(s) would change.")
                        if diffs:
                            risky_fields = frozenset(
                                f for f in diffs
                                if f in master_merge.RISKY_TEXT_FIELDS and master_merge.is_detail_loss(*diffs[f])
                            )
                            approved_fields = _render_field_rows(
                                diffs, f"{key_prefix}_link", default_checked=True, risky_fields=risky_fields
                            )
                            if approved_fields:
                                entry = decision_updates.setdefault(target_index, {})
                                entry.update(approved_fields)
                                entry["source_file"] = u.new_row.source_file

        # Intra-batch duplicates: two or more pending rows independently
        # failing to match master, but matching EACH OTHER (see
        # master_merge._dedup_key) with a genuine field conflict - offered
        # a field-level merge into one property, not a forced choice
        # between "add both" or "add one, discard the other's data".
        for i, group in enumerate(plan.unmatched_collisions):
            _render_intra_batch_duplicate_group(group, f"batch_dup_{i}", new_rows_final)

        if stale_indices:
            st.caption(
                "These existing master properties weren't matched by anything in this upload, but "
                "their own building was covered by it — meaning the latest availability data no "
                "longer mentions them. Review each one: keep it if it's still genuinely available, "
                "or remove it if it's no longer offered."
            )
            for idx in stale_indices:
                rec = plan.master_records[idx]
                provider_label = rec.get("provider") or "this provider's"
                decision = _render_stale_candidate_decision(rec, provider_label, f"stale_{idx}")
                if decision == "remove":
                    removed_indices.add(idx)

    # ==== 2. Automatic updates - one summary, details behind a single
    # "View changes" expander; no field-by-field Apply buttons for changes
    # already considered safe. ====
    if auto_updates:
        n = len(auto_updates)
        st.subheader("✅ Automatic updates")
        st.info(f"{n} existing propert{'y' if n == 1 else 'ies'} will be updated automatically.")
        with st.expander("View changes"):
            auto_diff_rows, _, _ = master_merge.build_approval_summary(plan, auto_updates, [], frozenset())
            for d in auto_diff_rows:
                st.markdown(f"**{d['property']}** — {d['field']}")
                display_utils.render_before_after(d["old"], d["new"])

    # ==== 3. New properties ====
    if plain_new:
        n = len(plain_new)
        st.subheader("📄 New properties")
        st.info(f"{n} new propert{'y' if n == 1 else 'ies'} will be added.")
        with st.expander("View new properties"):
            for label in master_merge.new_property_labels([u.new_row for u in plain_new]):
                st.write(label)
        new_rows_final.extend(
            u.new_row.model_copy(update={"property_id": str(uuid.uuid4())}) for u in plain_new
        )

    # ==== 4. No changes ====
    if plan.matched_unchanged:
        n = len(plan.matched_unchanged)
        st.caption(f"{n} propert{'y' if n == 1 else 'ies'} matched with no changes.")

    if not any_decisions and not auto_updates and not plain_new and not plan.matched_unchanged:
        st.info("Nothing to apply — this upload has no rows to review.")

    # Merged only now, right before Approve - see auto_updates/decision_
    # updates' own comment above for why they stay separate until here.
    updates = {idx: dict(fields) for idx, fields in auto_updates.items()}
    for idx, fields in decision_updates.items():
        updates.setdefault(idx, {}).update(fields)

    if st.button("Approve → Master", type="primary"):
        with st.spinner("Updating master spreadsheet..."):
            try:
                removed_indices_frozen = frozenset(removed_indices)
                diff_rows, new_labels, removed_labels = master_merge.build_approval_summary(
                    plan, updates, new_rows_final, removed_indices_frozen,
                )

                # The version to offer for "Undo this update" is whatever was
                # newest BEFORE this write - the one write_master() is about
                # to create is the new current state, not something to
                # restore back to (restoring that would be a no-op).
                previous_versions = master_writer.list_versions(limit=1)
                previous_version_path = previous_versions[0]["path"] if previous_versions else None

                # Silent (tolerant-formatting-only) updates are folded in only
                # here, right before writing - build_approval_summary above
                # deliberately never sees silent_by_index, so they never
                # appear in the "View what changed" confirmation.
                combined_updates = {idx: dict(fields) for idx, fields in silent_by_index.items()}
                for idx, fields in updates.items():
                    combined_updates.setdefault(idx, {}).update(fields)
                merged_rows = master_merge.apply_merge(
                    plan.master_records, combined_updates, new_rows_final, removed_indices_frozen,
                )
                master_writer.write_master(
                    merged_rows,
                    new_count=len(new_rows_final),
                    updated_count=len(updates),
                    removed_count=len(removed_indices_frozen),
                )
                for path in pending:
                    mark_as_approved(path)
                st.session_state["last_approval"] = {
                    "updated_count": len(updates),
                    "new_count": len(new_rows_final),
                    "removed_count": len(removed_indices_frozen),
                    "diff_rows": diff_rows,
                    "new_labels": new_labels,
                    "removed_labels": removed_labels,
                    "version_path": previous_version_path,
                }
                st.session_state["show_approval_details"] = False
                st.rerun()
            except Exception as e:
                st.error(f"Approval failed, master was not changed: {e}")


with page_setup.setup_page("review"):
    st.title("Review & Master Spreadsheet")

    just_discarded = st.session_state.pop("just_discarded", None)
    if just_discarded:
        st.success(f"Discarded {just_discarded} pending propert{'y' if just_discarded == 1 else 'ies'}.")

    pending = list_pending_staging_files()

    if pending:
        _render_pending_review(pending)
    else:
        _render_full_master_view()

    st.divider()
    with st.expander("Version history", expanded=bool(st.session_state.get("just_restored"))):
        if st.session_state.get("just_restored"):
            st.success(f"Restored from {st.session_state.pop('just_restored')}.")

        versions = master_writer.list_versions(limit=10)
        if not versions:
            st.info("No versions yet.")
        else:
            for v in versions:
                cols = st.columns([3, 4, 2])
                cols[0].write(display_utils.to_london_display(v["timestamp"]) if v["timestamp"] else "—")
                cols[1].write(v["label"])
                restore_key = f"restore_{v['path']}"
                pending_key = f"{restore_key}_pending"

                if cols[2].button("Restore this version", key=restore_key):
                    st.session_state[pending_key] = True

                if st.session_state.get(pending_key):
                    st.warning(
                        f"This replaces the current master.xlsx with the version from "
                        f"{display_utils.to_london_display(v['timestamp'])}. This itself creates a "
                        f"new version, so it can be undone."
                    )
                    confirm_cols = st.columns(2)
                    if confirm_cols[0].button("Confirm restore", key=f"{restore_key}_confirm", type="primary"):
                        with st.spinner("Restoring..."):
                            master_writer.restore_version(v["path"])
                        st.session_state[pending_key] = False
                        st.session_state["just_restored"] = v["path"]
                        st.rerun()
                    if confirm_cols[1].button("Cancel", key=f"{restore_key}_cancel"):
                        st.session_state[pending_key] = False
                        st.rerun()

    page_flow.render_nav_buttons("pages/2_Review_and_Master.py")
