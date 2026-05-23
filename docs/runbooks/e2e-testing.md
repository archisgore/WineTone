# Runbook: End-to-end testing with Playwright

*Set up 2026-05-22. Anonymous coverage shipped in v1; authenticated
flows are tracked as a follow-up.*

The Playwright + pytest suite at `tests/e2e/` runs against a live
deploy and proves that the public surface of WineTone works the
way users expect. It's the safety net that catches things the
unit tests can't: third-party outages (Anthropic, HF Inference,
Cloudflare), real DNS issues, stale CDN content, and browser-side
regressions.

---

## How to run

### From the GitHub Actions tab (recommended)

1. Open <https://github.com/archisgore/WineTone/actions/workflows/e2e.yml>
2. Click **Run workflow** (top right).
3. Pick the target environment (`staging` or `prod`) from the
   dropdown.
4. Click **Run workflow** again to start it.

Results appear in the Actions tab within ~2 minutes. A failure
attaches a Playwright trace artifact you can download and open in
`playwright show-trace` for a step-by-step replay.

### From your local machine

```bash
cd ~/github/archisgore/WineTone
pip install pytest pytest-playwright httpx
playwright install chromium

# Default target is prod; override with --base-url or WINETONE_E2E_URL.
WINETONE_E2E_URL=https://staging.tone.wine pytest tests/e2e -v

# Run a single test, with a visible browser, slow-motion:
WINETONE_E2E_URL=https://staging.tone.wine \
  pytest tests/e2e/test_smoke.py::test_palate_page_renders \
  -v --headed --slowmo=400
```

### Automatic runs

- **Push to `stage`** triggers the workflow against
  `staging.tone.wine`. The check shows up as a status on the
  commit; a red check means the staged change broke something.
- **Nightly at 06:00 UTC** triggers the workflow against
  `tone.wine`. Catches drift from third-party services and lets you
  wake up to a red Actions tab if anything broke overnight.

---

## What's covered

| Category | Test | Why |
|---|---|---|
| **Public routes** | Every advertised public page returns 200 anonymously | Regression check — catches accidental auth-gating of pages that should be public |
| **Auth-required POSTs** | Every auth-gated endpoint returns 401 (not 500) for anonymous traffic | Catches a real bug class: an unhandled exception masquerading as an auth check |
| **Health endpoint** | `/healthz` returns 200 with the expected JSON shape | UptimeRobot keys off this; field renames must be caught early |
| **Nav highlighting** | Active tab gets `.is-active` on every nav route | UX regression — silent if it breaks |
| **Catalog flows** | Filter form, FTS search both return real cards | End-to-end content delivery |
| **/vocab + /users + /wines/{id} + /u/{u}/palate** | Each renders without error using real corpus data | Detail-page-route smoke check |
| **PWA manifest + service worker** | Both reachable, manifest parses with required fields | Catches an iOS install regression |
| **Security headers** | HSTS / X-Frame / Referrer / nosniff / CSP all present | Regression check — the middleware is easy to accidentally bypass |
| **Robots / sitemap** | Both return 200 with expected content | SEO regression check |
| **Webhook signature gate** | Unsigned POST to `/webhooks/clerk` returns 400 or 503 (never 200) | Critical — a 200 here would let anyone wipe user data |
| **Admin route leak** | `/admin/reports` returns 404 (not 403) to non-admins | Confirms the route's existence doesn't leak |

---

## What's NOT covered yet

Authenticated flows. Signing in via Clerk requires either:

- **Email magic-link**: hard to automate without intercepting email
  (out of scope for a smoke suite)
- **Clerk test mode**: a feature on Clerk's paid plans that bypasses
  email confirmation for designated test users. Available but we
  haven't enabled it on the dev instance yet.
- **Pre-saved session cookie**: sign in once manually, save the
  Playwright `storage_state` to a CI secret, replay it in tests.
  This is the most pragmatic path.

The follow-up plan is the third option. Steps:

1. Sign in to `staging.tone.wine` once with a dedicated `e2e-test`
   account on the dev Clerk instance.
2. `context.storage_state(path="auth.json")` to capture the session.
3. Store `auth.json` content as a GitHub Actions secret
   (`E2E_STAGING_AUTH_STATE`).
4. Add a fixture that writes the secret to a temp file and passes
   `storage_state=...` to the Playwright browser context.
5. Add `tests/e2e/test_authenticated.py` covering:
   - Onboarding picker
   - Label add → list update → fit
   - Recommend with explanation
   - Inline label edit
   - Wine submission via `/wines/new` and `/wines/scan`
   - Account-delete cascade

Estimated effort: half a day. Skipped until we either commit to
buying Clerk's test-user feature or do the manual-cookie capture.

---

## When a test fails

1. The Actions tab shows which test failed, in red. Click into the
   run for the test output.
2. A failed run uploads a `playwright-trace-*` artifact (zip).
   Download it, then locally:
   ```bash
   playwright show-trace ~/Downloads/playwright-trace-prod/trace.zip
   ```
   You get a step-by-step replay with DOM snapshots, screenshots,
   and network logs.
3. The trace points at which assertion failed and what the page
   looked like at the moment of failure.

Common failure modes and what they mean:

- **`test_public_route_returns_200` failing on `/ask`** — the LLM
  router's HF Inference call is failing. Page itself renders, but
  the test would only fail if a 500 propagated. Check Sentry for
  the underlying exception. (Most common cause: HF_TOKEN rotated
  without "Inference Providers" scope.)
- **`test_webhook_rejects_unsigned` returning 200** — *critical*
  security regression. The webhook is accepting unsigned payloads.
  Roll back immediately and audit the auth_clerk.verify_webhook code.
- **`test_active_tab_highlight` failing on one route** — someone
  added a new route or renamed a nav link without updating the
  helper. Cosmetic.
- **`test_catalog_freetext_search_returns_results`** — the Postgres
  FTS index may have lost coverage or the corpus is empty. Check
  Neon directly.

---

## Adding new tests

Tests live in `tests/e2e/test_smoke.py` (or new modules under
`tests/e2e/`). Conventions:

- Use the `base_url` fixture for absolute URLs.
- Use `httpx` for pure HTTP checks (no JavaScript needed), Playwright
  `page` for content that depends on rendering or JS.
- Assertion failure messages should mention the path under test and
  the value seen, so the trace points at the bug, not the test.
- Aim for under 30s per test. The suite as a whole should run in
  under 2 minutes so it can be a real quality gate.

---

## Cost

GitHub Actions: free for public repos. The suite runs in ~90s
end-to-end, well under the free-tier minute budget.

Playwright: the chromium browser is downloaded by `playwright install`
at workflow start; cached between runs by the GitHub Actions cache
where applicable. No external paid services involved.
