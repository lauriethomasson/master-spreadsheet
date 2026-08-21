import hashlib
import html
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
from staging_writer import title_case_label
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


def _risky_field_reason(field: str) -> str:
    """
    Short, plain-English reason ONE specific risky field needs a manual
    decision - derived directly from WHICH of master_merge's own risk
    categories that field belongs to (see MatchedRow.risky_fields' own
    computation in build_merge_plan), never invented independently of
    that existing logic and never exposing an internal name/function.
    """
    if field in master_merge.HOUSE_NUMBER_FIELDS:
        return "Existing address would be replaced"
    if field in master_merge.GEOCODE_RISK_FIELDS:
        return "Existing location would be replaced"
    if field in master_merge.RISKY_TEXT_FIELDS:
        return "New text looks shorter than what's there now — may be missing detail, not just an update."
    return "Existing value differs from the new upload"


def _render_field_rows(diffs: dict, key_prefix: str, default_checked: bool, risky_fields: frozenset = frozenset()) -> dict:
    """
    Renders one compact row per field that genuinely needs a reviewer's
    own attention: field name (+ a short reason when risky - see
    _risky_field_reason), formatted "before" value, an editable "after"
    input, and its own Apply checkbox - field-level, not row-level, so a
    reviewer can accept some of a row's changes and reject others rather
    than an all-or-nothing choice for the whole row.

    A field NOT in risky_fields is only ever rendered this way when
    default_checked is False (the "every field here needs a deliberate
    look" case - e.g. a same-batch collision that collapsed to one row,
    see _render_matched_row's own docstring) - matches this function's
    OLD behavior exactly for that case. When default_checked is True, a
    non-risky field is instead bundled into a single "N other safe
    change(s) will be applied automatically" summary line rather than
    getting its own full row: master_merge's own risky_fields computation
    has ALREADY decided that field is safe (it would default to checked/
    applied here regardless, with no interaction expected), so giving it
    the same full before/after/checkbox treatment as a genuinely risky
    field only adds visual bulk with no real decision behind it. The
    bundled value applied is display_utils.coerced_new_value(new_val,
    kind) - byte-identical to what the always-rendered, left-untouched
    widget would have applied by default, so this is a pure display
    simplification, never a behavior change.

    risky_fields (see master_merge.is_detail_loss/house_number_changed)
    starts unchecked regardless of default_checked, and gets its own
    reason caption - the point is a reviewer has to notice and opt in, not
    just uncheck something that would otherwise apply silently.

    Returns only the fields whose checkbox is checked (or bundled as
    safe), using whatever value is currently entered - letting a reviewer
    correct a value, not just accept/reject it verbatim.
    """
    approved = {}
    bundle_safe_fields = default_checked
    individually_rendered = [f for f in diffs if f in risky_fields or not bundle_safe_fields]
    bundled_fields = [f for f in diffs if f not in risky_fields and bundle_safe_fields]

    for f in individually_rendered:
        old_val, new_val = diffs[f]
        is_risky = f in risky_fields
        kind = master_merge.field_kind(f)
        label_col, before_col, after_col, apply_col = st.columns([2, 2, 3, 1])
        with label_col:
            st.markdown(f"**{display_utils.friendly_field_label(f)}**")
            if is_risky:
                st.caption(f"⚠️ {_risky_field_reason(f)}")
        with before_col:
            st.caption(display_utils.format_field_value_for_display(f, old_val))
        with after_col:
            value = display_utils.render_new_value_input(
                new_val, kind, key=f"{key_prefix}_{f}_value", multiline=f in display_utils.WIDE_TEXT_COLUMNS,
            )
        with apply_col:
            apply_field = st.checkbox(
                "Apply", value=default_checked and not is_risky, key=f"{key_prefix}_{f}_apply",
            )
        if apply_field:
            approved[f] = value

    if bundled_fields:
        for f in bundled_fields:
            kind = master_merge.field_kind(f)
            approved[f] = display_utils.coerced_new_value(diffs[f][1], kind)
        n = len(bundled_fields)
        # Names a few of the actual bundled fields (never a generic count
        # alone) so this line still means something on its own - up to 3,
        # since that's plenty to convey "what kind of thing", with an
        # "etc." for however many more there are.
        shown_labels = [display_utils.friendly_field_label(f) for f in bundled_fields[:3]]
        names = ", ".join(shown_labels) + (", etc." if n > len(shown_labels) else "")
        st.caption(f"✓ {n} other change{'s' if n != 1 else ''} ({names}) will apply automatically.")

    return approved


# Shared styling for every real HTML diff table on this page (see
# _render_compact_diff_table/_render_auto_updates_diff below) - a plain
# <table>, never st.dataframe/st.table, specifically so a property's own
# name can be a genuine full-width divider row spanning all 3 columns
# (colspan="3"), which neither Streamlit table widget supports at all.
# rgba-based shading (not a hardcoded hex color) so the divider row reads
# correctly against both a light and a dark Streamlit theme.
_DIFF_TABLE_CSS = """
<style>
.diff-table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
.diff-table th, .diff-table td { padding: 4px 10px; text-align: left; border-bottom: 1px solid rgba(128, 128, 128, 0.25); }
.diff-table th { font-weight: 600; }
.diff-table tr.diff-table-divider td { font-weight: 700; background-color: rgba(128, 128, 128, 0.15); }
</style>
"""

_DIFF_TABLE_HEADER_ROW = "<tr><th>Field</th><th>Current</th><th>New</th></tr>"


def _diff_table_divider_row_html(label: str) -> str:
    """One property's own full-width, bold, shaded divider row - see
    _DIFF_TABLE_CSS - standing in for the separate bold heading this page
    used to render above each property's own block of plain-text lines.
    html.escape guards `label` (a real row_label()/property string) since
    this is genuine unsafe_allow_html markup."""
    return f'<tr class="diff-table-divider"><td colspan="3">{html.escape(label)}</td></tr>'


def _diff_table_row_html(field: str, old_val, new_val) -> str:
    """One Field/Current/New row - the same friendly_field_label/format_
    field_value_for_display formatting every other diff display on this
    page already uses. html.escape guards every value: a free-text field
    (special_features/contacts) can contain arbitrary extracted text, and
    this is genuine unsafe_allow_html markup."""
    label = html.escape(display_utils.friendly_field_label(field))
    old_display = html.escape(display_utils.format_field_value_for_display(field, old_val))
    new_display = html.escape(display_utils.format_field_value_for_display(field, new_val))
    return f"<tr><td>{label}</td><td>{old_display}</td><td>{new_display}</td></tr>"


