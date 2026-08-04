"""
extract_spreadsheet.py

Extraction path for uploaded provider .xlsx/.csv spreadsheets - unlike
extract.py/extract_email.py (which turn unstructured PDF/email content into
ListingRows via Gemini vision), a provider spreadsheet is already one row
per property in tabular form. The only real unknown is which of its own
column headers corresponds to which ListingRow field, so this is a header-
mapping problem, not an extraction problem - no Gemini call is involved.

Flow: read_spreadsheet() gets the raw DataFrame with the provider's own
headers untouched -> suggest_mapping() guesses a {header: field_name_or_
None} mapping automatically (exact synonym match, then a conservative
fuzzy fallback - no per-column human confirmation step) -> if every
CRITICAL_FIELDS entry mapped successfully, build_rows() applies the
mapping straight away. If a critical field didn't map (e.g. a provider
whose export uses the area name itself as the building-column header -
see unmapped_critical_fields), the caller (app.py) shows a narrow, one-
field-at-a-time confirmation prompt instead of the removed full-column
confirm-mapping UI, and remembers that specific field's assignment for
this exact header set going forward (storage.file_store.
get_saved_critical_field_rescue/save_critical_field_rescue, keyed by
header_hash) - every OTHER column still maps automatically, confirmed or
not. Rows then flow through the same geocode_rows()/save_staging_file()
steps as every other source type.
"""

import difflib
import hashlib
import math
import re
from io import BytesIO
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from master_merge import field_kind, normalize_key
from schema import ListingRow
from staging_writer import title_case_label
from storage.file_store import dataframe_to_listing_rows

# Fields no header should ever be mapped to - property_id is assigned only
# once a row lands in the master (master_merge.py), never from a source
# file, and source_file is set programmatically from the upload itself
# (see build_rows), not read from any column.
UNMAPPABLE_FIELDS = ("property_id", "source_file")

# Fields important enough that leaving them unmapped shouldn't happen
# silently - see unmapped_critical_fields. building is schema-required
# (ListingRow.building: str, not Optional) - a spreadsheet whose building
# column goes unmapped doesn't just have one blank field per row, it
# produces ZERO rows outright (dataframe_to_listing_rows skips every row
# with no building at all) - confirmed against two real UNION "by-area"
# export files, whose building-name column is headered with the area's
# own name ("Clerkenwell & Farringdon", "Fitzrovia & Marylebone" - text
# that's different per file and shares no vocabulary with "building" in
# any file-independent way a synonym or fuzzy match could ever catch).
#
# address_1 was considered too (no fallback fills it in either, unlike
# provider's filename-based guess_provider_name) but deliberately left
# out: confirmed against the real Kitt's Availability file - a format
# this rescue mechanism must NOT interrupt, since every one of its
# genuinely critical fields already maps on its own - that Kitt's has NO
# address column at all (only Building, used as the property identifier
# the same way several other real formats also do). Including address_1
# here would prompt for a rescue on Kitt's every single time, for a field
# that format simply doesn't have and was never expected to need. building
# earns the special treatment because losing it is catastrophic (zero
# rows, not just one blank field); address_1 going unmapped just leaves
# that one field blank per row, same as any other non-critical column.
CRITICAL_FIELDS = ("building",)

# Extra known header variants (already in normalize_key's output form - see
# _build_field_synonyms) beyond a field's own name/title-case label, drawn
# from real provider spreadsheets actually seen (not speculative guesses) -
# e.g. UNION's current export format uses "Floor/Unit", "Size (sq ft)",
# "Marketing Price (Based on Min Term) PCM", etc. An unrecognized header
# simply gets no guess (mapped to None) rather than a forced fuzzy pick -
# the confirm-mapping UI is where a human resolves that, not this table.
EXTRA_SYNONYMS = {
    "floor_unit": ("floorunit", "floor"),
    "size_sqft": ("size sq ft", "sq ft"),
    "desks_max": ("desks",),
    "rent_pcm": ("marketing price based on min term pcm",),
    "rent_psf": ("marketing price based on min term psf",),
    "brochure_link": ("brochure pdf", "link to file", "link to brochure", "brochure link", "link to brochure pdf"),
    "address_1": ("property address 1", "address"),
    "postcode": ("property postcode",),
    "lat": ("latitude",),
    "lng": ("longitude",),
    "submarket": ("area",),
    "internal_ref": ("external ref",),
    "provider": ("assigned agents",),
    "special_features": ("key features",),
}


