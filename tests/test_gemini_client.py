"""
Regression tests for gemini_client.call_gemini - real, confirmed production
report: uploading a large/complex document ("Extraction failed on file 1 of
1: Expecting property name enclosed in double quotes: line 2372 column 7
(char 75399)") surfaced a raw json.JSONDecodeError position/character
offset straight to the user, which means nothing to them and doesn't even
name the real cause.

Traced to a real, distinct failure mode this module never previously
checked for: Gemini's own response can be cut off before finishing
(finish_reason == MAX_TOKENS) once a document has enough content (many
units, a long brochure) to exceed the model's own implicit default output-
token budget - truncating the JSON mid-object. The existing retry (append
RETRY_INSTRUCTION, "return only valid JSON") does nothing for this, since
the response was never malformed by choice, only cut off partway through.

Fix: call_gemini now checks finish_reason before parsing. A truncated
response retries ONCE with an explicit, much higher max_output_tokens
instead of the RETRY_INSTRUCTION wording; a genuinely malformed (non-
truncated) response keeps the original retry-with-instruction behavior
unchanged. If a document is STILL truncated after the raised ceiling,
callers get a clear ResponseTruncatedError naming the real cause instead
of a bare JSONDecodeError.

Every existing caller (extract.py/extract_email.py/extract_spreadsheet_
gemini.py, and every test of theirs) mocks call_gemini itself at the
point of import, never exercising this module's own real body - so this
is the first and only place these code paths are actually tested.

Run with:
    .venv\\Scripts\\python.exe -m unittest tests.test_gemini_client -v
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from google.genai import errors as genai_errors
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gemini_client


def _response(text: str, finish_reason=types.FinishReason.STOP):
    candidate = MagicMock()
    candidate.finish_reason = finish_reason
    response = MagicMock()
    response.text = text
    response.candidates = [candidate]
    return response


class IsTruncatedTests(unittest.TestCase):
    def test_max_tokens_finish_reason_is_truncated(self):
        response = _response("{}", finish_reason=types.FinishReason.MAX_TOKENS)
        self.assertTrue(gemini_client._is_truncated(response))

    def test_stop_finish_reason_is_not_truncated(self):
        response = _response("{}", finish_reason=types.FinishReason.STOP)
        self.assertFalse(gemini_client._is_truncated(response))

    def test_no_candidates_is_not_truncated(self):
        response = MagicMock()
        response.candidates = []
        self.assertFalse(gemini_client._is_truncated(response))


class CallGeminiTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()

    def test_a_valid_response_returns_parsed_json_on_the_first_call(self):
        self.client.models.generate_content.return_value = _response('{"a": 1}')

        result = gemini_client.call_gemini(self.client, "prompt", [])

        self.assertEqual(result, {"a": 1})
        self.client.models.generate_content.assert_called_once()

    def test_malformed_non_truncated_json_retries_with_the_instruction_wording(self):
        self.client.models.generate_content.side_effect = [
            _response("not json at all", finish_reason=types.FinishReason.STOP),
            _response('{"a": 1}', finish_reason=types.FinishReason.STOP),
        ]

        result = gemini_client.call_gemini(self.client, "prompt", [])

        self.assertEqual(result, {"a": 1})
        self.assertEqual(self.client.models.generate_content.call_count, 2)
        second_call_prompt = self.client.models.generate_content.call_args_list[1].kwargs["contents"][0]
        self.assertIn(gemini_client.RETRY_INSTRUCTION, second_call_prompt)
        # The malformed-JSON retry must never also raise max_output_tokens -
        # that's specifically the truncation retry's own signal.
        second_call_config = self.client.models.generate_content.call_args_list[1].kwargs["config"]
        self.assertIsNone(second_call_config.max_output_tokens)

    def test_truncated_response_retries_with_a_raised_output_token_ceiling_not_the_instruction(self):
        truncated_json = '{"units": [' + '{"a": 1}, ' * 500  # deliberately cut off, invalid JSON
        self.client.models.generate_content.side_effect = [
            _response(truncated_json, finish_reason=types.FinishReason.MAX_TOKENS),
            _response('{"units": []}', finish_reason=types.FinishReason.STOP),
        ]

        result = gemini_client.call_gemini(self.client, "prompt", [])

        self.assertEqual(result, {"units": []})
        self.assertEqual(self.client.models.generate_content.call_count, 2)
        # The prompt itself must be unchanged - this isn't a wording problem.
        second_call_prompt = self.client.models.generate_content.call_args_list[1].kwargs["contents"][0]
        self.assertEqual(second_call_prompt, "prompt")
        second_call_config = self.client.models.generate_content.call_args_list[1].kwargs["config"]
        self.assertEqual(second_call_config.max_output_tokens, gemini_client.RETRY_MAX_OUTPUT_TOKENS)

    def test_still_truncated_after_retry_raises_a_clear_error_not_a_bare_json_error(self):
        truncated_json = '{"units": [' + '{"a": 1}, ' * 500
        self.client.models.generate_content.return_value = _response(
            truncated_json, finish_reason=types.FinishReason.MAX_TOKENS,
        )

        with self.assertRaises(gemini_client.ResponseTruncatedError) as ctx:
            gemini_client.call_gemini(self.client, "prompt", [])

        self.assertIn("too much content", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, json.JSONDecodeError)

    def test_still_malformed_and_not_truncated_after_retry_raises_the_original_json_error(self):
        self.client.models.generate_content.return_value = _response(
            "still not json", finish_reason=types.FinishReason.STOP,
        )

        with self.assertRaises(json.JSONDecodeError):
            gemini_client.call_gemini(self.client, "prompt", [])

    def test_quota_exceeded_is_raised_for_a_429(self):
        error = genai_errors.ClientError(429, {"error": {"message": "quota"}})
        self.client.models.generate_content.side_effect = error

        with self.assertRaises(gemini_client.QuotaExceededError):
            gemini_client.call_gemini(self.client, "prompt", [])

    def test_a_non_429_client_error_is_re_raised_unchanged(self):
        error = genai_errors.ClientError(400, {"error": {"message": "bad request"}})
        self.client.models.generate_content.side_effect = error

        with self.assertRaises(genai_errors.ClientError):
            gemini_client.call_gemini(self.client, "prompt", [])


class TransientServerErrorRetryTests(unittest.TestCase):
    """
    _generate_content_with_retry - confirmed real production evidence,
    three separate times, of a transient Gemini 503 ("Deadline expired
    before operation could complete.") on an otherwise perfectly healthy
    call (see that function's own docstring, and _GEMINI_TRANSIENT_
    STATUS_CODES' for the specific incident this traces to: design
    DAHENodGhUU, "The Timber Yard" Unit 13, whose Canva render succeeded
    fully but whose very next Gemini extraction call failed this way one
    second later, leaving the row silently unenriched).
    """

    def setUp(self):
        self.client = MagicMock()
        self.sleep_patcher = patch("gemini_client.time.sleep")
        self.mock_sleep = self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

    def test_a_single_503_is_retried_once_and_then_succeeds(self):
        error = genai_errors.ServerError(503, {"error": {"message": "Deadline expired"}})
        self.client.models.generate_content.side_effect = [error, _response('{"a": 1}')]

        result = gemini_client.call_gemini(self.client, "prompt", [])

        self.assertEqual(result, {"a": 1})
        self.assertEqual(self.client.models.generate_content.call_count, 2)
        self.mock_sleep.assert_called_once_with(gemini_client._GEMINI_RETRY_BACKOFF_SECONDS)

    def test_persistent_503s_exhaust_the_retry_and_raise(self):
        error = genai_errors.ServerError(503, {"error": {"message": "Deadline expired"}})
        self.client.models.generate_content.side_effect = [error, error]

        with self.assertRaises(genai_errors.ServerError):
            gemini_client.call_gemini(self.client, "prompt", [])

        self.assertEqual(self.client.models.generate_content.call_count, gemini_client._GEMINI_TRANSIENT_MAX_ATTEMPTS)

    def test_a_non_503_server_error_is_never_retried(self):
        # A genuine bad-request/parsing-shaped failure must still fail
        # immediately, exactly as before this retry existed - only the
        # specific, confirmed transient-outage signature (503) is retried.
        error = genai_errors.ServerError(500, {"error": {"message": "internal error"}})
        self.client.models.generate_content.side_effect = error

        with self.assertRaises(genai_errors.ServerError):
            gemini_client.call_gemini(self.client, "prompt", [])

        self.client.models.generate_content.assert_called_once()
        self.mock_sleep.assert_not_called()

    def test_a_healthy_first_call_never_sleeps_or_retries(self):
        self.client.models.generate_content.return_value = _response('{"a": 1}')

        result = gemini_client.call_gemini(self.client, "prompt", [])

        self.assertEqual(result, {"a": 1})
        self.client.models.generate_content.assert_called_once()
        self.mock_sleep.assert_not_called()

    def test_quota_exceeded_429_still_works_through_the_retry_wrapper(self):
        # ClientError (4xx) must pass through this wrapper completely
        # untouched - call_gemini's own existing except clause is what
        # turns a 429 into QuotaExceededError, unaffected by this retry.
        error = genai_errors.ClientError(429, {"error": {"message": "quota"}})
        self.client.models.generate_content.side_effect = error

        with self.assertRaises(gemini_client.QuotaExceededError):
            gemini_client.call_gemini(self.client, "prompt", [])

        self.mock_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
