"""FastAPI app for the local WineTone web demo.

Endpoints are HTMX-friendly: the page shell never reloads. Forms
POST to fragment endpoints that return small HTML blocks injected
into the live page. Zero JavaScript framework, ~one HTML file
worth of total markup.

Routes:

  GET  /                          landing — username picker
  POST /pick-user                 → redirect to /u/{name}
  GET  /u/{user}                  dashboard
  POST /u/{user}/calibrate/search HTMX: returns candidate-wines table fragment
  POST /u/{user}/calibrate/add    HTMX: returns updated labels-list fragment
  POST /u/{user}/calibrate/fit    HTMX: returns updated status pill
  POST /u/{user}/recommend        HTMX: returns recommendations table fragment
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from winetone import auth_clerk, calibrate, db, embed
from winetone import recommend as reco

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
# Web assets (HTML templates + CSS/JS/images) live at <repo-root>/www/.
# This separation keeps the deployable frontend cleanly carved out from
# the Python package — you can `rsync www/` to a CDN if we ever go SPA.
WWW = HERE.parent.parent.parent / "www"


def _resolve_user(request: Request) -> dict | None:
    """Turn the Clerk JWT into a local user row (or None when anonymous).

    Two-step: first decode + verify the JWT (cheap, cached JWKS), then
    look up — or create — the matching `users` row. We do creation here
    rather than at sign-in time because Clerk doesn't have a webhook
    into our app and a user's first request *is* their sign-in.
    """
    claims = auth_clerk.current_user(request)
    if claims is None:
        return None
    clerk_uid = claims["clerk_user_id"]
    # The session JWT carries username/email only if the Clerk session
    # template includes them. Fall back to the Backend API if missing.
    display_name = claims.get("username") or ""
    email = claims.get("email") or ""
    if not display_name:
        try:
            profile = auth_clerk.fetch_user_profile(clerk_uid)
            display_name = profile["username"]
            email = profile["email"]
        except Exception as e:  # noqa: BLE001
            log.warning("fetch_user_profile(%s) failed: %s", clerk_uid, e)
            display_name = reco.synthesize_display_name(clerk_uid, email)
    request_id = getattr(request.state, "request_id", None)
    user_id = reco.get_or_create_user_for_clerk(
        clerk_user_id=clerk_uid,
        display_name=display_name,
        email=email,
        request_id=request_id,
    )
    # Pull age-confirmation flag — used by templates to gate first-use
    # actions (calibrate, wine submission) behind the drinking-age modal.
    from sqlalchemy import text as _text
    try:
        with db.engine().connect() as conn:
            row = conn.execute(
                _text("SELECT confirmed_age_at FROM users WHERE user_id = :u"),
                {"u": user_id},
            ).fetchone()
        age_confirmed = bool(row and row[0] is not None)
    except Exception:  # noqa: BLE001
        # Column may not exist on very-old DBs; assume confirmed so we
        # don't block the user.
        age_confirmed = True
    return {
        "user_id": user_id,
        "clerk_user_id": clerk_uid,
        "display_name": display_name,
        "email": email,
        "age_confirmed": age_confirmed,
    }


_WINE_COUNT_CACHE: dict[str, float | int] = {"value": 0, "expires": 0.0}


def _wine_count() -> int:
    """Return the live wine count from the catalog, cached for 5 minutes.

    Templates that used to hard-code "164,069 wines" now render this
    value, so the corpus size on the live site grows with every
    user-submitted wine instead of telegraphing a stale number.

    Cache TTL is 5 minutes — well below the prompt-cache window, and
    the count grows by at most a handful of submissions in five
    minutes at any plausible rate.

    Returns 0 on DB error so callers can render "0 wines" rather than
    crash the page; in practice we never expect that branch.
    """
    import time
    now = time.time()
    if now > float(_WINE_COUNT_CACHE["expires"]):
        from sqlalchemy import text as _text
        try:
            with db.engine().connect() as conn:
                n = conn.execute(_text("SELECT COUNT(*) FROM wines")).scalar()
            _WINE_COUNT_CACHE["value"] = int(n or 0)
        except Exception as e:  # noqa: BLE001
            log.warning("_wine_count: DB query failed: %s", e)
        _WINE_COUNT_CACHE["expires"] = now + 300
    return int(_WINE_COUNT_CACHE["value"])


def _auth_context(request: Request) -> dict:
    """Inject signed-in user info + helpers into every render."""
    user = _resolve_user(request)
    current_path = request.url.path if request else "/"

    def active_nav(href: str) -> str:
        # Exact match for the root; prefix match otherwise (so /catalog
        # and /catalog?q=… both highlight Catalog).
        if href == "/":
            return "is-active" if current_path == "/" else ""
        return "is-active" if current_path.startswith(href) else ""

    return {
        "current_user": user,
        "auth_enabled": auth_clerk.is_enabled(),
        "clerk_publishable_key": os.environ.get("CLERK_PUBLISHABLE_KEY", ""),
        "clerk_frontend_api": auth_clerk.frontend_api_domain(),
        "clerk_sign_in_url": auth_clerk.sign_in_url(),
        "active_nav": active_nav,
        # Live corpus size — refreshed every 5 min via _wine_count cache.
        # Templates render via {{ "{:,}".format(wine_count) }}.
        "wine_count": _wine_count(),
        # Per-request CSP nonce; templates apply this to every inline
        # <script> via nonce="{{ csp_nonce }}". Set by
        # SecurityHeadersMiddleware before this context processor runs;
        # default to empty string if middleware didn't fire (test env).
        "csp_nonce": getattr(request.state, "csp_nonce", "") if request else "",
    }


TEMPLATES = Jinja2Templates(
    directory=str(WWW / "templates"),
    context_processors=[_auth_context],
)

# Globals injected into every render — let templates see env-driven
# config (analytics token, etc.) without each route having to pass them.
TEMPLATES.env.globals["cf_analytics_token"] = os.environ.get("CF_ANALYTICS_TOKEN", "")
TEMPLATES.env.globals["plausible_domain"] = os.environ.get("PLAUSIBLE_DOMAIN", "")


def _init_sentry() -> None:
    """Activate Sentry if SENTRY_DSN is set in the environment.

    The SDK is an optional dep — if it isn't installed we just skip.
    """
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        sentry_sdk.init(
            dsn=dsn,
            integrations=[FastApiIntegration()],
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
            send_default_pii=False,
            environment=os.environ.get("WINETONE_ENV", "production"),
        )
        log.info("Sentry initialized")
    except ImportError:
        log.info("sentry-sdk not installed; skipping")
    except Exception as e:  # noqa: BLE001
        log.warning("sentry init failed: %s", e)


def _client_ip(request: Request) -> str:
    """Best-effort client IP behind HF Spaces' reverse proxy.

    HF puts the original IP in X-Forwarded-For. We trust the first
    entry (the leftmost) — typical proxy convention. Falls back to
    request.client.host when the header isn't set (local dev).
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ─── Per-wine SEO copy helpers ─────────────────────────────────
# Produce title / meta-description / og-description strings from a
# wine row. Used by both /wines/{id} renders and the sitemap.

def _wine_display_title(wine: dict) -> str:
    """'<Producer> <Wine> <Vintage>' — best human-readable title.

    Falls through gracefully when any component is missing: a row
    with only producer + variety still produces something usable.
    """
    parts: list[str] = []
    producer = (wine.get("producer_display") or "").strip()
    wine_name = (wine.get("wine_display") or "").strip()
    vintage = wine.get("vintage")
    if producer:
        parts.append(producer)
    if wine_name and wine_name != producer:
        parts.append(wine_name)
    if vintage:
        parts.append(f"({int(vintage)})")
    return " ".join(parts) or "Unknown wine"


def _wine_meta_description(wine: dict) -> str:
    """Single sentence summarizing the wine for <meta description>.

    Format: '[Producer] [Wine] [Vintage]. [Variety] from [Region],
    [Country]. See tasting notes, descriptions, and find similar
    wines on WineTone.'
    """
    title = _wine_display_title(wine)
    bits: list[str] = []
    variety = (wine.get("variety") or "").strip()
    region  = (wine.get("region") or "").strip()
    country = (wine.get("country") or "").strip()
    if variety:
        place = ""
        if region and country:
            place = f" from {region}, {country}"
        elif country:
            place = f" from {country}"
        bits.append(f"{variety}{place}.")
    elif country:
        bits.append(f"From {country}.")
    bits.append("See tasting notes, descriptions, and find similar wines on WineTone.")
    return f"{title}. " + " ".join(bits)


_SOURCE_DISPLAY_NAMES = {
    "wine_enthusiast":       "Wine Enthusiast",
    "wine_enthusiast_130k":  "Wine Enthusiast",
    "wine_enthusiast_150k":  "Wine Enthusiast",
    "uci_wine_quality":      "UCI Wine Quality",
    "uci_wine":              "UCI Wine",
    "wikidata":              "Wikidata",
    "user_submission":       "User submission",
}


def _format_sources(raw: str | None) -> str:
    """Map raw dataset identifiers to clean publication names for
    user-facing display.

    `wines.sources_seen` is a comma-separated string of internal source
    IDs (e.g. `wine_enthusiast_150k,wikidata`). Users shouldn't see the
    internal IDs — this maps each token to its publication name,
    deduplicates (the two Wine Enthusiast corpora both become "Wine
    Enthusiast"), and joins with ", ".
    """
    if not raw:
        return ""
    seen: list[str] = []
    for token in (s.strip() for s in raw.split(",")):
        if not token:
            continue
        name = _SOURCE_DISPLAY_NAMES.get(
            token,
            # Unknown source — title-case the raw identifier as a
            # graceful fallback (`foo_bar_baz` → `Foo Bar Baz`).
            " ".join(p.capitalize() for p in token.split("_")),
        )
        if name not in seen:
            seen.append(name)
    return ", ".join(seen)


def _prefer_markdown(request: Request) -> bool:
    """Inspect the Accept header to decide whether to serve markdown.

    Returns True only when the client clearly prefers text/markdown
    over text/html (or any wildcard text/*). The default is False —
    browsers send `Accept: text/html,application/xhtml+xml,...` which
    never matches, so this never affects human users.

    Used by routes that support markdown content negotiation
    (/, /wines/{id}). Cloudflare's agent-readiness check rewards
    sites that vary on Accept, and an LLM client fetching with
    `Accept: text/markdown` gets a cleaner, citable view of the page.
    """
    accept = request.headers.get("accept", "")
    if not accept or "text/markdown" not in accept:
        return False
    # Cheap q-value parse: rank text/markdown vs text/html (default q=1).
    def _q(token: str, accept_str: str) -> float:
        for part in accept_str.split(","):
            part = part.strip()
            if part.startswith(token):
                # Look for a q= modifier after the type
                if ";" in part:
                    for attr in part.split(";")[1:]:
                        attr = attr.strip()
                        if attr.startswith("q="):
                            try:
                                return float(attr[2:])
                            except ValueError:
                                return 1.0
                return 1.0
        return 0.0
    return _q("text/markdown", accept) > _q("text/html", accept)


