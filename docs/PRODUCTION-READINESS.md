# WineTone Production Readiness Audit

*Snapshot 2026-05-21. WineTone is currently shipped as a research demo
at https://tone.wine but is graduating toward a real product. This is
the punch-list separating the two.*

---

## What's already in place

- **HTTPS everywhere** via HF Pro custom domain + Let's Encrypt.
- **Auth** via Clerk (email magic link, Google, GitHub).
- **GDPR-style right to erasure**: delete-my-account button at the
  bottom of every user's own dashboard cascades through every table
  via `ON DELETE CASCADE` and also deletes the Clerk-side account.
- **Privacy policy** at `/privacy`; banner on every page.
- **Persistent storage**: Neon Postgres (paid tier) holding wines,
  embeddings, user labels, projections, follows, fts indexes.
- **Versioned data releases**: every meaningful corpus change cuts a
  GitHub release with a tarball anyone can re-import.
- **Open-source code** (Apache-2.0), GitHub public.
- **Sentry SDK scaffolded** — activates on `SENTRY_DSN` Space secret.
- **CF Web Analytics scaffolded** — activates on `CF_ANALYTICS_TOKEN`.
- **robots.txt + sitemap.xml** auto-generated, SEO basics.
- **OG meta tags** so social cards render properly.
- **Resume-friendly batch scripts** for the slow operations
  (`reencode_corpus.py`, fine-tune training) — restartable mid-flight.

---

## What's missing for production

Roughly grouped, highest-leverage first.

### Tier 1: actually-block-launch

1. **Two-stage deploy pipeline (staging / prod).** Right now every
   `git push origin main` + Space rebuild goes straight to live. We
   should stand up a *staging* Space (e.g. `archisgore/winetone-stage`)
   that builds from a `stage` branch, pointed at a separate Neon
   branch/database. Promote stage → prod by fast-forwarding `main`
   to `stage`. Avoids the "broke prod for 4 minutes while debugging
   a NaN" pattern we hit during the fine-tune deploy.
   - Bonus: rollback is a single `git reset` + factory_reboot.

2. **Production Clerk instance.** Currently on `pk_test_*` /
   `sk_test_*` — small "Development mode" banner appears in the
   sign-in modal. Production needs:
     - Create a Production Instance in Clerk dashboard.
     - Add CNAME `accounts.tone.wine` → Clerk-provided target.
     - Get `pk_live_*` / `sk_live_*`, swap as Space secrets.
     - Rotate the test secret afterwards (the `sk_test_...` value
       leaked into the development transcript that birthed the deploy).

3. **HF token rotation** — same rationale as Clerk; the HF write
   token used during deploy was passed through a transcript. Rotate
   from huggingface.co/settings/tokens and re-add as `HF_TOKEN` Space
   secret.

4. **Clerk webhook for user-deletion.** If a user deletes their account
   from Clerk's own UI (User Button → Manage → Delete), Clerk fires a
   `user.deleted` webhook. We don't currently listen — the Clerk-side
   row goes but our DB keeps the local data, which violates the
   privacy policy. Add `/webhooks/clerk` that verifies the webhook
   signature and runs the same DELETE FROM users we run on the
   in-app flow.

5. **Rate limiting on writes.** No defenses against a script
   POST-ing 10,000 wine submissions in a minute. Add `slowapi`
   middleware: 30 req/min/IP on `/wines/new` and the calibrate
   endpoints, more permissive on reads. Cost: 0.5 day.

6. **Content moderation.** User labels and wine submissions are
   public the moment they're submitted. There's no spam filter, no
   profanity filter, no abuse-report mechanism. Minimum-viable: a
   reactive "Report this" link that emails me, plus a daily Sentry
   alert if Sentry catches anomalous text patterns.

### Tier 2: catch-anomalies / observability

7. **Activate Sentry properly.** Create the project at sentry.io,
   add the DSN as a Space secret. Currently scaffolded but inert.

8. **Activate analytics.** Create CF Web Analytics property for
   tone.wine, paste the token as a Space secret.

9. **Health/status endpoint** beyond the implicit liveness of `/`.
   `/healthz` returns JSON with: DB ping, Clerk JWKS reachability,
   HF Inference reachability. Used by UptimeRobot to alert.

10. **Uptime monitoring.** UptimeRobot free tier pings tone.wine
    every 5 min, alerts to email if down. Set up once, forget.

11. **Cost monitoring.** Right now I have no alerting if Neon
    storage breaches a tier or HF Pro credits exhaust. Set the
    billing-alert email on each provider.

