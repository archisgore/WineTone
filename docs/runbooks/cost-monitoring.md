# Runbook: Cost Monitoring & Billing Alerts

*Current recurring cost: ~$25/mo (HF Pro $9 + Neon Launch tier ~$19).*

Five providers can charge us; one is the hard floor that comes
out of pocket whether or not anyone visits the site, the others
scale with traffic. Set the alert at each so a 10× cost spike
emails you before it shows up on a card.

---

## At a glance

| Provider | Today's tier | Monthly floor | Alert threshold (suggested) | Where alerts go |
|---|---|---|---|---|
| **Hugging Face Pro** | Pro | $9 flat | — (flat fee, no surprise growth) | account email |
| **Neon Postgres** | Launch (paid) | ~$19 base + storage | $50 / mo | account email |
| **Clerk** | Hobby (free) | $0 ≤ 10K MAU | 80% of 10K MAU = 8,000 users | account email |
| **HF Inference Providers** | Pro credits | included up to monthly cap | 90% of monthly credit pool | account email |
| **Cloudflare** | Free tier (DNS only) | $0 | — | n/a unless we activate paid features |

---

## Hugging Face Pro

Flat $9/mo for the account. Doesn't auto-upgrade.

**What to monitor:**
- HF Inference Providers usage (router + narrator on `/ask` consume
  credits). Each call is ~600 input + 200 output tokens. At Llama-3.1
  rates that's roughly 800 micro-credits per call. The Pro plan
  includes a substantial monthly credit pool but it isn't infinite.
- Space hardware is `cpu-basic` (free with Pro). If we ever upgrade
  to T4-small or above, that's $0.40-1.50/hr **billed per second the
  Space is alive** — leave a paid Space running for a month and
  you've added $300-1,000.

**Where to set the alert:**
1. https://huggingface.co/settings/billing
2. "Usage notifications" — set 80% and 100% of the inference credit pool.
3. There is no global "stop me from spending more than $X" knob. The
   only protection is the credit pool itself (transactions auto-fail
   at the cap rather than auto-overage).

**Fail mode:** the LLM router has a graceful fallback. If Inference
returns "no credits", routes pass the raw query through to the
embedding search. The site keeps working; only the conversational
narrator goes silent.

---

## Neon Postgres (Launch tier)

Base ~$19/mo gets us ~10 GB storage + 300 compute-hours per project.

**What to monitor:**
- **Storage growth.** Today 580 MB. Adding ~150 MB per
  encoder-rotation (each Re-encode rewrites all 164K rows × ~3 KB
  vector text). Once Phase-A user-submission picks up steam, real
  growth tracks user_labels + user_label_embeddings, both small per
  user (~1 KB).
- **Compute hours.** Every active SQL connection burns autoscaling
  CU. Idle compute is auto-suspended after 5 min — so traffic spikes
  are the cost driver, not idle time.
- **Egress.** Reading 164K embeddings on every `recommend()` call is
  ~250 MB over the wire if there were no cache. There's no cache.
  At current traffic this is fine; at 10× it might matter.

**Where to set the alert:**
1. https://console.neon.tech → project settings → **Usage limits**.
2. Configure soft + hard ceilings on each of: storage, compute hours,
   data transfer.
3. The Neon Launch plan auto-bills overages. Set the soft alert at
   80% of your comfort budget; the hard alert at 100% triggers
   project suspension.

**Fail mode:** soft limit alerts via email. Hard limit suspends the
project (the live site goes 503). Set sane numbers.

---

## Clerk (Hobby free tier)

Free up to **10,000 monthly active users (MAUs)**. We're nowhere near
that today. The wall is at $25/mo above 10K MAU and scales from there.

**What to monitor:**
- MAU count in the Clerk dashboard.

**Where to set the alert:**
1. https://dashboard.clerk.com → your production instance → **Settings → Billing**.
2. No first-party alert mechanism. Workaround: a calendar reminder to
   check MAU once a month, or use a Zapier/IFTTT scrape if it
   becomes load-bearing.

**Fail mode:** Clerk does NOT block sign-ups at the limit — they
auto-upgrade you to the paid tier. Surprise bill.

---

## Cloudflare

Free tier (DNS-only, no proxy). No charges today.

**What would trigger charges:**
- Switching `tone.wine` to proxied DNS (orange cloud) — still free
  unless we cross the Workers / R2 / Stream paid thresholds.
- Adding Cloudflare Web Analytics with a paid tier (we use the free
  basic).
- Cloudflare Workers / R2 if we ever pre-warm static assets there.

**Where to set the alert:**
- https://dash.cloudflare.com → Billing — currently nothing to alert on.

---

## What I do every month

1. Open Neon console → glance at usage charts. Storage trending,
   compute-hours total.
2. Open HF Pro account → check Inference Provider credit-burn rate.
3. Glance at Clerk MAU count.
4. Total: ~10 minutes. If anything is unexpectedly high, dig into
   that provider's invoice for the line item.

Set a recurring calendar reminder labeled "WineTone cost check"
for the 1st of the month.

---

## Emergency: hitting a hard limit unexpectedly

The escalation order:

1. **HF Pro inference credit out of pool.** `/ask` falls back to
   raw-query passthrough. Annoying but not broken. Wait for the
   monthly reset OR upgrade to a paid Inference Providers plan
   ($X for X-hundred-K extra tokens).
2. **Neon hits a hard limit.** Site goes 503 on writes. Either bump
   the limit immediately (UI), or move read traffic to a Neon
   read-replica branch.
3. **Clerk hits MAU cap.** Auto-upgrade hits. Surprise $25-100. Not
   a service outage, just a bill.
4. **Cloudflare bandwidth.** N/A today — we don't use them for
   bandwidth.

None of these scenarios block the site permanently; all are billable
overages that resolve when you authorize the spend or wait for the
next month.
