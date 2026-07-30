import json
import sys
from io import BytesIO
from pathlib import Path

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from schema import ListingRow

MIN_COLUMN_WIDTH = 10
MAX_COLUMN_WIDTH = 40

# Columns holding a URL - given a real openpyxl hyperlink (not just plain text)
# so the cell is directly clickable when the .xlsx is opened in Excel/Google
# Sheets, not just styled to look like a link. The cell's displayed text is
# HYPERLINK_DISPLAY_TEXT, not the (often long, unreadable) raw URL - the real
# URL only ever lives in the hyperlink target from that point on, which is
# exactly why every read-back of a written file (read_xlsx_with_hyperlinks
# below) has to recover it from there instead of the cell's plain value.
HYPERLINK_COLUMNS = ["brochure_link"]
HYPERLINK_FONT = Font(color="0563C1", underline="single")
HYPERLINK_DISPLAY_TEXT = "Open Brochure"

# Columns that regularly hold long text (a multi-sentence description, several
# contacts, or a long URL) - wrapped so the full value is visible on several
# lines within the row instead of overflowing past the column's fixed width or
# being clipped. Deliberately a separate list from display_utils.WIDE_TEXT_COLUMNS:
# that one governs the in-app editable grid's column width and never includes
# brochure_link (the grid shows a fixed "Open Brochure" label there regardless
# of the underlying URL's length), whereas here brochure_link is exactly the
# column most likely to overflow a written .xlsx's column width.
WRAP_COLUMNS = ["special_features", "contacts", "brochure_link"]
WRAP_ALIGNMENT = Alignment(wrap_text=True, vertical="top")

# Points, matching Excel's own row-height unit - tall enough for ~3-4 wrapped
# lines. openpyxl can't compute a true auto-fit height (that's an application-
# side text-layout calculation, not derivable from raw XML), and row height is
# a per-row property in Excel - a row can't be tall for one column's wrapped
# text and short for the rest - so every data row gets this same fixed height.
DATA_ROW_HEIGHT = 60

# Present in every row for traceability (geocode failure logs, the
# brochure_link PDF-fallback default) but not something Mark/Laurie need to
# see day-to-day - hidden as an Excel column rather than dropped from the
# file, so it's still there to unhide/read directly (e.g. via pandas) if a
# listing ever needs tracing back to its source upload. property_id is
# master_merge.py's internal identity for a property across uploads - never
# meaningful to a person, but needed in the file so re-loading the master
# preserves it.
HIDDEN_COLUMNS = ["source_file", "property_id"]


def write_rows_to_xlsx(rows: list[ListingRow], output) -> None:
    """
    output is a filesystem path (str/Path) OR a writable binary stream (e.g.
    io.BytesIO) - openpyxl's Workbook.save() accepts either natively. A
    stream is what master_writer.py/storage/file_store.py pass so the result
    can go straight to blob_store (local disk or a GCS upload) without ever
    touching a local temp file.
    """
    fields = list(ListingRow.model_fields.keys())

    wb = Workbook()
    ws = wb.active
    ws.title = "Listings"

    ws.append(fields)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"

    for row in rows:
        data = row.model_dump()
        ws.append([data[field] for field in fields])

    for row_idx in range(2, ws.max_row + 1):
        ws.row_dimensions[row_idx].height = DATA_ROW_HEIGHT

    for col_idx, field in enumerate(fields, start=1):
        max_len = len(field)
        for row in rows:
            value = getattr(row, field)
            if value is not None:
                max_len = max(max_len, len(str(value)))
        width = min(max(max_len + 2, MIN_COLUMN_WIDTH), MAX_COLUMN_WIDTH)
        column_letter = get_column_letter(col_idx)
        ws.column_dimensions[column_letter].width = width
        if field in HIDDEN_COLUMNS:
            ws.column_dimensions[column_letter].hidden = True

        if field in WRAP_COLUMNS:
            for row_idx in range(2, ws.max_row + 1):
                ws.cell(row=row_idx, column=col_idx).alignment = WRAP_ALIGNMENT

        if field in HYPERLINK_COLUMNS:
            for row_idx in range(2, ws.max_row + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                if cell.value:
                    url = cell.value
                    cell.hyperlink = url
                    cell.value = HYPERLINK_DISPLAY_TEXT
                    cell.font = HYPERLINK_FONT

    if isinstance(output, (str, Path)):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
    wb.save(output)


def read_xlsx_with_hyperlinks(data: bytes) -> pd.DataFrame:
    """
    Reads a workbook written by write_rows_to_xlsx() back into a DataFrame -
    like pd.read_excel(), except for HYPERLINK_COLUMNS, where the cell's
    displayed text is HYPERLINK_DISPLAY_TEXT rather than the real URL (see
    write_rows_to_xlsx). pd.read_excel only ever sees that displayed text,
    which would silently replace every brochure_link with the literal string
    "Open Brochure" on every read-back after a write - reads the cell's
    actual hyperlink target instead for those columns; every other column
    is read exactly as pd.read_excel would.
    """
    wb = load_workbook(BytesIO(data))
    ws = wb.active
    headers = [cell.value for cell in ws[1]]
    hyperlink_col_indices = {i for i, h in enumerate(headers) if h in HYPERLINK_COLUMNS}

    records = []
    for row in ws.iter_rows(min_row=2):
        record = {}
        for col_idx, cell in enumerate(row):
            if col_idx in hyperlink_col_indices and cell.hyperlink is not None:
                record[headers[col_idx]] = cell.hyperlink.target
            else:
                record[headers[col_idx]] = cell.value
        records.append(record)
    return pd.DataFrame(records, columns=headers)


def main():
    if len(sys.argv) != 3:
        print("Usage: python staging_writer.py <rows.json> <output.xlsx>", file=sys.stderr)
        raise SystemExit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        raw_rows = json.load(f)

    rows = [ListingRow(**r) for r in raw_rows]
    write_rows_to_xlsx(rows, Path(sys.argv[2]))
    print(f"Wrote {len(rows)} row(s) to {sys.argv[2]}")


if __name__ == "__main__":
    main()
