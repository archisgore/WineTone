# Runbook: Toggle Cloudflare Proxy for tone.wine

*Status: **DNS-only today (grey cloud).** This runbook is the
analysis + step-by-step for the day we want to flip to proxied
(orange cloud). The toggle itself is a single click; the
research below is the part that's actually load-bearing.*

---

## Why we'd want it

Three real wins, in order of weight:

1. **Egress reduction.** HF Spaces bandwidth is metered (and on
   the Pro plan is generous but not infinite). Cloudflare caches
   responses at the edge, so a repeated GET of the landing page,
   `/static/*`, sitemap, etc. is served from Cloudflare for free
   instead of round-tripping to the Space.
2. **DDoS shielding.** Cloudflare has the world's largest L7
   filtering capacity. The Free plan includes "Under Attack
   mode" — a one-click JS challenge wall that holds up against
   anything short of nation-state-grade attacks. We will probably
   never need it, but if we do, having the proxy already in
   place saves the panicked-3am DNS-propagation wait.
3. **Free WAF rules + Bot Fight Mode + rate-limiting at the
   edge.** Free tier gives us a basic WAF with the OWASP
   ruleset; plus bot detection that blocks the obvious crawlers
   without us having to maintain a User-Agent denylist.

Smaller benefits:
- Edge TLS termination — better TLS handshake latency for
  visitors far from `us-east-2`.
- Analytics in CF dashboard for free (per-country, per-status-
  code) — already get this via the Web Analytics beacon, but
  proxied analytics are server-side, so they can't be blocked
  by adblockers.

---

## Gotcha matrix

The reason we're not just flipping it today: every external
integration that *terminates* on `tone.wine` has to keep working
after Cloudflare gets in the middle. Audit:

### Clerk JWKS verification

**Risk: low.** Clerk JWKS is fetched from `https://clerk.tone.wine`
(once we move to a production Clerk instance), not from
`https://tone.wine`. The JWKS endpoint *belongs to Clerk*, not
to our Space — Clerk's edge serves it. If we proxy Clerk's
CNAME through CF too, we'd be double-proxying their domain,
which neither breaks anything nor adds value. **Recommendation:**
keep the `clerk.tone.wine` CNAME as grey-cloud (DNS-only).
Proxy only `tone.wine` itself.

### HF Spaces certificate behavior

**Risk: medium.** When CF proxies, the origin (HF Spaces) sees
CF's IPs as the client, and CF terminates TLS at its edge with
its own cert. The connection CF→origin is TLS-encrypted with
HF's own Let's Encrypt cert. Two ways this can go sideways:
- **CF SSL mode must be set to "Full (strict)".** Default is
  "Flexible," which means CF terminates TLS but the back-leg
  to origin is *plain HTTP*. HF Spaces redirects everything to
  HTTPS, so Flexible mode causes a redirect loop. Full (strict)
  passes traffic over HTTPS end-to-end, validating HF's cert.
- **HF Spaces serves the cert for `tone.wine` because we added
  it as a custom domain.** That cert remains valid when
  proxied; CF just trusts it on the back leg. Verified pattern.

**Test plan:** before flipping prod, point a test subdomain
`test.tone.wine` at the same Space, proxy it, and confirm a
real request lands.

### `/webhooks/clerk` (svix signature verification)

**Risk: medium-high — this is the one that bit similar projects.**

Clerk POSTs webhooks from Clerk's own infra direct to
`tone.wine/webhooks/clerk`. With CF in the middle:
- CF *adds* its own headers (`CF-Connecting-IP`, `CF-Ray`,
  `X-Forwarded-For`).
- CF *might* strip / rewrite headers depending on rules. Most
  default rule sets do not modify `svix-id`, `svix-signature`,
  `svix-timestamp` — which are arbitrary `Svix-*` headers that
  the WAF doesn't know about — but a custom WAF rule, an
  aggressive transform rule, or future CF policy change could.
- The Svix signature is computed over (`svix-id` || `.` ||
  `svix-timestamp` || `.` || body). Any modification to those
  three headers or to the body breaks verification.
- CF can buffer requests > 100 MB or apply request transforms
  that re-encode JSON. Clerk webhooks are tiny (<10 KB) so the
  size limit is fine; the JSON-re-encoding risk is real if we
  ever enable a transform rule.

**Mitigations:**
1. Whitelist the Clerk webhook source IPs at the CF firewall
   (Clerk publishes their ranges) so the path bypasses bot
   challenges. WAF rule: "if request_path matches
   `/webhooks/clerk` and source IP in Clerk's range, skip all
   challenges."
2. Disable any "Rocket Loader" / "Auto Minify" / "Email
   Obfuscation" CF features for the webhook path — they
   rewrite response bodies and could plausibly clash with
   future Clerk client features.
3. Add a smoke test that runs after toggling: trigger a real
   Clerk event (delete a test user), confirm `/webhooks/clerk`
   logs a successful signature verify within 30 seconds.

### `/healthz`

**Risk: low.** UptimeRobot calls this endpoint every 5 minutes.
CF would cache `/healthz` aggressively by default (status 200
JSON is super cacheable), masking a real outage. **Mitigation:**
add a Cache Rule that bypasses cache for `/healthz`:
`URI Path equals /healthz → Bypass cache`.

### `/static/*` (CSS / JS)

