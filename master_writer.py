"""
master_writer.py

Replaces the entire master spreadsheet with an approved batch of rows.

Safe against partial failure: the new workbook is built and validated in
memory first, and only written to its final location (via blob_store -
local disk or a GCS bucket, depending on configuration) once that
validation passes. blob_store.write_bytes() itself is atomic on both
backends (temp-file-then-rename locally; a single upload is inherently
atomic on GCS), so a reader never sees a partially-written file and a
failure before that write leaves the existing master.xlsx untouched.
"""

import json
import sys
from datetime import datetime, timezone
from io import BytesIO

import pandas as pd
import streamlit as st
from openpyxl import load_workbook

from schema import ListingRow
from staging_writer import write_rows_to_xlsx
from storage import blob_store

DEFAULT_MASTER_PATH = "data/master.xlsx"
LOG_PATH = "data/master_write_log.jsonl"


def master_exists(master_path: str = DEFAULT_MASTER_PATH) -> bool:
    return blob_store.exists(master_path)


def load_master_as_dataframe(master_path: str = DEFAULT_MASTER_PATH) -> pd.DataFrame:
    # mtime is part of the cache key (not an underscore-prefixed arg), so a
    # fresh write_master() — which always changes the file's mtime — is
    # enough to invalidate this on its own, with no explicit .clear() needed.
    # A superseded (path, mtime) entry is never looked up again, so max_entries/ttl
    # bound how much of that unreachable history st.cache_data keeps around.
    return _load_master_as_dataframe_cached(master_path, blob_store.get_mtime(master_path))


@st.cache_data(max_entries=4, ttl=3600)
def _load_master_as_dataframe_cached(master_path: str, mtime: float) -> pd.DataFrame:
    return pd.read_excel(BytesIO(blob_store.read_bytes(master_path)))


def get_master_write_log(log_path: str = LOG_PATH) -> list:
    if not blob_store.exists(log_path):
        return []
    return _get_master_write_log_cached(log_path, blob_store.get_mtime(log_path))


@st.cache_data(max_entries=4, ttl=3600)
def _get_master_write_log_cached(log_path: str, mtime: float) -> list:
    text = blob_store.read_bytes(log_path).decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def log_master_write(success: bool, row_count: int = None, error: str = None):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "success": success,
        "row_count": row_count,
        "error": error,
    }
    blob_store.append_text(LOG_PATH, json.dumps(entry) + "\n")
    print(f"[master_writer] {entry}", file=sys.stderr)


def write_master(approved_rows: list[ListingRow], master_path: str = DEFAULT_MASTER_PATH):
    try:
        buffer = BytesIO()
        write_rows_to_xlsx(approved_rows, buffer)

        buffer.seek(0)
        wb = load_workbook(buffer)
        ws = wb.active
        actual_row_count = ws.max_row - 1
        if actual_row_count != len(approved_rows):
            raise ValueError(
                f"Validation failed: expected {len(approved_rows)} rows, "
                f"got {actual_row_count} in written file"
            )

        blob_store.write_bytes(master_path, buffer.getvalue())
        log_master_write(success=True, row_count=len(approved_rows))

    except Exception as e:
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
