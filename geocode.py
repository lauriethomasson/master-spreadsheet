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
"""

import json
import os
import re
import sys

import httpx

from env_utils import load_dotenv
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

    top = places[0]
    location = top.get("location", {})
    return {
        "status": "OK",
        "lat": location.get("latitude"),
        "lng": location.get("longitude"),
        "formatted_address": top.get("formattedAddress"),
        "address_components": top.get("addressComponents", []),
    }


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


def _backfill_submarket_from_coords(row: ListingRow, lat: float, lng: float) -> None:
    """
    Fills row.submarket from a reverse-geocode of coordinates already
    trusted (either source-provided or just resolved a few lines above) -
    never overwrites a genuinely-extracted value (the row.submarket guard
    below). Applies regardless of source type (spreadsheet/PDF/email),
    since geocode_row is the one shared code path for all of them - no
    per-source-type wiring needed. This is purely an additional read at
    coordinates already known, with no risk to lat/lng itself.
    """
    if row.submarket:
        return
    reverse = call_reverse_geocoding_api(lat, lng)
    if reverse["status"] == "OK":
        submarket = _submarket_from_components(reverse.get("address_components", []))
        if submarket:
            row.submarket = submarket


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
        # an in-bbox match, never as the first/preferred query.
        compound = split_compound_building(row.building)
        candidates = ([compound[1]] if compound else []) + [row.building]

        result = {"status": "ZERO_RESULTS"}
        for candidate in candidates:
            query_parts = [candidate]
            if row.submarket:
                query_parts.append(row.submarket)
            query_parts.append("London, UK")
            query = ", ".join(query_parts)

            result = call_places_text_search(query)
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
                    if not row.address_1 and address_1:
                        row.address_1 = address_1
                    if not row.postcode and postcode:
                        row.postcode = postcode
                    if not row.submarket:
                        submarket = _submarket_from_components(reverse.get("address_components", []))
                        if submarket:
                            row.submarket = submarket

                if not row.address_1 or not row.postcode:
                    log_geocode_failure(
                        row,
                        f"Places matched (lat={row.lat}, lng={row.lng}) but no street "
                        "address/postcode found there, even after a reverse-geocode fallback",
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


def geocode_rows(rows: list) -> list:
    for row in rows:
        geocode_row(row)
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
