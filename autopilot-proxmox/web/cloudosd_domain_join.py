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
  * ``guest_exec(node, vmid, powershell, timeout=...) -> {"exited","exitcode","out-data","err-data"}``
  * ``resolve_credential(cred_id) -> {"username", "password"}``
  * ``resolve_node(vmid) -> node name`` (only consulted when the run row has no node)

Security note
-------------
The join credential travels to the guest inside the PowerShell script we hand to
Proxmox's ``agent/exec`` API. ``-EncodedCommand`` and the base64 password literal
are transport encodings, *not* secrecy: both are trivially reversible, so the
plaintext domain-join password is recoverable by anyone who can read Proxmox's
task log or API access log for the duration those records are kept. That is
inherent to driving an online join over QGA - the alternative is the offline
unattend djoin used when no ``domain_controller_ipv4`` is set. Treat the Proxmox
API logs as credential-bearing, and never log the generated script.
"""
from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from psycopg import Connection

from web import ts_engine_pg

# Runs whose join we will drive. Anything already finished or dead is excluded.
_TERMINAL_RUN_STATES = ("complete", "failed", "canceled")

# A candidate whose most recent heartbeat is older than this is treated as a
# dead run (deleted VM / pruned telemetry) and skipped, so the executor does not
# probe long-abandoned runs every tick.
_DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 1800

# Add-Computer is followed by a reboot we do not wait for: the guest is on its
# way down, so polling exec-status until the normal timeout only burns the tick.
_REBOOT_TIMEOUT_SECONDS = 5.0

# A real MDM_DevDetail_Ext01 DeviceHardwareData blob is several thousand base64
# characters. Anything materially shorter means the read was truncated (host
# line-wrapping, a partial exec-status capture), and persisting it would write a
# silently unusable Autopilot CSV - so we fail the step instead.
_MIN_HARDWARE_HASH_LENGTH = 512


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def heartbeat_cutoff(max_age_seconds: float | None, now: datetime) -> datetime | None:
    """Oldest heartbeat timestamp still considered live, or None for no gate."""
    if max_age_seconds is None:
        return None
    return now - timedelta(seconds=max_age_seconds)


def _heartbeat_is_stale(heartbeat_at: Any, cutoff: datetime | None) -> bool:
    """True when a candidate's newest heartbeat is missing or predates ``cutoff``.

    Fails closed: a missing timestamp, or one in an unexpected shape, counts as
    stale. Treating an unparseable timestamp as live would silently disable the
    guard the moment the column type changed.
    """
    if cutoff is None:
        return False
    if not isinstance(heartbeat_at, datetime):
        return True
    if heartbeat_at.tzinfo is None:
        heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
    return heartbeat_at < cutoff


# Newest heartbeat per run, mirroring agent_telemetry_pg.latest_for_run. Runs
# are ordered live-first so a backlog of abandoned runs can never crowd live
# ones out of the LIMIT (the freshness check used to run in Python, i.e. after
# the LIMIT had already been spent on the oldest - and therefore deadest - rows).
_HEARTBEAT_LATERAL = """
        LEFT JOIN LATERAL (
            SELECT hb.received_at
            FROM agent_heartbeats hb
            JOIN agent_devices d ON d.agent_id = hb.agent_id
            WHERE hb.current_run_id = r.run_id
              AND d.revoked = false
            ORDER BY hb.received_at DESC, hb.id DESC
            LIMIT 1
        ) hb ON true
"""
_HEARTBEAT_FRESH_FIRST = """
        ORDER BY (
            hb.received_at IS NOT NULL
            AND (%s::timestamptz IS NULL OR hb.received_at >= %s)
        ) DESC, r.created_at
