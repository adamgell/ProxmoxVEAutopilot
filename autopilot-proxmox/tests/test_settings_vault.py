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
def app_client(pg_conn):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        secrets = tmp / "secrets"
        db = None
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
            from web import sequences_pg as _sdb
            secrets.mkdir(parents=True, exist_ok=True)
            _sdb.reset_for_tests(pg_conn)
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


def test_proxmox_bootstrap_script_repairs_role_storage_and_chassis_seed():
    from web import proxmox_permissions

    script = proxmox_permissions.build_bootstrap_script(
        api_token_id="autopilot@pve!ansible",
        disk_storage="ssdpool",
        iso_storage="isos",
        snippet_storage="local",
        chassis_types=(10,),
    )

    assert "pveum role add" in script
    assert "pveum role modify" in script
    assert "Datastore.Allocate" in script
    assert 'pveum acl modify "/storage/$storage"' in script
    assert "pvesm config" not in script
    assert "/etc/pve/storage.cfg" in script
    assert 'pvesm set "$SNIPPETS_STORAGE" --content "$next"' in script
    assert "autopilot-chassis-type-{chassis_type}.bin" in script
    assert "AUTOPILOT_BOOTSTRAP_OK" in script


def test_proxmox_bootstrap_endpoint_runs_ssh_and_saves_root_credentials(
    app_client, tmp_path, monkeypatch,
):
    from web import app as _app

    vault_path = tmp_path / "vault.yml"
    _write(vault_path, "---\nvault_proxmox_api_token_id: autopilot@pve!ansible\n")
    monkeypatch.setattr(_app, "VAULT_PATH", vault_path)

    ssh_calls = []

    def fake_runner(*, host, password, user):
        assert host == "192.168.2.200"
        assert password == "root-secret"
        assert user == "root"

        def run(cmd):
            ssh_calls.append(cmd)
            return 0, b"role_updated=AutopilotProvisioner\nAUTOPILOT_BOOTSTRAP_OK\n", b""

        return run

    with patch("web.app._load_proxmox_config", return_value={
        "proxmox_host": "192.168.2.200",
        "proxmox_storage": "ssdpool",
        "proxmox_iso_storage": "isos",
        "vault_proxmox_api_token_id": "autopilot@pve!ansible",
    }), patch("web.proxmox_permissions.answer_floppy_cache.make_sshpass_runner", fake_runner):
        r = app_client.post("/api/proxmox/bootstrap-permissions", data={
            "root_username": "root@pam",
            "root_password": "root-secret",
            "snippet_storage": "local",
            "save_root_credentials": "on",
        })

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["root_password_set"] is True
    assert "root-secret" not in body.get("stdout", "")
    assert ssh_calls, "bootstrap did not run over SSH"
    out = vault_path.read_text()
    assert "vault_proxmox_root_username: root@pam" in out
    assert "vault_proxmox_root_password: root-secret" in out


def test_proxmox_bootstrap_endpoint_does_not_save_on_failed_ssh(
    app_client, tmp_path, monkeypatch,
):
    from web import app as _app

    vault_path = tmp_path / "vault.yml"
    _write(vault_path, "---\n")
    monkeypatch.setattr(_app, "VAULT_PATH", vault_path)

    def fake_runner(*, host, password, user):
        def run(cmd):
            return 255, b"", b"Permission denied"

        return run

    with patch("web.app._load_proxmox_config", return_value={
        "proxmox_host": "192.168.2.200",
        "proxmox_storage": "ssdpool",
        "proxmox_iso_storage": "isos",
        "vault_proxmox_api_token_id": "autopilot@pve!ansible",
    }), patch("web.proxmox_permissions.answer_floppy_cache.make_sshpass_runner", fake_runner):
        r = app_client.post("/api/proxmox/bootstrap-permissions", data={
            "root_username": "root@pam",
            "root_password": "bad",
            "save_root_credentials": "on",
        })

    assert r.status_code == 502
    assert "Permission denied" in r.json()["detail"]
    assert "vault_proxmox_root_password" not in vault_path.read_text()


