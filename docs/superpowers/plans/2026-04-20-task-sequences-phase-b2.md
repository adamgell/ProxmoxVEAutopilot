# Task Sequences — Phase B.2 Implementation Plan

**Date:** 2026-04-20
**Status:** Draft, pending user review
**Depends on:** [Phase B.1](2026-04-19-task-sequences-phase-b1.md), [design spec](../specs/2026-04-19-task-sequences-design.md)

## Goal

Make sequence 2 "AD Domain Join — Local Admin" and any other non-Autopilot sequence actually provision a working VM end-to-end:

- Local admin account created during OOBE with credentials from the step's `local_admin` credential.
- Computer joined to AD during the specialize pass using the step's `domain_join` credential + optional OU.
- Computer renamed (by default to serial number), rebooting cleanly.

Plus the plumbing needed to make that safe and fast:

- Per-job compiled `unattend.xml` + `SetupComplete.cmd` delivered via a hash-addressed answer ISO on Proxmox. Same inputs → same ISO → build & upload only once.
- Reboot-aware waiter that follows the guest through the rename-triggered restart.
- Byte-identical no-regression for sequence 1 "Entra Join (default)".

Hybrid Autopilot stays stubbed per design §11. `autopilot_hybrid` step raises `StepNotImplemented` at compile time with the existing error; no execution path lands in this phase.

## Out of scope

- `run_script` / `install_module` step types (design §5 row entries, not needed for sequence 2).
- "Test connection" button for domain_join credentials (design §8.6 — can ship later).
- Per-step `causes_reboot` user override on `run_script`.
- Autopilot Hybrid execution, ODJ-blob consumption.
- Sequence import/export.

## Architecture deltas vs today

### 1. Compiled artifacts land on disk per-job

A new directory `/app/jobs/<job_id>/sequence/` holds, for each provision:

```
unattend.xml          # compiled from unattend_oobe.xml.j2 + step fragments
SetupComplete.cmd     # concatenated post-OOBE lines (rename, etc.)
AutopilotConfigurationFile.json   # only if autopilot_entra step present
vars.json             # non-secret step params, echoed for ansible
```

Secret-bearing artifacts (`unattend.xml`, `SetupComplete.cmd`) are `chmod 0600` while the job runs. On job completion they're unlinked.

### 2. Answer-ISO cache addressed by payload hash

New module `web/answer_iso_cache.py`:

- `compute_hash(artifacts_dir) -> str` — canonical order (`unattend.xml`, `SetupComplete.cmd`, `AutopilotConfigurationFile.json` if present), SHA-256, take first 16 hex chars.
- `ensure_iso(artifacts_dir) -> "isos:iso/autopilot-unattend-<hash>.iso"` — idempotent. Checks the ISO storage via the existing `_proxmox_api` for an existing volid; only builds+uploads on miss.
- `list_unused() -> list[volid]` — returns every `autopilot-unattend-*.iso` not referenced by any row in a new `answer_iso_cache` table plus any currently-running VM's `ide2:` config.
- `prune(volids: list[str]) -> list[str]` — deletes selected volumes.

New SQLite table `answer_iso_cache(hash TEXT PK, volid TEXT, compiled_at, last_used_at)`.

Existing ISO builder (`scripts/create_answer_iso.sh`) is reused unchanged — it already accepts an input directory and a target ISO path.

### 3. Ansible consumes the compiled ISO

`playbooks/provision_clone.yml` accepts a new required-when-sequence-present variable `_sequence_artifacts_dir`. `proxmox_vm_clone/tasks/update_config.yml` points `ide2` at the per-job hash-named ISO instead of the static `proxmox_answer_iso` — but **only when the sequence required compilation**. The Entra-default sequence compiles to the same bytes as today, so its hash is stable, its ISO is built exactly once across the cluster's lifetime, and the static `autounattend.iso` path continues to work for users that skip the B.2 flow entirely.

### 4. Compiler handler fan-out

`web/sequence_compiler.py` grows three handlers plus a "compile to three buckets" output shape:

```python
@dataclass
class CompiledSequence:
    ansible_vars: dict       # -e key=value tokens (today's shape)
    unattend_fragments: dict # {pass_name: [xml_snippet, ...]}
    setup_complete_lines: list[str]
    autopilot_enabled: bool
    causes_reboot_count: int  # how many reboot-waiter passes to insert
```

