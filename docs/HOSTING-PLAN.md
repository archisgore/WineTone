# Hosting plan — WineTone as a public web app

*A plan to take the local demo (`winetone serve`) and host it on the
internet, with login, persistent per-user calibration, and a UI
optimized for the calibrate → recommend loop. Bias toward low-cost
or no-cost lightweight stacks.*

---

## TL;DR — the stack I'd ship

| Layer | Choice | Cost |
|---|---|---|
| **Frontend** | Next.js 14 (app router) + Tailwind, deployed on **Vercel** | $0 (Hobby tier) |
| **Auth** | **Clerk** with magic-link email + Google OAuth | $0 (10K MAU) |
| **Backend API** | FastAPI on **Fly.io**, 1× shared-cpu-1x VM | ~$3/month |
| **Database** | **Neon** Postgres with pgvector extension | $0 (0.5GB free tier, or $19/month for 10GB) |
| **Object store** | **Cloudflare R2** for the sparse matrix + model artifacts | $0 (10GB free, no egress) |
| **Encoder** | Run on the FastAPI VM via ONNX Runtime (~100MB model) | (same VM) |
| **Domain** | `winetone.app` or similar via **Cloudflare Registrar** | ~$10/year at cost |
| **CI** | GitHub Actions | $0 (public repo) |
| **Telemetry** | **PostHog** Cloud or **Plausible** | $0 (free tier each) |

**All-in cost at single-digit-thousand-user scale: ~$5–25/month.**
At scale (~50k MAU, ~5M queries/month) you'd cross over to needing
the $19 Neon Scale plan and possibly a larger Fly VM (~$15) — call
it $50–80/month total.

