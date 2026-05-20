# Operational Readiness

This page describes what "ready" means for Proxmox VE Autopilot.

## Stack Ready

The stack is ready when initial setup has completed and `/setup` reports
operational readiness.

Required evidence:

- Proxmox VE API access is valid.
- The Ubuntu controller VM is running.
- The controller health endpoint is healthy.
- Docker Compose services are running inside the controller VM.
- Windows media is ready.
- VirtIO media is ready.
- The Windows build host agent has a fresh heartbeat.
- Required setup artifacts exist.
- Deployment ISOs are promoted to Proxmox ISO storage.

## Controller Services

The controller VM runs the long-lived application services:

- Web UI and API.
- MCP service.
- Postgres database.
- Monitor service.
- Builder worker service.

The Proxmox VE host should not run the Autopilot Docker stack.

## Build Host Ready

The build host is ready when:

- The Windows build-host VM exists.
- The build-host agent reports `phase=build-host`.
- The build-host agent reports `role=build-host`.
- The heartbeat is fresh.
- The identity is approved.
- Build work outside the allowlist is rejected.

## Artifact Ready

An artifact is ready when:

- It was built from the current source bundle.
- It has source and producer metadata.
- It has file size and SHA-256 metadata.
- It was uploaded back to the controller.
- It was promoted into Proxmox ISO storage when needed.
- The controller has a Proxmox volume ID for the ISO.

## Workstation Deployment Ready

A workstation deployment is ready to launch when:

- A CloudOSD artifact is published.
- The selected client Windows option is supported.
- Required assets exist.
- Proxmox node, ISO storage, disk storage, and network bridge are available.
- VM name and VMID do not collide with an existing VM.

## Server Deployment Ready

A server deployment is ready to launch when:

- An OSDeploy Server artifact is published.
- The artifact matches the requested Server OS metadata.
- Required VirtIO media is configured.
- Proxmox node, ISO storage, disk storage, and network bridge are available.
- Memory and disk size meet Server minimums.
- Selected role options are valid.

## Completion Ready

A deployed VM is complete only when the selected workflow has posted enough
evidence.

For workstations, evidence can include OSDCloud completion, AutopilotAgent
heartbeat, hardware hash capture, upload status, domain verification, and QGA
status.

For servers, evidence can include image apply, VirtIO driver injection, full-OS
heartbeat, QGA status, and server role status.

