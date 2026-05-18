# First-Run Hard Fix Log

This log records lab blockers that must be fixed in source, scripts, and tests. Do not treat these as manual runbook steps; each entry should describe the automated repair now expected from the first-run path.

## Controller container could not see build-host MSI artifact

- Symptom: CloudOSD preflight blocked fresh deployment runs with `asset_autopilotagent_msi_missing`, even though the Ubuntu controller host had `/opt/ProxmoxVEAutopilot/autopilot-agent/artifacts/AutopilotAgent.msi`.
- Evidence: The `autopilot` container only saw the web app and mounted runtime folders under `/app`; the full source checkout was mounted at `/host/repo`. The asset resolver fell through to `/autopilot-agent/artifacts/AutopilotAgent.msi`, which does not exist inside the container.
- Root cause: CloudOSD and OSDeploy asset lookup knew about `/app/output` and the repo-relative fallback, but not the controller's `/host/repo` bind mount.
- Script fix: CloudOSD and OSDeploy now resolve `AutopilotAgent.msi` from `HOST_REPO_MOUNT` (`/host/repo` by default) and `HOST_REPO_PATH` before falling back to repo-relative paths.
- Regression guard: Endpoint tests assert both deployment surfaces can resolve the MSI from the mounted controller source checkout.

## OSDeploy provision API queued a missing playbook

- Symptom: All OSDeploy provision jobs failed immediately with `the playbook: /app/playbooks/provision_proxmox_osdeploy.yml could not be found`.
- Evidence: The OSDeploy API queued `provision_proxmox_osdeploy.yml`, but the image only carried CloudOSD, WinPE, and legacy provision playbooks.
- Root cause: OSDeploy build/artifact APIs existed before the Proxmox VM launch playbook was added.
- Script fix: Added the OSDeploy Proxmox playbook pair. It clones the blank template, attaches the OSDeploy and VirtIO ISOs, records VM identity, waits for PE registration and image-apply completion, detaches media only after PE handoff, boots the installed disk, and waits for the full-OS AutopilotAgent heartbeat.
- Regression guard: OSDeploy playbook tests assert the wrapper exists, uses `/api/osdeploy/v1` routes, keeps install media until PE completion, and waits for AutopilotAgent completion.

## Parallel builders raced on Proxmox nextid

- Symptom: Scaling builders caused CloudOSD clone jobs to collide on the same Proxmox VMID; one job failed with `unable to create VM 104: config file already exists`.
- Evidence: Multiple pending provision jobs claimed `/cluster/nextid` before the first clone completed, so the advisory next VMID was reused.
- Root cause: Proxmox `/cluster/nextid` is advisory, not a reservation. The clone role treated the first returned VMID as exclusive.
- Script fix: Automatic VMID clone now retries subsequent VMID candidates when Proxmox reports `config file already exists`, while requested VMIDs still fail explicitly.
- Regression guard: Clone role tests assert automatic VMID collision retry is present and bounded.

## Source sync could overwrite controller-local PVE inventory

- Symptom: A source-only redeploy made new provision jobs resolve stale lab inventory such as `proxmox_host: 192.168.2.200` and `proxmox_node: pve2`, causing Proxmox API calls to fail with host lookup/config drift.
- Evidence: Active jobs that had already loaded variables continued, while newly claimed jobs read the overwritten controller-mounted `inventory/group_vars/all/vars.yml`.
- Root cause: The source sync path treated `vars.yml` and `vault.yml` as source files even though first-run treats them as generated runtime config.
- Script fix: PVE source sync now excludes generated `vars.yml` and `vault.yml`; runtime config is copied only through `sync_controller_runtime_config`. A new `--phase runtime-config` repairs PVE access/media/build-host state and syncs runtime config without rebuilding/restarting the controller stack.
- Regression guard: First-run init tests assert source sync excludes generated inventory and the runtime-config-only phase does not run controller init or source sync.

## OSDeploy triggered root-only SMBIOS file staging it does not need

- Symptom: OSDeploy clone jobs created stopped VMs, then failed at `Stage per-VM SMBIOS file on the Proxmox host` before identity or PE boot.
- Evidence: OSDeploy runs had no PE registration, PVE had new stopped clones at the blank-template disk size, and the failed task was the root SSH SMBIOS upload path.
- Root cause: The shared clone role can build a per-VM Type 0/1/3 SMBIOS binary when a chassis override is present. OSDeploy does not need hardware hash/chassis evidence, so invoking that branch only adds a root-only dependency.
- Script fix: The OSDeploy playbook now sets `_skip_chassis_type_smbios_file: true`, keeping the normal `smbios1` identity path and avoiding root-only SMBIOS file staging.
- Regression guard: OSDeploy playbook tests assert the chassis SMBIOS-file path is explicitly skipped.

