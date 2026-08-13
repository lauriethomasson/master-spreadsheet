"""
geocode.py

Resolves lat/lng for a ListingRow using a two-tier strategy:
1. Primary: Google Geocoding API, when a full address is available.
2. Fallback: Google Places API (Text Search + Details), when only a building
   name is available (no address_1/postcode).

Also attempts to backfill address_1/postcode from the Places result when the
fallback path succeeds, since Places often returns a formatted address even
when the source document didn't state one. When Places resolves coordinates
but its own record has no street_number/route/postal_code component at all
(seen for some buildings, e.g. "Kent House" — a valid, correctly-disambiguated
match with no structured address on file), falls back to reverse-geocoding
those coordinates through the legacy Geocoding API, which often has address
data for a location that Places' own record lacks.

Separately, also backfills submarket (never overwriting a genuinely-extracted
value) once coordinates are known from any source - source-provided,
Geocoding API, or Places - by reverse-geocoding those coordinates and reading
a "sublocality"/"sublocality_level_1" component (Google's equivalent of a
London neighbourhood name, e.g. "Fitzrovia", "Soho", "Mayfair"). Confirmed
necessary against real UNION rows missing submarket: neither the Geocoding
API nor Places' own forward-lookup result ever carries anything more specific
than postal_town="London" — the neighbourhood-level component only shows up
in a reverse-geocode's pooled results, never a forward address/Places lookup's
own top result. See _submarket_from_components/_backfill_submarket_from_coords.

When `building` is itself a compound "Name, Street Address" value (e.g.
"Bridge House, 22 Newman Street" - a real Kitt's Availability building),
the Places query tries the address portion alone FIRST, falling back to the
full compound value only if that fails - see split_compound_building's own
docstring for why: appending submarket + "London, UK" to an already-
compound value gives the matcher two competing location signals (a building
name shared by several real buildings across London, plus a specific-but-
different street), confirmed against the real API to sometimes return zero
results entirely and sometimes a confident but genuinely wrong match
hundreds of meters to over a kilometer away - silently, since a wrong-but-
plausible match has no error signal at all, unlike a zero-results failure.

Before accepting any Tier 2 candidate, also cross-checks it against a UK
postcode-district hint parsed out of whatever location evidence the SOURCE
already provided (row.postcode, row.address_1, or a trailing token on
row.building - see _source_location_hint/extract_postcode_hint) and rejects
a candidate that contradicts it, leaving the row's fields blank rather than
writing a confident-but-wrong result. within_london_bbox alone only rejects
a match outside Greater London entirely - it cannot and was never meant to
catch a wrong-but-plausible match to a DIFFERENT real place within that same
box. Confirmed real failure this exists for: a real beem Live Flex
Availability.xlsx row's own building text ("New Derwent House WC1") was
geocoded via Places Text Search to "25 Savile Row, London W1S 2ER" - a
different postcode area entirely, still comfortably inside the bbox. The
parsing is purely the UK postcode's own outward/inward code grammar, never a
hardcoded list of specific areas, buildings, or providers - the same check
applies identically to any other UK postcode district a future file states.
"""

import json
import os
import re
import sys

import httpx

from env_utils import load_dotenv
from master_merge import normalize_key
from schema import ListingRow

load_dotenv()

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Places API (New) — a distinct product/enablement from the legacy Places API.
# Text Search (New) can return address components directly in one call, given
# the right field mask, so no separate Place Details lookup is needed.
PLACES_NEW_SEARCHTEXT_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_NEW_FIELD_MASK = "places.formattedAddress,places.location,places.addressComponents"

PLACES_ENABLE_URL = "https://console.cloud.google.com/apis/library/places.googleapis.com"

# Greater London bounding box — reject any Places match outside this as
# low-confidence, since every source document in this pipeline is London-based.
LONDON_BBOX = {"lat_min": 51.28, "lat_max": 51.70, "lng_min": -0.51, "lng_max": 0.34}


def _api_key() -> str:
    key = os.environ.get("GOOGLE_GEOCODING_API_KEY")
    if not key:
        raise SystemExit("Set GOOGLE_GEOCODING_API_KEY in the environment before running.")
    return key


def _check_not_enabled(status: str, error_message: str, api_name: str):
    if status == "REQUEST_DENIED" and (
        "not activated" in error_message.lower() or "not authorized" in error_message.lower()
    ):
        raise RuntimeError(
            f"{api_name} is not enabled for this Google Cloud project. "
            f"Enable it here, then retry: {PLACES_ENABLE_URL}"
        )


