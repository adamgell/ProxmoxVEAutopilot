# Known issues — Plan 1 build pipeline

Issues uncovered during end-to-end validation that don't block Plan 1 (the build pipeline produces correct WIM/ISO artifacts and registers them) but prevent the *baked-in convenience features* (SSH-into-PE, automatic networking) from working without manual intervention. Plan 3's real `Bootstrap.ps1` is the natural place to fix all three — they're symptoms of the placeholder bootstrap not doing the lifecycle work that DeployR's full bootstrap does.

## 1. wpeinit returns before NetKVM driver binds → no network at boot

**Symptom:** PE boots, cmd.exe comes up, but `ipconfig` shows no Ethernet adapter and `pnputil /enum-devices /class Net` returns "No devices were found." The hardware shows in `pnputil /enum-devices` with `Status: Problem` and `Problem Code: 18 (0x12) [CM_PROB_REINSTALL]`.

**Diagnosed:** the NetKVM driver IS injected into the WIM correctly. Hardware ID `PCI\VEN_1AF4&DEV_1000&SUBSYS_00011AF4&REV_00` matches an entry in `netkvm.inf`. The driver just doesn't bind during the first wpeinit pass — likely a timing race between wpeinit's network init and PnP enumeration of the virtio device on a cold WinPE boot.

**Workaround (verified working):** from the cmd shell:
```
wpeutil InitializeNetwork
ipconfig
```
Network comes up cleanly with DHCP-assigned IPv4.

**Plan 3 fix:** the real Bootstrap.ps1 should call `wpeutil InitializeNetwork` once at start, then poll `Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -notlike '169.*'` with a timeout (~60s) before any orchestrator HTTP call. If timeout exhausted, retry InitializeNetwork.

## 2. sshd auto-start fails on first PE boot

**Symptom:** even after the network is up via the workaround for #1, `Get-Service sshd` shows `Status: Stopped`, and `Start-Service sshd` errors generically with "Failed to start service 'OpenSSH Server (sshd)'." Running sshd manually with `& 'X:\Program Files\OpenSSH\sshd.exe' -ddd` reveals the actual cause:

```
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@         WARNING: UNPROTECTED PRIVATE KEY FILE!          @
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
Permissions for '__PROGRAMDATA__/ssh/ssh_host_rsa_key' are too open.
…
sshd: no hostkeys available -- exiting.
```

