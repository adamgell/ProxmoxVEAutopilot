"""PostgreSQL repository for CloudOSD deployment artifacts and runs."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import ts_engine_pg


SCHEMA = """
CREATE TABLE IF NOT EXISTS cloudosd_artifacts (
    id uuid PRIMARY KEY,
    architecture text NOT NULL,
    osdcloud_module_version text NOT NULL,
    build_sha text NOT NULL,
    iso_path text NOT NULL,
    wim_path text NOT NULL,
    manifest_path text NOT NULL,
    iso_sha256 text NOT NULL,
    wim_sha256 text NOT NULL,
    built_by_host text NOT NULL,
    built_at timestamptz NOT NULL,
    proxmox_volid text NULL,
    build_job_id text NULL,
    publish_job_id text NULL,
    created_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cloudosd_artifacts_lookup
    ON cloudosd_artifacts(architecture, osdcloud_module_version, build_sha);

CREATE TABLE IF NOT EXISTS cloudosd_runs (
    run_id uuid PRIMARY KEY REFERENCES ts_provisioning_runs(id) ON DELETE CASCADE,
    artifact_id uuid NOT NULL REFERENCES cloudosd_artifacts(id),
    state text NOT NULL,
    workflow_name text NOT NULL UNIQUE,
    architecture text NOT NULL,
    os_version text NOT NULL,
    os_activation text NOT NULL,
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
    tpm_enabled boolean NOT NULL DEFAULT true,
    secure_boot boolean NOT NULL DEFAULT true,
    firmware_updates_enabled boolean NOT NULL DEFAULT false,
    driver_pack_policy text NOT NULL DEFAULT 'None',
    analytics_enabled boolean NOT NULL DEFAULT false,
    outbound_policy_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    vmid integer NULL,
    vm_uuid text NULL,
    mac text NULL,
    pe_registered_at timestamptz NULL,
    osdcloud_started_at timestamptz NULL,
    osdcloud_finished_at timestamptz NULL,
    first_heartbeat_at timestamptz NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cloudosd_runs_state
    ON cloudosd_runs(state, created_at);

CREATE INDEX IF NOT EXISTS idx_cloudosd_runs_identity
    ON cloudosd_runs(vm_uuid, mac)
    WHERE vm_uuid IS NOT NULL AND mac IS NOT NULL;

CREATE TABLE IF NOT EXISTS cloudosd_run_events (
    id bigserial PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES cloudosd_runs(run_id) ON DELETE CASCADE,
    phase text NOT NULL,
    event_type text NOT NULL,
    severity text NOT NULL DEFAULT 'info',
    message text NULL,
    data_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL
);
"""

DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS cloudosd_run_events CASCADE;
DROP TABLE IF EXISTS cloudosd_runs CASCADE;
DROP TABLE IF EXISTS cloudosd_artifacts CASCADE;
"""


DEFAULT_OS_VERSION = "Windows 11 25H2"
DEFAULT_OS_ACTIVATION = "Volume"
DEFAULT_OS_EDITION = "Enterprise"
DEFAULT_OS_LANGUAGE = "en-us"
DEFAULT_ARCHITECTURE = "amd64"
DEFAULT_OSDCLOUD_MODULE_VERSION = "26.4.17.1"
DEFAULT_DRIVER_PACK_POLICY = "None"
# OSDCloud 26.4.17.1 amd64 catalogs include Windows 10 22H2 and
# Windows 11 21H2 through 25H2. Keep these values aligned with the
# pinned module's deployable workflow/catalog values.
OS_VERSION_CATALOG = [
    DEFAULT_OS_VERSION,
    "Windows 11 24H2",
    "Windows 11 23H2",
    "Windows 11 22H2",
    "Windows 11 21H2",
    "Windows 10 22H2",
]
OS_ACTIVATION_CATALOG = ["Volume", "Retail"]
OS_EDITION_CATALOG = [
    "Home",
    "Home N",
    "Education",
    "Education N",
    "Pro",
    "Pro N",
    DEFAULT_OS_EDITION,
    "Enterprise N",
]
OS_LANGUAGE_CATALOG = [
    "ar-sa",
    "bg-bg",
    "cs-cz",
    "da-dk",
    "de-de",
    "el-gr",
    "en-gb",
    DEFAULT_OS_LANGUAGE,
    "es-es",
    "es-mx",
    "et-ee",
    "fi-fi",
    "fr-ca",
    "fr-fr",
    "he-il",
    "hr-hr",
    "hu-hu",
    "it-it",
    "ja-jp",
    "ko-kr",
    "lt-lt",
    "lv-lv",
    "nb-no",
    "nl-nl",
    "pl-pl",
    "pt-br",
    "pt-pt",
    "ro-ro",
    "ru-ru",
    "sk-sk",
    "sl-si",
    "sr-latn-rs",
    "sv-se",
    "th-th",
    "tr-tr",
    "uk-ua",
    "zh-cn",
    "zh-tw",
]
DEFAULT_VM_CORES = 4
DEFAULT_VM_MEMORY_MB = 8192
DEFAULT_VM_DISK_SIZE_GB = 80
MIN_VM_MEMORY_MB = 6144
RECOMMENDED_VM_MEMORY_MB = 8192
MIN_VM_DISK_SIZE_GB = 80
CLOUDOSD_MILESTONE_LABELS = [
    "controller",
    "Proxmox playbook",
    "PE bridge",
    "OSDCloud",
    "offline validation",
    "SetupComplete",
    "first boot",
    "AutopilotAgent",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


def _json(value: Any) -> Jsonb:
    return Jsonb(value)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat(timespec="seconds")
    return str(value)


def _normalize_uuid(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).strip().lower()


def _normalize_mac(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).strip().lower().replace("-", ":")


def normalize_windows_computer_name(value: str | None) -> str:
    """Match the CloudOSD PE bridge's offline unattend computer-name rule."""
    text = (value or "").strip()
    normalized = re.sub(r"[^A-Za-z0-9-]", "", text)
    if not normalized:
        return ""
    normalized = normalized[:15]
    if re.fullmatch(r"\d+", normalized):
        normalized = f"PVE-{normalized}"[:15]
    return normalized


def name_comparison(
    *,
    requested_name: str | None,
    pve_name: str | None = None,
    heartbeat_name: str | None = None,
) -> dict:
    expected = normalize_windows_computer_name(requested_name)

    def _norm(value: str | None) -> str:
        return normalize_windows_computer_name(value)

    stripped_requested = (requested_name or "").strip()
    stripped_chars = re.sub(r"[^A-Za-z0-9-]", "", stripped_requested)
    pve_normalized = _norm(pve_name)
    heartbeat_normalized = _norm(heartbeat_name)
    expected_key = expected.casefold()
    pve_mismatch = (
        bool(pve_normalized)
        and bool(expected)
        and pve_normalized.casefold() != expected_key
    )
    heartbeat_mismatch = (
        bool(heartbeat_normalized)
        and bool(expected)
        and heartbeat_normalized.casefold() != expected_key
    )
    return {
        "requested_name": requested_name,
        "expected_computer_name": expected,
        "pve_name": pve_name,
        "heartbeat_computer_name": heartbeat_name,
        "pve_normalized": pve_normalized,
        "heartbeat_normalized": heartbeat_normalized,
        "requested_was_normalized": bool(stripped_requested)
        and expected != stripped_requested,
        "truncated": bool(stripped_requested) and len(stripped_chars) > 15,
        "mismatch": bool(pve_mismatch or heartbeat_mismatch),
        "pve_mismatch": pve_mismatch,
        "heartbeat_mismatch": heartbeat_mismatch,
    }


def milestone_label_for_event(event: dict) -> str:
    phase = (event.get("phase") or "").strip()
    event_type = (event.get("event_type") or "").strip()
    phase_key = phase.casefold()
    event_key = event_type.casefold()
    if phase_key == "controller" or event_key in {"run_created", "identity_recorded"}:
        return "controller"
    if event_key == "pe_registered":
        return "PE bridge"
    if "osdcloud" in event_key:
        return "OSDCloud"
    if "validation" in event_key or phase_key == "offline_validation":
        return "offline validation"
    if "setupcomplete" in event_key or phase_key in {"setupcomplete", "setup_complete"}:
        return "SetupComplete"
    if "firstboot" in event_key or phase_key in {"first_boot", "first boot"}:
        return "first boot"
    if "autopilotagent" in event_key or phase_key == "full_os":
        return "AutopilotAgent"
    if phase_key == "pe":
        return "PE bridge"
    return phase or "controller"


def milestone_event_groups(events: list[dict]) -> dict[str, list[dict]]:
    groups = {label: [] for label in CLOUDOSD_MILESTONE_LABELS}
    for event in events:
        groups.setdefault(milestone_label_for_event(event), []).append(event)
    return groups


def init(conn: Connection) -> None:
    ts_engine_pg.init(conn)
    conn.execute(SCHEMA)
    conn.execute("ALTER TABLE cloudosd_artifacts ADD COLUMN IF NOT EXISTS build_job_id text NULL")
    conn.execute("ALTER TABLE cloudosd_artifacts ADD COLUMN IF NOT EXISTS publish_job_id text NULL")
    conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS requested_vm_name text NULL")
    conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS pve_vm_name text NULL")
    conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS expected_computer_name text NULL")
    conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS requested_vmid integer NULL")
    conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS iso_storage text NULL")
    conn.commit()


def reset_for_tests(conn: Connection) -> None:
    conn.execute(DROP_SCHEMA_FOR_TESTS)
    conn.commit()


def _artifact_row(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "id": str(row["id"]),
        "architecture": row["architecture"],
        "osdcloud_module_version": row["osdcloud_module_version"],
        "build_sha": row["build_sha"],
        "iso_path": row["iso_path"],
        "wim_path": row["wim_path"],
        "manifest_path": row["manifest_path"],
        "iso_sha256": row["iso_sha256"],
        "wim_sha256": row["wim_sha256"],
        "built_by_host": row["built_by_host"],
        "built_at": _iso(row["built_at"]),
        "proxmox_volid": row["proxmox_volid"],
        "build_job_id": row.get("build_job_id"),
        "publish_job_id": row.get("publish_job_id"),
        "created_at": _iso(row["created_at"]),
    }


def _run_row(row: dict | None) -> dict | None:
    if not row:
        return None
    requested_name = row.get("requested_vm_name") or row["vm_name"]
    expected_name = row.get("expected_computer_name") or normalize_windows_computer_name(
        requested_name,
    )
    return {
        "run_id": str(row["run_id"]),
        "artifact_id": str(row["artifact_id"]),
        "state": row["state"],
        "workflow_name": row["workflow_name"],
        "architecture": row["architecture"],
        "os_version": row["os_version"],
        "os_activation": row["os_activation"],
        "os_edition": row["os_edition"],
        "os_language": row["os_language"],
        "vm_name": row["vm_name"],
        "requested_vm_name": requested_name,
        "pve_vm_name": row.get("pve_vm_name"),
        "expected_computer_name": expected_name,
        "requested_vmid": row.get("requested_vmid"),
        "node": row["node"],
        "iso_storage": row.get("iso_storage"),
        "storage": row["storage"],
        "network_bridge": row["network_bridge"],
        "vm_cores": row["vm_cores"],
        "vm_memory_mb": row["vm_memory_mb"],
        "vm_disk_size_gb": row["vm_disk_size_gb"],
        "tpm_enabled": row["tpm_enabled"],
        "secure_boot": row["secure_boot"],
        "firmware_updates_enabled": row["firmware_updates_enabled"],
        "driver_pack_policy": row["driver_pack_policy"],
        "analytics_enabled": row["analytics_enabled"],
        "outbound_policy": row["outbound_policy_json"] or {},
        "vmid": row["vmid"],
        "vm_uuid": row["vm_uuid"],
        "mac": row["mac"],
        "pe_registered_at": _iso(row["pe_registered_at"]),
        "osdcloud_started_at": _iso(row["osdcloud_started_at"]),
        "osdcloud_finished_at": _iso(row["osdcloud_finished_at"]),
        "first_heartbeat_at": _iso(row["first_heartbeat_at"]),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def create_artifact(
    conn: Connection,
    *,
    architecture: str,
    osdcloud_module_version: str,
    build_sha: str,
    iso_path: str,
    wim_path: str,
    manifest_path: str,
    iso_sha256: str,
    wim_sha256: str,
    built_by_host: str,
    built_at: datetime | None = None,
    proxmox_volid: str | None = None,
    build_job_id: str | None = None,
    publish_job_id: str | None = None,
) -> dict:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO cloudosd_artifacts (
            id, architecture, osdcloud_module_version, build_sha,
            iso_path, wim_path, manifest_path, iso_sha256, wim_sha256,
            built_by_host, built_at, proxmox_volid, build_job_id,
            publish_job_id, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            _new_id(),
            architecture,
            osdcloud_module_version,
            build_sha,
            iso_path,
            wim_path,
            manifest_path,
            iso_sha256.lower(),
            wim_sha256.lower(),
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
            FROM cloudosd_artifacts
            WHERE architecture = %s
            ORDER BY built_at DESC, created_at DESC
            """,
            (architecture,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT *
            FROM cloudosd_artifacts
            ORDER BY built_at DESC, created_at DESC
            """
        ).fetchall()
    return [_artifact_row(row) for row in rows]


def get_artifact(conn: Connection, artifact_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM cloudosd_artifacts WHERE id = %s",
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
        UPDATE cloudosd_artifacts
        SET proxmox_volid = %s,
            publish_job_id = COALESCE(%s, publish_job_id)
        WHERE id = %s
        RETURNING *
        """,
        (proxmox_volid, publish_job_id, artifact_id),
    ).fetchone()
    conn.commit()
    return _artifact_row(row)


def _create_sequence_for_run(conn: Connection, *, name: str) -> str:
    sequence_id = ts_engine_pg.create_sequence(
        conn,
        name=name,
        description="Generated CloudOSD deployment sequence",
        created_by="cloudosd",
    )
    steps = [
        ("CloudOSD PE preflight", "cloudosd_preflight", "pe"),
        ("Run OSDCloud workflow", "cloudosd_deploy_os", "pe"),
        ("Validate offline Windows", "cloudosd_validate_offline_os", "pe"),
        ("Stage OSD client", "stage_osd_client", "pe"),
        ("Stage AutopilotAgent", "stage_autopilot_agent", "pe"),
        ("Wait for AutopilotAgent heartbeat", "wait_agent_heartbeat", "full_os"),
    ]
    for position, (step_name, kind, phase) in enumerate(steps):
        ts_engine_pg.add_step(
            conn,
            sequence_id=sequence_id,
            parent_id=None,
            name=step_name,
            kind=kind,
            phase=phase,
            position=position,
            params={},
            retry_count=0,
            reboot_behavior="none",
        )
    return ts_engine_pg.compile_sequence(conn, sequence_id, compiled_by="cloudosd")


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
    os_version: str = DEFAULT_OS_VERSION,
    os_activation: str = DEFAULT_OS_ACTIVATION,
    os_edition: str = DEFAULT_OS_EDITION,
    os_language: str = DEFAULT_OS_LANGUAGE,
    vm_cores: int = DEFAULT_VM_CORES,
    vm_memory_mb: int = DEFAULT_VM_MEMORY_MB,
    vm_disk_size_gb: int = DEFAULT_VM_DISK_SIZE_GB,
    tpm_enabled: bool = True,
    secure_boot: bool = True,
    firmware_updates_enabled: bool = False,
    driver_pack_policy: str = DEFAULT_DRIVER_PACK_POLICY,
    analytics_enabled: bool = False,
    outbound_policy: Optional[dict] = None,
) -> dict:
    artifact = get_artifact(conn, artifact_id)
    if not artifact:
        raise ValueError(f"CloudOSD artifact not found: {artifact_id}")
    if artifact["architecture"] != architecture:
        raise ValueError("CloudOSD artifact architecture does not match requested run")

    expected_computer_name = normalize_windows_computer_name(vm_name)
    if not expected_computer_name:
        raise ValueError(
            "CloudOSD requested VM name does not produce a valid Windows computer name",
        )

    version_id = _create_sequence_for_run(
        conn,
        name=f"CloudOSD deployment for {vm_name}",
    )
    run_id = ts_engine_pg.create_run_from_version(
        conn,
        sequence_version_id=version_id,
        deployment_target={
            "computer_name": expected_computer_name,
            "requested_name": vm_name,
            "architecture": architecture,
            "cloudosd": True,
        },
        run_variables={
            "deployment_path": "cloudosd",
            "artifact_id": artifact_id,
            "os_version": os_version,
            "os_activation": os_activation,
            "os_edition": os_edition,
            "os_language": os_language,
            "driver_pack_policy": driver_pack_policy,
            "firmware_updates_enabled": firmware_updates_enabled,
        },
        created_by="cloudosd",
        resolve_content=False,
    )
    workflow_name = f"pveautopilot-{run_id}"
    now = _now()
    row = conn.execute(
        """
        INSERT INTO cloudosd_runs (
            run_id, artifact_id, state, workflow_name, architecture,
            os_version, os_activation, os_edition, os_language, vm_name,
            requested_vm_name, expected_computer_name, requested_vmid,
            node, iso_storage, storage, network_bridge, vm_cores,
            vm_memory_mb, vm_disk_size_gb, tpm_enabled, secure_boot,
            firmware_updates_enabled, driver_pack_policy, analytics_enabled,
            outbound_policy_json, created_at, updated_at
        )
        VALUES (
            %s, %s, 'created', %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s
        )
        RETURNING *
        """,
        (
            run_id,
            artifact_id,
            workflow_name,
            architecture,
            os_version,
            os_activation,
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
            vm_cores,
            vm_memory_mb,
            vm_disk_size_gb,
            tpm_enabled,
            secure_boot,
            firmware_updates_enabled,
            driver_pack_policy,
            analytics_enabled,
            _json(outbound_policy or {}),
            now,
            now,
        ),
    ).fetchone()
    conn.execute(
        """
        UPDATE ts_provisioning_runs
        SET state = 'cloudosd_created', phase = 'cloudosd'
        WHERE id = %s
        """,
        (run_id,),
    )
    conn.commit()
    append_event(
        conn,
        run_id=run_id,
        phase="controller",
        event_type="run_created",
        message="CloudOSD run created",
    )
    return _run_row(row)


def get_run(conn: Connection, run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM cloudosd_runs WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    return _run_row(row)


def list_runs(conn: Connection, *, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM cloudosd_runs
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [_run_row(row) for row in rows]


def set_run_identity(
    conn: Connection,
    *,
    run_id: str,
    vmid: int,
    vm_uuid: str,
    mac: str,
    node: str | None = None,
    computer_name: str | None = None,
) -> dict | None:
    now = _now()
    normalized_uuid = _normalize_uuid(vm_uuid)
    normalized_mac = _normalize_mac(mac)
    resolved_computer_name = (computer_name or "").strip() or None
    row = conn.execute(
        """
        UPDATE cloudosd_runs
        SET state = 'awaiting_pe',
            vmid = %s,
            vm_uuid = %s,
            mac = %s,
            node = COALESCE(%s, node),
            pve_vm_name = COALESCE(%s, pve_vm_name),
            updated_at = %s
        WHERE run_id = %s
        RETURNING *
        """,
        (
            vmid,
            normalized_uuid,
            normalized_mac,
            node,
            resolved_computer_name,
            now,
            run_id,
        ),
    ).fetchone()
    if not row:
        conn.commit()
        return None
    conn.execute(
        """
        UPDATE ts_provisioning_runs
        SET state = 'awaiting_pe',
            phase = 'pe',
            vmid = %s,
            vm_uuid = %s,
            computer_name = %s
        WHERE id = %s
        """,
        (
            vmid,
            normalized_uuid,
            row.get("pve_vm_name") or row.get("expected_computer_name") or row["vm_name"],
            run_id,
        ),
    )
    conn.commit()
    append_event(
        conn,
        run_id=run_id,
        phase="controller",
        event_type="identity_recorded",
        message="CloudOSD VM identity recorded",
        data={
            "vmid": vmid,
            "vm_uuid": normalized_uuid,
            "mac": normalized_mac,
            "computer_name": row.get("pve_vm_name") or row["vm_name"],
        },
    )
    return _run_row(row)


def find_run_by_identity(
    conn: Connection,
    *,
    vm_uuid: str,
    mac: str,
    architecture: str | None = None,
    build_sha: str | None = None,
) -> dict | None:
    normalized_uuid = _normalize_uuid(vm_uuid)
    normalized_mac = _normalize_mac(mac)
    row = conn.execute(
        """
        SELECT r.*
        FROM cloudosd_runs r
        JOIN cloudosd_artifacts a ON a.id = r.artifact_id
        WHERE r.vm_uuid = %s
          AND r.mac = %s
          AND (%s::text IS NULL OR r.architecture = %s)
          AND (%s::text IS NULL OR a.build_sha = %s)
          AND r.state IN ('awaiting_pe', 'created', 'pe_registered')
        ORDER BY r.created_at DESC
        LIMIT 1
        """,
        (
            normalized_uuid,
            normalized_mac,
            architecture,
            architecture,
            build_sha,
            build_sha,
        ),
    ).fetchone()
    return _run_row(row)


def mark_pe_registered(conn: Connection, *, run_id: str) -> dict | None:
    now = _now()
    row = conn.execute(
        """
        UPDATE cloudosd_runs
        SET state = 'pe_registered',
            pe_registered_at = COALESCE(pe_registered_at, %s),
            updated_at = %s
        WHERE run_id = %s
        RETURNING *
        """,
        (now, now, run_id),
    ).fetchone()
    if row:
        conn.execute(
            """
            UPDATE ts_provisioning_runs
            SET state = 'pe_registered', phase = 'pe'
            WHERE id = %s
            """,
            (run_id,),
        )
    conn.commit()
    if row:
        append_event(
            conn,
            run_id=run_id,
            phase="pe",
            event_type="pe_registered",
            message="CloudOSD PE bridge registered",
        )
    return _run_row(row)


def mark_complete_from_heartbeat(
    conn: Connection,
    *,
    run_id: str,
    heartbeat_at: datetime,
) -> dict | None:
    now = _now()
    row = conn.execute(
        """
        UPDATE cloudosd_runs
        SET state = 'complete',
            first_heartbeat_at = COALESCE(first_heartbeat_at, %s),
            updated_at = %s
        WHERE run_id = %s
          AND state <> 'failed'
        RETURNING *
        """,
        (heartbeat_at, now, run_id),
    ).fetchone()
    if row:
        conn.execute(
            """
            UPDATE ts_provisioning_runs
            SET state = 'done',
                phase = 'full_os',
                finished_at = COALESCE(finished_at, %s)
            WHERE id = %s
            """,
            (heartbeat_at, run_id),
        )
    conn.commit()
    if row:
        append_event(
            conn,
            run_id=run_id,
            phase="full_os",
            event_type="autopilotagent_heartbeat",
            message="AutopilotAgent heartbeat observed for CloudOSD run",
            data={"first_heartbeat_at": _iso(heartbeat_at)},
        )
    return _run_row(row)


def mark_failed(
    conn: Connection,
    *,
    run_id: str,
    message: str | None = None,
) -> dict | None:
    now = _now()
    row = conn.execute(
        """
        UPDATE cloudosd_runs
        SET state = 'failed',
            updated_at = %s
        WHERE run_id = %s
          AND state <> 'complete'
        RETURNING *
        """,
        (now, run_id),
    ).fetchone()
    if row:
        conn.execute(
            """
            UPDATE ts_provisioning_runs
            SET state = 'failed',
                phase = COALESCE(phase, 'pe'),
                finished_at = COALESCE(finished_at, %s),
                last_error = COALESCE(%s, last_error)
            WHERE id = %s
            """,
            (now, message, run_id),
        )
    conn.commit()
    return _run_row(row)


def mark_osdcloud_started(conn: Connection, *, run_id: str) -> dict | None:
    now = _now()
    row = conn.execute(
        """
        UPDATE cloudosd_runs
        SET osdcloud_started_at = COALESCE(osdcloud_started_at, %s),
            updated_at = %s
        WHERE run_id = %s
        RETURNING *
        """,
        (now, now, run_id),
    ).fetchone()
    conn.commit()
    return _run_row(row)


def mark_osdcloud_finished(conn: Connection, *, run_id: str) -> dict | None:
    now = _now()
    row = conn.execute(
        """
        UPDATE cloudosd_runs
        SET osdcloud_finished_at = COALESCE(osdcloud_finished_at, %s),
            updated_at = %s
        WHERE run_id = %s
        RETURNING *
        """,
        (now, now, run_id),
    ).fetchone()
    conn.commit()
    return _run_row(row)


def append_event(
    conn: Connection,
    *,
    run_id: str,
    phase: str,
    event_type: str,
    severity: str = "info",
    message: str | None = None,
    data: Optional[dict] = None,
) -> dict:
    row = conn.execute(
        """
        INSERT INTO cloudosd_run_events (
            run_id, phase, event_type, severity, message, data_json, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            run_id,
            phase,
            event_type,
            severity,
            message,
            _json(data or {}),
            _now(),
        ),
    ).fetchone()
    conn.commit()
    return {
        "id": row["id"],
        "run_id": str(row["run_id"]),
        "phase": row["phase"],
        "event_type": row["event_type"],
        "severity": row["severity"],
        "message": row["message"],
        "data": row["data_json"] or {},
        "created_at": _iso(row["created_at"]),
    }


def list_events(conn: Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM cloudosd_run_events
        WHERE run_id = %s
        ORDER BY created_at ASC, id ASC
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "run_id": str(row["run_id"]),
            "phase": row["phase"],
            "event_type": row["event_type"],
            "severity": row["severity"],
            "message": row["message"],
            "data": row["data_json"] or {},
            "created_at": _iso(row["created_at"]),
        }
        for row in rows
    ]


def os_settings(run: dict) -> dict:
    edition = run["os_edition"]
    edition_ids = {
        "Home": "Core",
        "Home N": "CoreN",
        "Education": "Education",
        "Education N": "EducationN",
        "Pro": "Professional",
        "Pro N": "ProfessionalN",
        "Enterprise": "Enterprise",
        "Enterprise N": "EnterpriseN",
    }
    edition_id = edition_ids.get(edition, edition)
    return {
        "OperatingSystem": {
            "default": run["os_version"],
            "values": [run["os_version"]],
        },
        "OSActivation": {
            "default": run["os_activation"],
            "values": OS_ACTIVATION_CATALOG,
        },
        "OSEdition": {
            "default": edition,
            "values": [
                {
                    "Edition": edition,
                    "EditionId": edition_id,
                },
            ],
        },
        "OSLanguageCode": {
            "default": run["os_language"],
            "values": [run["os_language"]],
        },
    }


def user_settings(run: dict) -> dict:
    return {
        "DriverPacks": {
            "Default": run["driver_pack_policy"],
            "MicrosoftUpdateCatalog": False,
            "OSDCloud": run["driver_pack_policy"] != "None",
        },
        "UpdateDiskDrivers": False,
        "UpdateNetworkDrivers": False,
        "UpdateScsiDrivers": False,
        "UpdateSystemFirmware": bool(run["firmware_updates_enabled"]),
        "WinpeRestart": False,
        "WinpeShutdown": False,
        "Analytics": bool(run["analytics_enabled"]),
    }


def task_settings(run: dict) -> dict:
    firmware = bool(run["firmware_updates_enabled"])
    return {
        "name": "osdcloud" if firmware else "osdcloud-nofirmware",
        "workflow": run["workflow_name"],
        "cli": True,
        "firmware_updates_enabled": firmware,
        "validate_windows_image_hash": True,
        "winpe_oob_driver_export_apply": True,
    }