def within_london_bbox(lat: float, lng: float) -> bool:
    return (
        LONDON_BBOX["lat_min"] <= lat <= LONDON_BBOX["lat_max"]
        and LONDON_BBOX["lng_min"] <= lng <= LONDON_BBOX["lng_max"]
    )


# --- Generic UK postcode-district evidence -----------------------------
#
# within_london_bbox is a coarse ~30x50km rectangle covering the whole of
# Greater London - necessary (rejects a match in a different city entirely)
# but not sufficient to catch a wrong-but-plausible match to a DIFFERENT
# real place within that same box (confirmed real case: source building
# text "New Derwent House WC1" resolved via Places Text Search to a
# same/similarly-named place at "25 Savile Row, London W1S 2ER" - a
# different postcode AREA, nowhere near WC1, still comfortably inside the
# bbox). The functions below parse a UK postcode's own "outward code"
# grammar (1-2 area letters + 1-2 district digits + an optional finer-
# subdivision letter, e.g. "SE1", "EC1V", "W1S") to pull a location hint out
# of whatever free text the SOURCE already provided (row.postcode,
# row.address_1, or a trailing token on row.building - see
# _source_location_hint) and use it to reject a geocoder candidate that
# contradicts it. This is the country-wide postal FORMAT, never a lookup of
# specific area/place names - the same check works identically for a future
# file naming any other UK postcode district, with nothing London-specific
# or provider-specific hardcoded.
_OUTWARD_CODE_RE = re.compile(r"^([A-Za-z]{1,2})(\d{1,2})([A-Za-z]?)$")
_INWARD_CODE_RE = re.compile(r"^\d[A-Za-z]{2}$")


def _district_parts(outward_code: str):
    """
    (area, district_number) for an outward code, ignoring its optional
    finer-subdivision letter - "EC1A" and "EC1" both -> ("EC", "1"), since
    that letter marks a subdivision WITHIN the same numbered district, not a
    genuinely different one. Comparing on this tuple (rather than the raw
    string) avoids a false conflict between a source's plain "EC1" hint and
    a candidate's more specific "EC1A", while still catching a genuine
    disagreement in area letters or district number (WC1 vs W1S, SE1 vs
    EC1V, WC1 vs SW7 - the exact shape of every confirmed real/illustrative
    failure this exists for).
    """
    match = _OUTWARD_CODE_RE.match(outward_code.upper())
    if not match:
        return None
    return match.group(1), match.group(2)


def extract_postcode_hint(text: str):
    """
    Parses a UK postcode-district hint from the trailing token(s) of free
    text ("New Derwent House WC1" -> district ("WC", "1"); "22 Newman
    Street W1T 4PX" -> full postcode plus district ("W1", "T")->("W","1")),
    purely via the outward/inward code grammar - never a lookup of specific
    area/place names, so this works identically for any UK postcode.
    Returns None when the trailing token doesn't structurally look like a
    postcode/district at all, which is the common case for a plain building
    name with nothing appended.

    Deliberately only looks at the trailing token(s), never scans the whole
    string: every real example confirmed against actual provider data
    (beem Live Flex Availability.xlsx) states its postcode-district as the
    very last word of the building/address text, and anchoring there avoids
    a false hit on some unrelated mid-string token that happens to fit the
    same short letter+digit pattern.
    """
    if not text:
        return None
    tokens = [t for t in re.split(r"\s+", text.strip()) if t]
    if not tokens:
        return None

    last = tokens[-1]
    if len(tokens) >= 2 and _INWARD_CODE_RE.match(last):
        district = _district_parts(tokens[-2])
        if district:
            return {"full": f"{tokens[-2].upper()} {last.upper()}", "district": district}

    district = _district_parts(last)
    if district:
        return {"full": None, "district": district}
    return None


_LEADING_DIGIT_RE = re.compile(r"^\d")


