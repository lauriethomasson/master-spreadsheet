"""
storage/blob_store.py

A minimal read/write/list interface used by master_writer.py and
storage/file_store.py, backed by either local disk or a Google Cloud
Storage bucket - whichever is active is decided once, at import time, by
whether GCS_BUCKET_NAME is set in the environment.

Local disk stays the default with zero configuration, so existing local
development/testing is completely unaffected: nothing about GCS is ever
touched unless GCS_BUCKET_NAME is explicitly set. On Cloud Run, that env
var is the only required configuration - authentication itself comes from
the service's attached service account via Application Default Credentials,
not from a key file or anything else this module has to manage.

Every path used by callers (e.g. "data/master.xlsx", "staging/2026...xlsx")
is treated as a plain relative string - a filesystem path in local mode, a
GCS blob name (also called an "object name") in bucket mode. GCS has no
real directory structure, but blob names containing "/" are exactly how
gsutil/the console display pseudo-folders, so the existing path scheme
carries over unchanged either way.
"""

import os
import shutil
import tempfile
from pathlib import Path

GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")

_bucket = None


def _get_bucket():
    global _bucket
    if _bucket is None:
        from google.cloud import storage

        _bucket = storage.Client().bucket(GCS_BUCKET_NAME)
    return _bucket


def using_gcs() -> bool:
    return bool(GCS_BUCKET_NAME)


def exists(path: str) -> bool:
    if using_gcs():
        return _get_bucket().blob(path).exists()
    return Path(path).exists()


def get_mtime(path: str) -> float:
    """POSIX timestamp - interchangeable between backends and directly usable
    as an st.cache_data cache-key argument, exactly as os.path.getmtime()
    already was before this module existed."""
    if using_gcs():
        blob = _get_bucket().blob(path)
        blob.reload()
        return blob.updated.timestamp()
    return Path(path).stat().st_mtime


def read_bytes(path: str) -> bytes:
    if using_gcs():
        from google.api_core.exceptions import NotFound

        try:
            return _get_bucket().blob(path).download_as_bytes()
        except NotFound as e:
            # Normalized to the same exception local-mode callers already
            # handle (Path.read_bytes() raises this natively for a missing
            # file), so callers never need a backend-specific except clause.
            raise FileNotFoundError(path) from e
    return Path(path).read_bytes()


def _content_type_for(path: str) -> str:
    if path.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if path.endswith(".json") or path.endswith(".meta.json"):
        return "application/json"
    return "text/plain"


def write_bytes(path: str, data: bytes) -> None:
    if using_gcs():
        # A single upload is already atomic from every reader's point of view -
        # GCS never exposes a partially-written object - so unlike the local
        # backend, no temp-object-then-rename dance is needed here.
        _get_bucket().blob(path).upload_from_string(data, content_type=_content_type_for(path))
        return

    local_path = Path(path)
    local_path.parent.mkdir(parents=True, exist_ok=True)

    # Same atomic-replace pattern master_writer.py used to implement itself:
    # write to a temp file in the SAME directory, then os.replace it over the
    # target, so a reader never sees a partially-written file and a crash
    # mid-write never corrupts an existing file.
    temp_fd, temp_name = tempfile.mkstemp(dir=local_path.parent)
    try:
        with os.fdopen(temp_fd, "wb") as f:
            f.write(data)
        os.replace(temp_name, local_path)
    except Exception:
        if os.path.exists(temp_name):
            os.remove(temp_name)
        raise


def append_text(path: str, text: str) -> None:
    if using_gcs():
        # GCS has no native append - read-modify-write is the only option.
        # Only used for the (small, infrequently-written) master write log,
        # so this is never a meaningful cost in practice.
        #
        # KNOWN LIMITATION: this read-modify-write is not safe against
        # concurrent writers - two overlapping calls can race and the later
        # upload_from_string() wins outright, silently dropping the earlier
        # call's appended line. Not an issue for a single Cloud Run instance
        # (today's deployment), but would need a real fix (e.g. a per-append
        # object with log entries listed/merged on read, or a Firestore-backed
        # log) before scaling to multiple concurrent instances.
        existing = read_bytes(path) if exists(path) else b""
        write_bytes(path, existing + text.encode("utf-8"))
        return

    local_path = Path(path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with open(local_path, "a", encoding="utf-8") as f:
        f.write(text)


def delete(path: str) -> None:
    if using_gcs():
        from google.api_core.exceptions import NotFound

        try:
            _get_bucket().blob(path).delete()
        except NotFound:
            pass
        return
    Path(path).unlink(missing_ok=True)


def list_with_mtimes(prefix: str, suffix: str) -> list[tuple[str, float]]:
    """Every path under `prefix` ending in `suffix`, paired with its mtime -
    one list_blobs() call in GCS mode (Blob.updated comes back for free, no
    per-file round trip needed), one glob() in local mode."""
    if using_gcs():
        blobs = _get_bucket().list_blobs(prefix=prefix)
        return [(b.name, b.updated.timestamp()) for b in blobs if b.name.endswith(suffix)]

    local_dir = Path(prefix)
    if not local_dir.exists():
        return []
    # as_posix(), not str() - callers compare these against paths built as
    # plain "prefix/name" strings (matching GCS blob-name conventions), which
    # would never equal a str(Path(...)) backslash-separated path on Windows.
    return [(p.as_posix(), p.stat().st_mtime) for p in local_dir.glob(f"*{suffix}")]
