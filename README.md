# master-spreadsheet

Extracts structured commercial office listing data from PDF brochures using Gemini vision, validated against the `ListingRow` schema (`schema.py`).

## Model pin

Pinned to `gemini-3.6-flash` as of 2026-07-28.

Reasoning: full-quality flash tier (not `-lite`), avoids the instability of
`-preview` (experimental, can be renamed/retired without notice) and `-latest`
(silently repoints to a different underlying model over time). Confirmed
working against this project's API key — `gemini-2.0-flash*` returned a hard
quota block (`limit: 0`, not a transient rate limit) and `gemini-2.5-flash*`
were already retired for new users at time of testing.

Override via the `GEMINI_MODEL` environment variable if needed.
