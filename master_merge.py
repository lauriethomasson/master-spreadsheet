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

# The two fields a leading house number (see _leading_house_number/
# house_number_changed) can appear in - checked independently of
# RISKY_TEXT_FIELDS in build_merge_plan's risky_fields computation, since a
# structural house-number change (master's "18 Copthall Avenue" silently
# becoming an update's "14-18 Copthall Avenue") is a different concept from
# RISKY_TEXT_FIELDS' free-text detail-loss/richness-regression checks and
# would otherwise auto-apply unreviewed unless it happened to also collide
# with another row in the same batch.
HOUSE_NUMBER_FIELDS = ("address_1", "building")

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


def canonicalize_providers(rows: list[ListingRow]) -> None:
    """Applies canonicalize_provider_name to every row's provider in place -
    the list[ListingRow]-mutating counterpart to app.py's fill_missing_
    provider/fill_missing_address_from_building, meant to run right
    alongside them: after extraction/mapping (Gemini or spreadsheet) has
    produced a provider value, before that row is ever used for matching or
    diffing."""
    for row in rows:
        row.provider = canonicalize_provider_name(row.provider)


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


def _detail_items(text: str) -> list[str]:
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
    """
    parts = re.split(r"[;\n]+", text.lower())
    return [p.strip() for p in parts if len(p.strip()) >= 3]


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
    """
    words_a, words_b = _significant_words(item_a), _significant_words(item_b)
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b)
    return overlap / min(len(words_a), len(words_b)) >= ITEM_SIMILARITY_THRESHOLD


def is_detail_loss(old_val, new_val) -> bool:
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

    Only called for RISKY_TEXT_FIELDS, and only a review trigger (see that
    constant's docstring) - never applied automatically, callers still let a
    human apply, correct, or skip the field in manual review.
    """
    if _is_blank(old_val) or _is_blank(new_val):
        return False

    old_items = _detail_items(str(old_val))
    new_items = _detail_items(str(new_val))
    if not old_items:
        return False

    return any(not any(_items_similar(old_item, new_item) for new_item in new_items) for old_item in old_items)


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


def _fallback_key(row: dict) -> tuple:
    return (
        normalize_key(row.get("building")),
        normalize_key(row.get("provider")),
        normalize_key(row.get("floor_unit")),
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
    never replaces it."""
    target_building = new_dict.get("building")
    if not _building_has_no_digits(target_building):
        return []
    target = normalize_key(target_building)
    if not target:
        return []
    keys = [normalize_key(r.get("building")) for r in master_records]
    close = set(difflib.get_close_matches(target, keys, n=3, cutoff=BUILDING_FUZZY_MATCH_THRESHOLD))
    seen = set()
    results = []
    for rec, key in zip(master_records, keys):
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
            risky_fields = frozenset(
                f for f in diffs
                if f in RISKY_TEXT_FIELDS and (is_detail_loss(*diffs[f]) or is_richness_regression(*diffs[f]))
            ) | frozenset(
                f for f in diffs
                if f in HOUSE_NUMBER_FIELDS and house_number_changed(*diffs[f])
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
       means a different real property/unit despite the shared street name,
       not a spelling variant of the same one.

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
        for i in indices[1:]:
            union(indices[0], i)

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
        buildings = [unmatched[i].new_row.building for i in indices]
        numbers = {n for n in (_leading_house_number(b) for b in buildings) if n is not None}
        if len(numbers) > 1:
            continue  # genuinely different numbered units on the same street
        postcodes = {
            _normalize_postcode(unmatched[i].new_row.postcode)
            for i in indices
            if not _is_blank(unmatched[i].new_row.postcode)
        }
        if len(postcodes) > 1:
            continue  # genuinely different postcodes - real signal against merging
        for i in indices[1:]:
            union(indices[0], i)

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


def build_manual_edit(master_records: list, edited_rows: dict) -> tuple:
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
    """
    updates = {}
    diff_rows = []
    for row_pos, cols in edited_rows.items():
        real_changes = {c: v for c, v in cols.items() if c in ListingRow.model_fields}
        if not real_changes:
            continue
        row_pos = int(row_pos)
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
