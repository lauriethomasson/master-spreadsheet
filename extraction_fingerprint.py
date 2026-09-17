"""
extraction_fingerprint.py

Content-addressable fingerprints of this codebase's own extraction/
enrichment logic - moved out of app.py (which originally computed these
purely for its own content_hash functions) so a second, independent
consumer can use the SAME current-code signal: storage.file_store's own
active_and_superseded_staging_files, which needs to know whether a pending
staging entry was produced by the code that's running RIGHT NOW, not just
by a content_hash that happens to differ from another entry's.

Real, confirmed gap this closes: active_and_superseded_staging_files picks
one "active" staging entry per group of same-source-document pending files
using _enrichment_completeness_rank (brochure-enrichment progress) as its
primary signal, with recency only as a last-resort tie-break - deliberately,
since "latest wins" alone was already tried and rejected (see that
function's own docstring). But neither signal has ever known whether an
entry's own extraction logic is stale: a real case surfaced an OLDER
staging entry - fully brochure-enriched, but extracted before a DOM-text
LET-status fix landed - outranking a FRESH entry extracted with the fixed
code but not yet enrichment-complete, so the Review page displayed a
possible_missed_let_status note the current code could never have produced
(source text had zero LET-status wording; matches was empty). Enrichment
completeness is a real, useful signal for "which of two runs of the SAME
extraction logic is further along," but it's not evidence about which run's
own logic was actually correct - see active_and_superseded_staging_files'
own current_logic_fingerprints parameter for how this module's fingerprints
are used to close that gap.

This module exists purely to break an import cycle: file_store.py is
imported BY app.py (for save_staging_file/active_and_superseded_staging_
files), so app.py's own module can never be imported back from file_store.py
or from a page that only needs the fingerprint values. Living here instead,
with no dependency on app.py/storage.file_store/pages in either direction,
lets app.py (content_hash computation) and pages/2_Review_and_Master.py
(the staging tie-break) both compute the exact same values with no cycle.

Deliberately FUNCTIONS, not module-level constants computed once at import
time: app.py's own _SPREADSHEET_LOGIC_FINGERPRINT/_PDF_EMAIL_LOGIC_
FINGERPRINT rely on being recomputed from disk every time app.py's own
top-level code runs (every real Streamlit script rerun, and every
AppTest.from_file(...).run() in tests) so a genuine source change takes
effect on the very next run without a process restart - see tests/
test_app_upload_geocode_cache_invalidation.py's own UploadReuseAcrossA
FingerprintChangeTests, which simulates exactly that by patching Path.
read_bytes. A plain module-level constant HERE would defeat that: Python
only executes a module's own top-level code once per process (cached in
sys.modules), so importing an already-computed value a second time - from
either app.py's own next rerun or from pages/2_Review_and_Master.py -
would silently keep returning the FIRST run's stale hash. Calling a
function instead re-reads and re-hashes the source files fresh every time,
from every caller, with no caching layer to go stale.

spreadsheet_logic_fingerprint() covers every module a spreadsheet upload's
own processing pipeline unconditionally runs: extract_spreadsheet.py's own
column-header mapping, extract_spreadsheet_gemini.py's own Gemini-assisted
per-sheet extraction, brochure_enrichment.py (automatic enrichment now runs
unconditionally right after extraction), geocode.py (geocode_rows() also
runs unconditionally), and brochure_link_resolver.py (finalize_brochure_link,
called from extract_spreadsheet_gemini.py). Each was, at one point, a real
confirmed invalidation gap on its own: a fix landing in any one of these
without this fingerprint changing meant a byte-identical re-upload of an
already-staged file kept silently reusing pre-fix cached rows - dedup
working exactly as designed, just blind to that one dependency. Hashing
each module's own source directly makes invalidation automatic and
self-maintaining, with no version counter to remember to bump.

pdf_email_logic_fingerprint() is the equivalent for a PDF/email upload:
extract.py's/extract_email.py's own extraction (replacing a prior
human-maintained EXTRACTION_VERSION counter that was confirmed to have
already gone stale across two real fixes), brochure_link_resolver.py (same
finalize_brochure_link call, made by extract.py/extract_email.py instead),
and brochure_enrichment.py (automatic enrichment also runs unconditionally
for PDF/email uploads). geocode.py is deliberately NOT included here - it's
folded into _pdf_or_email_content_hash's own versioned_content in app.py
instead; this fingerprint's own scope is just brochure enrichment's gap,
not a broader reorganization of every dependency's placement.
"""

import hashlib
from pathlib import Path

import brochure_enrichment
import brochure_link_resolver
import extract
import extract_email
import extract_spreadsheet
import extract_spreadsheet_gemini
import geocode


def spreadsheet_logic_fingerprint() -> str:
    return hashlib.sha256(
        Path(extract_spreadsheet.__file__).read_bytes()
        + Path(extract_spreadsheet_gemini.__file__).read_bytes()
        + Path(brochure_enrichment.__file__).read_bytes()
        + Path(geocode.__file__).read_bytes()
        + Path(brochure_link_resolver.__file__).read_bytes()
    ).hexdigest()


def pdf_email_logic_fingerprint() -> str:
    return hashlib.sha256(
        Path(extract.__file__).read_bytes()
        + Path(extract_email.__file__).read_bytes()
        + Path(brochure_link_resolver.__file__).read_bytes()
        + Path(brochure_enrichment.__file__).read_bytes()
    ).hexdigest()
