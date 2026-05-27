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


def test_get_state_returns_none_when_no_row(pg_conn):
    assert onboarding_pg.get_state(pg_conn, "alice@example.com") is None


def test_put_state_creates_then_returns_etag(pg_conn):
    row = onboarding_pg.put_state(
        pg_conn,
        owner_sub="alice@example.com",
        if_match=None,
        patch={"persona": "lab", "current_step": "identity"},
    )
    assert row["owner_sub"] == "alice@example.com"
    assert row["persona"] == "lab"
    assert row["current_step"] == "identity"
    assert row["status"] == "in_progress"
    assert row["etag"]  # weak ETag derived from updated_at


def test_put_state_rejects_stale_if_match(pg_conn):
    first = onboarding_pg.put_state(
        pg_conn, owner_sub="bob@example.com", if_match=None, patch={"persona": "lab"}
    )
    onboarding_pg.put_state(
        pg_conn, owner_sub="bob@example.com", if_match=first["etag"], patch={"persona": "msp"}
    )
    with pytest.raises(onboarding_pg.StaleEtag):
        onboarding_pg.put_state(
            pg_conn, owner_sub="bob@example.com", if_match=first["etag"], patch={"persona": "corp"}
        )


def test_put_state_rejects_invalid_status(pg_conn):
    with pytest.raises(ValueError):
        onboarding_pg.put_state(
            pg_conn, owner_sub="bob@example.com", if_match=None, patch={"status": "purple"}
        )


def test_put_state_rejects_invalid_persona(pg_conn):
    with pytest.raises(ValueError):
        onboarding_pg.put_state(
            pg_conn, owner_sub="bob@example.com", if_match=None, patch={"persona": "weekend warrior"}
        )


def test_delete_state_removes_row(pg_conn):
    onboarding_pg.put_state(
        pg_conn, owner_sub="carol@example.com", if_match=None, patch={"persona": "lab"}
    )
    onboarding_pg.delete_state(pg_conn, "carol@example.com")
    assert onboarding_pg.get_state(pg_conn, "carol@example.com") is None


def test_set_launched_run_records_id_and_flips_status(pg_conn):
    onboarding_pg.put_state(
        pg_conn, owner_sub="dan@example.com", if_match=None, patch={"persona": "msp"}
    )
    onboarding_pg.set_launched_run(pg_conn, "dan@example.com", run_id="onboarding-dan-1")
    row = onboarding_pg.get_state(pg_conn, "dan@example.com")
    assert row["launched_run_id"] == "onboarding-dan-1"
    assert row["status"] == "launched"
