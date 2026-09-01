"""
Regression tests for extract_email.py's brochure_link/floorplan_link
fallback - never calls the real Gemini API or reads a real .eml file from
disk (get_client/call_gemini/load_eml_body are all mocked), same principle
as test_extract_spreadsheet_gemini.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_extract_email -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract_email


class BrochureLinkFloorplanFallbackTests(unittest.TestCase):
    """
    brochure_link falls back to floorplan_link's own URL whenever finalize_
    brochure_link ends up with nothing genuine but finalize_floorplan_link
    found a real floor plan - the same fallback extract_spreadsheet_gemini.
    py's own extract_sheet_with_metadata applies (see ListingRow.brochure_
    link_is_floorplan's own schema.py docstring), added here identically so
    all three extraction paths behave the same way - now genuinely
    identical across all three, since finalize_brochure_link's former PDF-
    fallback default (once PDF-specific) was removed entirely; an email
    (like every source type now) genuinely stays null when nothing was
    found, so this fallback is reachable the same way here as everywhere
    else.
    """

    def _extract(self, brochure_link, floorplan_link):
        raw = {
            "provider": "UNION", "contacts": None,
            "units": [
                {"building": "155 Fenchurch Street", "floor_unit": "7th",
                 "brochure_link": brochure_link, "floorplan_link": floorplan_link},
            ],
        }
        with patch("extract_email.get_client"), \
             patch("extract_email.load_eml_body", return_value="email body"), \
             patch("extract_email.call_gemini", return_value=raw):
            rows = extract_email.extract(Path("union.eml"))
        return rows[0]

    def test_brochure_blank_floorplan_present_fills_in_and_flags_as_floorplan(self):
        row = self._extract(
            brochure_link=None,
            floorplan_link="https://app.box.com/s/5cbox5mdsxeqe1jb26dgj1agx2e7kbmi",
        )
        self.assertEqual(row.brochure_link, "https://app.box.com/s/5cbox5mdsxeqe1jb26dgj1agx2e7kbmi")
        self.assertEqual(row.floorplan_link, "https://app.box.com/s/5cbox5mdsxeqe1jb26dgj1agx2e7kbmi")
        self.assertIs(row.brochure_link_is_floorplan, True)

    def test_both_present_brochure_link_stays_genuine_and_unflagged(self):
        row = self._extract(
            brochure_link="https://example.com/brochure.pdf",
            floorplan_link="https://app.box.com/s/floorplan-only",
        )
        self.assertEqual(row.brochure_link, "https://example.com/brochure.pdf")
        self.assertEqual(row.floorplan_link, "https://app.box.com/s/floorplan-only")
        self.assertIsNone(row.brochure_link_is_floorplan)

    def test_both_blank_brochure_link_stays_blank(self):
        row = self._extract(brochure_link=None, floorplan_link=None)
        self.assertIsNone(row.brochure_link)
        self.assertIsNone(row.floorplan_link)
        self.assertIsNone(row.brochure_link_is_floorplan)


if __name__ == "__main__":
    unittest.main()
