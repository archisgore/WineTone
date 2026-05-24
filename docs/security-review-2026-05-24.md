# WineTone security review — 2026-05-24

*Conducted against `main` at commit `117c032` per the checklist in
`docs/runbooks/security-review-plan.md`. Author: Claude (Opus 4.7)
under Archis's direction. Half-day pass — read-and-reason across
the 13 layers in the runbook, no penetration testing.*

**TL;DR:** the codebase is in a defensible state. No HIGH or
CRITICAL findings. Five MEDIUMs, three of which (rate-limit gaps,
description-length cap, starlette CVE) get patched in the same
commit as this report. Two MEDIUMs (scan-upload size cap, CSP
tightening) and three LOW findings are filed as follow-ups.

---

## Findings summary

| # | Severity | Area | Item | Status |
|---|---|---|---|---|
| 1 | MEDIUM | Rate limiting | `/vocab/search` POST has no `@limiter.limit` | **Patched this PR** |
| 2 | MEDIUM | Rate limiting | `/u/{user}/recommend` POST has no `@limiter.limit` | **Patched this PR** |
| 3 | MEDIUM | Input validation | Form `description` fields are unbounded length | **Patched this PR** |
| 4 | MEDIUM | Dependencies | `starlette 1.0.0` carries `PYSEC-2026-161`; fix in 1.0.1 | **Patched this PR** |
| 5 | MEDIUM | Input validation | Scan upload (`UploadFile`) has no explicit size cap | Follow-up task |
| 6 | LOW | SQL injection | `app.py:968` uses f-string inside `text(...)` (variable from a whitelist; not exploitable but a code smell) | Follow-up task |
| 7 | LOW | XSS | `narration_html\|safe` from python-markdown on LLM output (self-XSS only; the narration only renders for the asking user) | Follow-up task |
| 8 | LOW | Headers | CSP allows `'unsafe-inline'` + `'unsafe-eval'` in `script-src` (required by clerk-js + HTMX today; tightenable with nonces) | Backlog |

---

## Layer-by-layer

### 1. Authentication — OK

`src/winetone/auth_clerk.py`:

- JWT signature verified against a live JWKS endpoint with a cached
  client (no hard-coded keys).
- `iss` claim is checked against the configured Clerk frontend
  domain.
- `aud` verification is intentionally disabled — Clerk doesn't set
  one by default; the issuer + signature combination is the
  authority. Documented in a comment.
- `exp` is honored (default in `pyjwt`).
- The 2026-05-23 multi-cookie fix is in place: `current_user()`
  iterates every cookie whose name starts with `__session` and
  returns the first whose JWT validates. An invalid cookie does
  NOT poison the search — the loop continues to the next.
- `current_user()` never raises — every error path returns `None`.
- `is_enabled()` toggles off when the Clerk env vars are absent;
  in that mode `current_user` returns `None` deterministically.

### 2. Authorization — OK

- Every `@app.post("/u/{user}/...")` route that mutates user data
  calls `_require_self(request, user)`. Inspected: calibrate/search,
  calibrate/add, calibrate/delete, calibrate/fit, recommend. ✓
- `_require_self` enforces three things: signed-in, display-name
  matches, age_confirmed is True. Wines-new and labels write
  endpoints also enforce age_confirmed at the route level.
- `/admin/reports*` uses `_require_admin`, which returns **404 (not
  403)** when the viewer isn't the configured admin — page
  existence is not leaked.
- Profile-page gates (`/u/<n>`, `/u/<n>/palate`, `/users`,
  `/discover`) fire `raise HTTPException(401, ...)` BEFORE any
  user-data is fetched. No DB read on the unauthed path.
- IDOR check: `_require_self` compares `me["display_name"] !=
  user` (the URL param). A signed-in user attempting to POST to
  someone else's `/u/...` route gets 403 cleanly.

### 3. Input validation — 2 MEDIUM

**MEDIUM-3 (PATCHED): description fields are unbounded.**

The `description` form parameter on `/wines/new`,
`/wines/{wine_id}/label`, and `/u/{user}/calibrate/add` is declared
as `description: str = Form(...)` with no max_length. A malicious
user could submit a 50MB description; the limiter caps frequency
but each individual submission is unbounded.

**Patch:** add `max_length=4096` to the Form declaration on all
three handlers. 4096 chars is generous for a real label and small
enough that storage exhaustion isn't a concern.

**MEDIUM-5 (FOLLOW-UP): scan upload has no size cap.**

`/wines/scan` POST declares `image: UploadFile = File(...)` with
no explicit limit. FastAPI/Starlette defaults to reading the entire
body into memory. The 20/hour-per-IP rate limit caps frequency but
each individual request could be a multi-GB file. Containers have
limited memory; an attacker could OOM the prod replica.

**Patch (deferred):** check `request.headers.get("content-length")`
before reading, OR read into a `BytesIO` with a max size and 413
on overflow. Anthropic's vision API itself rejects images above
~5MB, so cap at ~8MB to give margin.

