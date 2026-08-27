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
import typing
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st

from brochure_link_resolver import looks_like_url
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
    fully_occupied_buildings: list = None, source_identity_hash: str = None,
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
            # The real uploaded bytes (+ ambiguous-sheet decisions for a
            # spreadsheet) ALONE - deliberately NOT including content_hash's
            # own code-logic fingerprint (_SPREADSHEET_LOGIC_FINGERPRINT/
            # _PDF_EMAIL_LOGIC_FINGERPRINT+geocode.py) - see active_and_superseded_
            # staging_files' own docstring for the real, confirmed gap this
            # closes: re-uploading the literal same source file across a
            # code change (a real fix landing between two test uploads of
            # the same real file) gets a DIFFERENT content_hash purely from
            # that fingerprint change, so it was never recognized as
            # superseding the earlier, now-stale pending copy of that exact
            # same source - both stayed "active" and both contributed rows
            # to the same merge plan. None for a staging entry written
            # before this field existed - group_pending_by_content_hash
            # falls back to content_hash for those, unchanged from before.
            "source_identity_hash": source_identity_hash,
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


def get_staging_row_count(path: str) -> int:
    """n_rows as recorded at save_staging_file time - the ORIGINAL row
    count for this staging file, not re-derived from its current .xlsx
    (which never changes row count after the fact anyway, only field
    values - see update_staging_rows' own docstring). Used for a per-file
    staging-management listing (see pages/2_Review_and_Master.py) where
    two entries sharing a filename otherwise look identical at a glance."""
    return _read_meta(path).get("n_rows", 0)


def get_staging_timestamp(path: str) -> str:
    """The ISO-format upload timestamp recorded at save_staging_file time -
    same per-file staging-management use as get_staging_row_count above."""
    return _read_meta(path).get("timestamp")


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


def _derive_floorplan_counts(floorplan_processed_urls: dict, unique_floorplans_considered: int) -> dict:
    """floorplans_done/floorplans_read_ok/floorplans_unavailable - the
    secondary floorplan-link pass's own counterpart to _derive_enrichment_
    counts (see its own docstring), tracked separately from the brochure
    counts above so "how many brochures remain" and "how many floorplans
    remain" can never be conflated into one, wrongly-optimistic number."""
    floorplan_processed_urls = floorplan_processed_urls or {}
    return {
        "unique_floorplans_considered": unique_floorplans_considered,
        "floorplans_done": len(floorplan_processed_urls),
        "floorplans_read_ok": sum(1 for v in floorplan_processed_urls.values() if v == "ok"),
        "floorplans_unavailable": sum(1 for v in floorplan_processed_urls.values() if v == "unavailable"),
    }


