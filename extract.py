import gc
import json
import re
import sys
import threading
from pathlib import Path

import fitz  # PyMuPDF
from google.genai import types
from pydantic import ValidationError

from brochure_link_resolver import finalize_brochure_link, finalize_floorplan_link
from gemini_client import ResponseTruncatedError, call_gemini, compute_rent, get_client
from master_merge import normalize_key
from schema import ExtractedFields, ListingRow

RENDER_DPI = 72

# Guards every call to render_pages (see extract_raw_units) - PyMuPDF/MuPDF's
# page-rendering keeps process-wide, not per-Document, internal state (see
# render_pages' own comment on fitz.TOOLS.store_shrink), so two threads
# rendering different PDFs at the same instant risk corrupting that shared
# state, not just running slowly. Only the render step is serialized - the
# actual Gemini network call (call_gemini, below) happens OUTSIDE this lock,
# so concurrent callers (see brochure_enrichment.enrich_rows_grouped's own
# bounded worker pool) still genuinely overlap on the by far more expensive
# part (a real vision-model API round trip), just never on rendering itself.
_RENDER_LOCK = threading.Lock()

# --- Per-row PDF hyperlink extraction ---
#
# Some tabular/schedule-style PDFs (e.g. a per-floor availability table with
# its own "Link to Brochure" column, rendered as a small "Here"/"View" link
# per row) embed a genuine, distinct hyperlink per unit as a PDF link
# annotation - invisible to Gemini's vision-based extraction (see extract(),
# which only ever shows Gemini rendered page IMAGES, never the underlying
# text/link layer), so without this step every such unit's own brochure_
# link would fall straight through to finalize_brochure_link's own rule 2/
# "nothing genuine" path, losing the real per-row destination entirely.
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
#
# EXCEPTION: a page with EXACTLY ONE unit needs only ONE caption-sized
# link, not MIN_UNITS_FOR_PER_ROW_LINKS/MIN_LINKS_FOR_PER_ROW_LINKS. Both
# of those thresholds exist purely to avoid cross-row ambiguity when
# matching links to rows on a busy, tabular schedule-style page - with
# only one unit on the page, there is only one row a link could possibly
# belong to, so that ambiguity is structurally impossible regardless of
# how few units/links there are. Confirmed real gap this closes: a
# Colliers Canva-deck export ("Colliers Flex and Managed Availability")
# uses a slide-per-building layout - one page, one line item, with the
# building name itself hyperlinked to a real, listing-specific colliers.com
# property page - which the flat ">= 2 units" gate skipped entirely,
# silently losing that real link to the generic whole-PDF rule-3 fallback
# instead. Every OTHER safeguard (row locatable on the page, exactly one
# nearby candidate link, brochure/floorplan column x-range narrowing) is
# completely unchanged and applies identically regardless of how many
# units are on the page - see _attach_per_row_pdf_links's own docstring.
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

    A page with EXACTLY ONE unit is the one exception to both gates (see
    the module-level comment's own "EXCEPTION" paragraph for the real
    Colliers slide-deck case this exists for): it only needs 1 caption-
    sized link, not MIN_UNITS_FOR_PER_ROW_LINKS/MIN_LINKS_FOR_PER_ROW_
    LINKS - there is only one row on the page a link could belong to, so
    the cross-row ambiguity those thresholds guard against cannot occur.
    Every downstream check (row locatable, exactly one nearby candidate,
    column x-range narrowing) still applies completely unchanged.
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
            if not (0 <= page_index < doc.page_count):
                continue
            # See the module-level comment's own "EXCEPTION" paragraph -
            # a single-unit page has no cross-row ambiguity to guard
            # against at all, so it's held to a single, lower link-count
            # threshold instead of MIN_UNITS_FOR_PER_ROW_LINKS/MIN_LINKS_
            # FOR_PER_ROW_LINKS.
            is_single_unit_page = len(page_units) == 1
            if not is_single_unit_page and len(page_units) < MIN_UNITS_FOR_PER_ROW_LINKS:
                continue
            page = doc[page_index]
            links = [l for l in _page_uri_links(page) if _is_caption_sized(l["rect"])]
            min_links_required = 1 if is_single_unit_page else MIN_LINKS_FOR_PER_ROW_LINKS
            if len(links) < min_links_required:
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
- contacts: every contact person or generic contact listed in the document as a whole (e.g. "Sales" if
  no named person is given) — the DOCUMENT-WIDE default, used for a unit that has no contact more
  specifically its own (see each unit's own "contacts" field below, which takes priority over this one
  whenever the document distinguishes a different contact for that specific unit/building). Format each
  contact as "Name, email, phone" — omit any of the three pieces that aren't given. If there are
  multiple contacts, join them with "; ". Contact details often appear on a later/closing page (an agent
  panel, a "get in touch" page) as well as a cover or intro page — check every page before concluding
  there are none. Leave null only if the document genuinely names no contact/agent anywhere.
