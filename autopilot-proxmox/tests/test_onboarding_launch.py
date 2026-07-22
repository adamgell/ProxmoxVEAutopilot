"""Tests for web/onboarding_launch.py."""
from __future__ import annotations

import pytest

from web import install_tracking_pg, onboarding_launch, onboarding_pg


@pytest.fixture(autouse=True)
def _reset(pg_conn):
    onboarding_pg.reset_for_tests(pg_conn)
    onboarding_pg.init(pg_conn)
    install_tracking_pg.reset_for_tests(pg_conn)
    install_tracking_pg.init(pg_conn)


def _seed_in_progress(pg_conn, owner_sub: str, *, artifact_source: str = "existing", identity_mode: str = "workgroup") -> None:
    onboarding_pg.put_state(
        pg_conn,
        owner_sub=owner_sub,
        if_match=None,
        patch={
            "persona": "lab",
            "answers": {
                "identity": {"mode": identity_mode},
                "tenant": {"skipped": True},
                "artifact": {
                    "kind": "cloudosd",
                    "source": artifact_source,
                    "existing_artifact_id": "cosd-1",
                },
                "trial": {
                    "vm_name": "autopilot-trial-9001",
                    "target_node": "pve2",
                    "os_edition": "win11-pro",
                },
            },
        },
    )


def test_launch_creates_run_and_seeds_phase_items(pg_conn, monkeypatch):
    _seed_in_progress(pg_conn, "alice@example.com")
    calls: list[str] = []

    def fake_kick(kind, run_id, payload):
        calls.append(f"{kind}:{run_id}")
        return {"job_id": "job-1"}

    monkeypatch.setattr(onboarding_launch, "_kick_provision", fake_kick)

    result = onboarding_launch.launch(pg_conn, owner_sub="alice@example.com")
    assert result["run_id"].startswith("onboarding-alice-")
    assert calls == [f"cloudosd:{result['run_id']}"]
    items = install_tracking_pg.list_run_items(pg_conn, result["run_id"])
    item_ids = {i["item_id"] for i in items}
    assert {"validate", "clone-template", "provision", "watch-oobe"} <= item_ids
    # Build phase only appears if source == 'build'; this case is 'existing'.
    assert "build-artifact" not in item_ids
    # Inject Autopilot phase only appears if identity != workgroup.
    assert "inject-autopilot" not in item_ids

    row = onboarding_pg.get_state(pg_conn, "alice@example.com")
    assert row["status"] == "launched"
    assert row["launched_run_id"] == result["run_id"]


def test_launch_includes_build_phase_when_source_is_build(pg_conn, monkeypatch):
    _seed_in_progress(pg_conn, "bob@example.com", artifact_source="build")
    monkeypatch.setattr(onboarding_launch, "_kick_provision", lambda *a, **kw: {"job_id": "j"})

    result = onboarding_launch.launch(pg_conn, owner_sub="bob@example.com")
    items = install_tracking_pg.list_run_items(pg_conn, result["run_id"])
    item_ids = {i["item_id"] for i in items}
    assert "build-artifact" in item_ids


def test_launch_includes_inject_autopilot_when_identity_is_aad(pg_conn, monkeypatch):
    _seed_in_progress(pg_conn, "carol@example.com", identity_mode="aad")
    monkeypatch.setattr(onboarding_launch, "_kick_provision", lambda *a, **kw: {"job_id": "j"})

    result = onboarding_launch.launch(pg_conn, owner_sub="carol@example.com")
    items = install_tracking_pg.list_run_items(pg_conn, result["run_id"])
    item_ids = {i["item_id"] for i in items}
    assert "inject-autopilot" in item_ids


def test_launch_rolls_back_on_kick_failure(pg_conn, monkeypatch):
    _seed_in_progress(pg_conn, "dave@example.com")

    def boom(*a, **kw):
        raise RuntimeError("provision endpoint blew up")

    monkeypatch.setattr(onboarding_launch, "_kick_provision", boom)

    with pytest.raises(RuntimeError):
        onboarding_launch.launch(pg_conn, owner_sub="dave@example.com")

    # After rollback, status stays in_progress and launched_run_id is unset.
    row = onboarding_pg.get_state(pg_conn, "dave@example.com")
    assert row["status"] == "in_progress"
    assert row["launched_run_id"] is None

    # No install_tracking_runs row should have been persisted with our onboarding prefix.
    runs = pg_conn.execute(
        "SELECT run_id FROM install_tracking_runs WHERE run_id LIKE %s",
        ("onboarding-dave-%",),
    ).fetchall()
    assert runs == []

    # And no phase items either: items are seeded with the same run_id and
    # would have an FK violation against the rolled-back run row, but assert
    # explicitly so a future change that decouples the FK still gets caught.
    items = pg_conn.execute(
        "SELECT run_id FROM install_tracking_items WHERE run_id LIKE %s",
        ("onboarding-dave-%",),
    ).fetchall()
    assert items == []


def test_launch_rejects_when_no_row(pg_conn, monkeypatch):
    monkeypatch.setattr(onboarding_launch, "_kick_provision", lambda *a, **kw: {})
    with pytest.raises(ValueError, match="no onboarding row"):
        onboarding_launch.launch(pg_conn, owner_sub="ghost@example.com")


def test_launch_rejects_when_already_launched(pg_conn, monkeypatch):
    _seed_in_progress(pg_conn, "eve@example.com")
    monkeypatch.setattr(onboarding_launch, "_kick_provision", lambda *a, **kw: {})
    onboarding_launch.launch(pg_conn, owner_sub="eve@example.com")

    with pytest.raises(ValueError, match="cannot launch from status="):
        onboarding_launch.launch(pg_conn, owner_sub="eve@example.com")
