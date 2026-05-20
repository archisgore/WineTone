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
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from winetone import calibrate, db
from winetone import recommend as reco

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
# Web assets (HTML templates + CSS/JS/images) live at <repo-root>/www/.
# This separation keeps the deployable frontend cleanly carved out from
# the Python package — you can `rsync www/` to a CDN if we ever go SPA.
WWW = HERE.parent.parent.parent / "www"
TEMPLATES = Jinja2Templates(directory=str(WWW / "templates"))


def build_app() -> FastAPI:
    """Construct the FastAPI app. Factored so tests can build without
    starting uvicorn."""
    app = FastAPI(title="WineTone demo")
    app.mount("/static", StaticFiles(directory=str(WWW / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> HTMLResponse:
        backend = calibrate.detect_backend()
        return TEMPLATES.TemplateResponse(
            request, "index.html",
            {"backend": backend, "backend_label": calibrate.describe_backend(backend)},
        )

    @app.post("/pick-user")
    def pick_user(name: str = Form(...)) -> RedirectResponse:
        name = name.strip()
        if not name:
            return RedirectResponse(url="/", status_code=303)
        return RedirectResponse(url=f"/u/{name}", status_code=303)

    @app.get("/u/{user}", response_class=HTMLResponse)
    def dashboard(request: Request, user: str) -> HTMLResponse:
        if not db.ping():
            raise HTTPException(503, "CedarDB unreachable; run `make db-up-bg`")
        user_id = reco.get_or_create_user(user)
        labels = _user_labels_rows(user_id)
        projection = reco.load_projection(user_id)
        fit_history = calibrate.history(user_id)
        return TEMPLATES.TemplateResponse(
            request, "dashboard.html",
            {
                "user": user,
                "user_id": user_id,
                "labels": labels,
                "labels_count": len(labels),
                "is_fit": projection is not None,
                "fit_versions": len(fit_history),
                "backend": calibrate.detect_backend(),
            },
        )

    @app.post("/u/{user}/calibrate/search", response_class=HTMLResponse)
    def calibrate_search(request: Request, user: str, q: str = Form(...)) -> HTMLResponse:
        reco.get_or_create_user(user)  # ensure exists
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
        user_id = reco.get_or_create_user(user)
        reco.add_label(user_id, wine_id, description)
        labels = _user_labels_rows(user_id)
        return TEMPLATES.TemplateResponse(
            request, "_labels_list.html",
            {"user": user, "labels": labels, "labels_count": len(labels)},
        )

    @app.post("/u/{user}/calibrate/fit", response_class=HTMLResponse)
    def calibrate_fit_route(request: Request, user: str) -> HTMLResponse:
        user_id = reco.get_or_create_user(user)
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
        user_id = reco.get_or_create_user(scope_user) if scope_user else None
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
        user_id = reco.get_or_create_user(user)
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