def extract_area_hint(text: str):
    """
    A safe, conservative source-area/locality hint (e.g. "London Bridge")
    immediately adjacent to a postcode hint (see extract_postcode_hint) -
    trusted ONLY when the source text itself already marks the boundary
    between building identity and locality with a real separator already
    established elsewhere in this codebase (a newline - the real, confirmed
    Beem convention, see extract_postcode_hint's own "Clove \\nLondon Bridge
    SE1" example - or a comma, the same separator split_compound_building
    already trusts for "Name, Address"), never by guessing where an
    unbroken run of words splits. Returns None for a single unbroken line
    with no such separator (e.g. "New Derwent House WC1") - there is no
    safe, generic way to tell where a building name ends and a locality
    begins in that shape, and guessing would risk exactly the "ambiguous
    free text becomes a guessed submarket" failure this must avoid. Never a
    hardcoded gazetteer of place names, and never provider-specific.

    Splits `text` into segments on every newline/comma, then looks at
    whichever segment the postcode hint (see extract_postcode_hint) is
    found in:
    - if that segment has leftover text once the hint's own trailing
      token(s) are removed (e.g. "London Bridge SE1" -> "London Bridge"),
      that leftover IS the area - unless it starts with a digit (a street
      address, e.g. "22 Newman Street WC1" -> "22 Newman Street", never
      mistaken for a locality name);
    - if the hint's own segment is nothing BUT the postcode itself (e.g.
      "Nutmeg House, London Bridge, SE1"), the PRECEDING segment is the
      area - again unless it starts with a digit.
    """
    if not text:
        return None
    segments = [s.strip() for s in re.split(r"[\n,]", text) if s.strip()]
    if len(segments) < 2:
        return None  # no real separator present at all - too ambiguous to safely split

    hint = extract_postcode_hint(segments[-1])
    if not hint:
        return None  # the postcode hint isn't where this parser expects it - stay conservative, no guess

    tokens = [t for t in re.split(r"\s+", segments[-1]) if t]
    consumed = 2 if hint["full"] else 1
    leftover_tokens = tokens[:-consumed]

    if leftover_tokens:
        if _LEADING_DIGIT_RE.match(leftover_tokens[0]):
            return None  # looks like a street address, not a locality name
        return " ".join(leftover_tokens)

    candidate = segments[-2]
    if _LEADING_DIGIT_RE.match(candidate):
        return None  # looks like a street address, not a locality name
    return candidate


def _source_location_hint(row: ListingRow):
    """
    The strongest postcode-district hint already stated by the SOURCE,
    checked in the same priority this module's own tiering already trusts
    (an explicit row.postcode outranks one merely embedded in address_1/
    building text) - row.postcode/address_1 are trusted verbatim; building
    is checked last since it's only ever a byproduct of a provider's own
    naming convention (e.g. Beem's "Property" column), not a dedicated
    location field. Returns None when nothing postcode-shaped is found
    anywhere, which leaves geocode_row's own acceptance check exactly as
    permissive as it always was for a row with no such evidence at all -
    this is additive validation, never a new requirement to have evidence.
    """
    for text in (row.postcode, row.address_1, row.building):
        hint = extract_postcode_hint(text)
        if hint:
            return hint
    return None


def _source_area_hint(row: ListingRow):
    """
    extract_area_hint's own row-level counterpart to _source_location_hint,
    checked in the same field priority (row.postcode, then address_1, then
    building) - used both to backfill submarket (see _fill_submarket) and as
    an extra Places query-disambiguation variant (see geocode_row's own
    Tier 2). Returns None when no field has a safely-separated locality hint
    to offer, same as _source_location_hint's own "nothing found" case.
    """
    for text in (row.postcode, row.address_1, row.building):
        area = extract_area_hint(text)
        if area:
            return area
    return None


def _postcode_hint_conflicts(source_hint: dict, candidate_postcode: str) -> bool:
    """
    True only when both sides parse to a district and genuinely disagree
    (see _district_parts) - never true when either side has nothing to
    compare. An absent/unparseable candidate postcode is a reason to accept
    on trust (no evidence either way), not a reason to reject - only an
    actual, parseable contradiction between two real signals counts.
    """
    if not source_hint or not candidate_postcode:
        return False
    candidate_hint = extract_postcode_hint(candidate_postcode)
    if not candidate_hint:
        return False
    return source_hint["district"] != candidate_hint["district"]


def _hint_label(hint: dict) -> str:
    """Human-readable form of a hint dict for a log/failure message - the
    full postcode when known, else just the district's own two parts
    stitched back together (("WC", "1") -> "WC1")."""
    return hint["full"] or "".join(hint["district"])


