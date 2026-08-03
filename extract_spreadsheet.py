"""
extract_spreadsheet.py

Extraction path for uploaded provider .xlsx/.csv spreadsheets - unlike
extract.py/extract_email.py (which turn unstructured PDF/email content into
ListingRows via Gemini vision), a provider spreadsheet is already one row
per property in tabular form. The only real unknown is which of its own
column headers corresponds to which ListingRow field, so this is a header-
mapping problem, not an extraction problem - no Gemini call is involved.

Flow: read_spreadsheet() gets the raw DataFrame with the provider's own
headers untouched -> header_hash() identifies this exact header set/order
-> a caller checks storage.file_store.get_saved_header_mapping() for a
previously-confirmed mapping, or shows suggest_mapping()'s best guess to a
human for confirmation (storage.file_store.save_header_mapping()) -> once a
mapping is confirmed, build_rows() applies it and produces real ListingRows,
which then flow through the same geocode_rows()/save_staging_file() steps
as every other source type.
"""

import hashlib
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
    "brochure_link": ("brochure pdf", "link to file"),
    "address_1": ("property address 1", "address"),
    "postcode": ("property postcode",),
    "lat": ("latitude",),
    "lng": ("longitude",),
    "submarket": ("area",),
    "internal_ref": ("external ref",),
    "provider": ("assigned agents",),
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
    """
    if suffix == ".csv":
        return pd.read_csv(BytesIO(data))

    wb = load_workbook(BytesIO(data))
    ws = wb.active
    headers = [cell.value for cell in ws[1]]
    records = []
    for row in ws.iter_rows(min_row=2):
        record = {}
        for col_idx, cell in enumerate(row):
            header = headers[col_idx]
            if cell.hyperlink is not None:
                record[header] = cell.hyperlink.target
            else:
                record[header] = cell.value
        records.append(record)
    return pd.DataFrame(records, columns=headers)


def header_hash(headers: list) -> str:
    """
    Identifies one exact header set, in order - a provider's recurring
    export (e.g. a monthly UNION availability sheet) keeps the same headers
    in the same order every time, so this is enough to recognize "we've
    already confirmed a mapping for this format" on a later re-upload,
    without needing anything fuzzier. ␟ (a control character never
    legitimately present in a spreadsheet header) separates fields so a
    header itself containing "|" or "," can't collide with the join.
    """
    joined = "␟".join(str(h) for h in headers)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def suggest_mapping(headers: list) -> dict:
    """
    Best-guess {header: field_name_or_None} mapping via FIELD_SYNONYMS.
    Never guesses two headers onto the same field - first match (in header
    order) wins, every later header that would also match that field is
    left unmapped instead, since a real spreadsheet shouldn't have two
    columns both claiming to be e.g. size_sqft, and guessing one of them
    wrong silently would be worse than leaving it for a human to assign.
    Purely a starting point for the confirm-mapping UI - never used to
    build rows without a human confirming it first (see build_rows).
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
    return mapping


def _coerce_numeric(value):
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
    """
    if value is None or isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


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
    """
    renamed = {header: field_name for header, field_name in mapping.items() if field_name}
    mapped_df = df[[h for h in df.columns if h in renamed]].rename(columns=renamed)
    for field_name in mapped_df.columns:
        if field_kind(field_name) in ("int", "float"):
            mapped_df[field_name] = mapped_df[field_name].apply(_coerce_numeric)
    rows = dataframe_to_listing_rows(mapped_df)
    for row in rows:
        row.source_file = source_file
    return rows