def _render_wine_markdown(wine: dict, labels: list,
                          sources_pretty: str,
                          viewer: dict | None) -> str:
    """Render a wine-detail page as markdown for `Accept: text/markdown`.

    Mirrors what wine_detail.html shows visually (title, region/variety
    line, public-reviewer aggregate, source attribution, user labels)
    minus the inline label editor and the JSON-LD. Author bylines for
    user labels are gated the same way the HTML is: anonymous viewers
    see no @username, signed-in viewers see them.
    """
    lines: list[str] = []
    title = _wine_display_title(wine)
    lines.append(f"# {title}")
    lines.append("")
    meta_bits: list[str] = []
    if wine.get("variety"):
        meta_bits.append(str(wine["variety"]))
    if wine.get("region") and wine.get("country"):
        meta_bits.append(f"{wine['region']}, {wine['country']}")
    elif wine.get("country"):
        meta_bits.append(str(wine["country"]))
    if meta_bits:
        lines.append(" · ".join(meta_bits))
        lines.append("")
    if wine.get("n_reviews") or wine.get("median_points") or wine.get("median_price"):
        lines.append("## From public reviewers")
        lines.append("")
        rev_bits = []
        if wine.get("n_reviews"):
            rev_bits.append(f"{int(wine['n_reviews']):,} reviews")
        if wine.get("median_points"):
            rev_bits.append(f"median {int(wine['median_points'])} pts")
        if wine.get("median_price"):
            rev_bits.append(f"median ${int(wine['median_price'])}")
        lines.append(" · ".join(rev_bits))
        lines.append("")
        if wine.get("review_text_all"):
            snippet = (wine["review_text_all"] or "")[:520]
            if len(wine.get("review_text_all", "")) > 520:
                snippet += "…"
            lines.append("> " + snippet)
            lines.append("")
        if sources_pretty:
            sep = "Sources" if "," in sources_pretty else "Source"
            lines.append(f"{sep}: {sources_pretty}")
            lines.append("")
    lines.append("## What WineTone users say")
    lines.append("")
    if labels:
        for lbl in labels:
            mark = "👎" if lbl.get("sentiment") == "negative" else "👍"
            author = ""
            if viewer is not None and lbl.get("author"):
                author = f" — @{lbl['author']}"
            date = ""
            if lbl.get("created_at"):
                import contextlib
                with contextlib.suppress(AttributeError):
                    date = " · " + lbl["created_at"].strftime("%Y-%m-%d")
            lines.append(f"- {mark} \"{lbl['description']}\"{author}{date}")
    else:
        lines.append("No WineTone users have labelled this wine yet.")
    lines.append("")
    lines.append("---")
    lines.append(f"Canonical: https://tone.wine/wines/{wine['wine_id']}")
    return "\n".join(lines) + "\n"


def _wine_og_description(wine: dict, labels: list) -> str:
    """og:description for social-card previews.

    Prefer a real user label when one exists (more vivid social
    preview) — fall back to the structured one-liner otherwise.
    Author intentionally not included (privacy).
    """
    if labels:
        first = labels[0]
        text = (first["description"] if isinstance(first, dict) else first.description) or ""
        text = text.strip()
        if text:
            # Keep social previews tight — most platforms truncate
            # around 200 chars anyway.
            return text[:190] + ("…" if len(text) > 190 else "")
    return _wine_meta_description(wine)