def call_geocoding_api(address: str) -> dict:
    resp = httpx.get(GEOCODE_URL, params={"address": address, "key": _api_key()}, timeout=10)
    data = resp.json()
    status = data.get("status", "UNKNOWN")
    _check_not_enabled(status, data.get("error_message", ""), "Geocoding API")

    if status != "OK" or not data.get("results"):
        return {"status": status}

    location = data["results"][0]["geometry"]["location"]
    return {"status": "OK", "lat": location["lat"], "lng": location["lng"]}


def call_reverse_geocoding_api(lat: float, lng: float) -> dict:
    """
    Looks up address data for a known coordinate (the reverse of
    call_geocoding_api) via the same legacy Geocoding API endpoint, just with
    a latlng param instead of an address. A reverse lookup typically returns
    several results at different specificity levels (street address, postal
    code area, neighborhood, ...) rather than one - address_components from
    all of them are pooled together, since the most specific result isn't
    always the one that happens to carry the postal_code component.
    """
    resp = httpx.get(GEOCODE_URL, params={"latlng": f"{lat},{lng}", "key": _api_key()}, timeout=10)
    data = resp.json()
    status = data.get("status", "UNKNOWN")
    _check_not_enabled(status, data.get("error_message", ""), "Geocoding API")

    if status != "OK" or not data.get("results"):
        return {"status": status}

    address_components = []
    for result in data["results"]:
        address_components.extend(result.get("address_components", []))

    return {"status": "OK", "address_components": address_components}


def call_places_text_search(query: str) -> dict:
    resp = httpx.post(
        PLACES_NEW_SEARCHTEXT_URL,
        json={"textQuery": query},
        headers={
            "X-Goog-Api-Key": _api_key(),
            "X-Goog-FieldMask": PLACES_NEW_FIELD_MASK,
        },
        timeout=10,
    )
    data = resp.json()

    if resp.status_code != 200:
        error = data.get("error", {})
        message = error.get("message", "")
        if resp.status_code in (400, 403) and (
            "not been used" in message.lower()
            or "disabled" in message.lower()
            or "it is disabled" in message.lower()
        ):
            raise RuntimeError(
                f"Places API (New) is not enabled for this Google Cloud project. "
                f"Enable it here, then retry: {PLACES_ENABLE_URL}"
            )
        return {"status": "ERROR", "message": message}

    places = data.get("places", [])
    if not places:
        return {"status": "ZERO_RESULTS"}

    candidates = []
    for place in places:
        location = place.get("location", {})
        candidates.append({
            "lat": location.get("latitude"),
            "lng": location.get("longitude"),
            "formatted_address": place.get("formattedAddress"),
            "address_components": place.get("addressComponents", []),
        })

    # "candidates" is additive - every existing caller/test that only reads
    # the flattened top-level lat/lng/formatted_address/address_components
    # (unpacked from candidates[0] below) keeps working completely
    # unchanged; only a caller that actually wants to consider more than the
    # top result (see _best_places_result) reads "candidates" itself.
    return {"status": "OK", "candidates": candidates, **candidates[0]}


def _address_line1_and_postcode(address_components: list, name_key: str = "longText") -> tuple:
    """
    name_key is "longText" for Places API (New) components (the shape
    call_places_text_search returns) or "long_name" for legacy Geocoding API
    components (call_reverse_geocoding_api). Guards against overwriting an
    already-found value, since call_reverse_geocoding_api pools components
    from several results and an earlier, more specific result should win.
    """
    street_number = None
    route = None
    postcode = None
    for comp in address_components:
        types = comp.get("types", [])
        if "street_number" in types and not street_number:
            street_number = comp[name_key]
        elif "route" in types and not route:
            route = comp[name_key]
        elif "postal_code" in types and not postcode:
            postcode = comp[name_key]

    address_1 = None
    if street_number and route:
        address_1 = f"{street_number} {route}"
    elif route:
        address_1 = route

    return address_1, postcode


def _submarket_from_components(address_components: list, name_key: str = "long_name") -> str:
    """
    A political "sublocality"/"sublocality_level_1" component is Google's
    equivalent of a London neighbourhood name (e.g. "Fitzrovia", "Soho",
    "Mayfair") - confirmed against real UNION rows previously missing
    submarket (addresses on Dering Street, Mortimer Street, Wardour Street
    each reverse-geocode to exactly the neighbourhood a Londoner would
    expect, not just "London"). "neighborhood" is checked too as a fallback
    type, though it never appeared in any real response seen so far.

    This never appears in a forward Geocoding/Places lookup's own top
    (most specific, street-level) result - confirmed against the same real
    addresses, whose top result only ever carries postal_town="London".
    It only surfaces via a reverse-geocode of the resolved coordinates,
    which returns several results at different specificity levels and pools
    every result's components together (see call_reverse_geocoding_api)
    rather than just the first - that's why this is only ever called with
    reverse-geocode output, never a forward lookup's.
    """
    for comp in address_components:
        types = comp.get("types", [])
        if "sublocality" in types or "neighborhood" in types:
            return comp[name_key]
    return None


