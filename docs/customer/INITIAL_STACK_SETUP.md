# Initial Stack Setup

This guide explains how Proxmox VE Autopilot is created from a clean Proxmox VE
node.

## Who Does What

| Area | Responsibility |
| --- | --- |
| Operator workstation | Copies the source bundle to the Proxmox VE node and opens the web UI |
| Proxmox VE host | Runs the installer, provides storage, creates VMs, hosts ISO media |
| Ubuntu controller VM | Runs the Autopilot web app, API, database, monitor, MCP, and setup state |
| Windows build host VM | Builds Windows-only artifacts such as MSI, WinPE, and CloudOSD media |

The Proxmox VE host does not become the application server. It runs setup and
VM operations only.

## Main Setup Flow

The normal setup flow is:

```text
Copy source to PVE
  -> run install-proxmox-ve.sh
  -> Foundation
  -> Bootstrap
  -> Operational
  -> /setup ready
```

## Phase 1: Foundation

Foundation makes the Proxmox VE environment usable by Autopilot.

The installer:

- Repairs basic host prerequisites when needed.
- Creates or repairs the Proxmox API token.
- Creates the required Proxmox role and ACLs.
- Detects the Proxmox node, storage, ISO storage, network bridge, and host IP.
- Generates runtime secrets without printing them.
- Creates or discovers the Ubuntu controller VM.
- Syncs the project source and config to the controller.
- Starts the controller runtime.
- Verifies the controller health endpoint.

Foundation is complete when the controller VM exists, the controller is running,
and `/healthz` is healthy.

## Phase 2: Bootstrap

Bootstrap makes deployment inputs ready.

The installer:

- Revalidates the Proxmox token and repairs it if needed.
- Finds or downloads Windows installation media.
- Finds or downloads VirtIO driver media.
- Publishes media readiness to the controller.
- Detects or prepares build-host state.
- Creates or repairs blank VM templates used by deployment flows.

Bootstrap is complete when Windows and VirtIO media are visible to the stack and
the controller knows where they are.

## Phase 3: Operational

Operational makes the stack usable for real deployments.

The installer:

- Verifies controller health.
- Publishes current setup state to the controller.
- Promotes setup-built artifacts.
- Pulls large ISO artifacts into Proxmox storage when needed.
- Repairs token, config, or media drift from previous runs.

Operational is complete when `/setup` reports ready.

## Windows Build Host

The Windows build host is a dedicated Windows 11 x64 VM used for Windows-only
build tools.

It builds artifacts from source, including:

- AutopilotAgent MSI files.
- WinPE ISO/WIM artifacts.
- CloudOSD ISO/WIM artifacts.
- Manifests and SHA-256 metadata.

The build host is disposable. If the VM exists but the agent is stale, the
controller can repair it through QEMU Guest Agent.

## Ready State

Initial setup is ready when:

- The controller web UI is reachable.
- `/setup` reports operational readiness.
- Windows and VirtIO media are ready.
- The build-host agent has a fresh heartbeat.
- Required setup artifacts exist.
- Deployment ISOs are promoted to Proxmox ISO storage.

After this, operators can deploy Windows workstations or Windows servers from
the web UI.

