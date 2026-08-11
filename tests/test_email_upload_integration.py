"""
Real-data integration regression test for a production report: after
uploading a real Knotel availability email, several properties' addresses
came out clearly wrong - not a blank field, but one property's row showing
a DIFFERENT real property's address entirely (e.g. "2 Leonard Circus"
showing "69-77 Paul Street", "16 Bowling Green Lane" showing "Bloomsbury
Place"), and "15 Hatfields" showing its own building name copied into
address_1 instead of its real physical address "Chadwick Court".

Traced end to end against the real email fixture (raw text -> extract_
email.extract() -> geocode_rows() -> canonicalize_providers() ->
save_staging_file() -> load_staging_as_dataframe() -> master_merge.
build_merge_plan()) - confirmed NOT a row/index/concurrency mixing bug:
every affected row has a distinct building name, and geocode_rows' own
grouping never conflates two different buildings for this file. The real
root cause is upstream, in extract_email.py's own PROMPT: its JSON example
template hardcoded "address_1": null / "postcode": null (no "..." or null
alternative shown, unlike every other optional field and unlike extract.py/
extract_spreadsheet_gemini.py's own templates), so Gemini systematically
dropped addresses that were explicitly, literally stated in the email
right next to each building's own name (e.g. "2 Leonard Circus" immediately
followed by its own line "2 Leonard Circus, EC2A 4LW"). With address_1/
postcode left blank, geocode_row's precise Tier 1 (explicit address ->
Geocoding API) never ran, falling through to Tier 2 (a bare building-name
Places Text Search) - which, for these specific building names, returned
genuinely wrong real Google results. Fixed by correcting the template to
match the prose instruction and the other two extraction prompts.

This test makes REAL Gemini and REAL Google Geocoding/Places API calls
against the real email fixture - unlike every other test in this suite,
it is not mocked, and is skipped automatically when GEMINI_API_KEY/
GOOGLE_GEOCODING_API_KEY aren't configured (e.g. in an environment without
network/API access), so it never breaks the full suite for someone
without those keys.

Runs from an isolated temporary working directory (never the real repo).

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_email_upload_integration -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from env_utils import load_dotenv

load_dotenv()

_HAS_API_KEYS = bool(os.environ.get("GEMINI_API_KEY")) and bool(os.environ.get("GOOGLE_GEOCODING_API_KEY"))

BASE = Path(__file__).resolve().parent.parent
REAL_EMAIL = Path(r"C:\Users\julie\Downloads\Fw_ Knotel Availability _ 03_08_2026 (1).eml")


@unittest.skipUnless(_HAS_API_KEYS, "GEMINI_API_KEY/GOOGLE_GEOCODING_API_KEY not configured")
@unittest.skipUnless(REAL_EMAIL.exists(), "real email fixture not present in this environment")
class RealKnotelEmailAddressIntegrityTests(unittest.TestCase):
    """
    Runs the actual real-app pipeline (see app.py's own .eml upload branch
    for the exact call order this mirrors: extract_email.extract ->
    geocode_rows -> canonicalize_providers -> save_staging_file), then
    reloads from staging and runs master_merge.build_merge_plan exactly
    as pages/2_Review_and_Master.py would - proving the address integrity
    holds through the FULL real path, not just the extraction/geocode
    functions in isolation.
    """

    # Run the real pipeline (real Gemini + real Geocoding/Places API calls)
    # exactly ONCE for the whole class, not once per test method - it's
    # slow (a real multi-minute round trip) and fully deterministic given
    # the same fixture, so every test method below just inspects the one
    # shared result instead of repeating the same real network work.
    _by_building = None

    @classmethod
    def setUpClass(cls):
        original_cwd = os.getcwd()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.chdir(tmp.name)
        try:
            cls._by_building = cls._run_real_pipeline()
        finally:
            os.chdir(original_cwd)
            tmp.cleanup()

    @staticmethod
    def _run_real_pipeline():
        import extract_email
        from geocode import geocode_rows
        from master_merge import canonicalize_providers
        from storage.file_store import (
            dataframe_to_listing_rows,
            load_staging_as_dataframe,
            save_staging_file,
        )
        import master_merge
        import pandas as pd
        from schema import ListingRow

        # Real pipeline, real network calls - the exact same functions and
        # order app.py's own .eml upload branch uses.
        rows = extract_email.extract(REAL_EMAIL, original_filename="Fw_ Knotel Availability.eml")
        geocode_rows(rows)
        canonicalize_providers(rows)

        staging_path = save_staging_file(rows, "Fw_ Knotel Availability.eml", content_hash="test-hash")

        # Reload from staging - proves the round trip (xlsx write + read
        # back) doesn't itself alter anything, same as a real page load.
        reloaded_df = load_staging_as_dataframe(staging_path)
        reloaded_rows = dataframe_to_listing_rows(reloaded_df)

        # Build the merge plan against an empty master - every row is
        # "new", so plan.unmatched's own new_row is exactly what Review
        # would show as the AFTER value for each property.
        empty_master_df = pd.DataFrame(columns=list(ListingRow.model_fields.keys()))
        plan = master_merge.build_merge_plan(reloaded_rows, empty_master_df)

        by_building = {}
        for u in plan.unmatched:
            by_building.setdefault(u.new_row.building, []).append(u.new_row)
        return by_building

    def test_2_leonard_circus_keeps_its_own_address(self):
        by_building = self._by_building
        rows = by_building["2 Leonard Circus"]
        self.assertTrue(rows)
        for r in rows:
            self.assertIn("2 Leonard Circus", r.address_1)
            self.assertEqual(r.postcode, "EC2A 4LW")

    def test_rufus_house_keeps_its_own_address(self):
        by_building = self._by_building
        rows = by_building["Rufus House"]
        self.assertTrue(rows)
        for r in rows:
            self.assertIn("Rufus St", r.address_1)
            self.assertEqual(r.postcode, "N1 6PE")

    def test_16_bowling_green_lane_keeps_its_own_address(self):
        by_building = self._by_building
        rows = by_building["16 Bowling Green Lane"]
        self.assertTrue(rows)
        for r in rows:
            self.assertIn("Bowling Green", r.address_1)
            self.assertEqual(r.postcode, "EC1R 0QH")

    def test_15_hatfields_keeps_chadwick_court_as_physical_address(self):
        by_building = self._by_building
        rows = by_building["15 Hatfields"]
        self.assertTrue(rows)
        for r in rows:
            # building and address_1 are different fields with different,
            # both-valid content - the building name must NOT overwrite
            # address_1's own real physical address.
            self.assertEqual(r.building, "15 Hatfields")
            self.assertIn("Chadwick Court", r.address_1)
            self.assertEqual(r.postcode, "SE1 8DJ")

    def test_hallmark_address_matches_the_source_email(self):
        by_building = self._by_building
        rows = by_building["Hallmark"]
        self.assertTrue(rows)
        for r in rows:
            self.assertIn("Fenchurch", r.address_1)
            self.assertEqual(r.postcode, "EC3M 5JE")

    def test_no_row_ever_receives_a_different_propertys_address(self):
        # The core corruption check: cross-referencing every OTHER known
        # property's own real address/postcode against every row here -
        # none of these five should ever show up on a DIFFERENT building's
        # own row.
        by_building = self._by_building
        known_addresses = {
            "2 Leonard Circus": ("2 Leonard Circus", "EC2A 4LW"),
            "Rufus House": ("Rufus St", "N1 6PE"),
            "16 Bowling Green Lane": ("Bowling Green", "EC1R 0QH"),
            "15 Hatfields": ("Chadwick Court", "SE1 8DJ"),
            "Hallmark": ("Fenchurch", "EC3M 5JE"),
        }
        for building, rows in by_building.items():
            if building not in known_addresses:
                continue
            own_fragment, own_postcode = known_addresses[building]
            for r in rows:
                for other_building, (other_fragment, other_postcode) in known_addresses.items():
                    if other_building == building:
                        continue
                    self.assertNotEqual(
                        r.postcode, other_postcode,
                        f"{building}'s row got {other_building}'s own postcode {other_postcode}",
                    )
                    self.assertNotIn(
                        other_fragment, (r.address_1 or ""),
                        f"{building}'s row got {other_building}'s own address fragment {other_fragment!r}",
                    )


if __name__ == "__main__":
    unittest.main()
