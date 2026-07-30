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
    desks_max: Optional[int] = None          # single unit's desk count, OR the upper bound of a range if desks_min is also set
    desks_min: Optional[int] = None          # only populated for a genuine range-group row (e.g. "24-58 desks")
    size_sqft_min: Optional[float] = None    # only populated for a genuine range-group row
    size_sqft_max: Optional[float] = None    # only populated for a genuine range-group row
    rent_pcm: Optional[float] = None
    rent_psf: Optional[float] = None
    rent_psf_min: Optional[float] = None     # directly stated range (e.g. "£190 - £230 psf"), range rows only
    rent_psf_max: Optional[float] = None
    rent_pcm_min: Optional[float] = None     # estimated band, computed downstream from size/psf min-max, never by the LLM
    rent_pcm_max: Optional[float] = None
    brochure_link: Optional[str] = None
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
    submarket: Optional[str] = None
    building: str
    floor_unit: Optional[str] = None
    size_sqft: Optional[float] = None
    desks_max: Optional[int] = None
    desks_min: Optional[int] = None
    size_sqft_min: Optional[float] = None
    size_sqft_max: Optional[float] = None
    rent_pcm: Optional[float] = None
    rent_psf: Optional[float] = None
    rent_psf_min: Optional[float] = None
    rent_psf_max: Optional[float] = None
    rent_pcm_min: Optional[float] = None
    rent_pcm_max: Optional[float] = None
    brochure_link: Optional[str] = None
    special_features: Optional[str] = None
    state_of_space: Optional[str] = None
    contacts: Optional[str] = None  # all contacts combined, one per line/semicolon, each as "Name, email, phone"
