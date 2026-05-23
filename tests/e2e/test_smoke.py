"""Anonymous-viewer smoke tests for WineTone.

What this proves:
- Every page meant to be public actually loads for an anonymous viewer.
- Auth-required endpoints reject anonymous traffic cleanly (401, not 500).
- The active-tab visual marker lands on the right nav link.
- The catalog's full-text search returns relevance-ranked results.
- The /vocab and /users pages render without server-side errors.
- /wines/{id} and /u/{user}/palate render for at least one real entity
  from the corpus.

What this does NOT prove:
- Anything behind Clerk auth (label adds, calibration, recommend
  personalization, /onboarding submit). Those need an authenticated
  session in CI — Playwright's storage_state load plus a Clerk dev-
  instance test account. Tracked separately.
"""

from __future__ import annotations

import httpx
import pytest

PUBLIC_ROUTES = [
    "/",
    "/ask",
    "/catalog",
    "/vocab",
    "/wines/new",
    "/wines/scan",
    "/privacy",
    "/terms",
]

# Social-graph routes that used to be public and now require sign-in
# (2026-05-23: usernames are no longer exposed to anonymous viewers).
# Each must return 401 to anonymous GETs.
AUTH_REQUIRED_GETS = [
    ("/users", 401),
    ("/u/archisgore", 401),
    ("/u/archisgore/palate", 401),
    ("/discover", 401),
]

AUTH_REQUIRED_POSTS = [
    # path, form-encoded payload, expected status code
    ("/wines/new", {"producer": "x"}, 401),
    ("/u/archisgore/calibrate/search", {"q": "barolo"}, 401),
    ("/u/archisgore/calibrate/add",
     {"wine_id": "x", "description": "x"}, 401),
    ("/u/archisgore/calibrate/fit", {}, 401),
    ("/u/archisgore/recommend", {"query": "x"}, 401),
    ("/onboarding", {"style": "old_world"}, 401),
]

# Nav links visible to anonymous viewers. People is signed-in-only
# now, so it's not in this list — see test_people_nav_hidden_anon.
NAV_LINKS = [
    ("/wines/scan", "Scan"),
    ("/ask", "Ask"),
    ("/catalog", "Catalog"),
    ("/vocab", "Vocabulary"),
]


# ─── Public-route HTTP-level smoke checks ────────────────────────

@pytest.mark.parametrize("path", PUBLIC_ROUTES)
def test_public_route_returns_200(app_url, path):
    """Every page advertised as 'public' renders for an anonymous visitor."""
    r = httpx.get(f"{app_url}{path}", timeout=30)
    assert r.status_code == 200, (
        f"GET {path} returned {r.status_code} — expected 200. "
        f"Response head: {r.text[:200]!r}"
    )
    assert "<html" in r.text.lower()


@pytest.mark.parametrize("path,payload,expected", AUTH_REQUIRED_POSTS)
def test_auth_required_endpoint_returns_401(app_url, path, payload, expected):
    """An anonymous POST to an auth-gated endpoint must return 401, not 500.

    Catching a 500 here means the route blew up before reaching its
    auth guard — almost always a bug worth investigating.
    """
    r = httpx.post(f"{app_url}{path}", data=payload, timeout=30,
                   follow_redirects=False)
    assert r.status_code == expected, (
        f"POST {path} returned {r.status_code} — expected {expected}. "
        f"Response head: {r.text[:200]!r}"
    )


@pytest.mark.parametrize("path,expected", AUTH_REQUIRED_GETS)
def test_social_graph_route_requires_signin(app_url, path, expected):
    """Social-graph GETs return 401 for anonymous viewers.

    These pages reveal usernames; per the 2026-05-23 privacy policy
    change, that is no longer exposed to anonymous traffic. A 200
    here means the gate was accidentally removed.
    """
    r = httpx.get(f"{app_url}{path}", timeout=30, follow_redirects=False)
    assert r.status_code == expected, (
        f"GET {path} returned {r.status_code} — expected {expected}. "
        f"Response head: {r.text[:200]!r}"
    )


# ─── /healthz ────────────────────────────────────────────────────

