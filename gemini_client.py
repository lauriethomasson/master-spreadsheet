import json
import os
import sys
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from env_utils import load_dotenv

load_dotenv()

MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

RETRY_INSTRUCTION = (
    "\n\nYour previous response was not valid JSON. "
    "Return ONLY valid JSON, nothing else."
)

# Applied only on a retry after a response was cut off before finishing (see
# _is_truncated) - a real, confirmed failure mode distinct from a genuine
# JSON-formatting mistake: a large/complex document (many units, a long
# brochure) can make Gemini's own reply exceed its implicit default output-
# token budget, truncating the JSON mid-object (e.g. "Expecting property
# name enclosed in double quotes" deep into the response) - RETRY_
# INSTRUCTION's own "return only valid JSON" wording does nothing for this,
# since the response was never malformed by choice, only cut off. Asking
# for more tokens than a model's own true ceiling is safely clamped by the
# API rather than rejected, so this is never itself a new failure mode for
# a document that was never going to need this much room in the first place.
RETRY_MAX_OUTPUT_TOKENS = 65536


class QuotaExceededError(Exception):
    """Raised when the Gemini API rejects a call for exceeding its quota (HTTP 429)."""


class ResponseTruncatedError(Exception):
    """
    Raised when Gemini's response was cut off before finishing (finish_reason
    == MAX_TOKENS) even after retrying with a raised output-token ceiling
    (see RETRY_MAX_OUTPUT_TOKENS) - the document itself has more content
    than fits in one response, not a formatting mistake a retry could fix.
    Callers already catch plain Exception around a call_gemini call (see
    app.py's own per-file extraction try/except) and show str(e) directly
    to the user, so this class's own message is written to read clearly
    there - never a raw json.JSONDecodeError position/character offset,
    which means nothing to a user and doesn't even name the real cause.
    """


def get_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise SystemExit(
            "Set GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment before running."
        )
    return genai.Client(api_key=api_key)