def _fill_submarket(row: ListingRow, address_components: list = None) -> None:
    """
    Fills row.submarket using this module's own priority order: (1) never
    overwrites a genuinely-extracted value (the row.submarket guard below -
    unchanged from before); (2) a safe source-text locality hint (see
    _source_area_hint/extract_area_hint) - preferred over Google's own
    reverse-geocoded neighbourhood since the source already said so
    explicitly, and confirmed real gap: Google's sublocality/neighborhood
    coverage is patchy (reliable for Mayfair/Fitzrovia/Soho, but genuinely
    blank for other real addresses this exists for), so a source that
    already states its own locality must never stay blank just because
    Google's reverse-geocode happens not to cover it; (3) address_components
    already fetched from a reverse-geocode (see _submarket_from_components) -
    checked only when the source itself had nothing safe to offer, and only
    if the caller already has a reverse-geocode result in hand (never
    fetches one itself - see _backfill_submarket_from_coords, the only
    caller that decides whether a reverse-geocode call is worth making at
    all). Idempotent/safe to call more than once for the same row - every
    branch is a no-op once row.submarket is set.
    """
    if row.submarket:
        return
    area_hint = _source_area_hint(row)
    if area_hint:
        row.submarket = area_hint
        return
    if address_components:
        submarket = _submarket_from_components(address_components)
        if submarket:
            row.submarket = submarket


def _backfill_submarket_from_coords(row: ListingRow, lat: float, lng: float) -> None:
    """
    Fills row.submarket for coordinates already trusted (either source-
    provided or just resolved a few lines above) - see _fill_submarket for
    the actual priority order. Only calls out to the reverse-geocode API
    when a safe source-text hint isn't already enough, saving the call
    entirely for a row whose source text already states its own locality.
    Applies regardless of source type (spreadsheet/PDF/email), since
    geocode_row is the one shared code path for all of them - no per-
    source-type wiring needed. This is purely an additional read at
    coordinates already known, with no risk to lat/lng itself.
    """
    if row.submarket:
        return
    _fill_submarket(row)
    if row.submarket:
        return
    reverse = call_reverse_geocoding_api(lat, lng)
    if reverse["status"] == "OK":
        _fill_submarket(row, reverse.get("address_components", []))


def split_compound_building(building: str) -> tuple:
    """
    Returns (name_part, address_part) if `building` looks like a "Name,
    Street Address" or "Name - Street Address" compound value - the part
    before the separator has no digit (name-like), the part after has one
    (address-like, a house/building number). None otherwise - a plain
    building name ("Kent House") or a plain address ("28 Bruton Street")
    with no competing name token is unaffected either way.

    Confirmed against every real compound building value in the actual
    Kitt's Availability file: address_part alone resolved correctly via
    the Places API in every case tested (Bridge House/22 Newman Street,
    Imperial House/8 Kean Street, Whitfield Court/30-32 Whitfield Street,
    and 6 others that already matched correctly even with the full
    compound value) - see geocode_row's own docstring for why the full
    compound value is unreliable.
    """
    for sep in (", ", " - "):
        if sep not in building:
            continue
        name_part, _, address_part = building.partition(sep)
        name_part, address_part = name_part.strip(), address_part.strip()
        if name_part and address_part and not re.search(r"\d", name_part) and re.search(r"\d", address_part):
            return name_part, address_part
    return None


FAILURES = []


def log_geocode_failure(row: ListingRow, reason: str):
    entry = {
        "building": row.building,
        "floor_unit": row.floor_unit,
        "submarket": row.submarket,
        "source_file": row.source_file,
        "reason": reason,
    }
    FAILURES.append(entry)
    print(f"[geocode] FAILED: {row.building!r} ({row.source_file}) — {reason}", file=sys.stderr)


