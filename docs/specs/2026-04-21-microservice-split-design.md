# Microservice split — web / builder / monitor

**Date:** 2026-04-21
**Status:** approved; ready for implementation planning
**Supersedes:** `docs/specs/2026-04-21-service-split-design.md` (parked sketch; this doc replaces it with concrete decisions)

## Motivation

Two operator-facing drivers:

1. **Resilience** — a wedged Ansible playbook today can freeze the web process because the subprocess is a child of the uvicorn worker. If Ansible hangs, `/monitoring`, `/jobs`, and login all stop responding. We want the UI + login + health view to stay live even when a playbook is stuck.
2. **Deployability** — a clean separation makes it easier to reason about what's running, scale provision throughput with `docker compose --scale`, and (eventually) move individual services to larger hosts without a rewrite.

Today's single container runs: FastAPI/uvicorn, the 15-min monitor sweep, the daily keytab refresher, Ansible subprocess spawns for every job, and a collection of background threads. Any one of those failing can poison the rest.

## Architecture

Three services, all built from **one image**, differing only by `command:` override in compose:

| Service | Replicas | Responsibility |
|---|---|---|
| `autopilot` (web) | 1 | UI, API, session/OIDC login, job enqueue. Tails logs for streaming. No background workloads. |
| `autopilot-builder` | N, default 1 | Claims Ansible jobs from the queue, runs `ansible-playbook`, writes log + status. Scale with `docker compose up -d --scale autopilot-builder=N`. |
| `autopilot-monitor` | 1 (hard singleton) | Sweep loop (AD/Entra/Intune probes), keytab refresher, orphan-job reaper. Only process talking to Microsoft Graph + AD LDAP. |

Keeping the web container named `autopilot` preserves operator muscle memory (`docker exec autopilot ...`, compose log pipelines, etc.). The two new services are additive names.

**Web never probes external APIs after this split.** It renders from `device_monitor.db` only. `/vms` and `/monitoring` become pure DB reads — no more "sometimes slow when AD is slow" behaviour.

## Job queue

A new `jobs` table in `output/jobs.db` (separate from `device_monitor.db` so monitor/web contention on the sweep DB is unaffected by queue churn):

```sql
CREATE TABLE jobs (
    id            TEXT PRIMARY KEY,        -- existing YYYYMMDD-XXXX format
    job_type      TEXT NOT NULL,           -- build_template / provision_clone / capture / ...
    playbook      TEXT NOT NULL,           -- path to the playbook
    cmd_json      TEXT NOT NULL,           -- JSON array of full argv (preserves -e vars)
    args_json     TEXT NOT NULL,           -- existing args dict (for UI display)
    status        TEXT NOT NULL,           -- pending / running / complete / failed / orphaned
    worker_id     TEXT,                    -- uuid of the claiming builder; NULL while pending
    kill_requested INTEGER NOT NULL DEFAULT 0,
    exit_code     INTEGER,
    created_at    TEXT NOT NULL,
    claimed_at    TEXT,
    last_heartbeat TEXT,                   -- builder touches this every 5s while running
    ended_at      TEXT
);
CREATE INDEX jobs_by_status ON jobs(status, created_at);
```

**Claim protocol (atomic).** Two-step transaction inside a single SQLite write lock:

```python
def claim_next_job(worker_id: str) -> dict | None:
    now = iso_utc_now()
    with db.transaction():                         # BEGIN IMMEDIATE — serialised
        row = db.execute("""
            SELECT j.id, j.job_type
            FROM jobs j
            JOIN job_type_limits l USING (job_type)
            WHERE j.status = 'pending'
              AND (SELECT COUNT(*) FROM jobs
                   WHERE job_type = j.job_type AND status = 'running') < l.max_concurrent
            ORDER BY j.created_at ASC
            LIMIT 1
        """).fetchone()
        if row is None:
            return None
        n = db.execute(
            "UPDATE jobs SET status='running', worker_id=?, "
            "claimed_at=?, last_heartbeat=? "
            "WHERE id=? AND status='pending'",
            (worker_id, now, now, row["id"]),
        ).rowcount
        if n != 1:
            return None    # someone else claimed between SELECT and UPDATE; try again next poll
        return db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
```

SQLite serialises writes inside `BEGIN IMMEDIATE`, so the whole transaction is atomic. The `status='pending'` guard on the UPDATE is belt-and-braces in case another writer slipped in; the `rowcount != 1` path treats that as "try again" rather than double-claim.

**Per-type concurrency caps** in `job_type_limits`:

