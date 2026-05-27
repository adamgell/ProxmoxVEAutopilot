"""Tests for web/onboarding_phases.py."""
from __future__ import annotations

import pytest

from web import install_tracking_pg, onboarding_pg, onboarding_phases


@pytest.fixture(autouse=True)
def _reset(pg_conn):
    onboarding_pg.reset_for_tests(pg_conn)
    onboarding_pg.init(pg_conn)
    install_tracking_pg.init(pg_conn)


def test_snapshot_returns_phase_list_for_owner(pg_conn):
    onboarding_pg.put_state(
        pg_conn,
        owner_sub="bob@example.com",
        if_match=None,
        patch={"persona": "msp"},
    )
    install_tracking_pg.upsert_run(
        pg_conn,
        run_id="onboarding-bob-1",
        name="t",
        target="pve2",
        source="test",
        status="running",
        commit=False,
    )
    for sort_order, item_id, status in [(10, "validate", "ready"), (50, "provision", "running")]:
        install_tracking_pg.upsert_item(
            pg_conn,
            run_id="onboarding-bob-1",
            item_id=item_id,
            category="Onboarding",
            label=item_id,
            description="",
            target="pve2",
            status=status,
            detail="",
            source="test",
            sort_order=sort_order,
            commit=False,
        )
    onboarding_pg.set_launched_run(pg_conn, "bob@example.com", run_id="onboarding-bob-1")

    snap = onboarding_phases.snapshot(pg_conn, owner_sub="bob@example.com")
    assert snap["run_id"] == "onboarding-bob-1"
    statuses = {p["item_id"]: p["status"] for p in snap["phases"]}
    assert statuses == {"validate": "ready", "provision": "running"}


def test_snapshot_returns_none_if_not_launched(pg_conn):
    onboarding_pg.put_state(
        pg_conn,
        owner_sub="carol@example.com",
        if_match=None,
        patch={"persona": "lab"},
    )
    assert onboarding_phases.snapshot(pg_conn, owner_sub="carol@example.com") is None


def test_snapshot_orders_phases_by_sort_order(pg_conn):
    onboarding_pg.put_state(
        pg_conn,
        owner_sub="dave@example.com",
        if_match=None,
        patch={"persona": "lab"},
    )
    install_tracking_pg.upsert_run(
        pg_conn,
        run_id="onboarding-dave-1",
        name="t",
        target="pve2",
        source="test",
        status="running",
        commit=False,
    )
    # Insert items out of sort_order on purpose: 50 first, then 10, then 30.
    for sort_order, item_id in [(50, "third"), (10, "first"), (30, "second")]:
        install_tracking_pg.upsert_item(
            pg_conn,
            run_id="onboarding-dave-1",
            item_id=item_id,
            category="Onboarding",
            label=item_id,
            description="",
            target="pve2",
            status="pending",
            detail="",
            source="test",
            sort_order=sort_order,
            commit=False,
        )
    onboarding_pg.set_launched_run(pg_conn, "dave@example.com", run_id="onboarding-dave-1")

    snap = onboarding_phases.snapshot(pg_conn, owner_sub="dave@example.com")
    assert [p["item_id"] for p in snap["phases"]] == ["first", "second", "third"]
    assert [p["sort_order"] for p in snap["phases"]] == [10, 30, 50]
