# Runbook: CDN-ing /static/* (and why we probably won't)

*Status: **not doing it today, and that's the right call.** This
runbook exists so the next time the question "should we move
static assets to a CDN?" comes up, the answer has reasoning
behind it.*

---

## The setup today

`/static/*` is served by `StaticFiles(directory="www/static")`
inside the FastAPI app inside the HF Space. The actual files
total ~30 KB:

```
www/static/
├── style.css      ~16 KB
├── favicon.svg    ~1 KB
└── (no JS, no images, no fonts — by design)
```

Every request hits the Space's CPU, briefly. Throughput at our
current scale is "trivial" — we're nowhere near the bottleneck.

---

## When CDN-ing this would actually pay off

The three reasons you'd put static assets on a CDN:

1. **Bandwidth costs at origin become real.** Egress from HF is
   metered. ~30 KB × 100K pageviews/month = ~3 GB. We are not
   on the boundary of any HF bandwidth limit.
2. **Latency to far-from-origin users matters.** A user in
   Tokyo fetching CSS from a us-east-2 HF Space pays ~150ms of
   RTT for a 16 KB asset. A CDN edge in Tokyo would serve it
   in <20ms. For our research-prototype audience this isn't
   load-bearing.
3. **The Space restarts and we want assets to stay served
   during the rebuild window.** Today static + dynamic restart
   together — if the Space is down, so is the CSS. With CDN'd
   static, the page would partially load even during a rebuild
   (visitors see structure + content, just no styling).

**None of these apply to us at our current scale.** The case
for CDN'ing static assets is a "1000× current traffic" case,
not a "ship the prototype" case.

---

## If we do it anyway, the options

### Option A: leave it on HF Spaces (recommended today)

- **Pros:** zero ops. Already works. Already gets the same
  caching benefit when the user revisits because we set
  proper `Cache-Control` headers (see below).
- **Cons:** none at current scale.

### Option B: Cloudflare proxy + cache rule

(See `cloudflare-proxy-toggle.md`.) Toggle the orange cloud
and CF will cache `/static/*` at the edge for free. This is
**the right next step if we want CDN'd static** because it
costs nothing extra — we'd be flipping the proxy for the
WAF / DDoS shield anyway.

- **Pros:** free, no code change, no separate bucket to
  maintain, automatic invalidation when we redeploy because
  `Cache-Control: max-age` controls TTL.
- **Cons:** locks `/static/*` to CF's PoP map. Doesn't
  help if CF is down (but if CF is down we'd be entirely
  down anyway, because the rest of the site is also proxied).
- **Workflow:** push a new CSS commit → Space rebuilds → next
  CF cache MISS for that asset re-fetches → new version
  propagates.

### Option C: Cloudflare R2 + custom subdomain

(`static.tone.wine` → R2 bucket.) For when we genuinely want
to decouple static-asset hosting from the app server.

- **Pros:** independent rebuild cycle (push CSS to R2 without
  redeploying the Space). R2 has zero egress fees. Survives
  Space outages.
- **Cons:** real ops complexity. Two deploys for every CSS
  change. Cache invalidation by version-hashed URLs
  (`style.css?v=abc123`) instead of just letting cache-control
  do its thing. Worth it at scale; ridiculous at our scale.
- **Workflow:** every CSS change → upload to R2 via
  `wrangler r2 object put` → bump the version string in the
  HTML template → commit.

### Option D: Cloudflare Pages

- **Pros:** built for static-site hosting; integrated with
  GitHub for auto-deploys on push.
- **Cons:** would mean two sites (Pages for static, Spaces
  for app) coordinated by the HTML — same problem as R2, just
  prettier dashboard.

### Option E: jsDelivr / unpkg from a public GitHub repo

- **Pros:** free, zero infra, public CDN.
- **Cons:** static-asset URLs become coupled to GitHub.com
  availability and jsDelivr's caching whims. Public CDNs
  cache aggressively (sometimes >24h) so updates aren't fast.
- **Not recommended** unless we want to publish our CSS as a
  reusable package, which we don't.

---

## What we should actually do today

**The win that's actually available without changing infra:**
make sure FastAPI's `StaticFiles` is serving with proper
`Cache-Control` headers so the *browser* caches the assets
after the first hit.

Out of the box, Starlette's `StaticFiles` sets weak headers
(`Last-Modified` + `ETag`, no `Cache-Control: max-age`). Add
a tiny middleware:

```python
# src/winetone/web/app.py — sketch only, not implementing today
@app.middleware("http")
async def add_static_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        # 1 hour for HTML-templated assets, immutable for hashed.
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response
```

That alone covers ~80% of the latency benefit a CDN would
provide for our repeat-visitor pattern. Worth doing as a
~5-line change before we move to anything fancier.

**Defer until then.** Revisit options B-E if and when we ever
hit a scale where one of the three "when CDN-ing pays off"
conditions becomes true.

---

## What we'd need to change in the app

If/when we adopt Option C (R2 + subdomain):

1. Add a Jinja global `STATIC_BASE = os.environ.get(
   "STATIC_BASE", "/static")`.
2. Replace every `/static/...` reference in templates with
   `{{ STATIC_BASE }}/...`.
3. Either upload manually via `wrangler r2 object put` or
   wire a GitHub Actions step on the `main` branch.
4. Set CSP `style-src 'self' static.tone.wine` to allow the
   subdomain.

Steps 1-2 are good prep regardless — they make the static
base swappable without code changes. If we ever do them, they
unlock Option C without further refactor.

---

## Final recommendation

| Today's traffic | Action |
|---|---|
| What we have now | **Leave static on HF Spaces.** Add 5-line cache-control middleware as the next refinement. |
| 10× current | Same. Add cache-control middleware if not already done. |
| 100× current | Flip Cloudflare proxy (orange cloud) — covered by the proxy runbook. Free. |
| 1000× current | Move to R2 with the prep in "what we'd need to change" above. |

At our scale, the time you'd spend on Option C is better spent
on literally anything else.
