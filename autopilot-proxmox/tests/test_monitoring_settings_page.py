"""GET /legacy/monitoring/settings smoke test — renders controls + seeded OU."""
import pytest


@pytest.fixture
def client(pg_conn):
    from fastapi.testclient import TestClient
    from web import app as app_module, device_history_pg

    device_history_pg.reset_for_tests(pg_conn)
    device_history_pg.init(pg_conn)
    with TestClient(app_module.app) as c:
        yield c


def test_settings_page_renders(client):
    r = client.get("/legacy/monitoring/settings", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/react/monitoring/settings"

    r = client.get("/api/monitoring/settings/full")
    assert r.status_code == 200
    assert set(r.json()) >= {"settings", "search_ous", "domain_creds", "keytab"}
