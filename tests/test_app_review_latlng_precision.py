"""
Regression tests for display_utils.render_new_value_input's own lat/lng
display precision (see its LATLNG_FIELDS docstring, and pages/2_Review_and_
Master.py's own _render_edit_property_form for the second call site with
the identical fix) - confirmed real bug: st.number_input defaults to a
HARDCODED "%0.2f" format for any float field with no explicit `format`
given (not derived from `step`, despite appearances - confirmed by reading
streamlit's own source), so a genuinely different lat/lng (the app already
drops anything under master_merge.SAME_LOCATION_METERS/~50m as "not a real
change" before a decision card is ever shown, so a shown card is provably
a real, larger difference) could render identically to the old value, e.g.
"Current: -0.1442492" next to "New: -0.14" - a reviewer reasonably reading
that as no real change at all. Purely a DISPLAY fix: the widget's own
returned VALUE was always already full-precision regardless of `format`
when left untouched.

Runs render_new_value_input directly through a small AppTest fixture
script (real Streamlit widget rendering, not a mock) rather than through
the full Review page - the page's own bundle/risky routing (see
_render_field_rows' own bundle_safe_fields) can route a non-risky float
field to a bundled, no-widget-rendered summary line instead of an
individual "New value" box, which has nothing to do with this display fix
and would make a full-page test of the "unaffected field" case fragile to
unrelated routing decisions.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_latlng_precision -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FIXTURE_SCRIPT = dedent("""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    import display_utils

    display_utils.render_new_value_input(-0.1442492, "float", key="lat_box", field="lat")
    display_utils.render_new_value_input(51.5074000, "float", key="lng_box", field="lng")
    display_utils.render_new_value_input(60.25, "float", key="rent_box", field="rent_psf")
""")


class RenderNewValueInputLatLngPrecisionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        script_path = Path(self._tmp.name) / "latlng_precision_fixture.py"
        script_path.write_text(_FIXTURE_SCRIPT)
        self.at = AppTest.from_file(str(script_path), default_timeout=30)
        self.at.run()

    def tearDown(self):
        self._tmp.cleanup()

    def _box(self, key):
        return next(n for n in self.at.number_input if n.key == key)

    def test_lat_and_lng_render_at_full_precision(self):
        self.assertFalse(self.at.exception)
        lat_box, lng_box = self._box("lat_box"), self._box("lng_box")

        # The fix: format="%.7f" (7 decimal places, ~1cm precision -
        # matching the "Current" column's own plain str(value) display),
        # never Streamlit's own hardcoded "%0.2f" float default.
        self.assertEqual(lat_box.proto.format, "%.7f")
        self.assertEqual(lng_box.proto.format, "%.7f")

        # And the underlying VALUE was always already full-precision
        # regardless of format (this was never a data-correctness bug) -
        # confirmed here too, not just asserted in the commit message.
        self.assertEqual(lat_box.value, -0.1442492)
        self.assertEqual(lng_box.value, 51.5074000)

    def test_a_non_latlng_float_field_keeps_its_exact_existing_format(self):
        # rent_psf must be COMPLETELY unaffected - still Streamlit's own
        # default float format, byte-identical to before this fix.
        rent_box = self._box("rent_box")

        self.assertEqual(rent_box.proto.format, "%0.2f")
        self.assertEqual(rent_box.value, 60.25)


if __name__ == "__main__":
    unittest.main()
