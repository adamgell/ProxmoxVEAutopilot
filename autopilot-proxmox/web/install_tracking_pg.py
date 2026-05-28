"""PostgreSQL install/init tracking for the operator console."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


VALID_STATUSES = {"pending", "running", "ready", "blocked", "failed", "skipped"}
DEFAULT_RUN_ID = "pvetest-clean-install"
DEFAULT_RUN = {
    "run_id": DEFAULT_RUN_ID,
    "name": "pvetest clean install",
    "target": "pvetest",
    "status": "running",
    "source": "019e28b1-38ff-7a53-8047-7d1426e96185",
}

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
        "item_id": "proxmox-deploy-options",
        "category": "Proxmox target",
        "label": "Deploy target options discovered",
        "description": "Nodes, storage, bridges, ISO storage, and VM defaults are readable before any OSDCloud or OSDeploy run is queued.",
        "target": "pvetest",
        "status": "pending",
        "detail": "Read from live Proxmox options and settings summary.",
        "source": "setup.get_readiness",
        "sort_order": 45,
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
        "item_id": "agent-seed-artifacts",
        "category": "Agent bootstrap",
        "label": "AutopilotAgent seed artifacts available",
        "description": "Windows build hosts and deployed VMs can download the current agent binary/config and use a hashed bootstrap token.",
        "target": "controller output",
        "status": "pending",
        "detail": "Confirm agent seed manifest and bootstrap token hash before build-host or VM readiness flows.",
        "source": "setup.get_readiness",
        "sort_order": 55,
    },
    {
        "item_id": "osdcloud-catalog",
        "category": "OSDCloud",
        "label": "OSDCloud catalog and deploy options ready",
        "description": "Client Windows catalog, image choices, language/edition options, and Proxmox deploy options are visible before desktop runs.",
        "target": "client Windows",
        "status": "pending",
        "detail": "Read catalog/options and keep OSDCloud client-Windows focused.",
        "source": "cloudosd.get_catalog",
        "sort_order": 56,
    },
    {
        "item_id": "osdcloud-assets",
        "category": "OSDCloud",
        "label": "OSDCloud assets/cache available",
        "description": "Required OSDCloud scripts, content, hashes, and publishable artifacts are present or clearly marked missing.",
        "target": "client Windows",
        "status": "pending",
        "detail": "Track assets status before queueing OSDCloud desktop deployments.",
        "source": "cloudosd.get_assets_status",
        "sort_order": 57,
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
        "item_id": "osdeploy-source-media",
        "category": "OSDeploy",
        "label": "OSDeploy source media and cache ready",
        "description": "Windows Server source media, OSDeploy build cache, drivers, and publish metadata are ready before server paths are queued.",
        "target": "Windows Server",
        "status": "pending",
        "detail": "Read OSDeploy catalog/cache/build status before server deploy runs.",
        "source": "osdeploy.get_catalog",
        "sort_order": 65,
    },
    {
        "item_id": "winpe-baseline",
        "category": "WinPE",
        "label": "Existing WinPE baseline preserved",
        "description": "The legacy WinPE WIM path remains available and unchanged as a fallback/regression surface.",
        "target": "Windows fallback",
        "status": "pending",
        "detail": "Do not replace /winpe behavior while adding OSDCloud and OSDeploy readiness.",
        "source": "repo docs",
        "sort_order": 66,
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
CREATE TABLE IF NOT EXISTS install_tracking_runs (
    run_id text PRIMARY KEY,
    name text NOT NULL,
    target text NOT NULL DEFAULT '',
    status text NOT NULL,
    source text NOT NULL DEFAULT '',
    summary_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (status IN ('pending', 'running', 'ready', 'blocked', 'failed', 'skipped'))
);

CREATE TABLE IF NOT EXISTS install_tracking_items (
    run_id text NOT NULL REFERENCES install_tracking_runs(run_id) ON DELETE CASCADE,
    item_id text NOT NULL,
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
    run_id text NOT NULL,
    item_id text NOT NULL,
    status text NOT NULL,
    detail text NOT NULL DEFAULT '',
    evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    source text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL,
    CHECK (status IN ('pending', 'running', 'ready', 'blocked', 'failed', 'skipped'))
);

"""

