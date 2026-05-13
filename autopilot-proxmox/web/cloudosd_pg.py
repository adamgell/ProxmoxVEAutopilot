"""PostgreSQL repository for CloudOSD deployment artifacts and runs."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional

from psycopg import Connection, errors
from psycopg.types.json import Jsonb

from web import ts_engine_pg


_INIT_LOCK = Lock()
_INIT_DONE = False
_INIT_LOCK_KEY = "proxmoxveautopilot:cloudosd_pg:init"


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
    vm_group_tag text NULL,
    vm_oem_profile text NULL,
    chassis_type_override integer NULL,
    source_surface text NULL,
    source_sequence_id integer NULL,
    tpm_enabled boolean NOT NULL DEFAULT true,
    secure_boot boolean NOT NULL DEFAULT true,
    firmware_updates_enabled boolean NOT NULL DEFAULT false,
    driver_pack_policy text NOT NULL DEFAULT 'None',
    analytics_enabled boolean NOT NULL DEFAULT false,
    outbound_policy_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    domain_join_json jsonb NOT NULL DEFAULT '{}'::jsonb,
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
    "Domain join",
    "SetupComplete",
    "first boot",
    "AutopilotAgent",
]
_CLOUDOSD_PE_STEP_KINDS = {
    "cloudosd_preflight",
    "cloudosd_deploy_os",
    "cloudosd_validate_offline_os",
    "stage_ad_domain_join_unattend",
    "stage_osd_client",
    "stage_autopilot_agent",
}
_CLOUDOSD_HEARTBEAT_STEP_KINDS = _CLOUDOSD_PE_STEP_KINDS | {
    "wait_agent_heartbeat",
}
_CLOUDOSD_DOMAIN_VERIFY_STEP_KINDS = {"verify_ad_domain_join"}


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
    if (
        phase_key in {"proxmox_playbook", "proxmox playbook"}
        or "playbook" in event_key
        or event_key == "provision_job_status"
    ):
        return "Proxmox playbook"
    if event_key == "pe_registered":
        return "PE bridge"
    if "osdcloud" in event_key:
        return "OSDCloud"
    if "validation" in event_key or phase_key == "offline_validation":
        return "offline validation"
    if "domain_join" in event_key or phase_key in {"domain_join", "domain join"}:
        return "Domain join"
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


def sync_ts_progress_for_run(conn: Connection, run_id: str) -> int:
    """Synchronize v2 run-plan progress from CloudOSD lifecycle evidence."""
    run = get_run(conn, run_id)
    if not run:
        return 0
    events = list_events(conn, run_id)
    event_types = {
        str(event.get("event_type") or "").casefold()
        for event in events
    }
    phases = {
        str(event.get("phase") or "").casefold()
        for event in events
    }

    done_kinds: set[str] = set()
    if run.get("pe_registered_at") or "pe_registered" in event_types:
        done_kinds.add("cloudosd_preflight")
    if "osdcloud_start" in event_types:
        done_kinds.add("cloudosd_preflight")
    if (
        "offline_validation_ok" in event_types
        or "offline validation" in phases
        or "offline_validation" in phases
    ):
        done_kinds.update({"cloudosd_deploy_os", "cloudosd_validate_offline_os"})
    if run.get("osdcloud_finished_at") or "cloudosd_pe_complete" in event_types:
        done_kinds.update(_CLOUDOSD_PE_STEP_KINDS)
    if (
        run.get("first_heartbeat_at")
        or run.get("state") == "complete"
        or "autopilotagent_heartbeat" in event_types
        or "autopilotagent_heartbeat_visible" in event_types
        or "firstboot_complete" in event_types
    ):
        done_kinds.update(_CLOUDOSD_HEARTBEAT_STEP_KINDS)
    if "domain_join_verified" in event_types:
        done_kinds.update(_CLOUDOSD_DOMAIN_VERIFY_STEP_KINDS)

    if not done_kinds:
        return 0
    return ts_engine_pg.mark_steps_done_by_kind(
        conn,
        run_id=run_id,
        kinds=done_kinds,
        agent_id="cloudosd-controller",
        message="CloudOSD lifecycle evidence advanced this v2 run step",
        data={"source": "cloudosd_lifecycle"},
    )


def sync_all_ts_progress(conn: Connection) -> int:
    changed = 0
    try:
        rows = conn.execute(
            "SELECT run_id FROM cloudosd_runs ORDER BY created_at DESC"
        ).fetchall()
    except errors.UndefinedTable:
        conn.rollback()
        return 0
    for row in rows:
        changed += sync_ts_progress_for_run(conn, str(row["run_id"]))
    return changed


def init(conn: Connection) -> None:
    global _INIT_DONE
    if _INIT_DONE:
        return
    with _INIT_LOCK:
        if _INIT_DONE:
            return
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_INIT_LOCK_KEY,))
        ts_engine_pg.init(conn)
        conn.execute(SCHEMA)
        conn.execute("ALTER TABLE cloudosd_artifacts ADD COLUMN IF NOT EXISTS build_job_id text NULL")
        conn.execute("ALTER TABLE cloudosd_artifacts ADD COLUMN IF NOT EXISTS publish_job_id text NULL")
        conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS requested_vm_name text NULL")
        conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS pve_vm_name text NULL")
        conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS expected_computer_name text NULL")
        conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS requested_vmid integer NULL")
        conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS iso_storage text NULL")
        conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS vm_group_tag text NULL")
        conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS vm_oem_profile text NULL")
        conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS chassis_type_override integer NULL")
        conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS source_surface text NULL")
        conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS source_sequence_id integer NULL")
        conn.execute("ALTER TABLE cloudosd_runs ADD COLUMN IF NOT EXISTS domain_join_json jsonb NOT NULL DEFAULT '{}'::jsonb")
        conn.commit()
        _INIT_DONE = True


def reset_for_tests(conn: Connection) -> None:
    global _INIT_DONE
    conn.execute(DROP_SCHEMA_FOR_TESTS)
    conn.commit()
    _INIT_DONE = False


def _domain_join_enabled(domain_join: dict | None) -> bool:
    return bool((domain_join or {}).get("enabled"))


def _unique_text(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        key = item.casefold()
        if not item or key in seen:
            continue
        out.append(item)
        seen.add(key)
    return out


def _sanitize_domain_join(
    domain_join: dict | None,
    *,
    expected_computer_name: str | None = None,
) -> dict:
    raw = dict(domain_join or {})
    if not raw.get("enabled"):
        return {"enabled": False}
    domain_fqdn = str(raw.get("domain_fqdn") or "").strip()
    credential_domain = str(raw.get("credential_domain") or "").strip()
    acceptable = _unique_text(
        list(raw.get("acceptable_domain_names") or [])
        + [domain_fqdn, credential_domain],
    )
    return {
        "enabled": True,
        "source_sequence_id": raw.get("source_sequence_id"),
        "credential_id": raw.get("credential_id"),
        "domain_fqdn": domain_fqdn,
        "credential_domain": credential_domain,
        "ou_path": str(raw.get("ou_path") or "").strip(),
        "acceptable_domain_names": acceptable,
        "expected_computer_name": (
            expected_computer_name
            or str(raw.get("expected_computer_name") or "").strip()
        ),
    }


def domain_join_verification(domain_join: dict | None, heartbeat: dict | None) -> dict:
    config = _sanitize_domain_join(domain_join)
    hb = heartbeat or {}
    observed = str(hb.get("domain_name") or "").strip()
    joined = bool(hb.get("domain_joined"))
    expected = _unique_text(config.get("acceptable_domain_names") or [])
    observed_key = observed.casefold()
    matched = bool(
        config.get("enabled")
        and joined
        and observed_key
        and observed_key in {item.casefold() for item in expected}
    )
    reason = "matched" if matched else "waiting_for_domain_membership"
    if not joined:
        reason = "heartbeat_not_domain_joined"
    elif not observed:
        reason = "heartbeat_missing_domain_name"
    elif expected and observed_key not in {item.casefold() for item in expected}:
        reason = "heartbeat_domain_mismatch"
    return {
        "matched": matched,
        "reason": reason,
        "expected_domain_names": expected,
        "observed_domain_name": observed,
        "domain_joined": joined,
    }


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
        "vm_group_tag": row.get("vm_group_tag"),
        "vm_oem_profile": row.get("vm_oem_profile"),
        "chassis_type_override": row.get("chassis_type_override"),
        "source_surface": row.get("source_surface"),
        "source_sequence_id": row.get("source_sequence_id"),
        "tpm_enabled": row["tpm_enabled"],
        "secure_boot": row["secure_boot"],
        "firmware_updates_enabled": row["firmware_updates_enabled"],
        "driver_pack_policy": row["driver_pack_policy"],
        "analytics_enabled": row["analytics_enabled"],
        "outbound_policy": row["outbound_policy_json"] or {},
        "domain_join": _sanitize_domain_join(
            row.get("domain_join_json") or {},
            expected_computer_name=expected_name,
        ),
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


def _create_sequence_for_run(
    conn: Connection,
    *,
    name: str,
    domain_join: dict | None = None,
) -> str:
    sequence_id = ts_engine_pg.create_sequence(
        conn,
        name=name,
        description="Generated CloudOSD deployment sequence",
        created_by="cloudosd",
    )
    domain_enabled = _domain_join_enabled(domain_join)
    steps = [
        ("CloudOSD PE preflight", "cloudosd_preflight", "pe"),
        ("Run OSDCloud workflow", "cloudosd_deploy_os", "pe"),
    ]
    if domain_enabled:
        steps.append(
            ("Stage AD domain join unattend", "stage_ad_domain_join_unattend", "pe"),
        )
    steps.extend([
        ("Validate offline Windows", "cloudosd_validate_offline_os", "pe"),
        ("Stage OSD client", "stage_osd_client", "pe"),
        ("Stage AutopilotAgent", "stage_autopilot_agent", "pe"),
        ("Capture Autopilot hardware hash", "capture_autopilot_hash", "full_os"),
        ("Wait for AutopilotAgent heartbeat", "wait_agent_heartbeat", "full_os"),
    ])
    if domain_enabled:
        steps.append(
            ("Verify AD domain membership", "verify_ad_domain_join", "full_os"),
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
            params=(
                domain_join
                if kind in {
                    "stage_ad_domain_join_unattend",
                    "verify_ad_domain_join",
                }
                else {}
            ),
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
    vm_group_tag: str | None = None,
    vm_oem_profile: str | None = None,
    chassis_type_override: int | None = None,
    source_surface: str | None = None,
    source_sequence_id: int | None = None,
    tpm_enabled: bool = True,
    secure_boot: bool = True,
    firmware_updates_enabled: bool = False,
    driver_pack_policy: str = DEFAULT_DRIVER_PACK_POLICY,
    analytics_enabled: bool = False,
    outbound_policy: Optional[dict] = None,
    domain_join: Optional[dict] = None,
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
    domain_join_config = _sanitize_domain_join(
        domain_join,
        expected_computer_name=expected_computer_name,
    )

    version_id = _create_sequence_for_run(
        conn,
        name=f"CloudOSD deployment for {vm_name}",
        domain_join=domain_join_config,
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
            "vm_group_tag": vm_group_tag or "",
            "vm_oem_profile": vm_oem_profile or "",
            "chassis_type_override": int(chassis_type_override or 0),
            "source_surface": source_surface or "cloudosd",
            "source_sequence_id": source_sequence_id,
            "os_version": os_version,
            "os_activation": os_activation,
            "os_edition": os_edition,
            "os_language": os_language,
            "driver_pack_policy": driver_pack_policy,
            "firmware_updates_enabled": firmware_updates_enabled,
            "domain_join": domain_join_config,
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
            vm_memory_mb, vm_disk_size_gb, vm_group_tag, vm_oem_profile,
            chassis_type_override, source_surface, source_sequence_id,
            tpm_enabled, secure_boot, firmware_updates_enabled,
            driver_pack_policy, analytics_enabled, outbound_policy_json,
            domain_join_json,
            created_at, updated_at
        )
        VALUES (
            %s, %s, 'created', %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
            vm_group_tag or None,
            vm_oem_profile or None,
            int(chassis_type_override or 0) or None,
            source_surface or None,
            source_sequence_id,
            tpm_enabled,
            secure_boot,
            firmware_updates_enabled,
            driver_pack_policy,
            analytics_enabled,
            _json(outbound_policy or {}),
            _json(domain_join_config),
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
    heartbeat: dict | None = None,
) -> dict | None:
    now = _now()
    current = conn.execute(
        "SELECT * FROM cloudosd_runs WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    if not current:
        return None
    if current["state"] == "failed":
        return _run_row(current)

    domain_join = _sanitize_domain_join(current.get("domain_join_json") or {})
    if _domain_join_enabled(domain_join):
        verification = domain_join_verification(domain_join, heartbeat)
        if not verification["matched"]:
            row = conn.execute(
                """
                UPDATE cloudosd_runs
                SET state = CASE
                        WHEN state = 'complete' THEN state
                        ELSE 'full_os_waiting_domain_join'
                    END,
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
                    SET state = CASE
                            WHEN state = 'done' THEN state
                            ELSE 'full_os_waiting_domain_join'
                        END,
                        phase = 'full_os'
                    WHERE id = %s
                    """,
                    (run_id,),
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
                append_event(
                    conn,
                    run_id=run_id,
                    phase="domain_join",
                    event_type="domain_join_pending",
                    severity="warning",
                    message="Waiting for AutopilotAgent heartbeat to report expected AD domain membership",
                    data=verification,
                )
                sync_ts_progress_for_run(conn, run_id)
            return _run_row(row)

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
        if _domain_join_enabled(domain_join):
            append_event(
                conn,
                run_id=run_id,
                phase="domain_join",
                event_type="domain_join_verified",
                message="AutopilotAgent heartbeat reported expected AD domain membership",
                data=domain_join_verification(domain_join, heartbeat),
            )
        sync_ts_progress_for_run(conn, run_id)
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
