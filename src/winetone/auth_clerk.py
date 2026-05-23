"""Clerk authentication for WineTone.

Clerk sets a `__session` cookie on the application domain containing a
short-lived JWT (1-minute lifetime, refreshed by Clerk's JS bundle on
the frontend). We validate that JWT on every request against Clerk's
JWKS endpoint and surface the identified user as request state.

Why we don't use Clerk's Python SDK:
- The SDK is ~50 deps deep and most of it is admin-API surface we
  don't need.
- For JWT validation we only need `pyjwt[crypto]`, a JWKS fetch, and
  a tiny TTL cache. Direct, auditable, fast.

The frontend (HTML + Clerk JS bundle) handles the entire sign-in dance
client-side. The cookie they set is HttpOnly false (Clerk needs to read
and refresh it from JS), so we trust the signature, not the cookie
storage. The JWT carries everything we need: sub, username, email.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any

import httpx
import jwt
from fastapi import HTTPException, Request, status
from jwt import PyJWKClient

log = logging.getLogger(__name__)

# Refresh JWKS at most once every TTL seconds. Clerk rotates keys
# infrequently; an hour is comfortable.
_JWKS_TTL_SEC = 3600
_jwks_cache: dict[str, Any] = {"client": None, "fetched_at": 0.0}


def is_enabled() -> bool:
    """True when Clerk env vars are present."""
    return bool(
        os.environ.get("CLERK_PUBLISHABLE_KEY")
        and os.environ.get("CLERK_SECRET_KEY")
    )


def frontend_api_domain() -> str:
    """Decode publishable key to extract the Clerk Frontend API host.

    Publishable keys are formatted `pk_test_<base64>` or `pk_live_<base64>`,
    where the base64 segment encodes the frontend domain with a trailing
    `$` padding marker (Clerk's own convention, not standard base64).
    """
    pk = os.environ.get("CLERK_PUBLISHABLE_KEY", "")
    parts = pk.split("_", 2)
    if len(parts) < 3:
        return ""
    encoded = parts[2]
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        return base64.b64decode(padded).decode("utf-8").rstrip("$")
    except Exception:  # noqa: BLE001
        return ""


def sign_in_url(redirect: str | None = None) -> str:
    """Hosted Clerk sign-in URL for the configured instance."""
    domain = frontend_api_domain()
    if not domain:
        return "/"
    url = f"https://{domain}/sign-in"
    if redirect:
        url += f"?redirect_url={redirect}"
    return url


def _jwks_client() -> PyJWKClient:
    """JWKS client with TTL-based refresh."""
    now = time.monotonic()
    if (
        _jwks_cache["client"] is None
        or now - _jwks_cache["fetched_at"] > _JWKS_TTL_SEC
    ):
        domain = frontend_api_domain()
        if not domain:
            raise RuntimeError("CLERK_PUBLISHABLE_KEY not set")
        url = f"https://{domain}/.well-known/jwks.json"
        log.info("fetching Clerk JWKS from %s", url)
        _jwks_cache["client"] = PyJWKClient(url, cache_keys=True)
        _jwks_cache["fetched_at"] = now
    return _jwks_cache["client"]


def _verify_token(token: str) -> dict[str, Any]:
    """Verify a Clerk session JWT against JWKS. Returns the claims."""
    client = _jwks_client()
    signing_key = client.get_signing_key_from_jwt(token).key
    claims = jwt.decode(
        token,
        signing_key,
        algorithms=["RS256"],
        # Clerk doesn't set an `aud` claim by default on session tokens —
        # the verification rests on the issuer + signature.
        options={"verify_aud": False},
    )
    # Verify the issuer matches our Clerk instance.
    expected_iss = f"https://{frontend_api_domain()}"
    iss = claims.get("iss", "")
    if iss != expected_iss:
        raise jwt.InvalidIssuerError(f"unexpected iss: {iss}")
    return claims


def current_user(request: Request) -> dict[str, Any] | None:
    """Return the signed-in user, or None.

    Reads Clerk's session cookie and verifies the JWT. Never raises;
    callers use this when auth is OPTIONAL (read pages, /ask, /vocab).

    Clerk-js v6 writes the session JWT to both `__session` (legacy)
    AND `__session_<8charSuffix>` (instance-isolated). On refresh,
    the suffixed cookie is reliably updated; the plain one can drift
    stale. We accept either: try every cookie whose name starts with
    `__session` and return the first JWT that validates. The cookies
    are ordered by Python's dict; we walk all of them rather than
    guessing the suffix from the publishable key.
    """
    if not is_enabled():
        return None
    session_tokens = [
        v for name, v in request.cookies.items()
        if name == "__session" or name.startswith("__session_")
    ]
    if not session_tokens:
        return None
    claims: dict | None = None
    last_err: Exception | None = None
    for token in session_tokens:
        try:
            claims = _verify_token(token)
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    if claims is None:
        log.info("no valid session JWT in %d candidate cookies; last err: %s",
                 len(session_tokens), last_err)
        return None
    # Clerk's session JWT carries: sub, sid, iat, exp, plus optionally
    # custom claims (we can configure these from the Clerk dashboard).
    return {
        "clerk_user_id": claims.get("sub", ""),
        "session_id": claims.get("sid", ""),
        "username": claims.get("username") or claims.get("preferred_username") or "",
        "email": claims.get("email", ""),
        "raw_claims": claims,
    }


def require_user(request: Request) -> dict[str, Any]:
    """Same as current_user but raises 401 when not authed. Use as a
    FastAPI dependency on routes that mutate state."""
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in required.",
        )
    return user


def verify_webhook(
    payload: bytes,
    headers: dict[str, str],
    secret: str,
) -> dict[str, Any]:
    """Verify a Clerk webhook signature and return the parsed event.

    Clerk uses Svix to sign webhooks (svix-id, svix-timestamp,
    svix-signature headers). We delegate to the svix Python lib so we
    don't have to re-implement HMAC + timestamp-skew checks.

    Raises a value error if the signature is bad, the timestamp is
    skewed, or the JSON is malformed.
    """
    from svix.webhooks import Webhook, WebhookVerificationError
    if not secret:
        raise ValueError("CLERK_WEBHOOK_SECRET not set")
    wh = Webhook(secret)
    try:
        # svix lib expects a dict with the 3 svix-* keys; FastAPI gives
        # us a starlette Headers (case-insensitive dict-like). Project
        # to a plain dict so svix can read it predictably.
        h = {k.lower(): v for k, v in headers.items()}
        return wh.verify(payload, h)
    except WebhookVerificationError as e:
        raise ValueError(f"webhook signature failed: {e}") from e


def fetch_user_profile(clerk_user_id: str) -> dict[str, Any]:
    """Pull the canonical username + email from Clerk's Backend API.

    Called when we first see a Clerk user_id we don't have in our
    `users` table — Clerk's JWT carries `sub` reliably but the
    `username` / `email` claims are configurable per instance, so
    pull them from the canonical source.
    """
    secret = os.environ.get("CLERK_SECRET_KEY", "")
    if not secret:
        raise RuntimeError("CLERK_SECRET_KEY not set")
    r = httpx.get(
        f"https://api.clerk.com/v1/users/{clerk_user_id}",
        headers={"Authorization": f"Bearer {secret}"},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    primary_email = ""
    for e in data.get("email_addresses", []):
        if e.get("id") == data.get("primary_email_address_id"):
            primary_email = e.get("email_address", "")
            break
    return {
        "clerk_user_id": clerk_user_id,
        "username": data.get("username")
                    or f"user_{clerk_user_id[:8].lower()}",
        "email": primary_email,
        "first_name": data.get("first_name", ""),
        "last_name": data.get("last_name", ""),
    }