def build_app() -> FastAPI:
    """Construct the FastAPI app. Factored so tests can build without
    starting uvicorn."""
    # Structured JSON logging — keeps request_id threaded through and
    # makes the logs greppable. Configure before _init_sentry so Sentry
    # picks up our handler config.
    from winetone import logging_config
    logging_config.configure(level=os.environ.get("WINETONE_LOG_LEVEL", "INFO"))

    _init_sentry()
    app = FastAPI(title="WineTone demo")
    app.mount("/static", StaticFiles(directory=str(WWW / "static")), name="static")

    @app.on_event("startup")
    async def _warmup_db_pool() -> None:
        """Prime the Neon Postgres connection pool at boot.

        Without this, the first user request after a factory_reboot
        hits a cold pool and pays a 5–15 second penalty (often
        manifesting as a 500 if Cloudflare's edge timeout fires first).
        Running a trivial SELECT here gives the pool a warm connection
        before any user traffic lands.

        Wrapped in try/except so a warmup failure (e.g. DB momentarily
        unreachable) doesn't prevent the app from starting — the app
        still binds the port and will succeed on a later request
        when the pool can be primed lazily.
        """
        try:
            from sqlalchemy import text as _text
            with db.engine().connect() as conn:
                conn.execute(_text("SELECT 1")).scalar()
                # Touch the table the home page reads on every render —
                # this primes the pgvector + table metadata cache too.
                conn.execute(_text("SELECT COUNT(*) FROM wines")).scalar()
            log.info("Neon connection pool warmed at startup")
        except Exception as e:  # noqa: BLE001
            log.warning("startup DB warmup failed (lazy retry on first request): %s", e)

    # --- Encoder pre-warm at startup ------------------------------------
    # sentence-transformers takes ~5s to load on a cold start (model
    # weights + tokenizer + ONNX backend init). Doing that lazily means
    # the FIRST real user request after a Space restart pays the cost.
    # Pre-warm during startup so they don't. The encoder is shared
    # across requests via the module-level _QUERY_ENCODER cache in
    # winetone.embed.
    @app.on_event("startup")
    async def _prewarm_encoder() -> None:  # noqa: D401
        import asyncio
        import time
        async def _warm() -> None:
            try:
                t0 = time.monotonic()
                # Hot the encoder + run one inference so any
                # lazy-initialized internal buffers are also pre-built.
                await asyncio.to_thread(embed.encode_query, "warmup")
                log.info(
                    "encoder pre-warmed in %.1fs",
                    time.monotonic() - t0,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("encoder pre-warm failed (will lazy-load): %s", e)
        # Don't block startup on this — fire-and-forget. The Space
        # health check passes fast; the encoder finishes loading in
        # the background, and if a request arrives mid-load the lazy
        # path in encode_query just blocks for the remaining few sec.
        asyncio.create_task(_warm())

    # Per-request UUID + access log line + X-Request-Id echo. Goes
    # first in the middleware chain so it wraps everything else.
    app.add_middleware(logging_config.RequestIdMiddleware)

    # --- Custom HTML error pages ----------------------------------------
    # FastAPI's default returns a JSON body for HTTPException; on the
    # public site we'd rather see styled "Page not found" / "Something
    # went wrong" pages that match the site chrome.
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    def _render_error(request: Request, code: int, title: str, message: str) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "_error.html",
            {"code": str(code), "title": title, "message": message},
            status_code=code,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        # Webhook and API endpoints want JSON; HTML pages want HTML.
        # Sniff by Accept header + path prefix.
        accept = (request.headers.get("accept") or "").lower()
        is_api = (
            request.url.path.startswith(("/webhooks/", "/healthz", "/report"))
            or "application/json" in accept
        )
        if is_api:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        title = {
            400: "Bad request",
            401: "Sign in required",
            403: "Not allowed",
            404: "Page not found",
            429: "Too many requests",
            503: "Service unavailable",
        }.get(exc.status_code, "Error")
        return _render_error(request, exc.status_code, title, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return _render_error(
            request, 422, "Invalid input",
            "Some of the fields you sent didn't pass validation. Try again with valid values.",
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        log.exception("unhandled exception on %s %s", request.method, request.url.path)
        return _render_error(
            request, 500, "Something went wrong",
            "An unexpected error occurred. The site operator has been notified.",
        )

    # --- Security headers -----------------------------------------------
    # Applied to every response. CSP is the loosest meaningful one we
    # can ship without breaking Clerk's JS bundle (which loads from
    # <clerk-frontend-api>.clerk.accounts.dev), htmx (unpkg), and the
    # Cloudflare analytics beacon. Tighter than nothing.
    from starlette.middleware.base import BaseHTTPMiddleware

    class SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Generate a per-request CSP nonce BEFORE the downstream
            # handler runs so templates can read it via request.state.
            # 16 random bytes → base64 ~22 chars, more than enough
            # entropy. Closes security-review-2026-05-24 LOW-8.
            import secrets
            request.state.csp_nonce = secrets.token_urlsafe(16)
            response = await call_next(request)
            # HSTS — 1 year, includeSubDomains, AND preload. The preload
            # directive signals consent for tone.wine to be added to the
            # hardcoded HSTS preload list browsers ship with. Submission
            # at https://hstspreload.org/ is a separate manual step.
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains; preload",
            )
            # Disallow framing — defeats clickjacking trivially.
            response.headers.setdefault("X-Frame-Options", "DENY")
            # Don't leak the full URL to third parties on cross-origin
            # navigation.
            response.headers.setdefault(
                "Referrer-Policy",
                "strict-origin-when-cross-origin",
            )
            # No MIME-type sniffing.
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            # No browser features by default. Includes the post-FLoC
            # ad-targeting features (browsing-topics, interest-cohort)
            # and hardware features we never use (usb, serial,
            # bluetooth, magnetometer, gyroscope, accelerometer).
            response.headers.setdefault(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), payment=(), "
                "usb=(), serial=(), bluetooth=(), magnetometer=(), "
                "gyroscope=(), accelerometer=(), midi=(), "
                "browsing-topics=(), interest-cohort=()",
            )
            # Cross-Origin-Resource-Policy: prevents other origins from
            # embedding our resources (images, fonts) as a side channel.
            # same-site is the right default for a single-origin app.
            response.headers.setdefault(
                "Cross-Origin-Resource-Policy", "same-site",
            )
            # Strip the Server header (uvicorn) so we don't broadcast
            # the framework version. Minor info-disclosure hardening.
            # NOTE: HF Spaces' reverse proxy re-adds this header after
            # our middleware runs, so the strip only takes effect when
            # running outside HF. Accepted as-is on HF.
            if "Server" in response.headers:
                del response.headers["Server"]
            # Link header — built later at the canonical-setting block
            # below, since that overwrites this header. Agent-readiness
            # links live there now.
            # CSP — explicit Clerk frontend domain in script-src and
            # connect-src so the auth flow works. challenges.cloudflare.com
            # is Clerk's CAPTCHA provider (Turnstile); without it sign-up
            # fails with "The CAPTCHA failed to load."
            #
            # Script-src tightening (2026-05-24):
            # - 'unsafe-inline' removed; replaced with per-request
            #   'nonce-<value>' that templates apply to every inline
            #   <script> tag.
            # - 'unsafe-eval' KEPT for now — clerk-js v6 still uses
            #   Function() in some plugin paths. Dropping it would
            #   break the sign-in modal. Revisit after Clerk's next
            #   minor release.
            # - style-src 'unsafe-inline' stays — we use inline `style="..."`
            #   attributes extensively in templates. Tightening that
            #   would require nonce-per-style + a template sweep.
            nonce = request.state.csp_nonce
            clerk_domain = auth_clerk.frontend_api_domain()
            clerk_origins = (
                f"https://{clerk_domain} https://*.clerk.accounts.dev"
                if clerk_domain else "https://*.clerk.accounts.dev"
            )
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}' 'unsafe-eval' "
                f"  https://unpkg.com {clerk_origins} "
                f"  https://challenges.cloudflare.com "
                f"  https://static.cloudflareinsights.com; "
                f"style-src 'self' 'unsafe-inline'; "
                f"img-src 'self' data: https: blob:; "
                f"font-src 'self'; "
                f"connect-src 'self' {clerk_origins} "
                f"  https://challenges.cloudflare.com "
                f"  https://huggingface.co https://*.huggingface.co "
                f"  https://cloudflareinsights.com; "
                f"frame-src {clerk_origins} https://challenges.cloudflare.com; "
                f"worker-src 'self' blob:; "
                f"frame-ancestors 'none';"
            )
            # Cache-Control for static assets — HF Spaces doesn't set this,
            # so without it browsers ask "is this still fresh?" on every
            # repeat visit. 1h public cache is conservative; bumping it would
            # require hashed filenames for cache-bust on deploy.
            if request.url.path.startswith("/static/"):
                response.headers.setdefault(
                    "Cache-Control", "public, max-age=3600"
                )
            # Canonical URL + agent-readiness links as a single Link
            # header (RFC 8288). The canonical overrides what HF Spaces'
            # reverse proxy would otherwise inject pointing at
            # huggingface.co. The agent-readiness rels (describedby /
            # service-desc) point at the citable site summary and the
            # A2A agent card — Cloudflare's agent-readiness check
            # rewards these.
            if request.method == "GET" and not request.url.path.startswith(
                ("/static/", "/webhooks/", "/healthz")
            ):
                response.headers["Link"] = (
                    f'<https://tone.wine{request.url.path}>; rel="canonical", '
                    '</llms.txt>; rel="describedby"; type="text/plain", '
                    '</.well-known/agent-card.json>; rel="service-desc"; '
                    'type="application/json"'
                )
            return response

    app.add_middleware(SecurityHeadersMiddleware)

    # --- Rate limiting (slowapi) ----------------------------------------
    # Keep reads liberal (`/`, `/ask`, `/vocab`, `/u/{user}` viewer pages
    # are unrate-limited) but throttle writes per-IP so a single script
    # can't flood the DB with submissions or label spam.
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware
    limiter = Limiter(key_func=_client_ip, default_limits=[])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    @app.get("/healthz")
    def healthz() -> dict:
        """Liveness + readiness probe for external monitoring.

        Returns 200 with per-dependency status when everything's
        reachable. Returns 503 with the same JSON body when any
        critical dependency is down. UptimeRobot can be configured to
        treat anything other than 200 as a fault.
        """
        import time

        from fastapi.responses import JSONResponse
        checks: dict[str, str] = {}
        overall_ok = True

        t0 = time.monotonic()
        try:
            checks["db"] = "ok" if db.ping() else "down"
            if not db.ping():
                overall_ok = False
        except Exception as e:  # noqa: BLE001
            checks["db"] = f"error: {e!s:.80}"
            overall_ok = False
        checks["db_latency_ms"] = f"{(time.monotonic() - t0) * 1000:.0f}"

        # Clerk JWKS reachability (only when auth is configured).
        if auth_clerk.is_enabled():
            try:
                domain = auth_clerk.frontend_api_domain()
                checks["clerk_frontend"] = domain or "unconfigured"
            except Exception as e:  # noqa: BLE001
                checks["clerk_frontend"] = f"error: {e!s:.80}"

        checks["encoder_loaded"] = (
            "yes" if getattr(embed, "_QUERY_ENCODER", None) is not None else "lazy"
        )

        # We deliberately do NOT call out to HF Inference here — it's
        # too slow for a health probe (200-2000ms) and a flaky upstream
        # would constantly trip alerts. The LLM router falls back
        # gracefully on its own when Inference is down.

        status = 200 if overall_ok else 503
        return JSONResponse(content={"status": "ok" if overall_ok else "degraded", **checks}, status_code=status)

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots() -> str:
        # Site is intentionally crawler-friendly: search engines AND the
        # major LLM crawlers (GPTBot, ClaudeBot, PerplexityBot, Google-
        # Extended) are all explicitly allowed. WineTone benefits from
        # being citable by LLM chat surfaces; the marketing model is
        # built around that. /admin/ and the HTMX fragment endpoints
        # are blocked as low-signal noise.
        #
        # Content-Signal declares AI-preference signals per the IETF
        # ai-preferences draft:
        #   ai-train=no  — don't use our content to train models.
        #                  User-contributed wine descriptions are their
        #                  personal vocabulary; that's not training data.
        #   search=yes   — index normally for search-engine retrieval.
        #   ai-input=yes — OK to retrieve in real time as LLM context
        #                  (e.g., when a user asks Claude about wine,
        #                  Claude can cite tone.wine). This is the
        #                  marketing channel we want.
        return (
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /admin/\n"
            "Disallow: /_editor\n"
            "Content-Signal: ai-train=no, search=yes, ai-input=yes\n"
            "\n"
            "User-agent: GPTBot\n"
            "Allow: /\n"
            "\n"
            "User-agent: ClaudeBot\n"
            "Allow: /\n"
            "\n"
            "User-agent: PerplexityBot\n"
            "Allow: /\n"
            "\n"
            "User-agent: Google-Extended\n"
            "Allow: /\n"
            "\n"
            "Sitemap: https://tone.wine/sitemap.xml\n"
        )

    @app.get("/.well-known/agent-card.json")
    def agent_card() -> dict:
        """A2A Agent Card — describes WineTone to agentic crawlers
        and LLM clients that follow the well-known discovery pattern.

        Skill list intentionally describes user-facing capabilities
        rather than a per-endpoint MCP-shaped tool spec. The
        endpoints listed are public read-only routes; the write
        routes (label / calibrate / fit) require human auth and
        aren't useful to advertise to autonomous agents.
        """
        return {
            "schemaVersion": "0.2.0",
            "version": "1.0.0",
            "name": "WineTone",
            "description": (
                "A wine recommender that learns how each user "
                "personally talks about wine, then re-ranks a "
                "catalog of 164k+ wines around that user's "
                "vocabulary instead of the average palate."
            ),
            "url": "https://tone.wine",
            "documentation": "https://tone.wine/llms.txt",
            "contact": "mailto:privacy@tone.wine",
            "publisher": {
                "name": "WineTone",
                "url": "https://tone.wine",
            },
            "supportedInterfaces": [
                {
                    "url": "https://tone.wine",
                    "transport": "HTTP+JSON",
                },
            ],
            "skills": [
                {
                    "id": "ask",
                    "name": "Ask for a wine in natural language",
                    "description": (
                        "Free-form natural language query, routed "
                        "through an LLM into a wine-recommendation, "
                        "vocabulary-search, or cheaper-alternative "
                        "search depending on intent."
                    ),
                    "endpoint": "https://tone.wine/ask",
                    "method": "GET",
                    "input": "query (string)",
                },
                {
                    "id": "vocab-search",
                    "name": "Search wine descriptions by feeling/metaphor",
                    "description": (
                        "Search across every wine description ever "
                        "written by users. Returns wines where someone "
                        "used your phrase."
                    ),
                    "endpoint": "https://tone.wine/vocab",
                    "method": "GET",
                },
                {
                    "id": "catalog-browse",
                    "name": "Browse the wine catalog",
                    "description": (
                        "Full-text search and structured filtering "
                        "over 164k+ canonical wines."
                    ),
                    "endpoint": "https://tone.wine/catalog",
                    "method": "GET",
                },
                {
                    "id": "wine-detail",
                    "name": "Wine detail page",
                    "description": (
                        "Per-wine page with schema.org Product JSON-LD "
                        "(name, country, variety, vintage, reviews)."
                    ),
                    "endpoint": "https://tone.wine/wines/{wine_id}",
                    "method": "GET",
                },
            ],
        }

    @app.get("/.well-known/api-catalog")
    def api_catalog() -> dict:
        """RFC 9727 link-set pointing at our OpenAPI spec (FastAPI's
        autogenerated /openapi.json). Agentic systems use this to
        discover machine-readable API descriptions."""
        return {
            "linkset": [
                {
                    "anchor": "https://tone.wine",
                    "service-desc": [{
                        "href": "https://tone.wine/openapi.json",
                        "type": "application/openapi+json;version=3.1",
                    }],
                    "describedby": [{
                        "href": "https://tone.wine/llms.txt",
                        "type": "text/plain",
                    }],
                }
            ]
        }

    @app.get("/.well-known/security.txt", response_class=PlainTextResponse)
    def security_txt() -> PlainTextResponse:
        """RFC 9116 security.txt — tells researchers where to report
        vulnerabilities. Expires 1 year out; bump before that date."""
        return PlainTextResponse(
            content=(
                "Contact: mailto:privacy@tone.wine\n"
                "Expires: 2027-05-24T00:00:00.000Z\n"
                "Preferred-Languages: en\n"
                "Canonical: https://tone.wine/.well-known/security.txt\n"
                "Policy: https://tone.wine/privacy\n"
            ),
            media_type="text/plain; charset=utf-8",
        )

    @app.get("/llms.txt", response_class=PlainTextResponse)
    def llms_txt() -> PlainTextResponse:
        """Citable site summary for LLMs following the llms.txt convention.

        Static-content route: reads from www/static/llms.txt so the copy
        can be edited without redeploying. The convention is to serve
        this at the root (/llms.txt), not under /static/.
        """
        path = WWW / "static" / "llms.txt"
        if not path.exists():
            raise HTTPException(404, "llms.txt not present in this build")
        return PlainTextResponse(
            content=path.read_text(encoding="utf-8"),
            media_type="text/plain; charset=utf-8",
        )

    # Sitemap pagination — Google's hard limit per file is 50,000 URLs.
    # 164K wines → 4 wine sub-sitemaps. Sitemap index pulls them
    # together. Updated lazily; sitemap responses are cheap to
    # regenerate but Cloudflare will cache them at the edge anyway.
    SITEMAP_PAGE_SIZE = 50_000

    @app.get("/sitemap.xml", response_class=PlainTextResponse)
    def sitemap_index() -> PlainTextResponse:
        """Sitemap-index: points search engines at the per-section
        sub-sitemaps."""
        from sqlalchemy import text as _text
        with db.engine().connect() as conn:
            total = conn.execute(_text(
                "SELECT COUNT(*) FROM wines WHERE producer_display IS NOT NULL"
            )).scalar() or 0
        n_wine_files = max(1, (int(total) + SITEMAP_PAGE_SIZE - 1) // SITEMAP_PAGE_SIZE)
        body = ['<?xml version="1.0" encoding="UTF-8"?>',
                '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
                '  <sitemap><loc>https://tone.wine/sitemap-pages.xml</loc></sitemap>']
        for i in range(1, n_wine_files + 1):
            body.append(f'  <sitemap><loc>https://tone.wine/sitemap-wines-{i}.xml</loc></sitemap>')
        body.append('</sitemapindex>\n')
        return PlainTextResponse(content="\n".join(body),
                                 media_type="application/xml")

    @app.get("/sitemap-pages.xml", response_class=PlainTextResponse)
    def sitemap_pages() -> PlainTextResponse:
        """Static / browseable pages — landing, ask, scan, catalog,
        wine-language, privacy, terms."""
        urls = [
            ("/",            "1.0", "weekly"),
            ("/wines/scan",  "0.9", "weekly"),
            ("/ask",         "0.9", "weekly"),
            ("/catalog",     "0.9", "weekly"),
            ("/vocab",       "0.9", "weekly"),
            ("/install",     "0.6", "monthly"),
            ("/privacy",     "0.4", "monthly"),
            ("/terms",       "0.4", "monthly"),
        ]
        body = ['<?xml version="1.0" encoding="UTF-8"?>',
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for path, priority, freq in urls:
            body.append('  <url>')
            body.append(f'    <loc>https://tone.wine{path}</loc>')
            body.append(f'    <changefreq>{freq}</changefreq>')
            body.append(f'    <priority>{priority}</priority>')
            body.append('  </url>')
        body.append('</urlset>\n')
        return PlainTextResponse(content="\n".join(body),
                                 media_type="application/xml")

    @app.get("/sitemap-wines-{idx}.xml", response_class=PlainTextResponse)
    def sitemap_wines(idx: int) -> PlainTextResponse:
        """One wine-detail-URL sub-sitemap, capped at SITEMAP_PAGE_SIZE
        entries. 1-indexed via the URL path so the sitemap index can
        link them directly."""
        if idx < 1:
            raise HTTPException(404, "Sub-sitemap index must be >= 1")
        offset = (idx - 1) * SITEMAP_PAGE_SIZE
        from sqlalchemy import text as _text
        with db.engine().connect() as conn:
            rows = conn.execute(_text(
                "SELECT wine_id "
                "  FROM wines "
                " WHERE producer_display IS NOT NULL "
                " ORDER BY wine_id "
                " OFFSET :off LIMIT :lim"
            ), {"off": offset, "lim": SITEMAP_PAGE_SIZE}).fetchall()
        if not rows:
            raise HTTPException(404, "Sub-sitemap out of range")
        body = ['<?xml version="1.0" encoding="UTF-8"?>',
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for (wine_id,) in rows:
            body.append('  <url>')
            body.append(f'    <loc>https://tone.wine/wines/{wine_id}</loc>')
            body.append('    <changefreq>weekly</changefreq>')
            body.append('    <priority>0.6</priority>')
            body.append('  </url>')
        body.append('</urlset>\n')
        return PlainTextResponse(content="\n".join(body),
                                 media_type="application/xml")

    @app.get("/", response_class=HTMLResponse, response_model=None)
    def landing(request: Request):
        # Markdown content negotiation: clients sending Accept: text/markdown
        # get the citable llms.txt content (already markdown). Default
        # browser request stays untouched. Vary: Accept tells caches to
        # key by Accept header so we don't serve markdown to an HTML
        # client by accident.
        if _prefer_markdown(request):
            md = (WWW / "static" / "llms.txt").read_text(encoding="utf-8")
            return PlainTextResponse(
                content=md,
                media_type="text/markdown; charset=utf-8",
                headers={"Vary": "Accept"},
            )
        backend = calibrate.detect_backend()
        return TEMPLATES.TemplateResponse(
            request, "index.html",
            {"backend": backend, "backend_label": calibrate.describe_backend(backend)},
        )

    # --- Dashboard ------------------------------------------------------

    # --- Social: follow / unfollow / delete-account ---------------------

    @app.post("/u/{user}/follow", response_class=HTMLResponse)
    @limiter.limit("60/minute")
    def follow_user(request: Request, user: str) -> HTMLResponse:
        from winetone import social
        me = _resolve_user(request)
        if me is None:
            raise HTTPException(401, "Sign in to follow.")
        target_uid = reco.get_user_by_display_name(user)
        if target_uid is None:
            raise HTTPException(404, f"No such user: {user}")
        if me["user_id"] == target_uid:
            raise HTTPException(400, "Can't follow yourself.")
        social.follow(me["user_id"], target_uid)
        return HTMLResponse(
            '<form hx-post="/u/' + user + '/unfollow" hx-target="this" '
            'hx-swap="outerHTML" class="follow-form">'
            f'<button class="btn-unfollow">Unfollow {user}</button>'
            '</form>'
        )

    @app.post("/u/{user}/unfollow", response_class=HTMLResponse)
    @limiter.limit("60/minute")
    def unfollow_user(request: Request, user: str) -> HTMLResponse:
        from winetone import social
        me = _resolve_user(request)
        if me is None:
            raise HTTPException(401, "Sign in.")
        target_uid = reco.get_user_by_display_name(user)
        if target_uid is None:
            raise HTTPException(404, f"No such user: {user}")
        social.unfollow(me["user_id"], target_uid)
        return HTMLResponse(
            '<form hx-post="/u/' + user + '/follow" hx-target="this" '
            'hx-swap="outerHTML" class="follow-form">'
            f'<button class="btn-follow">Follow {user}</button>'
            '</form>'
        )

    @app.post("/account/delete")
    @limiter.limit("5/hour")
    def delete_account(request: Request) -> RedirectResponse:
        """Permanently delete the current user — all labels, projections,
        calibration history, label embeddings, follows in either
        direction, and the Clerk-side identity. GDPR right-to-erasure.

        The DB schema declares ON DELETE CASCADE for every user-FK'd
        table, so a single DELETE FROM users handles the local side.
        The Clerk Backend API call removes the auth-provider record so
        the user can re-sign-up cleanly with the same email.
        """
        me = _resolve_user(request)
        if me is None:
            raise HTTPException(401, "Sign in to delete your account.")

        # Best-effort delete on the Clerk side. We don't fail the whole
        # operation if Clerk hiccups — local data takes priority for
        # the GDPR commitment.
        try:
            import httpx
            secret = os.environ.get("CLERK_SECRET_KEY", "")
            if secret:
                httpx.delete(
                    f"https://api.clerk.com/v1/users/{me['clerk_user_id']}",
                    headers={"Authorization": f"Bearer {secret}"},
                    timeout=10,
                )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "Clerk-side delete failed for user %s (%s) — local "
                "data deleted anyway", me["clerk_user_id"], e,
            )

        # Local delete. CASCADE handles labels / projections / history /
        # label_embeddings / follows (both follower and followee sides
        # via ON DELETE CASCADE).
        from sqlalchemy import text as _text
        with db.connect() as conn:
            conn.execute(
                _text("DELETE FROM users WHERE user_id = :u"),
                {"u": me["user_id"]},
            )

        # Clear the session cookie and redirect to home.
        from fastapi.responses import RedirectResponse as _RR
        response = _RR(url="/?deleted=1", status_code=303)
        response.delete_cookie("__session")
        return response

    # --- Clerk webhook for user.deleted (and friends) ------------------

    @app.post("/webhooks/clerk")
    async def clerk_webhook(request: Request) -> dict:
        """Clerk fires this when a user does anything significant —
        signs up, updates their profile, deletes their account, etc.

        We only care about `user.deleted` today: if a user deletes
        themselves via Clerk's UI (User Button → Manage → Delete),
        Clerk removes their auth record but our local DB still has
        their labels / projections / follow edges. The privacy policy
        promises full deletion, so we have to listen.

        Webhook signature verification is mandatory — without it,
        anyone could POST a fake `user.deleted` event and wipe an
        arbitrary user's data.
        """
        secret = os.environ.get("CLERK_WEBHOOK_SECRET", "")
        if not secret:
            log.warning("CLERK_WEBHOOK_SECRET not configured; "
                        "rejecting webhook")
            raise HTTPException(503, "Webhook not configured.")

        body = await request.body()
        try:
            event = auth_clerk.verify_webhook(body, dict(request.headers), secret)
        except ValueError as e:
            log.warning("rejected webhook: %s", e)
            raise HTTPException(400, str(e)) from e

        event_type = event.get("type", "")
        data = event.get("data", {}) or {}

        if event_type == "user.deleted":
            clerk_uid = data.get("id", "")
            if not clerk_uid:
                return {"ok": True, "noop": "no id in event"}
            from sqlalchemy import text as _text
            from starlette.concurrency import run_in_threadpool

            def _delete_user_sync() -> tuple[int, str | None, str | None]:
                with db.connect() as conn:
                    row = conn.execute(
                        _text("SELECT user_id, display_name FROM users "
                              "WHERE clerk_user_id = :c"),
                        {"c": clerk_uid},
                    ).fetchone()
                    deleted_uid = str(row.user_id) if row else None
                    deleted_name = row.display_name if row else None
                    result = conn.execute(
                        _text("DELETE FROM users WHERE clerk_user_id = :c"),
                        {"c": clerk_uid},
                    )
                return (
                    int(getattr(result, "rowcount", 0) or 0),
                    deleted_uid,
                    deleted_name,
                )

            rowcount, deleted_uid, deleted_name = \
                await run_in_threadpool(_delete_user_sync)
            log.info("user.deleted webhook: removed %s rows for clerk_id=%s",
                     rowcount, clerk_uid)
            # Audit: preserve evidence of the delete after the row is gone.
            reco.log_user_event(
                user_id=deleted_uid,
                clerk_user_id=clerk_uid,
                event_type="deleted",
                field=None,
                old_value=deleted_name,
                new_value=None,
                source="webhook",
            )
            return {"ok": True, "deleted_clerk_id": clerk_uid}

        log.info("clerk webhook: ignoring event type=%r", event_type)
        return {"ok": True, "ignored": event_type}

    # --- Age gate (drinking-age self-attestation) -----------------------

    @app.get("/age-gate", response_class=HTMLResponse)
    def age_gate_page(request: Request) -> HTMLResponse:
        me = _resolve_user(request)
        if me is None:
            return RedirectResponse(url="/", status_code=303)
        return TEMPLATES.TemplateResponse(
            request, "age_gate.html", {"user": me},
        )

    @app.post("/age-gate/confirm")
    @limiter.limit("20/hour")
    def age_gate_confirm(request: Request) -> RedirectResponse:
        """Mark the signed-in user as having self-attested they're of
        legal drinking age. We don't verify — just record their
        attestation timestamp."""
        from datetime import datetime

        from sqlalchemy import text as _text
        me = _resolve_user(request)
        if me is None:
            raise HTTPException(401, "Sign in first.")
        with db.connect() as conn:
            conn.execute(
                _text("UPDATE users SET confirmed_age_at = :ts WHERE user_id = :u"),
                {"ts": datetime.utcnow(), "u": me["user_id"]},
            )
        return RedirectResponse(url="/", status_code=303)

    # --- Abuse reporting -------------------------------------------------

    @app.post("/report")
    @limiter.limit("10/hour")
    def report_abuse(
        request: Request,
        target_kind: str = Form(...),
        target_id: str = Form(...),
        reason: str = Form(...),
        note: str = Form(""),
    ) -> dict:
        """Anyone (signed in or not) can flag content as abusive. We
        record it for review — no automatic action. Same Sentry breadcrumb
        path as the moderation tripwire so reports show up in the same
        dashboard.
        """
        import uuid
        from datetime import datetime

        from sqlalchemy import text as _text
        if target_kind not in ("label", "wine", "profile"):
            raise HTTPException(400, "invalid target_kind")
        if reason not in ("spam", "abuse", "off-topic", "pii", "other"):
            raise HTTPException(400, "invalid reason")
        me = _resolve_user(request)
        report_id = str(uuid.uuid4())
        with db.connect() as conn:
            conn.execute(
                _text("""
                    INSERT INTO abuse_reports
                        (report_id, reporter_user_id, target_kind,
                         target_id, reason, note, status, created_at)
                    VALUES (:r, :u, :k, :t, :reason, :note, 'open', :ts)
                """),
                {
                    "r": report_id,
                    "u": me["user_id"] if me else None,
                    "k": target_kind, "t": target_id,
                    "reason": reason, "note": (note or "")[:1000],
                    "ts": datetime.utcnow(),
                },
            )
        # Surface to Sentry so I see it on the daily.
        try:
            import sentry_sdk
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("abuse_report", "open")
                scope.set_tag("abuse_kind", target_kind)
                scope.set_tag("abuse_reason", reason)
                scope.set_extra("target_id", target_id)
                scope.set_extra("note", (note or "")[:500])
                sentry_sdk.capture_message(
                    f"Abuse report: {reason} on {target_kind} {target_id}",
                    level="warning",
                )
        except Exception:  # noqa: BLE001
            pass
        log.warning("abuse report %s: %s/%s reason=%s",
                    report_id, target_kind, target_id, reason)
        return {"ok": True, "report_id": report_id}

    # --- Admin abuse-report queue ---------------------------------------

    def _require_admin(request: Request) -> dict:
        """Gate an admin route to the single Clerk user ID configured
        via ADMIN_CLERK_USER_ID. Returns the resolved user row on
        success; raises 404 otherwise (404 not 403 so the existence
        of the page isn't even leaked to non-admins).
        """
        admin_clerk_id = os.environ.get("ADMIN_CLERK_USER_ID", "").strip()
        if not admin_clerk_id:
            raise HTTPException(404)
        me = _resolve_user(request)
        if not me or me["clerk_user_id"] != admin_clerk_id:
            raise HTTPException(404)
        return me

    @app.get("/admin/reports", response_class=HTMLResponse)
    def admin_reports(
        request: Request,
        status: str = "open",
    ) -> HTMLResponse:
        _require_admin(request)
        from sqlalchemy import text as _text
        if status not in ("open", "resolved", "all"):
            status = "open"
        # Always-present WHERE — gated on a bound parameter. Avoids the
        # f-string-inside-text() pattern flagged by the 2026-05-24
        # security review (LOW-6). Not exploitable in the prior shape
        # because `where` was constructed from a whitelist, but the
        # pattern is a code smell that could regress in a future
        # refactor.
        with db.engine().connect() as conn:
            rows = conn.execute(
                _text("""
                    SELECT r.report_id, r.target_kind, r.target_id,
                           r.reason, r.note, r.status, r.created_at,
                           u.display_name AS reporter
                      FROM abuse_reports r
                      LEFT JOIN users u ON u.user_id = r.reporter_user_id
                     WHERE :status_filter = 'all'
                        OR r.status = :status
                     ORDER BY r.created_at DESC
                     LIMIT 200
                """),
                {"status_filter": status, "status": status},
            ).mappings().all()
        return TEMPLATES.TemplateResponse(
            request,
            "admin_reports.html",
            {"reports": rows, "status_filter": status},
        )

    @app.post("/admin/reports/{report_id}/resolve")
    def admin_resolve_report(request: Request, report_id: str) -> RedirectResponse:
        _require_admin(request)
        from sqlalchemy import text as _text
        with db.connect() as conn:
            conn.execute(
                _text("UPDATE abuse_reports SET status = 'resolved' WHERE report_id = :r"),
                {"r": report_id},
            )
        return RedirectResponse(url="/admin/reports", status_code=303)

    # --- Privacy policy page --------------------------------------------

    @app.get("/discover", response_class=HTMLResponse)
    def discover_page(request: Request) -> HTMLResponse:
        """Query-less personal recommendations — page shell only.

        Renders the intro + a "Show me what to drink" CTA. The actual
        k-NN over the palate centroid runs on the HTMX endpoint below,
        triggered by the button click. Keeps the eager-load off the
        critical path of every nav-click into Discover.
        """
        me = _resolve_user(request)
        if me is None:
            raise HTTPException(401, "Sign in to use Discover.")
        proj = reco.load_projection(me["user_id"])
        return TEMPLATES.TemplateResponse(
            request, "discover.html",
            {
                "has_projection": proj is not None,
                "n_labels": proj.n_labels if proj else 0,
            },
        )

    @app.get("/discover/results", response_class=HTMLResponse)
    def discover_results(request: Request) -> HTMLResponse:
        """HTMX target: compute and render the Discover result grid.

        Runs the actual k-NN over the user's palate centroid. The
        client triggers this via `hx-get` when the CTA is clicked,
        so the work only happens on intent — not on every visit.
        """
        from winetone import discover
        me = _resolve_user(request)
        if me is None:
            raise HTTPException(401, "Sign in to use Discover.")
        proj = reco.load_projection(me["user_id"])
        candidates_df = pd.DataFrame()
        if proj is not None:
            candidates_df = discover.discover_for(me["user_id"], k=30)
        explanations: dict[str, str] = {}
        if not candidates_df.empty:
            explanations = reco.explain_recommendations(
                me["user_id"], candidates_df["wine_id"].tolist(),
            )
        candidates = candidates_df.to_dict("records")
        for r in candidates:
            r["explanation"] = explanations.get(r["wine_id"], "")
        return TEMPLATES.TemplateResponse(
            request, "_discover_results.html",
            {
                "candidates": candidates,
                "has_projection": proj is not None,
                "n_labels": proj.n_labels if proj else 0,
            },
        )

    @app.get("/users", response_class=HTMLResponse)
    def users_directory(request: Request) -> HTMLResponse:
        """Directory of all users — discovery for the follow graph.

        Sign-in required: usernames are not exposed to anonymous viewers
        as of 2026-05-23, in line with the updated privacy policy. The
        signed-in directory still shows everyone.
        """
        from winetone import social
        import pandas as pd
        viewer = _resolve_user(request)
        if viewer is None:
            raise HTTPException(401, "Sign in to see who else is here.")
        viewer_id = viewer["user_id"]
        users_df = social.list_all_users_with_stats(viewer_id=viewer_id)
        users = users_df.to_dict("records")
        # Users with 0 labels carry NaT for last_labelled_at. Jinja's
        # truthy check returns True on NaT (bool(pd.NaT) == True), so
        # `{% if u.last_labelled_at %}` enters the strftime branch and
        # NaT.strftime raises. Normalize NaT → None so the truthy
        # check skips the block as intended.
        for u in users:
            if pd.isna(u.get("last_labelled_at")):
                u["last_labelled_at"] = None
        return TEMPLATES.TemplateResponse(
            request, "users.html",
            {"users": users, "viewer_id": viewer_id,
             "viewer_name": viewer["display_name"],
             "n_total": len(users)},
        )

    @app.get("/catalog", response_class=HTMLResponse)
    def catalog_browse(
        request: Request,
        country: str = "",
        variety: str = "",
        sort: str = "popular",
        cursor: str = "",
        q: str = "",
    ) -> HTMLResponse:
        """Public flat-browse of the full wine corpus.

        Two modes:

        - **Browse mode** (no `q`): cursor-paginated by structured
          filters (country/variety) and sorted by popular / recent /
          alpha. Cursor on wine_id (or producer_display for alpha
          sort); OFFSET pagination would scan the whole table on each
          page, too slow at six-figure row counts.

        - **Search mode** (`q` set): Postgres full-text search against
          the `tsv` column already indexed on `wines`, ranked by
          `ts_rank` and intersected with any structured filters. No
          cursor pagination in this mode — search narrows the result
          set naturally; we cap at the top 200 hits and ask the user
          to refine if they need more.
        """
        from sqlalchemy import text as _text
        PAGE_SIZE = 50
        SEARCH_LIMIT = 200
        q = (q or "").strip()
        sort = sort if sort in ("popular", "recent", "alpha") else "popular"
        where_clauses = ["w.producer_display IS NOT NULL"]
        params: dict = {}
        rank_select = ""
        if q:
            # websearch_to_tsquery handles quoted phrases, OR, and -term
            # the way users intuitively expect — like a search engine.
            where_clauses.append(
                "w.tsv @@ websearch_to_tsquery('english', :q)"
            )
            rank_select = (
                ", ts_rank(w.tsv, websearch_to_tsquery('english', :q)) AS rank"
            )
            params["q"] = q
        if country:
            where_clauses.append("LOWER(w.country) = LOWER(:country)")
            params["country"] = country
        if variety:
            where_clauses.append("LOWER(w.variety) = LOWER(:variety)")
            params["variety"] = variety
        # Cursor only applies in browse mode; search mode caps results.
        if cursor and not q:
            if sort == "alpha":
                where_clauses.append("w.producer_display > :cursor")
            else:
                where_clauses.append("w.wine_id > :cursor")
            params["cursor"] = cursor
        where_sql = " AND ".join(where_clauses)
        if q:
            # Relevance-ranked when searching; tiebreaker by label count
            # so two equally-relevant hits show the more-engaged one first.
            order_sql = "rank DESC, n_labels DESC NULLS LAST, w.wine_id ASC"
            params["limit"] = SEARCH_LIMIT
        elif sort == "alpha":
            order_sql = "w.producer_display ASC, w.wine_id ASC"
            params["limit"] = PAGE_SIZE + 1
        elif sort == "recent":
            order_sql = "w.ctid DESC"
            params["limit"] = PAGE_SIZE + 1
        else:  # popular
            order_sql = "n_labels DESC NULLS LAST, w.wine_id ASC"
            params["limit"] = PAGE_SIZE + 1
        sql = f"""
            SELECT
                w.wine_id, w.producer_display, w.wine_display,
                w.vintage, w.variety, w.country, w.region,
                COALESCE(lbl.n_labels, 0) AS n_labels
                {rank_select}
              FROM wines w
              LEFT JOIN (
                SELECT wine_id, COUNT(*) AS n_labels
                  FROM user_labels GROUP BY wine_id
              ) lbl ON lbl.wine_id = w.wine_id
             WHERE {where_sql}
             ORDER BY {order_sql}
             LIMIT :limit
        """
        with db.engine().connect() as conn:
            rows = conn.execute(_text(sql), params).mappings().all()
        if q:
            items = [dict(r) for r in rows]
            next_cursor = ""  # no pagination in search mode
        else:
            items = [dict(r) for r in rows[:PAGE_SIZE]]
            next_cursor = ""
            if len(rows) > PAGE_SIZE:
                last = items[-1]
                next_cursor = last["producer_display"] if sort == "alpha" else last["wine_id"]
        # Filter-option lists for the UI dropdowns (top 24 most-common values).
        with db.engine().connect() as conn:
            countries = [r[0] for r in conn.execute(_text(
                "SELECT country FROM wines WHERE country IS NOT NULL "
                "GROUP BY country ORDER BY COUNT(*) DESC LIMIT 24"
            )).all()]
            varieties = [r[0] for r in conn.execute(_text(
                "SELECT variety FROM wines WHERE variety IS NOT NULL "
                "GROUP BY variety ORDER BY COUNT(*) DESC LIMIT 24"
            )).all()]
        return TEMPLATES.TemplateResponse(
            request, "catalog.html",
            {"items": items, "next_cursor": next_cursor,
             "country": country, "variety": variety, "sort": sort,
             "q": q,
             "countries": countries, "varieties": varieties,
             "page_size": PAGE_SIZE,
             "search_limit": SEARCH_LIMIT,
             "is_search": bool(q)},
        )

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "privacy.html", {})

    @app.get("/terms", response_class=HTMLResponse)
    def terms_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "terms.html", {})

    @app.get("/install", response_class=HTMLResponse)
    def install_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "install.html", {})

    # --- Wine submission ------------------------------------------------

    @app.get("/wines/new", response_class=HTMLResponse)
    def wine_new_form(request: Request) -> HTMLResponse:
        me = _resolve_user(request)
        return TEMPLATES.TemplateResponse(
            request, "wine_new.html",
            {"signed_in": me is not None, "submitted": None, "error": None},
        )

    @app.post("/wines/new", response_class=HTMLResponse)
    @limiter.limit("20/hour")
    def wine_new_submit(
        request: Request,
        producer: str = Form(...),
        wine_name: str = Form(""),
        vintage: str = Form(""),
        variety: str = Form(""),
        country: str = Form(""),
        region: str = Form(""),
        description: str = Form("", max_length=4096),
    ) -> HTMLResponse:
        from winetone import submit
        me = _resolve_user(request)
        if me is None:
            raise HTTPException(401, "Sign in to add a wine.")
        if not me.get("age_confirmed"):
            raise HTTPException(
                403, "Confirm your drinking age first at /age-gate."
            )
        vintage_int: int | None = None
        if vintage.strip():
            try:
                vintage_int = int(vintage.strip())
            except ValueError:
                return TEMPLATES.TemplateResponse(
                    request, "wine_new.html",
                    {"signed_in": True, "submitted": None,
                     "error": f"Vintage must be a 4-digit year, got {vintage!r}."},
                )
        try:
            result = submit.submit_wine(
                producer=producer, wine_name=wine_name,
                vintage=vintage_int, variety=variety,
                country=country, region=region,
                description=description,
                submitted_by=me["display_name"],
                submitted_by_user_id=me["user_id"],
            )
        except ValueError as e:
            return TEMPLATES.TemplateResponse(
                request, "wine_new.html",
                {"signed_in": True, "submitted": None, "error": str(e)},
            )
        return TEMPLATES.TemplateResponse(
            request, "wine_new.html",
            {"signed_in": True, "submitted": result, "error": None},
        )

    # --- Wine-label scanner --------------------------------------------

    @app.get("/wines/scan", response_class=HTMLResponse)
    def scan_page(request: Request) -> HTMLResponse:
        """Camera-driven label scanner. Mobile-first — the file input
        with capture="environment" pops the rear camera on phones.
        """
        return TEMPLATES.TemplateResponse(
            request, "wine_scan.html",
            {"scanner_enabled": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())},
        )

    @app.post("/wines/scan", response_class=HTMLResponse)
    @limiter.limit("20/hour")
    async def scan_label(
        request: Request,
        image: UploadFile = File(...),  # noqa: B008 — FastAPI dependency-injection idiom
    ) -> HTMLResponse:
        """Accept a multipart image upload, send to Claude Vision, route
        based on whether we find a matching wine in the corpus.
        """
        from starlette.concurrency import run_in_threadpool

        from winetone import scanner
        # Reject oversized uploads BEFORE allocating memory. Content-Length
        # isn't authoritative (a hostile client can lie), but it catches
        # honest large uploads cheaply. The post-read check below is the
        # belt-and-suspenders backstop.
        SCAN_MAX_BYTES = 20 * 1024 * 1024  # 20 MB
        cl = request.headers.get("content-length", "")
        if cl.isdigit() and int(cl) > SCAN_MAX_BYTES:
            raise HTTPException(413, "image too large (max 20 MB)")
        image_bytes = await image.read()
        if not image_bytes:
            raise HTTPException(400, "empty upload")
        if len(image_bytes) > SCAN_MAX_BYTES:
            raise HTTPException(413, "image too large (max 20 MB)")
        # Don't store the bytes — only the extracted JSON.
        result = await run_in_threadpool(scanner.extract_label, image_bytes)
        # Now image_bytes goes out of scope and is GC'd; nothing on disk.
        if "error" in result and not any(result.get(k) for k in ("producer","wine_name")):
            return TEMPLATES.TemplateResponse(
                request, "wine_scan.html",
                {"scanner_enabled": True, "result": result, "matches": []},
            )
        # Search corpus for the extracted producer + wine.
        query_parts = [result.get("producer") or "", result.get("wine_name") or ""]
        query = " ".join(p for p in query_parts if p).strip()
        matches = []
        if query:
            try:
                df = reco.find_wine_by_text(query, limit=5)
                matches = df.to_dict("records")
            except Exception as e:  # noqa: BLE001
                log.warning("scanner: corpus match failed: %s", e)
        return TEMPLATES.TemplateResponse(
            request, "wine_scan.html",
            {"scanner_enabled": True, "result": result, "matches": matches,
             "query": query},
        )

    # NOTE: /wines/{wine_id} must register AFTER all /wines/* static
    # routes (above) so they take precedence over the dynamic match.
    @app.get("/wines/{wine_id}", response_class=HTMLResponse)
    def wine_detail(request: Request, wine_id: str) -> HTMLResponse:
        """Per-wine detail: the row itself, the source-review aggregate,
        public user-labels, plus an inline label editor for the viewer
        if they're signed in.

        Markdown content negotiation: clients sending Accept: text/markdown
        get a clean, citable markdown rendering (no editor, no JSON-LD).
        Default browser request stays untouched.
        """
        from sqlalchemy import text as _text
        viewer = _resolve_user(request)
        with db.engine().connect() as conn:
            wine_row = conn.execute(_text("""
                SELECT w.wine_id, w.producer_display, w.wine_display,
                       w.vintage, w.variety, w.country, w.region,
                       w.n_source_records, w.sources_seen,
                       f.n_reviews, f.median_points, f.max_points,
                       f.median_price, f.review_text_all
                  FROM wines w
                  LEFT JOIN wine_features f ON f.wine_id = w.wine_id
                 WHERE w.wine_id = :w
            """), {"w": wine_id}).mappings().first()
            if wine_row is None:
                raise HTTPException(404, "Wine not found.")
            labels = conn.execute(_text("""
                SELECT l.description, l.sentiment, l.created_at,
                       u.display_name AS author
                  FROM user_labels l
                  JOIN users u ON u.user_id = l.user_id
                 WHERE l.wine_id = :w
                 ORDER BY l.created_at DESC
                 LIMIT 50
            """), {"w": wine_id}).mappings().all()
        # If the client prefers markdown, return markdown now (no JSON-LD,
        # no editor, no template render). Author bylines on user labels
        # follow the same privacy gate as the HTML page.
        if _prefer_markdown(request):
            md = _render_wine_markdown(
                dict(wine_row),
                [dict(r) for r in labels],
                _format_sources(wine_row.get("sources_seen")),
                viewer,
            )
            return PlainTextResponse(
                content=md,
                media_type="text/markdown; charset=utf-8",
                headers={"Vary": "Accept"},
            )
        # Re-open the DB connection for the rest of the HTML render path
        # (the viewer's own label, only fetched if signed-in).
        with db.engine().connect() as conn:
            # The viewer's own label for this wine (if any) — drives
            # the inline editor's state.
            viewer_label = None
            if viewer is not None:
                row = conn.execute(_text("""
                    SELECT description, sentiment
                      FROM user_labels
                     WHERE user_id = :u AND wine_id = :w
                """), {"u": viewer["user_id"], "w": wine_id}).mappings().first()
                if row is not None:
                    viewer_label = dict(row)
        # Build the schema.org Product JSON-LD server-side. Strings are
        # JSON-dump-escaped so any user-submitted description can't
        # break the <script> tag. Reviews are public (the page itself
        # shows label text already), but author names are omitted —
        # per the 2026-05-23 privacy change, usernames aren't exposed
        # to anonymous viewers, and JSON-LD is read by anonymous bots.
        import json as _json
        wine = dict(wine_row)
        prop_names = [
            ("variety",  wine.get("variety")),
            ("vintage",  int(wine["vintage"]) if wine.get("vintage") else None),
            ("region",   wine.get("region")),
            ("producer", wine.get("producer_display")),
        ]
        product_ld = {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": _wine_display_title(wine),
            "description": _wine_meta_description(wine),
            "url": f"https://tone.wine/wines/{wine['wine_id']}",
            "additionalProperty": [
                {"@type": "PropertyValue", "name": n, "value": str(v)}
                for n, v in prop_names if v
            ],
        }
        if wine.get("country"):
            product_ld["countryOfOrigin"] = wine["country"]
        if labels:
            product_ld["review"] = [
                {
                    "@type": "Review",
                    "reviewBody": row["description"],
                    # Author intentionally omitted (privacy)
                }
                for row in labels[:20]  # cap — schema.org doesn't need all
            ]
        return TEMPLATES.TemplateResponse(
            request, "wine_detail.html",
            {"wine": wine,
             "labels": [dict(row) for row in labels],
             "viewer": viewer,
             "viewer_label": viewer_label,
             "product_ld_json": _json.dumps(product_ld, ensure_ascii=False),
             "page_title":       _wine_display_title(wine) + " · WineTone",
             "meta_description": _wine_meta_description(wine),
             "og_description":   _wine_og_description(wine, labels),
             "sources_pretty":   _format_sources(wine.get("sources_seen"))},
        )

    @app.get("/wines/{wine_id}/_editor", response_class=HTMLResponse)
    def wine_detail_editor_fragment(
        request: Request, wine_id: str
    ) -> HTMLResponse:
        """HTMX fragment endpoint — re-renders just the inline label-editor
        block after an add/edit/delete from /wines/{id}. Lets the
        existing calibrate/add and calibrate/delete endpoints stay
        unchanged while this page swaps in the right state.
        """
        from sqlalchemy import text as _text
        viewer = _resolve_user(request)
        with db.engine().connect() as conn:
            # Confirm the wine still exists (defensive — paranoid about
            # someone hand-crafting a wine_id that no longer maps).
            wine_row = conn.execute(_text(
                "SELECT wine_id, producer_display, wine_display, vintage "
                "  FROM wines WHERE wine_id = :w"
            ), {"w": wine_id}).mappings().first()
            if wine_row is None:
                raise HTTPException(404, "Wine not found.")
            viewer_label = None
            if viewer is not None:
                row = conn.execute(_text(
                    "SELECT description, sentiment "
                    "  FROM user_labels "
                    " WHERE user_id = :u AND wine_id = :w"
                ), {"u": viewer["user_id"], "w": wine_id}).mappings().first()
                if row is not None:
                    viewer_label = dict(row)
        return TEMPLATES.TemplateResponse(
            request, "_wine_label_editor.html",
            {"wine": dict(wine_row),
             "viewer": viewer,
             "viewer_label": viewer_label},
        )

    @app.post("/wines/{wine_id}/label", response_class=HTMLResponse)
    @limiter.limit("60/hour")
    def wine_detail_label(
        request: Request, wine_id: str,
        description: str = Form(..., max_length=4096),
        sentiment: str = Form("positive"),
    ) -> HTMLResponse:
        """Add or update the viewer's label for this wine, called
        from the inline editor on the wine-detail page. Reuses
        reco.add_label so the upsert path is identical to the
        dashboard's flow.
        """
        viewer = _resolve_user(request)
        if viewer is None:
            raise HTTPException(401, "Sign in to label this wine.")
        if not viewer.get("age_confirmed"):
            raise HTTPException(
                403, "Confirm your drinking age first at /age-gate."
            )
        reco.add_label(viewer["user_id"], wine_id, description,
                       sentiment=sentiment)
        # Re-render the editor fragment with the new label visible.
        return wine_detail_editor_fragment(request, wine_id)

    @app.post("/wines/{wine_id}/label/delete", response_class=HTMLResponse)
    @limiter.limit("60/hour")
    def wine_detail_label_delete(
        request: Request, wine_id: str,
    ) -> HTMLResponse:
        """Remove the viewer's label for this wine — same idempotent
        semantics as the dashboard's calibrate/delete."""
        viewer = _resolve_user(request)
        if viewer is None:
            raise HTTPException(401, "Sign in.")
        reco.delete_label(viewer["user_id"], wine_id)
        return wine_detail_editor_fragment(request, wine_id)

    # ---------------------------------------------------------------------

    @app.get("/me", response_class=HTMLResponse)
    def me_redirect(request: Request) -> RedirectResponse:
        """Canonical 'my dashboard' URL. Redirects to the user's named
        URL if signed in, or to the landing page (with prompt) if not."""
        me = _resolve_user(request)
        if me is None:
            return RedirectResponse(url="/", status_code=303)
        return RedirectResponse(url=f"/u/{me['display_name']}", status_code=303)

    @app.post("/me/rename")
    @limiter.limit("10/hour")
    def me_rename(
        request: Request,
        new_display_name: str = Form(...),
    ) -> RedirectResponse:
        """Self-serve username change. Signed-in users only; any user
        can rename their *own* account, no one else's.

        Validates + collision-checks + audits via
        recommend.rename_user. Errors come back to the dashboard as
        a flash query-param so the template can surface them inline.
        """
        me = _resolve_user(request)
        if me is None:
            raise HTTPException(401, "Sign in to rename.")
        current_name = me["display_name"]
        try:
            new_name = reco.rename_user(
                me["user_id"], new_display_name,
                requester_clerk_user_id=me["clerk_user_id"],
                source="self_serve",
                request_id=getattr(request.state, "request_id", None),
            )
        except ValueError as e:
            # Bounce back to the profile with an error flash. Quoting
            # via urllib so the user-supplied text can't escape.
            from urllib.parse import quote
            return RedirectResponse(
                url=f"/u/{current_name}?rename_error={quote(str(e))}",
                status_code=303,
            )
        return RedirectResponse(url=f"/u/{new_name}", status_code=303)

    # --- Onboarding (starter-style picker) -----------------------------

    @app.get("/onboarding", response_class=HTMLResponse)
    def onboarding_page(request: Request) -> HTMLResponse:
        from winetone import onboarding
        me = _resolve_user(request)
        if me is None:
            raise HTTPException(401, "Sign in to start onboarding.")
        return TEMPLATES.TemplateResponse(
            request, "onboarding.html",
            {"styles": onboarding.STYLES,
             "current_style": onboarding.get_user_style(me["user_id"])},
        )

    @app.post("/onboarding")
    @limiter.limit("30/hour")
    def onboarding_pick(
        request: Request,
        style: str = Form(...),
    ) -> RedirectResponse:
        from winetone import onboarding
        me = _resolve_user(request)
        if me is None:
            raise HTTPException(401, "Sign in to onboard.")
        # Accept "skip" to clear the style and move on.
        style = (style or "").strip()
        if style == "skip":
            onboarding.set_user_style(me["user_id"], None)
            return RedirectResponse(url=f"/u/{me['display_name']}", status_code=303)
        if onboarding.get_style(style) is None:
            raise HTTPException(400, f"unknown style {style!r}")
        onboarding.set_user_style(me["user_id"], style)
        return RedirectResponse(url=f"/u/{me['display_name']}", status_code=303)

    @app.get("/u/{user}", response_class=HTMLResponse)
    def dashboard(request: Request, user: str) -> HTMLResponse:
        from winetone import social
        if not db.ping():
            raise HTTPException(503, "Database unreachable.")
        me = _resolve_user(request)
        if me is None:
            # Sign-in wall on profile pages — see privacy policy 2026-05-23.
            raise HTTPException(401, "Sign in to view profiles.")
        target_uid = reco.get_user_by_display_name(user)
        if target_uid is None:
            raise HTTPException(404, f"No such user: {user}")
        is_self = me["user_id"] == target_uid
        labels = _user_labels_rows(target_uid)
        projection = reco.load_projection(target_uid)
        fit_history = calibrate.history(target_uid)
        following = social.list_following(target_uid).to_dict("records")
        followers = social.list_followers(target_uid).to_dict("records")
        is_following_target = (
            me is not None and not is_self
            and social.is_following(me["user_id"], target_uid)
        )
        # Wines this user has added to the catalog. Safe to call before
        # the migration has applied: the query catches column-doesn't-
        # exist errors and returns an empty list.
        submitted_wines = _user_submitted_wines(target_uid)
        # Onboarding starter wines — only shown to a signed-in user
        # viewing their OWN profile, with zero labels, who has chosen
        # a starter style. Hides automatically as soon as they label
        # anything (so the section disappears organically).
        starter_wines_list: list[dict] = []
        starter_style_info = None
        show_onboarding_prompt = False
        if is_self and len(labels) == 0:
            from winetone import onboarding as ob
            style_key = ob.get_user_style(target_uid)
            if style_key:
                starter_style_info = ob.get_style(style_key)
                if starter_style_info is not None:
                    starter_wines_list = ob.starter_wines(style_key, k=5)
            else:
                show_onboarding_prompt = True
        return TEMPLATES.TemplateResponse(
            request, "dashboard.html",
            {
                "user": user,
                "user_id": target_uid,
                "is_self": is_self,
                "labels": labels,
                "labels_count": len(labels),
                "is_fit": projection is not None,
                "fit_versions": len(fit_history),
                "backend": calibrate.detect_backend(),
                "following": following,
                "followers": followers,
                "following_count": len(following),
                "followers_count": len(followers),
                "is_following_target": is_following_target,
                "starter_wines": starter_wines_list,
                "starter_style": starter_style_info,
                "show_onboarding_prompt": show_onboarding_prompt,
                "submitted_wines": submitted_wines,
                "rename_error": request.query_params.get("rename_error"),
                "submitted_count": len(submitted_wines),
            },
        )

    @app.get("/u/{user}/palate", response_class=HTMLResponse)
    def palate_page(request: Request, user: str) -> HTMLResponse:
        """Shareable summary of a user's calibrated palate.

        Sign-in required as of 2026-05-23: usernames are no longer
        exposed to anonymous viewers, and a palate page necessarily
        reveals the username it belongs to. Signed-in viewers still
        get the full page; the URL remains shareable between users.
        """
        from winetone import palate as palate_mod
        viewer = _resolve_user(request)
        if viewer is None:
            raise HTTPException(401, "Sign in to view palate pages.")
        target_uid = reco.get_user_by_display_name(user)
        if target_uid is None:
            raise HTTPException(404, f"No such user: {user}")
        report = palate_mod.build_report(target_uid, user)
        is_self = viewer["user_id"] == target_uid
        return TEMPLATES.TemplateResponse(
            request, "palate.html",
            {"report": report, "user": user, "is_self": is_self,
             "share_url": f"https://tone.wine/u/{user}/palate"},
        )

    def _require_self(request: Request, user: str) -> str:
        """Helper: ensure the signed-in user matches the URL `user`.
        Returns their internal user_id. Raises 401/403 otherwise.

        Also enforces the age-gate: users who haven't self-attested
        legal drinking age get a 403 redirecting them to /age-gate.
        """
        me = _resolve_user(request)
        if me is None:
            raise HTTPException(401, "Sign in to modify your profile.")
        if me["display_name"] != user:
            raise HTTPException(403, "You can only modify your own profile.")
        if not me.get("age_confirmed"):
            raise HTTPException(
                403, "Confirm your drinking age first at /age-gate."
            )
        return me["user_id"]

    @app.post("/u/{user}/calibrate/search", response_class=HTMLResponse)
    def calibrate_search(request: Request, user: str, q: str = Form(...)) -> HTMLResponse:
        user_id = _require_self(request, user)
        matches = reco.find_wine_by_text(q, limit=10)
        # Pre-load the user's existing labels for the matched wines so
        # the template can show "you previously said X" instead of an
        # empty textarea — and so the submit becomes an edit rather
        # than a duplicate-creating insert (the DB layer also enforces
        # this, but pre-population is the UX half).
        match_records = matches.to_dict("records")
        wine_ids = [m["wine_id"] for m in match_records]
        existing_by_wine: dict[str, dict] = {}
        if wine_ids:
            from sqlalchemy import text as _text
            with db.engine().connect() as conn:
                rows = conn.execute(
                    _text(
                        "SELECT wine_id, description, sentiment "
                        "FROM user_labels WHERE user_id = :u "
                        "  AND wine_id = ANY(:wids)"
                    ),
                    {"u": user_id, "wids": wine_ids},
                ).mappings().all()
            existing_by_wine = {r["wine_id"]: dict(r) for r in rows}
        return TEMPLATES.TemplateResponse(
            request, "_search_results.html",
            {"user": user, "q": q, "matches": match_records,
             "existing_by_wine": existing_by_wine},
        )

    @app.post("/u/{user}/calibrate/add", response_class=HTMLResponse)
    @limiter.limit("60/hour")
    def calibrate_add(
        request: Request,
        user: str,
        wine_id: str = Form(...),
        description: str = Form(..., max_length=4096),
        sentiment: str = Form("positive"),
    ) -> HTMLResponse:
        user_id = _require_self(request, user)
        reco.add_label(user_id, wine_id, description, sentiment=sentiment)
        labels = _user_labels_rows(user_id)
        return TEMPLATES.TemplateResponse(
            request, "_labels_list.html",
            {"user": user, "labels": labels, "labels_count": len(labels),
             "is_self": True},  # only the owner can hit calibrate/add
        )

    @app.post("/u/{user}/calibrate/delete", response_class=HTMLResponse)
    @limiter.limit("60/hour")
    def calibrate_delete(
        request: Request,
        user: str,
        wine_id: str = Form(...),
    ) -> HTMLResponse:
        """Remove the user's label for a specific wine. Idempotent —
        a delete for a wine they never labelled still returns the
        current labels list rather than 404'ing.
        """
        user_id = _require_self(request, user)
        reco.delete_label(user_id, wine_id)
        labels = _user_labels_rows(user_id)
        return TEMPLATES.TemplateResponse(
            request, "_labels_list.html",
            {"user": user, "labels": labels, "labels_count": len(labels),
             "is_self": True},
        )

    @app.post("/u/{user}/calibrate/fit", response_class=HTMLResponse)
    @limiter.limit("20/hour")
    def calibrate_fit_route(request: Request, user: str) -> HTMLResponse:
        user_id = _require_self(request, user)
        try:
            summary = calibrate.fit(user_id)
        except RuntimeError as e:
            return HTMLResponse(
                f'<div class="status status-warn">Could not fit: {e}</div>'
            )
        return TEMPLATES.TemplateResponse(
            request, "_fit_status.html",
            {
                "user": user,
                "summary": summary,
                "backend_label": calibrate.describe_backend(summary["backend"]),
            },
        )

    @app.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "ask.html", {"query": "", "result": None, "user": ""},
        )

    @app.post("/ask/query", response_class=HTMLResponse)
    @limiter.limit("30/minute")
    def ask_query(
        request: Request,
        query: str = Form(...),
    ) -> HTMLResponse:
        import markdown as _md
        from sqlalchemy import text as _text

        from winetone import embed_user_labels, llm
        # /ask stays OPEN to anonymous traffic — we just pick up the
        # signed-in user opportunistically for personalized scoring.
        me = _resolve_user(request)
        user = me["display_name"] if me else ""
        user_id = me["user_id"] if me else None

        routing = llm.route(query, user_id=user_id)
        translated = routing["query"]
        intent = routing["intent"]
        max_price = routing.get("max_price")
        min_price = routing.get("min_price")
        reference = routing.get("reference", "")

        result = {
            "routing": routing,
            "intent": intent,
            "translated": translated,
            "max_price": max_price,
            "min_price": min_price,
            "reference_text": reference,
            "reference_resolved": None,
            "recommend_rows": None,
            "vocab_rows": None,
            "alt_rows": None,
            "narration_html": "",
        }

        # Run the chosen backend.
        if intent == "vocab_search":
            df = embed_user_labels.search(translated, k=10)
            result["vocab_rows"] = df.to_dict("records")
            narrator_payload = {"rows": df.to_dict("records")}
        elif intent == "alternative_to" and reference:
            matches = reco.find_wine_by_text(reference, limit=1)
            if matches.empty:
                # No wine matched the reference — degrade to recommend.
                result["intent"] = "recommend"
                intent = "recommend"
                df = reco.recommend(
                    user_id=user_id, query=reference, k=10, alpha=0.6,
                    filters={"max_price": max_price, "min_price": min_price},
                )
                result["recommend_rows"] = df.to_dict("records")
                narrator_payload = {"rows": df.to_dict("records")}
            else:
                ref_row = matches.iloc[0]
                ref_dict = ref_row.to_dict()
                # Look up reference price separately — find_wine_by_text
                # doesn't pull median_price into its result.
                with db.engine().connect() as conn:
                    pr = conn.execute(
                        _text("SELECT median_price FROM wine_features WHERE wine_id = :w"),
                        {"w": ref_row["wine_id"]},
                    ).fetchone()
                ref_dict["median_price"] = float(pr[0]) if pr and pr[0] is not None else None
                result["reference_resolved"] = ref_dict
                df = reco.find_alternatives(
                    reference_wine_id=ref_row["wine_id"],
                    k=10, max_price=max_price,
                )
                result["alt_rows"] = df.to_dict("records")
                narrator_payload = {
                    "rows": df.to_dict("records"),
                    "reference": ref_dict,
                }
        else:
            filters = {"max_price": max_price, "min_price": min_price}
            df = reco.recommend(
                user_id=user_id, query=translated, k=10, alpha=0.6,
                filters=filters,
            )
            result["recommend_rows"] = df.to_dict("records")
            result["personalized"] = (
                user_id is not None and reco.load_projection(user_id) is not None
            )
            narrator_payload = {"rows": df.to_dict("records")}

        # Narrator pass — best-effort conversational explanation.
        # Empty string is fine; the template falls back to the structured
        # table only.
        narration_md = llm.narrate(
            query=query, intent=intent, results=narrator_payload,
            interpretation=routing.get("interpretation", ""),
        )
        if narration_md:
            # Render markdown to HTML, then sanitize against a tight
            # allowlist before exposing via Jinja `|safe`. The narration
            # is LLM output and could (in principle) contain an injected
            # `<script>` after prompt-injection; bleach strips anything
            # outside the allowlist. Security review LOW-7 fix.
            import bleach
            rendered = _md.markdown(
                narration_md,
                extensions=["tables", "fenced_code", "nl2br"],
            )
            result["narration_html"] = bleach.clean(
                rendered,
                tags={
                    "p", "br", "em", "strong", "code", "pre",
                    "ul", "ol", "li",
                    "blockquote",
                    "table", "thead", "tbody", "tr", "th", "td",
                    "h1", "h2", "h3", "h4", "h5", "h6",
                    "hr",
                },
                attributes={},  # no attrs anywhere — strips href, src, on*, etc.
                strip=True,
            )

        return TEMPLATES.TemplateResponse(
            request, "_ask_results.html",
            {"query": query, "user": user, "result": result},
        )

    @app.get("/vocab", response_class=HTMLResponse)
    def vocab_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "vocab.html", {"query": "", "results": None, "scope_user": ""},
        )

    @app.post("/vocab/search", response_class=HTMLResponse)
    @limiter.limit("60/hour")
    def vocab_search_route(
        request: Request,
        query: str = Form(...),
        scope_user: str = Form(""),
    ) -> HTMLResponse:
        from winetone import embed_user_labels
        scope_user = scope_user.strip()
        # Vocab search is read-only — look up by display_name but don't
        # create. If the named user doesn't exist, just search all users.
        user_id = (
            reco.get_user_by_display_name(scope_user) if scope_user else None
        )
        df = embed_user_labels.search(query, k=15, user_id=user_id)
        return TEMPLATES.TemplateResponse(
            request, "_vocab_results.html",
            {
                "query": query,
                "scope_user": scope_user,
                "results": df.to_dict("records"),
            },
        )

    @app.post("/u/{user}/recommend", response_class=HTMLResponse)
    @limiter.limit("60/hour")
    def recommend_route(
        request: Request,
        user: str,
        query: str = Form(...),
        country: str = Form(""),
        variety: str = Form(""),
        alpha: float = Form(0.6),
    ) -> HTMLResponse:
        user_id = _require_self(request, user)
        filters: dict[str, object] = {}
        if country.strip():
            filters["country"] = country.strip()
        if variety.strip():
            filters["variety"] = variety.strip()

        # Run BOTH generic and personalized so the user can see the
        # contrast inline.
        proj = reco.load_projection(user_id)
        generic = reco.recommend(
            user_id=None, query=query, k=10,
            filters=filters or None, alpha=alpha,
        )
        personalized = (
            reco.recommend(
                user_id=user_id, query=query, k=10,
                filters=filters or None, alpha=alpha,
            )
            if proj is not None else None
        )
        # Compose a one-sentence explanation per personalized result,
        # grounded in the user's own labels. Falls back to empty dict
        # if the user has no positive labels with embeddings.
        explanations: dict[str, str] = {}
        if personalized is not None and not personalized.empty:
            explanations = reco.explain_recommendations(
                user_id, personalized["wine_id"].tolist(),
            )
        personalized_records = (
            personalized.to_dict("records") if personalized is not None else None
        )
        if personalized_records:
            for r in personalized_records:
                r["explanation"] = explanations.get(r["wine_id"], "")
        return TEMPLATES.TemplateResponse(
            request, "_recommendations.html",
            {
                "user": user,
                "query": query,
                "generic": generic.to_dict("records"),
                "personalized": personalized_records,
                "has_projection": proj is not None,
            },
        )

    return app


