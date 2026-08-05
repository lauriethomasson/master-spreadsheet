"""
extract_spreadsheet_gemini.py

Gemini-based fallback for spreadsheet sheets that extract_spreadsheet.py's
column-header-mapping approach can't handle - a provider file with no single
consistent header row for the whole sheet (e.g. a repeating per-building
block layout: building name, prose description, address, then THAT
building's own mini-table of units), rather than one clean table straight
through. Real examples confirmed against actual provider files: Copthall
Estates' free-form building/description/unit layout, versus Kitt's/UNION's
clean single-table exports (which header-mapping already handles for free,
with no Gemini call at all - this module is only ever reached when that
path can't find a `building` column, see app.py).

Reuses the exact same text-extraction machinery extract_email.py already
uses (call_gemini given plain text, not images) - a spreadsheet's content is
fundamentally positional data, not a visual document the way a PDF often is,
so there's no separate vision-extraction mechanism to build here.
"""

import sys

from brochure_link_resolver import finalize_brochure_link
from gemini_client import call_gemini, compute_rent, get_client
from schema import ExtractedFields, ListingRow

PROMPT = """You are extracting structured commercial office availability data from ONE SHEET of a
provider's Excel availability spreadsheet. The sheet has been converted to plain text, one line per
non-blank spreadsheet row, prefixed with its original row number (e.g. "Row 14: ..."), with cell
values on that row joined by " | ", preserving column position (an empty string between two pipes
means that cell was blank or a merged-cell continuation of the value above it in that same column -
e.g. an Area/Building value that's blank on a row belongs to the SAME area/building as the nearest
row above it that DID state one in that column). Gaps in row numbers mean fully-blank rows were
skipped - they carry no information themselves, but a large gap often separates one section from the
next. A cell that contains a real hyperlink shows its actual target URL in parentheses right after its
displayed text, e.g. "Download Brochure (https://example.com/brochure.pdf)" - use that URL, not the
display text before it.

Two different sheet shapes are both common - work out which one this sheet actually is from its own
content, don't assume either:
(a) ONE CONSISTENT TABLE: a single header row near the top naming every column (e.g. "Building",
    "Floor/Unit", "Size (sq ft)", ...), followed by one data row per unit, straight through to the
    end of the sheet, in the same column order throughout.
(b) REPEATING BLOCKS: no single sheet-wide header row - instead the sheet has one block per building,
    each shaped roughly like: a line naming the building (often with its submarket/area after a
    " - ", e.g. "28 Lime Street - Fenchurch St / Bank"), a prose paragraph describing the building's
    amenities (not per-unit data - ignore for field extraction), a line with the building's real
    street address plus link labels like "Download Floorplans"/"Download Brochure" (ignore the label
    text itself unless a URL is given, see above), then THIS BUILDING'S OWN mini-table header row,
    then one or more data rows under it - each one a separate unit in that building. If a data row's
    first cell just says "Fully Occupied" (no real numbers), that building currently has NO available
    units - do not emit a unit for it.

Boilerplate to ignore entirely everywhere (never emit a unit or a contact from these): legal
disclaimers, terms and conditions notices, and a repeated generic company-wide enquiry email/phone
line. DO extract named individual contact people (e.g. "Kiri Norton-Brennan - 0795 811 8382") as
contacts even if a generic line also appears elsewhere. If the sheet has no per-unit availability data
at all (e.g. it's a commission/incentive-structure explanation, or a portfolio-wide index page rather
than its own listings), return an empty units list rather than inventing units from unrelated content.

Extract file-level information:
- provider: the company this spreadsheet belongs to (from a title, branding, or an "Assigned
  Agents"/company-name column). Transcribe the exact spelling/capitalization shown - never
  paraphrase or substitute a spelling you recognize from general knowledge of the brand.
- contacts: every NAMED individual contact person listed, each as "Name, email, phone" (omit
  whichever of email/phone isn't given). Join multiple with "; ".

Then extract EVERY SEPARATE AVAILABLE UNIT:
- building: the building name. Never leave this null - inherit from the most recent row/column that
  stated one if a data row doesn't restate it (shape (b)), or from that row's own Building-like
  column (shape (a)).
- submarket: the area/station text, from a " - " suffix on a building-name line (shape (b)) or an
  Area-like column (shape (a)).
- address_1: the street address, if given as its own value (before any postcode).
- postcode: the UK postcode, if given (often part of the same address text, or its own column).
- floor_unit: the unit's own label (e.g. "4th Floor", "G/LG East", "4th & 5th (Private Terrace)").
- size_sqft: the square footage figure for that row.
- desks_min / desks_max: an explicit desk count if one is stated as a number (a plain number is
  desks_max; a range like "10-15" is desks_min=10, desks_max=15); otherwise null - never guess one
  from square footage.
- rent_psf: a per-square-foot rate column/value if present.
- rent_pcm: a monthly rent total column/value if present.
- special_features: the unit's own description/features text, plus a commission/incentive column's
  text appended after a semicolon if one is given for that row.
- state_of_space: the physical fit-out state ONLY if the text EXPLICITLY states one (e.g. "Fitted",
  "CAT A", "To be fitted out") - never infer or guess this from a description that merely sounds
  furnished/equipped; leave null whenever the source doesn't use an explicit fit-out term.
- brochure_link: a URL given directly in the sheet content for that specific row/building, if any
  (see the hyperlink note above); otherwise null.

Return your answer as a single JSON object with this exact structure:

{
  "provider": "..." or null,
  "contacts": "..." or null,
  "units": [
    {
      "building": "...",
      "submarket": "..." or null,
      "address_1": "..." or null,
      "postcode": "..." or null,
      "floor_unit": "..." or null,
      "size_sqft": number or null,
      "desks_min": integer or null,
      "desks_max": integer or null,
      "rent_pcm": number or null,
      "rent_psf": number or null,
      "special_features": "..." or null,
      "state_of_space": "..." or null,
      "brochure_link": "..." or null
    }
  ]
}

Return ONLY this JSON object. No preamble, no explanation, no markdown code fences - just the raw JSON.

Sheet content follows:

"""


