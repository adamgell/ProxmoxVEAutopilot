"""GET /monitoring/settings smoke test — renders controls + seeded OU."""
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
    r = client.get("/monitoring/settings")
    assert r.status_code == 200
    # Controls present.
    assert 'id="enabled"' in r.text
    assert 'id="interval"' in r.text
    assert 'id="adcred"' in r.text
    # OU editor scaffolding + the client-side regex + API paths wired.
    assert "/api/monitoring/search-ous" in r.text
    assert "/api/monitoring/settings" in r.text
    assert "DN_RE" in r.text
    # Warning banner element present (hidden by default; JS adds .visible).
    assert "intervalWarn" in r.text