def _best_places_result(query: str, source_hint: dict) -> dict:
    """
    Sends ONE query to Places Text Search and returns the first candidate
    (see call_places_text_search's own "candidates" list, not just its
    top result) that passes bbox + source-postcode-evidence validation -
    confirmed real gap this closes: call_places_text_search previously
    exposed only the top Places result, so a source with strong location
    evidence (e.g. "SE1") had no way to fall through to a DIFFERENT
    candidate the same search already returned, even when that candidate
    was sitting right there in the same response.

    If every candidate this query returns fails validation, returns the
    LAST one's own conflict/failure info (for log_geocode_failure's own
    message) - never a candidate this function itself hasn't checked, and
    never blindly "the next one" without the exact same bbox/postcode
    checks geocode_row's own single-candidate path always applied.
    """
    search = call_places_text_search(query)
    if search["status"] != "OK":
        return search

    candidates = search.get("candidates") or [search]
    last = search
    for place in candidates:
        if not within_london_bbox(place.get("lat"), place.get("lng")):
            last = {**place, "status": "OK"}
            continue
        _, candidate_postcode = _address_line1_and_postcode(place.get("address_components", []))
        if _postcode_hint_conflicts(source_hint, candidate_postcode):
            last = {**place, "status": "LOCATION_CONFLICT", "postcode": candidate_postcode}
            continue
        return {**place, "status": "OK"}
    return last


def _fallback_query_texts(row: ListingRow, source_hint: dict) -> list:
    """
    Additional building-text variants to try in Places Text Search if every
    candidate from the existing compound-address/full-building attempts
    (see geocode_row's own Tier 2) still conflicts or fails - built from the
    same source evidence source_hint/_source_area_hint already extract,
    phrased as its OWN explicit, comma-separated segment rather than left
    mashed into the free-text building value (e.g. "New Derwent House, WC1"
    instead of relying on "New Derwent House WC1" as one run-on string) -
    a differently-phrased query can lead Places to surface a different (and
    possibly correct) candidate for the exact same real building, per this
    module's own priority order: postcode/district hint before area/
    submarket text, building name alone last (already covered by the
    caller's own existing candidates).

    Never replaces the existing attempts; only ever appended after them,
    and only actually queried at all if they didn't already resolve - see
    geocode_row's own loop, which stops at the first accepted candidate
    regardless of how many variants exist here.
    """
    variants = []
    if source_hint:
        variants.append(f"{row.building}, {_hint_label(source_hint)}")
    area_hint = _source_area_hint(row)
    if area_hint:
        variants.append(f"{row.building}, {area_hint}")
    return variants


