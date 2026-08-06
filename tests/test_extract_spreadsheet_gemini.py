"""
Regression tests for extract_spreadsheet_gemini.py - the Gemini text-
extraction fallback for spreadsheet sheets with no single consistent header
row (see the module docstring). Never calls the real Gemini API - get_client/
call_gemini are mocked throughout, same principle as test_brochure_link.py
mocking GCS: this dev environment has no live-model determinism to test
against, only the deterministic code around it (rendering, row construction,
brochure-link resolution, building inheritance).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_extract_spreadsheet_gemini -v
"""

import contextlib
import io
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract_spreadsheet_gemini


class RenderSheetAsTextTests(unittest.TestCase):
    def _sheet(self, rows):
        wb = Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        return ws

    def test_blank_rows_are_skipped_entirely(self):
        ws = self._sheet([["Building"], [None], ["28 Lime Street"]])
        text = extract_spreadsheet_gemini.render_sheet_as_text(ws)
        self.assertEqual(text, "Row 1: Building\nRow 3: 28 Lime Street")

    def test_merged_continuation_blank_cells_are_preserved_not_dropped(self):
        # Real Copthall Portfolio-sheet shape: Area/Building blank on a
        # continuation row must render as an empty slot between pipes, not
        # be silently compacted away - column position is the only signal
        # Gemini has that this row inherits the value above it.
        ws = self._sheet([
            ["Area", "Building", "Floor"],
            ["City", "Copthall Avenue", "LG Floor"],
            [None, None, "2nd Floor"],
        ])
        text = extract_spreadsheet_gemini.render_sheet_as_text(ws)
        lines = text.splitlines()
        self.assertEqual(lines[2], "Row 3:  |  | 2nd Floor")

    def test_trailing_blank_cells_are_trimmed(self):
        ws = self._sheet([["Building", None, None]])
        text = extract_spreadsheet_gemini.render_sheet_as_text(ws)
        self.assertEqual(text, "Row 1: Building")

    def test_hyperlink_target_is_inlined_after_display_text(self):
        ws = self._sheet([["Download Brochure"]])
        ws["A1"].hyperlink = "https://example.com/brochure.pdf"
        text = extract_spreadsheet_gemini.render_sheet_as_text(ws)
        self.assertEqual(text, "Row 1: Download Brochure (https://example.com/brochure.pdf)")

    def test_hyperlink_with_no_display_text_uses_bare_url(self):
        ws = self._sheet([[None]])
        ws["A1"].hyperlink = "https://example.com/brochure.pdf"
        # openpyxl's hyperlink setter auto-populates .value to match when the
        # cell had none - reset it to actually construct "a hyperlink with no
        # separate display text" rather than "display text that happens to
        # equal the URL".
        ws["A1"].value = None
        text = extract_spreadsheet_gemini.render_sheet_as_text(ws)
        self.assertEqual(text, "Row 1: https://example.com/brochure.pdf")

    def test_all_blank_sheet_renders_to_empty_string(self):
        ws = self._sheet([[None, None], [None]])
        self.assertEqual(extract_spreadsheet_gemini.render_sheet_as_text(ws), "")


class ExtractSheetTests(unittest.TestCase):
    def _sheet(self):
        wb = Workbook()
        ws = wb.active
        ws["A1"] = "Office"
        ws["A2"] = "4th Floor"
        return ws

    def test_empty_sheet_never_calls_gemini(self):
        wb = Workbook()
        ws = wb.active  # genuinely blank
        with patch("extract_spreadsheet_gemini.get_client") as mock_get_client, \
             patch("extract_spreadsheet_gemini.call_gemini") as mock_call_gemini:
            rows = extract_spreadsheet_gemini.extract_sheet(ws, "file.xlsx — Sheet1", "file.xlsx")

        self.assertEqual(rows, [])
        mock_get_client.assert_not_called()
        mock_call_gemini.assert_not_called()

    def test_builds_listing_rows_from_gemini_output(self):
        raw = {
            "provider": "Copthall Estates",
            "contacts": "Kiri Norton-Brennan, 0795 811 8382",
            "units": [
                {"building": "28 Lime Street", "floor_unit": "4th Floor", "size_sqft": 1358,
                 "rent_pcm": 19805, "rent_psf": 175, "special_features": "22 desks"},
            ],
        }
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            rows = extract_spreadsheet_gemini.extract_sheet(self._sheet(), "file.xlsx — City", "file.xlsx")

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.building, "28 Lime Street")
        self.assertEqual(row.provider, "Copthall Estates")
        self.assertEqual(row.contacts, "Kiri Norton-Brennan, 0795 811 8382")
        self.assertEqual(row.size_sqft, 1358)
        self.assertEqual(row.source_file, "file.xlsx — City")

    def test_unit_with_blank_building_inherits_from_prior_unit(self):
        raw = {
            "provider": None, "contacts": None,
            "units": [
                {"building": "111 Wardour Street", "floor_unit": "4th"},
                {"building": None, "floor_unit": "5th"},
            ],
        }
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            rows = extract_spreadsheet_gemini.extract_sheet(self._sheet(), "file.xlsx — Sheet1", "file.xlsx")

        self.assertEqual([r.building for r in rows], ["111 Wardour Street", "111 Wardour Street"])

    def test_unit_with_blank_building_and_no_prior_is_skipped(self):
        raw = {"provider": None, "contacts": None, "units": [{"building": None, "floor_unit": "4th"}]}
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            rows = extract_spreadsheet_gemini.extract_sheet(self._sheet(), "file.xlsx — Sheet1", "file.xlsx")

        self.assertEqual(rows, [])

    def test_no_units_returns_empty_list(self):
        raw = {"provider": "Copthall Estates", "contacts": None, "units": []}
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            rows = extract_spreadsheet_gemini.extract_sheet(self._sheet(), "file.xlsx — Incentives", "file.xlsx")

        self.assertEqual(rows, [])


