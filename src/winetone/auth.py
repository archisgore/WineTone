"""Sign-in with Hugging Face — OAuth flow + cookie-backed sessions.

This is opt-in: the deployed Space declares `hf_oauth: true` in its
README frontmatter, which makes HF inject `OAUTH_CLIENT_ID` and
`OAUTH_CLIENT_SECRET` env vars at runtime. If those aren't set we
quietly stay anonymous — the existing /pick-user path keeps working.

The auth dance:

  1. User clicks "Sign in" → /login generates a random state, sets it
     in a signed cookie, redirects to HF's authorize endpoint.
  2. HF redirects back to /login/callback?code=...&state=...
  3. We verify state, POST the code to /oauth/token, GET /oauth/userinfo
     with the access token.
  4. We write the resulting user identity (sub, preferred_username,
     name) into a signed session cookie. No tokens are kept on the
     server side — pure stateless.

Why itsdangerous-signed cookies rather than full SessionMiddleware:
we don't have state to carry beyond identity, the session is small,
and a single signed cookie keeps the deployment stateless.

What gets stored about a signed-in user:
  - hf_user_id    : stable identifier ("sub" from the OIDC token)
  - hf_username   : their HF handle (preferred_username)
  - hf_avatar_url : profile picture URL (cosmetic)

Nothing else. Tokens are discarded immediately after the userinfo call.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer

log = logging.getLogger(__name__)

HF_AUTHORIZE_URL = "https://huggingface.co/oauth/authorize"
HF_TOKEN_URL = "https://huggingface.co/oauth/token"
HF_USERINFO_URL = "https://huggingface.co/oauth/userinfo"

SESSION_COOKIE = "winetone_session"
STATE_COOKIE = "winetone_oauth_state"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _signer() -> URLSafeSerializer:
    """Lazy signer — secret is read once at first call."""
    secret = (
        os.environ.get("WINETONE_SESSION_SECRET")
        or os.environ.get("OAUTH_CLIENT_SECRET")  # fall back to HF-injected
        or "winetone-dev-secret-replace-me"
    )
    return URLSafeSerializer(secret, salt="winetone-session")


def is_enabled() -> bool:
    """True when the HF OAuth env vars look complete."""
    return bool(
        os.environ.get("OAUTH_CLIENT_ID")
        and os.environ.get("OAUTH_CLIENT_SECRET")
    )


def current_user(request: Request) -> dict[str, Any] | None:
    """Read + verify the session cookie. Returns None when unsigned in."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        return _signer().loads(raw)
    except BadSignature:
        log.info("invalid session cookie — treating as signed-out")
        return None


def _public_origin(request: Request) -> str:
    """The origin (scheme + host) the user is actually visiting.

    HF's reverse proxy preserves the original Host header behind a
    custom domain, so request.url.scheme/hostname normally Just Works.
    """
    return f"{request.url.scheme}://{request.url.netloc}"


def login(request: Request) -> RedirectResponse:
    """Redirect to HF's OAuth authorize endpoint."""
    if not is_enabled():
        raise HTTPException(503, "Sign-in is not configured on this Space.")
    state = secrets.token_urlsafe(24)
    redirect_uri = f"{_public_origin(request)}/login/callback"
    scopes = os.environ.get("OAUTH_SCOPES", "openid profile").replace(",", " ")
    params = httpx.QueryParams(
        {
            "response_type": "code",
            "client_id": os.environ["OAUTH_CLIENT_ID"],
            "scope": scopes,
            "state": state,
            "redirect_uri": redirect_uri,
        }
    )
    response = RedirectResponse(url=f"{HF_AUTHORIZE_URL}?{params}", status_code=302)
    signed_state = _signer().dumps({"state": state, "redirect_uri": redirect_uri})
    response.set_cookie(
        STATE_COOKIE, signed_state,
        max_age=600, httponly=True, samesite="lax", secure=True,
    )
    return response


def callback(request: Request, code: str, state: str) -> RedirectResponse:
    """Handle the redirect back from HF. Exchanges code → token →
    userinfo → sets the session cookie."""
    if not is_enabled():
        raise HTTPException(503, "Sign-in is not configured.")
    raw = request.cookies.get(STATE_COOKIE)
    if not raw:
        raise HTTPException(400, "Missing OAuth state cookie.")
    try:
        stored = _signer().loads(raw)
    except BadSignature:
        raise HTTPException(400, "Invalid OAuth state cookie.")
    if stored["state"] != state:
        raise HTTPException(400, "OAuth state mismatch.")

    with httpx.Client(timeout=15) as client:
        token_resp = client.post(
            HF_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": stored["redirect_uri"],
                "client_id": os.environ["OAUTH_CLIENT_ID"],
                "client_secret": os.environ["OAUTH_CLIENT_SECRET"],
            },
        )
        if token_resp.status_code != 200:
            log.warning("HF token exchange failed: %s %s",
                        token_resp.status_code, token_resp.text[:200])
            raise HTTPException(502, "Token exchange failed.")
        access_token = token_resp.json()["access_token"]

        info_resp = client.get(
            HF_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if info_resp.status_code != 200:
            raise HTTPException(502, "Could not fetch user info.")
        info = info_resp.json()

    session = {
        "hf_user_id": info.get("sub", ""),
        "hf_username": info.get("preferred_username", "") or info.get("name", ""),
        "hf_avatar_url": info.get("picture", ""),
    }
    if not session["hf_user_id"]:
        raise HTTPException(502, "HF didn't return a user id.")

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, _signer().dumps(session),
        max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", secure=True,
    )
    response.delete_cookie(STATE_COOKIE)
    return response


def logout() -> RedirectResponse:
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
