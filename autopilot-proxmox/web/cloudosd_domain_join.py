"""Server-side executor for the CloudOSD full_os ``join_domain_role`` step.

Background
----------
When a CloudOSD deployment carries a ``domain_controller_ipv4`` the AD join is
compiled as a ``full_os`` ``join_domain_role`` step (see
``cloudosd_pg._create_sequence_for_run``) instead of being baked into the PE
package as an offline-unattend djoin. Nothing reliably executes that step for
the isolated-DC case: the AutopilotAgent stays in the ``cloudosd`` phase and
never claims full_os osd_v2 work, and the ``cloudosd-controller`` lifecycle sync
only advances steps from evidence, it does not perform the join. So runs wedge
at ``full_os_waiting_domain_join`` and never receive a heartbeat reporting the
expected domain, which is what ``mark_complete_from_heartbeat`` waits on.

This module performs the join server-side over the QEMU guest agent (the same
transport the monitor already uses for screenshots), then marks the
``join_domain_role`` step done. The next AutopilotAgent heartbeat reports
``domain_joined=true`` and the existing ``mark_complete_from_heartbeat`` path
drives the run to completion.

All I/O is injected so the decision logic is unit-testable without live infra:
  * ``guest_exec(node, vmid, powershell) -> {"exited","exitcode","out-data","err-data"}``
  * ``resolve_credential(cred_id) -> {"username", "password"}``
  * ``resolve_node(vmid) -> node name`` (only consulted when the run row has no node)
"""
from __future__ import annotations

import base64
import time
from datetime import datetime, timezone
from typing import Any, Callable

from psycopg import Connection

from web import ts_engine_pg

# Runs whose join we will drive. Anything already finished or dead is excluded.
_TERMINAL_RUN_STATES = ("complete", "failed", "canceled")

# A candidate whose most recent heartbeat is older than this is treated as a
# dead run (deleted VM / pruned telemetry) and skipped, so the executor does not
# probe long-abandoned runs every tick.
_DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 1800


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _heartbeat_is_stale(heartbeat: dict | None, now: datetime, max_age_seconds: float) -> bool:
    """True when there is no heartbeat, or the newest one is older than the window."""
    if not heartbeat:
        return True
    received = heartbeat.get("received_at")
    if not isinstance(received, datetime):
        # Unknown/absent timestamp shape: don't over-filter a live-looking run.
        return received is None
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    return (now - received).total_seconds() > max_age_seconds

_PROBE_PS = (
    "$cs = Get-CimInstance Win32_ComputerSystem; "
    "Write-Output ('DOMAIN=' + $cs.PartOfDomain + ';NAME=' + $cs.Domain "
    "+ ';HOST=' + $env:COMPUTERNAME)"
)
_REBOOT_PS = "Restart-Computer -Force"


def encode_powershell(script: str) -> str:
    """Return the UTF-16LE base64 form PowerShell -EncodedCommand expects.

    Encoding sidesteps every layer of quote/escape mangling between the HTTP
    form body, the guest agent, and cmd.exe.
    """
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def qualify_user(username: str, domain_join: dict) -> str:
    """Return a domain-qualified logon name.

    Pass through anything already qualified (``DOMAIN\\user`` or ``user@upn``);
    otherwise append ``@<credential_domain or domain_fqdn>`` so a bare account
    name binds against the right domain.
    """
    username = (username or "").strip()
    if not username or "\\" in username or "@" in username:
        return username
    domain = str(domain_join.get("credential_domain") or domain_join.get("domain_fqdn") or "").strip()
    return f"{username}@{domain}" if domain else username


def build_join_powershell(*, username: str, password: str, domain: str, ou_path: str = "") -> str:
    """Build the Add-Computer script. Password goes in a here-string so special
    characters never need escaping."""
    ou_clause = f" -OUPath '{ou_path}'" if ou_path else ""
    return (
        "$ErrorActionPreference='Stop';"
        f"$pw = ConvertTo-SecureString @'\n{password}\n'@ -AsPlainText -Force;"
        f"$c = New-Object System.Management.Automation.PSCredential('{username}',$pw);"
        f"Add-Computer -DomainName '{domain}'{ou_clause} -Credential $c -Force;"
        "Write-Output 'JOIN_OK'"
    )


