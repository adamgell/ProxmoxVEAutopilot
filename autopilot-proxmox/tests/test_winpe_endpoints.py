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
