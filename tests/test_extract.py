"""
Regression tests for extract.py's per-row PDF hyperlink extraction (see the
module-level comment block in extract.py for the full design/conservatism
rationale) - the geometric matching between PyMuPDF link annotations and
Gemini-extracted units' floor_unit/size_sqft text, run entirely offline
(no Gemini calls - these test the pure geometry/matching functions and
_attach_per_row_pdf_links directly against constructed and real PDFs).

Synthetic test PDFs are built with PyMuPDF itself (fitz.open() with no
path creates a new in-memory document) rather than needing a real sample
file, since the specific real-world document this feature targets ("Kitt's
Availability PDF") isn't available in this repo or environment - see
_make_test_pdf. The "single incidental link" negative case is instead
checked directly against the real tests/sample_docs/city-tower-brochure.pdf
already in this repo.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_extract -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract

SAMPLE_DOCS = Path(__file__).resolve().parent / "sample_docs"


def _make_test_pdf(rows: list) -> Path:
    """
    rows: list of (floor_unit, size_sqft, link_uri_or_None) - one per text
    row on a single page. Each row with a link_uri gets its own small
    "Here" caption plus a real fitz.LINK_URI annotation placed at that
    caption's ACTUAL rendered bounding box (read back after inserting the
    text, rather than guessed from font metrics), so the test PDF is
    correct by construction rather than dependent on font-metric guesses.
    Returns a path to a temp .pdf file the caller is responsible for
    deleting.
    """
    doc = fitz.open()
    page = doc.new_page(width=400, height=40 + 30 * len(rows))
    y = 30
    row_ys = []
    for floor_unit, size_sqft, _ in rows:
        page.insert_text((50, y), str(floor_unit), fontsize=11)
        page.insert_text((150, y), str(size_sqft), fontsize=11)
        row_ys.append(y)
        y += 30

    words = page.get_text("words")
    for (floor_unit, size_sqft, link_uri), row_y in zip(rows, row_ys):
        if not link_uri:
            continue
        page.insert_text((250, row_y), "Here", fontsize=11)

    # Re-read words now that every "Here" caption is actually on the page,
    # then attach each row's link to ITS OWN caption's real bbox.
    words = page.get_text("words")
    for (floor_unit, size_sqft, link_uri), row_y in zip(rows, row_ys):
        if not link_uri:
            continue
        here_words = [w for w in words if w[4] == "Here" and abs((w[1] + w[3]) / 2 - row_y) < 8]
        assert here_words, f"test setup failed to place a 'Here' caption for row {floor_unit!r}"
        w = here_words[0]
        rect = fitz.Rect(w[0] - 1, w[1] - 1, w[2] + 1, w[3] + 1)
        page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": link_uri})

    # Closed immediately (not left open) before doc.save() - PyMuPDF's save
    # does an atomic replace on Windows, which conflicts with tempfile's own
    # still-open handle on the same path if delete=False without closing.
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    doc.save(str(tmp_path))
    doc.close()
    return tmp_path


class AttachPerRowPdfLinksTests(unittest.TestCase):
    def test_two_corroborating_rows_each_get_their_own_link(self):
        pdf_path = _make_test_pdf([
            ("3rd Floor", 5000, "https://example.com/unit-a"),
            ("5th Floor", 7500, "https://example.com/unit-b"),
        ])
        try:
            units = [
                {"floor_unit": "3rd Floor", "size_sqft": 5000, "brochure_link": None, "page_index": 0},
                {"floor_unit": "5th Floor", "size_sqft": 7500, "brochure_link": None, "page_index": 0},
            ]
            extract._attach_per_row_pdf_links(pdf_path, units)

            self.assertEqual(units[0]["brochure_link"], "https://example.com/unit-a")
            self.assertEqual(units[1]["brochure_link"], "https://example.com/unit-b")
        finally:
            pdf_path.unlink(missing_ok=True)

    def test_page_index_is_removed_from_every_unit_regardless_of_outcome(self):
        pdf_path = _make_test_pdf([("3rd Floor", 5000, None)])
        try:
            units = [{"floor_unit": "3rd Floor", "size_sqft": 5000, "brochure_link": None, "page_index": 0}]
            extract._attach_per_row_pdf_links(pdf_path, units)
            self.assertNotIn("page_index", units[0])
        finally:
            pdf_path.unlink(missing_ok=True)

    def test_a_single_link_on_the_page_never_gets_attached_to_anything(self):
        # Only ONE genuine link on the page (e.g. a logo/portfolio link, as
        # seen in city-tower-brochure.pdf) - MIN_LINKS_FOR_PER_ROW_LINKS
        # requires at least 2, so this must fall through completely
        # untouched, not get misapplied to either row.
        pdf_path = _make_test_pdf([
            ("3rd Floor", 5000, "https://example.com/only-link"),
            ("5th Floor", 7500, None),
        ])
        try:
            units = [
                {"floor_unit": "3rd Floor", "size_sqft": 5000, "brochure_link": None, "page_index": 0},
                {"floor_unit": "5th Floor", "size_sqft": 7500, "brochure_link": None, "page_index": 0},
            ]
            extract._attach_per_row_pdf_links(pdf_path, units)

            self.assertIsNone(units[0]["brochure_link"])
            self.assertIsNone(units[1]["brochure_link"])
        finally:
            pdf_path.unlink(missing_ok=True)

    def test_a_single_corroborating_unit_still_attaches_its_own_unambiguous_link(self):
        # Two links exist on the page (2 real rows), but only ONE unit was
        # handed to this function for this page - the single-unit
        # exception (see the module-level "EXCEPTION" comment) means this
        # is no longer held to MIN_UNITS_FOR_PER_ROW_LINKS/MIN_LINKS_FOR_
        # PER_ROW_LINKS at all. This is still a SAFE match, not a guess:
        # the row_y/ROW_Y_TOLERANCE check below finds exactly one nearby
        # link for "3rd Floor"'s own row (the "5th Floor" row's link sits
        # well outside tolerance), so attaching it is exactly as
        # geometrically confident as the ordinary >= 2 units case.
        pdf_path = _make_test_pdf([
            ("3rd Floor", 5000, "https://example.com/unit-a"),
            ("5th Floor", 7500, "https://example.com/unit-b"),
        ])
        try:
            units = [{"floor_unit": "3rd Floor", "size_sqft": 5000, "brochure_link": None, "page_index": 0}]
            extract._attach_per_row_pdf_links(pdf_path, units)
            self.assertEqual(units[0]["brochure_link"], "https://example.com/unit-a")
        finally:
            pdf_path.unlink(missing_ok=True)

    def test_units_on_a_different_page_index_are_left_alone(self):
        pdf_path = _make_test_pdf([
            ("3rd Floor", 5000, "https://example.com/unit-a"),
            ("5th Floor", 7500, "https://example.com/unit-b"),
        ])
        try:
            # page_index 1 doesn't exist on this single-page test PDF.
            units = [
                {"floor_unit": "3rd Floor", "size_sqft": 5000, "brochure_link": None, "page_index": 1},
                {"floor_unit": "5th Floor", "size_sqft": 7500, "brochure_link": None, "page_index": 1},
            ]
            extract._attach_per_row_pdf_links(pdf_path, units)
            self.assertIsNone(units[0]["brochure_link"])
            self.assertIsNone(units[1]["brochure_link"])
        finally:
            pdf_path.unlink(missing_ok=True)

    def test_a_unit_with_no_page_index_at_all_is_left_alone(self):
        pdf_path = _make_test_pdf([
            ("3rd Floor", 5000, "https://example.com/unit-a"),
            ("5th Floor", 7500, "https://example.com/unit-b"),
        ])
        try:
            units = [
                {"floor_unit": "3rd Floor", "size_sqft": 5000, "brochure_link": None},
                {"floor_unit": "5th Floor", "size_sqft": 7500, "brochure_link": None},
            ]
            extract._attach_per_row_pdf_links(pdf_path, units)
            self.assertIsNone(units[0]["brochure_link"])
            self.assertIsNone(units[1]["brochure_link"])
        finally:
            pdf_path.unlink(missing_ok=True)

    def test_a_pre_existing_brochure_link_is_overwritten_by_a_genuine_per_row_link(self):
        # Gemini may have already guessed something (e.g. a shared
        # portfolio link) - a confidently-located per-row link takes
        # priority over that guess.
        pdf_path = _make_test_pdf([
            ("3rd Floor", 5000, "https://example.com/unit-a"),
            ("5th Floor", 7500, "https://example.com/unit-b"),
        ])
        try:
            units = [
                {"floor_unit": "3rd Floor", "size_sqft": 5000, "brochure_link": "https://example.com/shared", "page_index": 0},
                {"floor_unit": "5th Floor", "size_sqft": 7500, "brochure_link": "https://example.com/shared", "page_index": 0},
            ]
            extract._attach_per_row_pdf_links(pdf_path, units)
            self.assertEqual(units[0]["brochure_link"], "https://example.com/unit-a")
            self.assertEqual(units[1]["brochure_link"], "https://example.com/unit-b")
        finally:
            pdf_path.unlink(missing_ok=True)


def _make_single_row_two_links_pdf(floor_unit, size_sqft, link_uris: list) -> Path:
    """
    A single-row PDF page with `floor_unit`/`size_sqft` text, plus one
    "Here" caption+link per entry in `link_uris`, all placed side-by-side
    on that SAME row - no column header text anywhere on the page, so
    neither _brochure_column_x_range nor _floor_plan_column_x_range can
    narrow anything; every link in `link_uris` stays a genuine "nearby"
    candidate for that one row. Models a single-unit page whose own row
    happens to have more than one candidate link near it (ambiguous),
    mirroring _make_multi_link_column_pdf's own row/link construction but
    deliberately without any column headers to disambiguate by.
    """
    doc = fitz.open()
    page = doc.new_page(width=500, height=70)
    y = 30
    page.insert_text((50, y), str(floor_unit), fontsize=11)
    page.insert_text((150, y), str(size_sqft), fontsize=11)
    x_positions = [250 + 60 * i for i in range(len(link_uris))]
    for x in x_positions:
        page.insert_text((x, y), "Here", fontsize=11)

    words = page.get_text("words")
    for x, uri in zip(x_positions, link_uris):
        candidates = [w for w in words if w[4] == "Here" and abs(w[0] - x) < 5]
        assert candidates, f"test setup failed to place a 'Here' caption at x={x}"
        w = candidates[0]
        rect = fitz.Rect(w[0] - 1, w[1] - 1, w[2] + 1, w[3] + 1)
        page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": uri})

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    doc.save(str(tmp_path))
    doc.close()
    return tmp_path


class SingleUnitPageLinkAttachmentTests(unittest.TestCase):
    """
    The single-unit exception (see extract.py's own module-level
    "EXCEPTION" comment and _attach_per_row_pdf_links's own docstring) -
    real confirmed case: a Colliers Canva-deck export ("Colliers Flex and
    Managed Availability") uses a slide-per-building layout, one page/one
    line item per building, with the building name itself hyperlinked to
    a real, listing-specific colliers.com property page. The flat
    ">= 2 units"/">= 2 links" gates skipped a page shaped like this
    entirely, silently losing that real per-property link to the generic
    whole-PDF rule-3 fallback instead.
    """

    def test_single_unit_page_with_one_link_gets_it_attached(self):
        pdf_path = _make_test_pdf([
            ("Kingsland House", 1120, "https://www.colliers.com/en-gb/properties/kingsland-house"),
        ])
        try:
            units = [{"floor_unit": "Kingsland House", "size_sqft": 1120, "brochure_link": None, "page_index": 0}]
            extract._attach_per_row_pdf_links(pdf_path, units)

            self.assertEqual(units[0]["brochure_link"], "https://www.colliers.com/en-gb/properties/kingsland-house")
        finally:
            pdf_path.unlink(missing_ok=True)

    def test_single_unit_page_with_no_links_is_left_untouched(self):
        # Falls through to whatever finalize_brochure_link's own existing
        # fallback rules decide (untouched here) - unchanged from today.
        pdf_path = _make_test_pdf([("Kingsland House", 1120, None)])
        try:
            units = [{"floor_unit": "Kingsland House", "size_sqft": 1120, "brochure_link": None, "page_index": 0}]
            extract._attach_per_row_pdf_links(pdf_path, units)

            self.assertIsNone(units[0]["brochure_link"])
        finally:
            pdf_path.unlink(missing_ok=True)

    def test_single_unit_page_with_two_ambiguous_links_is_left_untouched(self):
        # Two candidate links both sit near the page's own one row (no
        # column headers to disambiguate by) - genuinely ambiguous which
        # one is "the" brochure link, so this must be left alone, same
        # "leaving for the PDF fallback" logging path the existing >= 2
        # units ambiguous case already uses (see `elif len(nearby) > 1`
        # in _attach_per_row_pdf_links).
        pdf_path = _make_single_row_two_links_pdf(
            "Kingsland House", 1120,
            ["https://example.com/link-a", "https://example.com/link-b"],
        )
        try:
            units = [{"floor_unit": "Kingsland House", "size_sqft": 1120, "brochure_link": None, "page_index": 0}]
            extract._attach_per_row_pdf_links(pdf_path, units)

            self.assertIsNone(units[0]["brochure_link"])
        finally:
            pdf_path.unlink(missing_ok=True)


class BrochureLinkFloorplanFallbackTests(unittest.TestCase):
    """
    brochure_link falls back to floorplan_link's own URL whenever finalize_
    brochure_link ends up with nothing genuine but finalize_floorplan_link
    found a real floor plan - the same fallback extract_spreadsheet_gemini.
    py's own extract_sheet_with_metadata applies (see ListingRow.brochure_
    link_is_floorplan's own schema.py docstring), added here identically so
    all three extraction paths behave the same way.

    extract_raw_units (real PDF rendering + the Gemini vision call) is
    mocked wholesale, same principle as every other test in this file/
    module - no real PDF page rendering or network call involved.
    """

    def _extract(self, brochure_link, floorplan_link):
        raw = {
            "provider": "UNION", "contacts": None,
            "units": [
                {"building": "155 Fenchurch Street", "floor_unit": "7th",
                 "brochure_link": brochure_link, "floorplan_link": floorplan_link},
            ],
        }
        with patch("extract.extract_raw_units", return_value=raw):
            rows = extract.extract(Path(""))
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
        self.assertFalse(row.brochure_link)
        self.assertIsNone(row.floorplan_link)
        self.assertIsNone(row.brochure_link_is_floorplan)


class NoPdfFallbackLinkTests(unittest.TestCase):
    """
    Regression tests for the REMOVAL of finalize_brochure_link's former
    "rule 3" PDF-fallback default (defaulting brochure_link to the whole
    uploaded document whenever no genuine per-unit link was found) - a
    deliberate reversal of a prior intentional design decision, confirmed
    before removing it (see finalize_brochure_link's own docstring for the
    full reasoning). A unit with no genuine link of its own now gets
    brochure_link=None regardless of the document's own building count -
    single-building (a real Henly House-shaped brochure, previously the
    exact case rule 3 was "the expected default" for) and multi-building
    (a real Colliers bulk-tracker upload, previously handled by a NARROWER
    is_bulk_upload suppression guard - see git history for that guard's own
    prior implementation/tests) both now collapse to the same, simpler
    outcome, covering both extract() (real PDF upload) and extract_from_
    png_pages() (pasted Canva/Pitch link) since both share the same
    underlying _rows_from_raw.

    This class replaces the former MultiBuildingFallbackSuppressionTests,
    which tested the NOW-REMOVED is_bulk_upload suppression guard
    specifically (a mechanism that existed only to narrow rule 3's own
    reach) - that guard's entire reason to exist went away alongside rule
    3 itself, so those tests were exercising dead code; this class covers
    the new, simpler behavior those old tests' real-world scenarios
    (Henly House/Regent's Wharf/a Soho tracker) actually motivate now.
    """

    def test_single_building_document_now_gets_no_fallback_link(self):
        # The exact real Henly House-shaped case rule 3 used to treat as
        # its own expected default - now correctly None instead, the same
        # as a spreadsheet/email upload with nothing genuine already got.
        raw = {
            "provider": "Colliers", "contacts": None,
            "units": [
                {"building": "Henly House", "floor_unit": "1st Floor", "submarket": "Fitzrovia", "brochure_link": None},
                {"building": "Henly House", "floor_unit": "2nd Floor", "submarket": "Fitzrovia", "brochure_link": None},
            ],
        }
        with patch("extract.extract_raw_units", return_value=raw), patch("extract._attach_per_row_pdf_links"):
            rows = extract.extract(Path("henly.pdf"), original_filename="henly.pdf")

        self.assertIsNone(rows[0].brochure_link)
        self.assertIsNone(rows[1].brochure_link)

    def test_multi_building_tracker_also_gets_no_fallback_link(self):
        # The real Colliers bulk-tracker case (35 Gresse Street/Fitzrovia,
        # Whites Grounds/Bermondsey, Hatchers Yard/Surrey, Thames Court)
        # that originally motivated the now-removed is_bulk_upload guard -
        # this and the single-building case above now behave identically,
        # since there's no fallback left for building count to suppress.
        raw = {
            "provider": None, "contacts": None,
            "units": [
                {"building": "35 Gresse Street", "submarket": "Soho", "brochure_link": None},
                {"building": "Whites Grounds", "submarket": "Bermondsey", "brochure_link": None},
                {"building": "Hatchers Yard", "submarket": "Midtown", "brochure_link": None},
            ],
        }
        with patch("extract.get_client", return_value="fake-client"), \
                patch("extract.call_gemini", return_value=raw):
            rows = extract.extract_from_png_pages([b"\x89PNG\r\n\x1a\n rest"], original_filename="tracker.pdf")

        for row in rows:
            self.assertIsNone(row.brochure_link)

    def test_a_units_own_genuine_link_is_still_unaffected(self):
        # Only the removed fallback ever changed - a unit with its own
        # genuine, listing-specific link (rule 1) is completely unaffected.
        raw = {
            "provider": None, "contacts": None,
            "units": [
                {"building": "35 Gresse Street", "submarket": "Soho",
                 "brochure_link": "https://example.com/gresse-street.pdf"},
                {"building": "Whites Grounds", "submarket": "Bermondsey", "brochure_link": None},
            ],
        }
        with patch("extract.get_client", return_value="fake-client"), \
                patch("extract.call_gemini", return_value=raw):
            rows = extract.extract_from_png_pages([b"\x89PNG\r\n\x1a\n rest"], original_filename="tracker.pdf")

        self.assertEqual(rows[0].brochure_link, "https://example.com/gresse-street.pdf")
        self.assertIsNone(rows[1].brochure_link)


def _make_multi_link_column_pdf(rows: list) -> Path:
    """
    rows: list of (floor_unit, size_sqft, brochure_url, floor_plan_url,
    high_res_url) - each of the three link columns is optional (None to
    leave blank). Models a real Kitt's-style availability table: THREE
    distinct link columns per row (Link to Brochure, Floor Plan, High Res
    Images), all rendered as the same "Here" caption text, all landing at
    the same row y-position - exactly the shape that makes y-only matching
    ambiguous and requires _brochure_column_x_range's column disambiguation.
    """
    doc = fitz.open()
    page = doc.new_page(width=700, height=40 + 30 * len(rows))
    col_x = {"floor_unit": 50, "size": 150, "brochure": 250, "floor_plan": 350, "high_res": 450}

    header_y = 20
    page.insert_text((col_x["floor_unit"], header_y), "Floor/Unit", fontsize=11)
    page.insert_text((col_x["size"], header_y), "Size (sq ft)", fontsize=11)
    page.insert_text((col_x["brochure"], header_y), "Link to Brochure", fontsize=11)
    page.insert_text((col_x["floor_plan"], header_y), "Floor Plan", fontsize=11)
    page.insert_text((col_x["high_res"], header_y), "High Res Images", fontsize=11)

    row_ys = []
    y = 60
    for floor_unit, size_sqft, brochure_url, floor_plan_url, high_res_url in rows:
        page.insert_text((col_x["floor_unit"], y), str(floor_unit), fontsize=11)
        page.insert_text((col_x["size"], y), str(size_sqft), fontsize=11)
        if brochure_url:
            page.insert_text((col_x["brochure"], y), "Here", fontsize=11)
        if floor_plan_url:
            page.insert_text((col_x["floor_plan"], y), "Here", fontsize=11)
        if high_res_url:
            page.insert_text((col_x["high_res"], y), "Here", fontsize=11)
        row_ys.append(y)
        y += 30

    words = page.get_text("words")

    def _link_at(x_target, y_target, url):
        candidates = [w for w in words if w[4] == "Here" and abs(w[0] - x_target) < 5 and abs((w[1] + w[3]) / 2 - y_target) < 8]
        assert candidates, f"test setup failed to place a 'Here' caption near x={x_target} y={y_target}"
        w = candidates[0]
        rect = fitz.Rect(w[0] - 1, w[1] - 1, w[2] + 1, w[3] + 1)
        page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": url})

    for (floor_unit, size_sqft, brochure_url, floor_plan_url, high_res_url), row_y in zip(rows, row_ys):
        if brochure_url:
            _link_at(col_x["brochure"], row_y, brochure_url)
        if floor_plan_url:
            _link_at(col_x["floor_plan"], row_y, floor_plan_url)
        if high_res_url:
            _link_at(col_x["high_res"], row_y, high_res_url)

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    doc.save(str(tmp_path))
    doc.close()
    return tmp_path


def _make_floor_plan_only_pdf(rows: list, header_text: str = "Floor Plan") -> Path:
    """
    rows: list of (floor_unit, size_sqft, floor_plan_url) - models a page
    whose ONLY per-row link column is a Floor Plan column, with NO
    separate Brochure column at all - a genuinely distinct real shape from
    _make_multi_link_column_pdf's three-column table (which always has a
    real Brochure column too). header_text lets a caller check both real
    renderings this header is seen as ("Floor Plan" two words, or
    "Floorplan" one word - see _floor_plan_column_x_range).
    """
    doc = fitz.open()
    page = doc.new_page(width=500, height=40 + 30 * len(rows))
    col_x = {"floor_unit": 50, "size": 150, "floor_plan": 250}

    header_y = 20
    page.insert_text((col_x["floor_unit"], header_y), "Floor/Unit", fontsize=11)
    page.insert_text((col_x["size"], header_y), "Size (sq ft)", fontsize=11)
    page.insert_text((col_x["floor_plan"], header_y), header_text, fontsize=11)

    row_ys = []
    y = 60
    for floor_unit, size_sqft, floor_plan_url in rows:
        page.insert_text((col_x["floor_unit"], y), str(floor_unit), fontsize=11)
        page.insert_text((col_x["size"], y), str(size_sqft), fontsize=11)
        if floor_plan_url:
            page.insert_text((col_x["floor_plan"], y), "Here", fontsize=11)
        row_ys.append(y)
        y += 30

    words = page.get_text("words")

    def _link_at(x_target, y_target, url):
        candidates = [w for w in words if w[4] == "Here" and abs(w[0] - x_target) < 5 and abs((w[1] + w[3]) / 2 - y_target) < 8]
        assert candidates, f"test setup failed to place a 'Here' caption near x={x_target} y={y_target}"
        w = candidates[0]
        rect = fitz.Rect(w[0] - 1, w[1] - 1, w[2] + 1, w[3] + 1)
        page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": url})

    for (floor_unit, size_sqft, floor_plan_url), row_y in zip(rows, row_ys):
        if floor_plan_url:
            _link_at(col_x["floor_plan"], row_y, floor_plan_url)

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    doc.save(str(tmp_path))
    doc.close()
    return tmp_path


class FloorPlanOnlyColumnTests(unittest.TestCase):
    """
    A page whose only per-row link column is a Floor Plan column, with no
    separate Brochure column at all - previously, with no "brochure" header
    word to disambiguate by, _attach_per_row_pdf_links fell back to pure
    row-proximity with no check on the link's own column at all, so this
    floor plan link got attached as brochure_link. Fixed via
    _floor_plan_column_x_range acting as a negative signal.
    """

    def test_a_lone_floor_plan_link_is_never_attached_as_brochure_link(self):
        # Two rows, matching MIN_UNITS_FOR_PER_ROW_LINKS - with only one
        # unit, the whole page would be skipped before ever reaching the
        # row-matching logic this test means to exercise, and brochure_link
        # would trivially stay None regardless of whether the fix works.
        pdf_path = _make_floor_plan_only_pdf([
            ("1st", 759, "https://example.com/floorplan-1"),
            ("2nd", 1003, "https://example.com/floorplan-2"),
        ])
        try:
            units = [
                {"floor_unit": "1st", "size_sqft": 759, "brochure_link": None, "page_index": 0},
                {"floor_unit": "2nd", "size_sqft": 1003, "brochure_link": None, "page_index": 0},
            ]
            extract._attach_per_row_pdf_links(pdf_path, units)

            self.assertIsNone(units[0]["brochure_link"])
            self.assertIsNone(units[1]["brochure_link"])
        finally:
            pdf_path.unlink(missing_ok=True)

    def test_the_single_word_floorplan_header_is_also_recognized(self):
        pdf_path = _make_floor_plan_only_pdf(
            [
                ("1st", 759, "https://example.com/floorplan-1"),
                ("2nd", 1003, "https://example.com/floorplan-2"),
            ],
            header_text="Floorplan",
        )
        try:
            units = [
                {"floor_unit": "1st", "size_sqft": 759, "brochure_link": None, "page_index": 0},
                {"floor_unit": "2nd", "size_sqft": 1003, "brochure_link": None, "page_index": 0},
            ]
            extract._attach_per_row_pdf_links(pdf_path, units)

            self.assertIsNone(units[0]["brochure_link"])
            self.assertIsNone(units[1]["brochure_link"])
        finally:
            pdf_path.unlink(missing_ok=True)

    def test_this_never_affects_a_page_that_genuinely_has_a_brochure_column_too(self):
        # Regression guard: the negative signal must only ever apply when
        # _brochure_column_x_range found nothing at all - a real Brochure
        # column sitting alongside a Floor Plan column (already covered by
        # MultiLinkColumnDisambiguationTests) must keep working exactly as
        # before. Two rows, matching MIN_UNITS_FOR_PER_ROW_LINKS.
        pdf_path = _make_multi_link_column_pdf([
            ("1st", 759, "https://example.com/brochure-1", "https://example.com/floorplan-1", None),
            ("2nd", 1003, "https://example.com/brochure-2", "https://example.com/floorplan-2", None),
        ])
        try:
            units = [
                {"floor_unit": "1st", "size_sqft": 759, "brochure_link": None, "page_index": 0},
                {"floor_unit": "2nd", "size_sqft": 1003, "brochure_link": None, "page_index": 0},
            ]
            extract._attach_per_row_pdf_links(pdf_path, units)

            self.assertEqual(units[0]["brochure_link"], "https://example.com/brochure-1")
            self.assertEqual(units[1]["brochure_link"], "https://example.com/brochure-2")
        finally:
            pdf_path.unlink(missing_ok=True)


class FloorPlanColumnXRangeTests(unittest.TestCase):
    def test_finds_the_single_word_header(self):
        words = [(240, 20, 300, 30, "Floorplan", 0, 0, 0)]
        x_range = extract._floor_plan_column_x_range(words)
        self.assertIsNotNone(x_range)

    def test_finds_the_two_word_header(self):
        words = [
            (240, 20, 270, 30, "Floor", 0, 0, 0),
            (272, 20, 300, 30, "Plan", 0, 0, 1),
        ]
        x_range = extract._floor_plan_column_x_range(words)
        self.assertIsNotNone(x_range)
        self.assertTrue(x_range[0] <= 240)
        self.assertTrue(x_range[1] >= 300)

    def test_none_when_no_floor_plan_header_present(self):
        words = [(50, 20, 100, 30, "Brochure", 0, 0, 0)]
        self.assertIsNone(extract._floor_plan_column_x_range(words))

    def test_floor_and_plan_on_different_rows_do_not_count(self):
        words = [
            (240, 20, 270, 30, "Floor", 0, 0, 0),
            (272, 200, 300, 210, "Plan", 0, 5, 0),
        ]
        self.assertIsNone(extract._floor_plan_column_x_range(words))


class MultiLinkColumnDisambiguationTests(unittest.TestCase):
    """
    Grounded directly in a real Kitt's Availability PDF the user supplied
    (not committed into this repo - real provider data): every row there
    has up to three separate link columns (Link to Brochure, Floor Plan,
    High Res Images), all landing at the same row y-position. Without
    column-based disambiguation, _attach_per_row_pdf_links would see 3
    "nearby" links per row and treat every single row as ambiguous,
    attaching nothing at all - these confirm the fix.
    """

    def test_picks_the_brochure_column_link_not_floor_plan_or_high_res(self):
        pdf_path = _make_multi_link_column_pdf([
            ("1st", 759, "https://example.com/brochure-1", "https://example.com/floorplan-1", "https://example.com/highres-1"),
            ("2nd", 1003, "https://example.com/brochure-2", "https://example.com/floorplan-2", "https://example.com/highres-2"),
        ])
        try:
            units = [
                {"floor_unit": "1st", "size_sqft": 759, "brochure_link": None, "page_index": 0},
                {"floor_unit": "2nd", "size_sqft": 1003, "brochure_link": None, "page_index": 0},
            ]
            extract._attach_per_row_pdf_links(pdf_path, units)

            self.assertEqual(units[0]["brochure_link"], "https://example.com/brochure-1")
            self.assertEqual(units[1]["brochure_link"], "https://example.com/brochure-2")
        finally:
            pdf_path.unlink(missing_ok=True)

    def test_row_missing_the_brochure_link_gets_nothing_even_if_other_columns_have_links(self):
        pdf_path = _make_multi_link_column_pdf([
            ("1st", 759, None, "https://example.com/floorplan-1", "https://example.com/highres-1"),
            ("2nd", 1003, "https://example.com/brochure-2", "https://example.com/floorplan-2", "https://example.com/highres-2"),
        ])
        try:
            units = [
                {"floor_unit": "1st", "size_sqft": 759, "brochure_link": None, "page_index": 0},
                {"floor_unit": "2nd", "size_sqft": 1003, "brochure_link": None, "page_index": 0},
            ]
            extract._attach_per_row_pdf_links(pdf_path, units)

            self.assertIsNone(units[0]["brochure_link"])
            self.assertEqual(units[1]["brochure_link"], "https://example.com/brochure-2")
        finally:
            pdf_path.unlink(missing_ok=True)


class BrochureColumnXRangeTests(unittest.TestCase):
    def test_finds_the_topmost_brochure_word(self):
        words = [
            (240, 20, 300, 30, "Brochure", 0, 0, 0),  # header, topmost
            (100, 300, 250, 310, "brochure", 0, 5, 0),  # coincidental mention lower down - must be ignored
        ]
        x_range = extract._brochure_column_x_range(words)
        self.assertIsNotNone(x_range)
        self.assertTrue(x_range[0] <= 240)

    def test_none_when_no_brochure_word_present(self):
        words = [(50, 20, 100, 30, "Floor", 0, 0, 0)]
        self.assertIsNone(extract._brochure_column_x_range(words))


class PageUriLinksTests(unittest.TestCase):
    def test_internal_navigation_links_are_excluded(self):
        pdf_path = _make_test_pdf([("3rd Floor", 5000, "https://example.com/unit-a")])
        try:
            doc = fitz.open(pdf_path)
            page = doc[0]
            # A GOTO (internal nav) link, same shape as the page-turn arrows
            # seen throughout the real city-tower-brochure.pdf.
            page.insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(0, 0, 10, 10), "page": 0})

            links = extract._page_uri_links(page)

            self.assertEqual(len(links), 1)
            self.assertEqual(links[0]["uri"], "https://example.com/unit-a")
            doc.close()
        finally:
            pdf_path.unlink(missing_ok=True)


class IsCaptionSizedTests(unittest.TestCase):
    def test_small_rect_is_caption_sized(self):
        self.assertTrue(extract._is_caption_sized(fitz.Rect(0, 0, 50, 12)))

    def test_tall_rect_is_not_caption_sized(self):
        self.assertFalse(extract._is_caption_sized(fitz.Rect(0, 0, 50, 75)))


class FindUnitRowYTests(unittest.TestCase):
    WORDS = [
        (50, 100, 90, 110, "3rd", 0, 0, 0),
        (95, 100, 130, 110, "Floor", 0, 0, 1),
        (150, 100, 180, 110, "5000", 0, 0, 2),
        (50, 200, 90, 210, "5th", 1, 0, 0),
        (95, 200, 130, 210, "Floor", 1, 0, 1),
        (150, 200, 180, 210, "7500", 1, 0, 2),
    ]

    def test_size_sqft_match_wins_when_unique(self):
        y = extract._find_unit_row_y(self.WORDS, "3rd Floor", 5000)
        self.assertAlmostEqual(y, 105.0)

    def test_falls_back_to_floor_unit_tokens_when_size_sqft_is_none(self):
        y = extract._find_unit_row_y(self.WORDS, "5th Floor", None)
        self.assertAlmostEqual(y, 205.0)

    def test_returns_none_when_neither_anchor_is_locatable(self):
        y = extract._find_unit_row_y(self.WORDS, "Ground Floor", 9999)
        self.assertIsNone(y)

    def test_returns_none_when_both_anchors_are_blank(self):
        self.assertIsNone(extract._find_unit_row_y(self.WORDS, None, None))


class RealCityTowerBrochureGroundingTests(unittest.TestCase):
    """
    Grounds the detection heuristic against the actual real-world negative
    case named in the task: city-tower-brochure.pdf's first page has three
    genuine URI links (a large GPE logo, a caption-sized "portfolio/city-
    tower" link, and a map thumbnail link) - only ONE of which is caption-
    sized. MIN_LINKS_FOR_PER_ROW_LINKS requires at least two, so this real
    page must never qualify for per-row attachment, regardless of how many
    units Gemini places on it - confirmed directly against the real file's
    actual link geometry, no Gemini call needed for this check.
    """

    def test_page_0_has_only_one_caption_sized_link(self):
        doc = fitz.open(SAMPLE_DOCS / "city-tower-brochure.pdf")
        try:
            page = doc[0]
            caption_sized = [l for l in extract._page_uri_links(page) if extract._is_caption_sized(l["rect"])]
            self.assertEqual(len(caption_sized), 1)
            self.assertLess(len(caption_sized), extract.MIN_LINKS_FOR_PER_ROW_LINKS)
        finally:
            doc.close()


class ImagesFromPngPagesMimeTypeSniffingTests(unittest.TestCase):
    """
    images_from_png_pages' own mime-type sniffing (see that function's own
    docstring) - canva_renderer/app.py adaptively re-encodes a large/photo-
    dense capture as JPEG instead of PNG to stay under Cloud Run's own
    32MB response-size limit, with no new field in the renderer's JSON
    response this app has to know about - the actual mime_type Gemini
    receives is read from the real bytes' own magic number instead.
    """

    _PNG_BYTES = b"\x89PNG\r\n\x1a\n rest of a real PNG"
    _JPEG_BYTES = b"\xff\xd8\xff\xe0 rest of a real JPEG"

    def test_png_bytes_get_the_png_mime_type(self):
        parts = extract.images_from_png_pages([self._PNG_BYTES])
        self.assertEqual(parts[0].inline_data.mime_type, "image/png")

    def test_jpeg_bytes_get_the_jpeg_mime_type(self):
        parts = extract.images_from_png_pages([self._JPEG_BYTES])
        self.assertEqual(parts[0].inline_data.mime_type, "image/jpeg")

    def test_format_is_sniffed_once_from_the_first_page_and_applied_to_every_page(self):
        # A whole render's pages are always all the same format (the
        # renderer picks one format for the entire response, never mixed
        # per page) - confirming every page gets the SAME mime_type from
        # just the first page's own bytes, never a per-page re-sniff.
        parts = extract.images_from_png_pages([self._JPEG_BYTES, self._JPEG_BYTES, self._JPEG_BYTES])
        self.assertTrue(all(p.inline_data.mime_type == "image/jpeg" for p in parts))

    def test_empty_list_never_raises(self):
        self.assertEqual(extract.images_from_png_pages([]), [])

    def test_page_data_and_order_are_unaffected_by_the_format_sniff(self):
        parts = extract.images_from_png_pages([self._JPEG_BYTES, self._PNG_BYTES])
        self.assertEqual(parts[0].inline_data.data, self._JPEG_BYTES)
        self.assertEqual(parts[1].inline_data.data, self._PNG_BYTES)


class ImagesFromPngPagesLinkCandidatesTests(unittest.TestCase):
    """images_from_png_pages' own page_links param - interleaves a text
    Part describing a page's own real link candidates immediately after
    that page's own image, for the paste-a-link Canva/Pitch flow (see
    canva_renderer/app.py's own _page_link_candidates)."""

    _PNG_BYTES = b"\x89PNG\r\n\x1a\n rest of a real PNG"

    def test_default_page_links_none_behaves_exactly_like_before_this_existed(self):
        parts = extract.images_from_png_pages([self._PNG_BYTES, self._PNG_BYTES])
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(hasattr(p, "inline_data") for p in parts))

    def test_a_page_with_links_gets_a_text_part_right_after_its_image(self):
        page_links = [[{"href": "https://colliers.com/kingsland-house", "text": "LINK TO BROCHURE"}]]
        parts = extract.images_from_png_pages([self._PNG_BYTES], page_links=page_links)

        self.assertEqual(len(parts), 2)
        self.assertTrue(hasattr(parts[0], "inline_data"))  # the image, first
        self.assertIsInstance(parts[1], str)  # the link text, immediately after
        self.assertIn("'LINK TO BROCHURE' -> https://colliers.com/kingsland-house", parts[1])

    def test_a_page_with_no_links_gets_no_text_part_at_all(self):
        page_links = [[]]
        parts = extract.images_from_png_pages([self._PNG_BYTES], page_links=page_links)
        self.assertEqual(len(parts), 1)  # just the image - no empty/noise text Part

    def test_multiple_pages_each_get_their_own_links_right_after_their_own_image(self):
        page_links = [
            [{"href": "https://example.com/a.pdf", "text": "Brochure"}],
            [],
            [{"href": "https://example.com/c.pdf", "text": "Building C"}],
        ]
        parts = extract.images_from_png_pages(
            [self._PNG_BYTES, self._PNG_BYTES, self._PNG_BYTES], page_links=page_links,
        )

        # image, text, image, (no text for the middle page), image, text
        self.assertEqual(len(parts), 5)
        self.assertTrue(hasattr(parts[0], "inline_data"))
        self.assertIn("Brochure", parts[1])
        self.assertTrue(hasattr(parts[2], "inline_data"))
        self.assertTrue(hasattr(parts[3], "inline_data"))
        self.assertIn("Building C", parts[4])

    def test_multiple_links_on_one_page_are_all_included(self):
        page_links = [[
            {"href": "https://example.com/brochure.pdf", "text": "LINK TO BROCHURE"},
            {"href": "mailto:sales@example.com", "text": "BOOK A VIEWING"},
        ]]
        parts = extract.images_from_png_pages([self._PNG_BYTES], page_links=page_links)

        self.assertIn("LINK TO BROCHURE", parts[1])
        self.assertIn("BOOK A VIEWING", parts[1])


class ExtractFromPngPagesTests(unittest.TestCase):
    """extract_from_png_pages - the paste-a-link (Canva/Pitch) counterpart
    to extract(), operating on already-rendered page images rather than a
    real PDF file on disk. Gemini itself is mocked wholesale (call_gemini),
    same principle as every other test in this module - no real rendering
    or network call involved."""

    _PNG_BYTES = b"\x89PNG\r\n\x1a\n rest of a real PNG"

    def test_builds_rows_from_gemini_output_same_as_extract(self):
        raw = {
            "provider": "Colliers", "contacts": None,
            "units": [{"building": "Kingsland House", "floor_unit": "3rd", "brochure_link": None}],
        }
        with patch("extract.get_client", return_value="fake-client"), \
                patch("extract.call_gemini", return_value=raw) as mock_call_gemini:
            rows = extract.extract_from_png_pages([self._PNG_BYTES], original_filename="colliers.pdf")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].building, "Kingsland House")
        self.assertEqual(rows[0].source_file, "colliers.pdf")
        # No genuine per-unit link and no page_links given - correctly
        # None, the (former) PDF-fallback default was removed entirely.
        self.assertIsNone(rows[0].brochure_link)
        mock_call_gemini.assert_called_once()

    def test_development_name_from_gemini_output_flows_through_to_every_row(self):
        # Real Regent's Wharf shape: one brochure-level development_name,
        # shared across every unit regardless of which of the campus'
        # several separately-named buildings it's in (see extract.py's own
        # PROMPT and schema.ListingRow.development_name's own docstring).
        raw = {
            "provider": "Colliers", "contacts": None, "development_name": "Regent's Wharf",
            "units": [
                {"building": "The Canal Building", "floor_unit": "1st", "brochure_link": None},
                {"building": "Thorley Works", "floor_unit": "2nd", "brochure_link": None},
            ],
        }
        with patch("extract.get_client", return_value="fake-client"), \
                patch("extract.call_gemini", return_value=raw):
            rows = extract.extract_from_png_pages([self._PNG_BYTES], original_filename="regents-wharf.pdf")

        self.assertEqual(rows[0].development_name, "Regent's Wharf")
        self.assertEqual(rows[1].development_name, "Regent's Wharf")

    def test_no_development_name_in_gemini_output_leaves_it_null(self):
        # Regression: a raw payload with no "development_name" key at all
        # (every existing test/real single-building brochure) must leave
        # the field null, never raise.
        raw = {"provider": None, "contacts": None, "units": [{"building": "X", "brochure_link": None}]}
        with patch("extract.get_client", return_value="fake-client"), \
                patch("extract.call_gemini", return_value=raw):
            rows = extract.extract_from_png_pages([self._PNG_BYTES], original_filename="pasted.pdf")

        self.assertIsNone(rows[0].development_name)

    def test_page_links_are_threaded_through_to_the_gemini_call(self):
        raw = {"provider": None, "contacts": None, "units": []}
        page_links = [[{"href": "https://example.com/a.pdf", "text": "27-29 Gloucester Place"}]]
        with patch("extract.get_client", return_value="fake-client"), \
                patch("extract.call_gemini", return_value=raw) as mock_call_gemini:
            extract.extract_from_png_pages(
                [self._PNG_BYTES], original_filename="deck.pdf", page_links=page_links,
            )

        called_parts = mock_call_gemini.call_args.args[2]
        joined = " ".join(p for p in called_parts if isinstance(p, str))
        self.assertIn("27-29 Gloucester Place", joined)
        self.assertIn("https://example.com/a.pdf", joined)

    def test_a_genuine_per_unit_link_from_gemini_is_kept_not_overridden(self):
        raw = {
            "provider": "Colliers", "contacts": None,
            "units": [{
                "building": "27-29 Gloucester Place", "floor_unit": "2nd",
                "brochure_link": "https://blob.example.com/gloucester.pdf",
            }],
        }
        with patch("extract.get_client", return_value="fake-client"), \
                patch("extract.call_gemini", return_value=raw):
            rows = extract.extract_from_png_pages([self._PNG_BYTES], original_filename="deck.pdf")

        self.assertEqual(rows[0].brochure_link, "https://blob.example.com/gloucester.pdf")

    def test_no_genuine_link_and_no_page_links_gets_no_fallback_link(self):
        # Renamed from test_no_original_filename_or_brochure_url_falls_
        # back_to_the_filename_itself - that name/assertion described the
        # removed PDF-fallback default, which used to default all the way
        # down to the bare filename in this shape specifically.
        raw = {"provider": None, "contacts": None, "units": [{"building": "X", "brochure_link": None}]}
        with patch("extract.get_client", return_value="fake-client"), \
                patch("extract.call_gemini", return_value=raw):
            rows = extract.extract_from_png_pages([self._PNG_BYTES], original_filename="pasted.pdf")

        self.assertIsNone(rows[0].brochure_link)

    def test_page_indices_attribute_mirrors_each_units_own_page_index(self):
        # app._propagate_validated_links_within_page (Upload page) relies on
        # this parallel attribute to group rows by originating page - never
        # a real ListingRow field (page_index isn't part of the persisted
        # schema, see _rows_from_raw's own docstring).
        raw = {
            "provider": None, "contacts": None,
            "units": [
                {"building": "A", "floor_unit": "1st", "brochure_link": None, "page_index": 2},
                {"building": "A", "floor_unit": "2nd", "brochure_link": None, "page_index": 2},
                {"building": "B", "floor_unit": "1st", "brochure_link": None},  # no page_index at all
            ],
        }
        with patch("extract.get_client", return_value="fake-client"), \
                patch("extract.call_gemini", return_value=raw):
            rows = extract.extract_from_png_pages([self._PNG_BYTES], original_filename="deck.pdf")

        self.assertEqual(len(rows), 3)  # still a plain list a caller can iterate/index/len() normally
        self.assertEqual(rows.page_indices, [2, 2, None])

    def test_extract_itself_has_no_page_indices_attribute(self):
        # extract() (the real-PDF-upload path) never needs a row's own
        # page_index once _attach_per_row_pdf_links has already consumed
        # and popped it - keeps returning a bare list[ListingRow].
        raw = {"provider": None, "contacts": None, "units": [{"building": "X", "brochure_link": None}]}
        with patch("extract.extract_raw_units", return_value=raw), \
                patch("extract._attach_per_row_pdf_links"):
            rows = extract.extract(Path("irrelevant.pdf"))

        self.assertEqual(len(rows), 1)
        self.assertFalse(hasattr(rows, "page_indices"))