- property_features: notable amenities, certifications, or characteristics stated as applying to the
  WHOLE property/development this document describes, not to one specific building or unit within it —
  e.g. general sustainability/accreditation credentials ("WiredScore Platinum", "BREEAM Excellent",
  "WELL Platinum Enabled"), or shared campus-wide facilities ("16 showers & 108 lockers", "160 cycle
  spaces", "natural ventilation"), as a semicolon-separated list. Only include something here if it is
  stated as applying to the property/development as a whole. If the document covers multiple entirely
  unrelated properties, or a feature is only ever described for one specific building rather than the
  whole site, leave this null rather than guessing it's shared — that belongs in building_features
  instead (see below, after the units). Leave null if the document states nothing at this level.
- development_name: the name of the overall campus/development this brochure describes, ONLY when it is
  a genuinely DISTINCT name from any individual building's own name within it — e.g. a brochure branded
  "Regent's Wharf" that contains separately-named buildings "Thorley Works", "The Canal Building", "The
  Mill", and "The Packing House" would have development_name "Regent's Wharf" (never one of the building
  names themselves). If the brochure describes just a single building with no separate campus/development
  branding — i.e. the building's own name IS the only name given, with nothing bigger it belongs to — leave
  this null rather than repeating the building name here or inventing a development name that isn't
  actually stated.

Then, identify EVERY SEPARATE AVAILABLE UNIT/SPACE described in the brochure. A brochure may describe
just one unit, many units within one building (e.g. a schedule of areas listing multiple floors), or
many units across entirely unrelated properties (different streets, different buildings). Treat each
distinct floor, suite, or unit as a separate entry.

For each unit, extract its own location fields — do not assume they're shared with other units:
- building: the name of the building this specific unit is in (e.g. "John Stow House", "City Tower").
  ALWAYS populate this for every unit, even when several consecutive units are in the same building and
  it feels redundant to repeat it — never leave building null.
- address_1: the street address of this specific unit's building (e.g. "18 Bevis Marks") - ONLY
  when a real street address is actually stated somewhere for it. Many documents state no street
  address at all, only a building name and/or a neighbourhood/area label - if that's all this
  document gives you, leave address_1 null. NEVER put the building's own name here, and NEVER put
  a neighbourhood/area/district name here (that belongs in submarket below, not here) - a building
  name or area name is not a street address, even when it's the only location text available.
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
- rent_pcm: the rent expressed per calendar month (PCM), as a plain number (no currency symbols/commas/
  "pcm" suffix), ONLY if the document itself labels or clearly states a figure as a MONTHLY rent (e.g.
  "pcm", "per month", "/month"). Do not calculate this yourself from rent_psf, an annual figure, or size —
  leave null if not directly given as a monthly figure.
