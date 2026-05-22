# Runbook: Database Backup Verification

*Last reviewed: 2026-05-21. Test-restore: never (this runbook is the
first plan for one — a backup you've never tested is a wish, not a
backup).*

WineTone runs on **Neon Postgres Launch tier**, which gives us
point-in-time recovery (PITR) as a built-in feature. We don't run
our own pg_dump / S3 pipeline because Neon's branching is strictly
better for this scale: cheaper, faster to restore, and the
restored DB is byte-identical to the source instead of a snapshot
of a particular pg_dump invocation.

This runbook covers two things:
1. What we actually have (retention, scope, mechanism)
2. The quarterly test-restore procedure to prove the backups work
   before we need them to

---

## What we have

| Property | Value |
|---|---|
| Provider | Neon (Launch tier, paid) |
| Mechanism | Continuous WAL streaming + log retention |
| Retention | **7 days** of point-in-time recovery on Launch tier |
| Granularity | 1-second resolution |
| Scope | Every table in the WineTone database. No exclusions. |
| Encryption | At-rest AES-256, in-transit TLS (Neon-managed) |
| Geographic redundancy | Within Neon's region (us-east-2 today) |
| Authentication required for restore | Yes — Neon console / API; not exposed to the live app |
| Restore artifact | A **branch** (writable, independent compute) — not a flat dump |

Things that are **not** backed up via Neon PITR (because they
don't live in Neon):

- **HF Space configuration + secrets.** Set in the Space's
  Settings → Variables and Secrets. If the Space is deleted,
  re-add from `docs/runbooks/clerk-production-setup.md`.
- **The encoder model.** Lives on HF Hub at
  `archisgore/bge-small-winetone`. HF Hub has its own git-style
  versioning; the model isn't on Neon.
- **Server logs.** Hosted by HF; their retention policy applies.
- **Anything in `data/` locally.** Gitignored, not under any
  external backup. Re-derivable from the GitHub release tarballs
  + the source-record raw files if you have them.

If we ever move off Neon, this runbook also has to change.

---

## How to restore (when we actually need to)

The PITR mechanism for an emergency:

1. **Identify the timestamp you want to restore to.** Usually
   "5 minutes before the bad migration ran" or "before the
   destructive UPDATE that ate the followers table". Be precise:
   1-second resolution.
2. Go to https://console.neon.tech → project → **Branches**.
3. Click **Create branch** → choose **point-in-time** option →
   enter the timestamp.
4. Name it something obvious like `restore-2026-05-21-1830-pre-bad-migration`.
5. Wait ~30 seconds (Neon's branching is fast — it's CoW over the
   same storage).
6. Get the **connection string** from the new branch's Settings.
7. Now you have a writable, isolated copy at that exact moment in time.

What you do with the restored branch depends on the scenario:
- **Verify the bad event indeed corrupted production data:**
  diff specific tables between the branch and current main.
- **Recover specific rows:** `pg_dump` just those tables from the
  branch, restore into prod with a `WHERE` clause.
- **Full rollback:** point the Space's `WINETONE_DB_URL` at the
  branch's connection string + restart. (Make absolutely sure the
  branch has a compute attached and isn't auto-suspended.)
- **Promote the branch to main:** Neon Console → Branches → the
  restored branch → **Promote**. Old main becomes a child branch.

The **only** of these that you'd do without thinking twice is the
"diff to verify what got corrupted" one. The other three involve
production cutover decisions and should be done deliberately, not
in a panic.

---

## Quarterly test-restore (the actual verification)

The whole reason for this runbook. Once a quarter — first week
of Jan / Apr / Jul / Oct — run through the restore flow against
a non-emergency scenario, with no time pressure, while everything
still works. The goal is to:

1. Catch retention drift (did Neon silently shrink our PITR
   window from 7 days to 3 because of a plan change?)
2. Catch credential drift (does the Neon console still let us in?
   Did 2FA recovery codes get lost?)
3. Catch process drift (did the steps above become subtly wrong?)

### The procedure

1. Pick a timestamp from **24 hours ago** (well within retention,
   recent enough that data looks current).
2. Through the Neon console, create a branch from that PITR
   timestamp. Name it `test-restore-YYYY-MM-DD`.
3. Connect locally via `psql`:
   ```bash
   psql 'postgresql://...test-restore-branch...neon.tech/neondb?sslmode=require'
   ```
4. Verify row counts on the three load-bearing tables:
   ```sql
   SELECT 'wines' tbl, count(*) FROM wines
   UNION ALL SELECT 'wine_embeddings', count(*) FROM wine_embeddings
   UNION ALL SELECT 'users', count(*) FROM users
   UNION ALL SELECT 'user_labels', count(*) FROM user_labels;
   ```
5. Compare to the live main branch numbers. The branch will be
   24h stale, so live numbers should be ≥ branch numbers for
   append-mostly tables (`user_labels`, `wines` if user-submitted)
   and roughly equal for static tables (`wine_embeddings`).
6. **Delete the test branch** when done (Neon Console → branch →
   Delete). Leaving it around costs compute-hours.
7. Update the date in this file's header.

If anything in step 4-5 fails (auth fails, branch creation fails,
row counts are wildly off, the branch contains no data), file
an issue and treat that as a real outage even though we're not
in one.

### Don't skip it

The most common backup failure mode in industry is "the backups
have been silently failing for six months and we only found out
when we tried to restore." A 15-minute quarterly check is the
cheapest insurance against that being us.

---

## What happens if Neon itself goes down

This is outside Neon's PITR scope — it's about availability of
the Neon service, not about data loss.

Today's posture: **we have no warm standby outside Neon.** If
Neon has a multi-day outage we are dark for that period.
Acceptable for a research prototype; would not be acceptable for
revenue-bearing software.

If/when this matters, the upgrade path is:
- **Easy:** subscribe to a daily pg_dump → S3 / GCS via Neon's
  own connector. ~$0/mo at our data scale. Restores into any
  Postgres anywhere. Adds operational complexity, mostly
  pointless until we're past prototype.
- **Harder:** stand up a read-replica on a different provider
  (Supabase, Crunchy Bridge) with logical replication. We can
  fail over to it if Neon is down. ~1 day of work and a
  separately-billed DB.

We don't do either today, and noting that is part of the
documentation.
