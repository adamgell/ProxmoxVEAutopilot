"""Tests for /api/install-tracking soft-delete endpoints + storage helpers."""

from fastapi.testclient import TestClient


def _client(pg_conn, pg_dsn, monkeypatch):
    from web import install_tracking_pg

    install_tracking_pg.reset_for_tests(pg_conn)
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web.app import app

    return TestClient(app)


def test_delete_run_requires_reason(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)
    created = client.post(
        "/api/install-tracking/runs",
        json={"name": "delete-me", "target": "vm", "source": "operator"},
    )
    assert created.status_code == 200
    run_id = created.json()["run"]["run_id"]

    response = client.request(
        "DELETE",
        f"/api/install-tracking/runs/{run_id}",
        json={"reason": ""},
    )
    assert response.status_code == 422


def test_delete_run_marks_deleted_and_cascades(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)
    created = client.post(
        "/api/install-tracking/runs",
        json={"name": "delete-me", "target": "vm", "source": "operator"},
    )
    run_id = created.json()["run"]["run_id"]

    deleted = client.request(
        "DELETE",
        f"/api/install-tracking/runs/{run_id}",
        json={"reason": "duplicate run"},
    )
    assert deleted.status_code == 200
    body = deleted.json()
    assert body["run_id"] == run_id
    assert body["deleted_at"]
    assert body["delete_reason"] == "duplicate run"

    from web import install_tracking_pg

    items = install_tracking_pg.list_run_items(pg_conn, run_id, include_deleted=True)
    assert items, "expected seeded items on the new run"
    assert all(item["deleted_at"] for item in items)


def test_list_runs_hides_deleted_by_default(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)
    created = client.post(
        "/api/install-tracking/runs",
        json={"name": "hide-me", "target": "vm", "source": "operator"},
    )
    run_id = created.json()["run"]["run_id"]
    client.request(
        "DELETE",
        f"/api/install-tracking/runs/{run_id}",
        json={"reason": "dedupe"},
    )

    listed = client.get("/api/install-tracking/runs")
    assert listed.status_code == 200
    ids = [run["run_id"] for run in listed.json()["runs"]]
    assert run_id not in ids


def test_list_runs_include_deleted_returns_soft_deleted_rows(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)
    created = client.post(
        "/api/install-tracking/runs",
        json={"name": "audit-me", "target": "vm", "source": "operator"},
    )
    run_id = created.json()["run"]["run_id"]
    client.request(
        "DELETE",
        f"/api/install-tracking/runs/{run_id}",
        json={"reason": "audit case"},
    )

    listed = client.get("/api/install-tracking/runs?include_deleted=true")
    assert listed.status_code == 200
    runs = {run["run_id"]: run for run in listed.json()["runs"]}
    assert run_id in runs
    target = runs[run_id]
    assert target["deleted_at"]
    assert target["delete_reason"] == "audit case"


def test_delete_item_marks_deleted(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)

    deleted = client.request(
        "DELETE",
        "/api/install-tracking/runs/pvetest-clean-install/items/pve-foundation",
        json={"reason": "no longer tracked"},
    )
    assert deleted.status_code == 200
    body = deleted.json()
    assert body["item_id"] == "pve-foundation"
    assert body["deleted_at"]
    assert body["delete_reason"] == "no longer tracked"


def test_delete_item_missing_returns_404(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)
    response = client.request(
        "DELETE",
        "/api/install-tracking/runs/pvetest-clean-install/items/does-not-exist",
        json={"reason": "x"},
    )
    assert response.status_code == 404


def test_delete_run_missing_returns_404(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)
    response = client.request(
        "DELETE",
        "/api/install-tracking/runs/missing-run-id",
        json={"reason": "ghost"},
    )
    assert response.status_code == 404