class PastedLinkContactsNeverFallBackToTheSharedDeckTests(unittest.TestCase):
    """
    Real, confirmed production bug this closes: a shared multi-property
    Colliers Canva deck (21 real buildings, 13 genuinely different
    individually-linked brochures) has one generic "team" contact block on
    its own closing page - raw["contacts"] here. Gemini's own per-unit
    "contacts" pick came back blank for virtually every building (the deck's
    own text never names a distinct agent per building), so under the OLD
    behavior every row fell back to that one shared value regardless of
    which of the 13 genuinely different documents its own brochure_link
    would later resolve to - and since brochure_enrichment._apply_units_to_
    row's own contacts fill only ever applies to a genuinely BLANK field,
    the correct, later-derived per-row value (from each row's own real,
    separately-fetched brochure) could never actually land once this wrong
    value was already baked in here.

    extract_from_png_pages (paste-a-link/Canva-Pitch) must leave contacts
    genuinely blank for such a unit instead - see _rows_from_raw's own
    document_wide_contacts_is_row_own_document docstring. extract() (a real,
    single PDF/email upload, where raw genuinely IS that row's own one
    document) is a completely different case and must keep falling back to
    raw["contacts"] exactly as before - see PerUnitContactsTests above,
    the regression guard for that flow; if any of ITS tests start failing
    because of this fix, the fix is scoped wrong.
    """

    _PNG_BYTES = b"\x89PNG\r\n\x1a\n rest of a real PNG"

    def test_a_unit_with_no_contact_of_its_own_stays_blank_not_the_shared_deck_value(self):
        raw = {
            "provider": "Colliers",
            "contacts": "Joseph Mishon, joseph.mishon@colliers.com; Chloe Hechle, chloe.hechle@colliers.com",
            "units": [
                {"building": "Mainframe", "floor_unit": "3rd", "brochure_link": None, "contacts": None},
                {"building": "27-29 Gloucester Place", "floor_unit": "2nd", "brochure_link": None, "contacts": None},
            ],
        }
        with patch("extract.get_client", return_value="fake-client"), \
                patch("extract.call_gemini", return_value=raw):
            rows = extract.extract_from_png_pages([self._PNG_BYTES], original_filename="deck.pdf")

        self.assertIsNone(rows[0].contacts)
        self.assertIsNone(rows[1].contacts)

    def test_a_units_own_genuine_per_unit_contact_still_wins_unaffected_by_this_fix(self):
        raw = {
            "provider": "Colliers", "contacts": "Shared Team, team@colliers.com",
            "units": [
                {
                    "building": "The Met Building", "floor_unit": "12th", "brochure_link": None,
                    "contacts": "Joseph Mishon, joseph.mishon@colliers.com",
                },
            ],
        }
        with patch("extract.get_client", return_value="fake-client"), \
                patch("extract.call_gemini", return_value=raw):
            rows = extract.extract_from_png_pages([self._PNG_BYTES], original_filename="deck.pdf")

        self.assertEqual(rows[0].contacts, "Joseph Mishon, joseph.mishon@colliers.com")


