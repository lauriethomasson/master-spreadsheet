import json
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from schema import ListingRow

MIN_COLUMN_WIDTH = 10
MAX_COLUMN_WIDTH = 40


def write_rows_to_xlsx(rows: list[ListingRow], output_path: Path) -> None:
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

    for col_idx, field in enumerate(fields, start=1):
        max_len = len(field)
        for row in rows:
            value = getattr(row, field)
            if value is not None:
                max_len = max(max_len, len(str(value)))
        width = min(max(max_len + 2, MIN_COLUMN_WIDTH), MAX_COLUMN_WIDTH)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)


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
