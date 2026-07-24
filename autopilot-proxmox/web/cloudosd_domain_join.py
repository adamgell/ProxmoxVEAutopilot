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
from typing import Any, Callable

from psycopg import Connection

from web import ts_engine_pg

# Runs whose join we will drive. Anything already finished or dead is excluded.
_TERMINAL_RUN_STATES = ("complete", "failed", "canceled")

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
    """Runs that need a server-side AD join right now.

    A candidate has an enabled domain join with a DC IP, a still-pending
    ``join_domain_role`` step, and a *done* ``wait_agent_heartbeat`` predecessor
    (proof the VM is booted into the full OS and reachable). Terminal runs are
    excluded.
    """
    rows = conn.execute(
        """
        SELECT r.run_id, r.vmid, r.node, r.domain_join_json, r.vm_name,
               r.expected_computer_name, r.state
        FROM cloudosd_runs r
        JOIN ts_run_plan_steps j
          ON j.run_id = r.run_id AND j.kind = 'join_domain_role' AND j.state = 'pending'
        WHERE COALESCE(r.domain_join_json->>'enabled', 'false') = 'true'
          AND COALESCE(r.domain_join_json->>'domain_controller_ipv4', '') <> ''
          AND r.state <> ALL(%s)
          AND EXISTS (
              SELECT 1 FROM ts_run_plan_steps h
              WHERE h.run_id = r.run_id
                AND h.kind = 'wait_agent_heartbeat'
                AND h.state = 'done'
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


def _mark_join_done(conn: Connection, run_id: str, *, message: str, data: dict) -> None:
    ts_engine_pg.mark_steps_done_by_kind(
        conn,
        run_id=run_id,
        kinds=["join_domain_role"],
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
        _mark_join_done(conn, run_id, message="Guest already reports AD domain membership",
                        data={"vmid": vmid, "source": "probe"})
        _event("domain_join_already_present", message="Guest already domain-joined; marked step done")
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
    _mark_join_done(conn, run_id, message="Server-side Add-Computer AD join completed",
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
    """Drive runs wedged at ``full_os_waiting_domain_join`` forward once their
    heartbeat reports the expected domain.

    CloudOSD only re-evaluates completion lazily (on a run-detail page view), so
    a run can stay in ``full_os_waiting_domain_join`` for a long time after the
    guest is actually joined. This pass calls the same completion path the page
    view uses; ``mark_complete_from_heartbeat`` only advances when the heartbeat
    verification matches, so a still-unjoined run is left untouched.
    """
    rows = conn.execute(
        """
        SELECT run_id FROM cloudosd_runs
        WHERE state = 'full_os_waiting_domain_join'
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
        if run and run.get("state") != "full_os_waiting_domain_join":
            advanced += 1
    return {"waiting": len(rows), "advanced": advanced}


def run_pending_joins(
    conn: Connection,
    *,
    guest_exec: Callable[[str, int, str], dict],
    resolve_credential: Callable[[int], dict],
    resolve_node: Callable[[int], Any] | None = None,
    append_event: Callable[..., Any] | None = None,
    limit: int = 50,
) -> dict:
    """Drive every pending CloudOSD AD join. Per-run failures are isolated so
    one bad VM never blocks the others. Returns a summary of outcomes."""
    candidates = find_join_candidates(conn, limit=limit)
    summary = {"candidates": len(candidates), "joined": 0, "already_joined": 0,
               "failed": 0, "unreachable": 0, "skipped": 0, "results": []}
    for candidate in candidates:
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
