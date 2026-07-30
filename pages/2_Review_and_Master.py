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
    dataframe_to_listing_rows,
    list_pending_staging_files,
    load_staging_as_dataframe,
    mark_as_approved,
)


def _empty_master_df() -> pd.DataFrame:
    return pd.DataFrame(columns=list(ListingRow.model_fields.keys()))


def _render_field_rows(diffs: dict, key_prefix: str, default_checked: bool) -> dict:
    """
    Renders one line per changed field: name, old value, editable new value,
    and its OWN apply/skip checkbox - field-level, not row-level, so a
    reviewer can accept some of a row's changes and reject others rather
    than an all-or-nothing choice for the whole row. Returns only the
    fields whose checkbox is checked, using whatever value is currently
    entered (letting a reviewer correct a value, not just accept/reject it).
    """
    approved = {}
    for f, (old_val, new_val) in diffs.items():
        cols = st.columns([2, 3, 3, 1])
        cols[0].markdown(f"**{f}**")
        cols[1].write("—" if old_val in (None, "") else old_val)
        kind = master_merge.field_kind(f)
        with cols[2]:
            if kind in ("int", "float"):
                default = float(new_val) if new_val is not None else 0.0
                edited = st.number_input(
                    "New value", value=default, step=(1.0 if kind == "int" else 0.01),
                    key=f"{key_prefix}_{f}_value", label_visibility="collapsed",
                )
                value = int(edited) if kind == "int" else edited
            else:
                edited = st.text_input(
                    "New value", value="" if new_val is None else str(new_val),
                    key=f"{key_prefix}_{f}_value", label_visibility="collapsed",
                )
                value = edited if edited != "" else None
        with cols[3]:
            apply_field = st.checkbox(
                "Apply", value=default_checked, key=f"{key_prefix}_{f}_apply", label_visibility="collapsed"
            )
        if apply_field:
            approved[f] = value
    return approved


def _render_master_table(df: pd.DataFrame, key: str):
    """
    Full master, browsable and row-selectable (for the export step) - every
    column but Select is disabled so this stays a read/select view, never a
    silent side door for editing master data outside the diff-and-merge flow.

    The current selection (full rows, all columns including the ones hidden
    from this display) is written to session_state on every render, not just
    on a button click - Streamlit reruns this whole script on each checkbox
    toggle, so by the time the user clicks the shared "Export →" nav button
    at the bottom of the page, session_state already reflects whatever is
    currently checked.
    """
    visible = display_utils.visible_columns(df)
    display_df = df[visible].copy()
    display_df.insert(0, "Select", False)
    edited = st.data_editor(
        display_df,
        column_config={
            "Select": st.column_config.CheckboxColumn(required=True),
            **display_utils.link_column_config(display_df),
            **display_utils.wide_text_column_config(display_df),
        },
        disabled=[c for c in display_df.columns if c != "Select"],
        width="stretch",
        height=600,
        key=key,
    )

    selected_positions = edited.index[edited["Select"]].tolist()
    st.session_state["export_selected_df"] = df.loc[selected_positions].reset_index(drop=True)
    st.caption(f"{len(selected_positions)} of {len(df)} row(s) selected — carries over to the Export step.")

    display_utils.render_row_detail(df, key=f"{key}_detail")


def _render_full_master_view():
    if st.session_state.pop("just_approved", False):
        st.success("Approved — master spreadsheet updated.")

    if not master_writer.master_exists():
        st.info("No master spreadsheet yet — approve an upload to create one.")
        return

    with st.spinner("Loading..."):
        df = display_utils.sort_by_provider(master_writer.load_master_as_dataframe())

    _render_master_table(df, key="master_table_default_view")

    st.download_button(
        "Download master.xlsx",
        blob_store.read_bytes(master_writer.DEFAULT_MASTER_PATH),
        file_name="master.xlsx",
        key="download_master_default_view",
    )

    log = master_writer.get_master_write_log()
    if log:
        last = log[-1]
        st.caption(
            f"Last updated: {display_utils.to_london_display(last['timestamp'])} — {last['row_count']} rows"
        )


