# WinPE build pipeline

Builds a custom WinPE image used by the ProxmoxVEAutopilot phase-0 agent.

## Prerequisites (on the build VM)

- Windows 11 (any edition).
- Windows ADK installed (matching the Windows version).
- WinPE add-on for ADK.
- `pwsh` (PowerShell 7) for running tests; PowerShell 5.1 also works.
- A copy of the VirtIO Win drivers extracted or mounted at `D:\virtio`,
  `F:\BuildRoot\inputs\virtio`, or `F:\BuildRoot\inputs\virtio-win`.

## Building

```powershell
.\build-winpe.ps1 -Arch amd64
```

Outputs are dropped at `F:\BuildRoot\outputs\winpe-autopilot-<arch>-<sha>.{wim,iso,json}`.
The manifest JSON records the input hashes, ADK version, and SHA-256 of the
final WIM.

## Publishing to Proxmox

```powershell
.\build-winpe.ps1 -Arch amd64
# Then upload the .iso to your Proxmox ISO storage as
# winpe-autopilot-amd64-<sha>.iso so Flask can detect it.
```
