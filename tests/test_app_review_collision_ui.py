"""
Streamlit-level regression test for pages/2_Review_and_Master.py's collision
rendering (see master_merge.matched_collision_field_choice/
collision_group_fields and _render_collision_group/_render_matched_row in
the page itself).

Real reported case: a single uploaded Copthall Estates workbook where two
different sheets (a portfolio-wide rollup and that provider's own dedicated
per-building detail sheet) both independently extracted "Copthall House" -
4th Floor with byte-identical values for every changed field - rendered as
TWO separate, fully-expanded diff blocks, forcing a reviewer to approve the
same 6 fields twice. This runs the real page via Streamlit's AppTest and
confirms that same shape now renders as exactly ONE decision, and that a
group with one genuine field disagreement still only forces a choice on
that one field.

Runs from an isolated temporary working directory (never the real repo) -
master_writer.py/storage.blob_store's paths are plain relative strings
resolved against the process's cwd, same isolation approach already used by
tests/test_master_writer.py - so this can never touch the real data/
master.xlsx or staging/.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_collision_ui -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import master_writer
from schema import ListingRow
from storage.file_store import save_staging_file

BASE = Path(__file__).resolve().parent.parent


class CollisionGroupRendersAsOneDecisionTests(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()

    def test_identical_collision_renders_as_one_expander_not_two(self):
        # Master already has a Copthall House row with its OWN address
        # already on file (only submarket/lat/lng/features/contacts still
        # blank) - deliberately avoids also exercising house_number_changed
        # (a real, separate, unrelated safeguard that fires whenever
        # address_1 goes from blank to populated - not what this test is
        # about). This upload's two sheets both fill in the same remaining
        # fields identically, the exact reported shape.
        master_writer.write_master([
            ListingRow(
                building="Copthall House", provider="Copthall Estates", floor_unit="4th Floor",
                address_1="1 Copthall Avenue",
            ),
        ])
        shared_fields = dict(
            building="Copthall House", provider="Copthall Estates", floor_unit="4th Floor",
            address_1="1 Copthall Avenue", submarket="City", lat=51.5175, lng=-0.0896,
            special_features="Air conditioning; 24hr access", contacts="Jane Doe, jane@example.com",
        )
        rollup_row = ListingRow(**shared_fields, source_file="Copthall.xlsx — Portfolio")
        detail_row = ListingRow(**shared_fields, source_file="Copthall.xlsx — City Detail")
        save_staging_file([rollup_row, detail_row], "Copthall.xlsx", content_hash="test-hash-identical")

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        # Every field genuinely agrees between the two sources, so this
        # group auto-applies silently - no "Needs your decision" card of
        # any kind, no leftover per-source "pick one" radio, and it's
        # counted/shown exactly ONCE (not twice) in the automatic-updates
        # summary - the real "one property, one update" consolidation the
        # reported bug was about, now surfaced through that summary instead
        # of a manual-review toggle forcing a normally-silent path to render.
        self.assertNotIn("⚠️ Needs your decision", [s.value for s in at.subheader])
        self.assertEqual([r for r in at.radio if r.label == "Keep value from:"], [])

        info_text = "".join(i.value for i in at.info)
        self.assertIn("1 existing property will be updated automatically.", info_text)

        # st.expander's own contents run on every rerun regardless of
        # collapsed/expanded state (a purely client-side visual toggle) -
        # at.markdown already reflects this expander's full contents with
        # no need to simulate opening it.
        self.assertTrue(any(e.label == "View changes" for e in at.expander))
        markdown_text = "".join(m.value or "" for m in at.markdown)
        # The property name appears exactly ONCE as its own compact-table
        # group header (see _render_compact_diff_table) - confirms the
        # group was consolidated into a single property's worth of
        # changes, not double-counted, rather than counting one repeated
        # "**Copthall House** — field" header per field as the old
        # one-big-card-per-field layout did.
        self.assertEqual(markdown_text.count("**Copthall House — Copthall Estates — 4th Floor**"), 1)
        # Every changed field appears as its own compact "Field: before ->
        # after" line. address_1 isn't here - it's identical to what
        # master already had, so it's not a change at all.
        for label in ("Submarket", "Lat", "Lng", "Special Features", "Contacts"):
            self.assertIn(f"{label}:", markdown_text)

    def test_one_field_disagreement_still_forces_only_that_one_choice(self):
        master_writer.write_master([
            ListingRow(building="Copthall House", provider="Copthall Estates", floor_unit="4th Floor"),
        ])
        rollup_row = ListingRow(
            building="Copthall House", provider="Copthall Estates", floor_unit="4th Floor",
            address_1="1 Copthall Avenue", state_of_space="Cat A", source_file="Copthall.xlsx — Portfolio",
        )
        detail_row = ListingRow(
            building="Copthall House", provider="Copthall Estates", floor_unit="4th Floor",
            address_1="1 Copthall Avenue", state_of_space="Fitted", source_file="Copthall.xlsx — City Detail",
        )
        save_staging_file([rollup_row, detail_row], "Copthall.xlsx", content_hash="test-hash-disagree")

        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        # address_1 agrees between both sources - state_of_space genuinely
        # disagrees ("Cat A" vs "Fitted"). Even in the DEFAULT auto-accept
        # mode, the one disagreement forces exactly one expander to appear
        # (a field with no resolved value can never auto-apply) - the radio
        # for that field is the reviewer's only required click; address_1
        # still auto-resolves with no separate expander/click of its own.
        copthall_expanders = [e for e in at.expander if "Copthall House" in (e.label or "")]
        self.assertEqual(len(copthall_expanders), 1)

        choice_radios = [r for r in at.radio if r.label == "Keep value from:"]
        self.assertEqual(len(choice_radios), 1)


if __name__ == "__main__":
    unittest.main()