12. **Structured logging.** Current logs go to stdout in mixed
    formats (uvicorn + Python logger + Sentry). Pipe through
    `structlog` with JSON output and a consistent schema (request_id,
    user_id, latency_ms). Critical for any post-mortem.

13. **Database backups.** Neon does point-in-time recovery on the
    paid plan, but I haven't verified the retention window. Document
    it and test a restore.

### Tier 3: trust + UX polish

14. **Terms of Service.** Companion to the privacy policy at `/terms`.
    Standard "use at own risk, no warranty, comply with local
    alcohol laws, don't try to break the site, we can ban abuse."

15. **Cookie consent banner.** Strictly speaking the EU's
    ePrivacy directive requires consent for non-essential cookies
    *even on a "no privacy" site*. We currently set Clerk's session
    cookie without a consent prompt. Defensible position: it's
    essential for auth, so no consent needed. Document the call.

16. **Email infrastructure.** No transactional emails today. Add a
    minimal "your account was deleted" confirmation email so users
    know it worked. Resend / Postmark / Mailgun, ~free tier.

17. **Security headers.** Add `Content-Security-Policy`,
    `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-
    cross-origin`, `Strict-Transport-Security`. Half-day of FastAPI
    middleware + iteration to not break Clerk's JS bundle.

18. **Accessibility audit.** Run axe-core / Lighthouse over each
    page. The HTMX flows in particular probably miss `aria-live`
    regions that screen readers need.

19. **Mobile testing.** Visual check on iPhone + Android — the CSS
    is responsive in intent but I haven't actually opened the site
    on a phone.

20. **404 / 500 pages.** Currently FastAPI's default exception
    handler returns JSON. Custom HTML pages keep the brand even when
    things break.

### Tier 4: scale + DX

21. **Caching at the edge.** Put Cloudflare in front of tone.wine
    (proxied DNS) — even just for static assets. Free, reduces HF
    Spaces egress on the long tail.

22. **CDN-served static assets.** `/static/*.css`, the favicon, any
    OG images — currently served by uvicorn. Push them to R2 or
    Cloudflare Pages, save the Space's CPU for actual requests.

23. **Pre-warmed encoder.** First request after a Space restart
    pays a ~5-second cost loading the SentenceTransformer model.
    Pre-load it at app startup so the *first* user request is fast.

24. **Async-everything in the request path.** The recommend
    pipeline does sync `pd.read_sql` calls inside an async route.
    On a single-worker uvicorn this is fine; under load we'd want
    to move to `asyncpg` + async pandas.

25. **Database migrations system.** Today schema changes are
    one-off Python scripts I run by hand against Neon. Add Alembic
    so we have versioned, reversible migrations checked into the
    repo. Critical once we have actual users we don't want to lose.

26. **Encoder rotation playbook.** When we next fine-tune the
    encoder (next quarter probably), we need a documented and
    tested flow for swapping it without breakage. Right now I
    remember it because I just ran it; in three months I won't.
    Codify as `docs/encoder-rotation-runbook.md`.

27. **Automated tests.** There's a `tests/` directory with one
    sanity test. Add: a smoke test for each web route, a unit
    test for `lexical.score_candidates`, a regression test that
    "sunshine in a bottle" still pulls Tokaj Aszú after a refit.
    GitHub Actions running them on every push.

### Tier 5: longer-horizon product

28. **Multi-modal embeddings (text + chemistry).** Surveyed in
    `docs/chemical-analysis-options.md`. IMS is the right target;
    e-nose is the scrappy version.

29. **Mobile app.** Probably PWA-shaped first — installable, push
    notifications when someone follows you or labels a wine you
    labeled.

30. **Better moderation tooling.** Once we have any real abuse:
    spam classifier on label text, image-rec model on wine
    submissions, abuse-report queue with admin UI.

31. **Search-by-image.** Snap a wine label, OCR producer + vintage,
    auto-match to canonical. Big UX win, big computer vision
    investment.

32. **Public API.** Right now everything is HTML + HTMX. A real
    `/api/v1/recommend` JSON endpoint would let third parties
    build on top.

---

## Estimated time-to-launch-ready

If we did just Tier 1: **~3-5 working days** of focused work.

That gets us:
- Stage + prod isolation
- Real Clerk instance (no dev banner)
- Webhook-driven user deletion (closes the GDPR loop)
- Rate limits
- Basic content moderation

After Tier 1 it's safe to point real users at the site (e.g., post
to /r/wine, share with sommelier friends) without breaking the
"private data goes nowhere" promise.

Tiers 2-3 (~2 weeks total) get us to "I'd be comfortable having a
journalist write about this site."

Tiers 4-5 are quarterly+ horizon and depend on whether there's
actual traction.
