"""
house_number.py

The leading house-number token a UK street address/building string starts
with (e.g. "89", "27-30", "56a", or a spelled-out cardinal like "Nineteen"
-> "19" - see _spelled_out_leading_number) - one piece of address parsing
that both master_merge.py (guarding intra-batch building-name grouping in
unmatched_collisions, and flagging a structural address change during merge
review via house_number_changed) and extract_spreadsheet_gemini.py (verifying
Gemini's address_1 extraction against the raw sheet text it came from) need
identically. Factored out here so there's one authoritative regex/parse
instead of two independent copies that could quietly drift apart.

KNOWN, ACCEPTED tradeoff on the spelled-out-number recognition: a genuine
PLACE name that happens to start with a spelled-out number - "Nine Elms" (a
real London district/street name; "9 Elms" is not a thing) or "Seven
Dials"/"Seven Sisters"/"Seven Kings" - is indistinguishable from a real
spelled-out house number at the surface-pattern level this module works at.
No small, deterministic vocabulary can tell "Nineteen Wells Street" (a house
number) apart from "Nine Elms Lane" (no house number at all, "Nine" is just
part of the street's own name) - both are "<number word> <capitalized
word(s)>". Building a reliable disambiguator would mean either a name
gazetteer or genuine semantic parsing, neither of which fits this module's
own "small, explicit, deterministic" philosophy, so this is accepted as a
deliberate, documented risk rather than guarded against - see test_house_
number.py's own SpelledOutNumberTests for this exact case exercised and
locked in as known behavior. The real exposure is concentrated in extract_
spreadsheet_gemini.py's own raw-sheet-text scanning (_raw_house_number_for_
unit) - it can splice a wrong-but-plausible digit into address_1 when a raw
building/street line starts with a spelled-out-number place name that has no
real house number stated at all - more than in master_merge.py's own address_
1-vs-address_1 comparisons, where a leading spelled-out number is, in
practice, essentially always a real house number (address_1 is already a
curated street address by that point, not freeform building/place text).
"""

import re

# A leading house number, e.g. "89", "27-30", "56a". Matched against the RAW
# address/building string, not any punctuation-stripped normalized form -
# stripping "-" as punctuation first would mangle a real range like "27-30"
# into "2730" before this pattern ever saw the hyphen (confirmed against a
# real Copthall Estates address, "27-30 Lime Street"). \b after the optional
# letter/range so "27" doesn't partially match inside an unrelated word
# starting with a digit (there are none in real building/address strings seen
# so far, but this costs nothing to guard against). Case-insensitive since it
# runs before any lowercasing.
LEADING_HOUSE_NUMBER_RE = re.compile(r"^\s*(\d+[a-z]?(?:-\d+[a-z]?)?)\b", re.IGNORECASE)

# Spelled-out cardinal numbers, one through nineteen - confirmed real case:
# master's "19 Wells Street" vs an upload's own "Nineteen Wells St" (same
# real building, its own name already agreeing after the street-suffix fix -
# see master_merge._STREET_SUFFIX_EXPANSIONS) was flagged as a risky address
# change purely because leading_house_number found a digit on one side and
# nothing at all on the other. Deliberately a small, explicit, hand-written
# vocabulary - never a general number-word parser - same conservative
# philosophy as master_merge.py's own _STREET_SUFFIX_EXPANSIONS/
# _COMPASS_ABBREVIATIONS lookups.
_ONES_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_TENS_WORDS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
# The ones half of a compound tens-plus-ones form ("Twenty One") - only
# one through nine make sense there ("Twenty Ten" isn't a real number
# phrase), so this is a strict subset of _ONES_WORDS above.
_COMPOUND_ONES_WORDS = {word: value for word, value in _ONES_WORDS.items() if value < 10}

# Compound form first ("twenty one"/"twenty-one" - a space or hyphen between
# the two words), so "twenty" alone in _SPELLED_OUT_SINGLE_RE never wins
# against the fuller "twenty one" it's a prefix of. \b after each word, same
# word-boundary-safe philosophy as LEADING_HOUSE_NUMBER_RE above - matching
# "nine" as a candidate inside "nineteen" fails its own \b (the next
# character is "t", not a boundary), so the regex engine falls through to
# the full "nineteen" alternative instead; alternation order therefore never
# matters here.
_SPELLED_OUT_COMPOUND_RE = re.compile(
    rf"^\s*({'|'.join(_TENS_WORDS)})[\s-]({'|'.join(_COMPOUND_ONES_WORDS)})\b", re.IGNORECASE,
)
_SPELLED_OUT_SINGLE_RE = re.compile(
    rf"^\s*({'|'.join({**_ONES_WORDS, **_TENS_WORDS})})\b", re.IGNORECASE,
)


