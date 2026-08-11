"""
storage/file_store.py

Manages staging .xlsx files created by the Upload page and consumed by the
Review page, which combines every currently-pending file into one table.
Each staging upload gets its own file plus a sidecar .meta.json tracking
{filename, timestamp, status, n_rows, content_hash}, so multiple pending
uploads can coexist before any of them is approved. Approving marks every
pending file's status as approved; the underlying .xlsx files are never
deleted or edited in place after that - which is exactly what makes
content_hash usable as a permanent "has this exact file been processed
before" ledger (see find_previous_upload_by_hash) covering the upload's
entire history, not just what's still pending.

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


def save_staging_file(
    rows: list[ListingRow], original_filename: str, content_hash: str = None,
    fully_occupied_buildings: list = None,
) -> str:
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
            "content_hash": content_hash,
            # {"provider", "building"} dicts (see extract_spreadsheet_gemini.
            # extract_sheet_with_metadata) - buildings this upload's own
            # source text explicitly states have zero current availability.
            # Never present in a row (a fully-occupied building has none),
            # so this is the only place that signal survives past upload
            # time for master_merge.find_stale_candidates to use at review
            # time. Always a list, never None, so a caller can iterate it
            # unconditionally regardless of upload source type.
            "fully_occupied_buildings": fully_occupied_buildings or [],
        },
    )
    return staging_path


def get_staging_filename(path: str) -> str:
    """The original uploaded filename for a staging path (see save_staging_
    file) - for a UI label (e.g. pages/2_Review_and_Master.py's own per-file
    "Enrich from brochures" section) that needs to name a pending file
    without exposing its internal staging path."""
    return _read_meta(path).get("filename", path)


def update_staging_rows(path: str, rows: list[ListingRow]) -> None:
    """
    Overwrites an already-staged file's OWN rows in place at the same path -
    used by brochure enrichment (see pages/2_Review_and_Master.py and
    brochure_enrichment.enrich_rows_grouped) to persist an enriched result
    back to the exact staging file it read from, as a separate, later step
    from the original extraction that created it.

    Only the .xlsx blob is rewritten - the .meta.json sidecar (status,
    content_hash, filename, fully_occupied_buildings) is untouched, since
    none of those describe the ROWS themselves: status/content_hash exist to
    recognize a future re-upload of the same raw bytes (unaffected by this
    file's own rows changing after the fact), and filename/
    fully_occupied_buildings are provenance about the original upload, not
    about whatever enrichment has since filled in.

    load_staging_as_dataframe's own cache is keyed on this blob's mtime (see
    its own docstring), so a caller reads the freshly-enriched rows back on
    its very next call with no separate cache-clear needed.
    """
    buffer = BytesIO()
    write_rows_to_xlsx(rows, buffer)
    blob_store.write_bytes(path, buffer.getvalue())


def _derive_enrichment_counts(processed_urls: dict, unique_brochures_considered: int) -> dict:
    """
    brochures_done/brochures_read_ok/brochures_unavailable, all derived from
    processed_urls ({url: "ok" | "unavailable"}) rather than tracked as
    separate counters passed in alongside it - one source of truth for
    "what's been attempted so far" (across possibly several resumed runs),
    never two numbers that could quietly drift apart.
    """
    return {
        "unique_brochures_considered": unique_brochures_considered,
        "brochures_done": len(processed_urls),
        "brochures_read_ok": sum(1 for v in processed_urls.values() if v == "ok"),
        "brochures_unavailable": sum(1 for v in processed_urls.values() if v == "unavailable"),
    }


def set_staging_enrichment_summary(path: str, stats: dict, processed_urls: dict) -> None:
    """
    Persists brochure enrichment's own FINAL summary stats into this
    staging file's meta.json, tagged status="complete" - so Review & Master
    can show a read-only "brochure enrichment: N rows enriched" caption for
    a file that's already been through automatic enrichment (see app.py's
    own _run_automatic_brochure_enrichment), durably, without needing
    anything in session_state that a fresh browser session/server restart
    would lose.

    processed_urls is the FULL CUMULATIVE {url: "ok" | "unavailable"} map
    across every attempt this staging file has ever gone through (a fresh
    run's own outcomes merged with whatever an earlier, resumed attempt had
    already recorded - see enrich_rows_grouped's own already_processed
    param) - stats["rows_eligible"]/["rows_enriched"] still come from the
    caller's own stats dict (see enrich_rows_grouped's return value; not
    derivable from processed_urls alone), but unique_brochures_considered/
    brochures_done/brochures_read_ok/brochures_unavailable are recomputed
    from processed_urls here (see _derive_enrichment_counts) so the FINAL
    persisted record can never disagree with what's actually in
    processed_urls, regardless of what the caller's own stats dict says.

    Called only once enrich_rows_grouped has fully processed every
    remaining brochure - see set_staging_enrichment_progress for the
    interim marker written before and during a run, which this overwrites.
    Absent entirely (both this and the interim marker) for a file where
    enrichment never ran at all (no eligible rows in the first place, or an
    upload predating this feature) - a caller should treat a missing key as
    "nothing to show", not as "zero rows enriched" (see
    get_staging_enrichment_summary).
    """
    meta = _read_meta(path)
    meta["brochure_enrichment"] = {
        "status": "complete",
        "rows_eligible": stats["rows_eligible"],
        "rows_enriched": stats["rows_enriched"],
        "processed_urls": dict(processed_urls),
        **_derive_enrichment_counts(processed_urls, stats["unique_brochures_considered"]),
    }
    _write_meta(path, meta)


def set_staging_enrichment_progress(path: str, processed_urls: dict, unique_brochures_considered: int) -> None:
    """
    Persists an INTERIM brochure-enrichment marker - status="in_progress"
    plus the FULL CUMULATIVE per-URL outcome map so far (see
    set_staging_enrichment_summary's own docstring on why this is the
    cumulative map, not just this run's own increment) - written before
    enrich_rows_grouped starts (processed_urls={} on a fresh run, or
    whatever a prior interrupted attempt already recorded on a resume) and
    again at each of its own checkpoints (see app.py's
    _run_automatic_brochure_enrichment and pages/2_Review_and_Master.py's
    own "Continue enrichment" action), specifically so an interruption (a
    killed process, a crashed Cloud Run instance, a cancelled Streamlit
    rerun) that stops the run before set_staging_enrichment_summary's own
    final call is ever reached still leaves SOME record in meta.json,
    rather than none at all - AND so a SUBSEQUENT resume knows exactly
    which brochures to skip (see enrich_rows_grouped's own already_processed
    param), never just "how many", which alone can't identify WHICH ones.

    Without this, an interrupted run's staging rows end up a genuine mix of
    enriched and never-attempted rows (see enrich_rows_grouped's own
    checkpoint_callback, which already durably persists whatever was
    completed) while get_staging_enrichment_summary stays None forever -
    indistinguishable from "enrichment never ran/had nothing eligible" (see
    that function's own docstring), so a reviewer has no way to tell a
    blank special_features cell here apart from a brochure that was
    genuinely checked and had nothing. set_staging_enrichment_summary's own
    status="complete" tag overwrites this once the run actually finishes;
    a status="in_progress" entry still present when Review & Master reads
    it back means the run that wrote it never got that far.
    """
    meta = _read_meta(path)
    meta["brochure_enrichment"] = {
        "status": "in_progress",
        "processed_urls": dict(processed_urls),
        **_derive_enrichment_counts(processed_urls, unique_brochures_considered),
    }
    _write_meta(path, meta)


def get_staging_enrichment_summary(path: str) -> dict:
    """
    The stats dict set_staging_enrichment_summary (status="complete") or
    set_staging_enrichment_progress (status="in_progress", a run that never
    finished) last wrote for this staging file, or None if brochure
    enrichment never even started for it (see those functions' own
    docstrings on why this is None, not a zero-valued dict, in that case).
    Callers must check stats["status"] before treating this as a finished
    result - see pages/2_Review_and_Master.py's own _render_brochure_
    enrichment_summary. stats["processed_urls"] is present on every entry
    written by the current version of either setter - absent (plain
    .get() default needed by a caller) only for a staging file whose
    enrichment ran before this field existed.
    """
    return _read_meta(path).get("brochure_enrichment")


def get_staging_fully_occupied_buildings(path: str) -> list:
    """
    The {"provider", "building"} dicts persisted for this staging file (see
    save_staging_file) - [] for a file staged before this existed (plain
    .get() default, same "old entries just don't have it" tolerance as
    content_hash above) or for any non-Gemini-extracted upload, which never
    has any to begin with.
    """
    return _read_meta(path).get("fully_occupied_buildings", [])


def find_previous_upload_by_hash(content_hash: str) -> str:
    """
    The staging path of the most recent previously-processed upload with
    this exact content_hash (see app.py, which hashes the raw uploaded
    bytes before extraction), or None if this exact file content has never
    been uploaded before. Searches every staging entry regardless of status
    - pending or already approved - since staging .xlsx files are kept
    forever (see module docstring), making this a permanent ledger rather
    than something that only catches a re-upload while the first one is
    still awaiting review.

    A hit lets the caller skip re-extraction entirely and reuse the earlier
    rows verbatim - besides the wasted API call, re-extracting an unchanged
    document risks Gemini's own non-determinism producing different wording
    for a prose field (special_features, contacts) on a document that
    hasn't actually changed at all, which would otherwise show up as a
    spurious diff with nothing real behind it.

    Entries written before this existed have no "content_hash" key at all
    (None via .get()), so they simply never match a fresh hash - no
    backfill needed for old history to behave correctly.
    """
    if not content_hash:
        return None
    return _find_previous_upload_by_hash_cached(content_hash, _staging_signature())


@st.cache_data(max_entries=8, ttl=3600, show_spinner="Checking for a previous upload of this file...")
def _find_previous_upload_by_hash_cached(content_hash: str, signature: tuple) -> str:
    matches = []
    for xlsx_path, _ in blob_store.list_with_mtimes(STAGING_PREFIX, ".xlsx"):
        try:
            meta = _read_meta(xlsx_path)
        except FileNotFoundError:
            continue
        if meta.get("content_hash") == content_hash:
            matches.append((meta.get("timestamp", ""), xlsx_path))

    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


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


@st.cache_data(max_entries=4, ttl=3600, show_spinner="Checking for pending uploads...")
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


@st.cache_data(max_entries=8, ttl=3600, show_spinner="Loading previous results...")
def _load_staging_as_dataframe_cached(path: str, mtime: float) -> pd.DataFrame:
    return read_xlsx_with_hyperlinks(blob_store.read_bytes(path))


def mark_as_approved(path: str) -> None:
    meta = _read_meta(path)
    meta["status"] = "approved"
    _write_meta(path, meta)


def discard_pending_staging_files(paths: list) -> None:
    """
    Permanently deletes each staging .xlsx and its .meta.json sidecar -
    used when a user discards a pending upload without approving it (see
    pages/2_Review_and_Master.py). Unlike mark_as_approved, this removes
    the files entirely rather than changing their status: a discard means
    nothing was ever written to master.xlsx, so there's no version to
    create and nothing worth keeping around.

    Deliberately real deletion, not a "discarded" status alongside
    pending/approved: because the meta.json (and its content_hash) is
    gone afterward, a later re-upload of the exact same bytes is
    correctly treated as genuinely new by find_previous_upload_by_hash
    rather than "already seen" - the right behavior for content a user
    explicitly rejected, as opposed to one they approved. No explicit
    cache-clear needed - _staging_signature() (used by every @st.cache_data
    lookup in this module) is a tuple of every meta.json's own (name, mtime),
    so it changes the moment these sidecars disappear, same as every other
    mutation path here.
    """
    for path in paths:
        blob_store.delete(path)
        blob_store.delete(_meta_path(path))


def clean_value(value):
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def dataframe_to_listing_rows(df: pd.DataFrame) -> list[ListingRow]:
    """
    Skips any row with no building name, rather than only a row that's
    ENTIRELY blank - confirmed necessary against a real spreadsheet
    (Kitt's Availability), which had a spreadsheet-author's own section-
    header/note row mixed into the data range (Area="COMING SOON /
    ADDITIONAL OPTIONS TO SHARE", every other column - including Building -
    blank). That row isn't all-blank, so the old check let it through
    straight into ListingRow(building=None, ...), which fails validation
    (building is a required str) and aborts the WHOLE file's extraction
    over one non-property row. Without a building name a row can never
    become a real property either way, so skipping it here is exactly as
    safe as the all-blank check already was, just correctly broader -
    matches extract.py's own "no building and no prior unit to inherit
    from - skipping" handling for the same underlying situation in the
    PDF/email extraction path.
    """
    rows = []
    for record in df.to_dict(orient="records"):
        cleaned = {key: clean_value(value) for key, value in record.items()}
        building = cleaned.get("building")
        if building is None or (isinstance(building, str) and not building.strip()):
            continue
        rows.append(ListingRow(**cleaned))
    return rows


CRITICAL_FIELD_RESCUES_PREFIX = "critical_field_rescues"


def get_saved_critical_field_rescue(header_hash: str) -> dict:
    """
    The human-confirmed {field_name: header_or_None} rescue previously saved
    for this exact header set (see extract_spreadsheet.header_hash) - None
    if this format's critical fields have never needed rescuing before (or
    never needed rescuing at all, when every critical field already maps
    automatically). Deliberately narrow: unlike the removed full-column
    header-mapping persistence, this only ever remembers the specific
    field(s) suggest_mapping couldn't place on its own - every other column
    still maps automatically on every upload, confirmed or not.
    """
    path = f"{CRITICAL_FIELD_RESCUES_PREFIX}/{header_hash}.json"
    if not blob_store.exists(path):
        return None
    return json.loads(blob_store.read_bytes(path))


def save_critical_field_rescue(header_hash: str, headers: list, assignments: dict) -> None:
    """
    Persists a human-confirmed critical-field rescue for this exact header
    set, keyed by header_hash - so the same provider's recurring format
    (e.g. a monthly UNION export whose building column is headered with the
    area's own name) only needs rescuing once. headers is stored alongside
    the assignments purely for human inspection/debugging (e.g. reading
    critical_field_rescues/*.json directly to see what a hash corresponds
    to) - lookups only ever use the hash itself.
    """
    path = f"{CRITICAL_FIELD_RESCUES_PREFIX}/{header_hash}.json"
    blob_store.write_bytes(
        path, json.dumps({"headers": headers, "assignments": assignments}, indent=2).encode("utf-8")
    )
