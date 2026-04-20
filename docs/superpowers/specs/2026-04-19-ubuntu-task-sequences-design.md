# Ubuntu Task Sequences — Design Spec

**Date:** 2026-04-19
**Status:** Draft, pending user review
**Owner:** Adam
**Follows:** [2026-04-19-task-sequences-design.md](2026-04-19-task-sequences-design.md)

## 1. Context

The task-sequences spec introduced sequences as a first-class abstraction over Windows OOBE flows: Entra Join, AD Domain Join, Hybrid (stub). Phase A is already in flight on `feat/task-sequences-spec` — schema, credentials, CRUD UI, seeds. This spec extends sequences to a second OS family, **Ubuntu**, by reusing everything that was purposely built to be OOBE-agnostic (sequences builder, credentials store, job runner, DB) and adding only the Linux-specific bits: a new step library, a YAML compiler, and an Ubuntu-shaped template-build path.

The concrete trigger is [LinuxESP](https://github.com/ugurkocde/LinuxESP) — an Ubuntu autoinstall + Intune Portal + Edge deployment that's structurally identical to what this tool does for Windows. LinuxESP lands here as one seeded sequence; the underlying step library lets users compose other Ubuntu outcomes (dev workstation without MDE, kiosk, plain Ubuntu, etc.).

## 2. Goals

- Add Ubuntu 24.04 (Server and Desktop) provisioning via autoinstall + cloud-init, reusing the task-sequences architecture.
- Seed a "Ubuntu Intune + MDE (LinuxESP)" sequence that reproduces LinuxESP's behavior.
- Make `target_os` a first-class field on sequences; step types declare OS compatibility.
- Keep hardware identity (`oem_profiles.yml`) orthogonal — SMBIOS still applies to Ubuntu VMs even though Linux doesn't use it for Autopilot.
- Preserve zero-regression compatibility for existing Windows sequences (`target_os` backfills to `windows`).

## 3. Non-goals

- RHEL / Kickstart, Fedora, openSUSE, Debian (non-Ubuntu).
- Cloud images / qcow2 path — the autoinstall approach wins on LinuxESP parity.
- Ubuntu Pro attach, desktop-variant auto-detection, WSL.
- LTS-upgrade automation, post-provision config drift management.
- Intune *enrollment* automation — Intune Linux requires an interactive user sign-in; the agent is installed but enrollment completes at first login.

## 4. Core concept

Sequences gain a `target_os` dimension. The step library splits into OS-scoped families: Windows step types remain as today, Ubuntu step types are new. A sequence's `target_os` determines which step types the builder offers and which playbook path the job runner takes.

The provisioning pipeline diverges at two points:
- **Template build** — Ubuntu uses an autoinstall seed ISO (NoCloud, label `cidata`) instead of the Windows answer ISO (OEMDRV). After autoinstall finishes, a guest-exec call runs `cloud-init clean -l` as the sysprep analogue, then the VM is converted to a template.
- **Per-clone first boot** — a per-VM NoCloud seed ISO carries hostname + per-device `runcmd` lines. Cloud-init regenerates machine-id + SSH host keys on first boot.

The Windows path is untouched.

## 5. Step types (Ubuntu, v1)

| Step type | Purpose | Compiles to | `causes_reboot` |
|-----------|---------|-------------|-----------------|
| `install_ubuntu_core` | Version pin, locale, timezone, keyboard, LVM storage layout | `autoinstall:` root keys | no |
| `create_ubuntu_user` | References `local_admin` credential; password hashed with `crypt.METHOD_SHA512` | `autoinstall.user-data.users` | no |
| `install_apt_packages` | List of apt packages | `autoinstall.packages` | no |
| `install_snap_packages` | List of `{name, classic?}` entries | `autoinstall.snaps` | no |
| `remove_apt_packages` | List of packages to purge post-install | `late-commands` `apt-get purge` block | no |
| `install_intune_portal` | MS repo + `intune-portal` apt install | `late-commands` block | no |
| `install_edge` | `microsoft-edge-stable` via MS repo | `late-commands` block | no |
| `install_mde_linux` | `mdatp` + run onboarding script from `mde_onboarding` credential | `late-commands` block; onboarding blob written to `/tmp/`, invoked, deleted | no |
| `run_late_command` | Arbitrary shell during install | `late-commands` entry | no |
| `run_firstboot_script` | Per-clone cloud-init `runcmd` on first boot after clone | Per-clone seed-ISO `runcmd` entry | caller-declared |

Windows steps (`set_oem_hardware`, `local_admin`, `autopilot_entra`, `join_ad_domain`, `rename_computer`, etc.) remain typed as Windows-only. `set_oem_hardware` is the only cross-OS step — it applies SMBIOS at clone time regardless of target_os.

## 6. Data model delta

Additive changes to the Phase A schema:

```
task_sequences
  + target_os TEXT NOT NULL DEFAULT 'windows'   -- 'windows' | 'ubuntu'

credentials.type
  + 'mde_onboarding'                            -- uploaded .py from Defender portal
```

Migration: backfills `target_os='windows'` on all existing rows. No data loss.

Step-type-to-OS mapping lives in code (a dict on the step-compiler registry), not in the DB, so adding new step types in the future stays a code-only change.

## 7. Compiler

Parallel to the Windows XML compiler. For each Ubuntu step, `compile(params, credentials) -> StepOutput` contributes into three buckets:

- `autoinstall_body: dict` — merged into the `autoinstall:` YAML root
- `late_commands: list[str]` — appended to `autoinstall.late-commands` in order
- `firstboot_runcmd: list[str]` — appended to the per-clone cloud-init's `runcmd`

YAML merge uses `ruamel.yaml` (preserves key ordering and comments so debug output is readable). Dict merges are shallow at the autoinstall root with list keys (`packages`, `snaps`, `late-commands`) concatenated rather than replaced.

Compiled artifacts are written to `/app/jobs/<job_id>/sequence/ubuntu/`:
- `user-data` — the complete `#cloud-config\nautoinstall:\n...` document for the template build
- `meta-data` — the NoCloud metadata (instance-id derived from the job id)
- `firstboot-user-data` — the per-clone cloud-init (hostname + runcmd)
- `firstboot-meta-data` — per-clone NoCloud metadata

## 8. Template + seed-ISO mechanism

Two ISOs for Ubuntu, both built inside the container and uploaded to Proxmox ISO storage:

### 8.1 `ubuntu-seed.iso` (one per template-build sequence)

NoCloud seed labelled `cidata`. Contains the compiled `user-data` and `meta-data`. Built by a new web helper that mirrors the existing `rebuild_answer_iso` endpoint:

```
POST /api/ubuntu/rebuild-seed-iso?sequence_id=<id>
```

Attached alongside the Ubuntu ISO during template build with kernel cmdline args:

```
autoinstall ds=nocloud
```

Subiquity picks up the seed and runs the autoinstall non-interactively. Declaring `autoinstall.shutdown: poweroff` in the compiled YAML makes the VM halt on completion.

### 8.2 `per-vm-cloud-init-<vmid>.iso` (one per clone)

NoCloud seed labelled `cidata`. Contains `firstboot-user-data` + `firstboot-meta-data`. Attached at clone time on a separate CD-ROM slot. On first boot:

1. Cloud-init picks up the seed (NoCloud datasource, already-installed system).
2. Regenerates `/etc/machine-id` (cloud-init does this automatically when `machine-id` is absent).
3. Regenerates SSH host keys (cloud-init module `ssh`).
4. Sets hostname from the per-clone cloud-init.
5. Runs any `run_firstboot_script` step outputs via `runcmd`.

This is the sysprep analogue for Ubuntu.

### 8.3 Build Template playbook changes

`playbooks/build_template.yml` gains a branch on `target_os`. The Ubuntu branch:

1. Create VM with Ubuntu ISO + `ubuntu-seed.iso` attached. CPU/memory/disk from `vars.yml`. SMBIOS from the `set_oem_hardware` step (if present).
2. Set kernel cmdline to include `autoinstall ds=nocloud`. In Proxmox, this means either editing the boot config or (more robustly) using grub's boot-once `setparams` via serial console, which is what autoinstall tooling typically does. Exact mechanism TBD at implementation time — the Ubuntu autoinstall docs recommend editing the ISO's `grub.cfg`, which a new helper does before upload.
3. Start VM, wait for guest-agent-less power-off (since QEMU GA isn't installed yet — we poll the QMP status).
4. Start VM again, wait for guest agent (it's now installed).
5. Guest-exec: `cloud-init clean --logs --seed && rm -rf /var/lib/cloud/instances/* && shutdown -h now`.
6. Convert VM to template via Proxmox API.

## 9. Credentials

- **`local_admin`** reused. The Ubuntu step `create_ubuntu_user` reads `username` and `password`, applies `crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))`, and emits:

  ```yaml
  user-data:
    users:
      - name: <username>
        passwd: <hash>
        groups: [sudo]
        shell: /bin/bash
        lock_passwd: false
  ```

- **`mde_onboarding`** (new). Form uploads the `.py` onboarding script from the Microsoft Defender portal. Encrypted at rest with the same Fernet key as other credentials (`/app/secrets/credential_key`). Consumed only by `install_mde_linux`. The compiled late-commands block:

  ```
  # Install mdatp
  curtin in-target --target=/target -- apt-get install -y mdatp
  # Copy onboarding script into the image, run, delete
  curtin in-target --target=/target -- mkdir -p /tmp/mde
  curtin in-target --target=/target -- bash -c 'echo "<base64>" | base64 -d > /tmp/mde/onboard.py'
  curtin in-target --target=/target -- python3 /tmp/mde/onboard.py
  curtin in-target --target=/target -- rm -rf /tmp/mde
  ```

  The onboarding blob is plaintext in the compiled `user-data` file while the VM specializes. Mitigation identical to the Windows `<UnattendedJoin>` pattern: per-job artifacts directory is `0600`, scrubbed at job completion; recommend a dedicated onboarding script per lab/tenant.

## 10. Seeded sequences

On first container start, if the `task_sequences` table contains zero rows with `target_os='ubuntu'`, two Ubuntu sequences are inserted:

1. **"Ubuntu Intune + MDE (LinuxESP)"** — `target_os=ubuntu`, `produces_autopilot_hash=0`. Steps:
   - `install_ubuntu_core` — en_US, UTC (override from LinuxESP's Europe/Berlin default), LVM layout, autoinstall version 1
   - `create_ubuntu_user` — references the auto-seeded `default-local-admin` credential
   - `install_apt_packages` — `curl, git, wget, gpg`
   - `install_snap_packages` — `code (classic), postman, powershell (classic)`
   - `install_intune_portal`
   - `install_edge`
   - `install_mde_linux` — empty credential reference; user must create an `mde_onboarding` credential before first use. Compiler refuses at provision time if not set, returning a clear error similar to `autopilot_hybrid`'s unimplemented message.
   - `remove_apt_packages` — `libreoffice*, remmina*, transmission*`
2. **"Ubuntu Plain"** — `target_os=ubuntu`, `produces_autopilot_hash=0`. Steps:
   - `install_ubuntu_core`
   - `create_ubuntu_user`

Seed migration is idempotent, keyed on sequence `name`.

## 11. Provisioning flow

`playbooks/provision_clone.yml` gains a top-level branch on `target_os` read from the sequence. The Ubuntu branch delegates to a new `proxmox_vm_clone_linux` role that shares logic with `proxmox_vm_clone` for the clone, SMBIOS, disk-resize portions and diverges at:

- Attaches the per-VM cloud-init seed ISO to CD-ROM slot `ide3`.
- Waits for cloud-init completion via guest-exec `cloud-init status --wait` (600 s timeout).
- Skips Autopilot injection entirely (the sequence compiler gates it — only `autopilot_entra` would drop an `AutopilotConfigurationFile.json`, and that step is Windows-only).

Provision UI additions for Ubuntu sequences:

- Existing **Task Sequence** dropdown filters and shows the effective `target_os`: `"Ubuntu Intune + MDE (LinuxESP) (ubuntu)"`.
- Hostname pattern field (new, applies to both OSes): `autopilot-{serial}` default; compiles into the per-clone cloud-init or, for Windows, replaces the existing specialize-pass hostname logic. Existing `vm_name_prefix` behavior stays as the VMID-based Proxmox name.
- OEM profile dropdown stays — SMBIOS is harmless on Linux and still visible to `dmidecode`.

## 12. Devices page

- Capture Hash / Capture & Upload actions disable automatically for Ubuntu VMs — the existing `produces_autopilot_hash=false` check already covers this; no new code needed.
- **Check Enrollment** action (new, visible only for Ubuntu VMs). Runs via guest-exec:

  ```
  intune-portal --version 2>/dev/null || echo MISSING
  mdatp health 2>/dev/null || echo MISSING
  ```

  Outcome rendered as two green/red chips next to the VM row (Intune: healthy/missing; MDE: healthy/missing/not-configured). The chip state is persisted per-VM via Proxmox VM **tags** (the structured `tags` field, not the free-form description) with a prefix `enroll-intune-ok`, `enroll-intune-missing`, `enroll-mde-ok`, etc., so refreshes don't re-invoke the agent.

## 13. Precedence rules

Unchanged from the Windows spec:

```
vars.yml default  →  sequence step param  →  provision-form field
(lowest)                                     (highest)
```

New Ubuntu-specific `vars.yml` defaults (all overridable):

```yaml
ubuntu_release: "noble"          # 24.04 codename
ubuntu_locale: "en_US.UTF-8"
ubuntu_timezone: "UTC"
ubuntu_keyboard_layout: "us"
ubuntu_storage_layout: "lvm"
```

## 14. Compatibility

- **Existing Windows sequences** — the migration backfills `target_os='windows'`. Builder and compiler behavior unchanged.
- **Existing `vars.yml` installs** — new Ubuntu keys default as listed in §13. Nothing is required of an all-Windows user.
- **Existing ISOs on Proxmox** — Windows ISO and VirtIO ISO settings are unchanged. Ubuntu adds a new `ubuntu_iso` path setting; defaults to `isos:iso/ubuntu-24.04-live-server-amd64.iso`.
- **Existing `oem_profiles.yml`** — used by both OSes. No change.

## 15. Testing

**Unit (pytest, `tests/` directory):**
- One test per Ubuntu step type: fixture input → expected compiled `StepOutput`. Covers all three buckets.
- Compiler YAML round-trip: the generated `user-data` reparses to a dict that matches expected structure.
- `create_ubuntu_user` password-hash test: known password → hash matches `crypt.crypt(..., salt)` with that salt.
- `install_mde_linux` — compiler refuses at provision time if credential is empty.
- `mde_onboarding` credential encrypt → store → decrypt round-trip.

**Integration (pytest + httpx against FastAPI app):**
- POST new Ubuntu sequence → GET `/api/sequences/<id>/compile` → assert compiled `user-data` matches a golden fixture.
- Seed migration on empty DB produces exactly 2 Ubuntu + 3 Windows = 5 sequences.
- Provision-UI precedence: override hostname-pattern → compiled per-clone cloud-init has the override, not the sequence value.

**LinuxESP parity snapshot test:**
- The seeded "Ubuntu Intune + MDE (LinuxESP)" sequence compiles to a YAML whose `autoinstall:` body diff-checks against a snapshot of LinuxESP's upstream `autoinstall.yaml` stored at `tests/fixtures/linuxesp-snapshot.yaml`. The diff excludes the `install_mde_linux` block (LinuxESP upstream installs Intune + Edge but not MDE — MDE is our addition). Upstream changes in the overlapping surface trigger a test failure so we intentionally acknowledge drift rather than silently diverging.

## 16. Implementation slicing (for the subsequent plan)

Rough order, each a shippable slice:

1. Schema: `target_os` column + migration. Builder UI shows `target_os` read-only; no new step types yet. Add `ruamel.yaml` to `requirements.txt` (new runtime dep).
2. `mde_onboarding` credential type (type + CRUD form + encryption).
3. Ubuntu step-type registry + the ten step types' `compile()` functions (YAML-native, no playbook wiring yet).
4. Ubuntu seed-ISO web helper (mirrors existing `rebuild_answer_iso`). `POST /api/ubuntu/rebuild-seed-iso`.
5. Build Template page: `target_os` toggle + **Rebuild Ubuntu Seed ISO** + **Build Ubuntu Template** buttons.
6. `playbooks/build_template.yml` Ubuntu branch: autoinstall → `cloud-init clean` → template convert.
7. Per-clone cloud-init seed ISO generator + `proxmox_vm_clone_linux` role.
8. `playbooks/provision_clone.yml` `target_os` branch.
9. Seed migration: Ubuntu sequences inserted on empty DB.
10. Devices page **Check Enrollment** action + persisted status chip.
11. Parity snapshot test + unit + integration tests throughout.

## 17. Open items decided during brainstorming (recorded here)

| # | Decision |
|---|---|
| U1 | Ubuntu-only in v1; RHEL/Kickstart/cloud-images deferred. |
| U2 | Template + clone pattern (not install-from-ISO every VM). `cloud-init clean` is the sysprep analogue. |
| U3 | Decomposed step library, LinuxESP as one seeded sequence. No LinuxESP monolith step. |
| U4 | `mde_onboarding` is a new credential type, not a file in `files/`. Encrypted at rest like other credentials. |
| U5 | `target_os` is a sequence-level column, not a per-step discriminator. Step types declare OS compatibility in code. |
| U6 | Hostname pattern is a new provision-form field, applies to both OSes. Per-clone cloud-init seed ISO carries it for Ubuntu. |
| U7 | YAML compilation uses `ruamel.yaml` (YAML-native merge), not Jinja2 — Ubuntu autoinstall is structured YAML; Jinja fits XML better. |
| U8 | Intune Portal install via `intune-portal` apt package. Enrollment completes on user sign-in, not automated. |
| U9 | Parity with upstream LinuxESP is snapshot-tested, so drift is explicit rather than silent. |
| U10 | Kernel cmdline (`autoinstall ds=nocloud`) applied by editing the uploaded Ubuntu ISO's `grub.cfg` during the seed-ISO build step. Exact mechanism finalised at implementation time. |