## Build-host unattended setup stopped at Windows product key

- Symptom: Fresh build-host VM creation booted Windows Setup but stopped at the `Product key` page instead of installing unattended.
- Evidence: QEMU screendump of VMID 101 showed the Windows 11 Setup product-key prompt. The generated seed ISO contained `Autounattend.xml`, `agent.json`, and `bootstrap-build-host.ps1`, so the VM/media plumbing existed and the answer content was the failing layer.
- Root cause: The build-host answer generator emitted `AcceptEula` but omitted the `ProductKey` block that existing unattended templates use to suppress product-key UI. The media scanner also selected the first matching Windows ISO, so when both consumer and Enterprise Evaluation ISOs existed it could choose the consumer ISO for a build host.
- Script fix: Build-host `Autounattend.xml` now emits an empty `ProductKey` with `WillShowUI=Never`, matching the established unattended pattern. PVE media selection now uses `select_windows_iso`, prefers Enterprise/Evaluation media for unattended setup, and skips duplicate automated downloads when a usable Windows ISO is already present.
- Regression guard: First-run setup tests assert the build-host answer media includes the suppressing `ProductKey` block. First-run init tests assert media selection prefers Enterprise/Evaluation ISOs and reuses existing media before resolving another download.

## Proxmox API upload failure for large setup ISOs

- Symptom: Publishing a multi-GB OSDeploy ISO through the Proxmox `/upload` API failed with TLS EOF, while PVE `pveproxy` logged `No space left on device`.
- Evidence: PVE storage had hundreds of GiB free and direct PVE-side `rsync` of the same ISO into ISO storage succeeded.
- Root cause: Large setup artifacts can fail in the Proxmox API multipart/proxy/temp-spool path even when backing storage is healthy.
- Script fix: `init-proxmox-ve.sh --phase operational` pulls unpromoted setup ISO artifacts from the Ubuntu controller into PVE ISO storage, then calls the controller promotion API with `already_copied=true`.
- Regression guard: First-run init tests assert PVE-pull promotion exists, and setup promotion tests assert `already_copied=true` marks an artifact without retrying Proxmox API upload.

## Build-host publish tried to push a large ISO through the controller API

- Symptom: The build host completed MSI, WinPE, CloudOSD, and OSDeploy builds, then `publish_artifacts` failed with `HttpRequestException` and the work row stayed `claimed` because the controller web service was unavailable when the agent tried to report failure.
- Evidence: The controller had the OSDeploy manifest, WIM, and 7.6 GB ISO under `cache/osdeploy/setup-artifacts`, while PVE ISO storage only had the smaller WinPE and CloudOSD ISOs. Agent logs showed `publish_artifacts` failed and then the agent continued to later queued work.
- Root cause: Build-host `publish_artifacts` called the normal controller promotion endpoint, which attempted Proxmox API upload for every ISO. Multi-GB setup ISOs must use the PVE-pull promotion path instead of controller-to-PVE multipart upload.
- Script fix: Controller promotion now defers API upload for artifacts larger than `AUTOPILOT_SETUP_PROMOTE_API_MAX_BYTES` (default 1 GiB) and returns `pve_pull_required_for_large_artifact`. `init-proxmox-ve.sh --phase operational` remains the owner for copying those large artifacts into PVE ISO storage and marking them promoted with `already_copied=true`. Setup and monitoring also treat a claimed build-host work item as no longer active when the same agent has already completed later work, so a lost failure report cannot leave first-run looking permanently busy.
- Regression guard: Agent endpoint tests assert large setup ISO promotion is deferred without calling the Proxmox upload API, small ISO promotion and PVE-pulled promotion behavior remain unchanged, setup readiness ignores superseded claimed work, and deployment monitoring records the superseded claim as terminal instead of active.

## Selected hash upload missed hash output default