def test_settings_page_renders_proxmox_permission_bootstrap(
    app_client, tmp_path, monkeypatch,
):
    from web import app as _app

    vault_path = tmp_path / "vault.yml"
    _write(vault_path, "---\nvault_proxmox_root_password: ROOT-SECRET\n")
    monkeypatch.setattr(_app, "VAULT_PATH", vault_path)

    with patch("web.app._load_vars", return_value={"hypervisor_type": "proxmox"}), \
         patch("web.app._load_proxmox_config", return_value={
             "hypervisor_type": "proxmox",
             "proxmox_host": "192.168.2.200",
             "proxmox_storage": "ssdpool",
             "proxmox_iso_storage": "isos",
         }), \
         patch("web.app._fetch_settings_options", return_value={}):
        r = app_client.get("/settings")

    assert r.status_code == 200
    body = r.text
    assert "Proxmox Permission Bootstrap" in body
    assert "Apply Proxmox permissions over SSH" in body
    assert "ROOT-SECRET" not in body


def test_settings_page_treats_pve_alias_as_proxmox(app_client, monkeypatch):
    from web import app as _app

    with patch("web.app._load_vars", return_value={"hypervisor_type": "pve"}), \
         patch("web.app._load_vault", return_value={}), \
         patch("web.app._vault_presence", return_value={}), \
         patch("web.app._load_proxmox_config", return_value={
             "hypervisor_type": "pve",
             "proxmox_host": "192.168.2.200",
         }), \
         patch("web.app._fetch_settings_options", return_value={}):
        r = app_client.get("/settings")

    assert r.status_code == 200
    assert "Proxmox Connection" in r.text
    assert "Proxmox Permission Bootstrap" in r.text


def test_settings_page_renders_osdeploy_build_host_fields(app_client):
    with patch("web.app._load_vars", return_value={
        "hypervisor_type": "proxmox",
        "osdeploy_build_remote": "builder@example",
        "osdeploy_build_remote_root": "F:\\BuildRoot",
        "osdeploy_build_ssh_key_path": "/app/secrets/osdeploy_key",
    }), \
         patch("web.app._load_vault", return_value={}), \
         patch("web.app._vault_presence", return_value={}), \
         patch("web.app._load_proxmox_config", return_value={"hypervisor_type": "proxmox"}), \
         patch("web.app._fetch_settings_options", return_value={}):
        r = app_client.get("/settings")

    assert r.status_code == 200
    body = r.text
    assert "OSDeploy Build Host" in body
    assert 'name="osdeploy_build_remote"' in body
    assert 'name="osdeploy_build_remote_root"' in body
    assert 'name="osdeploy_build_ssh_key_path"' in body
    assert "builder@example" in body
    assert "F:\\BuildRoot" in body
    assert "/app/secrets/osdeploy_key" in body


def test_settings_save_persists_osdeploy_build_host_fields(app_client):
    saved_updates = {}

    def fake_save_vars(updates):
        saved_updates.update(updates)

    with patch("web.app._load_vars", return_value={"hypervisor_type": "proxmox"}), \
         patch("web.app._save_vars", side_effect=fake_save_vars), \
         patch("web.app._save_vault"):
        r = app_client.post("/api/settings", data={
            "hypervisor_type": "proxmox",
            "osdeploy_build_remote": "Adam.Gell@192.168.2.50",
            "osdeploy_build_remote_root": "F:\\OSDeployBuild",
            "osdeploy_build_ssh_key_path": "/app/secrets/osdeploy_devmachine_ed25519",
        }, follow_redirects=False)

    assert r.status_code == 303
    assert saved_updates["osdeploy_build_remote"] == "Adam.Gell@192.168.2.50"
    assert saved_updates["osdeploy_build_remote_root"] == "F:\\OSDeployBuild"
    assert saved_updates["osdeploy_build_ssh_key_path"] == "/app/secrets/osdeploy_devmachine_ed25519"
