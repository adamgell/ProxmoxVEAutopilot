# Windows Deployment Workflows

Proxmox VE Autopilot supports two primary Windows deployment workflows:

```text
Workstations -> OSDCloud
Servers      -> OSDeploy
```

Both workflows follow the same broad lifecycle:

```text
Choose deployment type
  -> pass preflight checks
  -> create a run
  -> clone a blank Proxmox VM
  -> boot WinPE
  -> install Windows
  -> boot installed Windows
  -> AutopilotAgent checks in
  -> optional post-install tasks run
  -> readiness is reported
```

## Workstation Flow

Use the workstation flow for client Windows deployments.

The system uses OSDCloud to deploy Windows, while Proxmox VE Autopilot manages:

- Proxmox VM creation.
- VM identity, VMID, UUID, and MAC tracking.
- OSDCloud media and cache support.
- First-boot script staging.
- AutopilotAgent installation.
- Autopilot hash capture.
- Optional Intune/Autopilot upload.
- Optional AD domain verification.
- Readiness evidence.

### What The Operator Chooses

The operator chooses workstation deployment settings such as:

- Windows version, edition, language, and activation type.
- VM name, CPU, memory, disk, storage, and network.
- Optional group tag.
- Optional domain join settings.
- Optional OEM/profile settings when available.

### What The System Does

The system:

1. Checks that the selected artifact and Proxmox target are usable.
2. Creates a CloudOSD run.
3. Clones a blank VM.
4. Boots CloudOSD WinPE.
5. Downloads the run package from the controller.
6. Runs OSDCloud to deploy Windows.
7. Stages drivers, SetupComplete, OSD client files, and AutopilotAgent.
8. Boots the installed Windows OS.
9. Waits for AutopilotAgent to check in.
10. Runs optional hash capture and domain verification.

### Workstation Complete Means

A workstation run is complete when the controller has enough evidence for the
selected path. That may include:

- WinPE registration.
- OSDCloud started and finished.
- Offline Windows validation passed.
- Installed Windows booted.
- AutopilotAgent heartbeat arrived.
- Hardware hash was captured when selected.
- Hash upload status is known when upload is configured.
- Domain membership was verified when domain join is configured.

## Server Flow

Use the server flow for Windows Server deployments.

The system uses OSDeploy to apply the Server image, while Proxmox VE Autopilot
manages:

- OSDeploy artifact build and publish.
- Proxmox VM creation.
- VM identity, VMID, UUID, and MAC tracking.
- WinPE image apply.
- VirtIO driver injection.
- SetupComplete and unattend staging.
- Full-OS AutopilotAgent check-in.
- Server role automation.
- Server readiness evidence.

### What The Operator Chooses

The operator chooses server deployment settings such as:

- Server OS version and edition.
- VM name, CPU, memory, disk, storage, and network.
- Server role.
- Role-specific options.
- Optional domain join settings for roles that require them.

### Server Roles

Supported server role branches include:

| Role | What it does |
| --- | --- |
| Windows Server Base | Deploys a base Server VM and verifies the agent/guest state |
| File Server | Creates the file server role, share path, ACLs, and SMB share |
| Isolated Domain Controller | Promotes a new isolated AD DS/DNS forest and verifies it |
| MECM Prereq Baseline | Installs baseline Windows features for a future MECM site server |
| Lab in a Box | Creates a multi-VM lab sequence: domain controller, joined file server, joined MECM prereq VM |

### What The System Does

The system:

1. Checks build host and artifact readiness.
2. Checks the requested server role and role options.
3. Creates an OSDeploy run.
4. Clones a blank VM.
5. Boots OSDeploy WinPE.
6. Applies the Server image.
7. Injects VirtIO drivers.
8. Stages unattend, SetupComplete, OSD client files, and AutopilotAgent.
9. Boots the installed Windows Server OS.
10. Waits for AutopilotAgent to check in.
11. Runs optional full-OS role steps.

### Server Complete Means

A base server run is complete when:

- Installed Windows Server boots.
- AutopilotAgent is online.
- QEMU Guest Agent status is known.
- Server role status is `base_ready`.

A role-based server run is complete when the final selected role step reports
success. Until then, it may show as role pending.

## Domain Join

Domain join can happen in two ways:

- Workstations can stage domain join settings into offline Windows during the
  OSDCloud deployment.
- Servers can join a domain as a full-OS role step after Windows Server boots.

In both cases, completion depends on telemetry from AutopilotAgent. The agent
reports whether the machine is domain joined and which domain name Windows sees.

## Task Sequence v2

Task Sequence v2 is the post-install orchestration engine.

The controller creates the task plan when a run is created. After Windows boots,
AutopilotAgent claims supported work items from the controller and posts logs and
results back.

Task Sequence v2 is used for:

- Autopilot hash capture.
- Heartbeat verification.
- Domain verification.
- Server role automation.
- Reboot-aware full-OS steps.

