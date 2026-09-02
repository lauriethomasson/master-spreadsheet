"""
Regression tests for a real, confirmed root cause: geocode_rows() runs
unconditionally right after extraction, before brochure enrichment ever
gets a chance to backfill address_1/postcode from the brochure itself - so
a row whose source spreadsheet stated only a bare building name falls
through to geocode.py's own weaker Tier 2 (Places name-only search), which
has no way to distinguish a same-named but genuinely different real place.

Confirmed real incident: a fresh Colliers upload's own "Thames Court" row
(4 Upper Thames Street, EC4V 3BJ - no street address of its own in the raw
spreadsheet) landed on a same-named building ~29km away in Surrey via
Tier 2, correctly flagged geocode_unverified=True, but nothing ever
revisited it once its own brochure went on to correctly backfill both
address_1 and postcode a few steps later.

Fix: app._pre_enrichment_geocode_snapshot/app._reattempt_geocoding_for_
newly_addressed_rows, called around app.py's own
_run_automatic_brochure_enrichment call site - re-runs geocode.geocode_row
(via Tier 1 this time, now that a real address+postcode exist) for any row
that (a) was already flagged geocode_unverified=True and (b) didn't
already have both address_1 and postcode before this specific enrichment
pass. Deliberately unit-tests the two extracted pure functions directly
rather than driving the whole upload flow through AppTest - streamlit.
testing.v1.AppTest cannot simulate st.data_editor/upload internals
meaningfully for this kind of pipeline-ordering logic (see this project's
own established testing convention for extracted pure logic).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_upload_geocode_reattempt -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema import ListingRow

import app


class ReattemptGeocodingAfterEnrichmentTests(unittest.TestCase):
    def test_unverified_row_with_no_prior_address_gets_re_geocoded_via_tier_1(self):
        # Thames Court's own real shape: no address_1/postcode at all
        # before enrichment, geocode_unverified=True from a Tier 2
        # zero-hint accept, with a STALE, WRONG lat/lng already on it.
        row = ListingRow(
            building="Thames Court", provider="Colliers",
            lat=51.2, lng=-0.6,  # the wrong, ~29km-away Surrey coordinate
            geocode_unverified=True,
        )
        pre_state = app._pre_enrichment_geocode_snapshot([row])

        # Enrichment backfills the real address+postcode from the brochure.
        row.address_1 = "4 Upper Thames Street"
        row.postcode = "EC4V 3BJ"

        with patch("app.geocode.geocode_row") as mock_geocode_row:
            def fake_geocode(r):
                r.lat = 51.5114
                r.lng = -0.0946
                r.geocode_unverified = False
                return r
            mock_geocode_row.side_effect = fake_geocode

            app._reattempt_geocoding_for_newly_addressed_rows([row], pre_state)

        mock_geocode_row.assert_called_once_with(row)
        # The stale coordinate was cleared before re-attempting - otherwise
        # geocode_row's own early-return (lat/lng already non-None) would
        # have made this whole re-attempt a silent no-op.
        self.assertEqual(row.lat, 51.5114)
        self.assertEqual(row.lng, -0.0946)
        self.assertIs(row.geocode_unverified, False)

    def test_trusted_row_is_never_re_geocoded_even_if_its_address_also_changed(self):
        # geocode_unverified=False (already trusted, real Tier 1/hint-
        # corroborated evidence) must never be re-geocoded, even if
        # enrichment also happens to touch its address_1/postcode.
        row = ListingRow(
            building="1 Fetter Lane", provider="Colliers",
            lat=51.514, lng=-0.108, geocode_unverified=False,
        )
        pre_state = app._pre_enrichment_geocode_snapshot([row])

        row.address_1 = "1 Fetter Lane"
        row.postcode = "EC4A 1BR"

        with patch("app.geocode.geocode_row") as mock_geocode_row:
            app._reattempt_geocoding_for_newly_addressed_rows([row], pre_state)

        mock_geocode_row.assert_not_called()
        self.assertEqual(row.lat, 51.514)
        self.assertEqual(row.lng, -0.108)

    def test_row_never_attempted_geocode_unverified_none_is_never_re_geocoded(self):
        # None means "this run never even touched the question" (e.g. the
        # row already had real lat/lng from a provider's own columns) - not
        # the same as an uncertain True, must never be re-geocoded either.
        row = ListingRow(building="9 Tanner Street", provider="Colliers", lat=51.5, lng=-0.08)
        pre_state = app._pre_enrichment_geocode_snapshot([row])

        row.address_1 = "9 Tanner Street"
        row.postcode = "SE1 3LE"

        with patch("app.geocode.geocode_row") as mock_geocode_row:
            app._reattempt_geocoding_for_newly_addressed_rows([row], pre_state)

        mock_geocode_row.assert_not_called()

    def test_unverified_row_that_already_had_both_fields_before_is_not_re_geocoded(self):
        # Scoped ONLY to a row that just gained genuinely NEW evidence -
        # a row that already had both address_1 and postcode before this
        # enrichment pass gained nothing new here, so re-running would
        # just be needless churn/API cost for no reason.
        row = ListingRow(
            building="Thames Court", provider="Colliers",
            address_1="4 Upper Thames Street", postcode="EC4V 3BJ",
            lat=51.2, lng=-0.6, geocode_unverified=True,
        )
        pre_state = app._pre_enrichment_geocode_snapshot([row])

        # Enrichment doesn't change anything further this time.
        with patch("app.geocode.geocode_row") as mock_geocode_row:
            app._reattempt_geocoding_for_newly_addressed_rows([row], pre_state)

        mock_geocode_row.assert_not_called()

    def test_unverified_row_still_missing_postcode_after_enrichment_is_not_re_geocoded(self):
        # Enrichment only backfilled address_1, not postcode - not "both"
        # yet, so this must not fire prematurely on partial evidence.
        row = ListingRow(building="Thames Court", provider="Colliers", geocode_unverified=True)
        pre_state = app._pre_enrichment_geocode_snapshot([row])

        row.address_1 = "4 Upper Thames Street"

        with patch("app.geocode.geocode_row") as mock_geocode_row:
            app._reattempt_geocoding_for_newly_addressed_rows([row], pre_state)

        mock_geocode_row.assert_not_called()


if __name__ == "__main__":
    unittest.main()
