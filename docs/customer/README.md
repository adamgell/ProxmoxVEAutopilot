# Proxmox VE Autopilot Customer Guide

Proxmox VE Autopilot turns a clean Proxmox VE host into a browser-managed
Windows deployment platform.

At a high level, the product does two things:

1. Builds the Autopilot stack: Proxmox access, Ubuntu controller, Windows build
   host, deployment media, and ready-to-use artifacts.
2. Deploys Windows VMs: workstation deployments through OSDCloud and server
   deployments through OSDeploy.

## The Three Big Ideas

### 1. The installer script drives setup

Most initial setup is handled by the Proxmox console installer:

```bash
bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/install-proxmox-ve.sh
```

That installer runs the stack through three phases:

```text
Foundation -> Bootstrap -> Operational
```

The Proxmox VE host stays a bootstrapper. The long-running web app, API,
database, monitoring, MCP service, and build orchestration run inside the Ubuntu
controller VM.

### 2. Workstations and servers use different deployment paths

```text
Workstations -> OSDCloud
Servers      -> OSDeploy
```

Workstations use OSDCloud for client Windows deployment, Autopilot hash capture,
optional Intune upload, and optional domain verification.

Servers use OSDeploy for Windows Server image deployment, base readiness, and
optional server role automation such as file server, isolated domain controller,
MECM prerequisite baseline, or Lab in a Box.

### 3. Full Windows completion is proven by evidence

The system does not treat a VM as healthy just because it booted. Completion is
based on evidence such as:

- Proxmox VM identity was recorded.
- WinPE registered back to the controller.
- Windows was applied and booted from disk.
- AutopilotAgent checked in from the installed OS.
- QEMU Guest Agent is running when available.
- Autopilot hash, domain join, or server role evidence exists when selected.

## Documentation Set

- [Initial Stack Setup](INITIAL_STACK_SETUP.md)
- [Windows Deployment Workflows](WINDOWS_DEPLOYMENT_WORKFLOWS.md)
- [Operational Readiness](OPERATIONAL_READINESS.md)

## Visual References

These browser-rendered flowcharts are useful for walkthroughs:

- [Initial stack setup flowchart](../initial-stack-setup-flowchart.html)
- [Windows deployment lifecycle flowchart](../osdcloud-osdeploy-flowchart.html)

