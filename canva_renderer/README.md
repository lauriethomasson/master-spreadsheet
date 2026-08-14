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

Capped at `MAX_CANVA_PAGES` (default 20, see below) regardless of how many
pages a design actually has, so a malformed/huge public design can't make
one `/render` call consume unbounded time or memory. If Canva's own page
controls aren't found at all (a single-page design, or a future Canva
frontend change), this service simply returns the one page it always did
before this feature existed - never a hard failure.

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
  --memory 1Gi \
  --cpu 1 \
  --timeout 180 \
  --concurrency 4 \
  --no-allow-unauthenticated
```

`--timeout 180` is comfortably above this service's own internal
`RENDER_TIMEOUT_SECONDS` backstop (scales with `MAX_CANVA_PAGES` - see
`app.py`), which itself is the ceiling for one `/render` call capturing up
to `MAX_CANVA_PAGES` pages of a single design.

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

| Variable                  | Default | Purpose                                                   |
|----------------------------|---------|------------------------------------------------------------|
| `PORT`                     | `8080`  | Set automatically by Cloud Run.                            |
| `MAX_CONCURRENT_RENDERS`   | `2`     | Hard cap on simultaneous browser renders.                  |
| `MAX_CANVA_PAGES`          | `20`    | Hard cap on pages captured per design (see above).          |
| `RENDERER_SHARED_SECRET`   | (unset) | Optional extra `Authorization: Bearer <secret>` check.     |

### Environment variable the MAIN APP needs

Set on the **main spreadsheet app's own** Cloud Run service, not here:

| Variable            | Purpose                                                                 |
|----------------------|--------------------------------------------------------------------------|
| `CANVA_RENDERER_URL` | This service's own URL (e.g. `https://canva-renderer-xyz-uc.a.run.app`). |

Leaving `CANVA_RENDERER_URL` unset on the main app keeps Canva links
exactly as unsupported as they were before this feature existed - Canva
support is entirely opt-in and requires deploying this service first.