def _build_field_synonyms() -> dict:
    synonyms = {}
    for field_name in ListingRow.model_fields:
        if field_name in UNMAPPABLE_FIELDS:
            continue
        options = {normalize_key(field_name), normalize_key(title_case_label(field_name))}
        options.update(EXTRA_SYNONYMS.get(field_name, ()))
        synonyms[field_name] = options
    return synonyms


FIELD_SYNONYMS = _build_field_synonyms()


# Matches the exact shape of a Google Sheets/IMPORTRANGE .xlsx export's
# formula cells - e.g. '=IFERROR(__xludf.DUMMYFUNCTION("..."),"Area")' or
# '=IFERROR(__xludf.DUMMYFUNCTION("""COMPUTED_VALUE"""),759.0)'. Google's
# export wraps every formula-derived cell this way; __xludf.DUMMYFUNCTION
# is a Google-only placeholder that can never actually execute in any real
# spreadsheet engine, so IFERROR always falls through to its second
# argument - which is exactly the real value a spreadsheet program would
# show for that cell. Greedy .* for the DUMMYFUNCTION argument naturally
# backtracks to the RIGHTMOST "), <fallback>)" split, correctly separating
# the (possibly comma/paren-containing) inner call from the real fallback
# even when the inner argument itself contains nested parens/commas (e.g.
# an IMPORTRANGE(...) call). re.DOTALL since the fallback itself can
# contain a real embedded newline (confirmed against a real export, e.g.
# a "Size \n(sq ft)" header).
_XLUDF_FORMULA_RE = re.compile(r"^=IFERROR\(__xludf\.DUMMYFUNCTION\(.*\),\s*(.+)\)$", re.DOTALL)


def _parse_xludf_fallback(formula):
    """
    Extracts the literal fallback value straight out of a Google Sheets
    export formula's own text - the last-resort path for a cell whose
    cached value (openpyxl's data_only=True) genuinely isn't present
    (can happen if the file was downloaded without ever being opened/
    recalculated by a real spreadsheet program first). Returns None for
    anything that isn't recognizably this exact shape, rather than
    guessing - a caller falls back to treating the cell as blank.
    """
    if not isinstance(formula, str) or not formula.startswith("=IFERROR(__xludf.DUMMYFUNCTION("):
        return None
    match = _XLUDF_FORMULA_RE.match(formula)
    if not match:
        return None
    fallback = match.group(1).strip()
    if len(fallback) >= 2 and fallback.startswith('"') and fallback.endswith('"'):
        return fallback[1:-1].replace('""', '"')
    try:
        return float(fallback) if "." in fallback else int(fallback)
    except ValueError:
        return fallback


def _resolve_cell_value(value_cell, formula_cell):
    """
    value_cell/formula_cell are the SAME cell read from two separate
    load_workbook() calls, one with data_only=True (the last-calculated
    result) and one with data_only=False (the raw formula) - openpyxl
    only exposes one or the other per loaded workbook, never both from a
    single load. Prefers the cached value; falls back to parsing it
    straight out of the formula text (see _parse_xludf_fallback) only when
    the cache is genuinely missing for what really is a formula cell -
    never applied to an actually-blank cell, which has no formula to
    parse in the first place and correctly stays None either way.
    """
    if value_cell.value is not None:
        return value_cell.value
    return _parse_xludf_fallback(formula_cell.value)


