"""
geocode.py

Resolves lat/lng for a ListingRow using a two-tier strategy:
1. Primary: Google Geocoding API, when a full address is available.
2. Fallback: Google Places API (Text Search + Details), when only a building
   name is available (no address_1/postcode).

Also attempts to backfill address_1/postcode from the Places result when the
fallback path succeeds, since Places often returns a formatted address even
when the source document didn't state one.
"""

import json
import os
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


def _address_line1_and_postcode(address_components: list) -> tuple:
    street_number = None
    route = None
    postcode = None
    for comp in address_components:
        types = comp.get("types", [])
        if "street_number" in types:
            street_number = comp["longText"]
        elif "route" in types:
            route = comp["longText"]
        elif "postal_code" in types:
            postcode = comp["longText"]

    address_1 = None
    if street_number and route:
        address_1 = f"{street_number} {route}"
    elif route:
        address_1 = route

    return address_1, postcode


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
    # --- Tier 1: Geocoding API ---
    if row.address_1 and row.postcode:
        query = f"{row.address_1}, {row.postcode}, UK"
        result = call_geocoding_api(query)
        if result["status"] == "OK":
            row.lat = result["lat"]
            row.lng = result["lng"]
            return row
        # fall through to Places if Geocoding fails despite having an address

    # --- Tier 2: Places API Text Search (fallback) ---
    if row.building:
        query_parts = [row.building]
        if row.submarket:
            query_parts.append(row.submarket)
        query_parts.append("London, UK")
        query = ", ".join(query_parts)

        result = call_places_text_search(query)
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

            return row

        if result["status"] == "OK":
            log_geocode_failure(
                row, f"Places match outside London bounding box (lat={result['lat']}, lng={result['lng']})"
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
