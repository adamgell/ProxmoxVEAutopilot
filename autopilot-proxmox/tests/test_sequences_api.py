"""End-to-end API tests for credentials and sequences routes."""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_env():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        secrets = tmp / "secrets"
        db = tmp / "sequences.db"
        # Reset the process-wide cipher cache so this test's patched
        # CREDENTIAL_KEY is actually used.
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
            # Init DB without seeds so "empty" tests remain valid.
            from web import sequences_db as _sdb
            secrets.mkdir(parents=True, exist_ok=True)
            _sdb.init(db)
            yield TestClient(app)


def test_credentials_list_empty(app_env):
    r = app_env.get("/api/credentials")
    assert r.status_code == 200
    assert r.json() == []


def test_create_credential(app_env):
    r = app_env.post("/api/credentials", json={
        "name": "acme-svc", "type": "domain_join",
        "payload": {"username": "acme\\svc", "password": "p@ss",
                    "domain_fqdn": "acme.local"},
    })
    assert r.status_code == 201
    cid = r.json()["id"]

    r = app_env.get("/api/credentials")
    assert len(r.json()) == 1
    assert r.json()[0]["name"] == "acme-svc"

    # Full get includes payload
    r = app_env.get(f"/api/credentials/{cid}")
    assert r.status_code == 200
    assert r.json()["payload"]["password"] == "p@ss"


def test_create_credential_duplicate_name(app_env):
    body = {"name": "a", "type": "local_admin",
            "payload": {"username": "x", "password": "y"}}
    assert app_env.post("/api/credentials", json=body).status_code == 201
    assert app_env.post("/api/credentials", json=body).status_code == 409


def test_update_credential_partial(app_env):
    cid = app_env.post("/api/credentials", json={
        "name": "a", "type": "local_admin",
        "payload": {"username": "x", "password": "y"},
    }).json()["id"]
    r = app_env.patch(f"/api/credentials/{cid}", json={"name": "a-new"})
    assert r.status_code == 200
    assert app_env.get(f"/api/credentials/{cid}").json()["name"] == "a-new"


def test_delete_credential_blocked(app_env):
    cid = app_env.post("/api/credentials", json={
        "name": "a", "type": "domain_join",
        "payload": {"username": "x", "password": "y", "domain_fqdn": "z"},
    }).json()["id"]
    sid = app_env.post("/api/sequences", json={
        "name": "S", "description": "",
        "steps": [
            {"step_type": "join_ad_domain",
             "params": {"credential_id": cid, "ou_path": "OU=X"},
             "enabled": True},
        ],
    }).json()["id"]
    r = app_env.delete(f"/api/credentials/{cid}")
    assert r.status_code == 409
    assert sid in r.json()["sequence_ids"]


def test_sequences_list_empty(app_env):
    assert app_env.get("/api/sequences").json() == []


def test_create_sequence_with_steps(app_env):
    r = app_env.post("/api/sequences", json={
        "name": "Entra", "description": "d", "is_default": True,
        "produces_autopilot_hash": True,
        "steps": [
            {"step_type": "set_oem_hardware",
             "params": {"oem_profile": "dell-latitude-5540"}, "enabled": True},
            {"step_type": "autopilot_entra", "params": {}, "enabled": True},
        ],
    })
    assert r.status_code == 201
    sid = r.json()["id"]
    got = app_env.get(f"/api/sequences/{sid}").json()
    assert got["name"] == "Entra"
    assert got["is_default"] is True
    assert [s["step_type"] for s in got["steps"]] == [
        "set_oem_hardware", "autopilot_entra"]


def test_update_sequence_replaces_steps(app_env):
    sid = app_env.post("/api/sequences", json={
        "name": "S", "description": "",
        "steps": [
            {"step_type": "autopilot_entra", "params": {}, "enabled": True},
        ],
    }).json()["id"]
    r = app_env.put(f"/api/sequences/{sid}", json={
        "name": "S", "description": "updated",
        "steps": [
            {"step_type": "local_admin",
             "params": {"credential_id": 99}, "enabled": True},
        ],
    })
    assert r.status_code == 200
    got = app_env.get(f"/api/sequences/{sid}").json()
    assert got["description"] == "updated"
    assert [s["step_type"] for s in got["steps"]] == ["local_admin"]


