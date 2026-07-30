"""
master_merge.py

Computes a merge plan for folding a batch of newly-extracted rows into the
cumulative master spreadsheet: matches each new row against current master
rows by a layered, normalized key, diffs matched rows field-by-field, and
separates out same-batch collisions so a human resolves them explicitly
instead of write order silently deciding a winner.

Match key: normalized building + provider + postcode + floor_unit when the
new row has a postcode, falling back to building + provider + floor_unit
(ignoring postcode entirely) otherwise or when the postcode-inclusive key
finds nothing. floor_unit is load-bearing here - matching on
building + provider + postcode alone (an earlier draft of this design)
collapses every distinct unit in a multi-unit building from the same
provider onto the same row, which real data in this repo already
demonstrates (16 Dufour's Place / GPE has 4 separate floor units sharing
one building+provider+postcode).

Pure logic, no Streamlit/storage - pages/2_Review_and_Master.py renders a
MergePlan and turns the user's decisions into the final row list via
apply_merge(); master_writer.write_master() only ever sees that final list
and has no awareness that a merge happened at all.
"""

import difflib
import re
import typing
import uuid
from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from schema import ListingRow
from storage.file_store import clean_value

# building/provider/floor_unit/postcode are matching keys, not excluded from
# diffing - a matched row can still show one of them as a changed field (e.g.
# a postcode filled in via the fallback tier, or a capitalization fix), and
# that's exactly the diff-and-merge value proposition working as intended.
# property_id is internal bookkeeping and source_file is handled separately
# (see pages/2_Review_and_Master.py) - neither belongs in a field-by-field diff.
DIFF_FIELDS = [f for f in ListingRow.model_fields if f not in ("property_id", "source_file")]


def normalize_key(value) -> str:
    """Lowercase, strip punctuation, collapse whitespace - deliberately
    conservative (e.g. doesn't strip a leading "The"), so a near-miss surfaces
    as "no match" for a human to catch rather than being silently guessed
    away by more aggressive fuzzy logic."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).lower()
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def field_kind(field_name: str) -> str:
    """"int" | "float" | "str" - derived from ListingRow's own type hints
    (via typing.get_args on the Optional[...] annotation) rather than a
    hardcoded list, so it can't drift out of sync with schema.py."""
    annotation = ListingRow.model_fields[field_name].annotation
    args = typing.get_args(annotation)
    base = next((a for a in args if a is not type(None)), annotation)
    if base is int:
        return "int"
    if base is float:
        return "float"
    return "str"


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _normalize_text(value) -> str:
    """Case- and whitespace-insensitive form of a text value, for tolerant
    comparison only (never used for what's actually stored) - lowercases
    and discards ALL whitespace, not just repeated runs, so a formatting-
    only difference like "+44(0)7837 270455" vs "+44(0)7837270455", or a
    stray capitalization difference like "METSPACE" vs "Metspace", doesn't
    register as a real change. Punctuation is left alone: a dash swapped
    for a space (or missing entirely) still compares different, since that
    can indicate an actual typo or a different source worth a human's
    attention, unlike whitespace/case."""
    return re.sub(r"\s+", "", str(value).strip().lower())


def _values_equal(old, new) -> bool:
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return abs(float(old) - float(new)) < 1e-6
    return _normalize_text(old) == _normalize_text(new)


def diff_fields(old: dict, new: dict) -> dict:
    """
    Only fields present in `new` (non-blank), different from `old`, and NOT
    merely a tolerant-equal formatting difference (see _values_equal /
    silent_field_updates) are included - a blank/missing value in a fresh
    extraction is treated as "no data this time", never as a change that
    would blank out an existing value. A field filled in for the first time
    (old blank, new has a value) is treated as a change like any other.
    Returns {field: (old, new)}.
    """
    diffs = {}
    for f in DIFF_FIELDS:
        new_val = new.get(f)
        if _is_blank(new_val):
            continue
        old_val = old.get(f)
        if _is_blank(old_val) or not _values_equal(old_val, new_val):
            diffs[f] = (old_val, new_val)
    return diffs


