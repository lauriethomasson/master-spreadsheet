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


def _values_equal(old, new) -> bool:
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return abs(float(old) - float(new)) < 1e-6
    return str(old).strip() == str(new).strip()


def diff_fields(old: dict, new: dict) -> dict:
    """
    Only fields present in `new` (non-blank) and different from `old` are
    included - a blank/missing value in a fresh extraction is treated as "no
    data this time", never as a change that would blank out an existing
    value. A field filled in for the first time (old blank, new has a value)
    is treated as a change like any other. Returns {field: (old, new)}.
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
            matched = MatchedRow(master_idx, old_rec["property_id"], new_row, diffs, tier)
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
