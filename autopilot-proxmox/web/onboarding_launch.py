"""Atomic launch transaction for the operator onboarding wizard.

Builds an install_tracking run + phase items from the wizard state and posts
to the artifact-bound provision endpoint. The run/items are written with
commit=False so that if the provision POST fails the whole transaction
rolls back; set_launched_run is the final commit point that promotes the
onboarding row to status='launched'.
"""
from __future__ import annotations

import time
from typing import Any

from psycopg import Connection

from web import install_tracking_pg, onboarding_pg


def _phases_for(answers: dict[str, Any]) -> list[dict[str, Any]]:
    """Project phase items from wizard answers. Order matters for sort_order."""
    identity = answers.get("identity") or {}
    artifact = answers.get("artifact") or {}
    items: list[dict[str, Any]] = [
        {"item_id": "validate", "label": "Validate inputs", "sort_order": 10},
    ]
    if artifact.get("source") == "build":
        items.append(
            {"item_id": "build-artifact", "label": "Build artifact", "sort_order": 20}
        )
    items.append(
        {"item_id": "clone-template", "label": "Clone template", "sort_order": 30}
    )
    if identity.get("mode") != "workgroup":
        items.append(
            {
                "item_id": "inject-autopilot",
                "label": "Inject Autopilot config",
                "sort_order": 40,
            }
        )
    items.append(
        {
            "item_id": "provision",
            "label": "Start VM and run task sequence",
            "sort_order": 50,
        }
    )
    items.append(
        {"item_id": "watch-oobe", "label": "Watch OOBE", "sort_order": 60}
    )
    return items


def _kick_provision(kind: str, run_id: str, payload: dict[str, Any]) -> dict:
    """POST to the artifact-bound provision endpoint. Overridable in tests.

    Hardcoded to localhost:8000 for now; the launch transaction is the
    artifact-discovery surface, not config-driven endpoint resolution.
    """
    import requests

    url = f"http://localhost:8000/api/{kind}/runs/{run_id}/provision"
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def launch(conn: Connection, *, owner_sub: str) -> dict:
    """Atomic launch: seed install_tracking run + phase items, kick provision,
    flip onboarding row to launched. Rolls back if the kick fails.
    """
    row = onboarding_pg.get_state(conn, owner_sub)
    if row is None:
        raise ValueError("no onboarding row to launch")
    if row["status"] != "in_progress":
        raise ValueError(f"cannot launch from status={row['status']}")
    answers = row["answers"] or {}
    kind = (answers.get("artifact") or {}).get("kind") or "cloudosd"
    trial = answers.get("trial") or {}
    target = trial.get("target_node") or ""

    # Stable run_id: onboarding-{sanitized-sub}-{unix-seconds}. The status
    # gate above blocks same-second double-launches from a single operator.
    short_sub = owner_sub.replace("@", "-").replace(".", "-")
    run_id = f"onboarding-{short_sub}-{int(time.time())}"

    # upsert_run (not create_run) because create_run generates its own
    # uuid-suffixed run_id and also seeds the legacy DEFAULT_ITEMS, which
    # would pollute our wizard run.
    install_tracking_pg.upsert_run(
        conn,
        run_id=run_id,
        name=f"Onboarding for {owner_sub}",
        target=target or "(unset)",
        status="running",
        source="onboarding_launch",
        commit=False,
    )
    for item in _phases_for(answers):
        install_tracking_pg.upsert_item(
            conn,
            run_id=run_id,
            item_id=item["item_id"],
            category="Onboarding",
            label=item["label"],
            description="",
            target=target,
            status="pending",
            detail="",
            source="onboarding_launch",
            sort_order=item["sort_order"],
            commit=False,
        )
    try:
        _kick_provision(kind, run_id, {"answers": answers})
    except Exception:
        conn.rollback()
        raise
    onboarding_pg.set_launched_run(conn, owner_sub, run_id=run_id)
    return {"run_id": run_id}