def geocode_row(row: ListingRow) -> ListingRow:
    # Already has real coordinates (e.g. a provider spreadsheet's own Lat/Lng
    # columns, mapped straight through by extract_spreadsheet.py) - calling
    # out to the API would be a wasted lookup at best, and at worst replaces
    # a correct source-provided coordinate with a worse guess.
    if row.lat is not None and row.lng is not None:
        _backfill_submarket_from_coords(row, row.lat, row.lng)
        return row

    # --- Tier 1: Geocoding API ---
    if row.address_1 and row.postcode:
        query = f"{row.address_1}, {row.postcode}, UK"
        result = call_geocoding_api(query)
        if result["status"] == "OK":
            row.lat = result["lat"]
            row.lng = result["lng"]
            _backfill_submarket_from_coords(row, row.lat, row.lng)
            return row
        # fall through to Places if Geocoding fails despite having an address

    # --- Tier 2: Places API Text Search (fallback) ---
    if row.building:
        # A compound building value tries its address portion alone
        # FIRST (see split_compound_building/this module's own
        # docstring) - the full building value is always tried too, but
        # only as a fallback if the address-only attempt doesn't produce
        # an in-bbox, non-conflicting match, never as the first/preferred
        # query.
        source_hint = _source_location_hint(row)
        compound = split_compound_building(row.building)
        base_candidates = ([compound[1]] if compound else []) + [row.building]
        query_variants = []
        for candidate in base_candidates + _fallback_query_texts(row, source_hint):
            if candidate not in query_variants:
                query_variants.append(candidate)

        # Validate BEFORE accepting - never write a candidate's lat/lng and
        # then discover the conflict afterwards (see this module's own
        # docstring on the confirmed "New Derwent House WC1" -> "25 Savile
        # Row W1S 2ER" real failure). A candidate with no postal_code
        # component at all has nothing to check against, so it's accepted
        # on trust the same as before this validation existed - only a
        # genuine, parseable contradiction rejects it. Every candidate a
        # given query itself returns is checked (see _best_places_result),
        # not just that query's own top result; if all of them conflict or
        # fail, the NEXT query_variant (a differently-phrased query built
        # from stronger source evidence - see _fallback_query_texts) is
        # tried before giving up - never "candidate 1 conflicts -> blank"
        # while a safer variant remains untried.
        result = {"status": "ZERO_RESULTS"}
        for candidate in query_variants:
            query_parts = [candidate]
            if row.submarket:
                query_parts.append(row.submarket)
            query_parts.append("London, UK")
            query = ", ".join(query_parts)

            result = _best_places_result(query, source_hint)
            if result["status"] == "OK" and within_london_bbox(result.get("lat"), result.get("lng")):
                break

        if result["status"] == "OK" and within_london_bbox(result["lat"], result["lng"]):
            row.lat = result["lat"]
            row.lng = result["lng"]

            if not row.address_1 or not row.postcode:
                address_1, postcode = _address_line1_and_postcode(
                    result.get("address_components", [])
                )
                if not row.address_1 and address_1:
                    row.address_1 = address_1
                if not row.postcode and postcode:
                    row.postcode = postcode

            # A safe source-text locality hint (see _fill_submarket) never
            # needs an API call - tried first so a row whose address_1/
            # postcode are already resolved (from the candidate above) can
            # skip the reverse-geocode call below entirely once this alone
            # is enough.
            _fill_submarket(row)

            if not row.address_1 or not row.postcode or not row.submarket:
                # Places matched real coordinates but its own record is missing
                # something (no street address on file at all - e.g. Kent House, a
                # correct, well-disambiguated match with a "premise"-only record -
                # and/or no submarket, see _submarket_from_components) -
                # reverse-geocode the coordinates we already trust as a second
                # attempt to fill in whichever of these is still missing. One call
                # covers both, since both read the exact same pooled components.
                reverse = call_reverse_geocoding_api(row.lat, row.lng)
                if reverse["status"] == "OK":
                    address_1, postcode = _address_line1_and_postcode(
                        reverse.get("address_components", []), name_key="long_name"
                    )
                    # Same validation as the forward-search candidate above -
                    # a reverse-geocode of an already-accepted coordinate can
                    # still surface a postcode that contradicts the source's
                    # own evidence (the accepted candidate had no postal_code
                    # component of its own to check at accept time). Leave
                    # address_1/postcode blank rather than write a
                    # confident-but-wrong value; lat/lng are left as-is since
                    # they already passed the bbox/no-evidence-to-contradict
                    # check above.
                    if postcode and _postcode_hint_conflicts(source_hint, postcode):
                        log_geocode_failure(
                            row,
                            f"reverse-geocode postcode {postcode!r} contradicts source location "
                            f"evidence ({_hint_label(source_hint)!r}) - address_1/postcode left blank",
                        )
                    else:
                        if not row.address_1 and address_1:
                            row.address_1 = address_1
                        if not row.postcode and postcode:
                            row.postcode = postcode
                    _fill_submarket(row, reverse.get("address_components", []))

                if not row.address_1 or not row.postcode:
                    log_geocode_failure(
                        row,
                        f"Places matched (lat={row.lat}, lng={row.lng}) but no street "
                        "address/postcode found there, even after a reverse-geocode fallback",
                    )

            return row

        if result["status"] == "LOCATION_CONFLICT":
            log_geocode_failure(
                row,
                f"Places candidate (postcode={result.get('postcode')!r}, lat={result.get('lat')}, "
                f"lng={result.get('lng')}) contradicts the source's own location evidence "
                f"({_hint_label(source_hint)!r}) - rejected rather than accepted on a weak "
                "building-name-only match",
            )
            return row

        if result["status"] == "OK":
            log_geocode_failure(
                row,
                f"Places match outside London bounding box (lat={result['lat']}, lng={result['lng']})",
            )
            return row

    # --- Neither worked ---
    log_geocode_failure(row, "no match from Geocoding or Places API")
    return row


def _physical_identity_key(row: ListingRow):
    """
    (building, provider) as an exact, normalize_key-tolerant tuple, or None
    if building is blank - the SAME identity tuple master_merge.py's own
    _fallback_key already trusts elsewhere in this pipeline as sufficient
    evidence two rows describe the same real building (never a fuzzy/
    similarity match - see master_merge.BUILDING_FUZZY_MATCH_THRESHOLD's
    own docstring on why a numbered address is never fuzzy-matched at all).
    Reused here, not a new/looser trust level, specifically so a physical
    location fact (lat/lng/address_1/postcode - which is true of the whole
    BUILDING, never just one floor of it) is resolved once per building
    rather than once per row.
    """
    building_key = normalize_key(row.building)
    if not building_key:
        return None
    return building_key, normalize_key(row.provider)


