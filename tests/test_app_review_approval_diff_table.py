"""
Regression tests for the post-approval "View what changed" panel's own
diff display (pages/2_Review_and_Master.py's _render_approval_
confirmation -> _render_compact_diff_table) - one continuous Field/
Current/New HTML table for the WHOLE panel, with each property's own name
as a full-width, bold, shaded divider row, rather than plain "Field: old
-> new" text lines under a separate bold heading per property.

Seeds st.session_state["last_approval"]/["show_approval_details"]
directly rather than driving a real Approve click followed by clicking
"View what changed" - that toggle button is ALREADY broken in the
unmodified codebase (confirmed directly, unrelated to this change): its
own click handler calls st.rerun(), and _render_full_master_view pops
"last_approval" from session_state (by design, so the confirmation only
flashes once) - so by the time the rerun that click triggers runs,
last_approval is already gone and the whole confirmation (including the
toggle button itself) silently disappears. Seeding session_state directly
exercises the real _render_compact_diff_table call path without needing
that separately-broken button to work.

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_page_restructure.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_approval_diff_table -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamlit.testing.v1 import AppTest

BASE = Path(__file__).resolve().parent.parent


class IsolatedCwdTestCase(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()


def _seeded_review_page(last_approval: dict):
    at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
    at.session_state["last_approval"] = last_approval
    at.session_state["show_approval_details"] = True
    at.run()
    return at


class PostApprovalDiffTableTests(IsolatedCwdTestCase):
    def test_one_continuous_table_spans_both_properties_with_their_own_divider_rows(self):
        last_approval = {
            "updated_count": 2,
            "new_count": 0,
            "removed_count": 0,
            "diff_rows": [
                {"property": "44 Pentonville Road — MetSpace", "field": "size_sqft", "old": 1000.0, "new": 1500.0},
                {"property": "50 Pentonville Road — MetSpace", "field": "size_sqft", "old": 2000.0, "new": 2500.0},
            ],
            "new_labels": [],
            "removed_labels": [],
            "version_path": None,
        }
        at = _seeded_review_page(last_approval)
        self.assertFalse(at.exception)

        markdown_text = "".join(m.value or "" for m in at.markdown)
        # ONE continuous table for the whole panel - never one per property.
        self.assertEqual(markdown_text.count('<table class="diff-table">'), 1)
        self.assertEqual(markdown_text.count('<tr class="diff-table-divider">'), 2)
        self.assertIn('<td colspan="3">44 Pentonville Road — MetSpace</td>', markdown_text)
        self.assertIn('<td colspan="3">50 Pentonville Road — MetSpace</td>', markdown_text)
        self.assertIn("<td>Size</td>", markdown_text)
        self.assertIn("<td>1,500 sq ft</td>", markdown_text)
        self.assertIn("<td>2,500 sq ft</td>", markdown_text)

    def test_no_table_at_all_when_details_are_collapsed(self):
        last_approval = {
            "updated_count": 1,
            "new_count": 0,
            "removed_count": 0,
            "diff_rows": [
                {"property": "44 Pentonville Road — MetSpace", "field": "size_sqft", "old": 1000.0, "new": 1500.0},
            ],
            "new_labels": [],
            "removed_labels": [],
            "version_path": None,
        }
        at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
        at.session_state["last_approval"] = last_approval
        at.session_state["show_approval_details"] = False
        at.run()
        self.assertFalse(at.exception)

        self.assertNotIn('<table class="diff-table">', "".join(m.value or "" for m in at.markdown))
        self.assertTrue(any(b.label == "View what changed" for b in at.button))

    def test_new_and_removed_property_labels_still_shown_alongside_the_table(self):
        last_approval = {
            "updated_count": 1,
            "new_count": 1,
            "removed_count": 1,
            "diff_rows": [
                {"property": "44 Pentonville Road — MetSpace", "field": "size_sqft", "old": 1000.0, "new": 1500.0},
            ],
            "new_labels": ["9 New Street — MetSpace"],
            "removed_labels": ["1 Old Street — MetSpace"],
            "version_path": None,
        }
        at = _seeded_review_page(last_approval)
        self.assertFalse(at.exception)

        markdown_text = "".join(m.value or "" for m in at.markdown)
        self.assertIn('<table class="diff-table">', markdown_text)
        self.assertIn("🆕 9 New Street — MetSpace — new property", markdown_text)
        self.assertIn("🗑️ 1 Old Street — MetSpace — removed from master", markdown_text)


if __name__ == "__main__":
    unittest.main()
