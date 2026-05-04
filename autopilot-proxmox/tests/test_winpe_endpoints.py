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
