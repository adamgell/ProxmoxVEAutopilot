from __future__ import annotations


def test_winpe_identity_route_uses_postgres_compat_store(
    pg_conn,
    tmp_secrets_dir,
    tmp_path,
    monkeypatch,
):
    from fastapi.testclient import TestClient
    from web import sequences_pg
    from web import app as web_app

    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)
    web_app._CIPHER = None
    monkeypatch.setattr(web_app, "SECRETS_DIR", tmp_secrets_dir)
    monkeypatch.setattr(web_app, "CREDENTIAL_KEY", tmp_secrets_dir / "credential_key")
    monkeypatch.setattr(web_app, "HASH_DIR", tmp_path / "hashes")

    client = TestClient(web_app.app)
    response = client.post(
        "/api/sequences",
        json={
            "name": "pg-winpe",
            "description": "",
            "target_os": "windows",
            "is_default": False,
            "produces_autopilot_hash": False,
            "steps": [
                {
                    "step_type": "partition_disk",
                    "params": {},
                    "enabled": True,
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    sequence_id = response.json()["id"]
    run_id = sequences_pg.create_provisioning_run(
        None,
        sequence_id=sequence_id,
        provision_path="winpe",
    )

    response = client.post(
        f"/winpe/run/{run_id}/identity",
        json={
            "vmid": 1234,
            "vm_uuid": "ABCDEF12-3456-7890-ABCD-EF1234567890",
        },
    )

    assert response.status_code == 200, response.text
    run = sequences_pg.get_provisioning_run(None, run_id)
    assert run["vmid"] == 1234
    assert run["vm_uuid"] == "abcdef12-3456-7890-abcd-ef1234567890"
    assert run["state"] == "awaiting_winpe"
