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

from house_number import leading_house_number


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


if __name__ == "__main__":
    unittest.main()