**OK paths:**

- Sentiment is whitelisted to `'positive'`/`'negative'` in handlers.
- Style key on `/onboarding` is whitelisted via `onboarding.get_style()`.
- `scope_user` on `/vocab/search` is pattern-matched
  `[A-Za-z0-9_\-]*` on the input element (defense in depth — the
  query is parameter-bound server-side regardless).
- Vintage is parsed via `int(vintage)` with a try/except in the
  catalog query path; bad input results in a clean 400, not a 500.

### 4. Output / XSS — 1 LOW

**LOW-7: `narration_html | safe` on `/ask` results.**

`result["narration_html"]` is populated by
`markdown.markdown(narration_md, extensions=[...])` where
`narration_md` is the LLM router's natural-language explanation
output. python-markdown does NOT escape raw HTML inline tags by
default — if the LLM emits `<script>alert(1)</script>`, the
markdown library preserves it, and the template's `| safe` filter
then renders it un-escaped into the DOM.

**Why this is LOW, not MEDIUM:** the narration only renders for
the user who typed the prompt. There's no path for a third party
to inject content into another user's `/ask` results. So even a
successful prompt-injection that gets the LLM to emit `<script>`
is effectively self-XSS — exploitable only by an attacker against
themselves.

**Patch (deferred):** either pass `safe_mode='escape'` to
`markdown.markdown`, or run the rendered HTML through
`bleach.clean(html, tags=ALLOWED, attributes={})` with a tight
allowlist before exposing it.

**OK paths:**

- Jinja2 autoescape is on by default for `.html` extensions in
  `Jinja2Templates` — verified by the framework's defaults.
- `{{ product_ld_json | safe }}` is JSON-dumped server-side with
  `json.dumps(..., ensure_ascii=False)` — escapes `</script>` and
  control characters. Safe.
- All user-submitted descriptions render through plain `{{ ... }}`
  (autoescaped).
- href/src attribute interpolations use quoted Jinja contexts —
  spot-checked the wine_detail, dashboard, catalog templates.

### 5. SQL injection — 1 LOW

**LOW-6: `app.py:968` admin_reports uses f-string inside `text(...)`.**

```python
where = "" if status == "all" else "WHERE r.status = :status"
params = {} if status == "all" else {"status": status}
rows = conn.execute(_text(f"""
    SELECT ...
      FROM abuse_reports r ...
      {where}
     ORDER BY r.created_at DESC
     LIMIT 200
"""), params).mappings().all()
```

The `where` value is constructed from a whitelist (`status not in
(...) : status = "open"`) — never user-controlled. The `:status`
binding inside the WHERE clause IS done correctly. So this is
**not exploitable**, but the f-string-inside-text pattern is a
code smell and could regress in a refactor.

**Patch (deferred):** rewrite to always include the WHERE clause
and gate on a constant:

```python
text("""SELECT ... WHERE :status_filter = 'all' OR r.status = :status ...""")
```

…or split into two distinct query strings (with vs without WHERE).

**OK paths:**

- Every other `text(...)` call in `app.py`, `recommend.py`,
  `calibrate.py`, `lexical.py` uses bound parameters
  (`:name`-style placeholders). Grepped `text(f"` and
  `text("...{` patterns site-wide — this is the only hit.
- Cursor pagination on `/catalog` binds the cursor as
  `:cursor`; sort directions are whitelisted via Python `if`
  branches.
- FTS query uses `websearch_to_tsquery('english', :q)` — the
  user input `q` is bound, not concatenated.

### 6. CSRF — OK

- The only auth cookie is Clerk's `__session`, set with
  `SameSite=Lax` (confirmed via the captured session JSON during
  e2e setup). Lax stops cross-origin POSTs from third-party sites.
- `X-Frame-Options: DENY` + `frame-ancestors 'none'` in CSP —
  page cannot be iframed.
- We don't set our own cookies; no CSRF token machinery needed
  given the SameSite + Clerk model.
- Account-delete uses a button (POST), not a link (GET). ✓

### 7. Rate limiting — 2 MEDIUM

**MEDIUM-1 (PATCHED): `/vocab/search` POST has no rate limit.**

The endpoint runs `embed_user_labels.search` which does a full
hybrid dense + sparse retrieval. Each call is expensive (~100ms on
warm DB, more on cold). No per-IP/per-user cap means a single
attacker can pin the DB connection pool.

**Patch:** `@limiter.limit("60/hour")` on `vocab_search_route`.

**MEDIUM-2 (PATCHED): `/u/{user}/recommend` POST has no rate limit.**

Same shape — hybrid retrieval per call, plus the LLM
explain-recommendations side path. `_require_self` gates to
signed-in self-only, so the blast radius per attacker is one
account, but they can still pin the pool for everyone.

**Patch:** `@limiter.limit("60/hour")` on `recommend_route`.

