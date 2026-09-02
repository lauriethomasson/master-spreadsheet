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
from staging_writer import title_case_label
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

# address_1/postcode/lat/lng specifically as they stood immediately after
# geocode.py's own Tier 2 fallback ran with ZERO source address_1/postcode/
# building-trailing-token hint to cross-check its result against at all
# (see that module's geocode_row/_source_location_hint) - row.geocode_
# unverified is the tri-state tag that run sets (see schema.ListingRow's
# own docstring). Unlike GEOCODE_RISK_FIELDS (lat/lng only, judged by
# distance + corroborating evidence - see _location_change_is_safe), and
# unlike HOUSE_NUMBER_FIELDS (address_1, only for a structural leading-
# number change), this has no leniency at all: a result with nothing
# whatsoever to check it against is unverified regardless of how far it
# moved or what shape the change is, so any of these fields showing up as
# a genuine diff on such a row always needs a human's own confirmation -
# see build_merge_plan's own risky_fields computation below.
GEOCODE_UNVERIFIED_FIELDS = ("address_1", "postcode", "lat", "lng")

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


# 8-point compass, N first and every 45 degrees clockwise after it -
# matches compass_bearing's own round(bearing / 45) % 8 index math below.
_COMPASS_POINTS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def compass_bearing(lat1, lng1, lat2, lng2) -> str:
    """
    The 8-point compass direction (N/NE/E/SE/S/SW/W/NW) FROM (lat1, lng1)
    TO (lat2, lng2) - standard atan2-based initial (forward) bearing
    formula, converted from degrees-from-true-north into the nearest of
    the 8 points rather than left as a raw degree figure, for the Review
    page's own lat/lng map card (see pages/2_Review_and_Master.py's
    combined Location row) - a reviewer glancing at a map wants "the new
    point is NE of the old one", not "047.3 degrees".

    Pure directional bearing, independent of haversine_distance_meters'
    own distance calculation - the two are always shown together on that
    card (distance + direction), but are two genuinely separate
    quantities, not derived from one another.

    Not itself great-circle-accurate over very long distances (initial
    bearing drifts from the straight compass reading the further apart the
    two points are, since a great-circle path isn't a straight line on a
    flat compass) - irrelevant here, since this only ever renders for two
    points already known to be within an ordinary walkable/drivable London
    distance (this app's own building-level coordinates), never anywhere
    close to a distance where that drift would matter.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lng2 - lng1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    bearing_degrees = (math.degrees(math.atan2(x, y)) + 360) % 360
    return _COMPASS_POINTS[round(bearing_degrees / 45) % 8]


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
# "U/O" is the literal abbreviation Workplace Plus's own real spreadsheet
# uses instead of spelling "Under Offer" out (confirmed present in 14 real
# rows) - the word-boundary match on "under offer" alone never catches this,
# so a re-upload showing "U/O" for a property already in master previously
# passed through with no review prompt at all.
LET_STATUS_KEYWORDS = (
    "let", "leased", "no longer available", "withdrawn", "under offer", "occupied", "u/o",
)


def _let_status_pattern(kw: str) -> str:
    """The word-boundary regex pattern for one LET_STATUS_KEYWORDS entry -
    factored out so mentions_let_status/_let_status_matches build their
    match from the exact same pattern, never two independently-drifting
    copies of it. "let" specifically excludes the "pre-let"/"re-let"/
    "sub-let" compound forms (see mentions_let_status's own docstring)."""
    pattern = rf"\b{re.escape(kw)}\b"
    if kw == "let":
        pattern = r"(?<!pre-)(?<!re-)(?<!sub-)" + pattern
    return pattern


def _let_status_matches(text) -> list:
    """
    Every substring of `text` matching one of LET_STATUS_KEYWORDS, in the
    ORIGINAL text's own casing/punctuation - never lowered, e.g. a real
    "U/O" cell stays "U/O", not "u/o" - via re.IGNORECASE rather than
    lower()'ing text first (which would only ever hand back the lowered
    substring). The single shared implementation mentions_let_status/
    matched_let_status_phrases both build on, so a future change to the
    matching rule (a new keyword, a new exclusion) can never update one
    without the other. May contain duplicates (e.g. the same keyword
    appearing twice) - see matched_let_status_phrases for the de-duplicated,
    display-ready version; this one is just the raw match list, ordered by
    each match's own real position in `text` (never by LET_STATUS_KEYWORDS'
    own list order - two different keywords both matching the same text
    must come back in reading order, not in whatever order this module
    happens to list keywords in).

    Returns [] for blank text or no match at all.
    """
    if _is_blank(text):
        return []
    text = str(text)
    found = []  # (start_position, matched_text) - sorted below into real text order
    for kw in LET_STATUS_KEYWORDS:
        for m in re.finditer(_let_status_pattern(kw), text, re.IGNORECASE):
            found.append((m.start(), m.group(0)))
    # Sorted by position, not by LET_STATUS_KEYWORDS' own list order - two
    # different keywords matching the same text (e.g. "Withdrawn" and
    # "U/O" in the same field) must come back in the order a reviewer
    # would actually read them, not in whatever order this tuple happens
    # to list keywords in.
    found.sort(key=lambda pair: pair[0])
    return [matched_text for _start, matched_text in found]


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

    "u/o"'s own word-boundary check still does real work despite the "/"
    in the middle: \b only requires a word/non-word transition at the
    pattern's own start and end, so standalone "U/O" (or wrapped in
    punctuation/whitespace, e.g. "(U/O)") matches, while it stays correctly
    excluded when "u"/"o" are themselves part of a longer alphanumeric run
    with no real boundary on that side - e.g. neither "flu/office" (no
    boundary right before "u", since "l" immediately before it is also a
    word character) nor "u/office" (no boundary right after "o", since "f"
    immediately after it is also a word character) matches.
    """
    return bool(_let_status_matches(text))


def matched_let_status_phrases(text) -> list:
    """
    The actual phrase(s) within `text` that make mentions_let_status(text)
    True, in their ORIGINAL source casing/punctuation (see
    _let_status_matches) - distinct phrases only, first-seen order (a field
    mentioning "Let" twice returns it once). Built on the exact same regex/
    word-boundary/pre-let-exclusion logic mentions_let_status itself uses,
    so the two can never drift apart into disagreeing about which text
    counts.

    Real gap this closes: a decision prompt previously showed a flagged
    field's ENTIRE text verbatim - e.g. a real Workplace Plus special_
    features value burying "U/O" inside a long amenity list - forcing a
    reviewer to hunt for the actual trigger. This returns just the
    trigger(s).

    Returns [] when text has no match at all - genuinely correct for text
    mentions_let_status already returned False for, but ALSO the honest
    answer if ever called on text that somehow contains no match despite
    mentions_let_status being True for it (shouldn't happen, since both
    share _let_status_matches, but never assumed) - see let_status_display_
    text for the display-safe wrapper that falls back to the full text
    rather than ever showing a caller something blank.
    """
    seen = []
    for m in _let_status_matches(text):
        if m not in seen:
            seen.append(m)
    return seen


def let_status_display_text(text) -> str:
    """
    Display-ready text for one let-status-flagged field's value: just the
    matched trigger phrase(s) (see matched_let_status_phrases), "; "-joined
    when there's more than one, in their original casing - never the
    field's entire text. Shared by both _render_let_status_decision
    (matched rows) and _render_new_property_let_status_decision (brand-new
    rows) in pages/2_Review_and_Master.py, so the two decision prompts stay
    consistent rather than drifting into two independent implementations.

    Falls back to `text` itself, completely unchanged, whenever matched_
    let_status_phrases comes up empty - genuinely should never happen for
    a field mentions_let_status already confirmed True for before either
    caller ever reaches this, but this is display code, reached only AFTER
    that confirmation already happened elsewhere; a defensive fallback to
    the full original text is far safer than ever surfacing a blank or
    broken message to a reviewer.
    """
    phrases = matched_let_status_phrases(text)
    return "; ".join(phrases) if phrases else str(text)


def _new_row_let_status_fields(new_row) -> frozenset:
    """
    Mirrors MatchedRow.let_status_fields' own computation (see build_merge_
    plan) for a row with no master record to diff against at all - a
    genuinely new property has no before/after pair, only its own current
    text, so this checks new_row's own LET_STATUS_FIELDS values directly via
    mentions_let_status. Used to build every UnmatchedRow's own let_status_
    fields (see that dataclass).

    Real gap this closes: previously, "does this text say Under Offer/Let/
    no longer available" was only ever checked for a row that matched an
    EXISTING master record and changed - a brand-new property (no master
    match at all) whose own special_features/state_of_space ALREADY states
    this sailed straight through with no decision prompt at all, even
    though a reviewer is exactly as likely to want a say over adding a
    property that's already unavailable as over updating one to say so.
    """
    return frozenset(f for f in LET_STATUS_FIELDS if mentions_let_status(getattr(new_row, f)))


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
    that KNOWN_PROVIDERS might then separately recognize.

    internal_ref is kept in sync with this SAME correction, but only for a
    row whose internal_ref already case-insensitively matched provider's
    own value BEFORE this correction ran. internal_ref is documented (see
    schema.ExtractedFields' own "internal_ref ... mirrors provider"
    comment) to mirror provider - confirmed real bug this fixes: a PDF
    upload's own Gemini extraction (extract.py's "internal_ref": raw.get(
    "provider"), a verbatim copy at extraction time) produced internal_ref
    ="business cube" while provider, corrected here to "Business Cube" via
    KNOWN_PROVIDERS, left internal_ref stranded at the old lowercase text -
    the two fields drifted apart despite representing the identical real
    fact, purely because only one of them ever got corrected.

    Deliberately NOT unconditional: extract_spreadsheet.py's own column-
    alias mapping can populate internal_ref from a genuinely different,
    provider-specific "External Ref" spreadsheet column (a real per-
    listing reference code, nothing to do with the provider's own name) -
    a row like that never case-insensitively matched provider in the
    first place, so this leaves it completely untouched rather than
    guessing that two already-different values were somehow meant to
    become the same one.
    """
    for row in rows:
        old_provider = row.provider
        new_provider = canonicalize_provider_name(_strip_provider_purpose_suffix(old_provider))
        if (
            not _is_blank(row.internal_ref) and not _is_blank(old_provider)
            and str(row.internal_ref).strip().lower() == str(old_provider).strip().lower()
        ):
            row.internal_ref = new_provider
        row.provider = new_provider


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


# address_1 ONLY - never every str field (see _values_equal's own docstring
# on why punctuation otherwise stays a real, flagged difference elsewhere).
# Confirmed real false-positive review this exists for: a re-upload
# restating the exact same "33 Cavendish Square" with a single harmless
# trailing comma ("33 Cavendish Square,") produced a decision card with
# nothing a reviewer could actually see or act on - _normalize_text alone
# already left the trailing comma in place (deliberately, for every OTHER
# field), so this was a genuine, if invisible-to-_values_equal, difference.
ADDRESS_TRAILING_PUNCTUATION_FIELDS = ("address_1",)

# Trailing punctuation only - a full address line's own harmless closing
# mark(s) (a stray comma/period/semicolon a source appends or omits) -
# never a hyphen (would corrupt a real range like "27-30") and never
# anything mid-string (an address with genuinely different embedded text,
# e.g. an appended locality one side states and the other doesn't, is left
# as a real, flagged difference - this only forgives the harmless trailing
# case the real "33 Cavendish Square,"/"33 Cavendish Square" pair is).
# Applied AFTER _normalize_text has already discarded all whitespace, so
# there is never any left to strip here - the set is punctuation only.
_ADDRESS_HARMLESS_TRAILING_CHARS = ",.;:"