"""


def _ps_quote(value: Any) -> str:
    """Escape a value for a PowerShell single-quoted string literal.

    A lone apostrophe (an OU path like ``OU=O'Brien,DC=corp`` is entirely
    ordinary) would otherwise terminate the literal and break the script.
    """
    return str(value or "").replace("'", "''")


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
    """Build the Add-Computer script.

    The password is carried as a base64 UTF-16LE literal and decoded inside the
    guest, so nothing in it - quotes, newlines, a here-string terminator - can
    break out of the script. Every other interpolated value goes through
    :func:`_ps_quote`, because an apostrophe in an OU path is entirely ordinary
    and would otherwise close the literal it sits in.
    """
    ou_clause = f" -OUPath '{_ps_quote(ou_path)}'" if ou_path else ""
    password_b64 = base64.b64encode(str(password or "").encode("utf-16-le")).decode("ascii")
    return (
        "$ErrorActionPreference='Stop';"
        "$pw = ConvertTo-SecureString ([Text.Encoding]::Unicode.GetString("
        f"[Convert]::FromBase64String('{password_b64}'))) -AsPlainText -Force;"
        f"$c = New-Object System.Management.Automation.PSCredential('{_ps_quote(username)}',$pw);"
        f"Add-Computer -DomainName '{_ps_quote(domain)}'{ou_clause} -Credential $c -Force;"
        "Write-Output 'JOIN_OK'"
    )


def probe_is_domain_joined(exec_result: dict) -> bool:
    """True when a probe result reports the guest is domain-joined."""
    return "DOMAIN=True" in str((exec_result or {}).get("out-data") or "")


def join_succeeded(exec_result: dict) -> bool:
    """True when an Add-Computer exec exited 0 and printed the success marker."""
    result = exec_result or {}
    return result.get("exitcode") == 0 and "JOIN_OK" in str(result.get("out-data") or "")


class GuestExecTimeout(RuntimeError):
    """agent-exec did not report an exit before the deadline."""


def guest_exec_via_proxmox(post_fn, get_fn, node, vmid, script, *, timeout: float = 90.0,
                           sleep: Callable[[float], None] = time.sleep) -> dict:
    """Run a PowerShell script in the guest via the Proxmox agent-exec API and
    poll agent-exec-status until it exits.

    Raises :class:`GuestExecTimeout` when the process has not exited within
    ``timeout``. Callers already treat a raised exception as "guest unreachable,
    retry next tick", so failing loudly beats returning a sentinel result that
    every caller would have to recognise on its own.
    """
    started = post_fn(
        f"/nodes/{node}/qemu/{vmid}/agent/exec",
        data={"command": ["powershell", "-NoProfile", "-EncodedCommand", encode_powershell(script)]},
    )
    pid = started.get("pid")
    deadline = time.monotonic() + timeout
    while True:
        status = get_fn(f"/nodes/{node}/qemu/{vmid}/agent/exec-status?pid={pid}")
        if status.get("exited"):
            return status
        if time.monotonic() >= deadline:
            raise GuestExecTimeout(
                f"agent-exec pid={pid} on {node}/{vmid} did not exit within {timeout:g}s"
            )
        sleep(2)


def find_join_candidates(
    conn: Connection, *, limit: int = 50, cutoff: datetime | None = None
) -> list[dict]:
    """Runs whose AD domain membership the executor still needs to drive.

    A candidate has an enabled domain join with a DC IP, a *done*
    ``wait_agent_heartbeat`` predecessor (proof the VM is booted into the full OS
    and reachable), and at least one still-pending membership step -
    ``join_domain_role`` (do the join) or ``verify_ad_domain_join`` (confirm it).
    Terminal runs are excluded. The executor owns both steps for these agent-less
    runs; a QGA ``PartOfDomain`` probe is the authoritative verification signal.

    Each row carries ``heartbeat_at`` (newest live heartbeat, or None) and rows
    are ordered live-first against ``cutoff``, so abandoned runs can never
    consume the ``limit`` ahead of runs that are actually running.
    """
    rows = conn.execute(
        """
        SELECT r.run_id, r.vmid, r.node, r.domain_join_json, r.vm_name,
               r.expected_computer_name, r.state, hb.received_at AS heartbeat_at
        FROM cloudosd_runs r
        """ + _HEARTBEAT_LATERAL + """
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
        """ + _HEARTBEAT_FRESH_FIRST + """
        LIMIT %s
        """,
        (list(_TERMINAL_RUN_STATES), cutoff, cutoff, limit),
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
            "heartbeat_at": row["heartbeat_at"],
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

    # credential_id is nullable on the run row (see cloudosd_pg._sanitize_domain_join),
    # so coerce defensively - int(None) would raise straight past the no_credential
    # branch below and surface as an opaque per-tick "failed".
    raw_credential_id = domain_join.get("credential_id")
    try:
        credential_id = int(raw_credential_id)
    except (TypeError, ValueError):
        _event("domain_join_failed", severity="warning",
               message="Domain join is enabled but no usable credential_id is stored",
               data={"vmid": vmid, "credential_id": str(raw_credential_id)})
        return {"vmid": vmid, "run_id": run_id, "status": "no_credential"}

    cred = resolve_credential(credential_id) or {}
    username = qualify_user(cred.get("username", ""), domain_join)
    password = cred.get("password", "")
    if not (username and password):
        _event("domain_join_failed", severity="warning",
                message="Domain-join credential missing username/password",
                data={"vmid": vmid, "credential_id": credential_id})
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

    # Join succeeded; a reboot finalizes membership. The guest is on its way down
    # so we do not wait for exec-status to report an exit - a short timeout keeps
    # one reboot from eating the tick. Reboot failures are non-fatal (the join
    # already took), so we swallow them and still mark the step done.
    try:
        guest_exec(node, vmid, _REBOOT_PS, timeout=_REBOOT_TIMEOUT_SECONDS)
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
    # rebooted flags the run so the hash-capture pass in the same tick does not
    # immediately probe a guest we just took down.
    return {"vmid": vmid, "run_id": run_id, "status": "joined", "rebooted": True}


def advance_domain_joined_runs(
    conn: Connection,
    *,
    latest_heartbeat: Callable[[str], dict | None],
    mark_complete: Callable[..., dict | None],
    max_heartbeat_age_seconds: float | None = None,
    now: Callable[[], datetime] | None = None,
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

    Carries the same live-first ordering and stale-run guard as the other two
    passes: a wedged run whose newest heartbeat predates
    ``max_heartbeat_age_seconds`` is skipped, and can never crowd a live run out
    of ``limit``.
    """
    cutoff = heartbeat_cutoff(max_heartbeat_age_seconds, (now or _utcnow)())
    rows = conn.execute(
        """
        SELECT r.run_id, hb.received_at AS heartbeat_at
        FROM cloudosd_runs r
        """ + _HEARTBEAT_LATERAL + """
        WHERE r.state IN ('full_os_waiting_domain_join', 'full_os_waiting_v2')
        """ + _HEARTBEAT_FRESH_FIRST + """
        LIMIT %s
        """,
        (cutoff, cutoff, limit),
    ).fetchall()
    advanced = 0
    stale = 0
    for row in rows:
        run_id = str(row["run_id"])
        if _heartbeat_is_stale(row["heartbeat_at"], cutoff):
            stale += 1
            continue
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
    return {"waiting": len(rows), "advanced": advanced, "stale": stale}