def read_spreadsheet(data: bytes, suffix: str) -> pd.DataFrame:
    """
    Reads an uploaded provider spreadsheet's raw headers/rows exactly as the
    provider wrote them - unlike storage.file_store.read_xlsx_with_hyperlinks
    (which assumes OUR OWN header format, written by staging_writer.
    write_rows_to_xlsx), headers here are whatever text is actually in row 1;
    which one means what is decided later by suggest_mapping/the confirm-
    mapping UI, never assumed from position or a fixed label set.

    Every .xlsx cell carrying a real hyperlink target uses that target
    instead of its displayed text (e.g. a "Brochure PDF" column showing just
    "Open" or "View") - which column ends up mapped to brochure_link isn't
    known yet at read time, so every column gets this treatment, not just
    ones that already look like a link column.

    Loads the workbook twice - once per openpyxl data_only mode - so every
    cell's cached value is available AND its raw formula text is available
    as a fallback (see _resolve_cell_value): confirmed necessary against a
    real Google Sheets/IMPORTRANGE export (Kitt's Availability), whose
    every header/data cell is a formula wrapping __xludf.DUMMYFUNCTION -
    reading only the formula (the old behavior) surfaced that raw formula
    text as if it were the real header/value.
    """
    if suffix == ".csv":
        return pd.read_csv(BytesIO(data))

    wb_values = load_workbook(BytesIO(data), data_only=True)
    ws_values = wb_values.active
    wb_formulas = load_workbook(BytesIO(data), data_only=False)
    ws_formulas = wb_formulas.active

    headers = [
        _resolve_cell_value(vcell, fcell) for vcell, fcell in zip(ws_values[1], ws_formulas[1])
    ]
    records = []
    for value_row, formula_row in zip(ws_values.iter_rows(min_row=2), ws_formulas.iter_rows(min_row=2)):
        record = {}
        for col_idx, (vcell, fcell) in enumerate(zip(value_row, formula_row)):
            header = headers[col_idx]
            if vcell.hyperlink is not None:
                record[header] = vcell.hyperlink.target
            else:
                record[header] = _resolve_cell_value(vcell, fcell)
        records.append(record)
    return pd.DataFrame(records, columns=headers)


# Words that carry no field-identifying meaning on their own - dropped
# before fuzzy word-matching so e.g. "Link to Brochure" and "Brochure"
# compare on {"link", "brochure"} vs {"brochure"} rather than being thrown
# off by "to". Deliberately NOT stripped from the exact-match path above -
# that path already gets normalize_key's full string, and a real header
# genuinely containing one of these words is either an exact synonym
# already (handled) or should fall through to fuzzy matching regardless.
_FUZZY_STOPWORDS = frozenset({"to", "of", "the", "a", "an", "for", "and"})

# A header word and a candidate word must be at least this similar (per
# difflib.SequenceMatcher.ratio(), 0-1) to count as "the same word" for
# fuzzy matching - confirmed against every real UNION/Kitt's header known
# to have no genuine field ("For Sale", "Min. Term", "Floor Plan", "Patch?",
# "Who Onboarded?", etc. - see tests.test_extract_spreadsheet), none of
# which cleared even 0.667 bidirectional similarity against any field, so
# 0.84 leaves comfortable headroom while still catching genuine near-misses
# ("Special Feature" vs special_features -> 0.967, "Rent (PCM)" vs
# rent_pcm -> 1.0). Short words are blocked from this ratio check entirely
# (see _word_similarity) - "To"/"Let" vs "Lat" would otherwise score 0.667
# from small-string noise alone, nowhere near a real match.
_FUZZY_WORD_MATCH_THRESHOLD = 0.84


def _fuzzy_words(key: str) -> list:
    return [w for w in key.split() if w not in _FUZZY_STOPWORDS]


