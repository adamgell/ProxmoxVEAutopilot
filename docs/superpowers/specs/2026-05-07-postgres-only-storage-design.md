# Postgres-Only Storage Design

## Summary

ProxmoxVEAutopilot will use PostgreSQL as the only runtime database. SQLite-backed runtime state will be removed instead of bridged. The cutover is a clean reset: existing SQLite operational history, cached device state, queued jobs, and legacy provisioning runs do not need to be migrated.

Postgres is already present for Task Sequence Engine V2. This design promotes that database from a V2 subsystem dependency to the application database for jobs, monitoring, devices, service health, task sequences, provisioning runs, WinPE state, OSD state, and content manifests.

## Goals

- Remove split-brain state between SQLite and Postgres.
- Make Postgres mandatory at app startup.
- Put WinPE, OSD, task sequence, content, job, monitoring, and device state in one transactional database.
- Prefer V2-native task sequence and provisioning tables over compatibility shims.
- Keep generated files and logs as artifacts, not as the source of truth for workflow state.
- Reset production operational history during cutover instead of importing SQLite data.

## Non-Goals

- No SQLite-to-Postgres migration importer.
- No long-term dual-write path.
- No compatibility promise for old integer provisioning run IDs after V2 cutover.
- No redesign of the UI beyond what is required to read/write the new Postgres repositories.
- No change to external artifacts such as downloaded ISOs, generated answer media, WinPE ISOs, logs, or hash CSV exports unless needed to index them in Postgres.

## Current SQLite Surfaces

The following runtime modules are SQLite-backed today and will be replaced:

- `web/sequences_db.py`: legacy sequences, credentials, provisioning runs, run steps, WinPE state, OSD state.
- `web/jobs_db.py`: job queue, job type limits, heartbeats, kill requests, interrupted job reconciliation.
- `web/device_history_db.py`: monitoring sweeps, PVE snapshots, device probes, monitoring settings, search OUs.
- `web/devices_db.py`: cached device and enrollment records.
- `web/service_health.py`: service heartbeat state shown in monitoring.

SQLite-specific app constants and filesystem database paths will be removed after call sites move.

## Target Architecture

Create a single Postgres application database layer with focused repositories:

- `web/db_pg.py`: connection resolution, connection helpers, schema bootstrap, transaction helpers, test reset helpers.
- `web/jobs_pg.py`: job queue and worker coordination.
- `web/device_history_pg.py`: monitoring settings, sweeps, snapshots, probes, and search OUs.
- `web/devices_pg.py`: cached device and enrollment state.
- `web/service_health_pg.py`: service heartbeats.
- `web/ts_engine_pg.py`: V2 task sequences, versions, provisioning runs, run plan steps, events, logs, and content manifests.

`ts_engine_pg.py` may remain the owner for task-sequence-specific tables, but connection/bootstrap should move to the shared app database layer so Postgres is not framed as optional V2 infrastructure.

## Startup Contract

`AUTOPILOT_TS_ENGINE_DATABASE_URL` becomes the required application database URL. A later rename to `AUTOPILOT_DATABASE_URL` is acceptable, but the first implementation should keep the existing env var to avoid deployment churn.

On startup:

1. Resolve the Postgres DSN.
2. If no DSN is configured, fail startup with a clear error.
3. Initialize all application schemas idempotently.
4. Do not initialize SQLite database files.

Docker Compose already provides Postgres. Local tests may start ephemeral Postgres containers or use fixture-provided DSNs.

## State Model

Postgres will own:

- Task sequence definitions and compiled versions.
- Provisioning runs and all run state transitions.
- WinPE actions, OSD actions, phase transitions, reboots, and errors.
- Content items, content versions, and per-run content manifests.
- Jobs, worker claims, job logs, heartbeats, kill requests, and queue limits.
- Monitoring sweeps, snapshots, probes, settings, and search OUs.
- Device cache and enrollment evidence.
- Service health heartbeats.

Filesystem artifacts remain valid outputs:

- Job logs can continue to be written to disk while Postgres stores job status and log references.
- Hash CSVs can continue to be exported for operator download, but captured hash metadata should be indexed in Postgres.
- Generated media and downloaded assets remain file-backed artifacts.

## WinPE And OSD Direction

WinPE and full-OS OSD should move fully to V2 run IDs and V2 endpoints:

- Run IDs are UUID strings from Postgres.
- WinPE agents register and claim against V2 state.
- `stage_osd_client` uses a V2 package/config endpoint that returns `engine = "v2"`, `api_version = 2`, `run_id`, `phase`, `agent_id`, and bearer token.
- Full-OS `OsdClient.ps1` uses V2 mode and reports through `/osd/v2/agent/*`.
- Legacy `/winpe/*` and `/osd/client/*` SQLite-backed state paths are removed after V2 replacements exist.

The old integer run ID model should not be preserved for new deployments.

## Clean Reset Cutover

Production cutover does not import SQLite data.

Expected reset behavior:

- Queued/running jobs reset.
- Monitoring history resets.
- Cached device history resets and repopulates from future monitoring sweeps.
- Legacy provisioning run history resets.
- Existing generated artifacts stay on disk but are no longer authoritative unless re-indexed by future code.
- Default task sequences/settings may be re-seeded into Postgres as part of schema bootstrap.

Before deployment, operators should finish or cancel active jobs and treat currently running provisioning tasks as non-resumable across the cutover.

## Implementation Order

1. Add shared Postgres app DB bootstrap and make Postgres mandatory in tests and runtime.
2. Port the job queue to Postgres because web, builder, and monitor services depend on it.
3. Port service health to Postgres.
4. Port monitoring history/settings/search OUs to Postgres.
5. Port device cache/enrollment state to Postgres.
6. Move legacy sequence/provisioning callers to V2-native Postgres run state.
7. Add V2 WinPE setup package/config staging.
8. Remove SQLite modules, constants, fixtures, and file-backed database setup.
9. Run full local tests and deploy with clean Postgres state.

## Testing Strategy

- Unit tests for each Postgres repository using ephemeral Postgres.
- Endpoint tests should stop monkeypatching SQLite paths and instead reset Postgres schemas between tests.
- Builder and monitor tests should exercise real Postgres job claiming and heartbeat updates.
- WinPE endpoint tests should validate V2 UUID run IDs, V2 package config, and full-OS client handoff.
- Regression tests should assert no runtime imports of `sqlite3` remain under `web/`.
- Production deploy smoke should verify `/healthz`, `/api/version`, job queue startup, monitoring startup, and V2 OSD route registration.

## Risks And Controls

- Risk: large blast radius.
  Control: port one repository surface at a time and keep tests green between slices.

- Risk: hidden SQLite imports in tests or utility code.
  Control: add a final static test that rejects `sqlite3` imports under runtime modules.

- Risk: active production work is lost during reset.
  Control: make clean reset explicit in release notes and deploy only when no provisioning jobs need to resume.

- Risk: V2 run model does not cover every legacy sequence capability yet.
  Control: implement missing V2 capabilities directly instead of preserving SQLite compatibility paths.

## Acceptance Criteria

- App fails startup when Postgres DSN is missing.
- No runtime `web/` module imports `sqlite3`.
- No runtime code uses `*.db` files for application state.
- Web, builder, and monitor services share the same Postgres-backed job queue and heartbeats.
- Monitoring pages read from Postgres-backed repositories.
- Device cache/enrollment bubbles read from Postgres-backed repositories.
- WinPE and OSD run state use UUID Postgres runs.
- Full local test suite passes.
- Production deploy reports healthy services on the new Postgres-only runtime.
