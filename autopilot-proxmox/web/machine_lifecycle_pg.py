"""Normalized machine lifecycle rollup for VM, agent, and directory evidence."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


SCHEMA = """
CREATE TABLE IF NOT EXISTS machine_lifecycle_current (
    identity_key text PRIMARY KEY,
    state text NOT NULL,
    label text NOT NULL,
    source text NOT NULL,
    priority integer NOT NULL,
    vmid integer NULL,
    agent_id text NULL,
    serial_number text NULL,
    computer_name text NULL,
    domain_name text NULL,
    tenant_id text NULL,
    autopilot_registered boolean NOT NULL DEFAULT false,
    intune_enrolled boolean NOT NULL DEFAULT false,
    entra_joined boolean NOT NULL DEFAULT false,
    domain_joined boolean NOT NULL DEFAULT false,
    first_observed_at timestamptz NOT NULL,
    last_observed_at timestamptz NOT NULL,
    evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_machine_lifecycle_current_vmid
    ON machine_lifecycle_current(vmid);
CREATE INDEX IF NOT EXISTS idx_machine_lifecycle_current_agent
    ON machine_lifecycle_current(agent_id);
CREATE INDEX IF NOT EXISTS idx_machine_lifecycle_current_serial
    ON machine_lifecycle_current(serial_number);

CREATE TABLE IF NOT EXISTS machine_lifecycle_events (
    id bigserial PRIMARY KEY,
    identity_key text NOT NULL,
    event_type text NOT NULL,
    previous_state text NULL,
    state text NOT NULL,
    label text NOT NULL,
    source text NOT NULL,
    priority integer NOT NULL,
    vmid integer NULL,
    agent_id text NULL,
    serial_number text NULL,
    computer_name text NULL,
    domain_name text NULL,
    tenant_id text NULL,
    autopilot_registered boolean NOT NULL DEFAULT false,
    intune_enrolled boolean NOT NULL DEFAULT false,
    entra_joined boolean NOT NULL DEFAULT false,
    domain_joined boolean NOT NULL DEFAULT false,
    observed_at timestamptz NOT NULL,
    evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_machine_lifecycle_events_identity_time
    ON machine_lifecycle_events(identity_key, observed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_machine_lifecycle_events_vmid_time
    ON machine_lifecycle_events(vmid, observed_at DESC, id DESC);
"""


DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS machine_lifecycle_events CASCADE;
DROP TABLE IF EXISTS machine_lifecycle_current CASCADE;
"""


_INIT_LOCK = Lock()
_INIT_DONE = False

STATE_LABELS = {
    "workgroup_unenrolled": "unenrolled",
    "ad_domain_joined": "domain",
    "entra_joined": "Entra ID",
    "hybrid_joined": "hybrid",
    "intune_enrolled": "Intune",
    "autopilot_registered": "Autopilot ID",
    "unknown_stale": "unknown",
}

STATE_PRIORITIES = {
    "unknown_stale": 0,
    "workgroup_unenrolled": 10,
    "autopilot_registered": 50,
    "ad_domain_joined": 70,
    "entra_joined": 80,
    "hybrid_joined": 85,
    "intune_enrolled": 90,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, Jsonb):
        return value.obj
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _json_list(value: Any) -> list:
    parsed = _json_value(value, [])
    return parsed if isinstance(parsed, list) else []


def _json_obj(value: Any) -> dict:
    parsed = _json_value(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Jsonb):
        return _json_safe(value.obj)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _identity_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _clean(value).lower())


def identity_key_for(
    *,
    vmid: int | None = None,
    serial_number: str | None = None,
    agent_id: str | None = None,
    computer_name: str | None = None,
) -> str:
    serial = _identity_token(serial_number)
    if serial:
        return f"serial:{serial}"
    agent = _identity_token(agent_id)
    if agent:
        return f"agent:{agent}"
    if vmid is not None:
        return f"vmid:{int(vmid)}"
    computer = _identity_token(computer_name)
    if computer:
        return f"computer:{computer}"
    return "unknown"


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
        conn.execute(SCHEMA)
        conn.commit()
        _INIT_DONE = True


def reset_for_tests(conn: Connection) -> None:
    global _INIT_DONE
    conn.execute(DROP_SCHEMA_FOR_TESTS)
    conn.commit()
    _INIT_DONE = False


def _row_dict(row: Any | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    for key in ("first_observed_at", "last_observed_at", "observed_at"):
        if key in data:
            data[key] = _iso(data.get(key))
    if "evidence_json" in data:
        data["evidence_json"] = _json_obj(data.get("evidence_json"))
    return data


def _state_from_evidence(
    *,
    autopilot_registered: bool = False,
    intune_enrolled: bool = False,
    entra_joined: bool = False,
    domain_joined: bool = False,
    explicit_workgroup: bool = False,
) -> str:
    if intune_enrolled:
        return "intune_enrolled"
    if entra_joined and domain_joined:
        return "hybrid_joined"
    if entra_joined:
        return "entra_joined"
    if domain_joined:
        return "ad_domain_joined"
    if autopilot_registered:
        return "autopilot_registered"
    if explicit_workgroup:
        return "workgroup_unenrolled"
    return "unknown_stale"


def _observe(
    conn: Connection,
    *,
    source: str,
    observed_at: datetime,
    vmid: int | None = None,
    agent_id: str | None = None,
    serial_number: str | None = None,
    computer_name: str | None = None,
    domain_name: str | None = None,
    tenant_id: str | None = None,
    autopilot_registered: bool = False,
    intune_enrolled: bool = False,
    entra_joined: bool = False,
    domain_joined: bool = False,
    explicit_workgroup: bool = False,
    evidence: dict[str, Any] | None = None,
) -> dict:
    init(conn)
    identity_key = identity_key_for(
        vmid=vmid,
        serial_number=serial_number,
        agent_id=agent_id,
        computer_name=computer_name,
    )
    existing = conn.execute(
        "SELECT * FROM machine_lifecycle_current WHERE identity_key = %s",
        (identity_key,),
    ).fetchone()
    if existing is not None:
        autopilot_registered = autopilot_registered or bool(existing["autopilot_registered"])
        intune_enrolled = intune_enrolled or bool(existing["intune_enrolled"])
        entra_joined = entra_joined or bool(existing["entra_joined"])
        domain_joined = domain_joined or bool(existing["domain_joined"])
        explicit_workgroup = explicit_workgroup and not any(
            (autopilot_registered, intune_enrolled, entra_joined, domain_joined)
        )
    state = _state_from_evidence(
        autopilot_registered=autopilot_registered,
        intune_enrolled=intune_enrolled,
        entra_joined=entra_joined,
        domain_joined=domain_joined,
        explicit_workgroup=explicit_workgroup,
    )
    label = STATE_LABELS[state]
    priority = STATE_PRIORITIES[state]
    event_type = "initial" if existing is None else "transition"
    previous_state = existing["state"] if existing else None
    if existing is None or previous_state != state:
        conn.execute(
            """
            INSERT INTO machine_lifecycle_events (
                identity_key, event_type, previous_state, state, label, source,
                priority, vmid, agent_id, serial_number, computer_name,
                domain_name, tenant_id, autopilot_registered, intune_enrolled,
                entra_joined, domain_joined, observed_at, evidence_json
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            """,
            (
                identity_key,
                event_type,
                previous_state,
                state,
                label,
                source,
                priority,
                vmid,
                agent_id,
                serial_number,
                computer_name,
                domain_name,
                tenant_id,
                autopilot_registered,
                intune_enrolled,
                entra_joined,
                domain_joined,
                observed_at,
                Jsonb(_json_safe(evidence or {})),
            ),
        )
    row = conn.execute(
        """
        INSERT INTO machine_lifecycle_current (
            identity_key, state, label, source, priority, vmid, agent_id,
            serial_number, computer_name, domain_name, tenant_id,
            autopilot_registered, intune_enrolled, entra_joined, domain_joined,
            first_observed_at, last_observed_at, evidence_json
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s
        )
        ON CONFLICT (identity_key) DO UPDATE SET
            state = EXCLUDED.state,
            label = EXCLUDED.label,
            source = EXCLUDED.source,
            priority = EXCLUDED.priority,
            vmid = COALESCE(EXCLUDED.vmid, machine_lifecycle_current.vmid),
            agent_id = COALESCE(EXCLUDED.agent_id, machine_lifecycle_current.agent_id),
            serial_number = COALESCE(
                EXCLUDED.serial_number,
                machine_lifecycle_current.serial_number
            ),
            computer_name = COALESCE(
                EXCLUDED.computer_name,
                machine_lifecycle_current.computer_name
            ),
            domain_name = COALESCE(EXCLUDED.domain_name, machine_lifecycle_current.domain_name),
            tenant_id = COALESCE(EXCLUDED.tenant_id, machine_lifecycle_current.tenant_id),
            autopilot_registered = EXCLUDED.autopilot_registered,
            intune_enrolled = EXCLUDED.intune_enrolled,
            entra_joined = EXCLUDED.entra_joined,
            domain_joined = EXCLUDED.domain_joined,
            last_observed_at = EXCLUDED.last_observed_at,
            evidence_json = EXCLUDED.evidence_json
        RETURNING *
        """,
        (
            identity_key,
            state,
            label,
            source,
            priority,
            vmid,
            agent_id,
            serial_number,
            computer_name,
            domain_name,
            tenant_id,
            autopilot_registered,
            intune_enrolled,
            entra_joined,
            domain_joined,
            observed_at,
            observed_at,
            Jsonb(_json_safe(evidence or {})),
        ),
    ).fetchone()
    return _row_dict(row) or {}


def observe_from_agent_heartbeat(
    conn: Connection,
    *,
    agent_id: str,
    heartbeat: dict[str, Any],
) -> dict:
    domain_joined = heartbeat.get("domain_joined")
    entra_joined = heartbeat.get("entra_joined")
    domain_name = _clean(heartbeat.get("domain_name"))
    explicit_workgroup = (
        domain_joined is False
        or domain_name.lower() == "workgroup"
    ) and not _bool(entra_joined)
    return _observe(
        conn,
        source="agent_heartbeat",
        observed_at=heartbeat.get("received_at") or _now(),
        vmid=heartbeat.get("vmid"),
        agent_id=agent_id,
        serial_number=heartbeat.get("serial_number"),
        computer_name=heartbeat.get("computer_name"),
        domain_name=domain_name,
        tenant_id=heartbeat.get("tenant_id"),
        domain_joined=_bool(domain_joined),
        entra_joined=_bool(entra_joined),
        explicit_workgroup=explicit_workgroup,
        evidence={"agent_id": agent_id, "heartbeat": heartbeat},
    )


def observe_from_monitor_probe(
    conn: Connection,
    *,
    probe: dict[str, Any],
) -> dict:
    entra_matches = _json_list(probe.get("entra_matches_json", probe.get("entra_matches")))
    trust_types = {
        str(match.get("trustType") or match.get("trust_type") or "").lower()
        for match in entra_matches
        if isinstance(match, dict)
    }
    dsreg = _json_obj(probe.get("dsreg_status"))
    aad_joined = str(
        dsreg.get("AzureAdJoined")
        or dsreg.get("azureAdJoined")
        or dsreg.get("aad_joined")
        or ""
    ).strip().lower() in {"yes", "true", "1"}
    ad_matches = _json_list(probe.get("ad_matches_json", probe.get("ad_matches")))
    first_ad = next((match for match in ad_matches if isinstance(match, dict)), {})
    domain_name = (
        first_ad.get("domain")
        or first_ad.get("dnsDomain")
        or str(first_ad.get("userPrincipalName") or "").split("@")[-1]
    )
    intune_enrolled = _bool(probe.get("intune_found"))
    entra_joined = _bool(probe.get("entra_found")) or aad_joined or bool(trust_types)
    domain_joined = _bool(probe.get("ad_found")) or "serverad" in trust_types
    explicit_workgroup = (
        bool(probe.get("win_name") or probe.get("serial") or probe.get("os_build"))
        and not any((intune_enrolled, entra_joined, domain_joined))
    )
    return _observe(
        conn,
        source="monitor_probe",
        observed_at=probe.get("checked_at") or _now(),
        vmid=probe.get("vmid"),
        serial_number=probe.get("serial"),
        computer_name=probe.get("win_name") or probe.get("vm_name"),
        domain_name=domain_name or None,
        intune_enrolled=intune_enrolled,
        entra_joined=entra_joined,
        domain_joined=domain_joined,
        explicit_workgroup=explicit_workgroup,
        evidence={"probe": probe},
    )


def observe_autopilot_registration(
    conn: Connection,
    *,
    vmid: int | None = None,
    serial_number: str | None = None,
    computer_name: str | None = None,
    registered: bool = True,
    evidence: dict[str, Any] | None = None,
) -> dict | None:
    if not registered or not (serial_number or vmid or computer_name):
        return None
    return _observe(
        conn,
        source="autopilot_registration",
        observed_at=_now(),
        vmid=vmid,
        serial_number=serial_number,
        computer_name=computer_name,
        autopilot_registered=True,
        evidence=evidence or {},
    )


def current_for_vm(conn: Connection, vmid: int) -> dict | None:
    init(conn)
    row = conn.execute(
        """
        SELECT *
        FROM machine_lifecycle_current
        WHERE vmid = %s
        ORDER BY priority DESC, last_observed_at DESC
        LIMIT 1
        """,
        (int(vmid),),
    ).fetchone()
    return _row_dict(row)


def current_for_agent(conn: Connection, agent_id: str) -> dict | None:
    init(conn)
    row = conn.execute(
        """
        SELECT *
        FROM machine_lifecycle_current
        WHERE agent_id = %s
        ORDER BY priority DESC, last_observed_at DESC
        LIMIT 1
        """,
        (agent_id,),
    ).fetchone()
    return _row_dict(row)


def current_by_vmids(vmids: list[int]) -> dict[int, dict]:
    if not vmids:
        return {}
    with db_pg.connection() as conn:
        init(conn)
        rows = conn.execute(
            """
            SELECT DISTINCT ON (vmid) *
            FROM machine_lifecycle_current
            WHERE vmid = ANY(%s)
            ORDER BY vmid, priority DESC, last_observed_at DESC
            """,
            (vmids,),
        ).fetchall()
    return {
        int(row["vmid"]): _row_dict(row) or {}
        for row in rows
        if row.get("vmid") is not None
    }


def current_by_agents(agent_ids: list[str]) -> dict[str, dict]:
    if not agent_ids:
        return {}
    with db_pg.connection() as conn:
        init(conn)
        rows = conn.execute(
            """
            SELECT DISTINCT ON (agent_id) *
            FROM machine_lifecycle_current
            WHERE agent_id = ANY(%s)
            ORDER BY agent_id, priority DESC, last_observed_at DESC
            """,
            (agent_ids,),
        ).fetchall()
    return {
        str(row["agent_id"]): _row_dict(row) or {}
        for row in rows
        if row.get("agent_id")
    }