def _word_similarity(a: str, b: str) -> float:
    # A short word (e.g. "to", "let", "lat") almost always scores
    # deceptively high against another short, unrelated word purely from
    # small-string noise (difflib.ratio() on "to"/"lat" is 0.67) - real
    # false positive found during the fuzzy-match investigation ("To Let"
    # scored 0.67 against "lat" this way). Requiring an exact match below
    # 3 characters closes that off without needing a separate length-ratio
    # guard.
    if len(a) < 3 or len(b) < 3:
        return 1.0 if a == b else 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _fuzzy_field_score(header_words: list, candidate_words: list) -> float:
    """
    0 unless EVERY header word has a close match among candidate_words AND
    EVERY candidate word has a close match among header_words - both
    directions. One-directional coverage is what produced the worst false
    positives in the fuzzy-match investigation: "Min. Term" (-> {"min",
    "term"}) sits entirely inside rent_pcm's own synonym "marketing price
    based on min term pcm" (-> {"marketing","price","based","min","term",
    "pcm"}), so a header-words-covered-by-candidate check alone scored it
    a perfect match to the wrong field. Requiring the reverse direction too
    - every candidate word must also be covered - correctly rejects that,
    since "marketing"/"price"/"based"/"pcm" have nothing to match against
    in "Min. Term". Returns the (mean forward, mean backward) average
    similarity purely so multiple full-coverage candidates (rare, given how
    strict this already is) can be ranked against each other, not as a
    softened threshold - full bidirectional coverage is still required for
    any nonzero result.
    """
    if not header_words or not candidate_words:
        return 0.0
    forward = [max(_word_similarity(hw, cw) for cw in candidate_words) for hw in header_words]
    backward = [max(_word_similarity(hw, cw) for hw in header_words) for cw in candidate_words]
    if min(forward) < _FUZZY_WORD_MATCH_THRESHOLD or min(backward) < _FUZZY_WORD_MATCH_THRESHOLD:
        return 0.0
    return (sum(forward) / len(forward) + sum(backward) / len(backward)) / 2


def _fuzzy_match(header_words: list, used_fields: set) -> str:
    best_field, best_score = None, 0.0
    for field_name, options in FIELD_SYNONYMS.items():
        if field_name in used_fields:
            continue
        for option in options:
            score = _fuzzy_field_score(header_words, _fuzzy_words(option))
            if score > best_score:
                best_field, best_score = field_name, score
    return best_field


def header_hash(headers: list) -> str:
    """
    Identifies one exact header set, in order - a provider's recurring
    export (e.g. a monthly UNION availability sheet) keeps the same headers
    in the same order every time, so this is enough to recognize "we've
    already resolved this format's critical fields" on a later re-upload
    (see storage.file_store.get_saved_critical_field_rescue), without
    needing anything fuzzier. ␟ (a control character never legitimately
    present in a spreadsheet header) separates fields so a header itself
    containing "|" or "," can't collide with the join.
    """
    joined = "␟".join(str(h) for h in headers)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def unmapped_critical_fields(mapping: dict) -> list:
    """
    Which of CRITICAL_FIELDS has no header mapped to it, in CRITICAL_FIELDS
    order - the caller (app.py) uses this to decide whether a spreadsheet
    upload can proceed fully automatically, or needs a targeted rescue
    prompt for specifically these fields (never a reason to re-confirm
    every column - every other field is trusted to suggest_mapping either
    way).
    """
    mapped_fields = {field for field in mapping.values() if field}
    return [field for field in CRITICAL_FIELDS if field not in mapped_fields]


def apply_critical_field_rescue(mapping: dict, rescue: dict) -> dict:
    """
    Overlays a human-confirmed {field_name: header_or_None} rescue (see
    storage.file_store.get_saved_critical_field_rescue) on top of an
    automatic mapping - a non-None header is assigned to that field even if
    suggest_mapping had guessed something else for it (a human's explicit
    choice always wins over an automatic guess); None means "confirmed - this
    format genuinely has no such column," left unmapped exactly as
    suggest_mapping already left it, just without asking again next time.
    Returns a new dict; never mutates the mapping passed in.
    """
    mapping = dict(mapping)
    for field, header in rescue.items():
        if header is not None:
            mapping[header] = field
    return mapping


def unresolved_critical_fields(mapping: dict, rescue: dict) -> list:
    """
    Critical fields that still need a human decision - unmapped_critical_
    fields() on the ALREADY-RESCUED mapping (see apply_critical_field_
    rescue), minus whatever `rescue` already has an entry for. A rescue
    entry of None means "confirmed - genuinely no such column," not "still
    needs asking" - without subtracting those out, a confirmed-blank field
    (e.g. a format that truly has no address_1 column) would prompt again
    on every single future upload of that same format, forever, rather than
    staying silently resolved the way a real assignment already does.
    """
    missing = unmapped_critical_fields(mapping)
    return [field for field in missing if field not in rescue]


