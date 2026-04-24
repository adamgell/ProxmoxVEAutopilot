# QEMU Guest Agent — Windows on ARM64

`qemu-ga-aarch64.msi` is the Windows-on-ARM64 build of the QEMU Guest
Agent. Fedora's virtio-win and utmapp's guest-tools ship x86/x64 QGA
only; this is the ARM64 sibling.

| | |
| --- | --- |
| Source | https://github.com/adamgell/qemu-ga-aarch64-msi |
| Release | `v11.0.0-1` |
| SHA256 | `86d19e4e01156c927114a959199be665ae86133620b9cf3cf7add261801653a6` |
| Size | 2,420,736 bytes |
| QGA version | `QEMU Guest Agent 11.0.0` (tracks upstream QEMU 11.0.0 via MSYS2 `clangarm64` package `mingw-w64-clang-aarch64-qemu-guest-agent 11.0.0-1`) |
| Signed | **NO** — test-install only; Azure Trusted Signing pending |

## Why this is committed instead of fetched

The upstream repo is currently private. Committing the MSI keeps the
Ansible build self-contained and offline-capable. Once the signed AzTS
version ships publicly, switch `utm_answer_qemu_ga_msi_path` in
`inventory/group_vars/all/vars.yml` to a URL-backed download step
instead of bundling the binary.

## Refreshing

```sh
cd autopilot-proxmox/assets/qemu-ga-aarch64-win
gh release download <TAG> -R adamgell/qemu-ga-aarch64-msi \
    -p 'qemu-ga-aarch64.msi' --clobber
shasum -a 256 qemu-ga-aarch64.msi   # must match the release notes
```

Update the table above and the corresponding SHA256 check in
`roles/utm_answer_iso/tasks/main.yml` (if added later) to match.

## Install contract

`msiexec /i qemu-ga-aarch64.msi /quiet /norestart` returns 0 (success)
or 3010 (reboot required — benign during firstboot since the VM
reboots afterward anyway). The MSI registers the `QEMU-GA` service
with `Start=Automatic`. No user interaction.

## Load-bearing prerequisite

The MSI is user-mode only — it opens
`\\.\Global\org.qemu.guest_agent.0` which requires the **virtio-serial
kernel-mode driver (`vioser.sys`)** to be loaded in the guest first.
Without it, the `QEMU-GA` service sits in `StartPending` forever.

This repo's `autounattend.xml.j2` stages `DriverPaths` for `viostor`,
`NetKVM`, and (as of the QGA wire-up) `vioser` — all sourced from the
Fedora virtio-win ARM64 driver set on `virtio-win.iso`.
