"""
storage/file_store.py

Manages staging .xlsx files created by the Upload page and consumed by the
Review page, which combines every currently-pending file into one table.
Each staging upload gets its own file plus a sidecar .meta.json tracking
{filename, timestamp, status, n_rows}, so multiple pending uploads can
coexist before any of them is approved. Approving marks every pending file's
status as approved; the underlying .xlsx files are never deleted or edited
in place after that.

Storage itself (local disk vs. a GCS bucket) is delegated entirely to
storage/blob_store - this module only deals in the same plain path/key
strings either way, e.g. "staging/20260101_120000_brochure.xlsx".
"""

import json
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st

from schema import ListingRow
from staging_writer import read_xlsx_with_hyperlinks, write_rows_to_xlsx
from storage import blob_store

STAGING_PREFIX = "staging"
BROCHURES_PREFIX = "brochures"


def _meta_path(xlsx_path: str) -> str:
    # as_posix(), not str() - this is a key/path string compared and stored
    # elsewhere as forward-slash-separated (matching GCS blob-name
    # conventions), which str() would break on Windows (backslash).
    return Path(xlsx_path).with_suffix(".meta.json").as_posix()


def _read_meta(xlsx_path: str) -> dict:
    return json.loads(blob_store.read_bytes(_meta_path(xlsx_path)))


def _write_meta(xlsx_path: str, meta: dict) -> None:
    blob_store.write_bytes(_meta_path(xlsx_path), json.dumps(meta, indent=2).encode("utf-8"))


def save_staging_file(rows: list[ListingRow], original_filename: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    stem = Path(original_filename).stem
    staging_path = f"{STAGING_PREFIX}/{timestamp}_{stem}.xlsx"

    buffer = BytesIO()
    write_rows_to_xlsx(rows, buffer)
    blob_store.write_bytes(staging_path, buffer.getvalue())
    _write_meta(
        staging_path,
        {
            "filename": original_filename,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "pending_review",
            "n_rows": len(rows),
        },
    )
    return staging_path


def save_original_pdf(data: bytes, original_filename: str) -> str:
    """
    Persists the original uploaded PDF's own bytes (not the rows extracted
    from it), so brochure_link's PDF-fallback rule (see
    brochure_link_resolver.finalize_brochure_link's rule 3) has a real,
    permanently-fetchable file to point at - previously the upload's temp
    file was deleted right after extraction with nothing kept anywhere, so
    that fallback could only ever be the bare original filename.

    Uploaded public=True (see blob_store.write_bytes) - unlike staging/master/
    versions, this one specific prefix is meant to be linked to directly
    from a spreadsheet cell, so it has to be world-readable.

    Returns the object's public URL. In local-disk dev mode (no
    GCS_BUCKET_NAME) there's no HTTP server to expose a local file through,
    so this returns None and callers should fall back to the bare filename,
    exactly as before this existed.
    """
    if not blob_store.using_gcs():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    stem = Path(original_filename).stem
    path = f"{BROCHURES_PREFIX}/{timestamp}_{stem}.pdf"
    blob_store.write_bytes(path, data, public=True)
    return blob_store.public_url(path)


def _staging_signature() -> tuple:
    """One (name, mtime) pair per sidecar file — changes whenever a file is
    added, removed, or edited in place (e.g. mark_as_approved rewriting an
    existing meta.json), so it's a reliable cache key for the directory's
    current state without needing an explicit .clear() on every write path.
    Every upload/approval produces a brand new signature that's never looked
    up again, so the cached functions below bound entries/ttl to keep that
    unreachable history from growing without limit over a long-running process.
    """
    return tuple(sorted(blob_store.list_with_mtimes(STAGING_PREFIX, ".meta.json")))


def list_pending_staging_files() -> list[str]:
    return _list_pending_staging_files_cached(_staging_signature())


@st.cache_data(max_entries=4, ttl=3600)
def _list_pending_staging_files_cached(signature: tuple) -> list[str]:
    pending = []
    for xlsx_path, _ in blob_store.list_with_mtimes(STAGING_PREFIX, ".xlsx"):
        try:
            meta = _read_meta(xlsx_path)
        except FileNotFoundError:
            continue
        if meta.get("status") == "pending_review":
            pending.append((meta.get("timestamp", ""), xlsx_path))

    pending.sort(reverse=True)
    return [path for _, path in pending]


def load_staging_as_dataframe(path: str) -> pd.DataFrame:
    return _load_staging_as_dataframe_cached(path, blob_store.get_mtime(path))


@st.cache_data(max_entries=8, ttl=3600)
def _load_staging_as_dataframe_cached(path: str, mtime: float) -> pd.DataFrame:
    return read_xlsx_with_hyperlinks(blob_store.read_bytes(path))


def mark_as_approved(path: str) -> None:
    meta = _read_meta(path)
    meta["status"] = "approved"
    _write_meta(path, meta)


def clean_value(value):
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def dataframe_to_listing_rows(df: pd.DataFrame) -> list[ListingRow]:
    rows = []
    for record in df.to_dict(orient="records"):
        cleaned = {key: clean_value(value) for key, value in record.items()}
        if all(value is None for value in cleaned.values()):
            continue
        rows.append(ListingRow(**cleaned))
    return rows
