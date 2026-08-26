"""
Regression tests for pages/2_Review_and_Master.py's _risky_field_reason -
the caption a reviewer sees under a risky field on the Review page, and the
`unverified` override that gives address_1/postcode/lat/lng their own
stronger caution when this run's geocode result had zero source hint to
cross-check itself against (see master_merge.GEOCODE_UNVERIFIED_FIELDS and
geocode.py's Tier 2 zero-hint fallback - the real Henly House/Ivybridge
House cases this exists for).

A pure function, no Streamlit rendering involved - loaded directly via
importlib (the file's own numeric-prefixed name, "2_Review_and_Master.py",
isn't a valid plain `import` target), same idiom already used by
tests/test_app_review_word_diff_highlight.py for this same page.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_risky_field_reason -v
"""

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_spec = importlib.util.spec_from_file_location(
    "review_and_master_page", Path(__file__).resolve().parent.parent / "pages" / "2_Review_and_Master.py",
)
review_and_master_page = importlib.util.module_from_spec(_spec)
sys.modules["review_and_master_page"] = review_and_master_page
_spec.loader.exec_module(review_and_master_page)

_risky_field_reason = review_and_master_page._risky_field_reason


class RiskyFieldReasonUnverifiedGeocodeTests(unittest.TestCase):
    def test_unverified_address_field_gets_the_stronger_caution(self):
        reason = _risky_field_reason("address_1", unverified=True)

        self.assertEqual(
            reason,
            "This address couldn't be independently verified — please confirm it yourself before applying",
        )

    def test_unverified_postcode_field_gets_the_same_stronger_caution(self):
        # postcode has no category of its own at all today (it's not in
        # HOUSE_NUMBER_FIELDS/GEOCODE_RISK_FIELDS/RISKY_TEXT_FIELDS) - the
        # unverified override is the only thing that gives it a specific
        # reason rather than the generic fallback.
        reason = _risky_field_reason("postcode", unverified=True)

        self.assertEqual(
            reason,
            "This address couldn't be independently verified — please confirm it yourself before applying",
        )

    def test_unverified_lat_lng_fields_get_the_same_stronger_caution(self):
        self.assertEqual(_risky_field_reason("lat", unverified=True), _risky_field_reason("address_1", unverified=True))
        self.assertEqual(_risky_field_reason("lng", unverified=True), _risky_field_reason("address_1", unverified=True))

    def test_unverified_false_keeps_the_normal_house_number_wording(self):
        # Regression: the existing address_1/building wording must be
        # completely unaffected for a row whose geocode wasn't unverified -
        # the default parameter value must preserve every pre-existing
        # caller's exact old behavior.
        self.assertEqual(_risky_field_reason("address_1"), "Existing address would be replaced")
        self.assertEqual(_risky_field_reason("address_1", unverified=False), "Existing address would be replaced")

    def test_unverified_true_does_not_affect_a_field_outside_the_geocode_set(self):
        # "building" is risky via HOUSE_NUMBER_FIELDS too, but it is never
        # set by geocode.py at all - unverified=True must not leak its
        # override onto a field the flag has nothing to do with.
        self.assertEqual(_risky_field_reason("building", unverified=True), "Existing address would be replaced")

    def test_unverified_true_still_falls_through_correctly_for_text_fields(self):
        self.assertEqual(
            _risky_field_reason("special_features", unverified=True),
            "New text looks shorter than what's there now — may be missing detail, not just an update.",
        )

    def test_normal_geocode_risk_wording_unaffected_when_not_unverified(self):
        self.assertEqual(_risky_field_reason("lat"), "Existing location would be replaced")


class RiskyFieldReasonAddressConflictTests(unittest.TestCase):
    """address_conflict (see schema.ListingRow's own docstring - the
    confirmed real Ivybridge House case) shows its own note verbatim as
    the caption, checked before even the unverified-geocode override -
    the most specific, most actionable reason there is."""

    def test_address_conflict_note_is_shown_verbatim(self):
        note = "Brochure states '1 to 5 Adam Street', file has '1 John Adam Street'"
        self.assertEqual(_risky_field_reason("address_1", address_conflict=note), note)

    def test_address_conflict_takes_priority_over_unverified(self):
        note = "Brochure states '1 to 5 Adam Street', file has '1 John Adam Street'"
        self.assertEqual(_risky_field_reason("address_1", unverified=True, address_conflict=note), note)

    def test_address_conflict_only_applies_to_address_1_never_another_field(self):
        # A conflict note only ever describes address_1 - it must never
        # leak onto some other field's own caption.
        note = "Brochure states '1 to 5 Adam Street', file has '1 John Adam Street'"
        self.assertEqual(_risky_field_reason("postcode", address_conflict=note), "Existing value differs from the new upload")

    def test_no_address_conflict_keeps_the_pre_existing_wording(self):
        self.assertEqual(_risky_field_reason("address_1", address_conflict=None), "Existing address would be replaced")
        self.assertEqual(_risky_field_reason("address_1", address_conflict=""), "Existing address would be replaced")


if __name__ == "__main__":
    unittest.main()
