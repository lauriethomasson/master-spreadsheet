"""
Regression tests for the Review page's combined lat/lng "Location" map row
(see pages/2_Review_and_Master.py's _render_combined_location_row and its
own wiring into _render_field_rows) - replaces two separate Lat/Lng
before/after rows with one embedded st.map (current + proposed pins),
distance/compass direction, raw coordinates in small print, and ONE Apply
checkbox for the pair, since lat and lng are never meaningfully applicable
independently of each other (see master_merge._location_distance_meters'
own docstring: "both always judge lat and lng TOGETHER as one location,
never independently").

Runs from an isolated temporary working directory (never the real repo),
same approach as tests/test_app_review_geocode_consolidation.py.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_app_review_latlng_map -v
"""

import importlib.util
import json
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

# _hex_to_rgba/_CURRENT_LOCATION_PIN_COLOR/_NEW_LOCATION_PIN_COLOR live on
# the page module itself - loaded via importlib (the file's own numeric-
# prefixed name isn't a valid plain `import` target), same idiom already
# used by tests/test_app_review_risky_field_reason.py for this same page.
_spec = importlib.util.spec_from_file_location(
    "review_and_master_page", BASE / "pages" / "2_Review_and_Master.py",
)
review_and_master_page = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("review_and_master_page", review_and_master_page)
_spec.loader.exec_module(review_and_master_page)


class IsolatedCwdTestCase(unittest.TestCase):
    def setUp(self):
        self._original_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._tmp.cleanup()


def _run_review_page():
    at = AppTest.from_file(str(BASE / "pages" / "2_Review_and_Master.py"), default_timeout=30)
    at.run()
    return at


def _deck_gl_elements(at):
    """Every DeckGlJsonChart element on the page (st.map's own underlying
    implementation - see st.map's docstring: "a wrapper around
    st.pydeck_chart") - AppTest has no typed wrapper for it, so it comes
    back as an UnknownElement; identified by its own proto message type
    rather than anything Streamlit-version-fragile."""
    found = []

    def walk(node):
        for child in getattr(node, "children", {}).values():
            if type(child).__name__ == "UnknownElement" and child.proto.DESCRIPTOR.name == "DeckGlJsonChart":
                found.append(child)
            walk(child)

    walk(at.main)
    return found


