"""Tests for /winpe/* endpoints."""
import pytest


def _create_seq(client, **overrides):
    """Helper: create a sequence via the existing API and return its id.

    The real API uses _StepIn with .params (NOT params_json) and returns
    HTTP 201. Caller may pass `steps` already in {step_type, params,
    enabled} shape; we coerce loose enabled values to bool.

    Names are globally unique per process via uuid; multiple calls in
    the same test against the same web_client must not collide on the
    UNIQUE(name) constraint.
    """
    import uuid as _uuid
    raw_steps = overrides.get("steps", [])
    steps = []
    for s in raw_steps:
        steps.append({
            "step_type": s["step_type"],
            "params": s.get("params") or s.get("params_json") and __import__("json").loads(s["params_json"]) or {},
            "enabled": bool(s.get("enabled", True)),
        })
    body = {
        "name": overrides.get("name", f"wpe-{_uuid.uuid4().hex[:8]}"),
        "description": "",
        "target_os": "windows",
        "produces_autopilot_hash": overrides.get("autopilot", False),
        "is_default": False,
        "steps": steps,
    }
    r = client.post("/api/sequences", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _create_run(db_path, sequence_id):
    from web import sequences_db
    return sequences_db.create_provisioning_run(
        db_path, sequence_id=sequence_id, provision_path="winpe",
    )


def test_post_identity_sets_vmid_and_uuid(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    r = web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234, "vm_uuid": "abc-1"},
    )
    assert r.status_code == 200, r.text
    from web import sequences_db
    run = sequences_db.get_provisioning_run(test_db, run_id)
    assert run["vmid"] == 1234
    assert run["vm_uuid"] == "abc-1"
    assert run["state"] == "awaiting_winpe"


def test_post_identity_rejects_unknown_run(web_client):
    r = web_client.post(
        "/winpe/run/99999/identity",
        json={"vmid": 1234, "vm_uuid": "abc"},
    )
    assert r.status_code == 404


def test_post_identity_is_idempotent_within_state(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    r1 = web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234, "vm_uuid": "abc-1"},
    )
    assert r1.status_code == 200
    r2 = web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234, "vm_uuid": "abc-1"},
    )
    # Already past 'queued', so identity update is a no-op success
    assert r2.status_code == 200


def test_post_identity_rejects_missing_fields(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    r = web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234},
    )
    assert r.status_code == 422


def test_register_returns_actions_and_token(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-100"},
    )
    r = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-100", "mac": "aa:bb:cc:dd:ee:ff",
              "build_sha": "deadbeef"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == run_id
    assert body["bearer_token"]
    assert isinstance(body["actions"], list)
    kinds = [a["kind"] for a in body["actions"]]
    assert "partition_disk" in kinds


def test_register_persists_steps(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-100"},
    )
    web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-100", "mac": "aa", "build_sha": "x"},
    )
    from web import sequences_db
    steps = sequences_db.list_run_steps(test_db, run_id=run_id)
    kinds = [s["kind"] for s in steps]
    assert kinds[0] == "partition_disk"
    assert all(s["state"] == "pending" for s in steps)


def test_register_reuses_only_unfinished_existing_steps(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-100"},
    )
    first = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-100", "mac": "aa", "build_sha": "x"},
    ).json()

    from web import sequences_db
    steps = sequences_db.list_run_steps(test_db, run_id=run_id)
    sequences_db.update_run_step_state(
        test_db, step_id=steps[0]["id"], state="ok",
    )

    second = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-100", "mac": "aa", "build_sha": "x"},
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert steps[0]["id"] not in [a["step_id"] for a in body["actions"]]
    assert [a["step_id"] for a in body["actions"]] == [
        a["step_id"] for a in first["actions"][1:]
    ]
    assert len(sequences_db.list_run_steps(test_db, run_id=run_id)) == len(steps)


def test_register_matches_uppercase_identity_to_lowercase_register(
    web_client, test_db,
):
    """Ansible's _vm_identity.uuid is uppercase. The agent reads SMBIOS
    via WMI in WinPE and lowercases the result. Both must reach the
    same DB row, so the layer normalizes UUIDs to lowercase on every
    write and lookup."""
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100,
              "vm_uuid": "AABBCCDD-EEFF-0011-2233-445566778899"},
    )
    r = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "aabbccdd-eeff-0011-2233-445566778899",
              "mac": "aa", "build_sha": "x"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["run_id"] == run_id


