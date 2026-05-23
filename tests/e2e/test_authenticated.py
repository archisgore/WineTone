"""Authenticated end-to-end tests for WineTone.

These run against staging only and rely on a captured Clerk session
(see `auth_storage_state_path` in conftest). When the session JSON
isn't available, every test self-skips — so the suite stays green
on PRs from contributors who don't have the secret.

Tests own their own data: each one creates what it needs, asserts,
then cleans up. The dedicated `e2e-test` account on the dev Clerk
instance is the persona we drive. Some tests do leave a tiny amount
of permanent state (e.g. submitting a wine — those wines stay in
the staging catalog forever, which is fine because the staging DB
is a CoW snapshot of prod that gets refreshed periodically).
"""

from __future__ import annotations

import time

import pytest

# ─── Sanity: the captured session actually works ────────────────

def test_me_resolves_to_signed_in_dashboard(signed_in_page, app_url, e2e_username):
    """/me redirects to /u/<test-user> when signed in; lands at / when not.

    If this test fails, the captured storage_state is stale or
    invalid. Re-capture per docs/runbooks/e2e-testing.md before
    debugging anything else.
    """
    signed_in_page.goto(f"{app_url}/me")
    # Either we end up on the dashboard, or on the age-gate page if
    # the test account has never confirmed drinking age. Both are
    # signed-in outcomes — landing on "/" means the session expired.
    final_url = signed_in_page.url.rstrip("/")
    assert final_url != app_url, (
        f"/me redirected to landing — session expired? final_url = {final_url!r}"
    )
    assert (
        f"/u/{e2e_username}" in final_url
        or "/age-gate" in final_url
    ), f"unexpected /me destination: {final_url!r}"


def test_dashboard_shows_is_self_markers(signed_in_page, app_url, e2e_username):
    """The test account's own dashboard renders with owner-only controls.

    Asserts on a self-only marker that does NOT appear on the 401
    sign-in page (canonical URL injection would otherwise leak
    `/u/<name>` into the error page's body, giving false positives).
    The starter-style picker or the "Fit my taste profile" button
    are reliable signed-in-self markers.
    """
    response = signed_in_page.goto(f"{app_url}/u/{e2e_username}")
    assert response is not None
    assert response.status == 200, (
        f"GET /u/{e2e_username} returned {response.status} for the "
        "signed-in test user — should be 200 (dashboard)."
    )
    body = signed_in_page.content()
    # A self-viewing dashboard has at least one of these owner-only
    # markers. Match generously — the exact wording can change but
    # ANY of them being present is strong evidence of is_self.
    self_markers = [
        "Fit my taste profile",
        "Add a label",
        "starter wines",
        "Onboarding",
        "Delete my account",
    ]
    found = [m for m in self_markers if m.lower() in body.lower()]
    assert found, (
        "dashboard rendered without any owner-only markers — "
        f"none of {self_markers} were in the body."
    )


# ─── Label add → display → delete (round-trip) ──────────────────

def _first_wine_id_from_catalog(page, app_url) -> str:
    """Find a wine_id we can label safely.

    Drives the catalog page and returns the href of the first card —
    so the test works against whatever the catalog currently contains.
    """
    page.goto(f"{app_url}/catalog?q=barolo")
    href = page.locator(".catalog-card").first.get_attribute("href")
    assert href and href.startswith("/wines/"), f"unexpected href: {href!r}"
    return href.split("/")[-1]


def test_label_add_edit_delete_round_trip(signed_in_page, app_url, e2e_username):
    """Add a label inline on a wine page, edit it, then delete it.

    Asserts the editor cycles through its three states:
        anonymous → add-form → display+edit/delete → add-form (after delete)

    The test user must have confirmed drinking age (POST /age-gate
    once via the visible browser during capture) — the label POST
    returns 403 without it. If we detect the age-gate banner on the
    dashboard, skip with instructions rather than fail mysteriously.
    """
    page = signed_in_page
    # Detect age-gate state. The landing page renders an
    # `.age-gate-banner` warning iff the signed-in user hasn't
    # confirmed drinking age. Label POSTs hard-require it (403
    # otherwise), so skip cleanly with instructions.
    page.goto(f"{app_url}/")
    if "age-gate-banner" in page.content():
        pytest.skip(
            "e2e-test user has not confirmed drinking age. "
            "Sign in to staging.tone.wine as e2e-test, visit "
            "/age-gate, click 'Yes, I'm of legal age', then "
            "re-run scripts/capture_e2e_session.py and update "
            "the E2E_STAGING_AUTH_STATE secret."
        )
    # Auto-accept any confirm() dialog (the delete button has
    # hx-confirm; Playwright's default is to dismiss, which would
    # cancel the cleanup mid-test).
    page.on("dialog", lambda d: d.accept())

    wine_id = _first_wine_id_from_catalog(page, app_url)
    page.goto(f"{app_url}/wines/{wine_id}")

    stamp = int(time.time())
    description = f"e2e-test add {stamp}"

    # 1) Pre-state: editor should be in "add" mode (no existing label
    #    for this wine — unless a prior failed test left one behind).
    #    If a label is already there, delete it first so the test
    #    starts clean.
    if page.locator(".wine-label-editor .label-action-delete").count() > 0:
        page.locator(".wine-label-editor .label-action-delete").first.click()
        page.wait_for_selector(".wine-label-editor textarea[name='description']")

    # 2) Add a new label.
    page.locator(".wine-label-editor textarea[name='description']").fill(description)
    page.locator(".wine-label-editor button[type='submit']").click()

    # The HTMX response swaps the editor into "display" mode — i.e.
    # the Delete button appears (state 3). Both state-3-display and
    # the hidden state-3-edit form get rendered; we just verify the
    # state transition, not the description text (which can get
    # munged by HTML-escaping rules across renders and isn't the
    # property we're testing here).
    page.wait_for_selector(
        ".wine-label-editor #wine-my-label-display", timeout=10_000
    )

    # 3) Click Edit, change the text, submit. The visible state-3
    #    edit form replaces the display block via JS-toggled hidden.
    # The Edit button has class `.label-action-btn` only — the delete
    # button gets the extra `.label-action-delete`. Locate by role+text
    # to avoid coupling to which class differentiates them.
    page.get_by_role("button", name="Edit your label for this wine").click()
    edit_textarea = page.locator(
        ".wine-label-editor #wine-my-label-edit textarea[name='description']"
    )
    edit_textarea.wait_for(state="visible", timeout=5_000)
    edit_textarea.fill(f"e2e-test edit {stamp}")
    page.locator(
        ".wine-label-editor #wine-my-label-edit button[type='submit']"
    ).click()
    page.wait_for_selector(
        ".wine-label-editor #wine-my-label-display", timeout=10_000
    )

    # 4) Delete. Editor swaps back to state 2 (add form).
    page.locator(".wine-label-editor .label-action-delete").first.click()
    # State-2 add form has a visible textarea (no hidden parent).
    page.wait_for_selector(
        ".wine-label-editor form.wine-label-form textarea[name='description']:visible",
        timeout=10_000,
    )