class CombinedLocationRowTests(IsolatedCwdTestCase):
    """Both lat and lng present together - the ordinary case for a real
    geocode-unverified location change."""

    def _staged_row_with_a_real_latlng_change(self):
        master_writer.write_master([
            ListingRow(
                building="1 Example Street", provider="UNION", floor_unit="1st",
                lat=51.5200000, lng=-0.1000000, property_id="row-latlng",
            ),
        ])
        save_staging_file(
            [
                ListingRow(
                    building="1 Example Street", provider="UNION", floor_unit="1st",
                    # ~530m away - nowhere near master_merge.SAME_LOCATION_
                    # METERS (~50m), so this is never dropped as trivial.
                    lat=51.5158796, lng=-0.1442492, geocode_unverified=True,
                ),
            ],
            "latlng_map_test.xlsx", content_hash="latlng-map-test-hash",
        )
        return _run_review_page()

    def test_renders_one_combined_location_row_not_two_separate_latlng_rows(self):
        at = self._staged_row_with_a_real_latlng_change()
        self.assertFalse(at.exception)

        # No separate Lat/Lng number_input boxes at all - display_utils.
        # render_new_value_input is never called for lat/lng once they're
        # combined (see _render_field_rows' own show_combined_location
        # gating). Identified by key suffix, not label - every field row's
        # own number_input shares the same collapsed "New value" label.
        latlng_boxes = [
            n for n in at.number_input if n.key and (n.key.endswith("_lat_value") or n.key.endswith("_lng_value"))
        ]
        self.assertEqual(latlng_boxes, [])
        self.assertNotIn("**Lat**", [m.value for m in at.markdown])
        self.assertNotIn("**Lng**", [m.value for m in at.markdown])

        # Exactly one "Location" label, one Apply checkbox for it.
        self.assertIn("**Location**", [m.value for m in at.markdown])
        location_checkboxes = [c for c in at.checkbox if c.key and c.key.endswith("_location_apply")]
        self.assertEqual(len(location_checkboxes), 1)

    def test_map_shows_both_current_and_new_pins_with_distinct_colors(self):
        at = self._staged_row_with_a_real_latlng_change()

        deck_elements = _deck_gl_elements(at)
        self.assertEqual(len(deck_elements), 1)
        spec = json.loads(deck_elements[0].proto.json)
        points = spec["layers"][0]["data"]

        self.assertEqual(len(points), 2)
        # pydeck's own getFillColor accessor needs each row's color as a
        # plain [r, g, b, a] int list, not a hex string (confirmed against
        # streamlit's own st.map implementation, which always converts via
        # its own to_int_color_tuple before handing off to the SAME
        # underlying deck.gl ScatterplotLayer this card now builds
        # directly) - see review_and_master_page._hex_to_rgba.
        colors = {tuple(p["color"]) for p in points}
        self.assertEqual(len(colors), 2)  # current and new are visually distinct
        self.assertEqual(
            colors,
            {
                tuple(review_and_master_page._hex_to_rgba(review_and_master_page._CURRENT_LOCATION_PIN_COLOR)),
                tuple(review_and_master_page._hex_to_rgba(review_and_master_page._NEW_LOCATION_PIN_COLOR)),
            },
        )
        coords = {(p["lat"], p["lng"]) for p in points}
        self.assertEqual(coords, {(51.52, -0.1), (51.5158796, -0.1442492)})

    def test_map_uses_a_light_basemap_style_matching_this_apps_theme(self):
        # A bare pydeck.Deck() with no map_style defaults to Carto's DARK-
        # matter style, which doesn't match this app's light theme or what
        # st.map itself rendered before this fix - confirmed real gap,
        # never actually visually inspected when the map was first added.
        at = self._staged_row_with_a_real_latlng_change()

        deck_elements = _deck_gl_elements(at)
        spec = json.loads(deck_elements[0].proto.json)
        self.assertIn("positron", spec["mapStyle"])  # Carto's light style
        self.assertEqual(spec["mapProvider"], "carto")  # still no API key needed

    def test_pins_use_pixel_based_radius_not_meter_based_size(self):
        # Confirmed real bug (fixed here): st.map's own `size` is a real-
        # world-meter radius, which balloons at a tight auto-fit zoom for
        # two CLOSE points (e.g. 33 Cavendish Square, 187m apart) and
        # shrinks to near-invisible at a zoomed-out view for two FAR
        # points (e.g. 44 Paul Street, 1,222m apart) - the same data
        # looking wildly different from card to card. A pydeck.Layer with
        # radiusUnits="pixels" stays a constant screen size regardless of
        # zoom, so this is exactly what has to hold for the fix to work.
        at = self._staged_row_with_a_real_latlng_change()

        deck_elements = _deck_gl_elements(at)
        self.assertEqual(len(deck_elements), 1)
        layer = json.loads(deck_elements[0].proto.json)["layers"][0]

        self.assertEqual(layer["radiusUnits"], "pixels")
        self.assertGreater(layer["radiusMinPixels"], 0)
        self.assertEqual(layer["radiusMinPixels"], layer["radiusMaxPixels"])
        self.assertEqual(layer["getRadius"], layer["radiusMinPixels"])

    def test_raw_coordinates_and_distance_direction_still_shown_in_small_print(self):
        at = self._staged_row_with_a_real_latlng_change()

        captions = [c.value for c in at.caption if c.value and "Current:" in c.value]
        self.assertEqual(len(captions), 1)
        caption = captions[0]
        self.assertIn("51.52", caption)
        self.assertIn("-0.1442492", caption)
        self.assertIn("m", caption)  # a distance figure is present

    def test_checking_the_combined_box_applies_both_lat_and_lng_together(self):
        at = self._staged_row_with_a_real_latlng_change()

        location_checkbox = next(c for c in at.checkbox if c.key and c.key.endswith("_location_apply"))
        location_checkbox.set_value(True).run()
        self.assertFalse(at.exception)

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        self.assertTrue(approve_buttons)
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        row = master_df.loc[master_df["property_id"] == "row-latlng"].iloc[0]
        self.assertEqual(row["lat"], 51.5158796)
        self.assertEqual(row["lng"], -0.1442492)

    def test_leaving_the_combined_box_unchecked_leaves_both_lat_and_lng_untouched(self):
        at = self._staged_row_with_a_real_latlng_change()

        approve_buttons = [b for b in at.button if b.label == "Approve → Master"]
        approve_buttons[0].click().run()
        self.assertFalse(at.exception)

        master_df = master_writer.load_master_as_dataframe()
        row = master_df.loc[master_df["property_id"] == "row-latlng"].iloc[0]
        self.assertEqual(row["lat"], 51.52)
        self.assertEqual(row["lng"], -0.1)


