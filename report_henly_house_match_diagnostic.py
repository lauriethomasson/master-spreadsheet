"""
report_henly_house_match_diagnostic.py

READ-ONLY diagnostic: runs the REAL brochure-enrichment matching pipeline
(real network fetch, real Gemini call - nothing mocked) against the exact,
real, repeatedly-reported Henly House brochure_link, for the exact two real
master rows (building "Henly House", floor_unit "1st Floor"/"4th Floor")
that keep coming back with address_1/postcode/state_of_space still blank
despite numerous re-uploads.

Never calls master_writer.write_master()/blob_store.write_bytes() and never
mutates brochure_enrichment.py's own matching logic - same "read-only,
report only" convention as report_state_of_space_gap.py. Prints, in order:

1. The raw Gemini units JSON from _extract_brochure_units(url) - every
   unit's building/floor_unit/size_sqft/state_of_space/address_1, verbatim.
2. _building_identity_matches(row.building, ...) for each of the two rows -
   which candidate indices (if any) it returns.
3. The real _match_unit(row, units) result for each row, plus a step-by-
   step trace of _match_unit's OWN internal tiers (building match count,
   then exact-floor-text/floor-number/size narrowing) built from the SAME
   real helper functions _match_unit itself calls - so a None result is
   never left unexplained.
4. needs_enrichment(row) and _is_eligible_brochure_url(row.brochure_link)
   for both rows, ruling out (or confirming) that these rows never even
   reach the matching logic in the first place.

Usage:
    python report_henly_house_match_diagnostic.py
"""

from brochure_enrichment import (
    _building_identity_matches,
    _extract_brochure_units,
    _floor_number,
    _is_eligible_brochure_url,
    _match_unit,
    _safe_float,
    _SIZE_MATCH_MIN_TOLERANCE_SQFT,
    _SIZE_MATCH_TOLERANCE_FRACTION,
    needs_enrichment,
    normalize_key,
)
from schema import ListingRow

HENLY_URL = (
    "https://b41f7e06-d641-4086-850b-cf4ffc542106.filesusr.com/ugd/"
    "c45d78_6784dd91706945dd8d50701e2b2ff873.pdf"
)

# The exact two real master rows this keeps failing for (see master
# (27).xlsx - Colliers, building "Henly House", floor_unit "4th Floor"/
# "1st Floor", brochure_link the URL above, size_sqft as currently on
# file). address_1/postcode/state_of_space are the fields staying blank.
ROWS = [
    ListingRow(
        internal_ref="Colliers", provider="Colliers", building="Henly House",
        floor_unit="4th Floor", size_sqft=1846.0, brochure_link=HENLY_URL,
    ),
    ListingRow(
        internal_ref="Colliers", provider="Colliers", building="Henly House",
        floor_unit="1st Floor", size_sqft=1927.0, brochure_link=HENLY_URL,
    ),
]


def _print_unit(u: dict) -> None:
    print(
        f"    building={u.get('building')!r}  floor_unit={u.get('floor_unit')!r}  "
        f"size_sqft={u.get('size_sqft')!r}  state_of_space={u.get('state_of_space')!r}  "
        f"address_1={u.get('address_1')!r}"
    )


