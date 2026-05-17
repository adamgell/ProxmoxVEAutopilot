"""PostgreSQL install/init tracking for the operator console."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


VALID_STATUSES = {"pending", "running", "ready", "blocked", "failed", "skipped"}

DEFAULT_ITEMS: tuple[dict[str, Any], ...] = (
    {
        "item_id": "pve-foundation",
        "category": "Proxmox host",
        "label": "Run one-shot Proxmox bootstrap",
        "description": "Install host prerequisites, clone/update the repo, write vault/vars/.env, and start the compose stack.",
        "target": "pvetest",
        "status": "running",
        "detail": "Track the clean install from the Proxmox shell bootstrap path.",
        "source": "019e28b1-38ff-7a53-8047-7d1426e96185",
        "sort_order": 10,
    },
    {
        "item_id": "controller-stack",
        "category": "Controller",
        "label": "Operator console containers healthy",
        "description": "autopilot, autopilot-postgres, monitor, builder, and MCP sidecar are reachable after init.",
        "target": "pvetest",
        "status": "running",
        "detail": "Use compose health and /healthz as the proof point.",
        "source": "skill.sh status",
        "sort_order": 20,
    },
    {
        "item_id": "mcp-docs-bridge",
        "category": "Controller",
        "label": "MCP docs bridge reachable",
        "description": "Local helper can reach the sidecar, list MCP tools, and index repo docs.",
        "target": "pvetest",
        "status": "ready",
        "detail": "MCP merged to main with 108 tools and 66 docs in the latest smoke.",
        "source": "skill.sh status",
        "sort_order": 30,
    },
    {
        "item_id": "media-discovery",
        "category": "Media",
        "label": "Live PVE media discovery verified",
        "description": "Init/provisioning discovers the current VirtIO ISO from live Proxmox storage instead of trusting stale vars.",
        "target": "pvetest",
        "status": "ready",
        "detail": "Recovered stale isos:iso/virtio-win-0.1.285.iso to local:iso/virtio-win.iso.",
        "source": "019e28b1-38ff-7a53-8047-7d1426e96185",
        "sort_order": 40,
        "evidence": {"virtio_iso": "local:iso/virtio-win.iso"},
    },
    {
        "item_id": "windows-build-box",
        "category": "Build box",
        "label": "Windows build-box responsibilities documented",
        "description": "WinPE, Windows media, drivers, and OSDeploy artifacts stay separate from the Proxmox bootstrap path.",
        "target": "windows-dev-box",
        "status": "pending",
        "detail": "Use this item to track whether the build box has seeded current artifacts for pvetest.",
        "source": "docs/WINDOWS_BUILD_BOX.md",
        "sort_order": 50,
    },
    {
        "item_id": "osdeploy-artifact",
        "category": "OSDeploy",
        "label": "OSDeploy artifact published to PVE",
        "description": "A generated OSDeploy ISO is present on the target Proxmox storage.",
        "target": "pvetest",
        "status": "ready",
        "detail": "Last known good artifact published as local:iso/osdeploy-server-amd64-20260517022555.iso.",
        "source": "019e28b1-38ff-7a53-8047-7d1426e96185",
        "sort_order": 60,
        "evidence": {
            "artifact_id": "9609c64f-e0c2-4100-8361-460a70640a92",
            "proxmox_volid": "local:iso/osdeploy-server-amd64-20260517022555.iso",
        },
    },
    {
        "item_id": "osdeploy-e2e-run",
        "category": "OSDeploy",
        "label": "Clean OSDeploy run completes",
        "description": "Fresh pvetest VM reaches the complete state after the clean install/init path.",
        "target": "VMID 106",
        "status": "ready",
        "detail": "OSDEPLOY-E2E-007 completed after stale media recovery.",
        "source": "019e28b1-38ff-7a53-8047-7d1426e96185",
        "sort_order": 70,
        "evidence": {
            "run_id": "d6376517-2306-49ea-bfbe-228ed6cb499a",
            "vmid": 106,
        },
    },
    {
        "item_id": "agent-readiness",
        "category": "Readiness",
        "label": "Agent and QGA readiness confirmed",
        "description": "The deployed VM reports agent heartbeat and guest-agent readiness.",
        "target": "VMID 106",
        "status": "ready",
        "detail": "Last known good readiness: QGA running, agent online, server role base_ready.",
        "source": "019e28b1-38ff-7a53-8047-7d1426e96185",
        "sort_order": 80,
        "evidence": {
            "qga_status": "running",
            "agent_status": "online",
            "server_role_status": "base_ready",
            "heartbeat_at": "2026-05-17T03:22:16Z",
        },
    },
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS install_tracking_items (
    item_id text PRIMARY KEY,
    category text NOT NULL,
    label text NOT NULL,
    description text NOT NULL DEFAULT '',
    target text NOT NULL DEFAULT '',
    status text NOT NULL,
    detail text NOT NULL DEFAULT '',
    evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    source text NOT NULL DEFAULT '',
    sort_order integer NOT NULL DEFAULT 1000,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (status IN ('pending', 'running', 'ready', 'blocked', 'failed', 'skipped'))
);

CREATE TABLE IF NOT EXISTS install_tracking_events (
    event_id bigserial PRIMARY KEY,
    item_id text NOT NULL REFERENCES install_tracking_items(item_id) ON DELETE CASCADE,
    status text NOT NULL,
    detail text NOT NULL DEFAULT '',
    evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    source text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL,
    CHECK (status IN ('pending', 'running', 'ready', 'blocked', 'failed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS idx_install_tracking_items_status
    ON install_tracking_items(status, sort_order);
CREATE INDEX IF NOT EXISTS idx_install_tracking_events_item_time
    ON install_tracking_events(item_id, created_at DESC);
"""

DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS install_tracking_events CASCADE;
DROP TABLE IF EXISTS install_tracking_items CASCADE;
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _clean_text(value: Any, *, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def _clean_status(value: Any) -> str:
    status = str(value or "pending").strip().lower()
    if status not in VALID_STATUSES:
        raise ValueError(f"unsupported install tracking status: {status!r}")
    return status


def _sanitize_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in ("token", "secret", "password", "authorization")):
                out[str(key)] = "[redacted]"
            else:
                out[str(key)] = _sanitize_evidence(child)
        return out
    if isinstance(value, list):
        return [_sanitize_evidence(item) for item in value]
    return value


def _row_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["created_at"] = _iso(out.get("created_at"))
    out["updated_at"] = _iso(out.get("updated_at"))
    out["evidence"] = out.pop("evidence_json") or {}
    return out


def init(conn: Connection | None = None) -> None:
    own = conn is None
    conn = conn or db_pg.connect()
    try:
        conn.execute(SCHEMA)
        seed_defaults(conn, commit=False)
        conn.commit()
    finally:
        if own:
            conn.close()


def reset_for_tests(conn: Connection) -> None:
    conn.execute(DROP_SCHEMA_FOR_TESTS)
    conn.commit()


def seed_defaults(conn: Connection, *, commit: bool = True) -> None:
    now = _now()
    for item in DEFAULT_ITEMS:
        conn.execute(
            """
            INSERT INTO install_tracking_items (
                item_id, category, label, description, target, status,
                detail, evidence_json, source, sort_order, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (item_id) DO NOTHING
            """,
            (
                item["item_id"],
                item["category"],
                item["label"],
                item.get("description", ""),
                item.get("target", ""),
                _clean_status(item.get("status")),
                item.get("detail", ""),
                Jsonb(_sanitize_evidence(item.get("evidence") or {})),
                item.get("source", ""),
                int(item.get("sort_order") or 1000),
                now,
                now,
            ),
        )
    if commit:
        conn.commit()


