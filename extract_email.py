import email
import json
import re
import sys
from email import policy
from pathlib import Path

from pydantic import ValidationError

from brochure_link_resolver import resolve_brochure_link
from gemini_client import call_gemini, compute_rent, get_client
from schema import ExtractedFields, ListingRow

PROMPT = """You are extracting structured commercial office availability data from the body
of an email. Emails of this type are periodic "availability update" newsletters from a
property agent/landlord, listing currently available office units across one or more buildings.

The email body has this general shape:
- Marketing/news content at the top (promotions, awards, press) — IGNORE this, it is not
  availability data.
- An availability section containing the real listings (may be headed "CURRENT AVAILABILITY",
  "WEEKLY AVAILABILITY", or similar). This is what you extract.
- Within it, short ALL-CAPS lines are submarket/area section headers (e.g. "SOHO", "THE CITY",
  "WEST END", "ANGEL", "SHOREDITCH", "MID TOWN"). Every building and unit that follows belongs
  to that submarket until the next ALL-CAPS header appears.
- Under each submarket header, one or more building names appear, each followed by that unit's
  metrics. Two different layouts are both common — handle either:
  (a) A small table with columns roughly [floor/label] [desk space] [sq ft] [price], with one
      row per unit; or
  (b) Inline labeled fields directly under the building name, e.g. "Sqft: 765", "Desks: 10 + MR
      + PB", "Price: £10,200", "Av: Now" — here the building name line itself IS the one unit
      (unless it has a floor suffix like "141 Fenchurch Street (Monument) - 3rd Floor", in which
      case each such line is a separate unit within that same building).
- A contacts section near the end (e.g. "Get in touch" or "Contact"), listing named people with
  phone numbers and/or emails.
- A company/sender mailing address may appear in marketing boilerplate or the footer/signature
  (e.g. "Our mailing address is: ..."). This is the SENDER's own registered address, not the
  address of any listed property — never attribute it to a unit's building, even if it's the
  only address anywhere in the document.

Extract the following email-level information (who sent this, not which property it's about):
- provider: the company/agent whose availability this is (e.g. "GPE", "MetSpace"). This is
  usually clear from the branding/sender context, not necessarily the literal email address
  domain or legal entity name (e.g. use "MetSpace" not "Metspace London LTD"). If genuinely
  no sender/branding is identifiable, leave this null rather than guessing.
- contacts: every named contact person listed (typically in a "Get in touch"/"Contact" section),
  each as "Name, email, phone" — omit whichever of email/phone isn't given for that person. Join
  multiple contacts with "; ".

Then identify EVERY SEPARATE AVAILABLE UNIT (each row/building-block of availability data).
For each unit, extract its own location fields — do not assume they're shared with other units,
since this email may list several unrelated buildings:
- building: the property name this unit belongs to (e.g. "16 Dufour's Place", "141 Fenchurch
  Street (Monument)"). ALWAYS populate this for every unit, even when several consecutive units
  are in the same building and it feels redundant to repeat it — never leave building null.
- submarket: the ALL-CAPS section header this building falls under, normalized to title case
  (e.g. "Soho", "West End", "Mid Town")
- address_1: leave this null unless a street address is stated for THIS SPECIFIC property
  (not the sender's own mailing address — see above). Most emails of this type never state one.
- postcode: leave this null for the same reason, with the same exception.

Also extract for each unit:
- floor_unit: the unit's label exactly as given — a floor ("6th floor", "G Floor", "3rd Floor"),
  a group-summary label like "3 workspaces" when one row summarizes several units together, or
  null if the building has only one undivided listing with no floor/suite label at all.
- desks: may appear as a plain number ("10"), a range ("48-72"), and/or with trailing qualifiers
  after a "+" (e.g. "10 + MR + PB", "48 + 4 MR + 3 PB", "48-72 + 5 MR" — MR/PB/etc. mean meeting
  room/phone booth, not extra desks):
  - Extract ONLY the leading desk number(s) into desks_max (single value) or desks_min+desks_max
    (range — lower into desks_min, higher into desks_max, both populated together).
  - Put everything from the "+" onward verbatim into special_features (e.g. "+4 MR + 3 PB"),
    never into desks_max/desks_min. A range and trailing qualifiers can both be present on the
    same line — handle both at once (e.g. "48-72 + 5 MR" → desks_min=48, desks_max=72,
    special_features includes "+5 MR").
  - A plain single number with no "+" qualifier needs no special_features note for desks.
- sq ft: a single number → size_sqft (leave size_sqft_min/max null). A range → size_sqft_min/max
  (leave size_sqft null).
- price: this is the trickiest field — the SAME word "Price" can mean either a monthly rent
  figure or a per-square-foot rate depending on the document, and the unit is often NOT labeled
  in the text at all. Decide which one you're looking at like this:
  - If the text explicitly says "psf", "per sq ft", or the column header itself says
    "Price (psf)", it's a per-sqft rate → put it in rent_psf (or rent_psf_min/max if a range).
  - Otherwise, if a single "Price:" figure is given with NO psf/per-sq-ft wording anywhere near
    it, treat it as a monthly rent total → put it in rent_pcm. A useful sanity check: a monthly
    total is roughly proportional to desk count and sq ft (e.g. a few thousand to a few tens of
    thousands of pounds for a small office); a true per-sqft rate is a much smaller number
    (typically double or triple digits, e.g. £100-£350) that stays roughly constant regardless of
    unit size. If the figure is large relative to the unit's size, it's pcm, not psf.
  - Never calculate or estimate rent_pcm, rent_pcm_min, or rent_pcm_max yourself — if you've
    identified a directly-stated pcm figure, put it in rent_pcm; otherwise leave rent_pcm and
    rent_pcm_min/max null. They are computed downstream in code from size and psf when needed —
    but only when psf was the one directly stated, never the reverse.
  - A single-value field and its range counterpart should never both be populated for the same
    metric — pick whichever the source actually shows.
- special_features: the bullet-point features listed for that building (if any), semicolon-joined
  with the desk "+" qualifiers described above, any inline caveat near that specific unit (e.g.
  "Reduced pricing for a limited time only*"), and an availability note if one is given (e.g.
  "Av: Now" / "Available: Now", "Av: Sept" → "Available: September").
- state_of_space: the fit-out condition if stated (e.g. "Fitted", "Fully Managed"). If ambiguous
  or unstated for this particular unit, leave this null rather than guessing.
- brochure_link: only a clean, directly-readable URL if one is plainly present in the text for THIS
  SPECIFIC unit/listing — do NOT attempt to decode or reconstruct a URL from redirect/tracking
  parameters. If every link near this unit is an obfuscated tracking redirect, leave this null. Never
  take a link that belongs to one specific listing and reuse it for a different, unrelated unit — if
  the document has one shared portfolio-level link that clearly applies to the whole email (not to
  any one specific listing), use that for every unit instead. This must be a link to an actual
  brochure, floorplan, or listing-specific page — NEVER a generic company homepage, "contact us" page,
  or top-level marketing domain (e.g. "www.workspace.co.uk" on its own, as opposed to a specific
  property page under that domain). If the only link present is a generic company URL with no
  listing-specific path, leave this null rather than populating it with a non-brochure link.

Return your answer as a single JSON object with this exact structure:

{
  "provider": "..." or null,
  "contacts": "..." or null,
  "units": [
    {
      "building": "...",
      "submarket": "..." or null,
      "address_1": null,
      "postcode": null,
      "floor_unit": "..." or null,
      "size_sqft": number or null,
      "size_sqft_min": number or null,
      "size_sqft_max": number or null,
      "desks_max": integer or null,
      "desks_min": integer or null,
      "rent_pcm": number or null,
      "rent_psf": number or null,
      "rent_psf_min": number or null,
      "rent_psf_max": number or null,
      "brochure_link": "..." or null,
      "special_features": "..." or null,
      "state_of_space": "..." or null
    }
  ]
}

Return ONLY this JSON object. No preamble, no explanation, no markdown code fences — just the raw JSON.

Email body follows:

"""


