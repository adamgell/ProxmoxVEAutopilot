"""Tests for /api/oem-profiles CRUD endpoints + two-tier merged view."""

import pathlib

from fastapi.testclient import TestClient


def _client(pg_conn, pg_dsn, monkeypatch, tmp_path: pathlib.Path):
    from web import oem_profiles_pg

    oem_profiles_pg.reset_for_tests(pg_conn)
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)

    yaml_text = """\
oem_profiles:
  lenovo-p520:
    manufacturer: Lenovo
    product: ThinkStation P520
    family: ThinkStation
    sku: 30BFS44D00
    chassis_type: 3
  dell-optiplex-7090:
    manufacturer: Dell Inc.
    product: OptiPlex 7090
    family: OptiPlex
    sku: 0YNTKM
    chassis_type: 3
"""
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    (files_dir / "oem_profiles.yml").write_text(yaml_text)

    import web.app as web_app

    monkeypatch.setattr(web_app, "FILES_DIR", files_dir)

    from web.app import app

    return TestClient(app)


def test_list_returns_builtin_profiles_when_no_customs(pg_conn, pg_dsn, monkeypatch, tmp_path):
    client = _client(pg_conn, pg_dsn, monkeypatch, tmp_path)

    response = client.get("/api/oem-profiles")
    assert response.status_code == 200
    profiles = response.json()["profiles"]
    keys = {row["key"] for row in profiles}
    assert keys == {"lenovo-p520", "dell-optiplex-7090"}
    assert all(row["source"] == "builtin" for row in profiles)