- rent_psf: the rent expressed per square foot (almost always per annum, e.g. "£33 psf" or "£33 per sq ft
  pa"), as a plain number (no currency symbols/commas/"psf" suffix), ONLY if the document itself labels or
  clearly states a figure as a PER-SQUARE-FOOT rent (e.g. "psf", "per sq ft", "per square foot"). Do not
  calculate this yourself from rent_pcm, an annual figure, or size — leave null if not directly given as a
  per-square-foot figure.
  A document routinely states only ONE of rent_pcm/rent_psf, never both — that is normal and expected;
  never derive the other one from it, and never copy the same number into both fields. Other monetary
  figures that can appear near a rent — a total ANNUAL rent, a service charge, business rates, or a
  deposit — are each a genuinely different figure, not a rent_pcm or rent_psf value; never let one of
  those fill either rent field. Never infer which category a figure belongs to from its magnitude alone
  (e.g. a small number is not automatically "per square foot" and a large one is not automatically
  "monthly") — only from the document's own explicit label or unambiguous wording. If a figure's own basis
  (monthly vs per-square-foot vs annual vs some other charge) is not clearly stated, or the document shows
  multiple rent-like figures and you cannot confidently tell which belongs to THIS unit, leave BOTH
  rent_pcm and rent_psf null for this unit rather than guessing — a missing rent is far less costly than a
  wrongly-labeled one.
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
  Some pages are immediately followed by a line in this exact shape: "(Real links found on the page shown
  just above: 'visible text' -> URL; ...)" — these are the page's own genuine, real links, already confirmed
  to exist (never invented). When one is present, use it (following every rule above — still never a
  booking/viewing/contact/floor-plan/unsubscribe link, still never reused across a different unit) rather
  than relying on the visual layout alone: match a candidate to a specific unit by its own visible text —
  e.g. the candidate's text IS that unit's own building name (a table row where the building name itself is
  the link), or the candidate's text is an unambiguous document label ("Link to Brochure", "View Brochure",
  "Download") sitting on a page that describes only that one unit. Never use a URL that ISN'T listed in one
  of these lines when they're present for that page — copy it verbatim, character for character, never
  paraphrased or guessed. A page with no such line has no real link candidates at all for this feature —
  fall back to every rule above exactly as if this paragraph didn't exist.
- floorplan_link: a URL specifically labeled/described as a floor plan for THIS unit (e.g. "Floor Plan",
  "Floorplan", "View floorplan", "Download Floorplans"), if one is given — the exact link brochure_link
  above must NEVER use. Leave null if no such link is given for this unit. Same generic-homepage/
  unsubscribe exclusions as brochure_link apply here too.