def run_pending_joins(
    conn: Connection,
    *,
    guest_exec: Callable[..., dict],
    resolve_credential: Callable[[int], dict],
    resolve_node: Callable[[int], Any] | None = None,
    append_event: Callable[..., Any] | None = None,
    max_heartbeat_age_seconds: float | None = None,
    now: Callable[[], datetime] | None = None,
    max_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    limit: int = 50,
) -> dict:
    """Drive every pending CloudOSD AD join. Per-run failures are isolated so
    one bad VM never blocks the others. Returns a summary of outcomes.

    When ``max_heartbeat_age_seconds`` is set, a run whose newest heartbeat is
    older than that (or has none) is skipped as ``stale`` - the VM is not
    currently alive, so probing it every tick is wasted work (and, for abandoned
    runs, needless failure-event noise). The candidate query orders live runs
    first, so stale ones never consume ``limit`` ahead of live ones.

    ``max_seconds`` bounds the wall-clock cost of one sweep: each guest probe can
    block for the guest_exec timeout, so an unbounded sweep over a large backlog
    could run well past its tick cadence. Candidates past the budget are counted
    as ``deferred`` and picked up next tick.
    """
    cutoff = heartbeat_cutoff(max_heartbeat_age_seconds, (now or _utcnow)())
    candidates = find_join_candidates(conn, limit=limit, cutoff=cutoff)
    deadline = None if max_seconds is None else monotonic() + max_seconds
    summary = {"candidates": len(candidates), "joined": 0, "already_joined": 0,
               "failed": 0, "unreachable": 0, "stale": 0, "skipped": 0,
               "deferred": 0, "rebooted_run_ids": [], "results": []}
    for candidate in candidates:
        if _heartbeat_is_stale(candidate.get("heartbeat_at"), cutoff):
            summary["stale"] += 1
            summary["results"].append({
                "vmid": candidate.get("vmid"), "run_id": candidate.get("run_id"),
                "status": "stale_no_heartbeat",
            })
            continue
        if deadline is not None and monotonic() >= deadline:
            summary["deferred"] += 1
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
        if result.get("rebooted") and result.get("run_id"):
            summary["rebooted_run_ids"].append(str(result["run_id"]))
        summary["results"].append(result)
    return summary