def test_healthz_returns_ok(app_url):
    """/healthz returns JSON status=ok when the DB and encoder are up."""
    r = httpx.get(f"{app_url}/healthz", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") == "ok", f"Got {body!r}"
    assert body.get("db") == "ok"
    assert body.get("encoder_loaded") in ("yes", "lazy")


def test_healthz_503s_on_db_failure_design(app_url):
    """We can't actually take the DB down in a test, but we can
    verify the schema /healthz uses so future code doesn't silently
    drop the 'db' field. UptimeRobot keys off this."""
    body = httpx.get(f"{app_url}/healthz", timeout=15).json()
    assert "db" in body
    assert "db_latency_ms" in body
    assert "clerk_frontend" in body
    assert "encoder_loaded" in body


# ─── Page-content smoke checks (Playwright) ──────────────────────

@pytest.mark.parametrize("path,label", NAV_LINKS)
def test_active_tab_highlight(page, app_url, path, label):
    """Navigating to a route highlights its nav tab with .is-active."""
    page.goto(f"{app_url}{path}")
    active = page.locator(".site-nav > a.is-active").first
    assert active.is_visible(), f"No active nav link on {path}"
    text = active.text_content().strip()
    assert label in text, (
        f"Active nav link on {path} says {text!r}, expected to contain {label!r}"
    )


def test_catalog_renders_wine_cards(page, app_url):
    page.goto(f"{app_url}/catalog")
    cards = page.locator(".catalog-card")
    assert cards.count() > 0, "No catalog-card elements rendered on /catalog"


def test_catalog_filter_form_works(page, app_url):
    """Submit the country filter and verify the URL reflects it."""
    page.goto(f"{app_url}/catalog")
    # Use the country dropdown — pick "Italy" if available
    select = page.locator('select[name="country"]')
    options = select.locator("option").all_text_contents()
    italy = next((o for o in options if "Italy" in o), None)
    if italy is None:
        pytest.skip("Italy not in the country dropdown on this env")
    select.select_option(label=italy)
    page.click('button[type="submit"]')
    # URL should now carry ?country=Italy
    assert "country=" in page.url
    cards = page.locator(".catalog-card")
    assert cards.count() > 0, "Filtered catalog returned no cards"


def test_catalog_freetext_search_returns_results(page, app_url):
    """The new FTS search input returns relevance-ranked cards.

    Filling the input and clicking submit triggers a regular GET
    navigation — Playwright doesn't await that by default, so we
    use page.goto directly with the search query in the URL. Same
    effect, but no race between content() and the form post.
    """
    page.goto(f"{app_url}/catalog?q=barolo")
    body = page.content()
    assert "ordered by relevance" in body, (
        f"Search page did not render the relevance banner. "
        f"First 300 chars of body: {body[:300]!r}"
    )
    cards = page.locator(".catalog-card")
    assert cards.count() > 0, "Search returned no cards"


def test_vocab_search_renders_input(page, app_url):
    page.goto(f"{app_url}/vocab")
    inp = page.locator('input[name="query"]').first
    assert inp.is_visible()


def test_people_nav_hidden_for_anonymous(page, app_url):
    """The People nav link is signed-in-only as of 2026-05-23.

    Anonymous viewers should never see it in the site header — the
    directory is gated behind sign-in along with all profile pages.
    """
    page.goto(f"{app_url}/")
    people_links = page.locator('nav.site-nav a[href="/users"]')
    assert people_links.count() == 0, (
        "People nav link appeared in the header for an anonymous viewer "
        "— the social-graph routes are supposed to be signed-in-only."
    )


def test_wine_detail_page_renders(page, app_url):
    """Click a wine on /catalog and verify the detail page loads."""
    page.goto(f"{app_url}/catalog")
    first_card = page.locator(".catalog-card").first
    href = first_card.get_attribute("href")
    assert href and href.startswith("/wines/"), f"Bad href: {href}"
    page.goto(f"{app_url}{href}")
    # Detail page has a back-link to /catalog and the producer in an h2
    assert page.locator("h2").count() > 0
    assert "Catalog" in page.content()  # back-link present


def test_wine_detail_hides_label_authors_for_anon(page, app_url):
    """On a wine page with at least one user label, anonymous viewers
    see a 'sign in to see author' placeholder, not a real @username.

    Picks the first wine on the catalog that has user labels — most
    don't, so we have to find one. If none exist on this env, skip.
    """
    page.goto(f"{app_url}/catalog?sort=popular")
    # Walk cards until one has the "N user labels" stat (means it
    # has at least one label rendered on the detail page).
    cards = page.locator(".catalog-card").all()
    target_href = None
    for card in cards:
        if "user label" in (card.text_content() or "").lower():
            target_href = card.get_attribute("href")
            break
    if target_href is None:
        pytest.skip("no catalog card with user labels visible on this env")
    page.goto(f"{app_url}{target_href}")
    body = page.content()
    # Anonymous viewer sees the placeholder, not raw @usernames.
    assert "sign in to see author" in body.lower(), (
        f"Expected anonymous placeholder on {target_href}, didn't find it. "
        "Either the gate regressed or the label is missing the placeholder."
    )


def test_scanner_page_renders(page, app_url):
    """/wines/scan renders the dropzone (or the 'not configured' message)."""
    page.goto(f"{app_url}/wines/scan")
    # Either the dropzone or the disabled-state warning should be visible
    body = page.content()
    assert ("Take a photo of the label" in body
            or "Scanner not configured" in body)


def test_pwa_manifest_serves(app_url):
    """The PWA manifest is reachable + parses as JSON with required fields."""
    r = httpx.get(f"{app_url}/static/manifest.json", timeout=15)
    assert r.status_code == 200
    m = r.json()
    for key in ("name", "short_name", "start_url", "display", "icons"):
        assert key in m, f"manifest.json missing required key: {key}"
    assert m["display"] == "standalone"


def test_service_worker_serves(app_url):
    r = httpx.get(f"{app_url}/static/service-worker.js", timeout=15)
    assert r.status_code == 200
    assert "addEventListener" in r.text


# ─── Security-headers smoke checks ───────────────────────────────

def test_security_headers_present(app_url):
    r = httpx.head(f"{app_url}/", timeout=15)
    h = r.headers
    assert h.get("strict-transport-security"), "missing HSTS"
    assert h.get("x-frame-options") == "DENY"
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("referrer-policy")
    assert h.get("content-security-policy")


# ─── Sitemap + robots ────────────────────────────────────────────

def test_robots_txt(app_url):
    r = httpx.get(f"{app_url}/robots.txt", timeout=15)
    assert r.status_code == 200
    assert "Sitemap:" in r.text


def test_sitemap_xml(app_url):
    r = httpx.get(f"{app_url}/sitemap.xml", timeout=15)
    assert r.status_code == 200
    assert "<urlset" in r.text


# ─── Webhook signature gating ────────────────────────────────────

def test_webhook_rejects_unsigned(app_url):
    """POST to /webhooks/clerk without a valid Svix signature returns
    400 (not 200) — confirms signature verification is on."""
    r = httpx.post(f"{app_url}/webhooks/clerk",
                   json={"type": "user.deleted", "data": {"id": "x"}},
                   timeout=15)
    assert r.status_code in (400, 503), (
        f"Webhook unsigned POST returned {r.status_code}; "
        "expected 400 (sig rejected) or 503 (no secret)"
    )


# ─── Admin gating ────────────────────────────────────────────────

def test_admin_reports_404_anonymous(app_url):
    """/admin/reports returns 404 (not 403) to non-admins, so the
    route's existence doesn't leak."""
    r = httpx.get(f"{app_url}/admin/reports", timeout=15,
                  follow_redirects=False)
    assert r.status_code == 404, (
        f"GET /admin/reports anonymous returned {r.status_code}; "
        "expected 404 (gating leak)"
    )


# ─── HTTP → HTTPS upgrade ─────────────────────────────────────────

def test_http_redirects_to_https(app_url):
    """Plain HTTP requests must redirect to HTTPS (3xx).

    A regression here means HSTS is the only thing keeping returning
    visitors on HTTPS — and HSTS doesn't help a first-time visitor.
    Skip if the target URL is already plain HTTP (e.g. a local-dev
    run against http://localhost:7860 has no TLS to enforce).
    """
    if not app_url.startswith("https://"):
        pytest.skip(f"target is not HTTPS ({app_url}); skipping redirect test")
    plain_url = "http://" + app_url[len("https://"):]
    r = httpx.get(plain_url, timeout=20, follow_redirects=False)
    assert r.status_code in (301, 302, 307, 308), (
        f"GET {plain_url} returned {r.status_code} — expected a 3xx "
        f"redirect to HTTPS. Response head: {r.text[:200]!r}"
    )
    location = r.headers.get("location", "")
    assert location.startswith("https://"), (
        f"Redirect location {location!r} doesn't go to HTTPS"
    )
