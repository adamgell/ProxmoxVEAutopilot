# Agent PowerShell Delivery v1

## Goal

Deliver a real-time, auditable PowerShell 7 runbook to one registered Autopilot Agent and return bounded structured proof to the controller.

## Scope

This first slice intentionally delivers one fixed, read-only runbook: `endpoint_facts`.
It reports the endpoint name, Windows version, PowerShell version, current user, and IPv4 addresses.  It does not accept an arbitrary script body, credentials, or a remote connection string.

## Architecture

The controller queues a `remote_powershell` work item only for an existing Agent ID and only when the request specifies `command_id: endpoint_facts`. The existing work-item claim, completion, and failure API provides delivery state and result retrieval. The Agent advertises the work kind, writes the packaged runbook to its ProgramData work directory, invokes `pwsh.exe` non-interactively as the installed service identity, and returns a JSON result.

The Agent enforces a 60-second timeout, kills the full process tree on timeout/cancellation, and limits returned stdout/stderr to 64 KiB. A non-zero exit, malformed JSON, unsupported request fields, unsupported command ID, or oversized output fails the work item with bounded diagnostic detail.

## Data Flow

1. An authenticated controller caller posts the Agent ID and `command_id: endpoint_facts`.
2. The controller validates that the Agent exists and stores `remote_powershell` as a standard pending work item.
3. The target Agent claims only work kinds it advertised and runs the packaged PowerShell 7 runbook.
4. The Agent completes the item with `command_id`, `computer_name`, `os_version`, `powershell_version`, `current_user`, and `ipv4_addresses`.
5. The controller exposes the standard work-item state and result. This is the v1 delivery/audit surface; live line-by-line streaming and cancellation control are deferred until the first job is proven in LABZ1.

## Security and Operational Constraints

- No arbitrary script input in v1.
- No embedded credentials, tokens, keys, installer media, or private configuration.
- The runbook is local to the installed Agent; it is not WinRM or QGA.
- The target is exactly the Agent ID selected at queue time.
- PowerShell is invoked with `-NoProfile`, `-NonInteractive`, and `-ExecutionPolicy Bypass` for the packaged runbook only.
- The first live proof targets `agent-ring0ivy24-01` and performs no mutation.

## Success Criteria

- Contract tests prove the Agent advertises `remote_powershell`, rejects arbitrary/unknown request content, and recognizes the packaged endpoint-facts runbook.
- Controller tests prove an existing Agent can queue the fixed runbook and an unknown Agent is rejected.
- A direct release updates the LABZ1 endpoint Agent without waiting for CI.
- A live work item completes on `agent-ring0ivy24-01` with structured endpoint facts from PowerShell 7.

## Follow-up Slices

1. Controller/UI run history with live progress events and cancel requests.
2. Versioned, signed/hashed approved runbooks and per-command operator approvals.
3. Explicit mutating runbooks with dry-run/result schemas and policy controls.
