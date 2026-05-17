"""PostgreSQL repository for OSDeploy v2 artifacts, runs, events, and readiness."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import ts_engine_pg


_INIT_LOCK = Lock()
_INIT_DONE = False
_INIT_LOCK_KEY = "proxmoxveautopilot:osdeploy_pg:init"

DEFAULT_ARCHITECTURE = "amd64"
DEFAULT_OS_VERSION = "Windows Server 2022"
DEFAULT_OS_EDITION = "Datacenter"
DEFAULT_OS_LANGUAGE = "en-us"
DEFAULT_IMAGE_NAME = "Windows Server 2022 Datacenter (Desktop Experience)"
DEFAULT_OSDEPLOY_MODULE_VERSION = "26.1.30.5"
DEFAULT_OSDBUILDER_MODULE_VERSION = "24.10.8.1"
DEFAULT_ADK_VERSION = "10.1.26100.1"
MIN_VM_MEMORY_MB = 4096
RECOMMENDED_VM_MEMORY_MB = 8192
MIN_VM_DISK_SIZE_GB = 80
DEFAULT_VM_DISK_SIZE_GB = 120
DEFAULT_VM_CORES = 4

SERVER_ROLE_CATALOG = [
    "base",
    "file_server",
    "isolated_domain_controller",
    "mecm_prereq",
    "lab_in_a_box",
]
M1_LAUNCHABLE_SERVER_ROLES = {"base"}

PE_EVENT_STEP_KINDS = {
    "pe_registered": ("osdeploy_preflight",),
    "osdeploy_image_applied": ("apply_wim",),
    "osdeploy_drivers_applied": ("apply_driver_package",),
    "osdeploy_setupcomplete_staged": (
        "prepare_windows_setup",
        "stage_osd_client",
        "stage_autopilot_agent",
    ),
    "osdeploy_boot_files_staged": ("bake_boot_entry",),
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS osdeploy_artifacts (
    id uuid PRIMARY KEY,
    architecture text NOT NULL,
    osdeploy_module_version text NOT NULL,
    osdbuilder_module_version text NOT NULL,
    adk_version text NOT NULL,
    build_sha text NOT NULL,
    iso_path text NOT NULL,
    wim_path text NOT NULL,
    manifest_path text NOT NULL,
    iso_sha256 text NOT NULL,
    wim_sha256 text NOT NULL,
    source_media text NOT NULL,
    image_name text NOT NULL,
    image_index integer NOT NULL,
    os_version text NOT NULL,
    os_edition text NOT NULL,
    os_language text NOT NULL,
    built_by_host text NOT NULL,
    built_at timestamptz NOT NULL,
    proxmox_volid text NULL,
    build_job_id text NULL,
    publish_job_id text NULL,
    created_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_osdeploy_artifacts_lookup
    ON osdeploy_artifacts(architecture, os_version, os_edition, build_sha);

CREATE TABLE IF NOT EXISTS osdeploy_runs (
    run_id uuid PRIMARY KEY REFERENCES ts_provisioning_runs(id) ON DELETE CASCADE,
    artifact_id uuid NOT NULL REFERENCES osdeploy_artifacts(id),
    state text NOT NULL,
    workflow_name text NOT NULL UNIQUE,
    architecture text NOT NULL,
    server_role text NOT NULL,
    os_version text NOT NULL,
    os_edition text NOT NULL,
    os_language text NOT NULL,
    vm_name text NOT NULL,
    requested_vm_name text NULL,
    pve_vm_name text NULL,
    expected_computer_name text NULL,
    requested_vmid integer NULL,
    node text NULL,
    iso_storage text NULL,
    storage text NULL,
    network_bridge text NULL,
    vm_cores integer NOT NULL,
    vm_memory_mb integer NOT NULL,
    vm_disk_size_gb integer NOT NULL,
    secure_boot boolean NOT NULL DEFAULT true,
    outbound_policy_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    vmid integer NULL,
    vm_uuid text NULL,
    mac text NULL,
    pe_registered_at timestamptz NULL,
    osdeploy_started_at timestamptz NULL,
    osdeploy_finished_at timestamptz NULL,
    first_heartbeat_at timestamptz NULL,
    archived_at timestamptz NULL,
    archived_by text NULL,
    archive_reason text NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_osdeploy_runs_state
    ON osdeploy_runs(state, created_at);

CREATE TABLE IF NOT EXISTS osdeploy_run_events (
    id bigserial PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES osdeploy_runs(run_id) ON DELETE CASCADE,
    phase text NOT NULL,
    event_type text NOT NULL,
    severity text NOT NULL DEFAULT 'info',
    message text NULL,
    data_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS osdeploy_readiness (
    run_id uuid PRIMARY KEY REFERENCES osdeploy_runs(run_id) ON DELETE CASCADE,
    state text NOT NULL,
    qga_status text NULL,
    agent_status text NULL,
    heartbeat_at timestamptz NULL,
    server_role_status text NULL,
    errors_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at timestamptz NOT NULL
);
"""

DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS osdeploy_readiness CASCADE;
DROP TABLE IF EXISTS osdeploy_run_events CASCADE;
DROP TABLE IF EXISTS osdeploy_runs CASCADE;
DROP TABLE IF EXISTS osdeploy_artifacts CASCADE;
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


def _json(value: Any) -> Jsonb:
    return Jsonb(value or {})


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def normalize_windows_computer_name(name: str | None) -> str:
    raw = str(name or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9-]+", "", raw)
    cleaned = cleaned.strip("-")[:15]
    return cleaned


def _normalize_uuid(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).strip().lower()


def _normalize_mac(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).strip().lower().replace("-", ":")


def _artifact_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["id"] = str(out["id"])
    for key in ("built_at", "created_at"):
        out[key] = _iso(out.get(key))
    return out


def _run_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["run_id"] = str(out["run_id"])
    out["artifact_id"] = str(out["artifact_id"])
    out["outbound_policy"] = out.pop("outbound_policy_json") or {}
    out["archived"] = bool(out.get("archived_at"))
    for key in (
        "pe_registered_at",
        "osdeploy_started_at",
        "osdeploy_finished_at",
        "first_heartbeat_at",
        "archived_at",
        "created_at",
        "updated_at",
    ):
        out[key] = _iso(out.get(key))
    return out


def _event_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["id"] = str(out["id"])
    out["run_id"] = str(out["run_id"])
    out["data"] = out.pop("data_json") or {}
    out["created_at"] = _iso(out.get("created_at"))
    return out


def _readiness_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["run_id"] = str(out["run_id"])
    out["errors"] = out.pop("errors_json") or []
    for key in ("heartbeat_at", "updated_at"):
        out[key] = _iso(out.get(key))
    return out


def init(conn: Connection) -> None:
    global _INIT_DONE
    with _INIT_LOCK:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_INIT_LOCK_KEY,))
        ts_engine_pg.init(conn)
        conn.execute(SCHEMA)
        conn.commit()
        _INIT_DONE = True


def reset_for_tests(conn: Connection) -> None:
    global _INIT_DONE
    conn.execute(DROP_SCHEMA_FOR_TESTS)
    conn.commit()
    _INIT_DONE = False


