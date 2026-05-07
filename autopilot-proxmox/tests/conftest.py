"""Shared pytest config + fixtures for the autopilot-proxmox test suite.

Config
------
Registers the ``integration`` marker (live-box tests) and a
``--run-integration`` CLI flag that gates their execution. Default pytest
runs skip integration tests so they never hit the box unintentionally.

Fixtures (opt-in helpers for new tests)
---------------------------------------
- ``tmp_secrets_dir``: an isolated writable secrets dir pre-seeded with a
  fresh Fernet credential_key file.
- ``test_db``: a freshly initialised Postgres sequence store (no default seeds).
- ``seeded_db``: a Postgres sequence store initialised AND seeded with defaults.
- ``web_client``: a FastAPI ``TestClient`` wired to ``test_db`` +
  ``tmp_secrets_dir``.
- ``seeded_web_client``: the same, but against the seeded DB.

The web fixtures ``monkeypatch`` ``web.app.SEQUENCES_DB``,
``web.app.SECRETS_DIR``, and ``web.app.CREDENTIAL_KEY`` so the real on-disk
paths are never touched, and reset the process-wide cipher cache
(``web.app._CIPHER``) so the isolated credential key is actually used.

Existing tests keep their own setup blocks unchanged so they continue to pass
as-is.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from contextlib import closing

# Tests never go through Entra — flip the auth bypass BEFORE web.app
# is imported so the env check in app.py picks it up.
os.environ.setdefault("AUTOPILOT_AUTH_BYPASS", "1")
os.environ.setdefault("AUTOPILOT_WINPE_TOKEN_SECRET", "test-token-secret")
os.environ.setdefault("AUTOPILOT_WINPE_IDENTITY_ALLOWLIST", "testclient,127.0.0.1")

from pathlib import Path

import pytest
from cryptography.fernet import Fernet


# --- Integration-test gating --------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run tests marked @pytest.mark.integration against a live autopilot host.",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: test that hits a live autopilot web UI "
        "(see tests/integration/, skipped unless --run-integration is passed).",
    )
    config.addinivalue_line(
        "markers",
        "real_app_database_startup: run the registered app database "
        "startup handler instead of the default test bootstrap mock.",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(reason="need --run-integration to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


# --- Fixtures -----------------------------------------------------------------

@pytest.fixture(autouse=True)
def app_database_startup_bootstrap(monkeypatch, request):
    """Let endpoint TestClient lifespans start without a live PostgreSQL DB."""
    if request.node.get_closest_marker("real_app_database_startup"):
        return

    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", "postgresql://autopilot-test")
    from web import app as web_app

    monkeypatch.setattr(web_app, "_init_app_database", lambda: None)
    monkeypatch.setattr(web_app, "_init_jobs_database", lambda: None)


@pytest.fixture(scope="session")
def pg_dsn():
    if shutil.which("docker") is None:
        pytest.skip("docker is required for PostgreSQL-backed tests")
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "-d",
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-e",
            "POSTGRES_DB=autopilot_test",
            "-p",
            "127.0.0.1::5432",
            "postgres:16-alpine",
        ],
        text=True,
    ).strip()
    try:
        port = subprocess.check_output(
            [
                "docker",
                "inspect",
                "-f",
                "{{(index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostPort}}",
                container,
            ],
            text=True,
        ).strip()
        dsn = (
            f"postgresql://postgres:postgres@127.0.0.1:{port}/"
            "autopilot_test"
        )
        import psycopg

        deadline = time.time() + 30
        while True:
            try:
                with psycopg.connect(dsn) as conn:
                    conn.execute("select 1")
                break
            except Exception:
                if time.time() > deadline:
                    logs = subprocess.run(
                        ["docker", "logs", container],
                        text=True,
                        capture_output=True,
                    ).stdout
                    raise RuntimeError(f"postgres did not start:\n{logs}")
                time.sleep(0.5)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False)


@pytest.fixture
def pg_conn(pg_dsn, monkeypatch):
    import psycopg
    from psycopg.rows import dict_row
    from web import jobs_pg

    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    with closing(psycopg.connect(pg_dsn, row_factory=dict_row)) as conn:
        jobs_pg.reset_for_tests(conn)
        jobs_pg.init(conn)
        yield conn


@pytest.fixture
def tmp_secrets_dir(tmp_path: Path) -> Path:
    """Writable secrets dir with a fresh Fernet ``credential_key``."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "credential_key").write_bytes(Fernet.generate_key())
    return secrets


@pytest.fixture
def test_db(pg_conn):
    """Fresh, initialised sequence store (no default sequences seeded)."""
    from web import sequences_pg

    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)
    return None


@pytest.fixture
def seeded_db(test_db, tmp_secrets_dir: Path):
    """Sequence store initialised + seeded with the default sequences."""
    from web import sequences_pg
    from web.crypto import Cipher

    sequences_pg.seed_defaults(
        test_db,
        Cipher(tmp_secrets_dir / "credential_key"),
    )
    return test_db


def _patch_app_paths(monkeypatch, db, secrets: Path, tmp_path: Path) -> None:
    """Redirect web.app's on-disk state at the module level."""
    import web.app as web_app

    # Reset the cached cipher so our isolated credential_key takes effect.
    web_app._CIPHER = None
    monkeypatch.setattr(web_app, "SEQUENCES_DB", db)
    monkeypatch.setattr(web_app, "SECRETS_DIR", secrets)
    monkeypatch.setattr(web_app, "CREDENTIAL_KEY", secrets / "credential_key")
    monkeypatch.setattr(web_app, "HASH_DIR", tmp_path / "hashes")


@pytest.fixture
def web_client(test_db, tmp_secrets_dir, tmp_path, monkeypatch):
    """FastAPI ``TestClient`` wired to an isolated, empty sequences DB."""
    from fastapi.testclient import TestClient

    _patch_app_paths(monkeypatch, test_db, tmp_secrets_dir, tmp_path)
    from web.app import app

    return TestClient(app)


@pytest.fixture
def seeded_web_client(seeded_db, tmp_secrets_dir, tmp_path, monkeypatch):
    """FastAPI ``TestClient`` wired to a DB pre-seeded with defaults."""
    from fastapi.testclient import TestClient

    _patch_app_paths(monkeypatch, seeded_db, tmp_secrets_dir, tmp_path)
    from web.app import app

    return TestClient(app)
