"""Deployment timing aggregation for the monitoring cockpit."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from statistics import median
import threading
from typing import Any

from psycopg import Connection

from web import deployment_health_pg


DEFAULT_PHASE_THRESHOLDS = {
    "queue_wait": 300,
    "builder_claim": 600,
    "execution": 3600,
    "pause_gate": 1800,
    "finish": 300,
    "run": 7200,
    "queued": 1800,
    "vm_identity": 1800,
    "winpe_boot": 1800,
    "partition_apply_wim": 5400,
    "driver_injection": 1800,
    "boot_handoff": 1800,
    "windows_setup": 5400,
    "osd_client": 3600,
    "hash_capture": 1800,
    "proxmox_provision": 3600,
    "pe_registration": 1800,
    "osdcloud": 7200,
    "offline_validation": 1800,
    "setup_complete": 3600,
    "first_boot": 3600,
    "agent_heartbeat": 1800,
    "hash_upload": 1800,
    "assignment_contact_enrollment": 7200,
    "prereqs": 3600,
    "source_fetch": 1800,
    "msi_build": 3600,
    "winpe_build": 7200,
    "cloudosd_build": 7200,
    "artifact_publish": 1800,
}
GENERIC_PHASE_THRESHOLD_SECONDS = 3600
GENERIC_STALE_WINDOW_SECONDS = 3600
RUN_TERMINAL_STATES = {"done", "failed", "skipped", "stale"}
HEALTH_PRIORITY = {
    "failed": 5,
    "stuck": 4,
    "regressed": 3,
    "slow": 2,
    "learning": 1,
    "healthy": 0,
}
_SYNC_LOCK = threading.Lock()
WINPE_STEP_PHASES = {
    "vm_identity": "vm_identity",
    "winpe_boot": "winpe_boot",
    "partition": "partition_apply_wim",
    "apply_wim": "partition_apply_wim",
    "apply_os_image": "partition_apply_wim",
    "inject_drivers": "driver_injection",
    "driver_injection": "driver_injection",
    "boot_handoff": "boot_handoff",
    "windows_setup": "windows_setup",
    "osd_client": "osd_client",
    "stage_osd_client": "osd_client",
    "hash_capture": "hash_capture",
}
CLOUDOSD_STEP_PHASES = {
    "cloudosd_preflight": "pe_registration",
    "cloudosd_deploy_os": "osdcloud",
    "cloudosd_validate_offline_os": "offline_validation",
    "stage_osd_client": "osd_client",
    "setup_complete": "setup_complete",
    "hash_capture": "hash_capture",
}
BUILD_HOST_WORK_PHASES = {
    "install_build_prerequisites": "prereqs",
    "fetch_source_bundle": "source_fetch",
    "build_agent_msi": "msi_build",
    "build_winpe": "winpe_build",
    "build_cloudosd": "cloudosd_build",
    "publish_artifacts": "artifact_publish",
}


def reset_for_tests(conn: Connection) -> None:
    deployment_health_pg.reset_for_tests(conn)
    deployment_health_pg.init(conn)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_value(value: Any) -> Any:
    return value if value is not None else {}


def _duration(started_at: Any, ended_at: Any) -> int | None:
    start = _coerce_dt(started_at)
    end = _coerce_dt(ended_at)
    if not start or not end:
        return None
    return max(0, int((end - start).total_seconds()))


def _table_exists(conn: Connection, table_name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) AS rel", (f"public.{table_name}",)).fetchone()
    return bool(row and row.get("rel"))


def _baseline_lookup(conn: Connection) -> dict[tuple[str, str], dict]:
    return {
        (row["deployment_type"], row["phase_key"]): row
        for row in deployment_health_pg.list_baselines(conn)
    }


def _record(
    conn: Connection,
    *,
    deployment_key: str,
    deployment_type: str,
    source: str,
    source_id: Any,
    phase_key: str,
    phase_label: str,
    state: str,
    started_at: Any,
    ended_at: Any = None,
    last_progress_at: Any = None,
    evidence: dict | None = None,
    error: str | None = None,
) -> None:
    deployment_health_pg.record_phase(
        conn,
        deployment_key=deployment_key,
        deployment_type=deployment_type,
        source=source,
        source_id=str(source_id),
        phase_key=phase_key,
        phase_label=phase_label,
        state=state,
        started_at=started_at or _now(),
        ended_at=ended_at,
        last_progress_at=last_progress_at,
        evidence=evidence or {},
        error=error,
        commit=False,
        init_schema=False,
    )


def _job_execution_state(row: dict) -> str:
    status = row.get("status")
    if status == "complete":
        return "done"
    if status == "failed":
        return "failed"
    if status == "orphaned":
        return "stale"
    if status == "pending":
        return "pending"
    if status == "running":
        return "running"
    return "failed"


def _legacy_step_state(state: str | None) -> str:
    if state == "ok":
        return "done"
    if state == "error":
        return "failed"
    if state == "running":
        return "running"
    return "pending"


def _run_state(state: str | None) -> str:
    if state in {"done", "complete", "completed"}:
        return "done"
    if state in {"failed", "error", "cancelled", "canceled"}:
        return "failed"
    if state in {"skipped"}:
        return "skipped"
    if state in {"queued", "pending"}:
        return "pending"
    if state in {"orphaned", "stale"}:
        return "stale"
    return "running"


def _ts_step_state(state: str | None) -> str:
    if state == "done":
        return "done"
    if state == "failed":
        return "failed"
    if state == "skipped":
        return "skipped"
    if state == "pending":
        return "pending"
    return "running"


def _normalized_step_phase_key(
    *,
    deployment_type: str,
    kind: str | None,
    ordinal: Any,
) -> str:
    kind_text = str(kind or "step")
    if deployment_type == "winpe" and kind_text in WINPE_STEP_PHASES:
        return WINPE_STEP_PHASES[kind_text]
    if deployment_type == "cloudosd" and kind_text in CLOUDOSD_STEP_PHASES:
        return CLOUDOSD_STEP_PHASES[kind_text]
    if deployment_type == "task_engine":
        if kind_text in CLOUDOSD_STEP_PHASES:
            return CLOUDOSD_STEP_PHASES[kind_text]
        if kind_text in WINPE_STEP_PHASES:
            return WINPE_STEP_PHASES[kind_text]
    return f"step:{ordinal}:{kind_text}"


def _evidence_from_job(row: dict) -> dict:
    args = _json_value(row.get("args_json"))
    evidence = {
        "job_id": row.get("id"),
        "job_type": row.get("job_type"),
        "playbook": row.get("playbook"),
        "status": row.get("status"),
        "worker_id": row.get("worker_id"),
        "exit_code": row.get("exit_code"),
    }
    if isinstance(args, dict):
        for key in (
            "vmid",
            "vm_vmid",
            "target_vmid",
            "node",
            "artifact",
            "run_id",
            "workflow_name",
            "sequence_id",
        ):
            if key in args:
                evidence[key] = args[key]
    return evidence


def _sync_jobs(conn: Connection) -> None:
    if not _table_exists(conn, "jobs"):
        return
    rows = conn.execute(
        """
        SELECT id, job_type, playbook, args_json, status, worker_id,
               exit_code, created_at, claimed_at, last_heartbeat, ended_at
        FROM jobs
        ORDER BY created_at DESC
        LIMIT 500
        """
    ).fetchall()
    for row in rows:
        evidence = _evidence_from_job(row)
        deployment_key = f"job:{row['id']}"
        queue_state = "done" if row.get("claimed_at") else (
            "running" if row.get("status") == "pending" else "skipped"
        )
        _record(
            conn,
            deployment_key=deployment_key,
            deployment_type=row["job_type"],
            source="jobs",
            source_id=row["id"],
            phase_key="queue_wait",
            phase_label="Queue wait",
            state=queue_state,
            started_at=row["created_at"],
            ended_at=row.get("claimed_at"),
            last_progress_at=row.get("claimed_at") or row.get("created_at"),
            evidence=evidence,
        )
        _record(
            conn,
            deployment_key=deployment_key,
            deployment_type=row["job_type"],
            source="jobs",
            source_id=row["id"],
            phase_key="execution",
            phase_label="Execution",
            state=_job_execution_state(row),
            started_at=row.get("claimed_at") or row.get("created_at"),
            ended_at=row.get("ended_at"),
            last_progress_at=row.get("ended_at") or row.get("last_heartbeat") or row.get("claimed_at") or row.get("created_at"),
            evidence=evidence,
            error=row.get("status") if row.get("status") in {"failed", "orphaned"} else None,
        )
        args = _json_value(row.get("args_json"))
        if isinstance(args, dict) and args.get("pause_enabled"):
            _record(
                conn,
                deployment_key=deployment_key,
                deployment_type=row["job_type"],
                source="jobs",
                source_id=row["id"],
                phase_key="pause_gate",
                phase_label="Pause gate",
                state="done" if row.get("ended_at") else "running",
                started_at=row.get("claimed_at") or row.get("created_at"),
                ended_at=row.get("ended_at"),
                last_progress_at=row.get("last_heartbeat") or row.get("created_at"),
                evidence=evidence,
            )


def _sync_legacy_runs(conn: Connection) -> None:
    if not (_table_exists(conn, "provisioning_runs") and _table_exists(conn, "provisioning_run_steps")):
        return
    runs = conn.execute(
        """
        SELECT id, vmid, provision_path, state, vm_uuid, started_at, finished_at, last_error
        FROM provisioning_runs
        ORDER BY started_at DESC
        LIMIT 500
        """
    ).fetchall()
    for run in runs:
        deployment_type = str(run["provision_path"])
        deployment_key = f"{deployment_type}:{run['id']}"
        evidence = {
            "run_id": run["id"],
            "vmid": run.get("vmid"),
            "vm_uuid": run.get("vm_uuid"),
            "state": run.get("state"),
        }
        _record(
            conn,
            deployment_key=deployment_key,
            deployment_type=deployment_type,
            source="provisioning_runs",
            source_id=run["id"],
            phase_key="run",
            phase_label=f"{deployment_type.title()} run",
            state=_run_state(run.get("state")),
            started_at=run["started_at"],
            ended_at=run.get("finished_at"),
            last_progress_at=run.get("finished_at") or run.get("started_at"),
            evidence=evidence,
            error=run.get("last_error"),
        )
    steps = conn.execute(
        """
        SELECT s.*, r.provision_path, r.started_at AS run_started_at
        FROM provisioning_run_steps s
        JOIN provisioning_runs r ON r.id = s.run_id
        ORDER BY r.started_at DESC, s.order_index ASC
        LIMIT 1500
        """
    ).fetchall()
    for step in steps:
        deployment_type = str(step["provision_path"])
        phase_key = _normalized_step_phase_key(
            deployment_type=deployment_type,
            kind=step.get("kind"),
            ordinal=step.get("order_index"),
        )
        label = str(step.get("kind") or "step").replace("_", " ").title()
        _record(
            conn,
            deployment_key=f"{deployment_type}:{step['run_id']}",
            deployment_type=deployment_type,
            source="provisioning_run_steps",
            source_id=step["id"],
            phase_key=phase_key,
            phase_label=label,
            state=_legacy_step_state(step.get("state")),
            started_at=step.get("started_at") or step.get("finished_at") or step.get("run_started_at"),
            ended_at=step.get("finished_at"),
            last_progress_at=step.get("finished_at") or step.get("started_at") or step.get("run_started_at"),
            evidence={
                "run_id": step["run_id"],
                "step_id": step["id"],
                "phase": step.get("phase"),
                "kind": step.get("kind"),
                "state": step.get("state"),
            },
            error=step.get("error"),
        )


def _sync_ts_runs(conn: Connection) -> None:
    if not (_table_exists(conn, "ts_provisioning_runs") and _table_exists(conn, "ts_run_plan_steps")):
        return
    cloudosd_exists = _table_exists(conn, "cloudosd_runs")
    jobs_exists = _table_exists(conn, "jobs")
    cloudosd_join = (
        """
        LEFT JOIN cloudosd_runs cloudosd
          ON cloudosd.run_id = r.id
        """
        if cloudosd_exists
        else ""
    )
    cloudosd_cols = (
        """
        cloudosd.state AS cloudosd_state,
        """
        if cloudosd_exists
        else """
        NULL::text AS cloudosd_state,
        """
    )
    provision_job_join = (
        """
        LEFT JOIN LATERAL (
          SELECT id, status, exit_code, ended_at, last_heartbeat
          FROM jobs j
          WHERE j.job_type = 'provision_cloudosd'
            AND COALESCE(j.args_json->>'cloudosd_run_id', j.args_json->>'run_id') = r.id::text
          ORDER BY j.created_at DESC
          LIMIT 1
        ) provision_job ON true
        """
        if jobs_exists
        else ""
    )
    provision_job_cols = (
        """
        provision_job.id AS provision_job_id,
        provision_job.status AS provision_job_status,
        provision_job.exit_code AS provision_job_exit_code,
        provision_job.ended_at AS provision_job_ended_at,
        provision_job.last_heartbeat AS provision_job_last_heartbeat,
        """
        if jobs_exists
        else """
        NULL::text AS provision_job_id,
        NULL::text AS provision_job_status,
        NULL::integer AS provision_job_exit_code,
        NULL::timestamptz AS provision_job_ended_at,
        NULL::timestamptz AS provision_job_last_heartbeat,
        """
    )
    runs = conn.execute(
        f"""
        SELECT r.id, r.state, r.phase, r.vmid, r.vm_uuid, r.computer_name,
               r.serial_number, r.started_at, r.finished_at, r.last_error,
               {cloudosd_cols}
               {provision_job_cols}
               NULL::text AS _unused
        FROM ts_provisioning_runs r
        {cloudosd_join}
        {provision_job_join}
        ORDER BY started_at DESC
        LIMIT 500
        """
    ).fetchall()
    for run in runs:
        provision_job_status = str(run.get("provision_job_status") or "")
        provision_job_failed = provision_job_status in {"failed", "orphaned", "canceled", "cancelled"}
        cloudosd_failed = _run_state(run.get("cloudosd_state")) == "failed"
        effective_failed = _run_state(run.get("state")) == "failed" or cloudosd_failed or provision_job_failed
        effective_state = "failed" if effective_failed else _run_state(run.get("state"))
        effective_finished_at = run.get("finished_at") or (
            run.get("provision_job_ended_at") if effective_failed else None
        )
        evidence = {
            "run_id": str(run["id"]),
            "vmid": run.get("vmid"),
            "vm_uuid": run.get("vm_uuid"),
            "computer_name": run.get("computer_name"),
            "serial_number": run.get("serial_number"),
            "state": run.get("state"),
            "phase": run.get("phase"),
            "cloudosd_state": run.get("cloudosd_state"),
            "provision_job_id": run.get("provision_job_id"),
            "provision_job_status": run.get("provision_job_status"),
            "provision_job_exit_code": run.get("provision_job_exit_code"),
        }
        _record(
            conn,
            deployment_key=f"ts:{run['id']}",
            deployment_type="task_engine",
            source="ts_provisioning_runs",
            source_id=run["id"],
            phase_key="run",
            phase_label="Task engine run",
            state=effective_state,
            started_at=run["started_at"],
            ended_at=effective_finished_at,
            last_progress_at=effective_finished_at or run.get("provision_job_last_heartbeat") or run.get("started_at"),
            evidence=evidence,
            error=run.get("last_error") or (provision_job_status if provision_job_failed else None),
        )
    steps = conn.execute(
        """
        SELECT s.*, r.started_at AS run_started_at
        FROM ts_run_plan_steps s
        JOIN ts_provisioning_runs r ON r.id = s.run_id
        ORDER BY r.started_at DESC, s.ordinal ASC
        LIMIT 2000
        """
    ).fetchall()
    for step in steps:
        phase_key = _normalized_step_phase_key(
            deployment_type="task_engine",
            kind=step.get("kind"),
            ordinal=step.get("ordinal"),
        )
        _record(
            conn,
            deployment_key=f"ts:{step['run_id']}",
            deployment_type="task_engine",
            source="ts_run_plan_steps",
            source_id=step["id"],
            phase_key=phase_key,
            phase_label=step.get("name") or str(step.get("kind") or "step").replace("_", " ").title(),
            state=_ts_step_state(step.get("state")),
            started_at=step.get("started_at") or step.get("run_started_at"),
            ended_at=step.get("finished_at"),
            last_progress_at=step.get("finished_at") or step.get("started_at") or step.get("claimed_at") or step.get("run_started_at"),
            evidence={
                "run_id": str(step["run_id"]),
                "step_id": str(step["id"]),
                "ordinal": step.get("ordinal"),
                "name": step.get("name"),
                "kind": step.get("kind"),
                "phase": step.get("phase"),
                "state": step.get("state"),
                "attempt": step.get("attempt"),
            },
            error=step.get("last_error"),
        )


def _sync_agent_work(conn: Connection) -> None:
    if not _table_exists(conn, "agent_work_items"):
        return
    rows = conn.execute(
        """
        SELECT wi.id, wi.agent_id, wi.kind, wi.status, wi.vmid, wi.job_id,
               wi.request_json, wi.result_json, wi.error, wi.created_at,
               wi.claimed_at, wi.completed_at, wi.updated_at,
               d.last_seen_at AS agent_last_seen_at,
               (
                 wi.status = 'claimed' AND EXISTS (
                 SELECT 1
                 FROM agent_work_items later
                 WHERE later.agent_id = wi.agent_id
                   AND later.status IN ('complete', 'completed', 'failed', 'error', 'cancelled', 'canceled')
                   AND COALESCE(later.completed_at, later.updated_at, later.claimed_at, later.created_at)
                       > COALESCE(wi.claimed_at, wi.created_at)
                 )
               ) AS superseded_by_later_terminal_work
        FROM agent_work_items wi
        LEFT JOIN agent_devices d ON d.agent_id = wi.agent_id
        ORDER BY wi.created_at DESC
        LIMIT 500
        """
    ).fetchall()
    for row in rows:
        deployment_key = f"agent-work:{row['id']}"
        deployment_type = f"agent_work:{row['kind']}"
        evidence = {
            "work_item_id": str(row["id"]),
            "agent_id": row.get("agent_id"),
            "kind": row.get("kind"),
            "status": row.get("status"),
            "vmid": row.get("vmid"),
            "job_id": row.get("job_id"),
            "agent_last_seen_at": row.get("agent_last_seen_at"),
            "request": _json_value(row.get("request_json")),
            "result": _json_value(row.get("result_json")),
            "superseded_by_later_terminal_work": bool(row.get("superseded_by_later_terminal_work")),
        }
        work_state = _run_state(row.get("status"))
        work_error = row.get("error")
        if (
            str(row.get("status") or "") == "claimed"
            and row.get("superseded_by_later_terminal_work")
        ):
            work_state = "failed"
            work_error = work_error or (
                "claimed work item was superseded by later terminal work from the same agent"
            )
        queue_state = "done" if row.get("claimed_at") else (
            "running" if row.get("status") == "pending" else "skipped"
        )
        _record(
            conn,
            deployment_key=deployment_key,
            deployment_type=deployment_type,
            source="agent_work_items",
            source_id=row["id"],
            phase_key="queue_wait",
            phase_label="Queue wait",
            state=queue_state,
            started_at=row["created_at"],
            ended_at=row.get("claimed_at"),
            last_progress_at=row.get("claimed_at") or row.get("updated_at") or row.get("created_at"),
            evidence=evidence,
        )
        _record(
            conn,
            deployment_key=deployment_key,
            deployment_type=deployment_type,
            source="agent_work_items",
            source_id=row["id"],
            phase_key=BUILD_HOST_WORK_PHASES.get(str(row.get("kind") or ""), "execution"),
            phase_label=str(row.get("kind") or "Agent work").replace("_", " ").title(),
            state=work_state,
            started_at=row.get("claimed_at") or row.get("created_at"),
            ended_at=row.get("completed_at"),
            last_progress_at=row.get("completed_at") or row.get("updated_at") or row.get("claimed_at") or row.get("created_at"),
            evidence=evidence,
            error=work_error,
        )


def _sync_cloudosd(conn: Connection) -> None:
    if not _table_exists(conn, "cloudosd_runs"):
        return
    jobs_exists = _table_exists(conn, "jobs")
    provision_job_join = (
        """
        LEFT JOIN LATERAL (
          SELECT id, status, exit_code, ended_at, last_heartbeat
          FROM jobs j
          WHERE j.job_type = 'provision_cloudosd'
            AND COALESCE(j.args_json->>'cloudosd_run_id', j.args_json->>'run_id') = r.run_id::text
          ORDER BY j.created_at DESC
          LIMIT 1
        ) provision_job ON true
        """
        if jobs_exists
        else ""
    )
    provision_job_cols = (
        """
        provision_job.id AS provision_job_id,
        provision_job.status AS provision_job_status,
        provision_job.exit_code AS provision_job_exit_code,
        provision_job.ended_at AS provision_job_ended_at,
        provision_job.last_heartbeat AS provision_job_last_heartbeat,
        """
        if jobs_exists
        else """
        NULL::text AS provision_job_id,
        NULL::text AS provision_job_status,
        NULL::integer AS provision_job_exit_code,
        NULL::timestamptz AS provision_job_ended_at,
        NULL::timestamptz AS provision_job_last_heartbeat,
        """
    )
    readiness_exists = _table_exists(conn, "cloudosd_autopilot_readiness")
    readiness_join = (
        """
        LEFT JOIN cloudosd_autopilot_readiness readiness
          ON readiness.run_id = r.run_id
        """
        if readiness_exists
        else ""
    )
    readiness_cols = (
        """
        readiness.state AS readiness_state,
        readiness.hash_status,
        readiness.upload_status,
        readiness.upload_started_at,
        readiness.upload_finished_at,
        readiness.upload_error,
        readiness.assignment_status,
        readiness.enrollment_status,
        readiness.contact_state,
        readiness.cache_status,
        """
        if readiness_exists
        else """
        NULL::text AS readiness_state,
        NULL::text AS hash_status,
        NULL::text AS upload_status,
        NULL::timestamptz AS upload_started_at,
        NULL::timestamptz AS upload_finished_at,
        NULL::text AS upload_error,
        NULL::text AS assignment_status,
        NULL::text AS enrollment_status,
        NULL::text AS contact_state,
        NULL::text AS cache_status,
        """
    )
    rows = conn.execute(
        f"""
        SELECT r.run_id, r.state, r.workflow_name, r.architecture, r.vm_name,
               r.expected_computer_name, r.requested_vmid, r.vmid, r.node,
               r.pe_registered_at, r.osdcloud_started_at, r.osdcloud_finished_at,
               r.first_heartbeat_at, r.created_at, r.updated_at,
               {provision_job_cols}
               {readiness_cols}
               r.vm_uuid
        FROM cloudosd_runs r
        {provision_job_join}
        {readiness_join}
        ORDER BY r.created_at DESC
        LIMIT 500
        """
    ).fetchall()
    for row in rows:
        deployment_key = f"cloudosd:{row['run_id']}"
        provision_job_status = str(row.get("provision_job_status") or "")
        provision_job_failed = provision_job_status in {"failed", "orphaned", "canceled", "cancelled"}
        failed = _run_state(row.get("state")) == "failed" or provision_job_failed
        evidence = {
            "run_id": str(row["run_id"]),
            "workflow_name": row.get("workflow_name"),
            "architecture": row.get("architecture"),
            "vm_name": row.get("vm_name"),
            "expected_computer_name": row.get("expected_computer_name"),
            "requested_vmid": row.get("requested_vmid"),
            "vmid": row.get("vmid"),
            "node": row.get("node"),
            "state": row.get("state"),
            "readiness_state": row.get("readiness_state"),
            "hash_status": row.get("hash_status"),
            "upload_status": row.get("upload_status"),
            "assignment_status": row.get("assignment_status"),
            "enrollment_status": row.get("enrollment_status"),
            "contact_state": row.get("contact_state"),
            "cache_status": row.get("cache_status"),
            "provision_job_id": row.get("provision_job_id"),
            "provision_job_status": row.get("provision_job_status"),
            "provision_job_exit_code": row.get("provision_job_exit_code"),
        }
        provision_end = row.get("pe_registered_at") or (
            row.get("provision_job_ended_at") if failed else None
        )
        provision_progress = (
            row.get("pe_registered_at")
            or row.get("provision_job_ended_at")
            or row.get("provision_job_last_heartbeat")
            or row.get("updated_at")
            or row.get("created_at")
        )
        if row.get("pe_registered_at"):
            provision_state = "done"
        elif failed:
            provision_state = "failed"
        else:
            provision_state = "running"
        _record(
            conn,
            deployment_key=deployment_key,
            deployment_type="cloudosd",
            source="cloudosd_runs",
            source_id=row["run_id"],
            phase_key="proxmox_provision",
            phase_label="Proxmox provision",
            state=provision_state,
            started_at=row["created_at"],
            ended_at=provision_end,
            last_progress_at=provision_progress,
            evidence=evidence,
            error=provision_job_status if provision_job_failed else (row.get("state") if failed else None),
        )
        if row.get("osdcloud_finished_at"):
            osdcloud_state = "done"
        elif row.get("osdcloud_started_at"):
            osdcloud_state = "failed" if failed else "running"
        elif row.get("pe_registered_at") and failed:
            osdcloud_state = "failed"
        else:
            osdcloud_state = "skipped" if failed else "pending"
        _record(
            conn,
            deployment_key=deployment_key,
            deployment_type="cloudosd",
            source="cloudosd_runs",
            source_id=row["run_id"],
            phase_key="osdcloud",
            phase_label="OSDCloud",
            state=osdcloud_state,
            started_at=row.get("osdcloud_started_at") or row.get("pe_registered_at") or row.get("created_at"),
            ended_at=row.get("osdcloud_finished_at") or (row.get("provision_job_ended_at") if failed else None),
            last_progress_at=row.get("osdcloud_finished_at") or row.get("osdcloud_started_at") or row.get("pe_registered_at") or row.get("updated_at"),
            evidence=evidence,
            error=row.get("state") if failed else None,
        )
        _record(
            conn,
            deployment_key=deployment_key,
            deployment_type="cloudosd",
            source="cloudosd_runs",
            source_id=row["run_id"],
            phase_key="first_boot",
            phase_label="First boot heartbeat",
            state="done" if row.get("first_heartbeat_at") else ("skipped" if failed else "pending"),
            started_at=row.get("osdcloud_finished_at") or row.get("created_at"),
            ended_at=row.get("first_heartbeat_at") or (row.get("provision_job_ended_at") if failed else None),
            last_progress_at=row.get("first_heartbeat_at") or row.get("updated_at") or row.get("created_at"),
            evidence=evidence,
        )
        if readiness_exists and (row.get("upload_started_at") or row.get("upload_finished_at") or row.get("hash_status")):
            upload_status = str(row.get("upload_status") or "")
            readiness_state = str(row.get("readiness_state") or "")
            upload_not_configured = (
                upload_status == "not_configured"
                or readiness_state == "upload_not_configured"
            )
            upload_failed = (
                upload_status in {"failed", "canceled", "cancelled"}
                or readiness_state == "upload_failed"
                or (bool(row.get("upload_error")) and not upload_not_configured)
            )
            if upload_not_configured:
                upload_state = "skipped"
                upload_ended_at = (
                    row.get("upload_finished_at")
                    or row.get("upload_started_at")
                    or row.get("first_heartbeat_at")
                    or row.get("updated_at")
                )
                upload_error = None
            elif upload_failed:
                upload_state = "failed"
                upload_ended_at = row.get("upload_finished_at") or row.get("updated_at")
                upload_error = row.get("upload_error") or upload_status or readiness_state
            elif failed and upload_status in {"", "not_started"} and not row.get("upload_started_at"):
                upload_state = "skipped"
                upload_ended_at = row.get("provision_job_ended_at") or row.get("updated_at")
                upload_error = None
            else:
                upload_done = bool(row.get("upload_finished_at")) or upload_status == "complete"
                upload_state = "done" if upload_done else "running"
                upload_ended_at = row.get("upload_finished_at") or (row.get("updated_at") if upload_done else None)
                upload_error = row.get("upload_error")
            _record(
                conn,
                deployment_key=deployment_key,
                deployment_type="cloudosd",
                source="cloudosd_autopilot_readiness",
                source_id=row["run_id"],
                phase_key="hash_upload",
                phase_label="Hash upload",
                state=upload_state,
                started_at=row.get("upload_started_at") or row.get("first_heartbeat_at") or row.get("created_at"),
                ended_at=upload_ended_at,
                last_progress_at=row.get("upload_finished_at") or row.get("upload_started_at") or row.get("updated_at"),
                evidence=evidence,
                error=upload_error,
            )


def sync_from_sources(conn: Connection) -> None:
    # Backfill can upsert the same deployment/phase rows from multiple
    # monitoring endpoints at once on first page load. Keep it single-flight
    # inside the web process so Postgres does not deadlock on identical rows.
    with _SYNC_LOCK:
        deployment_health_pg.init(conn)
        _sync_jobs(conn)
        _sync_legacy_runs(conn)
        _sync_ts_runs(conn)
        _sync_agent_work(conn)
        _sync_cloudosd(conn)
        deployment_health_pg.recompute_baselines(conn)


def _phase_threshold(phase_key: str) -> int:
    if phase_key in DEFAULT_PHASE_THRESHOLDS:
        return DEFAULT_PHASE_THRESHOLDS[phase_key]
    if phase_key.startswith("step:"):
        return DEFAULT_PHASE_THRESHOLDS["execution"]
    return GENERIC_PHASE_THRESHOLD_SECONDS


def _phase_health(phase: dict, baseline: dict | None, *, now: datetime | None = None) -> str:
    now = now or _now()
    state = phase.get("state")
    if state == "failed":
        return "failed"
    last_progress = _coerce_dt(phase.get("last_progress_at")) or _coerce_dt(phase.get("started_at"))
    if state in {"running", "pending"} and last_progress:
        stale_window = max(GENERIC_STALE_WINDOW_SECONDS, _phase_threshold(phase["phase_key"]))
        if int((now - last_progress).total_seconds()) > stale_window:
            return "stuck"
    duration = _safe_int(phase.get("duration_seconds"))
    if duration is None:
        return "healthy" if state in RUN_TERMINAL_STATES else "learning"
    if not baseline or baseline.get("sample_count", 0) < 5:
        return "learning"
    p95 = _safe_int(baseline.get("p95_seconds")) or 0
    threshold = max(_phase_threshold(phase["phase_key"]), p95)
    if p95 and duration > int(p95 * 1.25):
        return "regressed"
    if duration > threshold:
        return "slow"
    failure_rate = float(baseline.get("failure_rate") or 0)
    if failure_rate >= 0.10 and state == "done":
        return "regressed"
    return "healthy"


def _deployment_state(phases: list[dict]) -> str:
    states = {phase.get("state") for phase in phases}
    if "failed" in states:
        return "failed"
    if "stale" in states:
        return "stale"
    if phases and all(state in RUN_TERMINAL_STATES for state in states):
        return "done"
    if "running" in states:
        return "running"
    if "pending" in states:
        return "pending"
    return "unknown"


def _deployment_health(phases: list[dict]) -> str:
    if not phases:
        return "learning"
    return max((phase.get("health") or "learning" for phase in phases), key=lambda h: HEALTH_PRIORITY.get(h, 0))


def _seconds_since(value: Any) -> int | None:
    dt = _coerce_dt(value)
    if not dt:
        return None
    return max(0, int((_now() - dt).total_seconds()))


def _deployment_row(deployment_key: str, phases: list[dict]) -> dict:
    ordered = sorted(phases, key=lambda phase: (phase.get("started_at") or "", phase.get("phase_key") or ""))
    first = ordered[0]
    state = _deployment_state(ordered)
    health = _deployment_health(ordered)
    active = [phase for phase in ordered if phase.get("state") in {"running", "pending"}]
    failed_phases = [phase for phase in ordered if phase.get("state") == "failed"]
    if state == "failed" and failed_phases:
        current_phase = failed_phases[-1]
    else:
        current_phase = active[-1] if active else ordered[-1]
    started = _coerce_dt(ordered[0].get("started_at"))
    ended = max((_coerce_dt(phase.get("ended_at")) for phase in ordered if phase.get("ended_at")), default=None)
    duration = _duration(started, ended) if state == "done" else None
    if duration is None and started:
        duration = max(0, int((_now() - started).total_seconds()))
    last_progress = max(
        (_coerce_dt(phase.get("last_progress_at")) for phase in ordered if phase.get("last_progress_at")),
        default=None,
    )
    slowest_phase = max(
        ordered,
        key=lambda phase: _safe_int(phase.get("duration_seconds")) or 0,
    )
    evidence: dict[str, Any] = {}
    for phase in ordered:
        evidence.update(phase.get("evidence") or {})
    row = {
        "deployment_key": deployment_key,
        "deployment_type": first["deployment_type"],
        "source": first["source"],
        "source_id": first["source_id"],
        "state": state,
        "health": health,
        "current_phase": current_phase["phase_label"],
        "current_phase_key": current_phase["phase_key"],
        "elapsed_seconds": duration,
        "duration_seconds": duration if state == "done" else None,
        "last_progress_at": last_progress.isoformat() if last_progress else None,
        "last_progress_age_seconds": _seconds_since(last_progress),
        "started_at": started.isoformat() if started else None,
        "ended_at": ended.isoformat() if ended else None,
        "slowest_phase": slowest_phase["phase_label"],
        "slowest_phase_seconds": _safe_int(slowest_phase.get("duration_seconds")),
        "phase_count": len(ordered),
        "evidence": evidence,
        "detail_url": f"/api/monitoring/deployments/runs/{deployment_key}",
        "next_expected_evidence": _next_expected_evidence(current_phase),
    }
    return row


def _next_expected_evidence(phase: dict) -> str:
    if phase.get("state") in RUN_TERMINAL_STATES:
        return "terminal phase recorded"
    key = phase.get("phase_key")
    if key == "queue_wait":
        return "builder claim timestamp"
    if key == "execution":
        return "job heartbeat or exit code"
    if key == "proxmox_provision":
        return "PE registration"
    if key == "osdcloud":
        return "OSDCloud finish event"
    if key == "first_boot":
        return "first agent heartbeat"
    if key == "hash_upload":
        return "Autopilot hash upload result"
    if key and key.startswith("step:"):
        return "step finish, retry, or failure"
    return "phase progress timestamp"


def _completion_percentile(rows: list[dict], percentile: int) -> int | None:
    values = sorted(
        int(row["duration_seconds"])
        for row in rows
        if row.get("state") == "done" and row.get("duration_seconds") is not None
    )
    if not values:
        return None
    index = max(0, min(len(values) - 1, round((percentile / 100) * (len(values) - 1))))
    return values[index]


def _summary(rows: list[dict]) -> dict:
    completed = [row for row in rows if row.get("state") == "done"]
    failed = [row for row in rows if row.get("health") == "failed" or row.get("state") == "failed"]
    stuck = [row for row in rows if row.get("health") == "stuck"]
    regressed = [row for row in rows if row.get("health") == "regressed"]
    slow = [row for row in rows if row.get("health") == "slow"]
    active = [row for row in rows if row.get("state") in {"running", "pending", "stale"}]
    recent = rows[:50] if rows else []
    recent_failed = [row for row in recent if row.get("state") == "failed" or row.get("health") == "failed"]
    completion_values = [
        int(row["duration_seconds"])
        for row in completed
        if row.get("duration_seconds") is not None
    ]
    return {
        "total": len(rows),
        "active": len(active),
        "completed": len(completed),
        "failed": len(failed),
        "stuck": len(stuck),
        "regressed": len(regressed),
        "slow": len(slow),
        "median_completion_seconds": int(median(completion_values)) if completion_values else None,
        "p95_completion_seconds": _completion_percentile(completed, 95),
        "recent_failure_rate": round(len(recent_failed) / max(1, len(recent)), 3),
    }


def _bottlenecks(phases: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for phase in phases:
        if phase.get("health") in {"slow", "regressed", "stuck", "failed"}:
            grouped[(phase["deployment_type"], phase["phase_key"])].append(phase)
    rows: list[dict] = []
    for (deployment_type, phase_key), items in grouped.items():
        durations = [
            int(item["duration_seconds"])
            for item in items
            if item.get("duration_seconds") is not None
        ]
        rows.append({
            "deployment_type": deployment_type,
            "phase_key": phase_key,
            "phase_label": items[0]["phase_label"],
            "count": len(items),
            "health": max((item["health"] for item in items), key=lambda h: HEALTH_PRIORITY.get(h, 0)),
            "p95_seconds": _completion_percentile(
                [{"state": "done", "duration_seconds": duration} for duration in durations],
                95,
            ),
        })
    return sorted(rows, key=lambda row: (HEALTH_PRIORITY.get(row["health"], 0), row["count"]), reverse=True)[:10]


def _with_health(phases: list[dict], baselines: dict[tuple[str, str], dict]) -> list[dict]:
    out: list[dict] = []
    now = _now()
    for phase in phases:
        item = dict(phase)
        baseline = baselines.get((item["deployment_type"], item["phase_key"]))
        item["baseline"] = baseline
        item["health"] = _phase_health(item, baseline, now=now)
        if item.get("duration_seconds") is None and item.get("started_at"):
            started = _coerce_dt(item["started_at"])
            if started:
                item["elapsed_seconds"] = max(0, int((now - started).total_seconds()))
        else:
            item["elapsed_seconds"] = item.get("duration_seconds")
        out.append(item)
    return out


def build_deployments_payload(conn: Connection, *, limit: int = 100) -> dict:
    sync_from_sources(conn)
    baselines = _baseline_lookup(conn)
    phases = _with_health(deployment_health_pg.list_all_phases(conn, limit=5000), baselines)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for phase in phases:
        grouped[phase["deployment_key"]].append(phase)
    rows = [_deployment_row(key, items) for key, items in grouped.items()]
    rows = sorted(rows, key=lambda row: row.get("last_progress_at") or row.get("started_at") or "", reverse=True)
    active = [row for row in rows if row["state"] in {"running", "pending", "stale"}][:25]
    recent = [row for row in rows if row["state"] == "done"][:25]
    return {
        "schema_version": 1,
        "summary": _summary(rows),
        "runs": rows[: max(1, int(limit or 100))],
        "active": active,
        "recent_completions": recent,
        "bottlenecks": _bottlenecks(phases),
        "baselines": list(baselines.values()),
    }


def build_deployment_detail(conn: Connection, deployment_key: str) -> dict:
    sync_from_sources(conn)
    baselines = _baseline_lookup(conn)
    phases = _with_health(deployment_health_pg.list_phases(conn, deployment_key), baselines)
    if not phases:
        return {
            "deployment_key": deployment_key,
            "deployment_type": None,
            "source": None,
            "source_id": None,
            "state": "missing",
            "health": "learning",
            "phases": [],
            "evidence": {},
        }
    row = _deployment_row(deployment_key, phases)
    return {
        **row,
        "phases": phases,
    }


def build_baselines_payload(conn: Connection) -> dict:
    sync_from_sources(conn)
    return {
        "schema_version": 1,
        "baselines": deployment_health_pg.list_baselines(conn),
    }
