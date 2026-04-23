# UTM Path Sweep — Hardcoded Docker Path Inventory

_Branch_: `feature/utm-macos-arm64-support`  
_Agent scope_: all files **except** `web/app.py`, `web/utm_cli.py`,
`web/templates/utm_vms.html`, `roles/utm_answer_iso/`,
`roles/utm_vm_snapshot/`, `docs/UTM_SNAPSHOTS.md`,
`docs/UTM_ANSWER_ISO.md`.

---

## Inventory Table

| file:line | current / old value | classification | disposition |
|---|---|---|---|
| `web/paths.py` (new) | — | new helper | **ADDED** — `REPO_ROOT`, `OUTPUT_DIR`, `JOBS_DIR`, `SECRETS_DIR` |
| `web/entrypoint.py:57-58` | `/app/output`, `/app/jobs` | config default | **FIXED** commit `5f782a8` — `REPO_ROOT/{output,jobs}` |
| `web/builder.py:38` | `Path("/app/output")` in `_worker_id` | config default | **FIXED** commit `5f782a8` — `_OUTPUT_DIR` |
| `web/builder.py:62` | `Path("/app/VERSION")` in `_version_sha` | script constant | **FIXED** commit `5f782a8` — `REPO_ROOT/VERSION` |
| `web/builder.py:162-164` | `/app/jobs`, `/app/output/jobs.db`, `/app/output/device_monitor.db` | config defaults | **FIXED** commit `5f782a8` — `_JOBS_DIR`, `_OUTPUT_DIR/…` |
| `web/monitor_main.py:44-46` | `/app/output/{monitor.lock,device_monitor.db,jobs.db}` | config defaults | **FIXED** commit `5f782a8` — `_OUTPUT_DIR/…` |
| `web/monitor_main.py:221` | `Path("/app/VERSION")` in `_version_sha` | script constant | **FIXED** commit `5f782a8` — `REPO_ROOT/VERSION` |
| `scripts/ad/refresh_keytab.py:44` | `/app/secrets/krb5.keytab` | config default | **FIXED** commit `8c6f556` — `parents[2]/secrets/krb5.keytab` |
| `scripts/ad/configure_entra_auth.py:155` | `sys.path.insert(0, "/app")` | script constant | **FIXED** commit `8c6f556` — `Path(__file__).parents[2]` |
| `roles/hash_capture/defaults/main.yml:5` | `/opt/autopilot-proxmox/hashes` | role var | **FIXED** earlier in commit `ff56515` — `{{ playbook_dir }}/../output/hashes` |
| `web/app.py:823` | `cfg.get("ad_keytab_path", "/app/secrets/krb5.keytab")` | config default | **DEFERRED** — `web/app.py` owned by snapshots agent |
| `web/app.py:1069` | `cfg.get("ad_keytab_path", "/app/secrets/krb5.keytab")` | config default | **DEFERRED** — same |
| `web/app.py:2134` | `"Save to /app/output/hashes/<serial>_hwid.csv"` | doc string in UI | **DEFERRED** — same (cosmetic; update to `output/hashes/`) |
| `web/app.py:4878` | `# /api/update/status can tail /app/output/update.log` | code comment | **DEFERRED** — same (cosmetic) |
| `tests/test_jobs_db.py:82-83` | `/app/playbooks/provision_clone.yml` | test fixture | **SKIPPED** — intentionally mocks Docker layout |
| `tests/integration/test_live.py:374` | `/opt/ProxmoxVEAutopilot/…:/app/output` | test fixture | **SKIPPED** — intentionally mocks Docker volume mount |
| `Dockerfile:27,38,43-44,47` | `/app/…` (WORKDIR, ENV, RUN paths) | container internal | **KEPT** — legitimately lives at `/app` inside the image |
| `docker-compose.yml:11-15,50-54` | `./…:/app/…` volume mounts | container internal | **KEPT** — correct Docker volume syntax |
| `scripts/test_container.sh:33-40` | `/app/{ansible.cfg,playbooks,roles,…}` | container smoke-test | **KEPT** — explicitly tests Docker filesystem layout |

**Summary**: found **18** hardcoded path instances, fixed **9**, deferred **4** to `web/app.py` owner, skipped **5** (test fixtures / container-internal paths that are correct as-is).

---

## Environment Variables Introduced

| variable | default (Docker) | default (native macOS) | where read |
|---|---|---|---|
| `AUTOPILOT_OUTPUT_DIR` | `/app/output` | `<checkout>/autopilot-proxmox/output` | `web/paths.py`, `web/entrypoint.py` |
| `AUTOPILOT_JOBS_DIR` | `/app/jobs` | `<checkout>/autopilot-proxmox/jobs` | `web/paths.py`, `web/entrypoint.py` |
| `AUTOPILOT_SECRETS_DIR` | `/app/secrets` | `<checkout>/autopilot-proxmox/secrets` | `web/paths.py` (also `KEYTAB_PATH` below) |
| `KEYTAB_PATH` | `/app/secrets/krb5.keytab` | `<checkout>/autopilot-proxmox/secrets/krb5.keytab` | `scripts/ad/refresh_keytab.py` |

> **Note**: `AUTOPILOT_OUTPUT_DIR` and `AUTOPILOT_JOBS_DIR` are already
> exported by `scripts/tui.py` and `scripts/run_macos_native.sh` for
> native launches, so no operator action is needed for the TUI path.
> `AUTOPILOT_SECRETS_DIR` is new; the Dockerfile does not set it (the
> volume mount takes care of `/app/secrets` in Docker).

---

## Follow-ups for Excluded Files

### `web/app.py` (owned by snapshots agent)

Two places pass `"/app/secrets/krb5.keytab"` as a fallback to
`cfg.get("ad_keytab_path", …)`:

```python
# line 823 and line 1069
keytab_path = cfg.get("ad_keytab_path", "/app/secrets/krb5.keytab")
```

**Suggested fix** (do not apply here):
```python
from web.paths import SECRETS_DIR as _SECRETS_DIR
keytab_path = cfg.get(
    "ad_keytab_path",
    str(_SECRETS_DIR / "krb5.keytab"),
)
```

Two cosmetic strings (lines 2134, 4878) reference `/app/output/hashes`
and `/app/output/update.log` in UI copy / comments; update to
`output/hashes` and `output/update.log` respectively.

---

## Design Notes

### Path resolution strategy

`web/paths.py` is the single source of truth:

```
REPO_ROOT = Path(__file__).resolve().parent.parent
           # → /app            (Docker, __file__ = /app/web/paths.py)
           # → …/autopilot-proxmox  (macOS native)
```

All derived dirs (`OUTPUT_DIR`, `JOBS_DIR`, `SECRETS_DIR`) honour their
respective `AUTOPILOT_*` env vars, so operators can override them
without touching source.  Inside Docker these vars are not set, so the
defaults resolve to `/app/{output,jobs,secrets}` — exactly the paths
mounted as volumes in `docker-compose.yml`.

### Ansible roles

Roles use `{{ playbook_dir }}/../output/…` (Jinja2 path arithmetic) to
stay repo-relative without depending on any Python helper.  The
`autopilot_workdir` Ansible variable is not yet formalised across all
roles; the next sweep should define it in
`inventory/group_vars/all/vars.yml` as:

```yaml
autopilot_workdir: "{{ playbook_dir }}/.."
```

and update role `defaults/main.yml` files to use `{{ autopilot_workdir
}}/output/…`.
