from pydantic import BaseModel
from typing import Optional


class ExtractedFields(BaseModel):
    """Fields Gemini extracts directly from a brochure/document page image.

    Excludes lat/lng (filled in later via geocoding) and source_file
    (set from upload metadata) — those never go in the extraction prompt.
    """

    internal_ref: Optional[str] = None  # mirrors provider — null for landlord-direct brochures with no named agent
    provider: Optional[str] = None      # some brochures are produced directly by a landlord with no presenting agent
    address_1: Optional[str] = None  # not every source (e.g. email listings) states a street address
    postcode: Optional[str] = None   # same — never fabricate, leave null if not stated
    submarket: Optional[str] = None
    building: str
    floor_unit: Optional[str] = None
    size_sqft: Optional[float] = None        # single unit's size — normal case, unchanged
    desks_min: Optional[int] = None          # only populated for a genuine range-group row (e.g. "24-58 desks")
    desks_max: Optional[int] = None          # single unit's desk count, OR the upper bound of a range if desks_min is also set
    rent_pcm: Optional[float] = None
    rent_psf: Optional[float] = None
    brochure_link: Optional[str] = None
    floorplan_link: Optional[str] = None  # a genuinely different document from brochure_link - never a substitute
    special_features: Optional[str] = None
    state_of_space: Optional[str] = None
    contacts: Optional[str] = None  # all contacts combined, one per line/semicolon, each as "Name, email, phone"


class ListingRow(BaseModel):
    """
    Same fields as ExtractedFields, plus lat/lng/source_file which are set
    programmatically rather than by Gemini (see ExtractedFields' docstring).
    """

    internal_ref: Optional[str] = None
    provider: Optional[str] = None
    address_1: Optional[str] = None
    postcode: Optional[str] = None
    source_file: Optional[str] = None  # the real uploaded filename; never required — a phantom/blank row must still validate
    property_id: Optional[str] = None  # assigned once a row lands in the master as a distinct property (master_merge.py); never set by extraction
    lat: Optional[float] = None
    lng: Optional[float] = None
    # Tri-state, never Gemini-set (like lat/lng above) - set by brochure_
    # enrichment.py's own last render attempt for THIS row's brochure_link:
    # True only for a CONFIRMED dead link (Canva itself answered the page
    # load with a non-2xx status - see canva_renderer/app.py's own
    # navigation-status check), False when that same attempt read fine,
    # None whenever no attempt was made this run (already fully populated,
    # ineligible link, or a weaker failure signal - a timeout/exception -
    # not confirmed enough to call the link itself dead) OR for any link
    # type/failure shape this doesn't yet cover. None is deliberately NOT
    # the same as False - see master_merge.diff_fields's own blank-skip
    # rule, which is exactly why a fresh row that never got re-checked
    # this run can never accidentally clear a master row's own already-
    # confirmed True.
    brochure_link_broken: Optional[bool] = None
    submarket: Optional[str] = None
    building: str
    floor_unit: Optional[str] = None
    size_sqft: Optional[float] = None
    desks_min: Optional[int] = None
    desks_max: Optional[int] = None
    rent_pcm: Optional[float] = None
    rent_psf: Optional[float] = None
    brochure_link: Optional[str] = None
    floorplan_link: Optional[str] = None  # a genuinely different document from brochure_link - never a substitute
    # True only when brochure_link has nothing genuine of its own and was
    # filled in from floorplan_link instead - the same fallback applied
    # identically by all three extraction paths (extract.py, extract_
    # email.py, extract_spreadsheet_gemini.py's own extract_sheet_with_
    # metadata) right after each one's own finalize_brochure_link/finalize_
    # floorplan_link calls - so a real document is still shown rather than
    # a blank column, but never indistinguishable from an actual brochure.
    # floorplan_link itself is completely unaffected, still
    # holding the exact same URL independently. Never explicitly False -
    # left None whenever the fallback doesn't apply, same reasoning as
    # brochure_link_broken above: a fresh row that didn't need the fallback
    # this run must never overwrite a master row's existing True via master_
    # merge.diff_fields' own blank-new-value-skip rule. Checked by display_
    # utils.with_brochure_link_display_labels to swap brochure_link's own
    # display label to "Open floor plan" instead of "Open brochure".
    brochure_link_is_floorplan: Optional[bool] = None
    # Tri-state, never Gemini-set (like lat/lng above) - set True by
    # geocode.py's own geocode_row when its Tier 2 (Places) fallback had to
    # accept a candidate with ZERO source address_1/postcode/building-
    # trailing-token hint to cross-check it against at all (see that
    # module's _source_location_hint) - there is no independent evidence
    # the result is even the right building, as opposed to a same-named
    # but genuinely different real place (confirmed real cases: "Henly
    # House" not indexed under that spelling, "Ivybridge House" resolving
    # to a stale/mislabeled POI). Never set True for Tier 1, nor for a
    # Tier 2 run that DID have a hint to check against - those already have
    # real corroborating evidence. None is deliberately NOT the same as
    # False, same reasoning as brochure_link_broken above - a fresh row
    # that resolved with real corroborating evidence this run must never
    # accidentally clear a master row's own already-True unverified flag
    # via master_merge.diff_fields' own blank-skip rule.
    geocode_unverified: Optional[bool] = None
    special_features: Optional[str] = None
    state_of_space: Optional[str] = None
    contacts: Optional[str] = None  # all contacts combined, one per line/semicolon, each as "Name, email, phone"
