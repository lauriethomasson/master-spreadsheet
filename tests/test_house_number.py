"""
Regression tests for house_number.py's leading_house_number - the single
authoritative leading-house-number parse master_merge.py and extract_
spreadsheet_gemini.py both build on. Covers the digit form ("89", "27-30",
"56a") and the spelled-out-cardinal fallback ("Nineteen" -> "19") added this
session, including the "Nine Elms" known-tradeoff case documented in this
module's own top-of-file docstring.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_house_number -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from house_number import house_numbers_conflict, leading_house_number


class LeadingHouseNumberDigitFormTests(unittest.TestCase):
    def test_plain_number(self):
        self.assertEqual(leading_house_number("89 Charterhouse St"), "89")

    def test_hyphenated_range(self):
        self.assertEqual(leading_house_number("27-30 Lime Street"), "27-30")

    def test_letter_suffix(self):
        self.assertEqual(leading_house_number("56a Example Street"), "56a")

    def test_no_leading_number(self):
        self.assertIsNone(leading_house_number("Copthall House"))

    def test_blank_and_none(self):
        self.assertIsNone(leading_house_number(None))
        self.assertIsNone(leading_house_number(""))
        self.assertIsNone(leading_house_number("   "))

    def test_digit_form_always_wins_over_spelled_out_fallback(self):
        # A digit at the start is never reinterpreted, even in principle -
        # confirms the fallback is only ever consulted when the digit regex
        # finds nothing at all.
        self.assertEqual(leading_house_number("10 Downing Street"), "10")

    def test_word_to_range_normalizes_to_hyphen_form(self):
        # Confirmed real case: "1 to 5 Adam Street" (Ivybridge House) vs
        # master's own "1-5 Adam Street" - the same range, written two
        # common ways - must produce the IDENTICAL token so house_number_
        # changed treats them as equal, not a risky address change.
        self.assertEqual(leading_house_number("1 to 5 Adam Street"), "1-5")
        self.assertEqual(leading_house_number("1 to 5 Adam Street"), leading_house_number("1-5 Adam Street"))

    def test_word_to_range_is_case_insensitive_and_whitespace_tolerant(self):
        self.assertEqual(leading_house_number("1 To 5 Adam Street"), "1-5")
        self.assertEqual(leading_house_number("1  to  5 Adam Street"), "1-5")

    def test_spaced_hyphen_range_normalizes_to_the_same_token_as_the_bare_hyphen_form(self):
        # Confirmed real case: a real Workplace Plus "13-15 Dock Street" row
        # vs that SAME real brochure's own Gemini-extracted building text
        # "13 - 15 Dock Street" (spaced-out hyphen) - the bare-hyphen form
        # previously only tolerated "13-15" with no surrounding whitespace,
        # so "13 - 15" read as house number "13" alone with an un-strippable
        # " - 15 Dock Street" remainder, never bridging to the row's own
        # "13-15 Dock Street".
        self.assertEqual(leading_house_number("13 - 15 Dock Street"), "13-15")
        self.assertEqual(leading_house_number("13 - 15 Dock Street"), leading_house_number("13-15 Dock Street"))
        self.assertEqual(leading_house_number("13  -  15 Dock Street"), "13-15")

    def test_spaced_hyphen_does_not_swallow_an_unrelated_trailing_word(self):
        # "56a - West Wing" is NOT a house-number range - "West Wing" isn't
        # a number, so the optional high-number group must never match, and
        # this must still resolve to the single low number alone, exactly
        # as before the spaced-hyphen tolerance existed.
        self.assertEqual(leading_house_number("56a - West Wing"), "56a")

    def test_word_to_range_with_letter_suffixes(self):
        self.assertEqual(leading_house_number("27a to 30b Lime Street"), "27a-30b")


class SpelledOutNumberTests(unittest.TestCase):
    """The new fallback: a leading spelled-out cardinal number (one through
    nineteen, the tens, and compound tens-plus-ones forms) is recognized and
    normalized to the same canonical digit string the digit form itself
    would produce - so "Nineteen Wells St" compares equal to "19 Wells
    Street" everywhere leading_house_number is used."""

    def test_wells_street_real_case(self):
        # The confirmed real case this fallback exists for: master's "19
        # Wells Street" vs an upload's own "Nineteen Wells St" - same real
        # building (its name already matches after the street-suffix fix),
        # flagged as a risky address change purely because one side had no
        # digit to find at all.
        self.assertEqual(leading_house_number("Nineteen Wells St"), leading_house_number("19 Wells Street"))
        self.assertEqual(leading_house_number("Nineteen Wells St"), "19")

    def test_compound_tens_plus_ones_space_separated(self):
        self.assertEqual(leading_house_number("Twenty One Old Street"), leading_house_number("21 Old Street"))
        self.assertEqual(leading_house_number("Twenty One Old Street"), "21")

    def test_compound_tens_plus_ones_hyphen_separated(self):
        self.assertEqual(leading_house_number("Twenty-One Old Street"), "21")

    def test_bare_tens_word(self):
        self.assertEqual(leading_house_number("Thirty Example Street"), "30")

    def test_every_one_through_nineteen(self):
        words = [
            "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
            "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
            "eighteen", "nineteen",
        ]
        for i, word in enumerate(words, start=1):
            with self.subTest(word=word):
                self.assertEqual(leading_house_number(f"{word.title()} Example Street"), str(i))

    def test_case_insensitive(self):
        self.assertEqual(leading_house_number("NINETEEN Wells St"), "19")
        self.assertEqual(leading_house_number("nineteen wells st"), "19")

    def test_a_genuinely_different_spelled_out_number_is_not_equal(self):
        # Not a merge/equality bug - "Twenty" and "19" really are different
        # numbers, and must still compare unequal (house_number_changed
        # relies on exactly this - see HouseNumberChangedTests in
        # test_master_merge.py).
        self.assertNotEqual(leading_house_number("Twenty Wells Street"), leading_house_number("19 Wells Street"))

    def test_number_word_as_part_of_a_longer_word_is_not_matched(self):
        # \b after the matched word means "nineteenth" is never mistaken
        # for "nineteen" - same word-boundary-safe philosophy as the digit
        # form's own \b guard.
        self.assertIsNone(leading_house_number("Nineteenth Floor"))

    def test_nine_elms_known_tradeoff(self):
        # KNOWN, DELIBERATELY ACCEPTED tradeoff - see house_number.py's own
        # module docstring for the full reasoning. "Nine Elms" is a real
        # London place/street name (e.g. "Nine Elms Lane" has no house
        # number at all - "Nine" is just part of the street's own name),
        # but is surface-pattern-identical to a genuine spelled-out house
        # number ("<number word> <capitalized word(s)>"), and this module
        # deliberately does not attempt to tell the two apart. Locked in
        # here as documented, expected behavior - not an oversight - so a
        # future change to this tradeoff is a deliberate, visible decision.
        self.assertEqual(leading_house_number("Nine Elms"), "9")
        self.assertEqual(leading_house_number("Nine Elms Lane"), "9")


