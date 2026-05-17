# First-Run Hard Fix Log

This log records lab blockers that must be fixed in source, scripts, and tests. Do not treat these as manual runbook steps; each entry should describe the automated repair now expected from the first-run path.

## Proxmox API upload failure for large setup ISOs

- Symptom: Publishing a multi-GB OSDeploy ISO through the Proxmox `/upload` API failed with TLS EOF, while PVE `pveproxy` logged `No space left on device`.
- Evidence: PVE storage had hundreds of GiB free and direct PVE-side `rsync` of the same ISO into ISO storage succeeded.
- Root cause: Large setup artifacts can fail in the Proxmox API multipart/proxy/temp-spool path even when backing storage is healthy.
- Script fix: `init-proxmox-ve.sh --phase operational` pulls unpromoted setup ISO artifacts from the Ubuntu controller into PVE ISO storage, then calls the controller promotion API with `already_copied=true`.
- Regression guard: First-run init tests assert PVE-pull promotion exists, and setup promotion tests assert `already_copied=true` marks an artifact without retrying Proxmox API upload.

## Proxmox API token secret drift after resumed setup

- Symptom: OSDeploy provisioning failed on `/cluster/nextid` with `401 invalid token value`.
- Evidence: The `autopilot@pve!ansible` token object existed in PVE, but the secrets in PVE/controller `vault.yml` were rejected by Proxmox.
- Root cause: Token validation and rotation existed only in the foundation phase. Bootstrap/operational resumes could keep using a stale `vault.yml`, and the Ubuntu controller could keep a copied stale vault after PVE token rotation.
- Script fix: `init-proxmox-ve.sh` now uses a reusable PVE access contract in bootstrap and operational phases. The contract validates the token from `vault.yml`, rotates it when PVE rejects it, repairs role/ACL/storage access, rewrites `vars.yml`, and syncs `.env`, `vars.yml`, `vault.yml`, and `secrets/` into the controller without printing secrets.
- Regression guard: First-run init tests assert bootstrap and operational phases repair PVE access and sync controller runtime config before artifact publish/provision continuation.

## Generated controller vars omitted derived Proxmox API fields

- Symptom: OSDeploy provisioning failed before clone while resolving `Authorization` for the Proxmox `/cluster/nextid` call because `proxmox_api_auth_header` was undefined.
- Evidence: The controller builder loaded `/app/ansible.cfg` and `/app/inventory/hosts.yml`, but generated `vars.yml` contained only primitive fields such as `proxmox_host`, `proxmox_port`, and `proxmox_node`; it lacked the derived `proxmox_api_base` and `proxmox_api_auth_header` values that Proxmox roles consume.
- Root cause: First-run config generation rewrote the controller inventory without preserving the Jinja-derived API URL and token header entries from the repository defaults.
- Script fix: Both `init-proxmox-ve.sh` and `init-controller-ubuntu.sh` now write `proxmox_api_base` and `proxmox_api_auth_header` as Jinja expressions into `vars.yml`. The token secret remains in `vault.yml`, so job commands and job args do not need to carry the raw Proxmox token.
- Regression guard: First-run init tests assert both init scripts generate the derived Proxmox API vars.

## Stale media volids after moving between PVE storage layouts

- Symptom: Setup media scan found `local:iso/virtio-win.iso`, but OSDeploy provisioning still used an older `proxmox_virtio_iso` value from a different lab storage name.
- Evidence: `/setup` state showed the correct VirtIO ISO volid on earlier passes, while a fresh OSDeploy provision job still passed `isos:iso/virtio-win-0.1.285.iso` and Proxmox failed VM config update with `storage 'isos' does not exist`.
- Root cause: Media scan updated setup state only in bootstrap/operational phases. A foundation-only redeploy could repair and sync controller config without rescanning media, leaving stale `proxmox_windows_iso` and `proxmox_virtio_iso` values in `vars.yml`. OSDeploy preflight also trusted any non-empty VirtIO value without checking live PVE storage.
- Script fix: `scan_media` now runs during foundation before controller runtime config sync and writes detected Windows/VirtIO volids back into `vars.yml`. OSDeploy preflight/provision validates the configured VirtIO volid against live Proxmox storage and falls back to a discovered `virtio-win*.iso` on ISO-capable storage when the configured storage is stale.
- Regression guard: First-run init tests assert foundation and operational phases scan media before controller sync. OSDeploy endpoint tests assert stale VirtIO storage is recovered from live Proxmox ISO inventory and missing VirtIO media remains a launch blocker.

