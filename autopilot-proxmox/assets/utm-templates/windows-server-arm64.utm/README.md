# windows-server-arm64.utm — Skeleton Bundle

> ⚠️ **DO NOT open this bundle directly in UTM.app.**
>
> This is a skeleton template. All placeholder UUIDs and disk/ISO paths must be
> substituted by Ansible before the bundle is loaded in UTM. Opening it as-is
> will create a broken VM named `SKELETON-DO-NOT-USE` with no disk or ISO wired up.

## What this is

A pre-validated UTM bundle skeleton for **Windows Server ARM64** on Apple Silicon.
It ships with:

- `config.plist` — XML plist with placeholder values (see below)
- `Data/` — empty directory; Ansible places the qcow2 disk and ISO symlinks here at build time

## Placeholder values (Ansible substitutes these)

| Field                   | Placeholder value                          | Ansible action |
|-------------------------|--------------------------------------------|----------------|
| `Information.UUID`      | `00000000-0000-0000-0000-000000000000`     | Replace with `uuidgen` output |
| `Information.Name`      | `SKELETON-DO-NOT-USE`                       | Replace with VM name |
| `Drive[0].Identifier`   | `00000000-0000-0000-0000-000000000001`     | Replace with `uuidgen` output |
| `Drive[0].ImageName`    | *(absent)*                                  | Insert with installer ISO filename |
| `Drive[1].Identifier`   | `00000000-0000-0000-0000-000000000002`     | Replace with `uuidgen` output |
| `Drive[1].ImageName`    | `SYSTEM_DISK_PLACEHOLDER.qcow2`            | Replace with `<disk-uuid>.qcow2` |
| `Drive[2].Identifier`   | `00000000-0000-0000-0000-000000000003`     | Replace with `uuidgen` output |
| `Drive[2].ImageName`    | *(absent)*                                  | Insert with answer ISO filename (optional) |
| `Network[0].MacAddress` | *(absent)*                                  | UTM auto-generates on first load |

## VM configuration

| Parameter     | Value           |
|---------------|-----------------|
| Architecture  | aarch64         |
| Machine type  | virt            |
| CPU           | default (host)  |
| vCPUs         | 4               |
| RAM           | 8192 MB         |
| UEFI          | Yes             |
| TPM 2.0       | Yes             |
| GPU           | virtio-ramfb-gl |
| NIC           | virtio-net-pci  |
| Hypervisor    | Apple HVF       |
| Icon          | `windows` (no verified `server` icon slug in UTM 4.x) |

## Build-time requirements

- macOS on Apple Silicon (Hypervisor.framework)
- UTM 4.x installed
- `plutil` (built into macOS)
- `qemu-img` for disk creation
- Windows Server ARM64 ISO (2022 or 2025)

See `docs/UTM_BUNDLE_FORMAT.md` for the full schema reference and `plutil` command examples.