class AddressHouseNumberVerificationTests(unittest.TestCase):
    """The real reported failure: Gemini transcribed a raw sheet cell of
    "14-18 Copthall Avenue, EC2R 7DJ" as address_1 "18 Copthall Avenue",
    silently dropping the first half of a hyphenated house-number range.
    extract_sheet must catch and correct this against the raw sheet text
    itself - deterministic regex parsing of a cell can't drop characters the
    way an LLM transcription can - rather than relying solely on
    master_merge.py's house_number_changed to catch it later (and only if
    something else ever re-uploads the correct value to compare against)."""

    def _copthall_sheet(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["28 Lime Street - City"])
        ws.append(["Modern refurbished offices with excellent natural light."])
        ws.append(["14-18 Copthall Avenue, EC2R 7DJ", "Download Floorplans"])
        ws.append(["Floor", "Size (sq ft)", "Rent"])
        ws.append(["4th Floor", 1358, 19805])
        return ws

    def test_dropped_range_is_restored_from_the_raw_postcode_line(self):
        raw = {
            "provider": "Copthall Estates", "contacts": None,
            "units": [{
                "building": "28 Lime Street", "address_1": "18 Copthall Avenue",
                "postcode": "EC2R 7DJ", "floor_unit": "4th Floor", "size_sqft": 1358,
            }],
        }
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            rows = extract_spreadsheet_gemini.extract_sheet(self._copthall_sheet(), "file.xlsx — City", "file.xlsx")

        self.assertEqual(rows[0].address_1, "14-18 Copthall Avenue")

    def test_correct_extraction_is_left_untouched(self):
        # Gemini got it right this time - the raw line agrees with address_1,
        # so nothing should be rewritten at all.
        raw = {
            "provider": "Copthall Estates", "contacts": None,
            "units": [{
                "building": "28 Lime Street", "address_1": "14-18 Copthall Avenue",
                "postcode": "EC2R 7DJ", "floor_unit": "4th Floor", "size_sqft": 1358,
            }],
        }
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            rows = extract_spreadsheet_gemini.extract_sheet(self._copthall_sheet(), "file.xlsx — City", "file.xlsx")

        self.assertEqual(rows[0].address_1, "14-18 Copthall Avenue")

    def test_no_confident_raw_match_warns_but_does_not_override_or_crash(self):
        # No postcode given, and the unit's building never appears as its
        # own heading line in this sheet at all - nothing to confidently
        # verify against, so address_1 must survive unchanged and the call
        # must still complete normally, just with a warning on stderr.
        raw = {
            "provider": None, "contacts": None,
            "units": [{
                "building": "Some Other Building Not In This Sheet",
                "address_1": "18 Copthall Avenue", "floor_unit": "4th Floor",
            }],
        }
        wb = Workbook()
        ws = wb.active
        ws.append(["Unrelated content with no address or postcode at all"])

        stderr = io.StringIO()
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw), \
             contextlib.redirect_stderr(stderr):
            rows = extract_spreadsheet_gemini.extract_sheet(ws, "file.xlsx — City", "file.xlsx")

        self.assertEqual(rows[0].address_1, "18 Copthall Avenue")
        self.assertIn("WARNING", stderr.getvalue())

    def test_units_sharing_one_building_block_dont_bleed_into_each_others_address(self):
        # Two floors of the same building (no postcode on either) - the
        # block-boundary logic must still land on THIS building's own address
        # line, not wander into a neighboring building's block.
        wb = Workbook()
        ws = wb.active
        ws.append(["28 Lime Street - City"])
        ws.append(["14-18 Copthall Avenue"])
        ws.append(["Floor", "Size (sq ft)"])
        ws.append(["4th Floor", 1358])
        ws.append(["5th Floor", 2000])
        ws.append(["40 New Bond Street - Mayfair"])
        ws.append(["1 New Bond Street"])
        ws.append(["Floor", "Size (sq ft)"])
        ws.append(["Ground Floor", 900])

        raw = {
            "provider": None, "contacts": None,
            "units": [
                {"building": "28 Lime Street", "address_1": "18 Copthall Avenue", "floor_unit": "4th Floor"},
                {"building": "28 Lime Street", "address_1": "18 Copthall Avenue", "floor_unit": "5th Floor"},
                {"building": "40 New Bond Street", "address_1": "1 New Bond Street", "floor_unit": "Ground Floor"},
            ],
        }
        with patch("extract_spreadsheet_gemini.get_client"), \
             patch("extract_spreadsheet_gemini.call_gemini", return_value=raw):
            rows = extract_spreadsheet_gemini.extract_sheet(ws, "file.xlsx — City", "file.xlsx")

        self.assertEqual(rows[0].address_1, "14-18 Copthall Avenue")
        self.assertEqual(rows[1].address_1, "14-18 Copthall Avenue")
        self.assertEqual(rows[2].address_1, "1 New Bond Street")


