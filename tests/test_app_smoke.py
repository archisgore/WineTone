"""End-to-end smoke tests for the FastAPI app.

Uses TestClient so we don't need uvicorn + a port. Most tests just
verify that routes exist and return reasonable status codes; auth-
gated routes should return 401 when called anonymously, public
routes 200, missing routes 404.

We don't run the actual recommend/calibrate pipeline here — that
needs a populated Neon. These tests focus on the routing layer.
"""

from __future__ import annotations

import os

import pytest

# Skip everything in this module if we can't reach a DB — most CI
# runners won't have one configured.
pytestmark = pytest.mark.skipif(
    not (os.environ.get("WINETONE_DB_URL") or os.environ.get("DATABASE_URL")),
    reason="needs a database URL via WINETONE_DB_URL or DATABASE_URL",
)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from winetone.web.app import build_app
    return TestClient(build_app())


def test_landing_200(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "WineTone" in r.text


def test_privacy_page_200(client):
    r = client.get("/privacy")
    assert r.status_code == 200
    assert "right to be deleted" in r.text.lower() or "delete" in r.text.lower()


def test_terms_page_200(client):
    r = client.get("/terms")
    assert r.status_code == 200
    assert "Terms" in r.text


def test_healthz_returns_json(client):
    r = client.get("/healthz")
    assert r.status_code in (200, 503)
    data = r.json()
    assert "status" in data
    assert "db" in data


def test_robots_txt(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "User-agent" in r.text


def test_sitemap_xml(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "<urlset" in r.text


def test_favicon(client):
    r = client.get("/static/favicon.svg")
    assert r.status_code == 200


def test_404_returns_html_page(client):
    r = client.get("/nonexistent-page-please")
    assert r.status_code == 404
    # Our custom 404 page, not bare JSON.
    assert "Page not found" in r.text


def test_unauthenticated_follow_returns_401(client):
    r = client.post("/u/anyone/follow")
    assert r.status_code == 401


def test_unauthenticated_calibrate_add_returns_401(client):
    r = client.post(
        "/u/anyone/calibrate/add",
        data={"wine_id": "x", "description": "test"},
    )
    assert r.status_code == 401


def test_unauthenticated_wine_submit_returns_401(client):
    r = client.post("/wines/new", data={"producer": "Test"})
    assert r.status_code == 401


def test_webhook_without_secret_returns_503(client):
    # CLERK_WEBHOOK_SECRET isn't set in the test env — fail-closed.
    r = client.post("/webhooks/clerk", data="{}",
                    headers={"content-type": "application/json"})
    assert r.status_code == 503


def test_security_headers_present(client):
    r = client.get("/")
    assert "strict-transport-security" in r.headers
    assert r.headers.get("x-frame-options") == "DENY"
    assert "content-security-policy" in r.headers
    # Our request_id middleware echoes this back.
    assert "x-request-id" in r.headers


def test_admin_reports_404_without_env(client):
    # No ADMIN_CLERK_USER_ID set in test env → 404, never 403 (so
    # the existence of the route doesn't leak to non-admins).
    r = client.get("/admin/reports")
    assert r.status_code == 404