def silent_field_updates(old: dict, new: dict) -> dict:
    """
    Text fields where `new` is tolerant-equal to `old` (see _values_equal)
    but not byte-identical - e.g. a phone number gaining a missing space, or
    "METSPACE" replacing "Metspace". diff_fields() correctly excludes these
    from the review-worthy diff (see its docstring), but the improved
    formatting should still replace the old value in master rather than
    being silently discarded on every future upload - it's just applied
    without ever surfacing as a "change" a human needs to review. Restricted
    to str-kind fields: numeric near-equality (already tolerated by
    _values_equal) is a different, pre-existing concern and isn't affected
    here. Returns {field: new_value}.
    """
    updates = {}
    for f in DIFF_FIELDS:
        if field_kind(f) != "str":
            continue
        new_val = new.get(f)
        if _is_blank(new_val):
            continue
        old_val = old.get(f)
        if _is_blank(old_val):
            continue
        if str(old_val) == str(new_val):
            continue
        if _values_equal(old_val, new_val):
            updates[f] = new_val
    return updates


def _fallback_key(row: dict) -> tuple:
    return (
        normalize_key(row.get("building")),
        normalize_key(row.get("provider")),
        normalize_key(row.get("floor_unit")),
    )


def _primary_key(row: dict):
    if _is_blank(row.get("postcode")):
        return None
    return _fallback_key(row) + (normalize_key(row.get("postcode")),)


def row_label(row_dict: dict) -> str:
    parts = [row_dict.get("building") if not _is_blank(row_dict.get("building")) else "(no building)"]
    if not _is_blank(row_dict.get("provider")):
        parts.append(row_dict["provider"])
    if not _is_blank(row_dict.get("floor_unit")):
        parts.append(row_dict["floor_unit"])
    return " — ".join(parts)


def new_property_labels(rows: list) -> list:
    """
    One plain "{address_1} — {provider} — {floor_unit}" label per row, for
    the purely-informational "will be added as new" list on the Review page
    - floor_unit is appended only when needed to tell two rows in this same
    batch apart (i.e. more than one shares the same address_1, like 111
    Wardour Street's three separate floors); a single new property at a
    unique address is just "{address_1} — {provider}". Grouping uses
    normalize_key so two rows that share an address but differ only in
    case/punctuation/whitespace still count as the same address. A floor
    range or multiple non-contiguous floors extracted together (e.g. "4th &
    5th Floors") is untouched here - floor_unit is a single field, so
    whatever formatting the extraction produced for it is preserved as-is,
    never split into separate rows/labels.
    """
    dicts = [r.model_dump() for r in rows]
    address_counts = Counter(normalize_key(d.get("address_1")) for d in dicts)

    labels = []
    for d in dicts:
        parts = [d.get("address_1") if not _is_blank(d.get("address_1")) else "(no address)"]
        if not _is_blank(d.get("provider")):
            parts.append(d["provider"])
        shares_address = address_counts[normalize_key(d.get("address_1"))] > 1
        if shares_address and not _is_blank(d.get("floor_unit")):
            parts.append(d["floor_unit"])
        labels.append(" — ".join(parts))
    return labels


def _suggest_similar(new_dict: dict, master_records: list) -> list:
    """Cheap, stdlib-only fuzzy hint for the "no match" review section - not
    part of matching itself, just reduces manual searching when a near-miss
    (e.g. a slightly different building spelling) is the real cause."""
    target = normalize_key(new_dict.get("building"))
    if not target:
        return []
    keys = [normalize_key(r.get("building")) for r in master_records]
    close = set(difflib.get_close_matches(target, keys, n=3, cutoff=0.6))
    seen = set()
    results = []
    for rec, key in zip(master_records, keys):
        if key in close and key not in seen:
            seen.add(key)
            results.append(rec)
    return results


@dataclass
class MatchedRow:
    master_index: int
    property_id: str
    new_row: ListingRow
    diffs: dict
    match_tier: str  # "postcode" or "fallback"
    silent_updates: dict = field(default_factory=dict)  # see silent_field_updates - never shown in the diff-review UI


@dataclass
class UnmatchedRow:
    new_row: ListingRow
    suggestions: list = field(default_factory=list)


@dataclass
class MergePlan:
    master_records: list          # current master rows as cleaned dicts (property_id backfilled)
    matched_changed: list          # list[MatchedRow], diffs non-empty
    matched_unchanged: list        # list[MatchedRow], diffs empty
    unmatched: list                # list[UnmatchedRow]
    collisions: list               # list[list[MatchedRow]] - multiple incoming rows targeting the same master row
    unmatched_collisions: list     # list[list[UnmatchedRow]] - multiple incoming rows matching each other, no master row