def probe_is_domain_joined(exec_result: dict) -> bool:
    """True when a probe result reports the guest is domain-joined."""
    return "DOMAIN=True" in str((exec_result or {}).get("out-data") or "")


def join_succeeded(exec_result: dict) -> bool:
    """True when an Add-Computer exec exited 0 and printed the success marker."""
    result = exec_result or {}
    return result.get("exitcode") == 0 and "JOIN_OK" in str(result.get("out-data") or "")


def guest_exec_via_proxmox(post_fn, get_fn, node, vmid, script, *, timeout: float = 90.0,
                           sleep: Callable[[float], None] = time.sleep) -> dict:
    """Run a PowerShell script in the guest via the Proxmox agent-exec API and
    poll agent-exec-status until it exits (or ``timeout`` elapses)."""
    started = post_fn(
        f"/nodes/{node}/qemu/{vmid}/agent/exec",
        data={"command": ["powershell", "-NoProfile", "-EncodedCommand", encode_powershell(script)]},
    )
    pid = started.get("pid")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = get_fn(f"/nodes/{node}/qemu/{vmid}/agent/exec-status?pid={pid}")
        if status.get("exited"):
            return status
        sleep(2)
    return {"exited": 0, "exitcode": None, "err-data": "timeout waiting for exec-status"}