- special_features: a semicolon-separated list of notable amenities, inclusions, or notes specific to
  THIS unit/floor (e.g. "2 meeting rooms; deposit £36,000 required; 50Mb dedicated bandwidth"). A
  characteristic shared by the WHOLE building this unit is in (not just this one floor) belongs in
  building_features instead (see below, after the units) — do not repeat it here. Fit-out timing/
  completion details belong here too, as descriptive text (e.g. "Fit out to be completed in
  July 2026") — never in state_of_space, which only ever holds the fit-out category itself.
- state_of_space: the physical fit-out condition/readiness of the space — NOT when it becomes
  available, which is a timing detail and belongs in special_features instead (see above), not
  here. Capture this whenever the document states or clearly implies it, using the source's own
  wording where possible (e.g. "Fully Fitted", "Partially Fitted", "Fitout Underway",
  "Fully Managed", "Cat A", "Shell & Core", "Ready to Fit"). A space still being fitted out is
  still a real value here (e.g. "Fitout Underway") — that's not a reason to leave this null.
  Leave null only if the document truly gives no indication of fit-out condition at all.
- contacts: the contact person(s) for THIS SPECIFIC unit/building, ONLY if the document distinguishes
  one for it — e.g. a multi-building brochure whose own per-building schedule-of-areas page names a
  different agent right next to that building's own units, distinct from whichever contact(s) appear
  elsewhere in the document. Same "Name, email, phone" format, same "; "-joined for multiple, as the
  document-level contacts field above. Leave this null (never repeat the document-wide contacts here)
  whenever this unit's own building has no contact distinctly its own — the document-level contacts
  field above is used automatically for any unit left null here, so there is no need to duplicate it.

After the units, also extract building_features: an array of {"building", "features"} objects — one
entry for each DISTINCT building name (matching a "building" value used above) that has its own
descriptive text describing that WHOLE building specifically — not one particular floor within it, and
not the whole property/development (that's property_features, above). This is common in a brochure
covering several buildings on one site: a short paragraph about each individual building's own
character, construction, or amenities, often printed right next to that building's own schedule of
areas (e.g. "original steel columns, exposed beams and a spectacular canalside frontage" describing one
specific building, distinct from a neighbouring building's own different paragraph). Only include a
building here if the document genuinely states something at this specific level for it — omit a
building entirely (don't include an empty-string entry) if nothing building-specific is stated for it,
and never invent or infer building-level text from general knowledge.

Return your answer as a single JSON object with this exact structure:

{
  "provider": "..." or null,
  "contacts": "..." or null,
  "property_features": "..." or null,
  "development_name": "..." or null,
  "building_features": [
    {"building": "...", "features": "..."}
  ],
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
      "floorplan_link": "..." or null,
      "special_features": "..." or null,
      "state_of_space": "..." or null,
      "contacts": "..." or null
    }
  ]
}

Return ONLY this JSON object. No preamble, no explanation, no markdown code fences — just the raw JSON.
"""


def render_pages(pdf_source) -> list[types.Part]:
    """
    pdf_source is either a Path (existing on-disk file - the uploaded-PDF
    path, extract() below) or raw bytes (a brochure fetched purely in
    memory - see brochure_enrichment.py, which deliberately never writes a
    linked brochure to a temp file at all: on a container filesystem backed
    by tmpfs, e.g. Cloud Run's default /tmp, a temp file's bytes count
    against the SAME memory budget as an in-memory bytes object, so writing
    one would just be a second, redundant copy of a PDF that can be up to
    ~32MB, held for no benefit).

    doc.close()/store_shrink run in a finally, not just at the end of the
    happy path - a page that fails partway through rendering (a malformed
    PDF) must not leak this Document or skip shrinking the process-wide
    store below just because an exception is about to propagate.
    """
    if isinstance(pdf_source, (bytes, bytearray)):
        doc = fitz.open(stream=pdf_source, filetype="pdf")
    else:
        doc = fitz.open(pdf_source)
    try:
        parts = []
        for page in doc:
            pix = page.get_pixmap(dpi=RENDER_DPI)
            parts.append(types.Part.from_bytes(data=pix.tobytes("png"), mime_type="image/png"))
            del pix
        return parts
    finally:
        doc.close()
        # MuPDF's internal object store (decoded pixmaps/glyphs/images) is process-wide,
        # not tied to this Document - closing it above doesn't touch that cache. Every
        # upload here is a one-off brochure that's never re-rendered, so the cache is
        # pure dead weight that otherwise accumulates ~20MB per call, forever.
        fitz.TOOLS.store_shrink(100)


_JPEG_MAGIC = b"\xff\xd8\xff"


def _page_links_text(links: list) -> str:
    """One text line listing a single page's own real link candidates
    (see canva_renderer/app.py's _page_link_candidates), in the exact
    "'visible text' -> URL" shape PROMPT's own brochure_link instructions
    describe - semicolon-joined when a page has more than one. Never
    called for an empty list (see images_from_png_pages's own guard)."""
    return "; ".join(f"{l['text']!r} -> {l['href']}" for l in links)


def images_from_png_pages(png_pages: list, page_links: list = None) -> list:
    """
    `png_pages` (a list of already-rendered page image bytes, one per page
    - see canva_renderer/app.py's multi-page capture) wrapped as types.Part
    objects, in the same order, via the exact same construction render_
    pages already uses per page (types.Part.from_bytes(..., mime_type=...))
    - never a second, differently-built image representation.

    Despite this function's own name (kept for backward compatibility with
    every existing call site - see brochure_enrichment._extract_brochure_
    units, the only caller that omits page_links), the bytes aren't always
    actually PNG: canva_renderer/app.py adaptively re-encodes a large/
    photo-dense capture as JPEG instead, to stay under Cloud Run's own
    hard 32MB response-size ceiling (confirmed directly - a real 29-page
    brochure's PNG+base64 payload measured at 34.24MB, over that limit;
    JPEG at quality=85 measured at 7.00MB for the identical real capture).
    The mime_type used for EVERY page here is sniffed once from the FIRST
    page's own magic bytes (JPEG's fixed \\xff\\xd8\\xff header vs.
    anything else treated as PNG) - a whole render's pages are always ALL
    the same format (the renderer picks one format for the entire
    response, never mixed per page), so checking just the first is
    sufficient and avoids a per-page branch. Deliberately NOT a new field
    in the renderer's own JSON response that this app would have to know
    about - sniffing the actual bytes works correctly regardless of which
    of the two services gets deployed first, with zero cross-service
    wire-contract coordination required either way.

    Deliberately NOT routed through render_pages/fitz.open at all: these
    bytes are already rendered raster pages (a real browser's own
    screenshot), not a vector PDF that needs rasterizing - re-opening one
    at a time as a fake one-page "PDF" would just be a slower, no-op round
    trip through PyMuPDF for identical output. The caller (see brochure_
    enrichment._extract_brochure_units) still passes the result straight
    into render_and_extract, exactly like render_pages' own output - there
    is no separate extraction path for this source.

    page_links (default None - every existing call site keeps working
    completely unchanged) is the SAME length as png_pages (see brochure_
    enrichment.fetch_rendered_page_with_links) - when given, each page's
    own real <a href> candidates (see canva_renderer/app.py's own
    _page_link_candidates) are interleaved as a plain text Part
    immediately AFTER that page's own image, in PROMPT's own documented
    "'visible text' -> URL" shape - the same "surrounding text already
    tells you which link belongs to which unit" idea extract_email.py's
    own plain-text email body already lets Gemini use for attributing a
    link to a unit, just sourced from a rendered page's real DOM links
    instead of an email's inline text. A page with no real links at all
    ([] in page_links) gets no text Part - never an empty/noise line.
    """
    mime_type = "image/jpeg" if png_pages and png_pages[0][:3] == _JPEG_MAGIC else "image/png"
    if page_links is None:
        return [types.Part.from_bytes(data=page, mime_type=mime_type) for page in png_pages]

    parts = []
    for i, page in enumerate(png_pages):
        parts.append(types.Part.from_bytes(data=page, mime_type=mime_type))
        links = page_links[i] if i < len(page_links) else []
        if links:
            parts.append(f"(Real links found on the page shown just above: {_page_links_text(links)})")
    return parts


def render_and_extract(images: list, client=None, prompt: str = None) -> dict:
    """
    The second half of extract_raw_units, split out on its own so a caller
    that rendered from an in-memory bytes source (see render_pages) can
    drop that source's own reference BETWEEN rendering and this call,
    rather than being forced to hold it alive for the full, much slower
    Gemini round trip too (see brochure_enrichment._extract_brochure_units,
    the one caller that actually needs this split - extract_raw_units below
    remains the single call every other caller should keep using).

    client defaults to a fresh get_client() call if not given - accepting
    one explicitly only lets extract_raw_units reuse the SAME client it
    already made for this call, never a shared one across separate calls.

    prompt defaults to this module's own brochure-extraction PROMPT if not
    given - accepting one explicitly lets a caller reuse this exact render-
    then-call-Gemini plumbing against a DIFFERENT prompt entirely (see
    brochure_enrichment.FLOORPLAN_PROMPT, a narrower prompt for reading a
    floor plan document rather than a marketing brochure) without
    duplicating the render/GC/error-handling logic here.
    """
    client = client or get_client()
    raw = call_gemini(client, prompt or PROMPT, images)
    del images
    gc.collect()
    return raw


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
    Unchanged for this (Path-based, single-call) caller - see render_pages/
    render_and_extract's own docstrings for the one place that now calls
    those two steps separately instead of through here.

    Safe to call from multiple threads concurrently (see brochure_
    enrichment.enrich_rows_grouped's own bounded worker pool) - get_client()
    makes a fresh genai.Client per call (never a shared, mutable one across
    threads), and render_pages' own process-wide MuPDF state is serialized
    via _RENDER_LOCK, above - only the actual Gemini network call is ever
    allowed to genuinely overlap between threads.
    """
    client = get_client()
    with _RENDER_LOCK:
        images = render_pages(pdf_path)
    return render_and_extract(images, client=client)


def extract(pdf_path: Path, original_filename: str = None) -> list[ListingRow]:
    """
    original_filename is the name the user actually uploaded — pdf_path itself
    is often a temp file (pages/1_Upload.py copies the upload there before
    calling this), so pdf_path.name is a randomly-generated temp name, not
    something a person should ever see in the source_file column. Defaults
    to pdf_path.name for CLI usage, where pdf_path already is the real file.

    No longer accepts a brochure_url parameter - the uploaded PDF's own
    persisted-file URL used to be finalize_brochure_link's own PDF-fallback
    default (its former "rule 3": no genuine per-unit link found -> default
    to the whole uploaded document) whenever a unit had no genuine link of
    its own. That rule was REMOVED (a deliberate reversal of a prior
    intentional design decision, confirmed before removing it) - a PDF
    upload with no genuine per-unit link now leaves brochure_link blank,
    the same as a spreadsheet/email upload already did. Callers that still
    persist the uploaded PDF (see storage/file_store.save_original_pdf) for
    OTHER reasons (e.g. app.py's own paste-a-link validation/propagation)
    keep doing so independently of this function.
    """
    filename = original_filename or pdf_path.name

    raw = extract_raw_units(pdf_path)

    # Runs BEFORE finalize_brochure_link inside _rows_from_raw, and mutates
    # page_index out of each unit dict as it goes - so a unit that gets a
    # genuine per-row link here has it in place as "brochure_link" by the
    # time finalize_brochure_link's rule 1 sees it; a unit this doesn't
    # apply to is completely unaffected either way.
    _attach_per_row_pdf_links(pdf_path, raw.get("units", []))

    rows, _ = _rows_from_raw(raw, filename)
    return rows


def extract_from_png_pages(
    png_pages: list, original_filename: str, page_links: list = None,
) -> list[ListingRow]:
    """
    Like extract(), but for a set of already-rendered page images (a
    Canva/Pitch paste-a-link render - see app.py's own _fetch_pasted_link/
    brochure_enrichment.fetch_rendered_page_with_links) rather than a
    real PDF file on disk. Never rasterizes anything itself (png_pages
    are already real screenshots), and never calls _attach_per_row_pdf_
    links - that mechanism reads a real PDF's own embedded link
    ANNOTATIONS (see fitz.Page.get_links), which a screenshot-derived
    document has none of; page_links (see images_from_png_pages) is the
    equivalent signal for THIS source, read directly from the live DOM
    at render time instead, and is handed to Gemini itself rather than
    joined by page position after the fact.

    No longer accepts a brochure_url parameter - see extract()'s own
    docstring on why (finalize_brochure_link's former PDF-fallback default
    was removed entirely). A caller that still needs the pasted link's own
    persisted-copy URL for OTHER purposes (see app.py's own paste-a-link
    validation/propagation below) keeps and passes it around independently
    of this function.

    Returns rows exactly like extract() does, as a plain list[ListingRow] a
    caller can iterate/index/len() exactly like any other - EXCEPT this one
    also carries a `.page_indices` attribute (a parallel list, same order/
    length as the rows themselves, each entry the originating raw Gemini
    unit's own page_index, or None) - see _ExtractedRows below. A caller
    wanting to validate/replace a per-unit brochure_link this produced (see
    the Review page's own paste-a-link validation), or to backfill a
    validated per-unit link across other rows sharing the same page/
    building (see app.py's own _propagate_validated_links_within_page),
    does so afterward, on the returned rows; this function itself never
    fetches anything over the network.
    """
    client = get_client()
    images = images_from_png_pages(png_pages, page_links=page_links)
    raw = render_and_extract(images, client=client)
    rows, page_indices = _rows_from_raw(raw, original_filename, document_wide_contacts_is_row_own_document=False)
    result = _ExtractedRows(rows)
    result.page_indices = page_indices
    return result


class _ExtractedRows(list):
    """
    A plain list[ListingRow] that ALSO carries the originating page_index
    for each row (see extract_from_png_pages's own docstring) as a PARALLEL
    list - `.page_indices`, never a new list element - same idiom as
    brochure_enrichment._BrochureUnits, for the same reason: every existing
    caller that already treats this as a plain list (truthiness, len(),
    iteration, indexing) is completely unaffected, since it genuinely IS
    one; only code that explicitly asks for .page_indices sees it.
    """

    page_indices = None


def _match_building_features(unit_building: str, building_features: list) -> str | None:
    """
    The building-wide features text (a plain str) confidently identified as
    describing `unit_building`, or None - the extract.py counterpart to
    brochure_enrichment.py's own _match_building_feature, used for the same
    reason: a unit's own special_features (below) never repeats text the
    prompt already asks Gemini to place at building_features instead (see
    the extraction PROMPT's own building_features section) - it belongs
    combined in, not lost.

    Deliberately only the EXACT-match tier of brochure_enrichment.py's own
    _building_identity_matches (normalize_key on both sides - case/
    whitespace/punctuation-tolerant, never fuzzy), not its weaker address-
    suffix-stripped tiers: those exist there specifically to bridge a row's
    building name (e.g. typed into a landlord's own spreadheet, or matched
    against a SEPARATELY fetched enrichment brochure) against a genuinely
    different document's own spelling of it. Here, unit_building and
    building_features both come from the exact same single Gemini call over
    the exact same document, so that cross-document drift a stripped-suffix
    tier is for doesn't apply - and reusing brochure_enrichment.py's own
    function directly isn't possible without creating a circular import
    (brochure_enrichment.py already imports extract.py).

    Two or more entries matching the same normalized building name
    (shouldn't occur - the prompt asks for one entry per distinct building -
    but never assumed) is treated as ambiguous and returns None, same
    "incorrect enrichment is worse than a blank field" policy as
    brochure_enrichment.py's own version.
    """
    if not building_features:
        return None
    key = normalize_key(unit_building)
    if not key:
        return None
    matches = [
        bf.get("features") for bf in building_features
        if isinstance(bf, dict) and normalize_key(bf.get("building")) == key
    ]
    if len(matches) != 1:
        return None
    features = matches[0]
    return features if isinstance(features, str) and features.strip() else None


def _rows_from_raw(
    raw: dict, filename: str, document_wide_contacts_is_row_own_document: bool = True,
) -> tuple[list[ListingRow], list]:
    """
    The raw Gemini JSON's own "units" (plus document-level provider/
    contacts) turned into (rows, page_indices) - rows shared by extract() (a
    real PDF file) and extract_from_png_pages() (an already-rendered page-
    image source, e.g. a pasted Canva/Pitch link) so there is only ONE such
    conversion, never two independently-drifting implementations. Each
    caller is responsible for whatever page-specific per-unit link
    attachment applies to ITS OWN source BEFORE calling this (see
    extract()'s own _attach_per_row_pdf_links call, and extract_from_
    png_pages's own docstring on why that specific mechanism doesn't
    apply to a rendered-image source at all) - by the time raw["units"]
    reaches here, each unit's own "brochure_link" is already whatever
    that source-specific step (or Gemini's own vision-only guess) left
    it as.

    document_wide_contacts_is_row_own_document (default True, extract()'s
    own case) - whether raw["contacts"] (the document-wide fallback,
    below) genuinely describes every unit found in `raw`. True for
    extract(): `raw` there IS the one real document each returned row's
    own brochure_link already points at (or will, once finalize_
    brochure_link runs below), so a document-wide contact is a completely
    valid fallback for a unit with no distinguishable contact of its own.

    False for extract_from_png_pages() specifically: `raw` there is a
    SHARED multi-property overview deck (a real, confirmed Colliers
    Canva-deck production case - 21 different real buildings, 13 genuinely
    different individually-linked brochures, one shared "team" contact
    block on the deck's own closing page) - each returned row's own
    brochure_link, once validated/propagated by app.py, points at a
    DIFFERENT, separately-fetched document that this extraction never even
    read. Falling back to raw["contacts"] there baked the shared deck's own
    generic contact onto every unit whose own per-unit "contacts" Gemini
    left blank - permanently, since brochure_enrichment._apply_units_to_
    row's own contacts fill is gated on the field still being blank (see
    its own docstring), so the correct, later-derived per-row value from
    each row's own real brochure_link could never actually land. Leaving
    contacts genuinely blank here instead - never guessing at a document
    this extraction never saw - is what lets that same, already-correct
    per-row enrichment fill it properly afterward, exactly as it already
    does for every other field on these same rows.

    page_indices is a plain list, same order/length as rows, of each row's
    own raw unit's page_index (or None - either never stated, or already
    popped by extract()'s own _attach_per_row_pdf_links, which runs BEFORE
    this and has no further use for it once a per-row PDF link search has
    already consumed it). page_index itself is never part of ExtractedFields/
    ListingRow's own declared schema (never written to the staging file or
    shown in the Review UI) - read directly off the raw unit dict here,
    before it's discarded, purely so extract_from_png_pages's own caller can
    still group rows by originating page after the fact.
    """
    # No "contacts" key here - each unit below always sets its own resolved
    # value (its own per-unit contacts, or the document-wide fallback) onto
    # itself before the merge; a duplicate key in both dicts would make the
    # ExtractedFields(**brochure, **unit) call below raise TypeError.
    brochure = {
        "internal_ref": raw.get("provider"),
        "provider": raw.get("provider"),
        "development_name": raw.get("development_name"),
    }

    # The prompt asks Gemini for building_features/property_features
    # alongside every unit's own special_features (see the PROMPT's own
    # building_features/property_features sections, above) - but unlike
    # brochure_enrichment.py's equivalent combine (see its own enrich_row),
    # nothing here previously read either back out, so this per-document
    # descriptive text was extracted and then silently discarded for every
    # PDF/Canva-sourced row. Confirmed via real Colliers master-spreadsheet
    # data: a multi-page campus brochure's real per-building/per-property
    # text never reached special_features at all, even though Gemini's own
    # raw JSON response already had it. building_features/property_features
    # themselves are read once here, outside the loop - both are document-
    # level, identical for every unit.
    building_features = raw.get("building_features") or []
    property_features = raw.get("property_features")
    if not (isinstance(property_features, str) and property_features.strip()):
        property_features = None

    rows = []
    page_indices = []
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

        # Combined unconditionally, never gated on unit["special_features"]
        # being blank - a short but genuinely non-blank per-unit value (e.g.
        # "Cat A fit-out") must still pick up real building/property text
        # alongside it, same never-gate-on-blank policy as brochure_
        # enrichment.py's own combine (see its own enrich_row docstring for
        # why: gating on blank alone would silently drop this text for
        # every unit that already has SOME of its own, which is most units).
        unit["special_features"] = "; ".join(
            seg for seg in (
                unit.get("special_features"),
                _match_building_features(unit["building"], building_features),
                property_features,
            )
            if isinstance(seg, str) and seg.strip()
        ) or None

        # Per-unit contacts (see the PROMPT's own per-unit "contacts" field
        # docstring) PREFERRED over the document-wide one, never combined
        # with it - unlike special_features above, a unit's own distinct
        # contact and the document-wide default describe the SAME kind of
        # fact (who to contact), so showing both would read as two
        # different, possibly conflicting answers to "who do I call" rather
        # than one clear one. Confirmed real gap this closes: a real multi-
        # building Colliers deck names a genuinely DIFFERENT agent on
        # different buildings' own pages, which used to be flattened into
        # one shared value (raw["contacts"]) applied identically to every
        # row regardless of which building it actually described. Falls
        # back to the document-wide value (unchanged from before this
        # existed) whenever this unit's own "contacts" is blank AND
        # document_wide_contacts_is_row_own_document is True - the
        # overwhelmingly common single-contact-document case (extract()'s
        # own callers). See this function's own docstring on
        # document_wide_contacts_is_row_own_document for why extract_from_
        # png_pages's own paste-a-link/multi-property-deck case must NOT
        # fall back here - a real, confirmed production bug this guards
        # against (a shared deck's own generic team contact silently
        # winning over every individual building's own real, genuinely
        # different agent).
        unit_contacts = unit.get("contacts")
        if not (isinstance(unit_contacts, str) and unit_contacts.strip()):
            unit_contacts = raw.get("contacts") if document_wide_contacts_is_row_own_document else None
        unit["contacts"] = unit_contacts

        page_index = unit.get(PAGE_INDEX_KEY)
        if not isinstance(page_index, int):
            page_index = None

        unit["brochure_link"] = finalize_brochure_link(unit.get("brochure_link"))
        unit["floorplan_link"] = finalize_floorplan_link(unit.get("floorplan_link"))

        # No genuine brochure, but a real floor plan exists - shown as
        # brochure_link too (see ListingRow.brochure_link_is_floorplan's
        # own docstring) rather than silently hidden behind floorplan_link,
        # which stays a hidden column by default. floorplan_link itself is
        # untouched either way - this only ever ADDS a value to brochure_
        # link, never replaces a genuine one (that case never reaches here:
        # brochure_link is already non-blank).
        brochure_link_is_floorplan = None
        if not unit.get("brochure_link") and unit.get("floorplan_link"):
            unit["brochure_link"] = unit["floorplan_link"]
            brochure_link_is_floorplan = True

        fields = ExtractedFields(**brochure, **unit).model_dump()
        fields = compute_rent(fields)
        rows.append(
            ListingRow(
                **fields,
                lat=None,
                lng=None,
                source_file=filename,
                brochure_link_is_floorplan=brochure_link_is_floorplan,
            )
        )
        page_indices.append(page_index)
    return rows, page_indices


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
    except ResponseTruncatedError as e:
        raise SystemExit(str(e))
    except json.JSONDecodeError as e:
        raise SystemExit(f"Gemini did not return valid JSON after retry:\n{e}")

    print(json.dumps([row.model_dump() for row in rows], indent=2))


if __name__ == "__main__":
    main()