| job_type | max_concurrent (default) |
|---|---|
| build_template | 1 |
| provision_clone | 3 |
| capture_hash | 5 |
| hash_upload | 5 |
| retry_inject_hash | 3 |
| any unknown type | 1 |

Operator-tunable at `/settings → Job concurrency`. Capping template builds at 1 is critical — two parallel builds on the same Proxmox cluster contend for the same ISO storage and VMIDs.

**Worker loop:**

```
while True:
    if shutdown_requested: exit 0
    row = claim_next_job(worker_id)
    if row is None:
        sleep(2); continue       # idle-poll cadence
    proc = run_playbook(row)     # subprocess, log to /app/jobs/<id>.log
    while proc.running():
        jobs.touch_heartbeat(row.id)
        if jobs.get(row.id).kill_requested: proc.terminate()
        sleep(5)                 # job-heartbeat cadence
    finalize(row, exit_code)
```

Two distinct heartbeat writes live in the builder: the **job heartbeat** above (`jobs.last_heartbeat`, every 5s, drives the orphan reaper and the kill signal), and the **service heartbeat** (`service_health.last_heartbeat`, every 10s, drives the health UI). Different rates because they serve different SLOs — job heartbeats need to be fast enough for emergency-stop; service heartbeats just need to look "live" to an operator watching the /monitoring strip.

**Kill path** — `/api/jobs/{id}/kill` flips `kill_requested=1` on the row. The builder that owns the job is already inside its 5-second heartbeat loop (the 2-second claim poll only applies to idle builders looking for work), so worst-case latency is one heartbeat cycle: **~5 seconds**. Acceptable for the emergency-stop use case.

## Monitor singleton

Hard enforcement via SQLite advisory lock file:

```python
# monitor/entrypoint.py
lock_fd = os.open("/app/output/monitor.lock", os.O_CREAT | os.O_RDWR, 0o644)
try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    log.warning("monitor already running elsewhere — exiting cleanly")
    sys.exit(0)
# lock held for life of process; kernel releases on exit
```

Second instance exits 0 (not a failure — compose will back off cleanly instead of restart-looping). Operators who accidentally `--scale autopilot-monitor=2` get the expected singleton behaviour; the second container just sits in Exited (0) and the logs explain why.

## Secrets and config

**Decision: each container mounts `vault.yml` + `vars.yml` + `secrets/credential_key` directly** (Option A from the brainstorm). All three containers live on the same host, the key file is already `0600`, and the alternative (web decrypts + stuffs plaintext into job rows) widens the attack surface on the DB for a marginal benefit.

Compose mounts, identical across all three services:

```yaml
volumes:
  - ./inventory/group_vars/all/vault.yml:/app/inventory/group_vars/all/vault.yml
  - ./inventory/group_vars/all/vars.yml:/app/inventory/group_vars/all/vars.yml
  - ./secrets:/app/secrets
  - ./output:/app/output
  - autopilot-jobs:/app/jobs
```

**Config reload:** each container polls `vars.yml` mtime every 60s. On change, re-reads and emits a log line. No hot-config protocol — operator sees the change reflected within a minute. Matches today's behaviour for most settings.

## Log streaming

Today: web reads the subprocess's stdout via pipe because the job is its child.

After the split: builder writes to `/app/jobs/<id>.log` on the shared named volume `autopilot-jobs`. Web tails via file reads (existing `job_manager.get_log` + WebSocket stream already work against file content — only the write path moves to the builder).

The log format, filenames, and retention stay identical to today.

## Self-update

Today's `/api/update/run` recreates just the `autopilot` container via a sidecar that does `git pull` + `docker compose up -d autopilot`. After the split, the sidecar runs `docker compose pull && docker compose up -d` (no service arg) so all three roll together atomically.

One-line change in `scripts/update_sidecar.sh`; tracked as part of this PR so behaviour matches the new topology on first deploy.

## Health (service_health table)

Schema in `device_monitor.db`:

```sql
CREATE TABLE service_health (
    service_id      TEXT PRIMARY KEY,      -- 'web', 'monitor', or 'builder-<uuid>'
    service_type    TEXT NOT NULL,         -- 'web' / 'builder' / 'monitor'
    version_sha     TEXT NOT NULL,         -- running git sha
    started_at      TEXT NOT NULL,
    last_heartbeat  TEXT NOT NULL,         -- ISO UTC, refreshed every 10s
    detail          TEXT                   -- free-form (e.g. "claimed job 20260421-a3f2")
);
```