POST_MIGRATE_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_install_tracking_runs_updated
    ON install_tracking_runs(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_install_tracking_items_status
    ON install_tracking_items(run_id, status, sort_order);
CREATE INDEX IF NOT EXISTS idx_install_tracking_events_item_time
    ON install_tracking_events(run_id, item_id, created_at DESC);
"""

DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS install_tracking_events CASCADE;
DROP TABLE IF EXISTS install_tracking_items CASCADE;
DROP TABLE IF EXISTS install_tracking_runs CASCADE;
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
    out["deleted_at"] = _iso(out.get("deleted_at"))
    out["evidence"] = out.pop("evidence_json") or {}
    return out


def _run_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("started_at", "completed_at", "created_at", "updated_at", "deleted_at"):
        out[key] = _iso(out.get(key))
    out["summary"] = out.pop("summary_json") or {}
    return out


def _slug(value: str) -> str:
    chars = []
    for char in str(value or "").lower():
        if char.isalnum():
            chars.append(char)
        elif chars and chars[-1] != "-":
            chars.append("-")
    return "".join(chars).strip("-")[:48] or "install-run"


def init(conn: Connection | None = None) -> None:
    own = conn is None
    conn = conn or db_pg.connect()
    try:
        conn.execute(SCHEMA)
        _migrate_run_scope(conn)
        _migrate_soft_delete(conn)
        conn.execute(POST_MIGRATE_SCHEMA)
        ensure_default_run(conn, commit=False)
        seed_defaults(conn, DEFAULT_RUN_ID, commit=False)
        _refresh_run_summary(conn, DEFAULT_RUN_ID, commit=False, touch=False)
        conn.commit()
    finally:
        if own:
            conn.close()


def reset_for_tests(conn: Connection) -> None:
    conn.execute(DROP_SCHEMA_FOR_TESTS)
    conn.commit()


def _migrate_run_scope(conn: Connection) -> None:
    item_columns = {
        row["column_name"]
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'install_tracking_items'
            """
        ).fetchall()
    }
    event_columns = {
        row["column_name"]
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'install_tracking_events'
            """
        ).fetchall()
    }
    if "run_id" not in item_columns and item_columns:
        conn.execute("ALTER TABLE install_tracking_items ADD COLUMN run_id text")
        conn.execute("UPDATE install_tracking_items SET run_id = %s WHERE run_id IS NULL", (DEFAULT_RUN_ID,))
        conn.execute("ALTER TABLE install_tracking_items ALTER COLUMN run_id SET NOT NULL")
    if "run_id" not in event_columns and event_columns:
        conn.execute("ALTER TABLE install_tracking_events ADD COLUMN run_id text")
        conn.execute("UPDATE install_tracking_events SET run_id = %s WHERE run_id IS NULL", (DEFAULT_RUN_ID,))
        conn.execute("ALTER TABLE install_tracking_events ALTER COLUMN run_id SET NOT NULL")
    conn.execute("ALTER TABLE install_tracking_events DROP CONSTRAINT IF EXISTS install_tracking_events_item_id_fkey")
    conn.execute("ALTER TABLE install_tracking_items DROP CONSTRAINT IF EXISTS install_tracking_items_pkey")
    conn.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'install_tracking_items_run_item_pkey'
            ) THEN
                ALTER TABLE install_tracking_items
                ADD CONSTRAINT install_tracking_items_run_item_pkey PRIMARY KEY (run_id, item_id);
            END IF;
        END $$;
        """
    )
    conn.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'install_tracking_events_run_item_fkey'
            ) THEN
                ALTER TABLE install_tracking_events
                ADD CONSTRAINT install_tracking_events_run_item_fkey
                FOREIGN KEY (run_id, item_id)
                REFERENCES install_tracking_items(run_id, item_id)
                ON DELETE CASCADE;
            END IF;
        END $$;
        """
    )


def _migrate_soft_delete(conn: Connection) -> None:
    for table in ("install_tracking_runs", "install_tracking_items"):
        conn.execute(
            f"""
            ALTER TABLE {table}
            ADD COLUMN IF NOT EXISTS deleted_at timestamptz NULL,
            ADD COLUMN IF NOT EXISTS deleted_by text NULL,
            ADD COLUMN IF NOT EXISTS delete_reason text NULL
            """
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_install_tracking_runs_live
        ON install_tracking_runs(updated_at DESC)
        WHERE deleted_at IS NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_install_tracking_items_live
        ON install_tracking_items(run_id, status, sort_order)
        WHERE deleted_at IS NULL
        """
    )


