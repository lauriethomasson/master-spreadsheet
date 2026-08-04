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

    def test_a_single_corroborating_unit_never_triggers_attachment(self):
        # Two links exist, but only ONE unit is placed on this page -
        # MIN_UNITS_FOR_PER_ROW_LINKS requires at least 2, so even a
        # perfectly-matchable row must be left alone.
        pdf_path = _make_test_pdf([
            ("3rd Floor", 5000, "https://example.com/unit-a"),
            ("5th Floor", 7500, "https://example.com/unit-b"),
        ])
        try:
            units = [{"floor_unit": "3rd Floor", "size_sqft": 5000, "brochure_link": None, "page_index": 0}]
            extract._attach_per_row_pdf_links(pdf_path, units)
            self.assertIsNone(units[0]["brochure_link"])
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


if __name__ == "__main__":
    unittest.main()