# ─── Discover page renders for a signed-in user with a fit ──────

def test_discover_page_loads_for_signed_in_user(signed_in_page, app_url):
    """/discover is auth-gated; signed-in viewers get either the
    candidate grid or the 'no projection yet' empty state. Both
    are 200 with the page rendered — the route just shouldn't 401.
    """
    response = signed_in_page.goto(f"{app_url}/discover")
    assert response is not None
    assert response.status == 200, (
        f"GET /discover returned {response.status} for signed-in viewer"
    )
    body = signed_in_page.content()
    assert "Wines we think you'd love" in body


# ─── Recommend works for the signed-in user ─────────────────────

def test_recommend_returns_results(signed_in_page, app_url, e2e_username):
    """POST a query to the personalized recommend endpoint and
    verify it returns at least one card. Doesn't require a fitted
    projection — the route falls back to identity projection.
    """
    page = signed_in_page
    page.goto(f"{app_url}/u/{e2e_username}")
    # The dashboard's recommend form uses <input type="text" name="query">,
    # not a textarea. Form is gated on is_self (viewer == dashboard owner),
    # which is always true here since signed_in_page is the test account.
    query_input = page.locator('form[hx-post*="/recommend"] input[name="query"]')
    if query_input.count() == 0:
        pytest.skip(
            "Recommend form not on dashboard — unexpected for a signed-in "
            "self-viewer. Check is_self gating in dashboard.html."
        )
    query_input.first.fill("bold red wine with tobacco notes")
    # Track the HTMX response so a 4xx/5xx surfaces as a clear failure
    # rather than a generic timeout. The /u/<user>/recommend POST
    # carries the form submission; everything else is page assets.
    recommend_responses: list[int] = []

    def _on_resp(r):
        if "/recommend" in r.url and r.request.method == "POST":
            recommend_responses.append(r.status)

    page.on("response", _on_resp)
    page.locator('form[hx-post*="/recommend"] button[type="submit"]').click()
    # Cold-DB hybrid search can take 30–60s on staging Neon. Generous
    # timeout + fall through to a clearer error on non-200 response.
    try:
        page.wait_for_selector("#recommendations .reco-grid",
                               timeout=90_000)
    except Exception:
        if recommend_responses and recommend_responses[-1] >= 400:
            pytest.fail(
                f"POST /u/<user>/recommend returned "
                f"{recommend_responses[-1]} — HTMX did not swap a "
                "result grid because the server rejected the request. "
                "Check Sentry / staging logs."
            )
        raise


# ─── Vocab + ask still work when signed in ──────────────────────

def test_ask_endpoint_works_signed_in(signed_in_page, app_url):
    """/ask works for both anonymous and signed-in users.
    Verify the signed-in path doesn't accidentally 500."""
    signed_in_page.goto(f"{app_url}/ask?query=light+pinot")
    body = signed_in_page.content()
    # Either we get results or an empty state — never a 500 trace.
    assert "Traceback" not in body
    assert "Internal Server Error" not in body


# ─── Onboarding: GET works when signed in ───────────────────────

def test_onboarding_page_loads_when_signed_in(signed_in_page, app_url):
    """/onboarding is 401 for anonymous, 200 for signed-in.

    Don't actually pick a style — that would mutate the test
    account's onboarding state. Just verify the page renders.
    """
    response = signed_in_page.goto(f"{app_url}/onboarding")
    assert response is not None
    assert response.status == 200, (
        f"GET /onboarding returned {response.status} for signed-in viewer"
    )