def find_join_candidates(conn: Connection, *, limit: int = 50) -> list[dict]:
    """Runs whose AD domain membership the executor still needs to drive.

    A candidate has an enabled domain join with a DC IP, a *done*
    ``wait_agent_heartbeat`` predecessor (proof the VM is booted into the full OS
    and reachable), and at least one still-pending membership step -
    ``join_domain_role`` (do the join) or ``verify_ad_domain_join`` (confirm it).
    Terminal runs are excluded. The executor owns both steps for these agent-less
    runs; a QGA ``PartOfDomain`` probe is the authoritative verification signal.
    """
    rows = conn.execute(
        """
        SELECT r.run_id, r.vmid, r.node, r.domain_join_json, r.vm_name,
               r.expected_computer_name, r.state
        FROM cloudosd_runs r
        WHERE COALESCE(r.domain_join_json->>'enabled', 'false') = 'true'
          AND COALESCE(r.domain_join_json->>'domain_controller_ipv4', '') <> ''
          AND r.state <> ALL(%s)
          AND EXISTS (
              SELECT 1 FROM ts_run_plan_steps h
              WHERE h.run_id = r.run_id
                AND h.kind = 'wait_agent_heartbeat'
                AND h.state = 'done'
          )
          AND EXISTS (
              SELECT 1 FROM ts_run_plan_steps s
              WHERE s.run_id = r.run_id
                AND s.kind IN ('join_domain_role', 'verify_ad_domain_join')
                AND s.state = 'pending'
          )
        ORDER BY r.created_at
        LIMIT %s
        """,
        (list(_TERMINAL_RUN_STATES), limit),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        dj = row["domain_join_json"]
        if not isinstance(dj, dict):
            continue
        out.append({
            "run_id": str(row["run_id"]),
            "vmid": row["vmid"],
            "node": row["node"],
            "domain_join": dj,
            "vm_name": row["vm_name"],
        })
    return out


def _mark_steps(conn: Connection, run_id: str, kinds: list[str], *, message: str, data: dict) -> None:
    ts_engine_pg.mark_steps_done_by_kind(
        conn,
        run_id=run_id,
        kinds=kinds,
        agent_id="cloudosd-domain-join",
        message=message,
        data=data,
    )


def execute_join_for_run(
    conn: Connection,
    candidate: dict,
    *,
    guest_exec: Callable[[str, int, str], dict],
    resolve_credential: Callable[[int], dict],
    resolve_node: Callable[[int], Any] | None = None,
    append_event: Callable[..., Any] | None = None,
) -> dict:
    """Join one VM. Idempotent: probes first and only issues Add-Computer when
    the guest is reachable and reports it is *not* joined.

    Returns ``{"vmid","status", ...}`` where status is one of
    ``already_joined | joined | failed | unreachable | no_node | no_credential``.
    """
    vmid = candidate["vmid"]
    run_id = candidate["run_id"]
    domain_join = candidate["domain_join"]
    node = candidate.get("node") or (resolve_node(vmid) if resolve_node else None)
    if not node:
        return {"vmid": vmid, "run_id": run_id, "status": "no_node"}

    def _event(event_type, *, severity="info", message=None, data=None):
        if append_event is not None:
            append_event(conn, run_id=run_id, phase="domain_join",
                         event_type=event_type, severity=severity,
                         message=message, data=data or {})

    # Probe first. If the guest is unreachable (mid-reboot) we retry next tick
    # rather than risk an Add-Computer we cannot confirm.
    try:
        probe = guest_exec(node, vmid, _PROBE_PS)
    except Exception as exc:  # noqa: BLE001 - transient guest/API failure, retry later
        return {"vmid": vmid, "run_id": run_id, "status": "unreachable", "error": str(exc)[:200]}

    if probe_is_domain_joined(probe):
        # The QGA PartOfDomain probe confirms membership, so complete BOTH the
        # join and the verify step, and emit the domain_join_verified evidence
        # (the same event the osd_v2 verify path emits) so the run can complete.
        _mark_steps(conn, run_id, ["join_domain_role", "verify_ad_domain_join"],
                    message="Guest confirmed in AD domain via PartOfDomain probe",
                    data={"vmid": vmid, "source": "probe"})
        _event("domain_join_verified",
               message="Guest confirmed joined to the AD domain",
               data={"vmid": vmid, "source": "probe"})
        return {"vmid": vmid, "run_id": run_id, "status": "already_joined"}

    if "DOMAIN=False" not in str(probe.get("out-data") or ""):
        # Probe returned but not a shape we understand; do not attempt a blind join.
        return {"vmid": vmid, "run_id": run_id, "status": "unreachable",
                "error": f"unexpected probe output: {str(probe.get('out-data') or '')[:120]}"}

    cred = resolve_credential(int(domain_join.get("credential_id"))) or {}
    username = qualify_user(cred.get("username", ""), domain_join)
    password = cred.get("password", "")
    if not (username and password):
        _event("domain_join_failed", severity="warning",
                message="Domain-join credential missing username/password")
        return {"vmid": vmid, "run_id": run_id, "status": "no_credential"}

    join_ps = build_join_powershell(
        username=username, password=password,
        domain=str(domain_join.get("domain_fqdn") or ""),
        ou_path=str(domain_join.get("ou_path") or ""),
    )
    try:
        result = guest_exec(node, vmid, join_ps)
    except Exception as exc:  # noqa: BLE001
        return {"vmid": vmid, "run_id": run_id, "status": "unreachable", "error": str(exc)[:200]}

    if not join_succeeded(result):
        err = str(result.get("err-data") or result.get("out-data") or "")[:300]
        _event("domain_join_failed", severity="warning",
                message="Add-Computer did not report success", data={"error": err, "vmid": vmid})
        return {"vmid": vmid, "run_id": run_id, "status": "failed", "error": err}

    # Join succeeded; a reboot finalizes membership. Reboot failures are
    # non-fatal (the join already took) so we swallow them and still mark done.
    try:
        guest_exec(node, vmid, _REBOOT_PS)
    except Exception:  # noqa: BLE001
        pass
    # Mark only the join done here; verify_ad_domain_join is confirmed on a later
    # tick once the guest has rebooted and the PartOfDomain probe succeeds.
    _mark_steps(conn, run_id, ["join_domain_role"],
                message="Server-side Add-Computer AD join completed",
                data={"vmid": vmid, "source": "add_computer"})
    _event("domain_join_executed",
           message=f"Joined {candidate.get('vm_name') or vmid} to {domain_join.get('domain_fqdn')}",
           data={"vmid": vmid})
    return {"vmid": vmid, "run_id": run_id, "status": "joined"}


def advance_domain_joined_runs(
    conn: Connection,
    *,
    latest_heartbeat: Callable[[str], dict | None],
    mark_complete: Callable[..., dict | None],
    limit: int = 50,
) -> dict:
    """Drive runs wedged at ``full_os_waiting_domain_join`` or
    ``full_os_waiting_v2`` forward by re-running the completion path.

    CloudOSD only re-evaluates completion lazily (on a run-detail page view), so
    a run can sit in a full_os waiting state long after its guest is actually
    joined and its v2 steps are done. This pass calls the same completion path
    the page view uses; ``mark_complete_from_heartbeat`` only advances when the
    heartbeat verification matches and, for the final flip to ``complete``, only
    when ``v2_completion_status`` is ready - so a not-yet-ready run is left in
    place, not falsely completed.
    """
    rows = conn.execute(
        """
        SELECT run_id FROM cloudosd_runs
        WHERE state IN ('full_os_waiting_domain_join', 'full_os_waiting_v2')
        ORDER BY created_at
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    advanced = 0
    for row in rows:
        run_id = str(row["run_id"])
        heartbeat = latest_heartbeat(run_id)
        if not heartbeat:
            continue
        try:
            run = mark_complete(
                conn, run_id=run_id,
                heartbeat_at=heartbeat["received_at"], heartbeat=heartbeat,
            )
        except Exception:  # noqa: BLE001 - one bad run must not block the rest
            continue
        if run and run.get("state") not in (
            "full_os_waiting_domain_join", "full_os_waiting_v2"
        ):
            advanced += 1
    return {"waiting": len(rows), "advanced": advanced}


def run_pending_joins(
    conn: Connection,
    *,
    guest_exec: Callable[[str, int, str], dict],
    resolve_credential: Callable[[int], dict],
    resolve_node: Callable[[int], Any] | None = None,
    append_event: Callable[..., Any] | None = None,
    latest_heartbeat: Callable[[str], dict | None] | None = None,
    max_heartbeat_age_seconds: float = _DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
    now: Callable[[], datetime] | None = None,
    limit: int = 50,
) -> dict:
    """Drive every pending CloudOSD AD join. Per-run failures are isolated so
    one bad VM never blocks the others. Returns a summary of outcomes.

    When ``latest_heartbeat`` is supplied, a run whose newest heartbeat is older
    than ``max_heartbeat_age_seconds`` (or has none) is skipped as ``stale`` -
    the run's VM is not currently alive, so probing it every tick is wasted work
    (and, for abandoned test runs, needless failure-event noise).
    """
    candidates = find_join_candidates(conn, limit=limit)
    clock = now or _utcnow
    summary = {"candidates": len(candidates), "joined": 0, "already_joined": 0,
               "failed": 0, "unreachable": 0, "stale": 0, "skipped": 0, "results": []}
    for candidate in candidates:
        if latest_heartbeat is not None and _heartbeat_is_stale(
            latest_heartbeat(candidate["run_id"]), clock(), max_heartbeat_age_seconds
        ):
            summary["stale"] += 1
            summary["results"].append({
                "vmid": candidate.get("vmid"), "run_id": candidate.get("run_id"),
                "status": "stale_no_heartbeat",
            })
            continue
        try:
            result = execute_join_for_run(
                conn, candidate,
                guest_exec=guest_exec,
                resolve_credential=resolve_credential,
                resolve_node=resolve_node,
                append_event=append_event,
            )
        except Exception as exc:  # noqa: BLE001 - never let one run kill the sweep
            result = {"vmid": candidate.get("vmid"), "run_id": candidate.get("run_id"),
                      "status": "failed", "error": str(exc)[:200]}
        status = result.get("status")
        if status == "joined":
            summary["joined"] += 1
        elif status == "already_joined":
            summary["already_joined"] += 1
        elif status in ("failed",):
            summary["failed"] += 1
        elif status in ("unreachable",):
            summary["unreachable"] += 1
        else:
            summary["skipped"] += 1
        summary["results"].append(result)
    return summary


# --------------------------------------------------------------------------- #
# Autopilot hardware-hash capture (the other full_os step nothing executes)
# --------------------------------------------------------------------------- #
# The stock Get-WindowsAutopilotInfo.ps1 offline path reads exactly two CIM
# values: the serial from Win32_BIOS and the hardware hash from MDM_DevDetail_
# Ext01.DeviceHardwareData. We inline those two reads rather than shipping the
# ~500-line Microsoft script over the guest agent.
_HASH_CAPTURE_PS = (
    "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
    "$serial=(Get-CimInstance -Class Win32_BIOS).SerialNumber;"
    "$h=(Get-CimInstance -Namespace root/cimv2/mdm/dmmap -Class MDM_DevDetail_Ext01 "
    "-Filter \"InstanceID='Ext' AND ParentID='./DevDetail'\").DeviceHardwareData;"
    "Write-Output ('SERIAL=' + $serial);"
    "Write-Output ('HASH=' + $h)"
)


def parse_hash_output(exec_result: dict) -> tuple[str, str]:
    """Parse ``(serial, hardware_hash)`` from the capture script stdout."""
    serial = hardware_hash = ""
    for line in str((exec_result or {}).get("out-data") or "").splitlines():
        line = line.strip()
        if line.startswith("SERIAL="):
            serial = line[len("SERIAL="):].strip()
        elif line.startswith("HASH="):
            hardware_hash = line[len("HASH="):].strip()
    return serial, hardware_hash


def find_hash_candidates(conn: Connection, *, limit: int = 50) -> list[dict]:
    """Runs with a pending ``capture_autopilot_hash`` step whose VM is booted
    into the full OS (``wait_agent_heartbeat`` done) and not terminal."""
    rows = conn.execute(
        """
        SELECT r.run_id, r.vmid, r.node, r.vm_name, r.vm_group_tag, r.state
        FROM cloudosd_runs r
        JOIN ts_run_plan_steps h
          ON h.run_id = r.run_id AND h.kind = 'capture_autopilot_hash' AND h.state = 'pending'
        WHERE r.state <> ALL(%s)
          AND EXISTS (
              SELECT 1 FROM ts_run_plan_steps w
              WHERE w.run_id = r.run_id
                AND w.kind = 'wait_agent_heartbeat'
                AND w.state = 'done'
          )
        ORDER BY r.created_at
        LIMIT %s
        """,
        (list(_TERMINAL_RUN_STATES), limit),
    ).fetchall()
    return [{
        "run_id": str(row["run_id"]), "vmid": row["vmid"], "node": row["node"],
        "vm_name": row["vm_name"], "group_tag": row["vm_group_tag"] or "",
    } for row in rows]


def execute_hash_capture_for_run(
    conn: Connection,
    candidate: dict,
    *,
    guest_exec: Callable[[str, int, str], dict],
    persist_hash: Callable[..., Any],
    resolve_node: Callable[[int], Any] | None = None,
    append_event: Callable[..., Any] | None = None,
) -> dict:
    """Capture one VM's Autopilot hardware hash over the guest agent, persist it,
    and mark ``capture_autopilot_hash`` done. Returns a status dict."""
    vmid = candidate["vmid"]
    run_id = candidate["run_id"]
    node = candidate.get("node") or (resolve_node(vmid) if resolve_node else None)
    if not node:
        return {"vmid": vmid, "run_id": run_id, "status": "no_node"}

    def _event(event_type, *, severity="info", message=None, data=None):
        if append_event is not None:
            append_event(conn, run_id=run_id, phase="full_os",
                         event_type=event_type, severity=severity,
                         message=message, data=data or {})

    try:
        result = guest_exec(node, vmid, _HASH_CAPTURE_PS)
    except Exception as exc:  # noqa: BLE001 - transient guest/API failure, retry later
        return {"vmid": vmid, "run_id": run_id, "status": "unreachable", "error": str(exc)[:200]}

    serial, hardware_hash = parse_hash_output(result)
    if not (serial and hardware_hash):
        err = str(result.get("err-data") or result.get("out-data") or "")[:300]
        _event("autopilot_hash_capture_failed", severity="warning",
                message="Could not read serial/hardware hash from guest",
                data={"error": err, "vmid": vmid})
        return {"vmid": vmid, "run_id": run_id, "status": "failed", "error": err or "empty hash"}

    try:
        persist_hash(vmid=int(vmid), serial=serial, product_id="",
                     hardware_hash=hardware_hash, group_tag=candidate.get("group_tag") or "")
    except Exception as exc:  # noqa: BLE001
        return {"vmid": vmid, "run_id": run_id, "status": "failed",
                "error": f"persist failed: {str(exc)[:150]}"}

    ts_engine_pg.mark_steps_done_by_kind(
        conn, run_id=run_id, kinds=["capture_autopilot_hash"],
        agent_id="cloudosd-hash-capture",
        message="Server-side Autopilot hardware-hash capture completed",
        data={"vmid": vmid, "serial": serial})
    _event("autopilot_hash_captured",
           message=f"Captured Autopilot hardware hash for {candidate.get('vm_name') or vmid}",
           data={"vmid": vmid, "serial": serial})
    return {"vmid": vmid, "run_id": run_id, "status": "captured"}


def run_pending_hash_captures(
    conn: Connection,
    *,
    guest_exec: Callable[[str, int, str], dict],
    persist_hash: Callable[..., Any],
    resolve_node: Callable[[int], Any] | None = None,
    append_event: Callable[..., Any] | None = None,
    latest_heartbeat: Callable[[str], dict | None] | None = None,
    max_heartbeat_age_seconds: float = _DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
    now: Callable[[], datetime] | None = None,
    limit: int = 50,
) -> dict:
    """Capture the Autopilot hardware hash for every run whose step is pending.
    Same per-run isolation and stale-heartbeat guard as ``run_pending_joins``."""
    candidates = find_hash_candidates(conn, limit=limit)
    clock = now or _utcnow
    summary = {"candidates": len(candidates), "captured": 0, "failed": 0,
               "unreachable": 0, "stale": 0, "skipped": 0, "results": []}
    for candidate in candidates:
        if latest_heartbeat is not None and _heartbeat_is_stale(
            latest_heartbeat(candidate["run_id"]), clock(), max_heartbeat_age_seconds
        ):
            summary["stale"] += 1
            summary["results"].append({
                "vmid": candidate.get("vmid"), "run_id": candidate.get("run_id"),
                "status": "stale_no_heartbeat",
            })
            continue
        try:
            result = execute_hash_capture_for_run(
                conn, candidate,
                guest_exec=guest_exec,
                persist_hash=persist_hash,
                resolve_node=resolve_node,
                append_event=append_event,
            )
        except Exception as exc:  # noqa: BLE001 - never let one run kill the sweep
            result = {"vmid": candidate.get("vmid"), "run_id": candidate.get("run_id"),
                      "status": "failed", "error": str(exc)[:200]}
        status = result.get("status")
        if status == "captured":
            summary["captured"] += 1
        elif status == "failed":
            summary["failed"] += 1
        elif status == "unreachable":
            summary["unreachable"] += 1
        else:
            summary["skipped"] += 1
        summary["results"].append(result)
    return summary