Each service writes its own row every 10s. Stale rules, computed at render time:
- `ok` — heartbeat within 20s (2× interval)
- `degraded` — heartbeat 20–50s old (2–5× interval)
- `dead` — heartbeat >50s old (5× interval)

**UI:** a new strip at the top of `/monitoring` (not a separate nav entry — reduces clutter, health is observed in the same place as device state):

```
┌────────────────────────┬──────────┬───────────┬────────┬──────────────────────────┐
│ Service                │ Version  │ Heartbeat │ Uptime │ Detail                   │
├────────────────────────┼──────────┼───────────┼────────┼──────────────────────────┤
│ web                    │ caacd40  │ 3s ago    │ 2h14m  │ idle                     │
│ monitor                │ caacd40  │ 8s ago    │ 2h14m  │ sweep running            │
│ builder-7f2a1c         │ caacd40  │ 4s ago    │ 2h13m  │ job 20260421-a3f2        │
│ builder-9d8b43         │ caacd40  │ 2s ago    │ 2h13m  │ idle                     │
└────────────────────────┴──────────┴───────────┴────────┴──────────────────────────┘
```

Builder rows appear/disappear with `--scale`. The detail column surfaces the current job ID for active builders — clickable link to `/jobs/<id>`.

## Orphan job reaper

Runs in monitor as a 30s ticker:

```python
def reap_orphans():
    stale_cutoff = now - timedelta(minutes=2)
    db.execute(
        "UPDATE jobs SET status='orphaned', ended_at=? "
        "WHERE status='running' AND last_heartbeat < ?",
        (now, stale_cutoff)
    )
```

A builder that crashes mid-playbook leaves its job row in `status=running`. After 2 minutes without a heartbeat the reaper marks it `orphaned`. Operators see the status in `/jobs` and decide whether to re-queue. No automatic retry — orphaned jobs usually failed for a reason (Ansible hang, host OOM, etc.) and silent retry would mask root causes.

## Schema ownership and startup ordering

`depends_on` in compose:

```yaml
autopilot-builder:
  depends_on:
    autopilot:
      condition: service_healthy
autopilot-monitor:
  depends_on:
    autopilot:
      condition: service_healthy
```

Web owns schema init. On startup, web runs `sequences_db.init()` + `seed_defaults()` as today, plus the new `jobs.init()` + `service_health.init()`. Once schema is ready, web writes a row to a `schema_meta` table with `schema_ready_at`. Web's healthcheck endpoint `/healthz` returns 200 only after that row exists.

Builder/monitor startup waits on web's healthcheck (compose `condition: service_healthy`) before running their own claim/sweep loops. No race on `CREATE TABLE IF NOT EXISTS`.

## Data model changes

New tables (all in existing SQLite files, zero new DB files except `jobs.db`):

1. `output/jobs.db` — new file. Contains `jobs` and `job_type_limits`.
2. `output/device_monitor.db` — add `service_health` + `schema_meta` tables.