def create_artifact(
    conn: Connection,
    *,
    architecture: str = DEFAULT_ARCHITECTURE,
    osdeploy_module_version: str = DEFAULT_OSDEPLOY_MODULE_VERSION,
    osdbuilder_module_version: str = DEFAULT_OSDBUILDER_MODULE_VERSION,
    adk_version: str = DEFAULT_ADK_VERSION,
    build_sha: str,
    iso_path: str,
    wim_path: str,
    manifest_path: str,
    iso_sha256: str,
    wim_sha256: str,
    source_media: str,
    image_name: str,
    image_index: int,
    os_version: str = DEFAULT_OS_VERSION,
    os_edition: str = DEFAULT_OS_EDITION,
    os_language: str = DEFAULT_OS_LANGUAGE,
    built_by_host: str,
    built_at: datetime | None = None,
    proxmox_volid: str | None = None,
    build_job_id: str | None = None,
    publish_job_id: str | None = None,
) -> dict:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO osdeploy_artifacts (
            id, architecture, osdeploy_module_version, osdbuilder_module_version,
            adk_version, build_sha, iso_path, wim_path, manifest_path,
            iso_sha256, wim_sha256, source_media, image_name, image_index,
            os_version, os_edition, os_language, built_by_host, built_at,
            proxmox_volid, build_job_id, publish_job_id, created_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        RETURNING *
        """,
        (
            _new_id(),
            architecture,
            osdeploy_module_version,
            osdbuilder_module_version,
            adk_version,
            build_sha,
            iso_path,
            wim_path,
            manifest_path,
            iso_sha256.lower(),
            wim_sha256.lower(),
            source_media,
            image_name,
            int(image_index),
            os_version,
            os_edition,
            os_language,
            built_by_host,
            built_at or now,
            proxmox_volid,
            build_job_id,
            publish_job_id,
            now,
        ),
    ).fetchone()
    conn.commit()
    return _artifact_row(row)


def list_artifacts(conn: Connection, *, architecture: str | None = None) -> list[dict]:
    if architecture:
        rows = conn.execute(
            """
            SELECT *
            FROM osdeploy_artifacts
            WHERE architecture = %s
            ORDER BY built_at DESC, created_at DESC
            """,
            (architecture,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT *
            FROM osdeploy_artifacts
            ORDER BY built_at DESC, created_at DESC
            """
        ).fetchall()
    return [_artifact_row(row) for row in rows]


def get_artifact(conn: Connection, artifact_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM osdeploy_artifacts WHERE id = %s",
        (artifact_id,),
    ).fetchone()
    return _artifact_row(row)


def update_artifact_proxmox_volid(
    conn: Connection,
    *,
    artifact_id: str,
    proxmox_volid: str,
    publish_job_id: str | None = None,
) -> dict | None:
    row = conn.execute(
        """
        UPDATE osdeploy_artifacts
        SET proxmox_volid = %s,
            publish_job_id = COALESCE(%s, publish_job_id)
        WHERE id = %s
        RETURNING *
        """,
        (proxmox_volid, publish_job_id, artifact_id),
    ).fetchone()
    conn.commit()
    return _artifact_row(row)


def update_artifact_publish_job(
    conn: Connection,
    *,
    artifact_id: str,
    publish_job_id: str,
) -> dict | None:
    row = conn.execute(
        """
        UPDATE osdeploy_artifacts
        SET publish_job_id = %s
        WHERE id = %s
        RETURNING *
        """,
        (publish_job_id, artifact_id),
    ).fetchone()
    conn.commit()
    return _artifact_row(row)


def _create_sequence_for_run(conn: Connection, *, name: str, server_role: str) -> str:
    sequence_id = ts_engine_pg.create_sequence(
        conn,
        name=name,
        description="Generated OSDeploy Windows Server deployment sequence",
        target_os="windows",
        created_by="osdeploy",
    )
    steps = [
        ("OSDeploy PE preflight", "osdeploy_preflight", "pe"),
        ("Apply OSDeploy Server image", "apply_wim", "pe"),
        ("Apply VirtIO driver package", "apply_driver_package", "pe"),
        ("Prepare Windows Server setup", "prepare_windows_setup", "pe"),
        ("Bake Windows boot entry", "bake_boot_entry", "pe"),
        ("Stage OSD client", "stage_osd_client", "pe"),
        ("Stage AutopilotAgent", "stage_autopilot_agent", "pe"),
        ("Wait for AutopilotAgent heartbeat", "wait_agent_heartbeat", "full_os"),
    ]
    if server_role != "base":
        steps.insert(
            -1,
            ("Run server role baseline", "run_script", "full_os"),
        )
    for position, (step_name, kind, phase) in enumerate(steps):
        ts_engine_pg.add_step(
            conn,
            sequence_id=sequence_id,
            parent_id=None,
            name=step_name,
            kind=kind,
            phase=phase,
            position=position,
            params={"server_role": server_role} if kind == "run_script" else {},
            retry_count=60 if kind == "wait_agent_heartbeat" else 0,
            retry_delay_seconds=10,
            reboot_behavior="none",
        )
    return ts_engine_pg.compile_sequence(conn, sequence_id, compiled_by="osdeploy")