- Symptom: A completed CloudOSD v2 test captured `20260518T030221Z-vm102-APE2E004-osd-v2_hwid.csv`, but the upload job failed before reaching Graph with `hash_output_dir is undefined`.
- Evidence: The upload playbook was launched with a selected `hash_file` path. The discovery task was skipped, but the upload task still rendered `HASH_DIR: "{{ hash_output_dir }}"` in its environment.
- Root cause: `upload_hashes.yml` relied on the hash-capture role default without importing that role. Selected-file uploads still need a controller-local hash directory default because the PowerShell uploader accepts both `HASH_FILE` and `HASH_DIR`.
- Script fix: `upload_hashes.yml` now defines `upload_hash_output_dir` with a repo-relative default and uses it for discovery, no-file messages, and the uploader environment.
- Regression guard: Agent asset tests assert the selected-file upload contract keeps `HASH_FILE`, group tags, and the defaulted `HASH_DIR` wrapper.

## CloudOSD upload queued without Entra upload credentials

- Symptom: After the hash-output default was fixed, retrying the CloudOSD Autopilot upload failed immediately with `Missing Entra credentials`.
- Evidence: The completed test VM had QGA, AutopilotAgent heartbeat, and hash capture, but the controller vault for this dev first-run path had no Graph upload credentials.
- Root cause: Local first-run auth is intentionally allowed, but CloudOSD upload retry still treated missing Graph credentials as a failed deployment job instead of a configuration boundary.
- Script fix: CloudOSD Autopilot upload now checks Entra upload credential presence before queueing. If credentials are missing, readiness becomes `upload_not_configured`, no job is queued, and the next action is `configure_entra`.
- Regression guard: CloudOSD endpoint tests assert uploads queue when credentials exist and return `upload_not_configured` without creating a job when credentials are absent.

## CloudOSD monitoring kept failed hash-upload state after Entra boundary repair

- Symptom: `/api/cloudosd/.../autopilot/readiness` correctly reported `upload_not_configured`, but `/monitoring` still marked the same completed CloudOSD run as `failed` at the `Hash upload` phase.
- Evidence: Monitoring evidence showed `state=complete`, `hash_status=captured`, `upload_status=not_configured`, and `readiness_state=upload_not_configured`, while the deployment row still had `health=failed`.
- Root cause: The monitoring backfill treated any `upload_error` as a hash-upload failure and the phase telemetry store kept prior failed rows sticky even after the authoritative readiness state changed to a terminal non-failure.
- Script fix: CloudOSD deployment monitoring now classifies `upload_status=not_configured` or `readiness_state=upload_not_configured` as a terminal skipped hash-upload phase. The telemetry upsert can also recover a failed phase to authoritative terminal `done` or `skipped` state and clears stale errors for those recovered terminal states.
- Regression guard: Deployment health tests assert upload-not-configured CloudOSD runs become completed monitoring rows, and deployment telemetry tests assert terminal source truth can recover a failed phase without keeping the old error.

## Aborted CloudOSD run stayed active after PE registration and hash capture

- Symptom: `/monitoring` showed an old CloudOSD run as active/stuck in `Hash upload` even though its Proxmox provision job had already failed.
- Evidence: The row had `provision_job_status=failed`, `provision_job_exit_code=-15`, `state=pe_registered`, `hash_status=captured`, and `upload_status=not_started`.
- Root cause: CloudOSD monitoring treated PE registration as a completed provision phase and hash capture as enough to open the upload phase, but it did not close downstream phases when the owning provision job failed before OSDCloud completion.
- Script fix: If the provision job fails after PE registration but before OSDCloud starts/finishes, monitoring marks the OSDCloud phase failed and skips hash upload when no upload was started. Failed aborted runs no longer remain active solely because a readiness row exists.
- Regression guard: Deployment health tests assert a post-PE provision failure does not leave `hash_upload` active and is absent from the active deployment list.

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

## Controller bootstrap stopped with only Postgres running

- Symptom: Guided foundation created the Ubuntu controller, built the seed agent and web image, then returned to the installer with only `autopilot-postgres` running. The controller web UI was not listening on port 5000.
- Evidence: Controller setup state had `web_image_ready=true` and `controller_seed_agent_ready=true`, but no `postgres_database_ready`, `compose_ready`, `controller_runtime_ready`, or `console_health_ready`. Compose showed only `autopilot-postgres` healthy; rerunning the controller init script completed and started `autopilot`, `autopilot-builder`, `autopilot-monitor`, and `autopilot-mcp`.
- Root cause: On a fresh Postgres volume, `pg_isready` can report ready during the image entrypoint's temporary bootstrap server before the normal fast shutdown/restart. The controller init script treated that early readiness as stable and could race the restart while running setup SQL, exiting before launching the app services.
- Script fix: Controller init now waits for the Postgres container healthcheck plus a stable SQL probe before database repair, records a non-secret `controller_bootstrap_error` when bootstrap fails, and restores the controller `phase` after migration bundle restore. PVE foundation retries the controller bootstrap once and prints Compose/Postgres diagnostics so a partial Postgres-only first-run state is repaired automatically.
- Regression guard: First-run init tests assert controller bootstrap waits for container health, records failure state, and the PVE controller bootstrap path retries once after a partial first-run failure.

