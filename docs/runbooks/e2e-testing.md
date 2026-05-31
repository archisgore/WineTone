# Runbook: End-to-end testing with Playwright

*Set up 2026-05-22. Authenticated coverage added 2026-05-23.*

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

## Authenticated tests — Clerk sign-in tokens

**Refactored 2026-05-31.** Previously we captured a Clerk session in
a local browser and replayed the cookies in CI; that path was
fragile (device-binding, `client-uat` cookie scope, short refresh
windows). Now we mint a fresh **sign-in ticket** via Clerk's
Backend API at the start of each test, redeem it in a clean
Playwright context, and let Clerk establish the session naturally.
No captured cookies, no replay-window staleness.

The dev Clerk secret only mints tokens for the dev instance (used
by `staging.tone.wine`), so authenticated tests self-skip unless
`--target` points at staging.

### One-time setup

You need:

1. **A dedicated test user** on the staging Clerk instance, with
   display_name `e2e-test`. If you don't already have one, sign in
   via `https://staging.tone.wine`, rename via the ✎ button on your
   profile.
2. **The Clerk user_id** of that account. Find it in the
   Clerk dashboard → Users → search "e2e-test" → click the row → the
   `user_…` ID is in the URL and on the detail panel.
3. **The dev Clerk secret key** (`sk_test_…`). Clerk dashboard →
   API Keys.

### Storing the credentials

Add both as **environment secrets** in the `production` GHA
environment (alongside `WINETONE_DB_URL`, `HF_TOKEN_WRITE`,
`BACKUP_REPO_TOKEN`):

```bash
gh secret set CLERK_SECRET_KEY  --env production --repo archisgore/WineTone
# Paste the sk_test_… value when prompted.

gh secret set CLERK_E2E_USER_ID --env production --repo archisgore/WineTone
# Paste the user_… ID when prompted.
```

The e2e workflow's pytest step exposes both as env vars; the
`_clerk_e2e_creds` fixture (`tests/e2e/conftest.py`) reads them and
mints a sentinel ticket once per session to fail fast if either is
wrong, then per-test mints fresh tickets for `signed_in_context`.

### How it works at runtime

1. **Session start** — `_clerk_e2e_creds` reads the two env vars
   and does one sentinel `POST /v1/sign_in_tokens` to verify the
   Clerk Backend API + the credentials are working. If this fails
   the whole authenticated suite fails fast with a clear message.
2. **Per test** — `signed_in_context` calls
   `mint_sign_in_url(user_id, secret_key)` to get a fresh
   single-use ticket URL, opens a clean `browser.new_context()`,
   and `page.goto(ticket_url)`. Clerk validates the ticket,
   creates a fresh session keyed to *this* Playwright context, and
   redirects back to the app.
3. **Verify** — `signed_in_page` hits `/me` and asserts the
   redirect lands on `/u/<display_name>` before yielding the page.

### Refreshing

No manual refresh ever needed. Every test session mints fresh
tickets from a fresh API call. The dev Clerk secret key is the
only long-lived material, and Clerk dashboard makes rotation a
one-click operation if you need it.

### Running authenticated tests locally

```bash
# Point at staging. Authenticated tests skip on prod.
export WINETONE_E2E_URL=https://staging.tone.wine

# Same two secrets the CI uses.
export CLERK_SECRET_KEY=sk_test_…
export CLERK_E2E_USER_ID=user_…

pytest tests/e2e/test_authenticated.py -v
```

### What's covered by the authenticated suite

- `/me` resolves to the test account's dashboard (session sanity).
- Dashboard renders self-only markers for the test user.
- Inline label editor on `/wines/{id}` round-trips: add → edit → delete.
- `/discover` loads for a signed-in user (gate works, page renders).
- `/u/<user>/recommend` returns at least one card.
- `/ask` works in signed-in mode (no regression vs anonymous path).
- `/onboarding` is reachable when signed in.

### Cleanup discipline

Authenticated tests own their data and clean up afterwards. The
label round-trip test, in particular, asserts the editor is back
to its empty state after a delete — so even if it fails partway
through, you'll know exactly what got left behind.

Wine submissions via `/wines/new` are intentionally not exercised
on every run — they leave permanent data in the catalog. Test
manually before merging changes to that flow.

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