## Missing OSDeploy blank template on fresh PVE hardware

- Symptom: OSDeploy provisioning failed while cloning `osdeploy_blank_template_vmid` because VMID `9001` did not exist on the node.
- Evidence: Proxmox returned `unable to find configuration file for VM 9001 on node`.
- Root cause: OSDeploy provisioning expects a blank clone source with a VirtIO disk, but first-run setup only carried a configured VMID value. Fresh hardware had no matching template.
- Script fix: The PVE access contract now creates or repairs `autopilot-osdeploy-blank-template`. It reuses a configured VMID if it exists, reuses a named existing template, or creates a new blank VirtIO disk template and writes its VMID back to `vars.yml` and setup state.
- Regression guard: First-run init tests assert OSDeploy blank-template detection, creation, state, and `vars.yml` ownership are part of the PVE init script.

## Host-local Proxmox operations required root SSH but first-run had no credential

- Symptom: OSDeploy clone succeeded, then failed while staging the per-VM SMBIOS binary on the PVE host. The task was `no_log`, but it was the first root SSH operation after clone.
- Evidence: PVE init had no root password in `vault.yml`, and the clone role only supported `sshpass` password auth for host-local writes and QEMU `args`.
- Root cause: Some Proxmox operations are host-local or root-only even when normal API token provisioning works. Fresh labs should not require the operator to paste a root password into the controller.
- Script fix: The PVE access contract now generates a controller-to-PVE root SSH key, authorizes it in PVE root `authorized_keys`, stores the private key in controller-mounted secrets, and writes `proxmox_root_ssh_key_path` into `vars.yml`. The clone role prefers this key for SMBIOS staging and QEMU `args`, then falls back to password/ticket paths.
- Regression guard: First-run init tests assert root SSH key repair and config ownership; sequence tests assert the clone role supports root SSH key and password paths.

## OSDeploy PE media used stale controller URL

- Symptom: OSDeploy provisioning cloned and started a VM successfully, but the run stayed in `awaiting_pe` and never recorded PE registration.
- Evidence: The generated VM booted from the OSDeploy ISO and read hundreds of MiB from the media, but the controller saw only `run_created` and `identity_recorded` events. The OSDeploy PE config template still contained a hard-coded old controller URL.
- Root cause: OSDeploy artifact builds copied `tools/osdeploy-build/config.json` into the boot WIM without forcing the current controller URL. On new hardware or an Ubuntu controller VM, PE could call back to a different controller instead of the one that owns the run.
- Script fix: OSDeploy build scripts now require/pass `ControllerUrl` from the controller job or build-host work item, write that URL into PE `config.json`, clear unsafe fallback URLs unless explicitly provided, and record the embedded controller URL in the artifact manifest.
- Regression guard: OSDeploy build tests assert the remote build command carries `-ControllerUrl`, the build script refuses missing callback URLs, the static config no longer contains the old lab IP, and the Windows build-host worker passes `ControllerUrl` into `build-osdeploy.ps1`.

## Controller Postgres password drift after source sync

- Symptom: A resumed PVE foundation run rebuilt the controller image but `autopilot` restarted unhealthy with `password authentication failed for user "autopilot"`.
- Evidence: The existing Docker Postgres volume was healthy, but the controller `.env` and `secrets/postgres-password` had been replaced during the PVE-to-controller sync.
- Root cause: The PVE bootstrap source sync used `rsync --delete` against the controller repo and did not exclude controller-local runtime files. The later runtime sync copied PVE host `.env` and all PVE secrets, including a different `postgres-password`, over the controller's Docker runtime contract.
- Script fix: PVE source sync now excludes controller-local `.env`, `secrets/`, `cache/`, `jobs/`, and `output/`. PVE runtime sync copies only the Proxmox API config/vault and selected PVE root SSH key files. Controller init preserves a known Postgres password from `migration/restored.env` or existing `.env` when a Postgres data volume already exists, then reconciles the `autopilot` database role password to the active `.env` before starting the web app.
- Regression guard: First-run init tests assert controller runtime files are excluded from source sync, PVE runtime sync no longer copies `.env` or all secrets, controller init preserves the Postgres password for existing volumes, and bootstrap verifies TCP password auth before marking Postgres ready.