class BuildingAndPropertyFeaturesMergeTests(unittest.TestCase):
    """
    _rows_from_raw now folds raw["building_features"]/raw["property_
    features"] into each unit's own special_features (see _match_building_
    features/_rows_from_raw's own combine, both in extract.py) - the
    PDF/Canva-sourced counterpart to brochure_enrichment.py's own enrich_row
    combine, which never runs for this source type at all (see app.py's own
    is_spreadsheet_source/is_email_source gating - "Never for PDF... already
    extracted from the actual brochure itself"). Confirmed real gap: a real
    Colliers multi-page campus brochure's own per-building/per-property
    descriptive text was extracted into raw["building_features"]/raw
    ["property_features"] by Gemini and then silently discarded - neither
    ever reached any row's special_features.
    """

    def _extract(self, raw):
        with patch("extract.extract_raw_units", return_value=raw), patch("extract._attach_per_row_pdf_links"):
            return extract.extract(Path("doc.pdf"), original_filename="doc.pdf")

    def test_building_features_text_is_appended_to_matching_unit(self):
        raw = {
            "provider": "Colliers", "contacts": None,
            "building_features": [
                {"building": "The Canal Building", "features": "original steel columns, exposed beams"},
            ],
            "units": [
                {"building": "The Canal Building", "floor_unit": "1st Floor", "special_features": "Cat A fit-out"},
                {"building": "The Mill", "floor_unit": "2nd Floor", "special_features": "Cat A fit-out"},
            ],
        }
        rows = self._extract(raw)
        self.assertEqual(rows[0].special_features, "Cat A fit-out; original steel columns, exposed beams")
        # The Mill has no building_features entry of its own - unaffected.
        self.assertEqual(rows[1].special_features, "Cat A fit-out")

    def test_property_features_is_appended_to_every_unit(self):
        raw = {
            "provider": "Colliers", "contacts": None,
            "property_features": "WiredScore Platinum; 160 cycle spaces",
            "units": [
                {"building": "The Canal Building", "floor_unit": "1st Floor", "special_features": "Cat A fit-out"},
                {"building": "The Mill", "floor_unit": "2nd Floor", "special_features": None},
            ],
        }
        rows = self._extract(raw)
        self.assertEqual(rows[0].special_features, "Cat A fit-out; WiredScore Platinum; 160 cycle spaces")
        self.assertEqual(rows[1].special_features, "WiredScore Platinum; 160 cycle spaces")

    def test_short_but_non_blank_special_features_still_gets_building_and_property_text(self):
        # The same "never gate on blank" trap already reasoned through for
        # brochure_enrichment.py's own combine (see its enrich_row
        # docstring) - a short, genuinely non-blank value must not skip
        # this enrichment, only an actually-blank one would ever look
        # exempt.
        raw = {
            "provider": "Colliers", "contacts": None,
            "property_features": "BREEAM Excellent",
            "building_features": [{"building": "The Mill", "features": "Grade II listed"}],
            "units": [
                {"building": "The Mill", "floor_unit": "3rd Floor", "special_features": "Lift access"},
            ],
        }
        rows = self._extract(raw)
        self.assertEqual(rows[0].special_features, "Lift access; Grade II listed; BREEAM Excellent")

    def test_no_building_or_property_features_leaves_special_features_unaffected(self):
        raw = {
            "provider": "Colliers", "contacts": None,
            "units": [
                {"building": "Henly House", "floor_unit": "1st Floor", "special_features": "2 meeting rooms"},
                {"building": "Henly House", "floor_unit": "2nd Floor", "special_features": None},
            ],
        }
        rows = self._extract(raw)
        self.assertEqual(rows[0].special_features, "2 meeting rooms")
        self.assertIsNone(rows[1].special_features)

    def test_building_name_matching_is_case_and_whitespace_tolerant(self):
        raw = {
            "provider": "Colliers", "contacts": None,
            "building_features": [{"building": " the canal building ", "features": "canalside frontage"}],
            "units": [
                {"building": "THE CANAL BUILDING", "floor_unit": "1st Floor", "special_features": None},
            ],
        }
        rows = self._extract(raw)
        self.assertEqual(rows[0].special_features, "canalside frontage")

    def test_ambiguous_building_features_match_is_never_guessed(self):
        # Two building_features entries normalizing to the same key is
        # treated as ambiguous and skipped entirely - same "incorrect
        # enrichment is worse than a blank field" policy as brochure_
        # enrichment.py's own _match_building_feature.
        raw = {
            "provider": "Colliers", "contacts": None,
            "building_features": [
                {"building": "The Mill", "features": "first entry"},
                {"building": "the mill", "features": "second entry"},
            ],
            "units": [
                {"building": "The Mill", "floor_unit": "1st Floor", "special_features": "Cat A fit-out"},
            ],
        }
        rows = self._extract(raw)
        self.assertEqual(rows[0].special_features, "Cat A fit-out")


