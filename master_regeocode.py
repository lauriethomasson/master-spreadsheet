"""
master_regeocode.py

Finds master rows worth re-checking against the CURRENT geocode_row logic
(e.g. after a geocoding bug fix lands) and re-runs geocoding for a chosen
selection, producing the same (merged_rows, diff_rows, fields_changed)
shape as master_merge.build_manual_edit - so the result rides the exact
same write_master()/versioning/undo path as any other master edit (see
pages/2_Review_and_Master.py).

Two independent reasons a row is "suspect" (see find_suspect_rows):
1. building is a compound "Name, Street Address" value (see
   geocode.split_compound_building) - the exact pattern that produced
   confidently wrong coordinates (Imperial House -> Baldwin's Gardens,
   911m off; Whitfield Court -> Whitfield Place, 513m off) or outright
   zero-results (Bridge House, 22 Newman Street) before geocode_row
   started trying the address portion alone first.
2. lat/lng is missing entirely, for any reason.
"""

import geocode
from master_merge import apply_merge, row_label
from schema import ListingRow

REGEOCODE_FIELDS = ("lat", "lng", "address_1", "postcode")


def find_suspect_rows(master_records: list) -> list:
    """
    master_records: cleaned master dicts (see storage.file_store.clean_value),
    in the same order/indexing as the caller will pass into regeocode_rows -
    the same master_records list every other master_merge/master_writer
    caller already builds from load_master_as_dataframe().to_dict(orient=
    "records").

    Returns [{"index", "row_label", "building", "reason"}, ...] - index is
    the master_records position, matching apply_merge's updates dict key
    convention.
    """
    suspects = []
    for index, record in enumerate(master_records):
        building = record.get("building")
        reasons = []
        if building and geocode.split_compound_building(building):
            reasons.append("compound building name/address")
        if record.get("lat") is None or record.get("lng") is None:
            reasons.append("no coordinates on file")
        if reasons:
            suspects.append({
                "index": index,
                "row_label": row_label(record),
                "building": building,
                "reason": "; ".join(reasons),
            })
    return suspects


def regeocode_rows(master_records: list, indices: list) -> tuple:
    """
    Re-runs geocode_row for master_records[i], each i in indices, forcing a
    real lookup by clearing lat/lng first - geocode_row otherwise short-
    circuits immediately on an already-populated lat/lng (see its own
    early-return comment), which is exactly right for a first-time geocode
    but would make a re-geocode pass a permanent no-op.

    Returns (merged_rows, diff_rows, fields_changed) - identical shape to
    master_merge.build_manual_edit, so the caller can save the result
    through the exact same write_master() call a manual cell edit already
    uses, just with a different `source` string ("re-geocode" rather than
    "manual_edit").

    A row whose re-geocode produces no actual field change (the fix didn't
    change anything for that particular row, or the API returns the same
    values again) contributes nothing to updates/diff_rows - re-running
    this pass repeatedly is always safe and idempotent, never manufactures
    a spurious diff/version.
    """
    updates = {}
    diff_rows = []

    for index in indices:
        record = master_records[index]
        before = {field: record.get(field) for field in REGEOCODE_FIELDS}

        row = ListingRow(**{k: v for k, v in record.items() if k in ListingRow.model_fields})
        row.lat = None
        row.lng = None

        # A compound building value is exactly the scenario the fix
        # addresses - its existing address_1/postcode may themselves be
        # corrupted by that same bug (a "valid but wrong-for-this-row"
        # address - e.g. Imperial House's stored "16 Baldwin's Gardens,
        # EC1N 7RJ", a real address, just for the wrong building 911m
        # away). Left in place, geocode_row's Tier 1 (address_1+postcode
        # already present) would just re-confirm that same wrong address
        # via the Geocoding API and return immediately, NEVER reaching
        # Tier 2's fixed compound-address-first logic at all - confirmed
        # by actually running this against the real broken production
        # rows before adding this clear. Left alone for a row that's only
        # missing coordinates with a plain (non-compound) building name -
        # its address_1/postcode, if present, came from a genuine source
        # (e.g. read directly off a PDF), not from this bug, and
        # shouldn't be discarded just to force a lookup Tier 1 would
        # already handle correctly with what's already there.
        if row.building and geocode.split_compound_building(row.building):
            row.address_1 = None
            row.postcode = None

        geocode.geocode_row(row)

        changed = {}
        for field in REGEOCODE_FIELDS:
            new_value = getattr(row, field)
            if new_value != before[field]:
                changed[field] = new_value

        if changed:
            updates[index] = changed
            label = row_label(record)
            for field, new_value in changed.items():
                diff_rows.append({
                    "property": label, "field": field, "old": before[field], "new": new_value,
                })

    fields_changed = sum(len(v) for v in updates.values())
    merged_rows = apply_merge(master_records, updates, [])
    return merged_rows, diff_rows, fields_changed