def geocode_rows(rows: list) -> list:
    """
    Resolves lat/lng (and backfills address_1/postcode/submarket where
    blank) for every row, grouping rows that share the same (building,
    provider) identity (see _physical_identity_key) so the group is
    geocoded ONCE and every member's own blank location fields copy that
    ONE result, rather than each row independently calling geocode_row and
    risking a different answer for the same real building.

    This exists because geocode_row's own Tier 2 Places query includes
    row.submarket as a disambiguation hint (see its own docstring) - two
    rows for the exact same building, listed under two different genuine
    source areas/submarkets (see master_merge._group_unmatched_duplicates'
    own source-row-identity handling - those two rows are deliberately kept
    as separate master rows, never merged, since that's a question of
    listing identity, not physical identity), can each bias the SAME
    Places query differently and come back with two different addresses/
    postcodes/coordinates for one real building (the confirmed real Nexus
    Place case: EC4M 4AB vs EC1M 3HA). Grouping first makes the two
    questions independent: which/how-many LISTING rows exist is decided
    entirely by source-row identity (submarket included), while WHERE that
    building actually is gets one consistent, correctly-resolved answer
    shared by every row naming it - never two rows silently disagreeing
    about their own physical location metadata.

    Only ever fills a row's OWN blank fields (identical guarantee to
    geocode_row's own field-by-field rule) - a row that already has its own
    lat/lng, or its own address_1+postcode, is never touched by another
    group member's result; if one IS already fully resolved, its result
    becomes the group's shared answer instead of a fresh API call. The
    representative actually sent through geocode_row is whichever member
    has the most identifying information already (address_1+postcode, else
    just address_1, else neither), preferring an existing head start over
    an arbitrary first-in-batch pick - submarket is deliberately excluded
    from that preference and never touched by this copy-down at all, since
    it's the one field this whole grouping exists to keep row-specific.
    """
    groups = {}
    ungrouped = []
    for row in rows:
        key = _physical_identity_key(row)
        if key is None:
            ungrouped.append(row)
        else:
            groups.setdefault(key, []).append(row)

    for row in ungrouped:
        geocode_row(row)

    def _completeness(row):
        if row.address_1 and row.postcode:
            return 2
        if row.address_1:
            return 1
        return 0

    for members in groups.values():
        if len(members) == 1:
            geocode_row(members[0])
            continue

        already_resolved = next((r for r in members if r.lat is not None and r.lng is not None), None)
        representative = already_resolved or max(members, key=_completeness)
        geocode_row(representative)

        if representative.lat is None or representative.lng is None:
            # The group's one shared attempt found nothing - falls back to
            # each OTHER member trying independently (the pre-grouping
            # behavior) rather than letting one failed lookup silently fail
            # the whole group; a different member's own address/building
            # text is a genuinely different query that might still resolve
            # even though the representative's own didn't.
            for row in members:
                if row is not representative:
                    geocode_row(row)
            continue

        for row in members:
            if row is representative:
                continue
            if row.lat is None and row.lng is None:
                row.lat = representative.lat
                row.lng = representative.lng
            if not row.address_1 and representative.address_1:
                row.address_1 = representative.address_1
            if not row.postcode and representative.postcode:
                row.postcode = representative.postcode
            # submarket is NEVER copied here - see this function's own
            # docstring on why that field alone stays row-specific.
            if row.lat is not None and row.lng is not None:
                _backfill_submarket_from_coords(row, row.lat, row.lng)

    return rows


def main():
    if len(sys.argv) != 2:
        print("Usage: python geocode.py <rows.json>", file=sys.stderr)
        raise SystemExit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        raw_rows = json.load(f)

    rows = [ListingRow(**r) for r in raw_rows]
    geocode_rows(rows)

    print(json.dumps([row.model_dump() for row in rows], indent=2))
    if FAILURES:
        print(f"\n{len(FAILURES)} row(s) failed to geocode:", file=sys.stderr)
        for f_ in FAILURES:
            print(f"  - {f_}", file=sys.stderr)


if __name__ == "__main__":
    main()
