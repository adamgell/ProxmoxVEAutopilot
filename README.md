# Proxmox VE Autopilot

Proxmox VE Autopilot turns a clean Proxmox VE node into a browser-managed Windows deployment platform. It creates Proxmox VMs, assigns OEM-like identity, builds and promotes deployment artifacts, tracks readiness evidence, and captures Autopilot hardware hashes without requiring direct network access to the guest OS.

![Operator dashboard](docs/screenshots/home.png?v=1093f7d)

## Current Model

The stack is split by responsibility:

| Layer | Responsibility |
| --- | --- |
| Proxmox VE host | Runs the installer, provides storage/networking, creates VMs, hosts ISO media |
| Ubuntu controller VM | Runs the web UI, API, Postgres, monitor, builder worker, MCP service, and setup state |
| Windows build host VM | Builds Windows-only artifacts such as AutopilotAgent MSI, WinPE, CloudOSD, and OSDeploy media |
| Operator browser | Drives setup, artifact promotion, deployment runs, monitoring, and recovery |

The primary deployment paths are:

| Path | Use it for | Read more |
| --- | --- | --- |
| OSDCloud Desktop | Windows client/workstation deployment, hash capture, optional Intune upload, optional domain verification | [Workstation flow](docs/customer/WINDOWS_DEPLOYMENT_WORKFLOWS.md#workstation-flow) |
| OSDeploy Server | Windows Server image deployment and optional server role automation | [Server flow](docs/customer/WINDOWS_DEPLOYMENT_WORKFLOWS.md#server-flow) |
| Task Engine v2 | Post-install orchestration claimed by AutopilotAgent after Windows boots | [Task Sequence v2](docs/customer/WINDOWS_DEPLOYMENT_WORKFLOWS.md#task-sequence-v2) |
| Legacy WinPE / Clone | Fallback image-apply and template-based workflows | [Setup guide](docs/SETUP.md) |
| Ubuntu v2 | Ubuntu desktop/server sequence experiments | [Ubuntu path](docs/SETUP.md#ubuntu-path) |

All guest work is coordinated through Proxmox APIs, QEMU Guest Agent when available, boot media, staged scripts, and the AutopilotAgent check-in path. WinRM and SSH into the deployed guest are not required for the normal Windows flow.

## Quick Start

For the current first-run path, start from the Proxmox VE node as `root`:

```bash
bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/install-proxmox-ve.sh
```

The installer drives the stack through:

```text
Foundation -> Bootstrap -> Operational -> /setup ready
```

For a non-interactive lab run with defaults:

```bash
bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/install-proxmox-ve.sh --action guided --yes --download-windows --download-virtio
```

After the controller is reachable, open:

```text
http://<controller-ip>:5000/setup
```

Use the setup console to validate Proxmox access, media readiness, Windows build-host state, artifact promotion, and operational readiness.

Detailed setup docs live in `/docs`:

- [Initial stack setup](docs/customer/INITIAL_STACK_SETUP.md)
- [Proxmox VE init reference](docs/PVE_INIT.md)
- [Windows build host runbook](docs/WINDOWS_BUILD_BOX.md)
- [First-run E2E runbook](docs/FIRST_RUN_E2E.md)
- [Operational readiness](docs/customer/OPERATIONAL_READINESS.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

The browser-friendly docs entry point is [docs/index.html](docs/index.html).

## Screenshots

| Setup | OSDCloud Desktop |
| --- | --- |
| ![Setup console](docs/screenshots/setup.png?v=1093f7d) | ![OSDCloud Desktop cockpit](docs/screenshots/osdcloud.png?v=1093f7d) |

| OSDeploy Server | Provision VMs |
| --- | --- |
| ![OSDeploy Server cockpit](docs/screenshots/osdeploy.png?v=1093f7d) | ![Provision VMs](docs/screenshots/provision.png?v=1093f7d) |

| Task Engine v2 | Monitoring |
| --- | --- |
| ![Task Engine v2](docs/screenshots/task-engine.png?v=1093f7d) | ![Monitoring](docs/screenshots/monitoring.png?v=1093f7d) |

| VM Fleet | Jobs |
| --- | --- |
| ![VM Fleet](docs/screenshots/devices.png?v=1093f7d) | ![Jobs](docs/screenshots/jobs.png?v=1093f7d) |

| Hashes | Settings |
| --- | --- |
| ![Hashes](docs/screenshots/hashes.png?v=1093f7d) | ![Settings](docs/screenshots/settings.png?v=1093f7d) |

## Operator Workflow

1. Copy this repo to the Proxmox VE node.
2. Run the console installer.
3. Finish readiness checks in `/setup`.
4. Build or repair the Windows build host when Windows-only artifacts are needed.
5. Promote CloudOSD or OSDeploy artifacts into Proxmox ISO storage.
6. Launch workstation runs from OSDCloud Desktop or server runs from OSDeploy Server.
7. Watch `/monitoring`, run details, `/jobs`, and the VM fleet until the selected readiness evidence is complete.

See [Windows deployment workflows](docs/customer/WINDOWS_DEPLOYMENT_WORKFLOWS.md) for the evidence expected from workstation and server runs.

## Readiness Signals

A VM is not considered complete simply because it booted. Depending on the selected workflow, completion can require:

- WinPE or CloudOSD registration.
- Image apply completion.
- Installed Windows boot.
- AutopilotAgent heartbeat from the installed OS.
- QEMU Guest Agent status when available.
- Hardware hash capture and upload status when selected.
- Domain verification when selected.
- Server role completion when selected.

The readiness model is documented in [Operational readiness](docs/customer/OPERATIONAL_READINESS.md).

## Main UI Surfaces

| Surface | Purpose |
| --- | --- |
| `/setup` | First-run and operational readiness console |
| `/osdcloud` | Windows desktop deployment cockpit |
| `/osdeploy` | Windows Server deployment cockpit |
| `/provision` | Unified launch form for OSDCloud, OSDeploy, legacy WinPE, clone, and Ubuntu paths |
| `/task-engine` | Task Sequence v2 templates, builder, and post-install plan surface |
| `/vms` | Proxmox VM fleet and VM actions |
| `/cloud` | Entra, Intune, and Autopilot device view |
| `/monitoring` | Deployment evidence, AD/Entra/Intune status, and operator health |
| `/jobs` | Background job history and logs |
| `/answer-isos` | Answer ISO inventory and cleanup |
| `/settings` | Proxmox, deployment, build-host, and monitoring settings |

## Local Helper

This repo includes `skill.sh` for Codex/MCP-assisted work against the live stack:

```bash
./skill.sh status
./skill.sh docs "CloudOSD OSDeploy"
./skill.sh read <doc_id> 8000
./skill.sh shell
```

The helper manages the MCP tunnel and token-injecting proxy. It must not print or commit the MCP bearer token.

## Experimental Mac/UTM Path

The repo still includes an experimental UTM + QEMU path for Windows ARM64 VMs on Apple Silicon. It requires running the web service natively on macOS because `utmctl` is a host binary.

Start here: [UTM macOS setup](docs/UTM_MACOS_SETUP.md).

## OEM Profiles

Built-in OEM profiles live in `autopilot-proxmox/files/oem_profiles.yml`. They set SMBIOS type 1 fields and manufacturer-style serial prefixes for Lenovo, Dell, HP, Microsoft Surface, and generic Proxmox desktop/laptop identities.

## More Documentation

- [Customer guide](docs/customer/README.md)
- [Setup guide](docs/SETUP.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Python versions](docs/PYTHON_VERSIONS.md)
- [Autopilot Proxmox developer README](autopilot-proxmox/README.md)
