# Runbook: Two-Stage Deploy (stage → prod)

*Adopted 2026-05-22 after the Clerk-prod migration day caused live
disruption with the CSP / CAPTCHA bug and the user-creation 500.
Both would have been caught on staging in five minutes of
click-through.*

The pipeline: every change ships to **stage** first, gets clicked
through at `stage.tone.wine`, then gets fast-forwarded to **prod**
when it looks good.

---

## The two environments

| | Prod | Stage |
|---|---|---|
| Git branch | `main` | `stage` |
| HF Space | `archisgore/winetone` | `archisgore/winetone-staging` |
| URL | `https://tone.wine` | `https://stage.tone.wine` (DNS pending) + `https://archisgore-winetone-staging.hf.space` |
| Neon DB branch | `main` (compute: `ep-misty-cell-ap9jcmx1`) | `stage` (compute: `ep-little-dream-aps7sdvu`) |
| Clerk instance | Production (`clerk.tone.wine`) | Development (`united-stork-42.clerk.accounts.dev`, `pk_test_*` / `sk_test_*`) |
| Hardware | `cpu-basic` (free with Pro) | `cpu-basic` (also free) |

Cost: $0 incremental — both Spaces use the free `cpu-basic` tier
included with the HF Pro account; Neon branches are free on the
Launch plan; the dev Clerk instance is free under 10K MAUs.

---

## Day-to-day flow

```bash
# (1) Work happens on the stage branch.
git checkout stage
# … make changes …
git commit -m "feat: foo"
git push origin stage
# → archisgore/winetone-staging rebuilds (~3 min).
# → click around at stage.tone.wine or the HF .hf.space URL.
# → file Sentry events, watch /healthz, do the actual flow you changed.

# (2) Promote when stage looks good.
git push origin stage:main
# → in a terminal where HF_TOKEN is set:
.venv/bin/python -c "
from huggingface_hub import HfApi
HfApi().restart_space('archisgore/winetone', factory_reboot=True)
"
# → prod rebuilds (~3 min) and serves the same code that stage just verified.
```

That's the standard happy path. The whole loop is one push for stage,
one push + one reboot for prod.

---

## When can I skip stage?

For documentation-only commits (anything under `docs/`, `README.md`),
or for `.github/workflows/` changes that don't touch app code,
pushing direct to `main` is fine — those can't introduce a runtime
regression. Everything else goes through stage first. When in doubt:
stage.

---

## Promotion checklist

Before `git push origin stage:main`, click through:

1. **Sign-in flow.** Hit stage in an incognito window. Sign in with
   a throwaway dev-Clerk account. The Clerk modal renders without
   the CSP-blocked-CAPTCHA error.
2. **Landing page authenticated.** After sign-in, the dashboard
   renders without 500. (This is the bug that hit us on prod-day.)
3. **Add a label and refit.** Confirm the calibration round-trip
   works. The label list updates; the fit-status flips to "fitted."
4. **Any feature you changed.** If you changed the CSP, hit a route
   that loads a third-party script. If you changed the schema, check
   that the new column / index is present in the stage Neon branch.
5. **`/healthz`** returns 200 + reasonable DB latency.
6. **CI is green** on the stage commit (`gh run list --branch stage`).

If any of these fail, fix on stage, don't promote.

---

## Rollback

Whatever you just promoted is broken on prod. Don't panic.

```bash
# Find the last-good commit on main.
git log --oneline main | head -10

# Force prod back to it.
git push --force-with-lease origin <last-good-sha>:main

# Reboot prod against that older code.
.venv/bin/python -c "
from huggingface_hub import HfApi
HfApi().restart_space('archisgore/winetone', factory_reboot=True)
"
```

Prod is back to the previous code in ~3 minutes (Space rebuild time).
The bad commit is still on `stage` for diagnosis.

The `--force-with-lease` (rather than plain `-f`) is mandatory — it
refuses the push if anyone else's commit landed on main between your
last fetch and the rollback. There shouldn't be anyone else, but the
safety belt is free.

---

## Resetting the stage Neon branch

Stage data drifts (test accounts, broken migrations, mid-experiment
schema). To reset stage back to a clean copy of prod:

1. Neon console → branches → delete the `stage` branch.
2. Create a new branch off `main`, name it `stage`.
3. Copy the new compute's connection string.
4. Update the `archisgore/winetone-staging` Space's `DATABASE_URL` /
   `WINETONE_DB_URL` secret. (Or the `WINETONE_DB_URL` if that's the
   name — match what the app reads.)
5. Factory-reboot the staging Space.

Takes ~3 minutes. The reset is non-destructive to prod (different
compute, different storage) — you can do it any time.

---

## What the staging Space's secrets are

Set in the HF Space's **Settings → Variables and Secrets**:

| Secret | Value source |
|---|---|
| `DATABASE_URL` (or `WINETONE_DB_URL` — whichever the app reads) | Stage Neon branch connection string |
| `CLERK_PUBLISHABLE_KEY` | dev Clerk instance `pk_test_*` |
| `CLERK_SECRET_KEY` | dev Clerk instance `sk_test_*` |
| `CLERK_WEBHOOK_SECRET` | dev Clerk's webhook endpoint signing secret. Create a separate endpoint in dev Clerk pointed at `https://stage.tone.wine/webhooks/clerk` (or `archisgore-winetone-staging.hf.space/webhooks/clerk` until DNS lands) — same shape as the prod webhook setup. |
| `HF_TOKEN` | Your HF token (read-only is fine; the Space only needs to pull the model) |
| `SENTRY_DSN` | Either reuse prod's DSN with a stage-tagged environment, or create a separate Sentry project for stage. **Default to no DSN on stage** — stage exceptions are signal-noise, not actionable alerts. |
| `CF_ANALYTICS_TOKEN` | Don't set on stage — we don't want stage traffic polluting prod analytics. |
| `ADMIN_CLERK_USER_ID` | Your dev-Clerk user ID, if you want `/admin/reports` accessible on stage. |
| `WINETONE_ENV` | `staging` — informational; routes that want to behave differently (e.g., a "STAGING" banner) can check this. Not used in code today; reserved. |

The Space pulls source from the **`stage`** branch of `archisgore/WineTone`
(not main). This is configured in the Space's Dockerfile / git clone
step — verify it points at `--branch stage` before considering setup
complete.

---

## What I'd add later

These are deliberately not in v1 because they add complexity
without solving a real problem yet:

- **Auto-promotion on green CI.** Not yet — manual promotion is the
  guard. A misbehaving feature that passes our tests is still likely
  to need a click-through before going to prod.
- **A "STAGING" visual banner.** Easy to add when stage gets confused
  with prod. For now the URL and the dev-mode Clerk banner make it
  obvious which is which.
- **Per-PR preview deploys.** Overkill for one-person development.
  When someone else starts contributing, revisit.
