import gc
import json
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF
from google.genai import types
from pydantic import ValidationError

from brochure_link_resolver import finalize_brochure_link
from gemini_client import call_gemini, compute_rent, get_client
from schema import ExtractedFields, ListingRow

RENDER_DPI = 72

# --- Per-row PDF hyperlink extraction ---
#
# Some tabular/schedule-style PDFs (e.g. a per-floor availability table with
# its own "Link to Brochure" column, rendered as a small "Here"/"View" link
# per row) embed a genuine, distinct hyperlink per unit as a PDF link
# annotation - invisible to Gemini's vision-based extraction (see extract(),
# which only ever shows Gemini rendered page IMAGES, never the underlying
# text/link layer), so without this step every such unit falls through to
# finalize_brochure_link's rule 3 (the whole uploaded PDF as a fallback
# link), losing the real per-row destination entirely.
#
# The join problem this solves: Gemini's returned units carry no page/
# position back-reference on their own, so a link found via PyMuPDF (tied
# to a page + page-coordinate rect) has nothing to attach to. PAGE_INDEX_KEY
# (added to the extraction prompt/schema below) gives each unit a page to
# search on; that unit's own floor_unit/size_sqft values (already extracted
# for every unit regardless of this feature) are then located as literal
# text on that page via PyMuPDF's word-level bounding boxes, giving each
# unit an approximate row position to compare against each candidate link's
# own rect position.
#
# Deliberately conservative in three independent ways, any one of which
# leaves every unit on a page completely untouched (falling through to the
# existing rule-3 PDF fallback exactly as before this feature existed):
# a page needs at least MIN_UNITS_FOR_PER_ROW_LINKS Gemini units placed on
# it (page_index), at least MIN_LINKS_FOR_PER_ROW_LINKS distinct small
# (LINK_CAPTION_MAX_HEIGHT-or-under) URI link annotations on it, and each
# individual unit needs its row locatable on the page AND exactly one
# candidate link near that row (never more than one - see
# _attach_per_row_pdf_links) before a link is ever attached to it.
PAGE_INDEX_KEY = "page_index"
MIN_UNITS_FOR_PER_ROW_LINKS = 2
MIN_LINKS_FOR_PER_ROW_LINKS = 2
LINK_CAPTION_MAX_HEIGHT = 20  # points - typical caption text is ~8-12pt; a floorplan/logo/photo link runs much taller
ROW_Y_TOLERANCE = 12  # points - how close a link's and a unit's row y-center must be to count as "the same row"


def _page_uri_links(page) -> list:
    """
    Every genuine external-URL link annotation on `page`, as {"uri", "rect"}
    - filtered to fitz.LINK_URI links with a real uri only. PDFs also embed
    internal navigation links (LINK_GOTO, kind 4 - "next page"/"back to
    contents" arrows, uri=None) which have nothing to attach to a unit's
    brochure_link and would otherwise inflate the link count on nearly
    every page of a real multi-page brochure (confirmed against
    city-tower-brochure.pdf: page-turn arrows appear as 3-4 LINK_GOTO
    annotations on almost every one of its 45 pages).
    """
    links = []
    for link in page.get_links():
        if link.get("kind") != fitz.LINK_URI:
            continue
        uri = link.get("uri")
        if not uri:
            continue
        rect = fitz.Rect(link["from"])
        links.append({"uri": uri, "rect": rect, "y_center": (rect.y0 + rect.y1) / 2})
    return links


def _is_caption_sized(rect) -> bool:
    return rect.height <= LINK_CAPTION_MAX_HEIGHT


def _tokenize(text) -> list:
    return [t for t in re.findall(r"[a-z0-9]+", str(text).lower()) if len(t) >= 2]


# Generic words that recur across many different rows' floor_unit labels
# ("3rd Floor", "5th Floor", "Ground Floor", "Suite 12", "Suite 4C") and so
# carry no distinguishing power on their own - matching on one of these
# alone would confidently (and wrongly) "locate" a row via a word that's
# actually sitting in a completely different row's label on the same page.
_FLOOR_UNIT_STOPWORDS = {"floor", "floors", "suite", "unit", "office", "level", "the", "of", "and"}


