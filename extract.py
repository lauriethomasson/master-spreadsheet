import gc
import json
import sys
from pathlib import Path

import fitz  # PyMuPDF
from google.genai import types
from pydantic import ValidationError

from brochure_link_resolver import finalize_brochure_link
from gemini_client import call_gemini, compute_rent, get_client
from schema import ExtractedFields, ListingRow

RENDER_DPI = 72

PROMPT = """You are extracting structured data from a commercial office property brochure.
You will be shown the pages of the brochure as images. Read all pages carefully,
including tables, floor plans, and photo captions.

Extract the following brochure-level information (these describe who is presenting this
brochure, not which property it's about — they apply to the whole document regardless of
how many properties or units it covers):
- provider: the company/agent presenting this brochure (e.g. "Breezblok", "GPE", "The Crown Estate Workplaces").
  Some brochures are produced directly by a landlord/developer with no presenting agent named anywhere —
  in that case leave this null rather than guessing or using the building/property name as a stand-in.
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
- brochure_link: a URL for this specific unit/listing (e.g. a "view listing" or floorplan link), if one is
  clearly given for it. If the document instead has one shared portfolio-level link that applies to the
  whole document (not to any one specific listing), use that for every unit. Never take a link that belongs
  to one specific listing and reuse it for a different, unrelated unit — leave it null for units that don't
  have their own link when the only link found belongs to another listing. This must be a link to an actual
  brochure, floorplan, or listing-specific page — NEVER a generic company homepage, "contact us" page, or
  top-level marketing domain (e.g. "www.workspace.co.uk" on its own, as opposed to a specific property page
  under that domain). If the only link present is a generic company URL with no listing-specific path, leave
  this null rather than populating it with a non-brochure link.
  HARD RULE, no exceptions: if a link sits near words like "unsubscribe", "opt out", "opt-out", "manage
  preferences", "manage your subscription", or "email preferences", it must NEVER be used as a brochure_link,
  even as a last resort when nothing else is found. Leave brochure_link null for that unit instead.
- special_features: a semicolon-separated list of notable amenities, inclusions, or notes
  (e.g. "2 meeting rooms; deposit £36,000 required; 50Mb dedicated bandwidth")
- state_of_space: the fit-out condition if stated or clearly implied (e.g. "Fitted", "Fully Managed",
  "Shell and Core", "Ready to Fit", "Cat A"). Leave null if genuinely unclear.

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


def extract(pdf_path: Path, original_filename: str = None) -> list[ListingRow]:
    """
    original_filename is the name the user actually uploaded — pdf_path itself
    is often a temp file (pages/1_Upload.py copies the upload there before
    calling this), so pdf_path.name is a randomly-generated temp name, not
    something a person should ever see in a brochure_link fallback or in the
    source_file column. Defaults to pdf_path.name for CLI usage, where pdf_path
    already is the real file.
    """
    filename = original_filename or pdf_path.name

    client = get_client()
    images = render_pages(pdf_path)
    raw = call_gemini(client, PROMPT, images)
    del images
    gc.collect()

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
            unit.get("brochure_link"), is_pdf=True, own_filename=filename
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
