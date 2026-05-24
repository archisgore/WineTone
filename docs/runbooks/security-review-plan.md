# Runbook: WineTone security review

*Plan drafted 2026-05-24. Execute as a focused day of work; output is
a written findings doc + tracked remediation tasks.*

Goal: systematically inspect every surface where user data, auth
state, or external input crosses a trust boundary, and verify the
defensive code holds. Output: a `docs/security-review-2026-MM-DD.md`
with one section per layer, each finding labeled `OK` / `LOW` /
`MEDIUM` / `HIGH` / `CRITICAL` and (for non-OK) a remediation note.

This runbook is the *checklist*. The actual review writes findings
against it.

---

## Methodology

Per layer below:
1. Read the relevant code with the grep patterns listed.
2. Reason about the threat model — what an attacker could try.
3. Confirm the defense by tracing the code path.
4. Note any gap, even minor, with a severity tag.
5. For HIGH/CRITICAL findings, file an immediate task; for MEDIUM
   and below, batch into the findings doc.

Tools to have running:
- `ruff check` for static lints
- `pip-audit` or `safety check` for dependency CVEs
- `gh secret list` to inventory active secrets
- A browser with DevTools for header inspection
- The e2e suite (already covers some of these as regression tests)

---

## 1. Authentication

**Code locations:**
- `src/winetone/auth_clerk.py` — JWT verification, JWKS fetch,
  cookie reading (incl. the multi-cookie pattern fix from 2026-05-23)
- `src/winetone/web/app.py::_resolve_user` — translates JWT claims
  to a local user row

**Checks:**
- JWT signature verification uses the live JWKS, not a hard-coded key
- `iss` claim is compared to the configured Clerk frontend domain
- `aud` verification is intentionally disabled (Clerk default) —
  confirm this is still true and document why
- `exp` is honored — expired tokens reject
- Multi-cookie cascade (`__session` + `__session_<suffix>`) — verify
  every cookie variant is tried and an invalid one doesn't poison
  the result for a valid one
- `current_user()` never raises — all error paths return None
- JWKS client is cached; verify it doesn't grow unbounded
- Confirm `is_enabled()` returns False in test mode and the bypass
  doesn't leak into prod via env-var typos

**Threat model:** forged JWT, replay of an old session, JWKS spoofing,
race between key rotation and verification.

## 2. Authorization

**Code locations:**
- `src/winetone/web/app.py::_require_self` — ensures `user_id`
  matches the URL `user`
- All `@app.post("/u/{user}/...")` handlers — verify each calls
  `_require_self`
- `@app.get("/admin/...")` — admin gating mechanism
- The 2026-05-23 username-privacy gates on `/u/<n>`, `/u/<n>/palate`,
  `/users`, `/discover`

**Checks:**
- Every mutating endpoint calls `_require_self` before mutating
- `_require_self` enforces the age-gate (`age_confirmed` must be True)
- Admin endpoints (`/admin/reports`) gate on a known-admin list, not
  on a header or query param
