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

    Clerk's `__session` JWT has a 60-second expiry. The captured
    state's JWT is long-expired by CI time; clerk-js mints a fresh
    one once it boots, but only writes the freshly-issued JWT to the
    instance-suffixed `__session_<suffix>` cookie. The server reads
    either the legacy or the suffixed cookie (see auth_clerk.py).

    The warm-up loads the landing page so clerk-js initializes,
    then forces a fresh-mint via `getToken({skipCache: true})` and
    verifies by hitting /me. If /me redirects to /, the captured
    state is broken — re-run scripts/capture_e2e_session.py.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    page = signed_in_context.new_page()
    page.goto(f"{app_url}/")
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
            "clerk-js did not appear within 15s. Re-run "
            "scripts/capture_e2e_session.py to refresh the auth state."
        )

    # Verify the session works end-to-end by hitting /me.
    page.goto(f"{app_url}/me")
    final_url = page.url.rstrip("/")
    if final_url in (app_url, app_url + "/"):
        pytest.fail(
            f"Warm-up minted a Clerk token but /me still redirects to "
            f"landing ({final_url}). Captured client-uat cookie may be "
            "revoked. Re-run scripts/capture_e2e_session.py."
        )

    # Diagnostic: snapshot the cookie state RIGHT BEFORE yielding.
    # The test will compare against the cookies it sees and we can
    # tell whether they're drifting in the brief moment between
    # yield and the first test action.
    page._winetone_warmup_cookies = {  # type: ignore[attr-defined]
        c["name"]: c.get("value", "")[:30]
        for c in signed_in_context.cookies()
        if c["name"].startswith("__session") or c["name"].startswith("__client")
    }
    page._winetone_warmup_me_url = final_url  # type: ignore[attr-defined]

    try:
        yield page
    finally:
        page.close()


@pytest.fixture
def e2e_username() -> str:
    """Display name of the dedicated test account."""
    return E2E_TEST_USERNAME
