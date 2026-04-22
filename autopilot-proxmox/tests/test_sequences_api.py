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
    # vars.yml happens to live on the developer's machine. Root token
    # is required any time a chassis override is used (args is root-only
    # in Proxmox), so the mocked config includes it.
    with patch("web.app._load_proxmox_config", return_value={
        "proxmox_node": "pve", "proxmox_snippets_storage": "local",
        "proxmox_host": "10.0.0.1",
        "vault_proxmox_root_username": "root@pam",
        "vault_proxmox_root_password": "fake-root-pw",
    }), patch("web.proxmox_snippets.require_chassis_type_binary") as mock_require, \
         patch("web.answer_floppy_cache.ensure_floppy",
               return_value="/var/lib/vz/snippets/autopilot-unattend-deadbeefdeadbeef.img") \
                    as mock_ensure_floppy, \
         patch("web.answer_floppy_cache.make_sshpass_runner",
               return_value=lambda cmd: (0, b"", b"")), \
         patch("web.app._proxmox_root_ticket_fetch",
               return_value=("PVE:root@pam:FAKETICKET",
                             "csrf-value")) as mock_ticket:
        mock_require.return_value = "/var/lib/vz/snippets/fake.bin"
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
    mock_require.assert_called_with(node="pve", storage="local", chassis_type=31)
    # Sequence compile → per-VM answer floppy was built & wired into ansible.
    mock_ensure_floppy.assert_called_once()
    assert "_answer_floppy_path=/var/lib/vz/snippets/autopilot-unattend-deadbeefdeadbeef.img" in cmd
    # Chassis override OR any sequence → root ticket fetched once.
    mock_ticket.assert_called_once()
    assert "_proxmox_root_ticket=PVE:root@pam:FAKETICKET" in cmd
    assert "_proxmox_root_csrf_token=csrf-value" in cmd


def test_provision_rejects_chassis_override_without_root_password(app_env):
    """The args config field is root-only in Proxmox and API tokens
    can't satisfy the literal 'root@pam' eq check. If the password
    isn't configured in vault.yml, the provision must fail fast with
    a 400 pointing at docs, not queue a job that will die mid-run."""
    from web import sequences_db
    from web.app import SEQUENCES_DB

    seq_id = sequences_db.create_sequence(
        SEQUENCES_DB, name="chassis-no-root", description="",
    )
    sequences_db.set_sequence_steps(SEQUENCES_DB, seq_id, [
        {"step_type": "autopilot_entra", "params": {}, "enabled": True},
    ])

    from unittest.mock import patch
    with patch("web.app._load_proxmox_config", return_value={
        "proxmox_node": "pve", "proxmox_snippets_storage": "local",
        # Intentionally no root password.
    }), patch("web.proxmox_snippets.require_chassis_type_binary",
              return_value="/var/lib/vz/snippets/fake.bin"), \
         patch("web.answer_floppy_cache.ensure_floppy",
               return_value="/var/lib/vz/snippets/unused.img"):
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
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "root@pam" in detail
    assert "vault_proxmox_root_password" in detail


def test_provision_with_rename_computer_passes_reboot_count(app_env):
    """A sequence containing rename_computer + local_admin compiles
    causes_reboot_count=1 (rename itself runs in specialize and shares
    the specialize→OOBE reboot Windows already does; the +1 comes from
    the autologon finalizer that local_admin enables). That value must
    flow through to ansible as -e _causes_reboot_count=1 so the role's
    wait_reboot_cycle loop runs once."""
    from web import sequences_db
    from web.app import SEQUENCES_DB, job_manager

    # Need a local_admin credential so the step can reference it by id.
    from web.app import _cipher
    la_id = sequences_db.create_credential(
        SEQUENCES_DB, _cipher(), name="rename-test-admin", type="local_admin",
        payload={"username": "Admin", "password": "Pw!"},
    )

    seq_id = sequences_db.create_sequence(
        SEQUENCES_DB, name="rename-only", description="",
    )
    sequences_db.set_sequence_steps(SEQUENCES_DB, seq_id, [
        {"step_type": "local_admin",
         "params": {"credential_id": la_id}, "enabled": True},
        {"step_type": "rename_computer", "params": {}, "enabled": True},
    ])

    captured = {}
    def fake_start(name, cmd, args=None):
        captured["cmd"] = list(cmd)
        return {"id": "fake-job"}
    job_manager.start.side_effect = fake_start
    job_manager.set_arg = lambda *a, **k: None
    job_manager.add_on_complete = lambda *a, **k: None

    from unittest.mock import patch
    with patch("web.app._load_proxmox_config", return_value={
        "proxmox_node": "pve", "proxmox_snippets_storage": "local",
        "proxmox_host": "10.0.0.1",
        "vault_proxmox_root_password": "pw",
    }), patch("web.answer_floppy_cache.ensure_floppy",
              return_value="/var/lib/vz/snippets/autopilot-unattend-cafebabecafebabe.img"), \
         patch("web.answer_floppy_cache.make_sshpass_runner",
               return_value=lambda cmd: (0, b"", b"")), \
         patch("web.app._proxmox_root_ticket_fetch",
               return_value=("T", "C")):
        r = app_env.post("/api/jobs/provision", data={
            "profile": "", "count": "1", "cores": "0", "memory_mb": "0",
            "disk_size_gb": "0", "serial_prefix": "", "group_tag": "",
            "sequence_id": str(seq_id),
        }, follow_redirects=False)
    assert r.status_code == 303
    cmd = captured["cmd"]
    assert "_causes_reboot_count=1" in cmd
    assert "_answer_floppy_path=/var/lib/vz/snippets/autopilot-unattend-cafebabecafebabe.img" in cmd


