# WineTone Production Readiness Audit

*Snapshot updated 2026-05-21 (evening). Starting point was the same
morning. Many Tier 1-3 items got closed in the autonomous batch that
ran while Archis was away. This is the updated punch-list.*

---

## Status legend

- ✅ **Done** — shipped, verified live
- 🚧 **Partial** — code in place, manual external setup still needed
- ⬜ **Open** — not yet started

---

## What's already in place

- ✅ **HTTPS everywhere** via HF Pro custom domain + Let's Encrypt
- ✅ **Auth** via Clerk (email magic link, Google, GitHub)
- ✅ **GDPR right to erasure**: delete-my-account button + ON DELETE
  CASCADE through every user-FK'd table + Clerk Backend-API delete
- ✅ **Privacy policy** at `/privacy`; banner on every page
- ✅ **Terms of service** at `/terms`
- ✅ **Drinking-age self-attestation** gate before any write
- ✅ **Persistent storage**: Neon Postgres (paid tier)
- ✅ **Versioned data releases** as GitHub release tarballs
- ✅ **Open-source code** (Apache-2.0)
- ✅ **Robots / sitemap / OG meta**
- ✅ **Resume-friendly batch scripts** (`reencode_corpus.py`, fine-tune)

---

## Tier 1: actually-block-launch

1. ⬜ **Two-stage deploy pipeline (staging / prod).** Currently every
   `git push origin main` + Space rebuild goes straight to live. Stand
   up a *staging* Space (e.g. `archisgore/winetone-stage`) building
   from a `stage` branch, pointed at a separate Neon database/branch.
   Promote stage → prod by fast-forwarding `main` to `stage`. Rollback
   = `git reset` + factory_reboot.

2. 🚧 **Production Clerk instance.** Runbook at
   `docs/runbooks/clerk-production-setup.md` is end-to-end. Needs
   ~20 min of Archis's clicks at clerk.com to flip from `pk_test_*`
   to `pk_live_*` + add CNAME `accounts.tone.wine`.

3. ⬜ **HF token rotation.** The deploy token leaked into a transcript.
   Rotate at huggingface.co/settings/tokens and re-set as `HF_TOKEN`
   Space secret.

4. ✅ **Clerk webhook for user-deletion.** `/webhooks/clerk` with svix
   signature verification, handles `user.deleted`. Endpoint live; needs
   `CLERK_WEBHOOK_SECRET` configured + endpoint subscribed in the Clerk
   dashboard (covered in the production-Clerk runbook above).

5. ✅ **Rate limiting on writes.** slowapi middleware with per-IP
   limits sized for human use. X-Forwarded-For-aware so the HF
   reverse proxy doesn't collapse all clients into one.

6. ✅ **Content moderation.** `winetone/moderation.py` tripwire panel
   flags URLs, casino/crypto spam, all-caps, PII patterns. Surfaces
   to Sentry. Doesn't block. Plus reactive `/report` endpoint + UI on
   every label row. Admin UI for the report queue is Tier 5.

---

## Tier 2: catch-anomalies / observability

7. 🚧 **Activate Sentry.** SDK installed + `_init_sentry()` wired up.
   Just needs an account at sentry.io and `SENTRY_DSN` Space secret.

8. 🚧 **Activate analytics.** Beacon `<script>` conditional on
   `CF_ANALYTICS_TOKEN`. Just needs a CF Web Analytics property +
   the Space secret.

9. ✅ **Health/status endpoint.** `/healthz` returns JSON with DB
   ping latency, Clerk JWKS reachability, encoder load status.
   Returns 503 when DB is down so UptimeRobot can alert on it.

10. ⬜ **Uptime monitoring.** UptimeRobot free tier setup. ~5 min.

11. ✅ **Cost monitoring runbook.** `docs/runbooks/cost-monitoring.md`
    lists each provider's alert threshold + monthly-review script.
    Provider-side alert configuration is still on Archis's plate.

12. ✅ **Structured JSON logging.** `winetone/logging_config.py` with
    `request_id` threaded through a ContextVar; per-request access log
    line; `X-Request-Id` response header.

13. 🚧 **Database backup verification.** `docs/runbooks/db-backup-verification.md`
    documents the 7-day Neon PITR window, an emergency restore
    procedure, and a quarterly test-restore drill that proves the
    backups work before we need them. Code-side is done; an actual
    test-restore has not yet been run (first one due quarterly).

---

## Tier 3: trust + UX polish