def suggest_mapping(headers: list) -> dict:
    """
    Best-guess {header: field_name_or_None} mapping - applied automatically
    to every spreadsheet upload with no human confirmation step (see
    build_rows/app.py); a column that ends up genuinely unmapped is simply
    dropped, so both passes below are deliberately conservative rather than
    forcing a guess.

    Pass 1 - exact match via FIELD_SYNONYMS (field name/title-case label/
    known real-header synonyms, see EXTRA_SYNONYMS). Pass 2 - for whatever
    a header didn't exact-match, a conservative word-level fuzzy fallback
    (see _fuzzy_field_score) catches near-miss variants of a field's own
    name or a known synonym (pluralization, punctuation/parenthetical
    noise, minor rewording) without needing every possible phrasing hand-
    added to EXTRA_SYNONYMS - e.g. "Rent (PCM)" or "Special Feature"
    (singular) - while staying strict enough to leave genuinely unrelated
    headers (a real spreadsheet's "Min. Term", "Floor Plan", "Patch?", etc.)
    unmapped rather than guessing wrong; see the fuzzy-match investigation
    for the false positives an earlier, looser design produced against
    these exact columns.

    Never guesses two headers onto the same field, in EITHER pass - first
    match (in header order) wins, every later header that would also match
    that field is left unmapped instead, since a real spreadsheet shouldn't
    have two columns both claiming to be e.g. size_sqft, and guessing one
    of them wrong silently would be worse than dropping it.
    """
    mapping = {}
    used_fields = set()
    for header in headers:
        key = normalize_key(header)
        matched_field = None
        for field_name, options in FIELD_SYNONYMS.items():
            if field_name in used_fields:
                continue
            if key in options:
                matched_field = field_name
                break
        mapping[header] = matched_field
        if matched_field:
            used_fields.add(matched_field)

    for header in headers:
        if mapping[header] is not None:
            continue
        header_words = _fuzzy_words(normalize_key(header))
        matched_field = _fuzzy_match(header_words, used_fields)
        mapping[header] = matched_field
        if matched_field:
            used_fields.add(matched_field)

    return mapping


# Words that recur across many different providers' own export filenames
# and describe the FILE, not who's presenting it - stripped when guessing a
# default provider name (see guess_provider_name). Deliberately a short,
# evidence-grounded list (every entry drawn from a real filename seen in
# this project) rather than a speculative attempt at every possible
# boilerplate word - a guess that keeps a stray word is a minor annoyance
# a human corrects in one edit; this is only ever a pre-filled starting
# point for the one-time provider-name prompt, never used unconfirmed.
_PROVIDER_GUESS_STOPWORDS = frozenset({
    "availability", "external", "export", "extract", "download", "report",
    "update", "updated", "current", "live", "final", "draft", "copy",
    "schedule", "listing", "listings", "spreadsheet", "sheet", "data",
})

# Numeric date-like substrings ("2026-07-17", "2026_07_17", "17.07.2026")
# commonly embedded in a recurring export's filename - describe when the
# file was produced, not who produced it. No \b anchors: a real filename
# routinely butts the date straight up against an underscore separator
# ("Garden_2026-07-17") - "_" is itself a \w character, so \b never fires
# at that boundary and the date would go unstripped. The digit-group/
# separator shape is already specific enough not to need one.
_FILENAME_DATE_RE = re.compile(r"\d{4}[-_./]\d{1,2}[-_./]\d{1,2}|\d{1,2}[-_./]\d{1,2}[-_./]\d{2,4}")


def guess_provider_name(filename: str) -> str:
    """
    Best-guess provider name derived from an uploaded spreadsheet's own
    filename - the pre-filled default for the one-time per-header-format
    provider-name prompt (see app.py), never applied unconfirmed. Strips
    the extension, parenthetical/bracketed asides (almost always describe
    the file - "(External)", "[DRAFT]" - not who's presenting it), embedded
    dates, and a small set of generic boilerplate words that recur across
    many providers' own export filenames (see _PROVIDER_GUESS_STOPWORDS) -
    e.g. "Kitt's Availability (External).xlsx" -> "Kitt's". Falls back to
    the bare filename stem (extension stripped only) if stripping
    everything would leave nothing at all, rather than guessing blank.
    """
    stem = Path(filename).stem
    stripped = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", stem)
    stripped = _FILENAME_DATE_RE.sub(" ", stripped)
    words = re.findall(r"[A-Za-z0-9']+", stripped)
    kept = [w for w in words if w.lower() not in _PROVIDER_GUESS_STOPWORDS]
    guess = " ".join(kept).strip()
    return guess or stem


