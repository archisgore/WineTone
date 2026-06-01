"""Clerk Backend API helper for E2E authentication.

Replaces the old "capture cookies in a browser, replay in CI" path
(which broke on Clerk's device-binding / client-uat behavior).

The new model:
    pytest session start  → mint a sign-in token via Clerk Backend API
                           → returns a single-use ticket URL
    each authenticated test  → Playwright navigates to the ticket URL
                              → Clerk redeems, creates a fresh session
                              → no captured state, no replay window

Requires two GHA environment / `production` env-scoped secrets:

    CLERK_SECRET_KEY    — the dev Clerk instance's `sk_test_…` key
    CLERK_E2E_USER_ID   — the Clerk user_id (`user_…`) of the
                          dedicated `e2e-test` account on the dev
                          instance. Look it up once in the Clerk
                          dashboard, then pin as a secret.

Docs: https://clerk.com/docs/reference/backend-api/tag/Sign-in-Tokens
"""
from __future__ import annotations

import os

import httpx
import pytest

# Clerk's Backend API base. Same endpoint for prod and dev instances —
# the secret key authenticates against whichever instance owns it.
CLERK_API_BASE = "https://api.clerk.com/v1"

# How long the minted ticket is valid for. Generous — gives the test
# session up to 10 minutes to redeem. Tickets are single-use anyway,
# so a long TTL doesn't widen any attack surface.
TICKET_TTL_SECONDS = 600


def require_env(*names: str) -> dict[str, str]:
    """Pull required env vars or skip the calling test cleanly.

    Forks of this repo (or local runs without the secrets) shouldn't
    fail hard on missing Clerk credentials — they skip the
    authenticated tests and let anonymous coverage carry on.
    """
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(
            "Authenticated e2e tests require: " + ", ".join(missing)
            + ". See docs/runbooks/e2e-testing.md to configure them."
        )
    return {n: os.environ[n] for n in names}


def mint_sign_in_token(clerk_user_id: str, clerk_secret_key: str) -> str:
    """Create a single-use sign-in ticket for the given Clerk user.

    Returns the **raw token string** (the `token` field on the
    sign-in-token resource), suitable for redemption via Clerk JS:

        await Clerk.client.signIn.create({
            strategy: "ticket",
            ticket: <token>,
        });

    NOT the `url` field — that one is the user-facing magic-link
    URL whose post-redeem destination is controlled by the Clerk
    dashboard's "default redirect URL", which on Account Portal
    instances falls back to `/default-redirect` and never reaches
    the app. JS-based redemption bypasses that whole flow.
    """
    r = httpx.post(
        f"{CLERK_API_BASE}/sign_in_tokens",
        json={
            "user_id": clerk_user_id,
            "expires_in_seconds": TICKET_TTL_SECONDS,
        },
        headers={
            "Authorization": f"Bearer {clerk_secret_key}",
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"Clerk Backend API rejected sign-in-token request "
            f"({r.status_code}): {r.text[:400]}"
        )
    body = r.json()
    token = body.get("token")
    if not token:
        raise RuntimeError(
            f"Clerk sign_in_tokens response missing `token` field: "
            f"{body!r}"
        )
    return token
