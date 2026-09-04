"""
report_state_of_space_gap_causes.py

READ-ONLY diagnostic: for every row report_state_of_space_gap.py already
finds (special_features filled, brochure_link eligible, but state_of_space
blank), actually runs the REAL matching pipeline (real fetch, real Gemini
call - nothing mocked) against that row's own brochure_link and
categorizes WHY state_of_space never got filled, using the exact same real
helper functions brochure_enrichment.py itself calls:

  1. BUILDING_NOT_MATCHED - _row_building_match_indices(row, ...) returned
     zero candidate indices at all, even after trying row.building itself,
     a sub-building name embedded in row.floor_unit, and row.address_1 (see
     that function's own docstring for the real cases each of those three
     signals closes). The brochure's own extracted building name(s) never
     resolved to this row's own identity by any of them - a real building-
     identity-matching gap, the class of bug this repo's own tier 3d/3e/3f
     fixes (and _row_building_match_indices itself) have already closed
     several real cases of.
  2. AMBIGUOUS_NARROWING - _row_building_match_indices found 2+ candidates,
     but _match_unit(row, units) still returned None - floor_unit/
     size_sqft couldn't narrow to exactly one. Genuine document ambiguity
     (or a floor-label mismatch _floor_number doesn't yet bridge), not a
     building-identity gap.
  3. MATCHED_BUT_BLANK - _match_unit found a confident single unit, but
     that unit's own state_of_space was null/blank in Gemini's own
     extraction - the brochure simply never stated a fit-out for that
     floor. Not a matching bug at all.
  4. FETCH_FAILED - _extract_brochure_units(url) returned None for this
     row's own brochure_link - the real fetch/render/extraction itself
     failed (report_state_of_space_gap.py's own filter already requires
     _is_eligible_brochure_url, so this is never a URL-shape/eligibility
     rejection here, only a genuine fetch-time failure - still checked and
     reported per-row for transparency in case anything upstream changed
     since the master export this runs against).

Distinct brochure_link values are fetched ONCE each (many rows share one
url - a real schedule-of-areas brochure covering several floors) via
_extract_brochure_units's own in-process cache PLUS an explicit dict here,
so a real run with N rows across M << N distinct brochures only ever
costs M real Gemini calls, not N.

Never calls master_writer.write_master()/blob_store.write_bytes() and
never mutates brochure_enrichment.py's own matching logic - same
"read-only, report only" convention as report_state_of_space_gap.py.

Usage:
    python report_state_of_space_gap_causes.py [master_path]
"""

import sys
from collections import Counter

import master_writer
from brochure_enrichment import (
    _extract_brochure_units,
    _is_blank,
    _is_eligible_brochure_url,
    _match_unit,
    _row_building_match_indices,
    needs_enrichment,
)
from schema import ListingRow

BUILDING_NOT_MATCHED = "1_BUILDING_NOT_MATCHED"
AMBIGUOUS_NARROWING = "2_AMBIGUOUS_NARROWING"
MATCHED_BUT_BLANK = "3_MATCHED_BUT_BLANK"
FETCH_FAILED = "4_FETCH_FAILED"
NOT_ELIGIBLE = "4_NOT_ELIGIBLE"  # see module docstring point 4 - never expected among these rows, kept for transparency

CATEGORY_LABELS = {
    BUILDING_NOT_MATCHED: "Building didn't match at all",
    AMBIGUOUS_NARROWING: "Building matched 2+, floor/size couldn't narrow to one",
    MATCHED_BUT_BLANK: "Matched, but brochure's own state_of_space was blank",
    FETCH_FAILED: "Fetch/extraction of the brochure itself failed",
    NOT_ELIGIBLE: "Row no longer eligible (needs_enrichment/URL shape)",
}


def _nan_to_none(value):
    """
    `value` unchanged, except a pandas/numpy float NaN becomes None.

    Real, confirmed gap this closes - NOT a bug in the live enrichment
    pipeline itself (a ListingRow's own fields are never raw floats for a
    blank text field - Pydantic validation on the real extraction path
    never produces this shape), but a genuine gap in reading a persisted
    master.xlsx BACK into a raw pandas DataFrame for reporting purposes,
    which report_state_of_space_gap.py already also does: a blank Excel
    cell in an otherwise-numeric-dominant or mixed column round-trips
    through pandas' own DataFrame construction as float('nan'), not None -
    confirmed directly against the real production master export this
    runs against (state_of_space for real, genuinely-blank rows). _is_blank
    (brochure_enrichment.py) only ever recognizes `None` or an all-
    whitespace string as blank - `value is nan` is neither, so a raw
    DataFrame row's own blank cell silently reads as "not blank" to it,
    which was actually causing report_state_of_space_gap.py's own filter
    to SILENTLY UNDER-COUNT (in this real master export, to zero) rather
    than merely to leave the "why" unexplained. NaN is never itself a
    valid value for any of the fields this script or ListingRow read from
    a master row, so converting it to None here - before either _is_blank
    or ListingRow ever sees it - is always correct, never lossy.
    """
    if isinstance(value, float) and value != value:  # NaN is the one float that never equals itself
        return None
    return value


