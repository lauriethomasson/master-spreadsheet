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


class EmptyResponseError(Exception):
    """
    Raised when Gemini's final attempt has genuinely no text content to
    parse at all - response.text is None (no text part whatsoever, e.g. a
    candidate with only a "thought" part or none at all) or "" (a text part
    present but empty) - and the response was NOT truncated (see
    ResponseTruncatedError for that, distinct, case). Confirmed real
    production case this covers: a large, image-heavy Canva-deck paste-link
    extraction (22 pages) whose second/final Gemini attempt came back with
    finish_reason=STOP and an empty text part - not a JSON-formatting
    mistake RETRY_INSTRUCTION could fix, and not truncation either, so
    retrying with more output tokens would do nothing.

    Message-building is best-effort (see _describe_empty_response) - a
    genuine safety/content block names its own real reason (prompt_feedback.
    block_reason, or a candidate finish_reason like SAFETY/PROHIBITED_
    CONTENT/RECITATION) when Gemini actually reports one; otherwise this
    falls back to a plain "Gemini returned an empty response" plus whatever
    finish_reason WAS given (often just STOP, as in the confirmed Canva case
    above - Gemini's own API gives no further explanation for an empty-but-
    STOP response). Either way, this is always a clearer message than the
    raw json.JSONDecodeError("Expecting value: line 1 column 1 (char 0)")
    this replaces - same "callers show str(e) directly to the user" reasoning
    as ResponseTruncatedError's own docstring above.
    """


def _describe_empty_response(response) -> str:
    """
    A short, user-facing reason `response` came back with no text to parse,
    for EmptyResponseError's own message - never a stack trace or raw SDK
    repr. Checked in order, most specific/confident evidence first:

    1. prompt_feedback.block_reason - the PROMPT itself was rejected before
       generation ever started (see google.genai.types.BlockedReason) -
       whenever this is set, no candidate ever gets real content, so this is
       always the most specific, confident answer available.
    2. The first candidate's own finish_reason, when it's anything other
       than STOP/the "unspecified" default/blank (see google.genai.types.
       FinishReason) - SAFETY/RECITATION/PROHIBITED_CONTENT/etc. all name a
       real, specific cause for stopping with nothing to show for it, even
       though generation itself was allowed to start.
    3. Otherwise (confirmed real case: finish_reason == STOP with an empty
       text part - the Gemini API's own response gives no further reason a
       caller can extract) - a plain, honest "empty response" message
       naming whatever finish_reason WAS reported, rather than claiming a
       specific cause this function has no real evidence for.
    """
    prompt_feedback = getattr(response, "prompt_feedback", None)
    block_reason = getattr(prompt_feedback, "block_reason", None)
    if block_reason:
        return f"Gemini blocked this request ({getattr(block_reason, 'name', block_reason)})."

    candidates = getattr(response, "candidates", None) or []
    finish_reason = candidates[0].finish_reason if candidates else None
    finish_reason_label = getattr(finish_reason, "name", finish_reason) or "unknown"
    if finish_reason and finish_reason not in (
        types.FinishReason.STOP, types.FinishReason.FINISH_REASON_UNSPECIFIED,
    ):
        return f"Gemini stopped without returning usable content (finish reason: {finish_reason_label})."

    return f"Gemini returned an empty response (finish reason: {finish_reason_label})."


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
        # response.text read into a local ONCE, then checked for emptiness
        # BEFORE ever calling json.loads, rather than inside a try/except
        # around it - response.text (see the google-genai SDK's own
        # GenerateContentResponse._get_text) can be None (no text part at
        # all - a candidate with only a "thought" part, or none) just as
        # easily as "" (a text part present but empty), and json.loads(None)
        # raises TypeError, never json.JSONDecodeError - the OLD code here
        # only ever caught the latter, so a None response.text would have
        # escaped as an uncaught TypeError instead of reaching either retry/
        # final-error branch below. Handling both shapes uniformly here
        # closes that gap. A confirmed real production case (a large,
        # 22-page Canva-deck paste-link extraction) hit exactly this
        # "empty, not truncated" shape on its final attempt.
        text = response.text
        parse_error = None
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                parse_error = e

        # Nothing usable this attempt, either an empty response or one that
        # failed to parse - same retry/give-up shape either way, since
        # RETRY_INSTRUCTION's own "return only valid JSON" wording is a
        # reasonable ask of Gemini regardless of which of the two happened.
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
                if not text:
                    print(
                        "[gemini_client] Response had no text content to parse - "
                        f"retrying ({_describe_empty_response(response)}).",
                        file=sys.stderr,
                    )
                current_prompt = prompt + RETRY_INSTRUCTION
            continue

        if truncated:
            raise ResponseTruncatedError(
                "This document has too much content for Gemini to return in one response, "
                "even after retrying with a higher output limit. Try splitting it into smaller "
                "files (e.g. fewer pages/units per upload)."
            ) from parse_error
        if not text:
            raise EmptyResponseError(_describe_empty_response(response))
        raise parse_error


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
