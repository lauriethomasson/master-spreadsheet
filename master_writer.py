"""
master_writer.py

Replaces the entire master spreadsheet with an approved batch of rows.

Safe against partial failure: writes to a temp file first (in the SAME
directory as the target, so the final replace is an atomic rename rather
than a slow cross-device copy), validates it, then atomically replaces the
real master.xlsx. If anything fails before that final replace, the existing
master.xlsx is untouched.
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from schema import ListingRow
from staging_writer import write_rows_to_xlsx

DEFAULT_MASTER_PATH = "data/master.xlsx"
LOG_PATH = "data/master_write_log.jsonl"


def master_exists(master_path: str = DEFAULT_MASTER_PATH) -> bool:
    return Path(master_path).exists()


def load_master_as_dataframe(master_path: str = DEFAULT_MASTER_PATH) -> pd.DataFrame:
    return pd.read_excel(master_path)


def get_master_write_log(log_path: str = LOG_PATH) -> list:
    if not Path(log_path).exists():
        return []
    with open(log_path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def log_master_write(success: bool, row_count: int = None, error: str = None):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "success": success,
        "row_count": row_count,
        "error": error,
    }
    Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[master_writer] {entry}", file=sys.stderr)


def write_master(approved_rows: list[ListingRow], master_path: str = DEFAULT_MASTER_PATH):
    master_dir = os.path.dirname(master_path) or "."
    os.makedirs(master_dir, exist_ok=True)

    # Temp file MUST live in the same directory as master_path — that's what
    # makes the final shutil.move a same-filesystem atomic rename instead of
    # a copy-then-delete, which would reintroduce the exact corruption risk
    # this function exists to avoid.
    temp_fd, temp_path = tempfile.mkstemp(suffix=".xlsx", dir=master_dir)
    os.close(temp_fd)

    try:
        write_rows_to_xlsx(approved_rows, temp_path)

        wb = load_workbook(temp_path)
        ws = wb.active
        actual_row_count = ws.max_row - 1
        if actual_row_count != len(approved_rows):
            raise ValueError(
                f"Validation failed: expected {len(approved_rows)} rows, "
                f"got {actual_row_count} in written file"
            )

        shutil.move(temp_path, master_path)
        log_master_write(success=True, row_count=len(approved_rows))

    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        log_master_write(success=False, error=str(e))
        raise


def main():
    if len(sys.argv) not in (2, 3):
        print("Usage: python master_writer.py <rows.json> [master_path]", file=sys.stderr)
        raise SystemExit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        raw_rows = json.load(f)

    rows = [ListingRow(**r) for r in raw_rows]
    master_path = sys.argv[2] if len(sys.argv) == 3 else DEFAULT_MASTER_PATH
    write_master(rows, master_path)
    print(f"Wrote {len(rows)} row(s) to {master_path}")


if __name__ == "__main__":
    main()