def set_staging_enrichment_summary(
    path: str, stats: dict, processed_urls: dict,
    floorplan_processed_urls: dict = None, unique_floorplans_considered: int = 0, document_issues: list = None,
) -> None:
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

    floorplan_processed_urls/unique_floorplans_considered are the SAME kind
    of cumulative record, for the secondary floorplan-link pass (see
    enrich_rows_grouped's own floorplan_already_processed param) - optional,
    defaulting to "no floorplans considered at all" so every existing
    caller that only ever enriched from brochures is unaffected.

    Called only once enrich_rows_grouped has fully processed every
    remaining brochure AND floorplan - see set_staging_enrichment_progress
    for the interim marker written before and during a run, which this
    overwrites. Absent entirely (both this and the interim marker) for a
    file where enrichment never ran at all (no eligible rows in the first
    place, or an upload predating this feature) - a caller should treat a
    missing key as "nothing to show", not as "zero rows enriched" (see
    get_staging_enrichment_summary).

    document_issues (see brochure_enrichment.enrich_rows_grouped's own
    stats["document_issues"] docstring) is ADDITIVE diagnostics, purely for
    a compact "these need a look" UI list - never used for resume/retry
    logic, unlike processed_urls. Always the CALLER'S current, complete
    picture (enrich_rows_grouped recomputes ineligible-link issues fresh on
    every call, and retries any URL not already "ok" - see its own
    urls_to_fetch filter - so this run's own document_issues already
    reflects everything still genuinely outstanding, with no separate merge
    against a prior run's stale list needed). Defaults to [] so every
    existing caller that predates this stays unaffected.
    """
    meta = _read_meta(path)
    meta["brochure_enrichment"] = {
        "status": "complete",
        "rows_eligible": stats["rows_eligible"],
        "rows_enriched": stats["rows_enriched"],
        "processed_urls": dict(processed_urls),
        **_derive_enrichment_counts(processed_urls, stats["unique_brochures_considered"]),
        "floorplan_processed_urls": dict(floorplan_processed_urls or {}),
        **_derive_floorplan_counts(floorplan_processed_urls, unique_floorplans_considered),
        "document_issues": list(document_issues or []),
    }
    _write_meta(path, meta)


def set_staging_enrichment_progress(
    path: str, processed_urls: dict, unique_brochures_considered: int,
    floorplan_processed_urls: dict = None, unique_floorplans_considered: int = 0, document_issues: list = None,
) -> None:
    """
    Persists an INTERIM brochure-enrichment marker - status="in_progress"
    plus the FULL CUMULATIVE per-URL outcome map so far, for BOTH the
    brochure pass and the secondary floorplan pass (see set_staging_
    enrichment_summary's own docstring on why these are the cumulative
    maps, not just this run's own increment) - written before enrich_rows_
    grouped starts (processed_urls={} on a fresh run, or whatever a prior
    interrupted attempt already recorded on a resume) and again at each of
    its own checkpoints, for EITHER pass (see app.py's _run_automatic_
    brochure_enrichment and pages/2_Review_and_Master.py's own "Continue
    enrichment" action), specifically so an interruption (a killed process,
    a crashed Cloud Run instance, a cancelled Streamlit rerun) that stops
    the run before set_staging_enrichment_summary's own final call is ever
    reached - during the brochure pass OR the floorplan pass, which only
    ever starts once every brochure is already marked "done" - still leaves
    SOME record in meta.json, rather than none at all, AND so a SUBSEQUENT
    resume knows exactly which brochures/floorplans to skip (see enrich_
    rows_grouped's own already_processed/floorplan_already_processed
    params), never just "how many", which alone can't identify WHICH ones.

    floorplan_processed_urls/unique_floorplans_considered default to "no
    floorplans considered at all" so every existing caller that only ever
    passes the brochure-side arguments is unaffected - a genuinely floorplan
    -eligible run threads its own real values through instead (see
    brochure_enrichment.run_brochure_enrichment).

    Without this, an interrupted run's staging rows end up a genuine mix of
    enriched and never-attempted rows (see enrich_rows_grouped's own
    checkpoint_callback, which already durably persists whatever was
    completed) while get_staging_enrichment_summary stays None forever -
    indistinguishable from "enrichment never ran/had nothing eligible" (see
    that function's own docstring), so a reviewer has no way to tell a
    blank special_features cell here apart from a brochure/floorplan that
    was genuinely checked and had nothing. set_staging_enrichment_summary's
    own status="complete" tag overwrites this once the run actually
    finishes; a status="in_progress" entry still present when Review &
    Master reads it back means the run that wrote it never got that far -
    and, critically, "0 remaining" only holds once BOTH unique_brochures_
    considered - brochures_done AND unique_floorplans_considered -
    floorplans_done are zero, never brochures alone (see pages/2_Review_
    and_Master.py's own "remaining" calculation).
    """
    meta = _read_meta(path)
    meta["brochure_enrichment"] = {
        "status": "in_progress",
        "processed_urls": dict(processed_urls),
        **_derive_enrichment_counts(processed_urls, unique_brochures_considered),
        "floorplan_processed_urls": dict(floorplan_processed_urls or {}),
        **_derive_floorplan_counts(floorplan_processed_urls, unique_floorplans_considered),
        "document_issues": list(document_issues or []),
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


def find_previous_upload_by_hash(content_hash: str, source_identity_hash: str = None) -> str:
    """
    The staging path of the most recent previously-processed upload
    identified by `source_identity_hash` if given (the real uploaded
    bytes alone - see save_staging_file's own docstring), else by
    `content_hash` (see app.py, which hashes the raw uploaded bytes
    BEFORE extraction, folded together with the current code's own
    extraction-logic fingerprint) - or None if this upload has never been
    seen before. Searches every staging entry regardless of status -
    pending or already approved - since staging .xlsx files are kept
    forever (see module docstring), making this a permanent ledger rather
    than something that only catches a re-upload while the first one is
    still awaiting review.

    A hit lets the caller skip re-extraction entirely and reuse the earlier
    rows verbatim - besides the wasted API call, re-extracting an unchanged
    document risks Gemini's own non-determinism producing different wording
    for a prose field (special_features, contacts) on a document that
    hasn't actually changed at all, which would otherwise show up as a
    spurious diff with nothing real behind it.

    Real, confirmed gap this closes (the SAME one group_pending_by_
    content_hash/active_and_superseded_staging_files already fixed via
    _grouping_hash, which this function now reuses rather than
    duplicating its own copy of the same fallback logic): content_hash
    ALONE bakes in the current code's own extraction-logic fingerprint
    (_SPREADSHEET_LOGIC_FINGERPRINT/_PDF_EMAIL_LOGIC_FINGERPRINT + geocode.py, see
    app.py), so re-uploading the exact same source file after ANY change
    to that logic produces a different content_hash and this lookup
    wrongly returned None - the file was re-extracted from scratch
    instead of reusing the cached rows, even though nothing about the
    actual source document had changed at all. Passing the caller's own
    source_identity_hash through closes that gap the same way grouping
    already does; omitting it (the default) preserves the exact prior
    content_hash-only behavior for any caller that hasn't been updated.

    Entries written before either hash existed have neither key at all
    (None via .get()), so they simply never match a fresh hash - no
    backfill needed for old history to behave correctly.
    """
    if not content_hash:
        return None
    return _find_previous_upload_by_hash_cached(content_hash, source_identity_hash, _staging_signature())


@st.cache_data(max_entries=8, ttl=3600, show_spinner="Checking for a previous upload of this file...")
def _find_previous_upload_by_hash_cached(content_hash: str, source_identity_hash: str, signature: tuple) -> str:
    # The SAME "source_identity_hash if present, else content_hash"
    # preference _grouping_hash already applies to a STORED entry's own
    # meta, applied here to THIS upload's own freshly-computed hashes too
    # - but NOT as the only comparison: a stored entry's PREFERRED hash
    # (_grouping_hash(meta)) is compared against this upload's preferred
    # hash, OR its plain content_hash is compared directly against this
    # upload's content_hash, independent of either side's preference.
    # That second, always-on check is required - confirmed directly by a
    # real test failure while writing this fix - because relying on
    # _grouping_hash ALONE breaks the exact-content_hash-match case
    # whenever only ONE side happens to carry a source_identity_hash the
    # other has nothing to compare it against: e.g. this upload's own
    # caller omits source_identity_hash (the documented backward-
    # compatible default) against an entry that DOES have one recorded -
    # _grouping_hash(meta) would then prefer THAT entry's source_identity_
    # hash, which this upload's own preferred value (its plain
    # content_hash, since it has no source_identity_hash of its own to
    # prefer) would never equal, even though the two content_hash values
    # are byte-for-byte identical.
    preferred_target = source_identity_hash or content_hash
    matches = []
    for xlsx_path, _ in blob_store.list_with_mtimes(STAGING_PREFIX, ".xlsx"):
        try:
            meta = _read_meta(xlsx_path)
        except FileNotFoundError:
            continue
        if _grouping_hash(meta) == preferred_target or meta.get("content_hash") == content_hash:
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


def _enrichment_completeness_rank(path: str) -> tuple:
    """
    (status_rank, brochures_done) - higher sorts as more enrichment-
    complete/preferred, for active_and_superseded_staging_files' own
    ordering between two staging files that share the same content_hash.
    complete (2) beats any in-progress state (1), which beats a file
    enrichment never even touched (0, get_staging_enrichment_summary
    returns None - see that function's own docstring on why that's not a
    zero-valued dict); within the same status, more brochures actually
    checked wins. Never the sole signal on its own - see
    active_and_superseded_staging_files' own docstring on why recency is
    only ever the LAST tie-break, after this.
    """
    stats = get_staging_enrichment_summary(path)
    if not stats:
        return (0, 0)
    status_rank = 2 if stats.get("status") == "complete" else 1
    return (status_rank, stats.get("brochures_done", 0))


def _grouping_hash(meta: dict) -> str:
    """
    The hash group_pending_by_content_hash/active_and_superseded_staging_
    files actually groups by - source_identity_hash (the real uploaded
    bytes + any ambiguous-sheet decisions, see save_staging_file's own
    docstring) when present, falling back to content_hash (which also
    bakes in the current code's own logic fingerprint) for a staging entry
    written before source_identity_hash existed. Real, confirmed gap this
    closes: content_hash ALONE means re-uploading the literal same source
    file across a code change (a real fix landing between two uploads of
    the same real file, during genuine iterative development/testing) gets
    a DIFFERENT hash purely from that fingerprint change, so the earlier,
    now-stale pending copy was never recognized as superseded by the new
    one - both stayed "active" and both contributed their own rows to the
    same merge plan, duplicating every real listing in that file. Grouping
    on the fingerprint-independent identity instead means ANY two uploads
    of the same real bytes (with the same ambiguous-sheet decisions, for a
    spreadsheet) are recognized as the same upload regardless of how many
    code changes happened in between.
    """
    return meta.get("source_identity_hash") or meta.get("content_hash")


def group_pending_by_content_hash(pending: list) -> dict:
    """
    {hash: [paths...]} for every entry in `pending` that has a genuine,
    non-blank grouping hash of its own (see _grouping_hash) - grouping
    candidate staging files that are uploads of the same real source file,
    never a fuzzy or extracted-content-based match. An entry with no
    grouping hash at all (a pre-this-feature upload with neither field
    set, or any future upload type that doesn't set one) is deliberately
    EXCLUDED here rather than grouped under some placeholder key - it has
    nothing reliable to be compared against, so active_and_superseded_
    staging_files' own caller must treat every path missing from this
    dict's own values as its own, always-active singleton.
    """
    groups = {}
    for path in pending:
        grouping_hash = _grouping_hash(_read_meta(path))
        if grouping_hash:
            groups.setdefault(grouping_hash, []).append(path)
    return groups


def active_and_superseded_staging_files(pending: list) -> tuple:
    """
    (active_paths, superseded_paths), both subsets of `pending`, in `pending`'s
    own relative order - splits pending staging files by SOURCE identity
    (see group_pending_by_content_hash/_grouping_hash) so Review & Master's
    own merge-plan combination (see pages/2_Review_and_Master.py's
    _render_pending_review) reads rows from exactly ONE staging file per
    genuinely distinct uploaded document, never once per PROCESSING RUN of
    the same document.

    Real problem this solves: re-uploading the identical source file while
    an earlier run's brochure enrichment was still incomplete (or simply
    re-uploading it again later) creates a SECOND staging entry - by
    design, see save_staging_file's own docstring on per-upload durability
    - that shares the first one's source identity. Combining both into one
    Review batch double-counts every real property in that file (275 rows
    -> 550) and forces master_merge's own intra-batch duplicate logic to
    reconcile hundreds of pairs that are really "the same upload, two
    processing runs" rather than genuine duplicates - producing false
    conflicts whenever the two runs' own independent geocoding happened to
    disagree, on top of just being needless work. This grouping is
    deliberately based on source_identity_hash (the real bytes alone), NOT
    the code-fingerprint-dependent content_hash, for the same reason - a
    second, real, confirmed report: re-uploading the exact same source file
    again LATER, after a genuine code fix shipped in between the two
    uploads, changed content_hash purely from that fingerprint change, so
    the stale, pre-fix pending copy was never recognized as superseded
    either - it kept contributing its own (now-outdated) rows to every
    later Review render indefinitely, silently duplicating every real
    listing in that file across however many code changes happened while
    it sat there un-discarded.

    Within a source-identity group of 2+ staging files, exactly ONE is
    "active" (the one whose brochure-enrichment state is most complete -
    see _enrichment_completeness_rank; a genuine tie breaks toward the
    most recently written entry, the LAST resort here, never the primary
    signal - "latest wins" alone is exactly the naive rule this function
    deliberately does NOT implement) - every other member of that group is
    "superseded": excluded from Review's own row-combination/counts, but
    still a completely ordinary member of `pending` for every other
    purpose (staging management's own per-file listing, its own
    individual Discard button, Continue enrichment if the reviewer
    genuinely wants to finish it anyway - superseded is a Review-page
    display/counting decision only, never a deletion).

    A path with no content_hash at all, or the lone member of its own
    content_hash group, is always active - there is nothing to supersede
    it, and nothing it could safely supersede either (see
    group_pending_by_content_hash's own docstring on why a blank hash is
    excluded from grouping rather than treated as one shared group).
    """
    groups = group_pending_by_content_hash(pending)
    grouped_paths = {p for paths in groups.values() for p in paths}

    active, superseded = [], []
    for path in pending:
        if path not in grouped_paths:
            active.append(path)

    for paths in groups.values():
        if len(paths) == 1:
            active.append(paths[0])
            continue
        ranked = sorted(
            paths,
            key=lambda p: (_enrichment_completeness_rank(p), _read_meta(p).get("timestamp", "")),
            reverse=True,
        )
        active.append(ranked[0])
        superseded.extend(ranked[1:])

    active_order = {path: i for i, path in enumerate(pending)}
    active.sort(key=lambda p: active_order[p])
    superseded.sort(key=lambda p: active_order[p])
    return active, superseded


def clean_value(value):
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


# Raw provider-supplied fields that must genuinely look like a URL (see
# brochure_link_resolver.looks_like_url) before being trusted as a link -
# applied here, the one choke point every dataframe-to-ListingRow
# construction path funnels through, so it catches a source this narrowly
# never otherwise validates at all: extract_spreadsheet.py's header-mapped
# path copies a provider's own "Brochure" column straight across with no
# other check in between (unlike the PDF/email/Gemini-sheet paths, which
# already run every candidate through brochure_link_resolver.
# finalize_brochure_link). Confirmed real failure this guards against: a
# UNION row whose own Brochure cell reads "TBC" (the provider's own way of
# saying "no brochure yet") was written straight through into brochure_link
# and then rendered as a real, broken, clickable link downstream -
# staging_writer.write_rows_to_xlsx turns ANY truthy value into a real
# openpyxl hyperlink, and display_utils' LinkColumn renders ANY truthy
# value as clickable. A placeholder must never survive as brochure_link -
# only a genuine URL, or blank.
URL_LIKE_FIELDS = ("brochure_link", "floorplan_link")


def _sanitize_url_like_fields(cleaned: dict) -> dict:
    for field in URL_LIKE_FIELDS:
        value = cleaned.get(field)
        if value is not None and not looks_like_url(value):
            cleaned[field] = None
    return cleaned


def _is_string_field(field_name: str) -> bool:
    """True for a ListingRow field declared Optional[str] - derived from the
    schema's own type hints (same approach as master_merge.field_kind, not
    importable here - master_merge already imports FROM this module, so
    importing it back would be circular), never a hardcoded field list."""
    if field_name not in ListingRow.model_fields:
        return False
    annotation = ListingRow.model_fields[field_name].annotation
    args = typing.get_args(annotation)
    base = next((a for a in args if a is not type(None)), annotation)
    return base is str


def _coerce_string_fields(cleaned: dict) -> dict:
    """
    Coerces a raw number sitting in a str-typed field to its own genuine
    text (e.g. 2.3 -> "2.3", a whole-number float like 2.0 -> "2", matching
    how the number itself would actually read) rather than letting it reach
    ListingRow(**cleaned) as-is - confirmed real failure this guards
    against: a real beem Live Flex Availability.xlsx row's own Floor cell
    held the raw number 2.3 (not text like "2nd"), which previously failed
    ListingRow's own str validation and aborted the WHOLE file's extraction
    over that one cell - the exact same "one bad cell must never take down
    every other row" principle this module's own dataframe_to_listing_rows
    already applies to a blank building value.
    """
    for field, value in cleaned.items():
        if value is None or isinstance(value, str) or not _is_string_field(field):
            continue
        if isinstance(value, float) and value.is_integer():
            cleaned[field] = str(int(value))
        else:
            cleaned[field] = str(value)
    return cleaned


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
        cleaned = _coerce_string_fields(cleaned)
        cleaned = _sanitize_url_like_fields(cleaned)
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
