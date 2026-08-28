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

The postcode-district cross-check above needs a source hint to check
AGAINST - when there is none at all (a Tier 2 row with no address_1/
postcode/building-trailing-token evidence whatsoever), an accepted
candidate is flagged row.geocode_unverified rather than trusted like any
other result, since a building-name-only match has no independent
evidence it resolved to the RIGHT same-named building at all. Confirmed
real failures this exists for: "Henly House" (not indexed under that
spelling by Places) and "Ivybridge House" (resolves to a stale/mislabeled
POI) - genuine Google Places data problems no query rephrasing or name-
similarity check can fix, since the returned candidate is itself wrong.
See schema.ListingRow.geocode_unverified's own docstring for how this is
surfaced to a reviewer. Tier 1 and a hint-corroborated Tier 2 match both
set this explicitly False (never leave it at None) - real evidence, so
either one also self-corrects a stale True a prior upload's own zero-hint
fallback left on this same row, rather than that warning sitting there
forever with no way to clear (confirmed real gap this closes - see
schema.ListingRow.geocode_unverified's own docstring on why False and
None are deliberately different values here).

A row whose OWN building text states a genuine street address (a leading
house number - see leading_house_number/_street_name_words) gets one more
check the zero-hint case above still can't cover: the accepted candidate's
own returned street (its "route" address component) must share at least
one significant word with the SOURCE's own stated street name, or it's
rejected as a STREET_CONFLICT (see _best_places_result) rather than
accepted with just a geocode_unverified flag. Confirmed real failure this
exists for: a real Kitt's "44 Paul Street" (Shoreditch, EC2A) - with no
address_1/postcode of its own, so nothing for the postcode-district check
above to compare against - resolved via Places Text Search to a genuinely
different, unrelated street (Little Britain, EC1A). Deliberately scoped to
an address-SHAPED building value only - a bare building NAME ("Kent
House", "Canal Building") has no street of its own to compare in the
first place, and comparing its own name text against a returned route
would false-positive on the ordinary case, where a building's name is
routinely nothing like the street it actually sits on.

A candidate with NO route component of its own at all (only e.g.
street_number/postal_code) used to slip straight past the STREET_CONFLICT
check above with nothing to compare - the exact confirmed real shape of
the "44 Paul Street" incident above, live-traced after the fact: the
route-less candidate itself was accepted at real coordinates ~1km away,
and a LATER, separate reverse-geocode of those now-trusted-but-wrong
coordinates is what actually produced the "20 Little Britain, EC1A 7DH"
address a reviewer saw - the STREET_CONFLICT check itself never even saw
that text. Now either accepted on the candidate's own postcode DISTRICT
alone (weaker evidence than a genuine street match, but real - flagged
geocode_unverified and recorded via log_geocode_weak_match/WEAK_MATCHES,
so a repeat of this is never invisible again) or, with nothing at all to
corroborate against, rejected outright as STREET_UNVERIFIABLE - same
"can't verify, don't guess" philosophy as STREET_CONFLICT.

row.development_name (see schema.ListingRow's own docstring) is tried as
an extra Tier 2 query disambiguator, ahead of submarket - a real London
office campus often brands an overall development name distinct from
each building's own (often generic) name inside it, and building name +
coarse submarket alone can still collide with an unrelated real place.
Confirmed real failure this exists for: "Canal Building" (Colliers, part
of the real "Regent's Wharf" campus on All Saints Street, King's Cross -
alongside "Thorley Works", "The Mill", "The Packing House" - with no
street number stated anywhere in its own brochure) resolved via "Canal
Building, King's Cross, London, UK" to a genuinely different "Canal
Reach" street also in King's Cross. Never wired into _source_location_
hint or geocode_unverified - a development-name-assisted match is a
better GUESS, not independent evidence, so it's still flagged for manual
confirmation exactly like any other zero-hint Tier 2 result.

When a bare-name building (no leading house number of its own - see
leading_house_number/_street_name_words, same gate the STREET_CONFLICT
check above uses) has NEITHER a development_name NOR any other source
hint to disambiguate the query, the mitigation above doesn't apply either
- there was nothing at all left to corroborate an accepted Tier 2
candidate against, the exact shape the "Canal Building" case above still
resolved wrong even WITH that mitigation in place (its brochure never
stated an overall development name). See _building_name_words/_best_
places_result's own NAME_CONFLICT docstring: the accepted candidate's own
returned name (Places' own displayName) must share at least one
significant, non-generic word (see _GENERIC_BUILDING_WORDS -
"House"/"Building"/"Court"/... are filtered out first, since those are
shared by hundreds of unrelated real buildings) with the source's own
row.building, or it's rejected outright rather than accepted with just a
geocode_unverified flag. Confirmed real failures this closes, both live-
traced against the real Places API: "Packing House" (same Regent's Wharf
campus as "Canal Building" above) resolved via "Packing House, King's
Cross, London, UK" to a genuinely different, unrelated "King's House"
(242 Pentonville Road, N1 9JY - not a real match for any building on All
Saints Street at all); "Canal Building" itself resolved to a candidate
sharing no name resemblance whatsoever. Same "can't verify, don't guess"
philosophy as STREET_CONFLICT/STREET_UNVERIFIABLE above, deliberately
scoped to the bare-name-AND-zero-hint case only - a building WITH a
development_name or source_hint already has real corroboration this
check would only risk second-guessing, and an address-shaped building
already has STREET_CONFLICT.

Tier 1 also runs for a row with a genuinely numbered address_1 (its own
leading house number - see leading_house_number) but no postcode yet -
not just the original "both present" case. Confirmed real gap: once
_building_identity_matches' own bare-street-reference tier (brochure_
enrichment.py) backfills address_1 from a bare street building name (e.g.
"Clerkenwell Road") to a real numbered address ("67 Clerkenwell Rd") via
the row's own linked brochure, the row could still never get real
coordinates at all - Tier 1 used to refuse to even attempt a lookup
without a postcode already on file, and Tier 2 only ever reads row.
building (unchanged, still the bare street name), never address_1.
Deliberately gated on address_1's own leading house number specifically -
a bare address_1 with no number of its own has no more identifying power
than the bare-street building case Tier 2 already exists to handle
cautiously, and shouldn't bypass it. On success, row.postcode is filled
in via a reverse-geocode of the now-trusted coordinates (see
_backfill_postcode_via_reverse_geocode below) - the same proven,
already-tested primitives (call_reverse_geocoding_api/_address_line1_
and_postcode/_postcode_hint_conflicts) Tier 2's own success path already
relies on for exactly this, reused rather than duplicated, though kept as
its own small helper rather than forcing Tier 2's own tightly-coupled
address_1+postcode+submarket block (a single reverse-geocode call feeding
all three at once) to share it - Tier 1 already has address_1 and gets
submarket filled by the same _backfill_submarket_from_coords call its
original branch already makes, so it only ever needs postcode from this.
"""

import json
import os
import re
import sys

import httpx

from env_utils import load_dotenv
from house_number import leading_house_number
from master_merge import normalize_key
from schema import ListingRow

load_dotenv()

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Places API (New) — a distinct product/enablement from the legacy Places API.
# Text Search (New) can return address components directly in one call, given
# the right field mask, so no separate Place Details lookup is needed.
PLACES_NEW_SEARCHTEXT_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_NEW_FIELD_MASK = "places.displayName,places.formattedAddress,places.location,places.addressComponents"

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


def _is_bare_postcode_district(value: str) -> bool:
    """
    True only when `value`, once trimmed, IS a bare UK postcode outward
    code/district (e.g. "SE1", "EC2", "W1S") and nothing else - reuses the
    exact same outward-code grammar _district_parts already uses, never a
    lookup of specific area names. False for a genuine place name
    ("Shoreditch", "Mayfair", "London Bridge", "Borough"), which never
    matches this short letter+digit[+letter] shape, and false for a full
    postcode ("SE1 8QH", with its own inward code) - only the bare-
    district shape a provider sometimes uses as an area/section label on
    its own (e.g. a "SE1" section heading grouping several buildings) is
    targeted here.
    """
    if not value:
        return False
    return _district_parts(value.strip()) is not None


def _submarket_needs_improvement(value) -> bool:
    """
    True when `value` is blank OR is itself just a bare postcode district
    (see _is_bare_postcode_district) - i.e. not yet a genuinely useful
    named locality a reviewer would recognize. False for any real,
    already-useful submarket value ("Shoreditch", "Clerkenwell", "City",
    "Mayfair", "London Bridge", "Borough", ...), which is never touched.
    """
    return not value or _is_bare_postcode_district(value)


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


def _street_name_words(text: str) -> frozenset:
    """
    The normalized, digit-stripped word set of `text`'s own street-name
    text - same idea as brochure_enrichment._street_name_words (word-
    overlap corroboration for an address comparison, see that function's
    own docstring for the real Ivybridge House case it was built for),
    reimplemented here rather than imported: brochure_enrichment.py
    already imports this module (import geocode), so the reverse import
    would be circular.

    Used by _best_places_result's own STREET_CONFLICT check - see
    geocode_row's Tier 2 candidate loop, which only ever passes a real
    word set here for an address-SHAPED source value (one with its own
    leading house number), never a bare building name.
    """
    return frozenset(w for w in normalize_key(text).split() if not w.isdigit())


# Generic UK building-TYPE words - same "small, explicit, conservative"
# precedent as brochure_enrichment._TRAILING_STREET_SUFFIX_WORDS, but for
# building names rather than street names. Deliberately filtered OUT of
# _building_name_words below: dozens of genuinely unrelated real London
# buildings share a bare type word like "House"/"Building"/"Court", so
# leaving it in would make a plain _street_name_words-style overlap check
# nearly useless for a bare building name - confirmed real case: "Packing
# House" and "King's House" (a genuinely different, unrelated building)
# share nothing but "house".
_GENERIC_BUILDING_WORDS = frozenset({
    "house", "building", "buildings", "court", "centre", "center", "place",
    "wharf", "works", "mews", "tower", "towers", "hall", "halls", "gardens",
    "yard", "square", "plaza", "gate", "point", "quarter", "walk", "row",
    "green", "park", "view", "corner", "hub", "wing", "block",
})


def _building_name_words(text: str) -> frozenset:
    """
    _street_name_words' own word set for `text`, with one extra filtering
    pass a STREET name never needs: generic building-TYPE words (see
    _GENERIC_BUILDING_WORDS) are dropped first, since a London building
    name routinely ends in one shared by hundreds of unrelated real
    buildings - without this, _best_places_result's own NAME_CONFLICT
    check below would accept "Packing House" against a Places candidate
    genuinely named "King's House" purely because both happen to end in
    "House", the exact real failure this exists to catch (confirmed live
    against the real Places API: "Packing House, King's Cross, London, UK"
    -> top result "King's House", 242 Pentonville Road).

    Falls back to the UNFILTERED word set when filtering would otherwise
    leave nothing at all (e.g. a building genuinely named just "The
    Courtyard") - comparing against a weak remaining word is still better
    evidence than comparing against an empty set, which would silently
    no-op the check entirely (see _best_places_result's own handling of an
    empty candidate word set) - same "start conservative, don't lose the
    check to an edge case" precedent brochure_enrichment._strip_trailing_
    street_suffix_word already follows for the identical reason.

    Used by _best_places_result's own NAME_CONFLICT check - see
    geocode_row's Tier 2 candidate loop, which only ever passes a real
    word set here for a BARE building name (no leading house number of its
    own - see leading_house_number) with no development_name/source_hint
    already disambiguating the query, the one case with zero other
    corroboration available at all (see this module's own docstring on
    "Canal Building"/"Packing House", the confirmed real cases this
    closes).
    """
    words = _street_name_words(text)
    return frozenset(w for w in words if w not in _GENERIC_BUILDING_WORDS) or words


def call_geocoding_api(address: str) -> dict:
    resp = httpx.get(GEOCODE_URL, params={"address": address, "key": _api_key()}, timeout=10)
    data = resp.json()
    status = data.get("status", "UNKNOWN")
    _check_not_enabled(status, data.get("error_message", ""), "Geocoding API")

    if status != "OK" or not data.get("results"):
        return {"status": status}

    top = data["results"][0]
    location = top["geometry"]["location"]
    # address_components (the top result's own, never pooled across
    # results the way call_reverse_geocoding_api's own version is - a
    # forward address lookup has one clearly-best result, unlike a
    # reverse lookup, which returns several at different specificity
    # levels) is purely additive - every existing caller/test already
    # only ever reads "status"/"lat"/"lng" from this dict, so this adds
    # nothing any of them needs to change for. Lets a caller that already
    # has a trusted address (e.g. geocode_address_lookup below, or
    # geocode_row's own relaxed Tier 1 branch) pull a clean address_1/
    # postcode back out via _address_line1_and_postcode without a
    # separate reverse-geocode call.
    return {
        "status": "OK", "lat": location["lat"], "lng": location["lng"],
        "address_components": top.get("address_components", []),
    }


def geocode_address_lookup(address: str, postcode: str = None) -> dict:
    """
    A real Geocoding API (Tier 1) lookup for a plain address string a
    REVIEWER typed in - never used by geocode_row itself, which always
    works from a ListingRow's own fields. See pages/2_Review_and_Master.py's
    own missing-location lookup UI for a genuinely new property, the one
    caller of this function.

    Query built exactly like geocode_row's own Tier 1 (see that function's
    own two branches): "{address}, {postcode}, UK" when postcode is given
    (even from an address alone, since the whole point of this path is
    a reviewer who may not know/have a postcode yet), else "{address}, UK"
    alone.

    Returns {"status": "OK", "address_1", "postcode", "lat", "lng",
    "submarket"} on success - address_1/postcode/submarket may still
    individually be None when the API's own result doesn't state one
    (never guessed/fabricated) - or {"status": <call_geocoding_api's own
    non-OK status>} otherwise, the exact same "no match" contract that
    function already has, so a caller only ever needs to check
    status == "OK".

    address_1/postcode are read from the API's own top result via
    _address_line1_and_postcode (name_key="long_name" - the legacy
    Geocoding API's own component shape, same as call_reverse_geocoding_
    api's own caller uses), never a second, independently-drifting parser.
    submarket is resolved via the SAME _fill_submarket/_backfill_
    submarket_from_coords machinery geocode_row itself trusts for every
    other source of coordinates - tried against a throwaway ListingRow
    built from this lookup's OWN freshly-resolved address_1/postcode
    (the most specific, current location text available here), never the
    caller's own possibly-blank row.
    """
    query = f"{address}, {postcode}, UK" if postcode else f"{address}, UK"
    result = call_geocoding_api(query)
    if result["status"] != "OK":
        return result

    address_1, resolved_postcode = _address_line1_and_postcode(
        result.get("address_components", []), name_key="long_name"
    )
    scratch_row = ListingRow(building=address_1 or address, address_1=address_1, postcode=resolved_postcode)
    _backfill_submarket_from_coords(scratch_row, result["lat"], result["lng"])

    return {
        "status": "OK",
        "address_1": address_1,
        "postcode": resolved_postcode,
        "lat": result["lat"],
        "lng": result["lng"],
        "submarket": scratch_row.submarket,
    }


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
            "name": (place.get("displayName") or {}).get("text"),
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
    overwrites a genuinely-extracted, already-useful value (the row.
    submarket guard below - now also lets a bare postcode-district value
    like "SE1" through, see _submarket_needs_improvement's own docstring
    for why: a provider's own "SE1" section heading is a real, faithfully-
    extracted value, but it is not a useful named submarket the way
    "Shoreditch"/"Mayfair" already are, so it gets the same one-time
    improvement attempt as a genuinely blank value, never a real place
    name); (2) a safe source-text locality hint (see _source_area_hint/
    extract_area_hint) - preferred over Google's own reverse-geocoded
    neighbourhood since the source already said so explicitly, and
    confirmed real gap: Google's sublocality/neighborhood coverage is
    patchy (reliable for Mayfair/Fitzrovia/Soho, but genuinely blank for
    other real addresses this exists for), so a source that already
    states its own locality must never stay blank just because Google's
    reverse-geocode happens not to cover it; (3) address_components
    already fetched from a reverse-geocode (see _submarket_from_components) -
    checked only when the source itself had nothing safe to offer, and only
    if the caller already has a reverse-geocode result in hand (never
    fetches one itself - see _backfill_submarket_from_coords, the only
    caller that decides whether a reverse-geocode call is worth making at
    all). Idempotent/safe to call more than once for the same row - every
    branch is a no-op once row.submarket holds a genuinely useful value.
    """
    if not _submarket_needs_improvement(row.submarket):
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
    the actual priority order (which now also applies when row.submarket
    is merely a bare postcode district like "SE1" - see _submarket_needs_
    improvement). Only calls out to the reverse-geocode API when a safe
    source-text hint isn't already enough, saving the call entirely for a
    row whose source text already states its own locality. Applies
    regardless of source type (spreadsheet/PDF/email), since geocode_row
    is the one shared code path for all of them - no per-source-type
    wiring needed. This is purely an additional read at coordinates
    already known, with no risk to lat/lng itself.
    """
    if not _submarket_needs_improvement(row.submarket):
        return
    _fill_submarket(row)
    if not _submarket_needs_improvement(row.submarket):
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


# Real, confirmed gap this closes: a Tier 2 candidate accepted on
# genuinely weaker-than-usual corroboration (see _best_places_result's
# own "weak_corroboration" - a candidate with no route to check row's own
# stated street against at all, corroborated only by its postcode
# district agreeing with source_hint) left NO trace anywhere once
# accepted - a real Kitt's "44 Paul Street" incident (accepted onto a
# genuinely wrong "20 Little Britain" address/coordinate this exact way)
# had to be reconstructed entirely by hand afterward, with nothing in any
# log to point at. Deliberately a SEPARATE list/function from FAILURES/
# log_geocode_failure above - this is a genuine ACCEPTANCE, not a
# rejection, so folding it into "FAILED" logging would misdescribe it.
WEAK_MATCHES = []


def log_geocode_weak_match(row: ListingRow, reason: str):
    entry = {
        "building": row.building,
        "floor_unit": row.floor_unit,
        "submarket": row.submarket,
        "source_file": row.source_file,
        "reason": reason,
    }
    WEAK_MATCHES.append(entry)
    print(f"[geocode] WEAK MATCH ACCEPTED: {row.building!r} ({row.source_file}) — {reason}", file=sys.stderr)


def _best_places_result(
    query: str, source_hint: dict, source_street_words: frozenset = None, source_name_words: frozenset = None
) -> dict:
    """
    Sends ONE query to Places Text Search and returns the first candidate
    (see call_places_text_search's own "candidates" list, not just its
    top result) that passes bbox + source-postcode-evidence validation -
    confirmed real gap this closes: call_places_text_search previously
    exposed only the top Places result, so a source with strong location
    evidence (e.g. "SE1") had no way to fall through to a DIFFERENT
    candidate the same search already returned, even when that candidate
    was sitting right there in the same response.

    source_street_words (see _street_name_words) is a THIRD, independent
    validation, only ever passed by geocode_row for an address-shaped
    source building value (its own leading house number - see that
    caller's own comment): a candidate whose own returned street (its
    "route" address component) shares NOT ONE significant word with it is
    rejected as a STREET_CONFLICT - confirmed real failure this closes:
    a real Kitt's "44 Paul Street" (no address_1/postcode of its own, so
    nothing for the postcode-district check above to compare against)
    resolved to a candidate on a genuinely different, unrelated street
    (Little Britain). None (the default) is a pure no-op, same permissive
    behavior as source_hint=None for the postcode check above - never a
    new requirement to have evidence, only extra corroboration when it's
    genuinely available. Deliberately a ZERO-overlap rejection, not exact-
    set equality (contrast brochure_enrichment._address_conflict_note,
    which flags ANY word difference for a human to review) - rejecting a
    Tier 2 candidate outright here is a more consequential decision than
    surfacing a note (a wrongly-rejected genuine match silently leaves the
    row completely unmapped), so this only ever fires on a fully disjoint
    match, the strongest possible signal something is actually wrong.

    A candidate with NO route component at all (only e.g. street_number/
    postal_code) makes candidate_street_words an EMPTY set - falsy, so the
    check above silently no-ops and the candidate was accepted exactly as
    if source_street_words had never been passed at all. Real, confirmed
    incident this closes: live-traced against the actual Places/Geocoding
    APIs for the exact "44 Paul Street" case above - a route-less candidate
    at real coordinates ~1km away (a genuinely different, unrelated place)
    was accepted this way, and a LATER, separate reverse-geocode of those
    now-trusted-but-wrong coordinates is what produced the "20 Little
    Britain, EC1A 7DH" address actually shown to a reviewer - a route-less
    candidate can still corroborate via its own postcode DISTRICT (already
    confirmed non-conflicting against source_hint above) - weaker evidence
    than a genuine street-word match, but real and independent, so this is
    still accepted, just flagged ("weak_corroboration" in the returned
    dict, checked by geocode_row - see log_geocode_weak_match) rather than
    left as invisible as the original incident was. With NOTHING to
    corroborate against at all (no source_hint, or the candidate has no
    postcode of its own either - the exact real Paul Street shape), this
    is rejected outright instead (STREET_UNVERIFIABLE) - the same "can't
    verify, don't guess" philosophy the STREET_CONFLICT branch above
    already uses.

    source_name_words (see _building_name_words) is a FOURTH, independent
    validation, only ever passed by geocode_row for a BARE building name
    (no leading house number of its own, so source_street_words above is
    never passed for the same candidate) with no development_name or
    source_hint already disambiguating the query - the one shape with
    otherwise ZERO corroboration available: a candidate whose own returned
    name shares NOT ONE significant word with it is rejected as a
    NAME_CONFLICT. Confirmed real failures this closes, both live-traced
    against the real Places API: "Packing House" (Regent's Wharf, King's
    Cross - no street number stated anywhere in its own brochure) resolved
    via "Packing House, King's Cross, London, UK" to a genuinely different,
    unrelated "King's House" (242 Pentonville Road); "Canal Building" (same
    brochure/campus) resolved via "Canal Building, King's Cross, London,
    UK" to a candidate sharing no name resemblance at all. Same zero-
    overlap-only rejection philosophy as source_street_words above, for the
    same reason - a wrongly-rejected genuine match silently leaves the row
    completely unmapped, so this only ever fires on a fully disjoint match.

    A candidate with no displayName of its own at all makes candidate_name_
    words an EMPTY set - falsy, so the check silently no-ops and the
    candidate is accepted exactly as if source_name_words had never been
    passed (same permissive-on-missing-evidence precedent as the route-less
    case above) - there is no weaker fallback corroboration to offer here
    the way the street check's own postcode-district fallback has, since
    source_name_words is only ever passed when source_hint is ALREADY
    absent.

    If every candidate this query returns fails validation, returns the
    LAST one's own conflict/failure info (for log_geocode_failure's own
    message) - never a candidate this function itself hasn't checked, and
    never blindly "the next one" without the exact same bbox/postcode/
    street/name checks geocode_row's own single-candidate path always
    applied.
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
        candidate_address_1, candidate_postcode = _address_line1_and_postcode(place.get("address_components", []))
        if _postcode_hint_conflicts(source_hint, candidate_postcode):
            last = {**place, "status": "LOCATION_CONFLICT", "postcode": candidate_postcode}
            continue
        weak_corroboration = None
        if source_street_words:
            candidate_street_words = _street_name_words(candidate_address_1) if candidate_address_1 else frozenset()
            if candidate_street_words:
                if not (source_street_words & candidate_street_words):
                    last = {**place, "status": "STREET_CONFLICT", "candidate_street": candidate_address_1}
                    continue
            elif source_hint and candidate_postcode:
                weak_corroboration = "no_route_postcode_district_only"
            else:
                last = {**place, "status": "STREET_UNVERIFIABLE", "candidate_postcode": candidate_postcode}
                continue
        if source_name_words:
            candidate_name = place.get("name")
            candidate_name_words = _building_name_words(candidate_name) if candidate_name else frozenset()
            if candidate_name_words and not (source_name_words & candidate_name_words):
                last = {**place, "status": "NAME_CONFLICT", "candidate_name": candidate_name}
                continue
        result = {**place, "status": "OK"}
        if weak_corroboration:
            result["weak_corroboration"] = weak_corroboration
        return result
    return last


def _submarket_query_variants(submarket: str) -> list:
    """
    Submarket text for a Places Text Search query, as one or two variants
    to try in order. The NO-SPACE variant is tried FIRST, the submarket
    exactly as extracted/normalized (never altered - that normalization
    is correct and untouched by this) SECOND, as one additional fallback,
    never a replacement - see below for why this order, specifically, is
    the only one that actually changes anything. Returns [None] for a row
    with no submarket at all (geocode_row's own query-building already
    treats None as "omit this segment"), or [submarket] alone (no second
    variant) when stripping spaces changes nothing.

    General on purpose - strips whatever submarket string is actually
    present, never a hardcoded "Midtown" special case, so any other
    multi-word submarket whose no-space form happens to disambiguate
    better gets the same chance.

    Confirmed real case this exists for: a real MetSpace email lists
    "Adler House" under its own "MID TOWN" section header, with no
    address_1/postcode stated anywhere - Tier 2's building-only fallback
    is the only path available. Querying the real Places API directly:
    "Adler House, Mid Town, London, UK" (the stated, correct two-word
    submarket) returns 9 ambiguous candidates, including two different
    wrong buildings (one of which a real upload actually geocoded to);
    "Adler House, Midtown, London, UK" (no space) returns exactly ONE
    candidate - the real Adler House in Holborn. Google's Text Search
    appears to match "Mid Town" against an unrelated place literally
    named "Midtown" (20 Procter St) among the 9, diluting the result set
    in a way the single-word form doesn't.

    Why no-space has to go FIRST, not merely be appended as a fallback
    tried only once the spaced version fails: a row with no source-stated
    postcode/address hint at all (exactly the Adler House shape) has
    nothing for _postcode_hint_conflicts to check a candidate against, so
    _best_places_result accepts the FIRST in-bbox candidate a query
    returns, unconditionally - confirmed directly, this is exactly what
    already (wrongly) geocodes Adler House today. The spaced query's own
    first candidate is always comfortably within the London bbox, so it
    is always accepted on the spot; a same-candidate no-space attempt
    tried only AFTER that would therefore never even run for this shape
    of row. Trying no-space first costs nothing when it isn't needed (a
    single-word or absent submarket returns no second variant at all, see
    above; a multi-word submarket that already resolves correctly today
    is not touched by this module's own extraction/normalization, only by
    which STRING gets sent to Places first) and is the only ordering that
    lets the more discriminating query actually get first use of that
    same "accept the first in-bbox candidate" rule, before a noisier
    spaced query result claims it instead.
    """
    if not submarket:
        return [None]
    no_space = submarket.replace(" ", "")
    if no_space != submarket:
        return [no_space, submarket]
    return [submarket]


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


# The street-suffix words Tier 2's own bare-street-reference guard (see
# _is_bare_street_reference) checks a building's own last word against -
# a DELIBERATELY NARROWER subset of master_merge._STREET_SUFFIX_
# EXPANSIONS' own full vocabulary (street/road/avenue/lane and their
# abbreviations only), not that dict's complete key+value set. Confirmed
# real gap checked directly against this project's own test fixtures: the
# dict's OTHER values - square/place/court/gardens/terrace - are also
# common, genuine standalone UK BUILDING names in their own right (e.g.
# this project's own "Nexus Place" fixture, used as a real building
# throughout test_geocode.py), unlike street/road/avenue/lane, which are
# essentially never a real building's own name on their own. Using the
# full vocabulary here would silently skip Tier 2 geocoding entirely for
# a genuine building like "Nexus Place" - a real regression, not a
# hypothetical one, caught by this module's own existing test suite the
# moment the full set was tried. See IsBareStreetReferenceTests/
# BareStreetReferenceSkipsTier2Tests in tests/test_geocode.py.
_RELIABLE_STREET_SUFFIX_WORDS = frozenset({"st", "street", "rd", "road", "ave", "av", "avenue", "ln", "lane"})


def _is_bare_street_reference(row: ListingRow) -> bool:
    """
    True when row.building is structurally just a street name, with no
    way to identify any SPECIFIC building on it - confirmed real case: a
    MetSpace email listing whose only location text was "Clerkenwell
    Road" (no house number, no building name at all). Tier 2's own Places
    Text Search never returns "no result" for a query shaped like this;
    it hands back SOME plausible-looking address on that street instead
    (confirmed: "156 Clerkenwell Road" for a listing that was actually 67
    Clerkenwell Road, a real but wrong building) - a specific-looking
    wrong number is worse than an honest blank, so this is detected and
    skipped BEFORE Tier 2 ever queries Places at all, rather than let
    through and merely flagged geocode_unverified afterward (too easy to
    miss/click through in review).

    Three conditions, all required:
    - row.building has no leading house number anywhere in it at all (via
      house_number.leading_house_number, reused rather than reimplemented -
      "27-30 Lime Street" is already excluded by this alone, a real
      building reference regardless of its own last word); and
    - a COMPOUND "Name, Address"/"Name - Address" building value (see
      split_compound_building, tried FIRST by Tier 2's own address-only
      query just below) has no leading house number in its own address
      PART either - checked separately from the raw-string check above,
      since a real compound value's own number sits after the separator
      ("Bridge House, 22 Newman Street" has no digit at position 0, but
      its own address part genuinely does - confirmed real regression
      this specific check avoids, caught by this module's own existing
      CompoundBuildingGeocodingTests the moment it was missing); and
    - row.building's own LAST word is one of the reliable street-suffix
      words (see _RELIABLE_STREET_SUFFIX_WORDS above - street/road/
      avenue/lane and their abbreviations ONLY, a deliberately narrower
      subset of master_merge._STREET_SUFFIX_EXPANSIONS' own full
      vocabulary, see that constant's own docstring for the real "Nexus
      Place" gap this avoids) - a real building name ending in "House"/
      "Building"/"Mill"/"Place"/"Square"/"Court"/etc. never matches this
      at all.

    row.development_name must ALSO be blank - a stated development name
    (e.g. "Regent's Wharf") is genuine extra identifying evidence (see
    this module's own "Canal Building" docstring case) that could still
    make a street-suffix-ending building name resolvable via Tier 2's own
    development-name-first query variant; this guard must never block
    that path just because building alone looks like a bare street.
    """
    if not row.building or row.development_name:
        return False
    if leading_house_number(row.building) is not None:
        return False
    compound = split_compound_building(row.building)
    if compound and leading_house_number(compound[1]) is not None:
        return False
    words = row.building.strip().split()
    if not words:
        return False
    last_word = re.sub(r"[^\w]", "", words[-1]).lower()
    return last_word in _RELIABLE_STREET_SUFFIX_WORDS


def _backfill_postcode_via_reverse_geocode(row: ListingRow, source_hint: dict) -> None:
    """
    Fills row.postcode (only) from a reverse-geocode of row's own already-
    accepted, trusted lat/lng - built from the same proven primitives
    Tier 2's own success path already relies on for this exact purpose
    (call_reverse_geocoding_api/_address_line1_and_postcode/_postcode_
    hint_conflicts), reused here rather than a second, differently-tuned
    implementation. Used by Tier 1's own relaxed "address_1 has a house
    number but no postcode yet" path (see geocode_row) - deliberately its
    own small helper rather than forcing Tier 2's own block to share it:
    that block is a single reverse-geocode call feeding address_1,
    postcode, AND submarket all at once (see its own comment), which this
    caller doesn't need - it already has address_1, and submarket is
    already handled by the same _backfill_submarket_from_coords call
    Tier 1's own branch already makes right after this.

    A no-op whenever row.postcode is already present (never overwrites a
    real value, including one this same call might have just set on an
    earlier pass), the reverse-geocode itself fails, or it returns nothing
    postcode-shaped. Same validation as Tier 2's own version: a reverse-
    geocoded postcode that contradicts `source_hint` (see _postcode_hint_
    conflicts) is logged and left blank rather than written - the
    coordinate itself is untouched either way, since it already passed
    its own acceptance check before this ever runs.
    """
    if row.postcode:
        return
    reverse = call_reverse_geocoding_api(row.lat, row.lng)
    if reverse["status"] != "OK":
        return
    _, postcode = _address_line1_and_postcode(reverse.get("address_components", []), name_key="long_name")
    if not postcode:
        return
    if _postcode_hint_conflicts(source_hint, postcode):
        log_geocode_failure(
            row,
            f"reverse-geocode postcode {postcode!r} contradicts source location "
            f"evidence ({_hint_label(source_hint)!r}) - postcode left blank",
        )
        return
    row.postcode = postcode


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
            # Always an independently corroborated lookup, never a guess -
            # explicitly clears a stale True a prior upload's own Tier 2
            # zero-hint fallback may have left on this row (see schema.
            # ListingRow.geocode_unverified's own docstring). An explicit
            # False, not None, is what actually matters here: master_
            # merge.diff_fields' blank-skip rule treats None as "no data
            # this time, don't touch master's existing value" (correct -
            # that's what stops a row that wasn't rechecked from erasing a
            # real warning), but False is a real, positive value that DOES
            # overwrite a stale True.
            row.geocode_unverified = False
            _backfill_submarket_from_coords(row, row.lat, row.lng)
            return row
        # fall through to Places if Geocoding fails despite having an address
    elif row.address_1 and leading_house_number(row.address_1) is not None:
        # address_1 has its own house number - a genuinely specific,
        # numbered address, not a bare street reference - but no postcode
        # yet, e.g. a row whose address_1 was just backfilled from its own
        # brochure (see _building_identity_matches' own bare-street-
        # reference tier in brochure_enrichment.py) from a source document
        # that itself never states one. Deliberately gated on address_1's
        # own leading house number specifically, not merely "address_1 is
        # non-blank" - a bare address_1 with no number has no more
        # identifying power than the bare-street BUILDING case Tier 2
        # already exists to handle cautiously (see _is_bare_street_
        # reference below), and must still go through that path unchanged,
        # never this one.
        query = f"{row.address_1}, London, UK"
        result = call_geocoding_api(query)
        if result["status"] == "OK":
            row.lat = result["lat"]
            row.lng = result["lng"]
            # Same explicit-False reasoning as the branch above - a
            # genuinely numbered address resolving via the Geocoding API
            # is real, corroborated evidence, same confidence level as the
            # "postcode already on file" case, not a weaker guess.
            row.geocode_unverified = False
            _backfill_postcode_via_reverse_geocode(row, _source_location_hint(row))
            _backfill_submarket_from_coords(row, row.lat, row.lng)
            return row
        # fall through to Places if Geocoding fails despite having an address

    # --- Tier 2: Places API Text Search (fallback) ---
    if _is_bare_street_reference(row):
        # See _is_bare_street_reference's own docstring for the real
        # confirmed MetSpace "Clerkenwell Road" case this guards against -
        # skipped BEFORE ever querying Places at all, leaving lat/lng/
        # address_1/postcode completely untouched (never geocode_unverified
        # either - there's no accepted candidate here to flag at all).
        log_geocode_failure(
            row,
            "building is a bare street name with no house number — no way to identify a specific building "
            "on it, skipped rather than guessing",
        )
        return row

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
        #
        # Each candidate is itself tried against a list of location-text
        # variants, most specific first, before moving on to the next
        # candidate - same "keep trying safer/differently-phrased queries
        # before giving up" principle, one level deeper. row.development_
        # name (the overall campus/development's own brand name, e.g.
        # "Regent's Wharf" for "The Canal Building" - see schema.
        # ListingRow's own docstring), when present, is tried FIRST: it's a
        # more specific, more reliable disambiguator than submarket alone -
        # confirmed real failure this exists for: "Canal Building" (no
        # street number stated anywhere in its own brochure) resolved via
        # a plain "Canal Building, King's Cross, London, UK" query to a
        # genuinely different "Canal Reach" street also in King's Cross,
        # since building name + coarse area alone isn't unique enough.
        # _submarket_query_variants() (a no-space submarket variant first,
        # then the submarket exactly as extracted/normalized - see that
        # function's own docstring for why THAT specific order is the only
        # one that changes anything) is always tried too, as a fallback
        # for a development-name query that itself doesn't resolve, or for
        # any row with no development_name at all - never skipped, only
        # ever tried after the more specific variant first. Same OK-and-
        # in-bbox acceptance check throughout; neither variant is ever
        # given any different treatment than the base query once built.
        result = {"status": "ZERO_RESULTS"}
        for candidate in query_variants:
            matched = False
            # Only an address-SHAPED candidate (its OWN leading house
            # number - see _street_name_words/_best_places_result's own
            # STREET_CONFLICT docstring) states a real street worth cross-
            # checking a Places candidate against - a bare building NAME
            # ("Canal Building", "Kent House") has no street of its own to
            # compare, and comparing its own name text against a returned
            # route would false-positive on the ordinary case.
            candidate_street_words = (
                _street_name_words(candidate) if leading_house_number(candidate) is not None else None
            )
            # A bare-name candidate with NEITHER a development_name NOR a
            # source_hint to disambiguate the query has zero corroboration
            # available at all once a Places candidate comes back - see
            # _building_name_words/_best_places_result's own NAME_CONFLICT
            # docstring for the confirmed real "Packing House" -> "King's
            # House" and "Canal Building" failures this guards against.
            # Skipped the moment either development_name or source_hint IS
            # available - both are already-trusted, independent
            # disambiguators for THIS row (not just this one query variant),
            # so the weaker name-only check never second-guesses a match
            # they already helped corroborate.
            candidate_name_words = (
                _building_name_words(candidate)
                if leading_house_number(candidate) is None and not row.development_name and not source_hint
                else None
            )
            location_texts = ([row.development_name] if row.development_name else []) + (
                _submarket_query_variants(row.submarket)
            )
            for location_text in location_texts:
                query_parts = [candidate]
                if location_text:
                    query_parts.append(location_text)
                query_parts.append("London, UK")
                query = ", ".join(query_parts)

                result = _best_places_result(query, source_hint, candidate_street_words, candidate_name_words)
                if result["status"] == "OK" and within_london_bbox(result.get("lat"), result.get("lng")):
                    matched = True
                    break
            if matched:
                break

        if result["status"] == "OK" and within_london_bbox(result["lat"], result["lng"]):
            row.lat = result["lat"]
            row.lng = result["lng"]

            # No source address_1/postcode/building-trailing-token hint at
            # all (see _source_location_hint) means this candidate was
            # accepted purely on a building-name-only Places match, with
            # nothing to cross-check it against - flag it so the Review
            # page can give it its own stronger caution (see schema.
            # ListingRow.geocode_unverified's own docstring) rather than
            # treating it with the same confidence as a hint-corroborated
            # result. A weak_corroboration accept (see _best_places_
            # result's own docstring - a route-less candidate corroborated
            # only by its postcode DISTRICT, never a genuine street-word
            # match) gets the same treatment, even though source_hint IS
            # present here - postcode-district agreement alone is real but
            # weaker evidence than the street-level corroboration every
            # OTHER hint-corroborated match actually has, so it still
            # deserves a reviewer's own closer look, not the full
            # confidence an explicit False elsewhere implies.
            if not source_hint or result.get("weak_corroboration"):
                row.geocode_unverified = True
            else:
                # A genuinely hint-corroborated Tier 2 match is real
                # evidence, same as Tier 1 - explicit False (never just
                # left at None) so it too can clear a stale True a prior
                # upload's own zero-hint fallback left on this row (see
                # geocode_row's own Tier 1 success branch for the same
                # explicit-False reasoning).
                row.geocode_unverified = False

            if result.get("weak_corroboration"):
                # Real, confirmed gap this closes: the exact "44 Paul
                # Street" -> "20 Little Britain" incident this whole
                # mechanism exists for left NO trace anywhere once
                # accepted - reconstructing what actually happened took
                # tracing it by hand, well after the fact, with nothing in
                # any log to point at. This is that record.
                log_geocode_weak_match(
                    row,
                    f"Places candidate at (lat={result['lat']}, lng={result['lng']}) has no route/street "
                    f"of its own to verify against the source's own stated street in {row.building!r} - "
                    f"accepted only on its postcode district agreeing with source evidence "
                    f"({_hint_label(source_hint)!r}), not a genuine street match",
                )

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

            if not row.address_1 or not row.postcode or _submarket_needs_improvement(row.submarket):
                # Places matched real coordinates but its own record is missing
                # something (no street address on file at all - e.g. Kent House, a
                # correct, well-disambiguated match with a "premise"-only record -
                # and/or no useful submarket, see _submarket_from_components/
                # _submarket_needs_improvement) - reverse-geocode the
                # coordinates we already trust as a second attempt to fill in
                # whichever of these is still missing/unimproved. One call
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

        if result["status"] == "STREET_CONFLICT":
            log_geocode_failure(
                row,
                f"Places candidate's own street ({result.get('candidate_street')!r}) shares no words "
                f"with the source's own stated street in {row.building!r} - rejected rather than "
                "accepted on an otherwise-uncorroborated building-name-only match",
            )
            return row

        if result["status"] == "NAME_CONFLICT":
            log_geocode_failure(
                row,
                f"Places candidate's own name ({result.get('candidate_name')!r}) shares no words "
                f"with the source's own bare building name {row.building!r}, and there was no "
                "development_name/other source evidence to disambiguate the query either - rejected "
                "rather than accepted on a completely uncorroborated building-name-only match",
            )
            return row

        if result["status"] == "STREET_UNVERIFIABLE":
            log_geocode_failure(
                row,
                f"Places candidate (postcode={result.get('candidate_postcode')!r}) has no route/street "
                f"of its own to verify against the source's own stated street in {row.building!r}, and "
                "nothing else to corroborate it with either - rejected rather than accepted on a "
                "completely uncorroborated building-name-only match",
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

    geocode_unverified (see schema.ListingRow's own docstring) travels
    alongside lat/lng/address_1/postcode in this same copy-down, never with
    submarket - it describes the CONFIDENCE of that one shared physical-
    location result, not a row-specific fact, so every member inheriting
    the representative's address/coordinates must inherit its unverified
    status too, or the Review page's stronger caution (see pages/2_Review_
    and_Master.py's _risky_field_reason) would only ever show up on
    whichever single row happened to be the representative.
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
            # geocode_unverified describes the CONFIDENCE of the shared
            # lat/lng/address_1/postcode result above, not a fact of its
            # own - it must travel with those fields to every member that
            # inherits them, or a group's other rows would silently share
            # an unverified address with no caution shown at all (confirmed
            # real gap: Hatchers Yard/Ivybridge House groups only flagged
            # the one representative actually sent through geocode_row).
            # Checked with `is None`, never truthiness - representative.
            # geocode_unverified can genuinely be False (a real, positive
            # "this IS verified" value - see schema.ListingRow's own
            # docstring on why False and None are deliberately different),
            # which a bare truthiness check would wrongly treat as
            # "nothing to propagate" and silently drop, leaving other
            # group members' own still-None value unresolved.
            if row.geocode_unverified is None and representative.geocode_unverified is not None:
                row.geocode_unverified = representative.geocode_unverified
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