def create_run(
    conn: Connection,
    *,
    artifact_id: str,
    vm_name: str,
    node: str | None = None,
    iso_storage: str | None = None,
    storage: str | None = None,
    network_bridge: str | None = None,
    requested_vmid: int | None = None,
    architecture: str = DEFAULT_ARCHITECTURE,
    server_role: str = "base",
    os_version: str = DEFAULT_OS_VERSION,
    os_edition: str = DEFAULT_OS_EDITION,
    os_language: str = DEFAULT_OS_LANGUAGE,
    vm_cores: int = DEFAULT_VM_CORES,
    vm_memory_mb: int = RECOMMENDED_VM_MEMORY_MB,
    vm_disk_size_gb: int = DEFAULT_VM_DISK_SIZE_GB,
    secure_boot: bool = True,
    outbound_policy: dict | None = None,
) -> dict:
    artifact = get_artifact(conn, artifact_id)
    if not artifact:
        raise ValueError(f"OSDeploy artifact not found: {artifact_id}")
    if artifact["architecture"] != architecture:
        raise ValueError("OSDeploy artifact architecture does not match requested run")
    expected_computer_name = normalize_windows_computer_name(vm_name)
    if not expected_computer_name:
        raise ValueError("OSDeploy requested VM name does not produce a valid Windows computer name")

    version_id = _create_sequence_for_run(
        conn,
        name=f"OSDeploy deployment for {vm_name}",
        server_role=server_role,
    )
    run_id = ts_engine_pg.create_run_from_version(
        conn,
        sequence_version_id=version_id,
        deployment_target={
            "computer_name": expected_computer_name,
            "requested_name": vm_name,
            "architecture": architecture,
            "osdeploy": True,
        },
        run_variables={
            "deployment_path": "osdeploy_v2",
            "artifact_id": artifact_id,
            "server_role": server_role,
            "os_version": os_version,
            "os_edition": os_edition,
            "os_language": os_language,
        },
        created_by="osdeploy",
        resolve_content=False,
    )
    now = _now()
    workflow_name = f"pveautopilot-osdeploy-{run_id}"
    row = conn.execute(
        """
        INSERT INTO osdeploy_runs (
            run_id, artifact_id, state, workflow_name, architecture,
            server_role, os_version, os_edition, os_language, vm_name,
            requested_vm_name, expected_computer_name, requested_vmid,
            node, iso_storage, storage, network_bridge, vm_cores,
            vm_memory_mb, vm_disk_size_gb, secure_boot, outbound_policy_json,
            created_at, updated_at
        )
        VALUES (
            %s, %s, 'created', %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s
        )
        RETURNING *
        """,
        (
            run_id,
            artifact_id,
            workflow_name,
            architecture,
            server_role,
            os_version,
            os_edition,
            os_language,
            vm_name,
            vm_name,
            expected_computer_name,
            requested_vmid,
            node,
            iso_storage,
            storage,
            network_bridge,
            int(vm_cores),
            int(vm_memory_mb),
            int(vm_disk_size_gb),
            bool(secure_boot),
            _json(outbound_policy or {}),
            now,
            now,
        ),
    ).fetchone()
    conn.execute(
        """
        UPDATE ts_provisioning_runs
        SET state = 'osdeploy_created', phase = 'osdeploy'
        WHERE id = %s
        """,
        (run_id,),
    )
    conn.execute(
        """
        INSERT INTO osdeploy_readiness (
            run_id, state, qga_status, agent_status, server_role_status,
            errors_json, updated_at
        )
        VALUES (%s, 'waiting_for_heartbeat', 'pending', 'pending', 'pending', %s, %s)
        """,
        (run_id, Jsonb([]), now),
    )
    conn.commit()
    append_event(
        conn,
        run_id=run_id,
        phase="controller",
        event_type="run_created",
        message="OSDeploy run created",
    )
    return _run_row(row)


