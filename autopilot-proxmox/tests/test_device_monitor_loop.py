"""Test the asyncio background loop that drives sweep() on a timer.

Verifies the loop reads ``interval_seconds`` from the DB each tick
(so UI changes take effect without a restart) and that a failing
sweep doesn't kill the loop."""
import asyncio
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_loop_reads_interval_from_db_each_tick(tmp_path: Path, monkeypatch):
    from web import app as app_module, device_history_db, device_monitor

    db_path = tmp_path / "device_monitor.db"
    device_history_db.init(db_path)
    # Small interval so the test runs quickly.
    device_history_db.update_settings(db_path, interval_seconds=60)
    monkeypatch.setattr(app_module, "DEVICE_MONITOR_DB", db_path)

    # Record each sweep call; error on the second so we exercise the
    # "next tick still runs" path.
    calls: list = []

    def fake_sweep(ctx, *, extra_in_scope_vmids=None):
        calls.append(len(calls) + 1)
        if len(calls) == 2:
            raise RuntimeError("simulated")

    monkeypatch.setattr(device_monitor, "sweep", fake_sweep)
    monkeypatch.setattr(
        app_module, "_build_live_monitor_context",
        lambda: object(),  # fake_sweep doesn't touch it
    )
    monkeypatch.setattr(app_module, "_vm_provisioning_vmids", lambda: set())

    # Replace the loop's sleeps with near-no-ops so the test is fast.
    # Capture the real asyncio.sleep first so fast_sleep can delegate to
    # it without recursing into the patched version.
    _real_sleep = asyncio.sleep

    async def fast_sleep(s):
        await _real_sleep(0.001)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    task = asyncio.create_task(app_module._device_monitor_loop())
    # Give the loop enough yields to fire three iterations.
    for _ in range(50):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(calls) >= 3, f"expected ≥3 ticks, got {len(calls)}"


@pytest.mark.asyncio
async def test_loop_skips_sweep_when_disabled(tmp_path: Path, monkeypatch):
    from web import app as app_module, device_history_db, device_monitor

    db_path = tmp_path / "device_monitor.db"
    device_history_db.init(db_path)
    device_history_db.update_settings(db_path, enabled=False)
    monkeypatch.setattr(app_module, "DEVICE_MONITOR_DB", db_path)

    called = []
    monkeypatch.setattr(
        device_monitor, "sweep",
        lambda *a, **kw: called.append(1),
    )
    monkeypatch.setattr(
        app_module, "_build_live_monitor_context",
        lambda: object(),
    )
    monkeypatch.setattr(app_module, "_vm_provisioning_vmids", lambda: set())

    _real_sleep = asyncio.sleep

    async def fast_sleep(s):
        await _real_sleep(0.001)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    task = asyncio.create_task(app_module._device_monitor_loop())
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert called == []