def test_register_returns_404_for_unknown_uuid(web_client):
    r = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "nope", "mac": "aa", "build_sha": "x"},
    )
    assert r.status_code == 404


def test_register_returns_409_when_state_wrong(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-100"},
    )
    from web import sequences_db
    sequences_db.update_provisioning_run_state(
        test_db, run_id=run_id, state="awaiting_specialize",
    )
    r = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-100", "mac": "aa", "build_sha": "x"},
    )
    assert r.status_code == 409


def test_register_token_is_verifiable(web_client, test_db, monkeypatch):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-100"},
    )
    body = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-100", "mac": "aa", "build_sha": "x"},
    ).json()
    from web import winpe_token
    payload = winpe_token.verify(body["bearer_token"])
    assert payload["run_id"] == run_id


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_sequence_get_returns_same_actions_after_register(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-100"},
    )
    reg = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-100", "mac": "aa", "build_sha": "x"},
    ).json()
    r = web_client.get(
        f"/winpe/sequence/{run_id}",
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 200
    actions = r.json()["actions"]
    assert [a["kind"] for a in actions] == [a["kind"] for a in reg["actions"]]
    assert [a["step_id"] for a in actions] == [a["step_id"] for a in reg["actions"]]


def test_sequence_get_rejects_missing_token(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    r = web_client.get(f"/winpe/sequence/{run_id}")
    assert r.status_code == 401


def test_sequence_get_rejects_token_for_wrong_run(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_a = _create_run(test_db, seq_id)
    run_b = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_a}/identity",
        json={"vmid": 100, "vm_uuid": "u-A"},
    )
    web_client.post(
        f"/winpe/run/{run_b}/identity",
        json={"vmid": 101, "vm_uuid": "u-B"},
    )
    reg_a = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-A", "mac": "aa", "build_sha": "x"},
    ).json()
    r = web_client.get(
        f"/winpe/sequence/{run_b}",
        headers=_bearer(reg_a["bearer_token"]),
    )
    assert r.status_code == 403


def test_autopilot_config_returns_real_file_bytes_when_enabled(
    web_client, test_db, tmp_path, monkeypatch,
):
    """The endpoint must serve the real AutopilotConfigurationFile.json
    that roles/autopilot_inject would otherwise inject via QGA, not a
    compiler-side placeholder. Operator-managed bytes flow unchanged."""
    real = tmp_path / "AutopilotConfigurationFile.json"
    real.write_bytes(
        b'{"CloudAssignedTenantId":"00000000-0000-0000-0000-000000000001",'
        b'"Version":2049}'
    )
    from web import winpe_endpoints
    monkeypatch.setattr(
        winpe_endpoints, "_resolve_autopilot_config_path",
        lambda: real,
    )
    seq_id = _create_seq(web_client, steps=[{
        "step_type": "autopilot_entra",
        "params": {}, "enabled": True, "order_index": 0,
    }], autopilot=True)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-A"},
    )
    reg = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-A", "mac": "aa", "build_sha": "x"},
    ).json()
    r = web_client.get(
        f"/winpe/autopilot-config/{run_id}",
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.content == real.read_bytes()


def test_autopilot_config_returns_404_when_not_enabled(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-A"},
    )
    reg = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-A", "mac": "aa", "build_sha": "x"},
    ).json()
    r = web_client.get(
        f"/winpe/autopilot-config/{run_id}",
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 404


def test_unattend_returns_xml_without_windowsPE(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-A"},
    )
    reg = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-A", "mac": "aa", "build_sha": "x"},
    ).json()
    r = web_client.get(
        f"/winpe/unattend/{run_id}",
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml") or \
           r.headers["content-type"].startswith("text/xml")
    assert b'pass="windowsPE"' not in r.content
    assert b'pass="specialize"' in r.content


def test_unattend_returns_404_for_unknown_run(web_client, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "test-token-secret")
    from web import winpe_token
    tok = winpe_token.sign(run_id=99999, ttl_seconds=60)
    r = web_client.get(
        "/winpe/unattend/99999",
        headers=_bearer(tok),
    )
    assert r.status_code == 404


def _register(client, db, vm_uuid="u-X"):
    seq_id = _create_seq(client)
    run_id = _create_run(db, seq_id)
    client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": vm_uuid},
    )
    reg = client.post(
        "/winpe/register",
        json={"vm_uuid": vm_uuid, "mac": "aa", "build_sha": "x"},
    ).json()
    return run_id, reg