def _is_truncated(response) -> bool:
    """
    True when `response`'s own first candidate stopped because it hit its
    output-token limit before finishing, rather than completing normally -
    the real, distinct cause behind an otherwise-inexplicable "invalid JSON
    deep into a long response" failure for a large/complex document, never
    itself a JSON-formatting mistake RETRY_INSTRUCTION could fix. Defensive
    against a missing/empty candidates list (never seen in practice, but a
    bare attribute lookup on an unexpected response shape must not itself
    raise here - "unknown, so not confirmed truncated" is the safe default).
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return False
    return candidates[0].finish_reason == types.FinishReason.MAX_TOKENS


# Confirmed real production evidence, three separate times: a genuine 503
# ("Deadline expired before operation could complete.") from Gemini's own
# backend, on an otherwise perfectly healthy call - a real Canva render
# that had just succeeded (7/7 pages) had its very next step, the Gemini
# extraction call, fail this way one second later, leaving that row with
# no enrichment data and no error surfaced anywhere a reviewer would see
# (design DAHENodGhUU, "The Timber Yard" Unit 13 - see brochure_
# enrichment.py's own STATUS_EXTRACTION_FAILED handling for why a failure
# here doesn't get flagged the same way a confirmed-dead link does). Two
# earlier occurrences (Colonial Building, Uncommon Liverpool St) and a
# fourth from an unrelated concurrency benchmark are the same signature -
# never a sign the DOCUMENT/URL itself is bad, unlike a genuine bad-
# request or parsing error, which must still fail immediately exactly as
# before. Scoped to 503 specifically (not blanket 5xx) - the one status
# actually confirmed, mirroring how brochure_enrichment.py's own
# _CANVA_RENDERER_TRANSIENT_STATUS_CODES is scoped to the specific codes
# actually observed rather than guessing at every plausible one.
_GEMINI_TRANSIENT_STATUS_CODES = (503,)
_GEMINI_TRANSIENT_MAX_ATTEMPTS = 2
_GEMINI_RETRY_BACKOFF_SECONDS = 2


def _generate_content_with_retry(client: genai.Client, contents: list, config):
    """
    client.models.generate_content, retried a small, bounded number of
    times (_GEMINI_TRANSIENT_MAX_ATTEMPTS) with a short fixed backoff on a
    transient Gemini-side 503 (see _GEMINI_TRANSIENT_STATUS_CODES' own
    docstring for the real production evidence this covers) - mirrors
    brochure_enrichment._fetch_canva_rendered_page's own transient-retry
    loop for the Canva renderer's 502/503s, the same "small, bounded
    retry with a short fixed backoff, logged so a future trace shows it
    happening" shape.

    A non-503 ServerError (or a 503 on the final attempt) is re-raised
    unchanged - never swallowed, and never retried further. genai_errors.
    ClientError (4xx, including the 429/quota case) is left completely
    untouched by this function - it propagates straight through to call_
    gemini's own existing except clause exactly as before this retry
    existed, since a bad request/quota error is never transient and
    retrying it would just waste time before the same, certain failure.

    No trailing return/raise after this loop - every one of its
    iterations either returns (a successful call) or raises (the final
    attempt, or a non-transient status), by construction, so control can
    never actually fall off the end.
    """
    for attempt in range(1, _GEMINI_TRANSIENT_MAX_ATTEMPTS + 1):
        try:
            return client.models.generate_content(model=MODEL_NAME, contents=contents, config=config)
        except genai_errors.ServerError as e:
            if e.code not in _GEMINI_TRANSIENT_STATUS_CODES or attempt == _GEMINI_TRANSIENT_MAX_ATTEMPTS:
                raise
            print(
                f"[gemini_client] Gemini returned a transient HTTP {e.code} on attempt "
                f"{attempt}/{_GEMINI_TRANSIENT_MAX_ATTEMPTS} - retrying after "
                f"{_GEMINI_RETRY_BACKOFF_SECONDS}s.",
                file=sys.stderr,
            )
            time.sleep(_GEMINI_RETRY_BACKOFF_SECONDS)


def call_gemini(client: genai.Client, prompt: str, parts: list) -> dict:
    current_prompt = prompt
    config = types.GenerateContentConfig(response_mime_type="application/json")
    for attempt in range(2):
        try:
            response = _generate_content_with_retry(client, [current_prompt, *parts], config)
        except genai_errors.ClientError as e:
            if e.code == 429:
                raise QuotaExceededError(
                    "Gemini API quota exceeded for this model. Try again later, "
                    "or set GEMINI_MODEL to a different model with separate quota."
                ) from e
            raise

        truncated = _is_truncated(response)
        try:
            return json.loads(response.text)
        except json.JSONDecodeError as e:
            if attempt == 0:
                if truncated:
                    print(
                        "[gemini_client] Response was cut off before finishing (MAX_TOKENS) - "
                        f"retrying with max_output_tokens={RETRY_MAX_OUTPUT_TOKENS}.",
                        file=sys.stderr,
                    )
                    config = types.GenerateContentConfig(
                        response_mime_type="application/json", max_output_tokens=RETRY_MAX_OUTPUT_TOKENS,
                    )
                else:
                    current_prompt = prompt + RETRY_INSTRUCTION
                continue
            if truncated:
                raise ResponseTruncatedError(
                    "This document has too much content for Gemini to return in one response, "
                    "even after retrying with a higher output limit. Try splitting it into smaller "
                    "files (e.g. fewer pages/units per upload)."
                ) from e
            raise


def compute_rent(fields: dict) -> dict:
    size = fields.get("size_sqft")
    pcm = fields.get("rent_pcm")
    psf = fields.get("rent_psf")
    if size:
        if pcm and not psf:
            fields["rent_psf"] = round((pcm * 12) / size, 2)
        elif psf and not pcm:
            fields["rent_pcm"] = round((size * psf) / 12, 2)

    return fields
