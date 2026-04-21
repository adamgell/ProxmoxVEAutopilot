"""Entra OIDC authentication for the web UI.

Flow (Authorization Code + PKCE):

    /auth/login    → generate state + code-verifier, redirect to Entra
    /auth/callback → exchange auth code for ID token, validate, drop
                     session cookie, redirect back to `next` (default /)
    /auth/logout   → clear session, redirect to /

The session cookie is signed with a per-deployment secret and holds
only the claims we need (sub, name, email, groups). Protected routes
use `Depends(current_user)` to require a session and pull the user
dict back out.

Group restriction (optional): if ``vault_entra_admin_group_id`` is
set, only users whose ID-token ``groups`` claim contains that GUID
are allowed. Otherwise, any signed-in user in the tenant is allowed.

Exempt routes (always public): ``/auth/*``, ``/healthz``,
``/api/version``. Static files and the login template itself are
served without auth too.
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional
from urllib.parse import urlencode, urlparse

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from authlib.jose import jwt

log = logging.getLogger(__name__)


# Routes that bypass auth. Keep this small — anything here is
# world-readable. /healthz is for uptime probes, /api/version is the
# footer version check (doesn't leak any config).
_EXEMPT_PREFIXES = (
    "/auth/",
    "/healthz",
    "/api/version",
    "/static/",
    "/favicon.ico",
)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _issuer(tenant_id: str) -> str:
    """Entra's OIDC issuer URL for a specific tenant."""
    return f"https://login.microsoftonline.com/{tenant_id}/v2.0"


def _authorize_url(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"


def _token_url(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


def _jwks_uri(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"


# Cache the JWKS keys per tenant — Entra rotates them but not often;
# authlib's jwt.decode handles key-id lookup against the JWK set.
_JWKS_CACHE: dict = {}


def _jwks_for(tenant_id: str) -> dict:
    import requests
    cached = _JWKS_CACHE.get(tenant_id)
    if cached:
        return cached
    r = requests.get(_jwks_uri(tenant_id), timeout=10)
    r.raise_for_status()
    jwks = r.json()
    _JWKS_CACHE[tenant_id] = jwks
    return jwks


# ---------------------------------------------------------------------------
# Session cookie middleware installer
# ---------------------------------------------------------------------------

def install_session_middleware(app, *, secret: str) -> None:
    """Called once at app startup. Uses Starlette's SessionMiddleware —
    cookie is HMAC-signed, httponly, samesite=lax (so the Entra redirect
    can carry it back across the redirect). Mark secure=True only when
    we detect HTTPS — operators running behind an http-only reverse
    proxy would otherwise lose the cookie."""
    # We DON'T set https_only=True globally; let the proxy decide. The
    # upstream proxy (NPM) terminates TLS and adds X-Forwarded-Proto;
    # Starlette's SessionMiddleware doesn't read that, so unconditional
    # https_only would break localhost dev. Session hijack risk here is
    # low (single-operator lab).
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="autopilot_session",
        same_site="lax",
        https_only=False,
        max_age=60 * 60 * 8,  # 8-hour session; operator re-logs daily
    )


# ---------------------------------------------------------------------------
# Exemption + enforcement
# ---------------------------------------------------------------------------

def is_exempt_path(path: str) -> bool:
    return any(path.startswith(p) for p in _EXEMPT_PREFIXES)


def current_user(request: Request) -> dict:
    """FastAPI dependency — returns the session user or raises 302 to
    /auth/login. Use with `Depends(current_user)`.
    """
    user = request.session.get("user")
    if not user:
        # For HTML requests, redirect to login. For API requests (JSON
        # Accept header or /api/ path), return 401 so automation can
        # detect + retry. Distinguish by path prefix: /api/ returns
        # 401 JSON; everything else redirects.
        accept = request.headers.get("accept", "")
        if request.url.path.startswith("/api/") or "application/json" in accept:
            raise HTTPException(
                status_code=401,
                detail="authentication required — POST /auth/login in a browser session",
            )
        next_url = request.url.path
        if request.url.query:
            next_url += "?" + request.url.query
        raise HTTPException(
            status_code=302,
            headers={"Location": f"/auth/login?next={next_url}"},
        )
    return user


# ---------------------------------------------------------------------------
# Login / callback / logout handlers
# ---------------------------------------------------------------------------

def build_login_url(
    request: Request, *,
    tenant_id: str, client_id: str,
    redirect_uri: str, next_url: str = "/",
) -> str:
    """Generate the Entra authorize URL + stash state/verifier in session."""
    state = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(48)

    # Minimal PKCE — S256 challenge.
    import hashlib, base64
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    request.session["oidc_state"] = state
    request.session["oidc_verifier"] = code_verifier
    request.session["oidc_next"] = next_url

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "openid profile email User.Read",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "response_mode": "query",
        # Single-tenant — use common/organizations if you ever want to
        # accept guest users. AzureADMyOrg on the app reg gates this
        # anyway; the tenant-scoped authorize URL is belt-and-braces.
        "prompt": "select_account",
    }
    return f"{_authorize_url(tenant_id)}?{urlencode(params)}"


def exchange_code_for_token(*, tenant_id: str, client_id: str,
                            client_secret: str, redirect_uri: str,
                            code: str, code_verifier: str) -> dict:
    """POST to /token, return the token response dict."""
    import requests
    r = requests.post(
        _token_url(tenant_id),
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        timeout=15,
    )
    if r.status_code >= 400:
        raise HTTPException(
            500,
            f"token exchange failed ({r.status_code}): {r.text[:300]}",
        )
    return r.json()


def validate_id_token(id_token: str, *, tenant_id: str,
                      client_id: str) -> dict:
    """Verify Entra's JWT signature + standard claims, return the
    parsed claims dict."""
    jwks = _jwks_for(tenant_id)
    issuer = _issuer(tenant_id)
    # authlib's jwt.decode handles signature + exp + nbf when we pass
    # the claims_options. iss / aud / exp are mandatory; we also check
    # tid matches our tenant so a rogue token minted for another
    # tenant's app reg can't satisfy us.
    claims = jwt.decode(
        id_token, jwks,
        claims_options={
            "iss": {"essential": True, "values": [issuer]},
            "aud": {"essential": True, "values": [client_id]},
            "tid": {"essential": True, "values": [tenant_id]},
            "exp": {"essential": True},
        },
    )
    claims.validate()
    return dict(claims)


def user_from_claims(claims: dict) -> dict:
    """Extract the user identity we care about from ID-token claims."""
    return {
        "sub": claims.get("sub"),
        "oid": claims.get("oid"),
        "name": claims.get("name") or claims.get("preferred_username"),
        "email": claims.get("email") or claims.get("preferred_username"),
        "upn": claims.get("preferred_username"),
        "groups": claims.get("groups") or [],
        "tid": claims.get("tid"),
    }


def is_authorized(user: dict, *, admin_group_id: Optional[str]) -> bool:
    """If admin_group_id is unset, any tenant user is authorized.
    Otherwise the user's groups claim must include it."""
    if not admin_group_id:
        return True
    return admin_group_id in (user.get("groups") or [])


def safe_next_url(value: str) -> str:
    """Only allow relative next URLs so an attacker can't pivot a
    successful login into an open-redirect."""
    if not value:
        return "/"
    try:
        parsed = urlparse(value)
    except ValueError:
        return "/"
    if parsed.scheme or parsed.netloc:
        return "/"
    if not parsed.path.startswith("/"):
        return "/"
    return value