def _render_pending_review(pending: list):
    with st.spinner("Loading..."):
        combined_df = display_utils.sort_by_provider(pd.concat(
            [load_staging_as_dataframe(path) for path in pending], ignore_index=True
        ))
        new_rows = dataframe_to_listing_rows(combined_df)
        master_df = master_writer.load_master_as_dataframe() if master_writer.master_exists() else _empty_master_df()
        plan = master_merge.build_merge_plan(new_rows, master_df)

    st.caption(
        f"{len(pending)} pending upload(s), {len(new_rows)} row(s) total — "
        f"{len(plan.matched_changed)} matched with changes, "
        f"{len(plan.unmatched)} with no match, "
        f"{len(plan.matched_unchanged)} matched with no changes."
    )

    colliding_changed_ids = {id(m) for group in plan.collisions for m in group}
    colliding_unmatched_ids = {id(u) for group in plan.unmatched_collisions for u in group}

    if plan.collisions or plan.unmatched_collisions:
        st.warning(
            "Some rows in this batch appear to target the same property (marked "
            "⚠️ below). These always need a manual pick, regardless of the mode "
            "chosen below, rather than write order silently deciding a winner."
        )

    mode = st.radio(
        "How should this batch be handled?",
        ["Review each field", "Auto-accept all changes"],
        key="review_mode",
        horizontal=True,
    )
    auto_accept = mode == "Auto-accept all changes"

    if auto_accept:
        auto_changed = [m for m in plan.matched_changed if id(m) not in colliding_changed_ids]
        auto_new = [u for u in plan.unmatched if id(u) not in colliding_unmatched_ids]
        total_fields = sum(len(m.diffs) for m in auto_changed)
        st.info(
            f"Auto-accept: {total_fields} field(s) will be updated across "
            f"{len(auto_changed)} propert{'y' if len(auto_changed) == 1 else 'ies'}, "
            f"{len(auto_new)} new propert{'y' if len(auto_new) == 1 else 'ies'} will be added. "
            + ("Rows involved in a collision above are excluded and still need manual review below."
               if (colliding_changed_ids or colliding_unmatched_ids) else "")
        )

    updates = {}         # master_index -> {field: approved_value}
    new_rows_final = []  # ListingRow objects confirmed as genuinely new

    if plan.matched_changed:
        if not auto_accept or colliding_changed_ids:
            st.subheader("Matched — changes detected")
        for i, m in enumerate(plan.matched_changed):
            is_collision = id(m) in colliding_changed_ids
            if auto_accept and not is_collision:
                entry = {f: new_val for f, (old_val, new_val) in m.diffs.items()}
                entry["source_file"] = m.new_row.source_file
                updates[m.master_index] = entry
                continue

            prefix = "⚠️ " if is_collision else ""
            label = f"{prefix}{display_utils.row_label(m.new_row.model_dump())} — {len(m.diffs)} field(s) changed"
            with st.expander(label):
                key_prefix = f"matched_{i}_{m.property_id}"
                approved_fields = _render_field_rows(m.diffs, key_prefix, default_checked=not is_collision)
            if approved_fields:
                entry = updates.setdefault(m.master_index, {})
                entry.update(approved_fields)
                entry["source_file"] = m.new_row.source_file

    if plan.unmatched:
        show_unmatched_detail = not auto_accept or colliding_unmatched_ids
        if show_unmatched_detail:
            st.subheader("No match found — will be added as new")
        master_options = {"— add as new —": None}
        for rec in plan.master_records:
            master_options[f"{display_utils.row_label(rec)} ({rec['property_id'][:8]})"] = rec["property_id"]

        for i, u in enumerate(plan.unmatched):
            is_collision = id(u) in colliding_unmatched_ids
            if auto_accept and not is_collision:
                new_rows_final.append(u.new_row.model_copy(update={"property_id": str(uuid.uuid4())}))
                continue

            row_dict = u.new_row.model_dump()
            key_prefix = f"unmatched_{i}"
            prefix = "⚠️ " if is_collision else ""
            with st.expander(f"{prefix}{display_utils.row_label(row_dict)}"):
                summary = {
                    k: v for k, v in row_dict.items()
                    if k not in ("property_id", "source_file") and v not in (None, "")
                }
                st.write(summary)

                if u.suggestions:
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
                    confirm_new = st.checkbox(
                        "Confirm — add as a new property", value=True, key=f"{key_prefix}_confirm_new"
                    )
                    if confirm_new:
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
                        approved_fields = _render_field_rows(diffs, f"{key_prefix}_link", default_checked=True)
                        if approved_fields:
                            entry = updates.setdefault(target_index, {})
                            entry.update(approved_fields)
                            entry["source_file"] = u.new_row.source_file

    if plan.matched_unchanged:
        st.caption(f"{len(plan.matched_unchanged)} row(s) matched with no changes.")

    if st.button("Approve → Master", type="primary"):
        with st.spinner("Updating master spreadsheet..."):
            try:
                merged_rows = master_merge.apply_merge(plan.master_records, updates, new_rows_final)
                master_writer.write_master(
                    merged_rows,
                    new_count=len(new_rows_final),
                    updated_count=len(updates),
                )
                for path in pending:
                    mark_as_approved(path)
                st.session_state["just_approved"] = True
                st.rerun()
            except Exception as e:
                st.error(f"Approval failed, master was not changed: {e}")


with page_setup.setup_page("review"):
    st.title("Review & Master Spreadsheet")

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
