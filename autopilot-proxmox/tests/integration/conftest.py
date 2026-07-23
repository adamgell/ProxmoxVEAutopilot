"""Integration-suite conftest.

These tests target a LIVE HTTP instance (see test_live.py's module docstring),
not an in-process TestClient. The root tests/conftest.py has an autouse fixture
(``app_database_startup_bootstrap``) that imports ``web.app`` to stub its
database bootstrap for TestClient lifespans. That import drags in the whole
backend (psycopg, fastapi, ldap, ...), which is pointless here and forces the
CI runner to install the full backend just to talk HTTP.

Override that fixture with a no-op so the integration suite runs with only
``pytest`` + ``requests`` installed.
"""
import pytest


@pytest.fixture(autouse=True)
def app_database_startup_bootstrap():
    # No-op override of tests/conftest.py::app_database_startup_bootstrap.
    # Integration tests hit a real running server; there is nothing to
    # bootstrap in-process and web.app must not be imported here.
    yield