def _find_affected_rows(df) -> list:
    """Same filter as report_state_of_space_gap.py's own main() - a row
    with special_features filled, an eligible brochure_link, and
    state_of_space still blank - EXCEPT NaN-safe (see _nan_to_none's own
    docstring for the real, confirmed gap this closes that report_state_
    of_space_gap.py itself does not yet account for)."""
    affected = []
    for _, row in df.iterrows():
        special_features = _nan_to_none(row.get("special_features"))
        state_of_space = _nan_to_none(row.get("state_of_space"))
        brochure_link = _nan_to_none(row.get("brochure_link"))

        if _is_blank(special_features):
            continue
        if not _is_blank(state_of_space):
            continue
        if not _is_eligible_brochure_url(brochure_link):
            continue

        affected.append({
            "provider": _nan_to_none(row.get("provider")),
            "building": _nan_to_none(row.get("building")),
            "floor_unit": _nan_to_none(row.get("floor_unit")),
            "address_1": _nan_to_none(row.get("address_1")),
            "size_sqft": _nan_to_none(row.get("size_sqft")),
            "brochure_link": brochure_link,
        })
    return affected


def _listing_row_for(row: dict) -> ListingRow:
    # address_1 included (unlike the rest of this script's own fields
    # before it) specifically because _row_building_match_indices reads it
    # as a fallback identity signal when row["building"] alone matches
    # nothing - omitting it here would silently under-report BUILDING_NOT_
    # MATCHED for exactly the real Northumberland House-shaped case that
    # fallback exists for.
    return ListingRow(
        building=row["building"], floor_unit=row.get("floor_unit"), size_sqft=row.get("size_sqft"),
        provider=row.get("provider"), brochure_link=row.get("brochure_link"), address_1=row.get("address_1"),
    )


def _categorize(row: dict, units) -> str:
    if units is None:
        return FETCH_FAILED

    listing_row = _listing_row_for(row)

    match_indices = _row_building_match_indices(
        listing_row,
        [u.get("building") for u in units if isinstance(u, dict)],
        [u.get("address_1") for u in units if isinstance(u, dict)],
    )
    if not match_indices:
        return BUILDING_NOT_MATCHED

    matched_unit = _match_unit(listing_row, units)
    if matched_unit is None:
        return AMBIGUOUS_NARROWING

    if _is_blank(matched_unit.get("state_of_space")):
        return MATCHED_BUT_BLANK

    # Matched AND the unit's own state_of_space is non-blank - shouldn't
    # be reachable given this row was selected specifically because state_
    # of_space stayed blank in master, but reported plainly rather than
    # silently miscategorized if the live document has since changed.
    return "0_MATCHED_WITH_STATE_OF_SPACE (document has changed since the master row was written)"


def main():
    master_path = sys.argv[1] if len(sys.argv) > 1 else master_writer.DEFAULT_MASTER_PATH

    if not master_writer.master_exists(master_path):
        print(f"No master file found at {master_path!r} - nothing to report.")
        return

    df = master_writer.load_master_as_dataframe(master_path)
    affected = _find_affected_rows(df)

    print(f"Master path: {master_path}")
    print(f"Rows with special_features filled + eligible brochure_link + blank state_of_space: {len(affected)}")

    distinct_urls = list(dict.fromkeys(r["brochure_link"] for r in affected))
    print(f"Distinct brochure_link values among them: {len(distinct_urls)}")
    print()

    units_by_url = {}
    for i, url in enumerate(distinct_urls, start=1):
        print(f"[{i}/{len(distinct_urls)}] Fetching + extracting {url!r} ...", file=sys.stderr)
        try:
            units_by_url[url] = _extract_brochure_units(url)
        except Exception as e:
            print(f"    -> raised {e!r}, treating as fetch failure", file=sys.stderr)
            units_by_url[url] = None

    results = []
    for row in affected:
        if not needs_enrichment(_listing_row_for(row)) or not _is_eligible_brochure_url(row["brochure_link"]):
            category = NOT_ELIGIBLE
        else:
            category = _categorize(row, units_by_url.get(row["brochure_link"]))
        results.append({**row, "category": category})

    counts = Counter(r["category"] for r in results)

    print()
    print("=== Category breakdown ===")
    for category in sorted(counts):
        label = CATEGORY_LABELS.get(category, category)
        print(f"  {category}: {counts[category]}  ({label})")

    for category in (BUILDING_NOT_MATCHED, AMBIGUOUS_NARROWING):
        print()
        print(f"=== {category} - {CATEGORY_LABELS[category]} ===")
        rows_in_category = [r for r in results if r["category"] == category]
        if not rows_in_category:
            print("  (none)")
        for r in rows_in_category:
            print(
                f"  provider={r['provider']!r}  building={r['building']!r}  floor_unit={r['floor_unit']!r}  "
                f"size_sqft={r['size_sqft']!r}  brochure_link={r['brochure_link']!r}"
            )


if __name__ == "__main__":
    main()