**Risk: nil — this is in fact a *win*.** Static files are exactly
what CF caches best. With proxy on, HF Spaces will serve each
static asset once per CF POP per cache-TTL. We do want to set
proper `Cache-Control` headers in the FastAPI app
(`StaticFiles` defaults are weak); covered in the CDN-static-
assets runbook.

### HTMX swap endpoints (`/u/.../calibrate/search`, etc.)

**Risk: low.** These POST. CF doesn't cache POSTs by default.
The only sharp edge is: HTMX sets `HX-Request: true` headers,
which CF doesn't know about, but doesn't strip either. Safe.

### Server-Sent Events / WebSockets

**Risk: N/A — we don't use either.** If we add streaming
endpoints later (e.g. for `/ask` token-by-token narration),
CF supports both but they need to opt out of buffering
(`Cache-Control: no-store` + the `text/event-stream` content
type, which CF detects automatically).

### Cron / external POSTs to the app

**Risk: low.** If we eventually have any (we don't today),
they'd hit `tone.wine` and go through CF same as any other
client. Same Clerk-webhook-style audit applies.

---

## Step-by-step toggle

Once the gotchas above have been planned for:

1. **Pre-flight.** Make a known-working request and save the
   response for comparison:
   ```bash
   curl -s -o /tmp/pre-toggle-landing.html -w "%{http_code}\n" https://tone.wine/
   curl -s -o /dev/null -w "%{http_code}\n" https://tone.wine/healthz
   ```

2. **Configure CF SSL mode FIRST.** Cloudflare dashboard →
   `tone.wine` → SSL/TLS → **Full (strict)**. Wait 30 seconds
   for it to propagate. (Doing this *after* flipping the proxy
   causes a redirect loop and you'll be debugging on prod.)

3. **Add the `/healthz` cache-bypass rule.** Cloudflare → Rules
   → Cache Rules → "Bypass cache for /healthz" → URI path
   equals `/healthz` → Action: Bypass cache.

4. **Add the `/webhooks/clerk` security exemption.** Cloudflare
   → Security → WAF → Custom rules:
   - Name: `clerk-webhooks-bypass`
   - Condition: `URI Path equals /webhooks/clerk`
   - Action: Skip → all features

5. **Flip the orange cloud.** Cloudflare dashboard → DNS →
   `tone.wine` A/AAAA record → click the grey cloud → it
   becomes orange. (If we used CNAME flattening to HF Spaces,
   it's the CNAME row instead.)

6. **Wait 60 seconds**, then verify:
   ```bash
   # Should still return 200 + same shape as pre-toggle:
   curl -s -o /tmp/post-toggle-landing.html -w "%{http_code}\n" https://tone.wine/
   diff /tmp/pre-toggle-landing.html /tmp/post-toggle-landing.html  # expect no significant diff
   curl -sI https://tone.wine/ | grep -i 'server\|cf-ray\|cf-cache-status'
   # Should now show "Server: cloudflare" + "CF-RAY: ..." + "CF-Cache-Status: ..."
   ```

7. **Verify /healthz is uncached:**
   ```bash
   curl -sI https://tone.wine/healthz | grep -i 'cf-cache-status'
   # Expect: cf-cache-status: BYPASS
   ```

8. **Trigger a real Clerk webhook test.** From the Clerk
   dashboard's webhook settings page, click "Send test event."
   Watch the live Space logs (`/api/spaces/.../logs` or the
   Space UI) for the signature-verification log line.

9. **Watch for 30 minutes**, then declare success. If anything
   regresses, click the orange cloud back to grey — DNS
   reverts within ~60 seconds for cached resolvers, much
   faster on direct hits.

---

## Rollback

```
Cloudflare DNS → tone.wine → click orange cloud → grey cloud.
```

That's it. DNS-only resumes within minutes; CF stops being in
the path. The CF rules (cache-bypass, WAF exception) stay
configured but become inert; they don't need to be deleted.

---

## What proxying costs us

- **One extra hop.** Adds ~5-20ms RTT depending on visitor
  geography. CF's edge is usually closer to the user than HF's
  origin, so it often *reduces* perceived latency.
- **CF becomes a single point of failure in our stack.** If
  CF has a global outage (rare but happens), `tone.wine` is
  unreachable until they recover. The grey-cloud configuration
  doesn't have this risk. For a research prototype, taking it
  is fine.
- **Some user analytics get attributed to CF IPs unless we
  read CF's `CF-Connecting-IP` header.** Our `_client_ip()`
  helper already prefers `X-Forwarded-For`'s first entry; need
  to add `CF-Connecting-IP` as a higher-priority fallback if
  we proxy. Code change is ~3 lines.

---

## Recommendation

**Worth doing — but not urgent.** At current traffic (single-
digit RPS) the egress savings are negligible and the DDoS
risk is theoretical. The reasons to wait:

1. The Clerk-prod flip happens first (Tier 1 #2). After CF is
   in the middle, debugging a Clerk webhook failure has two
   places to look instead of one. Better to know Clerk works
   directly first.
2. The CDN-static-assets runbook (`cdn-static-assets.md`)
   makes the case that for our scale, leaving `/static/*` on
   HF Spaces is fine. So one of CF proxying's main wins isn't
   load-bearing for us today.

Revisit when:
- We see meaningful bandwidth growth (>100 GB/mo egress from HF)
- We get a small-scale abuse incident the rate-limiter doesn't
  cleanly handle
- We add features that benefit from edge caching (a public
  JSON API, image-thumbnail endpoints, etc.)
