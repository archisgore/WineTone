"""End-to-end test fixtures.

`app_url` is the URL to test against. CI sets it via WINETONE_E2E_URL
in the workflow; local runs default to the production site. Pass
`--target=https://staging.tone.wine` on the command line to override.

Anonymous tests use the default Playwright `page` fixture (no cookies).
Authenticated tests use `signed_in_page`, which mints a fresh sign-in
ticket via Clerk's Backend API at the start of the pytest session and
redeems it in a clean Playwright context per test. No captured cookies,
no replay-window staleness — see docs/runbooks/e2e-testing.md.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

from ._clerk_helpers import mint_sign_in_token, require_env

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page


# Display name of the dedicated e2e test account on the Clerk dev
# instance. The sign-in tokens we mint below target this account.
# It exists only on staging — the dev Clerk secret key won't mint
# tokens for prod identities, so authenticated tests self-skip
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
def _clerk_e2e_creds(app_url) -> tuple[str, str]:
    """Resolve `CLERK_E2E_USER_ID` + `CLERK_SECRET_KEY` once per session.

    Self-skips authenticated tests under any of these conditions:
      - the target isn't staging (the dev Clerk secret won't mint
        tokens for the prod instance)
      - either env var is missing (a fork without the secrets)

    Also does one sentinel mint at session start so a wrong key or
    bad user_id fails the entire authenticated suite once with a
    clear infrastructure error, instead of seven copies of the same
    error one per test.
    """
    if "staging.tone.wine" not in app_url:
        pytest.skip(
            f"Authenticated tests only run against staging "
            f"(target = {app_url}). The dev Clerk secret can't mint "
            f"tokens for the prod instance."
        )
    env = require_env("CLERK_SECRET_KEY", "CLERK_E2E_USER_ID")
    user_id = env["CLERK_E2E_USER_ID"]
    secret_key = env["CLERK_SECRET_KEY"]
    # Sentinel mint — verifies the endpoint, the secret, and the
    # user_id are all valid. Cheap (~200ms) and the resulting token
    # we just throw away.
    try:
        mint_sign_in_token(user_id, secret_key)
    except Exception as e:  # noqa: BLE001
        pytest.fail(
            f"Cannot mint Clerk sign-in tokens — authenticated tests "
            f"can't run. Underlying error: {e}"
        )
    return user_id, secret_key


# JS snippet that redeems a Clerk sign-in token in the page's Clerk
# client, then activates the resulting session. Replaces the older
# URL-based redirect flow which depended on Clerk dashboard's default
# redirect URL setting (Account Portal instances fall back to
# `/default-redirect` which never reaches the app). This pattern
# mirrors what @clerk/testing does internally.
_REDEEM_TICKET_JS = """
async (ticket) => {
    await window.Clerk.load();
    const signIn = await window.Clerk.client.signIn.create({
        strategy: "ticket",
        ticket: ticket,
    });
    if (signIn.status !== "complete") {
        throw new Error("signIn.status=" + signIn.status);
    }
    await window.Clerk.setActive({ session: signIn.createdSessionId });
    return signIn.createdSessionId;
}
"""


@pytest.fixture
def signed_in_context(
    browser, browser_context_args, app_url, _clerk_e2e_creds,
) -> Iterator[BrowserContext]:
    """A clean Playwright context with a freshly-minted Clerk session.

    Per-test: mint a new sign-in token via Clerk Backend API, then
    redeem in-browser via the Clerk JS SDK
    (`Clerk.client.signIn.create({strategy: "ticket", ticket})`).
    After `Clerk.setActive`, the session cookies are written to the
    Playwright context and subsequent navigations carry the
    signed-in identity.
    """
    user_id, secret_key = _clerk_e2e_creds
    token = mint_sign_in_token(user_id, secret_key)

    ctx = browser.new_context(**browser_context_args)
    page = ctx.new_page()
    page.goto(f"{app_url}/")  # any app page works — we just need Clerk JS
    page.wait_for_function(
        "() => typeof window.Clerk !== 'undefined'",
        timeout=15_000,
    )
    try:
        session_id = page.evaluate(_REDEEM_TICKET_JS, token)
    except Exception as e:  # noqa: BLE001
        pytest.fail(
            f"JS-based ticket redemption failed inside the browser: {e}. "
            f"Token may be invalid or Clerk JS may not be wired up "
            f"correctly on the staging app."
        )
    if not session_id:
        pytest.fail(
            "Clerk.setActive succeeded but createdSessionId was empty. "
            "Check the dev Clerk instance's sign-in factor settings."
        )
    page.close()  # signed_in_page makes its own page off the same ctx
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def signed_in_page(signed_in_context, app_url) -> Iterator[Page]:
    """A Page from the signed-in context — verified live before yield.

    By the time we land here, the ticket has been redeemed and the
    Clerk session is established in the context's cookies. We verify
    by hitting /me: the server should redirect to /u/<display_name>.
    """
    page = signed_in_context.new_page()
    page.goto(f"{app_url}/me")
    final_url = page.url.rstrip("/")
    if final_url in (app_url, app_url + "/"):
        pytest.fail(
            f"Sign-in ticket was redeemed but /me redirects to landing "
            f"({final_url}). The session may have expired during test "
            f"setup — check Clerk session-token TTL on the dev instance."
        )
    try:
        yield page
    finally:
        page.close()


@pytest.fixture
def e2e_username() -> str:
    """Display name of the dedicated test account."""
    return E2E_TEST_USERNAME
