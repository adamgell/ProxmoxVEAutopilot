"""Tests for web/onboarding_pg.py."""
from __future__ import annotations

import json

import pytest

from web import onboarding_pg


@pytest.fixture(autouse=True)
def _reset(pg_conn):
    onboarding_pg.init(pg_conn)
    onboarding_pg.reset_for_tests(pg_conn)
    onboarding_pg.init(pg_conn)
    yield


def test_init_creates_table_and_is_idempotent(pg_conn):
    onboarding_pg.init(pg_conn)  # second call must not raise
    row = pg_conn.execute(
        "SELECT to_regclass('onboarding_state') AS exists"
    ).fetchone()
    assert row["exists"] == "onboarding_state"