def test_step_result_running_then_ok_records_state(web_client, test_db):
    run_id, reg = _register(web_client, test_db)
    step_id = reg["actions"][0]["step_id"]
    web_client.post(
        f"/winpe/step/{step_id}/result",
        json={"state": "running"},
        headers=_bearer(reg["bearer_token"]),
    )
    r = web_client.post(
        f"/winpe/step/{step_id}/result",
        json={"state": "ok", "stdout_tail": "done", "elapsed_seconds": 12},
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert "bearer_token" in body
    from web import sequences_db
    s = sequences_db.get_run_step(test_db, step_id)
    assert s["state"] == "ok"
    assert s["started_at"] is not None
    assert s["finished_at"] is not None


def test_step_result_error_marks_run_failed(web_client, test_db):
    run_id, reg = _register(web_client, test_db)
    step_id = reg["actions"][0]["step_id"]
    web_client.post(
        f"/winpe/step/{step_id}/result",
        json={"state": "error", "error": "disk too small"},
        headers=_bearer(reg["bearer_token"]),
    )
    from web import sequences_db
    run = sequences_db.get_provisioning_run(test_db, run_id)
    assert run["state"] == "failed"
    assert "disk too small" in (run["last_error"] or "")


def test_step_result_token_refresh_is_verifiable(web_client, test_db):
    run_id, reg = _register(web_client, test_db)
    step_id = reg["actions"][0]["step_id"]
    r = web_client.post(
        f"/winpe/step/{step_id}/result",
        json={"state": "running"},
        headers=_bearer(reg["bearer_token"]),
    )
    new_tok = r.json()["bearer_token"]
    from web import winpe_token
    assert winpe_token.verify(new_tok)["run_id"] == run_id


def test_step_result_rejects_step_in_different_run(web_client, test_db):
    run_a, reg_a = _register(web_client, test_db, vm_uuid="u-A")
    run_b, reg_b = _register(web_client, test_db, vm_uuid="u-B")
    step_in_b = reg_b["actions"][0]["step_id"]
    r = web_client.post(
        f"/winpe/step/{step_in_b}/result",
        json={"state": "ok"},
        headers=_bearer(reg_a["bearer_token"]),
    )
    assert r.status_code == 403


def test_done_advances_run_state_and_calls_proxmox(web_client, test_db, monkeypatch):
    calls = []
    power_cycles = []

    def fake_detach(*, vmid, slots, set_boot_order):
        calls.append({"vmid": vmid, "slots": list(slots),
                      "boot": set_boot_order})

    from web import winpe_endpoints
    monkeypatch.setattr(winpe_endpoints, "_proxmox_detach_and_set_boot",
                        fake_detach)
    monkeypatch.setattr(
        winpe_endpoints, "_proxmox_power_cycle_for_pending_config",
        lambda **kw: power_cycles.append(kw),
    )
    run_id, reg = _register(web_client, test_db)
    r = web_client.post(
        "/winpe/done",
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 200
    from web import sequences_db
    run = sequences_db.get_provisioning_run(test_db, run_id)
    assert run["state"] == "awaiting_specialize"
    assert calls == [{"vmid": 100, "slots": ["ide2", "sata0"],
                      "boot": "order=scsi0"}]
    assert power_cycles == [{"vmid": 100}]


def test_done_uses_sata_handoff_boot_order_for_winpe_safe_disk(
    web_client, test_db, monkeypatch,
):
    calls = []

    from web import winpe_endpoints

    monkeypatch.setattr(
        winpe_endpoints, "_proxmox_handoff_boot_order",
        lambda *, vmid: "order=sata1",
    )
    monkeypatch.setattr(
        winpe_endpoints, "_proxmox_detach_and_set_boot",
        lambda **kw: calls.append(kw),
    )
    monkeypatch.setattr(
        winpe_endpoints, "_proxmox_power_cycle_for_pending_config",
        lambda **kw: None,
    )
    run_id, reg = _register(web_client, test_db)
    r = web_client.post(
        "/winpe/done",
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 200
    assert calls[0]["set_boot_order"] == "order=sata1"


def test_done_is_idempotent(web_client, test_db, monkeypatch):
    from web import winpe_endpoints
    monkeypatch.setattr(winpe_endpoints, "_proxmox_detach_and_set_boot",
                        lambda **kw: None)
    monkeypatch.setattr(
        winpe_endpoints, "_proxmox_power_cycle_for_pending_config",
        lambda **kw: None,
    )
    run_id, reg = _register(web_client, test_db)
    web_client.post("/winpe/done", headers=_bearer(reg["bearer_token"]))
    r = web_client.post("/winpe/done", headers=_bearer(reg["bearer_token"]))
    # Already past awaiting_winpe; second call must not error
    assert r.status_code == 200


def test_api_run_returns_state_and_steps(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234, "vm_uuid": "u-1"},
    )
    web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-1", "mac": "aa", "build_sha": "x"},
    )
    r = web_client.get(f"/api/runs/{run_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == run_id
    assert body["state"] == "awaiting_winpe"
    assert isinstance(body["steps"], list)
    assert body["steps"][0]["kind"] == "partition_disk"


def test_api_run_returns_404_for_unknown(web_client):
    r = web_client.get("/api/runs/99999")
    assert r.status_code == 404


def test_api_run_fail_marks_run_failed(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234, "vm_uuid": "u-1"},
    )
    r = web_client.post(
        f"/api/runs/{run_id}/fail",
        json={"reason": "controller timeout 1800s"},
    )
    assert r.status_code == 200
    from web import sequences_db
    run = sequences_db.get_provisioning_run(test_db, run_id)
    assert run["state"] == "failed"
    assert "controller timeout" in (run["last_error"] or "")


def test_api_run_fail_is_idempotent_on_terminal_state(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    from web import sequences_db
    sequences_db.update_provisioning_run_state(
        test_db, run_id=run_id, state="done",
    )
    r = web_client.post(
        f"/api/runs/{run_id}/fail", json={"reason": "x"},
    )
    assert r.status_code == 200
    run = sequences_db.get_provisioning_run(test_db, run_id)
    assert run["state"] == "done"


def test_auth_exempts_winpe_machine_callbacks():
    from web import auth

    for path in (
        "/winpe/run/1/identity",
        "/winpe/register",
        "/winpe/step/1/result",
        "/winpe/done",
        "/api/runs/1",
        "/api/runs/1/fail",
        "/api/runs/1/complete",
    ):
        assert auth.is_exempt_path(path)


def test_provision_post_with_boot_mode_winpe_creates_run(
    web_client, test_db, monkeypatch,
):
    """POST /api/jobs/provision with boot_mode=winpe creates a
    provisioning_runs row, skips the answer-floppy build, and launches
    provision_proxmox_winpe.yml."""
    monkeypatch.setenv("AUTOPILOT_WINPE_BLANK_TEMPLATE_VMID", "9001")
    monkeypatch.setenv("AUTOPILOT_WINPE_ISO", "isos:iso/winpe-test.iso")

    seq_id = _create_seq(web_client)

    launches = []
    from web import app as web_app

    def fake_run_playbook(playbook_path, extra_vars=None, **_):
        launches.append({"playbook": playbook_path,
                         "extra_vars": dict(extra_vars or {})})
        return {"job_id": 1}

    monkeypatch.setattr(
        web_app, "_launch_provision_job", fake_run_playbook, raising=False,
    )

    r = web_client.post(
        "/api/jobs/provision",
        data={
            "profile": "generic-desktop",
            "count": 1,
            "sequence_id": seq_id,
            "boot_mode": "winpe",
        },
    )
    assert r.status_code == 200, r.text

    import sqlite3
    with sqlite3.connect(test_db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM provisioning_runs "
            "WHERE provision_path='winpe' AND state='queued'"
        ).fetchone()[0]
    assert n == 1
    assert any(
        str(l["playbook"]).endswith("provision_proxmox_winpe.yml")
        for l in launches
    )
    assert launches[0]["extra_vars"]["_skip_chassis_type_smbios_file"] is True


def test_provision_post_winpe_rejected_when_not_configured(
    web_client, monkeypatch,
):
    monkeypatch.delenv("AUTOPILOT_WINPE_BLANK_TEMPLATE_VMID", raising=False)
    monkeypatch.delenv("AUTOPILOT_WINPE_ISO", raising=False)
    seq_id = _create_seq(web_client)
    r = web_client.post(
        "/api/jobs/provision",
        data={
            "profile": "generic-desktop", "count": 1,
            "sequence_id": seq_id, "boot_mode": "winpe",
        },
    )
    assert r.status_code == 400
    assert b"WinPE" in r.content


def test_provision_post_winpe_rejected_when_token_secret_missing(
    web_client, monkeypatch,
):
    monkeypatch.setenv("AUTOPILOT_WINPE_BLANK_TEMPLATE_VMID", "9001")
    monkeypatch.setenv("AUTOPILOT_WINPE_ISO", "isos:iso/winpe-test.iso")
    monkeypatch.delenv("AUTOPILOT_WINPE_TOKEN_SECRET", raising=False)
    seq_id = _create_seq(web_client)
    r = web_client.post(
        "/api/jobs/provision",
        data={
            "profile": "generic-desktop", "count": 1,
            "sequence_id": seq_id, "boot_mode": "winpe",
        },
    )
    assert r.status_code == 400
    assert b"WinPE" in r.content


def test_provision_page_renders_winpe_option_when_configured(
    web_client, monkeypatch,
):
    monkeypatch.setenv("AUTOPILOT_WINPE_BLANK_TEMPLATE_VMID", "9001")
    monkeypatch.setenv("AUTOPILOT_WINPE_ISO", "isos:iso/x.iso")
    r = web_client.get("/provision")
    assert r.status_code == 200
    assert b'name="boot_mode"' in r.content


def test_run_detail_page_renders(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234, "vm_uuid": "u-1"},
    )
    web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-1", "mac": "aa", "build_sha": "x"},
    )
    r = web_client.get(f"/runs/{run_id}")
    assert r.status_code == 200
    assert b"partition_disk" in r.content
    assert b"awaiting_winpe" in r.content


