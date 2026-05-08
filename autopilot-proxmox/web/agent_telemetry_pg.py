"""PostgreSQL store for AutopilotAgent device identity and telemetry."""
from __future__ import annotations

import hmac
import os
import secrets
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Optional

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
"""


DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS agent_events CASCADE;
DROP TABLE IF EXISTS agent_heartbeats CASCADE;
DROP TABLE IF EXISTS agent_devices CASCADE;
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _commit(conn: Connection) -> None:
    if not conn.closed:
        conn.commit()


def _row_dict(row: Any) -> dict:
    data = dict(row)
    for key in ("created_from_run_id", "current_run_id"):
        if data.get(key) is not None:
            data[key] = str(data[key])
    return data


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


def init(conn: Connection | None = None) -> None:
    if conn is None:
        with db_pg.connection() as live:
            init(live)
        return
    conn.execute(SCHEMA)
    _commit(conn)


def reset_for_tests(conn: Connection) -> None:
    conn.execute(DROP_SCHEMA_FOR_TESTS)
    _commit(conn)


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
            payload.get("current_run_id"),
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