# Unicode dash-punctuation variants a copy-paste (e.g. via Word/Outlook's
# own autocorrect, or a source PDF's own typography) routinely substitutes
# for a plain hyphen-minus within a house-number range - hyphen (U+2010),
# non-breaking hyphen (U+2011), figure dash (U+2012), EN DASH (U+2013, by
# far the most common real substitution), em dash (U+2014), horizontal bar
# (U+2015). Normalized to a plain "-" for COMPARISON only (never written
# back to any stored value) - address_1 alone, same narrow scope as the
# trailing-punctuation tolerance above, never a general character-class
# strip. Confirmed real case: "19-21 Great Portland Street" (hyphen-minus)
# vs "19–21 Great Portland Street" (en dash) - the exact same real address,
# semantically identical, previously compared as genuinely different since
# _normalize_text leaves every character other than whitespace/case
# untouched. Deliberately a SUBSTITUTION (dash-for-dash), not a strip -
# "19-21" and "19 21" must never become equal, only two different dash
# GLYPHS meaning the same thing should.
_ADDRESS_DASH_VARIANTS_RE = re.compile("[‐‑‒–—―]")


def _normalize_address_for_comparison(value) -> str:
    """
    address_1's own tolerant-comparison form: every Unicode dash variant
    folded to a plain hyphen-minus (see _ADDRESS_DASH_VARIANTS_RE's own
    docstring), then _normalize_text's case/whitespace folding, PLUS a
    harmless trailing punctuation mark stripped (see _ADDRESS_HARMLESS_
    TRAILING_CHARS/ADDRESS_TRAILING_PUNCTUATION_FIELDS' own docstring for
    the real "33 Cavendish Square," case this exists for). Deliberately
    NOT a general remove_all_punctuation() - a hyphen inside the string (a
    real house-number range, "27-30") is left completely untouched (only
    ever substituted for an equivalent dash GLYPH, never removed), and so
    is any OTHER mid-string punctuation/content difference; only the
    trailing mark(s) are stripped and dash variants folded, so two
    genuinely different addresses ("33 Cavendish Square" vs "34 Cavendish
    Square", "33 Cavendish Square" vs "33 Cavendish Street", "19-21 Great
    Portland Street" vs "19 Great Portland Street", or vs "19-23 Great
    Portland Street") still compare different exactly as before - only the
    digits/letters themselves (and, now, dash GLYPH choice) ever decide
    that, completely unaffected by this.
    """
    text = _ADDRESS_DASH_VARIANTS_RE.sub("-", str(value))
    return _normalize_text(text).rstrip(_ADDRESS_HARMLESS_TRAILING_CHARS)


# Confirmed real case (Kitt's Availability file): rent_pcm/rent_psf
# routinely come from two different sheets computed to different
# precision - one sheet states rent_psf as 243.108108... (a live
# rent_pcm/size_sqft division), the other has the SAME figure pre-rounded
# to 243.0 - both display as the identical "£243" on screen (see
# listing_summary_lines' own f"{value:,.0f}" rounding), but the default
# 1e-6 tolerance below is far tighter than what a person can even see,
# so this was flagging a genuine display-identical pair as a real
# disagreement. Compared at that SAME whole-pound precision instead, for
# these two fields only - desks_min/desks_max and size_sqft have no
# confirmed case of this exact rounding-precision mismatch, and widening
# tolerance for a field with no such confirmed need would be exactly the
# kind of guess this module avoids elsewhere.
_WHOLE_NUMBER_TOLERANCE_FIELDS = ("rent_pcm", "rent_psf")


def _values_equal(old, new, field_name: str = None) -> bool:
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        if field_name in _WHOLE_NUMBER_TOLERANCE_FIELDS:
            return round(float(old)) == round(float(new))
        return abs(float(old) - float(new)) < 1e-6
    if field_name in ADDRESS_TRAILING_PUNCTUATION_FIELDS:
        return _normalize_address_for_comparison(old) == _normalize_address_for_comparison(new)
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
        if _is_blank(old_val) or not _values_equal(old_val, new_val, f):
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
        if _values_equal(old_val, new_val, f):
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


