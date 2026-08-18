# canva_renderer

A small, isolated service whose only job is: given a public Canva "view"
URL, render it in a real headless Chromium and return a PNG screenshot of
every page of that design, up to `MAX_CANVA_PAGES`.

## Why this is a separate service

A plain HTTP fetch of a Canva "view" link returns Canva's own
"Unsupported client" HTML shell instead of the actual design (confirmed
directly - see the main app's `brochure_link_resolver.is_canva_view_link`
docstring). A real headless browser renders the actual content correctly,
but Chromium is heavy (memory, CPU, occasional stuck-tab risk on a
misbehaving page) - the main spreadsheet app already runs close to Cloud
Run's own memory limit, so Chromium never runs inside that container.
This service isolates that entire risk in its own container with its own
memory/CPU allocation: if Chromium OOMs or a page hangs here, the main
app is completely unaffected.

## Multi-page capture

A real public Canva "view" link renders as a single-page-at-a-time viewer,
not a continuously-scrollable list of every page (confirmed directly). This
service captures every page by driving Canva's own **accessible** page
controls - the "Next page" button's stable `aria-label` and its
`aria-disabled="true"` state on the last page (confirmed directly against a
real multi-page brochure) - never a CSS-class-dependent scrape, and never
one HTTP request per page (one browser context loads the design once, then
navigates and screenshots in place). A design's own reported "N / M" page
count (read from the accessible "Go to page" control) is used only for
logging, never to decide when to stop.

Capped at `MAX_CANVA_PAGES` (default 30, see below) regardless of how many
pages a design actually has, so a malformed/huge public design can't make
one `/render` call consume unbounded time or memory. If Canva's own page
controls aren't found at all (a single-page design, or a future Canva
frontend change), this service simply returns the one page it always did
before this feature existed - never a hard failure.

## Concurrency: queueing, not instant-rejection

`MAX_CONCURRENT_RENDERS` (default 2) is a hard cap on simultaneous browser
renders, but a request arriving while that cap is already reached now
**queues** for a free slot (up to `SEMAPHORE_WAIT_TIMEOUT_SECONDS`, default
90s) rather than being rejected with a 503 instantly. A bulk spreadsheet
upload with many Canva-linked rows genuinely dispatches several brochures
concurrently (the main app's own worker pool defaults to 5), so with only
2 render slots it used to take just 3 simultaneous Canva requests for the
rest to be dropped outright - regardless of whether that specific design
would have rendered fine. The wait is a firm ceiling, not unbounded: a slot
that never frees up within the wait budget still fails safely (503), and
this wait time is kept entirely separate from `RENDER_TIMEOUT_SECONDS` -
the render clock only starts once a slot is actually acquired, so a
request queued behind a burst is never penalized for time spent merely
waiting for capacity.

If the cached Chromium browser itself crashes or is OOM-killed mid-load,
the very next request detects this (`Browser.is_connected()`) and
relaunches it automatically - a single crash no longer poisons every
subsequent render for the rest of this container instance's lifetime.

## Response size: adaptive JPEG re-encoding

Cloud Run enforces a hard **32MB limit on a single HTTP response body**
(`CLOUD_RUN_MAX_RESPONSE_BYTES` in `app.py`) - not a configurable setting;
`--memory`/`--timeout`/`--concurrency` have no effect on it at all. Real
production evidence: a genuine 29-page brochure (Risborough, the first
capture to actually reach that size after `MAX_CANVA_PAGES` was raised
from 20 to 30) measured at **34.24MB** as PNG+base64+JSON - over the
limit, surfacing as Cloud Run's own platform WARNING ("Response size was
too large...") immediately followed by a 500, even though the render
itself succeeded and canva_renderer's own logs said so.

`do_POST` estimates the payload size from the raw captured bytes
(`RESPONSE_SIZE_SAFETY_THRESHOLD_BYTES`, a safety margin below the 32MB
ceiling) and, only when that estimate is large enough to risk the limit,
re-encodes every page as JPEG (`JPEG_QUALITY = 85`) instead of sending the
lossless PNG Chromium produced - confirmed against that same real 29-page
capture: 25.68MB raw PNG -> 5.25MB JPEG, comfortably under the limit with
real margin, and matching the intuition that PNG (lossless) is a poor
format for a real screenshot of a photo-heavy brochure page in the first
place. The large majority of renders (small/medium decks) never approach
the threshold and keep full lossless PNG quality completely unchanged -
this only ever costs anything on the rare large/photo-dense capture, and
only pays for exactly as much compression as needed to avoid a hard
platform failure.

The response's own `"image_format"` field is purely informational (for
Cloud Run logs/manual inspection) - the main app never trusts it. It
instead sniffs the actual bytes' own magic number (see `extract.
images_from_png_pages`) to decide the correct mime_type to hand Gemini,
so this adaptive behavior needs **no wire-contract coordination** between
the two independently-deployed services at all - it works correctly
regardless of which one gets redeployed first.

## Local development

```bash
pip install -r requirements.txt
playwright install chromium
python app.py
```

Then `POST http://localhost:8080/render` with `{"url": "https://www.canva.com/design/.../.../view"}`.
A `200` response body is JSON: `{"pages": ["<base64 PNG>", ...], "page_count_detected": 7}`
(`pages` in page order, always at least one; `page_count_detected` is Canva's
own best-effort reported total, or `null` if it couldn't be read - never a
promise that many pages were actually captured, see `MAX_CANVA_PAGES`).
A recognized failure returns `422` with `{"error": "render_failed", "reason": "..."}`.