Existing tables untouched. Existing `jobs/index.json` (JobManager's current storage) is migrated on first startup: web reads `index.json`, inserts rows into `jobs.db` with `status` preserved, then renames `index.json` → `index.json.pre-split.bak` so an operator can recover it if the migration goes wrong.

## Compose + Dockerfile changes

**Dockerfile** — no structural change. Same base image, same installed packages. Add one new ENTRYPOINT arg parser so `command:` can select web/builder/monitor mode. Specifically: a thin `/app/entrypoint.py` that dispatches on `sys.argv[1]` ∈ `{web, builder, monitor}` and launches the right process. Default (no arg) stays `web` for backwards compatibility with anyone running the old image.

**docker-compose.yml:**

```yaml
services:
  autopilot:
    image: ghcr.io/adamgell/proxmox-autopilot:latest
    container_name: autopilot
    command: ["web"]
    network_mode: host
    volumes: &common_mounts
      - ./inventory/group_vars/all/vault.yml:/app/inventory/group_vars/all/vault.yml
      - ./inventory/group_vars/all/vars.yml:/app/inventory/group_vars/all/vars.yml
      - ./secrets:/app/secrets
      - ./output:/app/output
      - autopilot-jobs:/app/jobs
      - /var/run/docker.sock:/var/run/docker.sock
      - /opt/ProxmoxVEAutopilot:/host/repo
    environment: &common_env
      OPENSSL_CONF: /etc/ssl/openssl-legacy.cnf
      HOST_REPO_PATH: /opt/ProxmoxVEAutopilot
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8080/healthz"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 20s
    restart: unless-stopped

  autopilot-builder:
    image: ghcr.io/adamgell/proxmox-autopilot:latest
    command: ["builder"]
    network_mode: host
    volumes: *common_mounts
    environment: *common_env
    depends_on:
      autopilot: { condition: service_healthy }
    restart: unless-stopped

  autopilot-monitor:
    image: ghcr.io/adamgell/proxmox-autopilot:latest
    container_name: autopilot-monitor
    command: ["monitor"]
    network_mode: host
    volumes: *common_mounts
    environment: *common_env
    depends_on:
      autopilot: { condition: service_healthy }
    restart: unless-stopped

volumes:
  autopilot-jobs:
```

`autopilot-builder` intentionally has no `container_name:` so `--scale` works (Docker auto-generates `autopilot-autopilot-builder-1`, `-2`, etc.).

## Migration from single-container

Upgrade path for an existing deploy:

1. Operator pulls the new image (via the in-UI Update button).
2. New image starts as `web` only (existing container's `command:` is unset → defaults to `web`). Service stays online during this brief window.
3. Operator edits `docker-compose.yml` to add the two new services (a `docker-compose.split.yml` shipped in the repo can be copy-pasted).
4. `docker compose up -d` brings up builder + monitor.
5. On first boot, web migrates `jobs/index.json` → `jobs.db`.

**In-flight jobs at migration time:** web process terminates → its subprocess (Ansible) dies → that job fails with exit code -1 (already how crashes are handled today by `_cleanup_orphans`). Document in release notes: finish any running jobs before doing the split upgrade.

## Open items flagged during brainstorming (all decided)

| # | Decision |
|---|---|
| Secret delivery | (a) — each container mounts vault directly. Tight-enough boundary at single-host scale. |
| Kill latency | ~5s (one heartbeat cycle on the claimed builder). Acceptable for emergency stop. |
| Log streaming | Builder writes to shared volume; web reads from disk. Existing WebSocket code works unchanged. |
| Self-update scope | `docker compose up -d` whole stack. |
| Orphan cleanup | Monitor reaps jobs with heartbeat older than 2min; marks `orphaned`; no auto-retry. |
| Schema race | Web owns init; others wait on healthcheck. |
| Container names | Web stays `autopilot`; builder is unnamed (for `--scale`); monitor is `autopilot-monitor`. |
| Monitor singleton | Hard via flock on `/app/output/monitor.lock`. 2nd instance exits 0. |
| Worker role split | Builder runs Ansible; monitor runs sweeps + keytab + orphan reaper. No queue between them. |
| Multi-worker support | Atomic claim designed in from day 1; `--scale` knob on builder. |
| Monitor sweep vs builder writes | Both use WAL; contention negligible at our write volumes (<1/sec). Documented, not engineered around. |
| Config reload | Poll `vars.yml` mtime every 60s per container. |

## Testing approach

**Unit:**
- Claim protocol — single-row-affected, per-type cap enforcement, ordering (oldest pending first).
- Orphan reaper — stale-heartbeat threshold, state transitions.
- Monitor singleton — second instance exits 0 with expected log line.
- Schema migration — `index.json` → `jobs.db` round-trips preserves status, args, timestamps.

**Integration (live harness, gated on `--run-integration`):**
- Kill path end-to-end — enqueue long-running job, POST /kill, observe status flip + log tail shows termination within 10s.
- Scale — `docker compose up -d --scale autopilot-builder=3`, enqueue 5 concurrent `provision_clone` jobs (cap=3), observe 3 running + 2 pending, then first to finish triggers a pending to claim.
- Monitor singleton — manually start a second monitor container against the same volume, observe it exits 0.
- Web survives a wedged playbook — kill -STOP a builder's ansible process, confirm web and monitor continue heartbeating and `/monitoring` stays responsive.

## Scope cap (YAGNI)

Not in this spec; explicit non-goals:

- **Multi-host deploy.** Same-host SQLite queue covers today. Cross-host would need HTTP RPC + external queue; parkable.
- **Ansible-runner library.** We stay with `subprocess.Popen(ansible-playbook)` — simpler, debuggable, no new dependency. Swap in later if the builder gains complex job composition needs.
- **Kubernetes / Helm.** Compose-only target. K8s would need a ReadWriteMany volume for jobs + DB; out of scope.
- **Per-tenant queues / priorities.** One queue, one scheduler, FIFO within type. No priority lanes.
- **Encrypted `jobs.db`.** Vault + credentials DB already exist and are encrypted; `jobs.db` holds job metadata only (command lines with `-e` vars that may briefly contain plaintext, but we already redact in log streaming). File is `0600` and host-mounted; no change in trust boundary.