def test_create_custom_profile_succeeds(pg_conn, pg_dsn, monkeypatch, tmp_path):
    client = _client(pg_conn, pg_dsn, monkeypatch, tmp_path)

    response = client.post(
        "/api/oem-profiles?key=acme-thinkpad",
        json={
            "manufacturer": "Acme",
            "product": "ThinkPad Acme",
            "family": "Acme",
            "sku": "AC-1234",
            "chassis_type": 10,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["key"] == "acme-thinkpad"
    assert body["source"] == "custom"

    listed = client.get("/api/oem-profiles").json()["profiles"]
    keys = {row["key"]: row for row in listed}
    assert "acme-thinkpad" in keys
    assert keys["acme-thinkpad"]["source"] == "custom"


def test_create_rejects_invalid_chassis_type(pg_conn, pg_dsn, monkeypatch, tmp_path):
    client = _client(pg_conn, pg_dsn, monkeypatch, tmp_path)

    response = client.post(
        "/api/oem-profiles?key=bad-chassis",
        json={
            "manufacturer": "Acme",
            "product": "Bad",
            "family": "Bad",
            "sku": "X-1",
            "chassis_type": 99,
        },
    )
    assert response.status_code == 422


def test_create_rejects_invalid_key(pg_conn, pg_dsn, monkeypatch, tmp_path):
    client = _client(pg_conn, pg_dsn, monkeypatch, tmp_path)

    response = client.post(
        "/api/oem-profiles?key=BadKey%21",
        json={
            "manufacturer": "Acme",
            "product": "X",
            "family": "X",
            "sku": "X",
            "chassis_type": 3,
        },
    )
    assert response.status_code == 422


def test_create_rejects_builtin_collision_without_override(pg_conn, pg_dsn, monkeypatch, tmp_path):
    client = _client(pg_conn, pg_dsn, monkeypatch, tmp_path)

    response = client.post(
        "/api/oem-profiles?key=lenovo-p520",
        json={
            "manufacturer": "Custom Lenovo",
            "product": "P520-mod",
            "family": "ThinkStation",
            "sku": "MOD-1",
            "chassis_type": 3,
        },
    )
    assert response.status_code == 409


def test_create_override_succeeds_with_query_flag(pg_conn, pg_dsn, monkeypatch, tmp_path):
    client = _client(pg_conn, pg_dsn, monkeypatch, tmp_path)

    response = client.post(
        "/api/oem-profiles?key=lenovo-p520&override=true",
        json={
            "manufacturer": "Custom Lenovo",
            "product": "P520-mod",
            "family": "ThinkStation",
            "sku": "MOD-1",
            "chassis_type": 3,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["source"] == "override"

    detail = client.get("/api/oem-profiles/lenovo-p520").json()
    assert detail["source"] == "override"
    assert detail["product"] == "P520-mod"


def test_get_returns_builtin_when_no_custom_row(pg_conn, pg_dsn, monkeypatch, tmp_path):
    client = _client(pg_conn, pg_dsn, monkeypatch, tmp_path)

    response = client.get("/api/oem-profiles/lenovo-p520")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "builtin"
    assert body["product"] == "ThinkStation P520"


def test_get_returns_404_for_unknown(pg_conn, pg_dsn, monkeypatch, tmp_path):
    client = _client(pg_conn, pg_dsn, monkeypatch, tmp_path)

    response = client.get("/api/oem-profiles/nope")
    assert response.status_code == 404


def test_update_existing_custom_profile(pg_conn, pg_dsn, monkeypatch, tmp_path):
    client = _client(pg_conn, pg_dsn, monkeypatch, tmp_path)
    client.post(
        "/api/oem-profiles?key=acme-1",
        json={
            "manufacturer": "Acme",
            "product": "v1",
            "family": "F",
            "sku": "S",
            "chassis_type": 3,
        },
    )
    response = client.put(
        "/api/oem-profiles/acme-1",
        json={
            "manufacturer": "Acme",
            "product": "v2",
            "family": "F",
            "sku": "S",
            "chassis_type": 10,
        },
    )
    assert response.status_code == 200
    assert response.json()["product"] == "v2"
    assert response.json()["chassis_type"] == 10


def test_update_builtin_rejected_with_403(pg_conn, pg_dsn, monkeypatch, tmp_path):
    client = _client(pg_conn, pg_dsn, monkeypatch, tmp_path)
    response = client.put(
        "/api/oem-profiles/lenovo-p520",
        json={
            "manufacturer": "x",
            "product": "y",
            "family": "z",
            "sku": "s",
            "chassis_type": 3,
        },
    )
    assert response.status_code == 403


def test_delete_custom_row(pg_conn, pg_dsn, monkeypatch, tmp_path):
    client = _client(pg_conn, pg_dsn, monkeypatch, tmp_path)
    client.post(
        "/api/oem-profiles?key=acme-delete-me",
        json={
            "manufacturer": "Acme",
            "product": "x",
            "family": "F",
            "sku": "S",
            "chassis_type": 3,
        },
    )
    response = client.delete("/api/oem-profiles/acme-delete-me")
    assert response.status_code == 204

    listed = {row["key"] for row in client.get("/api/oem-profiles").json()["profiles"]}
    assert "acme-delete-me" not in listed


def test_delete_builtin_rejected_with_403(pg_conn, pg_dsn, monkeypatch, tmp_path):
    client = _client(pg_conn, pg_dsn, monkeypatch, tmp_path)
    response = client.delete("/api/oem-profiles/lenovo-p520")
    assert response.status_code == 403


def test_delete_override_restores_builtin_visibility(pg_conn, pg_dsn, monkeypatch, tmp_path):
    client = _client(pg_conn, pg_dsn, monkeypatch, tmp_path)
    client.post(
        "/api/oem-profiles?key=lenovo-p520&override=true",
        json={
            "manufacturer": "Custom Lenovo",
            "product": "P520-mod",
            "family": "ThinkStation",
            "sku": "MOD-1",
            "chassis_type": 3,
        },
    )
    response = client.delete("/api/oem-profiles/lenovo-p520")
    assert response.status_code == 204

    detail = client.get("/api/oem-profiles/lenovo-p520").json()
    assert detail["source"] == "builtin"
    assert detail["product"] == "ThinkStation P520"