def _render_compact_diff_table(diff_rows: list) -> None:
    """
    Compact, read-only rendering of a list of {"property", "field", "old",
    "new"} dicts (see master_merge.build_approval_summary's own return
    shape) as ONE continuous Field/Current/New HTML table (see _DIFF_
    TABLE_CSS - real markup via st.markdown(unsafe_allow_html=True), never
    st.dataframe/st.table, neither of which supports a spanning divider
    row) - the property name is shown once per group (consecutive rows for
    the same property, exactly how build_approval_summary already produces
    them - one master_index's fields appended together, never interleaved
    with another's) as its own full-width divider row (see _diff_table_
    divider_row_html) rather than a separate heading above a block of
    plain-text lines. Used for every purely-confirmatory diff display on
    this page (a post-approval summary, a manual cell-edit confirmation) -
    never a decision UI, so there is nothing to check/apply here, only to
    read.
    """
    if not diff_rows:
        return
    body_rows = []
    last_property = None
    for d in diff_rows:
        if d["property"] != last_property:
            body_rows.append(_diff_table_divider_row_html(d["property"]))
            last_property = d["property"]
        body_rows.append(_diff_table_row_html(d["field"], d["old"], d["new"]))
    st.markdown(
        _DIFF_TABLE_CSS + f'<table class="diff-table">{_DIFF_TABLE_HEADER_ROW}{"".join(body_rows)}</table>',
        unsafe_allow_html=True,
    )


