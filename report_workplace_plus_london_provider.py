"""
report_workplace_plus_london_provider.py

READ-ONLY report: lists every master row whose provider or internal_ref is
literally "Workplace Plus London" (case-insensitive), so a human can review
exactly what a future correction to "Workplace Plus" (see extract_spreadsheet.
py's _KNOWN_PROVIDER_NAMES fix) would change before anyone approves running
it for real.

This script NEVER calls master_writer.write_master() or blob_store.write_
bytes() - it only reads master via master_writer.load_master_as_dataframe()
(the same read path app.py itself uses, so it transparently respects
GCS_BUCKET_NAME/blob_store exactly like the live app does - no separate,
hand-rolled file-reading logic that could drift from how the app actually
reads master). Safe to run against production master data as many times as
needed; it makes no changes.

Usage:
    python report_workplace_plus_london_provider.py [master_path]

master_path defaults to master_writer.DEFAULT_MASTER_PATH ("data/master.xlsx")
- pass an explicit path only if the real master lives somewhere else.
"""

import sys

import master_writer

TARGET = "workplace plus london"


def main():
    master_path = sys.argv[1] if len(sys.argv) > 1 else master_writer.DEFAULT_MASTER_PATH

    if not master_writer.master_exists(master_path):
        print(f"No master file found at {master_path!r} - nothing to report.")
        return

    df = master_writer.load_master_as_dataframe(master_path)

    affected = []
    for _, row in df.iterrows():
        provider = row.get("provider")
        internal_ref = row.get("internal_ref")
        matched_fields = [
            field for field, value in (("provider", provider), ("internal_ref", internal_ref))
            if isinstance(value, str) and value.strip().lower() == TARGET
        ]
        if matched_fields:
            affected.append({
                "building": row.get("building"),
                "floor_unit": row.get("floor_unit"),
                "provider": provider,
                "internal_ref": internal_ref,
                "matched_on": matched_fields,
            })

    print(f"Master path: {master_path}")
    print(f"Total rows scanned: {len(df)}")
    print(f"Rows with provider or internal_ref == \"Workplace Plus London\": {len(affected)}")
    print()

    for r in affected:
        print(
            f"  building={r['building']!r}  floor_unit={r['floor_unit']!r}  "
            f"provider={r['provider']!r}  internal_ref={r['internal_ref']!r}  "
            f"matched_on={r['matched_on']}"
        )

    if not affected:
        print("  (none found)")


if __name__ == "__main__":
    main()