def get_run(conn: Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM osdeploy_runs WHERE run_id = %s", (run_id,)).fetchone()
    return _run_row(row)


def list_runs(
    conn: Connection,
    *,
    limit: int = 100,
    include_archived: bool = False,
    active_only: bool = False,
    stale_failed_hours: int | None = None,
) -> list[dict]:
    where = []
    params: list[Any] = []
    if not include_archived:
        where.append("archived_at IS NULL")
    if active_only:
        where.append("state NOT IN ('complete','failed','canceled')")
    if stale_failed_hours is not None:
        where.append("state = 'failed'")
        where.append("updated_at < %s")
        params.append(_now() - timedelta(hours=stale_failed_hours))
    sql = "SELECT * FROM osdeploy_runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_run_row(row) for row in rows]


def append_event(
    conn: Connection,
    *,
    run_id: str,
    phase: str,
    event_type: str,
    severity: str = "info",
    message: str | None = None,
    data: dict | None = None,
) -> dict:
    row = conn.execute(
        """
        INSERT INTO osdeploy_run_events (
            run_id, phase, event_type, severity, message, data_json, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (run_id, phase, event_type, severity, message, _json(data or {}), _now()),
    ).fetchone()
    conn.commit()
    _mark_steps_done_from_event(
        conn,
        run_id=run_id,
        phase=phase,
        event_type=event_type,
        data=data or {},
    )
    _mark_pe_phase_progress_from_event(
        conn,
        run_id=run_id,
        phase=phase,
        event_type=event_type,
    )
    return _event_row(row)


def _mark_pe_phase_progress_from_event(
    conn: Connection,
    *,
    run_id: str,
    phase: str,
    event_type: str,
) -> None:
    if phase != "pe" or event_type == "pe_registered":
        return
    now = _now()
    if event_type == "osdeploy_boot_files_staged":
        conn.execute(
            """
            UPDATE osdeploy_runs
            SET osdeploy_started_at = COALESCE(osdeploy_started_at, pe_registered_at, %s),
                osdeploy_finished_at = COALESCE(osdeploy_finished_at, %s),
                updated_at = %s
            WHERE run_id = %s
            """,
            (now, now, now, run_id),
        )
    else:
        conn.execute(
            """
            UPDATE osdeploy_runs
            SET osdeploy_started_at = COALESCE(osdeploy_started_at, pe_registered_at, %s),
                updated_at = %s
            WHERE run_id = %s
            """,
            (now, now, run_id),
        )
    conn.commit()


def _mark_steps_done_from_event(
    conn: Connection,
    *,
    run_id: str,
    phase: str,
    event_type: str,
    data: dict,
) -> int:
    if phase != "pe":
        return 0
    kinds = PE_EVENT_STEP_KINDS.get(event_type)
    if not kinds:
        return 0
    return ts_engine_pg.mark_steps_done_by_kind(
        conn,
        run_id=run_id,
        kinds=kinds,
        agent_id="osdeploy-pe",
        message=f"OSDeploy PE event completed {event_type}",
        data={"event_type": event_type, **(data or {})},
    )


def list_events(conn: Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM osdeploy_run_events
        WHERE run_id = %s
        ORDER BY created_at ASC, id ASC
        """,
        (run_id,),
    ).fetchall()
    return [_event_row(row) for row in rows]


def get_readiness(conn: Connection, run_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM osdeploy_readiness WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    if row:
        return _readiness_row(row)
    return {
        "run_id": run_id,
        "state": "waiting_for_heartbeat",
        "qga_status": "pending",
        "agent_status": "pending",
        "heartbeat_at": None,
        "server_role_status": "pending",
        "errors": [],
        "updated_at": None,
    }


def set_run_identity(
    conn: Connection,
    *,
    run_id: str,
    vmid: int | None = None,
    vm_uuid: str | None = None,
    mac: str | None = None,
    node: str | None = None,
    computer_name: str | None = None,
) -> dict | None:
    row = conn.execute(
        """
        UPDATE osdeploy_runs
        SET vmid = COALESCE(%s, vmid),
            vm_uuid = COALESCE(NULLIF(%s, ''), vm_uuid),
            mac = COALESCE(NULLIF(%s, ''), mac),
            node = COALESCE(NULLIF(%s, ''), node),
            pve_vm_name = COALESCE(NULLIF(%s, ''), pve_vm_name),
            state = CASE WHEN state = 'created' THEN 'awaiting_pe' ELSE state END,
            updated_at = %s
        WHERE run_id = %s
        RETURNING *
        """,
        (vmid, _normalize_uuid(vm_uuid), _normalize_mac(mac), node, computer_name, _now(), run_id),
    ).fetchone()
    conn.execute(
        """
        UPDATE ts_provisioning_runs
        SET vmid = COALESCE(%s, vmid),
            vm_uuid = COALESCE(NULLIF(%s, ''), vm_uuid),
            state = CASE WHEN state = 'osdeploy_created' THEN 'osdeploy_awaiting_pe' ELSE state END
        WHERE id = %s
        """,
        (vmid, _normalize_uuid(vm_uuid), run_id),
    )
    conn.commit()
    run = _run_row(row)
    if run:
        append_event(
            conn,
            run_id=run_id,
            phase="controller",
            event_type="identity_recorded",
            message="OSDeploy VM identity recorded",
            data={"vmid": vmid, "vm_uuid": vm_uuid, "mac": mac, "node": node},
        )
    return run


def find_run_by_identity(
    conn: Connection,
    *,
    vm_uuid: str,
    mac: str,
    architecture: str | None = None,
    build_sha: str | None = None,
) -> dict | None:
    row = conn.execute(
        """
        SELECT r.*
        FROM osdeploy_runs r
        JOIN osdeploy_artifacts a ON a.id = r.artifact_id
        WHERE r.vm_uuid = %s
          AND r.mac = %s
          AND (%s::text IS NULL OR r.architecture = %s)
          AND (%s::text IS NULL OR a.build_sha = %s)
          AND r.state IN ('awaiting_pe', 'created', 'pe_registered')
        ORDER BY r.created_at DESC
        LIMIT 1
        """,
        (
            _normalize_uuid(vm_uuid),
            _normalize_mac(mac),
            architecture,
            architecture,
            build_sha,
            build_sha,
        ),
    ).fetchone()
    return _run_row(row)


def mark_pe_registered(conn: Connection, *, run_id: str, metadata: dict | None = None) -> dict | None:
    now = _now()
    row = conn.execute(
        """
        UPDATE osdeploy_runs
        SET state = 'pe_registered',
            pe_registered_at = COALESCE(pe_registered_at, %s),
            updated_at = %s
        WHERE run_id = %s
        RETURNING *
        """,
        (now, now, run_id),
    ).fetchone()
    conn.execute(
        """
        UPDATE ts_provisioning_runs
        SET state = 'osdeploy_pe_registered', phase = 'pe'
        WHERE id = %s
        """,
        (run_id,),
    )
    conn.commit()
    run = _run_row(row)
    if run:
        append_event(
            conn,
            run_id=run_id,
            phase="pe",
            event_type="pe_registered",
            message="OSDeploy PE client registered",
            data=metadata or {},
        )
    return run


def mark_complete_from_heartbeat(
    conn: Connection,
    *,
    run_id: str,
    agent_id: str,
    heartbeat: dict,
) -> dict | None:
    run = get_run(conn, run_id)
    if not run:
        return None
    now = _now()
    role_is_ready = run.get("server_role") in M1_LAUNCHABLE_SERVER_ROLES
    step_rows = conn.execute(
        """
        SELECT DISTINCT kind
        FROM ts_run_plan_steps
        WHERE run_id = %s
          AND state <> 'skipped'
          AND (%s OR kind <> 'run_script')
        """,
        (run_id, role_is_ready),
    ).fetchall()
    ts_engine_pg.mark_steps_done_by_kind(
        conn,
        run_id=run_id,
        kinds=[row["kind"] for row in step_rows],
        agent_id=agent_id,
        message="OSDeploy Server base completed from agent heartbeat",
        data={
            "computer_name": heartbeat.get("computer_name"),
            "os_name": heartbeat.get("os_name"),
            "qga_state": heartbeat.get("qga_state"),
            "current_phase": heartbeat.get("current_phase"),
        },
    )
    next_state = "complete" if role_is_ready else "role_pending"
    readiness_state = "complete" if role_is_ready else "role_pending"
    server_role_status = "base_ready" if role_is_ready else "pending"
    row = conn.execute(
        """
        UPDATE osdeploy_runs
        SET state = %s,
            first_heartbeat_at = COALESCE(first_heartbeat_at, %s),
            updated_at = %s
        WHERE run_id = %s
        RETURNING *
        """,
        (next_state, now, now, run_id),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO osdeploy_readiness (
            run_id, state, qga_status, agent_status, heartbeat_at,
            server_role_status, errors_json, updated_at
        )
        VALUES (%s, %s, %s, 'online', %s, %s, %s, %s)
        ON CONFLICT (run_id) DO UPDATE SET
            state = EXCLUDED.state,
            qga_status = EXCLUDED.qga_status,
            agent_status = EXCLUDED.agent_status,
            heartbeat_at = EXCLUDED.heartbeat_at,
            server_role_status = EXCLUDED.server_role_status,
            errors_json = EXCLUDED.errors_json,
            updated_at = EXCLUDED.updated_at
        """,
        (
            run_id,
            readiness_state,
            heartbeat.get("qga_state") or "unknown",
            now,
            server_role_status,
            Jsonb([]),
            now,
        ),
    )
    if role_is_ready:
        conn.execute(
            """
            UPDATE ts_provisioning_runs
            SET state = 'done',
                phase = 'full_os',
                finished_at = COALESCE(finished_at, %s)
            WHERE id = %s
              AND state <> 'failed'
            """,
            (now, run_id),
        )
    else:
        conn.execute(
            """
            UPDATE ts_provisioning_runs
            SET state = 'role_pending',
                phase = 'full_os'
            WHERE id = %s
              AND state <> 'failed'
            """,
            (run_id,),
        )
    conn.commit()
    append_event(
        conn,
        run_id=run_id,
        phase="full_os",
        event_type="agent_heartbeat",
        message="OSDeploy Server base heartbeat observed",
        data={
            "agent_id": agent_id,
            "computer_name": heartbeat.get("computer_name"),
            "os_name": heartbeat.get("os_name"),
            "qga_state": heartbeat.get("qga_state"),
        },
    )
    return _run_row(row)


def mark_failed_from_job(
    conn: Connection,
    *,
    run_id: str,
    job_id: str,
    exit_code: int | None = None,
    message: str | None = None,
    vmid: int | None = None,
    phase: str = "provision",
    log_tail: str | None = None,
) -> dict | None:
    run = get_run(conn, run_id)
    if not run or run.get("state") in {"complete", "canceled"}:
        return None
    event_exists = conn.execute(
        """
        SELECT 1
        FROM osdeploy_run_events
        WHERE run_id = %s
          AND event_type = 'provision_job_failed'
          AND data_json->>'job_id' = %s
        LIMIT 1
        """,
        (run_id, job_id),
    ).fetchone()
    if run.get("state") == "failed" and event_exists:
        return None
    now = _now()
    error_message = (
        (message or "").strip()
        or f"OSDeploy provision job {job_id} failed"
    )
    existing_readiness = get_readiness(conn, run_id)
    errors = list(existing_readiness.get("errors") or [])
    error_entry = {
        "source": "builder",
        "job_id": job_id,
        "exit_code": exit_code,
        "message": error_message,
    }
    if vmid is not None:
        error_entry["vmid"] = int(vmid)
    if log_tail:
        error_entry["log_tail"] = log_tail
    replaced = False
    for index, existing in enumerate(errors):
        if existing.get("source") == "builder" and existing.get("job_id") == job_id:
            errors[index] = error_entry
            replaced = True
            break
    if not replaced:
        errors.append(error_entry)

    row = conn.execute(
        """
        UPDATE osdeploy_runs
        SET state = 'failed',
            vmid = COALESCE(%s, vmid),
            updated_at = %s
        WHERE run_id = %s
          AND state NOT IN ('complete', 'canceled')
        RETURNING *
        """,
        (vmid, now, run_id),
    ).fetchone()
    conn.execute(
        """
        UPDATE ts_provisioning_runs
        SET state = 'failed',
            phase = %s,
            vmid = COALESCE(%s, vmid),
            finished_at = COALESCE(finished_at, %s),
            last_error = %s
        WHERE id = %s
          AND state <> 'done'
        """,
        (phase, vmid, now, error_message, run_id),
    )
    conn.execute(
        """
        INSERT INTO osdeploy_readiness (
            run_id, state, qga_status, agent_status, heartbeat_at,
            server_role_status, errors_json, updated_at
        )
        VALUES (%s, 'failed', 'failed', 'failed', NULL, 'failed', %s, %s)
        ON CONFLICT (run_id) DO UPDATE SET
            state = 'failed',
            qga_status = 'failed',
            agent_status = 'failed',
            server_role_status = 'failed',
            errors_json = EXCLUDED.errors_json,
            updated_at = EXCLUDED.updated_at
        """,
        (run_id, Jsonb(errors), now),
    )
    conn.commit()
    failed = _run_row(row)
    if failed and not event_exists:
        data = {
            "job_id": job_id,
            "exit_code": exit_code,
            "vmid": vmid,
        }
        if log_tail:
            data["log_tail"] = log_tail
        append_event(
            conn,
            run_id=run_id,
            phase=phase,
            event_type="provision_job_failed",
            severity="error",
            message=error_message,
            data=data,
        )
    return failed


def archive_run(
    conn: Connection,
    run_id: str,
    *,
    archived_by: str = "operator",
    reason: str = "",
) -> dict | None:
    now = _now()
    row = conn.execute(
        """
        UPDATE osdeploy_runs
        SET archived_at = COALESCE(archived_at, %s),
            archived_by = COALESCE(archived_by, %s),
            archive_reason = COALESCE(NULLIF(archive_reason, ''), NULLIF(%s, '')),
            updated_at = %s
        WHERE run_id = %s
        RETURNING *
        """,
        (now, archived_by, reason, now, run_id),
    ).fetchone()
    conn.commit()
    return _run_row(row)


def unarchive_run(conn: Connection, run_id: str) -> dict | None:
    row = conn.execute(
        """
        UPDATE osdeploy_runs
        SET archived_at = NULL,
            archived_by = NULL,
            archive_reason = NULL,
            updated_at = %s
        WHERE run_id = %s
        RETURNING *
        """,
        (_now(), run_id),
    ).fetchone()
    conn.commit()
    return _run_row(row)


def archive_runs_by_filter(
    conn: Connection,
    *,
    state: str,
    older_than_hours: int,
    reason: str,
) -> list[dict]:
    rows = conn.execute(
        """
        UPDATE osdeploy_runs
        SET archived_at = %s,
            archived_by = 'operator',
            archive_reason = %s,
            updated_at = %s
        WHERE archived_at IS NULL
          AND state = %s
          AND updated_at < %s
        RETURNING *
        """,
        (_now(), reason, _now(), state, _now() - timedelta(hours=older_than_hours)),
    ).fetchall()
    conn.commit()
    return [_run_row(row) for row in rows]
