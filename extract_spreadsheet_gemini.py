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

import re
import sys

from brochure_link_resolver import finalize_brochure_link
from gemini_client import call_gemini, compute_rent, get_client
from house_number import LEADING_HOUSE_NUMBER_RE, leading_house_number
from schema import ExtractedFields, ListingRow

# Strips render_sheet_as_text's own "Row 14: " row-number prefix back off a
# rendered line before any house-number regex ever sees it - otherwise the
# row number itself (also a leading digit sequence) would be mistaken for
# the address's leading house number.
_ROW_PREFIX_RE = re.compile(r"^Row \d+:\s*")

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
- address_1: the street address, if given as its own value (before any postcode). Transcribe it EXACTLY
  character-for-character as it appears in the source text - especially a hyphenated house-number
  range like "14-18": never shorten, round, or simplify a range down to a single number (e.g. "14-18
  Copthall Avenue" must stay "14-18 Copthall Avenue", never become "18 Copthall Avenue").
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


def _next_distinct_building(units: list[dict], i: int):
    """The building name of the next unit after index i whose building
    differs from unit i's own (units' own iteration order, after inheritance
    has already backfilled every unit's building - see extract_sheet), or
    None if every remaining unit shares this one's building or there are no
    units left. Used only to bound a building's own block in the raw sheet
    text (see _raw_house_number_for_unit) - several consecutive units
    legitimately share one building block (one row each for that building's
    own several available floors), so the block's end is the next DIFFERENT
    building, not simply the next unit."""
    current = units[i]["building"]
    for unit in units[i + 1:]:
        if unit["building"] != current:
            return unit["building"]
    return None


def _raw_house_number_for_unit(raw_text: str, building, next_building, postcode):
    """
    The leading house-number token (see leading_house_number) of the raw
    sheet-text line that actually belongs to this unit's address - the
    ground truth to verify Gemini's own address_1 extraction against, since
    a regex over the raw text can't drop characters the way an LLM
    transcription can. Returns None whenever no single raw line can be
    identified with confidence - callers must then leave Gemini's own
    extraction untouched rather than risk overriding a correct value from an
    ambiguous match.

    Two strategies, preferring the more direct one:
    - postcode given: the address line is whichever raw line contains that
      exact postcode text - reliable regardless of sheet shape, but only
      when EXACTLY one raw line contains it (0 or multiple is ambiguous).
    - no postcode: fall back to this building's own "block" in a repeating-
      block sheet (see PROMPT's shape (b)) - the lines between this
      building's own heading line and the next DIFFERENT building's heading
      line (or end of text) - and take the first line in that block whose
      own leading cell starts with a house number. Shape (a) (one
      consistent table, building repeated per row with no distinct heading
      line) has no such block to find, so this returns None for it - the
      postcode strategy is the only reliable one there.
    """
    lines = [_ROW_PREFIX_RE.sub("", line) for line in raw_text.splitlines()]

    if postcode and str(postcode).strip():
        needle = str(postcode).strip().lower()
        matches = [line for line in lines if needle in line.lower()]
        if len(matches) != 1:
            return None
        return leading_house_number(matches[0].split("|", 1)[0])

    if not building or not str(building).strip():
        return None
    building_key = str(building).strip().lower()
    start = next((i for i, line in enumerate(lines) if line.strip().lower().startswith(building_key)), None)
    if start is None:
        return None

    end = len(lines)
    if next_building and str(next_building).strip().lower() != building_key:
        next_key = str(next_building).strip().lower()
        later = [i for i in range(start + 1, len(lines)) if lines[i].strip().lower().startswith(next_key)]
        if later:
            end = later[0]

    candidates = [
        leading_house_number(lines[i].split("|", 1)[0])
        for i in range(start + 1, end)
    ]
    candidates = [n for n in candidates if n is not None]
    return candidates[0] if len(candidates) == 1 else None


def _splice_leading_house_number(address_1: str, raw_token: str) -> str:
    """address_1 with only its OWN leading house-number token replaced by
    raw_token, everything else Gemini extracted (street name spelling,
    capitalization, trailing postcode text, etc.) left exactly as-is - never
    a wholesale replacement with the raw line's own full text, which may
    carry link labels or other columns' content that don't belong in
    address_1 at all."""
    match = LEADING_HOUSE_NUMBER_RE.match(address_1)
    if match:
        return raw_token + address_1[match.end():]
    return f"{raw_token} {address_1.strip()}"


def _verify_address_house_numbers(units: list[dict], raw_text: str, sheet_label: str):
    """
    Cross-checks every unit's Gemini-extracted address_1 against the raw
    sheet text its building block came from (see _raw_house_number_for_unit)
    and corrects it in place whenever the two disagree on the leading house
    number - the real, confirmed failure mode this guards against: Gemini
    transcribing "14-18 Copthall Avenue" as just "18 Copthall Avenue",
    silently dropping the first half of a hyphenated range. Regex parsing of
    the raw cell text is deterministic and can't drop characters the way an
    LLM transcription can, so it wins whenever the two disagree.

    Mutates each unit dict's "address_1" in place; a unit with no address_1
    at all is skipped (nothing to verify). When no raw line can be
    identified with confidence, the value is left untouched and a warning is
    printed to stderr (same style as geocode.py's log_geocode_failure) so a
    silently-unverifiable extraction is still visible in extraction logs,
    rather than either crashing or guessing.
    """
    for i, unit in enumerate(units):
        address_1 = unit.get("address_1")
        if not address_1 or not str(address_1).strip():
            continue

        raw_token = _raw_house_number_for_unit(
            raw_text, unit.get("building"), _next_distinct_building(units, i), unit.get("postcode")
        )
        if raw_token is None:
            print(
                f"[extract_spreadsheet_gemini] WARNING: {sheet_label} unit {i} "
                f"({unit.get('building')!r}) - could not confidently locate this unit's "
                f"address line in the raw sheet text to verify address_1 {address_1!r} "
                "against; leaving it as extracted.",
                file=sys.stderr,
            )
            continue

        if raw_token != leading_house_number(address_1):
            unit["address_1"] = _splice_leading_house_number(address_1, raw_token)


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

    units = []
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
        units.append(unit)

    # Runs only once every unit's building is resolved (inheritance above) -
    # _next_distinct_building needs every unit's real building name to find
    # a block's boundary, not just the ones that restated their own.
    _verify_address_house_numbers(units, text, sheet_label)

    rows = []
    for unit in units:
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


def _heading_cell(line: str) -> str:
    """The first non-blank pipe-separated cell on a rendered line, stripped -
    a repeating-block sheet's heading/address/data lines all carry an entirely
    blank leading column on the real files this was confirmed against, so the
    building name itself lives in the first cell AFTER that blank one, never
    literally at the start of the line."""
    return next((c.strip() for c in line.split("|") if c.strip()), "")


def _apparent_data_row_count(raw_text: str, building: str) -> int:
    """
    Rough count of how many genuine per-unit data rows a building's own
    block in the raw sheet text appears to contain - a cheap, structural
    cross-check against how many units extract_sheet actually returned
    for that building (see app.py's _warn_if_units_look_undercounted),
    never a replacement for reading the sheet. Real, confirmed production
    case this exists to catch: a real Copthall Estates "11 Cursitor
    Street" mini-table with 3 floor rows (G/LG East, 1st Floor, 4th
    Floor) came back from a live Gemini call with only 1 unit - a
    silent, validly-parsed but short JSON response that no existing
    check catches, since the one row that DID survive had a completely
    normal, non-garbled size_sqft/rent_pcm (ruling out app.py's own
    _warn_if_extraction_looks_garbled).

    Bails to 0 immediately if the raw text has no "download" line
    anywhere (see PROMPT shape (b)'s own address/link line note) - with
    no per-building link marker anywhere in the sheet at all, this isn't
    a repeating-blocks sheet this heuristic was designed for (e.g. a
    single-consistent-table sheet, PROMPT shape (a)), and guessing a
    block boundary anyway risks running to the end of the sheet and
    counting trailing boilerplate/disclaimer rows as if they were this
    building's own data.

    Locates the building's own heading line by its first non-blank cell
    (see _heading_cell) STARTING WITH the building name - never a plain
    substring-anywhere-on-the-line search, which a real file this was
    confirmed against showed landing inside a completely different,
    unrelated building's own prose description paragraph that happened to
    mention this building's name in passing. When more than one line
    qualifies (e.g. this building's name is itself a prefix of another,
    different building's longer name, so that OTHER building's own
    heading also "starts with" this one), the SHORTEST qualifying cell
    wins - the real heading for this exact building, unlike a different
    building's heading that merely shares its name as a prefix, has
    nothing extra appended.

    The block's end is the next "download" line after this building's
    OWN one (never the next building's heading line, which - confirmed
    against a real file - can run unbounded straight past an intervening
    building that was itself "Fully Occupied" and so never produced any
    surviving unit to anchor a boundary on, silently absorbing that
    unrelated building's own mini-table header row as if it were an
    extra data row for THIS building). Falls back to the end of the text
    if this building has no "download" line of its own, or none follow it.

    Within that block, counts lines with at least 3 pipe-separated cells
    - real column count varies row to row (render_sheet_as_text trims
    trailing blank cells per row, so a data row missing its last column
    or two, e.g. no stated Commission, has fewer cells than one where
    every column is filled; 3 is a low floor that still comfortably
    excludes a single-cell prose paragraph, never a requirement to match
    the table's own header width exactly) - other than the address/link
    line itself (identified by containing "download"). The FIRST such
    line is treated as the mini-table's own header row (never itself a
    unit); every one after it, up to the block's end, counts as one
    apparent data row.

    Returns 0 if the building's own heading line can't be found at all -
    "no evidence of undercounting" is the safe default when this cheap
    heuristic can't even locate the block, not a false alarm.
    """
    lines = [_ROW_PREFIX_RE.sub("", line) for line in raw_text.splitlines()]
    if not any("download" in line.lower() for line in lines):
        return 0

    building_key = str(building).strip().lower()
    candidates = [i for i, line in enumerate(lines) if _heading_cell(line).lower().startswith(building_key)]
    if not candidates:
        return 0
    start = min(candidates, key=lambda i: (len(_heading_cell(lines[i])), i))

    own_download = next((i for i in range(start + 1, len(lines)) if "download" in lines[i].lower()), None)
    if own_download is None:
        end = len(lines)
    else:
        next_download = next(
            (i for i in range(own_download + 1, len(lines)) if "download" in lines[i].lower()), None
        )
        end = next_download if next_download is not None else len(lines)

    header_seen = False
    data_rows = 0
    for line in lines[start + 1:end]:
        if "download" in line.lower():
            continue
        if len(line.split("|")) < 3:
            continue
        if not header_seen:
            header_seen = True
            continue
        data_rows += 1
    return data_rows


def sheet_shows_fully_occupied_building(raw_text: str) -> bool:
    """
    True if the raw sheet text contains at least one "Fully Occupied"
    marker row (see PROMPT shape (b): "If a data row's first cell just
    says 'Fully Occupied' ... that building currently has NO available
    units - do not emit a unit for it"). Distinguishes a sheet where
    extract_sheet legitimately, correctly returned zero units because a
    recognized building simply has nothing available right now, from one
    where no listing data was recognized on the sheet at all (e.g. a
    portfolio-wide index page) - used only to choose the right
    skipped-sheet message wording in app.py.
    """
    for line in raw_text.splitlines():
        if _heading_cell(_ROW_PREFIX_RE.sub("", line)).lower() == "fully occupied":
            return True
    return False


def find_undercounted_buildings(raw_text: str, units: list[dict]) -> list[tuple]:
    """
    Cross-checks every DISTINCT building among `units` (each a dict with
    at least a "building" key - the shape extract_sheet's own raw units
    have before becoming ListingRows, and what app.py adapts its final
    rows into) against _apparent_data_row_count - returns [(building,
    apparent_count, actual_count), ...] for every building where the raw
    text appears to have MORE rows than were actually extracted for it.
    Never the reverse: genuinely fewer real rows than this heuristic's
    own rough count is common (a building with just 1 floor has no
    "extra" rows to miscount) and is not itself suspicious.

    A building entirely missing from `units` (every one of its rows
    dropped, not just some) can't be caught here at all - there's no
    surviving unit to anchor the block search on. This only ever reduces
    manual checking for a PARTIAL loss, never replaces reading the sheet
    for a complete one - callers must always pass every unit actually
    extracted from the WHOLE sheet at once, never a single building's
    units in isolation.
    """
    buildings = []
    seen = set()
    for u in units:
        b = u.get("building")
        if b and b not in seen:
            seen.add(b)
            buildings.append(b)

    mismatches = []
    for building in buildings:
        actual = sum(1 for u in units if u.get("building") == building)
        apparent = _apparent_data_row_count(raw_text, building)
        if apparent > actual:
            mismatches.append((building, apparent, actual))
    return mismatches
