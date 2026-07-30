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


def _row_label(row_dict: dict) -> str:
    parts = [row_dict.get("building") or "(no building)"]
    if row_dict.get("provider"):
        parts.append(row_dict["provider"])
    if row_dict.get("floor_unit"):
        parts.append(row_dict["floor_unit"])
    return " — ".join(parts)


def _render_diff_fields(diffs: dict, key_prefix: str) -> dict:
    """
    Renders an old value / editable new value row per changed field and
    returns the field: value mapping currently entered (defaulting to the
    new extracted value) - this is what gets applied if the row is approved,
    letting a reviewer correct a value rather than only accept or reject it
    wholesale. Numeric fields get a number_input (typed correctly, per
    master_merge.field_kind) rather than a free-text box.
    """
    result = {}
    for f, (old_val, new_val) in diffs.items():
        cols = st.columns([2, 3, 3])
        cols[0].markdown(f"**{f}**")
        cols[1].write("—" if old_val in (None, "") else old_val)
        kind = master_merge.field_kind(f)
        if kind in ("int", "float"):
            default = float(new_val) if new_val is not None else 0.0
            edited = cols[2].number_input(
                "New value", value=default, step=(1.0 if kind == "int" else 0.01),
                key=f"{key_prefix}_{f}", label_visibility="collapsed",
            )
            result[f] = int(edited) if kind == "int" else edited
        else:
            edited = cols[2].text_input(
                "New value", value="" if new_val is None else str(new_val),
                key=f"{key_prefix}_{f}", label_visibility="collapsed",
            )
            result[f] = edited if edited != "" else None
    return result


with page_setup.setup_page("review"):
    st.title("Review & Master Spreadsheet")

    pending = list_pending_staging_files()

    if pending:
        # New pending work supersedes any confirmation from a previous approval.
        st.session_state["just_approved"] = False

    updates = {}         # master_index -> {field: approved_value}
    new_rows_final = []  # ListingRow objects confirmed as genuinely new

    if not pending:
        st.info("No pending uploads to review.")
    else:
        with st.spinner("Loading..."):
            combined_df = pd.concat(
                [load_staging_as_dataframe(path) for path in pending], ignore_index=True
            )
            new_rows = dataframe_to_listing_rows(combined_df)
            master_df = master_writer.load_master_as_dataframe() if master_writer.master_exists() else _empty_master_df()
            plan = master_merge.build_merge_plan(new_rows, master_df)

        st.caption(
            f"{len(pending)} pending upload(s), {len(new_rows)} row(s) total — "
            f"{len(plan.matched_changed)} matched with changes, "
            f"{len(plan.unmatched)} with no match, "
            f"{len(plan.matched_unchanged)} matched with no changes."
        )

        if plan.collisions or plan.unmatched_collisions:
            st.warning(
                "Some rows in this batch appear to target the same property. "
                "They're marked with ⚠️ below and default to unchecked — review "
                "them carefully, since approving more than one for the same "
                "property applies them in the order shown, each on top of the last."
            )

        colliding_ids = {id(m) for group in plan.collisions for m in group}

        if plan.matched_changed:
            st.subheader("Matched — changes detected")
            for i, m in enumerate(plan.matched_changed):
                is_collision = id(m) in colliding_ids
                prefix = "⚠️ " if is_collision else ""
                label = f"{prefix}{_row_label(m.new_row.model_dump())} — {len(m.diffs)} field(s) changed"
                with st.expander(label):
                    key_prefix = f"matched_{i}_{m.property_id}"
                    field_values = _render_diff_fields(m.diffs, key_prefix)
                    apply_it = st.checkbox(
                        "Apply this update", value=not is_collision, key=f"{key_prefix}_apply"
                    )
                if apply_it:
                    entry = updates.setdefault(m.master_index, {})
                    entry.update(field_values)
                    entry["source_file"] = m.new_row.source_file

        if plan.unmatched:
            st.subheader("No match found — will be added as new")
            master_options = {"— add as new —": None}
            for rec in plan.master_records:
                master_options[f"{_row_label(rec)} ({rec['property_id'][:8]})"] = rec["property_id"]

            for i, u in enumerate(plan.unmatched):
                row_dict = u.new_row.model_dump()
                key_prefix = f"unmatched_{i}"
                with st.expander(_row_label(row_dict)):
                    summary = {
                        k: v for k, v in row_dict.items()
                        if k not in ("property_id", "source_file") and v not in (None, "")
                    }
                    st.write(summary)

                    if u.suggestions:
                        st.caption(
                            "Possible near-misses already in the master: "
                            + ", ".join(_row_label(s) for s in u.suggestions)
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
                        field_values = _render_diff_fields(diffs, f"{key_prefix}_link") if diffs else {}
                        apply_link = st.checkbox(
                            "Apply this update to the linked property", value=True, key=f"{key_prefix}_apply_link"
                        )
                        if apply_link:
                            entry = updates.setdefault(target_index, {})
                            entry.update(field_values)
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
                except Exception as e:
                    st.session_state["just_approved"] = False
                    st.error(f"Approval failed, master was not changed: {e}")

    if st.session_state.get("just_approved"):
        st.success("Approved — master spreadsheet updated.")
        if master_writer.master_exists():
            st.download_button(
                "Download master.xlsx",
                blob_store.read_bytes(master_writer.DEFAULT_MASTER_PATH),
                file_name="master.xlsx",
                key="download_after_approve",
            )

    st.divider()
    with st.expander("View / download current master.xlsx"):
        if master_writer.master_exists():
            with st.spinner("Loading..."):
                df = master_writer.load_master_as_dataframe()
            visible_master = df[display_utils.visible_columns(df)]
            st.dataframe(visible_master, column_config=display_utils.link_column_config(visible_master))

            st.download_button(
                "Download master.xlsx",
                blob_store.read_bytes(master_writer.DEFAULT_MASTER_PATH),
                file_name="master.xlsx",
                key="download_master_section",
            )

            log = master_writer.get_master_write_log()
            if log:
                last = log[-1]
                st.caption(f"Last updated: {last['timestamp']} — {last['row_count']} rows")
        else:
            st.info("No master spreadsheet yet — approve an upload to create one.")

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
                cols[0].write(v["timestamp"] or "—")
                cols[1].write(v["label"])
                restore_key = f"restore_{v['path']}"
                pending_key = f"{restore_key}_pending"

                if cols[2].button("Restore this version", key=restore_key):
                    st.session_state[pending_key] = True

                if st.session_state.get(pending_key):
                    st.warning(
                        f"This replaces the current master.xlsx with the version from "
                        f"{v['timestamp']}. This itself creates a new version, so it can "
                        f"be undone."
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