class UndercountedBuildingsTests(unittest.TestCase):
    """Real production case: a live Gemini call against the real Copthall
    Estates "Mid Town" sheet's Cursitor Street mini-table (G/LG East, 1st
    Floor, 4th Floor) returned only 1 unit - a silent, validly-parsed but
    short response no existing check catches, since the one row that DID
    survive (G/LG East) had a completely normal size_sqft/rent_pcm. This
    reproduces that exact sheet's shape - confirmed against the real file:
    a blank leading column on every row, including each building's own
    heading line, two building blocks separated by a prose amenities
    paragraph and an address/download-link line."""

    def _mid_town_sheet(self):
        wb = Workbook()
        ws = wb.active
        ws.append([None, "Charterhouse Street - Farringdon / Barbican"])
        ws.append([None, "Nestled in the vibrant Farringdon/Smithfield quarter, this stylish workspace features high-speed fibre."])
        ws.append([None, "89 Charterhouse Street, EC1M 6PE", None, None, None, "Download Floorplans"])
        ws.append([
            None, "Office", "Sq.Ft", "Price Per Sq.Ft", "Monthly List Price",
            "Office Description", "Minimum Term", "Available From", "Commission",
        ])
        ws.append([
            None, "3rd Floor", 1960.0, 175.0, 28583.0,
            "Currently set up with 24 desks, 1 large boardroom, 1 meeting room, 1 chat room, 1 phone booths "
            "and separate breakout/kitchen",
            "24 Months", "Now", "Up to 15% - See incentives tab",
        ])
        ws.append([None, "Cursitor Street - Chancery Lane"])
        ws.append([
            None, "Cursitor Street offers fully furnished, self-contained office spaces with meeting rooms, "
            "kitchens, high-speed internet, air-conditioning, showers, bike storage, a passenger lift, and "
            "24/7 access.",
        ])
        ws.append([None, "11 Cursitor Street, EC4A 1LL", None, None, None, "Download Floorplans"])
        ws.append([
            None, "Office", "Sq.Ft", "Price Per Sq.Ft", "Monthly List Price",
            "Office Description", "Minimum Term", "Available From", "Commission",
        ])
        ws.append([
            None, "G/LG East", 1800.0, 143.0, 21379.0,
            "24+ desks on the ground floor and a dedicated kitchen, and a breakout area in the LG floor. "
            "The layout also provides the option for a private meeting room on the ground floor, with its "
            "location to be determined by the tenant",
            "24 Months", "Now",
            "10% on the first 12 months, 2% commission on months 13-24 for any deals with a lease term of "
            "36 months or longer. Payable when the deal enters year 3.  Bonus Available for 4th and G/LG "
            "East Floors **See incentives tab",
        ])
        ws.append([
            None, "1st Floor", 1696.0, 160.0, 22614.0,
            "18 desks, 1 exec office, 10 person boardroom, dedicated kitchen and breakout space",
            "24 Months", "1st October 2026",
        ])
        ws.append([
            None, "4th Floor", 706.0, 160.0, 9413.0,
            "12 desks, dedicated kitchen, dedicated 8 person boardroom",
            "24 Months", "Now",
        ])
        return ws

    def test_partial_response_is_flagged(self):
        # The exact real failure: only G/LG East (the first, longest-
        # described row) survived out of Cursitor Street's 3 floors. Also
        # the true-positive re-check for the heading-anchor/boundary rework
        # below - this must still be caught, not just the 3 false positives
        # it fixes.
        text = extract_spreadsheet_gemini.render_sheet_as_text(self._mid_town_sheet())
        units = [
            {"building": "Charterhouse Street"},
            {"building": "Cursitor Street"},
        ]

        mismatches = extract_spreadsheet_gemini.find_undercounted_buildings(text, units)

        self.assertEqual(mismatches, [("Cursitor Street", 3, 1)])

    def test_correct_extraction_has_no_mismatch(self):
        text = extract_spreadsheet_gemini.render_sheet_as_text(self._mid_town_sheet())
        units = [
            {"building": "Charterhouse Street"},
            {"building": "Cursitor Street"},
            {"building": "Cursitor Street"},
            {"building": "Cursitor Street"},
        ]

        self.assertEqual(extract_spreadsheet_gemini.find_undercounted_buildings(text, units), [])

    def test_single_floor_building_has_no_false_positive(self):
        # Charterhouse's own 1-floor block must never be flagged just
        # because its address/header lines are also multi-column - checked
        # alongside Cursitor Street's full, correct 3-unit extraction (the
        # real calling convention: app.py always passes every unit
        # extracted from the WHOLE sheet at once, never a single
        # building's units in isolation - block boundaries are only
        # anchored against buildings that actually appear in `units`, so a
        # building missing entirely from that list can't bound anything
        # after it).
        text = extract_spreadsheet_gemini.render_sheet_as_text(self._mid_town_sheet())
        units = [
            {"building": "Charterhouse Street"},
            {"building": "Cursitor Street"},
            {"building": "Cursitor Street"},
            {"building": "Cursitor Street"},
        ]

        self.assertEqual(extract_spreadsheet_gemini.find_undercounted_buildings(text, units), [])

    def test_building_not_found_in_text_returns_no_mismatch(self):
        mismatches = extract_spreadsheet_gemini.find_undercounted_buildings(
            "Row 1: unrelated content", [{"building": "Nowhere"}],
        )
        self.assertEqual(mismatches, [])