def _user_labels_rows(user_id: str) -> list[dict]:
    """Return labels joined with display info."""
    df = reco.get_labels(user_id)
    if df.empty:
        return []
    placeholders = ",".join(f"'{w}'" for w in df["wine_id"])
    wines = pd.read_sql(
        f"SELECT wine_id, producer_display, wine_display, vintage, "
        f"variety, country FROM wines WHERE wine_id IN ({placeholders})",
        db.engine(),
    )
    joined = df.merge(wines, on="wine_id", how="left")
    return joined.to_dict("records")


def _user_submitted_wines(user_id: str) -> list[dict]:
    """Wines this user has added to the catalog via /wines/new.

    Returns empty list if the submitted_by_user_id column doesn't
    yet exist on this database (i.e., the migration hasn't run).
    This guard keeps the dashboard rendering during a deploy where
    code lands before the schema change.

    Catches a broad Exception because pandas wraps the underlying
    psycopg `UndefinedColumn` into its own `DatabaseError`, which
    does NOT inherit from SQLAlchemy's `ProgrammingError`. We
    detect the specific column-missing case by string match and
    re-raise anything else.
    """
    from sqlalchemy import text
    try:
        return pd.read_sql(
            text(
                "SELECT wine_id, producer_display, wine_display, vintage, "
                "       variety, country, region "
                "  FROM wines "
                " WHERE submitted_by_user_id = :u "
                " ORDER BY ctid DESC "
                " LIMIT 100"
            ),
            db.engine(),
            params={"u": user_id},
        ).to_dict("records")
    except Exception as e:  # noqa: BLE001 — see docstring
        # Column missing — migration hasn't applied yet. Fail open.
        if "submitted_by_user_id" in str(e):
            log.warning("submitted_by_user_id column not present; "
                        "skipping submitted-wines query")
            return []
        raise
