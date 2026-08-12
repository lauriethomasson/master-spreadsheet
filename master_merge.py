"""
master_merge.py

Computes a merge plan for folding a batch of newly-extracted rows into the
cumulative master spreadsheet: matches each new row against current master
rows by a layered, normalized key, diffs matched rows field-by-field, and
separates out same-batch collisions so a human resolves them explicitly
instead of write order silently deciding a winner.

Match key: normalized building + provider + postcode + floor_unit when the
new row has a postcode, falling back to building + provider + floor_unit
(ignoring postcode entirely) otherwise or when the postcode-inclusive key
finds nothing. floor_unit is load-bearing here - matching on
building + provider + postcode alone (an earlier draft of this design)
collapses every distinct unit in a multi-unit building from the same
provider onto the same row, which real data in this repo already
demonstrates (16 Dufour's Place / GPE has 4 separate floor units sharing
one building+provider+postcode).

Pure logic, no Streamlit/storage - pages/2_Review_and_Master.py renders a
MergePlan and turns the user's decisions into the final row list via
apply_merge(); master_writer.write_master() only ever sees that final list
and has no awareness that a merge happened at all.
"""

import difflib
import math
import re
import typing
import uuid
from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from house_number import LEADING_HOUSE_NUMBER_RE as _LEADING_HOUSE_NUMBER_RE
from house_number import leading_house_number as _leading_house_number
from schema import ListingRow
from storage.file_store import clean_value

# building/provider/floor_unit/postcode are matching keys, not excluded from
# diffing - a matched row can still show one of them as a changed field (e.g.
# a postcode filled in via the fallback tier, or a capitalization fix), and
# that's exactly the diff-and-merge value proposition working as intended.
# property_id is internal bookkeeping and source_file is handled separately
# (see pages/2_Review_and_Master.py) - neither belongs in a field-by-field diff.
DIFF_FIELDS = [f for f in ListingRow.model_fields if f not in ("property_id", "source_file")]

# Free-text list fields where a re-upload replacing a detailed value with a
# much shorter one is a red flag rather than a normal update - a brochure
# re-upload has, in practice, replaced a full amenities list with a one-line
# availability status (see is_detail_loss and, for the coarser backstop that
# catches what item-loss's own documented blind spot misses, is_richness_
# regression). Deliberately not every str field: short descriptive fields
# like state_of_space don't have this "list of distinct items" shape, so a
# length/retention check on them would just be noise.
RISKY_TEXT_FIELDS = ("special_features", "contacts")

# Subset of RISKY_TEXT_FIELDS that gets build_merge_plan's own OLD-vs-NEW
# (matched-row) merge/detail-loss-risk treatment - special_features only.
# contacts is deliberately excluded here: for a confidently matched same-
# provider/property row, the newest nonblank contact set is authoritative
# (see build_merge_plan's own CONTACTS_NEWEST_WINS_FIELDS handling) rather
# than merged with a possibly-stale old one - a departing agent's own
# details must not persist forever just because they were once correct
# (confirmed real want: "John Smith" -> "Sarah Jones" must become "Sarah
# Jones", never "John Smith; Sarah Jones"). contacts stays listed in
# RISKY_TEXT_FIELDS itself (unchanged) for the SEPARATE same-batch
# collision-peer path (matched_collision_field_choice/_text_variants_
# compatible) - there, "richest compatible peer variant" is still the
# right question, since neither peer row is "the old one" to begin with
# (both are this same batch's own current data, not an update over time).
DETAIL_LOSS_MERGE_FIELDS = ("special_features",)

# contacts, for a confidently matched row, always resolves to the newest
# NONBLANK value the incoming row supplies, verbatim - never merged with
# master's old value (see DETAIL_LOSS_MERGE_FIELDS' own docstring for why),
# and never flagged risky (a normal contact replacement needs no manual
# click). This is really just "contacts behaves like any other ordinary
# scalar field" - diff_fields already does exactly this (new nonblank
# replaces old, new blank preserves old, identical values are no diff) -
# nothing extra to implement, this constant exists purely so that intent
# is named and easy to find, the same way DETAIL_LOSS_MERGE_FIELDS/
# GEOCODE_RISK_FIELDS/HOUSE_NUMBER_FIELDS are.
CONTACTS_NEWEST_WINS_FIELDS = ("contacts",)

# Which of RISKY_TEXT_FIELDS gets the extra comma-splitting described in
# _detail_items - special_features only. contacts' own per-person format is
# "Name, email, phone" (a single genuine item, comma-joined internally) -
# the same comma-splitting there would shred one contact into three fake
# items, so it always stays semicolon/newline-only, exactly as before this
# existed.
_MERGE_COMMA_SPLIT_FIELDS = frozenset({"special_features"})

# The two fields a leading house number (see _leading_house_number/
# house_number_changed) can appear in - checked independently of
# RISKY_TEXT_FIELDS in build_merge_plan's risky_fields computation, since a
# structural house-number change (master's "18 Copthall Avenue" silently
# becoming an update's "14-18 Copthall Avenue") is a different concept from
# RISKY_TEXT_FIELDS' free-text detail-loss/richness-regression checks and
# would otherwise auto-apply unreviewed unless it happened to also collide
# with another row in the same batch.
HOUSE_NUMBER_FIELDS = ("address_1", "building")

# Coordinates this pipeline itself generates via geocode.py's own API calls
# whenever a row's source left them blank (see that module's geocode_row) -
# never overwritten there once a row already has both, but that guarantee
# only covers ONE row's own extraction, not a later upload's row being
# diffed against a DIFFERENT, already-correct master value here. No field-
# level provenance exists to tell "this value was explicitly stated" apart
# from "this value was API-generated" once a row reaches this module (see
# build_merge_plan's own risky_fields computation for the full reasoning) -
# unlike address_1/postcode, deliberately left OUT of this list since a
# genuine provider correction there is common and already safe (see
# HOUSE_NUMBER_FIELDS' own structural-change guard), lat/lng has no
# comparable signal at all - see _location_change_risk below for how a
# change here is actually judged (distance + corroborating identity
# evidence), rather than every change being flagged unconditionally.
GEOCODE_RISK_FIELDS = ("lat", "lng")

_EARTH_RADIUS_METERS = 6371000.0


