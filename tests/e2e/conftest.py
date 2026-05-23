"""End-to-end test fixtures.

`app_url` is the URL to test against. CI sets it via WINETONE_E2E_URL
in the workflow; local runs default to the production site. Pass
`--target=https://staging.tone.wine` on the command line to override.

Anonymous tests use the default Playwright `page` fixture (no cookies).
Authenticated tests use `signed_in_page`, which loads a captured Clerk
session from the `E2E_STAGING_AUTH_STATE` env var — a JSON blob stored
as a GitHub Actions secret. See docs/runbooks/e2e-testing.md for the
capture procedure.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page


# Display name of the dedicated e2e test account on the Clerk dev
# instance. The captured session below belongs to this account.
# It exists only on staging — its cookie would not validate on
# prod (different Clerk instance), so authenticated tests self-skip
# unless --target points at staging.
E2E_TEST_USERNAME = "e2e-test"


def pytest_addoption(parser):
    parser.addoption(
        "--target",
        action="store",
        default=None,
        help="Base URL to test against. Falls back to WINETONE_E2E_URL "
             "env var, then to https://tone.wine.",
    )


@pytest.fixture(scope="session")
def app_url(pytestconfig) -> str:
    """Resolve the target URL — CLI flag wins, then env var, then prod."""
    url = (
        pytestconfig.getoption("--target")
        or os.environ.get("WINETONE_E2E_URL")
        or "https://tone.wine"
    )
    return url.rstrip("/")


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args, app_url):
    """Default browser context — anonymous, with target as base_url."""
    return {**browser_context_args, "base_url": app_url}


@pytest.fixture(scope="session")
def auth_storage_state_path(tmp_path_factory) -> str | None:
    """Materialize the captured Clerk session JSON to a temp file.

    Returns the file path, or None if no auth state is available.
    Tests that need a signed-in browser self-skip when this is None.
    """
    blob = os.environ.get("E2E_STAGING_AUTH_STATE", "").strip()
    if not blob:
        return None
    try:
        json.loads(blob)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"E2E_STAGING_AUTH_STATE is not valid JSON ({e}). "
            "Re-capture per docs/runbooks/e2e-testing.md."
        )
    f = tmp_path_factory.mktemp("auth") / "storage_state.json"
    f.write_text(blob)
    return str(f)


@pytest.fixture
def signed_in_context(
    browser, browser_context_args, app_url, auth_storage_state_path
) -> Iterator[BrowserContext]:
    """A fresh browser context with the captured Clerk session loaded.

    Self-skips if no auth state is configured, or if target isn't
    staging — the captured session is staging-only.
    """
    if auth_storage_state_path is None:
        pytest.skip(
            "No E2E_STAGING_AUTH_STATE configured — see "
            "docs/runbooks/e2e-testing.md for capture steps."
        )
    if "staging.tone.wine" not in app_url:
        pytest.skip(
            f"Authenticated tests only run against staging "
            f"(target = {app_url}). Captured sessions are staging-only."
        )
    ctx = browser.new_context(
        **browser_context_args,
        storage_state=auth_storage_state_path,
    )
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def signed_in_page(signed_in_context, app_url) -> Iterator[Page]:
    """A Page from the signed-in context — with Clerk warmed up.

    Clerk's `__session` cookie carries a JWT with a 60-second expiry.
    The captured auth.json was written hours or days ago, so by CI
    time that JWT is long-dead. The longer-lived `__client_uat`
    cookie in storage_state lets clerk-js (the in-browser SDK)
    mint a fresh session token — but only AFTER it loads.

    So before yielding, we navigate to the landing page and wait
    for clerk-js to initialize and surface a session. After that
    completes, the __session cookie is fresh and server-side
    auth checks succeed.

    If clerk-js doesn't surface a session within 15s, the captured
    auth state is either expired (re-capture per docs) or was
    anonymous when captured — we fail loudly so the diagnosis is
    immediate.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    page = signed_in_context.new_page()
    page.goto(f"{app_url}/")
    # Wait for clerk-js, then force a fresh __session JWT to be
    # minted by calling getToken({skipCache: true}). This is the
    # only way to update the server-readable cookie when the
    # captured JWT (60s validity) is long expired.
    try:
        page.wait_for_function(
            "() => typeof window.Clerk !== 'undefined'",
            timeout=15_000,
        )
        token = page.evaluate(
            """async () => {
                await window.Clerk.load();
                if (!window.Clerk.session) return null;
                return await window.Clerk.session.getToken({ skipCache: true });
            }"""
        )
        if not token:
            pytest.fail(
                "Clerk.load() resolved but there is no active session. "
                "Captured auth state was likely anonymous — re-run "
                "scripts/capture_e2e_session.py."
            )
    except PlaywrightTimeout:
        pytest.fail(
            "clerk-js did not appear within 15s of loading /. Re-run "
            "scripts/capture_e2e_session.py to refresh the auth state."
        )

    # End-to-end verification of the warm-up: hit /me and confirm it
    # redirects to a signed-in destination. If clerk-js minted a fresh
    # JWT but the cookie didn't actually propagate to the server-visible
    # cookie jar, this catches it before any downstream test runs.
    page.goto(f"{app_url}/me")
    final_url = page.url.rstrip("/")
    if final_url in (app_url, app_url + "/"):
        # /me redirected to / — server saw no valid session.
        # Dump some diagnostics so we can see what's actually in the jar.
        cookies = signed_in_context.cookies()
        cookie_summary = ", ".join(
            f"{c['name']}@{c['domain']}"
            for c in cookies
            if c["name"] in {"__session", "__client", "__client_uat"}
        )
        pytest.fail(
            f"Warm-up minted a Clerk token but /me still redirects to landing.\n"
            f"  final_url={final_url}\n"
            f"  Clerk cookies in jar: {cookie_summary or '(none)'}\n"
            f"  Token returned by getToken: {bool(token)} "
            f"({(token[:20] + '...') if token else 'null'})\n"
            "This usually means the captured __client_uat cookie is "
            "stale (Clerk revoked the client). Re-run "
            "scripts/capture_e2e_session.py."
        )
    try:
        yield page
    finally:
        page.close()


@pytest.fixture
def e2e_username() -> str:
    """Display name of the dedicated test account."""
    return E2E_TEST_USERNAME