## Build-host agent looked stale during long prerequisite install

- Symptom: `/setup` reported the build-host agent as stale while the Windows build host was actively running the ADK/WinPE prerequisite workload.
- Evidence: VMID 101 had `adkwinpesetup.exe` and `msiexec.exe` running through QGA, while `/api/setup/v1/state` showed the last heartbeat aging past 180 seconds and blocked `build_host_agent`.
- Root cause: The Windows agent processes long build-host work items synchronously after its heartbeat loop, so it does not emit another heartbeat until the workload finishes. Setup readiness treated heartbeat age alone as agent health.
- Script fix: `/setup` now looks for a claimed build-host work item for the expected agent. If the claimed work is still inside its workload-specific timeout, the agent is reported as `busy` and the active work details are surfaced instead of blocking as stale.
- Regression guard: First-run setup tests assert a stale heartbeat plus an in-window claimed `install_build_prerequisites` item reports `agent_state=busy` and keeps the build-host agent readiness unblocked.

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

## OSDeploy v2 heartbeat could complete before persistent AutopilotAgent install

- Symptom: OSDeploy base did not need hardware hash capture, but the Task Engine v2 plan still needed persistent AutopilotAgent ownership for post-OS work. The run could reach readiness from the full-OS OSD client heartbeat alone.
- Evidence: Generated OSDeploy v2 plans had `stage_autopilot_agent` and `wait_agent_heartbeat`, but no explicit full-OS `install_autopilot_agent` step. The OSDeploy package had a run bootstrap token but did not carry the MSI/postinstall payload metadata into `osd-config.json`, and `OsdClient.ps1` treated `wait_agent_heartbeat` as already satisfied.
- Root cause: The CloudOSD first-boot path installs AutopilotAgent before the OSD client heartbeat gate, but OSDeploy reused the same OSD client action behavior without adding its own persistent-agent install path.
- Script fix: OSDeploy packages now include source-built AutopilotAgent MSI and postinstall payload metadata. The WinPE bridge writes those payloads plus the run-scoped agent bootstrap contract into `osd-config.json`. Generated OSDeploy v2 plans include a full-OS `install_autopilot_agent` step before `wait_agent_heartbeat`, and the OSD client installs the MSI, runs postinstall, and keeps `wait_agent_heartbeat` as an idempotent fallback.
- Regression guard: OSD v2 endpoint tests assert OSDeploy packages include the agent payload contract and generated run plans order `install_autopilot_agent` before `wait_agent_heartbeat`. Pester tests assert the OSD client invokes the persistent-agent install path for both explicit install steps and OSDeploy heartbeat fallback.

## Dev-lab reset missed generated batch VM names

- Symptom: `reset-dev-lab` removed the controller, build host, template, and explicit `*-E2E-*` names, but left generated lab batch VMs such as `CSD17185201` and `OSD17191201`.
- Evidence: The live pvetest inventory still contained CloudOSD `CSD...` and OSDeploy `OSD...` test VMs after the reset allowlist was reviewed. Those names came from the `/provision` batch patterns used during five-machine testing, not the older `CLOUDOSD-E2E-*` and `OSDEPLOY-E2E-*` patterns.
- Root cause: The destructive reset allowlist only covered the original named E2E patterns and missed the compact generated prefixes used by the current lab batch launch flow.
- Script fix: `init-proxmox-ve.sh --phase reset-dev-lab` now treats `CSD[0-9]*` and `OSD[0-9]*` as disposable generated dev-lab VM names, alongside the controller/build-host/template and explicit E2E prefixes.
- Regression guard: First-run init tests assert the reset allowlist includes generated CloudOSD and OSDeploy batch prefixes before the script is used on pvetest.

## Dev-lab reset missed final APE2E acceptance VM