class UndercountedBuildingsFalsePositiveTests(unittest.TestCase):
    """The first version of find_undercounted_buildings/_apparent_data_row_
    count had 3 confirmed real false positives against the actual Copthall
    Estates Availability file's City, Westend Soho, and Portfolio sheets -
    each reproduced here against the exact shape that triggered it, plus the
    Mid Town true positive re-checked above in UndercountedBuildingsTests."""

    def _sheet(self, rows):
        wb = Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        return ws

    def test_building_name_mentioned_in_another_buildings_description_is_ignored(self):
        # Real false positive: a plain substring search for a building's
        # name landed inside a COMPLETELY DIFFERENT building's own prose
        # description paragraph (which happened to mention it in passing),
        # rather than that building's own real heading line further down -
        # here "The Scalpel" is name-dropped inside Moor House's description,
        # before The Scalpel's own heading and block even appear.
        text = extract_spreadsheet_gemini.render_sheet_as_text(self._sheet([
            [None, "Moor House - Moorgate"],
            [None, "Just five minutes from The Scalpel and Bank station, this striking building offers "
                   "24/7 access and a rooftop terrace."],
            [None, "1 London Wall, EC2Y 5EA", None, None, None, "Download Floorplans"],
            [None, "Office", "Sq.Ft", "Rent PCM", "Available From"],
            [None, "2nd Floor", 2200.0, 15000.0, "Now"],
            [None, "The Scalpel - Leadenhall"],
            [None, "An instantly recognisable landmark tower with panoramic City views."],
            [None, "52 Lime Street, EC3M 7AF", None, None, None, "Download Floorplans"],
            [None, "Office", "Sq.Ft", "Rent PCM", "Available From"],
            [None, "5th Floor", 3100.0, 21000.0, "Now"],
            [None, "9th Floor", 2900.0, 19500.0, "1st October 2026"],
        ]))
        units = [
            {"building": "Moor House"},
            {"building": "The Scalpel"},
            {"building": "The Scalpel"},
        ]

        mismatches = extract_spreadsheet_gemini.find_undercounted_buildings(text, units)

        self.assertEqual(mismatches, [])

    def test_fully_occupied_building_with_zero_units_does_not_inflate_neighbor(self):
        # Real false positive: a building with NO surviving units at all
        # (correctly, per PROMPT, since its own mini-table just says "Fully
        # Occupied") never appears in `units`, so it can't bound the
        # PRECEDING building's own block boundary by its heading line -
        # the old substring-based boundary ran straight past it to the next
        # SURVIVING building's heading, silently counting the fully-occupied
        # building's own mini-table header row as an extra data row for the
        # building before it.
        text = extract_spreadsheet_gemini.render_sheet_as_text(self._sheet([
            [None, "Bankside House - Southwark"],
            [None, "A refurbished period building moments from Southwark station."],
            [None, "10 Bankside, SE1 9EY", None, None, None, "Download Floorplans"],
            [None, "Office", "Sq.Ft", "Rent PCM", "Available From"],
            [None, "1st Floor", 1500.0, 12000.0, "Now"],
            [None, "Riverside Building - Southwark"],
            [None, "A modern glass-fronted office building overlooking the Thames."],
            [None, "15 Riverside Walk, SE1 9EZ", None, None, None, "Download Floorplans"],
            [None, "Office", "Sq.Ft", "Rent PCM", "Available From"],
            [None, "Fully Occupied"],
            [None, "Skyline Tower - Southwark"],
            [None, "A striking new-build tower with flexible floorplates."],
            [None, "20 Skyline Way, SE1 9FA", None, None, None, "Download Floorplans"],
            [None, "Office", "Sq.Ft", "Rent PCM", "Available From"],
            [None, "3rd Floor", 1800.0, 14000.0, "Now"],
            [None, "4th Floor", 1750.0, 13500.0, "Now"],
        ]))
        units = [
            {"building": "Bankside House"},
            {"building": "Skyline Tower"},
            {"building": "Skyline Tower"},
        ]

        mismatches = extract_spreadsheet_gemini.find_undercounted_buildings(text, units)

        self.assertEqual(mismatches, [])

    def test_single_consistent_table_sheet_is_never_flagged(self):
        # Real false positive: a single-consistent-table sheet (PROMPT shape
        # (a) - one sheet-wide header row, building repeated per data row,
        # no per-building heading/address/download lines at all) isn't the
        # repeating-blocks shape this heuristic was designed for. With no
        # per-building end-of-block anchor available, the old code ran the
        # last building's own block all the way to the end of the sheet,
        # picking up trailing boilerplate rows as if they were extra units.
        text = extract_spreadsheet_gemini.render_sheet_as_text(self._sheet([
            [None, "Building", "Floor", "Size (sq ft)", "Rent PCM"],
            [None, "Portfolio Building A", "1st Floor", 1000, 20000],
            [None, "Portfolio Building A", "2nd Floor", 1200, 24000],
            [None, "Portfolio Building B", "Ground Floor", 900, 18000],
            [None, "Terms and Conditions apply", "See website for details", "Not for reliance"],
            [None, "This document does not constitute an offer or contract", "E&OE", "v2"],
        ]))
        units = [
            {"building": "Portfolio Building A"},
            {"building": "Portfolio Building A"},
            {"building": "Portfolio Building B"},
        ]

        mismatches = extract_spreadsheet_gemini.find_undercounted_buildings(text, units)

        self.assertEqual(mismatches, [])


class SheetShowsFullyOccupiedBuildingTests(unittest.TestCase):
    def test_true_when_a_fully_occupied_row_is_present(self):
        text = (
            "Row 1: | Riverside Building - Southwark\n"
            "Row 2: | A modern glass-fronted office building.\n"
            "Row 3: | 15 Riverside Walk, SE1 9EZ | | | | Download Floorplans\n"
            "Row 4: | Office | Sq.Ft | Rent PCM | Available From\n"
            "Row 5: | Fully Occupied"
        )
        self.assertTrue(extract_spreadsheet_gemini.sheet_shows_fully_occupied_building(text))

    def test_false_when_no_fully_occupied_row_is_present(self):
        text = "Row 1: unrelated content\nRow 2: nothing here either"
        self.assertFalse(extract_spreadsheet_gemini.sheet_shows_fully_occupied_building(text))


if __name__ == "__main__":
    unittest.main()
