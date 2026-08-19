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
from datetime import date

from brochure_link_resolver import finalize_brochure_link, finalize_floorplan_link
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
    units - do not emit a unit for it, and add its name to fully_occupied_buildings instead (see
    below).

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
- fully_occupied_buildings: the exact name (as you'd give it in a unit's own "building" field) of
  every building on THIS sheet whose own mini-table states it has zero current availability (shape
  (b)'s "Fully Occupied" marker row - see below) rather than real unit rows. Do not include a
  building just because it happens to have few units right now - only one whose own block
  explicitly states this. Empty list if none, or if this sheet is shape (a).

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
- size_sqft: the square footage figure for that row. There is only one size_sqft field (no min/max
  pair) - if the source states a range (e.g. "5,515 - 7,282"), use the UPPER end of the range
  (7282), the same convention desks_max already uses for a plain desk range below. Never leave this
  null merely because a range was given instead of one plain number.
- desks_min / desks_max: an explicit desk count if one is stated as a number (a plain number is
  desks_max; a range like "10-15" is desks_min=10, desks_max=15). A row combining more than one
  sub-unit's own desk count (e.g. "52 + 5 MR (Unit A) & 68 + 5 MR (Unit B)") is also a range for
  this purpose: use the smallest and largest of the sub-units' own MAIN desk counts (52 and 68
  here) as desks_min/desks_max, ignoring a separately-labeled meeting-room/ancillary add-on (the
  "+5 MR" here) - never leave both null merely because the count wasn't a single plain "X-15"-style
  range. Otherwise null - never guess one from square footage.
- rent_psf: a per-square-foot rate column/value if present. Same range rule as size_sqft above if a
  range is stated - use the upper end, never leave this null merely because a range was given.
- rent_pcm: a monthly rent total column/value if present. Same range rule as size_sqft above if a
  range is stated - use the upper end, never leave this null merely because a range was given.
- special_features: the unit's own description/features text, plus a commission/incentive column's
  text appended after a semicolon if one is given for that row.
- state_of_space: the physical fit-out state ONLY if the text EXPLICITLY states one (e.g. "Fitted",
  "CAT A", "To be fitted out") - never infer or guess this from a description that merely sounds
  furnished/equipped; leave null whenever the source doesn't use an explicit fit-out term.
