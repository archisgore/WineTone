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
from fastapi import FastAPI, Form, HTTPException, Request
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
            display_name = f"user_{clerk_uid[5:13].lower()}"
    user_id = reco.get_or_create_user_for_clerk(
        clerk_user_id=clerk_uid,
        display_name=display_name,
        email=email,
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


def _auth_context(request: Request) -> dict:
    """Inject signed-in user info into every render."""
    user = _resolve_user(request)
    return {
        "current_user": user,
        "auth_enabled": auth_clerk.is_enabled(),
        "clerk_publishable_key": os.environ.get("CLERK_PUBLISHABLE_KEY", ""),
        "clerk_frontend_api": auth_clerk.frontend_api_domain(),
        "clerk_sign_in_url": auth_clerk.sign_in_url(),
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
            response = await call_next(request)
            # HSTS — force HTTPS for a year on (sub)domains.
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
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
            # No browser features by default.
            response.headers.setdefault(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), payment=()",
            )
            # CSP — note the explicit Clerk frontend domain in
            # script-src / connect-src so the auth flow works. Without
            # that the sign-in modal is blank.
            clerk_domain = auth_clerk.frontend_api_domain()
            clerk_origins = (
                f"https://{clerk_domain} https://*.clerk.accounts.dev"
                if clerk_domain else "https://*.clerk.accounts.dev"
            )
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; "
                f"script-src 'self' 'unsafe-inline' 'unsafe-eval' "
                f"  https://unpkg.com {clerk_origins} "
                f"  https://static.cloudflareinsights.com; "
                f"style-src 'self' 'unsafe-inline'; "
                f"img-src 'self' data: https: blob:; "
                f"font-src 'self' data:; "
                f"connect-src 'self' {clerk_origins} "
                f"  https://huggingface.co https://*.huggingface.co "
                f"  https://cloudflareinsights.com; "
                f"frame-src {clerk_origins}; "
                f"frame-ancestors 'none';"
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
        return (
            "User-agent: *\n"
            "Allow: /\n"
            "Sitemap: https://tone.wine/sitemap.xml\n"
        )

    @app.get("/sitemap.xml", response_class=PlainTextResponse)
    def sitemap() -> PlainTextResponse:
        urls = ["/", "/ask", "/vocab"]
        body = '<?xml version="1.0" encoding="UTF-8"?>\n'
        body += '<urlset xmlns="http://www.sitemaps.org/schemas/0.9/sitemap-image/1.1">\n'
        for u in urls:
            body += f"  <url><loc>https://tone.wine{u}</loc></url>\n"
        body += "</urlset>\n"
        return PlainTextResponse(content=body, media_type="application/xml")

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> HTMLResponse:
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

            def _delete_user_sync() -> int:
                with db.connect() as conn:
                    result = conn.execute(
                        _text("DELETE FROM users WHERE clerk_user_id = :c"),
                        {"c": clerk_uid},
                    )
                return int(getattr(result, "rowcount", 0) or 0)

            rowcount = await run_in_threadpool(_delete_user_sync)
            log.info("user.deleted webhook: removed %s rows for clerk_id=%s",
                     rowcount, clerk_uid)
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
        where = "" if status == "all" else "WHERE r.status = :status"
        params = {} if status == "all" else {"status": status}
        with db.engine().connect() as conn:
            rows = conn.execute(
                _text(f"""
                    SELECT r.report_id, r.target_kind, r.target_id,
                           r.reason, r.note, r.status, r.created_at,
                           u.display_name AS reporter
                      FROM abuse_reports r
                      LEFT JOIN users u ON u.user_id = r.reporter_user_id
                      {where}
                     ORDER BY r.created_at DESC
                     LIMIT 200
                """),
                params,
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

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "privacy.html", {})

    @app.get("/terms", response_class=HTMLResponse)
    def terms_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "terms.html", {})

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
        description: str = Form(""),
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
                description=description, submitted_by=me["display_name"],
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

    # ---------------------------------------------------------------------

    @app.get("/me", response_class=HTMLResponse)
    def me_redirect(request: Request) -> RedirectResponse:
        """Canonical 'my dashboard' URL. Redirects to the user's named
        URL if signed in, or to the landing page (with prompt) if not."""
        me = _resolve_user(request)
        if me is None:
            return RedirectResponse(url="/", status_code=303)
        return RedirectResponse(url=f"/u/{me['display_name']}", status_code=303)

    @app.get("/u/{user}", response_class=HTMLResponse)
    def dashboard(request: Request, user: str) -> HTMLResponse:
        from winetone import social
        if not db.ping():
            raise HTTPException(503, "Database unreachable.")
        target_uid = reco.get_user_by_display_name(user)
        if target_uid is None:
            raise HTTPException(404, f"No such user: {user}")
        me = _resolve_user(request)
        is_self = me is not None and me["user_id"] == target_uid
        labels = _user_labels_rows(target_uid)
        projection = reco.load_projection(target_uid)
        fit_history = calibrate.history(target_uid)
        following = social.list_following(target_uid).to_dict("records")
        followers = social.list_followers(target_uid).to_dict("records")
        is_following_target = (
            me is not None and not is_self
            and social.is_following(me["user_id"], target_uid)
        )
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
            },
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
        _require_self(request, user)
        matches = reco.find_wine_by_text(q, limit=10)
        return TEMPLATES.TemplateResponse(
            request, "_search_results.html",
            {"user": user, "q": q, "matches": matches.to_dict("records")},
        )

    @app.post("/u/{user}/calibrate/add", response_class=HTMLResponse)
    @limiter.limit("60/hour")
    def calibrate_add(
        request: Request,
        user: str,
        wine_id: str = Form(...),
        description: str = Form(...),
        sentiment: str = Form("positive"),
    ) -> HTMLResponse:
        user_id = _require_self(request, user)
        reco.add_label(user_id, wine_id, description, sentiment=sentiment)
        labels = _user_labels_rows(user_id)
        return TEMPLATES.TemplateResponse(
            request, "_labels_list.html",
            {"user": user, "labels": labels, "labels_count": len(labels)},
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
            result["narration_html"] = _md.markdown(
                narration_md,
                extensions=["tables", "fenced_code", "nl2br"],
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
        return TEMPLATES.TemplateResponse(
            request, "_recommendations.html",
            {
                "user": user,
                "query": query,
                "generic": generic.to_dict("records"),
                "personalized": (
                    personalized.to_dict("records") if personalized is not None else None
                ),
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
