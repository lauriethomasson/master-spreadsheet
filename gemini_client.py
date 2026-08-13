import json
import os
import sys

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


def call_gemini(client: genai.Client, prompt: str, parts: list) -> dict:
    current_prompt = prompt
    config = types.GenerateContentConfig(response_mime_type="application/json")
    for attempt in range(2):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=[current_prompt, *parts],
                config=config,
            )
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