def _spelled_out_leading_number(stripped: str):
    """The canonical digit string ("19", "21") a leading spelled-out
    cardinal number in `stripped` represents, or None if it doesn't start
    with one - see the module-level word tables above for exactly which
    words are recognized. Only called once LEADING_HOUSE_NUMBER_RE has
    already failed to find a digit at all.

    KNOWN, DELIBERATELY ACCEPTED tradeoff: a genuine PLACE name that itself
    starts with a spelled-out number - "Nine Elms" (a real London district;
    "Nine Elms Lane", a real street there, has no house number at all) or
    "Seven Dials"/"Seven Sisters"/"Seven Kings" - reads identically to a
    real spelled-out house number at the surface-pattern level this
    function works at; there's no small, deterministic way to tell them
    apart (both are "<number word> <capitalized word(s)>"). See house_
    number.py's own module docstring and this session's own commit message
    for the full reasoning on why this is accepted rather than guarded
    against, and test_house_number.py's own SpelledOutNumberTests for this
    exact "Nine Elms" case exercised and locked in as expected/known
    behavior, not silently untested.
    """
    match = _SPELLED_OUT_COMPOUND_RE.match(stripped)
    if match:
        tens_value = _TENS_WORDS[match.group(1).lower()]
        ones_value = _COMPOUND_ONES_WORDS[match.group(2).lower()]
        return str(tens_value + ones_value)
    match = _SPELLED_OUT_SINGLE_RE.match(stripped)
    if match:
        word = match.group(1).lower()
        return str(_ONES_WORDS.get(word, _TENS_WORDS.get(word)))
    return None


def leading_house_number(text):
    """
    The leading house-number token `text` starts with (e.g. "89", "27-30",
    or the canonical digit form of a spelled-out number like "nineteen" ->
    "19" - see _spelled_out_leading_number), lowercased, or None if it's
    blank or doesn't start with one at all.

    Used to guard against merging/matching two genuinely DIFFERENT numbered
    units on the same street ("27 Cannon Street" and "108 Cannon Street" are
    a real, confirmed-different pair - see master_merge.py's
    BUILDING_FUZZY_MATCH_THRESHOLD comment), and to compare a stated house
    number against another source's own transcription of the same address.

    The digit form (LEADING_HOUSE_NUMBER_RE) is tried FIRST and always wins
    when present - the spelled-out fallback only ever runs when there's no
    digit at all to find, so a genuinely digit-led string is never
    reinterpreted.
    """
    if text is None:
        return None
    stripped = str(text).strip()
    if not stripped:
        return None
    match = LEADING_HOUSE_NUMBER_RE.match(stripped)
    if match:
        return match.group(1).lower()
    return _spelled_out_leading_number(stripped)


# A leading_house_number()-shaped token, parsed back out into its own
# (low, high) integer range - "89" -> (89, 89), "27-30" -> (27, 30), "56a"
# -> (56, 56). Anchored full-string (unlike LEADING_HOUSE_NUMBER_RE, which
# only anchors the START - this is only ever fed a token leading_house_
# number() itself already produced, never raw free text, so nothing should
# trail).
_HOUSE_NUMBER_TOKEN_RE = re.compile(r"^(\d+)[a-z]?(?:-(\d+)[a-z]?)?$")


def _house_number_range(token: str):
    if not token:
        return None
    match = _HOUSE_NUMBER_TOKEN_RE.match(token.strip().lower())
    if not match:
        return None
    low = int(match.group(1))
    high = int(match.group(2)) if match.group(2) else low
    return (low, high) if low <= high else (high, low)


def house_numbers_conflict(a, b) -> bool:
    """
    True only when `a` and `b` (each a leading_house_number()-shaped token -
    "89", "27-30", "56a", or None) parse to a genuinely DISJOINT numeric
    range - never true when either side is missing or fails to parse (a
    token leading_house_number() itself produced always parses; this only
    guards a caller passing something else). A trailing letter suffix
    ("56a") is dropped for this comparison - it marks a sub-unit of the SAME
    numbered building, not a different one. Two overlapping ranges (a single
    number falling inside a wider range, or two ranges sharing any number
    at all) are never a conflict - confirmed real UK convention: a single
    postal address is routinely quoted as one number from within a wider
    range a different source states for the same building, and a naive
    "ranges must match exactly" check would incorrectly reject that.

    Confirmed real failure this exists for: a real "138 Cheapside" (no
    range, no ambiguity at all) geocoded via Places Text Search to a
    genuinely different, nearby "134-136 Cheapside" - same street, adjacent
    but numerically DISJOINT (138 falls outside 134-136), previously
    accepted since the existing STREET_CONFLICT check (geocode._street_
    name_words) only ever compares the STREET NAME, never the house number
    itself - see geocode.py's own module docstring and _best_places_
    result's HOUSE_NUMBER_CONFLICT check, the one caller of this function.
    """
    range_a = _house_number_range(a)
    range_b = _house_number_range(b)
    if not range_a or not range_b:
        return False
    return range_a[1] < range_b[0] or range_b[1] < range_a[0]