def list_items(conn: Connection) -> list[dict]:
    init(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM install_tracking_items
        ORDER BY sort_order ASC, category ASC, label ASC
        """
    ).fetchall()
    return [_row_dict(row) for row in rows]


def get_item(conn: Connection, item_id: str) -> dict | None:
    init(conn)
    row = conn.execute(
        "SELECT * FROM install_tracking_items WHERE item_id = %s",
        (item_id,),
    ).fetchone()
    return _row_dict(row)


def upsert_item(
    conn: Connection,
    *,
    item_id: str,
    category: str,
    label: str,
    description: str = "",
    target: str = "",
    status: str = "pending",
    detail: str = "",
    evidence: dict | None = None,
    source: str = "",
    sort_order: int = 1000,
    commit: bool = True,
) -> dict:
    clean_id = _clean_text(item_id, limit=120)
    if not clean_id:
        raise ValueError("item_id is required")
    clean_status = _clean_status(status)
    clean_evidence = _sanitize_evidence(evidence or {})
    now = _now()
    row = conn.execute(
        """
        INSERT INTO install_tracking_items (
            item_id, category, label, description, target, status, detail,
            evidence_json, source, sort_order, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (item_id) DO UPDATE SET
            category = EXCLUDED.category,
            label = EXCLUDED.label,
            description = EXCLUDED.description,
            target = EXCLUDED.target,
            status = EXCLUDED.status,
            detail = EXCLUDED.detail,
            evidence_json = EXCLUDED.evidence_json,
            source = EXCLUDED.source,
            sort_order = EXCLUDED.sort_order,
            updated_at = EXCLUDED.updated_at
        RETURNING *
        """,
        (
            clean_id,
            _clean_text(category, limit=120),
            _clean_text(label, limit=240),
            _clean_text(description),
            _clean_text(target, limit=240),
            clean_status,
            _clean_text(detail),
            Jsonb(clean_evidence),
            _clean_text(source, limit=240),
            int(sort_order or 1000),
            now,
            now,
        ),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO install_tracking_events (
            item_id, status, detail, evidence_json, source, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            clean_id,
            clean_status,
            _clean_text(detail),
            Jsonb(clean_evidence),
            _clean_text(source, limit=240),
            now,
        ),
    )
    if commit:
        conn.commit()
    return _row_dict(row)


def update_item(
    conn: Connection,
    item_id: str,
    *,
    status: str,
    detail: str = "",
    evidence: dict | None = None,
    source: str = "",
    commit: bool = True,
) -> dict:
    init(conn)
    existing = get_item(conn, item_id)
    if not existing:
        raise KeyError(item_id)
    merged_evidence = {
        **(existing.get("evidence") or {}),
        **_sanitize_evidence(evidence or {}),
    }
    return upsert_item(
        conn,
        item_id=item_id,
        category=existing["category"],
        label=existing["label"],
        description=existing.get("description") or "",
        target=existing.get("target") or "",
        status=status,
        detail=detail or existing.get("detail") or "",
        evidence=merged_evidence,
        source=source or existing.get("source") or "",
        sort_order=int(existing.get("sort_order") or 1000),
        commit=commit,
    )


def list_events(conn: Connection, *, limit: int = 50) -> list[dict]:
    init(conn)
    rows = conn.execute(
        """
        SELECT event_id, item_id, status, detail, evidence_json, source, created_at
        FROM install_tracking_events
        ORDER BY created_at DESC, event_id DESC
        LIMIT %s
        """,
        (max(1, min(int(limit or 50), 250)),),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["created_at"] = _iso(item.get("created_at"))
        item["evidence"] = item.pop("evidence_json") or {}
        out.append(item)
    return out


def summary(conn: Connection) -> dict:
    items = list_items(conn)
    total = len(items)
    counts = {status: 0 for status in VALID_STATUSES}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    complete = counts.get("ready", 0) + counts.get("skipped", 0)
    blockers = counts.get("blocked", 0) + counts.get("failed", 0)
    return {
        "total": total,
        "complete": complete,
        "blockers": blockers,
        "running": counts.get("running", 0),
        "percent": round(100 * complete / total) if total else 0,
        "counts": counts,
    }


def payload(conn: Connection) -> dict:
    return {
        "schema_version": 1,
        "summary": summary(conn),
        "items": list_items(conn),
        "events": list_events(conn, limit=20),
    }
