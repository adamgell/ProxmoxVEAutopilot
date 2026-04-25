# UTM Guest Tools (Windows)

`utm-guest-tools-0.1.271.exe` is UTM's bundled guest tools installer —
an NSIS self-extractor that lays down Spice vdagent + vdservice
(clipboard sharing, dynamic-resolution display), virtio driver helpers,
and a few other goodies. Fedora's virtio-win does not include the
Spice vdagent, and we want clipboard / dynamic-resolution behavior in
the built template, so we stage this installer alongside the QGA MSI
and run it silently during firstboot.

| | |
| --- | --- |
| Source | Bundled inside `/Applications/UTM.app`; also downloadable from the utmapp/qemu GitHub release |
| Version | `0.1.271` |
| SHA256 | `839cdf4c00e5ff564f8337aba16ad7bbe8f02edd2b5c908b5daca31d88a6cdd1` |
| Size | 78,488,152 bytes (78 MB) |
| Architecture | Universal installer — carries x86, x64, and ARM64 payloads; only the ARM64 bits get installed on our templates |
| Installer type | NSIS (Nullsoft) self-extractor. Supports standard `/S` silent flag and `/D=<path>` for install directory override |
| Signed | Varies by version — check the UTM release notes |

## What it installs (ARM64)

- `vdagent.exe` + `vdservice.exe` under `%ProgramFiles%\UTM Guest Tools`
  (provides clipboard, dynamic-resolution resize via Spice vdagent
  protocol).
- `spice-webdavd-arm64-*.msi` (Spice WebDAV) — runs as a sub-installer
  for folder sharing if configured on the UTM side.
- virtio drivers if the guest doesn't already have them; our
  `DriverPaths` in `unattend.xml.j2` already seeds `viostor`, `NetKVM`,
  and `vioserial` so this layer is usually a no-op.

## Install contract

`utm-guest-tools-0.1.271.exe /S` installs silently. Exit code 0 on
success; non-zero is logged by firstboot.ps1 as a WARNING but does
NOT halt firstboot (the UX features are additive, not load-bearing).

## Why bundled instead of fetched

- The macOS UTM app bundles this installer internally; there's no
  single public URL that pins a specific version reliably.
- Template builds should be reproducible — fetching at runtime could
  drift if the UTM release updates.
- 78 MB is noticeable but tolerable for a git blob given how rarely
  this needs updating.

## Refreshing

Replace the `.exe` with a newer version from UTM's release, update
the version + SHA256 table above, and regenerate any E2E-verified
template bundles.

## Why we also bundle the QGA MSI separately

Adjacent to this dir is `assets/qemu-ga-aarch64-win/qemu-ga-aarch64.msi`
from `adamgell/qemu-ga-aarch64-msi`. UTM Guest Tools does NOT include
qemu-ga for Windows ARM64 (only Spice vdagent). The QGA MSI adds
host→guest orchestration (`utmctl ip-address`, QMP guest-exec, etc.),
which is a separate capability from Spice's clipboard + display
features. Both are installed by `firstboot.ps1`.