def _render_auto_updates_diff(plan, auto_updates: dict, key_prefix: str) -> set:
    """
    Like _render_compact_diff_table, but specifically for the "Automatic
    updates" -> "View changes" expander: each property gets its own "Don't
    apply this update" checkbox, so a reviewer can opt one property back
    out of the auto-applied set before Approve, without needing to review
    or re-accept any of its individual fields. Nothing is written to master
    until Approve is clicked (see the `updates` dict built right before that
    button), so excluding a property here just means it never enters that
    dict at all - its OLD master values are left exactly as they were,
    never a real reversal of anything already on disk.

    Same Field/Current/New HTML table styling as _render_compact_diff_
    table (see _DIFF_TABLE_CSS/_diff_table_divider_row_html/_diff_table_
    row_html) for visual consistency across the page, but split into one
    small table PER property rather than a single continuous one - a real
    st.checkbox is a live widget, which can never be embedded inside
    static unsafe_allow_html markup, so it's rendered as an ordinary
    Streamlit widget alongside each property's own divider row (via
    st.columns) rather than inside the table itself.

    Returns the set of master_index values the reviewer chose to exclude -
    the caller pops those out of auto_updates before merging it into
    `updates`.
    """
    excluded = set()
    if auto_updates:
        st.markdown(_DIFF_TABLE_CSS, unsafe_allow_html=True)
    for master_index, fields in auto_updates.items():
        old_rec = plan.master_records[master_index]
        label = display_utils.row_label(old_rec)
        header_col, checkbox_col = st.columns([5, 2])
        with header_col:
            st.markdown(
                f'<table class="diff-table">{_diff_table_divider_row_html(label)}</table>',
                unsafe_allow_html=True,
            )
        with checkbox_col:
            if st.checkbox("↩ Don't apply this update", key=f"{key_prefix}_{master_index}_exclude"):
                excluded.add(master_index)
        field_rows = "".join(
            _diff_table_row_html(field_name, old_rec.get(field_name), new_val)
            for field_name, new_val in fields.items() if field_name != "source_file"
        )
        st.markdown(
            f'<table class="diff-table">{_DIFF_TABLE_HEADER_ROW}{field_rows}</table>',
            unsafe_allow_html=True,
        )
    return excluded


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
    master_merge._dedup_key) - rows that only reach here at all because
    master_merge itself (see _partition_by_listing_evidence) already found
    no strong, confident evidence one way or the other; a pair with
    dramatically different size/desks/rent (the confirmed real "1 Oliver's
    Yard" case) never reaches this UI in the first place - it's already
    kept as two separate properties automatically, with no prompt at all.
    So the real open question here is genuinely "are these the same
    listing, or two different ones sharing an ambiguous, under-specified
    identity (e.g. both floor_unit blank)?" - never "which value is
    correct for field X", which is why this shows each candidate LISTING's
    own summary and asks ONE question about identity, rather than a
    per-field radio for every disagreeing value (see master_merge.
    listing_summary_lines - reused so a reviewer sees the actual evidence,
    not an opaque source filename).

    That "actual evidence" is now exactly the field(s) master_merge.
    genuinely_differing_fields says genuinely disagree across this group -
    never a fixed, hand-picked set. A card showing the same five fields
    for every group regardless of WHY it was flagged is exactly what let a
    real Kitt's "28 Bruton Street" pair look completely identical to a
    reviewer (the actual disagreement was rent_psf, a field that fixed
    set never included at all) while the app correctly insisted they
    might be different listings - reusing genuinely_differing_fields
    (rather than a second, separately-tuned "which fields to show" rule)
    is what guarantees this card can never again disagree with the very
    logic that decided this group needs a human at all.
    DUPLICATE_CARD_HIDDEN_FIELDS are dropped from what's shown (internal
    pipeline bookkeeping no reviewer needs to see - see that constant's
    own docstring) even if one of them happens to be the genuine
    disagreement; floor_unit is the fallback whenever nothing displayable
    is left, so the card is never blank (see master_merge.
    genuinely_differing_fields' own docstring on why a group can reach
    this UI with no NON-hidden disagreement at all: rare, but not
    provably impossible, so this never assumes it away).

    Four outcomes, one radio, matching the identity question directly:
    - "Keep {all/both} — separate listings" (the safe DEFAULT - selected
      with no interaction needed) - each row becomes its own new property.
    - "Same listing — use Listing X" - exactly ONE of the group's own rows
      is kept, wholesale, as the single new property; the others are
      discarded entirely, deliberately no per-field cherry-picking here -
      if the reviewer can tell these are the same listing, they can also
      tell which one is the correct/more complete row to keep.
    - "Same listing — merge {the two/them all}" - offered ONLY when at
      least one of differing_fields is a RISKY_TEXT_FIELDS field (see
      mergeable_text_fields below); for a disagreement on any OTHER kind
      of field (rent_psf, floor_unit, ...), merging free text isn't a
      meaningful operation, so this choice simply never appears. Reveals
      one editable text box per such field (pre-filled with master_merge.
      draft_merge_text's plain, uncleaned join of every listing's own
      value - the reviewer edits it into its real final wording
      themselves, never auto-cleaned further here) - every OTHER field
      comes from whichever one of the group's rows master_merge.
      richest_listing_index picks as the base, completely unchanged (the
      same richness tie-break already trusted elsewhere in this module,
      not a second, separately-invented one) - only the field(s) shown in
      a box get the reviewer's own typed value instead of that base row's
      own value for exactly that field.
    """
    dicts = [u.new_row.model_dump() for u in group]
    listing_labels = [f"Listing {chr(65 + i)}" for i in range(len(dicts))]
    combined_source = " + ".join(d.get("source_file") or label for d, label in zip(dicts, listing_labels))

    differing_fields = [
        f for f in master_merge.genuinely_differing_fields(dicts) if f not in master_merge.DUPLICATE_CARD_HIDDEN_FIELDS
    ]
    if not differing_fields:
        differing_fields = ["floor_unit"]
    mergeable_text_fields = [f for f in differing_fields if f in master_merge.RISKY_TEXT_FIELDS]

    with st.expander(
        f"⚠️ Possible duplicate listings — {display_utils.row_label(dicts[0])}",
        key=f"{key_prefix}_expander",
    ):
        st.caption(
            "These rows share the same provider/building, but the app cannot safely tell whether "
            "they're the same listing or genuinely different ones."
        )
        cols = st.columns(len(dicts), border=True)
        for col, label, d in zip(cols, listing_labels, dicts):
            with col:
                st.markdown(f"**{label}**")
                lines = master_merge.listing_summary_lines(d, differing_fields)
                if not lines:
                    st.write("These look identical — see raw data.")
                else:
                    for line in lines:
                        st.write(line)

        keep_separate_label = "Keep both — separate listings" if len(dicts) == 2 else "Keep all — separate listings"
        merge_label = "Same listing — merge the two" if len(dicts) == 2 else "Same listing — merge them all"
        options = [keep_separate_label] + [f"Same listing — use {label}" for label in listing_labels]
        if mergeable_text_fields:
            options.append(merge_label)
        choice = st.radio("What are these?", options, key=f"{key_prefix}_choice")

        if choice == keep_separate_label:
            new_rows_final.extend(
                u.new_row.model_copy(update={"property_id": str(uuid.uuid4())}) for u in group
            )
        elif choice == merge_label:
            text_overrides = {}
            for f in mergeable_text_fields:
                draft = master_merge.draft_merge_text([d.get(f) for d in dicts])
                text_overrides[f] = st.text_area(
                    title_case_label(f), value=draft, key=f"{key_prefix}_merge_{f}",
                )
            base = dict(dicts[master_merge.richest_listing_index(dicts)])
            base.update(text_overrides)
            base.update({"property_id": str(uuid.uuid4()), "source_file": combined_source})
            new_rows_final.append(ListingRow(**base))
        else:
            chosen = group[options.index(choice) - 1]
            new_rows_final.append(
                chosen.new_row.model_copy(update={"property_id": str(uuid.uuid4()), "source_file": combined_source})
            )


def _render_matched_row(m, key_prefix: str, prefix: str, default_checked: bool, updates: dict) -> None:
    """
    Single-row expander for an ordinary (non-collision, or a collision
    group that shrank to size 1 after its let-status member was pulled out
    - see the matched_changed loop below) matched-row diff - factored out
    of that loop so the rare group-of-one edge case can reuse it instead of
    duplicating the expander/checkbox rendering.

    The expander's own label counts DECISIONS, not changed fields: when
    m.risky_fields is non-empty, that's exactly how many fields
    _render_field_rows renders individually (everything else is bundled
    into its own "N other safe changes" line - see that function's own
    docstring); when there's no risky field at all (default_checked=False,
    a same-batch collision forcing a deliberate look at every field), every
    changed field still needs its own decision, matching _render_field_
    rows' behavior for that case exactly.
    """
    decisions_needed = len(m.risky_fields) if m.risky_fields else len(m.diffs)
    noun = "decision" if decisions_needed == 1 else "decisions"
    label = f"{prefix}{display_utils.row_label(m.new_row.model_dump())} — {decisions_needed} {noun} needed"
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

    # Decisions needed: every genuine disagreement (choice_fields) plus any
    # agreed field that's ALSO risky - _render_field_rows renders those
    # individually too (see its own docstring), everything else in
    # agree_diffs gets bundled into a single "N other safe changes" line.
    risky_agree_count = sum(1 for f in agree_diffs if f in risky_fields)
    decisions_needed = len(choice_fields) + risky_agree_count
    noun = "decision" if decisions_needed == 1 else "decisions"
    label = f"⚠️ {display_utils.row_label(old_rec)} — {decisions_needed} {noun} needed ({len(group)} sources)"
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
    # Just the actual trigger phrase(s) (e.g. "U/O"), never the flagged
    # field's ENTIRE text - see master_merge.let_status_display_text's own
    # docstring for why a field's full value (often a long amenity list
    # with the real trigger buried in it) is the wrong thing to show here.
    status_text = "; ".join(master_merge.let_status_display_text(m.diffs[f][1]) for f in m.let_status_fields)

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


def _render_new_property_let_status_decision(u, key_prefix: str) -> str:
    """
    Like _render_let_status_decision, for a genuinely NEW property (see
    master_merge.UnmatchedRow.let_status_fields) rather than a matched one -
    there is no existing master record here at all, so that function's own
    "remove property"/"keep current information" choices don't apply
    (there's nothing in master yet to remove or to leave untouched); the
    only real decision is whether to add this property to master at all,
    given its own text already says it's no longer available. A dedicated
    function rather than a parameterized variant of _render_let_status_
    decision, same precedent as _render_stale_candidate_decision already
    being its own sibling rather than a reuse of that one - the two share
    no rendering logic once "there's an old value to show" is gone.
    """
    row_dict = u.new_row.model_dump()
    label = display_utils.row_label(row_dict)
    provider = u.new_row.provider or "This upload"
    # Just the actual trigger phrase(s), never the flagged field's ENTIRE
    # text - see master_merge.let_status_display_text's own docstring, and
    # _render_let_status_decision's own identical use of it above (kept
    # consistent between the two decision prompts deliberately).
    status_text = "; ".join(master_merge.let_status_display_text(getattr(u.new_row, f)) for f in u.let_status_fields)

    st.warning(f"**{label}**\n\n{provider} lists this brand-new property as **{status_text}**.")
    if master_merge.new_property_missing_location(row_dict):
        st.caption("📍 Address, postcode & map location not found — added anyway")

    choice = st.radio(
        "What would you like to do?",
        [
            "Add anyway — add this property to master despite the status.",
            "Don't add — skip this property, it won't be added.",
        ],
        key=f"{key_prefix}_new_let_decision",
    )
    return "skip" if choice.startswith("Don't add") else "add"


# Search fields for the one master search bar - this used to be two
# independent lists (_MASTER_SEARCH_COLUMNS and a Remove-rows-only
# _REMOVAL_SEARCH_COLUMNS adding postcode - a duplicate-row cleanup is often
# easier to spot by postcode than by address text alone) feeding two
# completely independent search bars, from back when a compact selector
# table and the full table were two separate widgets each with their own
# search. Now that there's only ONE master table, there's only one search
# box - postcode is kept in this merged list (never dropped) so that real
# use case still works from it.
_SEARCH_COLUMNS = ("provider", "building", "address_1", "floor_unit", "postcode")


def _search_mask(df: pd.DataFrame, query: str, columns: tuple) -> pd.Series:
    """Boolean mask, aligned to df's own index, of every row where ANY of
    `columns` (see _SEARCH_COLUMNS) contains `query` case-insensitively - the
    one filter the main master table's own search bar applies.
    inside "Edit master spreadsheet" apply to the SAME underlying df."""
    search_cols = [c for c in columns if c in df.columns]
    mask = pd.Series(False, index=df.index)
    for c in search_cols:
        mask = mask | df[c].fillna("").astype(str).str.contains(query.strip(), case=False)
    return mask


def _render_master_table(df: pd.DataFrame, key: str) -> None:
    """
    ONE full master table, browsable and natively row-selectable - the
    single visible table for browsing, selecting, exporting, and removing.
    Editing a property is a deliberately separate, explicit action (see
    _render_selection_actions/_render_edit_property_form) rather than a
    second table on screen.

    Real regression history behind this design: selection used to live
    inside a collapsed "Remove rows" expander, which made the Export
    workflow (which selection feeds just as much as removal) look like it
    had disappeared entirely - fixed by making the selector always-visible.
    That, in turn, meant two full-width tables (a compact 4-column selector
    plus a full editable grid) were both on screen by default, which caused
    a DIFFERENT real confusion: a reviewer testing editability on the first
    (compact, permanently read-only by construction - st.dataframe never
    supports editing) table concluded editing didn't work at all, when the
    real editable table was a second widget further down the page. A third
    design (one selector table + the same full editable grid tucked inside
    a collapsed expander) still kept a second full-width st.data_editor
    around purely because it happened to support editing - this is the
    actual resolution: there is only ever ONE table. Editing one property at
    a time is a small, explicit form (_render_edit_property_form), not a
    second spreadsheet-shaped widget competing for the same screen space.

    Selection uses st.dataframe's native on_select/selection_mode (see
    _render_row_selector) - never a manually-added editable "Select" Boolean
    column, which is the exact design that caused a real two-click/light-
    trackpad selection race before this split existed at all (see
    tests/test_app_review_master_table.py's own module docstring).

    Row selection is tracked by property_id in session_state, not by the
    widget's own positional state - a saved edit reloads the master (freshly
    re-sorted; see sort_by_provider), which can shift a row's position, and
    Streamlit's own widget state is keyed by position. Trusting that
    position across a reload risks silently reapplying a stale selection to
    the wrong row. property_id is immune to re-sorting, so it survives that
    reload intact. A search filter is the same kind of positional shift,
    handled the same way - _render_row_selector resolves its own widget's
    positions back to property_id before this function ever sees them.
    """
    st.subheader("Master spreadsheet")

    with_labels = display_utils.with_brochure_link_display_labels(df)
    visible = display_utils.visible_columns(with_labels)
    display_df = with_labels[visible].copy()

    query = st.text_input("Search master spreadsheet", key=f"{key}_filter")
    filtered_df = display_df
    if query.strip():
        filtered_df = display_df[_search_mask(df, query, _SEARCH_COLUMNS)]

    selected_positions = _render_row_selector(df, filtered_df, key)
    st.session_state["export_selected_df"] = df.loc[selected_positions].reset_index(drop=True)
    _render_selection_actions(df, filtered_df, selected_positions, key)


def _selection_epoch_key(key: str) -> str:
    return f"{key}_selection_epoch"


def _selector_widget_key(df: pd.DataFrame, filtered_df: pd.DataFrame, key: str) -> str:
    """
    The main table's own st.dataframe selection widget key, suffixed with a
    short fingerprint of the property_id sequence CURRENTLY backing it
    (filtered_df's own row order, resolved to df's property_id column) AND
    the current selection epoch (see _selection_epoch_key/_clear_row_
    selection).

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

    The epoch component exists for a DIFFERENT real, confirmed gap the
    property_id fingerprint alone never covers: "Clear selection" (see
    _clear_row_selection) doesn't change which rows are visible at all, so
    that fingerprint is byte-identical before and after the click -
    deleting st.session_state's own entry for the (unchanged) key string
    resets Streamlit's server-side record of the widget's value, but the
    KEY STRING passed to the component is what actually decides whether
    the browser mounts a genuinely new frontend widget instance or keeps
    the existing one; an unchanged key keeps the existing instance, which
    keeps its own internal (glide-data-grid canvas) checkbox state
    regardless of anything deleted server-side - the real reason the
    checkboxes stayed visually ticked after a clear even though the
    tracked selection was correctly empty. Bumping the epoch on every
    clear guarantees a genuinely different key string even when the row
    set itself hasn't moved.

    Deliberately UNCHANGED across a rerun that doesn't alter which
    property_ids are visible or their order AND doesn't bump the epoch
    (typing in an unrelated widget elsewhere on the page, an unrelated
    button click) - remounting the widget on every such rerun would
    itself be a bug: a genuine in-flight browser click needs its target
    widget instance to survive from the click to the next rerun
    uninterrupted to be reliably recorded at all, and gratuitous remounts
    are exactly the kind of race a real user's light/quick trackpad tap
    can lose (see this module's prior, already-fixed report of a similar
    race - _render_row_selector's own docstring).
    """
    if "property_id" in df.columns:
        ids = tuple(df.loc[filtered_df.index, "property_id"])
    else:
        ids = tuple(filtered_df.index)
    epoch = st.session_state.get(_selection_epoch_key(key), 0)
    fingerprint = hashlib.sha256(repr((ids, epoch)).encode("utf-8")).hexdigest()[:16]
    return f"{key}_selector_{fingerprint}"


def _render_row_selector(df: pd.DataFrame, filtered_df: pd.DataFrame, key: str) -> list:
    """
    Renders the MAIN, always-visible, READ-ONLY, natively row-selectable
    table (st.dataframe's own on_select/selection_mode, added specifically
    for this "select rows, then act on the selection" pattern) and returns
    selected_positions - real positions in df (immune to whatever the
    shared search bar narrowed filtered_df to this render - see
    _render_master_table's own docstring), kept in session_state[
    "export_selected_property_ids"] by property_id exactly as before.

    Shows the full column set (with real column_config - link/wide-text/
    numeric formatting included, so brochure/floor plan links are still
    clickable "Open brochure"/"Open floor plan" labels) - previously this
    was a deliberately narrow 4-column identity strip (provider/building/
    floor_unit/address_1), with a separate full st.data_editor rendered
    below it for actual editing. That narrow strip being the first, most
    prominent table on the page while being permanently read-only (st.
    dataframe never supports editing) caused a real report that editing
    "didn't work" - the actual editable widget was a second table further
    down, easy to miss. Editing a property is now its own explicit, single-
    property form (see _render_edit_property_form), not a second table -
    there is only ONE table on screen by default, and it's this one.

    A real-browser report confirmed the old "Remove N selected row(s)"
    button (see _render_selection_actions) sometimes needed two clicks to
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
    selector_df = filtered_df

    selected_ids = st.session_state.get("export_selected_property_ids", set())
    default_rows = []
    if "property_id" in df.columns and selected_ids:
        filtered_property_ids = df.loc[filtered_df.index, "property_id"]
        default_rows = [i for i, pid in enumerate(filtered_property_ids) if pid in selected_ids]

    selection = st.dataframe(
        selector_df,
        column_config={
            **display_utils.label_column_config(selector_df),
            **display_utils.link_column_config(selector_df),
            **display_utils.wide_text_column_config(selector_df),
            **display_utils.numeric_column_config(selector_df),
        },
        width="stretch",
        height=600,
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
    what's really in st.session_state. Deleting that entry is hygiene for
    the now-orphaned old key either way, but is NOT what actually resets
    the on-screen checkboxes - deleting a key's session_state entry only
    resets Streamlit's own server-side record of that widget's value, it
    does not by itself change the KEY STRING the component is mounted
    under, and an unchanged key string means the browser keeps the SAME
    frontend widget instance (with its own internal, canvas-rendered
    checkbox state) rather than mounting a fresh one (real, confirmed
    report: "Clear selection" correctly zeroed the tracked count, but the
    row checkboxes stayed visually ticked). For the successful-removal
    case, master itself is about to shrink, so the NEXT render already
    computes a different property_id fingerprint (and therefore a
    different key) regardless. For the "Clear selection" case, df/
    filtered_df are unchanged, so that fingerprint alone is byte-identical
    before and after this call - the epoch bump below is what forces a
    genuinely different key string in THAT case too, so both paths mount
    a truly fresh widget instance with no inherited frontend state."""
    st.session_state["export_selected_property_ids"] = set()
    st.session_state["export_selected_df"] = df.iloc[0:0].reset_index(drop=True)
    selector_key = _selector_widget_key(df, filtered_df, key)
    if selector_key in st.session_state:
        del st.session_state[selector_key]
    epoch_key = _selection_epoch_key(key)
    st.session_state[epoch_key] = st.session_state.get(epoch_key, 0) + 1


def _editing_property_id_key(key: str) -> str:
    return f"{key}_editing_property_id"


def _render_selection_actions(df: pd.DataFrame, filtered_df: pd.DataFrame, selected_positions: list, key: str) -> None:
    """
    Export/Remove/Edit/Clear, all driven by the SAME selection - selecting
    rows never itself edits, removes, or exports anything; each is its own
    explicit button click. Edit selected property is enabled only when
    EXACTLY one row is selected (see _render_edit_property_form's own
    docstring for why editing is one property at a time, not a grid).

    editing_property_id (session_state, keyed by _editing_property_id_key)
    is deliberately independent of the LIVE selection once set - clicking
    Edit snapshots which property_id to edit; changing the table's own
    selection afterward (without touching Cancel/Save) does not yank the
    form away mid-edit. It's still cleared whenever selection is cleared or
    a removal happens, and defensively cleared if it ever points at a
    property_id no longer in df at all (e.g. removed by a concurrent
    action), so the form can never render for a row that no longer exists.
    """
    editing_key = _editing_property_id_key(key)
    selected_ids = set(df.loc[selected_positions, "property_id"]) if "property_id" in df.columns else set()

    with st.container(horizontal=True):
        count_text = f"{len(selected_positions)} of {len(df)} row(s) selected."
        if len(selected_positions) != 1:
            count_text += " Select exactly 1 property to edit."
        st.caption(count_text)

        # Same st.switch_page(...) call page_flow.render_nav_buttons already
        # uses elsewhere in this app for page-to-page navigation - no new
        # mechanism, and no second export-selection state: this only ever
        # switches to a page that already reads export_selected_df/
        # export_selected_property_ids (see pages/3_Export.py), both of
        # which _render_row_selector above keeps current every render
        # regardless of whether this button is ever clicked.
        if st.button(
            "Export selected →", key=f"{key}_export_selected",
            disabled=not selected_positions, type="primary",
        ):
            st.switch_page("pages/3_Export.py")

        # Reuses the exact same apply_merge/write_master path a let-status
        # removal (during upload review, see removed_indices above) already
        # rides - same versioning/undo/write-log, just triggered directly
        # from the master table's own row selection instead of from an
        # upload-merge diff. Deliberately NEVER disabled=not selected_positions
        # (see tests/test_app_review_master_table.py's own module docstring
        # for why - conditionally disabling it reintroduces the same class
        # of rerun-timing risk a past two-click-race investigation removed
        # this exact disabled= for) - the empty-selection case is instead a
        # safe inline no-op message below.
        remove_clicked = st.button("Remove selected", key=f"{key}_remove_selected")

        if st.button(
            "Edit selected property", key=f"{key}_edit_selected",
            disabled=len(selected_ids) != 1,
        ):
            st.session_state[editing_key] = next(iter(selected_ids))

        if st.button("Clear selection", key=f"{key}_clear_selection", disabled=not selected_positions):
            _clear_row_selection(df, filtered_df, key)
            st.session_state[editing_key] = None
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
                st.session_state[editing_key] = None
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

    editing_property_id = st.session_state.get(editing_key)
    if editing_property_id:
        if "property_id" in df.columns and editing_property_id in set(df["property_id"]):
            _render_edit_property_form(df, editing_property_id, key)
        else:
            st.session_state[editing_key] = None


def _save_property_edit(df: pd.DataFrame, property_id: str, changed_fields: dict) -> bool:
    """
    Saves a single property's changed fields to master.xlsx, reusing the
    EXACT same master_merge.build_manual_edit/apply_merge/write_master path
    the old direct-cell-editing grid used (see git history) - a manual edit
    from a form is not a new kind of write, just a differently-collected
    delta ({row_position: {field: new_value}}, the same shape build_manual_
    edit has always expected), so it rides the same versioning/undo/write-
    log mechanism and the same validation (ListingRow(**merged) inside
    apply_merge) rather than writing raw form values into master.xlsx
    directly. Returns True iff a save happened (changed_fields was non-empty
    and the write succeeded) - False for a no-op (nothing actually changed)
    or a failed write (already reported via st.error).

    property_id, not a positional index, is the caller's own identity for
    WHICH row to edit - resolved to master_records' real position here,
    once, right before the one place that actually needs a position
    (build_manual_edit's own {row_position: ...} shape) - df is expected to
    already be the freshly-loaded, 0..len(df)-1-indexed master (see
    sort_by_provider's own reset_index(drop=True)), so this position is
    real, not filtered/displayed.
    """
    if not changed_fields:
        return False

    master_records = [{k: clean_value(v) for k, v in rec.items()} for rec in df.to_dict(orient="records")]
    row_pos = int(df.index[df["property_id"] == property_id][0])
    merged_rows, diff_rows, fields_changed = master_merge.build_manual_edit(
        master_records, {row_pos: changed_fields}
    )
    if fields_changed == 0:
        return False

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
    return True


def _render_edit_property_form(df: pd.DataFrame, property_id: str, key: str) -> None:
    """
    A compact, single-property edit form - deliberately NOT a second
    st.data_editor grid. Streamlit 1.60 does have st.dialog, but AppTest has
    no way to interact with a dialog's own contents at all (no API to open
    one, find widgets inside it, or click Save/Cancel within it) - an
    expander/form directly under the table is used instead specifically so
    this form's own Save/Cancel/field behavior stays covered by real
    regression tests, matching this codebase's own testing conventions.

    Fields shown are exactly display_utils.visible_columns(df) - the same
    editable field set the old data_editor grid always showed (property_id/
    source_file excluded), so no new hardcoded field list exists here.
    property_id itself is shown read-only (never an input) - it's the row's
    own stable identity, never meant to be user-edited.

    Save only ever passes ACTUALLY-changed fields to _save_property_edit -
    every untouched field is compared against its own ORIGINAL value first,
    so re-saving a form with nothing changed is correctly a no-op (no
    version created, no diff line), not a same-value "change" recorded for
    every field on screen.

    st.number_input has no representable "blank" state - it always returns
    a real float (0.0 by default for a blank int/float field, since there's
    no None it could show instead). Comparing the SAVED value against the
    raw original (None for a blank field) would therefore see every single
    UNTOUCHED blank numeric field as "changed to 0.0" and silently zero it
    out on any save at all - confirmed as a real bug while testing this
    form end-to-end (lat/lng included, since a manually-added row/one with
    no coordinates yet has both blank). Fixed by comparing against the
    WIDGET's own default (0.0) for a field that started blank, not the raw
    original None - a genuinely blank field stays blank unless the widget's
    OWN value actually changes from what it was initialized to. The one
    accepted tradeoff: deliberately setting a previously-blank numeric field
    to exactly 0 looks identical to leaving it untouched and is silently
    ignored - an inherent st.number_input limitation, not fixable without a
    separate explicit "set to zero" control, and far safer than the
    data-corrupting alternative.
    """
    row = df.loc[df["property_id"] == property_id].iloc[0]
    visible = display_utils.visible_columns(df)
    editing_key = _editing_property_id_key(key)

    with st.expander(f"✏️ Edit {display_utils.row_label(row.to_dict())}", expanded=True):
        st.caption(f"Property ID: {property_id}")
        new_values = {}
        original_for_compare = {}
        for field in visible:
            kind = master_merge.field_kind(field)
            current = clean_value(row[field])
            field_key = f"{key}_edit_{property_id}_{field}"
            if kind in ("int", "float"):
                default = float(current) if current is not None else 0.0
                edited = st.number_input(
                    title_case_label(field), value=default,
                    step=(1.0 if kind == "int" else 0.01), key=field_key,
                )
                new_values[field] = int(edited) if kind == "int" else edited
                original_for_compare[field] = int(default) if kind == "int" else default
            elif field in display_utils.WIDE_TEXT_COLUMNS:
                edited = st.text_area(
                    title_case_label(field), value="" if current is None else str(current), key=field_key,
                )
                new_values[field] = edited if edited != "" else None
                original_for_compare[field] = current
            else:
                edited = st.text_input(
                    title_case_label(field), value="" if current is None else str(current), key=field_key,
                )
                new_values[field] = edited if edited != "" else None
                original_for_compare[field] = current

        # horizontal=True sizes each button to its own content instead of
        # stretching across equal-width st.columns - the same fix as "Undo
        # this update" above (see that container's own comment) applied
        # here, so "Cancel" sits right beside "Save" instead of stranded
        # at the 50% mark of the form's own full width.
        with st.container(horizontal=True):
            save_clicked = st.button("Save", key=f"{key}_edit_save_{property_id}", type="primary")
            cancel_clicked = st.button("Cancel", key=f"{key}_edit_cancel_{property_id}")

        if cancel_clicked:
            st.session_state[editing_key] = None
            st.rerun()

        if save_clicked:
            changed_fields = {
                field: new_val for field, new_val in new_values.items()
                if new_val != original_for_compare[field]
            }
            if _save_property_edit(df, property_id, changed_fields):
                st.session_state[editing_key] = None
                st.rerun()
            else:
                st.info("No changes to save.")


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
            _render_compact_diff_table(diff_rows)
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

    _render_compact_diff_table(edit["diff_rows"])


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

    _render_master_table(df, key="master_table_default_view")

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

    A no-op with exactly one (or zero) pending upload: with only one entry
    pending, this whole-batch action and the per-file "Discard this upload"
    button already target the exact same single staging entry - a second,
    real production report confirmed showing BOTH controls in that case is
    confusing, two near-identical buttons for one effective action. The
    per-file button alone (relabeled "Discard upload" for this specific
    case - see _render_brochure_enrichment_summary) is the only discard
    control shown then; this function draws nothing at all, not even its
    own leftover confirmation prompt (see the state-clearing below) if a
    reviewer had it open from a moment when 2+ uploads were still pending.

    Two-click confirm, same pattern as "Restore this version" in the
    Version history section below - this is a real, permanent deletion
    with no undo (unlike a master.xlsx change, which is always versioned),
    so a single click isn't enough.
    """
    if len(pending) <= 1:
        st.session_state.pop("discard_pending_confirm", None)
        return

    if st.button("Discard all pending uploads", key="discard_pending"):
        st.session_state["discard_pending_confirm"] = True

    if st.session_state.get("discard_pending_confirm"):
        n = len(new_rows)
        st.warning(
            f"Are you sure? This will permanently discard {n} pending propert{'y' if n == 1 else 'ies'} "
            f"across {len(pending)} file{'s' if len(pending) != 1 else ''} — no changes will be applied to "
            "master, and this cannot be undone (nothing was ever written to master.xlsx, so there's no "
            "version to restore)."
        )
        # horizontal=True sizes each button to its own content instead of
        # stretching across equal-width st.columns - the same fix as "Undo
        # this update" above (see that container's own comment) applied
        # here, so "Cancel" sits right beside "Confirm discard" instead of
        # stranded at the 50% mark of the card's own full width.
        with st.container(horizontal=True):
            confirm_clicked = st.button("Confirm discard", key="discard_pending_confirm_btn", type="primary")
            cancel_clicked = st.button("Cancel", key="discard_pending_cancel")
        if confirm_clicked:
            discard_pending_staging_files(pending)
            st.session_state.pop("discard_pending_confirm", None)
            st.session_state["just_discarded"] = n
            st.rerun()
        if cancel_clicked:
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

        df = display_utils.with_brochure_link_display_labels(df)
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


def _render_single_file_discard(path: str, label: str = "Discard this upload") -> None:
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

    label defaults to "Discard this upload" (the 2+ pending case, where
    _render_discard_pending's own separate "Discard all pending uploads"
    button also exists and the "this" distinguishes the two) - the caller
    passes "Discard upload" instead for the exactly-one-pending case (see
    _render_brochure_enrichment_summary), where _render_discard_pending
    renders nothing at all, so this is the ONLY discard control on the
    page and doesn't need to distinguish itself from a sibling bulk button
    that isn't there.

    Session-state keys are suffixed with `path` itself (a real, unique
    staging path, e.g. "staging/20260811_..._UNION.xlsx") - never the
    filename, which two entries here can share - so confirming discard on
    one entry can never leave a stale confirm flag armed against a
    DIFFERENT entry, and clicking Confirm always targets whichever single
    path this specific render call was for.
    """
    confirm_key = f"discard_single_confirm_{path}"
    if st.button(label, key=f"discard_single_{path}"):
        st.session_state[confirm_key] = True

    if st.session_state.get(confirm_key):
        st.warning(
            "Are you sure? This can't be undone — but master isn't affected either way, since nothing "
            "here was ever written to it."
        )
        # horizontal=True sizes each button to its own content instead of
        # stretching across equal-width st.columns - the same fix as "Undo
        # this update" above (see that container's own comment) applied
        # here, so "Cancel" sits right beside "Confirm discard" instead of
        # stranded at the 50% mark of the card's own full width.
        with st.container(horizontal=True):
            confirm_clicked = st.button("Confirm discard", key=f"discard_single_confirm_btn_{path}", type="primary")
            cancel_clicked = st.button("Cancel", key=f"discard_single_cancel_{path}")
        if confirm_clicked:
            n = get_staging_row_count(path)
            discard_pending_staging_files([path])
            st.session_state.pop(confirm_key, None)
            st.session_state["just_discarded"] = n
            st.rerun()
        if cancel_clicked:
            st.session_state.pop(confirm_key, None)
            st.rerun()


# Fraction of a file's own document issues that must share the Canva
# unsupported-link reason (see brochure_enrichment._ineligible_link_issues'
# own "unsupported_reason" docstring) before the summary names Canva by
# type - a real, data-established majority, never a guess merely because
# SOME of a file's issues happen to be Canva links.
_CANVA_MAJORITY_THRESHOLD = 0.5


def _render_document_issues(any_links_checked: bool, issues: list) -> None:
    """
    Plain-English summary of this file's own discovered brochure/floorplan
    links - a reviewer only needs to know what worked, what couldn't be
    read, and whether they need to do anything; internal status constants
    (unsupported_link_type, fetch_failed, extraction_failed, ...) are never
    shown directly, only brochure_enrichment.issue_label's own friendly
    wording - this is the one place a reviewer sees this, and it must stay
    simple and non-technical.

    Deliberately never claims a combined "N found — X read, Y couldn't be
    read" total: unique_brochures_considered/unique_floorplans_considered
    (real fetch attempts) are counted per unique URL, while `issues` is
    counted per AFFECTED ROW (see brochure_enrichment._ineligible_link_
    issues/enrich_rows_grouped's own docstrings) - two rows sharing one
    failed brochure_link each get their own issue entry, so a combined
    total mixing both granularities could overstate or understate "how
    many documents" were involved. The one count this DOES show - `len(
    issues)` - is unambiguous and always accurate on its own terms: exactly
    how many rows have a document that couldn't be read, never a
    reconciled figure that might not add up (see this task's own real
    report: a stray "3/3 checked" beside "50 need a look" read as
    contradictory for exactly this reason).

    A file with nothing checked at all (any_links_checked=False) and no
    issues draws nothing - there's nothing to report. A file that checked
    at least one link and had no issues draws a single "✓ Documents
    checked" with no further statistics - "3/3 checked, 0 enriched" is
    exactly the kind of processing-internal detail a normal user doesn't
    need.

    When most (see _CANVA_MAJORITY_THRESHOLD) of this file's own issues
    share the Canva-specific reason (see _ineligible_link_issues'
    "unsupported_reason" key, itself derived from brochure_link_resolver.
    is_canva_view_link - never guessed), an extra line names Canva
    specifically rather than reading like the whole upload failed for an
    unknown reason. This is purely a wording choice made from a fact this
    module already has - it never changes which links are attempted,
    retried, or fetched (Canva's own safe, pre-fetch-rejected behavior from
    commit a67e337 is completely unchanged).
    """
    if not issues:
        if any_links_checked:
            st.caption("✓ Documents checked")
        return

    n_issues = len(issues)
    st.caption(f"⚠️ {n_issues} document{'s' if n_issues != 1 else ''} couldn't be read.")
    canva_count = sum(1 for i in issues if i.get("unsupported_reason") == "canva")
    mostly_canva = bool(canva_count) and canva_count / n_issues >= _CANVA_MAJORITY_THRESHOLD
    if mostly_canva:
        st.caption("Most are Canva links, which aren't currently supported.")

    with st.expander("View affected properties" if mostly_canva else "View document issues"):
        for issue in issues:
            location = " ".join((issue["building"] or "(no building)").split())
            if issue.get("floor_unit"):
                location += f" — {' '.join(issue['floor_unit'].split())}"
            st.markdown(f"**{location}**  \n{brochure_enrichment.issue_label(issue['status'])}.")


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
                any_links_checked = bool(
                    stats["unique_brochures_considered"] or stats.get("unique_floorplans_considered")
                )
                _render_document_issues(any_links_checked, stats.get("document_issues") or [])

            # Exactly one pending upload: _render_discard_pending renders
            # nothing at all (see its own docstring) - this per-file button
            # is the ONLY discard control on the page, so it doesn't need
            # "this" to distinguish itself from a sibling bulk button that
            # isn't there. 2+ pending: unchanged "Discard this upload",
            # since "Discard all pending uploads" also exists below.
            discard_label = "Discard upload" if len(pending) == 1 else "Discard this upload"
            _render_single_file_discard(path, label=discard_label)


# The near-miss card's own comparison fields - deliberately a SUBSET of
# DIFF_FIELDS, not every field: these 5 are what actually establishes
# "is this plausibly the same property" (identity + rough size), never
# free-text fields like special_features/contacts which say nothing about
# identity and would just add noise to a card that's already asking a
# reviewer to make one focused decision.
_NEAR_MISS_COMPARISON_FIELDS = ("building", "provider", "floor_unit", "address_1", "size_sqft")

# Short, lowercase, plain-English nouns for the near-miss summary SENTENCE
# specifically - display_utils.friendly_field_label's own labels (e.g.
# "Floor / Unit") read fine as a table header (see _render_near_miss_
# comparison_table) but awkwardly mid-sentence ("same building, provider,
# floor / unit, ..."), so the sentence uses this separate, shorter set.
_NEAR_MISS_FIELD_NOUNS = {
    "building": "building",
    "provider": "provider",
    "floor_unit": "floor",
    "address_1": "address",
    "size_sqft": "size",
}


def _near_miss_matching_and_differing(old_rec: dict, row_dict: dict) -> tuple:
    """
    Splits _NEAR_MISS_COMPARISON_FIELDS into (matching, differing) between
    `old_rec` (the closest master suggestion) and `row_dict` (the new
    upload row) - reuses master_merge.diff_fields' own blank-skip/
    tolerant-equality rules rather than a separate equality check, so this
    card's own "what differs" can never disagree with what "would change"
    already means everywhere else changes are reviewed on this page.
    """
    diffs = master_merge.diff_fields(old_rec, row_dict)
    matching = [f for f in _NEAR_MISS_COMPARISON_FIELDS if f not in diffs]
    differing = [f for f in _NEAR_MISS_COMPARISON_FIELDS if f in diffs]
    return matching, differing


def _oxford_join(items: list) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _render_near_miss_summary(old_rec: dict, row_dict: dict, matching: list, differing: list) -> None:
    """
    One short sentence, above the full comparison table, comparing
    `row_dict` against its closest suggestion `old_rec` across
    _NEAR_MISS_COMPARISON_FIELDS - names whatever matches, then whatever
    differs. A single differing field gets its own actual before/after
    values inline (short enough to stay one sentence); 2+ differing
    fields are just named, not spelled out one by one, to keep this
    genuinely short either way.
    """
    matching_nouns = [_NEAR_MISS_FIELD_NOUNS[f] for f in matching]
    if matching_nouns:
        sentence = f"This looks like a property already in master — same {_oxford_join(matching_nouns)}."
    else:
        sentence = "This looks like a property already in master."

    if differing:
        if len(differing) == 1:
            field = differing[0]
            noun = _NEAR_MISS_FIELD_NOUNS[field]
            old_display = display_utils.format_field_value_for_display(field, old_rec.get(field))
            new_display = display_utils.format_field_value_for_display(field, row_dict.get(field))
            old_phrase = "master has none recorded" if old_display == "—" else f"master says '{old_display}'"
            sentence += f" The only thing different is the {noun}: {old_phrase}, this upload says '{new_display}'."
        else:
            differing_nouns = [_NEAR_MISS_FIELD_NOUNS[f] for f in differing]
            sentence += f" The things that differ are {_oxford_join(differing_nouns)}."

    st.write(sentence)


def _render_near_miss_comparison_table(old_rec: dict, row_dict: dict, differing: list) -> None:
    """
    Full "In master" / "This upload" comparison across
    _NEAR_MISS_COMPARISON_FIELDS, replacing the old plain-text "Possible
    near-misses already in the master: ..." caption - same friendly-label/
    value formatting the rest of the page already uses (see display_
    utils.friendly_field_label/format_field_value_for_display), so this
    never introduces a second, inconsistent way of showing a field's
    value. Whichever field(s) `differing` names get a bold, background-
    tinted row so the one thing actually worth checking stands out from
    everything that already matches.
    """
    _, old_header_col, new_header_col = st.columns([2, 3, 3])
    with old_header_col:
        st.caption("IN MASTER")
    with new_header_col:
        st.caption("THIS UPLOAD")

    for field in _NEAR_MISS_COMPARISON_FIELDS:
        label = display_utils.friendly_field_label(field)
        old_display = display_utils.format_field_value_for_display(field, old_rec.get(field))
        new_display = display_utils.format_field_value_for_display(field, row_dict.get(field))
        label_col, old_col, new_col = st.columns([2, 3, 3])
        if field in differing:
            with label_col:
                st.markdown(f"**:orange-background[{label}]**")
            with old_col:
                st.markdown(f"**:orange-background[{old_display}]**")
            with new_col:
                st.markdown(f"**:orange-background[{new_display}]**")
        else:
            with label_col:
                st.write(label)
            with old_col:
                st.write(old_display)
            with new_col:
                st.write(new_display)


def _render_near_miss_link_diff(u, row_dict: dict, target_index: int, plan, key_prefix: str, decision_updates: dict) -> None:
    """
    Shared by both the single-suggestion Yes/No path and the multiple-
    suggestion dropdown path once a target master property has been
    chosen - computes the full field-level diff against that target and
    renders the same field-review UI a matched row's own manual review
    already uses (see _render_field_rows), recording anything the
    reviewer approves into decision_updates exactly as before this
    redesign. `diffs` can genuinely be empty (the linked property turns
    out to differ on nothing at all once blank/tolerant fields are
    excluded) - handled the same as always, a plain "0 field(s) would
    change" caption with nothing further to render.
    """
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
    plain_new_candidates = [u for u in plan.unmatched if id(u) not in collision_ids and not u.suggestions]
    # See master_merge.UnmatchedRow.let_status_fields - a genuinely new
    # property whose own special_features/state_of_space already says
    # "Under Offer"/"Let"/etc. gets pulled into its own decision below,
    # exactly like a matched row's let_status_fields already does (see
    # let_status_ids above) - never silently added via plain_new.
    decision_new_property_let_status = [u for u in plain_new_candidates if u.let_status_fields]
    plain_new = [u for u in plain_new_candidates if not u.let_status_fields]

    matched_master_indices = {m.master_index for m in plan.matched_changed} | {
        m.master_index for m in plan.matched_unchanged
    }
    stale_indices = master_merge.find_stale_candidates(
        new_rows, plan.master_records, matched_master_indices, fully_occupied_buildings=fully_occupied_buildings,
    )

    any_decisions = bool(
        decision_let_status or decision_collision_groups or decision_solo_collision or decision_risky
        or near_miss or plan.unmatched_collisions or stale_indices or decision_new_property_let_status
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

        for i, u in enumerate(decision_new_property_let_status):
            decision = _render_new_property_let_status_decision(u, f"new_let_status_{i}")
            if decision == "add":
                new_rows_final.append(u.new_row.model_copy(update={"property_id": str(uuid.uuid4())}))
            # "skip" - never added to master at all.
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
                closest = u.suggestions[0]
                matching, differing = _near_miss_matching_and_differing(closest, row_dict)

                with st.expander(f"⚠️ {display_utils.row_label(row_dict)}", key=f"{key_prefix}_expander"):
                    _render_near_miss_summary(closest, row_dict, matching, differing)
                    _render_near_miss_comparison_table(closest, row_dict, differing)

                    if len(u.suggestions) == 1:
                        # The common case - a plain Yes/No beats a dropdown
                        # whose only real choices are "this one property" or
                        # "add as new" anyway. 2+ suggestions (below) still
                        # need the dropdown - a button pair can't offer a
                        # choice between more than two things.
                        decision_key = f"{key_prefix}_yesno_decision"
                        if decision_key not in st.session_state:
                            # Matches the OLD selectbox's own default ("— add
                            # as new —" pre-selected until a reviewer acts) -
                            # never linked unless the reviewer explicitly says so.
                            st.session_state[decision_key] = "new"
                        same_property = st.session_state[decision_key] == "same"

                        yes_col, no_col = st.columns(2)
                        with yes_col:
                            if st.button(
                                "✓ Yes, same property", key=f"{key_prefix}_yes",
                                type="primary" if same_property else "secondary",
                            ):
                                st.session_state[decision_key] = "same"
                                st.rerun()
                        with no_col:
                            if st.button(
                                "Add as new instead", key=f"{key_prefix}_no",
                                type="primary" if not same_property else "secondary",
                            ):
                                st.session_state[decision_key] = "new"
                                st.rerun()

                        if same_property:
                            target_index = next(
                                idx for idx, rec in enumerate(plan.master_records)
                                if rec["property_id"] == closest["property_id"]
                            )
                            _render_near_miss_link_diff(u, row_dict, target_index, plan, key_prefix, decision_updates)
                        else:
                            new_rows_final.append(
                                u.new_row.model_copy(update={"property_id": str(uuid.uuid4())})
                            )
                    else:
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
                            _render_near_miss_link_diff(u, row_dict, target_index, plan, key_prefix, decision_updates)

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
        st.subheader("✅ Automatic updates")
        with st.expander("View changes"):
            excluded_auto_indices = _render_auto_updates_diff(plan, auto_updates, "auto_update")
        for idx in excluded_auto_indices:
            auto_updates.pop(idx, None)
        # Counted AFTER exclusion, so a checked "Don't apply this update"
        # box is reflected here too - not just in the final `updates` merge.
        n = len(auto_updates)
        st.info(f"{n} existing propert{'y' if n == 1 else 'ies'} will be updated automatically.")

    # ==== 3. New properties ====
    if plain_new:
        n = len(plain_new)
        st.subheader("📄 New properties")
        st.info(f"{n} new propert{'y' if n == 1 else 'ies'} will be added.")
        with st.expander("View new properties"):
            labels = master_merge.new_property_labels([u.new_row for u in plain_new])
            for label, u in zip(labels, plain_new):
                st.write(label)
                if master_merge.new_property_missing_location(u.new_row.model_dump()):
                    st.caption("📍 Address, postcode & map location not found — added anyway")
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
                    # horizontal=True sizes each button to its own content
                    # instead of stretching across equal-width st.columns -
                    # the same fix as "Undo this update" elsewhere on this
                    # page (see that container's own comment) applied here,
                    # so "Cancel" sits right beside "Confirm restore"
                    # instead of stranded at the 50% mark of the row's own
                    # full width.
                    with st.container(horizontal=True):
                        confirm_clicked = st.button("Confirm restore", key=f"{restore_key}_confirm", type="primary")
                        cancel_clicked = st.button("Cancel", key=f"{restore_key}_cancel")
                    if confirm_clicked:
                        with st.spinner("Restoring..."):
                            master_writer.restore_version(v["path"])
                        st.session_state[pending_key] = False
                        st.session_state["just_restored"] = v["path"]
                        st.rerun()
                    if cancel_clicked:
                        st.session_state[pending_key] = False
                        st.rerun()

    page_flow.render_nav_buttons("pages/2_Review_and_Master.py")
