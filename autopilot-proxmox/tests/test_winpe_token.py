"""Tests for HMAC bearer token sign + verify."""
import time

import pytest


def test_sign_and_verify_round_trip(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "test-secret")
    from web import winpe_token
    tok = winpe_token.sign(run_id=42, ttl_seconds=60)
    payload = winpe_token.verify(tok)
    assert payload["run_id"] == 42


def test_verify_rejects_expired_token(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "test-secret")
    from web import winpe_token
    tok = winpe_token.sign(run_id=42, ttl_seconds=-1)
    with pytest.raises(winpe_token.TokenExpired):
        winpe_token.verify(tok)


def test_verify_rejects_tampered_payload(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "test-secret")
    from web import winpe_token
    tok = winpe_token.sign(run_id=42, ttl_seconds=60)
    head, sig = tok.rsplit(".", 1)
    # flip a character in the payload
    tampered = head[:-1] + ("A" if head[-1] != "A" else "B") + "." + sig
    with pytest.raises(winpe_token.TokenInvalid):
        winpe_token.verify(tampered)


def test_verify_rejects_wrong_secret(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "secret-A")
    from web import winpe_token
    tok = winpe_token.sign(run_id=42, ttl_seconds=60)
    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "secret-B")
    with pytest.raises(winpe_token.TokenInvalid):
        winpe_token.verify(tok)


def test_sign_without_secret_raises(monkeypatch):
    monkeypatch.delenv("AUTOPILOT_WINPE_TOKEN_SECRET", raising=False)
    from web import winpe_token
    with pytest.raises(winpe_token.TokenSecretMissing):
        winpe_token.sign(run_id=1, ttl_seconds=60)


def test_token_is_url_safe(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "test-secret")
    from web import winpe_token
    tok = winpe_token.sign(run_id=42, ttl_seconds=60)
    # Acceptable chars: base64url alphabet plus the "." separator
    import re
    assert re.fullmatch(r"[A-Za-z0-9_\-.]+", tok) is not None