- brochure_link: the URL of the link specifically labeled "Brochure" (e.g. "Download Brochure
  (https://...)") for that specific row/building, if one is given (see the hyperlink note above).
  A building's own address/link line in shape (b) often carries TWO separate links side by side -
  e.g. "... | Download Floorplans (https://a) | | | Download Brochure (https://b)" - these are two
  DIFFERENT assets, not two labels for one link: brochure_link is specifically the "Brochure"-labeled
  URL, never the "Floorplans" one as a substitute, even when "Brochure" isn't present at all for that
  building. If no link labeled "Brochure" is given anywhere for that row/building, brochure_link is
  null - do not fall back to a "Floorplans" (or any other differently-labeled) link.
- floorplan_link: the URL of the link specifically labeled "Floorplan"/"Floorplans"/"Floor Plan" (e.g.
  "Download Floorplans (https://...)") for that specific row/building, if one is given - the exact
  link brochure_link above must NEVER use as a substitute. Null if no such link is given.

Return your answer as a single JSON object with this exact structure:

{
  "provider": "..." or null,
  "contacts": "..." or null,
  "fully_occupied_buildings": ["...", ...],
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
      "brochure_link": "..." or null,
      "floorplan_link": "..." or null
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


def _brochure_url_from_cell_text(cell_text: str):
    """The URL inlined (see render_sheet_as_text) in `cell_text` if that cell's
    OWN display text mentions "brochure" - i.e. this is a real Excel hyperlink
    behind a "Download Brochure"-labeled cell, not just the word "brochure"
    appearing somewhere in unrelated prose with no link at all. Returns None
    for a "Download Floorplans" cell even when it sits right next to a
    Brochure cell on the exact same rendered line - checked against this ONE
    cell's own text only, never the whole line, which is what makes this safe
    to use even when a line has more than one inlined hyperlink."""
    if "brochure" not in cell_text.lower():
        return None
    match = re.search(r"\((https?://[^)]+)\)\s*$", cell_text)
    return match.group(1) if match else None


def _brochure_urls_in_block(lines: list, start: int, end: int) -> list:
    """Every DISTINCT URL found via _brochure_url_from_cell_text across every
    cell in lines[start:end] (see _building_block_bounds - start is the
    heading line itself, included since it's never the source of a
    "download" cell in practice but costs nothing to also check), in the
    order first seen."""
    urls = []
    for line in lines[start:end]:
        for cell_text in line.split("|"):
            url = _brochure_url_from_cell_text(cell_text.strip())
            if url and url not in urls:
                urls.append(url)
    return urls


def deterministic_brochure_link_for_building(raw_text: str, building: str):
    """
    The single, unambiguous brochure URL for `building`'s own block in the
    raw sheet text (see _building_block_bounds) - read straight from the
    real Excel hyperlink behind that building's own "Download Brochure"
    cell (render_sheet_as_text already inlines a cell's real hyperlink
    target after its display text), bypassing Gemini's own free-form
    reading of that same rendered text entirely. Confirmed against the real
    Copthall Estates Availability.xlsx: every building's address/link row
    carries the Floorplans and Brochure links as two separate cells with
    two distinct real URLs, so the source of truth for which is which is
    the cell's own hyperlink object, not any judgment call about wording.

    Returns None - deliberately never a guess - whenever this building's
    own block can't be found at all, has no Brochure-labeled hyperlink in
    it, or has more than one DIFFERENT such link (a real conflict, not
    resolvable from the block alone). A caller finding None here has learned
    nothing about whether Gemini's own value (if any) is right or wrong -
    it just means this specific safety net can't independently confirm it
    either way, so Gemini's own extraction is left as the best available
    answer.
    """
    lines = [_ROW_PREFIX_RE.sub("", line) for line in raw_text.splitlines()]
    bounds = _building_block_bounds(lines, building)
    if bounds is None:
        return None
    start, end = bounds
    urls = _brochure_urls_in_block(lines, start, end)
    return urls[0] if len(urls) == 1 else None


def _apply_deterministic_brochure_links(units: list[dict], raw_text: str) -> None:
    """
    Overrides each unit's own brochure_link in place with its building's
    deterministic value (see deterministic_brochure_link_for_building)
    whenever one can be confidently identified - this takes priority over
    whatever Gemini itself read out of the free-form text, since the real
    target URL is already known with certainty straight from the source
    cell, for BOTH directions: recovers a brochure_link Gemini dropped
    entirely, and corrects one Gemini picked wrong (e.g. the neighboring
    Floorplans link) - a real conflict between the two never happens here,
    since a confident deterministic value always wins outright rather than
    being weighed against Gemini's own answer.

    Leaves a unit's brochure_link completely untouched - whatever Gemini
    itself returned, blank or not - whenever its building's own block has
    no such link at all, or has more than one conflicting one: Gemini's own
    read is the best available signal in that case, not something to
    second-guess further with a guess of our own. Never invents a link for
    a building with no real Brochure hyperlink in the source.

    One deterministic_brochure_link_for_building call per DISTINCT building
    (cached in `cache`), not per unit - several units legitimately share one
    building block (its own several available floors), and each one gets
    that building's own single answer.
    """
    cache = {}
    for unit in units:
        building = unit.get("building")
        if not building:
            continue
        if building not in cache:
            cache[building] = deterministic_brochure_link_for_building(raw_text, building)
        deterministic_url = cache[building]
        if deterministic_url:
            unit["brochure_link"] = deterministic_url


def _floorplan_url_from_cell_text(cell_text: str):
    """Mirrors _brochure_url_from_cell_text, but for a cell whose own
    display text mentions "floorplan"/"floor plan" (either spelling seen in
    real files - "Download Floorplans" as one word) instead of "brochure" -
    see that function's own docstring for the identical rationale.

    Returns None when the SAME cell's text also mentions "brochure" (e.g.
    "Download Brochure and Floorplans") - that single link is one combined
    document, which stays classified as a brochure only (see brochure_link_
    resolver.is_floorplan_not_brochure_url for the identical reasoning
    applied to a URL's own text elsewhere in this repo); without this, the
    exact same URL would otherwise be written into BOTH brochure_link and
    floorplan_link from one cell, an unnecessary duplication a combined
    document's own single link never actually needs."""
    lowered = cell_text.lower()
    if "brochure" in lowered:
        return None
    if "floorplan" not in lowered and "floor plan" not in lowered:
        return None
    match = re.search(r"\((https?://[^)]+)\)\s*$", cell_text)
    return match.group(1) if match else None


def _floorplan_urls_in_block(lines: list, start: int, end: int) -> list:
    """Mirrors _brochure_urls_in_block, for floorplan-labeled cells."""
    urls = []
    for line in lines[start:end]:
        for cell_text in line.split("|"):
            url = _floorplan_url_from_cell_text(cell_text.strip())
            if url and url not in urls:
                urls.append(url)
    return urls


def deterministic_floorplan_link_for_building(raw_text: str, building: str):
    """Mirrors deterministic_brochure_link_for_building, reading `building`'s
    own Floorplan-labeled hyperlink instead of its Brochure one - same
    "never a guess" behavior: None whenever the block can't be found, has no
    Floorplan-labeled hyperlink, or has more than one conflicting one."""
    lines = [_ROW_PREFIX_RE.sub("", line) for line in raw_text.splitlines()]
    bounds = _building_block_bounds(lines, building)
    if bounds is None:
        return None
    start, end = bounds
    urls = _floorplan_urls_in_block(lines, start, end)
    return urls[0] if len(urls) == 1 else None


def _apply_deterministic_floorplan_links(units: list[dict], raw_text: str) -> None:
    """Mirrors _apply_deterministic_brochure_links, for floorplan_link - see
    that function's own docstring for the full rationale (identical, just a
    different field/label pair)."""
    cache = {}
    for unit in units:
        building = unit.get("building")
        if not building:
            continue
        if building not in cache:
            cache[building] = deterministic_floorplan_link_for_building(raw_text, building)
        deterministic_url = cache[building]
        if deterministic_url:
            unit["floorplan_link"] = deterministic_url


def extract_sheet(ws, sheet_label: str, filename: str) -> list[ListingRow]:
    """Thin wrapper over extract_sheet_with_metadata for every caller that
    only needs the rows themselves (the overwhelming majority - see that
    function's own docstring for why fully_occupied_buildings needs a
    second return value at all)."""
    rows, _fully_occupied_buildings = extract_sheet_with_metadata(ws, sheet_label, filename)
    return rows


def extract_sheet_with_metadata(ws, sheet_label: str, filename: str) -> tuple:
    """
    Same extraction as extract_sheet, returning (rows, fully_occupied_
    buildings) - the second element is a list of {"provider", "building"}
    dicts (provider exactly as this sheet's own raw Gemini response gave it,
    unnormalized - callers needing the canonical spelling apply master_merge.
    canonicalize_provider_name themselves, same as every other raw provider
    value) naming each building this sheet's own text explicitly states has
    zero current availability (see PROMPT's own fully_occupied_buildings
    field). Unlike find_undercounted_buildings/find_buildings_missing_
    brochure_link, this genuinely can't be cheaply re-derived later from a
    fresh render_sheet_as_text call the way those are (see e.g. app.py's
    _warn_if_units_look_undercounted) - a fully-occupied building produces no
    surviving unit at all, so there's no building name in the final rows to
    anchor a later, pure-code re-check on; recognizing "Fully Occupied"
    belongs to a specific building heading (as opposed to just noticing the
    marker exists somewhere on the sheet, see sheet_shows_fully_occupied_
    building) is exactly the free-form structural judgment call Gemini is
    already making to correctly skip emitting a unit for it, so this simply
    asks it to also name what it recognized. provider is bundled in here
    (rather than left for the caller to infer from this same sheet's other
    rows) because a sheet whose ENTIRE content is one fully-occupied
    building has no surviving row at all to read a provider off of.

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
        return [], []
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

    # Deterministic Excel-hyperlink override takes priority over Gemini's own
    # free-form reading of the same rendered text - see _apply_deterministic_
    # brochure_links's own docstring. Must run before finalize_brochure_link
    # below so a recovered/corrected link still goes through the same admin-
    # link/generic-link/landing-page handling every other brochure_link does.
    _apply_deterministic_brochure_links(units, text)
    _apply_deterministic_floorplan_links(units, text)

    rows = []
    for unit in units:
        unit["brochure_link"] = finalize_brochure_link(
            unit.get("brochure_link"), is_pdf=False, pdf_fallback_link=filename
        )
        unit["floorplan_link"] = finalize_floorplan_link(unit.get("floorplan_link"))

        # No genuine brochure, but a real floor plan exists - shown as
        # brochure_link too (see ListingRow.brochure_link_is_floorplan's
        # own docstring) rather than silently hidden behind floorplan_link,
        # which stays a hidden column by default. floorplan_link itself is
        # untouched either way - this only ever ADDS a value to brochure_
        # link, never replaces a genuine one (that case never reaches here:
        # brochure_link is already non-blank).
        brochure_link_is_floorplan = None
        if not unit["brochure_link"] and unit["floorplan_link"]:
            unit["brochure_link"] = unit["floorplan_link"]
            brochure_link_is_floorplan = True

        fields = ExtractedFields(**brochure, **unit).model_dump()
        fields = compute_rent(fields)
        rows.append(
            ListingRow(
                **fields,
                lat=None,
                lng=None,
                source_file=sheet_label,
                brochure_link_is_floorplan=brochure_link_is_floorplan,
            )
        )

    fully_occupied_buildings = [
        {"provider": raw.get("provider"), "building": b}
        for b in (raw.get("fully_occupied_buildings", []) or [])
        if b
    ]
    return rows, fully_occupied_buildings


def _heading_cell(line: str) -> str:
    """The first non-blank pipe-separated cell on a rendered line, stripped -
    a repeating-block sheet's heading/address/data lines all carry an entirely
    blank leading column on the real files this was confirmed against, so the
    building name itself lives in the first cell AFTER that blank one, never
    literally at the start of the line."""
    return next((c.strip() for c in line.split("|") if c.strip()), "")


def _building_block_bounds(lines: list, building: str):
    """
    (start, end) bounding a building's own block among already-row-prefix-
    stripped `lines` (see _apparent_data_row_count/find_undercounted_
    buildings and find_buildings_missing_brochure_link, which both need the
    exact same block first) - lines[start] is the building's own heading
    line itself (never part of the block's own content), lines[start+1:end]
    is everything belonging to it. Returns None if the raw text doesn't look
    like the repeating-blocks shape at all (no "download" line anywhere -
    see PROMPT shape (b)'s own address/link line note; e.g. a single-
    consistent-table sheet, PROMPT shape (a), guessing a block boundary
    for which risks running to the end of the sheet and mistaking trailing
    boilerplate/disclaimer rows for this building's own data), or if this
    building's own heading line can't be found at all.

    Locates the heading line by its first non-blank cell (see _heading_cell)
    STARTING WITH the building name - never a plain substring-anywhere-on-
    the-line search, which a real file this was confirmed against showed
    landing inside a completely different, unrelated building's own prose
    description paragraph that happened to mention this building's name in
    passing. When more than one line qualifies (e.g. this building's name
    is itself a prefix of another, different building's longer name, so
    that OTHER building's own heading also "starts with" this one), the
    SHORTEST qualifying cell wins - the real heading for this exact
    building, unlike a different building's heading that merely shares its
    name as a prefix, has nothing extra appended.

    The block's end is the next "download" line after this building's OWN
    one (never the next building's heading line, which - confirmed against
    a real file - can run unbounded straight past an intervening building
    that was itself "Fully Occupied" and so never produced any surviving
    unit to anchor a boundary on, silently absorbing that unrelated
    building's own mini-table header row as if it were an extra data row
    for THIS building). Falls back to the end of the text if this building
    has no "download" line of its own, or none follow it.
    """
    if not any("download" in line.lower() for line in lines):
        return None

    building_key = str(building).strip().lower()
    candidates = [i for i, line in enumerate(lines) if _heading_cell(line).lower().startswith(building_key)]
    if not candidates:
        return None
    start = min(candidates, key=lambda i: (len(_heading_cell(lines[i])), i))

    own_download = next((i for i in range(start + 1, len(lines)) if "download" in lines[i].lower()), None)
    if own_download is None:
        end = len(lines)
    else:
        next_download = next(
            (i for i in range(own_download + 1, len(lines)) if "download" in lines[i].lower()), None
        )
        end = next_download if next_download is not None else len(lines)
    return start, end


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

    Within its own block (see _building_block_bounds), counts lines with
    at least 3 pipe-separated cells - real column count varies row to row
    (render_sheet_as_text trims trailing blank cells per row, so a data
    row missing its last column or two, e.g. no stated Commission, has
    fewer cells than one where every column is filled; 3 is a low floor
    that still comfortably excludes a single-cell prose paragraph, never a
    requirement to match the table's own header width exactly) - other
    than the address/link line itself (identified by containing
    "download"). The FIRST such line is treated as the mini-table's own
    header row (never itself a unit); every one after it, up to the
    block's end, counts as one apparent data row.

    Returns 0 if the building's own block can't be found at all - "no
    evidence of undercounting" is the safe default when this cheap
    heuristic can't even locate the block, not a false alarm.
    """
    lines = [_ROW_PREFIX_RE.sub("", line) for line in raw_text.splitlines()]
    bounds = _building_block_bounds(lines, building)
    if bounds is None:
        return 0
    start, end = bounds

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


def is_non_authoritative_rollup_sheet(ws, raw_text: str) -> bool:
    """
    True when a Gemini-fallback-eligible sheet is confirmed non-authoritative
    - a stale/legacy rollup rather than a genuine source of current per-
    building availability - and should be skipped from extraction entirely,
    rather than risk producing duplicate/stale rows alongside its sibling
    sheets' own genuinely current data.

    Confirmed against the real Copthall Estates Availability.xlsx: its
    "Portfolio" sheet (a single flat Area/Station/Building/Office/... table,
    titled "Copthall Estates Availibility - Updated 05/08/25" - about a year
    stale compared to its sibling sheets' own "Updated 03/08/2026") is a
    hidden sheet in the actual workbook, while every genuinely current sheet
    (City, Mid Town, Westend Soho, Blackfriars) is visible - an explicit,
    deliberate signal from the workbook's own author that this specific
    sheet isn't meant to be seen or used, not a guess derived from its name
    or content alone.

    Requires BOTH of:
    - ws.sheet_state is "hidden" or "veryHidden" (openpyxl reads this
      straight from the workbook's own <sheet state="..."> attribute - the
      same thing Excel itself uses to decide whether to show a tab);
    - raw_text has NO "download" line anywhere at all (see
      _building_block_bounds) - i.e. this sheet could never contain even ONE
      genuine shape (b) building block, since a repeating per-building block
      always carries its own hyperlink-bearing Floorplans/Brochure address
      line (see PROMPT).

    Deliberately requires BOTH, never either alone:
    - hidden alone would risk skipping a hidden HELPER/lookup sheet that
      happens to also carry real per-building hyperlinked blocks (unlikely,
      but not something to assume away) - "skip every hidden sheet" is
      exactly the global rule this must never become;
    - "no download line" alone would risk skipping a genuinely current, but
      hyperlink-less, free-form sheet from some other provider - the risk
      already documented on find_buildings_missing_brochure_link's own
      "not every provider supplies one" note.
    Only the intersection - deliberately tucked away by the workbook's own
    author AND structurally incapable of holding genuine per-building
    availability data - is what's actually confirmed safe to skip.
    """
    if ws.sheet_state not in ("hidden", "veryHidden"):
        return False
    return not any("download" in line.lower() for line in raw_text.splitlines())


# Sheet names known, in general, to describe a rollup/index/summary rather
# than a building's own current listings - a small, explicit, hand-
# maintained list (same "conservative, human catches it" philosophy as
# master_merge.KNOWN_PROVIDERS/COMPLETE_SNAPSHOT_PROVIDERS), matched only
# against the sheet's OWN full name (case-insensitive, trimmed), never a
# substring-anywhere check. Deliberately never used alone to auto-skip - see
# classify_sheet_for_extraction's own docstring: a sheet literally named
# "Portfolio" is common enough, and legitimate enough in principle, that
# this is only ever ONE contributing signal toward "ambiguous", not a
# decision by itself.
SUSPICIOUS_SHEET_NAMES = ("portfolio", "summary", "archive", "index", "overview")

# How many days older than the newest sibling sheet's own "Updated" date
# (see extract_update_date) counts as "significantly older" for the
# staleness signal below - deliberately generous (roughly 4 months): the
# real Copthall sibling sheets' own dates differ by a few weeks against each
# other (a normal, unremarkable per-area update cadence), while the real
# stale Portfolio sheet is about a YEAR behind them. This threshold is
# comfortably below that real gap while comfortably above the real sibling-
# to-sibling variation, so it doesn't fire on ordinary staggered updates.
STALE_UPDATE_DATE_THRESHOLD_DAYS = 120

_UPDATED_DATE_RE = re.compile(r"updated\s+(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", re.IGNORECASE)


def extract_update_date(raw_text: str):
    """
    The date parsed from an "Updated DD/MM/YY(YY)" phrase in this sheet's
    own title text, if one is present - checked only against the first few
    lines (a title line is always at or near the top of a real sheet, per
    every real example confirmed so far: "Copthall Estates City
    Availability - Updated 03/08/2026", "Copthall Estates Availibility -
    Updated 05/08/25") - or None if no such phrase is found there, or if one
    is found but isn't a real calendar date (never raises). A 2-digit year
    is read as 20XX - every real example seen uses either a 4-digit year or
    a 2-digit one clearly meaning 20XX, never a genuinely ambiguous century.
    """
    for line in raw_text.splitlines()[:5]:
        match = _UPDATED_DATE_RE.search(line)
        if not match:
            continue
        day, month, year = (int(g) for g in match.groups())
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None


def classify_sheet_for_extraction(ws, raw_text: str, sibling_dates: dict = None) -> dict:
    """
    Classifies a Gemini-fallback-eligible sheet (one header-mapping has
    already failed to resolve - see app.py) into exactly one of three
    outcomes - never a numeric score, and never decided from a single
    signal alone:

    - "auto_skip": confidently non-authoritative - is_non_authoritative_
      rollup_sheet's own two-signal intersection (hidden/veryHidden AND no
      real hyperlink/"download" line anywhere), UNCHANGED by this function -
      still the only rule strong enough to decide without asking a human.
    - "ambiguous": at least one independent, meaningful piece of evidence
      this sheet MIGHT be non-authoritative, but not the confident
      intersection above - a human must decide (see app.py's upload-time
      decision UI). The individual signals, NONE of which trigger this
      alone (see reasons below and each helper's own docstring):
        - hidden/veryHidden, even though real hyperlinked blocks ARE
          present (a hidden sheet is always at least worth asking about -
          why would a genuinely current listings sheet be hidden? - even
          when it isn't confidently skippable on structure alone);
        - its own name is one of SUSPICIOUS_SHEET_NAMES;
        - its own "Updated" date (see extract_update_date) is
          STALE_UPDATE_DATE_THRESHOLD_DAYS+ older than the newest sibling
          sheet's own date in the same upload (sibling_dates) - never
          guessed from a single data point, so this never fires without at
          least one comparably-dated sibling.
      A flat data table with no "download"/brochure line by itself is
      deliberately NOT one of these signals (removed after a real confirmed
      false positive: beem Live Flex Availability.xlsx's own Sheet1 - a
      completely ordinary, current, flat per-unit availability table that
      simply never had a brochure/download column at all) - a provider is
      allowed to have a current availability spreadsheet with no brochures,
      and the mere shape of a flat table carries no staleness signal on its
      own; only the stronger, more specific signals above do.
    - "authoritative": none of the above - process automatically, exactly
      as every sheet did before this function existed.

    sibling_dates: {other_sheet_name: date} for every OTHER sheet in the
    SAME uploaded file that also needed classifying - omit or pass {} when
    there are none yet; that signal then simply never fires.

    Returns {"outcome": "auto_skip"|"ambiguous"|"authoritative",
    "reasons": [...], "sheet_state": ws.sheet_state} - reasons is always []
    for "authoritative", a single confirmed reason for "auto_skip", and one
    entry per contributing signal for "ambiguous" (a reviewer-facing
    explanation, not machine-parsed anywhere).
    """
    if is_non_authoritative_rollup_sheet(ws, raw_text):
        return {
            "outcome": "auto_skip",
            "reasons": ["hidden_with_no_download_lines"],
            "sheet_state": ws.sheet_state,
        }

    hidden = ws.sheet_state in ("hidden", "veryHidden")
    name_key = str(ws.title or "").strip().lower()

    reasons = []
    if hidden:
        reasons.append("hidden")
    if name_key in SUSPICIOUS_SHEET_NAMES:
        reasons.append("suspicious_sheet_name")

    own_date = extract_update_date(raw_text)
    if own_date and sibling_dates:
        newest_sibling_date = max(sibling_dates.values())
        if (newest_sibling_date - own_date).days > STALE_UPDATE_DATE_THRESHOLD_DAYS:
            reasons.append("stale_update_date_vs_siblings")

    if reasons:
        return {"outcome": "ambiguous", "reasons": reasons, "sheet_state": ws.sheet_state}
    return {"outcome": "authoritative", "reasons": [], "sheet_state": ws.sheet_state}


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


_BROCHURE_HYPERLINK_RE = re.compile(r"brochure.*\(https?://", re.IGNORECASE)


def find_buildings_missing_brochure_link(raw_text: str, units: list[dict]) -> list[str]:
    """
    Structural safety net matching find_undercounted_buildings' own
    approach (reuses the exact same block-boundary logic, see
    _building_block_bounds), for a different real, confirmed failure mode:
    every building checked in the real Copthall Estates Availability file
    (across its City, Mid Town, Westend Soho, and Blackfriars sheets) has a
    real, working "Download Brochure" hyperlink in the source - see
    PROMPT's own brochure_link field note on picking that one over a
    separately-labeled "Download Floorplans" link when a row/block has
    both. This catches whichever of the two Gemini picked wrong (or
    dropped entirely) at the structural level, without re-deciding which
    URL is correct itself.

    Cross-checks every DISTINCT building among `units` (each a dict with
    at least "building" and "brochure_link" keys) - returns the building
    names whose own block in the raw text contains a "Brochure"-labeled
    hyperlink (see _BROCHURE_HYPERLINK_RE - a real inlined hyperlink
    target, per render_sheet_as_text, not just the bare word "brochure"
    appearing in running prose with no link at all) but where NONE of that
    building's own extracted units carry a GENUINE brochure_link - a unit
    whose brochure_link is only present via the brochure_link_is_floorplan
    fallback (see that field's own schema.py docstring; passed here via an
    optional "brochure_link_is_floorplan" key, defaulting to falsy for a
    caller that never sets it) never counts as "extracted" for this check.
    A real, genuine "Download Brochure" link was still missed structurally
    even though brochure_link ends up non-blank - showing a floor plan
    document is strictly better than a blank cell, but it must never
    silently mask the exact production failure mode this check exists to
    catch (Gemini picking the wrong link, or dropping the right one,
    between two candidates in the same block).

    Never the reverse: a building with no "Brochure"-labeled link in the
    source at all is not itself suspicious - not every provider supplies
    one, and this never invents a requirement that one exist.
    """
    lines = [_ROW_PREFIX_RE.sub("", line) for line in raw_text.splitlines()]
    buildings = []
    seen = set()
    for u in units:
        b = u.get("building")
        if b and b not in seen:
            seen.add(b)
            buildings.append(b)

    missing = []
    for building in buildings:
        bounds = _building_block_bounds(lines, building)
        if bounds is None:
            continue
        start, end = bounds
        has_brochure_link_in_source = any(_BROCHURE_HYPERLINK_RE.search(line) for line in lines[start:end])
        if not has_brochure_link_in_source:
            continue
        any_extracted = any(
            u.get("brochure_link") and not u.get("brochure_link_is_floorplan")
            for u in units if u.get("building") == building
        )
        if not any_extracted:
            missing.append(building)
    return missing
