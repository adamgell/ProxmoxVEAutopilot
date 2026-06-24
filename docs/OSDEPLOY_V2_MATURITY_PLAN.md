# OSDeploy v2 Maturity Plan

OSDeploy v2 is the advanced Windows deployment path for Windows Server and later
role automation. It must match the CloudOSD operational surface before it is used
for real Server workloads. CloudOSD remains the desktop-client path. Existing
WinPE is labeled Legacy WinPE and remains supported as the fallback image-apply
path.

## M1 Scope

M1 proves one deployable role only: OSDeploy Windows Server Base.

The other role templates stay visible in the v2 task library so the long-term
shape is clear, but they are not launchable until the Server Base path has passed
the live Proxmox E2E gate:

- OSDeploy Windows Server Base
- OSDeploy File Server
- OSDeploy Isolated Domain Controller
- OSDeploy MECM Prereq Baseline
- OSDeploy Lab in a Box

## Operator Surface

The OSDeploy cockpit owns advanced Windows Server operations:

- `/osdeploy`: overview, metrics, active and archived run history
- `/osdeploy/builder`: preflight, review, and launch controls
- `/osdeploy/artifacts`: build, publish, status, hashes, source media, and job links
- `/osdeploy/cache`: catalog refresh, warm, verify, delete, download, and errors
- `/osdeploy/runs/{run_id}`: v2 plan, event timeline, job evidence, heartbeat, and readiness

API parity lives under `/api/osdeploy/v1` and is additive to CloudOSD and Legacy
WinPE. OSDeploy-specific code owns artifact, cache, PE callback, and launch
metadata. The phase-neutral task engine and full-OS package protocol stay under
`/osd/v2`.

## Artifact Lifecycle

The build/publish artifact lifecycle is:

1. Queue an `osdeploy_build_iso` job from `/api/osdeploy/v1/artifacts/build`.
2. Send the repo OSDeploy build tools to the Windows build host.
3. Build pinned Server media with OSDeploy, OSD, OSDBuilder, and ADK inputs.
4. Inject the OSDeploy PE bridge into boot media.
5. Record the manifest, hashes, source media, image name, edition/index/language,
   module versions, ADK path, and local output paths.
6. Queue an `osdeploy_publish_iso` job from
   `/api/osdeploy/v1/artifacts/{artifact_id}/publish`.
7. Upload the ISO to Proxmox content storage and only then set `proxmox_volid`.
8. Block launch until the artifact has ISO/WIM hashes and a Proxmox volid.

Build host configuration is operator-owned in `inventory/group_vars/all/vars.yml`
or the equivalent runtime config:

- `osdeploy_build_remote`: SSH target for the Windows build host, for example
  `builder@192.0.2.55`
- `osdeploy_build_remote_root`: Windows build workspace, for example
  `E:\OSDeployBuild`
- `osdeploy_build_ssh_key_path`: key path inside the controller/builder
  container, normally under `/app/secrets`

Use `autopilot-proxmox/scripts/osdeploy-build-host-key.sh` to create the
controller key under the bind-mounted `secrets/` directory and print the public
key that must be installed in the Windows build user's `authorized_keys` file.
The cockpit artifacts view also reports the resolved key path, whether the key
exists, and the public key when available.

The cockpit and `/api/osdeploy/v1/artifacts/build/preflight` must report these
resolved values and block build jobs when the key is missing or the build host
is not reachable on TCP 22.

Artifact build requests carry the Server image inputs into both direct SSH and
build-host-agent paths:

- `source_media_path`
- `image_name`
- `image_index`
- `os_version`
- `os_edition`
- `os_language`

These values are passed to `build-osdeploy.ps1` and must appear in the resulting
manifest so the artifact row can prove the exact Server source image that was
built and published.

When first-run setup state has not published `build_host_expected_agent_id`,
operators can pass `build_host_agent_id` to build with an explicitly registered
build-host agent. The agent must have a fresh heartbeat and report
`current_phase=build-host`; ordinary bootstrap or deployment agents are blocked
from receiving OSDeploy build work.

To intentionally convert a registered agent into a build host, the OSDeploy
cockpit queues `configure_build_host_role` through
`POST /api/osdeploy/v1/build-host/agents/{agent_id}/activate`. The endpoint
requires `confirm_build_host=true`, a fresh heartbeat, and writes the
`build-host` phase/role through the agent work queue before OSDeploy build jobs
are allowed. The controller only queues this conversion when the latest
heartbeat advertises `configure_build_host_role`, which prevents old
AutopilotAgent builds from receiving work they cannot claim.

When an explicitly selected agent is fresh but does not advertise that
capability, operators can use
`POST /api/osdeploy/v1/build-host/agents/{agent_id}/repair` from the cockpit.
That QGA repair path preserves the existing agent identity and token binding,
sets `phase=build-host` and `role=build-host`, writes the build capabilities,
optionally replaces the local `AutopilotAgent.exe` from the controller seed
agent endpoint, and restarts the existing service or scheduled task.

Primary files:

- `autopilot-proxmox/scripts/osdeploy_remote_build.py`
- `autopilot-proxmox/scripts/osdeploy_build_job.py`
- `autopilot-proxmox/scripts/osdeploy_publish_job.py`
- `autopilot-proxmox/tools/osdeploy-build/build-osdeploy.ps1`
- `autopilot-proxmox/tools/osdeploy-build/Invoke-OSDeployBridge.ps1`

## Cache Lifecycle

The cache lifecycle is:

1. Refresh the catalog for Server image and update content.
2. Warm selected entries into the configured cache root.
3. Verify size and SHA256 before marking entries ready.
4. Serve ready cache entries through the OSDeploy cache download endpoint.
5. Track served count, last served time, storage status, and per-entry errors.
6. Delete local cached files without deleting the catalog row.

Primary files:

- `autopilot-proxmox/web/osdeploy_cache.py`
- `autopilot-proxmox/scripts/osdeploy_cache_job.py`

## Launch Gates

M1 launch requires all of these checks to pass:

- artifact exists
- artifact architecture matches the requested VM
- artifact has ISO hash, WIM hash, and `proxmox_volid`
- requested role is `base`
- requested memory is at least the Server minimum
- requested disk is at least the Server minimum
- Proxmox node, ISO storage, disk storage, and bridge are available
- `proxmox_virtio_iso` or `virtio_iso` is configured

VirtIO media is a hard requirement because the PE bridge injects VirtIO storage,
network, and QGA drivers before first disk boot.

## Deployment Evidence

Each OSDeploy run must expose enough evidence to diagnose a failed Server build:

- v2 task plan from `ts_provisioning_runs` and `ts_run_plan_steps`
- controller events for run creation, VM identity, archive, and unarchive
- PE events for registration, WIM apply, VirtIO driver injection, offline
  unattend staging, OSD client staging, and boot file staging
- full-OS AutopilotAgent bootstrap and heartbeat
- QGA status reported by the full-OS client
- final readiness state in `osdeploy_readiness`
- job and log links for build, publish, and provision jobs

For M1, only a `base` run can reach final `complete`. Future roles may reach
`role_pending` after Server Base is healthy, but they must not be reported as
complete until the role automation has executed and posted evidence.

## Follow-up TODOs

All follow-up implementation work in this section is subagent-driven. Treat each
bullet as a separate task: create a clean worktree, dispatch one fresh
implementer subagent, run the task's tests, dispatch a task reviewer with a diff
package, fix Critical or Important findings through a subagent, and update
`.superpowers/sdd/progress.md` only after the review is clean. After the selected
follow-ups are complete, dispatch one final whole-branch review before deploy or
PR cleanup.

- Make Gen 1 Autopilot hardware-hash upload resolve an explicit lab M365
  boundary credential reference instead of always using the controller-wide
  `vault_entra_*` values from `upload_hashes.yml`. The upload job should record
  the target tenant ID and Entra app/client ID in `args_json` or equivalent
  evidence so operators can prove which tenant received the hardware hash. This
  is required before labs can safely use a different Entra/Intune tenant than
  the ProxmoxVEAutopilot primary tenant.

## Live Proxmox E2E gate

Feature completion requires a live Proxmox E2E, not only unit tests:

Execute this gate as one subagent-owned validation task so the evidence stays
coherent. The validation subagent must collect command output, UI/API evidence,
run IDs, VM identifiers, and regression results in a task report file. If the
gate exposes a code defect, stop the validation task, open a separate
subagent-driven implementation task for the fix, review it, deploy it, then
resume this gate from the last proven step.

1. Confirm `proxmox_virtio_iso` is configured and points to an uploaded
   VirtIO driver ISO.
2. Build a Windows Server Base OSDeploy artifact on the Windows build host.
3. Publish the artifact to Proxmox ISO/content storage.
4. Refresh and verify any needed OSDeploy cache entries.
5. Pass `/api/osdeploy/v1/preflight` for a Server Base VM.
6. Launch through `/api/osdeploy/v1/runs/{run_id}/provision`.
7. Observe PE registration by SMBIOS UUID/MAC.
8. Observe image apply, VirtIO driver injection, offline unattend staging, OSD
   client staging, and boot file staging events.
9. Confirm the VM stops PE, detaches install/VirtIO ISOs, boots from disk, and
   starts installed Windows Server.
10. Observe AutopilotAgent bootstrap and full-OS heartbeat with QGA running.
11. Verify `/osdeploy/runs/{run_id}` shows all Server Base evidence and final
    readiness `complete`.
12. Re-run CloudOSD and Legacy WinPE regression tests to prove behavior was not
    replaced or broken.

## Regression Contract

OSDeploy v2 must stay additive:

- CloudOSD behavior and playbooks remain unchanged for desktop-client deployment.
- Legacy WinPE behavior and `/winpe/*` tests remain unchanged.
- OSDeploy playbooks must not call CloudOSD or Legacy WinPE playbooks.
- `/provision` can offer `boot_mode=osdeploy`, but WinPE remains Legacy WinPE.
- `/osd/v2` remains the shared v2 task engine and agent package protocol.