# --------------------------------------------------------------------------- #
# Autopilot hardware-hash capture (the other full_os step nothing executes)
# --------------------------------------------------------------------------- #
# The stock Get-WindowsAutopilotInfo.ps1 offline path reads exactly two CIM
# values: the serial from Win32_BIOS and the hardware hash from MDM_DevDetail_
# Ext01.DeviceHardwareData. We inline those two reads rather than shipping the
# ~500-line Microsoft script over the guest agent.
#
# Output goes through [Console]::Out, NOT Write-Output. Write-Output hands the
# string to PowerShell's formatter, which hard-wraps at the host width (120, or
# 80, when powershell.exe runs with no console attached - exactly how qemu-ga
# launches it). DeviceHardwareData is several thousand base64 characters, so
# Write-Output would split it across ~35 lines and any naive parse would persist
# a silently truncated, unusable hash. [Console]::Out bypasses the formatter and
# emits the string verbatim.
_HASH_CAPTURE_PS = (
    "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
    "$serial=(Get-CimInstance -Class Win32_BIOS).SerialNumber;"
    "$h=(Get-CimInstance -Namespace root/cimv2/mdm/dmmap -Class MDM_DevDetail_Ext01 "
    "-Filter \"InstanceID='Ext' AND ParentID='./DevDetail'\").DeviceHardwareData;"
    "[Console]::Out.WriteLine('SERIAL=' + $serial);"
    "[Console]::Out.WriteLine('HASH=' + $h)"
)


def parse_hash_output(exec_result: dict) -> tuple[str, str]:
    """Parse ``(serial, hardware_hash)`` from the capture script stdout.

    Lines after ``HASH=`` that do not open a new marker are treated as
    continuations and concatenated. The script writes through ``[Console]::Out``
    specifically so wrapping cannot happen, but base64 carries no whitespace, so
    reassembly is lossless - and it means a host that wraps anyway yields the
    whole hash rather than its first line.
    """
    serial = ""
    hash_parts: list[str] = []
    in_hash = False
    for raw_line in str((exec_result or {}).get("out-data") or "").splitlines():
        line = raw_line.strip()
        if line.startswith("SERIAL="):
            serial = line[len("SERIAL="):].strip()
            in_hash = False
        elif line.startswith("HASH="):
            hash_parts = [line[len("HASH="):].strip()]
            in_hash = True
        elif in_hash and line:
            hash_parts.append(line)
    return serial, "".join(hash_parts)