Handlers:

- `_handle_local_admin(params, creds)` — resolves `credential_id` → `{username, password}` → emits unattend `<UserAccounts>/<LocalAccounts>/<LocalAccount>` fragment in `oobeSystem` pass, plus `<AutoLogon>` when `params.autologon: true`.
- `_handle_join_ad_domain(params, creds)` — resolves `credential_id` → `{domain_fqdn, username, password}` → emits unattend `<Identification><JoinDomain>`, `<MachineObjectOU>` (if step's `ou_path`), and `<Credentials>` block in `specialize` pass. `causes_reboot=false` (joins during the reboot Windows already does from specialize → OOBE).
- `_handle_rename_computer(params, _creds)` — emits a line into `SetupComplete.cmd`:
  ```
  powershell -NoProfile -Command "Rename-Computer -NewName '$(wmic csproduct get uuid | findstr -v UUID | %%i in (%%i) do @echo %%i)' -Restart -Force"
  ```
  Actually: the name source is `params.name_source in ('serial','pattern')`. Serial pulls from SMBIOS `SerialNumber`. Pattern is a format string like `"DEV-{n}"` where `{n}` is the VMID. `causes_reboot=true`.

### 5. Credential decryption boundary

Compile path loads the cipher once per job, passes a minimal resolver function into each handler:

```python
def compile(sequence, *, resolve_credential=None) -> CompiledSequence:
    ...
    handler(step_params, out, resolver=resolve_credential)
```

`resolve_credential(credential_id: int) -> dict` returns the decrypted payload. Handlers that don't need credentials (`set_oem_hardware`, `rename_computer`, `autopilot_entra`) never see the resolver argument. Plaintext is therefore only in memory inside the compile call and inside the resulting artifact file strings — never on `CompiledSequence` attributes or logs.

### 6. Unattend template

`files/unattend_oobe.xml` → `files/unattend_oobe.xml.j2`. Jinja blocks match the design spec §9:

- `{% block specialize_extra %}{% endblock %}` for `join_ad_domain` fragment.
- `{% block oobe_user_accounts %}…{% endblock %}` with today's hardcoded Administrator as the fallback when no `local_admin` step is present.
- `{% block autologon %}{% endblock %}`.

Compiled rendering happens in Python (`jinja2.Template(src).render(fragments=...)`), not in Ansible, so the artifact that lands on disk is the final XML and tests can assert on it directly.

### 7. Reboot-aware waiter

New `roles/common/tasks/wait_reboot_cycle.yml` per design §10:

```yaml
- name: Read pre-reboot boot-time
  ...guest-exec get-time → set_fact _pre_boot_epoch

- name: Poll until boot-time jumps or agent disappears
  ...retry loop

- name: Post-reboot guest-agent wait
  include_tasks: wait_guest_agent.yml
```

Invoked once per `causes_reboot_count` at the right spot in `_provision_clone_vm.yml` (after the Start-VM task). For Phase B.2, that's zero or one times (only `rename_computer` triggers it).

## File-by-file change list

**New:**
- `autopilot-proxmox/web/answer_iso_cache.py` — hash + cache + list/prune API
- `autopilot-proxmox/web/answer_iso_builder.py` — small wrapper around `create_answer_iso.sh` that accepts an artifacts dir and a target path
- `autopilot-proxmox/roles/common/tasks/wait_reboot_cycle.yml`
- `autopilot-proxmox/files/unattend_oobe.xml.j2` — Jinja version of today's unattend with named blocks
- `autopilot-proxmox/tests/test_answer_iso_cache.py`
- `autopilot-proxmox/tests/test_sequence_compiler_b2.py`
- `autopilot-proxmox/tests/test_unattend_regression.py` — byte-identical assertion for the default seed
- `docs/superpowers/plans/2026-04-20-task-sequences-phase-b2.md` (this file)

**Modified:**
- `autopilot-proxmox/web/sequence_compiler.py` — add 3 handlers, new `CompiledSequence` shape, credential resolver hookup
- `autopilot-proxmox/web/sequences_db.py` — new `answer_iso_cache` table + helpers
- `autopilot-proxmox/web/app.py` — on provision: compile sequence to artifacts dir → `answer_iso_cache.ensure_iso` → pass `_sequence_artifacts_dir` and the resolved ISO volid to Ansible. Add `/api/answer-isos` (list + prune) and a small UI link.
- `autopilot-proxmox/playbooks/provision_clone.yml` + `_provision_clone_vm.yml` — accept `_sequence_artifacts_dir`, `_answer_iso_volid`; pass through
- `autopilot-proxmox/roles/proxmox_vm_clone/tasks/update_config.yml` — use `_answer_iso_volid` for `ide2` when set, else existing static path
- `autopilot-proxmox/roles/proxmox_vm_clone/tasks/main.yml` — invoke `wait_reboot_cycle.yml` once per `_causes_reboot_count`
- `autopilot-proxmox/files/unattend_oobe.xml` — kept for backward-compat path; deleted once all tests assert equivalent output from the template
- `docs/SETUP.md` — section 7 "Using AD domain join" walkthrough

## Ship sequence

Each bullet is a commit that passes tests and docker builds:

1. **Compiler shape expansion + credential resolver** — refactor `CompiledSequence` to the 4-bucket form, keep existing handlers unchanged, plumb `resolve_credential`. Old `ansible_vars` behavior preserved. New tests for the shape.
2. **Unattend Jinja template + byte-identical regression test** — ships `unattend_oobe.xml.j2`, keeps `unattend_oobe.xml` as the comparison target.
3. **`local_admin` handler** — emits `<UserAccounts>` / `<AutoLogon>` fragments; credential decryption path wired.
4. **`rename_computer` handler** + SetupComplete.cmd assembly + reboot waiter task.
5. **`join_ad_domain` handler** — specialize-pass `<Identification>` fragment.
6. **Answer-ISO cache** — `answer_iso_cache.py` + table + builder wrapper. Still unused by the provision path at this step; tested in isolation.
7. **Provision-path wiring** — compile on submit, ensure_iso, pass artifacts dir + volid to Ansible. Ansible role points `ide2` at the per-job volid when present. Default-sequence VMs get the stable-hash ISO, non-Autopilot sequences get their own.
8. **Cache listing + prune UI** — `/answer-isos` page + `/api/answer-isos` endpoints.
9. **Docs** — SETUP.md §7 walkthrough: create a `domain_join` credential, pick sequence 2, provision, watch a VM join.

Each step ends with a passing `pytest` and a successful `docker compose up -d` with the seeded "Entra Join" sequence still producing a working Autopilot provision (regression safety net).

## Risk + mitigation

| Risk | Mitigation |
|---|---|
| Unattend XML drifts from today's byte output and no one notices until the next Entra-default run fails. | Byte-identical test runs on every commit. The test renders the seeded default sequence through the compiler and diffs against `unattend_oobe.xml`. |
| Plaintext credentials leak into job logs via Ansible verbosity. | `no_log: true` on every task that references `_sequence_artifacts_dir`. Compiled artifacts are `0600`. Server-side redaction already exists for Password= / base64 blobs per design §7. |
| Hash collisions silently share an ISO across sequences that shouldn't share one. | SHA-256 truncated to 16 hex chars = 64 bits → birthday bound ≈ 2³² entries. For realistic fleets that's zero. But: `answer_iso_cache` row stores the full 64-hex digest too, and `ensure_iso` compares the full digest before reuse — the short form is purely for the filename. |
| The per-job artifacts dir survives a crashed job and grows unbounded. | Startup sweep: on app boot, delete any `/app/jobs/*/sequence/` older than 24h regardless of job state. |
| `Rename-Computer -Restart` races with `SetupComplete.cmd` returning → Windows considers setup complete and re-enters OOBE. | `SetupComplete.cmd` exits **after** `Rename-Computer`; Windows SetupComplete is synchronous. `-Restart` schedules the reboot inside Windows as a clean shutdown following script exit. Tested on the existing template. |

## Acceptance

- `pytest` is green.
- Sequence 2 provisions a VM that: boots, creates the local admin, joins `home.gell.com`, renames to its SMBIOS serial, reboots, returns healthy via the guest agent.
- Sequence 1 ("Entra Join (default)") continues to produce a working Autopilot VM with output byte-identical to the current flow.
- `autopilot_hybrid` step (sequence 3) still returns `StepNotImplemented` at submit time — no regression, no new functionality.
- `/answer-isos` page lists the cache with compiled_at and reference status; unused rows can be pruned with a button and vanish from the ISO storage.