def load_eml_body(eml_path: Path) -> str:
    with open(eml_path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)

    body = None
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            body = part.get_content()
            break
    if body is None:
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = part.get_content()
                body = re.sub(r"<[^>]+>", " ", html)
                break
    if body is None:
        raise ValueError(f"No text/plain or text/html body found in {eml_path}")

    return clean_email_text(body)


def clean_email_text(text: str) -> str:
    text = re.sub(r"<https?://\S+?>", "", text)
    lines = [
        line for line in text.splitlines()
        if not re.fullmatch(r"\[https?://\S+\]", line.strip())
    ]
    cleaned = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def extract(eml_path: Path) -> list[ListingRow]:
    client = get_client()
    body = load_eml_body(eml_path)
    raw = call_gemini(client, PROMPT, [body])

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
                    f"Warning: {eml_path.name} unit {i} has no building and no prior "
                    "unit to inherit one from — skipping this unit.",
                    file=sys.stderr,
                )
                continue
            unit["building"] = last_building
        last_building = unit["building"]

        if unit.get("brochure_link"):
            unit["brochure_link"] = resolve_brochure_link(unit["brochure_link"])

        fields = ExtractedFields(**brochure, **unit).model_dump()
        fields = compute_rent(fields)
        rows.append(
            ListingRow(
                **fields,
                lat=None,
                lng=None,
                source_file=eml_path.name,
            )
        )
    return rows


def main():
    if len(sys.argv) != 2:
        print("Usage: python extract_email.py <path_to_eml>", file=sys.stderr)
        raise SystemExit(1)

    eml_path = Path(sys.argv[1])
    if not eml_path.exists():
        raise SystemExit(f"File not found: {eml_path}")

    try:
        rows = extract(eml_path)
    except ValidationError as e:
        raise SystemExit(f"Gemini output did not match schema:\n{e}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"Gemini did not return valid JSON after retry:\n{e}")

    print(json.dumps([row.model_dump() for row in rows], indent=2))


if __name__ == "__main__":
    main()