- Symptom: After a successful guided-install acceptance run, `reset-dev-lab` would have removed the controller, build host, and blank template but left the final CloudOSD test VM `APE2E004`.
- Evidence: The live pvetest inventory showed `100 autopilot-controller-01`, `101 autopilot-buildhost-01`, `102 APE2E004`, and `9001 autopilot-osdeploy-blank-template`; only `APE2E004` did not match the reset allowlist.
- Root cause: The single-VM acceptance flow used the `APE2E###` naming pattern instead of the earlier `CSD###`, `OSD###`, or explicit `*-E2E-*` dev-lab names.
- Script fix: `init-proxmox-ve.sh --phase reset-dev-lab` now treats `APE2E[0-9]*` as disposable dev-lab VM names.
- Regression guard: First-run init tests assert the APE2E reset pattern is present before teardown.

## Dev-lab reset missed generated WinPE artifact media

- Symptom: Running `reset-dev-lab --reset-media` removed Windows, VirtIO, CloudOSD, OSDeploy, and build-host seed media, but left `winpe-autopilot-amd64-401fe155b3d54fb3.iso` in PVE ISO storage.
- Evidence: After reset, the generated-media match set was empty but `find /var/lib/vz/template/iso -name '*.iso'` still showed the WinPE artifact ISO.
- Root cause: The reset media allowlist included CloudOSD and OSDeploy artifact patterns but not the generated WinPE artifact naming pattern.
- Script fix: `reset-dev-lab --reset-media` now removes `winpe-autopilot-*.iso` alongside the other generated setup artifacts.
- Regression guard: First-run init tests assert the WinPE artifact reset pattern is present.

## CloudOSD provisioning omitted the blank-template VMID contract

- Symptom: A CloudOSD run failed before cloning with `winpe_blank_template_vmid` undefined.
- Evidence: The provision job command had CloudOSD artifact/media fields but no blank-template VMID, while setup state had the shared blank template VMID `9001`.
- Root cause: The CloudOSD provision endpoint did not pass `cloudosd_blank_template_vmid` from setup state/config into the playbook, and the playbook default expression referenced `winpe_blank_template_vmid` without guarding it.
- Script fix: CloudOSD provision jobs now pass `cloudosd_blank_template_vmid` and a `winpe_blank_template_vmid` fallback from setup state/config. The playbook guards the fallback with `default(None)`.
- Regression guard: CloudOSD endpoint tests assert the provision job receives both blank-template fields.

## Requested-VMID CloudOSD clone tripped the automatic retry loop label

- Symptom: A requested-VMID CloudOSD clone created VMID `102`, then the Ansible job failed in the skipped automatic VMID retry loop with `_initial_auto_vmid is undefined`.
- Evidence: PVE showed the cloned VM existed, but the job failed before VM configuration because Ansible evaluated `loop_control.label` for the skipped retry task.
- Root cause: `_initial_auto_vmid` was only set for automatic-VMID runs. Requested-VMID runs skip the retry loop, but Ansible can still template loop labels while reporting skipped items.
- Script fix: The clone role now captures the first VMID candidate unconditionally before the automatic retry task, so skipped labels are safe and requested VMIDs continue past clone.
- Regression guard: CloudOSD playbook tests assert the candidate fact is unconditional and the retry label still has a `default(vm_vmid)` fallback.

## CloudOSD disk handoff produced an empty EFI system partition

- Symptom: CloudOSD PE applied Windows, passed offline validation, detached media, and rebooted, but the VM stopped at `No bootable option or device was found`.
- Evidence: PVE disk inspection showed a GPT disk with a 500 MiB EFI system partition and Windows OS partition, but the EFI partition contained no boot files. The VM also inherited SeaBIOS from the stale blank template until repaired.
- Root cause: The CloudOSD bridge delegated partitioning to OSDCloud but did not explicitly run `bcdboot` or pass an EFI root into offline validation. The blank template creation path also did not create OVMF EFI/TPM devices.
- Script fix: The CloudOSD bridge now locates or assigns the EFI system partition, runs `bcdboot.exe <WindowsRoot> /s <EfiRoot> /f UEFI`, emits a `uefi_boot_files_staged` event, and validates EFI BCD files before PE completion. The clone role adds OVMF, EFI disk, and TPM state when Secure Boot or TPM is requested, and PVE init creates new blank templates with OVMF/EFI/TPM.
- Regression guard: CloudOSD bridge Pester tests assert the bcdboot staging path and EFI validation are present. Clone role and first-run init tests assert UEFI/EFI/TPM configuration is applied.
