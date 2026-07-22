"""Read-only projection over install_tracking for the onboarding setup monitor.

`snapshot(conn, owner_sub=...)` returns the launched run for an operator's
onboarding row, plus a sort-ordered list of its phase items. Returns None when
the operator's row has no launched_run_id yet (e.g. they haven't finished the
wizard, or Task 11's launch transaction never ran).

This module performs no writes.
"""
from __future__ import annotations

from psycopg import Connection

from web import install_tracking_pg, onboarding_pg


def snapshot(conn: Connection, *, owner_sub: str) -> dict | None:
    """Return the launched run snapshot for `owner_sub`, or None if not launched."""
    row = onboarding_pg.get_state(conn, owner_sub)
    if row is None or not row.get("launched_run_id"):
        return None
    run_id = row["launched_run_id"]
    items = install_tracking_pg.list_run_items(conn, run_id)
    phases = [
        {
            "item_id": item["item_id"],
            "label": item["label"],
            "status": item["status"],  # pending|running|ready|blocked|failed|skipped
            "detail": item.get("detail") or "",
            "sort_order": item.get("sort_order") or 0,
        }
        for item in items
    ]
    phases.sort(key=lambda p: p["sort_order"])
    return {
        "run_id": run_id,
        "phases": phases,
    }
