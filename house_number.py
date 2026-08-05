"""
house_number.py

The leading house-number token a UK street address/building string starts
with (e.g. "89", "27-30", "56a") - one piece of address parsing that both
master_merge.py (guarding intra-batch building-name grouping in
unmatched_collisions, and flagging a structural address change during merge
review via house_number_changed) and extract_spreadsheet_gemini.py (verifying
Gemini's address_1 extraction against the raw sheet text it came from) need
identically. Factored out here so there's one authoritative regex/parse
instead of two independent copies that could quietly drift apart.
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


def leading_house_number(text):
    """
    The leading house-number token `text` starts with (e.g. "89", "27-30"),
    lowercased, or None if it's blank or doesn't start with one at all.

    Used to guard against merging/matching two genuinely DIFFERENT numbered
    units on the same street ("27 Cannon Street" and "108 Cannon Street" are
    a real, confirmed-different pair - see master_merge.py's
    BUILDING_FUZZY_MATCH_THRESHOLD comment), and to compare a stated house
    number against another source's own transcription of the same address.
    """
    if text is None:
        return None
    stripped = str(text).strip()
    if not stripped:
        return None
    match = LEADING_HOUSE_NUMBER_RE.match(stripped)
    return match.group(1).lower() if match else None