## OSDeploy evaluation media failed Windows specialize from invalid product key

- Symptom: An OSDeploy VM applied the image, injected VirtIO drivers, staged SetupComplete/unattend, detached PE media, and booted from disk, then never posted the full-OS heartbeat.
- Evidence: A read-only QEMU screendump showed Windows Setup stopped at `Getting ready` with `Windows could not parse or process the unattend answer file for pass [specialize]` in component `Microsoft-Windows-Shell-Setup`. The run artifact metadata showed `image_name=Windows 11 Enterprise Evaluation`, while the PE bridge always wrote a Server Datacenter product key into the specialize `Shell-Setup` component.
- Root cause: The unattend generator assumed every OSDeploy artifact was Windows Server. Official evaluation/client media can reject an unrelated product key during specialize before SetupComplete or the full-OS heartbeat can run.
- Script fix: `Invoke-OSDeployBridge.ps1` now resolves product keys from actual artifact metadata. It omits `ProductKey` for evaluation media, uses client KMS keys only for known Windows 10/11 non-evaluation editions, keeps Server keys for known Server editions, and removes stale `ProductKey` entries when a regenerated unattend does not need one. OSDeploy preflight also blocks launch requests whose requested OS version/edition do not match the selected artifact metadata.
- Regression guard: OSDeploy bridge tests assert image metadata is passed into unattend generation, evaluation images remove `ProductKey`, client/server key mappings remain explicit, and OSDeploy API tests reject artifact/request OS mismatches before provisioning starts.

## Build-host identity disappeared after controller rebuild

- Symptom: After a healthy controller rebuild, `/api/setup/v1/state` reported the build host as `missing` with no VMID or expected agent identity, even though PVE still had `autopilot-buildhost-01` running and prior artifacts showed `producer_agent_id=buildhost-101`.
- Evidence: PVE `qm list` showed VMID `101 autopilot-buildhost-01 running`, while the regenerated `foundation_state.json` contained only controller/PVE foundation fields and no `build_host_vmid` or `build_host_expected_agent_id`.
- Root cause: PVE foundation state can be recreated during resume/redeploy without carrying controller-local build-host fields forward. Setup readiness trusted only the state file, so existing build-host VMs became invisible until a manual state edit or full rebuild-host recreation.
- Script fix: `/setup` build-host readiness now discovers an existing VM named `autopilot-buildhost-01` from Proxmox cluster inventory when `build_host_vmid` is absent, then derives `expected_agent_id=buildhost-<vmid>` and the node from live PVE state. Repair/workload APIs can recover the existing build host without manual state edits.
- Regression guard: First-run setup tests assert build-host readiness infers VMID, node, and expected agent identity from Proxmox inventory when the state file was recreated.

## Build-host repair reported success while QGA was unavailable

- Symptom: `/api/setup/v1/build-host/repair-agent` returned success, but the build-host heartbeat stayed stale and later QGA probes returned `QEMU guest agent is not running`.
- Evidence: The repair response showed the expected VMID and controller URL, but `/api/setup/v1/build-host` kept the same old heartbeat timestamp. PVE reported VMID 101 running while `qm agent 101 ping` failed.
- Root cause: The repair endpoint treated guest-exec completion as sufficient and had no recovery path for a stale build host whose QGA disappeared during or after repair. A stale Windows build host can need a controlled reboot before QGA and the agent service recover.
- Script fix: Build-host repair now detects QGA/guest-exec unavailability. When `allow_reboot` is enabled, it issues a Proxmox VM reset for the stale build-host VM and returns `next_expected_state=wait_for_qga_then_rerun_repair` instead of falsely claiming the agent was repaired. A Proxmox `reboot` request was not sufficient on the lab build host because uptime kept increasing and QGA stayed down.
- Regression guard: First-run setup tests assert QGA-unavailable repair queues a Proxmox reset and returns the explicit rerun state.

## Build-host agent started but never received an agent token

