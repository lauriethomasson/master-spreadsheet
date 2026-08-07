import uuid

import pandas as pd
import streamlit as st

import display_utils
import master_merge
import master_writer
import page_flow
import page_setup
from schema import ListingRow
from storage import blob_store
from storage.file_store import (
    clean_value,
    dataframe_to_listing_rows,
    discard_pending_staging_files,
    get_staging_fully_occupied_buildings,
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
        needs_choice, resolved = master_merge.matched_collision_field_choice(values)
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
    Prominently shown - never inside a collapsed expander like a normal
    field diff - when a matched row's update contains wording suggesting
    the property is no longer available (see
    master_merge.mentions_let_status). Whether this property still belongs
    in master at all is a more fundamental question than "which fields to
    accept", so it gets its own explicit choice instead of being folded
    into the ordinary per-field checkboxes.

    Defaults to "keep" (the non-destructive option) if the reviewer never
    touches the radio and just clicks Approve - this is a review trigger
    forcing a human to look, not a trap that silently deletes anything.
    """
    st.warning(
        f"🏷️ **{display_utils.row_label(m.new_row.model_dump())}** — this update's wording suggests "
        "the property may no longer be available."
    )
    for f in m.let_status_fields:
        old_val, new_val = m.diffs[f]
        st.markdown(f"**{f}**")
        display_utils.render_before_after(old_val, new_val)

    choice = st.radio(
        "What should happen to this property?",
        ["Keep in master — apply this update", "Remove this property from master entirely"],
        key=f"{key_prefix}_let_decision",
    )
    return "remove" if choice.startswith("Remove") else "keep"


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


def _render_master_table(df: pd.DataFrame, key: str) -> bool:
    """
    Full master, browsable, directly editable, and row-selectable (for the
    export step). Every visible column is a real edit target except Select,
    which is a UI-only checkbox never itself a ListingRow field (see
    master_merge.build_manual_edit) - hidden columns (property_id,
    source_file) are simply absent from display_df, so they can't be edited
    through this UI at all, same as before.

    Row selection is tracked by property_id in session_state, not by the
    data_editor's own positional widget state - a saved edit reloads the
    master (freshly re-sorted; see sort_by_provider), which can shift a
    row's position, and Streamlit's data_editor state is keyed by position.
    Trusting that position across a reload risks silently reapplying a
    stale selection (or edit - see _process_manual_edits) to the wrong row.
    property_id is immune to re-sorting, so it survives that reload intact.

    The text filter below narrows only what's DISPLAYED, never what df/
    master_records themselves contain - data_editor's own edited_rows
    positions are always relative to whatever (possibly filtered) subset
    was actually passed to it this render, so real_positions[i] (the real
    position in df of whatever row sits at filtered position i) is threaded
    through to _process_manual_edits/build_manual_edit below, and row
    selection stays keyed by property_id exactly as it already was for the
    re-sort case above - a filter is just another way a row's position can
    shift out from under a stale positional reference.

    Returns True if a real field edit was saved this render - the caller
    should st.rerun() so the rest of the page reflects the fresh master
    (download button bytes, write-log caption, Version history) rather than
    the pre-edit snapshot already in hand.
    """
    visible = display_utils.visible_columns(df)
    display_df = df[visible].copy()

    selected_ids = st.session_state.get("export_selected_property_ids", set())
    display_df.insert(
        0, "Select",
        df["property_id"].isin(selected_ids) if "property_id" in df.columns else False,
    )

    query = st.text_input(
        "Filter (building, address, provider, or floor/unit)",
        key=f"{key}_filter",
    )
    filtered_df = display_df
    real_positions = list(range(len(df)))
    if query.strip():
        search_cols = [c for c in ("building", "address_1", "provider", "floor_unit") if c in df.columns]
        mask = pd.Series(False, index=df.index)
        for c in search_cols:
            mask = mask | df[c].fillna("").astype(str).str.contains(query.strip(), case=False)
        filtered_df = display_df[mask]
        real_positions = [i for i, keep in enumerate(mask) if keep]

    edited = st.data_editor(
        filtered_df,
        column_config={
            **display_utils.label_column_config(filtered_df),
            "Select": st.column_config.CheckboxColumn(required=True),
            **display_utils.link_column_config(filtered_df),
            **display_utils.wide_text_column_config(filtered_df),
            **display_utils.numeric_column_config(filtered_df),
        },
        width="stretch",
        height=600,
        key=key,
    )
    st.caption(f"{len(filtered_df)} of {len(df)} row(s) shown.")

    if "property_id" in df.columns:
        visible_ids = set(df.loc[filtered_df.index, "property_id"])
        now_selected_visible = set(df.loc[edited.index[edited["Select"]], "property_id"])
        st.session_state["export_selected_property_ids"] = master_merge.merge_selected_property_ids(
            selected_ids, visible_ids, now_selected_visible
        )
        selected_positions = df.index[df["property_id"].isin(st.session_state["export_selected_property_ids"])].tolist()
    else:
        selected_positions = edited.index[edited["Select"]].tolist()
    st.session_state["export_selected_df"] = df.loc[selected_positions].reset_index(drop=True)

    with st.container(horizontal=True):
        st.caption(f"{len(selected_positions)} of {len(df)} row(s) selected — carries over to the Export step.")
        if st.button("Clear selection", key=f"{key}_clear_selection", disabled=not selected_positions):
            st.session_state["export_selected_property_ids"] = set()
            st.session_state["export_selected_df"] = df.iloc[0:0].reset_index(drop=True)
            # Also resets the data_editor's OWN widget state (same reasoning
            # as _process_manual_edits below) - otherwise its cached
            # per-checkbox overrides from before this click would just
            # reapply themselves on the next render, on top of the freshly
            # all-unchecked Select column this seeds from the now-empty
            # export_selected_property_ids, leaving every box looking still
            # checked despite the tracked set genuinely being empty.
            if key in st.session_state:
                del st.session_state[key]
            st.rerun()

        # Reuses the exact same apply_merge/write_master path a let-status
        # removal (during upload review, see removed_indices above) already
        # rides - same versioning/undo/write-log, just triggered directly
        # from the master table's own row selection instead of from an
        # upload-merge diff. Added for one-time duplicate cleanup (e.g. rows
        # left behind by a provider-name fix that changed the match key -
        # see master_merge.py's own module docstring on why provider is
        # part of the key at all) - no separate delete mechanism invented.
        #
        # Deliberately never `disabled=not selected_positions`: a real-
        # browser report of this button needing two clicks to register
        # couldn't be reproduced in AppTest (it can't simulate the DOM-level
        # timing of a click landing right as a checkbox toggle is still
        # updating this button's own disabled state) - but leaving it always
        # clickable side-steps that class of bug entirely, at the cost of a
        # no-op click needing its own friendly message instead of the button
        # simply refusing the click.
        remove_clicked = st.button(
            f"Remove {len(selected_positions)} selected row(s)",
            key=f"{key}_remove_selected",
        )

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
                st.session_state["export_selected_property_ids"] = set()
                st.session_state["export_selected_df"] = df.iloc[0:0].reset_index(drop=True)
                if key in st.session_state:
                    del st.session_state[key]
                st.session_state["last_removal"] = {
                    "count": len(removed_indices),
                    "version_path": previous_version_path,
                }
                st.rerun()
        else:
            # Popped, not just read - shown once right on the rerun
            # immediately after the removal above, then gone, rather than
            # persisting in (page-wide, cross-navigation) session state
            # until someone happens to click Undo - see the equivalent fix
            # for last_approval/last_manual_edit in _render_full_master_view.
            last_removal = st.session_state.pop("last_removal", None)
            if last_removal:
                n = last_removal["count"]
                st.markdown(f":red[✓ {n} row{'s' if n != 1 else ''} removed]")
                if last_removal.get("version_path") and st.button("Undo", key=f"{key}_undo_manual_removal"):
                    with st.spinner("Undoing..."):
                        master_writer.restore_version(last_removal["version_path"])
                    st.session_state["just_restored"] = last_removal["version_path"]
                    st.rerun()

    return _process_manual_edits(df, real_positions, key)


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
    wrong file was uploaded). Whole-batch, not per-file: the pending-review
    UI already has no per-file grouping at all - every pending file's rows
    are combined into one diff/plan and one "Approve -> Master" button
    covers the lot, so "discard" applies at the same granularity. Per-file
    discard would need its own "pending files" list UI first, which
    doesn't exist today.

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


def _render_pending_review(pending: list):
    with st.spinner("Loading..."):
        combined_df = display_utils.sort_by_provider(pd.concat(
            [load_staging_as_dataframe(path) for path in pending], ignore_index=True
        ))
        new_rows = dataframe_to_listing_rows(combined_df)
        master_df = master_writer.load_master_as_dataframe() if master_writer.master_exists() else _empty_master_df()
        plan = master_merge.build_merge_plan(new_rows, master_df)
        fully_occupied_buildings = [
            fo for path in pending for fo in get_staging_fully_occupied_buildings(path)
        ]

    st.caption(master_merge.pending_status_line(len(pending), plan))
    _render_master_lookup(master_df)

    colliding_changed_ids = {id(m) for group in plan.collisions for m in group}
    colliding_unmatched_ids = {id(u) for group in plan.unmatched_collisions for u in group}
    any_collisions = bool(colliding_changed_ids or colliding_unmatched_ids)

    # See master_merge.is_detail_loss - a matched row whose special_features/
    # contacts update looks like it dropped real information is forced into
    # manual review exactly like a same-batch collision, rather than being
    # auto-appliable.
    risky_changed_ids = {id(m) for m in plan.matched_changed if m.risky_fields}
    any_risky = bool(risky_changed_ids - colliding_changed_ids)

    # See master_merge.mentions_let_status - wording suggesting a property
    # is no longer available always forces a manual keep/remove decision,
    # same principle as a same-batch collision never being auto-resolved.
    let_status_ids = {id(m) for m in plan.matched_changed if m.let_status_fields}
    any_let_status = bool(let_status_ids)

    if any_collisions:
        st.warning(
            "Some rows in this batch appear to target the same property (marked "
            "⚠️ below) — these always need a manual pick before the rest can be "
            "applied, rather than write order silently deciding a winner."
        )

    if any_risky:
        st.warning(
            "Some updates (marked ⚠️ below) look like they may be missing detail "
            "compared to what's already stored — these need a manual look before "
            "being applied automatically."
        )

    if any_let_status:
        st.warning(
            "Some updates (marked 🏷️ below) look like they may mean a property is "
            "no longer available — these always need a manual keep-or-remove "
            "decision, regardless of auto-accept."
        )

    manual_review = st.toggle(
        "Review each field manually instead of applying automatically",
        key="manual_review_toggle",
    )
    auto_accept = not manual_review

    if auto_accept:
        auto_changed = [
            m for m in plan.matched_changed
            if id(m) not in colliding_changed_ids and id(m) not in risky_changed_ids and id(m) not in let_status_ids
        ]
        auto_new = [u for u in plan.unmatched if id(u) not in colliding_unmatched_ids]
        summary_parts = []
        if auto_changed:
            summary_parts.append(f"update {len(auto_changed)} propert{'y' if len(auto_changed) == 1 else 'ies'}")
        if auto_new:
            summary_parts.append(f"add {len(auto_new)} new propert{'y' if len(auto_new) == 1 else 'ies'}")
        if summary_parts:
            st.info(
                "This will " + " and ".join(summary_parts) + "."
                + (" Rows flagged above need manual review first." if (any_collisions or any_risky or any_let_status) else "")
            )
        elif not any_collisions and not any_risky and not any_let_status:
            st.info("Nothing to apply automatically — every row already matches the master with no changes.")

    _render_discard_pending(pending, new_rows)

    updates = {}          # master_index -> {field: approved_value} - real, review-worthy changes only
    silent_by_index = {}  # master_index -> {field: value} - tolerant-formatting fixes, never shown in the diff UI
    new_rows_final = []   # ListingRow objects confirmed as genuinely new
    removed_indices = set()  # master_index values confirmed no longer available - see _render_let_status_decision

    def _apply_silent(m):
        # Applies regardless of auto/manual mode and regardless of whether
        # the row's real diff (if any) was approved - a tolerant-formatting
        # fix (case/whitespace only, see master_merge.silent_field_updates)
        # is independent of that decision and never itself needs review.
        if m.silent_updates:
            entry = silent_by_index.setdefault(m.master_index, {})
            entry.update(m.silent_updates)
            entry["source_file"] = m.new_row.source_file

    if plan.matched_changed:
        if not auto_accept or colliding_changed_ids or risky_changed_ids or let_status_ids:
            st.subheader("Matched — changes detected")
        collision_groups_by_index = {group[0].master_index: group for group in plan.collisions}
        rendered_collision_indices = set()
        for i, m in enumerate(plan.matched_changed):
            is_collision = id(m) in colliding_changed_ids
            is_risky = id(m) in risky_changed_ids
            is_let_status = id(m) in let_status_ids
            _apply_silent(m)

            if is_let_status:
                key_prefix = f"matched_{i}_{m.property_id}"
                decision = _render_let_status_decision(m, key_prefix)
                if decision == "remove":
                    removed_indices.add(m.master_index)
                else:
                    entry = updates.setdefault(m.master_index, {})
                    entry.update({f: new_val for f, (old_val, new_val) in m.diffs.items()})
                    entry["source_file"] = m.new_row.source_file
                continue

            if is_collision:
                if m.master_index in rendered_collision_indices:
                    continue  # this group's peer already rendered the whole group below
                rendered_collision_indices.add(m.master_index)
                # A collision group's own let-status members (if any) were
                # already pulled out above, individually, before this loop
                # ever reaches them - only the remaining, non-let-status
                # members are compared against each other here.
                group = [g for g in collision_groups_by_index[m.master_index] if id(g) not in let_status_ids]
                if len(group) < 2:
                    # Nothing left to compare against as a group (a sibling
                    # was pulled into its own let-status decision above) -
                    # fall through to the ordinary single-row rendering,
                    # still flagged ⚠️ since it did collide with something.
                    if group:
                        _render_matched_row(group[0], f"matched_{i}_{group[0].property_id}", "⚠️ ", False, updates)
                    continue
                _render_collision_group(group, i, plan, updates, auto_accept)
                continue

            if auto_accept and not is_risky:
                entry = {f: new_val for f, (old_val, new_val) in m.diffs.items()}
                entry["source_file"] = m.new_row.source_file
                updates[m.master_index] = entry
                continue

            _render_matched_row(m, f"matched_{i}_{m.property_id}", "⚠️ " if is_risky else "", True, updates)

    for m in plan.matched_unchanged:
        _apply_silent(m)

    if plan.unmatched:
        collision_ids = {id(u) for group in plan.unmatched_collisions for u in group}
        near_miss = [u for u in plan.unmatched if id(u) not in collision_ids and u.suggestions]
        plain_new = [u for u in plan.unmatched if id(u) not in collision_ids and not u.suggestions]

        # Ordinary new properties: no batch duplicate, no near-miss against
        # master - nothing to decide, so this stays a plain one-line list
        # (see master_merge.new_property_labels) rather than the detailed
        # comparison view below, which is reserved for rows that actually
        # need a decision. Collapsed by default (a real upload can easily
        # have 15+ of these with nothing to review) so it doesn't push the
        # rows that DO need a decision below the fold - Streamlit expanders
        # default to collapsed unless expanded=True is passed, so this
        # needs no extra session state of its own.
        if plain_new:
            n = len(plain_new)
            with st.expander(f"📄 {n} new propert{'y' if n == 1 else 'ies'} will be added — click to view"):
                for label in master_merge.new_property_labels([u.new_row for u in plain_new]):
                    st.write(label)
            new_rows_final.extend(
                u.new_row.model_copy(update={"property_id": str(uuid.uuid4())}) for u in plain_new
            )

        # near_miss (against an existing master property) and
        # unmatched_collisions (against another row in this same upload)
        # both genuinely need a human decision - grouped under one shared,
        # always-visible heading (never collapsed, unlike plain_new above)
        # so it's obvious at a glance which rows are just FYI vs. waiting
        # on an answer.
        if near_miss or plan.unmatched_collisions:
            st.subheader("⚠️ Needs a decision")
            st.caption(
                "Some of these look similar to a property already in master; others look "
                "like the same property may have been listed twice in this upload. Open "
                "each one and decide: is this the same property, or genuinely different?"
            )

            # Near-misses against an EXISTING master property (not a batch
            # duplicate) - a genuine decision (is this actually that property,
            # reworded/typo'd?), so it keeps the detailed link-or-confirm UI.
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
                                    entry = updates.setdefault(target_index, {})
                                    entry.update(approved_fields)
                                    entry["source_file"] = u.new_row.source_file

            # Intra-batch duplicates: two or more pending rows independently
            # failing to match master, but matching EACH OTHER (see
            # master_merge._dedup_key) - offered a field-level merge into one
            # property, not a forced choice between "add both" or "add one,
            # discard the other's data".
            for i, group in enumerate(plan.unmatched_collisions):
                _render_intra_batch_duplicate_group(group, f"batch_dup_{i}", new_rows_final)

    matched_master_indices = {m.master_index for m in plan.matched_changed} | {
        m.master_index for m in plan.matched_unchanged
    }
    stale_indices = master_merge.find_stale_candidates(
        new_rows, plan.master_records, matched_master_indices, fully_occupied_buildings=fully_occupied_buildings,
    )
    if stale_indices:
        st.subheader("🕳️ No longer present in latest availability")
        st.caption(
            "These existing master properties weren't matched by anything in this upload, but their "
            "own building was covered by it — meaning the latest availability data no longer mentions "
            "them. Review each one: keep it if it's still genuinely available, or remove it if it's "
            "no longer offered."
        )
        for idx in stale_indices:
            rec = plan.master_records[idx]
            provider_label = rec.get("provider") or "this provider's"
            decision = _render_stale_candidate_decision(rec, provider_label, f"stale_{idx}")
            if decision == "remove":
                removed_indices.add(idx)

    if plan.matched_unchanged:
        st.caption(f"{len(plan.matched_unchanged)} row(s) matched with no changes.")

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
