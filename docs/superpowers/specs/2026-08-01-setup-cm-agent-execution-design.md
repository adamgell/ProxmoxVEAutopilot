# Setup-CM Autopilot Agent Execution Design

## Goal

Make AutopilotAgent the required execution plane for heavyweight Setup-CM work on LABZ1-CM01 while keeping the Setup-CM PowerShell module as the single deployment engine.

## Scope

The Agent will claim four curated work kinds: `setup_cm_acquire`, `setup_cm_sql`, `setup_cm_mecm`, and `setup_cm_health`. Each item identifies a stage, a local LABZ1 configuration path, a hash-pinned Setup-CM module archive, and an evidence root. The Agent validates the stage and archive hash, expands the module into its own work directory, and invokes `pwsh.exe` with the module's existing `Invoke-SetupCm` command in unattended mode. It captures bounded stdout/stderr plus the evidence directory path and completes or fails the controller work item.

The controller will expose a typed endpoint that only creates those four work kinds for a registered Agent. It rejects arbitrary commands, arbitrary PowerShell, unknown stages, a missing archive hash, and paths outside the approved Setup-CM roots. Queue payloads contain no media, passwords, product keys, tokens, or generated credentials.

## Media and Secret Boundary

Media remains in the private LABZ1 SetupCm share and is copied by the Agent running as LocalSystem, using the CM01 machine account. The private LABZ1 config and hash-verification evidence remain on CM01 under `C:\ProgramData\SetupCm`; neither is committed. The configuration keeps `licenseAccepted: false` until the operator explicitly authorizes installation terms. Product keys, if Enterprise media requires one, remain in the private vault and are never passed through Git, API logs, evidence artifacts, or job output.

## Execution Flow

1. A controller operator queues a selected `setup_cm_*` stage for the CM01 Agent, with the archive URI/path and SHA-256 plus the fixed local config/evidence roots.
2. The Agent validates every path and stage, copies the archive into its private work directory, verifies its SHA-256, expands it, and verifies that the expected `SetupCm.psd1` module manifest exists.
3. The Agent runs PowerShell 7 in unattended mode for exactly the queued stage. The existing Setup-CM `Test -> Apply -> Verify` stage boundary remains authoritative.
4. The Agent sends structured completion/failure output to the controller and leaves the stage's JSON evidence on disk for diagnostics and collection.
5. The controller/UI can display job status. The existing Agent log collection capability can retrieve Setup-CM evidence afterward.

## Safety and Recovery

The agent process never evaluates supplied PowerShell. It starts only the repository's fixed entry point with an allowlisted stage. A retry starts a new process and relies on each Setup-CM stage's preflight/test checks, so an already-installed SQL instance or MECM role is not blindly reinstalled. Agent work has a long but finite timeout, reports exit code and bounded output, and preserves evidence paths on failure.

## Acceptance

- Contract tests prove the Agent advertises and executes only the four Setup-CM work kinds, uses PowerShell 7, rejects path traversal and hash mismatch, and reports evidence.
- API tests prove the controller accepts only a registered Agent and valid typed queue payloads, and emits no secret data.
- The upgraded Agent on LABZ1-CM01 heartbeats with the new capability, claims an Acquire work item, stages/verifies the two private media files, and uploads evidence.
- SQL/MECM installation remains gated on explicit license acceptance and any required private product key. After the gate opens, the Agent performs SQL, MECM, and Health stages end-to-end.