def _coerce_numeric(value, kind: str):
    """
    None for anything that isn't a real number - confirmed necessary
    against an actual current-format UNION file, whose Lat/Lng columns
    hold the literal text "Needs manual lookup" for rows the provider
    hasn't geocoded yet themselves, sitting in what's otherwise a numeric
    column. Passing that straight into ListingRow's lat: Optional[float]
    would raise a validation error and fail the WHOLE file's extraction
    over one placeholder cell in one column - dropping it to None instead
    (same as a genuinely blank cell) leaves the row's other, valid fields
    intact and lets geocode_rows attempt a real lookup for that row, same
    as it always has for PDF/email sources with no coordinates at all.

    kind is the target ListingRow field's own declared type ("int" or
    "float" - see master_merge.field_kind). For an "int" field, a
    genuinely fractional value is rounded to the nearest whole number
    rather than passed through as-is - confirmed necessary against a real
    Kitt's Availability row, whose desks_max resolved to 38.257143 (almost
    certainly a computed/averaged cell caught up in the mapped column
    range). Pydantic's int validation rejects a fractional float outright,
    which - like the lat/lng case above - would otherwise abort the WHOLE
    file's extraction over one cell; a rounded desk count is a far better
    outcome than that.
    """
    if value is None:
        numeric = None
    elif isinstance(value, (int, float)):
        numeric = value
    else:
        try:
            numeric = float(str(value).replace(",", "").strip())
        except ValueError:
            numeric = None

    # A pandas NaN is itself a float (math.isnan(float("nan")) is True, and
    # isinstance(nan, float) is True) - it slides straight through the
    # isinstance branch above as if it were a real number, rather than
    # hitting the ValueError branch a non-numeric string would. round(nan)
    # raises ValueError - confirmed necessary against a real Kitt's
    # Availability file, whose desks_max column has genuinely blank cells
    # that pandas reads as NaN rather than None. Treated the same as a
    # genuinely blank cell (None), for the same reason as every other
    # branch in this function: one bad cell must not abort the WHOLE
    # file's extraction.
    if isinstance(numeric, float) and math.isnan(numeric):
        numeric = None

    if numeric is not None and kind == "int":
        return round(numeric)
    return numeric


def build_rows(df: pd.DataFrame, mapping: dict, source_file: str) -> list:
    """
    Applies a confirmed {header: field_name_or_None} mapping and converts
    the result to real ListingRows - reuses storage.file_store.
    dataframe_to_listing_rows for the actual row construction (blank-row
    skipping, NaN cleanup, ListingRow(**cleaned) with extra="ignore" for any
    column left unmapped) rather than duplicating that logic here, exactly
    as the earlier design investigation recommended. Every column mapped to
    an int/float ListingRow field is coerced first (see _coerce_numeric) -
    a raw provider spreadsheet's numeric-looking columns aren't guaranteed
    to actually be clean numbers.

    No longer fills in a default provider itself - app.py's
    fill_missing_provider does that uniformly for every upload type (PDF,
    email, spreadsheet, reused rows alike), not just spreadsheets, so
    duplicating the same fallback here would be redundant.
    """
    renamed = {header: field_name for header, field_name in mapping.items() if field_name}
    mapped_df = df[[h for h in df.columns if h in renamed]].rename(columns=renamed)
    for field_name in mapped_df.columns:
        kind = field_kind(field_name)
        if kind in ("int", "float"):
            mapped_df[field_name] = mapped_df[field_name].apply(lambda v: _coerce_numeric(v, kind))
    rows = dataframe_to_listing_rows(mapped_df)
    for row in rows:
        row.source_file = source_file
    return rows