Why this stack vs alternatives is detailed in
[§ Hosting choices, with reasons](#hosting-choices-with-reasons).

---

## Product requirements

### What the user can do

1. **Land** on a marketing page that explains WineTone in one
   minute and shows the "your oaky vs my oaky" hook with a small
   embedded animation or demo gif.
2. **Sign up** with email (magic link, no password) or Google OAuth.
3. **Onboard** with a one-screen explanation of the calibration
   loop.
4. **Calibrate** — search the catalog, pick wines, write their own
   descriptions. UI nudges them toward 5+ labels.
5. **Get recommended** — type a free-text query, see results.
   Optionally compare generic vs. personalized side-by-side
   ("see what changed").
6. **Manage** their labels — edit, delete, view fit history.
7. **Share** their *calibration*. A user can publish a link like
   `winetone.app/u/archis` that shows their tasting style as a
   shareable artifact. Others can clone the calibration to bootstrap
   their own profile.
8. **Export** their data on demand (every label, the projection,
   their history) — same Apache-2.0 + user-data-portability
   posture as the local repo.

### What the operator (us) can do

- Add new wine sources without forcing a full re-train.
- Re-run global re-embedding monthly when the catalog grows
  meaningfully.
- Monitor query volume + latency.
- Backfill new user-contributed descriptions into the global corpus
  on the next rebuild (per the feedback-loop design already in
  `canonicalize.py`).

---

## UI design

### Information architecture

```
/                          marketing landing + demo gif
/login                     magic-link / Google
/onboarding                first-time only, single screen
/dashboard                 the main app (auth required)
  ├ calibrate (default tab)
  │   search + label list + fit
  ├ recommend (tab)
  │   query bar + results side-by-side
  └ profile (tab)
      labels list (edit / delete)
      calibration history (versioned)
      export / delete account
/u/{username}              public read-only view of a user's profile
                           (if they've opted in to share)
/wines/{wine_id}           wine detail page
                           ("who's labeled this & what they said")
/about                     thesis + tech credits
/api/...                   REST endpoints powering all the above
```

### Visual sketch (text mockups)

**Landing (`/`)** — single fold, scroll-revealed sections:

```
┌──────────────────────────────────────────────────────────────────┐
│  WineTone                                  log in / sign up  →  │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│        Your "oaky" isn't my "oaky."                              │
│        A wine recommender that calibrates to YOU,                │
│        not the dictionary.                                       │
│                                                                  │
│   ┌────────────────┐                                             │
│   │  ▶ 30-sec demo │   [pick a username]  → try it without auth  │
│   └────────────────┘                                             │
│                                                                  │
│   ──────  How it works  ──────────────────────────────────────   │
│                                                                  │
│    1 · You label 5 wines in your own words                       │
│    2 · WineTone learns your vocabulary                           │
│    3 · Ask for "earthy with jasmine" and get YOUR meaning of it  │
│                                                                  │
│   ──────  The demo  ─────────────────────────────────────────    │
│                                                                  │
│   [animated side-by-side: same query, generic vs. personalized]  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

**Dashboard — calibrate tab** (the daily-use surface):

```
┌──────────────────────────────────────────────────────────────────┐
│  WineTone     calibrate  recommend  profile           ⚙ logout  │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│   👋  archis · 6 labels · fit v3 (mlx) · ‖A−I‖=4.5               │
│                                                                  │
│   ┌── Add a wine ────────────────────────────────────────────┐   │
│   │  search: [ Barolo                              ] 🔍       │   │
│   │                                                            │   │
│   │  ▸ Beni di Batasiolo Boscareto · Nebbiolo · Italy         │   │
│   │  ▸ Mario Marengo Barolo (2008) · Nebbiolo · Italy         │   │
│   │  ▸ Viberti Buon Padre (Barolo) · Nebbiolo · Italy         │   │
│   │     ┌──────────────────────────────────────────────────┐  │   │
│   │     │ what does this wine taste like to YOU?            │  │   │
│   │     │ ─────────────────────────────────────────────────│  │   │
│   │     │ tar and roses, oaky like a Pottery Barn,         │  │   │
│   │     │ jasmine on the finish                             │  │   │
│   │     └──────────────────────────────────────────────────┘  │   │
│   │                                          [add label]      │   │
│   └────────────────────────────────────────────────────────────┘   │
│                                                                  │
│   ┌── Your labels (6) ──────────────────────────────────────┐    │
│   │  Beni di Batasiolo · "tar and roses, grippy..."   ✏ 🗑   │    │
│   │  Domaine Jessiaume · "earthy and quiet..."        ✏ 🗑   │    │
│   │  ...                                                      │    │
│   └──────────────────────────────────────────────────────────┘    │
│                                                                  │
│   ┌── Fit your taste profile ──────────────────────────────┐     │
│   │  [⚙ refit now]    auto-refit on every add: ◉ on  ○ off │     │
│   └────────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────┘
```

**Dashboard — recommend tab**:

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│   describe what you want:                                        │
│   ┌────────────────────────────────────────────────────────────┐ │
│   │  earthy and oaky with jasmine notes                       │ │
│   └────────────────────────────────────────────────────────────┘ │
│                                                                  │
│   filters: country [_____]  variety [_____]  α ●━━━○━ 0.6        │
│                                                                  │
│                              [Recommend →]                       │
│                                                                  │
│   ─────────────────────────────────────────────────────────────  │
│                                                                  │
│   Generic baseline              Personalized for archis 🟢       │
│   ┌────────────────────┐        ┌──────────────────────────┐    │
│   │ Rooster Hill (US)  │        │ Beni di Batasiolo (IT) 🍷│    │
│   │ Ravenswood (US)    │        │ Mario Marengo (IT) 🍷    │    │
│   │ Jarvis (US)        │        │ Viberti Barolo (IT) 🍷   │    │
│   │ ...                │        │ Claverana Olo (IT) 🍷    │    │
│   └────────────────────┘        └──────────────────────────┘    │
│                                                                  │
│        ─────────  "your oaky moved here"  ─────────              │
└──────────────────────────────────────────────────────────────────┘
```

The side-by-side is *the* visual moment. Don't bury it.

### Empty / cold states

- **No labels yet**: cold-start prompts the user to add their first
  wine. Suggest 3–5 starter labels they can clone ("not sure
  where to start? clone archis's calibration").
- **First fit**: animate the projection drift bar (`‖A−I‖`) growing
  from 0 — small delight.
- **Generic == personalized for first few labels**: explain that
  the calibration is still close to identity. Set expectations.

### Mobile considerations

- Side-by-side recommend table collapses to vertical stack on
  < 768px.
- Search results use full-screen sheet on mobile.
- "Add label" textarea expands to fill — labels are written on the
  go, often after a meal.

---

## Hosting choices, with reasons

### Frontend: Next.js on Vercel

- **Why Next.js**: React server components keep most rendering on
  the edge; great for marketing pages + the dashboard.
- **Why Vercel** over alternatives:
  - **Cloudflare Pages**: cheaper at scale but worse Next.js
    DX (some app-router features don't work yet on CF runtime).
  - **Netlify**: comparable to Vercel, slightly slower cold
    starts on the free tier.
  - **GitHub Pages**: static only — won't work for our app.
  - **Self-host on Fly.io**: viable but you give up the edge CDN.
- **Free tier limits**: 100GB bandwidth/mo, 100 deployments/day,
  custom domains — plenty for low-thousand-user scale.

### Auth: Clerk

- **Why Clerk**: nicest DX of the auth-as-a-service options.
  Magic-link, OAuth, multi-factor, user metadata, prebuilt React
  components. ~5 lines to integrate with Next.js.
- **Free tier**: 10,000 MAU. Cliff at ~$25/month for 10K + ¢/MAU
  scaling. At ~30K MAU you're paying ~$25 + ~$5/extra-1K-MAU.
- **Alternatives**:
  - **Supabase Auth** (bundled with their Postgres) is fine and
    lock-in-free, but Clerk's pre-built React components are
    significantly nicer than Supabase's, and we're not using
    Supabase for the DB (see below).
  - **Auth.js (NextAuth)** is self-hosted — no service to pay for,
    but you manage sessions yourself. Worth it only if you really
    want zero third-party deps.
  - **Magic Link via Resend + custom code**: minimal but ~150
    lines of session-management code. Skip unless we're
    minimum-deps-religious.

### Backend: FastAPI on Fly.io

- **Why FastAPI**: it's what the local demo already runs. Zero
  rewrites. We can ship the same `winetone.web.app` module to
  production.
- **Why Fly.io**:
  - Stateful workload (the encoder is in-memory, the DB is
    talked-to). Vercel Edge Functions / Cloudflare Workers
    have execution-time limits that the encoder bumps into.
  - Fly's smallest VM (shared-cpu-1x, 256MB RAM) is **free**
    for hobby; 1GB RAM is ~$3/month.
  - Fly has automatic suspend-on-idle which keeps costs near
    zero for low-traffic apps.
  - Multi-region trivial later when we want global low latency.
- **Alternatives**:
  - **Render**: free tier sleeps after 15min idle. Cold-start
    feels bad for a recommender. Avoid.
  - **Railway**: pay-as-you-go from minute one. Slightly more
    expensive than Fly at low scale.
  - **Modal**: pay-per-call, great for ML but bills for the
    encoder load time on each cold start.
  - **HuggingFace Spaces**: free with Gradio/Streamlit, but
    constrains the UI; we want our own React frontend.
  - **AWS Lambda + container image**: works but Lambda's
    cold-start tax on the encoder load (~2s) is meh.

### Database: Neon Postgres

- **Why Neon**:
  - Serverless Postgres with **autosuspend** when idle — DB cost
    drops to literally pennies/day when you have no traffic.
  - First-class **pgvector** support; we don't lose the embedding
    similarity-search story.
  - 0.5GB free (164K canonical wines fit; embeddings need careful
    storage — see below).
  - Postgres wire-compatible — SQLAlchemy + psycopg works
    unchanged from the local CedarDB setup.
- **Storage math**:
  - Canonical wines table: ~50MB
  - source_records (the review text): ~150MB
  - wine_features: ~80MB
  - **wine_embeddings**: 164K × 384 × 4 bytes = **~250MB raw**;
    pgvector adds overhead. Realistic: ~350MB.
  - sparse_index (just IDs): ~6MB
  - **Total**: ~640MB → just over the 0.5GB free tier.
  - **Recommended plan**: Neon Launch ($19/month) gives 10GB and
    autoscaling.
- **Alternatives**:
  - **Supabase Postgres**: 500MB free, also has pgvector. We'd
    use it if we wanted their bundled Auth — but we chose Clerk.
  - **CockroachDB Serverless**: 5GB free, no pgvector support.
  - **PlanetScale**: MySQL only, killed their free tier in 2024.
  - **CedarDB self-hosted**: not currently offered as managed.
    Would need to run on Fly.io with persistent volumes (~$5–10
    extra/month). Defer unless we hit Neon performance limits.

### Object store: Cloudflare R2

- For the **sparse TF-IDF matrix** (~30MB joblib) + the **encoder
  ONNX model** (~80MB) + future GC-MS data. Keep large blobs out
  of Postgres.
- R2's "no egress" pricing is the differentiator — friends
  downloading the release tarball cost us nothing.
- **Alternatives**: S3 (egress-billed), R2 (no egress), B2 (cheap
  but eggress-billed), Hetzner storage (cheapest at scale but
  worse DX).

### Encoder strategy

The bge-small-en-v1.5 ONNX model (~80MB) gets loaded once into
FastAPI process memory on Fly. Subsequent encodes are <50ms each.
Per-query encoder cost: zero (it's already in memory).

**For scale**: when we cross ~10K queries/minute, peel the encoder
into its own VM and put it behind an internal HTTPS endpoint —
this is a 50-line refactor, no architectural change.

### Telemetry & analytics

- **PostHog Cloud** free tier: 1M events/month. Track page views,
  query rates, calibration funnel, conversion to fit.
- **Plausible.io** (paid, ~$9/month) for the marketing-page-only
  Lighthouse-light pageview analytics.
- **Sentry**: free hobby tier, 5K events/month. Good for catching
  errors in the FastAPI layer.

### Domain & DNS

- **Cloudflare Registrar** for the domain itself — at-cost pricing
  (no markup).
- **Cloudflare DNS** for nameservers — free, fast, and gives us
  Workers + R2 + Pages eligibility if we ever want them.

---

## Build phases

A rough sequencing — each phase ships something usable.

### Phase 1 — Auth + dashboard scaffolding (week 1)

- Set up Vercel project + Clerk + Neon.
- Port the existing FastAPI app to a Next.js shell that hits the
  FastAPI backend.
- Stub user persistence: rather than auto-creating users by name
  (current local behavior), tie `user_id` to Clerk's `user.id`.
- Deploy a "hello world" of the dashboard to a custom subdomain.

### Phase 2 — Import the local model (week 2)

- Bring the release tarball from this repo onto Neon.
- The Fly.io backend reads from Neon. The frontend reads from the
  Fly API.
- Verify end-to-end calibrate + recommend on production.

### Phase 3 — Polish + marketing landing (week 3)

- Tailwind landing page with the side-by-side animation.
- Onboarding flow that walks new users through their first 3
  labels.
- Demo mode that anyone can try without signing up (uses a shared
  guest user, no persistence beyond 24 hours).

### Phase 4 — Public release (week 4)

- ProductHunt launch.
- HN post (Tuesday morning Pacific).
- Twitter/LinkedIn announcement.
- Open-source the Next.js frontend in the same repo (`web/`
  directory).

### Phase 5 — Iterate (ongoing)

- Watch which labels users add and bake interesting ones into the
  global corpus on the next rebuild.
- Add the chemistry layer (GC-MS-based WinePrint) as a
  paid/premium tier — see `PLAN.md`.
- Expand multilingual support (French/Italian/Spanish reviews
  exist in our raw corpora but we haven't trained on them).

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| **Neon free tier doesn't fit embeddings** | Storage math is tight; pay the $19/month Launch plan from day one, or store embeddings in R2 with a tiny pgvector-less lookup table. |
| **Cold-start latency on Fly autosuspend** | Acceptable for v1; if users complain, switch to "always on" ($3 → $7/month). |
| **Clerk lock-in** | Auth.js as fallback — same DB schema (we store user_id, not Clerk-specific fields). Migration is a one-time script. |
| **CedarDB → Neon dialect drift** | We've already worked around CedarDB-isms (`CREATE TABLE IF NOT EXISTS` + `DEFAULT NOW()`); the production code path is Postgres-correct anyway. |
| **Encoder inference cost at scale** | Run the encoder on the Fly VM until ~10K req/min, then move to a dedicated encoder service. The recommender pattern (encode-once-per-query) keeps this cheap. |
| **User-generated content liability** | User labels are user-authored text. Add a terms-of-use prohibiting trademark / personal-info content. Standard pattern, low risk for wine vocabulary. |

---

## Open questions for Archis

1. **Domain name** — `winetone.app`? `winetone.io`? Other?
2. **Comparison-with-friends feature** — should `winetone.app/u/archis` be public-by-default or opt-in? (Opt-in feels safer.)
3. **Demo mode** vs. always-signup-required — letting people try without an account boosts signups, but burns DB rows.
4. **Pricing** — should this stay free forever? Or premium tier
   for (a) the GC-MS chemistry layer when it lands, (b) more
   labels / faster re-fit, (c) wine-merchant integrations?

---

*Plan v0.1 — May 2026. Companion to `PLAN.md` (concept) and
`docs/DATA-AND-ML-PIPELINE-PLAN.md` (the actual build that powers
this hosted version).*