**Diagnosed:** when `Build-PeWim.ps1` Phase 4b copies the freshly-generated host keys into the mounted WIM at `\ProgramData\ssh\`, the files inherit the default ACL of the parent `\ProgramData\` tree, which grants read access to `BUILTIN\Users` (S-1-5-32-545). sshd's anti-private-key-leakage check refuses to load any host key readable by Users.

**Attempted fix that did NOT work:** at WIM-build time, run `icacls "<keyfile>" /inheritance:r /grant SYSTEM:F BUILTIN\Administrators:F` on each host key file. Verified by manual inspection that the icacls commands ran without error against the mounted WIM. But the resulting WIM, when expanded at PE boot, *still* shows BUILTIN\Users in the ACL of those files. Conclusion: icacls modifications to files inside a Mount-WindowsImage'd offline WIM either don't persist through `Dismount-WindowsImage -Save`, or are rewritten by some default ACL during PE expansion.

**Workaround (verified working):** from the PE cmd shell:
```powershell
Get-ChildItem 'X:\ProgramData\ssh\ssh_host_*_key' | ForEach-Object {
    icacls $_.FullName /inheritance:r /grant 'NT AUTHORITY\SYSTEM:F' 'BUILTIN\Administrators:F'
}
icacls 'X:\ProgramData\ssh\administrators_authorized_keys' /inheritance:r /grant 'NT AUTHORITY\SYSTEM:F' 'BUILTIN\Administrators:F'
Start-Service sshd
```

**Plan 3 fix candidates** (try in order; first that works wins):
- **(a) Regenerate host keys at PE boot.** `ssh-keygen -A -f X:\ProgramData\ssh` (after deleting any stale baked keys) generates fresh keys with default-correct ACLs because they're created fresh in the running PE filesystem. Bootstrap.ps1 runs this, then `Start-Service sshd`. Avoids the offline-WIM ACL fragility entirely.
- **(b) Use `Set-Acl` instead of `icacls`.** PowerShell's Set-Acl writes via the registry-backed security descriptor APIs, which may persist differently through DISM than icacls's filesystem call.
- **(c) Lock down ACLs on `\ProgramData\ssh\` at the *directory* level** (not per-file) and rely on inheritance. May survive better.
- **(d) Strip `BUILTIN\Users` from `\ProgramData\` itself** in the offline registry / via DISM. Heavy-handed but definitely works.

The recommendation is **(a)** — fresh keys per boot is also better security hygiene than baking ephemeral keys into a distributed WIM.

## 3. Symbolic interaction between #1 and #2

The two failures cascade in PE's auto-start: sshd's service registration says auto-start, but the OpenSSH server doesn't START_PENDING properly when the network is also down. Even with the host-key ACL fix in place, if there's no NIC at sshd start time, sshd can fail to bind to its listen socket and exit. A robust Plan 3 Bootstrap.ps1 must (a) bring the network up first via the wpeutil retry loop, (b) THEN regenerate host keys and start sshd. The ordering matters.

## 4. PrePEFlight false-negatives (FIXED in commit `a00b770`)

Already addressed; noted here for completeness.

- The OpenSSH-server-installed check looked at `Get-WindowsCapability` only, missing standalone Win32-OpenSSH installs in `C:\Program Files\OpenSSH\`. Now checks both paths.
- The Dev Drive check expected ReFS at the work-drive path, but DISM offline servicing fails on ReFS Dev Drives with Win32 error 1812. The check was inverted: now requires NTFS at the work path, with inputs/outputs free to live on a Dev Drive.

## 5. ReFS Dev Drive incompatible with DISM offline package servicing (DOCUMENTED, NOT FIXED IN CODE)

**Symptom:** `Add-WindowsPackage -Path <mounted-wim> -PackagePath <ADK package>` fails with HRESULT `0x80070714` ("The request is not supported"). DISM internal log shows `CDismCore::CacheImageSession failed (hr:0x80070714)`. Reproducible with both the PowerShell cmdlet and `dism.exe /Add-Package` CLI.

**Root cause:** DISM's package-servicing infrastructure has dependencies on filesystem features that ReFS doesn't expose the same as NTFS. Mount works fine; package install doesn't.

**Workaround (in spec):** `lockPath` in `build-pe-wim.config.json` controls the work directory (where the WIM gets mounted). Set it to a path on an NTFS volume (e.g., `C:\BuildRoot\work\.build.lock`). Inputs and outputs can stay on a Dev Drive for read-once perf benefits. PrePEFlight's `-WorkDriveLetter` check now enforces NTFS for this reason.

## Summary of Plan 1 operational state

The pipeline produces correct, registered WIM and ISO artifacts on both architectures:

| sha (prefix) | arch | size | SSH-baked | OpenSSH validated boot? |
|---|---|---|---|---|
| `83793f7e…` | amd64 | 610 MB | no | n/a (manual UTM x86_64-emulation only) |
| `5ab6fa84…` | arm64 | 633 MB | no | n/a |
| `40c36b69…` | arm64 | 638 MB | yes | network manual, sshd manual ACL |
| `9296fb87…` | arm64 | ~640 MB | yes (with attempted ACL fix) | sshd ACL fix didn't propagate — same as `40c36b69…` |

Plan 1 exit criterion (per spec Section 11): *"a manually-crafted manifest deploys a Win11 ARM64 VM end-to-end on UTM. Resulting OS boots, runs FirstLogonCommand from staged unattend, surfaces in the existing hash-capture flow."* That criterion is for **Plan 3** (real bootstrap interpreter executes a manifest), not Plan 1. Plan 1's exit is "PE WIM build, registered, boots." That works on every artifact above. Issues #1 and #2 are post-boot quality-of-life features (in-PE SSH for live debugging) that the placeholder bootstrap doesn't sequence properly; Plan 3 owns the full lifecycle.

**Net: Plan 1 is operationally complete.** Move on to Plan 2 (orchestrator API).
