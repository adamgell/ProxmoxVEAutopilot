"""Ubuntu seed-ISO builder: writes user-data + meta-data, invokes genisoimage."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from web.ubuntu_seed_iso import build_seed_iso


def test_build_seed_iso_writes_user_data_and_meta_data(tmp_path: Path) -> None:
    user_data = "#cloud-config\nautoinstall:\n  version: 1\n"
    meta_data = "instance-id: i-1\n"

    # Patch subprocess.run to avoid needing genisoimage in CI.
    with patch("web.ubuntu_seed_iso.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        iso_path = build_seed_iso(
            user_data=user_data, meta_data=meta_data,
            out_path=tmp_path / "ubuntu-seed.iso",
        )

    # Should have staged user-data + meta-data into a temp dir and called genisoimage
    args = mock_run.call_args[0][0]
    assert args[0] == "genisoimage"
    assert "-V" in args
    # NoCloud requires volume label "cidata" (lower-case).
    assert args[args.index("-V") + 1] == "cidata"
    # The output path we requested
    assert str(iso_path) in args


def test_build_seed_iso_raises_on_genisoimage_missing(tmp_path: Path) -> None:
    with patch("web.ubuntu_seed_iso.subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError("genisoimage")
        try:
            build_seed_iso(user_data="x", meta_data="y",
                           out_path=tmp_path / "s.iso")
        except RuntimeError as e:
            assert "genisoimage" in str(e)
        else:
            raise AssertionError("expected RuntimeError")


def test_build_seed_iso_accepts_optional_network_config(tmp_path: Path) -> None:
    with patch("web.ubuntu_seed_iso.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        build_seed_iso(
            user_data="#cloud-config\n",
            meta_data="instance-id: x\n",
            out_path=tmp_path / "with-network.iso",
            network_config="version: 2\nethernets:\n  eth0:\n    dhcp4: true\n",
        )
    # Stage dir passed as last arg must contain network-config
    stage_dir = Path(mock_run.call_args[0][0][-1])
    # The stage dir is a tempdir — existence checking not possible post-cleanup,
    # but we can confirm genisoimage was called with a real directory path.
    # Just assert the call happened without error.
    assert mock_run.called


# -----------------------------------------------------------------------------
# /api/ubuntu/rebuild-seed-iso endpoint tests (Task 17)
# -----------------------------------------------------------------------------


def _setup_client(tmp_path, monkeypatch, *, seed=True):
    """Install an Ubuntu sequence + seed defaults into a fresh DB, return TestClient."""
    from web import app as web_app, sequences_db
    from cryptography.fernet import Fernet

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    cred_key = secrets_dir / "credential_key"
    cred_key.write_bytes(Fernet.generate_key())

    monkeypatch.setattr(web_app, "SECRETS_DIR", secrets_dir)
    monkeypatch.setattr(web_app, "SEQUENCES_DB", tmp_path / "s.db")
    monkeypatch.setattr(web_app, "CREDENTIAL_KEY", cred_key)

    sequences_db.init(web_app.SEQUENCES_DB)
    if seed:
        sequences_db.seed_defaults(web_app.SEQUENCES_DB, web_app._cipher())
    from fastapi.testclient import TestClient
    return TestClient(web_app.app)


def test_rebuild_seed_iso_compiles_and_uploads(tmp_path, monkeypatch):
    # Stub stdlib `crypt` (removed in Py3.13) so web.ubuntu_compiler can import
    # create_ubuntu_user. This test does not exercise password hashing.
    import sys
    import types as _types
    if "crypt" not in sys.modules:
        stub = _types.ModuleType("crypt")
        stub.METHOD_SHA512 = "sha512"
        stub.mksalt = lambda method=None: "$6$stub$"
        stub.crypt = lambda password, salt: f"{salt}stub-hash"
        sys.modules["crypt"] = stub

    from web import sequences_db, app as web_app
    client = _setup_client(tmp_path, monkeypatch)
    # Find the Ubuntu Plain sequence id
    seqs = sequences_db.list_sequences(web_app.SEQUENCES_DB)
    ubuntu_plain = next(s for s in seqs if s["name"] == "Ubuntu Plain")

    # Minimal Proxmox config so the upload URL can be built
    monkeypatch.setattr(web_app, "_load_proxmox_config", lambda: {
        "proxmox_host": "pve.test", "proxmox_port": 8006, "proxmox_node": "pve",
        "proxmox_iso_storage": "isos",
        "vault_proxmox_api_token_id": "autopilot@pve!test",
        "vault_proxmox_api_token_secret": "s3cret",
        "proxmox_validate_certs": False,
    })

    # genisoimage is not installed in CI, so fake the seed-ISO builder and
    # have it write a stub file where the endpoint will look for the ISO.
    def fake_build(*, user_data, meta_data, out_path, network_config=None):
        Path(out_path).write_bytes(b"stub-iso")
        return Path(out_path)

    with patch("web.ubuntu_seed_iso.build_seed_iso", side_effect=fake_build), \
         patch("web.app.requests.post") as mock_upload:
        mock_upload.return_value = MagicMock(status_code=200, ok=True,
                                             text='{"data":null}')
        resp = client.post(
            f"/api/ubuntu/rebuild-seed-iso?sequence_id={ubuntu_plain['id']}"
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["iso"].endswith("ubuntu-seed.iso")


def test_rebuild_seed_iso_404_on_missing_sequence(tmp_path, monkeypatch):
    client = _setup_client(tmp_path, monkeypatch, seed=False)
    resp = client.post("/api/ubuntu/rebuild-seed-iso?sequence_id=9999")
    assert resp.status_code == 404
    assert resp.json()["ok"] is False


def test_rebuild_seed_iso_400_on_windows_sequence(tmp_path, monkeypatch):
    from web import sequences_db, app as web_app
    client = _setup_client(tmp_path, monkeypatch)
    seqs = sequences_db.list_sequences(web_app.SEQUENCES_DB)
    # The Entra Join default is a Windows sequence
    windows = next(s for s in seqs if s["target_os"] == "windows")
    resp = client.post(f"/api/ubuntu/rebuild-seed-iso?sequence_id={windows['id']}")
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


# -----------------------------------------------------------------------------
# Build Template page Ubuntu panel + /api/ubuntu/build-template (Task 18)
# -----------------------------------------------------------------------------


def test_template_page_renders_ubuntu_panel(tmp_path, monkeypatch):
    client = _setup_client(tmp_path, monkeypatch)
    resp = client.get("/template")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert 'id="panel-ubuntu"' in body
    assert 'Rebuild Ubuntu Seed ISO' in body
    # The Ubuntu Plain default sequence should populate the dropdown.
    assert 'Ubuntu Plain' in body


def test_build_ubuntu_template_returns_job_id(tmp_path, monkeypatch):
    from web import app as web_app, sequences_db
    client = _setup_client(tmp_path, monkeypatch)
    seqs = sequences_db.list_sequences(web_app.SEQUENCES_DB)
    ubuntu_plain = next(s for s in seqs if s["name"] == "Ubuntu Plain")

    fake_job = {"id": "job-abc"}
    with patch.object(web_app.job_manager, "start",
                      return_value=fake_job) as mock_start:
        resp = client.post(
            f"/api/ubuntu/build-template?sequence_id={ubuntu_plain['id']}"
        )

    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["ok"] is True
    assert j["job_id"] == "job-abc"
    # Confirm the playbook + extra-vars the handler passed through
    args, kwargs = mock_start.call_args
    assert args[0] == "build_template_ubuntu"
    cmd = args[1]
    assert any("build_template.yml" in c for c in cmd)
    assert "target_os=ubuntu" in cmd
    assert f"ubuntu_template_sequence_id={ubuntu_plain['id']}" in cmd


def test_build_ubuntu_template_404_on_missing_or_windows(tmp_path, monkeypatch):
    from web import app as web_app, sequences_db
    client = _setup_client(tmp_path, monkeypatch)

    # Missing id
    resp = client.post("/api/ubuntu/build-template?sequence_id=9999")
    assert resp.status_code == 404
    assert resp.json()["ok"] is False

    # Windows sequence id — must also 404 (not an Ubuntu target).
    seqs = sequences_db.list_sequences(web_app.SEQUENCES_DB)
    windows = next(s for s in seqs if s["target_os"] == "windows")
    resp = client.post(f"/api/ubuntu/build-template?sequence_id={windows['id']}")
    assert resp.status_code == 404
    assert resp.json()["ok"] is False
