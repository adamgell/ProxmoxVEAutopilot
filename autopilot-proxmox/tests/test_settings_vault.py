"""Editing vault.yml via the /settings page.

The vault section of SETTINGS_SCHEMA renders password/text inputs that
reflect presence only (never the stored value). Saving with a blank
secret leaves the existing value intact; saving a non-empty value
rewrites the line in vault.yml via _save_yaml_file, which preserves
comments and unrelated keys.
"""
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def app_client():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        secrets = tmp / "secrets"
        db = tmp / "sequences.db"
        import web.app as _wa
        _wa._CIPHER = None
        with patch("web.app.SECRETS_DIR", secrets), \
             patch("web.app.SEQUENCES_DB", db), \
             patch("web.app.CREDENTIAL_KEY", secrets / "credential_key"), \
             patch("web.app.HASH_DIR", tmp / "hashes"), \
             patch("web.app.job_manager") as jm:
            jm.list_jobs.return_value = []
            jm.jobs_dir = str(tmp / "jobs")
            from web.app import app
            from web import sequences_db as _sdb
            secrets.mkdir(parents=True, exist_ok=True)
            _sdb.init(db)
            yield TestClient(app)


def test_save_yaml_file_updates_existing_key(tmp_path):
    from web import app as _app
    p = tmp_path / "vault.yml"
    _write(p, "---\n"
              "# This is a comment\n"
              "vault_proxmox_api_token_id: old-id\n"
              "vault_proxmox_api_token_secret: OLD\n"
              "vault_entra_app_id: ''\n")
    _app._save_yaml_file(p, {"vault_proxmox_api_token_secret": "NEW-SECRET"})
    out = p.read_text()
    # Secret rewritten inline; comment and other keys preserved.
    assert "# This is a comment" in out
    assert "vault_proxmox_api_token_id: old-id" in out
    assert "vault_proxmox_api_token_secret: NEW-SECRET" in out
    assert "OLD" not in out
    assert "vault_entra_app_id: ''" in out


def test_save_yaml_file_appends_missing_key(tmp_path):
    from web import app as _app
    p = tmp_path / "vault.yml"
    _write(p, "---\nvault_proxmox_api_token_id: x\n")
    _app._save_yaml_file(p, {"vault_proxmox_root_password": "hunter2"})
    out = p.read_text()
    assert "vault_proxmox_root_password: hunter2" in out
    assert "vault_proxmox_api_token_id: x" in out


def test_save_yaml_file_quotes_special_chars(tmp_path):
    """Passwords containing YAML-special chars must be quoted so the
    file remains parseable. Regression guard for ':' and '#'."""
    from web import app as _app
    import yaml
    p = tmp_path / "vault.yml"
    _write(p, "---\nvault_proxmox_root_password: \"\"\n")
    _app._save_yaml_file(p, {
        "vault_proxmox_root_password": "pa:ss#word!",
    })
    parsed = yaml.safe_load(p.read_text())
    assert parsed["vault_proxmox_root_password"] == "pa:ss#word!"


def test_vault_presence_reports_only_which_keys_are_set(tmp_path, monkeypatch):
    from web import app as _app
    p = tmp_path / "vault.yml"
    _write(p, "---\n"
              "vault_proxmox_api_token_id: abc\n"
              "vault_proxmox_api_token_secret: \"\"\n"
              "vault_entra_app_id: tenant-guid\n")
    monkeypatch.setattr(_app, "VAULT_PATH", p)
    presence = _app._vault_presence()
    assert presence["vault_proxmox_api_token_id"] is True
    assert presence["vault_proxmox_api_token_secret"] is False
    assert presence["vault_entra_app_id"] is True
    # Missing keys don't appear (caller is expected to default to False).
    assert "vault_proxmox_root_password" not in presence


def test_settings_page_never_echoes_secret_values(app_client, tmp_path, monkeypatch):
    """A secret that is set in vault.yml must NOT appear in the HTML.
    Only the 'set' badge + blank password input."""
    from web import app as _app
    p = tmp_path / "vault.yml"
    _write(p, "---\nvault_proxmox_api_token_secret: SUPER-SECRET-XYZ\n"
              "vault_proxmox_root_password: \"\"\n")
    monkeypatch.setattr(_app, "VAULT_PATH", p)

    from unittest.mock import patch
    with patch("web.app._load_vars", return_value={}), \
         patch("web.app._fetch_settings_options", return_value={}):
        r = app_client.get("/settings")
    body = r.text
    assert r.status_code == 200
    assert "SUPER-SECRET-XYZ" not in body, \
        "stored secret leaked into settings page HTML"
    # The presence badge is rendered instead.
    assert "vault_proxmox_api_token_secret" in body
    # Unset secrets show 'not set' badge.
    assert "not set" in body


def test_settings_save_preserves_secret_when_form_blank(app_client, tmp_path, monkeypatch):
    from web import app as _app
    p = tmp_path / "vault.yml"
    _write(p, "---\nvault_proxmox_api_token_secret: ORIGINAL-SECRET\n"
              "vault_proxmox_api_token_id: tok-id\n")
    monkeypatch.setattr(_app, "VAULT_PATH", p)

    from unittest.mock import patch
    with patch("web.app._load_vars", return_value={}), \
         patch("web.app._save_vars"):  # don't touch vars.yml on disk
        r = app_client.post("/api/settings", data={
            "vault_proxmox_api_token_id": "new-id",
            # vault_proxmox_api_token_secret: blank → preserve
        }, follow_redirects=False)
    assert r.status_code == 303
    out = p.read_text()
    assert "vault_proxmox_api_token_id: new-id" in out
    # Critical: the blank submit didn't clobber the existing secret.
    assert "ORIGINAL-SECRET" in out


def test_settings_save_rotates_secret_when_form_nonempty(app_client, tmp_path, monkeypatch):
    from web import app as _app
    p = tmp_path / "vault.yml"
    _write(p, "---\nvault_proxmox_api_token_secret: ORIGINAL\n")
    monkeypatch.setattr(_app, "VAULT_PATH", p)

    from unittest.mock import patch
    with patch("web.app._load_vars", return_value={}), \
         patch("web.app._save_vars"):
        r = app_client.post("/api/settings", data={
            "vault_proxmox_api_token_secret": "ROTATED-VALUE",
        }, follow_redirects=False)
    assert r.status_code == 303
    out = p.read_text()
    assert "ROTATED-VALUE" in out
    assert "ORIGINAL" not in out