def test_duplicate_sequence(app_env):
    sid = app_env.post("/api/sequences", json={
        "name": "Original", "description": "",
        "steps": [
            {"step_type": "autopilot_entra", "params": {}, "enabled": True},
        ],
    }).json()["id"]
    r = app_env.post(f"/api/sequences/{sid}/duplicate",
                     json={"new_name": "Original (copy)"})
    assert r.status_code == 201
    new_id = r.json()["id"]
    assert app_env.get(f"/api/sequences/{new_id}").json()["name"] == \
        "Original (copy)"


def test_delete_sequence(app_env):
    sid = app_env.post("/api/sequences", json={
        "name": "S", "description": "", "steps": [],
    }).json()["id"]
    assert app_env.delete(f"/api/sequences/{sid}").status_code == 200
    assert app_env.get(f"/api/sequences/{sid}").status_code == 404


def test_only_one_default_via_api(app_env):
    a = app_env.post("/api/sequences", json={
        "name": "A", "description": "", "is_default": True, "steps": [],
    }).json()["id"]
    b = app_env.post("/api/sequences", json={
        "name": "B", "description": "", "is_default": True, "steps": [],
    }).json()["id"]
    got_a = app_env.get(f"/api/sequences/{a}").json()
    got_b = app_env.get(f"/api/sequences/{b}").json()
    assert got_a["is_default"] is False
    assert got_b["is_default"] is True