def render_sheet_as_text(ws) -> str:
    """
    One line per non-blank row, prefixed with its real spreadsheet row
    number, cells joined by " | " preserving column position (a blank/
    merged-continuation cell renders as an empty string between two pipes,
    never dropped - column position is exactly what tells Gemini a value
    belongs to the same building/area as the nearest stated one above it in
    shape (b), see PROMPT). A cell with a real hyperlink gets its target
    inlined after its displayed text, since a provider's "Download Brochure"
    link is usually a display label over the real URL, not the URL itself -
    dropping it would silently make every such link unextractable.

    Fully-blank rows are skipped entirely (not just blanked) - confirmed
    against real Copthall Estates files, whose declared sheet dimensions run
    to hundreds or thousands of rows purely from Excel formatting bloat, while
    genuinely non-empty content is a few dozen rows at most; skipping keeps
    the rendered text small (and cheap) without losing any real information,
    since a row-number gap already signals "rows were skipped here" on its
    own.
    """
    lines = []
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
        cells = []
        for cell in row:
            value = cell.value
            text = "" if value is None else str(value).replace("\n", " ").strip()
            if cell.hyperlink is not None and cell.hyperlink.target:
                text = f"{text} ({cell.hyperlink.target})" if text else cell.hyperlink.target
            cells.append(text)
        if not any(c.strip() for c in cells):
            continue
        last_non_blank = max(i for i, c in enumerate(cells) if c.strip())
        lines.append(f"Row {row[0].row}: " + " | ".join(cells[: last_non_blank + 1]))
    return "\n".join(lines)


def extract_sheet(ws, sheet_label: str, filename: str) -> list[ListingRow]:
    """
    ws: an openpyxl worksheet (already loaded by the caller - see app.py,
    which needs the same workbook open for other sheets in the same file
    too, so loading happens once at the call site rather than per sheet
    here).

    sheet_label is what a person sees in the source_file column and any
    skipped-sheet/sanity-check message - "filename — Sheet Name" so a
    multi-sheet upload's rows/warnings are traceable back to which sheet
    produced them, since a single .xlsx upload can now yield rows from
    several different sheets via several different Gemini calls.
    """
    text = render_sheet_as_text(ws)
    if not text.strip():
        return []
    client = get_client()
    raw = call_gemini(client, PROMPT, [text])

    brochure = {
        "internal_ref": raw.get("provider"),
        "provider": raw.get("provider"),
        "contacts": raw.get("contacts"),
    }

    rows = []
    last_building = None
    for i, unit in enumerate(raw.get("units", [])):
        if not unit.get("building"):
            if not last_building:
                print(
                    f"Warning: {sheet_label} unit {i} has no building and no prior "
                    "unit to inherit one from — skipping this unit.",
                    file=sys.stderr,
                )
                continue
            unit["building"] = last_building
        last_building = unit["building"]

        unit["brochure_link"] = finalize_brochure_link(
            unit.get("brochure_link"), is_pdf=False, pdf_fallback_link=filename
        )

        fields = ExtractedFields(**brochure, **unit).model_dump()
        fields = compute_rent(fields)
        rows.append(
            ListingRow(
                **fields,
                lat=None,
                lng=None,
                source_file=sheet_label,
            )
        )
    return rows
