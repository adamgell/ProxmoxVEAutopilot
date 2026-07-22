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


@pytest.mark.xfail(
    reason=(
        "web/onboarding_pg.py on main is an intentional no-op stub (see its "
        "module docstring). The onboarding_state table and init()/state store "
        "are the Postgres foundation of the operator onboarding wizard feature "
        "parked on un-merged branch claude/naughty-buck-700ffc; the test landed "
        "ahead of the implementation. Remove this marker when that branch lands."
    ),
    strict=False,
)
def test_init_creates_table_and_is_idempotent(pg_conn):
    onboarding_pg.init(pg_conn)  # second call must not raise
    row = pg_conn.execute(
        "SELECT to_regclass('onboarding_state') AS exists"
    ).fetchone()
    assert row["exists"] == "onboarding_state"