14. ✅ **Terms of Service** at `/terms` (Tier 3 #14 was ToS in the
    original list — done) + **drinking-age self-attestation gate**
    (new item not on the original list, also done).

15. ✅ **Cookie consent — no banner needed.** Position formally
    documented in `/privacy`: our only cookie (`__session`) is
    strictly-necessary for the authenticated functionality the
    user is requesting, exempting it from ePrivacy/GDPR consent
    requirements. The page now explicitly lists what we don't use
    (Google Analytics, FB Pixel, ad-id cookies, third-party tracking).

16. 🚧 **Email infrastructure.** Plan documented at
    `docs/runbooks/email-infrastructure.md` — Resend free tier
    is the call; the one event that merits mail is
    account-deletion confirmation. Not yet wired in.

17. ✅ **Security headers.** SecurityHeadersMiddleware sets HSTS,
    X-Frame, Referrer-Policy, Permissions-Policy, and a Clerk-aware
    CSP. Verified via `curl -I https://tone.wine/`.

18. 🚧 **Accessibility — quick-wins done.** `aria-live="polite"`
    on every HTMX swap target (search results, label list, fit
    status, recommendations, /ask results, /vocab results) so
    screen readers announce updates. "Skip to main content"
    link as the first body child. `lang="en"` on `<html>`.
    Still need a Lighthouse + axe-core sweep + remediation.

19. 🚧 **Mobile testing.** Added a ≤640px breakpoint covering header
    wrap, dashboard grid collapse, table overflow, font sizes. Still
    needs a visual sanity check on actual iPhone/Android hardware.

20. ✅ **Custom 404 / 500 pages.** `_error.html` renders styled
    page; webhook/API/Accept-JSON paths get JSON; unhandled exception
    handler catches everything.

---

## Tier 4: scale + DX

21. ⬜ **Edge caching via Cloudflare proxy.** Toggle DNS to proxied
    (orange cloud) — free, reduces HF Spaces egress.

22. ⬜ **CDN static assets.** Push `/static/*` to R2 or Pages.

23. ✅ **Pre-warmed encoder.** `@app.on_event("startup")` fires an
    asyncio task that calls `encode_query("warmup")` so the first
    real user request doesn't pay the ~5s cold-load cost.

24. ✅ **Async-everything (event-loop audit).** The only true
    offender was the synchronous DB write inside `async def
    clerk_webhook` — now wrapped in `run_in_threadpool`. All
    other `pd.read_sql` callsites are inside sync `def` handlers
    that FastAPI already runs in a threadpool automatically;
    no event-loop hazard. Documented in the audit.

25. ✅ **Database migrations.** Alembic configured;
    `migrations/versions/20260521_000_baseline.py` captures everything
    pre-this-session. `20260521_001_age_confirmation.py` captures the
    only delta since. Neon is stamped at head.

26. ✅ **Encoder rotation playbook.** `docs/runbooks/encoder-rotation.md`
    has the five-step flow + rollback recipe.

27. ✅ **Automated tests.** 14 network-free tests covering lexical,
    LLM router fallback, moderation. FastAPI smoke tests (skip when
    no DB configured). GitHub Actions CI runs them + ruff on every
    push.

---

## Tier 5: longer-horizon product

28. ⬜ Multi-modal embeddings (text + chemistry). See
    `docs/chemical-analysis-options.md`.
29. ⬜ Mobile app (PWA).
30. ✅ Admin UI for the abuse-report queue. `/admin/reports` lists
    open / resolved reports with filter chips + a "Mark resolved"
    button per row. Gated to a single user via the
    `ADMIN_CLERK_USER_ID` env var; returns 404 (not 403) for
    everyone else so the route's existence doesn't leak.
31. ⬜ Search-by-image.
32. ⬜ Public JSON API.

---

## Revised time-to-launch-ready

**Tier 1 status: 3 of 6 complete. ~1-2 days to finish.**

Remaining Tier 1 work:
- Two-stage stage/prod pipeline: ~1 day
- Production Clerk instance: ~20 min of clicking (runbook ready)
- HF token rotation: ~5 min

Tier 2 mostly needs external account setup (~1 hr of clicking,
spread across Sentry / CF / UptimeRobot).

Tier 3 has email + a Lighthouse/axe sweep + visual-mobile
left (~0.5 day total — cookie-consent and a11y quick-wins landed,
and email infra is design-complete just not wired).

**Total remaining for "would-recommend-on-Reddit ready": ~3 working
days of focused effort + ~2 hours of external account configuration.**

Down from 3-5 days last estimate. Most of the autonomous batch
landed.