**OK coverage:** every other mutating/expensive endpoint has a
limiter. The scan endpoint specifically is 20/hour (Anthropic
budget cap). Webhook signature verification provides effective
rate-limiting via cryptographic gating.

### 8. Secrets handling — OK

- Git history grep for `ghp_`, `github_pat_`, `hf_lfmz`, `sk-ant-api`,
  `npg_` returned no tracked files. ✓
- Secrets live in HF Space settings (runtime + build-time mount via
  `--mount=type=secret`) — verified by the cutover work. None are
  baked into image layers.
- Sentry config: `send_default_pii=False` (line 177).
- `auth.json` (e2e captured Clerk session) is gitignored. We rely
  on humans to delete it locally after upload.
- The `~/.claude/settings.json` permissions surface is conservative
  per the user's standing instruction.

### 9. Privacy posture (post-2026-05-23) — OK

- `/u/<n>`, `/u/<n>/palate`, `/users`, `/discover` all 401 anon.
- Wine-detail page conditionally renders `@username` only when
  `current_user` is set. `&mdash; sign in to see author &mdash;`
  is shown to anon (then updated to "Signed-in members only ·"
  later).
- `/ask` and `/vocab` result tables: the "by" column header AND
  cell are wrapped in `{% if current_user %}`. Anon viewers see
  the labels and descriptions but not the username column.
- JSON-LD Product/Review on wine pages intentionally OMITS
  `author` per the privacy gate. Verified in `app.py` review
  serialization.
- Sitemap (`/sitemap-pages.xml`) lists only public landing pages —
  no `/u/<name>` URLs. The wine sub-sitemaps are wine_id paths only.
- robots.txt does NOT explicitly Disallow `/u/` or `/users`. This
  is acceptable because both 401 anon and crawlers won't index a
  401 — but worth tightening as defense-in-depth in a future pass.

### 10. Dependencies — 1 MEDIUM

**MEDIUM-4 (PATCHED): starlette 1.0.0 has PYSEC-2026-161.**

`pip-audit` flagged exactly one CVE. Fix: starlette >= 1.0.1.
FastAPI pins starlette as a transitive dep but the resolved
version can be bumped.

**Patch:** add `starlette>=1.0.1` to `pyproject.toml` or pin via
`fastapi[...]` to a version that requires the fix.

### 11. Security headers — OK with 1 LOW

Live verification against prod (`curl -sI https://tone.wine/`):

- `Strict-Transport-Security: max-age=31536000; includeSubDomains` ✓
- `X-Frame-Options: DENY` ✓
- `X-Content-Type-Options: nosniff` ✓
- `Referrer-Policy: strict-origin-when-cross-origin` ✓
- `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()` ✓
- `Content-Security-Policy: default-src 'self'; script-src 'self'
  'unsafe-inline' 'unsafe-eval' ...; frame-ancestors 'none'`

**LOW-8: CSP `'unsafe-inline' 'unsafe-eval'`.**

Both are required by the current setup:

- `unsafe-inline` — small inline `<script>` blocks in base.html
  (Clerk-mount glue, public-notice JS, chip-click handler, etc.).
- `unsafe-eval` — clerk-js v6 internals.

Tightenable but not necessary right now. Long-term: move inline
scripts to external files + use a CSP nonce.

### 12. Clerk webhook — OK

- `/webhooks/clerk` requires `CLERK_WEBHOOK_SECRET` env var;
  returns 503 if missing (won't process unsigned).
- Uses `svix.webhooks.Webhook(secret).verify(payload, headers)`
  which checks signature, replay timestamp, and message-id. The
  svix lib is the official Clerk recommendation.
- Returns 400 on signature failure (clean rejection).
- e2e `test_webhook_rejects_unsigned` covers this as a regression
  test.

### 13. PII in logs / errors — OK

- `winetone.access` log line per request: method, path, status,
  duration, request_id. No body content. ✓
- Query params ARE in the path → if any sensitive param is ever
  added to a query string, it would land in logs. Today's query
  params are all non-sensitive (sort, cursor, country, variety, q).
- Sentry: `send_default_pii=False` → request bodies, cookies,
  headers like Authorization, query params with sensitive names
  are scrubbed.
- Application loggers (`winetone.*`) — grepped for `log.*query`,
  `log.*description`, `log.*password` — no hits writing user
  content to logs.

---

## Follow-up tasks filed

- **MEDIUM-5:** scan-upload size cap (8 MB recommendation)
- **LOW-6:** refactor admin_reports f-string-in-text out
- **LOW-7:** sanitize narration_html before `| safe`
- **LOW-8:** CSP nonces + drop `unsafe-inline`/`unsafe-eval`
- **Defense-in-depth:** add `Disallow: /u/` and `Disallow: /users`
  to robots.txt (minor)

## Re-run cadence

- Light pass after every dependency bump: §10 only.
- Full §1, §2 after any auth-related code change.
- Full pass once per quarter.
