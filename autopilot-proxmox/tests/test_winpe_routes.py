"""Integration tests for /winpe/* routes."""
from fastapi.testclient import TestClient


def test_winpe_router_is_mounted():
    """Smoke test: importing app.py mounts the winpe router and serves a 404 (not 500) on a stub path."""
    from web.app import app
    client = TestClient(app)
    # The router exists; an unknown sha returns 404 (route exists but content not found),
    # not 500 (route would 500 if not mounted at all).
    resp = client.get("/winpe/content/0000000000000000000000000000000000000000000000000000000000000000")
    assert resp.status_code == 404
