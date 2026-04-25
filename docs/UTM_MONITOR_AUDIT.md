# UTM Monitor/Builder Audit — Phase 8

Audit of Proxmox-specific assumptions in `web/monitor_main.py`,
`web/builder.py`, and `web/device_monitor.py` as of Phase 8 (UTM dashboard).

---

## `web/monitor_main.py`

| Location | Assumption | Status |
|---|---|---|
| `_do_sweep_tick` | Calls `web.app._build_live_monitor_context()` which hits the Proxmox API (`/nodes/…`). Would raise immediately on UTM hosts. | **Fixed (Phase 8)**: short-circuits when `hypervisor_type == utm`. |
| `_do_keytab_tick` | Calls `web.app._run_keytab_checks()` — gMSA credential probe, not Proxmox-specific. | **No action needed** — keytab/gMSA is relevant on UTM too (same AD/Entra tenant). |
| `run_monitor` default args | `lock_path`, `monitor_db_path`, `jobs_db_path` default to `/app/output/…` (Docker paths). | **Deferred → `debt-docker-path-sweep`** |

---

## `web/builder.py`

| Location | Assumption | Status |
|---|---|---|
| `run_builder` / `_worker_id` default args | `/app/output/…` and `/app/jobs` Docker paths. | **Deferred → `debt-docker-path-sweep`** |
| `_version_sha` | Tries `/app/VERSION` first; falls back to repo root. | **No action** — already UTM-safe with the fallback. |
| Job dispatch | Playbook names differ (`build_proxmox_template.yml` vs `build_utm_template.yml`). Dispatch is hypervisor-aware in `app.py`. | **No action** — already correct. |
| Builder startup | No early-boot PVE API call. `service_health.init` + `heartbeat` are DB-only (no Proxmox). | **No action** — UTM-safe. |

---

## `web/device_monitor.py`

| Location | Assumption | Status |
|---|---|---|
| `MonitorContext` | Entire struct is Proxmox-centric (`list_pve_vms`, `fetch_pve_config`, etc.). | **No action this phase** — `sweep()` is never called on UTM (guarded in `_do_sweep_tick`). |
| `sweep()` | Enumerates Proxmox VMs, probes PVE config, writes `pve_snapshots` table. | **Not reached on UTM** — guarded upstream. |
| `probe_pve` / parsers | Pure Proxmox config parsers. | **Irrelevant on UTM** — called only inside `sweep()`. |

---

## Deferred to `debt-docker-path-sweep`

- `monitor_main.py`: `/app/output/monitor.lock`, `/app/output/device_monitor.db`, `/app/output/jobs.db`
- `builder.py`: `/app/output/jobs.db`, `/app/output/device_monitor.db`, `/app/jobs`
- `builder._worker_id`: `Path("/app/output")`
- `builder._version_sha`: `Path("/app/VERSION")`