def test_record_vms_only_for_success_and_anchored_regex(app_env, tmp_path):
    """The VMID scraper must only write vm_provisioning rows for successful
    jobs (no partial-failure VMIDs) and must only match the success debug
    line (not the failure-diagnostic line in proxmox_vm_clone/main.yml)."""
    from web import sequences_db
    from web.app import _record_vms_for_sequence, SEQUENCES_DB, job_manager

    seq_id = sequences_db.create_sequence(
        SEQUENCES_DB, name="rec-test", description="",
    )

    # Build a fake log that contains BOTH a failure-diagnostic line and a
    # success line. The scraper must only pick up the success VMID (102).
    log_dir = Path(job_manager.jobs_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "job-xyz.log"
    log_path.write_text(
        "TASK [proxmox_vm_clone : Fail if cloned VM has no scsi0 disk] ****\n"
        "fatal: [localhost]: FAILED! => Cloned VM 'foo' (VMID: 999) has no scsi0 disk.\n"
        "TASK [proxmox_vm_clone : Report cloned VM] ****\n"
        "ok: [localhost] => (Cloned VM 'bar' (VMID: 102) from template 9000. ...)\n"
    )

    # Failing job → no rows written
    _record_vms_for_sequence({"id": "job-xyz", "status": "failed"}, seq_id)
    assert sequences_db.get_vm_sequence_id(SEQUENCES_DB, 102) is None
    assert sequences_db.get_vm_sequence_id(SEQUENCES_DB, 999) is None

    # Successful job → only the success-line VMID (102) is recorded.
    _record_vms_for_sequence({"id": "job-xyz", "status": "complete"}, seq_id)
    assert sequences_db.get_vm_sequence_id(SEQUENCES_DB, 102) == seq_id
    assert sequences_db.get_vm_sequence_id(SEQUENCES_DB, 999) is None


def test_provision_passes_chassis_type_override_to_ansible(app_env):
    """POST /api/jobs/provision with chassis_type_override must put
    -e chassis_type_override=N into the ansible command AND call the
    snippet uploader for that chassis type."""
    from web import sequences_db, crypto
    from web.app import SEQUENCES_DB, CREDENTIAL_KEY, job_manager
    cipher = crypto.Cipher(CREDENTIAL_KEY)

    seq_id = sequences_db.create_sequence(
        SEQUENCES_DB, name="test-chassis", description="",
    )
    sequences_db.set_sequence_steps(SEQUENCES_DB, seq_id, [
        {"step_type": "autopilot_entra", "params": {}, "enabled": True},
    ])

    captured = {}
    def fake_start(name, cmd, args=None):
        captured["cmd"] = list(cmd)
        return {"id": "fake-job"}
    job_manager.start.side_effect = fake_start
    job_manager.set_arg = lambda *a, **k: None
    job_manager.add_on_complete = lambda *a, **k: None

    from unittest.mock import patch
    # Pin the Proxmox config so the test is independent of whatever
    # vars.yml happens to live on the developer's machine.
    with patch("web.app._load_proxmox_config", return_value={
        "proxmox_node": "pve", "proxmox_snippets_storage": "local",
    }), patch("web.proxmox_snippets.ensure_chassis_type_binary") as mock_ensure:
        mock_ensure.return_value = "/var/lib/vz/snippets/fake.bin"
        r = app_env.post("/api/jobs/provision", data={
            "profile": "",
            "count": "1",
            "cores": "2",
            "memory_mb": "4096",
            "disk_size_gb": "64",
            "serial_prefix": "",
            "group_tag": "",
            "sequence_id": str(seq_id),
            "chassis_type_override": "31",
        }, follow_redirects=False)
    assert r.status_code == 303
    cmd = captured["cmd"]
    assert "chassis_type_override=31" in cmd
    mock_ensure.assert_called_with(node="pve", storage="local", chassis_type=31)


def test_provision_renders_runonce_scripts_per_job(app_env, tmp_path):
    """POST /api/jobs/provision with a sequence that includes
    join_ad_domain renders a per-job runonce/ dir and includes its
    manifest in the ansible -e args."""
    from web import sequences_db, crypto
    from web.app import SEQUENCES_DB, CREDENTIAL_KEY, job_manager
    cipher = crypto.Cipher(CREDENTIAL_KEY)

    cred_id = sequences_db.create_credential(
        SEQUENCES_DB, cipher, name="test-dj", type="domain_join",
        payload={"domain_fqdn": "example.local", "username": "x",
                 "password": "y", "ou_hint": ""},
    )
    seq_id = sequences_db.create_sequence(
        SEQUENCES_DB, name="test-ad", description="",
    )
    sequences_db.set_sequence_steps(SEQUENCES_DB, seq_id, [
        {"step_type": "join_ad_domain",
         "params": {"credential_id": cred_id, "ou_path": "OU=Test"},
         "enabled": True},
    ])

    captured = {}
    def fake_start(name, cmd, args=None):
        captured["cmd"] = list(cmd)
        return {"id": "fake-job"}
    job_manager.start.side_effect = fake_start
    job_manager.set_arg = lambda *a, **k: None
    job_manager.add_on_complete = lambda *a, **k: None
    job_manager.jobs_dir = str(tmp_path / "jobs")

    from unittest.mock import patch
    with patch("web.app._load_proxmox_config", return_value={
        "proxmox_node": "pve", "proxmox_snippets_storage": "local",
    }), patch("web.proxmox_snippets.ensure_chassis_type_binary", return_value=""):
        r = app_env.post("/api/jobs/provision", data={
            "profile": "",
            "count": "1",
            "cores": "2",
            "memory_mb": "4096",
            "disk_size_gb": "64",
            "serial_prefix": "",
            "group_tag": "",
            "sequence_id": str(seq_id),
        }, follow_redirects=False)
    assert r.status_code == 303

    import json as _json
    cmd = captured["cmd"]
    runonce_arg = next(
        (c for c in cmd if isinstance(c, str) and c.startswith("_runonce_scripts_json=")),
        None,
    )
    assert runonce_arg is not None, f"no _runonce_scripts_json in cmd: {cmd}"
    manifest = _json.loads(runonce_arg.split("=", 1)[1])
    assert len(manifest) == 1
    assert manifest[0]["step_type"] == "join_ad_domain"
    assert manifest[0]["causes_reboot"] is True

    from pathlib import Path
    p = Path(manifest[0]["path"])
    assert p.exists()
    assert p.stat().st_mode & 0o777 == 0o600
    content = p.read_text()
    assert "example.local" in content
    assert "OU=Test" in content
    assert content.count("'y'") >= 1
    # Branding envelope present
    assert "ProxmoxVEAutopilot" in content
    assert "EventId 1001" in content
    assert r"HKLM:\SOFTWARE\ProxmoxVEAutopilot\Provisioning" in content
    assert "Restart-Computer -Force" in content


def test_test_domain_join_endpoint_uses_ldap3(app_env):
    """The /api/credentials/test-domain-join endpoint calls ldap_tester."""
    from unittest.mock import patch
    fake_result = {"ok": True, "dns": {"ok": True, "servers": ["dc01"]},
                   "connect": {"ok": True}, "bind": {"ok": True},
                   "rootdse": {"ok": True}, "ou": {"ok": True}}
    with patch("web.ldap_tester.test_domain_join", return_value=fake_result):
        r = app_env.post("/api/credentials/test-domain-join", json={
            "payload": {"domain_fqdn": "example.local",
                        "username": "x", "password": "y", "ou_hint": ""}
        })
    assert r.status_code == 200
    assert r.json() == fake_result


def test_startup_seeds_defaults(tmp_path):
    """When the app starts on an empty DB, the three seed sequences appear."""
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        secrets = tmp / "secrets"
        db = tmp / "sequences.db"
        # Reset the process-wide cipher cache so this test's patched
        # CREDENTIAL_KEY is actually used.
        import web.app as _wa
        _wa._CIPHER = None
        with patch("web.app.SECRETS_DIR", secrets), \
             patch("web.app.SEQUENCES_DB", db), \
             patch("web.app.CREDENTIAL_KEY", secrets / "credential_key"), \
             patch("web.app.HASH_DIR", tmp / "hashes"), \
             patch("web.app.job_manager") as jm:
            jm.list_jobs.return_value = []
            jm.jobs_dir = str(tmp / "jobs")
            # Importing triggers @on_event("startup"); TestClient replays it.
            from web.app import app
            with TestClient(app) as c:
                got = c.get("/api/sequences").json()
    names = [s["name"] for s in got]
    assert "Entra Join (default)" in names
    assert "AD Domain Join — Local Admin" in names
    assert "Hybrid Autopilot (stub)" in names


def test_provision_with_sequence_passes_compiled_vars(app_env):
    """POST /api/jobs/provision with sequence_id=<seq> must put
    autopilot_enabled=true and the compiled vm_oem_profile into the
    ansible-playbook command's -e args."""
    from web import sequences_db, crypto
    from web.app import SEQUENCES_DB, CREDENTIAL_KEY, job_manager
    cipher = crypto.Cipher(CREDENTIAL_KEY)
    # Seed a local_admin credential + a sequence that uses OEM + entra.
    sequences_db.create_credential(
        SEQUENCES_DB, cipher, name="la", type="local_admin",
        payload={"username": "Administrator", "password": "x"},
    )
    seq_id = sequences_db.create_sequence(
        SEQUENCES_DB, name="test-entra", description="",
        is_default=True, produces_autopilot_hash=True,
    )
    sequences_db.set_sequence_steps(SEQUENCES_DB, seq_id, [
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "lenovo-t14"}, "enabled": True},
        {"step_type": "autopilot_entra", "params": {}, "enabled": True},
    ])

    # Capture the command JobManager.start is called with.
    captured = {}
    def fake_start(name, cmd, args=None):
        captured["cmd"] = list(cmd)
        captured["args"] = args or {}
        return {"id": "fake-job"}
    job_manager.start.side_effect = fake_start
    job_manager.set_arg = lambda *a, **k: None
    job_manager.add_on_complete = lambda *a, **k: None

    r = app_env.post("/api/jobs/provision", data={
        "profile": "",
        "count": "1",
        "cores": "2",
        "memory_mb": "4096",
        "disk_size_gb": "64",
        "serial_prefix": "",
        "group_tag": "",
        "sequence_id": str(seq_id),
    }, follow_redirects=False)
    assert r.status_code == 303

    cmd = captured["cmd"]
    assert "autopilot_enabled=true" in cmd
    assert "vm_oem_profile=lenovo-t14" in cmd