def ensure_default_run(conn: Connection, *, commit: bool = True) -> dict:
    now = _now()
    run = conn.execute(
        """
        INSERT INTO install_tracking_runs (
            run_id, name, target, status, source, summary_json,
            started_at, completed_at, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, NULL, %s, %s)
        ON CONFLICT (run_id) DO NOTHING
        RETURNING *
        """,
        (
            DEFAULT_RUN["run_id"],
            DEFAULT_RUN["name"],
            DEFAULT_RUN["target"],
            DEFAULT_RUN["status"],
            DEFAULT_RUN["source"],
            now,
            now,
            now,
        ),
    ).fetchone()
    if run is None:
        run = conn.execute(
            "SELECT * FROM install_tracking_runs WHERE run_id = %s",
            (DEFAULT_RUN["run_id"],),
        ).fetchone()
    if commit:
        conn.commit()
    return _run_dict(run)


def upsert_run(
    conn: Connection,
    *,
    run_id: str,
    name: str,
    target: str = "",
    status: str = "running",
    source: str = "",
    summary: dict | None = None,
    completed_at: datetime | None = None,
    commit: bool = True,
) -> dict:
    clean_run_id = _clean_text(run_id, limit=120)
    if not clean_run_id:
        raise ValueError("run_id is required")
    clean_status = _clean_status(status)
    now = _now()
    row = conn.execute(
        """
        INSERT INTO install_tracking_runs (
            run_id, name, target, status, source, summary_json,
            started_at, completed_at, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (run_id) DO UPDATE SET
            name = EXCLUDED.name,
            target = EXCLUDED.target,
            status = EXCLUDED.status,
            source = EXCLUDED.source,
            summary_json = EXCLUDED.summary_json,
            completed_at = EXCLUDED.completed_at,
            updated_at = EXCLUDED.updated_at
        RETURNING *
        """,
        (
            clean_run_id,
            _clean_text(name, limit=240),
            _clean_text(target, limit=240),
            clean_status,
            _clean_text(source, limit=240),
            Jsonb(_sanitize_evidence(summary or {})),
            now,
            completed_at,
            now,
            now,
        ),
    ).fetchone()
    if commit:
        conn.commit()
    return _run_dict(row)


def create_run(
    conn: Connection,
    *,
    name: str,
    target: str = "",
    source: str = "",
    commit: bool = True,
) -> dict:
    init(conn)
    base = _slug(name)
    run_id = f"{base}-{uuid4().hex[:8]}"
    run = upsert_run(
        conn,
        run_id=run_id,
        name=name,
        target=target,
        status="running",
        source=source,
        commit=False,
    )
    seed_defaults(conn, run_id, commit=False)
    _refresh_run_summary(conn, run_id, commit=False)
    if commit:
        conn.commit()
    return run


def list_runs(conn: Connection, *, include_deleted: bool = False) -> list[dict]:
    init(conn)
    where = "" if include_deleted else "WHERE deleted_at IS NULL"
    rows = conn.execute(
        f"""
        SELECT *
        FROM install_tracking_runs
        {where}
        ORDER BY updated_at DESC, created_at DESC
        """
    ).fetchall()
    return [_run_dict(row) for row in rows]


def get_run(conn: Connection, run_id: str, *, include_deleted: bool = False) -> dict | None:
    init(conn)
    if include_deleted:
        sql = "SELECT * FROM install_tracking_runs WHERE run_id = %s"
    else:
        sql = "SELECT * FROM install_tracking_runs WHERE run_id = %s AND deleted_at IS NULL"
    row = conn.execute(sql, (run_id,)).fetchone()
    return _run_dict(row)


def default_run(conn: Connection) -> dict:
    init(conn)
    row = conn.execute(
        """
        SELECT *
        FROM install_tracking_runs
        ORDER BY
            CASE WHEN status IN ('running', 'pending', 'blocked', 'failed') THEN 0 ELSE 1 END,
            updated_at DESC,
            created_at DESC
        LIMIT 1
        """
    ).fetchone()
    return _run_dict(row) or ensure_default_run(conn)