def _trace_match_unit(row: ListingRow, units: list, match_indices: list) -> None:
    """
    Re-derives, step by step, the SAME tiers _match_unit itself runs
    internally (see that function's own source) - never a second,
    independently-written matching algorithm, just the identical
    building_matches/floor_matches/number_matches/size_matches values it
    computes, printed so a None result is never left unexplained.
    """
    plain_units = [u for u in units if isinstance(u, dict)]
    building_matches = [plain_units[i] for i in match_indices]
    print(f"    building_matches: {len(building_matches)}")
    if not building_matches:
        print("    -> _match_unit returns None: ZERO building matches (tier 1-5 of "
              "_building_identity_matches all failed to identify this row's own "
              "building in the brochure's own units list).")
        return
    if len(building_matches) == 1:
        print("    -> exactly ONE building match: _match_unit returns it directly, "
              "no floor/size narrowing needed at all.")
        return

    print(f"    (2+ building matches - floor_unit/size_sqft must narrow to exactly one)")
    if row.floor_unit:
        row_floor_key = normalize_key(row.floor_unit)
        floor_matches = [u for u in building_matches if normalize_key(u.get("floor_unit")) == row_floor_key]
        print(f"    exact floor_unit text match (row key {row_floor_key!r}): {len(floor_matches)} candidate(s)")
        if len(floor_matches) == 1:
            print("    -> _match_unit returns the exact floor_unit text match.")
            return

        row_floor_number = _floor_number(row.floor_unit)
        print(f"    row's own _floor_number(floor_unit): {row_floor_number!r}")
        if row_floor_number is not None:
            number_matches = [u for u in building_matches if _floor_number(u.get("floor_unit")) == row_floor_number]
            candidate_numbers = [(u.get("floor_unit"), _floor_number(u.get("floor_unit"))) for u in building_matches]
            print(f"    candidate floor_unit -> _floor_number: {candidate_numbers}")
            print(f"    floor-number match: {len(number_matches)} candidate(s)")
            if len(number_matches) == 1:
                print("    -> _match_unit returns the floor-number match.")
                return
    else:
        print("    row.floor_unit is blank - exact-text/floor-number tiers both skipped entirely.")

    if row.size_sqft:
        tolerance = max(_SIZE_MATCH_MIN_TOLERANCE_SQFT, row.size_sqft * _SIZE_MATCH_TOLERANCE_FRACTION)
        candidate_sizes = [(u.get("floor_unit"), _safe_float(u.get("size_sqft"))) for u in building_matches]
        size_matches = [
            u for u in building_matches
            if _safe_float(u.get("size_sqft")) is not None
            and abs(_safe_float(u["size_sqft"]) - row.size_sqft) <= tolerance
        ]
        print(f"    row size_sqft={row.size_sqft!r}, tolerance=±{tolerance!r}")
        print(f"    candidate floor_unit -> size_sqft: {candidate_sizes}")
        print(f"    size match: {len(size_matches)} candidate(s)")
        if len(size_matches) == 1:
            print("    -> _match_unit returns the size match.")
            return
    else:
        print("    row.size_sqft is blank - size tier skipped entirely.")

    print("    -> _match_unit returns None: every narrowing tier stayed ambiguous "
          "(0 or 2+ candidates) after 2+ building matches.")


def main():
    print(f"brochure_link: {HENLY_URL}")
    print()

    print("=== Step 4 (checked first): eligibility, before any fetch ===")
    for row in ROWS:
        print(
            f"  floor_unit={row.floor_unit!r}: "
            f"needs_enrichment={needs_enrichment(row)!r}  "
            f"_is_eligible_brochure_url={_is_eligible_brochure_url(row.brochure_link)!r}"
        )
    print()

    print("=== Step 1: real _extract_brochure_units(url) - real fetch + real Gemini call ===")
    units = _extract_brochure_units(HENLY_URL)
    if units is None:
        print("  _extract_brochure_units returned None - fetch/render/extraction FAILED entirely. Stopping.")
        return
    print(f"  {len(units)} unit(s) returned:")
    for u in units:
        _print_unit(u)
    print(f"  document-level property_features: {getattr(units, 'property_features', None)!r}")
    print(f"  document-level contacts: {getattr(units, 'contacts', None)!r}")
    print(f"  building_features: {getattr(units, 'building_features', None)!r}")
    print()

    plain_units = [u for u in units if isinstance(u, dict)]
    candidate_buildings = [u.get("building") for u in plain_units]
    candidate_addresses = [u.get("address_1") for u in plain_units]

    for row in ROWS:
        print(f"=== Row: building={row.building!r}  floor_unit={row.floor_unit!r}  size_sqft={row.size_sqft!r} ===")

        print("--- Step 2: _building_identity_matches(row.building, candidate_buildings, candidate_addresses) ---")
        match_indices = _building_identity_matches(row.building, candidate_buildings, candidate_addresses)
        print(f"    row.building normalized key: {normalize_key(row.building)!r}")
        print(f"    candidate buildings: {candidate_buildings}")
        print(f"    match_indices: {match_indices}")
        if match_indices:
            print(f"    -> matches: {[candidate_buildings[i] for i in match_indices]}")
        print()

        print("--- Step 3: real _match_unit(row, units) + tier-by-tier trace ---")
        result = _match_unit(row, units)
        print(f"    _match_unit real result: {result}")
        if result is None:
            _trace_match_unit(row, units, match_indices)
        print()


if __name__ == "__main__":
    main()