def _item_numbers(item: str) -> frozenset:
    """
    Every digit sequence in `item`, as a set of number strings - used only
    by _items_similar's own numeric-contradiction veto. A PARTIAL overlap
    (one number shared, another genuinely different - e.g. "5 meeting
    rooms on floor 3" vs "2 meeting rooms on floor 3") still makes the two
    sets unequal, so it's still treated as a contradiction by that check's
    own `numbers_a != numbers_b` comparison - deliberately not "completely
    disjoint" specifically, since a real changed fact routinely sits
    alongside an unrelated, unchanged number in the same item, and
    partial-overlap-as-safe would miss exactly that shape.
    """
    return frozenset(re.findall(r"\d+", item))


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

    A NUMERIC CONTRADICTION vetoes similarity outright, checked before the
    significant-words comparison below - two items can share every
    significant WORD while stating a genuinely different FACT purely via
    an embedded number ("5 meeting rooms" vs "2 meeting rooms", "36 month
    term" vs "24 month term") - _significant_words itself only tokenizes
    letters, so the word-overlap ratio alone reads these as a 100% match
    ("meeting"/"rooms" shared, the number itself never compared at all),
    exactly the shape a genuine changed FACT (not a reworded one) takes.
    Only fires when BOTH items contain at least one number and those
    number sets genuinely differ - an item with no number at all, or two
    items agreeing on every number they each state, are both unaffected
    (see _item_numbers's own docstring for why a PARTIAL number overlap -
    e.g. one shared floor number alongside one genuinely different room
    count - still counts as a contradiction, not a partial match).
    """
    if " ".join(item_a.lower().split()) == " ".join(item_b.lower().split()):
        return True

    numbers_a, numbers_b = _item_numbers(item_a), _item_numbers(item_b)
    if numbers_a and numbers_b and numbers_a != numbers_b:
        return False

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


def _safe_to_auto_merge_detail_loss(old_val, new_val, field_name: str = None) -> bool:
    """
    True when is_detail_loss(old_val, new_val) is safe for build_merge_
    plan's own auto-merge loop to resolve via merge_compatible_text,
    rather than needing a manual review decision - a NARROWER question
    than is_detail_loss itself: not every old-item-goes-unrestated shape
    is safe to silently patch back together, even though every one of
    them is real evidence worth flagging for review on its own.

    False (never safe, must go to manual review even though merge_
    compatible_text COULD technically reassemble something) when either:

    1. new_val's own items are a pure EXACT-match SUBSET of old_val's (see
       _detail_items/normalize_key) - old_val had something new_val simply
       dropped, with NOTHING new offered in its place at all. Confirmed
       real want: "Great natural light; 5 meeting rooms; Bike storage" ->
       "Great natural light; Bike storage" must be a review decision, not
       silently re-patched back to "...; Bike storage; 5 meeting rooms" -
       a pure loss with zero new information is exactly the shape a
       reviewer needs to see and decide on, not have quietly reconciled
       for them.
    2. A NUMERIC CONTRADICTION exists between an old item and a new item
       that otherwise shares enough words to be the "same" topic (see
       _item_numbers/_items_similar's own numeric veto) - "5 meeting
       rooms" -> "2 meeting rooms" is a genuinely CHANGED fact, not two
       independent facts safe to concatenate; silently keeping both
       ("2 meeting rooms; ...; 5 meeting rooms") would be actively
       misleading, worse than either value alone.

    Still True (safe to auto-merge) for the ordinary, already-established
    case merge_compatible_text exists for: an old item entirely REPLACED
    by a genuinely different, unrelated new item (e.g. "10-person
    boardroom" -> "newly fitted kitchen", sharing no words and no
    numbers) - new_val is not a pure subset there (it offers real new
    content), and there's no numeric contradiction to find, so this stays
    exactly as permissive as it always was for that real, already-tested
    shape.
    """
    old_items = _detail_items(str(old_val), field_name)
    new_items = _detail_items(str(new_val), field_name)

    old_keys = {normalize_key(i) for i in old_items if normalize_key(i)}
    if old_keys and all((normalize_key(i) in old_keys) for i in new_items if normalize_key(i)):
        return False

    for old_item in old_items:
        old_numbers = _item_numbers(old_item)
        if not old_numbers:
            continue
        for new_item in new_items:
            new_numbers = _item_numbers(new_item)
            if not new_numbers or new_numbers == old_numbers:
                continue
            words_a, words_b = _significant_words(old_item), _significant_words(new_item)
            if not words_a or not words_b:
                continue
            overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
            if overlap >= ITEM_SIMILARITY_THRESHOLD:
                return False

    return True


def _has_suspicious_duplicate_items(text, field_name: str = None) -> bool:
    """
    True when `text` (split via _detail_items - the SAME semicolon/newline
    [/comma for special_features] split is_detail_loss itself already uses)
    restates the exact same item twice - a THIRD, independent detail-loss
    signal alongside is_detail_loss/is_richness_regression, needed because
    neither of those reliably catches a pure accidental duplication:
    is_detail_loss only fires when an OLD item goes unrestated (a duplicate
    that also restates everything old_val had triggers nothing there), and
    is_richness_regression only fires on an overall SHORTER new_val (a
    duplicated item can make new_val even LONGER than old_val, the opposite
    signal). Confirmed real case this exists for: a genuine Thirty
    Lighterman re-extraction whose special_features stated "Penthouse
    suite, private terrace" twice.

    Deliberately CONSERVATIVE - exact, normalize-via-_detail_items
    duplication only (case/whitespace-insensitive, but never a fuzzy/
    reworded near-duplicate - see _items_similar for that separate, much
    weaker-evidence concern, used only for old-vs-new comparison, never for
    this same-text-twice check). A literal repeated item is strong,
    unambiguous evidence of an accidental extraction duplication; a merely
    SIMILAR pair of items is common and legitimate (e.g. two genuinely
    different meeting-room features that both happen to mention "meeting
    room") and must never be flagged here.

    Works on a SINGLE value, not an old/new pair - callers use it two ways:
    directly on a freshly extracted value (no old_val needed at all - a
    self-duplicating extraction is suspicious regardless of what master
    already has, including when old_val is blank), and by build_merge_plan
    as one more risky_fields signal alongside is_detail_loss/is_richness_
    regression.
    """
    items = _detail_items(str(text), field_name)
    return len(items) != len(set(items))


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


def _deduped_special_features(text: str) -> str:
    """
    text with every EXACT (normalize_key-equal) repeated item collapsed to
    its first occurrence - a final, ingestion-PATH-INDEPENDENT safety net,
    used by build_merge_plan on new_dict's own incoming special_features
    value before diffs/risky_fields ever see it (see that function's own
    call site comment for exactly why/where).

    Confirmed real gap this closes: brochure_enrichment._apply_units_to_
    row's own item-level dedup (commit 085272b) only ever runs for the
    brochure-link tier-COMBINING path - a raw single-source extraction
    (e.g. extract_spreadsheet_gemini.py, one Gemini call per row with
    nothing to combine at all) never passes through it, so a self-
    duplicating raw Gemini response (a known LLM failure mode - the same
    sentence restated back-to-back) previously reached a reviewer
    completely uncleaned. _has_suspicious_duplicate_items already detects
    this correctly and blocks it from auto-applying - that detection was
    always working; nothing ever REPAIRED it, so the row sat in manual
    review forever with no path to clearing itself, even on a re-upload
    (the same source is likely to reproduce the same duplication again).

    Only ever called once _has_suspicious_duplicate_items has ALREADY
    confirmed `text` genuinely contains a repeated item (see that call
    site) - never called speculatively on a value that might already be
    clean, so a genuinely clean value (including one 085272b's own dedup
    already cleaned - this is a safe no-op there) is never reformatted by
    passing through here at all. This matters concretely: _split_list_
    items also comma-splits special_features specifically (see
    _MERGE_COMMA_SPLIT_FIELDS), so rebuilding via split-then-rejoin would
    otherwise cosmetically turn "a, b" into "a; b" even on a value with no
    duplication whatsoever - gating the whole rebuild behind an already-
    confirmed duplicate keeps that side effect confined to rows that were
    already going to be flagged as malformed, never a universal rewrite.

    Same conservative, exact-match-only philosophy as _has_suspicious_
    duplicate_items itself (see its own docstring - never a fuzzy/reworded
    match) - reuses _split_list_items/normalize_key directly rather than
    reimplementing splitting or normalization.
    """
    items = _split_list_items(text, "special_features")
    seen = set()
    deduped = []
    for item in items:
        key = normalize_key(item)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(item)
    return "; ".join(deduped) if deduped else text


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


def draft_merge_text(values: list) -> str:
    """
    Plain starting draft for a reviewer-edited RISKY_TEXT_FIELDS merge box
    (see pages/2_Review_and_Master.py's own _render_intra_batch_duplicate_
    group "Same listing — merge" choice) - every non-blank value in
    `values` (listing order), joined with "; ", the field's own documented
    canonical separator (see merge_compatible_text/_detail_items).

    Deliberately NOT merge_compatible_text - this is a genuine conflict
    (_text_variants_compatible already said no, or the review card
    wouldn't be showing a merge box for it at all), so there's no
    confirmed-non-conflicting "newest wins, append only what's missing"
    resolution to fall back on here; a straightforward, uncleaned
    concatenation is deliberately all this offers - the reviewer edits it
    into its own final wording themselves, this is never the final value.
    """
    return "; ".join(str(v) for v in values if not _is_blank(v))


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


_FLOOR_WORD_RE = re.compile(r"\bfloor\b", re.IGNORECASE)

# Common compass-direction abbreviations, mapped to their canonical
# expanded form - see _floor_unit_key's own docstring. Same small, explicit,
# whole-token-only philosophy as _STREET_SUFFIX_EXPANSIONS (used only for
# building-street matching, never here) - deliberately scoped to
# _floor_unit_key alone, never applied by normalize_key/_building_match_key
# themselves.
_COMPASS_ABBREVIATIONS = {
    "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
}

# Generic descriptor words that routinely sit directly next to a building's
# own name as part of how a source phrases it ("Hallmark House", "Nexus
# Building") - see _floor_unit_key's own building-prefix/suffix strip.
# Deliberately a short, explicit, hand-picked pair rather than a general
# scheme - the same conservative philosophy as _STREET_SUFFIX_EXPANSIONS/
# _COMPASS_ABBREVIATIONS above.
_BUILDING_DESCRIPTOR_WORDS = ("house", "building")


def _floor_unit_key(building, floor_unit) -> str:
    """
    normalize_key(floor_unit), with a redundant occurrence of the row's OWN
    building name stripped from either end first - confirmed real shape:
    the same real unit's floor_unit extracted as "Hallmark 6th Floor" in
    one upload and plain "6th Floor" in another (building "Hallmark" in
    both) - an exact normalize_key comparison never reconciles these two
    strings even though a human reads them as unambiguously the same
    floor, since building+provider already agree and only floor_unit
    carries the redundant building-name text one source happened to repeat.

    Deliberately a plain prefix/suffix strip, never a fuzzy/similarity
    match: only fires when floor_unit's own normalized text literally
    BEGINS or ENDS with building's own normalized text at a word boundary -
    "Hallmark 6th Floor" strips to "6th floor" (building "Hallmark"), but
    "Hallmark Annex 6th Floor" does NOT strip against building "Hallmark"
    (the word right after the shared prefix is "annex", not one of the
    tolerated descriptor words below, and not a boundary onto the rest of
    the text either) - a genuinely different, longer building name/phrase
    is never partially matched against a shorter one this way.

    Tolerates exactly ONE trailing/leading occurrence of a generic building
    descriptor word (_BUILDING_DESCRIPTOR_WORDS - "house"/"building")
    immediately adjacent to the building name, on whichever side it
    strips from: "Hallmark House 6th Floor" strips to "6th floor" against
    building "Hallmark" (prefix side: building name, then descriptor, then
    a word boundary onto the rest), and "6th Floor Hallmark House" strips
    the same way from the end (suffix side: the rest of the text, then
    building name, then descriptor, right at the end) - confirmed real
    case, see below. Any OTHER word in that position (not "house"/
    "building") is left alone, exactly as before this tolerance existed -
    see test_a_different_building_that_happens_to_start_the_same_is_not_
    stripped's own "Annex" case.

    Also strips the standalone word "floor" itself (word-boundary matched
    via \\bfloor\\b, case-insensitively, on the RAW floor_unit text -
    BEFORE normalize_key ever runs, then re-normalized) - confirmed real
    cases from one real upload: Kent House ("1st floor (South)" vs "1st
    (South)") and two Elm Yard rows ("5th floor" vs "5th", "4th floor" vs
    "4th"), all flagged as near-misses purely because the word "floor" was
    present in one source's own phrasing and absent in the other's, with
    nothing else different. Doing this strip on the RAW text, rather than
    normalize_key(floor_unit) as originally implemented, matters for a
    trickier real case: Elsley House's "1st floor(N) Elsley House" - by the
    time normalize_key has already stripped "(" ")", "floor(N)" has fused
    into "floorn" with no word boundary left for \\bfloor\\b to ever match
    (the character immediately after "floor" is now "n", a word character,
    not a boundary). The raw string still has a real "(" there, a genuine
    boundary character, so stripping "floor" first - while it's still
    visible - catches this. \\bfloor\\b still only ever matches the exact,
    separate word either way, so "Floorspace" is never touched.

    After normalize_key, any token (whitespace-split) that's an EXACT,
    whole match for a _COMPASS_ABBREVIATIONS key is expanded to its
    canonical form - same "N" vs "North" idea Elsley House's own address
    needed ("(N)" -> "n" once normalize_key strips the parentheses around
    it), never applied inside a longer token (a unit code like "N12" is
    left alone). Scoped to this function alone.

    Finally, if what's left after every strip/expansion above is NOTHING
    BUT a single floor number - optionally with an ordinal suffix ("8th"
    or "8"), recognized via the same _FLOOR_NUMBER_TOKEN_RE number-SHAPE
    pattern _floor_number (below) uses - normalizes to that bare number.
    Confirmed real cases: Mainframe's "8th floor" (master) vs "8"
    (upload), and likewise "7th floor"/"7", "4th floor"/"4".

    Deliberately a FULL-STRING match against the entire remaining text
    (_FLOOR_NUMBER_TOKEN_RE.fullmatch), never _floor_number's own token-
    by-token scan, which tolerates and ignores OTHER surrounding words by
    design for its own, looser near-miss-conflict purpose (see that
    function's own docstring - "Mainframe — Colliers — 8" deliberately
    still resolves to 8 there, junk words and all). Reusing that same
    tolerant behavior HERE would be unsafe: it would reduce "1st South"
    and "1st North" down to the same bare "1", silently discarding the
    one word that actually distinguishes them. Requiring the ENTIRE
    remaining text to be nothing but the number means "2nd & 4th Floors"
    (two numbers) and "LG" (no number) both correctly keep falling through
    unchanged, exactly as they do today - and Elsley House's own "1st
    north" (a number PLUS a real extra word) stays "1st north", never
    collapsed to bare "1", the same way "1st South"/"1st North" already do.
    """
    raw = "" if floor_unit is None or (isinstance(floor_unit, float) and pd.isna(floor_unit)) else str(floor_unit)
    floor_key = normalize_key(_FLOOR_WORD_RE.sub("", raw))
    building_key = normalize_key(building)

    if building_key and floor_key.startswith(building_key):
        rest = floor_key[len(building_key):]
        if rest == "" or rest[0] == " ":
            floor_key = rest.strip()
            for descriptor in _BUILDING_DESCRIPTOR_WORDS:
                if floor_key == descriptor or floor_key.startswith(descriptor + " "):
                    floor_key = floor_key[len(descriptor):].strip()
                    break
    elif building_key:
        # Unlike the prefix side above, entry into this branch can't be
        # gated on a plain floor_key.endswith(building_key) check - the
        # real Elsley House shape this exists for ends in "...elsley
        # house", which does NOT end with bare "elsley" at all (it ends
        # with "house"). So the combo ("<building> house"/"<building>
        # building") suffix is tried FIRST, and only falls back to a bare
        # building_key suffix match if no descriptor is present.
        stripped = None
        for descriptor in _BUILDING_DESCRIPTOR_WORDS:
            combo = f"{building_key} {descriptor}"
            if floor_key == combo or floor_key.endswith(" " + combo):
                stripped = floor_key[: -len(combo)].strip()
                break
        if stripped is None and (floor_key == building_key or floor_key.endswith(" " + building_key)):
            stripped = floor_key[: -len(building_key)].strip()
        if stripped is not None:
            floor_key = stripped

    floor_key = " ".join(_COMPASS_ABBREVIATIONS.get(word, word) for word in floor_key.split())

    number_match = _FLOOR_NUMBER_TOKEN_RE.fullmatch(floor_key)
    return str(int(number_match.group(1))) if number_match else floor_key


def _fallback_key(row: dict) -> tuple:
    return (
        _building_match_key(row.get("building")),
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


def _expand_street_suffix(normalized_text: str) -> str:
    """Expands a common UK street-suffix abbreviation (see
    _STREET_SUFFIX_EXPANSIONS) in the LAST word only of already-normalize_
    key'd text - e.g. "89 charterhouse st" -> "89 charterhouse street".
    Never touches any word but the last, so "st james's" (a name, not a
    trailing street suffix) is left completely alone. Shared by
    _address_street_key and _building_match_key so the two never drift
    apart on what "expand the suffix" means."""
    words = normalized_text.split()
    if not words:
        return normalized_text
    words[-1] = _STREET_SUFFIX_EXPANSIONS.get(words[-1], words[-1])
    return " ".join(words)


def _building_match_key(building) -> str:
    """normalize_key(building) with a trailing street-suffix abbreviation
    expanded (see _expand_street_suffix) - lets building-identity matching
    (_fallback_key, and by extension _primary_key/_dedup_key) recognize
    "Nineteen Wells Street" and "Nineteen Wells St" as the same building.

    Deliberately does NOT strip a leading house number the way
    _address_street_key does for its own intra-batch grouping use: this
    key drives actual matching against master (and dedup among pending
    rows), not just a near-miss grouping a human still reviews, so
    collapsing two different numbered buildings on the same street onto
    the same key here would be unsafe."""
    return _expand_street_suffix(normalize_key(building))


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


def _house_number_silently_dropped(old_val, new_val) -> bool:
    """
    True ONLY when a house number that was genuinely present in old_val has
    gone missing entirely in new_val, with the REST of the street text
    otherwise unchanged - the narrow "122-124 Regent Street" -> "Regent
    Street" shape (confirmed against real listings: Kitt's own "122-124
    Regent Street"/"Regent Street" pair, also seen with Parker Street's own
    "40-42" prefix). A legitimate address correction virtually always still
    states SOME house number, even a wrong one, so a number vanishing
    entirely while the rest of the street name stays byte-for-byte the same
    (once tolerantly normalized) is strong, narrow evidence of a bad/
    incomplete extraction, not a real edit - see build_merge_plan's own
    kept_as_is_fields for what this actually drives (the OLD value is kept
    automatically, but still surfaced as a non-blocking FYI, never a fully
    silent no-op, since there's a real if small chance the new value is
    actually a genuine renumbering).

    Deliberately much narrower than house_number_changed itself, which this
    does NOT alter or weaken in any way - a separate function layered on
    top, never a modification to what THAT one returns (other callers may
    depend on its current "any difference at all" semantics):
    - old_val must have had a REAL leading house number (leading_house_
      number(old_val) is not None) - nothing to "drop" otherwise.
    - new_val must have NONE at all (leading_house_number(new_val) is
      None) - a genuine number CHANGE ("18" -> "24") is a real edit, not
      this shape, and still falls straight through to house_number_
      changed's own existing "any difference" check, completely
      unaffected.
    - old_val's own remainder, with its leading house-number token
      stripped off (via LEADING_HOUSE_NUMBER_RE, matched against the RAW
      string first - same reason _address_street_key's own comment gives:
      normalize_key would destroy a hyphenated range's "-" before this
      pattern ever saw it), must normalize_key() to the EXACT same thing
      as new_val itself. Deliberately exact-match only, same conservative
      philosophy as _has_suspicious_duplicate_items/_safe_to_auto_merge_
      detail_loss elsewhere in this file - if the street name differs even
      slightly once the number's gone, this returns False and the
      existing risky-review path is completely untouched. Never
      _address_street_key - that function's own docstring explicitly
      documents it as unsafe for anything except intra-batch dedup
      grouping; this is a different, narrower, already-safe comparison
      (the number's absence is independently confirmed above before the
      remainder is ever compared), using normalize_key directly to keep
      the two concerns visibly separate.

    Callers must ALSO check address_conflict separately before trusting
    this (see build_merge_plan's own kept_as_is_fields computation) - a
    row whose own brochure has independently flagged a genuine address
    disagreement must always still force review, even on a field that
    also happens to match this shape.
    """
    if _leading_house_number(old_val) is None:
        return False
    if _leading_house_number(new_val) is not None:
        return False
    remainder = str(old_val)
    match = _LEADING_HOUSE_NUMBER_RE.match(remainder)
    if match:
        remainder = remainder[match.end():]
    return normalize_key(remainder) == normalize_key(new_val)


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
    against master - _primary_key/_fallback_key never call THIS function.
    They do share this function's suffix-expansion half via
    _building_match_key/_expand_street_suffix, but deliberately without
    the leading-house-number strip below: stripping the number here is
    safe only for this function's own looser near-miss grouping (a human
    still reviews the result), never for _fallback_key's actual matching
    against master, where collapsing two different numbered buildings on
    the same street onto one key would be unsafe. The fuzzy-matching tier
    (_fuzzy_building_match/_building_has_no_digits/
    BUILDING_FUZZY_MATCH_THRESHOLD) is untouched by any of this. That
    tier's numbered-address exclusion exists specifically because a
    SIMILARITY-score-based fuzzy match (difflib ratio) scores a genuinely
    different real property higher than an actual typo often enough to be
    dangerous on short numbered strings - a real, measured risk documented
    on BUILDING_FUZZY_MATCH_THRESHOLD itself. This function is a
    different, more conservative kind of check: a deterministic rule
    (strip a leading number, expand one suffix abbreviation), paired with
    _leading_house_number's own guard against merging two rows whose
    numbers genuinely disagree - not a score that could rank a different
    property above a real match.

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
    return _expand_street_suffix(text)


def merge_field_choice(values: list, field_name: str = None) -> tuple:
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

    field_name is optional and threaded straight through to _values_equal
    (see its own whole-pound tolerance for rent_pcm/rent_psf) - a caller
    that doesn't pass one keeps the exact prior byte/near-exact comparison
    for every field, same opt-in principle as matched_collision_field_
    choice's own field_name parameter.
    """
    classes = []  # one representative value per distinct class seen so far
    for v in values:
        blank = _is_blank(v)
        if any(blank == _is_blank(rep) and (blank or _values_equal(v, rep, field_name)) for rep in classes):
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


def listing_summary_lines(row_dict: dict, fields: list) -> list:
    """
    Human-readable "Label: value" lines for exactly `fields` (an ordered
    subset of DIFF_FIELDS) - used by the Review UI's ambiguous-duplicate-
    listing prompt (see pages/2_Review_and_Master.py's own _render_intra_
    batch_duplicate_group) so a reviewer sees the actual evidence that
    makes two candidate listings look different, not a fixed, arbitrary
    set of fields that may not even include the one that's genuinely
    different (see genuinely_differing_fields, whose result the caller is
    expected to pass in here - one call per row in the group, same
    `fields` list every time, so every listing's card names the same set
    of facts). Skips any requested field that's blank on THIS row -
    never a placeholder "—" line for a fact this row simply doesn't
    state, even when it's in `fields` only because some OTHER row in the
    group has a value for it.

    desks_min/desks_max are handled together as a single "Desks" line (a
    range when both are non-blank and differ, matching the pre-existing
    convention) whenever EITHER is requested - using THIS row's own
    actual values regardless of which one of the two was the one that
    genuinely differed across the group, so a range is never shown half-
    complete.

    Every other requested field reuses this module's own pre-existing
    per-field wording (floor/unit, size in sq ft, rent in £/pcm, rent in
    £/psf, fit-out state) where one already exists; anything else (e.g.
    building, submarket, brochure_link) falls back to a plain Title Case
    label and the raw value - rare in practice (most of those are part of
    the group's own matching identity and unlikely to genuinely differ),
    but never raises just because a field this function doesn't have
    bespoke wording for shows up in `fields`.
    """
    remaining = set(fields)
    lines = []

    if "floor_unit" in remaining and not _is_blank(row_dict.get("floor_unit")):
        lines.append(f"Floor/unit: {row_dict['floor_unit']}")
    if "size_sqft" in remaining and not _is_blank(row_dict.get("size_sqft")):
        lines.append(f"Size: {row_dict['size_sqft']:,.0f} sq ft")
    if "desks_min" in remaining or "desks_max" in remaining:
        desks_min, desks_max = row_dict.get("desks_min"), row_dict.get("desks_max")
        if not _is_blank(desks_min) and not _is_blank(desks_max) and desks_min != desks_max:
            lines.append(f"Desks: {desks_min:,.0f}–{desks_max:,.0f}")
        elif not _is_blank(desks_max):
            lines.append(f"Desks: {desks_max:,.0f}")
        elif not _is_blank(desks_min):
            lines.append(f"Desks: {desks_min:,.0f}")
    if "rent_pcm" in remaining and not _is_blank(row_dict.get("rent_pcm")):
        lines.append(f"Rent: £{row_dict['rent_pcm']:,.0f} pcm")
    if "rent_psf" in remaining and not _is_blank(row_dict.get("rent_psf")):
        lines.append(f"Rent per sq ft: £{row_dict['rent_psf']:,.0f}")
    if "state_of_space" in remaining and not _is_blank(row_dict.get("state_of_space")):
        lines.append(f"State: {row_dict['state_of_space']}")

    handled = {"floor_unit", "size_sqft", "desks_min", "desks_max", "rent_pcm", "rent_psf", "state_of_space"}
    for f in fields:
        if f in handled or _is_blank(row_dict.get(f)):
            continue
        lines.append(f"{title_case_label(f)}: {row_dict[f]}")

    return lines


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


# The four fields that together make up a new property's own "location"
# for this check - see new_property_missing_location/missing_location_
# labels below. lat/lng collapse into a single "map location" label when
# displayed (see missing_location_labels) - a reviewer thinks of them as
# one thing (a pin on a map), never as two independently-missing values.
NEW_PROPERTY_LOCATION_FIELDS = ("address_1", "postcode", "lat", "lng")

_MISSING_LOCATION_FIELD_LABELS = {"address_1": "address", "postcode": "postcode"}


def new_property_missing_location(row_dict: dict) -> bool:
    """True when ANY of address_1, postcode, lat, or lng is blank on a
    genuinely new property - purely informational (see the Review page's
    own "added anyway" note, which names specifically which of these are
    missing via missing_location_labels), never a reason to withhold the
    row itself."""
    return any(_is_blank(row_dict.get(f)) for f in NEW_PROPERTY_LOCATION_FIELDS)


def missing_location_labels(row_dict: dict) -> list:
    """
    Which of NEW_PROPERTY_LOCATION_FIELDS are blank on `row_dict`, as
    short, user-facing labels (never raw field names) for the Review
    page's own "added anyway" note - address_1/postcode each get their
    own label; lat/lng collapse into one "map location" label, listed
    once if EITHER half is blank (see NEW_PROPERTY_LOCATION_FIELDS' own
    comment on why). Order is always address, postcode, map location -
    never dependent on dict iteration order. Returns [] when nothing is
    missing (new_property_missing_location is False).
    """
    labels = [
        label for field, label in _MISSING_LOCATION_FIELD_LABELS.items()
        if _is_blank(row_dict.get(field))
    ]
    if _is_blank(row_dict.get("lat")) or _is_blank(row_dict.get("lng")):
        labels.append("map location")
    return labels


def _dict_location_hint(row: dict):
    """
    geocode.extract_postcode_hint's own postcode-district hint for `row`
    (a plain dict, master_merge's own row shape - never a ListingRow),
    checked in the exact same postcode/address_1/building priority
    geocode._source_location_hint already uses for a ListingRow - a local
    import (never a module-level one) because geocode.py itself imports
    normalize_key FROM this module, so a module-level "import geocode"
    here would be a circular import; deferred to call time, well after
    both modules have already finished loading, this is safe.
    """
    import geocode

    for field in ("postcode", "address_1", "building"):
        hint = geocode.extract_postcode_hint(row.get(field))
        if hint:
            return hint
    return None


def _address_conflicts(new_dict: dict, candidate: dict) -> bool:
    """
    True only when BOTH new_dict and candidate state a real, parseable
    postcode-district hint (see _dict_location_hint) and those hints
    genuinely disagree (different district) - the same conflict
    definition geocode.py's own _postcode_hint_conflicts already uses for
    the equivalent geocoding-acceptance check, reused here rather than a
    second, independently-drifting comparison. Never true when either
    side has nothing parseable to compare (an absent/unparseable hint is
    a reason to trust the fuzzy building-name match, not a reason to
    reject it) - this only ever EXCLUDES a candidate that already passed
    _suggest_similar's own fuzzy name/provider checks, never adds a new
    requirement for one that didn't.

    Confirmed real gap this closes: a master "City Tower"/GPE record
    whose own address_1/postcode state a real Canary Wharf address (3
    Limeharbour, E14 - a genuinely different, unrelated building) was
    spuriously suggested for a "City Tower"/GPE upload whose own address_1/
    postcode state GPE's actual managed office building at 40 Basinghall
    Street, EC2V - purely because the building NAME string is identical
    and both share the same provider, despite the two real addresses
    having nothing to do with each other.
    """
    new_hint = _dict_location_hint(new_dict)
    if not new_hint:
        return False
    candidate_hint = _dict_location_hint(candidate)
    if not candidate_hint:
        return False
    return new_hint["district"] != candidate_hint["district"]


_FLOOR_NUMBER_TOKEN_RE = re.compile(r"(\d+)(?:st|nd|rd|th)?", re.IGNORECASE)


def _floor_number(floor_unit):
    """
    The single floor number `floor_unit` unambiguously identifies, or None
    when there isn't exactly one to find - never a guess between several
    candidates. Splits on whitespace/comma/hyphen/en-dash/em-dash (covers
    both "8th Floor" - the number leading a word - and "Mainframe —
    Colliers — 8" - the number trailing a dash-separated provider/agent
    chain) and looks for a token that IS a bare number, optionally with an
    ordinal suffix ("8th" -> 8, "3rd" -> 3, "8" -> 8) - never a number
    merely embedded inside a longer token (a suite/unit code like "204A"
    is never mistaken for floor 204). Returns None when zero such tokens
    exist (nothing to compare) OR more than one genuinely different number
    does (e.g. "Suite 204, 3rd Floor" - which of 204/3 is "the" floor
    number is genuinely ambiguous, so this stays conservative rather than
    guessing) - a repeated token for the SAME number is fine either way.
    """
    if not floor_unit:
        return None
    tokens = re.split(r"[\s,–—-]+", str(floor_unit).strip())
    numbers = set()
    for token in tokens:
        match = _FLOOR_NUMBER_TOKEN_RE.fullmatch(token)
        if match:
            numbers.add(int(match.group(1)))
    if len(numbers) == 1:
        return next(iter(numbers))
    return None


def _floor_conflicts(new_dict: dict, candidate: dict) -> bool:
    """
    True only when BOTH new_dict and candidate have a floor_unit with a
    single, clearly extractable floor number (see _floor_number) and those
    numbers genuinely differ - the same permissive-default shape as
    _address_conflicts: never true when either side has nothing (or
    something ambiguous) to compare, so this only ever EXCLUDES a
    candidate that already passed every other check, never adds a new
    requirement for one that can't be compared at all. "8th Floor" vs a
    differently-FORMATTED same number ("Mainframe — Colliers — 8") is
    never a conflict - 8 == 8 regardless of how each source wrote it; only
    a genuinely different number (e.g. floor 7 vs floor 8) excludes.
    """
    new_floor = _floor_number(new_dict.get("floor_unit"))
    if new_floor is None:
        return False
    candidate_floor = _floor_number(candidate.get("floor_unit"))
    if candidate_floor is None:
        return False
    return new_floor != candidate_floor


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
    even as a hint a human is still free to dismiss.

    Also excludes any candidate whose own address genuinely conflicts with
    new_dict's (see _address_conflicts) - the opposite failure mode from
    _building_has_no_digits' own numbered-address exclusion: a generic,
    non-numbered building NAME shared by two genuinely different real
    buildings, rather than two numbered addresses that merely look
    similar. Confirmed real gap this closes: a master "City Tower"/GPE
    record at 3 Limeharbour (Canary Wharf, E14 - a real, unrelated
    residential building) was spuriously suggested for a "City Tower"/GPE
    upload describing GPE's own actual managed office building at 40
    Basinghall Street, EC2V, purely because the building name string is
    identical and both share the same provider. Only ever excludes a
    candidate that already passed every check above - never a new
    requirement for one whose address can't be compared at all (see
    _address_conflicts' own permissive "nothing to compare" default).

    Also excludes any candidate whose own floor_unit states a clearly
    different floor NUMBER from new_dict's (see _floor_conflicts) - the
    same shape of guard as _address_conflicts, one level down: two
    genuinely different floors of the same-NAMED building are not a near-
    miss for the SAME listing, regardless of how closely the building
    names themselves fuzzy-match. Never excludes merely differently-
    FORMATTED same number ("Mainframe — Colliers — 8" vs "8th Floor" is
    still 8 == 8, so it's still suggested, exactly as before this
    existed) and never excludes when either side's floor_unit has nothing
    clearly extractable to compare at all."""
    target_building = new_dict.get("building")
    if not _building_has_no_digits(target_building):
        return []
    target = normalize_key(target_building)
    if not target:
        return []
    target_provider = normalize_key(new_dict.get("provider"))
    candidates = [
        r for r in master_records
        if normalize_key(r.get("provider")) == target_provider
        and _building_has_no_digits(r.get("building"))
        and not _address_conflicts(new_dict, r)
        and not _floor_conflicts(new_dict, r)
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
    # see _house_number_silently_dropped - the OLD value is kept
    # automatically (never applied, never blocks a review decision), but
    # still surfaced as a non-blocking FYI caption, a third bucket
    # alongside risky_fields ("needs a decision") and the ordinary bundled-
    # safe-changes summary ("will apply automatically").
    kept_as_is_fields: frozenset = field(default_factory=frozenset)


@dataclass
class UnmatchedRow:
    new_row: ListingRow
    suggestions: list = field(default_factory=list)
    # see _new_row_let_status_fields/mentions_let_status - forces manual
    # review for a genuinely new property, same as MatchedRow's own field
    let_status_fields: frozenset = field(default_factory=frozenset)


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
        if any(_values_equal(v, rep, field_name) for rep in classes):
            continue
        classes.append(v)
    if len(classes) <= 1:
        return False, (classes[0] if classes else None)
    if field_name in RISKY_TEXT_FIELDS and _text_variants_compatible(classes):
        return False, _richest_text_value(classes)
    return True, None


def geocode_consolidation_groups(rows: list) -> dict:
    """
    Groups `rows` (MatchedRow objects already known to have a non-empty
    risky_fields - see pages/2_Review_and_Master.py's own decision_risky)
    by (building, provider) identity plus an identical NEW value on every
    GEOCODE_UNVERIFIED_FIELDS (address_1/postcode/lat/lng) member present
    in the row's own diffs - geocode.py's own geocode_rows grouping
    already guarantees the NEW value for these specific fields is
    resolved ONCE per building+provider and copied verbatim (including
    geocode_unverified - see that module's own group copy-down) to every
    row sharing that identity, so two rows agreeing on which of these
    fields changed and what they changed TO are describing the exact
    same underlying geocode fact, regardless of what each row's own OLD
    (master) value happened to be beforehand - a floor whose master
    record already had some stale text on file and a floor that was
    still blank are still the SAME shared geocode result once resolved,
    not two different ones. Returns only keys with 2+ members - a lone
    row with nothing else sharing its own geocode fields is left for its
    own individual card, unchanged.

    Deliberately restricted to GEOCODE_UNVERIFIED_FIELDS alone, never
    generalized to "any field that happens to match across rows" - e.g. a
    special_features value matching by coincidence across two rows carries
    no such guarantee of being the SAME underlying fact, only geocode.py's
    own grouped-and-copied fields do.

    The field LIST itself (which of the 4 fields actually changed for
    this row, sorted for determinism) is still part of the key, not just
    the values - two rows must agree on WHICH fields changed, not merely
    happen to share a value on the fields they both have; this is
    membership in the row's own diffs, never m.risky_fields' fuller set
    (a field can be risky for an unrelated reason - e.g. lat/lng via
    GEOCODE_RISK_FIELDS' own distance check, or address_1 via a
    structural house-number change - without saying anything about
    whether every OTHER member shares that same reason or value).
    """
    groups = {}
    for m in rows:
        geo_fields = tuple(sorted(f for f in GEOCODE_UNVERIFIED_FIELDS if f in m.diffs))
        if not geo_fields:
            continue
        key = (
            normalize_key(m.new_row.building),
            normalize_key(m.new_row.provider),
            tuple((f, m.diffs[f][1]) for f in geo_fields),
        )
        groups.setdefault(key, []).append(m)
    return {key: members for key, members in groups.items() if len(members) >= 2}


# DIFF_FIELDS fields that are internal pipeline bookkeeping, never
# something a reviewer should be asked to eyeball on the duplicate-
# listing comparison card (see pages/2_Review_and_Master.py's own
# _render_intra_batch_duplicate_group, via listing_summary_lines).
# property_id/source_file are already excluded from DIFF_FIELDS itself;
# these fields are the same idea for a value genuinely_differing_fields/
# _group_has_genuine_conflict still legitimately checks for the ACTUAL
# is-this-a-conflict decision - a genuine lat/lng or brochure_link_is_
# floorplan disagreement is still real evidence worth flagging the group
# for review over, it's only ever hidden from what gets SHOWN.
# development_name is behind-the-scenes geocoding metadata (see schema.
# ListingRow's own docstring), same idea as geocode_unverified alongside
# it here.
DUPLICATE_CARD_HIDDEN_FIELDS = (
    "brochure_link_broken", "brochure_link_is_floorplan", "lat", "lng", "geocode_unverified", "development_name",
    "special_features_matched",
)


def genuinely_differing_fields(dicts: list) -> list:
    """
    Every DIFF_FIELDS field (in DIFF_FIELDS' own declared order) with 2+
    genuinely different non-blank values across `dicts` (see matched_
    collision_field_choice, called with the field name so RISKY_TEXT_
    FIELDS gets its own reworded-but-compatible tolerance) - the single
    generic rule this module uses to decide whether a group of rows
    claiming to be the same property/unit can be safely auto-merged (see
    consolidate_unmatched_duplicates) or must be left for a human:
    automatically combining them would only ever change the DATA'S
    MEANING if some field genuinely disagrees, never merely because there
    happen to be several source rows. _group_has_genuine_conflict is
    defined directly in terms of THIS function's result (just "is it
    non-empty") rather than a second, separately-tuned copy of the same
    per-field rule - so does the duplicate-listing review card (pages/2_
    Review_and_Master.py's _render_intra_batch_duplicate_group, via
    listing_summary_lines), which is exactly why this is public and
    returns the actual field list rather than a bare bool. Confirmed real
    gap this closes: a Kitt's "28 Bruton Street" pair genuinely disagreed
    only on rent_psf (£310 vs £296, from a currency-formatted source cell
    that made it look blank and eligible for a second, disagreeing
    brochure re-read - see extract_spreadsheet._coerce_numeric), but the
    review card's own fixed five-field summary never included that field
    at all, so the two listings looked completely identical to a
    reviewer even though the app had correctly detected a real
    difference between them.

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

    Includes every DIFF_FIELDS field, even ones the review card itself
    chooses to hide from a human (see DUPLICATE_CARD_HIDDEN_FIELDS) -
    filtering those out is the CALLER's own display decision, never this
    function's: _group_has_genuine_conflict must keep acting on a genuine
    disagreement in a hidden field regardless of whether anyone ever sees
    it named on the card.
    """
    weak_geocoded_fields = ("postcode", "address_1")
    differing = []
    for f in DIFF_FIELDS:
        needs_choice, _ = matched_collision_field_choice([d.get(f) for d in dicts], f)
        if needs_choice and not (f in weak_geocoded_fields and _brochure_link_identity_override(dicts)):
            differing.append(f)
    return differing


def _group_has_genuine_conflict(dicts: list) -> bool:
    """True if genuinely_differing_fields(dicts) is non-empty - see that
    function's own docstring for the actual per-field rule (unchanged by
    this refactor to a shared helper - see its own docstring for why the
    duplicate-listing review card also needs the identical logic, not a
    second, separately-tuned copy of it)."""
    return bool(genuinely_differing_fields(dicts))


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
        merged_rows.append(UnmatchedRow(merged_row, suggestions, _new_row_let_status_fields(merged_row)))

    new_unmatched = [u for u in plan.unmatched if id(u) not in safe_member_ids] + merged_rows

    return MergePlan(
        plan.master_records, plan.matched_changed, plan.matched_unchanged,
        new_unmatched, plan.collisions, still_needs_review,
    )


# A bare UK postcode outward code and nothing else - area letters, district
# digits, an optional finer-subdivision letter (e.g. "SE1", "EC2", "W1S") -
# used both to recognize a submarket value that's really just a postcode
# district (never a genuine place name, which never matches this short
# letter+digit[+letter] shape) and to pull the identical exact code out of a
# row's own postcode field. Deliberately self-contained here rather than
# imported from geocode.py's own _is_bare_postcode_district/_district_parts
# (which answer a related but different question, see _outward_code_from_
# postcode's own docstring for why) - geocode.py already imports normalize_
# key FROM this module, so importing the other way would create a circular
# import between the two.
_OUTWARD_CODE_RE = re.compile(r"^([A-Za-z]{1,2}\d{1,2}[A-Za-z]?)$")


def _is_bare_postcode_district(value) -> bool:
    """True only when `value`, once trimmed, IS a bare UK postcode outward
    code (e.g. "SE1", "EC1V") and nothing else - false for a genuine place
    name ("Borough", "Shoreditch"), which never matches this shape, and
    false for a full postcode ("SE1 8QH", with its own inward code)."""
    if not value or not isinstance(value, str):
        return False
    return bool(_OUTWARD_CODE_RE.match(value.strip().upper()))


def _outward_code_from_postcode(postcode):
    """
    The exact, letter-preserving UK postcode outward code from a full or
    bare postcode string ("SE1 8QH" or "SE1" -> "SE1"; "EC1V 9RT" or
    "EC1V" -> "EC1V") - or None when `postcode` doesn't structurally look
    like a UK postcode at all.

    Deliberately NEVER collapses the optional subdivision letter the way
    geocode.py's own _district_parts does ("EC1A"/"EC1V"/"EC1" all treated
    as the same district there) - that collapsing is correct for THAT
    function's own purpose (rejecting a geocoding candidate in an
    obviously wrong area, where over-matching is the only real risk), but
    wrong for naming a submarket: a subdivision letter can mark a
    genuinely different named area, and collapsing it away here would risk
    exactly the kind of cross-area conflation this feature exists to
    avoid (see build_postcode_submarket_lookup's own docstring - SE1
    itself, which has no subdivision letter at all, is the sharpest real
    example of this exact ambiguity already existing at the finest grain
    UK postcodes offer).
    """
    if not postcode or not isinstance(postcode, str):
        return None
    match = re.match(r"^([A-Za-z]{1,2}\d{1,2}[A-Za-z]?)(?:\s*\d[A-Za-z]{2})?$", postcode.strip().upper())
    return match.group(1) if match else None


def build_postcode_submarket_lookup(master_records: list) -> dict:
    """
    Maps a bare UK postcode outward code (e.g. "SE1", "EC1V") to the one
    real, named submarket value already confirmed for it elsewhere in
    master - built ENTIRELY from existing master rows that already have
    both a real postcode and a genuinely useful (non-blank, non-bare-
    postcode-itself) submarket. Never a guess: an outward code with two or
    more confirmed rows that disagree on the name (a real, expected case -
    SE1 alone genuinely covers South Bank, Borough, Bankside, and
    Waterloo) maps to nothing here rather than picking one - "no confident
    answer yet" is never treated as license to guess among several
    already-observed real answers, the same zero-fabrication standard
    applied to why this doesn't ask Gemini to guess either. An outward
    code no confirmed row has ever stated at all is simply absent from the
    returned dict - a "not yet known" gap that resolves itself the next
    time any brochure in that district states its real name, never
    something this function tries to fill in.

    Takes build_merge_plan's own ALREADY-clean_value'd master_records
    (never a raw master_df.to_dict()) - deliberately, not just for reuse:
    a missing submarket/postcode on a raw DataFrame record comes back as
    float('nan'), which is truthy in Python and NOT caught by a plain
    `not submarket` check, so an unrelated row with no submarket at all
    would otherwise silently count as a second, conflicting "confirmed"
    value for its own postcode district - a real bug caught directly by
    this function's own test suite, not a hypothetical. clean_value
    already normalizes NaN to real None for exactly this reason.

    Sourced from each record's own `postcode` field only (never address_1/
    building free-text parsing, unlike geocode.py's own broader location-
    hint fallbacks) - keeps this feature's own definition of "confirmed"
    narrow and simple: a row's OWN stated postcode, nothing inferred.
    """
    confirmed_names_by_code = {}
    for rec in master_records:
        submarket = rec.get("submarket")
        if not submarket or _is_bare_postcode_district(submarket):
            continue
        outward_code = _outward_code_from_postcode(rec.get("postcode"))
        if not outward_code:
            continue
        confirmed_names_by_code.setdefault(outward_code, set()).add(submarket)
    return {code: next(iter(names)) for code, names in confirmed_names_by_code.items() if len(names) == 1}


def backfill_postcode_submarkets(master_records: list) -> tuple:
    """
    A SEPARATE, EXPLICIT action - deliberately never run automatically as
    part of build_merge_plan/every approve, unlike the fresh-row
    correction there. Re-scans EVERY existing row in `master_records`
    whose own submarket is still a bare postcode district and fills it in
    wherever build_postcode_submarket_lookup (built from this SAME
    snapshot) already has a confirmed name for that exact district - the
    same "never a guess, disagreement resolves to nothing" contract as
    the fresh-row path, applied retroactively instead of only going
    forward.

    Deliberately not automatic: silently rewriting existing master rows
    on every ordinary approve - unrelated to whatever that approve was
    actually about - is a much bigger, less reviewable blast radius than
    fixing only the rows a human just chose to re-check. A caller (e.g. a
    "Fix known bare postcodes" button on the Review & Master page, or a
    one-off script) is expected to show `changes` to a reviewer before
    actually writing `updated_records` to master, the same way every
    other write to master in this app is reviewable rather than silent.

    Returns (updated_records, changes) - a NEW list (master_records
    itself is never mutated) plus [{"property_id", "building", "postcode",
    "old_submarket", "new_submarket"}, ...] for every row actually
    corrected, in master_records' own order.
    """
    lookup = build_postcode_submarket_lookup(master_records)
    updated_records = []
    changes = []
    for rec in master_records:
        submarket = rec.get("submarket")
        confirmed = lookup.get(submarket.strip().upper()) if submarket and _is_bare_postcode_district(submarket) else None
        if confirmed:
            updated_records.append({**rec, "submarket": confirmed})
            changes.append({
                "property_id": rec.get("property_id"), "building": rec.get("building"),
                "postcode": rec.get("postcode"), "old_submarket": submarket, "new_submarket": confirmed,
            })
        else:
            updated_records.append(rec)
    return updated_records, changes


def build_submarket_casing_lookup(master_records: list) -> dict:
    """
    Maps normalize_key(submarket) -> the one, already-confirmed properly-
    cased submarket value for it - self-learning from master's own
    existing data, the same philosophy as build_postcode_submarket_
    lookup, deliberately never a maintained list (unlike canonicalize_
    provider_name's own KNOWN_PROVIDERS). Fixes e.g. a Workplace Plus
    upload's own "MAYFAIR"/"OLD STREET"/"KING'S CROSS" (a source
    spreadsheet's column-wide ALL-CAPS convention, copied verbatim by
    this pipeline with zero normalization) back to whatever properly-
    cased form ("Mayfair"/"Old Street"/"King's Cross") is already
    confirmed elsewhere in master for that same real place.

    normalize_key (lowercases, strips punctuation, collapses whitespace)
    is reused as-is, not reimplemented - confirmed directly against this
    exact real data that it does the right thing on every case that
    matters here: "KING'S CROSS" and "King's Cross" both normalize to
    "kings cross" (apostrophe simply stripped from both sides, never
    reconstructed via a naive .title()-style transform that would mangle
    it into "King'S Cross" - this function never algorithmically re-cases
    anything at all; it only ever substitutes an already-existing,
    humanly-authored verbatim string, exactly like build_postcode_
    submarket_lookup already does). "BANK", "MONUMENT", and "CANNON
    STREET/MONUMENT" normalize to three genuinely distinct keys ("bank",
    "monument", "cannon streetmonument") - the "/" is deleted as
    punctuation, never replaced with a space, so it can never accidentally
    fuse into a key some OTHER real area also happens to produce.

    A candidate value counts as "properly cased" only when it is NEITHER
    fully upper-case NOR fully lower-case (str.isupper()/.islower()) - an
    ALL-CAPS or all-lowercase value is exactly the symptom this fixes (a
    source spreadsheet's own column-wide casing convention), never
    trusted as the canonical form itself even though it still counts as
    real evidence that the underlying key exists.

    When several DIFFERENT properly-cased spellings already exist in
    master for the same key (a real, if rare, possibility - inconsistent
    history), the MOST FREQUENT one wins, ties broken alphabetically for
    a fully deterministic result - deliberately NOT build_postcode_
    submarket_lookup's own "disagreement resolves to nothing" rule: that
    rule protects against picking the wrong REAL FACT among several
    substantively different, all-equally-plausible answers (which actual
    submarket a postcode district is in - a genuine factual risk). Every
    candidate for one key here already names the IDENTICAL real place;
    the only question is which already-attested spelling/casing variant
    is most representative, a purely cosmetic choice with none of the
    "confidently wrong fact" risk a postcode guess carries, so a majority
    vote is a reasonable, low-risk tie-break rather than refusing to
    correct at all.

    A key with NO properly-cased candidate at all (every existing
    occurrence is itself all-caps/all-lowercase, never yet fixed) is
    simply absent from the returned dict - nothing confirmed-good exists
    yet to borrow, so nothing is corrected: the same "not yet known is
    not a wrong answer" principle as the postcode lookup, e.g. a
    genuinely new area (this same file's own Manchester rows) that has
    never appeared in any casing before is left completely untouched.
    """
    candidates_by_key = {}
    for rec in master_records:
        submarket = rec.get("submarket")
        if not submarket or not isinstance(submarket, str):
            continue
        if submarket.isupper() or submarket.islower():
            continue
        key = normalize_key(submarket)
        if not key:
            continue
        candidates_by_key.setdefault(key, Counter())[submarket] += 1

    lookup = {}
    for key, counts in candidates_by_key.items():
        max_count = max(counts.values())
        best = sorted(value for value, count in counts.items() if count == max_count)[0]
        lookup[key] = best
    return lookup


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

    postcode_submarket_lookup = build_postcode_submarket_lookup(master_records)
    submarket_casing_lookup = build_submarket_casing_lookup(master_records)

    matched_changed, matched_unchanged, unmatched = [], [], []

    for new_row in new_rows:
        # Deterministic postcode-district -> submarket correction, BEFORE
        # matching/diffing - see build_postcode_submarket_lookup's own
        # docstring for the full "never a guess" contract. Applied to
        # new_row itself (a fresh model_copy, never mutated in place -
        # ListingRow is frozen-by-convention elsewhere in this codebase),
        # not just a local dict, so this reaches BOTH downstream paths
        # equally: a MATCHED row's diff (built from new_dict below) and an
        # UNMATCHED row's own eventual master entry (built directly from
        # new_row.model_dump() elsewhere - see UnmatchedRow's own call
        # sites) - a fix that only touched a local dict here would silently
        # never reach the unmatched/brand-new-property path at all.
        submarket = new_row.submarket
        if submarket and _is_bare_postcode_district(submarket):
            confirmed_submarket = postcode_submarket_lookup.get(submarket.strip().upper())
            if confirmed_submarket:
                new_row = new_row.model_copy(update={"submarket": confirmed_submarket})

        # Casing correction runs AFTER the postcode fix above (on whatever
        # submarket value is current at this point) and deliberately needs
        # NO explicit diffs/silent routing of its own, unlike brochure_link_
        # broken's - _values_equal/silent_field_updates (both already
        # case/whitespace-insensitive - see their own docstrings) already
        # do the right thing automatically once the value itself is fixed
        # here, BEFORE new_dict/diffs/silent are computed below: an old_rec
        # that already has the good casing makes this byte-identical (no
        # diff, no silent update, fully invisible - correct, nothing
        # changed); an old_rec that's ALSO still badly cased makes this
        # tolerant-equal-but-not-identical, which silent_field_updates
        # already auto-applies as a silent update with no new code needed
        # here at all; see build_submarket_casing_lookup's own docstring
        # for why this is a silent update rather than a reviewable diff
        # (formatting of an already-known fact, never a new one).
        submarket = new_row.submarket
        if submarket:
            confirmed_casing = submarket_casing_lookup.get(normalize_key(submarket))
            if confirmed_casing and confirmed_casing != submarket:
                new_row = new_row.model_copy(update={"submarket": confirmed_casing})

        # A self-duplicating special_features value - the SAME item
        # restated twice, verbatim (see _has_suspicious_duplicate_items'
        # own docstring) - is repaired HERE, on new_row itself before
        # new_dict is built, for the exact same reason the submarket
        # corrections just above are applied to new_row rather than a
        # local dict: this must reach BOTH downstream paths equally, a
        # MATCHED row's diff (built from new_dict below) and an UNMATCHED
        # row's own eventual master entry (built directly from new_row.
        # model_dump() elsewhere - see UnmatchedRow's own call sites), and
        # it must run regardless of which extractor produced this row
        # (brochure_enrichment.py, extract_spreadsheet_gemini.py,
        # extract.py, extract_email.py all funnel through here - see
        # _deduped_special_features' own docstring for why this specific,
        # single, path-independent location was chosen). Gated on _has_
        # suspicious_duplicate_items itself, so a genuinely clean value
        # (including one brochure_enrichment.py's own dedup, commit
        # 085272b, already cleaned) is never touched at all.
        if new_row.special_features and _has_suspicious_duplicate_items(new_row.special_features, "special_features"):
            new_row = new_row.model_copy(
                update={"special_features": _deduped_special_features(new_row.special_features)}
            )

        new_dict = new_row.model_dump()
        master_idx, tier = None, None

        pk = _primary_key(new_dict)
        primary_candidates = primary_index.get(pk, []) if pk else []
        if len(primary_candidates) == 1:
            master_idx, tier = primary_candidates[0], "postcode"
        elif len(primary_candidates) > 1:
            # 2+ existing master rows already share this exact identity key -
            # the real, confirmed "1 Oliver's Yard" shape (two genuinely
            # separate listings, same building/provider/postcode, both
            # floor_unit blank - see master_merge's own listing-identity
            # rule). An ambiguous key match alone is exactly as unsafe to
            # auto-apply as no match at all, UNLESS this row's own size/
            # desks/rent evidence (see _best_listing_evidence_match) picks
            # out exactly one of them with real confidence - e.g. a re-
            # upload of that same source now stating fuller data than
            # whichever version is already on file for THAT specific
            # listing. Never guessed when two candidates fit equally well.
            best = _best_listing_evidence_match(new_dict, [master_records[i] for i in primary_candidates])
            if best is not None:
                master_idx, tier = primary_candidates[best], "postcode"

        if master_idx is None:
            candidates = fallback_index.get(_fallback_key(new_dict), [])
            if len(candidates) == 1:
                master_idx, tier = candidates[0], "fallback"
            elif len(candidates) > 1:
                best = _best_listing_evidence_match(new_dict, [master_records[i] for i in candidates])
                if best is not None:
                    master_idx, tier = candidates[best], "fallback"
            # Still nothing - an ambiguous fallback match is exactly as
            # unsafe to auto-apply as no match at all.
            if master_idx is None:
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

            # brochure_link_broken is diagnostic pipeline metadata, not a
            # property fact a reviewer should ever be asked to approve -
            # moved out of diffs (where it would otherwise show up as a
            # normal, clickable "field changed" row) and into silent
            # (auto-applied, never shown in any diff UI - see MatchedRow.
            # silent_updates' own docstring), the SAME bucket _apply_silent
            # already folds into every write regardless of whether this
            # row has any OTHER change at all. diff_fields' own blank-new-
            # value-skip rule already gives this exactly the right merge
            # behavior before this move: a fresh row that never got its
            # link re-checked this run (None) never overwrites master's
            # existing True/False, while a genuinely fresh True/False
            # (this run's own real render outcome) always does - nothing
            # else needed for a fixed-and-reuploaded link to self-heal.
            if "brochure_link_broken" in diffs:
                silent["brochure_link_broken"] = diffs.pop("brochure_link_broken")[1]

            # special_features_matched (see schema.ListingRow's own
            # docstring) is the same kind of diagnostic pipeline metadata as
            # brochure_link_broken immediately above - purely enrich_rows_
            # grouped's own resume bookkeeping (see _row_has_a_genuinely_
            # blank_enrichable_field's own docstring), never a property fact
            # a reviewer should ever be asked to approve. Same blank-new-
            # value-skip reasoning applies unchanged: a fresh row that never
            # got a genuine combine this run (None) never clears master's
            # existing True.
            if "special_features_matched" in diffs:
                silent["special_features_matched"] = diffs.pop("special_features_matched")[1]

            # geocode_unverified is the same kind of diagnostic pipeline
            # metadata as brochure_link_broken above - never a property
            # fact of its own for a reviewer to approve, just a signal this
            # module reads below (GEOCODE_UNVERIFIED_FIELDS) to decide
            # whether THIS row's own address_1/postcode/lat/lng diffs need
            # a stronger caution, then folds silently into the write like
            # any other diagnostic flag.
            if "geocode_unverified" in diffs:
                silent["geocode_unverified"] = diffs.pop("geocode_unverified")[1]

            # address_conflict (see schema.ListingRow's own docstring) is
            # the same kind of diagnostic pipeline metadata as brochure_
            # link_broken/geocode_unverified above - never shown as its
            # own raw diff line (a "None -> 'Brochure states ...'" row
            # would be meaningless to a reviewer on its own), folded
            # silently into the write instead. address_1 is then injected
            # into diffs (if not already there) so the row still gets a
            # genuine risky-field decision card below - the confirmed real
            # Ivybridge House shape this exists for has address_1 UNCHANGED
            # between old and new (the row's own value was already wrong
            # before AND after this run; only the brochure's own SEPARATE
            # text disagrees with it), so diff_fields alone would never
            # have surfaced address_1 as a field to review at all. Reuses
            # the exact same editable "New" value + Apply checkbox
            # mechanism every other risky field already has - a reviewer
            # who agrees with the brochure just edits the New value to
            # match it and applies, never a new, bespoke decision-card
            # shape invented just for this one field.
            if "address_conflict" in diffs:
                silent["address_conflict"] = diffs.pop("address_conflict")[1]
                diffs.setdefault("address_1", (old_rec.get("address_1"), new_dict.get("address_1")))

            # Auto-merge a DETAIL_LOSS_MERGE_FIELDS update BEFORE risky_fields
            # is computed below, whenever it's safe to (see merge_compatible_
            # text's own docstring): is_detail_loss says old_val has a
            # genuine item new_val doesn't restate, but is_richness_
            # regression says new_val isn't a drastic, all-round terser
            # replacement, AND _has_suspicious_duplicate_items says new_val
            # doesn't restate one of its OWN items twice (either shape must
            # stay a manual review, unmerged, exactly as before this
            # existed - see those functions' own docstrings). Mutating
            # diffs[f] here, rather
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
                # A suspicious duplicate WITHIN new_val (see _has_
                # suspicious_duplicate_items) blocks auto-merge the same
                # way is_richness_regression already does, just above -
                # merge_compatible_text's own merged output starts from
                # new_val's own items verbatim (see its own docstring), so
                # a self-duplicating extraction would otherwise carry its
                # duplicate straight into an auto-applied value with
                # nobody ever reviewing it.
                if _has_suspicious_duplicate_items(new_val, f):
                    continue
                # A pure, nothing-added loss or a genuine numeric
                # contradiction (see _safe_to_auto_merge_detail_loss's own
                # docstring) is a review decision, never something to
                # silently patch back together - only the ordinary
                # "old item replaced by different, unrelated new content"
                # shape merge_compatible_text was actually built for stays
                # auto-mergeable.
                if is_detail_loss(old_val, new_val, field_name=f) and _safe_to_auto_merge_detail_loss(
                    old_val, new_val, field_name=f
                ):
                    diffs[f] = (old_val, merge_compatible_text(old_val, new_val, field_name=f))

            # A house number silently dropped while the rest of the street
            # text stays unchanged (see _house_number_silently_dropped's
            # own docstring - the "122-124 Regent Street" -> "Regent
            # Street" shape) is kept-as-is: the OLD value is never applied
            # and never blocks a review decision, but still surfaced as a
            # non-blocking FYI (see pages/2_Review_and_Master.py's own
            # kept_as_is_fields wiring) - a THIRD bucket alongside risky_
            # fields ("needs a decision") and the ordinary bundled-safe-
            # changes summary ("will apply automatically"), never a fully
            # silent no-op, since there's a real if small chance the new
            # value is actually a genuine renumbering.
            #
            # address_conflict (see that clause's own comment below) is a
            # stronger, already-CONFIRMED-genuine disagreement - explicitly
            # excluded here so address_1 can never land in kept_as_is_
            # fields merely because it ALSO happens to match this shape;
            # the address_conflict clause below still independently adds
            # it to risky_fields regardless of this exclusion.
            kept_as_is_fields = frozenset(
                f for f in diffs
                if f in HOUSE_NUMBER_FIELDS and _house_number_silently_dropped(*diffs[f])
                and not (f == "address_1" and new_dict.get("address_conflict"))
            )
            risky_fields = frozenset(
                f for f in diffs
                if f in DETAIL_LOSS_MERGE_FIELDS and (
                    is_detail_loss(*diffs[f], field_name=f)
                    or is_richness_regression(*diffs[f])
                    or _has_suspicious_duplicate_items(diffs[f][1], f)
                )
            ) | frozenset(
                f for f in diffs
                if f in HOUSE_NUMBER_FIELDS and house_number_changed(*diffs[f])
                and f not in kept_as_is_fields
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
            ) | frozenset(
                # This run's own geocode result had ZERO source evidence to
                # check itself against at all (see GEOCODE_UNVERIFIED_
                # FIELDS' own docstring) - flagged unconditionally, with
                # none of the leniency HOUSE_NUMBER_FIELDS/GEOCODE_RISK_
                # FIELDS apply above (a structural-only address check, a
                # distance+corroboration check for lat/lng): there is
                # nothing at all backing this result, so any genuine diff
                # on it always needs a human's own confirmation, regardless
                # of how small/plausible the change looks.
                f for f in diffs
                if f in GEOCODE_UNVERIFIED_FIELDS and new_dict.get("geocode_unverified")
            ) | frozenset(
                # address_conflict (see schema.ListingRow's own docstring
                # and the address_1-injection comment above) - flagged
                # unconditionally whenever present, same "positive
                # evidence of a real problem, always needs a human's own
                # look" philosophy as the GEOCODE_UNVERIFIED_FIELDS clause
                # just above, never the more lenient HOUSE_NUMBER_FIELDS
                # treatment (a structural change is tolerated there when
                # the number itself still agrees - this is a DIFFERENT,
                # already-confirmed-genuine disagreement, not a plain
                # structural check).
                f for f in diffs
                if f == "address_1" and new_dict.get("address_conflict")
            )
            let_status_fields = frozenset(
                f for f in diffs if f in LET_STATUS_FIELDS and mentions_let_status(diffs[f][1])
            )
            matched = MatchedRow(
                master_idx, old_rec["property_id"], new_row, diffs, tier, silent, risky_fields, let_status_fields,
                kept_as_is_fields,
            )
            (matched_changed if diffs else matched_unchanged).append(matched)
        else:
            unmatched.append(
                UnmatchedRow(new_row, _suggest_similar(new_dict, master_records), _new_row_let_status_fields(new_row))
            )

    # Two incoming rows can independently match the SAME master row via
    # building/provider/floor identity alone (the matching tiers above have
    # no unit-level evidence to go further once floor_unit itself is blank
    # on both sides) while their own size/desks/rent prove they're actually
    # different listings - see _resolve_listing_evidence_conflicts' own
    # docstring for the real confirmed "1 Oliver's Yard" case. Splitting
    # this BEFORE collisions are computed means such a pair is never even
    # offered as a "pick a value per field" collision at all - the closer-
    # matching one keeps updating master, the other becomes a fresh,
    # independent new property. A no-op for every ordinary collision (two
    # sources genuinely re-extracting the same listing).
    matched_changed, unmatched = _resolve_listing_evidence_conflicts(matched_changed, unmatched, master_records)

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


# --- Listing-level identity: SAME BUILDING is not SAME LISTING -------------
#
# building+provider+floor_unit (see _fallback_key/_dedup_key) is necessary
# but not sufficient evidence that two rows describe the same LISTING once
# floor_unit itself carries no real discriminating information (blank on
# both sides - a provider's own sheet may simply have no floor/unit concept
# at all). Real confirmed case: "1 Oliver's Yard" (The Workplace Company) -
# two rows sharing (building, provider, blank floor_unit) but genuinely
# separate suites: 5,515-7,282 sqft / 52-68 desks / £34,468-45,512 rent vs
# 18,118-42,892 sqft / ~200-400 desks / £168,799-230,811 rent. Forcing
# these into one identity (a same-batch duplicate group, or a master-row
# collision) produced a field-by-field "7282 or 42892?" prompt for
# something that was never actually ambiguous - the numbers themselves
# already prove these are two different listings.
#
# Conservative by construction: no SINGLE field difference is ever treated
# as evidence on its own - a provider legitimately revises rent/size over
# time, and one field alone can't distinguish that from a genuinely
# different listing. Only agreement across several INDEPENDENT numeric
# signals, each far beyond ordinary revision noise, counts.
_LISTING_DIFFERENCE_FIELDS = ("size_sqft", "desks_min", "desks_max", "rent_pcm", "rent_psf")

# How much bigger the larger value must be than the smaller before a single
# field counts as a "signal" at all - validated against the real Oliver's
# Yard ratios (size ~5.8x, desks ~3.8x, rent ~4.9x, all comfortably clear)
# while staying well above an ordinary revision (e.g. a rent increase from
# £10,000 to £11,000 is only 1.1x - nowhere near this).
_MATERIAL_DIFFERENCE_RATIO = 1.4

# How many of _LISTING_DIFFERENCE_FIELDS must each independently clear
# _MATERIAL_DIFFERENCE_RATIO before two rows are treated as confidently
# DIFFERENT listings - deliberately more than one, so a single outlier
# field (a genuine typo, or a provider correcting just one number) can
# never trigger this alone; Oliver's Yard clears all 4 available numeric
# fields (size, desks_min substitute via desks_max, rent_pcm), comfortably
# clearing this bar with real margin to spare.
_MIN_INDEPENDENT_SIGNALS_FOR_SEPARATE_LISTINGS = 3


def _materially_different(a, b) -> bool:
    """
    True only when both a and b are genuine positive numbers and the
    larger is at least _MATERIAL_DIFFERENCE_RATIO times the smaller. Never
    true when either side is blank/non-numeric - nothing to compare there,
    never evidence of anything (same "blank is not evidence" principle
    _postcode_hint_conflicts-style checks use throughout this codebase).
    """
    if _is_blank(a) or _is_blank(b):
        return False
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if a <= 0 or b <= 0:
        return False
    hi, lo = (a, b) if a >= b else (b, a)
    return hi / lo >= _MATERIAL_DIFFERENCE_RATIO


def _listing_evidence_signal_count(a: dict, b: dict) -> int:
    """How many of _LISTING_DIFFERENCE_FIELDS materially differ (see
    _materially_different) between `a` and `b` - the raw count
    _listing_evidence_conflicts itself thresholds; exposed separately so a
    caller comparing against several candidates at once (see
    _partition_by_listing_evidence/_best_listing_evidence_match) can rank
    them by closeness of fit, not just a yes/no per candidate."""
    return sum(1 for f in _LISTING_DIFFERENCE_FIELDS if _materially_different(a.get(f), b.get(f)))


def _listing_evidence_conflicts(a: dict, b: dict) -> bool:
    """
    True when `a` and `b` (two ListingRow dicts that already agree on
    building/provider/floor identity) show strong, independent, multi-
    signal evidence of being genuinely DIFFERENT listings rather than one
    listing whose values have simply drifted between sources - see the
    module-level comment above this function's own docstring for the real
    Oliver's Yard case and why a single field is never enough alone.
    """
    return _listing_evidence_signal_count(a, b) >= _MIN_INDEPENDENT_SIGNALS_FOR_SEPARATE_LISTINGS


def _listing_evidence_richness(d: dict) -> int:
    """How many of _LISTING_DIFFERENCE_FIELDS are non-blank on `d` - used
    only to decide processing ORDER for clustering (see _partition_by_
    listing_evidence), never as evidence of anything by itself. A richer
    row (more of these fields actually stated) makes a more reliable
    cluster anchor than a sparser one - see that function's own docstring
    for the real, confirmed gap this closes: a pre-enrichment "desks only"
    row and a fully-enriched "desks+size+rent" row of the SAME listing
    both existing at once (e.g. an old, not-yet-superseded pending upload
    alongside a freshly re-uploaded one)."""
    return sum(1 for f in _LISTING_DIFFERENCE_FIELDS if not _is_blank(d.get(f)))


def richest_listing_index(dicts: list) -> int:
    """
    Index of `dicts`' own member with the most non-blank _LISTING_
    DIFFERENCE_FIELDS (see _listing_evidence_richness) - the same richness
    tie-break _partition_by_listing_evidence already trusts to pick a more
    reliable cluster anchor over a sparser row, reused here (rather than a
    second, separately-invented tie-break) to pick the base row for pages/
    2_Review_and_Master.py's own _render_intra_batch_duplicate_group
    "Same listing — merge" choice: every field the reviewer isn't
    explicitly overriding in a text box comes from THIS row, unchanged.

    Ties (equal richness) resolve to the EARLIEST index - max() only ever
    replaces its current pick on a strictly greater score, never an equal
    one - so this is stable and deterministic rather than arbitrary when
    two candidates are equally rich.
    """
    return max(range(len(dicts)), key=lambda i: _listing_evidence_richness(dicts[i]))


def _best_listing_evidence_match(new_dict: dict, candidates: list) -> int:
    """
    Index into `candidates` (a list of dicts) of the single candidate
    whose own listing evidence most closely matches new_dict, or None when
    there isn't a clear single best match - either every candidate already
    conflicts outright (see _listing_evidence_conflicts), or two or more
    tie for the fewest conflicting signals. Both "no match" cases are
    treated identically to genuine ambiguity - never guessed (see this
    module's own "incorrect enrichment is worse than a blank field"
    principle, applied here to picking among several same-identity-key
    candidates rather than to a single pairwise comparison).
    """
    scored = sorted(
        ((i, _listing_evidence_signal_count(new_dict, c)) for i, c in enumerate(candidates)),
        key=lambda pair: pair[1],
    )
    in_range = [pair for pair in scored if pair[1] < _MIN_INDEPENDENT_SIGNALS_FOR_SEPARATE_LISTINGS]
    if not in_range:
        return None
    if len(in_range) > 1 and in_range[0][1] == in_range[1][1]:
        return None
    return in_range[0][0]


def _partition_by_listing_evidence(indices: list, unmatched: list) -> list:
    """
    Splits `indices` (candidates already sharing a building/provider/floor
    identity key, and already partitioned by source submarket - see
    _partition_by_source_submarket) into sub-groups such that two rows with
    strong, multi-signal evidence of being genuinely DIFFERENT listings
    (see _listing_evidence_conflicts) are never placed in the same sub-
    group. Conservative by construction (see _listing_evidence_conflicts'
    own multi-signal requirement) - an ordinary same-listing update (a
    rent increase, a size correction) never triggers a split, so this is a
    pure no-op for every case that isn't a genuine, strongly-evidenced
    separate listing, exactly like _partition_by_source_submarket already
    is for a shared/blank submarket.

    Processes `indices` in DESCENDING evidence-richness order (see
    _listing_evidence_richness), not their original/upload order, and
    joins each row to the BEST-fitting existing cluster (fewest
    conflicting signals - see _best_listing_evidence_match), not just the
    first non-conflicting one. Both changes together fix a real, confirmed
    gap: a re-upload leaving an old, sparse "desks only" pending copy of
    Oliver's Yard sitting alongside a freshly re-uploaded, fully-enriched
    copy (four rows total: two old, two new, all sharing one identity key)
    used to collapse into a single messy 4-way group - comparing a sparse
    row only against whichever OTHER sparse row happened to be seen first
    (itself lacking the size/rent evidence needed to tell the two real
    listings apart) let a completely unrelated pair of rows bridge
    together transitively. Processing the two fully-evidenced rows FIRST
    lets them form their own correct, clearly-conflicting clusters right
    away; each sparse row is then compared against BOTH of those already-
    formed clusters and correctly joins whichever one it has genuinely
    fewer (here, zero) conflicting signals with, rather than whichever
    cluster simply happened to exist first.
    """
    ordered = sorted(
        indices, key=lambda i: _listing_evidence_richness(unmatched[i].new_row.model_dump()), reverse=True,
    )
    clusters = []  # each: [representative_dict, [indices...]]
    for i in ordered:
        row_dict = unmatched[i].new_row.model_dump()
        if clusters:
            best = _best_listing_evidence_match(row_dict, [rep for rep, _ in clusters])
        else:
            best = None
        if best is not None:
            clusters[best][1].append(i)
        else:
            clusters.append([row_dict, [i]])
    return [members for _, members in clusters]


def _resolve_listing_evidence_conflicts(matched_changed: list, unmatched: list, master_records: list) -> tuple:
    """
    (matched_changed, unmatched) - splits any matched_changed group that
    shares a master_index but shows strong, multi-signal listing-evidence
    conflict between its own members (see _listing_evidence_conflicts):
    two genuinely different listings must never be forced to update the
    SAME existing master property just because they both independently
    matched it via building/provider/floor identity (the matching tiers in
    build_merge_plan have no unit-level evidence to disambiguate a blank-
    floor building any further than that). The member with the FEWEST
    diffs from master (the closest existing match) keeps updating that
    master row exactly as before; every other conflicting member is
    demoted to a fresh, independent unmatched row instead (master_index=
    None) - it becomes an entirely new property on approval (going through
    the same unmatched/near-miss/same-batch-duplicate pipeline as any
    other new row, via _suggest_similar below), never silently forced into
    the wrong existing property, and never a field-by-field "pick one"
    collision prompt either, since the two are already confidently known
    to be different, not merely disagreeing.

    A group with no evidence conflict (the overwhelming common case - an
    ordinary shared-identity collision, e.g. two sheets both re-extracting
    the same real listing) is returned completely untouched, byte-for-byte
    identical to the input - this function is a pure no-op for it.
    """
    by_master_idx = {}
    for m in matched_changed:
        by_master_idx.setdefault(m.master_index, []).append(m)

    kept, demoted = [], []
    for master_idx, members in by_master_idx.items():
        if len(members) < 2:
            kept.extend(members)
            continue
        dicts = [m.new_row.model_dump() for m in members]
        conflict = any(
            _listing_evidence_conflicts(dicts[i], dicts[j])
            for i in range(len(dicts)) for j in range(i + 1, len(dicts))
        )
        if not conflict:
            kept.extend(members)
            continue
        members_by_closeness = sorted(members, key=lambda m: len(m.diffs))
        kept.append(members_by_closeness[0])
        demoted.extend(members_by_closeness[1:])

    kept_ids = {id(m) for m in kept}  # identity, not dataclass equality - avoids comparing diffs dicts wholesale
    new_matched_changed = [m for m in matched_changed if id(m) in kept_ids]
    new_unmatched = list(unmatched) + [
        UnmatchedRow(
            m.new_row, _suggest_similar(m.new_row.model_dump(), master_records),
            _new_row_let_status_fields(m.new_row),
        )
        for m in demoted
    ]
    return new_matched_changed, new_unmatched


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

    BOTH passes are further split by _partition_by_source_submarket, THEN
    _partition_by_listing_evidence, BEFORE either one ever unions anything:
    a genuinely different, non-blank submarket/area between two otherwise-
    identical candidates means the ORIGINAL provider workbook intentionally
    represented them as separate listings (see that function's own
    docstring); strong, multi-signal evidence that two rows' own size/
    desks/rent are dramatically different (see _partition_by_listing_
    evidence/_listing_evidence_conflicts - the real confirmed "1 Oliver's
    Yard" case) means the same thing even when submarket itself doesn't
    distinguish them (a provider's own sheet may have no submarket/area
    column at all). Neither must ever be collapsed into a merge OR a
    manual duplicate prompt merely because building/provider/floor/
    postcode/brochure_link happen to agree - source-listing identity and
    physical-property identity are two different questions, and this
    function only ever answers the first. A shared brochure_link is real
    evidence the same PHYSICAL property is involved, but it is not
    evidence the provider intended one listing row, so it never overrides
    a genuine submarket or listing-evidence split, unlike the narrower
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
            for sub in _partition_by_listing_evidence(cluster, unmatched):
                for i in sub[1:]:
                    union(sub[0], i)

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
            for sub in _partition_by_listing_evidence(cluster, unmatched):
                if len(sub) < 2:
                    continue
                for i in sub[1:]:
                    union(sub[0], i)

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