def test_provision_without_sequence_skips_floppy_compile(app_env):
    """A raw provision with no sequence_id must NOT invoke the floppy
    cache (backward-compatible path — template's baked-in sata0 is
    left alone and no per-VM answer media is built)."""
    from web.app import job_manager

    captured = {}
    def fake_start(name, cmd, args=None):
        captured["cmd"] = list(cmd)
        return {"id": "fake-job"}
    job_manager.start.side_effect = fake_start
    job_manager.set_arg = lambda *a, **k: None
    job_manager.add_on_complete = lambda *a, **k: None

    from unittest.mock import patch
    with patch("web.app._load_proxmox_config", return_value={
        "proxmox_node": "pve", "proxmox_snippets_storage": "local",
    }), patch("web.answer_floppy_cache.ensure_floppy") as mock_ensure:
        # Empty profile avoids the chassis-type preflight.
        r = app_env.post("/api/jobs/provision", data={
            "profile": "", "count": "1", "cores": "0",
            "memory_mb": "0", "disk_size_gb": "0",
            "serial_prefix": "", "group_tag": "",
        }, follow_redirects=False)
    assert r.status_code == 303
    assert mock_ensure.call_count == 0
    cmd = captured["cmd"]
    assert not any(t.startswith("_answer_floppy_path=") for t in cmd)
    # _causes_reboot_count is always emitted (even as 0) so the playbook's
    # "Follow guest through N reboot(s)" task name template doesn't throw
    # on an undefined var. Emitting 0 is the explicit no-reboot signal.
    assert "_causes_reboot_count=0" in cmd


def test_proxmox_root_ticket_fetch_posts_credentials(monkeypatch):
    """The ticket fetch must POST username/password to /access/ticket
    and return the (ticket, csrf) tuple from the response."""
    from web import app as _app

    captured = {}
    class _Resp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"data": {
                "ticket": "PVE:root@pam:ABC123",
                "CSRFPreventionToken": "csrf:XYZ",
                "username": "root@pam",
            }}

    def fake_post(url, data=None, verify=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["verify"] = verify
        return _Resp()

    monkeypatch.setattr(_app.requests, "post", fake_post)
    ticket, csrf = _app._proxmox_root_ticket_fetch({
        "proxmox_host": "10.0.0.1", "proxmox_port": 8006,
        "vault_proxmox_root_username": "root@pam",
        "vault_proxmox_root_password": "topsecret",
        "proxmox_validate_certs": False,
    })
    assert ticket == "PVE:root@pam:ABC123"
    assert csrf == "csrf:XYZ"
    assert captured["url"] == "https://10.0.0.1:8006/api2/json/access/ticket"
    assert captured["data"]["username"] == "root@pam"
    assert captured["data"]["password"] == "topsecret"
    assert captured["verify"] is False


def test_proxmox_root_ticket_fetch_refuses_empty_password():
    from web import app as _app
    import pytest
    with pytest.raises(ValueError) as exc:
        _app._proxmox_root_ticket_fetch({
            "proxmox_host": "10.0.0.1",
            "vault_proxmox_root_password": "",
        })
    assert "vault_proxmox_root_password" in str(exc.value)


def test_proxmox_root_ticket_fetch_appends_pam_realm_to_bare_username(monkeypatch):
    """Operators who type 'root' into the settings field should not get a
    silent 401 from /access/ticket — Proxmox requires <user>@<realm>.
    The helper compensates by defaulting to @pam when no realm is given."""
    from web import app as _app

    captured = {}
    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"data": {"ticket": "T", "CSRFPreventionToken": "C"}}
    def fake_post(url, data=None, verify=None, timeout=None):
        captured["data"] = data
        return _Resp()
    monkeypatch.setattr(_app.requests, "post", fake_post)

    _app._proxmox_root_ticket_fetch({
        "proxmox_host": "h", "proxmox_port": 8006,
        "vault_proxmox_root_username": "root",   # bare, no realm
        "vault_proxmox_root_password": "pw",
    })
    assert captured["data"]["username"] == "root@pam"

    # Already-qualified usernames pass through unchanged.
    _app._proxmox_root_ticket_fetch({
        "proxmox_host": "h", "proxmox_port": 8006,
        "vault_proxmox_root_username": "someone@pve",
        "vault_proxmox_root_password": "pw",
    })
    assert captured["data"]["username"] == "someone@pve"


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

    # Sequence provisioning needs the Proxmox root password + floppy
    # builder wired up. Tests that exercise the provisioning path patch
    # these; this one was missing them, which is why it historically
    # 400'd.
    from unittest.mock import patch
    with patch("web.app._load_proxmox_config", return_value={
        "proxmox_node": "pve", "proxmox_snippets_storage": "local",
        "proxmox_host": "10.0.0.1",
        "vault_proxmox_root_password": "pw",
    }), patch("web.answer_floppy_cache.ensure_floppy",
              return_value="/var/lib/vz/snippets/autopilot-unattend-deadbeefdeadbeef.img"), \
         patch("web.answer_floppy_cache.make_sshpass_runner",
               return_value=lambda cmd: (0, b"", b"")), \
         patch("web.app._proxmox_root_ticket_fetch",
               return_value=("T", "C")):
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
