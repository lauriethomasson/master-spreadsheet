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
    desks_min: Optional[int] = None
    desks_max: Optional[int] = None
    rent_pcm: Optional[float] = None
    rent_psf: Optional[float] = None
    brochure_link: Optional[str] = None
    special_features: Optional[str] = None
    state_of_space: Optional[str] = None
    contacts: Optional[str] = None  # all contacts combined, one per line/semicolon, each as "Name, email, phone"