def seed_defaults(conn: Connection, run_id: str = DEFAULT_RUN_ID, *, commit: bool = True) -> None:
    now = _now()
    for item in DEFAULT_ITEMS:
        conn.execute(
            """
            INSERT INTO install_tracking_items (
                run_id, item_id, category, label, description, target, status,
                detail, evidence_json, source, sort_order, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, item_id) DO NOTHING
            """,
            (
                run_id,
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


def list_items(conn: Connection, *, include_deleted: bool = False) -> list[dict]:
    return list_run_items(conn, DEFAULT_RUN_ID, include_deleted=include_deleted)


def list_run_items(conn: Connection, run_id: str, *, include_deleted: bool = False) -> list[dict]:
    extra = "" if include_deleted else " AND deleted_at IS NULL"
    rows = conn.execute(
        f"""
        SELECT *
        FROM install_tracking_items
        WHERE run_id = %s{extra}
        ORDER BY sort_order ASC, category ASC, label ASC
        """,
        (run_id,),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def get_item(
    conn: Connection,
    run_id: str,
    item_id: str | None = None,
    *,
    include_deleted: bool = False,
) -> dict | None:
    if item_id is None:
        item_id = run_id
        run_id = DEFAULT_RUN_ID
    extra = "" if include_deleted else " AND deleted_at IS NULL"
    row = conn.execute(
        f"SELECT * FROM install_tracking_items WHERE run_id = %s AND item_id = %s{extra}",
        (run_id, item_id),
    ).fetchone()
    return _row_dict(row)


def upsert_item(
    conn: Connection,
    *,
    run_id: str = DEFAULT_RUN_ID,
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
            run_id, item_id, category, label, description, target, status, detail,
            evidence_json, source, sort_order, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (run_id, item_id) DO UPDATE SET
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
            run_id,
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
            run_id, item_id, status, detail, evidence_json, source, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id,
            clean_id,
            clean_status,
            _clean_text(detail),
            Jsonb(clean_evidence),
            _clean_text(source, limit=240),
            now,
        ),
    )
    _refresh_run_summary(conn, run_id, commit=False)
    if commit:
        conn.commit()
    return _row_dict(row)


def update_item(
    conn: Connection,
    item_id: str,
    *,
    run_id: str = DEFAULT_RUN_ID,
    status: str,
    detail: str = "",
    evidence: dict | None = None,
    source: str = "",
    commit: bool = True,
) -> dict:
    init(conn)
    existing = get_item(conn, run_id, item_id)
    if not existing:
        raise KeyError(item_id)
    merged_evidence = {
        **(existing.get("evidence") or {}),
        **_sanitize_evidence(evidence or {}),
    }
    return upsert_item(
        conn,
        run_id=run_id,
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


def delete_run(
    conn: Connection,
    run_id: str,
    *,
    reason: str,
    deleted_by: str | None = None,
    commit: bool = True,
) -> dict | None:
    reason_clean = (reason or "").strip()
    if not reason_clean:
        raise ValueError("delete reason is required")
    reason_clean = reason_clean[:500]
    now = _now()
    by = (deleted_by or "").strip() or None
    row = conn.execute(
        """
        UPDATE install_tracking_runs
        SET deleted_at = %s,
            deleted_by = %s,
            delete_reason = %s,
            updated_at = %s
        WHERE run_id = %s AND deleted_at IS NULL
        RETURNING *
        """,
        (now, by, reason_clean, now, run_id),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        """
        UPDATE install_tracking_items
        SET deleted_at = %s,
            deleted_by = %s,
            delete_reason = %s,
            updated_at = %s
        WHERE run_id = %s AND deleted_at IS NULL
        """,
        (now, by, reason_clean, now, run_id),
    )
    if commit:
        conn.commit()
    return _run_dict(dict(row))


def delete_item(
    conn: Connection,
    run_id: str,
    item_id: str,
    *,
    reason: str,
    deleted_by: str | None = None,
    commit: bool = True,
) -> dict | None:
    reason_clean = (reason or "").strip()
    if not reason_clean:
        raise ValueError("delete reason is required")
    reason_clean = reason_clean[:500]
    now = _now()
    by = (deleted_by or "").strip() or None
    row = conn.execute(
        """
        UPDATE install_tracking_items
        SET deleted_at = %s,
            deleted_by = %s,
            delete_reason = %s,
            updated_at = %s
        WHERE run_id = %s AND item_id = %s AND deleted_at IS NULL
        RETURNING *
        """,
        (now, by, reason_clean, now, run_id, item_id),
    ).fetchone()
    if commit:
        conn.commit()
    return _row_dict(dict(row)) if row else None


def list_events(conn: Connection, run_id: str = DEFAULT_RUN_ID, *, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT event_id, run_id, item_id, status, detail, evidence_json, source, created_at
        FROM install_tracking_events
        WHERE run_id = %s
        ORDER BY created_at DESC, event_id DESC
        LIMIT %s
        """,
        (run_id, max(1, min(int(limit or 50), 250))),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["created_at"] = _iso(item.get("created_at"))
        item["evidence"] = item.pop("evidence_json") or {}
        out.append(item)
    return out


def summary(conn: Connection, run_id: str = DEFAULT_RUN_ID) -> dict:
    items = list_run_items(conn, run_id)
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


def _status_from_summary(item: dict) -> str:
    if item["blockers"]:
        return "blocked"
    if item["total"] and item["complete"] >= item["total"]:
        return "ready"
    if item["running"]:
        return "running"
    return "pending"


def _refresh_run_summary(conn: Connection, run_id: str, *, commit: bool = True, touch: bool = True) -> dict:
    data = summary(conn, run_id)
    status = _status_from_summary(data)
    completed_at = _now() if status == "ready" else None
    if touch:
        updated_at = _now()
    else:
        updated_at = conn.execute(
            "SELECT updated_at FROM install_tracking_runs WHERE run_id = %s",
            (run_id,),
        ).fetchone()["updated_at"]
    conn.execute(
        """
        UPDATE install_tracking_runs
        SET summary_json = %s, status = %s, completed_at = %s, updated_at = %s
        WHERE run_id = %s
        """,
        (Jsonb(data), status, completed_at, updated_at, run_id),
    )
    if commit:
        conn.commit()
    return data


def refresh_evidence(conn: Connection, run_id: str, evidence: dict[str, Any]) -> dict:
    init(conn)
    mappings = [
        ("controller-stack", evidence.get("controller_stack")),
        ("mcp-docs-bridge", evidence.get("mcp_docs")),
        ("pve-foundation", evidence.get("setup_readiness")),
        ("windows-build-box", evidence.get("build_host")),
        ("osdeploy-artifact", evidence.get("osdeploy_artifact")),
        ("osdeploy-e2e-run", evidence.get("osdeploy_run")),
        ("agent-readiness", evidence.get("agent_readiness")),
    ]
    for item_id, item_evidence in mappings:
        if not item_evidence:
            continue
        existing = get_item(conn, run_id, item_id)
        if not existing or existing["status"] not in {"pending", "running"}:
            continue
        ready = bool(
            item_evidence.get("ready")
            or item_evidence.get("healthy")
            or item_evidence.get("ok")
            or item_evidence.get("status") in {"ready", "healthy", "complete", "completed", "online"}
            or item_evidence.get("tool_count")
            or item_evidence.get("doc_count")
        )
        next_status = "ready" if ready else existing["status"]
        detail = item_evidence.get("detail") or existing.get("detail") or ""
        update_item(
            conn,
            item_id,
            run_id=run_id,
            status=next_status,
            detail=detail,
            evidence=item_evidence,
            source=item_evidence.get("source") or "refresh evidence",
            commit=False,
        )
    data = _refresh_run_summary(conn, run_id, commit=False)
    conn.commit()
    return {"run": get_run(conn, run_id), "summary": data}


def payload(conn: Connection, run_id: str | None = None) -> dict:
    run = get_run(conn, run_id) if run_id else default_run(conn)
    if not run:
        run = ensure_default_run(conn)
    run_id = run["run_id"]
    return {
        "schema_version": 1,
        "run": run,
        "runs": list_runs(conn),
        "summary": summary(conn, run_id),
        "items": list_run_items(conn, run_id),
        "events": list_events(conn, run_id, limit=20),
    }
