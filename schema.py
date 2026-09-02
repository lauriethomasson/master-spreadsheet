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
    # The overall campus/development's own brand name, distinct from any
    # individual building's own name within it (e.g. "Regent's Wharf"
    # containing "The Canal Building", "Thorley Works", ...) - only when
    # the source document genuinely states one; null for a brochure
    # describing just one single building with no separate campus
    # branding, never invented. See geocode.py's own use of this as an
    # extra Tier 2 disambiguator.
    development_name: Optional[str] = None
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
    # to a stale/mislabeled POI).
    #
    # Set explicitly False (never left at None) by Tier 1, and by a Tier 2
    # run that DID have a hint to check against - both are real,
    # independently corroborated evidence, so both positively CLEAR a
    # stale True a prior upload's own zero-hint fallback may have left on
    # this same row, rather than leaving it stuck forever. This is exactly
    # why False and None are deliberately different values here, same
    # reasoning as brochure_link_broken above: None means "this run didn't
    # even touch the question" (row already had lat/lng and returned
    # early, e.g.) and must never disturb master's existing flag either
    # way via master_merge.diff_fields' own blank-skip rule, while False
    # means "this run has real evidence the location IS verified" and
    # must be written through like any other genuine value.
    geocode_unverified: Optional[bool] = None
    # Human-readable note (never a bare bool - see this docstring's own
    # last paragraph for why) set by brochure_enrichment.py's own address_1
    # cross-check whenever a row's ALREADY-STATED address_1 (right or
    # wrong - address_1 being non-blank is normally treated as "trusted,
    # never touched again" by BUILDING_LEVEL_FIELDS' own blank-only
    # backfill rule) disagrees with what that row's own brochure
    # independently states for the same building, once the brochure has
    # been fetched anyway for other fields. Confirmed real case: an
    # Ivybridge House row's own address_1 read "1 John Adam Street", but
    # its own brochure states "1 to 5 Adam Street" on its cover page and
    # every floor plan - no "John" anywhere. The brochure was always
    # fetchable; nothing blocked reading it, the pipeline just never
    # compared what it already had on file against what the document
    # actually says.
    #
    # Purely a REVIEW FLAG, never an auto-correction - see brochure_
    # enrichment._address_conflict_note's own docstring for exactly what
    # counts as a genuine conflict (a disagreeing leading house number, or
    # street-name text that doesn't substantially overlap) vs. what
    # doesn't (an exact match, no house-number-shaped brochure text to
    # compare against at all, or nothing on file yet to conflict with -
    # that last case is what BUILDING_LEVEL_FIELDS' own ordinary blank-
    # backfill already handles, not this field). address_1 ITSELF is never
    # written by this check - see pages/2_Review_and_Master.py's own
    # surfacing of this field for the decision a reviewer makes with it.
    #
    # None (never explicitly False) when no conflict was found OR this
    # row's own address_1 was never checked this run (e.g. no brochure
    # fetched, brochure had nothing address-shaped to compare) - same
    # "None means this run didn't touch the question" convention as
    # brochure_link_broken/geocode_unverified above, so master_merge.
    # diff_fields' own blank-skip rule never lets a fresh, unchecked row
    # silently clear a master row's own already-flagged conflict note.
    address_conflict: Optional[str] = None
    # The overall campus/development's own brand name, distinct from any
    # individual building's own name within it (e.g. "Regent's Wharf"
    # containing "The Canal Building", "Thorley Works", ...) - only when
    # the source document genuinely states one; null for a brochure
    # describing just one single building with no separate campus
    # branding, never invented. See geocode.py's own use of this as an
    # extra Tier 2 disambiguator.
    development_name: Optional[str] = None
    special_features: Optional[str] = None
    state_of_space: Optional[str] = None
    contacts: Optional[str] = None  # all contacts combined, one per line/semicolon, each as "Name, email, phone"
