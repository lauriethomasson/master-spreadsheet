"""
merge_packing_house_g_ground_duplicate.py

ONE-OFF script: removes the confirmed real duplicate "Ground Floor" row for
Packing House (Colliers), keeping the "G" row's own (fuller) data - the
exact real production case _floor_unit_key's new "g" <-> "ground"
equivalence (see master_merge.py) now prevents from happening again on any
FUTURE upload, but does not retroactively fix.

Identified rows (confirmed against master (27).xlsx, both size_sqft=1084,
both building="Packing House"/provider="Colliers" - genuinely the same
physical listing, duplicated only because "G" and "Ground Floor" never
matched before this fix):

  KEEP   property_id=75c51b9b-acbd-4205-89a1-4bba65010710  floor_unit="G"
         special_features="2 meeting rooms; Term 24 months; Manned
         reception; Communal lounge; On-site cafe; Event space; Wellness
         room; Passenger lifts; Bike racks; Shower facilities; Communal
         terrace; DDA Compliant floors; Carbon Neutral / BREEAM Excellent
         / EPC A / WELL Gold / Wiredscore Platinum / Activescore Platinum"

  DROP   property_id=73749799-d752-4ec0-a241-b1274fd46288  floor_unit=
         "Ground Floor"  special_features="2 meeting rooms; 24 month term"

Both property_ids are checked explicitly before any change is made - this
script refuses to run (raises) if either one isn't found exactly where
expected, rather than silently matching something else.

IMPORTANT - this repo has no GCS credentials configured in THIS
environment (confirmed: no GCS_BUCKET_NAME set, no gcloud CLI available),
so this can only ever write to a LOCAL output file, never the real
production master.xlsx in GCS directly. Run it against the real production
path (with real credentials available) to actually change live data - this
script accepts --master-path/--output-path so the exact same code runs
either way.

Usage (dry run - the default, prints what WOULD change, writes nothing):
    python merge_packing_house_g_ground_duplicate.py --master-path "<path>"

Usage (apply - writes the result to --output-path via the real
master_writer.write_master, same code path the app itself uses):
    python merge_packing_house_g_ground_duplicate.py --master-path "<path>" --output-path "<path>" --apply
"""

import argparse
import sys

import master_writer
from storage.file_store import dataframe_to_listing_rows

KEEP_PROPERTY_ID = "75c51b9b-acbd-4205-89a1-4bba65010710"
DROP_PROPERTY_ID = "73749799-d752-4ec0-a241-b1274fd46288"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-path", default=master_writer.DEFAULT_MASTER_PATH)
    parser.add_argument("--output-path", default=None, help="Required with --apply.")
    parser.add_argument("--apply", action="store_true", help="Actually write the result. Default is dry-run.")
    args = parser.parse_args()

    if not master_writer.master_exists(args.master_path):
        print(f"No master file found at {args.master_path!r}.", file=sys.stderr)
        sys.exit(1)

    df = master_writer.load_master_as_dataframe(args.master_path)
    rows = dataframe_to_listing_rows(df)

    keep_row = next((r for r in rows if r.property_id == KEEP_PROPERTY_ID), None)
    drop_row = next((r for r in rows if r.property_id == DROP_PROPERTY_ID), None)

    if keep_row is None:
        print(f"KEEP row {KEEP_PROPERTY_ID!r} not found - refusing to run.", file=sys.stderr)
        sys.exit(1)
    if drop_row is None:
        print(f"DROP row {DROP_PROPERTY_ID!r} not found - refusing to run.", file=sys.stderr)
        sys.exit(1)
    if keep_row.building != "Packing House" or drop_row.building != "Packing House":
        print("One or both rows no longer say building='Packing House' - refusing to run.", file=sys.stderr)
        sys.exit(1)
    if keep_row.floor_unit != "G" or drop_row.floor_unit != "Ground Floor":
        print(
            f"Unexpected floor_unit (keep={keep_row.floor_unit!r}, drop={drop_row.floor_unit!r}), "
            "expected 'G'/'Ground Floor' - refusing to run.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Master path: {args.master_path}")
    print(f"Total rows: {len(rows)}")
    print()
    print("KEEP (unchanged):")
    print(f"  property_id={keep_row.property_id}  floor_unit={keep_row.floor_unit!r}  "
          f"special_features={keep_row.special_features!r}")
    print("DROP:")
    print(f"  property_id={drop_row.property_id}  floor_unit={drop_row.floor_unit!r}  "
          f"special_features={drop_row.special_features!r}")

    new_rows = [r for r in rows if r.property_id != DROP_PROPERTY_ID]
    print()
    print(f"Rows after dedup: {len(new_rows)} (was {len(rows)})")

    if not args.apply:
        print()
        print("Dry run only - nothing written. Pass --apply --output-path <path> to write the result.")
        return

    if not args.output_path:
        print("--apply requires --output-path.", file=sys.stderr)
        sys.exit(1)

    master_writer.write_master(
        new_rows, master_path=args.output_path, source="dedup_packing_house_g_ground_one_off", removed_count=1,
    )
    print(f"Written to {args.output_path!r}.")


if __name__ == "__main__":
    main()
