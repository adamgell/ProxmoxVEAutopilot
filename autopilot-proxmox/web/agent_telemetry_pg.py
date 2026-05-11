"""PostgreSQL store for AutopilotAgent device identity and telemetry."""
from __future__ import annotations

import hmac
import os
import secrets
from datetime import datetime, timezone
from hashlib import sha256
from threading import Lock
from typing import Any, Optional
from uuid import uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_devices (
    agent_id text PRIMARY KEY,
    token_hash text NOT NULL,
    vmid integer NULL,
    vm_uuid text NULL,
    serial_number text NULL,
    computer_name text NULL,
    agent_version text NULL,
    created_from_run_id uuid NULL,
    revoked boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    token_rotated_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_devices_vmid
    ON agent_devices(vmid);

CREATE TABLE IF NOT EXISTS agent_heartbeats (
    id bigserial PRIMARY KEY,
    agent_id text NOT NULL REFERENCES agent_devices(agent_id) ON DELETE CASCADE,
    received_at timestamptz NOT NULL,
    vmid integer NULL,
    vm_uuid text NULL,
    computer_name text NULL,
    serial_number text NULL,
    primary_ipv4 text NULL,
    ip_addresses_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    nics_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    os_name text NULL,
    os_version text NULL,
    os_build text NULL,
    boot_time text NULL,
    uptime_seconds bigint NULL,
    qga_service_name text NULL,
    qga_state text NULL,
    domain_name text NULL,
    domain_joined boolean NULL,
    entra_joined boolean NULL,
    tenant_id text NULL,
    current_run_id uuid NULL,
    current_phase text NULL,
    current_step_id text NULL,
    agent_version text NULL,
    raw_json jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_agent_heartbeats_vmid_time
    ON agent_heartbeats(vmid, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_heartbeats_agent_time
    ON agent_heartbeats(agent_id, received_at DESC);

CREATE TABLE IF NOT EXISTS agent_events (
    id bigserial PRIMARY KEY,
    agent_id text NOT NULL REFERENCES agent_devices(agent_id) ON DELETE CASCADE,
    received_at timestamptz NOT NULL,
    severity text NOT NULL,
    event_type text NOT NULL,
    message text NULL,
    data_json jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_agent_events_agent_time
    ON agent_events(agent_id, received_at DESC);

CREATE TABLE IF NOT EXISTS agent_bootstrap_approvals (
    approval_id uuid PRIMARY KEY,
    agent_id text NOT NULL,
    bootstrap_token_hash text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    phase text NULL,
    vmid integer NULL,
    vm_uuid text NULL,
    computer_name text NULL,
    serial_number text NULL,
    agent_version text NULL,
    created_from_run_id uuid NULL,
    agent_token text NULL,
    requested_at timestamptz NOT NULL,
    approved_at timestamptz NULL,
    claimed_at timestamptz NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_bootstrap_approvals_agent_pending
    ON agent_bootstrap_approvals(agent_id)
    WHERE status IN ('pending', 'approved');
CREATE INDEX IF NOT EXISTS idx_agent_bootstrap_approvals_status_time
    ON agent_bootstrap_approvals(status, requested_at DESC);

CREATE TABLE IF NOT EXISTS agent_work_items (
    id uuid PRIMARY KEY,
    agent_id text NOT NULL REFERENCES agent_devices(agent_id) ON DELETE CASCADE,
    kind text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    vmid integer NULL,
    job_id text NULL,
    request_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text NULL,
    created_at timestamptz NOT NULL,
    claimed_at timestamptz NULL,
    completed_at timestamptz NULL,
    updated_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_work_items_agent_status_time
    ON agent_work_items(agent_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_work_items_vmid_status_time
    ON agent_work_items(vmid, status, created_at);
"""


DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS agent_work_items CASCADE;
DROP TABLE IF EXISTS agent_events CASCADE;
DROP TABLE IF EXISTS agent_heartbeats CASCADE;
DROP TABLE IF EXISTS agent_bootstrap_approvals CASCADE;
DROP TABLE IF EXISTS agent_devices CASCADE;
"""

_INIT_LOCK = Lock()
_INIT_DONE = False
_INIT_LOCK_KEY = "proxmoxveautopilot:agent_telemetry_pg:init"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _commit(conn: Connection) -> None:
    if not conn.closed:
        conn.commit()


def _row_dict(row: Any) -> dict:
    data = dict(row)
    for key in ("id", "approval_id", "created_from_run_id", "current_run_id"):
        if data.get(key) is not None:
            data[key] = str(data[key])
    return data


def _uuid_or_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _secret() -> bytes:
    secret = (
        os.environ.get("AUTOPILOT_AGENT_TOKEN_HASH_SECRET")
        or os.environ.get("AUTOPILOT_WINPE_TOKEN_SECRET")
        or "autopilot-agent-dev-secret"
    )
    return secret.encode("utf-8")


def hash_token(token: str) -> str:
    return hmac.new(_secret(), token.encode("utf-8"), sha256).hexdigest()


def new_agent_token() -> str:
    return secrets.token_urlsafe(48)


def public_sha256(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def init(conn: Connection | None = None) -> None:
    global _INIT_DONE
    if conn is None:
        with db_pg.connection() as live:
            init(live)
        return
    if _INIT_DONE:
        return
    with _INIT_LOCK:
        if _INIT_DONE:
            return
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_INIT_LOCK_KEY,))
        conn.execute(SCHEMA)
        conn.execute(
            """
            ALTER TABLE agent_bootstrap_approvals
                ADD COLUMN IF NOT EXISTS agent_token text NULL
            """
        )
        _commit(conn)
        _INIT_DONE = True


def reset_for_tests(conn: Connection) -> None:
    global _INIT_DONE
    conn.execute(DROP_SCHEMA_FOR_TESTS)
    _commit(conn)
    _INIT_DONE = False


def upsert_device(
    conn: Connection,
    *,
    agent_id: str,
    token: str,
    vmid: Optional[int] = None,
    vm_uuid: Optional[str] = None,
    serial_number: Optional[str] = None,
    computer_name: Optional[str] = None,
    agent_version: Optional[str] = None,
    created_from_run_id: Optional[str] = None,
) -> dict:
    now = _now()
    token_hash = hash_token(token)
    row = conn.execute(
        """
        INSERT INTO agent_devices (
            agent_id, token_hash, vmid, vm_uuid, serial_number,
            computer_name, agent_version, created_from_run_id, revoked,
            created_at, first_seen_at, last_seen_at, token_rotated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, false, %s, %s, %s, %s)
        ON CONFLICT (agent_id) DO UPDATE SET
            token_hash = EXCLUDED.token_hash,
            vmid = COALESCE(EXCLUDED.vmid, agent_devices.vmid),
            vm_uuid = COALESCE(EXCLUDED.vm_uuid, agent_devices.vm_uuid),
            serial_number = COALESCE(EXCLUDED.serial_number, agent_devices.serial_number),
            computer_name = COALESCE(EXCLUDED.computer_name, agent_devices.computer_name),
            agent_version = COALESCE(EXCLUDED.agent_version, agent_devices.agent_version),
            created_from_run_id = COALESCE(
                EXCLUDED.created_from_run_id,
                agent_devices.created_from_run_id
            ),
            revoked = false,
            last_seen_at = EXCLUDED.last_seen_at,
            token_rotated_at = EXCLUDED.token_rotated_at
        RETURNING *
        """,
        (
            agent_id,
            token_hash,
            vmid,
            vm_uuid,
            serial_number,
            computer_name,
            agent_version,
            created_from_run_id,
            now,
            now,
            now,
            now,
        ),
    ).fetchone()
    _commit(conn)
    return _row_dict(row)


def get_device(conn: Connection, agent_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM agent_devices WHERE agent_id = %s",
        (agent_id,),
    ).fetchone()
    return _row_dict(row) if row else None


def validate_agent_token(conn: Connection, token: str) -> dict | None:
    row = conn.execute(
        """
        SELECT * FROM agent_devices
        WHERE token_hash = %s AND revoked = false
        """,
        (hash_token(token),),
    ).fetchone()
    return _row_dict(row) if row else None


def revoke_agent(conn: Connection, agent_id: str) -> None:
    conn.execute(
        """
        UPDATE agent_devices
        SET revoked = true, last_seen_at = %s
        WHERE agent_id = %s
        """,
        (_now(), agent_id),
    )
    _commit(conn)


def upsert_manual_agent(
    conn: Connection,
    *,
    agent_id: str,
    vmid: Optional[int] = None,
    computer_name: Optional[str] = None,
    serial_number: Optional[str] = None,
    agent_version: Optional[str] = None,
    created_from_run_id: Optional[str] = None,
) -> dict:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO agent_devices (
            agent_id, token_hash, vmid, serial_number, computer_name,
            agent_version, created_from_run_id, revoked, created_at,
            first_seen_at, last_seen_at, token_rotated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, false, %s, %s, %s, %s)
        ON CONFLICT (agent_id) DO UPDATE SET
            vmid = EXCLUDED.vmid,
            serial_number = EXCLUDED.serial_number,
            computer_name = EXCLUDED.computer_name,
            agent_version = EXCLUDED.agent_version,
            created_from_run_id = EXCLUDED.created_from_run_id,
            revoked = false,
            last_seen_at = EXCLUDED.last_seen_at
        RETURNING *
        """,
        (
            agent_id,
            hash_token(new_agent_token()),
            vmid,
            serial_number,
            computer_name,
            agent_version,
            _uuid_or_none(created_from_run_id),
            now,
            now,
            now,
            now,
        ),
    ).fetchone()
    _commit(conn)
    return _row_dict(row)


def update_agent_metadata(
    conn: Connection,
    *,
    agent_id: str,
    vmid: Optional[int] = None,
    computer_name: Optional[str] = None,
    serial_number: Optional[str] = None,
    agent_version: Optional[str] = None,
    created_from_run_id: Optional[str] = None,
) -> dict | None:
    now = _now()
    row = conn.execute(
        """
        UPDATE agent_devices
        SET vmid = %s,
            computer_name = %s,
            serial_number = %s,
            agent_version = %s,
            created_from_run_id = %s,
            last_seen_at = %s
        WHERE agent_id = %s
        RETURNING *
        """,
        (
            vmid,
            computer_name,
            serial_number,
            agent_version,
            _uuid_or_none(created_from_run_id),
            now,
            agent_id,
        ),
    ).fetchone()
    _commit(conn)
    return _row_dict(row) if row else None


def hard_delete_agent(conn: Connection, agent_id: str) -> bool:
    conn.execute(
        "DELETE FROM agent_bootstrap_approvals WHERE agent_id = %s",
        (agent_id,),
    )
    row = conn.execute(
        "DELETE FROM agent_devices WHERE agent_id = %s RETURNING agent_id",
        (agent_id,),
    ).fetchone()
    _commit(conn)
    return row is not None


def create_bootstrap_approval(
    conn: Connection,
    *,
    bootstrap_token: str,
    agent_id: str,
    phase: Optional[str] = None,
    vmid: Optional[int] = None,
    vm_uuid: Optional[str] = None,
    computer_name: Optional[str] = None,
    serial_number: Optional[str] = None,
    agent_version: Optional[str] = None,
    created_from_run_id: Optional[str] = None,
) -> dict:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO agent_bootstrap_approvals (
            approval_id, agent_id, bootstrap_token_hash, status, phase,
            vmid, vm_uuid, computer_name, serial_number, agent_version,
            created_from_run_id, requested_at
        )
        VALUES (%s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (agent_id) WHERE status IN ('pending', 'approved')
        DO UPDATE SET
            bootstrap_token_hash = EXCLUDED.bootstrap_token_hash,
            phase = EXCLUDED.phase,
            vmid = COALESCE(EXCLUDED.vmid, agent_bootstrap_approvals.vmid),
            vm_uuid = COALESCE(EXCLUDED.vm_uuid, agent_bootstrap_approvals.vm_uuid),
            computer_name = COALESCE(
                EXCLUDED.computer_name,
                agent_bootstrap_approvals.computer_name
            ),
            serial_number = COALESCE(
                EXCLUDED.serial_number,
                agent_bootstrap_approvals.serial_number
            ),
            agent_version = COALESCE(
                EXCLUDED.agent_version,
                agent_bootstrap_approvals.agent_version
            ),
            created_from_run_id = COALESCE(
                EXCLUDED.created_from_run_id,
                agent_bootstrap_approvals.created_from_run_id
            ),
            requested_at = EXCLUDED.requested_at
        RETURNING *
        """,
        (
            uuid4(),
            agent_id,
            public_sha256(bootstrap_token),
            phase,
            vmid,
            vm_uuid,
            computer_name,
            serial_number,
            agent_version,
            created_from_run_id,
            now,
        ),
    ).fetchone()
    _commit(conn)
    return _row_dict(row)


def get_bootstrap_approval(conn: Connection, approval_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM agent_bootstrap_approvals WHERE approval_id = %s",
        (approval_id,),
    ).fetchone()
    return _row_dict(row) if row else None


def approve_bootstrap_approval(
    conn: Connection,
    approval_id: str,
    *,
    agent_token: Optional[str] = None,
) -> dict | None:
    token = agent_token or new_agent_token()
    now = _now()
    pending = get_bootstrap_approval(conn, approval_id)
    if not pending:
        return None
    if pending["status"] not in ("pending", "approved"):
        return pending
    upsert_device(
        conn,
        agent_id=pending["agent_id"],
        token=token,
        vmid=pending.get("vmid"),
        vm_uuid=pending.get("vm_uuid"),
        serial_number=pending.get("serial_number"),
        computer_name=pending.get("computer_name"),
        agent_version=pending.get("agent_version"),
        created_from_run_id=pending.get("created_from_run_id"),
    )
    row = conn.execute(
        """
        UPDATE agent_bootstrap_approvals
        SET status = 'approved',
            approved_at = COALESCE(approved_at, %s),
            agent_token = %s
        WHERE approval_id = %s
        RETURNING *
        """,
        (now, token, approval_id),
    ).fetchone()
    _commit(conn)
    return _row_dict(row)


def claim_bootstrap_approval(
    conn: Connection,
    approval_id: str,
    *,
    bootstrap_token: str,
) -> dict | None:
    row = conn.execute(
        """
        SELECT *
        FROM agent_bootstrap_approvals
        WHERE approval_id = %s AND bootstrap_token_hash = %s
        """,
        (approval_id, public_sha256(bootstrap_token)),
    ).fetchone()
    if not row:
        return None
    approval = _row_dict(row)
    if approval["status"] == "approved" and approval.get("agent_token"):
        conn.execute(
            """
            UPDATE agent_bootstrap_approvals
            SET claimed_at = COALESCE(claimed_at, %s)
            WHERE approval_id = %s
            """,
            (_now(), approval_id),
        )
        _commit(conn)
    return approval


def mark_bootstrap_approval_claimed(conn: Connection, approval_id: str) -> None:
    conn.execute(
        """
        UPDATE agent_bootstrap_approvals
        SET claimed_at = COALESCE(claimed_at, %s)
        WHERE approval_id = %s
        """,
        (_now(), approval_id),
    )
    _commit(conn)


def pending_bootstrap_approvals(conn: Connection | None = None) -> list[dict]:
    if conn is None:
        with db_pg.connection() as live:
            return pending_bootstrap_approvals(live)
    rows = conn.execute(
        """
        SELECT *
        FROM agent_bootstrap_approvals
        WHERE status IN ('pending', 'approved')
        ORDER BY requested_at DESC, agent_id ASC
        """
    ).fetchall()
    return [_row_dict(row) for row in rows]


def record_heartbeat(conn: Connection, *, agent_id: str, payload: dict[str, Any]) -> dict:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO agent_heartbeats (
            agent_id, received_at, vmid, vm_uuid, computer_name, serial_number,
            primary_ipv4, ip_addresses_json, nics_json, os_name, os_version,
            os_build, boot_time, uptime_seconds, qga_service_name, qga_state,
            domain_name, domain_joined, entra_joined, tenant_id, current_run_id,
            current_phase, current_step_id, agent_version, raw_json
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        RETURNING *
        """,
        (
            agent_id,
            now,
            payload.get("vmid"),
            payload.get("vm_uuid"),
            payload.get("computer_name"),
            payload.get("serial_number"),
            payload.get("primary_ipv4"),
            Jsonb(payload.get("ip_addresses") or []),
            Jsonb(payload.get("nics") or []),
            payload.get("os_name"),
            payload.get("os_version"),
            payload.get("os_build"),
            payload.get("boot_time"),
            payload.get("uptime_seconds"),
            payload.get("qga_service_name"),
            payload.get("qga_state"),
            payload.get("domain_name"),
            payload.get("domain_joined"),
            payload.get("entra_joined"),
            payload.get("tenant_id"),
            _uuid_or_none(payload.get("current_run_id")),
            payload.get("current_phase"),
            payload.get("current_step_id"),
            payload.get("agent_version"),
            Jsonb(payload),
        ),
    ).fetchone()
    conn.execute(
        """
        UPDATE agent_devices
        SET last_seen_at = %s,
            vmid = COALESCE(%s, vmid),
            vm_uuid = COALESCE(%s, vm_uuid),
            computer_name = COALESCE(%s, computer_name),
            serial_number = COALESCE(%s, serial_number),
            agent_version = COALESCE(%s, agent_version)
        WHERE agent_id = %s
        """,
        (
            now,
            payload.get("vmid"),
            payload.get("vm_uuid"),
            payload.get("computer_name"),
            payload.get("serial_number"),
            payload.get("agent_version"),
            agent_id,
        ),
    )
    conn.execute(
        """
        UPDATE agent_bootstrap_approvals
        SET status = 'claimed',
            claimed_at = COALESCE(claimed_at, %s)
        WHERE agent_id = %s
          AND status = 'approved'
          AND agent_token IS NOT NULL
        """,
        (now, agent_id),
    )
    _commit(conn)
    return _row_dict(row)


def record_event(conn: Connection, *, agent_id: str, payload: dict[str, Any]) -> dict:
    row = conn.execute(
        """
        INSERT INTO agent_events (
            agent_id, received_at, severity, event_type, message, data_json
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            agent_id,
            _now(),
            payload.get("severity") or "info",
            payload.get("event_type"),
            payload.get("message"),
            Jsonb(payload.get("data") or {}),
        ),
    ).fetchone()
    _commit(conn)
    return _row_dict(row)


def list_events(conn: Connection, agent_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM agent_events
        WHERE agent_id = %s
        ORDER BY received_at DESC, id DESC
        """,
        (agent_id,),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def latest_by_vmid(conn: Connection | None = None) -> dict[int, dict]:
    if conn is None:
        with db_pg.connection() as live:
            return latest_by_vmid(live)
    rows = conn.execute(
        """
        SELECT DISTINCT ON (h.vmid)
            h.*, d.revoked
        FROM agent_heartbeats h
        JOIN agent_devices d ON d.agent_id = h.agent_id
        WHERE h.vmid IS NOT NULL AND d.revoked = false
        ORDER BY h.vmid, h.received_at DESC, h.id DESC
        """
    ).fetchall()
    return {int(row["vmid"]): _row_dict(row) for row in rows}


def latest_for_agent(conn: Connection, agent_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT * FROM agent_heartbeats
        WHERE agent_id = %s
        ORDER BY received_at DESC, id DESC
        LIMIT 1
        """,
        (agent_id,),
    ).fetchone()
    return _row_dict(row) if row else None


def latest_for_run(conn: Connection, run_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT h.*
        FROM agent_heartbeats h
        JOIN agent_devices d ON d.agent_id = h.agent_id
        WHERE h.current_run_id = %s
          AND d.revoked = false
        ORDER BY h.received_at DESC, h.id DESC
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    return _row_dict(row) if row else None


def latest_agents(conn: Connection | None = None) -> list[dict]:
    if conn is None:
        with db_pg.connection() as live:
            return latest_agents(live)
    rows = conn.execute(
        """
        SELECT
            d.agent_id,
            d.vmid AS device_vmid,
            d.vm_uuid AS device_vm_uuid,
            d.serial_number AS device_serial_number,
            d.computer_name AS device_computer_name,
            d.agent_version AS device_agent_version,
            d.created_from_run_id,
            d.revoked,
            d.created_at,
            d.first_seen_at,
            d.last_seen_at,
            h.id AS heartbeat_id,
            h.received_at,
            h.vmid,
            h.vm_uuid,
            h.computer_name,
            h.serial_number,
            h.primary_ipv4,
            h.ip_addresses_json,
            h.nics_json,
            h.os_name,
            h.os_version,
            h.os_build,
            h.boot_time,
            h.uptime_seconds,
            h.qga_service_name,
            h.qga_state,
            h.domain_name,
            h.domain_joined,
            h.entra_joined,
            h.tenant_id,
            h.current_run_id,
            h.current_phase,
            h.current_step_id,
            h.agent_version,
            h.raw_json
        FROM agent_devices d
        LEFT JOIN LATERAL (
            SELECT *
            FROM agent_heartbeats h
            WHERE h.agent_id = d.agent_id
            ORDER BY h.received_at DESC, h.id DESC
            LIMIT 1
        ) h ON true
        WHERE d.revoked = false
        ORDER BY COALESCE(h.received_at, d.last_seen_at) DESC, d.agent_id ASC
        """
    ).fetchall()
    return [_row_dict(row) for row in rows]


def create_work_item(
    conn: Connection,
    *,
    agent_id: str,
    kind: str,
    request: dict[str, Any],
    vmid: Optional[int] = None,
    job_id: Optional[str] = None,
) -> dict:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO agent_work_items (
            id, agent_id, kind, status, vmid, job_id, request_json,
            result_json, created_at, updated_at
        )
        VALUES (%s, %s, %s, 'pending', %s, %s, %s, '{}'::jsonb, %s, %s)
        RETURNING *
        """,
        (
            uuid4(),
            agent_id,
            kind,
            vmid,
            job_id,
            Jsonb(request),
            now,
            now,
        ),
    ).fetchone()
    _commit(conn)
    return _row_dict(row)


def attach_work_item_job(
    conn: Connection,
    work_item_id: str,
    *,
    job_id: str,
) -> dict | None:
    row = conn.execute(
        """
        UPDATE agent_work_items
        SET job_id = %s,
            updated_at = %s
        WHERE id = %s
        RETURNING *
        """,
        (job_id, _now(), work_item_id),
    ).fetchone()
    _commit(conn)
    return _row_dict(row) if row else None


def get_work_item(conn: Connection, work_item_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM agent_work_items WHERE id = %s",
        (work_item_id,),
    ).fetchone()
    return _row_dict(row) if row else None


def claim_next_work_item(
    conn: Connection,
    *,
    agent_id: str,
    supported_kinds: list[str],
) -> dict | None:
    if not supported_kinds:
        return None
    with conn.transaction():
        pending = conn.execute(
            """
            SELECT id
            FROM agent_work_items
            WHERE agent_id = %s
              AND status = 'pending'
              AND kind = ANY(%s)
            ORDER BY created_at ASC, id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            (agent_id, supported_kinds),
        ).fetchone()
        if not pending:
            return None
        now = _now()
        row = conn.execute(
            """
            UPDATE agent_work_items
            SET status = 'claimed',
                claimed_at = COALESCE(claimed_at, %s),
                updated_at = %s
            WHERE id = %s AND status = 'pending'
            RETURNING *
            """,
            (now, now, pending["id"]),
        ).fetchone()
    _commit(conn)
    return _row_dict(row) if row else None


def complete_work_item(
    conn: Connection,
    work_item_id: str,
    *,
    agent_id: str,
    result: dict[str, Any],
) -> dict | None:
    now = _now()
    row = conn.execute(
        """
        UPDATE agent_work_items
        SET status = 'complete',
            result_json = %s,
            error = NULL,
            completed_at = COALESCE(completed_at, %s),
            updated_at = %s
        WHERE id = %s
          AND agent_id = %s
          AND status IN ('pending', 'claimed')
        RETURNING *
        """,
        (Jsonb(result), now, now, work_item_id, agent_id),
    ).fetchone()
    _commit(conn)
    return _row_dict(row) if row else None


def fail_work_item(
    conn: Connection,
    work_item_id: str,
    *,
    agent_id: str,
    error: str,
    result: Optional[dict[str, Any]] = None,
) -> dict | None:
    now = _now()
    row = conn.execute(
        """
        UPDATE agent_work_items
        SET status = 'failed',
            result_json = %s,
            error = %s,
            completed_at = COALESCE(completed_at, %s),
            updated_at = %s
        WHERE id = %s
          AND agent_id = %s
          AND status IN ('pending', 'claimed')
        RETURNING *
        """,
        (Jsonb(result or {}), error, now, now, work_item_id, agent_id),
    ).fetchone()
    _commit(conn)
    return _row_dict(row) if row else None


def list_work_items(
    conn: Connection,
    *,
    status: Optional[str] = None,
    job_id: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = %s")
        params.append(status)
    if job_id:
        clauses.append("job_id = %s")
        params.append(job_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT *
        FROM agent_work_items
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
        (*params, limit),
    ).fetchall()
    return [_row_dict(row) for row in rows]