def build_merge_plan(new_rows: list, master_df: pd.DataFrame) -> MergePlan:
    master_records = [
        {key: clean_value(value) for key, value in rec.items()}
        for rec in master_df.to_dict(orient="records")
    ]
    for rec in master_records:
        if _is_blank(rec.get("property_id")):
            rec["property_id"] = str(uuid.uuid4())

    primary_index = {}
    fallback_index = {}
    for i, rec in enumerate(master_records):
        pk = _primary_key(rec)
        if pk:
            primary_index.setdefault(pk, []).append(i)
        fallback_index.setdefault(_fallback_key(rec), []).append(i)

    matched_changed, matched_unchanged, unmatched = [], [], []

    for new_row in new_rows:
        new_dict = new_row.model_dump()
        master_idx, tier = None, None

        pk = _primary_key(new_dict)
        if pk and len(primary_index.get(pk, [])) == 1:
            master_idx, tier = primary_index[pk][0], "postcode"
        else:
            candidates = fallback_index.get(_fallback_key(new_dict), [])
            if len(candidates) == 1:
                master_idx, tier = candidates[0], "fallback"
            # 0 or >1 candidates both fall through as unmatched - an
            # ambiguous fallback match is exactly as unsafe to auto-apply as
            # no match at all.

        if master_idx is not None:
            old_rec = master_records[master_idx]
            diffs = diff_fields(old_rec, new_dict)
            silent = silent_field_updates(old_rec, new_dict)
            matched = MatchedRow(master_idx, old_rec["property_id"], new_row, diffs, tier, silent)
            (matched_changed if diffs else matched_unchanged).append(matched)
        else:
            unmatched.append(UnmatchedRow(new_row, _suggest_similar(new_dict, master_records)))

    by_master_idx = {}
    for m in matched_changed:
        by_master_idx.setdefault(m.master_index, []).append(m)
    collisions = [group for group in by_master_idx.values() if len(group) > 1]

    by_key = {}
    for u in unmatched:
        by_key.setdefault(_fallback_key(u.new_row.model_dump()), []).append(u)
    unmatched_collisions = [group for group in by_key.values() if len(group) > 1]

    return MergePlan(master_records, matched_changed, matched_unchanged, unmatched, collisions, unmatched_collisions)


def apply_merge(master_records: list, updates: dict, new_rows: list) -> list:
    """
    master_records: full current master (property_id already backfilled), as
    plain dicts, in original order - untouched rows pass through verbatim.
    updates: {master_index: {field: approved_value, ...}} for rows with at
    least one approved change - only the approved fields are overlaid.
    new_rows: fully-formed ListingRow objects (property_id already assigned)
    confirmed as genuinely new, appended after all existing rows.
    """
    result = []
    for i, rec in enumerate(master_records):
        merged = dict(rec)
        if i in updates:
            merged.update(updates[i])
        result.append(ListingRow(**{k: v for k, v in merged.items() if k in ListingRow.model_fields}))
    result.extend(new_rows)
    return result


def pending_status_line(n_uploads: int, plan: MergePlan) -> str:
    """
    Plain, sentence-case summary of what a pending batch actually contains -
    zero-count clauses are dropped entirely rather than spelled out (e.g.
    never "0 matched with changes"), so this reads naturally whether the
    batch is all-new, all-changes, a mix, or (rare) entirely unchanged.
    """
    parts = []
    if plan.unmatched:
        n = len(plan.unmatched)
        parts.append(f"{n} new propert{'y' if n == 1 else 'ies'}")
    if plan.matched_changed:
        n = len(plan.matched_changed)
        parts.append(f"{n} propert{'y' if n == 1 else 'ies'} with changes")

    headline = f"{n_uploads} upload{'s' if n_uploads != 1 else ''} pending"
    if parts:
        headline += " — " + ", ".join(parts)
    elif plan.matched_unchanged:
        headline += " — no changes"
    return headline


def build_approval_summary(plan: MergePlan, updates: dict, new_rows_final: list) -> tuple:
    """
    Compact, read-only diff data for a post-approve confirmation UI - plan/
    updates/new_rows_final are all local to whatever render pass computed
    them and typically gone by the time the confirmation is shown (e.g.
    after a Streamlit rerun), so this is what a caller persists instead.
    Returns (diff_rows, new_labels): diff_rows is a list of
    {"property", "field", "old", "new"} dicts (one per approved field
    change), new_labels is a list of row_label() strings for genuinely new
    properties.
    """
    diff_rows = []
    for master_index, fields in updates.items():
        old_rec = plan.master_records[master_index]
        label = row_label(old_rec)
        for field_name, new_val in fields.items():
            if field_name == "source_file":
                continue  # internal bookkeeping, not a meaningful change to show
            diff_rows.append({"property": label, "field": field_name, "old": old_rec.get(field_name), "new": new_val})
    new_labels = [row_label(r.model_dump()) for r in new_rows_final]
    return diff_rows, new_labels
