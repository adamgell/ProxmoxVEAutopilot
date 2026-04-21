"""GET /monitoring/settings smoke test — renders controls + seeded OU."""
from pathlib import Path
import pytest


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    from web import app as app_module, device_history_db
    db_path = tmp_path / "device_monitor.db"
    monkeypatch.setattr(app_module, "DEVICE_MONITOR_DB", db_path)
    device_history_db.init(db_path)
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
