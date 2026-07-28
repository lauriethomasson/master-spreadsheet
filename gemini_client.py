import json
import os

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


class QuotaExceededError(Exception):
    """Raised when the Gemini API rejects a call for exceeding its quota (HTTP 429)."""


def get_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise SystemExit(
            "Set GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment before running."
        )
    return genai.Client(api_key=api_key)


def call_gemini(client: genai.Client, prompt: str, parts: list) -> dict:
    current_prompt = prompt
    for attempt in range(2):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=[current_prompt, *parts],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
        except genai_errors.ClientError as e:
            if e.code == 429:
                raise QuotaExceededError(
                    "Gemini API quota exceeded for this model. Try again later, "
                    "or set GEMINI_MODEL to a different model with separate quota."
                ) from e
            raise
        try:
            return json.loads(response.text)
        except json.JSONDecodeError:
            if attempt == 0:
                current_prompt = prompt + RETRY_INSTRUCTION
                continue
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

    size_min = fields.get("size_sqft_min")
    size_max = fields.get("size_sqft_max")
    psf_min = fields.get("rent_psf_min")
    psf_max = fields.get("rent_psf_max")
    if size_min and psf_min and not fields.get("rent_pcm_min"):
        fields["rent_pcm_min"] = round((size_min * psf_min) / 12, 2)
    if size_max and psf_max and not fields.get("rent_pcm_max"):
        fields["rent_pcm_max"] = round((size_max * psf_max) / 12, 2)

    return fields
