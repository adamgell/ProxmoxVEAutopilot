# Service split + health UI — future work

**Date stacked:** 2026-04-21
**Status:** parked — not started. Resume after other higher-priority work.

## Motivation

The web container today runs:

- FastAPI / uvicorn (the UI + API itself)
- Sweep loop (monitoring every 15 min)
- Keytab refresher (daily asyncio task)
- Ansible subprocess spawns (every provision / template / capture job)
- Background threads (log streamers, update sidecar watchers)

A failing Ansible playbook that hangs can wedge the web container;
a container restart nukes the sweep ticker mid-flight; the keytab
refresher's 24h cadence can't survive crashes cleanly. Plus
there's no operator-visible "is each subsystem alive" signal.

## Three shapes considered

### Option A — Worker-only split (recommended first step)

- `autopilot-web` — UI + API + monitor sweep + keytab refresher
  (everything that's already asyncio stays put)
- `autopilot-ansible` — dedicated container running Ansible
  playbooks as jobs. Web enqueues, ansible-runner pulls + runs.

Split reason: Ansible is the riskiest / longest-running workload.
Pulling it out keeps the web responsive while a playbook runs, and
a hung playbook can be killed by restarting just the ansible
container.

### Option B — Full service split

- `autopilot-web`
- `autopilot-monitor` (sweep loop)
- `autopilot-keytab` (refresher)
- `autopilot-ansible` (jobs)

True isolation per concern. 4× container count, 4× Dockerfile
maintenance, 4× CI build cost. Probably not worth it for a home
lab.

### Option C — Sidecar pattern

Keep web as-is. Move the loops into sidecars that mount
`/app/output` and heartbeat to a shared sqlite table. Web reads
+ renders. Least invasive; medium payoff.

## Health UI (independent of split shape)

New `service_health` single-row-per-service table in
`device_monitor.db`:

```sql
CREATE TABLE service_health (
    service_name    TEXT PRIMARY KEY,   -- web / monitor-sweep / keytab-refresher / ansible-worker
    version_sha     TEXT,               -- running git sha
    started_at      TEXT,               -- when the process came up
    last_heartbeat  TEXT,               -- ISO UTC, refreshed ~10s
    status          TEXT,               -- ok / degraded / dead
    detail          TEXT                -- free-form
);
```

Each service writes its own heartbeat row every N seconds. The UI
reads in one query, turns rows yellow at `last_heartbeat > 2×interval`,
red at `> 5×interval`.

New `/services` page (or panel strip on `/monitoring`):

```
┌────────────────────┬──────────┬───────────┬────────┬───────┐
│ Service            │ Version  │ Heartbeat │ Uptime │ Status│
├────────────────────┼──────────┼───────────┼────────┼───────┤
│ web (self)         │ 0ced64d  │ now       │ 3h12m  │ ✅    │
│ monitor sweep      │ 0ced64d  │ 42s ago   │ 3h12m  │ ✅    │
│ keytab refresher   │ 0ced64d  │ 8h ago    │ 22h    │ ✅    │
│ ansible worker     │ —        │ —         │ —      │ ⚠️    │
└────────────────────┴──────────┴───────────┴────────┴───────┘
```

## Open questions when this resumes

- **Failure semantics** — if ansible worker is down, does the web
  UI accept new provision jobs and queue them, or reject with
  "worker offline"? Answer shapes whether we need a queue layer
  (sqlite row + polling, or actual redis / nats).
- **Shared state** — sequences.db + device_monitor.db both need RW
  from multiple containers. sqlite's WAL mode handles concurrent
  readers, but concurrent writers get serialised by the OS. Fine
  for our volumes (< 1 write/sec).
- **Where does `update.log` live** — currently `/app/output/update.log`
  mounted from the host. Same path across all containers if they
  share the volume, which is fine.
- **Self-update flow** — the existing sidecar pattern in
  `/api/update/run` only recreates the `autopilot` container. Extend
  it to `docker compose up -d` (no arg) so all service containers
  get pulled + restarted in one shot? Or give each service its own
  updater? Probably one compose-wide update.

## Starting point when resuming

Option A — pull Ansible out first. Smallest change, most visible
benefit. Add the `service_health` table + heartbeat writes in the
web container at the same time. After that lands, revisit whether
to pull sweep + keytab out too.