def _find_unit_row_y(page_words: list, floor_unit, size_sqft):
    """
    Approximate y-center (page coordinates) of a unit's own row on the page
    `page_words` (PyMuPDF's page.get_text("words")) came from - located by
    searching for the unit's own floor_unit label and/or size_sqft value
    verbatim among the page's words, reusing values Gemini already extracts
    for every unit rather than requiring a brand-new dedicated field.

    size_sqft is preferred when it matches exactly once on the page - a
    specific number is far less likely to coincidentally repeat elsewhere
    on a busy page than a short, common floor-label word (e.g. "1st").
    Falls back to the median y-center of every word matching a token (2+
    chars, excluding _FLOOR_UNIT_STOPWORDS - "Floor"/"Suite"/etc. recur
    across every row's label and would confidently match the wrong row's
    text) of floor_unit. Returns None if neither anchor can be located at
    all - callers must treat that as "this unit can't be geometrically
    placed on this page", never guess a position.
    """
    if size_sqft:
        try:
            size_str = str(int(size_sqft)) if float(size_sqft).is_integer() else str(size_sqft)
        except (TypeError, ValueError):
            size_str = None
        if size_str:
            size_hits = [w for w in page_words if re.sub(r"[^\d.]", "", w[4]) == size_str]
            if len(size_hits) == 1:
                w = size_hits[0]
                return (w[1] + w[3]) / 2

    if floor_unit:
        tokens = set(_tokenize(floor_unit)) - _FLOOR_UNIT_STOPWORDS
        if tokens:
            y_hits = [(w[1] + w[3]) / 2 for w in page_words if _tokenize(w[4]) and _tokenize(w[4])[0] in tokens]
            if y_hits:
                y_hits.sort()
                return y_hits[len(y_hits) // 2]

    return None


def _brochure_column_x_range(page_words: list):
    """
    Approximate x-range (page coordinates) of a "Link to Brochure"-style
    column, located from the page's OWN header text - confirmed necessary
    against a real Kitt's-style availability table, where each row has
    THREE separate link columns (Link to Brochure, Floor Plan, High Res
    Images), all rendered as the same "Here" caption text at the exact
    same row y-position. Without this, a row's y-position alone can't
    tell those three apart - _attach_per_row_pdf_links would see 3
    "nearby" links and (correctly, given no other information) treat the
    row as ambiguous, attaching nothing at all.

    Finds the word "brochure" (case-insensitive, trailing punctuation
    stripped) sitting HIGHEST on the page (smallest y0) - the column
    header itself, not a coincidental mention of the word elsewhere (e.g.
    inside a prose special_features cell lower down the same page).
    Returns None if the page has no such word at all, meaning no column-
    based disambiguation is possible on this page.
    """
    candidates = [w for w in page_words if w[4].strip(".,:;").lower() == "brochure"]
    if not candidates:
        return None
    header_word = min(candidates, key=lambda w: w[1])
    # Widened left by 40pt to also cover "Link to" (the words immediately
    # before "Brochure" in a "Link to Brochure" header) sitting in the same
    # column, and slightly right too, so a link rect that doesn't align
    # pixel-perfectly with the header word's own width still counts.
    return (header_word[0] - 40, header_word[2] + 10)


def _floor_plan_column_x_range(page_words: list):
    """
    Mirrors _brochure_column_x_range, but locates a "Floorplan"/"Floor
    Plan" column header instead (e.g. the real Kitt's-style table's "Link
    to Floorplan" column - seen rendered as either one word or two,
    handled here as either shape) - used as a NEGATIVE signal in
    _attach_per_row_pdf_links: on a page with no separate Brochure column
    at all (_brochure_column_x_range returns None there), a row's only
    nearby link landing in THIS column's x-range is a floor plan, not a
    brochure - confirmed necessary since a floor plan is a genuinely
    different document from the brochure (see the Gemini extraction
    prompt's own brochure_link instructions), so it must be excluded from
    consideration entirely rather than attached for lack of anything
    else - leaving that unit to the existing PDF-wide fallback (or null)
    exactly as if no per-row link existed on the page at all.
    """
    single_word = [w for w in page_words if w[4].strip(".,:;").lower() == "floorplan"]
    if single_word:
        header_word = min(single_word, key=lambda w: w[1])
        return (header_word[0] - 40, header_word[2] + 10)

    # "Floor" immediately followed by "Plan" as two separate words on the
    # same row - PyMuPDF's own word-level text extraction always splits on
    # whitespace regardless of how the source PDF's text was originally
    # inserted/rendered, so "Floor Plan" (one header, two words) is at
    # least as common a real shape as the single-word "Floorplan".
    floor_words = [w for w in page_words if w[4].strip(".,:;").lower() == "floor"]
    plan_words = [w for w in page_words if w[4].strip(".,:;").lower() == "plan"]
    pairs = [
        (fw, pw) for fw in floor_words for pw in plan_words
        if abs((fw[1] + fw[3]) / 2 - (pw[1] + pw[3]) / 2) < 4 and 0 <= pw[0] - fw[2] < 20
    ]
    if not pairs:
        return None
    floor_word, plan_word = min(pairs, key=lambda pair: pair[0][1])
    return (floor_word[0] - 40, plan_word[2] + 10)


def _in_x_range(rect, x_range) -> bool:
    x_lo, x_hi = x_range
    return x_lo <= rect.x0 <= x_hi or x_lo <= rect.x1 <= x_hi


def _nearby_caption_text(page_words: list, rect, pad: float = 2.0) -> str:
    """
    Visible words overlapping `rect` (expanded by `pad` points) - the
    caption text a link sits on (e.g. "Here"). Used only for logging/
    debugging visibility into what _attach_per_row_pdf_links matched, never
    for the row-matching join itself - the link's own rect already IS its
    row position, independent of whatever text happens to be on it.
    """
    expanded = fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
    hits = [w[4] for w in page_words if fitz.Rect(w[:4]).intersects(expanded)]
    return " ".join(hits)


def _attach_per_row_pdf_links(pdf_path: Path, units: list) -> None:
    """
    Mutates each eligible unit's "brochure_link" in place with a genuine,
    per-row PDF-embedded hyperlink, when one can be confidently located -
    see the module-level comment above for the full design/conservatism
    rationale. Runs once per page that has enough corroborating units AND
    enough corroborating links (both gates independent - see module
    constants); does nothing at all to units on any page that doesn't
    clear both, or to a unit whose row can't be located, or whose row has
    zero or more than one candidate link nearby (ambiguous - left for the
    existing rule-3 PDF fallback rather than guessing).
    """
    units_by_page = {}
    for unit in units:
        page_index = unit.pop(PAGE_INDEX_KEY, None)
        if isinstance(page_index, int):
            units_by_page.setdefault(page_index, []).append(unit)

    if not units_by_page:
        return

    doc = fitz.open(pdf_path)
    try:
        for page_index, page_units in units_by_page.items():
            if len(page_units) < MIN_UNITS_FOR_PER_ROW_LINKS or not (0 <= page_index < doc.page_count):
                continue
            page = doc[page_index]
            links = [l for l in _page_uri_links(page) if _is_caption_sized(l["rect"])]
            if len(links) < MIN_LINKS_FOR_PER_ROW_LINKS:
                continue

            page_words = page.get_text("words")
            # When a row has several distinct link columns (e.g. a real
            # Kitt's-style table's Link to Brochure / Floor Plan / High Res
            # Images, all rendered as the same "Here" caption at the same
            # row y) - narrow to the Brochure column specifically BEFORE
            # counting candidates, rather than treating same-row links in
            # unrelated columns as ambiguous.
            brochure_x_range = _brochure_column_x_range(page_words)
            # No separate Brochure column on this page at all - if there's
            # a Floorplan column instead, its links are excluded from
            # consideration entirely (see _floor_plan_column_x_range) so a
            # row whose ONLY per-row link is that floor plan never gets it
            # attached as brochure_link. Skipped when brochure_x_range was
            # already found, since a genuinely separate Floorplan column
            # sitting alongside a Brochure column is already correctly
            # disambiguated by narrowing to the Brochure column instead.
            floor_plan_x_range = None if brochure_x_range is not None else _floor_plan_column_x_range(page_words)

            for unit in page_units:
                row_y = _find_unit_row_y(page_words, unit.get("floor_unit"), unit.get("size_sqft"))
                if row_y is None:
                    continue
                nearby = [l for l in links if abs(l["y_center"] - row_y) <= ROW_Y_TOLERANCE]
                if brochure_x_range is not None:
                    nearby = [l for l in nearby if _in_x_range(l["rect"], brochure_x_range)]
                elif floor_plan_x_range is not None:
                    nearby = [l for l in nearby if not _in_x_range(l["rect"], floor_plan_x_range)]
                if len(nearby) == 1:
                    unit["brochure_link"] = nearby[0]["uri"]
                    print(
                        f"[extract] page {page_index}: attached per-row link {nearby[0]['uri']!r} to unit "
                        f"{unit.get('floor_unit')!r} (caption text near link: "
                        f"{_nearby_caption_text(page_words, nearby[0]['rect'])!r})",
                        file=sys.stderr,
                    )
                elif len(nearby) > 1:
                    print(
                        f"[extract] page {page_index}: {len(nearby)} candidate links near unit "
                        f"{unit.get('floor_unit')!r}'s row — ambiguous, leaving for the PDF fallback.",
                        file=sys.stderr,
                    )
    finally:
        doc.close()


PROMPT = """You are extracting structured data from a commercial office property brochure.
You will be shown the pages of the brochure as images. Read all pages carefully,
including tables, floor plans, and photo captions.

Extract the following brochure-level information (these describe who is presenting this
brochure, not which property it's about — they apply to the whole document regardless of
how many properties or units it covers):
- provider: the company/agent presenting this brochure (e.g. "Breezblok", "GPE", "The Crown Estate Workplaces").
  Some brochures are produced directly by a landlord/developer with no presenting agent named anywhere —
  in that case leave this null rather than guessing or using the building/property name as a stand-in.
  Transcribe the name EXACTLY as it is printed wherever it appears as a name in running text (not a URL
  or email address, which never reflect the name's real spacing/punctuation/case) — like copying an exact
  quotation, not paraphrasing it. Preserve special characters and symbols precisely (e.g. a name printed
  as "Workplace+" stays "Workplace+", never expanded to "Workplace Plus"; a name printed as "Workplace
  Plus" stays "Workplace Plus", never condensed to "Workplace+"). Preserve the exact capitalization shown
  (e.g. don't write "WORKPLACE+" if the document prints "Workplace+", and don't write "Metspace" if the
  document prints "MetSpace"). Never substitute a spelling you recognize from general knowledge of the
  company's branding elsewhere — use only what this specific document actually shows.
- contacts: every contact person or generic contact listed in the document (e.g. "Sales" if no named
  person is given). Format each contact as "Name, email, phone" — omit any of the three pieces that
  aren't given. If there are multiple contacts, join them with "; ".

Then, identify EVERY SEPARATE AVAILABLE UNIT/SPACE described in the brochure. A brochure may describe
just one unit, many units within one building (e.g. a schedule of areas listing multiple floors), or
many units across entirely unrelated properties (different streets, different buildings). Treat each
distinct floor, suite, or unit as a separate entry.

For each unit, extract its own location fields — do not assume they're shared with other units:
- building: the name of the building this specific unit is in (e.g. "John Stow House", "City Tower").
  ALWAYS populate this for every unit, even when several consecutive units are in the same building and
  it feels redundant to repeat it — never leave building null.
- address_1: the street address of this specific unit's building (e.g. "18 Bevis Marks")
- postcode: the UK postcode of this specific unit's building (e.g. "EC3A 7JB")
- submarket: the general area/district for this specific unit, if stated or clearly inferable
  (e.g. "City of London", "Soho", "West End"). If not stated, infer from the address/postcode
  context if reasonably confident, otherwise leave null.

If the document describes only one building, these values will be identical across all units —
that's expected and correct. If the document describes multiple unrelated properties, each unit's
building/address_1/postcode/submarket should reflect its own specific property, not another unit's.

Also extract for each unit:
- page_index: the 0-based index of the page (counting the images shown to you, in order,
  starting at 0 for the first page) where THIS SPECIFIC unit's own row/section is stated. If a
  unit's information spans multiple pages, use the page where its floor_unit/size_sqft is given.
- floor_unit: the floor/suite/unit label (e.g. "5th Floor West", "Office 302", "Suite 4C")
- size_sqft: the area in square feet as a plain number, no commas or units. If a range is given
  (e.g. "2,123–4,454 sq ft" across multiple workspaces), do NOT guess an average — leave this null
  and note the range in special_features instead.
- desks_max: the maximum desk count as a plain integer. If given as a range ("24-58 desks"), use the
  higher number. If given as a composite like "10 + MR + PB" (meeting room, phone booth), extract just
  the numeric desk count (10) and note "+ meeting room + phone booth" in special_features.
- rent_pcm: monthly rent as a plain number (no currency symbols/commas), ONLY if explicitly stated in
  the document. Do not calculate this yourself — leave null if not directly given.
- rent_psf: rent per square foot as a plain number, ONLY if explicitly stated in the document. Do not
  calculate this yourself — leave null if not directly given.
- brochure_link: a URL for this specific unit/listing (e.g. a "view listing" or brochure/document link), if
  one is clearly given for it. A floor plan link is NOT a brochure_link — it's a genuinely different
  document (a drawing, not the brochure), even when it's the only per-row link given for a unit. If the only
  link available for a unit is explicitly labeled/described as a floor plan (e.g. "Floor Plan", "Floorplan",
  "View floorplan"), leave brochure_link null for that unit rather than substituting it — same principle as
  never substituting a generic company homepage (below). If the document instead has one shared portfolio-
  level link that applies to the whole document (not to any one specific listing), use that for every unit.
  Never take a link that belongs to one specific listing and reuse it for a different, unrelated unit —
  leave it null for units that don't have their own link when the only link found belongs to another
  listing. This must be a link to an actual brochure or listing-specific page — NEVER a generic company
  homepage, "contact us" page, top-level marketing domain (e.g. "www.workspace.co.uk" on its own, as opposed
  to a specific property page under that domain), or a floor plan. If the only link present is a generic
  company URL with no listing-specific path, leave this null rather than populating it with a non-brochure
  link.
  HARD RULE, no exceptions: if a link sits near words like "unsubscribe", "opt out", "opt-out", "manage
  preferences", "manage your subscription", or "email preferences", it must NEVER be used as a brochure_link,
  even as a last resort when nothing else is found. Leave brochure_link null for that unit instead.
- special_features: a semicolon-separated list of notable amenities, inclusions, or notes
  (e.g. "2 meeting rooms; deposit £36,000 required; 50Mb dedicated bandwidth"). Fit-out timing/
  completion details belong here too, as descriptive text (e.g. "Fit out to be completed in
  July 2026") — never in state_of_space, which only ever holds the fit-out category itself.
- state_of_space: the physical fit-out condition/readiness of the space — NOT when it becomes
  available, which is a timing detail and belongs in special_features instead (see above), not
  here. Capture this whenever the document states or clearly implies it, using the source's own
  wording where possible (e.g. "Fully Fitted", "Partially Fitted", "Fitout Underway",
  "Fully Managed", "Cat A", "Shell & Core", "Ready to Fit"). A space still being fitted out is
  still a real value here (e.g. "Fitout Underway") — that's not a reason to leave this null.
  Leave null only if the document truly gives no indication of fit-out condition at all.

Return your answer as a single JSON object with this exact structure:

{
  "provider": "..." or null,
  "contacts": "..." or null,
  "units": [
    {
      "building": "...",
      "address_1": "...",
      "postcode": "...",
      "submarket": "..." or null,
      "page_index": integer,
      "floor_unit": "..." or null,
      "size_sqft": number or null,
      "desks_max": integer or null,
      "rent_pcm": number or null,
      "rent_psf": number or null,
      "brochure_link": "..." or null,
      "special_features": "..." or null,
      "state_of_space": "..." or null
    }
  ]
}

Return ONLY this JSON object. No preamble, no explanation, no markdown code fences — just the raw JSON.
"""


def render_pages(pdf_path: Path) -> list[types.Part]:
    doc = fitz.open(pdf_path)
    parts = []
    for page in doc:
        pix = page.get_pixmap(dpi=RENDER_DPI)
        parts.append(types.Part.from_bytes(data=pix.tobytes("png"), mime_type="image/png"))
    doc.close()
    # MuPDF's internal object store (decoded pixmaps/glyphs/images) is process-wide,
    # not tied to this Document - closing it above doesn't touch that cache. Every
    # upload here is a one-off brochure that's never re-rendered, so the cache is
    # pure dead weight that otherwise accumulates ~20MB per call, forever.
    fitz.TOOLS.store_shrink(100)
    return parts


def extract_raw_units(pdf_path: Path) -> dict:
    """
    The raw Gemini JSON ({"provider", "contacts", "units": [...]}) for one
    PDF - one page-image render + one Gemini vision call against PROMPT,
    before per-row PDF-hyperlink attachment (_attach_per_row_pdf_links,
    page-index-dependent - only meaningful for a PDF Gemini has just been
    shown page images from) or ListingRow conversion. The shared core both
    extract() (below - the uploaded-PDF path, behavior-identical to before
    this was factored out) and brochure_enrichment.py build on, so there is
    only ONE PDF-vision extraction pipeline for both an uploaded brochure
    that becomes ListingRows directly and a linked brochure fetched purely
    to enrich a row from a different source (e.g. a spreadsheet row's own
    brochure_link) - never a second, independently-drifting implementation.
    """
    client = get_client()
    images = render_pages(pdf_path)
    raw = call_gemini(client, PROMPT, images)
    del images
    gc.collect()
    return raw


def extract(pdf_path: Path, original_filename: str = None, brochure_url: str = None) -> list[ListingRow]:
    """
    original_filename is the name the user actually uploaded — pdf_path itself
    is often a temp file (pages/1_Upload.py copies the upload there before
    calling this), so pdf_path.name is a randomly-generated temp name, not
    something a person should ever see in a brochure_link fallback or in the
    source_file column. Defaults to pdf_path.name for CLI usage, where pdf_path
    already is the real file.

    brochure_url is the uploaded PDF's own persisted-file URL (see
    storage/file_store.save_original_pdf), used as the PDF-fallback rule's
    brochure_link value (see finalize_brochure_link's rule 3) whenever the
    caller has one - i.e. whenever storage is GCS-backed. Falls back to the
    bare filename for CLI usage and local-disk dev mode, where there's
    nothing to point a real URL at.
    """
    filename = original_filename or pdf_path.name
    pdf_fallback_link = brochure_url or filename

    raw = extract_raw_units(pdf_path)

    # Runs BEFORE finalize_brochure_link below, and mutates page_index out of
    # each unit dict as it goes - so a unit that gets a genuine per-row link
    # here has it in place as "brochure_link" by the time finalize_brochure_
    # link's rule 1 sees it, skipping rule 3's PDF-fallback entirely; a unit
    # this doesn't apply to is completely unaffected either way.
    _attach_per_row_pdf_links(pdf_path, raw.get("units", []))

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
                    f"Warning: {filename} unit {i} has no building and no prior "
                    "unit to inherit one from — skipping this unit.",
                    file=sys.stderr,
                )
                continue
            unit["building"] = last_building
        last_building = unit["building"]

        unit["brochure_link"] = finalize_brochure_link(
            unit.get("brochure_link"), is_pdf=True, pdf_fallback_link=pdf_fallback_link
        )

        fields = ExtractedFields(**brochure, **unit).model_dump()
        fields = compute_rent(fields)
        rows.append(
            ListingRow(
                **fields,
                lat=None,
                lng=None,
                source_file=filename,
            )
        )
    return rows


def main():
    if len(sys.argv) != 2:
        print("Usage: python extract.py <path_to_pdf>", file=sys.stderr)
        raise SystemExit(1)

    pdf_path = Path(sys.argv[1])
    if not pdf_path.exists():
        raise SystemExit(f"File not found: {pdf_path}")

    try:
        rows = extract(pdf_path)
    except ValidationError as e:
        raise SystemExit(f"Gemini output did not match schema:\n{e}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"Gemini did not return valid JSON after retry:\n{e}")

    print(json.dumps([row.model_dump() for row in rows], indent=2))


if __name__ == "__main__":
    main()
