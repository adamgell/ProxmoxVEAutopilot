"""Tests for web.crypto — Fernet key bootstrap + encrypt/decrypt round-trip."""
from pathlib import Path

import pytest


@pytest.fixture
def secrets_dir(tmp_path):
    return tmp_path / "secrets"


def test_load_or_generate_creates_key_if_missing(secrets_dir):
    from web import crypto
    key_path = secrets_dir / "credential_key"
    assert not key_path.exists()
    key = crypto.load_or_generate_key(key_path)
    assert key_path.exists()
    assert len(key) == 44  # base64-encoded 32-byte Fernet key
    assert key_path.stat().st_mode & 0o777 == 0o600


def test_load_or_generate_is_idempotent(secrets_dir):
    from web import crypto
    key_path = secrets_dir / "credential_key"
    first = crypto.load_or_generate_key(key_path)
    second = crypto.load_or_generate_key(key_path)
    assert first == second


def test_encrypt_decrypt_round_trip(secrets_dir):
    from web import crypto
    key_path = secrets_dir / "credential_key"
    crypto.load_or_generate_key(key_path)
    cipher = crypto.Cipher(key_path)
    plaintext = b"hunter2 is not a great password"
    encrypted = cipher.encrypt(plaintext)
    assert encrypted != plaintext
    assert cipher.decrypt(encrypted) == plaintext


def test_decrypt_fails_with_wrong_key(secrets_dir, tmp_path):
    from web import crypto
    from cryptography.fernet import InvalidToken
    first = secrets_dir / "credential_key"
    second = tmp_path / "other_key"
    crypto.load_or_generate_key(first)
    crypto.load_or_generate_key(second)
    cipher_a = crypto.Cipher(first)
    cipher_b = crypto.Cipher(second)
    token = cipher_a.encrypt(b"secret")
    with pytest.raises(InvalidToken):
        cipher_b.decrypt(token)


def test_cipher_encrypts_json_payload(secrets_dir):
    from web import crypto
    key_path = secrets_dir / "credential_key"
    crypto.load_or_generate_key(key_path)
    cipher = crypto.Cipher(key_path)
    payload = {"username": "acme\\svc_join", "password": "p@ss"}
    token = cipher.encrypt_json(payload)
    assert cipher.decrypt_json(token) == payload