def haversine_distance_meters(lat1, lng1, lat2, lng2) -> float:
    """
    Great-circle distance between two (lat, lng) points in meters - a
    proper geographic distance, not a raw decimal-degree comparison (a
    fixed number of degrees covers very different real distances at
    different latitudes/for lat vs lng, so comparing digits or a flat
    epsilon on the coordinates themselves would be meaningless).
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    a = min(1.0, a)  # clamp against floating-point rounding pushing a hair past 1.0 at ~0 distance
    return 2 * _EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


# Two thresholds, both anchored to a real, confirmed failure mode already
# documented in this project: geocode.py's own docstring records a genuine
# wrong-building Places API match landing "hundreds of meters to over a
# kilometer away" from the real target - i.e. a real wrong match starts
# somewhere in the hundreds of meters. Both thresholds below sit well
# under that with a deliberate safety margin, never approaching it:
#
# - SAME_LOCATION_METERS (50m): a coordinate difference this small is
#   never treated as a meaningful change at all, regardless of any other
#   evidence - it covers the genuine, expected noise of re-geocoding the
#   same real building (rooftop vs street-entrance vs geometric-center
#   anchor, or a slightly different Places/Geocoding API result for the
#   same address), which routinely differs by tens of meters for a single
#   building's own footprint, without ever reaching into "another building
#   down the street" territory - central London buildings can sit as
#   close as a few tens of meters apart, so this is deliberately kept
#   small rather than generously rounded up.
# - CORROBORATED_LOCATION_METERS (150m): the ceiling for auto-resolving a
#   LARGER move, and only ever with independent corroborating evidence
#   (see _has_corroborating_location_evidence) - still comfortably below
#   the "hundreds of meters" real wrong-match floor, wide enough to also
#   cover a materially different anchor point for a large building's own
#   footprint. Beyond this, a change is NEVER auto-resolved regardless of
#   what else agrees - matching+building+floor alone is never enough on
#   its own (see that function's own docstring).
SAME_LOCATION_METERS = 50
CORROBORATED_LOCATION_METERS = 150


def _has_corroborating_location_evidence(old_rec: dict, new_dict: dict) -> bool:
    """
    True only when address_1 AND postcode are BOTH non-blank on both sides
    AND agree (tolerant-equal, see _values_equal/_normalize_postcode) -
    deliberately requiring both together, never just one: building,
    provider, and floor_unit are already guaranteed equal for any row this
    is called on at all (that's what a confident match already means - see
    build_merge_plan), so address_1+postcode agreeing too is genuine EXTRA
    identity evidence beyond the match itself, not a restatement of it.

    No provenance exists (and none is invented here) to tell an explicit
    provider-stated address/postcode apart from a geocoded one - this
    checks only whether the CURRENT upload's own address_1/postcode agree
    with what master already has, regardless of how either originally got
    there. If address_1 or postcode themselves changed too (or either is
    blank on either side), this returns False - a simultaneous address AND
    coordinate change is exactly the case that should still get a human's
    attention (see GEOCODE_RISK_FIELDS' own docstring), not a reason to
    also wave the coordinate change through.
    """
    old_address, new_address = old_rec.get("address_1"), new_dict.get("address_1")
    old_postcode, new_postcode = old_rec.get("postcode"), new_dict.get("postcode")
    if _is_blank(old_address) or _is_blank(new_address) or _is_blank(old_postcode) or _is_blank(new_postcode):
        return False
    return _values_equal(old_address, new_address) and _normalize_postcode(old_postcode) == _normalize_postcode(new_postcode)


def _location_distance_meters(old_rec: dict, new_dict: dict):
    """
    Real distance in meters between old_rec's and new_dict's own (lat, lng)
    pairs, or None if either side is missing one half of its own pair -
    there's no coordinate pair to measure a real distance between in that
    case, so callers fall back to the existing, more conservative "any
    change needs a look" behavior rather than guessing at a partial one.
    Shared by _is_same_location/_location_change_is_safe so both always
    judge lat and lng TOGETHER as one location, never independently.
    """
    old_lat, old_lng = old_rec.get("lat"), old_rec.get("lng")
    new_lat, new_lng = new_dict.get("lat"), new_dict.get("lng")
    if _is_blank(old_lat) or _is_blank(old_lng) or _is_blank(new_lat) or _is_blank(new_lng):
        return None
    return haversine_distance_meters(float(old_lat), float(old_lng), float(new_lat), float(new_lng))


def _is_same_location(old_rec: dict, new_dict: dict) -> bool:
    """
    True when a lat/lng difference is trivially small (see
    SAME_LOCATION_METERS) and should be treated as no meaningful change AT
    ALL - not merely "safe to auto-apply", but removed from the diff
    entirely (see build_merge_plan), so master's existing coordinate is
    left completely untouched rather than being rewritten to a barely-
    different value on every single upload. False whenever either side is
    missing half its own coordinate pair (see _location_distance_meters) -
    handled as an ordinary diff instead, same as before this existed.
    """
    distance = _location_distance_meters(old_rec, new_dict)
    return distance is not None and distance <= SAME_LOCATION_METERS


def _location_change_is_safe(old_rec: dict, new_dict: dict) -> bool:
    """
    True when a lat/lng change on an already-non-blank master coordinate -
    one _is_same_location has already said is NOT trivially small - should
    still be safe to auto-apply anyway: a larger-but-still-plausible move
    corroborated by matching address_1+postcode (see
    _has_corroborating_location_evidence), and only up to
    CORROBORATED_LOCATION_METERS. False (never safe) whenever either side
    is missing one half of its own (lat, lng) pair, or the move exceeds
    CORROBORATED_LOCATION_METERS regardless of any corroboration - matching
    provider+building+floor alone is never enough on its own (see
    GEOCODE_RISK_FIELDS' own docstring).
    """
    distance = _location_distance_meters(old_rec, new_dict)
    if distance is None or distance > CORROBORATED_LOCATION_METERS:
        return False
    return _has_corroborating_location_evidence(old_rec, new_dict)

# Free-text fields where wording indicating a property is no longer on the
# market might appear - the two descriptive prose fields, never a matching
# key or a structured numeric field. Checked against a MATCHED row's DIFF
# (the new value only - see build_merge_plan) since a status that was
# already there unchanged never shows up as a diff at all.
LET_STATUS_FIELDS = ("special_features", "state_of_space")

# Checked against this repo's real sample documents (tests/sample_docs)
# rather than assumed - "Let"/"Leased"/"No longer available"/"Withdrawn"
# are the user's own starting vocabulary; "Under Offer" and "Occupied" are
# confirmed present in real documents in this repo: 40_New_Bond_Street_
# Brochure.pdf's Schedule of Areas lists a floor's Availability as "Under
# Offer" (also repeated across Office_Space_by_The_Crown_Estate_-_July_2026.
# pdf), and Breezblok.pdf's own brochure states "The centre is now 100%
# Occupied" - the exact live scenario this feature exists for: a
# re-upload's wording implying a unit is no longer genuinely available.
LET_STATUS_KEYWORDS = (
    "let", "leased", "no longer available", "withdrawn", "under offer", "occupied",
)


def mentions_let_status(text) -> bool:
    """
    True if text contains wording suggesting the property is no longer on
    the market - see LET_STATUS_KEYWORDS. Word-boundary matching throughout,
    with "let" specifically excluding the "pre-let"/"re-let"/"sub-let"
    compound forms (confirmed present in these same sample documents, e.g.
    GPE.eml's "high pre-let demand" note about a DIFFERENT building's
    overall leasing momentum) - those describe a leasing trend, not this
    specific unit's own current availability, and would otherwise be a
    false positive on real data.
    """
    if _is_blank(text):
        return False
    lowered = str(text).lower()
    for kw in LET_STATUS_KEYWORDS:
        pattern = rf"\b{re.escape(kw)}\b"
        if kw == "let":
            pattern = r"(?<!pre-)(?<!re-)(?<!sub-)" + pattern
        if re.search(pattern, lowered):
            return True
    return False

# Threshold for _items_similar - a review trigger, not a block, so this is
# deliberately lenient (a genuinely shorter-but-current update still goes
# through once a human confirms it in manual review). Expressed as a
# fraction of the SMALLER item's significant-word count, not the total -
# see _items_similar's docstring for why.
ITEM_SIMILARITY_THRESHOLD = 0.5

# Dropped when tokenizing an item for _items_similar - common words that
# would otherwise register as "shared content" between two items that don't
# actually share any real subject matter (e.g. "with" appearing in both
# "terrace with views" and "kitchen with dishwasher" says nothing about
# whether either one has a counterpart in the other's item list).
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "with", "from", "for", "to",
    "in", "on", "at", "by", "is", "are", "this", "that", "has", "have",
})


def normalize_key(value) -> str:
    """Lowercase, strip punctuation, collapse whitespace - deliberately
    conservative (e.g. doesn't strip a leading "The"), so a near-miss surfaces
    as "no match" for a human to catch rather than being silently guessed
    away by more aggressive fuzzy logic."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).lower()
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_postcode(value) -> str:
    """normalize_key() plus full whitespace removal, for postcode specifically.

    A postcode written with or without the space before its inward code
    ("W1T 4PX" vs "W1T4PX") is the same postcode, purely a formatting
    difference - not the kind of near-miss normalize_key's own docstring
    means to preserve as "no match" for a human to catch (that's about
    genuinely different wording, e.g. a missing "The", not whitespace).
    _values_equal/_normalize_text already treat this kind of formatting
    difference as identical for diffing; the match key was the one place
    still treating them as different postcodes, which could send a
    postcode-inclusive match to the fallback tier for no real reason."""
    return re.sub(r"\s+", "", normalize_key(value))


def _canonical_provider_key(value) -> str:
    """Comparison key for canonicalize_provider_name - like normalize_key,
    but "+"/"&" are expanded to their word form FIRST. normalize_key alone
    would just delete "+"/"&" as punctuation, losing the concept they stand
    for entirely (e.g. "Workplace+" would become "workplace", one word, not
    "workplaceplus" - and could then spuriously equal some unrelated
    single-word provider). All whitespace is then stripped, like
    _normalize_postcode, so "Workplace Plus" and "Workplace+" compare equal
    regardless of spacing."""
    text = str(value or "").replace("+", " plus ").replace("&", " and ")
    return re.sub(r"\s+", "", normalize_key(text))


# Known real providers seen across uploads so far, in their correct verbatim
# spelling - derived from every distinct provider value observed in real
# data this project has processed so far (see canonicalize_provider_name's
# docstring for the specific confirmed variants this was checked against).
# Deliberately a small, explicit, hand-maintained list rather than a
# general-purpose fuzzy match against anything: this only ever corrects
# TOWARD one of these specific, already-confirmed-real names, never invents
# a "closest" canonical spelling for a provider that isn't on it.
KNOWN_PROVIDERS = (
    "Business Cube",
    "GPE",
    "JLL / HK London",
    "Kitt's",
    "Knotel",
    "MetSpace",
    "UNION",
    "Workplace Plus",
)

_CANONICAL_PROVIDER_BY_KEY = {_canonical_provider_key(p): p for p in KNOWN_PROVIDERS}


def canonicalize_provider_name(value):
    """
    Corrects value to its known-correct spelling from KNOWN_PROVIDERS when
    it's a recognizable variant of one of them (case, "+"/"Plus"/"&"/"and",
    or minor punctuation/whitespace difference - see _canonical_provider_
    key); returns value completely unchanged otherwise, including for every
    provider not yet on that list. That's deliberate: the point is to fix
    known, already-confirmed spelling drift for names this project has
    already seen, not to guess at correctness for one it hasn't - a
    genuinely new real provider should be added to KNOWN_PROVIDERS once
    confirmed, not silently matched against something close on the list by
    a looser heuristic. Same "conservative, human catches it" philosophy as
    normalize_key itself, applied one level earlier - fixing the value at
    the source instead of loosening how match keys compare it.

    Confirmed against real extraction non-determinism: the same real "77
    Gracechurch Street" brochure PDF, extracted repeatedly, returned
    "Workplace Plus" (its own literal document text - "At Workplace Plus,
    we believe..."), "Workplace+", and "WORKPLACE+" across different runs -
    all three canonicalize to "Workplace Plus" here. Real spreadsheet data
    separately shows "MetSpace" vs "Metspace" the same way.
    """
    if _is_blank(value):
        return value
    return _CANONICAL_PROVIDER_BY_KEY.get(_canonical_provider_key(value), value)


# Sheet-title-derived provider text can carry a trailing word describing the
# SHEET's purpose/content rather than who the provider actually is - real,
# confirmed case: a single uploaded Copthall Estates workbook where one
# sheet's own title/branding text is literally "Copthall Estates" (a
# portfolio-wide rollup) and another sheet's is "Copthall Estates
# Availibility" (that exact misspelling, not "Availability" - confirmed
# against the real reported bug) for a per-area detail sheet.
# extract_spreadsheet_gemini.py's PROMPT correctly instructs verbatim
# transcription of each sheet's own title, so Gemini reporting these as two
# different literal provider strings is itself correct extraction, not a
# bug there - but left uncorrected, provider is part of every matching key
# (_fallback_key/_primary_key/_dedup_key/_fuzzy_anchor_key below), so the
# exact same real units extracted from each sheet never recognize each
# other as the same provider. Confirmed real symptom: the same Throgmorton
# Avenue 2nd Floor unit appearing twice as two separate "no match found -
# will be added as new" rows, one under each spelling, instead of one
# flagged batch duplicate. A small, explicit, hand-maintained set of
# confirmed sheet-purpose words - same "conservative, human catches it"
# philosophy as KNOWN_PROVIDERS/_STREET_SUFFIX_EXPANSIONS/LET_STATUS_
# KEYWORDS in this same file - not a general "strip anything that looks
# like a suffix" heuristic, so a provider whose real name genuinely ends
# in some other word is never touched.
_PROVIDER_PURPOSE_SUFFIXES = ("availability", "availibility")


def _strip_provider_purpose_suffix(value):
    """
    Drops a trailing sheet-purpose word (see _PROVIDER_PURPOSE_SUFFIXES)
    from value's own last word, if present - e.g. "Copthall Estates
    Availability" / "Copthall Estates Availibility" both -> "Copthall
    Estates". Applies to ANY provider string, not just ones already on
    KNOWN_PROVIDERS - unlike canonicalize_provider_name, this isn't
    correcting toward an already-confirmed spelling, it's removing a word
    that was never part of the provider's name at all, and that's true
    whether or not this project has seen this particular provider before.

    Returns value completely unchanged when its last word isn't one of
    these, OR when the value is nothing BUT that one word (e.g. a bare
    "Availability" with no provider name at all) - stripping every word
    would leave nothing meaningful, and a value this bare was never a
    confident provider extraction to begin with, so there's nothing safe
    to correct it toward.
    """
    if _is_blank(value):
        return value
    words = str(value).split()
    if len(words) > 1 and words[-1].lower() in _PROVIDER_PURPOSE_SUFFIXES:
        return " ".join(words[:-1])
    return value


def canonicalize_providers(rows: list[ListingRow]) -> None:
    """Applies _strip_provider_purpose_suffix then canonicalize_provider_name
    to every row's provider in place - the list[ListingRow]-mutating
    counterpart to app.py's fill_missing_provider/fill_missing_address_from_
    building, meant to run right alongside them: after extraction/mapping
    (Gemini or spreadsheet) has produced a provider value, before that row
    is ever used for matching or diffing. Suffix-stripping runs first since
    it can turn an otherwise-unrecognized "X Availability" into the bare "X"
    that KNOWN_PROVIDERS might then separately recognize."""
    for row in rows:
        row.provider = canonicalize_provider_name(_strip_provider_purpose_suffix(row.provider))


# Providers whose spreadsheet upload always represents that provider's ENTIRE
# current portfolio, never a partial one-area/one-building export - a small,
# explicit, hand-maintained allow-list, same "conservative, human catches it"
# philosophy as KNOWN_PROVIDERS. find_stale_candidates is scoped to these
# providers ONLY: for any other provider, a property genuinely absent from
# the latest upload says nothing about whether it's still on the market (a
# partial export covering just one area/building is common and expected -
# see that function's own docstring), so treating absence as evidence of
# staleness there would be unsafe. Confirmed against the real Copthall
# Estates Availability.xlsx: every upload is that provider's full City/Mid
# Town/Westend Soho/Blackfriars portfolio, sheet-per-area, never a subset.
COMPLETE_SNAPSHOT_PROVIDERS = ("Copthall Estates",)


def _is_complete_snapshot_provider(provider) -> bool:
    if _is_blank(provider):
        return False
    return canonicalize_provider_name(_strip_provider_purpose_suffix(provider)) in COMPLETE_SNAPSHOT_PROVIDERS


def find_stale_candidates(
    new_rows: list, master_records: list, matched_master_indices: set, fully_occupied_buildings: list = None,
) -> list:
    """
    master_index values (into master_records) for existing master rows this
    upload batch gives strong, narrowly-scoped evidence are no longer
    available - never a blanket "missing from this upload" check (see
    COMPLETE_SNAPSHOT_PROVIDERS' own docstring for why that would be unsafe
    for most providers/uploads).

    A master row is a stale candidate only when ALL of:
    - its own provider (canonicalized) is on COMPLETE_SNAPSHOT_PROVIDERS;
    - its master_index isn't already in matched_master_indices - a row this
      exact batch matched (with or without a diff) obviously still exists,
      whatever else is true below;
    - this upload has real evidence about its own building specifically -
      either another row in new_rows shares that same provider+building
      (normalize_key-equal - the building WAS covered by this upload, just
      not this exact floor/unit), or that building's own name appears in
      fully_occupied_buildings for the same provider (the source sheet
      explicitly states zero current availability for it - see extract_
      spreadsheet_gemini.extract_sheet_with_metadata). A building this
      upload never mentions AT ALL is not evidence of anything - exactly
      the "this upload only covers one area/building" case that must never
      be punished, and the real reason this is safe to scope by provider
      alone rather than needing to separately confirm the WHOLE FILE is a
      complete snapshot: a partial upload from an allow-listed provider
      still only ever flags floors within buildings it actually mentions.
    - and, when the building wasn't marked fully-occupied specifically,
      no row in new_rows for that same provider+building shares this
      master row's own floor_unit (normalize_key-equal) - i.e. this exact
      unit has no counterpart in the fresh data either.

    Returns master_index values only - never mutates master_records or
    decides removal itself. pages/2_Review_and_Master.py surfaces these for
    an explicit human keep/remove decision and feeds a "remove" choice into
    apply_merge's existing removed_indices parameter - the exact same
    mechanism mentions_let_status already uses (see _render_let_status_
    decision), reused here rather than duplicated.
    """
    fully_occupied_keys = {
        (canonicalize_provider_name(_strip_provider_purpose_suffix(fo.get("provider"))), normalize_key(fo.get("building")))
        for fo in (fully_occupied_buildings or [])
        if not _is_blank(fo.get("building"))
    }

    covered_buildings = set()  # (provider_key, building_key) mentioned anywhere in new_rows
    covered_units = {}  # (provider_key, building_key) -> set of floor_unit_key mentioned in new_rows
    for row in new_rows:
        if not _is_complete_snapshot_provider(row.provider):
            continue
        combo = (canonicalize_provider_name(_strip_provider_purpose_suffix(row.provider)), normalize_key(row.building))
        covered_buildings.add(combo)
        covered_units.setdefault(combo, set()).add(normalize_key(row.floor_unit))

    stale = []
    for i, rec in enumerate(master_records):
        if i in matched_master_indices:
            continue
        if not _is_complete_snapshot_provider(rec.get("provider")):
            continue
        combo = (
            canonicalize_provider_name(_strip_provider_purpose_suffix(rec.get("provider"))),
            normalize_key(rec.get("building")),
        )

        if combo in fully_occupied_keys:
            stale.append(i)
            continue

        if combo not in covered_buildings:
            continue  # this upload never mentioned this building at all - no evidence either way

        if normalize_key(rec.get("floor_unit")) not in covered_units.get(combo, set()):
            stale.append(i)

    return stale


def field_kind(field_name: str) -> str:
    """"int" | "float" | "str" - derived from ListingRow's own type hints
    (via typing.get_args on the Optional[...] annotation) rather than a
    hardcoded list, so it can't drift out of sync with schema.py."""
    annotation = ListingRow.model_fields[field_name].annotation
    args = typing.get_args(annotation)
    base = next((a for a in args if a is not type(None)), annotation)
    if base is int:
        return "int"
    if base is float:
        return "float"
    return "str"


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _normalize_text(value) -> str:
    """Case- and whitespace-insensitive form of a text value, for tolerant
    comparison only (never used for what's actually stored) - lowercases
    and discards ALL whitespace, not just repeated runs, so a formatting-
    only difference like "+44(0)7837 270455" vs "+44(0)7837270455", or a
    stray capitalization difference like "METSPACE" vs "Metspace", doesn't
    register as a real change. Punctuation is left alone: a dash swapped
    for a space (or missing entirely) still compares different, since that
    can indicate an actual typo or a different source worth a human's
    attention, unlike whitespace/case."""
    return re.sub(r"\s+", "", str(value).strip().lower())


def _values_equal(old, new) -> bool:
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return abs(float(old) - float(new)) < 1e-6
    return _normalize_text(old) == _normalize_text(new)


def diff_fields(old: dict, new: dict) -> dict:
    """
    Only fields present in `new` (non-blank), different from `old`, and NOT
    merely a tolerant-equal formatting difference (see _values_equal /
    silent_field_updates) are included - a blank/missing value in a fresh
    extraction is treated as "no data this time", never as a change that
    would blank out an existing value. A field filled in for the first time
    (old blank, new has a value) is treated as a change like any other.
    Returns {field: (old, new)}.
    """
    diffs = {}
    for f in DIFF_FIELDS:
        new_val = new.get(f)
        if _is_blank(new_val):
            continue
        old_val = old.get(f)
        if _is_blank(old_val) or not _values_equal(old_val, new_val):
            diffs[f] = (old_val, new_val)
    return diffs


def silent_field_updates(old: dict, new: dict) -> dict:
    """
    Text fields where `new` is tolerant-equal to `old` (see _values_equal)
    but not byte-identical - e.g. a phone number gaining a missing space, or
    "METSPACE" replacing "Metspace". diff_fields() correctly excludes these
    from the review-worthy diff (see its docstring), but the improved
    formatting should still replace the old value in master rather than
    being silently discarded on every future upload - it's just applied
    without ever surfacing as a "change" a human needs to review. Restricted
    to str-kind fields: numeric near-equality (already tolerated by
    _values_equal) is a different, pre-existing concern and isn't affected
    here. Returns {field: new_value}.
    """
    updates = {}
    for f in DIFF_FIELDS:
        if field_kind(f) != "str":
            continue
        new_val = new.get(f)
        if _is_blank(new_val):
            continue
        old_val = old.get(f)
        if _is_blank(old_val):
            continue
        if str(old_val) == str(new_val):
            continue
        if _values_equal(old_val, new_val):
            updates[f] = new_val
    return updates


def _detail_items(text: str, field_name: str = None) -> list[str]:
    """Splits a free-text list field (special_features, contacts) into its
    individual items - on ";" and newline ONLY, matching the delimiters
    those fields are actually documented to use (see extract.py's
    extraction prompt for special_features: "a semicolon-separated list...
    e.g. '2 meeting rooms; deposit £36,000 required'"; schema.py's comment
    on contacts: "one per line/semicolon"). Deliberately NOT comma - a
    single genuine item routinely contains one on its own (that exact
    "£36,000" example), and contacts' own per-person format is "Name,
    email, phone", so splitting on comma there would shred one contact
    into three fake items. The cost of this: a value that isn't actually
    semicolon-itemized (e.g. a single comma-joined sentence with no ";" at
    all) is treated as ONE item rather than several - see is_detail_loss's
    docstring for why that's the right trade-off anyway.

    A too-short fragment (stray punctuation/an empty trailing segment from
    a trailing ";") is dropped; it's not solid enough evidence either way
    for the similarity check in is_detail_loss to build on.

    field_name, when it's "special_features" specifically (see
    _MERGE_COMMA_SPLIT_FIELDS), ALSO splits each semicolon/newline chunk
    further on a comma immediately followed by whitespace - a real list
    separator ("Private terrace, 10-person boardroom" -> 2 items) - never a
    comma immediately followed by more digits ("36,000", a thousands
    separator - never followed by whitespace, so never split). Contacts is
    deliberately excluded even when comma-splitting would otherwise apply -
    its own per-person format IS "Name, email, phone", so this same rule
    would shred one contact's own details into three fake items, exactly
    the failure mode the paragraph above already explains rejecting comma-
    splitting for generally. Defaults to None (the original, unchanged
    semicolon/newline-only split) so every existing caller that doesn't
    pass this is completely unaffected.
    """
    parts = re.split(r"[;\n]+", text.lower())
    items = [p.strip() for p in parts if len(p.strip()) >= 3]
    if field_name not in _MERGE_COMMA_SPLIT_FIELDS:
        return items
    expanded = []
    for item in items:
        expanded.extend(p.strip() for p in re.split(r",\s+", item) if len(p.strip()) >= 3)
    return expanded


def _significant_words(item: str) -> frozenset:
    """Lowercased alphanumeric tokens with stopwords and very short
    fragments (len <= 2 - "a", "of", a stray unit fragment) removed - the
    comparable "content" of one item for _items_similar, order-independent
    so word-reordering in a rewording never itself looks like a mismatch."""
    words = re.findall(r"[a-z0-9]+", item.lower())
    return frozenset(w for w in words if len(w) > 2 and w not in _STOPWORDS)


def _items_similar(item_a: str, item_b: str) -> bool:
    """
    True if item_a and item_b look like the same underlying fact, tolerating
    rewording - e.g. "Benefits from a large private terrace landscaped with
    plants, trees and premium Italian outdoor furniture" and "Private
    landscaped terrace" are the same fact at different lengths, not two
    different facts.

    Measured as shared significant words divided by the SMALLER item's
    word count, not the total (i.e. not a symmetric Jaccard/union-based
    ratio) - a short rewording's words are typically close to a full subset
    of the longer original's, so this rewards exactly that shape of match
    (a compressed paraphrase) rather than penalizing it for not sharing the
    longer item's other, dropped words too. Order-independent - a set
    intersection, not a sequence-alignment score - since paraphrasing
    routinely reorders words ("large private terrace landscaped" vs
    "private landscaped terrace") without changing the underlying fact.

    An exact match (case/whitespace-normalized) is always similar, checked
    BEFORE the significant-words comparison below - confirmed real gap this
    guards against: a short, abbreviation/number-heavy item like "4 MR + 3
    PB" tokenizes to "4", "mr", "3", "pb", every one of them <= 2 characters
    and therefore filtered out by _significant_words entirely, leaving an
    EMPTY significant-words set on both sides. The comparison below treats
    two empty sets as "nothing to compare" and returns False - which, with
    no exact-match check first, wrongly concluded an item was DROPPED even
    when it was restated in new_val completely verbatim, both corrupting
    is_detail_loss's own review trigger (a false "something was lost") and
    merge_compatible_text's merged output (re-appending old_val's own copy
    of an item new_val already states, producing a literal duplicate like
    "4 MR + 3 PB; Available: December; 4 MR + 3 PB"). This never weakens
    genuine detail-loss detection - two items that are actually different,
    however short, still correctly fall through to the significant-words
    comparison (or fail it) exactly as before.
    """
    if " ".join(item_a.lower().split()) == " ".join(item_b.lower().split()):
        return True

    words_a, words_b = _significant_words(item_a), _significant_words(item_b)
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b)
    return overlap / min(len(words_a), len(words_b)) >= ITEM_SIMILARITY_THRESHOLD


def is_detail_loss(old_val, new_val, field_name: str = None) -> bool:
    """
    True when `new_val` looks like it dropped a genuine item `old_val` had,
    rather than just rewording/shortening it - e.g. a brochure re-upload's
    special_features carrying just "Available Q3 2026" over a master row
    whose special_features listed six amenities (dropped everything), vs.
    a long single-fact description compressed into a short rewording of the
    same fact (not a drop at all - see _items_similar).

    Splits both values into items (see _detail_items) and flags if ANY of
    old_val's items has no reasonably similar counterpart (see
    _items_similar) anywhere in new_val's items - a real item disappearing
    with nothing standing in for it, not merely "new_val is short" or
    "these exact characters aren't in new_val verbatim" - a length or
    substring check flags legitimate rewording as a false positive (a long
    fact compressed into a short paraphrase looks identical, by either of
    those measures, to a fact being deleted outright).

    field_name is passed straight through to _detail_items (see its own
    docstring) - defaults to None, the original semicolon/newline-only
    split, so every existing call site (there are several - the risky-
    field gate below, the peer-collision path, direct test calls) keeps
    its exact prior behavior unless it explicitly opts into comma-splitting.

    Used two ways: as a review trigger (the original, still-used meaning -
    see RISKY_TEXT_FIELDS' own docstring - never applied automatically on
    its own) AND, when field_name is given, as the gate for whether
    merge_compatible_text should run at all (see build_merge_plan) - in
    that second use, TRUE does not mean "force a review", it means "there's
    a genuine old item worth carrying forward into an automatic merge".
    """
    if _is_blank(old_val) or _is_blank(new_val):
        return False

    old_items = _detail_items(str(old_val), field_name)
    new_items = _detail_items(str(new_val), field_name)
    if not old_items:
        return False

    return any(not any(_items_similar(old_item, new_item) for new_item in new_items) for old_item in old_items)


def _split_list_items(text: str, field_name: str = None) -> list[str]:
    """
    Case-PRESERVING counterpart to _detail_items - same splitting rules
    (semicolon/newline always, plus comma-then-whitespace for
    special_features specifically - see that function's own docstring),
    but keeps each item's original text intact rather than lowercasing it,
    since merge_compatible_text's output is real text a person reads, not
    just a comparison key.
    """
    parts = re.split(r"[;\n]+", text)
    items = [p.strip() for p in parts if len(p.strip()) >= 3]
    if field_name not in _MERGE_COMMA_SPLIT_FIELDS:
        return items
    expanded = []
    for item in items:
        expanded.extend(p.strip() for p in re.split(r",\s+", item) if len(p.strip()) >= 3)
    return expanded


# Small, explicit, hand-maintained EXTRA signal that a NEW item is stating
# a feature's removal/unavailability rather than just omitting it - things
# LET_STATUS_KEYWORDS doesn't already cover, since that list is about the
# whole LISTING no longer being on the market, not one specific feature
# ("gym removed", "not included") - checked ALONGSIDE mentions_let_status
# below, not instead of it. Same "conservative, human-curated list, never
# generalized NLP" philosophy as LET_STATUS_KEYWORDS/KNOWN_PROVIDERS/
# _STREET_SUFFIX_EXPANSIONS. Used only by merge_compatible_text, and only
# to decide whether an OLD item that shares a topic with this new one
# should be dropped rather than carried forward - never to invent or
# reword anything; the new item's own text is always used exactly as given.
_FEATURE_NEGATION_KEYWORDS = ("no longer", "removed", "not available", "unavailable", "not included")


def _item_mentions_negation(item: str) -> bool:
    """
    True if `item` reads as stating something is no longer there/available -
    reuses mentions_let_status (this file's own existing "let"/"under
    offer"/"withdrawn"/etc. detector, complete with its word-boundary and
    pre-/re-/sub-let exclusions) as the PRIMARY signal, since a feature-
    level phrase like "Under Offer" is exactly the same vocabulary as a
    whole-listing one - confirmed against real data: a real Knotel update's
    "Under Offer" replacing an old "Available: Now" was, before this reuse,
    concatenated into a nonsensical "Under Offer; Available: Now" because
    the negation list here didn't happen to include a word LET_STATUS_
    KEYWORDS already had. _FEATURE_NEGATION_KEYWORDS adds a few extra,
    narrower words that are about one feature specifically, not the whole
    listing, which mentions_let_status was never meant to catch.
    """
    if mentions_let_status(item):
        return True
    lowered = item.lower()
    return any(kw in lowered for kw in _FEATURE_NEGATION_KEYWORDS)


def _item_shares_a_topic(item_a: str, item_b: str) -> bool:
    """
    True if item_a and item_b share ANY significant word at all - a much
    weaker bar than _items_similar's own ratio (which decides "these are
    the same restated fact"). Used only alongside _item_mentions_negation
    in merge_compatible_text: two items can plausibly be ABOUT the same
    subject (share a topic) without being similar enough to count as a
    reword of each other - e.g. "Manned reception desk" and "Reception no
    longer staffed" share only "reception", nowhere near _items_similar's
    own 0.5 threshold, but are obviously about the same fact turning false.
    """
    return bool(_significant_words(item_a) & _significant_words(item_b))


def _is_availability_statement(item: str) -> bool:
    """
    True if `item` itself reads as a current-availability-status claim
    ("Available", "Available: Now", "Available from September 1, 2026",
    "Under Offer", "Let", ...) rather than an independent amenity/fact.

    Confirmed necessary against a REAL Knotel availability update: an old
    "Available: Now" and a new "Under Offer" share NO significant word at
    all (so _item_shares_a_topic - the general negation-drop's own gate -
    never fires for them), yet these are obviously two competing claims
    about the exact same thing, not two independent facts - concatenating
    them ("Under Offer; Available: Now") is nonsensical. Confirmed correct
    per extract.py's own PROMPT, which documents availability timing as
    belonging in special_features, not state_of_space - Knotel's own real
    availability emails state exactly this kind of phrase per unit, and
    two claims of this kind never coexist the way "private terrace" and
    "newly fitted kitchen" safely do; one always supersedes the other,
    exactly like a scalar/current-state field, even though this
    particular fact happens to live inside special_features's own text.

    mentions_let_status already recognizes the "no longer on the market"
    subset of this vocabulary; "available" is the one further word needed
    to recognize the affirmative side of the same concept.
    """
    if mentions_let_status(item):
        return True
    return bool(re.search(r"\bavailable\b", item.lower()))


def merge_compatible_text(old_val, new_val, field_name: str = None) -> str:
    """
    The value to use for a RISKY_TEXT_FIELDS update once is_detail_loss has
    already established old_val has at least one item new_val doesn't
    restate, AND is_richness_regression has already established new_val
    isn't a drastically terser replacement (see build_merge_plan's own
    call site - never called directly on a pair that failed either check).

    Preserves BOTH sources' useful, non-conflicting information rather
    than either extreme: never a blind overwrite (which would silently
    drop whatever old_val's items weren't restated in new_val) and never a
    blind concatenation (which would keep a fact new_val has explicitly
    negated sitting right alongside its own contradiction). Result order is
    new_val's own items first (in new_val's own current wording - the
    freshest source for anything it actually restates), then any old_val
    item that's neither:
    - already restated/reworded in new_val (see _items_similar) - would
      just duplicate what new_val already says, in old_val's stale wording,
    - explicitly negated by a new_val item sharing its topic (see
      _item_shares_a_topic/_item_mentions_negation) - carrying that item
      forward would concatenate a claim new_val has just contradicted, or
    - itself a current-availability-status claim (see _is_availability_
      statement) that a new_val item of the SAME kind supersedes - these
      never coexist as two independent facts (see that function's own
      docstring for the real case this covers, where the two sides share
      no word at all so the topic-overlap check above can't catch it).

    Always joined with "; ", the field's own documented canonical
    separator (see extract.py's PROMPT) - regardless of whether either
    source used commas, semicolons, or a mix; normalizing the join
    character is not a loss of information, just a formatting choice.
    """
    new_items = _split_list_items(str(new_val), field_name)
    old_items = _split_list_items(str(old_val), field_name)

    merged = list(new_items)
    for old_item in old_items:
        if any(_items_similar(old_item, new_item) for new_item in new_items):
            continue
        if any(
            _item_shares_a_topic(old_item, new_item) and _item_mentions_negation(new_item)
            for new_item in new_items
        ):
            continue
        if _is_availability_statement(old_item) and any(_is_availability_statement(ni) for ni in new_items):
            continue
        merged.append(old_item)

    return "; ".join(merged) if merged else str(new_val)


# Threshold for is_richness_regression - a new value under HALF of the old
# value's word count is flagged for review even when is_detail_loss finds no
# missing item. Matches ITEM_SIMILARITY_THRESHOLD's own 0.5, for the same
# reason: lenient enough that a genuinely current, if terser, update still
# goes through once a human confirms it - this is a review trigger, never a
# block.
#
# Confirmed against every real/test old-new pair available when this was
# added: correctly catches the real Bond Street amenity-list-to-one-liner
# case (ratio 0.25) and the "Fully fitted, CAT A+ finish, breakout area,
# phone booths, kitchen" -> "Fitted" compression (0.10, is_detail_loss's own
# documented blind spot - no ";" to itemize on, so item-loss can't see this
# one at all) without reintroducing a flag on the reword-with-growth cases
# (1.22, 1.33) or the real Gracechurch St re-listing pair (1.03).
#
# One known, accepted false positive: "Benefits from a large private
# terrace landscaped with plants, trees and premium Italian outdoor
# furniture" -> "Private landscaped terrace" (0.20) is a legitimate
# compressed paraphrase of ONE fact - is_detail_loss already recognizes
# this correctly (see its own tests), but a pure length ratio structurally
# cannot tell it apart from the amenity-loss case above, since it shrinks
# by an even larger ratio (0.20 < 0.25). That's an accepted trade-off, not
# a bug: this check only ever forces a manual review, never a silent
# discard, so the cost is one extra glance from a human, not a wrong
# answer applied automatically.
RICHNESS_RATIO_THRESHOLD = 0.5


def is_richness_regression(old_val, new_val) -> bool:
    """
    True when new_val's word count is under RICHNESS_RATIO_THRESHOLD of
    old_val's - a coarse "did this get meaningfully less descriptive" check,
    independent of is_detail_loss's per-item comparison. Exists for exactly
    the case is_detail_loss's own docstring documents as its accepted blind
    spot: text with no ";"/newline to itemize on collapses to a single
    "item" for is_detail_loss, so a long comma-joined or prose value
    compressed hard passes item-loss cleanly even though real content
    vanished (see RICHNESS_RATIO_THRESHOLD's own comment for the real
    example this exists to catch).

    Doesn't try to tell "reworded" from "genuinely lost" apart - see
    RICHNESS_RATIO_THRESHOLD's own comment on the terrace-rewording false
    positive this accepts - it only measures raw shrinkage, and always
    routes to the same manual-review gate as is_detail_loss rather than
    deciding anything on its own.

    Deliberately raw whitespace word count, not is_detail_loss's stopword-
    filtered significant-word count - keeping this a genuinely separate,
    simpler signal rather than a second implementation of the same
    semantic-overlap idea.
    """
    if _is_blank(old_val) or _is_blank(new_val):
        return False

    old_words = len(str(old_val).split())
    new_words = len(str(new_val).split())
    return new_words / old_words < RICHNESS_RATIO_THRESHOLD


def _floor_unit_key(building, floor_unit) -> str:
    """
    normalize_key(floor_unit), with a redundant leading occurrence of the
    row's OWN building name stripped first - confirmed real shape: the same
    real unit's floor_unit extracted as "Hallmark 6th Floor" in one upload
    and plain "6th Floor" in another (building "Hallmark" in both) - an
    exact normalize_key comparison never reconciles these two strings even
    though a human reads them as unambiguously the same floor, since
    building+provider already agree and only floor_unit carries the
    redundant building-name prefix one source happened to repeat.

    Deliberately a plain prefix strip, never a fuzzy/similarity match: only
    fires when floor_unit's own normalized text literally BEGINS with
    building's own normalized text followed by a word boundary - "Hallmark
    6th Floor" strips to "6th floor" (building "Hallmark"), but "Hallmark
    House 6th Floor" does NOT strip against building "Hallmark" (the next
    character after the shared prefix is "h", not a boundary) - a genuinely
    different, longer building name is never partially matched against a
    shorter one this way. A floor_unit that just short of literally repeats
    the building name (any other real-world phrasing) falls through
    unchanged, exactly as before this existed - still exact-match-only,
    same "conservative, human catches it" philosophy as normalize_key
    itself.
    """
    floor_key = normalize_key(floor_unit)
    building_key = normalize_key(building)
    if building_key and floor_key.startswith(building_key):
        rest = floor_key[len(building_key):]
        if rest == "" or rest[0] == " ":
            return rest.strip()
    return floor_key


def _fallback_key(row: dict) -> tuple:
    return (
        normalize_key(row.get("building")),
        normalize_key(row.get("provider")),
        _floor_unit_key(row.get("building"), row.get("floor_unit")),
    )


# Similarity floor for the fuzzy building-name tier below (see
# _fuzzy_building_match) - validated against every real building name seen
# across Kitt's/UNION/Knotel/MetSpace data in this project: the two real
# extraction-nondeterminism typos actually seen ("Thirty Lighterman" vs
# "Thirty Lightman" -> 0.938, "Conran Building" vs "Coman Building" ->
# 0.897) both clear this comfortably, while the closest UNRELATED pure-name
# pair in that same real dataset ("Orion House" vs "York House" - the
# "York House, 23 Kingsway" compound's name part) tops out at 0.762 - a
# real, checked margin, not a guess.
BUILDING_FUZZY_MATCH_THRESHOLD = 0.85

_DIGIT_RE = re.compile(r"\d")


def _building_name_only(building) -> str:
    """The NAME part of a compound "Name, Street Address" building value
    (e.g. "Bridge House, 22 Newman Street" -> "Bridge House") - same
    concept as geocode.split_compound_building, applied here so a compound
    value's address portion never leaks into the fuzzy comparison below.
    A non-compound value passes through unchanged."""
    text = str(building)
    return text.split(", ", 1)[0] if ", " in text else text


def _building_has_no_digits(building) -> bool:
    """A numbered address ("138 Cheapside") must never be fuzzy-matched -
    confirmed against real master data that a genuinely DIFFERENT real
    property routinely scores HIGHER than a genuine typo does ("20 St
    James's Square" vs "30 St James's Square", different postcodes, both
    real listings -> 0.950; "108 Cannon Street" vs "120 Cannon Street" ->
    0.941 - both above either real typo pair's own score). Checks the WHOLE
    value, deliberately NOT just the name part of a compound "Name, Street
    Address" building (see _building_name_only) - a compound value's own
    address portion is exactly as much a numbered address as a bare one,
    so it's excluded from this tier entirely too, not just fuzzy-matched
    on its name part alone. Only a genuinely digit-free building (a pure
    name, with no address suffix at all) is eligible; a numbered address
    stays exact-match-only, exactly as before this tier existed."""
    if _is_blank(building):
        return False
    return not _DIGIT_RE.search(str(building))


def _fuzzy_anchor_key(row: dict) -> tuple:
    """Same anchor as _fallback_key, but WITHOUT building - the fuzzy
    building-name tier's grouping key, since building itself is the one
    field this tier ever tolerates a near-miss on. provider+floor_unit
    must still match exactly; postcode is deliberately not required here
    either (a pure-name building routinely has no postcode stated at all
    on some sources), but every candidate still needs a real fuzzy score
    above BUILDING_FUZZY_MATCH_THRESHOLD to match at all."""
    return (normalize_key(row.get("provider")), normalize_key(row.get("floor_unit")))


def _fuzzy_building_match(new_dict: dict, candidate_indices: list, master_records: list) -> int:
    """
    Among master rows sharing new_dict's exact _fuzzy_anchor_key (provider+
    floor_unit), returns the single master_index whose building name is a
    safe fuzzy match for new_dict's building - or None if new_dict's
    building is a numbered address (see _building_has_no_digits), no
    candidate clears BUILDING_FUZZY_MATCH_THRESHOLD, or more than one does
    (ambiguous - exactly as unsafe to auto-apply as the existing fallback
    tier's own ">1 candidates" case, so it falls through the same way:
    unmatched, for a human to resolve).
    """
    new_building = new_dict.get("building")
    if not _building_has_no_digits(new_building):
        return None
    new_key = normalize_key(_building_name_only(new_building))

    matches = []
    for idx in candidate_indices:
        old_building = master_records[idx].get("building")
        if not _building_has_no_digits(old_building):
            continue
        old_key = normalize_key(_building_name_only(old_building))
        ratio = difflib.SequenceMatcher(None, new_key, old_key).ratio()
        if ratio >= BUILDING_FUZZY_MATCH_THRESHOLD:
            matches.append(idx)

    return matches[0] if len(matches) == 1 else None


def _primary_key(row: dict):
    if _is_blank(row.get("postcode")):
        return None
    return _fallback_key(row) + (_normalize_postcode(row.get("postcode")),)


def _dedup_key(row: dict):
    """Grouping key for intra-batch duplicate detection (see
    unmatched_collisions below) - the same postcode-preferring tiering
    _primary_key/_fallback_key already use for matching a new row against
    MASTER, applied instead pairwise among pending rows themselves so two
    rows that both independently fail to match master can still be
    recognized as the same property. Prefers the postcode-inclusive key
    when a row has one: two rows sharing building+provider+floor_unit but
    with DIFFERENT non-blank postcodes are deliberately never grouped - a
    postcode difference is real signal against merging, not just missing
    data, same principle as _primary_key's own postcode requirement."""
    return _primary_key(row) or _fallback_key(row)


# Common UK street-suffix abbreviations, mapped to their canonical expanded
# form - see _address_street_key. Deliberately a small, explicit list of
# genuinely common abbreviations rather than a general transliteration
# scheme - the same "conservative, human catches it if this doesn't cover a
# case" philosophy as normalize_key itself.
_STREET_SUFFIX_EXPANSIONS = {
    "st": "street", "rd": "road", "ave": "avenue", "av": "avenue",
    "sq": "square", "ln": "lane", "pl": "place", "ct": "court",
    "cres": "crescent", "gdns": "gardens", "ter": "terrace",
}

# _leading_house_number is imported from house_number.py (see that module's
# own docstring) - shared verbatim with extract_spreadsheet_gemini.py's
# address_1 verification pass, rather than reimplemented here, so the two
# never drift apart on what counts as "the leading house number".


def house_number_changed(old_val, new_val) -> bool:
    """
    True when old_val and new_val's leading house-number tokens (see
    _leading_house_number - reused here rather than re-implemented) genuinely
    differ: a different number entirely ("18" vs "24"), one side having a
    number and the other not, or a plain number vs. a hyphenated range that
    happens to share an endpoint ("18" vs "14-18") - still a real change,
    since the range covers a different, wider set of units than the plain
    number alone, not the same fact restated. Inherits _leading_house_number's
    own case/whitespace tolerance for free, so "18" vs " 18 " or "56A" vs
    "56a" is genuinely the same token and correctly NOT flagged.

    Used by build_merge_plan to flag address_1/building changes as risky
    (see HOUSE_NUMBER_FIELDS) independent of RISKY_TEXT_FIELDS - a silent
    house-number change is a structural address concern, not the free-text
    detail-loss/richness-regression concept RISKY_TEXT_FIELDS exists for.
    """
    return _leading_house_number(old_val) != _leading_house_number(new_val)


def _address_street_key(building) -> str:
    """
    normalize_key(building) with a leading house number stripped and a
    common UK street-suffix abbreviation expanded to its canonical form
    (e.g. "St" -> "Street") - lets unmatched_collisions' address-aware
    grouping pass recognize "89 Charterhouse St" and "Charterhouse Street"
    as the same real street, which share no normalize_key() overlap at all
    otherwise (confirmed against a real pair from the same uploaded
    Copthall Estates file: a portfolio-wide rollup sheet abbreviates and
    prefixes a house number onto a building name that provider's own
    dedicated per-area sheet gives in full, with no number, as its own
    building-name line).

    ONLY used for intra-batch duplicate grouping, never for matching
    against master - _primary_key/_fallback_key/normalize_key and the
    fuzzy-matching tier (_fuzzy_building_match/_building_has_no_digits/
    BUILDING_FUZZY_MATCH_THRESHOLD) are completely untouched by this
    function and never call it. That tier's numbered-address exclusion
    exists specifically because a SIMILARITY-score-based fuzzy match
    (difflib ratio) scores a genuinely different real property higher than
    an actual typo often enough to be dangerous on short numbered strings -
    a real, measured risk documented on BUILDING_FUZZY_MATCH_THRESHOLD
    itself. This function is a different, more conservative kind of check:
    a deterministic rule (strip a leading number, expand one suffix
    abbreviation), paired with _leading_house_number's own guard against
    merging two rows whose numbers genuinely disagree - not a score that
    could rank a different property above a real match.

    Returns "" for a blank building (or one that's number-only after
    stripping) - callers must treat that as "no address-aware key
    available", never group every such row together.
    """
    if _is_blank(building):
        return ""
    # Number stripped from the RAW string first (see _LEADING_HOUSE_NUMBER_
    # RE's own comment on why - normalize_key would destroy a hyphenated
    # range's "-" before this pattern ever saw it), then the remainder goes
    # through the usual normalize_key pass for case/punctuation.
    text = str(building)
    match = _LEADING_HOUSE_NUMBER_RE.match(text)
    if match:
        text = text[match.end():]
    text = normalize_key(text)
    words = text.split()
    if not words:
        return ""
    words[-1] = _STREET_SUFFIX_EXPANSIONS.get(words[-1], words[-1])
    return " ".join(words)


def merge_field_choice(values: list) -> tuple:
    """
    For one field across a group of intra-batch duplicate rows (see
    _dedup_key/unmatched_collisions - pages/2_Review_and_Master.py calls
    this once per field when rendering the merge UI): returns (needs_choice,
    resolved_value).

    needs_choice is False only when `values` has no real disagreement at
    all: every value is blank (resolved_value is then None), or every value
    is the same non-blank value (tolerant-equal - see _values_equal, so
    "METSPACE" vs "Metspace" counts as agreeing; resolved_value is that
    shared value). needs_choice is True the moment two values fall into
    different "classes" - including one source blank and another non-blank,
    not just two DIFFERENT non-blank values - resolved_value is then None
    and the caller must ask a human to pick one of `values` itself (see
    default_merge_choice_index, which is exactly what lets a blank-vs-filled
    case default to the filled one without hiding the choice entirely - a
    reviewer can still deliberately pick "blank" if the one value present
    is actually wrong for the merged property, rather than that ever being
    silently decided for them).
    """
    classes = []  # one representative value per distinct class seen so far
    for v in values:
        blank = _is_blank(v)
        if any(blank == _is_blank(rep) and (blank or _values_equal(v, rep)) for rep in classes):
            continue
        classes.append(v)
    if len(classes) <= 1:
        return False, (classes[0] if classes else None)
    return True, None


def default_merge_choice_index(values: list) -> int:
    """Index into `values` to preselect in the merge-choice UI when
    merge_field_choice says a human must pick - the one non-blank source
    when exactly one is non-blank, else the first value (arbitrary but
    predictable; the human sees every actual value in the UI and can always
    override the preselection)."""
    non_blank = [i for i, v in enumerate(values) if not _is_blank(v)]
    return non_blank[0] if len(non_blank) == 1 else 0


def row_label(row_dict: dict) -> str:
    parts = [row_dict.get("building") if not _is_blank(row_dict.get("building")) else "(no building)"]
    if not _is_blank(row_dict.get("provider")):
        parts.append(row_dict["provider"])
    if not _is_blank(row_dict.get("floor_unit")):
        parts.append(row_dict["floor_unit"])
    return " — ".join(parts)


def new_property_labels(rows: list) -> list:
    """
    One plain "{address_1} — {provider} — {floor_unit}" label per row, for
    the purely-informational "will be added as new" list on the Review page
    - floor_unit is appended only when needed to tell two rows in this same
    batch apart (i.e. more than one shares the same address_1, like 111
    Wardour Street's three separate floors); a single new property at a
    unique address is just "{address_1} — {provider}". Grouping uses
    normalize_key so two rows that share an address but differ only in
    case/punctuation/whitespace still count as the same address. A floor
    range or multiple non-contiguous floors extracted together (e.g. "4th &
    5th Floors") is untouched here - floor_unit is a single field, so
    whatever formatting the extraction produced for it is preserved as-is,
    never split into separate rows/labels.
    """
    dicts = [r.model_dump() for r in rows]
    address_counts = Counter(normalize_key(d.get("address_1")) for d in dicts)

    labels = []
    for d in dicts:
        parts = [d.get("address_1") if not _is_blank(d.get("address_1")) else "(no address)"]
        if not _is_blank(d.get("provider")):
            parts.append(d["provider"])
        shares_address = address_counts[normalize_key(d.get("address_1"))] > 1
        if shares_address and not _is_blank(d.get("floor_unit")):
            parts.append(d["floor_unit"])
        labels.append(" — ".join(parts))
    return labels


def _suggest_similar(new_dict: dict, master_records: list) -> list:
    """Cheap, stdlib-only fuzzy hint for the "no match" review section - not
    part of matching itself, just reduces manual searching when a near-miss
    (e.g. a slightly different building spelling) is the real cause.

    Reuses BUILDING_FUZZY_MATCH_THRESHOLD and _building_has_no_digits - the
    same validated threshold and numbered-address exclusion the real fuzzy-
    matching tier uses (_fuzzy_building_match) - rather than a separate,
    untuned cutoff. A lower cutoff here previously suggested completely
    unrelated buildings: confirmed against real data, "77 Gracechurch
    Street" spuriously matched "27 Greville Street", "55 Grosvenor Street",
    and "141 Fenchurch Street (Monument)" at an 0.6 cutoff - any two short
    "digit + word + Street" addresses cluster in the 0.5-0.65
    SequenceMatcher range purely from shared generic structure, not real
    similarity, and a numbered address like that should never be fuzzy-
    suggested at all (same reasoning _building_has_no_digits documents for
    the real matching tier). Showing zero suggestions is always preferable
    to showing irrelevant ones - this only ever reduces manual searching,
    never replaces it.

    Candidates are also scoped to new_dict's own provider (normalize_key
    equality, the same convention _fallback_key/_fuzzy_anchor_key already
    use) and, like _fuzzy_building_match, excludes any candidate whose OWN
    building is a numbered address too, not just the target's - provider is
    part of listing identity throughout this module (see _fallback_key/
    _primary_key/_fuzzy_anchor_key), and a hint section is no exception:
    confirmed real gap this closes - "Clerkenwell Road" (incoming, provider
    A) previously suggested "80 Clerkenwell Road" (an unrelated existing
    listing from a DIFFERENT provider B) as a "possible near-miss" purely
    because SequenceMatcher scores a short address as a superstring of
    itself very highly, regardless of which provider either one belongs to
    or that the candidate is itself a numbered address _fuzzy_building_
    match would already exclude on the real matching tier. A different
    provider is never the same listing (see this module's own module-level
    identity principle), so it must never be suggested as a possible one,
    even as a hint a human is still free to dismiss."""
    target_building = new_dict.get("building")
    if not _building_has_no_digits(target_building):
        return []
    target = normalize_key(target_building)
    if not target:
        return []
    target_provider = normalize_key(new_dict.get("provider"))
    candidates = [
        r for r in master_records
        if normalize_key(r.get("provider")) == target_provider and _building_has_no_digits(r.get("building"))
    ]
    keys = [normalize_key(r.get("building")) for r in candidates]
    close = set(difflib.get_close_matches(target, keys, n=3, cutoff=BUILDING_FUZZY_MATCH_THRESHOLD))
    seen = set()
    results = []
    for rec, key in zip(candidates, keys):
        if key in close and key not in seen:
            seen.add(key)
            results.append(rec)
    return results


@dataclass
class MatchedRow:
    master_index: int
    property_id: str
    new_row: ListingRow
    diffs: dict
    match_tier: str  # "postcode" or "fallback"
    silent_updates: dict = field(default_factory=dict)  # see silent_field_updates - never shown in the diff-review UI
    risky_fields: frozenset = field(default_factory=frozenset)  # see is_detail_loss - forces manual review, like a collision
    let_status_fields: frozenset = field(default_factory=frozenset)  # see mentions_let_status - forces manual review, like a collision


@dataclass
class UnmatchedRow:
    new_row: ListingRow
    suggestions: list = field(default_factory=list)


@dataclass
class MergePlan:
    master_records: list          # current master rows as cleaned dicts (property_id backfilled)
    matched_changed: list          # list[MatchedRow], diffs non-empty
    matched_unchanged: list        # list[MatchedRow], diffs empty
    unmatched: list                # list[UnmatchedRow]
    collisions: list               # list[list[MatchedRow]] - multiple incoming rows targeting the same master row
    unmatched_collisions: list     # list[list[UnmatchedRow]] - multiple incoming rows matching each other, no master row


def collision_group_fields(group: list) -> list:
    """
    Union of every field that appears in ANY member's diffs, in DIFF_FIELDS
    order (a stable, predictable render order rather than dict/set
    insertion order) - see matched_collision_field_choice for how each
    field's proposed values across the group get resolved. A field none of
    the group's members changed relative to master isn't included at all -
    there's nothing to resolve for it, same as it wouldn't appear in a
    single non-colliding matched row's own diff either.
    """
    fields_with_diffs = {f for m in group for f in m.diffs}
    return [f for f in DIFF_FIELDS if f in fields_with_diffs]


def _text_variants_compatible(values: list) -> bool:
    """
    True if no value in `values` is missing an item another value has (see
    is_detail_loss, checked pairwise in BOTH directions) - i.e. these look
    like the same underlying fact/list at different levels of completeness
    or rewording (e.g. two independent Gemini extractions of the SAME real
    brochure, which real extraction non-determinism confirms can come back
    with slightly different phrasing for the same fact), never a genuine
    conflict. Only meaningful for RISKY_TEXT_FIELDS - see
    matched_collision_field_choice, the only caller - reusing the exact
    same item-similarity tolerance already trusted for an old-vs-new master
    diff, applied here peer-vs-peer instead.
    """
    return not any(
        is_detail_loss(values[i], values[j]) or is_detail_loss(values[j], values[i])
        for i in range(len(values)) for j in range(i + 1, len(values))
    )


def _richest_text_value(values: list) -> str:
    """The most complete of a set of already-confirmed-compatible text
    variants (see _text_variants_compatible) - most itemizable content
    (see _detail_items) wins; ties broken by raw length. Never called on
    genuinely conflicting values."""
    return max(values, key=lambda v: (len(_detail_items(str(v))), len(str(v))))


def matched_collision_field_choice(values: list, field_name: str = None) -> tuple:
    """
    Like merge_field_choice, but for one field's PROPOSED values across a
    collision group of PEER rows (rows that all independently propose a
    value for the same identity, with no inherent priority order between
    them) rather than merge_field_choice's own brand-new-property case
    (unmatched_collisions, no master row to fall back to at all... except
    unmatched_collisions now calls THIS function too - see
    consolidate_unmatched_duplicates - since the same "peer rows, resolve
    or flag" shape applies whether or not a master row happens to exist).
    Blank vs non-blank is deliberately NOT treated as a disagreement here,
    unlike merge_field_choice: a colliding row that's blank on a field has
    no opinion on it, exactly as it would in an ordinary, non-colliding
    matched-row diff (diff_fields itself already treats "no data this
    time" as nothing to review, never a value to choose between).

    needs_choice is True only when two or more colliding rows propose
    genuinely different non-blank values (tolerant-equal, see _values_equal)
    for this field - resolved_value is then None and the caller must ask a
    human to pick one (see _render_merge_field_choice) - UNLESS field_name
    is one of RISKY_TEXT_FIELDS and every differing value is confirmed
    compatible (see _text_variants_compatible), in which case this still
    auto-resolves, to the richest variant, rather than forcing a click over
    what's really just reworded/differently-complete phrasing of the same
    fact. field_name is optional (existing callers that don't pass it keep
    the exact prior behavior for text fields - always a real choice on any
    textual disagreement) precisely so this tolerance is opt-in per call
    site, not a silent behavior change for every existing caller.

    Otherwise - every value blank, or exactly one distinct non-blank value
    regardless of how many rows propose it - auto-resolves to that value
    with no manual click, same as this field would need in a non-colliding
    matched row.
    """
    classes = []
    for v in values:
        if _is_blank(v):
            continue
        if any(_values_equal(v, rep) for rep in classes):
            continue
        classes.append(v)
    if len(classes) <= 1:
        return False, (classes[0] if classes else None)
    if field_name in RISKY_TEXT_FIELDS and _text_variants_compatible(classes):
        return False, _richest_text_value(classes)
    return True, None


def _group_has_genuine_conflict(dicts: list) -> bool:
    """
    True if any DIFF_FIELD has 2+ genuinely different non-blank values
    across `dicts` (see matched_collision_field_choice, called with the
    field name so RISKY_TEXT_FIELDS gets its own reworded-but-compatible
    tolerance) - the single generic rule this module uses to decide
    whether a group of rows claiming to be the same property/unit can be
    safely auto-merged (see consolidate_unmatched_duplicates) or must be
    left for a human: automatically combining them would only ever change
    the DATA'S MEANING if some field genuinely disagrees, never merely
    because there happen to be several source rows.

    postcode AND address_1 get one narrow exception, mirroring
    _group_unmatched_duplicates' own grouping-stage override: a
    disagreement on either is excused - not counted as a genuine conflict
    - when `dicts` also share one identical, non-blank brochure_link (see
    _brochure_link_identity_override). Reusing that SAME helper here (not
    a second, separately-tuned check) is what makes the two stages
    consistent - a group _group_unmatched_duplicates only formed because
    of this override must not then turn around and get stuck in manual
    review for the exact disagreement it was already excused for (real
    case: the Nexus Place pair, byte-identical brochure_link, genuinely
    different geocoded postcodes - see _group_unmatched_duplicates'
    docstring).

    address_1 is included alongside postcode, not just postcode alone,
    because it is backfilled by the exact same geocode.py API call/result
    as postcode whenever the source document didn't state one
    (_address_line1_and_postcode is called once and unpacks both from the
    same address_components list - see geocode.py's geocode_row Tier 2) -
    the real Nexus Place pair this override exists for disagrees on BOTH
    for that reason, not postcode alone, so excusing postcode while still
    treating address_1 as a hard conflict would leave the exact same
    geocoding artifact blocking the exact same real case this fix targets.

    Every OTHER field - including building and floor_unit, which come
    straight from the provider's own text and are never touched by
    geocode.py, so a genuinely different property/unit still blocks
    auto-merge even if it happens to share a brochure_link - is checked
    with no such exception.
    """
    weak_geocoded_fields = ("postcode", "address_1")
    for f in DIFF_FIELDS:
        needs_choice, _ = matched_collision_field_choice([d.get(f) for d in dicts], f)
        if needs_choice and not (f in weak_geocoded_fields and _brochure_link_identity_override(dicts)):
            return True
    return False


def _merge_unmatched_group(group: list) -> ListingRow:
    """
    Consolidates a group of UnmatchedRow objects (see
    _group_unmatched_duplicates) that _group_has_genuine_conflict has
    already confirmed has NO genuine field conflict into a single
    ListingRow - every field resolved via matched_collision_field_choice
    (a blank source contributes nothing; multiple agreeing/compatible
    sources resolve to one shared or richest value - the exact same
    source-priority-agnostic resolution _render_collision_group already
    trusts for the analogous matched-against-master case, never a second,
    differently-behaved merge system). Never called on a group with a
    genuine conflict - see consolidate_unmatched_duplicates.

    source_file becomes every contributing row's own source_file joined
    with " + " (falling back to a positional "Row N" label for whichever
    lack one) - the same provenance convention _render_intra_batch_
    duplicate_group's manual merge path already uses, so debugging which
    original upload(s) fed a consolidated row works identically whether
    that consolidation happened automatically or via a manual click.
    """
    dicts = [u.new_row.model_dump() for u in group]
    merged = {}
    for f in DIFF_FIELDS:
        _, resolved = matched_collision_field_choice([d.get(f) for d in dicts], f)
        merged[f] = resolved
    labels = [d.get("source_file") or f"Row {i + 1}" for i, d in enumerate(dicts)]
    merged["property_id"] = str(uuid.uuid4())
    merged["source_file"] = " + ".join(labels)
    return ListingRow(**merged)


def consolidate_unmatched_duplicates(plan: MergePlan) -> MergePlan:
    """
    Auto-merges every intra-batch duplicate group (see build_merge_plan's
    own unmatched_collisions) that has no genuine field conflict (see
    _group_has_genuine_conflict) into a single UnmatchedRow, carrying
    forward the union of the group's own near-miss suggestions - so a
    duplicated property that ALSO looks like an existing master property
    still gets the normal link-or-add-new decision downstream, exactly as
    a single non-duplicated near-miss row already would (auto-merging
    duplicates first, then letting it flow through the SAME near-miss
    check every other unmatched row goes through, rather than bypassing
    that check just because it started out as several rows).

    Only a group with a genuine conflict is left in unmatched_collisions,
    for the existing manual-duplicate-review UI to handle exactly as
    before - manual review becomes the exception (a real conflict), not
    the default (any group size > 1).

    Returns a NEW MergePlan (never mutates the one passed in) -
    master_records/matched_changed/matched_unchanged/collisions are
    identical references; only unmatched/unmatched_collisions differ.
    Members of a group still needing review are left exactly where
    build_merge_plan already put them (present in BOTH plan.unmatched and
    plan.unmatched_collisions) - the existing pages-layer id() tracking
    that excludes collision members from near_miss/plain_new already
    handles that correctly and needs no change.
    """
    still_needs_review = []
    safe_groups = []
    for group in plan.unmatched_collisions:
        dicts = [u.new_row.model_dump() for u in group]
        (still_needs_review if _group_has_genuine_conflict(dicts) else safe_groups).append(group)

    safe_member_ids = {id(u) for group in safe_groups for u in group}

    merged_rows = []
    for group in safe_groups:
        merged_row = _merge_unmatched_group(group)
        suggestions, seen_ids = [], set()
        for u in group:
            for s in u.suggestions:
                if id(s) not in seen_ids:
                    seen_ids.add(id(s))
                    suggestions.append(s)
        merged_rows.append(UnmatchedRow(merged_row, suggestions))

    new_unmatched = [u for u in plan.unmatched if id(u) not in safe_member_ids] + merged_rows

    return MergePlan(
        plan.master_records, plan.matched_changed, plan.matched_unchanged,
        new_unmatched, plan.collisions, still_needs_review,
    )


def build_merge_plan(new_rows: list, master_df: pd.DataFrame) -> MergePlan:
    master_records = [
        {key: clean_value(value) for key, value in rec.items()}
        for rec in master_df.to_dict(orient="records")
    ]
    for rec in master_records:
        if _is_blank(rec.get("property_id")):
            rec["property_id"] = str(uuid.uuid4())

    primary_index = {}
    fallback_index = {}
    fuzzy_anchor_index = {}
    for i, rec in enumerate(master_records):
        pk = _primary_key(rec)
        if pk:
            primary_index.setdefault(pk, []).append(i)
        fallback_index.setdefault(_fallback_key(rec), []).append(i)
        fuzzy_anchor_index.setdefault(_fuzzy_anchor_key(rec), []).append(i)

    matched_changed, matched_unchanged, unmatched = [], [], []

    for new_row in new_rows:
        new_dict = new_row.model_dump()
        master_idx, tier = None, None

        pk = _primary_key(new_dict)
        if pk and len(primary_index.get(pk, [])) == 1:
            master_idx, tier = primary_index[pk][0], "postcode"
        else:
            candidates = fallback_index.get(_fallback_key(new_dict), [])
            if len(candidates) == 1:
                master_idx, tier = candidates[0], "fallback"
            # 0 or >1 candidates both fall through as unmatched - an
            # ambiguous fallback match is exactly as unsafe to auto-apply as
            # no match at all.
            else:
                # Last resort: same provider+floor_unit exactly, building
                # name close enough to be the same real property reworded
                # by extraction non-determinism (e.g. Gemini spelling a
                # building name slightly differently between two uploads of
                # the same source) - see _fuzzy_building_match for the real
                # data this threshold was validated against, and why a
                # numbered address never reaches this tier at all.
                fuzzy_candidates = fuzzy_anchor_index.get(_fuzzy_anchor_key(new_dict), [])
                fuzzy_idx = _fuzzy_building_match(new_dict, fuzzy_candidates, master_records)
                if fuzzy_idx is not None:
                    master_idx, tier = fuzzy_idx, "fuzzy_building"

        if master_idx is not None:
            old_rec = master_records[master_idx]
            diffs = diff_fields(old_rec, new_dict)
            silent = silent_field_updates(old_rec, new_dict)

            # Auto-merge a DETAIL_LOSS_MERGE_FIELDS update BEFORE risky_fields
            # is computed below, whenever it's safe to (see merge_compatible_
            # text's own docstring): is_detail_loss says old_val has a
            # genuine item new_val doesn't restate, but is_richness_
            # regression says new_val isn't a drastic, all-round terser
            # replacement (the one shape that must stay a manual review,
            # unmerged, exactly as before this existed - see that
            # function's own docstring). Mutating diffs[f] here, rather
            # than adding a separate resolved-value field, is what lets the
            # EXISTING risky_fields expression right below re-evaluate
            # against the MERGED value with no changes of its own: a merged
            # value is constructed to always contain every old item it
            # didn't just deliberately drop, so is_detail_loss(old_val,
            # merged_val) is guaranteed to come back False.
            #
            # contacts is deliberately NOT in this loop (see CONTACTS_
            # NEWEST_WINS_FIELDS' own docstring) - its diff, if any, is left
            # completely untouched here, so it flows through exactly like
            # any other ordinary scalar field: new nonblank value wins
            # verbatim, ever merged with master's old one.

            # A trivially small lat/lng movement (see _is_same_location) is
            # not a meaningful property change at all - removed from diffs
            # entirely, BEFORE risky_fields/matched_changed below ever see
            # it, so master's existing coordinate is left completely
            # untouched rather than being rewritten to a barely-different
            # value on every single upload. If this was the row's only
            # diff, it now correctly lands in matched_unchanged instead of
            # matched_changed - genuinely nothing changed.
            if ("lat" in diffs or "lng" in diffs) and _is_same_location(old_rec, new_dict):
                diffs.pop("lat", None)
                diffs.pop("lng", None)

            for f in DETAIL_LOSS_MERGE_FIELDS:
                if f not in diffs:
                    continue
                old_val, new_val = diffs[f]
                if _is_blank(old_val) or _is_blank(new_val):
                    continue
                if is_richness_regression(old_val, new_val):
                    continue
                if is_detail_loss(old_val, new_val, field_name=f):
                    diffs[f] = (old_val, merge_compatible_text(old_val, new_val, field_name=f))

            risky_fields = frozenset(
                f for f in diffs
                if f in DETAIL_LOSS_MERGE_FIELDS and (is_detail_loss(*diffs[f]) or is_richness_regression(*diffs[f]))
            ) | frozenset(
                f for f in diffs
                if f in HOUSE_NUMBER_FIELDS and house_number_changed(*diffs[f])
            ) | frozenset(
                # lat/lng get no provenance tag distinguishing an explicit
                # provider-stated coordinate from one this pipeline
                # generated via geocode.py's own API calls (see GEOCODE_
                # RISK_FIELDS' own docstring) - unlike address_1/postcode,
                # which are deliberately left freely auto-updatable (a
                # genuine provider correction is common and safe there, and
                # house_number_changed already catches the one structural
                # danger). A trivially small movement never even reaches
                # here (already removed from diffs above, see
                # _is_same_location) - what remains is a genuinely larger
                # move, safe to auto-apply only when independently
                # corroborated by address_1+postcode both still agreeing
                # (see _location_change_is_safe's own docstring); anything
                # else on an already-non-blank master coordinate still
                # needs a look. A blank old value is unaffected either way -
                # filling in a never-before-known coordinate is not a
                # REPLACEMENT. Both fields are added or withheld from
                # risky_fields TOGETHER, as one location decision, never
                # independently.
                f for f in diffs
                if f in GEOCODE_RISK_FIELDS and not _is_blank(old_rec.get(f))
                and not _location_change_is_safe(old_rec, new_dict)
            )
            let_status_fields = frozenset(
                f for f in diffs if f in LET_STATUS_FIELDS and mentions_let_status(diffs[f][1])
            )
            matched = MatchedRow(
                master_idx, old_rec["property_id"], new_row, diffs, tier, silent, risky_fields, let_status_fields,
            )
            (matched_changed if diffs else matched_unchanged).append(matched)
        else:
            unmatched.append(UnmatchedRow(new_row, _suggest_similar(new_dict, master_records)))

    by_master_idx = {}
    for m in matched_changed:
        by_master_idx.setdefault(m.master_index, []).append(m)
    collisions = [group for group in by_master_idx.values() if len(group) > 1]

    unmatched_collisions = _group_unmatched_duplicates(unmatched)

    return MergePlan(master_records, matched_changed, matched_unchanged, unmatched, collisions, unmatched_collisions)


def _brochure_link_identity_override(dicts: list) -> bool:
    """
    True when `dicts` (dumped ListingRow dicts) share one identical,
    non-blank brochure_link - a provider-issued document tied to one
    specific property, treated as stronger, less error-prone identity
    evidence than a geocoded postcode (see _group_unmatched_duplicates'
    own docstring for the confirmed real Nexus Place case this exists
    for). A blank brochure_link on some rows is "no opinion", the exact
    same tolerance matched_collision_field_choice already applies to
    every other field: this is NOT "every row individually carries a
    brochure_link", only that none of the non-blank ones disagree, AND at
    least one non-blank value exists at all (all rows blank is an absence
    of evidence, not evidence of identity, so does not qualify).

    One shared check, called from BOTH _group_unmatched_duplicates (to
    allow grouping rows despite a genuine postcode disagreement) and
    _group_has_genuine_conflict (to then also excuse that SAME postcode
    disagreement from forcing manual review) - so the grouping stage and
    the merge-safety stage can never disagree about whether this specific,
    narrow override applies to a given set of rows.
    """
    links = {d.get("brochure_link") for d in dicts if not _is_blank(d.get("brochure_link"))}
    return len(links) == 1


def _partition_by_source_submarket(indices: list, unmatched: list) -> list:
    """
    Splits `indices` (candidate duplicates already sharing an identity key -
    see _group_unmatched_duplicates) into sub-groups such that two rows with
    a genuinely different, BOTH non-blank submarket/area value are NEVER
    placed in the same sub-group - the signal that a provider's own source
    workbook intentionally lists the same building/floor more than once
    under a different area/submarket context (a real UNION file lists the
    same physical unit once per area sheet it's marketed under) rather than
    one listing accidentally duplicated by this pipeline. A blank submarket
    on either side is never treated as evidence of anything - see test #3's
    own "one area/submarket blank -> use existing identity evidence" rule -
    a row with no stated submarket simply joins whichever sub-group it's
    compatible with under this same tolerant rule, exactly as if this
    function didn't exist for it.

    THE BROADER PRINCIPLE this is one instance of, not the whole of: two
    genuinely intentional source listings must never be collapsed (or
    forced into a merge/manual-duplicate decision) merely because
    building+provider+floor identity matches - submarket is simply the
    ONE currently-available field this module can PROVE reflects the
    original file's own structure (a real per-row column, or a per-sheet
    structural fallback populated before geocoding ever runs - see
    fill_missing_submarket_from_structural_header) rather than the
    pipeline's own downstream guesswork.

    A future case with the SAME submarket (or none at all) but a genuinely
    different, non-blank source-supplied value elsewhere - size_sqft,
    rent_pcm, desks_max, state_of_space, brochure_link, or anything else
    DIFF_FIELDS covers - is NOT given this same auto-preserve treatment,
    even though it MIGHT also be two intentional listings rather than a
    drifted duplicate. That's a deliberate limitation, not an oversight:
    this codebase has no signal that reliably tells "two intentional
    listings, same everything except this one stated difference" apart
    from "one accidental duplicate whose values happen to disagree" for
    any of those fields the way submarket's own structural provenance
    does - inventing one would mean guessing at exactly the kind of
    silent, hard-to-audit decision this whole module exists to avoid (see
    _group_has_genuine_conflict's own docstring: "incorrect enrichment is
    worse than a blank field", the same principle applied to identity
    decisions here). Such a case is therefore left to fall through to the
    EXISTING, already-safe fallback unchanged: _group_has_genuine_conflict
    still flags a genuine non-blank disagreement on any DIFF_FIELDS value
    as a real conflict needing manual review, exactly as it always has,
    regardless of whether submarket happens to match. A human reviewing it
    can tell "two real listings" from "a data-entry drift" in a way this
    code cannot; if a future format's provenance changes so a NEW field
    becomes as provably structural as submarket is today, extending this
    same treatment to it is a deliberate follow-up, not something to infer
    here.

    Deliberately provider-agnostic and format-agnostic: this reads only
    ListingRow's own submarket field, never a provider name, a sheet name,
    or any hardcoded area string - it applies identically to any current or
    future spreadsheet format that states a per-row area/submarket, and is
    a pure no-op (single sub-group, unchanged behavior) for one that
    doesn't.

    A single sub-group (the whole of `indices`, in order) whenever every
    member's submarket is blank or they all agree - the overwhelmingly
    common case, and exactly the previous no-submarket-guard behavior.
    Greedy first-fit, not a fully general conflict-graph partition: two
    non-blank, DIFFERING submarket values are always split; a blank value
    joins the first sub-group it's compatible with (which may itself have
    started blank and since adopted a non-blank identity from an earlier
    member) - correct for every realistic shape seen in practice (a
    handful of distinct areas, or none), though a contrived, unordered mix
    of several blanks and several conflicting non-blank values could
    theoretically cluster differently under a different member order.
    """
    clusters = []  # each: [submarket_or_None, [indices...]]
    for i in indices:
        submarket_i = normalize_key(unmatched[i].new_row.submarket)
        for cluster in clusters:
            rep_submarket = cluster[0]
            if not submarket_i or not rep_submarket or submarket_i == rep_submarket:
                cluster[1].append(i)
                if not rep_submarket and submarket_i:
                    cluster[0] = submarket_i
                break
        else:
            clusters.append([submarket_i, [i]])
    return [members for _, members in clusters]


def _group_unmatched_duplicates(unmatched: list) -> list:
    """
    Groups pending rows that both independently failed to match master but
    refer to the same real property - two passes feeding one partition
    (union-find over indices into `unmatched`), not two separate group
    lists, since a row can plausibly qualify under either:

    1. Exact _dedup_key equality (building+provider+floor_unit[+postcode],
       normalize_key-tolerant only - the original, still-primary path).
    2. Address-aware equality (_address_street_key) - same provider and
       floor_unit, and the SAME street once a leading house number is
       stripped and a suffix abbreviation expanded (e.g. "89 Charterhouse
       St" / "Charterhouse Street") - guarded against merging two rows with
       genuinely different, disagreeing house numbers (_leading_house_
       number) or genuinely different non-blank postcodes, either of which
       normally means a different real property/unit despite the shared
       street name, not a spelling variant of the same one - UNLESS every
       member of the group also shares one identical, non-blank
       brochure_link (see the postcode-conflict check below), in which case
       that overrides the postcode signal specifically, never the house-
       number one.

       Confirmed real case this override exists for: the real UNION "Nexus
       Place - 25 Farringdon Place" / 5th floor row appears on both the
       "City" and "Clerkenwell & Farringdon" sheets of the same real
       workbook, byte-identical building/floor_unit/brochure_link - but
       geocode.py's own submarket-biased Places search (each sheet's own
       different area name used as a disambiguation hint, see geocode_row's
       Tier 2) returned two DIFFERENT real postcodes for the same actual
       building (EC4M 4AB vs EC1M 3HA, confirmed against the real API).
       Without the override, that geocoding artifact alone permanently
       split one real property into two never-linked rows, with the
       genuinely strong evidence (an identical, provider-issued brochure
       document link) never even consulted.

    BOTH passes are further split by _partition_by_source_submarket BEFORE
    either one ever unions anything - a genuinely different, non-blank
    submarket/area between two otherwise-identical candidates means the
    ORIGINAL provider workbook intentionally represented them as separate
    listings (see that function's own docstring), which must never be
    collapsed into a merge OR a manual duplicate prompt merely because
    building/provider/floor/postcode/brochure_link happen to agree - source-
    listing identity and physical-property identity are two different
    questions, and this function only ever answers the first. A shared
    brochure_link is real evidence the same PHYSICAL property is involved,
    but it is not evidence the provider intended one listing row, so it
    never overrides a genuine submarket split, unlike the narrower
    postcode-only override above.

    Returns groups of size > 1 only, exactly like the single-pass version
    this replaces.
    """
    parent = list(range(len(unmatched)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    by_key = {}
    for i, u in enumerate(unmatched):
        by_key.setdefault(_dedup_key(u.new_row.model_dump()), []).append(i)
    for indices in by_key.values():
        for cluster in _partition_by_source_submarket(indices, unmatched):
            for i in cluster[1:]:
                union(cluster[0], i)

    address_groups = {}
    for i, u in enumerate(unmatched):
        row = u.new_row.model_dump()
        street = _address_street_key(row.get("building"))
        if not street:
            continue
        key = (street, normalize_key(row.get("provider")), normalize_key(row.get("floor_unit")))
        address_groups.setdefault(key, []).append(i)

    for indices in address_groups.values():
        if len(indices) < 2:
            continue
        for cluster in _partition_by_source_submarket(indices, unmatched):
            if len(cluster) < 2:
                continue
            buildings = [unmatched[i].new_row.building for i in cluster]
            numbers = {n for n in (_leading_house_number(b) for b in buildings) if n is not None}
            if len(numbers) > 1:
                continue  # genuinely different numbered units on the same street
            postcodes = {
                _normalize_postcode(unmatched[i].new_row.postcode)
                for i in cluster
                if not _is_blank(unmatched[i].new_row.postcode)
            }
            if len(postcodes) > 1:
                # A real postcode disagreement is normally strong signal
                # against merging (see this function's own docstring) - EXCEPT
                # when every member of this SAME provider+floor_unit+street
                # (+submarket, per the partition above) group also agrees on
                # one identical, non-blank brochure_link (see
                # _brochure_link_identity_override) - a provider-issued
                # document tied to one specific property is stronger, less
                # error-prone identity evidence than a geocoded postcode
                # (which this pipeline derives via an API call biased by each
                # sheet's own submarket text, not sourced from the provider's
                # own spreadsheet at all - see the confirmed real Nexus Place
                # case above). Never overrides the house-number check above,
                # which comes straight from the provider's own text, not from
                # geocoding.
                group_dicts = [unmatched[i].new_row.model_dump() for i in cluster]
                if not _brochure_link_identity_override(group_dicts):
                    continue
            for i in cluster[1:]:
                union(cluster[0], i)

    components = {}
    for i in range(len(unmatched)):
        components.setdefault(find(i), []).append(unmatched[i])
    return [group for group in components.values() if len(group) > 1]


def apply_merge(master_records: list, updates: dict, new_rows: list, removed_indices: frozenset = frozenset()) -> list:
    """
    master_records: full current master (property_id already backfilled), as
    plain dicts, in original order - untouched rows pass through verbatim.
    updates: {master_index: {field: approved_value, ...}} for rows with at
    least one approved change - only the approved fields are overlaid.
    new_rows: fully-formed ListingRow objects (property_id already assigned)
    confirmed as genuinely new, appended after all existing rows.
    removed_indices: master_index values to drop entirely - e.g. a property
    confirmed no longer available (see mentions_let_status) where the
    reviewer chose "remove" rather than "keep with update". A removed
    index is skipped even if it also has an entry in updates - removal is
    the more fundamental decision, so any field-level update for that same
    row is moot.
    """
    result = []
    for i, rec in enumerate(master_records):
        if i in removed_indices:
            continue
        merged = dict(rec)
        if i in updates:
            merged.update(updates[i])
        result.append(ListingRow(**{k: v for k, v in merged.items() if k in ListingRow.model_fields}))
    result.extend(new_rows)
    return result


def build_manual_edit(master_records: list, edited_rows: dict, displayed_positions: list = None) -> tuple:
    """
    Turns a data_editor "edited_rows" delta - {row_position: {column: new_value,
    ...}, ...}, straight from the Master default view's direct cell-editing
    grid (see pages/2_Review_and_Master.py) - into the same shape a normal
    approve produces, so a manual edit rides the exact same write_master()/
    versioning/undo mechanism:

      - merged_rows: the complete new master row list (list[ListingRow]) -
        every row unchanged except the ones edited, via apply_merge with no
        new rows.
      - diff_rows: [{"property", "field", "old", "new"}, ...], one entry per
        genuinely changed field - the same shape build_approval_summary
        produces, so the manual-edit confirmation banner can show "what
        changed" exactly like the approve-confirmation banner already does.
      - fields_changed: total count of individual field-level changes across
        every edited row - what the "Manual edit: N field(s) changed" version
        label and the pinned confirmation banner both report.

    Only keys that are real ListingRow fields are treated as an edit - the
    grid also carries a UI-only "Select" checkbox column (for row-selection/
    export) that is never itself a ListingRow field, and ends up in this same
    edited_rows dict whenever a row's checkbox was toggled in the same
    render as a real field edit. Silently ignored here rather than raising,
    since the caller has no other way to tell "just a checkbox" apart from
    "a real edit" - a row whose only changes are non-ListingRow keys
    contributes nothing to updates/diff_rows/fields_changed.

    displayed_positions, when given, is a list where displayed_positions[i]
    is the REAL position in master_records of whatever row sat at position i
    in the (possibly filtered) subset actually passed to data_editor this
    render - see pages/2_Review_and_Master.py's own text filter on the
    master table. edited_rows's own keys are always positions within THAT
    displayed subset, per Streamlit's own data_editor semantics, never
    directly meaningful against master_records once a filter has narrowed
    what's shown - e.g. the second VISIBLE row is not necessarily
    master_records[1] if the first two real rows were filtered out. Omitted
    (None) is the identity mapping, for the unfiltered case where displayed
    position and real position are the same thing.
    """
    updates = {}
    diff_rows = []
    for row_pos, cols in edited_rows.items():
        real_changes = {c: v for c, v in cols.items() if c in ListingRow.model_fields}
        if not real_changes:
            continue
        row_pos = int(row_pos)
        if displayed_positions is not None:
            row_pos = displayed_positions[row_pos]
        updates[row_pos] = real_changes

        old_rec = master_records[row_pos]
        label = row_label(old_rec)
        for field_name, new_val in real_changes.items():
            diff_rows.append({
                "property": label, "field": field_name,
                "old": old_rec.get(field_name), "new": new_val,
            })

    fields_changed = sum(len(v) for v in updates.values())
    merged_rows = apply_merge(master_records, updates, [])
    return merged_rows, diff_rows, fields_changed


def merge_selected_property_ids(previous_ids: set, visible_ids: set, now_selected_visible_ids: set) -> set:
    """
    The new export-selection set (see pages/2_Review_and_Master.py's
    export_selected_property_ids) after one render of the master table's
    own (possibly filtered) data_editor.

    previous_ids minus whichever of them are CURRENTLY VISIBLE, unioned
    with now_selected_visible_ids - a property_id's own checkbox state is
    only authoritative while its row is actually on screen to check/
    uncheck; one selected before a text filter narrowed the view stays
    selected even though its row isn't there to uncheck right now, since a
    filter narrowing what's DISPLAYED must never silently narrow what's
    actually SELECTED for removal/export too.
    """
    return (previous_ids - visible_ids) | now_selected_visible_ids


def pending_status_line(n_uploads: int, plan: MergePlan) -> str:
    """
    Plain, sentence-case summary of what a pending batch actually contains -
    zero-count clauses are dropped entirely rather than spelled out (e.g.
    never "0 matched with changes"), so this reads naturally whether the
    batch is all-new, all-changes, a mix, or (rare) entirely unchanged.
    """
    parts = []
    if plan.unmatched:
        n = len(plan.unmatched)
        parts.append(f"{n} new propert{'y' if n == 1 else 'ies'}")
    if plan.matched_changed:
        n = len(plan.matched_changed)
        parts.append(f"{n} propert{'y' if n == 1 else 'ies'} with changes")

    headline = f"{n_uploads} upload{'s' if n_uploads != 1 else ''} pending"
    if parts:
        headline += " — " + ", ".join(parts)
    elif plan.matched_unchanged:
        headline += " — no changes"
    return headline


def build_approval_summary(
    plan: MergePlan, updates: dict, new_rows_final: list, removed_indices: frozenset = frozenset(),
) -> tuple:
    """
    Compact, read-only diff data for a post-approve confirmation UI - plan/
    updates/new_rows_final are all local to whatever render pass computed
    them and typically gone by the time the confirmation is shown (e.g.
    after a Streamlit rerun), so this is what a caller persists instead.
    Returns (diff_rows, new_labels, removed_labels): diff_rows is a list of
    {"property", "field", "old", "new"} dicts (one per approved field
    change), new_labels is a list of row_label() strings for genuinely new
    properties, removed_labels is a list of row_label() strings for
    properties removed entirely (see apply_merge's removed_indices).
    """
    diff_rows = []
    for master_index, fields in updates.items():
        old_rec = plan.master_records[master_index]
        label = row_label(old_rec)
        for field_name, new_val in fields.items():
            if field_name == "source_file":
                continue  # internal bookkeeping, not a meaningful change to show
            diff_rows.append({"property": label, "field": field_name, "old": old_rec.get(field_name), "new": new_val})
    new_labels = [row_label(r.model_dump()) for r in new_rows_final]
    removed_labels = [row_label(plan.master_records[i]) for i in removed_indices]
    return diff_rows, new_labels, removed_labels
