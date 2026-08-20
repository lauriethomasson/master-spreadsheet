"""
report_state_of_space_gap.py

READ-ONLY report: scans master for rows showing the "special_features filled,
brochure_link present and eligible, but state_of_space blank" pattern.

Why this pattern can happen at all (see brochure_enrichment.py's own
_apply_units_to_row/_match_unit/_building_identity_matches docstrings):
special_features can be filled from a document-wide property_features
fallback that never requires a building-name match at all, while
state_of_space only ever comes from a confident single building+floor match
via _match_unit - which itself requires _building_identity_matches to
recognize the row's own `building` text as the SAME building the brochure's
extracted JSON calls it. If that building-name identity check fails (e.g. a
brochure branded under a marketing/proper name that shares no words at all
with the row's own building text - not just a missing street-suffix word),
state_of_space (and every other UNIT_LEVEL_FIELD) stays blank even while
special_features/contacts still get applied.

This script never calls master_writer.write_master()/blob_store.write_bytes()
- read-only, same convention as report_workplace_plus_london_provider.py. It
only reads master via master_writer.load_master_as_dataframe(), the same
path the app itself uses.

Usage:
    python report_state_of_space_gap.py [master_path]
"""

import sys

import master_writer
from brochure_enrichment import _is_blank, _is_eligible_brochure_url


def main():
    master_path = sys.argv[1] if len(sys.argv) > 1 else master_writer.DEFAULT_MASTER_PATH

    if not master_writer.master_exists(master_path):
        print(f"No master file found at {master_path!r} - nothing to report.")
        return

    df = master_writer.load_master_as_dataframe(master_path)

    affected = []
    for _, row in df.iterrows():
        special_features = row.get("special_features")
        state_of_space = row.get("state_of_space")
        brochure_link = row.get("brochure_link")

        if _is_blank(special_features):
            continue
        if not _is_blank(state_of_space):
            continue
        if not _is_eligible_brochure_url(brochure_link):
            continue

        affected.append({
            "provider": row.get("provider"),
            "building": row.get("building"),
            "floor_unit": row.get("floor_unit"),
            "brochure_link": brochure_link,
        })

    print(f"Master path: {master_path}")
    print(f"Total rows scanned: {len(df)}")
    print(f"Rows with special_features filled + eligible brochure_link + blank state_of_space: {len(affected)}")
    print()

    for r in affected:
        print(
            f"  provider={r['provider']!r}  building={r['building']!r}  "
            f"floor_unit={r['floor_unit']!r}  brochure_link={r['brochure_link']!r}"
        )

    if not affected:
        print("  (none found)")


if __name__ == "__main__":
    main()
