# Auto-retrain plan

*Continuous self-improvement of WineTone's user projections and wine
encoder, with no manual prompting. Designed 2026-05-25 alongside
the MLP and encoder fine-tune scaffolding.*

---

## What this is

A pipeline that periodically refits **per-user projections** (the
MLP that maps each user's words → wine-embedding space) **and**
continue-trains the **wine encoder itself** (a LoRA adapter on top
of `bge-small-en-v1.5`) on user-contributed (description, wine)
pairs. Both happen on a cron schedule via GitHub Actions; no human
needs to push a button for the system to get smarter.

## Why

1. **The system is supposed to learn from use.** Every label a user
   writes is a (description, wine) training pair. Without
   auto-retraining, that signal sits in the DB until I manually
   re-run `winetone calibrate fit`.
2. **User-added wines are pure HyperLanguage_users signal.** Wines
   submitted via `/wines/new` have *no* professional-reviewer prose
   — their entire description corpus is user vocabulary. That's
   exactly what the encoder should learn from.
3. **"Set and forget" is the robustness test.** If the pipeline
   needs manual oversight, it isn't really robust. Cron-driven
   retraining proves the artifact lifecycle works end-to-end.

## Two artifacts, two cadences

| Artifact | What it is | Refit when | Cost |
|---|---|---|---|
| **Per-user MLP** | 384 → 128 → 384 residual MLP, one per user. Replaces the linear `A·L + b`. | A user has ≥ 5 new labels since last fit, OR weekly fallback cron. | < 1 min per user, CPU. |
| **Encoder LoRA** | LoRA adapter on `bge-small-en-v1.5`, trained on all user pairs across all users. | Total user_labels crosses growth threshold (next 100, 500, 2000…), OR monthly cron. | ~30 min CPU on GHA. |

The MLP is cheap and per-user — refit aggressively. The encoder is
expensive and shared — refit conservatively, and only when there's
enough new signal to justify the recompute of all 164k wine
embeddings that follows.

## Trigger options

**Yes, GitHub Actions cron supports this.** The relevant block:

```yaml
on:
  schedule:
    - cron: '0 6 * * *'    # daily, 06:00 UTC — fire often, decide cheaply
  workflow_dispatch: {}     # manual trigger button in the UI
```

Cron resolution is "approximately on time" — GHA may delay up to ~15
min under load. For private repos, the free tier is **2000 min/month**.
The daily cadence runs ~30 invocations per month, and ~28 of them
will exit in <30 seconds (see "change-gated execution" below); only
the 1-2 actual retrain runs take real wall time. Total estimated burn:
~120 min/mo, well under free tier.

### Why daily, not weekly

The first version of this plan said "weekly cron, run the full
retrain". The user pushed back: he doesn't want to monitor for when
labels accumulate enough to be worth retraining. So instead:

- **Cron fires daily.**
- **The job's first step is a cheap change-detection query.**
- **If no meaningful change since last successful retrain, the job
  exits in seconds with `result: no-op`.**
- **Otherwise it proceeds to backup + retrain.**

The cost of an idle daily cron (~10 seconds × 30 = 5 min/month) is
negligible against the 2000-min budget, and removes any need for me
to remember "has it been a week".

### Change-gated execution

We compute a small `retrain_state` row that captures the current
shape of the training data:

```sql
CREATE TABLE retrain_state (
  artifact      TEXT PRIMARY KEY,    -- 'mlp' | 'encoder'
  last_run_at   TIMESTAMP NOT NULL,
  last_result   TEXT NOT NULL,       -- 'promoted' | 'skipped' | 'aborted'
  -- The signature we compare against for change detection.
  -- See scripts/retrain.py::compute_signature() for the spec.
  signature     JSONB NOT NULL
);
```

The `signature` field is a JSON object including:

```json
{
  "total_labels": 412,
  "total_users_with_labels": 18,
  "user_added_wines": 7,
  "labels_hash": "sha256:...",     // hash of sorted (label_id, description) tuples
  "max_label_ts": "2026-05-25T..." // newest label timestamp
}
```

`labels_hash` is the key trick: any insert, update, or delete in the
`user_labels` table flips it, because the hash is computed over the
*set* of (id, description) tuples. So we naturally detect all three
mutation types the user called out (added / deleted / edited).

**Per-artifact thresholds:**

| Artifact | Triggers retrain when… |
|---|---|
| MLP refits | ≥ 1 user has new/edited/deleted labels since last fit (per-user check, not global) |
| Encoder fine-tune | `total_labels` grew by ≥ 50 since last successful encoder run, OR `labels_hash` changed and ≥ 30 days since last run |

A side benefit: the change signature gives us *cost control*. The
encoder rebuild is the expensive step; we want it to fire only when
the marginal information from new data exceeds the cost of recomputing
164k wine embeddings.

### Auto-refit for forgetful users

A specific user-facing promise this design makes: **users never need
to run `winetone calibrate fit` manually**. If a user adds, edits, or
deletes labels via the app, the next daily cron tick picks up the
change and refits their per-user MLP automatically.

The mechanism: per-user `labels_sig` on `user_projections_mlp` (hash
of that user's `(wine_id, description, created_at)` tuples). On each
cron tick, `calibrate_mlp.refit_users_with_changes()` compares the
live sig to the stored one — any mismatch triggers a refit. So an
edit that changes only a description (no row count change, no
created_at change) still flips the hash and still triggers refit.

This is the "I forgot" path. The "I'm impatient" path is the
manual `winetone calibrate fit --backend mlp -u <user>` CLI, which
still works for ad-hoc refits.

### Webhook escape hatch

If a user adds 50 labels in a single session and doesn't want to wait
for the next daily cron, the app can also `POST` to GitHub's
`workflow_dispatch` API to fire the workflow immediately. Punt this
until the daily cron version is proven; it's an obvious extension.

## Backups — user-labelling data must survive a Neon outage

The user labels are the irreplaceable signal in this whole system.
Neon is reliable, but it's a single vendor, and the data is small
enough (kilobytes per user, megabytes total even at large scale)
that there's no excuse for relying on a single store.

### Where backups go: private GitHub repo (`archisgore/winetone-labels-backup`)

Switched 2026-05-30 from HF Dataset Hub → GitHub. Archis tracks
everything in GitHub, so the backup living next to the source repo
makes operational sense. The GitHub repo is private; pushes are
authenticated via a fine-grained PAT (`BACKUP_REPO_TOKEN` repo
secret) scoped only to the backup repo.

Why GitHub works here:

1. **Different vendor from Neon.** A Neon outage doesn't take both.
2. **Git history.** Every snapshot is a commit; we can restore any
   day's state.
3. **One auth surface** for the whole project — same `gh` CLI, same
   PR/commit workflow as everything else Archis does.
4. **Easy export.** `git clone` pulls the whole thing locally for
   offline analysis.

Size headroom: GitHub flags >50 MB per file and rejects >100 MB
without LFS. Today's total backup is ~0.6 MB (projections are the
biggest, ~600 KB). Even with 10K users × 100 labels each, parquet
compression keeps the total well under GH's free-tier limits.

### What gets backed up

Pseudonymized by design (2026-05-30 redesign): every row references
the user only via their `masked_user_id` (a random UUID minted at
user creation), never via `user_id`. The `users` table — the only
place that maps `masked_user_id` → display_name / email /
clerk_user_id — is **excluded entirely from the backup**.

| File | Contents |
|---|---|
| `user_labels.parquet` | masked_user_id, wine_id, description, sentiment, created_at |
| `user_projections.parquet` | masked_user_id, n_labels, A, b, fit_at (linear projection) |
| `user_projections_mlp.parquet` | masked_user_id, n_labels, weights, fit_at, loss, arch_id, labels_sig |
| `follows.parquet` | follower_masked_id, followee_masked_id, weight, created_at |
| `wines_user_added.parquet` | wine catalog row + `submitted_by_masked_id` (catalog fields are not PII) |
| `user_audit_log.parquet` | event_id, masked_user_id, event_at, event_type, field, source, request_id — **no `old_value` / `new_value`** because those can carry a previous display_name |
| `manifest.json` | schema version (`v2-pseudonymized`), row counts, snapshot timestamp, source DB host |

We deliberately *do not* back up:

- The `users` table itself (the PII source — never makes it off Neon)
- `user_label_embeddings` (regenerable from label text)
- The full 164k canonical `wines` catalog (re-pullable from public sources)
- The `old_value` / `new_value` fields in `user_audit_log` (potential PII)

### What the GDPR property looks like

When a user requests deletion:
1. Clerk fires the `user.deleted` webhook
2. Our handler runs `DELETE FROM users WHERE clerk_user_id = ?`
3. The user's `masked_id` no longer maps to any identity, anywhere
4. Every backed-up row keyed by their `masked_user_id` becomes a
   permanent orphan — no path back to a real person, even if
   someone had every snapshot we've ever taken

### Cadence

- **Daily, separate workflow.** `backup.yml` runs at 03:00 UTC every
  day, independent of the retrain workflow. Always runs, no gating —
  backups are cheap (a few MB at most for the foreseeable future).
- **Retention:** the HF git history retains every commit, so we keep
  *all* history by default. If the repo ever gets too big (multi-GB
  range), prune via `huggingface_hub` API to keep last 90 days.

### What restoring looks like

If Neon goes down and we have to rebuild the DB elsewhere:

```bash
huggingface-cli download archisgore/winetone-labels-backup \
   --repo-type dataset --local-dir /tmp/wt-restore
psql "$NEW_DB_URL" -c "..."   # create empty schema
python -m winetone.tools.restore_from_backup /tmp/wt-restore
```

The `restore_from_backup` helper is part of this plan but punted to
build-out until we actually need it; the manifest contains enough
schema info that the restore is straightforward.

### What this *isn't*

This isn't a hot replica — there's an up-to-24h gap between failure
and last snapshot. For a wine-labelling app at WineTone's stage,
that's fine. If/when this becomes a paying-customer product, we
revisit: Neon's PITR + cross-region read-replicas + an external
snapshot at hourly cadence.

## Where artifacts live

| Artifact | Storage | Why |
|---|---|---|
| MLP weights | `user_projections_mlp.weights BYTEA` on Neon | Tiny (a few KB per user), needs row-level access from the runtime. |
| LoRA weights | HF model repo `archisgore/winetone-encoder` | Versioned, downloadable from the Space at boot, separate from the Space's git. |
| Wine embeddings | `wine_embeddings` pgvector table (existing) | Same as today — the encoder swap regenerates this column-by-column or full-rebuild. |
| Validation metrics | `encoder_retrain_history` table | Append-only log of every retrain run + accuracy numbers, so we can plot improvement over time. |

## Data flow per retrain run

1. **GHA cron fires** (daily, or manual dispatch).
2. **Compute change signature.** Read `retrain_state` row.
   Compare current `labels_hash`, `total_labels`, etc. to last
   successful run. If unchanged → exit `result: no-op` in <10s.
3. **Per-user MLP refit** (only for users whose label set changed).
   Run `calibrate_mlp.fit(user_id)`. Heavy weight-decay so tiny
   per-user data doesn't overfit. Write each MLP's state_dict back
   to `user_projections_mlp`.
4. **Encoder readiness check.** Two gates, both must pass:
   - Total labels ≥ 500 (data-volume gate, prevents underfit).
   - Either: +50 labels since last encoder run, OR `labels_hash`
     changed and ≥ 30 days since last encoder run.
   Otherwise: skip encoder step, write `result: mlp-only` to state.
5. **Encoder fine-tune** (only when threshold met):
   - Gather all (description, wine_id) pairs.
   - Upweight pairs where the wine has `submitted_by_user_id IS NOT NULL`
     by **5-10×** (pure HyperLanguage_users signal).
   - Hold out 10% for validation.
   - Train LoRA on 90% with a contrastive loss
     (`MultipleNegativesRankingLoss` from sentence-transformers).
   - Validate: top-10 accuracy on the held-out set, compared to the
     currently-deployed encoder.
6. **Validation gate.** If `accuracy_new < accuracy_old - 0.02`,
   **abort**. Log the regression to `encoder_retrain_history`, do
   not promote. Notify on failure channel.
7. **Promote.** Push LoRA weights to HF `archisgore/winetone-encoder`.
   Regenerate `wine_embeddings` for the whole corpus using the new
   encoder. This is the expensive part — ~10 min for 20k sampled wines,
   ~2 hours for the full 164k. Stage in a sidecar column
   (`wine_embeddings_v2`), then atomic swap once the rebuild
   succeeds.
8. **Cascading per-user refit.** All MLPs are now stale because the
   wine-embedding target space shifted. Refit them all against the
   new corpus (this is the small/fast step again).
9. **Notify.** Slack/email summary: # MLPs refit, encoder changed
   y/n, accuracy delta, embeddings touched, total runtime.

## Validation gate (the bit that prevents regressions)

The encoder swap is the only step that can silently degrade quality.
The gate:

- **Held-out set:** 10% of `(description, wine_id)` pairs, stratified
  by user so no user is entirely held out.
- **Metric:** *top-10 accuracy* — for each held-out description, is
  the correct wine_id in the top-10 nearest neighbors in the new
  embedding space?
- **Baseline:** the same metric, computed against the **currently
  deployed** encoder (loaded from HF before the rebuild). This is
  the apples-to-apples comparison.
- **Acceptance rule:** new ≥ old − 0.02. (Noise floor; a 2pp drop on a
  ~500-pair holdout is barely significant but at least it gives us a
  fixed criterion.)
- **Logged:** every run writes a row to `encoder_retrain_history`
  whether it promoted or not, so we can plot the encoder's quality
  trajectory.

If/when validation fails, the only side effect is a logged metric.
No DB writes, no Space restart. Roll-forward = next week's cron.

## Failure handling

- **DB writes per user are independent.** A bad MLP fit for user X
  doesn't poison user Y.
- **Encoder rebuild is two-phase.** New weights → sidecar
  `wine_embeddings_v2` → atomic schema swap. If the rebuild dies
  half-way, the live `wine_embeddings` is untouched.
- **GHA timeouts:** each job step has a generous `timeout-minutes`.
  If the encoder step blows past 50 min, fail and notify.
- **Notification channel:** `SLACK_WEBHOOK_URL` repo secret if set,
  else email via GHA's default failure notification.

## Repo secrets needed

Stored as **environment secrets** on a GHA environment named
`production` (restricted to the `main` branch), not as flat repo
secrets — so a misconfigured feature-branch workflow can't resolve
them.

| Secret | Purpose |
|---|---|
| `WINETONE_DB_URL` | Neon prod connection string (read + write per-user MLP rows + retrain_state). |
| `HF_TOKEN_WRITE` | Push permission on `archisgore/winetone-encoder` *and* `archisgore/winetone-labels-backup`. |

Failure notifications: GHA emails the repo owner by default when a
scheduled workflow fails — no Slack webhook is wired up. If we ever
want per-run summary pings (success + failure), add a Slack or
Discord webhook here and a notify step at the end of each workflow.

`HF_TOKEN_WRITE` is intentionally separate from the existing
`HF_TOKEN` used to deploy the Space. That one is read-only on the
model repo. We don't want a Space deploy and a model-repo push to
share a credential.

## Cost

- **GHA compute:** ~120 min/mo, well under the 2000 min free tier
  for private repos.
- **Neon DB:** existing connection, no incremental cost.
- **HF model repo:** free for public model repos; we'd publish the
  LoRA there (the base encoder is BAAI's, our delta is small, no
  privacy issue with publishing it). If we want it private,
  Pro tier (already paying for Spaces) includes private model repos.
- **Recomputing wine_embeddings:** CPU on GHA can do the 20k-sample
  rebuild in ~10 min. Full 164k corpus would need an order of
  magnitude more time — ideally we'd push this to a separate
  longer-running job (Modal, Lambda, or run it locally) once we
  cross the user-volume threshold to need full embeddings.

## When this turns on (phased rollout)

| Phase | Trigger | What runs | Why |
|---|---|---|---|
| **1 — soon, today's work** | MLP module + GHA cron live, encoder step *skipped* by the threshold check. | Weekly per-user MLP refits. | Proves the cron + DB-write loop works. Cost is near-zero (refitting 2 users takes seconds). |
| **2 — at 500 total labels** | First encoder fine-tune. Manual `workflow_dispatch` initially, to inspect the validation output before letting cron auto-promote. | LoRA on 90% / validate on 10%. | First real encoder change. Want a human in the loop the first time before fully trusting the gate. |
| **3 — at 2000 total labels** | Automatic encoder fine-tune on cron. | All steps end-to-end with no human. | The system is now genuinely self-improving. |

## Open questions worth deciding *before* this turns on for real

1. **Sample size for wine_embeddings rebuild.** Today we embed a
   20k stratified sample, not the full 164k. After encoder swap,
   do we (a) keep sampling and refresh the 20k, or (b) bite the
   bullet and embed all 164k? Affects GHA runtime budget significantly.

2. **Privacy:** the LoRA could memorize specific user phrasings if
   any user's labels dominate. Defense: minimum 3 distinct users
   contributing pairs before running encoder fine-tune at all,
   and a `max_pairs_per_user` cap in the training set. Probably
   safe today (we have 2 users); will matter at scale.

3. **What if a user *deletes* a label after it's been used for a
   prior encoder train?** The label is gone from `user_labels` but
   the encoder weights are already shaped by it. We accept this — the
   *next* retrain will see the new corpus and shift accordingly. The
   encoder isn't a place where deletions need to propagate
   retroactively.

4. **Schema migrations from GHA.** The retrain workflow expects
   `user_projections_mlp` and `encoder_retrain_history` to exist.
   First run after this lands needs a migration. Either: bundle
   into the GHA workflow with `init_schema()` on startup, or do a
   one-time manual `psql` from the runbook. Lean toward auto-init
   because it's the same pattern as `recommend.init_user_schema()`.

---

## See also

- `src/winetone/calibrate_mlp.py` — the MLP implementation
- `src/winetone/embed_finetune.py` — the encoder fine-tune scaffold
- `.github/workflows/retrain.yml` — the cron pipeline
- `src/winetone/calibrate.py` — the current linear projection (kept
  alongside MLP as fallback during rollout)
- The HyperLinguistics thesis behind WineTone (in `PLAN.md` and
  `docs/blog/2026-05-23-winetone-was-never-about-wine.md`) — this
  whole pipeline is the operational form of that thesis.