class HouseNumbersConflictTests(unittest.TestCase):
    """
    Regression coverage for house_numbers_conflict - the leading_house_
    number()-shaped range-overlap check geocode.py's own HOUSE_NUMBER_
    CONFLICT check builds on. Confirmed real failure this exists for: a
    real "138 Cheapside" resolved via Places Text Search to a genuinely
    different, adjacent "134-136 Cheapside" - same street (so the existing
    STREET_CONFLICT check alone never caught it), but a numerically
    disjoint building.
    """

    def test_identical_plain_numbers_do_not_conflict(self):
        self.assertFalse(house_numbers_conflict("138", "138"))

    def test_disjoint_plain_number_and_range_conflict(self):
        # The real "138 Cheapside" vs "134-136 Cheapside" failure.
        self.assertTrue(house_numbers_conflict("138", "134-136"))

    def test_disjoint_plain_numbers_conflict(self):
        # The real "44 Paul Street" vs "20 Little Britain" shape - same
        # numeric disjointness, even though that real case is already
        # caught earlier by STREET_CONFLICT (different street entirely).
        self.assertTrue(house_numbers_conflict("44", "20"))

    def test_identical_ranges_do_not_conflict(self):
        self.assertFalse(house_numbers_conflict("14-18", "14-18"))

    def test_a_number_inside_a_wider_range_does_not_conflict(self):
        # A single postal address quoted as one number from within a wider
        # range a different source states for the same building is a real,
        # legitimate UK convention - never a conflict.
        self.assertFalse(house_numbers_conflict("27-30", "28"))
        self.assertFalse(house_numbers_conflict("28", "27-30"))

    def test_overlapping_but_not_identical_ranges_do_not_conflict(self):
        self.assertFalse(house_numbers_conflict("14-18", "16-20"))

    def test_disjoint_ranges_conflict(self):
        self.assertTrue(house_numbers_conflict("14-18", "20-24"))

    def test_letter_suffix_is_ignored(self):
        # "56a" marks a sub-unit of the SAME numbered building, not a
        # different one.
        self.assertFalse(house_numbers_conflict("138", "138a"))
        self.assertFalse(house_numbers_conflict("56a", "56b"))

    def test_either_side_missing_is_never_a_conflict(self):
        self.assertFalse(house_numbers_conflict("138", None))
        self.assertFalse(house_numbers_conflict(None, "138"))
        self.assertFalse(house_numbers_conflict(None, None))

    def test_either_side_unparseable_is_never_a_conflict(self):
        self.assertFalse(house_numbers_conflict("138", "not-a-number"))
        self.assertFalse(house_numbers_conflict("not-a-number", "138"))


if __name__ == "__main__":
    unittest.main()