def find_hash_candidates(
    conn: Connection, *, limit: int = 50, cutoff: datetime | None = None
) -> list[dict]:
    """Runs with a pending ``capture_autopilot_hash`` step whose VM is booted
    into the full OS (``wait_agent_heartbeat`` done) and not terminal.

    Same ``heartbeat_at`` column and live-first ordering as
    :func:`find_join_candidates`.
    """
    rows = conn.execute(
        """
        SELECT r.run_id, r.vmid, r.node, r.vm_name, r.vm_group_tag, r.state,
               hb.received_at AS heartbeat_at
        FROM cloudosd_runs r
        JOIN ts_run_plan_steps h
          ON h.run_id = r.run_id AND h.kind = 'capture_autopilot_hash' AND h.state = 'pending'
        """ + _HEARTBEAT_LATERAL + """
        WHERE r.state <> ALL(%s)
          AND EXISTS (
              SELECT 1 FROM ts_run_plan_steps w
              WHERE w.run_id = r.run_id
                AND w.kind = 'wait_agent_heartbeat'
                AND w.state = 'done'
          )
        """ + _HEARTBEAT_FRESH_FIRST + """
        LIMIT %s
        """,
        (list(_TERMINAL_RUN_STATES), cutoff, cutoff, limit),
    ).fetchall()
    return [{
        "run_id": str(row["run_id"]), "vmid": row["vmid"], "node": row["node"],
        "vm_name": row["vm_name"], "group_tag": row["vm_group_tag"] or "",
        "heartbeat_at": row["heartbeat_at"],
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
    problem = ""
    if not serial:
        problem = "no serial in guest output"
    elif not hardware_hash:
        problem = "no hardware hash in guest output"
    elif len(hardware_hash) < _MIN_HARDWARE_HASH_LENGTH:
        # A truncated hash produces a CSV Intune rejects. Fail the step so the
        # next tick retries, rather than marking it done with unusable data.
        problem = (
            f"hardware hash is {len(hardware_hash)} chars, expected at least "
            f"{_MIN_HARDWARE_HASH_LENGTH} (truncated capture)"
        )
    if problem:
        err = str(result.get("err-data") or result.get("out-data") or "")[:300]
        _event("autopilot_hash_capture_failed", severity="warning",
                message=f"Could not read a usable hardware hash from guest: {problem}",
                data={"error": err, "problem": problem, "vmid": vmid})
        return {"vmid": vmid, "run_id": run_id, "status": "failed", "error": problem}

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
    guest_exec: Callable[..., dict],
    persist_hash: Callable[..., Any],
    resolve_node: Callable[[int], Any] | None = None,
    append_event: Callable[..., Any] | None = None,
    max_heartbeat_age_seconds: float | None = None,
    now: Callable[[], datetime] | None = None,
    max_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    skip_run_ids: set[str] | None = None,
    limit: int = 50,
) -> dict:
    """Capture the Autopilot hardware hash for every run whose step is pending.
    Same per-run isolation, stale-heartbeat guard and time budget as
    :func:`run_pending_joins`.

    ``skip_run_ids`` holds runs the join pass just rebooted in this same tick;
    their guests are on the way down, so probing them now would only burn the
    guest_exec timeout to learn they are unreachable.
    """
    cutoff = heartbeat_cutoff(max_heartbeat_age_seconds, (now or _utcnow)())
    candidates = find_hash_candidates(conn, limit=limit, cutoff=cutoff)
    deadline = None if max_seconds is None else monotonic() + max_seconds
    rebooting = skip_run_ids or set()
    summary = {"candidates": len(candidates), "captured": 0, "failed": 0,
               "unreachable": 0, "stale": 0, "skipped": 0, "deferred": 0,
               "rebooting": 0, "results": []}
    for candidate in candidates:
        if _heartbeat_is_stale(candidate.get("heartbeat_at"), cutoff):
            summary["stale"] += 1
            summary["results"].append({
                "vmid": candidate.get("vmid"), "run_id": candidate.get("run_id"),
                "status": "stale_no_heartbeat",
            })
            continue
        if candidate["run_id"] in rebooting:
            summary["rebooting"] += 1
            summary["results"].append({
                "vmid": candidate.get("vmid"), "run_id": candidate.get("run_id"),
                "status": "rebooting_after_join",
            })
            continue
        if deadline is not None and monotonic() >= deadline:
            summary["deferred"] += 1
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
