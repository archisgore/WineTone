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

from winetone import auth_clerk, calibrate, db
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
    return {
        "user_id": user_id,
        "clerk_user_id": clerk_uid,
        "display_name": display_name,
        "email": email,
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


def build_app() -> FastAPI:
    """Construct the FastAPI app. Factored so tests can build without
    starting uvicorn."""
    _init_sentry()
    app = FastAPI(title="WineTone demo")
    app.mount("/static", StaticFiles(directory=str(WWW / "static")), name="static")

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
            },
        )

    def _require_self(request: Request, user: str) -> str:
        """Helper: ensure the signed-in user matches the URL `user`.
        Returns their internal user_id. Raises 401/403 otherwise."""
        me = _resolve_user(request)
        if me is None:
            raise HTTPException(401, "Sign in to modify your profile.")
        if me["display_name"] != user:
            raise HTTPException(403, "You can only modify your own profile.")
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
    def calibrate_add(
        request: Request,
        user: str,
        wine_id: str = Form(...),
        description: str = Form(...),
    ) -> HTMLResponse:
        user_id = _require_self(request, user)
        reco.add_label(user_id, wine_id, description)
        labels = _user_labels_rows(user_id)
        return TEMPLATES.TemplateResponse(
            request, "_labels_list.html",
            {"user": user, "labels": labels, "labels_count": len(labels)},
        )

    @app.post("/u/{user}/calibrate/fit", response_class=HTMLResponse)
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
    def ask_query(
        request: Request,
        query: str = Form(...),
    ) -> HTMLResponse:
        from sqlalchemy import text as _text

        import markdown as _md
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
