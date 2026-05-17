# Windows Build Host Runbook

The Windows build host is a disposable Windows 11 x64 VM used to build Windows
artifacts from source. It is required because WinPE and MSI production depend on
Windows-only tooling such as Microsoft ADK, the WinPE add-on, and WiX.

The build host is not a source of trusted prebuilt binaries. It receives an
exact source bundle from the controller, builds artifacts locally, uploads them
back with metadata, and lets the controller promote them into Proxmox storage.

## Default Identity

```text
VM name: autopilot-buildhost-01
VMID: 100
Computer name: AUTOPILOT-BLD
Agent ID: buildhost-100
Agent role: build-host
Controller URL: http://192.168.2.115:5000
```

The seed agent initially uses a bootstrap token. The controller approves the
expected build-host identity automatically only when the requested VM identity
matches setup state; otherwise operator approval is required.

## Creation Path

The controller owns the build-host workflow through `/setup` and
`/api/setup/v1/*`.

1. PVE init scans Windows and VirtIO media and publishes state.
2. `/setup` generates a seed ISO containing:
   - `AutopilotAgent.exe`
   - `agent.json`
   - `bootstrap-build-host.ps1`
   - a first-boot unattend command
3. `/setup` creates the Windows build-host VM with UEFI, TPM, VirtIO, QGA
   enabled, Windows ISO, VirtIO ISO, and the seed ISO.
4. First boot installs/starts the seed agent.
5. The agent reports `phase=build-host` and `role=build-host`.
6. The controller approves or waits for operator approval.
7. The controller queues allowlisted build workloads.

## Repair Existing VMID 100

If VMID 100 already exists but the agent is stale, repair it through QGA:

```bash
curl -fsS -X POST 'http://192.168.2.115:5000/api/setup/v1/build-host/repair-agent' \
  -H 'Content-Type: application/json' \
  -d '{"vmid":"100","agent_id":"buildhost-100","computer_name":"AUTOPILOT-BLD","auto_approve":true}'
```

The repair action:

- Downloads the current seed `AutopilotAgent.exe` from the controller.
- Rewrites `agent.json` with the current controller URL and bootstrap token.
- Sets `phase=build-host` and `role=build-host`.
- Clears stale registered-agent tokens.
- Restarts the Windows service.
- Waits for a fresh heartbeat.

## Allowlisted Work Kinds

The build-host role accepts only these work kinds:

| Work kind | Purpose |
| --- | --- |
| `install_build_prerequisites` | Install or verify local build prerequisites |
| `fetch_source_bundle` | Download the exact controller source bundle and manifest |
| `build_agent_msi` | Build x64 and arm64 AutopilotAgent MSI artifacts |
| `build_winpe` | Build WinPE ISO/WIM from repo scripts and ADK |
| `build_cloudosd` | Build CloudOSD ISO/WIM from repo scripts and ADK |
| `publish_artifacts` | Upload artifacts, manifests, and SHA-256 metadata to the controller |

Unsupported work is rejected by the agent.

Queue the normal first-run set:

```bash
curl -fsS -X POST 'http://192.168.2.115:5000/api/setup/v1/build-host/workloads' \
  -H 'Content-Type: application/json' \
  -d '{"force":true,"kinds":["fetch_source_bundle","build_agent_msi","build_winpe","build_cloudosd","publish_artifacts"]}'
```

## Artifact Metadata

Every uploaded artifact should include:

- Artifact kind.
- File name and size.
- SHA-256.
- Source commit SHA.
- Dirty-state.
- Build time.
- Producer agent ID.
- Build-host machine/OS/process architecture.
- RID or architecture where applicable.
- Source manifest from the controller.

Expected artifact families:

```text
agent-msi
winpe-iso
cloudosd-iso
wim
manifest
```

The controller stores setup artifacts under:

```text
/opt/ProxmoxVEAutopilot/autopilot-proxmox/output/setup/artifacts/
```

Promoted ISOs are copied to Proxmox ISO storage and recorded with `proxmox_volid`.

## Build Inputs

Allowed external vendor inputs:

- Microsoft Windows ISO, downloaded by the PVE init script from Microsoft's
  official software download connector or supplied by the operator from official
  Microsoft Windows download sources.
- Microsoft ADK and WinPE add-on, downloaded by the Windows build host.
- NuGet packages needed by the .NET and WiX build.
- PowerShell modules needed by CloudOSD build scripts.
- VirtIO ISO from the official virtio-win source.

Project binaries are not accepted as inputs. `AutopilotAgent.exe`, MSI, WinPE,
and CloudOSD artifacts must come from the source bundle for the current run.

## Health Checks

Build-host agent readiness from setup:

```bash
tmp=$(mktemp); curl -fsS -c "$tmp" -X POST 'http://192.168.2.115:5000/auth/local/start?next=/setup' -o /tmp/autopilot-login.html; curl -fsS -b "$tmp" 'http://192.168.2.115:5000/api/setup/v1/build-host'; rm -f "$tmp"
```

Agent work status from Postgres through PVE:

```bash
ssh pve-dev-192-168-2-252 'ssh -i /root/.local/share/proxmoxveautopilot/controller-bootstrap-ed25519 -o BatchMode=yes -o StrictHostKeyChecking=accept-new autopilot@192.168.2.115 "sudo docker exec -i autopilot-postgres psql -U autopilot -d autopilot -c \"select kind,status,error,created_at,claimed_at,completed_at from agent_work_items where agent_id='\''buildhost-100'\'' order by created_at desc limit 10;\""'
```

Promoted CloudOSD/WinPE artifacts from setup readiness:

```bash
tmp=$(mktemp); curl -fsS -c "$tmp" -X POST 'http://192.168.2.115:5000/auth/local/start?next=/setup' -o /tmp/autopilot-login.html; curl -fsS -b "$tmp" 'http://192.168.2.115:5000/api/setup/v1/readiness' | jq '.artifacts'; rm -f "$tmp"
```

## Recovery

If the build-host VM is stuck during Windows setup, inspect the Proxmox console
and verify the Windows, VirtIO, and seed ISOs are attached.

If the agent is installed but not heartbeating:

1. Confirm VMID 100 is running.
2. Confirm QGA works for the build host: `qm guest cmd 100 ping`.
3. Re-run the repair endpoint.
4. Check agent work errors in Postgres.

If artifact publish succeeds but `/setup` is not operational:

1. Re-run artifact promotion:

   ```bash
   curl -fsS -X POST 'http://192.168.2.115:5000/api/setup/v1/artifacts/promote' -H 'Content-Type: application/json' -d '{}'
   ```

2. Re-run PVE operational phase:

   ```bash
   ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase operational --resume --controller-ip 192.168.2.115 --non-interactive'
   ```

3. Refresh `/setup`.

If the source bundle bakes an old controller URL into CloudOSD media, queue a
fresh `fetch_source_bundle`, rebuild `build_cloudosd`, then `publish_artifacts`.
The source-bundle generator rewrites CloudOSD controller config to the current
`AUTOPILOT_BASE_URL`.

## Acceptance

Build-host acceptance is complete when:

- The build-host VM exists and boots.
- The agent reports `build-host` role and a fresh heartbeat.
- Work kinds outside the allowlist are rejected.
- MSI, WinPE, and CloudOSD artifacts are source-built.
- Artifacts include source/producer/SHA/RID metadata.
- Promoted ISO artifacts appear in PVE ISO storage.
- `/setup` reports artifact readiness.
- `/monitoring` shows build-host work timing rows.
