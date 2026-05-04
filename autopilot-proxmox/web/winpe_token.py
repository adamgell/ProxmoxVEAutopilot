"""HMAC-signed bearer tokens for the WinPE phase-0 agent.

Tokens encode {run_id, expires_at} as base64url(JSON).base64url(HMAC).
The shared secret comes from the AUTOPILOT_WINPE_TOKEN_SECRET env var,
populated by web/app.py from vault_autopilot_winpe_token_secret.

Tokens are stateless: the server does not store them. Verification is
constant-time hmac.compare_digest. Tokens carry only a run_id; per-call
authorization (e.g. step belongs to run) lives in the endpoint code.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import time
from hashlib import sha256


class TokenError(Exception):
    pass


class TokenInvalid(TokenError):
    pass


class TokenExpired(TokenError):
    pass


class TokenSecretMissing(TokenError):
    pass


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _secret() -> bytes:
    s = os.environ.get("AUTOPILOT_WINPE_TOKEN_SECRET")
    if not s:
        raise TokenSecretMissing(
            "AUTOPILOT_WINPE_TOKEN_SECRET is not set; "
            "configure vault_autopilot_winpe_token_secret"
        )
    return s.encode("utf-8")


def sign(*, run_id: int, ttl_seconds: int) -> str:
    payload = {"run_id": run_id, "exp": int(time.time()) + ttl_seconds}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    head = _b64url(raw)
    sig = hmac.new(_secret(), head.encode("ascii"), sha256).digest()
    return head + "." + _b64url(sig)


def verify(token: str) -> dict:
    try:
        head, sig_b64 = token.rsplit(".", 1)
    except ValueError:
        raise TokenInvalid("malformed token")
    expected = hmac.new(_secret(), head.encode("ascii"), sha256).digest()
    try:
        actual = _b64url_decode(sig_b64)
    except Exception:
        raise TokenInvalid("malformed signature")
    if not hmac.compare_digest(expected, actual):
        raise TokenInvalid("signature mismatch")
    try:
        payload = json.loads(_b64url_decode(head))
    except Exception:
        raise TokenInvalid("malformed payload")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise TokenExpired("token expired")
    if "run_id" not in payload:
        raise TokenInvalid("payload missing run_id")
    return payload
