from pydantic import BaseModel
from typing import Optional


class ExtractedFields(BaseModel):
    """Fields Gemini extracts directly from a brochure/document page image.

    Excludes lat/lng (filled in later via geocoding) and source_file
    (set from upload metadata) — those never go in the extraction prompt.
    """

    internal_ref: str
    provider: str
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
    internal_ref: str
    provider: str
    address_1: Optional[str] = None
    postcode: Optional[str] = None
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
    source_file: str