class PinRadiusDistanceIndependenceTests(IsolatedCwdTestCase):
    """
    The actual regression test for the confirmed root cause behind BOTH
    reported symptoms - pins that balloon and nearly merge for a CLOSE
    pair (e.g. 33 Cavendish Square, 187m apart) AND pins that shrink to
    invisible for a FAR pair (e.g. a wrong-geocode jump of several km) are
    the SAME bug (a real-world-meter radius is inherently zoom-dependent
    on screen), just at opposite ends of the same distance range. Stages
    two independent rows at very different distances apart in the SAME
    upload, so both cards render on one page, and confirms both layers
    report the exact same pixel radius - proving radius no longer depends
    on how far apart that particular pair of points happens to be.
    """

    def test_radius_is_identical_for_a_close_pair_and_a_far_outlier_pair(self):
        master_writer.write_master([
            ListingRow(
                building="A", provider="UNION", floor_unit="1st",
                lat=51.5200000, lng=-0.1000000, property_id="row-close",
            ),
            ListingRow(
                building="B", provider="UNION", floor_unit="1st",
                lat=51.5074000, lng=-0.1278000, property_id="row-far",
            ),
        ])
        save_staging_file(
            [
                # ~187m apart - the originally-reported "balloon/merge" case.
                ListingRow(
                    building="A", provider="UNION", floor_unit="1st",
                    lat=51.5171, lng=-0.1450, geocode_unverified=True,
                ),
                # ~6km apart - a wrong-geocode-jump-scale far outlier, the
                # "vanishing dot" case.
                ListingRow(
                    building="B", provider="UNION", floor_unit="1st",
                    lat=51.5560, lng=-0.2000, geocode_unverified=True,
                ),
            ],
            "radius_independence_test.xlsx", content_hash="radius-independence-test-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        deck_elements = _deck_gl_elements(at)
        self.assertEqual(len(deck_elements), 2)
        layers = [json.loads(el.proto.json)["layers"][0] for el in deck_elements]

        radii = {(layer["radiusMinPixels"], layer["radiusMaxPixels"], layer["getRadius"]) for layer in layers}
        self.assertEqual(len(radii), 1)  # identical regardless of distance apart
        (min_px, max_px, get_radius), = radii
        self.assertGreater(min_px, 0)
        self.assertEqual(min_px, max_px)
        self.assertEqual(min_px, get_radius)
        for layer in layers:
            self.assertEqual(layer["radiusUnits"], "pixels")


class SingleCoordinateFieldFallbackTests(IsolatedCwdTestCase):
    """The rare case where only ONE of lat/lng is genuinely present in the
    diff (its own pair unchanged/still missing) - must fall back to the
    ordinary single-field row, never a broken one-point map or a crash."""

    def test_lat_only_diff_falls_back_to_the_ordinary_single_field_row(self):
        master_writer.write_master([
            ListingRow(
                building="1 Example Street", provider="UNION", floor_unit="1st",
                lat=51.5200000, lng=-0.1000000, property_id="row-lat-only",
            ),
        ])
        save_staging_file(
            [
                ListingRow(
                    building="1 Example Street", provider="UNION", floor_unit="1st",
                    lat=51.5158796, lng=-0.1000000,  # lng genuinely unchanged
                    geocode_unverified=True,
                ),
            ],
            "lat_only_test.xlsx", content_hash="lat-only-test-hash",
        )
        at = _run_review_page()
        self.assertFalse(at.exception)

        # _render_field_rows' own field-row number_input is always labeled
        # "New value" (label_visibility="collapsed") regardless of field -
        # identified by its key suffix instead, the same convention
        # tests/test_app_review_latlng_precision.py already uses.
        lat_boxes = [n for n in at.number_input if n.key and n.key.endswith("_lat_value")]
        self.assertEqual(len(lat_boxes), 1)
        self.assertEqual(lat_boxes[0].value, 51.5158796)
        self.assertEqual([n for n in at.number_input if n.key and n.key.endswith("_lng_value")], [])
        self.assertIn("**Lat**", [m.value for m in at.markdown])
        self.assertEqual(_deck_gl_elements(at), [])
        self.assertNotIn("**Location**", [m.value for m in at.markdown])


if __name__ == "__main__":
    unittest.main()