def test_run_detail_404_when_unknown(web_client):
    r = web_client.get("/runs/99999")
    assert r.status_code == 404


def test_post_run_complete_advances_to_done(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234, "vm_uuid": "u-1"},
    )
    from web import sequences_db
    sequences_db.update_provisioning_run_state(
        test_db, run_id=run_id, state="awaiting_specialize",
    )
    r = web_client.post(f"/api/runs/{run_id}/complete")
    assert r.status_code == 200
    run = sequences_db.get_provisioning_run(test_db, run_id)
    assert run["state"] == "done"


def test_post_hash_writes_csv_into_hash_dir(
    web_client, test_db, tmp_path, monkeypatch,
):
    """The endpoint persists by writing a CSV file into HASH_DIR using
    the same column shape get_hash_files / hash_capture role produce.
    Existing parser (web.app.get_hash_files at line 1794) picks it up
    without code changes."""
    from web import app as web_app
    monkeypatch.setattr(web_app, "HASH_DIR", tmp_path, raising=True)

    run_id, reg = _register(web_client, test_db)
    r = web_client.post(
        "/winpe/hash",
        json={
            "serial_number": "S1", "product_id": "PK1",
            "hardware_hash": "HH1",
        },
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 200

    csvs = list(tmp_path.glob("*.csv"))
    assert len(csvs) == 1
    text = csvs[0].read_text()
    # Match the columns roles/hash_capture/files/Get-WindowsAutopilotInfo
    # emits ("Device Serial Number,Windows Product ID,Hardware Hash"),
    # which app.py's parser already understands.
    assert "Device Serial Number" in text
    assert "Hardware Hash" in text
    assert "S1" in text
    assert "HH1" in text


def test_post_hash_requires_bearer(web_client):
    r = web_client.post(
        "/winpe/hash",
        json={"serial_number": "S", "product_id": "P", "hardware_hash": "H"},
    )
    assert r.status_code == 401