- Symptom: After QGA recovered and repair restarted `AutopilotAgent.exe`, `/api/setup/v1/build-host` still showed a stale heartbeat.
- Evidence: Guest logs showed the agent process running, repeatedly logging `Bootstrap completed` followed by `AgentToken is missing; heartbeat skipped`. The controller state file had no `build_host_agent_auto_approve`, `build_host_vmid`, or expected build-host agent identity fields.
- Root cause: A controller rebuild can recreate setup state without the expected build-host auto-approval identity. The fleet bootstrap endpoint correctly returned a pending approval without an `agent_token`, but the Windows agent treated that pending response as completed bootstrap and kept looping without a token.
- Script fix: Build-host repair now re-persists the expected build-host identity and auto-approval fields before it writes guest config and restarts the agent. The Windows agent now treats bootstrap responses without `agent_token` as pending approval, logs that pending state, honors `retry_after_seconds`, and does not claim bootstrap completion until a real token is returned.
- Regression guard: First-run setup tests assert repair restores the build-host auto-approval identity fields. Agent contract tests assert pending bootstrap responses deserialize without an agent token and the worker keeps explicit pending-bootstrap handling.

## Build-host MSI restore had no NuGet source on clean Windows

- Symptom: The first build-host `build_agent_msi` work item failed during .NET restore with `No sources found`.
- Evidence: The clean Windows build host had the .NET SDK installed, but `dotnet nuget list source` returned no configured sources, so restore could not resolve Microsoft extension packages.
- Root cause: The MSI build script assumed `nuget.org` existed in the machine-level NuGet configuration. New Windows build hosts can install the SDK without a usable package source.
- Script fix: `Build-AutopilotAgent.ps1` now verifies `nuget.org` before publishing. It enables the source when present or adds the official `https://api.nuget.org/v3/index.json` source when missing.
- Regression guard: Agent asset tests assert the build script carries the NuGet source repair helper and official NuGet source URL.

## OSDeploy provision failure before VM identity left stuck runs

- Symptom: A stale-media OSDeploy provision job cloned a VM, then failed before posting VM identity to the controller. The job was failed, but the OSDeploy run stayed `created` with no VMID, no failure state, and no actionable evidence.
- Evidence: The job log showed `Clone template to new VM 105`, followed by a Proxmox config update failure for `storage 'isos' does not exist`. The run detail still had `vmid=null` and no terminal state because the playbook had not reached the identity POST task.
- Root cause: The OSDeploy run lifecycle trusted the successful identity callback as the first source of VM identity. Builder job finalization only updated the generic `jobs` table and did not reconcile deployment-specific state when Ansible died early.
- Script fix: The builder now finalizes related OSDeploy deployments on failed `provision_osdeploy` jobs. It redacts and stores the job log tail, extracts the cloned VMID from Ansible evidence when available, marks the OSDeploy and Task Engine runs `failed`, updates readiness errors, and emits a `provision_job_failed` event. Builder startup also reconciles already-failed OSDeploy provision jobs so a redeploy repairs stuck runs without manual database edits.
- Regression guard: Builder tests assert live job failure and startup reconciliation both mark the OSDeploy run failed, recover VMID `105`, persist readiness errors, close the Task Engine run, and avoid duplicate failure events on repeated reconciliation.

## OSDeploy preflight allowed unavailable VM disk storage

- Symptom: A fresh OSDeploy run used `local-lvm` on dev PVE hardware that only had image storage on `local-zfs`. The clone failed immediately with `storage 'local-lvm' does not exist`.
- Evidence: `pvesm status` showed ISO storage `local` and image storage `local-zfs`; `/api/osdeploy/v1/proxmox/options` exposed `local-zfs`, but an explicit launch payload using `local-lvm` still passed preflight and reached Ansible.
- Root cause: OSDeploy preflight validated artifact/media compatibility and VirtIO ISO reachability, but did not validate that the selected VM disk storage existed and supported `images` content on the current Proxmox installation.
- Script fix: OSDeploy preflight now reads Proxmox storage inventory and blocks launch when selected ISO storage is not ISO-capable or selected VM disk storage is not image-capable. The UI already receives hardware-specific storage options, and the API now protects direct or stale requests too.
- Regression guard: OSDeploy endpoint tests assert invalid disk storage returns `disk_storage_missing` and the run-create endpoint rejects the launch before a job is queued.