## Deployment (Cloud Run)

This is a **separate** Cloud Run service from the main app - do not add
this container/its dependencies to the main app's own `Dockerfile`.

```bash
gcloud run deploy canva-renderer \
  --source canva_renderer \
  --region <same region as the main app> \
  --memory 4Gi \
  --cpu 1 \
  --timeout 420 \
  --concurrency 4 \
  --no-allow-unauthenticated
```

`--memory 4Gi` (raised from `2Gi`, itself raised from an earlier `1Gi`) -
a real production render hit `Memory limit of 1024 MiB exceeded with 1027
MiB used` while capturing a multi-page brochure: every page visited
during pagination keeps its own DOM/canvas rendering state alive in the
SAME browser context for the whole request (Canva's own pagination
advances the already-loaded design in place, it's not a real page
navigation - see `app.py`'s own pagination docstring), so a many-page
render costs meaningfully more memory than a single-page one did.
`MAX_CANVA_PAGES` already bounds that per-request cost, but raising it
from 20 to 30 (see that constant's own docstring - a real brochure had
its contact info lost on page 29 of 29) raises the per-render ceiling
too; two concurrent cap=30 renders (`MAX_CONCURRENT_RENDERS=2`) can
approach ~2.5GB, close enough to `2Gi` to have no real headroom left -
`4Gi` keeps comfortable headroom above that without having to reduce
`MAX_CONCURRENT_RENDERS` (which would slow down bulk uploads dispatching
several brochures at once). CPU stays at `1` - nothing about this fix is
CPU-bound, and `1 vCPU` still supports up to `4Gi` on Cloud Run.

`--timeout 420` is comfortably above this service's own internal worst
case: `RENDER_TIMEOUT_SECONDS` (scales with `MAX_CANVA_PAGES` AND
`NAV_TIMEOUT_MS` - see `app.py`) PLUS `SEMAPHORE_WAIT_TIMEOUT_SECONDS` (a
request arriving while `MAX_CONCURRENT_RENDERS` are already in flight now
queues for a free slot rather than being rejected instantly - see that
constant's own docstring for the real bulk-upload production bug this
fixes), summed - 365s at current defaults (raised from 285s when
`MAX_CANVA_PAGES` went from 20 to 30 - see that constant's own docstring).
`--concurrency 4` lets Cloud Run itself route a genuine overflow (more
requests than even the queue can absorb) to a fresh instance rather than
piling everything onto one.

`--no-allow-unauthenticated` is the primary access control (see
"Authentication" below) - do not deploy this publicly.

### Authentication

The renderer relies on Cloud Run's own built-in IAM invoker check, not a
custom auth scheme:

1. Deploy with `--no-allow-unauthenticated` (above).
2. Grant the main app's own runtime service account the `roles/run.invoker`
   role on this service:

   ```bash
   gcloud run services add-iam-policy-binding canva-renderer \
     --member="serviceAccount:<main-app-service-account>" \
     --role="roles/run.invoker"
   ```

The main app mints a Google-signed ID token scoped to this service's URL
on every call (see `brochure_enrichment._canva_renderer_auth_headers`) -
no shared secret to store or rotate. `RENDERER_SHARED_SECRET` (optional,
set as an env var on this service) is an additional, cheap defense-in-depth
check on top of that, never a replacement for `--no-allow-unauthenticated`.

### Environment variables this service reads

| Variable                       | Default | Purpose                                                   |
|---------------------------------|---------|------------------------------------------------------------|
| `PORT`                          | `8080`  | Set automatically by Cloud Run.                            |
| `MAX_CONCURRENT_RENDERS`        | `2`     | Hard cap on simultaneous browser renders.                  |
| `SEMAPHORE_WAIT_TIMEOUT_SECONDS`| `90`    | How long a request queues for a free render slot before failing (see `app.py`) - never instant-reject. |
| `MAX_CANVA_PAGES`               | `30`    | Hard cap on pages captured per design (see above).          |
| `RENDERER_SHARED_SECRET`        | (unset) | Optional extra `Authorization: Bearer <secret>` check.     |

### Environment variable the MAIN APP needs

Set on the **main spreadsheet app's own** Cloud Run service, not here:

| Variable            | Purpose                                                                 |
|----------------------|--------------------------------------------------------------------------|
| `CANVA_RENDERER_URL` | This service's own URL (e.g. `https://canva-renderer-xyz-uc.a.run.app`). |

Leaving `CANVA_RENDERER_URL` unset on the main app keeps Canva links
exactly as unsupported as they were before this feature existed - Canva
support is entirely opt-in and requires deploying this service first.