- Profile-page gates fire BEFORE any user-data is read from DB (so
  failed-auth doesn't leak `user_id` existence)
- IDOR check: can a signed-in user post to `/u/other-username/...`
  routes and modify someone else's labels?

**Threat model:** IDOR, privilege escalation, gating bypass via
URL manipulation, missing age-gate on a write endpoint.

## 3. Input validation

**Code locations:**
- `/wines/new` (POST) — producer, wine_name, vintage, variety,
  country, region, description
- `/wines/{wine_id}/label` (POST) — description, sentiment
- `/wines/scan` (POST) — multipart file upload
- `/u/{user}/calibrate/add` (POST)
- `/ask/query`, `/vocab/search` (POST) — free-text query
- `/onboarding` (POST) — style key

**Checks:**
- Vintage clamped to a sane range (1800–current year + 2)
- Description length capped (current limit? confirm in handler)
- Scan upload size limit enforced (likely needs explicit check —
  HF Spaces / uvicorn defaults aren't tight enough)
- Scan upload MIME type validated (not just trusted from header)
- Sentiment is whitelisted (`positive`/`negative` only)
- Style key whitelisted against `onboarding.STYLES`
- Country / region / variety — what's the validation? Just
  string-trim, or whitelist? If freeform, that's a vector for
  catalog spam.
- `scope_user` on `/vocab/search` pattern-matches `[A-Za-z0-9_\-]*`
  (already verified in template); confirm server enforces too.
- Display-name uniqueness + format constraints on Clerk-side

**Threat model:** malformed input crashes a route, unbounded text
floods labels, malicious image triggers vision-model abuse,
prompt-injection into the LLM router via the query field.

## 4. Output / XSS

**Code locations:**
- All Jinja templates (`www/templates/`)
- Any `|safe` filter usage — particularly `product_ld_json` and
  `narration_html` in `_ask_results.html`

**Checks:**
- Jinja autoescape is on for `.html` (default — verify)
- `product_ld_json` is JSON-dumped server-side and safe-injected
  into a `<script type="application/ld+json">` block. Verify the
  JSON encoder used (`json.dumps`) escapes `</script>` and U+2028/
  U+2029 — these can break out of script tags.
- `narration_html` in `_ask_results.html` — where does this come
  from? If it's LLM-generated, it could carry XSS payloads. Confirm
  the LLM output is either:
    a. Treated as plain text and escaped, OR
    b. Run through a server-side sanitizer (bleach / similar)
- User-submitted descriptions render through Jinja — confirm no
  raw-HTML interpolation
- href / src values populated from user input — confirm they're
  inside Jinja's quoted attribute context

**Threat model:** LLM emits `<script>`, user labels a wine
"description with <img onerror=...>", JSON-LD escapes break out.

## 5. SQL injection

**Code locations:**
- Every `text(...)` call in `src/winetone/web/app.py`
- Every `text(...)` in `src/winetone/recommend.py` and other modules

**Checks:**
- Every `text("...")` uses `:param` binding, never string
  interpolation. Grep for `text\(f"` or `text\(".*\{` patterns —
  zero hits expected.
- LIKE / ILIKE patterns — confirm the user input is bound, not
  concatenated
- `OFFSET` / `LIMIT` — confirm bound, not concatenated
- Cursor pagination uses bound parameters
- The websearch_to_tsquery in catalog search — confirm the user
  input is the second arg to `websearch_to_tsquery`, not embedded
  in SQL

**Threat model:** classic SQLi via free-text fields. The risk surface
is small (most user input goes through ORM-style binding), but
worth confirming nothing slipped through.

## 6. CSRF

**Code locations:**
- All `<form hx-post="...">` and `<form action="..." method="post">`
  endpoints
- Cookie auth (Clerk's `__session` is the only auth cookie)

**Checks:**
- Same-origin policy — confirm Clerk's `__session` cookie has
  `SameSite=Lax` or `Strict`. Looking at the captured auth.json,
  it's `Lax` — that protects against cross-site POSTs from
  third-party origins.
- HTMX POSTs include `hx-headers` or rely on cookie SameSite —
  confirm we're not vulnerable to CSRF from embedded iframes
- `X-Frame-Options: DENY` already enforced — verify
- POST-only auth-mutating endpoints — confirm none accept GET
- Account-delete in particular: verify it requires both auth AND
  some additional confirmation (button click, not a link)

**Threat model:** attacker site embeds a hidden form that POSTs to
`/u/<victim>/recommend` or `/account/delete` if victim is logged in.

## 7. Rate limiting

**Code locations:**
- `@limiter.limit(...)` annotations across `src/winetone/web/app.py`

**Checks:**
- Every mutating endpoint has `@limiter.limit`
- Scan endpoint specifically: 20/hour/IP (Anthropic cost control)
- /ask endpoint: rate limit per IP to cap HF Inference / LLM cost
- Label add/edit/delete: 60/hour per user
- Account delete: low limit to prevent abuse
- Limiter uses `_client_ip()` correctly behind the HF reverse proxy
  (X-Forwarded-For)
- Per-user vs per-IP — verify the right key is used

**Threat model:** spam-flood labels, drain Anthropic budget via
mass scans, drain HF Inference quota via mass /ask.

## 8. Secrets handling

**Code locations:**
- `HF Space secrets` (list via HF API)
- `~/.claude/settings.json` permissions
- `.env` files (gitignored)
- The `github_token` build-time secret on both Spaces (PAT)

**Checks:**
- No secrets committed to git (grep history: `gh_pat_`, `ANTHROPIC_API_KEY`,
  `npg_` (Neon connection string prefix), `hf_lf` (HF deploy token), `sk-`,
  `pk_test_`, `pk_live_`)
- No secrets in HF Space's public Dockerfile (the build-time secret
  mount keeps them out of layers)
- No secrets in error responses or 500 traces
- Sentry config: `send_default_pii=False` — confirm
- The captured `auth.json` for e2e is treated as a credential (in
  HF Space secret, not in git)
- Rotation policy: PAT lasts 1 year; Clerk session JWT 60s; HF
  deploy token — what's its expiry?

**Threat model:** secret in git history, secret in log output,
secret in image layers, secret in URL params (analytics leak).

## 9. Privacy posture (post-2026-05-23 changes)

**Code locations:**
- Anonymity-gating on `/u/<n>`, `/u/<n>/palate`, `/users`,
  `/discover` (raise 401 to anon)
- Wine-page label attribution gating (`{% if current_user %}`)
- /ask + /vocab results gating (`<th>by</th>` conditional)
- Privacy policy / Terms wording

**Checks:**
- Anon viewer cannot reach a profile via direct URL
- Anon viewer cannot extract usernames from JSON-LD on wine pages
  (we deliberately omitted `author` from the Review schema — verify)
- JSON-LD does not include user labels' authors
- Sitemap does not include `/u/<username>` URLs (confirm —
  these would leak usernames to crawlers)
- Robots.txt does not allow `/u/` — actually we DO allow it currently
  since the route 401s anon. If the username-set is sensitive, we
  should `Disallow: /u/` and `Disallow: /users` for crawlers too,
  even though they'd get 401 anyway.
- Anon viewer of /ask cannot see usernames via the result HTML
- The Clerk publishable key in HTML is acceptable (it's meant to be
  public per Clerk's design)

**Threat model:** crawler indexing usernames, side-channel
username leak via timing/error-message, JSON-LD scrape.

## 10. Dependencies (CVE scan)

**Tool:** `pip-audit` (or `safety check --full-report`).

**Checks:**
- Run against `pyproject.toml` resolved deps in the active env
- Specifically watch for CVEs in: FastAPI, Starlette, Jinja2,
  pyjwt, cryptography, sqlalchemy, psycopg, httpx, urllib3,
  Pillow (if used in scan), fastembed
- Triage any HIGH/CRITICAL — patch immediately
- MEDIUM — schedule for next deploy

## 11. Security headers

**Code locations:**
- The middleware that sets HSTS, CSP, X-Frame, etc. (find it; might
  be in `app.py` or a separate module)

**Checks:**
- HSTS: `max-age >= 6 months`, `includeSubDomains`
- CSP: `default-src 'self'` (verify), no `unsafe-eval` outside
  necessary scripts. The current CSP allows `unsafe-inline` and
  `unsafe-eval` for Clerk + HTMX — confirm the scope is intentional
  and minimal.
- `X-Frame-Options: DENY` — confirm
- `X-Content-Type-Options: nosniff` — confirm
- `Referrer-Policy` — confirm
- `Permissions-Policy` — confirm (camera/microphone/geolocation
  should be tight)
- Already covered by e2e `test_security_headers_present` — verify
  the test asserts the actual values, not just presence

## 12. Clerk webhook

**Code locations:**
- `/webhooks/clerk` endpoint

**Checks:**
- Webhook signature verification (Svix) is required — confirm
  request without valid `svix-signature` returns 400
- The signing secret is from HF Space secrets, never default-empty
- The webhook handler doesn't trust the payload for any auth-relevant
  decisions other than the (verified) Clerk event semantics
- Replay protection: timestamps verified within a tolerance window
- E2e already covers the unsigned-rejection case — verify

**Threat model:** attacker triggers `user.deleted` event for victim,
unsigned POST nukes data.

## 13. PII in logs / errors

**Code locations:**
- `logging_config.py`
- Sentry init
- The middleware that logs request/response

**Checks:**
- Request URLs are logged — verify no query params with PII
  (search queries are public, but if any sensitive params ever get
  added, they'd leak)
- Stack traces — confirm Clerk JWT, DB connection strings, and
  user descriptions are NOT in tracebacks (Sentry's
  `send_default_pii=False` should cover this, but verify)
- Bodies are not logged
- IP addresses are logged briefly — confirm retention policy aligns
  with the privacy policy ("hosting providers age out per their own
  policies")

---

## Estimated effort

Half a day of focused work for the read-and-reason pass. Add
another half-day for actually writing remediations for any HIGH
or CRITICAL findings discovered, plus the report doc.

## Output artifacts

1. `docs/security-review-2026-MM-DD.md` — the findings doc
2. Task entries for each MEDIUM-and-above remediation
3. PR(s) for any CRITICAL fix that lands during the review itself

## When to re-run

- After every dependency bump (light pass — just §10)
- After any auth-related code change (full §1, §2)
- Once a quarter as a baseline (full pass)
