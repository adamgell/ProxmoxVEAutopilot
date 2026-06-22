import pytest
from fastapi.testclient import TestClient


def _init_managed_labs_db(pg_dsn):
    from web import db_pg, managed_labs_pg

    with db_pg.connection(pg_dsn) as conn:
        managed_labs_pg.reset_for_tests(conn)
        managed_labs_pg.init(conn)


def _create_lab(
    client: TestClient,
    *,
    name: str = "NTT Lab",
    short_code: str = "ntt01",
    group_tag: str = "NTT-Lab",
    network_cidr: str = "10.50.20.0/24",
    gateway_ip: str = "10.50.20.1",
    sdn_zone: str = "lab-ntt01",
    sdn_vnet: str = "ntt01-vnet",
):
    response = client.post(
        "/api/labs",
        json={
            "name": name,
            "short_code": short_code,
            "group_tag": group_tag,
            "network_cidr": network_cidr,
            "gateway_ip": gateway_ip,
            "sdn_zone": sdn_zone,
            "sdn_vnet": sdn_vnet,
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
    assert response.json()["templates"][0]["id"] == "standard-hybrid-lab"


def test_create_lab_from_template_tracks_intent_and_reserves_default_names(monkeypatch, pg_dsn):
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web import app as web_app

    _init_managed_labs_db(pg_dsn)

    client = TestClient(web_app.app)
    response = client.post(
        "/api/labs",
        json={
            "template_id": "standard-hybrid-lab",
            "name": "NTT Lab",
            "short_code": "ntt01",
            "group_tag": "NTT-Lab",
            "network_cidr": "10.50.20.0/24",
            "gateway_ip": "10.50.20.1",
            "desktop_count": 2,
            "server_count": 1,
        },
    )

    assert response.status_code == 201
    lab = response.json()
    assert lab["desired_state"]["template_id"] == "standard-hybrid-lab"
    assert lab["desired_state"]["device_counts"] == {"desktop": 2, "server": 1}

    page = client.get(f"/api/labs/page?selected_lab_id={lab['id']}")
    assert page.status_code == 200
    reservations = {
        (row["reservation_type"], row["value"])
        for row in page.json()["reservations"]
    }
    assert ("hostname", "ntt01-wks-001") in reservations
    assert ("hostname", "ntt01-wks-002") in reservations
    assert ("hostname", "ntt01-srv-001") in reservations


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


def test_create_lab_page_includes_seeded_boundary_state(monkeypatch, pg_dsn):
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web import app as web_app

    _init_managed_labs_db(pg_dsn)

    client = TestClient(web_app.app)
    created = _create_lab(client)

    page = client.get(f"/api/labs/page?selected_lab_id={created['id']}")

    assert page.status_code == 200
    payload = page.json()
    assert payload["boundaries"]
    assert payload["boundary_objects"]
    assert {row["provider"] for row in payload["boundaries"]} >= {"proxmox", "ad", "entra", "intune", "deployment"}
    assert {row["kind"] for row in payload["boundary_objects"] if row["provider"] == "proxmox"} >= {
        "sdn_zone",
        "sdn_vnet",
        "sdn_subnet",
    }


def test_create_lab_rejects_invalid_network_inputs(monkeypatch, pg_dsn):
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web import app as web_app

    _init_managed_labs_db(pg_dsn)

    client = TestClient(web_app.app)
    invalid_cidr = client.post(
        "/api/labs",
        json={
            "name": "Bad CIDR Lab",
            "short_code": "bad01",
            "group_tag": "BAD-Lab",
            "network_cidr": "10.50.300.0/24",
            "gateway_ip": "10.50.20.1",
        },
    )
    invalid_gateway = client.post(
        "/api/labs",
        json={
            "name": "Bad Gateway Lab",
            "short_code": "bad02",
            "group_tag": "BAD-Gateway",
            "network_cidr": "10.50.20.0/24",
            "gateway_ip": "not-an-ip",
        },
    )

    assert invalid_cidr.status_code == 422
    assert "network_cidr" in str(invalid_cidr.json()["detail"])
    assert invalid_gateway.status_code == 422
    assert "gateway_ip" in str(invalid_gateway.json()["detail"])




def test_create_lab_populates_boundary_current_state(monkeypatch, pg_dsn):
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web import app as web_app

    _init_managed_labs_db(pg_dsn)
    monkeypatch.setattr(web_app, "_proxmox_api", lambda path, method="GET", data=None, files=None: [])

    client = TestClient(web_app.app)
    created = _create_lab(client)
    response = client.get(f"/api/labs/page?selected_lab_id={created['id']}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["boundaries"]
    assert payload["boundary_objects"]
    assert any(row["provider"] == "proxmox" for row in payload["boundaries"])
    assert {
        row["kind"]
        for row in payload["boundary_objects"]
        if row["provider"] == "proxmox"
    } >= {"sdn_zone", "sdn_vnet", "sdn_subnet"}


@pytest.mark.parametrize(
    ("field", "value", "detail_snippet"),
    [
        ("network_cidr", "10.50.20.999/24", "network_cidr"),
        ("gateway_ip", "10.50.20.999", "gateway_ip"),
    ],
)
def test_create_lab_rejects_malformed_network_inputs(monkeypatch, pg_dsn, field, value, detail_snippet):
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web import app as web_app

    _init_managed_labs_db(pg_dsn)

    client = TestClient(web_app.app)
    payload = {
        "name": "Bad Lab",
        "short_code": "bad01",
        "group_tag": "BAD-Lab",
        "network_cidr": "10.50.20.0/24",
        "gateway_ip": "10.50.20.1",
        "sdn_zone": "lab-bad01",
        "sdn_vnet": "bad01-vnet",
    }
    payload[field] = value

    response = client.post("/api/labs", json=payload)

    assert response.status_code == 422
    assert detail_snippet in response.text

def test_create_lab_rolls_back_visible_state_when_database_reservation_fails(monkeypatch, pg_dsn):
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web import app as web_app
    from web import managed_labs_pg

    _init_managed_labs_db(pg_dsn)

    original_reserve_value = managed_labs_pg.reserve_value
    reservation_calls = []

    def fail_on_cidr(conn, *, lab_id, reservation_type, value, metadata=None, commit=True):
        reservation_calls.append((reservation_type, value))
        if reservation_type == "cidr":
            conn.execute("SELECT 1 / 0")
        return original_reserve_value(
            conn,
            lab_id=lab_id,
            reservation_type=reservation_type,
            value=value,
            metadata=metadata,
            commit=commit,
        )

    monkeypatch.setattr(managed_labs_pg, "reserve_value", fail_on_cidr)

    client = TestClient(web_app.app, raise_server_exceptions=False)
    response = client.post(
        "/api/labs",
        json={
            "name": "Rollback Lab",
            "short_code": "rbk01",
            "group_tag": "RBK-Lab",
            "network_cidr": "10.50.22.0/24",
            "gateway_ip": "10.50.22.1",
            "sdn_zone": "lab-rbk01",
            "sdn_vnet": "rbk01-vnet",
        },
    )

    assert response.status_code == 500
    assert reservation_calls == [("group_tag", "RBK-Lab"), ("cidr", "10.50.22.0/24")]

    page = client.get("/api/labs/page")
    assert page.status_code == 200
    assert page.json()["labs"] == []


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



def test_run_fix_rejects_fix_from_another_lab(monkeypatch, pg_dsn):
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web import app as web_app
    from web import db_pg
    from web import managed_labs_network, managed_labs_pg

    _init_managed_labs_db(pg_dsn)
    monkeypatch.setattr(web_app, "_proxmox_api", lambda path, method="GET", data=None, files=None: [])
    monkeypatch.setattr(web_app, "_proxmox_api_put", lambda path, data=None: {"ok": True})
    monkeypatch.setattr(web_app, "_proxmox_api_delete", lambda path: {"ok": True})

    client = TestClient(web_app.app)
    lab_a = _create_lab(client)
    lab_b = _create_lab(
        client,
        name="Other Lab",
        short_code="oth01",
        group_tag="OTH-Lab",
        network_cidr="10.50.21.0/24",
        gateway_ip="10.50.21.1",
        sdn_zone="lab-oth01",
        sdn_vnet="oth01-vnet",
    )

    with db_pg.connection(pg_dsn) as conn:
        fix = managed_labs_pg.create_fix_action(
            conn,
            lab_id=lab_a["id"],
            reconcile_run_id=None,
            provider="proxmox",
            action_type="create_sdn_zone",
            priority=10,
            detail="Run create_sdn_zone",
            request={"zone": "lab-ntt01", "type": "simple"},
        )

    calls = []

    def fake_fix(conn, *, fix_action_id, pve_api, pve_put, pve_delete):
        calls.append(fix_action_id)
        return {"status": "fixed", "fix_action_id": fix_action_id}

    monkeypatch.setattr(managed_labs_network, "execute_fix_action", fake_fix)

    response = client.post(f"/api/labs/{lab_b['id']}/fixes/{fix['id']}/run")

    assert response.status_code == 404
    assert response.json()["detail"] == "fix not found"
    assert calls == []



def test_reconcile_failure_finishes_run_and_leaves_lab_retryable(monkeypatch, pg_dsn):
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web import app as web_app
    from web import db_pg, managed_labs_pg

    _init_managed_labs_db(pg_dsn)

    client = TestClient(web_app.app)
    created = _create_lab(client)

    def failing_api(path, method="GET", data=None, files=None):
        raise RuntimeError("inventory exploded")

    monkeypatch.setattr(web_app, "_proxmox_api", failing_api)

    response = client.post(f"/api/labs/{created['id']}/reconcile")

    assert response.status_code == 500

    page = client.get(f"/api/labs/page?selected_lab_id={created['id']}")
    assert page.status_code == 200
    payload = page.json()
    assert payload["selected_lab"]["status"] == "validating"
    assert payload["selected_lab"]["retry_count"] == 1
    assert payload["reconcile_runs"][0]["status"] == "failed"
    assert payload["reconcile_runs"][0]["attempt"] == 1

    with db_pg.connection(pg_dsn) as conn:
        lab = managed_labs_pg.get_lab(conn, created["id"])
        runs = managed_labs_pg.page_payload(conn, selected_lab_id=created["id"])["reconcile_runs"]

    assert lab is not None
    assert lab["status"] == "validating"
    assert lab["retry_count"] == 1
    assert runs[0]["status"] == "failed"


def test_reconcile_failure_on_fifth_attempt_blocks_lab(monkeypatch, pg_dsn):
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web import app as web_app
    from web import db_pg

    _init_managed_labs_db(pg_dsn)

    client = TestClient(web_app.app)
    created = _create_lab(client)

    with db_pg.connection(pg_dsn) as conn:
        conn.execute("UPDATE labs SET retry_count = 4 WHERE id = %s", (created["id"],))
        conn.commit()

    def failing_api(path, method="GET", data=None, files=None):
        raise RuntimeError("inventory exploded")

    monkeypatch.setattr(web_app, "_proxmox_api", failing_api)

    response = client.post(f"/api/labs/{created['id']}/reconcile")

    assert response.status_code == 500

    page = client.get(f"/api/labs/page?selected_lab_id={created['id']}")
    assert page.status_code == 200
    payload = page.json()
    assert payload["selected_lab"]["status"] == "blocked"
    assert payload["selected_lab"]["retry_count"] == 5
    assert payload["reconcile_runs"][0]["status"] == "failed"
    assert payload["reconcile_runs"][0]["attempt"] == 5
