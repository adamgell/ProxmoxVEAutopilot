from fastapi.testclient import TestClient


def _init_managed_labs_db(pg_dsn):
    from web import db_pg, managed_labs_pg

    with db_pg.connection(pg_dsn) as conn:
        managed_labs_pg.reset_for_tests(conn)
        managed_labs_pg.init(conn)


def _create_lab(client: TestClient):
    response = client.post(
        "/api/labs",
        json={
            "name": "NTT Lab",
            "short_code": "ntt01",
            "group_tag": "NTT-Lab",
            "network_cidr": "10.50.20.0/24",
            "gateway_ip": "10.50.20.1",
            "sdn_zone": "lab-ntt01",
            "sdn_vnet": "ntt01-vnet",
        },
    )
    assert response.status_code == 201
    return response.json()


def test_labs_page_starts_empty(monkeypatch, pg_dsn):
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web import app as web_app
    from web import db_pg

    _init_managed_labs_db(pg_dsn)

    client = TestClient(web_app.app)
    response = client.get("/api/labs/page")

    assert response.status_code == 200
    assert response.json()["labs"] == []


def test_create_lab_then_reconcile_plans_sdn_fixes(monkeypatch, pg_dsn):
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web import app as web_app

    _init_managed_labs_db(pg_dsn)
    monkeypatch.setattr(web_app, "_proxmox_api", lambda path, method="GET", data=None, files=None: [])

    client = TestClient(web_app.app)
    created = _create_lab(client)

    listed = client.get("/api/labs")
    assert listed.status_code == 200
    assert [lab["id"] for lab in listed.json()] == [created["id"]]

    loaded = client.get(f"/api/labs/{created['id']}")
    assert loaded.status_code == 200
    assert loaded.json()["id"] == created["id"]
    assert loaded.json()["group_tag"] == "NTT-Lab"

    result = client.post(f"/api/labs/{created['id']}/reconcile")

    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "fixing"
    assert [fix["action_type"] for fix in body["fix_actions"]] == [
        "create_sdn_zone",
        "create_sdn_vnet",
        "create_sdn_subnet",
        "apply_sdn",
    ]


def test_fix_endpoints_delegate_to_managed_labs_network(monkeypatch, pg_dsn):
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web import app as web_app
    from web import db_pg
    from web import managed_labs_network, managed_labs_pg

    _init_managed_labs_db(pg_dsn)
    monkeypatch.setattr(web_app, "_proxmox_api", lambda path, method="GET", data=None, files=None: [])
    monkeypatch.setattr(web_app, "_proxmox_api_put", lambda path, data=None: {"ok": True})
    monkeypatch.setattr(web_app, "_proxmox_api_delete", lambda path: {"ok": True})

    client = TestClient(web_app.app)
    created = _create_lab(client)

    with db_pg.connection(pg_dsn) as conn:
        fix = managed_labs_pg.create_fix_action(
            conn,
            lab_id=created["id"],
            reconcile_run_id=None,
            provider="proxmox",
            action_type="create_sdn_zone",
            priority=10,
            detail="Run create_sdn_zone",
            request={"zone": "lab-ntt01", "type": "simple"},
        )

    pending_calls = []
    fix_calls = []

    def fake_pending(conn, *, lab_id, pve_api, pve_put, pve_delete):
        pending_calls.append(
            {
                "lab_id": lab_id,
                "pve_api": pve_api,
                "pve_put": pve_put,
                "pve_delete": pve_delete,
            }
        )
        return {"fixed": [{"id": "fixed-1"}], "blocked": [], "failed": []}

    def fake_fix(conn, *, fix_action_id, pve_api, pve_put, pve_delete):
        fix_calls.append(
            {
                "fix_action_id": fix_action_id,
                "pve_api": pve_api,
                "pve_put": pve_put,
                "pve_delete": pve_delete,
            }
        )
        return {"status": "fixed", "fix_action_id": fix_action_id}

    monkeypatch.setattr(managed_labs_network, "execute_pending_network_fixes", fake_pending)
    monkeypatch.setattr(managed_labs_network, "execute_fix_action", fake_fix)

    pending = client.post(f"/api/labs/{created['id']}/fixes/run-pending")
    single = client.post(f"/api/labs/{created['id']}/fixes/{fix['id']}/run")

    assert pending.status_code == 200
    assert pending.json() == {"fixed": [{"id": "fixed-1"}], "blocked": [], "failed": []}
    assert single.status_code == 200
    assert single.json() == {"status": "fixed", "fix_action_id": fix["id"]}
    assert pending_calls[0]["lab_id"] == created["id"]
    assert fix_calls[0]["fix_action_id"] == fix["id"]
