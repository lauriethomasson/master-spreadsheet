"""
Regression tests for pages/2_Review_and_Master.py's _word_diff_highlight -
the word-level (difflib.SequenceMatcher, split on whitespace) diff helper
_render_field_rows uses to highlight what actually changed in a string
field's before/after display (see fb1c9f6).

A pure function, no Streamlit rendering involved - loaded directly via
importlib (the file's own numeric-prefixed name, "2_Review_and_Master.py",
isn't a valid plain `import` target), same idiom already used by
tests/test_canva_renderer.py for canva_renderer/app.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_word_diff_highlight -v
"""

import importlib.util
import sys
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "review_and_master_page", Path(__file__).resolve().parent.parent / "pages" / "2_Review_and_Master.py",
)
review_and_master_page = importlib.util.module_from_spec(_spec)
sys.modules["review_and_master_page"] = review_and_master_page
_spec.loader.exec_module(review_and_master_page)

_word_diff_highlight = review_and_master_page._word_diff_highlight
_has_shared_words = review_and_master_page._has_shared_words
_char_diff_highlight = review_and_master_page._char_diff_highlight
_looks_identical_but_differs = review_and_master_page._looks_identical_but_differs


class WordDiffHighlightTests(unittest.TestCase):
    def test_replaced_words_are_marked_removed_in_before_and_added_in_after(self):
        old_val = "MR + PB; Available: November"
        new_val = "+ MR; Available: September"

        before, after = _word_diff_highlight(old_val, new_val)

        # "MR" survives (present in both, just reordered relative to "+"),
        # "PB;" and "November" are only in old_val - removed.
        self.assertIn(":red[PB;]", before)
        self.assertIn(":red[November]", before)
        # "September" is only in new_val - added.
        self.assertIn(":green[September]", after)
        self.assertNotIn(":red[", after)
        self.assertNotIn(":green[", before)

    def test_no_overlap_at_all_marks_every_word_on_both_sides(self):
        old_val = "Cat A shell and core"
        new_val = "Fully fitted turnkey space"

        before, after = _word_diff_highlight(old_val, new_val)

        # Zero shared words - the whole old_val is one contiguous removed
        # span, the whole new_val one contiguous added span (a run of
        # changed words gets ONE highlighted span, never one per word).
        self.assertEqual(before, "**:red[Cat A shell and core]**")
        self.assertEqual(after, "**:green[Fully fitted turnkey space]**")

    def test_identical_values_produce_no_highlighting_at_all(self):
        value = "Fully fitted; 24 desks; 1 boardroom"

        before, after = _word_diff_highlight(value, value)

        self.assertEqual(before, value)
        self.assertEqual(after, value)
        self.assertNotIn(":red[", before)
        self.assertNotIn(":green[", after)


class HasSharedWordsTests(unittest.TestCase):
    """
    _has_shared_words is what _render_field_rows checks before rendering
    _word_diff_highlight's own captions at all - see that function's own
    docstring on why a complete replacement (zero word overlap) must skip
    them entirely rather than showing the whole new value wrapped in green,
    which would just duplicate the editable input right above it.
    """

    def test_a_partial_edit_with_real_overlap_is_shared(self):
        # Same shape as WordDiffHighlightTests' own replaced-words case -
        # "MR" survives across both values, a genuine partial edit.
        self.assertTrue(_has_shared_words("MR + PB; Available: November", "+ MR; Available: September"))

    def test_a_complete_replacement_with_zero_overlap_is_not_shared(self):
        # Real confirmed case: an address field replaced wholesale, not
        # edited - "Great Portland Street" and "30 Barkston Gardens" share
        # not one single word.
        self.assertFalse(_has_shared_words("Great Portland Street", "30 Barkston Gardens"))

    def test_identical_values_are_shared(self):
        value = "Fully fitted; 24 desks; 1 boardroom"
        self.assertTrue(_has_shared_words(value, value))


class LooksIdenticalButDiffersTests(unittest.TestCase):
    """
    Regression coverage for a real reported confusion: a geocode-
    unverified decision card showing Address Current='1 Broadgate' / New=
    '1 Broadgate' with no visible difference at all - master_merge.
    diff_fields correctly found a real difference (that's the only reason
    this field is in a diffs dict at all - see master_merge._values_equal's
    own docstring on why punctuation/hidden characters are deliberately
    never normalized away), but nothing in the UI told the reviewer WHERE.
    """

    def test_hidden_character_within_a_word_looks_identical(self):
        # A zero-width space inside "Broadgate" - invisible in normal
        # rendering, but a genuinely different string _values_equal
        # correctly refuses to treat as equal.
        self.assertTrue(_looks_identical_but_differs("1 Broadgate", "1 Broad​gate"))

    def test_curly_vs_straight_apostrophe_looks_identical(self):
        self.assertTrue(_looks_identical_but_differs("16 Dufour's Place", "16 Dufour’s Place"))

    def test_en_dash_vs_hyphen_looks_identical(self):
        self.assertTrue(_looks_identical_but_differs("24-30 Great Titchfield Street", "24–30 Great Titchfield Street"))

    def test_genuinely_different_address_is_never_flagged(self):
        # Real, visible difference (different house numbers) - digits
        # survive _alphanumeric_only, so this must never be treated as
        # "looks identical".
        self.assertFalse(_looks_identical_but_differs("138 Cheapside", "134-136 Cheapside"))

    def test_exactly_equal_values_are_never_flagged(self):
        # Byte-identical values are never even in a diffs dict in the
        # first place (see master_merge.diff_fields), but this must still
        # be a safe no-op if ever called with them.
        self.assertFalse(_looks_identical_but_differs("1 Broadgate", "1 Broadgate"))

    def test_blank_values_are_never_flagged(self):
        self.assertFalse(_looks_identical_but_differs("", "1 Broadgate"))
        self.assertFalse(_looks_identical_but_differs("1 Broadgate", None))

    def test_non_string_values_are_never_flagged(self):
        self.assertFalse(_looks_identical_but_differs(14600, 14600.0))


class CharDiffHighlightTests(unittest.TestCase):
    def test_a_single_differing_character_is_localized(self):
        before, after = _char_diff_highlight("1 Broadgate", "1 Broad​gate")

        self.assertIn("1 Broad", before)
        self.assertIn("gate", before)
        self.assertNotIn(":red[", before)  # nothing REMOVED - only an insertion
        self.assertIn("​", after)
        self.assertIn(":green[", after)

    def test_identical_values_produce_no_highlighting_at_all(self):
        value = "1 Broadgate"

        before, after = _char_diff_highlight(value, value)

        self.assertEqual(before, value)
        self.assertEqual(after, value)


if __name__ == "__main__":
    unittest.main()