class PerUnitContactsTests(unittest.TestCase):
    """
    _rows_from_raw resolves each unit's own "contacts" (see PROMPT's own
    per-unit contacts field) PREFERRED over the document-wide raw["contacts"]
    - the document-wide value is used only as a fallback for a unit that
    states none of its own. Real, confirmed gap this closes: a real multi-
    building Colliers deck names a genuinely DIFFERENT agent on different
    buildings' own pages, which used to be flattened into one shared
    raw["contacts"] value applied identically to every row regardless of
    which building it actually described.
    """

    def _extract(self, raw):
        with patch("extract.extract_raw_units", return_value=raw), patch("extract._attach_per_row_pdf_links"):
            return extract.extract(Path("doc.pdf"), original_filename="doc.pdf")

    def test_each_unit_with_its_own_contact_gets_a_different_value(self):
        raw = {
            "provider": "Colliers", "contacts": "Jane Doe, jane@colliers.com",
            "units": [
                {"building": "The Canal Building", "floor_unit": "1st Floor",
                 "contacts": "Alice Smith, alice@colliers.com"},
                {"building": "The Mill", "floor_unit": "2nd Floor",
                 "contacts": "Bob Jones, bob@colliers.com"},
            ],
        }
        rows = self._extract(raw)
        self.assertEqual(rows[0].contacts, "Alice Smith, alice@colliers.com")
        self.assertEqual(rows[1].contacts, "Bob Jones, bob@colliers.com")

    def test_a_unit_with_no_contact_of_its_own_falls_back_to_document_wide(self):
        raw = {
            "provider": "Colliers", "contacts": "Jane Doe, jane@colliers.com",
            "units": [
                {"building": "The Canal Building", "floor_unit": "1st Floor",
                 "contacts": "Alice Smith, alice@colliers.com"},
                {"building": "The Mill", "floor_unit": "2nd Floor", "contacts": None},
            ],
        }
        rows = self._extract(raw)
        self.assertEqual(rows[0].contacts, "Alice Smith, alice@colliers.com")
        self.assertEqual(rows[1].contacts, "Jane Doe, jane@colliers.com")

    def test_single_contact_document_behaves_exactly_as_before(self):
        raw = {
            "provider": "Colliers", "contacts": "Jane Doe, jane@colliers.com",
            "units": [
                {"building": "Henly House", "floor_unit": "1st Floor"},
                {"building": "Henly House", "floor_unit": "4th Floor"},
            ],
        }
        rows = self._extract(raw)
        self.assertEqual(rows[0].contacts, "Jane Doe, jane@colliers.com")
        self.assertEqual(rows[1].contacts, "Jane Doe, jane@colliers.com")


if __name__ == "__main__":
    unittest.main()
