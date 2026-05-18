# WinPE PE Bootstrap Archive Notes

Archived branch: `archive/winpe-pe-bootstrap`

Archived commit: `475e7f8eca7d9a23bc4c1d183e2ea765d90447a9`

## Decision

Do not merge the archived branch back into `main`. It predates the current
MCP sidecar, Postgres-backed console surfaces, OSDeploy/CloudOSD separation,
install tracking, first-run docs, and agent telemetry work. A wholesale merge
would delete or replace current working code.

Use the archive only as reference material for additive WinPE/OSDeploy work.

## Useful Patterns To Extract

The old branch still has implementation ideas worth mining when the current
WinPE or OSDeploy path needs them:

- PE launcher heartbeat model: `build/launcher/Orchestrator.cs` sends checkins
  and periodic heartbeat payloads during long-running downloads and steps.
- Content download verification: the launcher streams content to disk and
  verifies SHA-256 before allowing the step to continue.
- Step dispatch boundary: `build/launcher/StepRunner.cs` keeps a fixed step
  allowlist instead of arbitrary script execution.
- Existing-Windows guard: `build/launcher/Guard.cs` checks for an existing
  Windows install before destructive PE actions.
- PE display/progress model: `build/launcher/Display.cs` and the launcher tests
  give a simple console progress surface suitable for PVE/VM console viewing.
- Build-host preflight: `build/PrePEFlight-Check.ps1` validates ADK, WinPE
  add-on, OpenSSH, runtime zips, build root layout, and NTFS work volume.
- PE WIM hardening: `build/Build-PeWim.ps1` captures the registry/env changes
  needed for PowerShell 7, .NET runtime, module paths, profile variables, and
  optional OpenSSH debugging inside WinPE.
- PE transport/steps modules: `build/pe-payload/Modules/Autopilot.PETransport`
  and `build/pe-payload/Modules/Autopilot.PESteps` are useful references for
  manifest fetch, content fetch, checkin, partition, apply-WIM, bcdboot,
  offline registry, driver injection, and scheduled-task staging.

## Superseded Pieces

Do not revive these parts without a fresh design:

- The old `/winpe/*` API as an active router. Current console state and MCP
  orchestration should remain the control plane.
- SQLite artifact/checkin databases under `var/artifacts`. Current persistent
  service state is Postgres-backed.
- Branch-level deletions of MCP, CloudOSD, OSDeploy, setup, install tracking,
  agent telemetry, or current tests.
- Any tracked generated database or build artifact.

## Current Extraction Guidance

When a current task needs PE bootstrap functionality, create a new branch from
`main` and copy only the minimum files or logic from the archive tag. Keep the
new work additive to the existing WinPE build scripts and OSDeploy surfaces.

Good first extraction candidates:

1. Port the `PrePEFlight-Check.ps1` validations into the current build-host
   readiness path.
2. Add SHA-verified streamed downloads and heartbeat callbacks to the current
   OSDeploy/WinPE bridge where long-running operations are opaque.
3. Add an explicit existing-Windows guard before any destructive PE disk action.
4. Convert the useful launcher behavior into tests around the current
   OSDeploy/WinPE implementation before changing runtime behavior.

