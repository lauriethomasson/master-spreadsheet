import gc
import json
import sys
from pathlib import Path

import fitz  # PyMuPDF
from google.genai import types
from pydantic import ValidationError

from gemini_client import call_gemini, compute_rent, get_client
from schema import ExtractedFields, ListingRow

RENDER_DPI = 72

PROMPT = """You are extracting structured data from a commercial office property brochure.
You will be shown the pages of the brochure as images. Read all pages carefully,
including tables, floor plans, and photo captions.

Extract the following brochure-level information (these describe who is presenting this
brochure, not which property it's about — they apply to the whole document regardless of
how many properties or units it covers):
- provider: the company/agent presenting this brochure (e.g. "Breezblok", "GPE", "The Crown Estate Workplaces")
- brochure_link: any URL present in the document (e.g. a portfolio page link), otherwise null
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
- special_features: a semicolon-separated list of notable amenities, inclusions, or notes
  (e.g. "2 meeting rooms; deposit £36,000 required; 50Mb dedicated bandwidth")
- state_of_space: the fit-out condition if stated or clearly implied (e.g. "Fitted", "Fully Managed",
  "Shell and Core", "Ready to Fit", "Cat A"). Leave null if genuinely unclear.

Return your answer as a single JSON object with this exact structure:

{
  "provider": "...",
  "brochure_link": "..." or null,
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
    return parts


def extract(pdf_path: Path) -> list[ListingRow]:
    client = get_client()
    images = render_pages(pdf_path)
    raw = call_gemini(client, PROMPT, images)
    del images
    gc.collect()

    brochure = {
        "internal_ref": raw.get("provider"),
        "provider": raw.get("provider"),
        "brochure_link": raw.get("brochure_link"),
        "contacts": raw.get("contacts"),
    }

    rows = []
    last_building = None
    for i, unit in enumerate(raw.get("units", [])):
        if not unit.get("building"):
            if not last_building:
                print(
                    f"Warning: {pdf_path.name} unit {i} has no building and no prior "
                    "unit to inherit one from — skipping this unit.",
                    file=sys.stderr,
                )
                continue
            unit["building"] = last_building
        last_building = unit["building"]

        fields = ExtractedFields(**brochure, **unit).model_dump()
        fields = compute_rent(fields)
        rows.append(
            ListingRow(
                **fields,
                lat=None,
                lng=None,
                source_file=pdf_path.name,
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
