# LabZ1 SmartDeploy + WDS PXE Runbook

## Goal

Build a manual but repeatable LabZ1 test path for the customer SmartDeploy/WDS
automation work:

```text
ProxmoxVEAutopilot provisions Windows Server 2025
  -> server joins test.gell.one
  -> AutopilotAgent is online and ready for commands
  -> operator configures WDS + SmartDeploy
  -> blank UEFI x64 VM PXE boots from WDS
  -> SmartDeploy applies Windows 11
  -> deployed client joins test.gell.one
```

This runbook deliberately proves the manual Windows steps before they are folded
into ProxmoxVEAutopilot role automation.

## Lab Boundary

- Domain: `test.gell.one`
- Existing domain/DNS/DHCP server: `LABZ1-DC01`
- New WDS + SmartDeploy server: `LABZ1-SD01`
- PXE test client: `LABZ1-PXE01`
- Network: same Proxmox bridge/VLAN/subnet for DC, WDS server, and PXE client
- DHCP placement: `LABZ1-DC01`
- WDS placement: `LABZ1-SD01`

Because DHCP and WDS are on separate servers and all PXE participants are on the
same broadcast domain, v1 does not configure DHCP options 60, 66, or 67.

## Operator Split

Use the Mac/Codex session for:

- Repo edits.
- ProxmoxVEAutopilot automation changes.
- Runbook updates.
- Live Proxmox/API checks.

Use the Windows 11 devbox/Codex session for:

- PowerShell execution against `LABZ1-SD01`.
- Windows-only tooling.
- SmartDeploy installer and media creation checks.
- WDS/DISM validation commands.

## Inputs To Fill In

Record these before starting the Windows-side work:

```powershell
$DomainFqdn = 'test.gell.one'
$DomainNetbios = 'TEST'
$DcName = 'LABZ1-DC01'
$WdsServerName = 'LABZ1-SD01'
$WdsServerIp = '192.168.16.100'
$SmartDeployRoot = 'E:\SmartDeploy'
$RemoteInstallRoot = 'E:\RemoteInstall'
$Windows11IsoPath = 'E:\ISO\en-us_windows_11_business_editions_version_25h2_updated_jan_2026_x64_dvd_09c1e011.iso'
$SmartDeployInstallerPath = '<path-to-smartdeploy-installer>'
$SmartDeployLicenseOrAccount = '<document-manual-step-or-secret-reference>'
$ClientOuDn = '<OU-for-deployed-clients>'
$DomainJoinCredentialRef = '<credential-reference-or-account-name>'
```

Do not commit real passwords, SmartDeploy account secrets, or license material.

## Current LabZ1 State

Last verified: `2026-07-08`.

- `LABZ1-SD01` is VMID `122` on Proxmox bridge `labz1`.
- `LABZ1-SD01` is reachable at `192.168.16.100`.
- `LABZ1-DC01` is the LabZ1 DC/DNS/DHCP server at `192.168.16.10`.
- OSDeploy run `3cb9aa71-445f-4951-8529-e983a61b3717` is complete.
- AutopilotAgent heartbeat reports `domain_joined=true` and
  `domain_name=test.gell.one`.
- The OSDeploy readiness row is complete with `server_role_status=base_ready`.
- Proxmox disk `scsi1` is `nvmepool:vm-122-disk-0`, `300G`.
- Windows volume `E:` is formatted as `DeployData`.
- `E:\SmartDeploy`, `E:\RemoteInstall`, and these SmartDeploy data folders
  exist:
  - `E:\SmartDeploy\Answer Files`
  - `E:\SmartDeploy\Application Packs`
  - `E:\SmartDeploy\Boot Media`
  - `E:\SmartDeploy\Deployment Packages`
  - `E:\SmartDeploy\Images`
  - `E:\SmartDeploy\Logs`
  - `E:\SmartDeploy\Platform Packs`
  - `E:\SmartDeploy\Reference Machines`
  - `E:\SmartDeploy\Scratch`
  - `E:\SmartDeploy\User States`
- WDS is installed and initialized to `E:\RemoteInstall`.
- `WDSServer` is running and the `REMINST` share points to
  `E:\RemoteInstall`.
- WDS policy is configured for the lab:
  - `Answer clients: Yes`
  - Known and new client PXE prompt policy: `NoPrompt`
  - WDS client logging: `Enabled: Yes`, `Logging level: Info`
  - DHCP service on SD01: `Not Installed`
  - DHCP option 60: `<Not Applicable>`
  - Default boot images are still blank until the SmartDeploy boot WIM is
    imported.
- SmartDeploy is installed:
  - Display name: `SmartDeploy`
  - Version: `3.0.2060.1239`
  - Publisher: `SmartDeploy`
  - Install location: `C:\Program Files\SmartDeploy\SmartDeploy\`
  - Services running: `SDApiService`, `SDWebToolsService`
- The installer did not prompt for a drive. This is expected for the app
  install path: SmartDeploy application binaries are under `C:\Program
  Files\SmartDeploy\SmartDeploy`, while the SmartDeploy content directory is
  configured separately.
- SmartDeploy content directory is already set to `E:\SmartDeploy` in
  `C:\Program Files\SmartDeploy\SmartDeploy\Resources\Configuration\SmartDeploy.config`:
  `<SmartDeployDirectory>E:\SmartDeploy</SmartDeployDirectory>`.
- SmartDeploy repository inventory after first launch:
  - `E:\SmartDeploy\Logs\SmartDeploy.WebTools.log` exists.
  - `E:\SmartDeploy\Images` now contains the SmartDeploy Capture Wizard image
    `Win11-25H2-x64-vmware.wim`.
  - `E:\SmartDeploy\Answer Files` is empty.
  - `E:\SmartDeploy\Platform Packs` is empty.
  - `E:\SmartDeploy\Boot Media` is empty.
- SmartDeploy WebTools now uses the E: repository path. The earlier
  `C:\SmartDeploy\Images` import was a canceled false start; the current
  `C:\SmartDeploy\Images` folder is empty. The later raw ISO
  `Win11-25H2-Business-x64-install.wim` entry was also removed from
  `E:\SmartDeploy\Images`.
- SmartDeploy WebTools may still show stale image inventory after local file
  changes. On `2026-07-08`, delete attempts for the raw ISO WIM failed in
  `SmartDeploy.WebTools.log` while deleting the API/cloud asset record with a
  `NullReferenceException`, even though the local file was gone. Refresh the UI
  or restart SmartDeploy services before trusting the Images grid.
- SmartDeploy shares were retargeted to the E: repository after the installer
  initially created them on `C:\SmartDeploy`:
  - `SDShare` -> `E:\SmartDeploy`
  - `SDUserStateShare` -> `E:\SmartDeploy\User States`
  - `SDShare` grants `LABZ1-SD01\SDShareUser` read and
    `BUILTIN\Administrators` change.
  - `SDUserStateShare` grants `LABZ1-SD01\SDShareUser` change.
  - `E:\SmartDeploy\User States` grants `LABZ1-SD01\SDShareUser` modify.
  - Evidence log:
    `E:\SmartDeploy\Logs\LabZ1-ShareRetarget-20260708-063622.txt`.
  - `\\localhost\SDShare\Images\Win11-25H2-Business-x64-install.wim`
    resolves successfully.
- Windows 11 25H2 business ISO is staged at
  `E:\ISO\en-us_windows_11_business_editions_version_25h2_updated_jan_2026_x64_dvd_09c1e011.iso`.
- Full `sources\install.wim` from that ISO has been staged to
  `E:\SmartDeploy\Images\Win11-25H2-Business-x64-install.wim`.
- Staged WIM length is `7329228036` bytes.
- Source and target WIM SHA-256 both equal
  `73DABCC86BF540F83B788A18A54DBBEB4798359E6F6E26830EC264E27D0141E5`.
- WIM metadata sidecar:
  `E:\SmartDeploy\Images\Win11-25H2-Business-x64-install.metadata.json`.
- Critical image-format finding from `2026-07-08`:
  - The raw Microsoft ISO `install.wim` can be staged and hashed, and
    SmartDeploy can read its normal WIM metadata.
  - It is not accepted by the SmartDeploy Answer File Wizard as a deployment
    image. The wizard displays: the selected WIM does not appear to have been
    created with SmartDeploy Capture Wizard.
  - Product docs for creating answer files, capturing images, and WDS
    integration all route through a SmartDeploy Capture Wizard-created image
    WIM before creating answer files or WDS install-image workflows:
    <https://smartdeploy.pdq.com/hc/en-us/articles/12982126240923-Create-an-Answer-File>,
    <https://smartdeploy.pdq.com/hc/en-us/articles/12982110366747-Capture-an-Image-from-a-Virtual-Disk-File>,
    <https://smartdeploy.pdq.com/hc/en-us/articles/12982168950043-Integrate-SmartDeploy-with-Windows-Deployment-Services>.
  - Customer-side `DeployWizard.log` comparison shows why: the working
    `W11_25H2_x64.wim` contains a `<CUSTOMDATA><SMARTDEPLOY VERSION=...>`
    block with disk and partition layout metadata. A plain ISO `install.wim`
    does not.
  - Local SmartDeploy binary inspection confirmed the validation path:
    `SelectImageModel.Validate()` rejects the image when
    `CurrentWindowsImageModel.CustomData` is null and raises
    `SDError.WimNotCreatedBySmartDeploy`.
- Repo helper staged on SD01:
  `E:\SmartDeploy\Scratch\ProxmoxVEAutopilot\scripts\New-SmartDeployWindowsRelease.ps1`.
  The staged helper SHA-256 is
  `89E7CE115F06067C822FAE95D6EA7BDCC0902CE2197833F494AC18BB336C4B30`, and it
  parses cleanly in Windows PowerShell.
- The staged helper was validated on SD01 with `-PlanOnly`; stdout was valid
  JSON, selected Windows 11 Enterprise image index `3`, found `10` total ISO
  images, and reported expected boot WIM
  `E:\SmartDeploy\Boot Media\Win11-25H2-Business-x64-boot.wim`.
  Current plan output also reports `target_image_for_answer=null`,
  `raw_iso_wim_answer_file_compatible=false`, and
  `requires_smartdeploy_capture_wizard_image=true` until a
  Capture Wizard-created WIM path is provided.
- The ISO contains these indexes:
  - `1` Windows 11 Education
  - `2` Windows 11 Education N
  - `3` Windows 11 Enterprise
  - `4` Windows 11 Enterprise N
  - `5` Windows 11 Pro
  - `6` Windows 11 Pro N
  - `7` Windows 11 Pro Education
  - `8` Windows 11 Pro Education N
  - `9` Windows 11 Pro for Workstations
  - `10` Windows 11 Pro N for Workstations
- The local SmartDeploy extracted-tree zip was staged for inspection at
  `E:\SmartDeploy\Scratch\SmartDeploy-20260708.zip`.
- The staged zip SHA-256 is
  `FC4114B1F9225E67A00868994CFD916DCDFF7C803A7876AD5B2BA77CC4A71495`.
- The zip was extracted to
  `E:\SmartDeploy\Scratch\SmartDeployExtracted`.
- The extracted tree is useful for inspection, but it is not a substitute for
  the official SmartDeploy install/licensing path.
- SmartDeploy installer observation:
  - Earlier `SDSetup.msi` transaction for SmartDeploy `3.0.2060.1239` ended
    with MSI status `1602` and `Product: SmartDeploy -- Installation operation
    failed.`
  - A later `SDESetup.exe` run started another `SDSetup.msi` transaction.
  - The later transaction completed successfully with MSI status `0`.
- `LABZ1-PXE01` has been created and left powered off:
  - Lab bubble asset ID: `59e0cbef-46e1-49f2-a218-6efba85ebedf`
  - VMID: `123`
  - Firmware: OVMF/UEFI, Secure Boot off
  - Machine: `pc-q35-10.1`
  - NIC: `virtio`, bridge `labz1`
  - MAC: `BC:24:11:74:10:37`
  - Disk: `nvmepool:vm-123-disk-1`, `64G`
  - Boot order: `net0;scsi0`

Management notes from the live setup:

- QEMU Guest Agent was good for short commands, but became unreliable after
  long WDS commands. Prefer WinRM or an AutopilotAgent role step for ongoing
  Windows-side work.
- Direct `WDSUTIL.exe /Get-Server /Show:Config` inside an NTLM WinRM session
  returned `0x4DC` even though the session was admin. Launching `WDSUTIL.exe`
  locally through a temporary scheduled task running as the LabZ1 domain admin
  worked and produced the authoritative WDS config.
- `WDSUTIL.exe /Set-Server /AnswerClients:All` from QGA/SYSTEM returned
  `0x5 Access is denied`. Treat this as a credential/context problem, not a
  failed WDS installation.
- `MediaWizard.exe /?`, `MediaWizard.exe --help`, and `MediaWizard.exe /help`
  behaved like GUI launches, not CLI help. The escaped processes were killed.
- `SDCommandPrompt.cmd` exposes `SMARTWIM` and `SMARTVDK` VBS/COM scripting
  samples. `SMARTWIM` covers WIM operations such as `APPEND`, `APPLY`,
  `CAPTURE`, `DELETE`, `DELTA`, `EXPORT`, `INFO`, `MOUNT`, `SPLIT`, and
  `UNMOUNT`; it does not appear to create SmartDeploy WDS boot media.
- SmartDeploy local API notes:
  - `SDApiService` listens on `https://localhost:8080`; `/health` returns
    `Healthy`.
  - Direct unauthenticated collection reads return `401` or `404`, so do not
    automate writes through the API until the supported auth flow is known.
  - `SmartDeploy.WebTools.log` showed successful local `200` responses while
    registering the local distribution point.
- SmartDeploy answer-file credential notes:
  - A previous `SmartDeploy.xml` proves that `image_file`,
    `organizational_unit`, and UNC path fields are normal XML and can be safely
    updated by the repo helper.
  - Domain-join, share, and proxy passwords are stored as SmartDeploy-encrypted
    element text.
  - Do not hand-write plaintext passwords into a SmartDeploy answer file unless
    a tested SmartDeploy-supported encryption/import path is found.

Needed before the next end-to-end PXE attempt:

- SmartDeploy license/sign-in state confirmed in the console.
- Lab answer file under `E:\SmartDeploy\Answer Files`.
- Lab answer file under `E:\SmartDeploy\Answer Files` that points to
  `\\LABZ1-SD01\SDShare\Images\Win11-25H2-x64-vmware.wim`.
- Required Proxmox/Windows platform pack under `E:\SmartDeploy\Platform Packs`.
- SmartDeploy WDS boot WIM under `E:\SmartDeploy\Boot Media`.

## Phase 1: Launch LABZ1-SD01 With OSDeploy

Use the existing OSDeploy Server flow to create the bare Windows Server 2025 VM.
The first target is not WDS automation yet. The first target is a healthy,
domain-joined Server 2025 VM with AutopilotAgent online.

Run these commands from a machine that can reach the ProxmoxVEAutopilot
controller. Replace `<controller-ip>` and the fill-in values first.

```bash
BASE='http://<controller-ip>:5000'
COOKIE="$(mktemp)"
curl -fsS -c "$COOKIE" -X POST "$BASE/auth/local/start?next=/osdeploy" -o /tmp/autopilot-login.html
```

Find a ready OSDeploy artifact:

```bash
curl -fsS -b "$COOKIE" "$BASE/api/osdeploy/v1/artifacts?architecture=amd64" |
  jq '.artifacts[] | {id, ready, os_version, os_edition, image_name, proxmox_volid}'
```

Find Proxmox launch defaults:

```bash
curl -fsS -b "$COOKIE" "$BASE/api/osdeploy/v1/proxmox/options" | jq
```

Optional: find the LabZ1 bubble ID if you want the server tracked in the lab
asset model immediately:

```bash
curl -fsS -b "$COOKIE" "$BASE/api/lab-bubbles" |
  jq '.bubbles[] | {id, name, domain_name}'
```

Create `labz1-sd01-payload.json`:

```json
{
  "artifact_id": "<ready-osdeploy-artifact-id>",
  "vm_name": "LABZ1-SD01",
  "node": "<proxmox-node>",
  "iso_storage": "<iso-storage>",
  "storage": "<disk-storage>",
  "network_bridge": "labz1",
  "architecture": "amd64",
  "server_role": "base",
  "os_version": "Windows Server 2025",
  "os_edition": "Datacenter",
  "os_language": "en-us",
  "vm_cores": 4,
  "vm_memory_mb": 12288,
  "vm_disk_size_gb": 160,
  "secure_boot": false,
  "outbound_policy": {"mode": "blocked"},
  "role_options": {
    "domain_join": {
      "credential_id": "<domain-join-credential-id>",
      "domain_fqdn": "test.gell.one",
      "credential_domain": "TEST",
      "domain_controller_ipv4": "192.168.16.10",
      "acceptable_domain_names": ["test.gell.one", "TEST"]
    }
  },
  "bubble_id": "c33202da-3d9e-4252-82eb-604955082b55",
  "asset_role": "deployment_server"
}
```

Why `server_role=base`: the first handoff only needs a clean Windows Server
2025 VM, AutopilotAgent, and full-OS domain join. WDS, SmartDeploy, the
`E:\SmartDeploy` directory, and any SMB permissions are configured in the
Windows-side phases after the server is online.

Run preflight:

```bash
curl -fsS -b "$COOKIE" \
  -H 'Content-Type: application/json' \
  -d @labz1-sd01-payload.json \
  "$BASE/api/osdeploy/v1/preflight" |
  jq
```

Pass criteria:

- `launch_allowed` is `true`.
- No `blocking_checks` are returned.
- If the LabZ1 bubble is not ready yet, either fix the bubble readiness or
  remove `bubble_id` for the first isolated handoff test.

Create the run:

```bash
RUN_ID="$(
  curl -fsS -b "$COOKIE" \
    -H 'Content-Type: application/json' \
    -d @labz1-sd01-payload.json \
    "$BASE/api/osdeploy/v1/runs" |
    jq -r '.run.run_id'
)"
echo "$RUN_ID"
```

Start provisioning:

```bash
curl -fsS -b "$COOKIE" -X POST "$BASE/api/osdeploy/v1/runs/$RUN_ID/provision" | jq
```

Watch the run:

```bash
watch -n 15 "curl -fsS -b '$COOKIE' '$BASE/api/osdeploy/v1/runs/$RUN_ID' | jq '{run: .run | {state, vmid, vm_name, server_role, expected_computer_name}, readiness, steps: [.v2_steps[] | {kind, state}]}'"
```

Expected handoff result:

- Run reaches installed Windows Server.
- AutopilotAgent heartbeat arrives.
- Domain join steps complete.
- `server_role_status` reaches `base_ready` with the domain join verified.
- VM is usable for the WDS/SmartDeploy phases below.

## Phase 2: ProxmoxVEAutopilot Server Handoff

Expected handoff from ProxmoxVEAutopilot:

- Windows Server 2025 VM exists as `LABZ1-SD01`.
- VM is joined to `test.gell.one`.
- Static IP is assigned or reserved.
- DNS resolves:
  - `LABZ1-DC01.test.gell.one`
  - `LABZ1-SD01.test.gell.one`
- QEMU Guest Agent is running.
- AutopilotAgent reports online and ready to receive work.

Validation from the Mac or controller side:

```bash
# Replace the URL/run lookup with the actual LabZ1 run once provisioned.
curl -fsS 'http://<controller-ip>:5000/api/monitoring/deployments/runs?limit=20'
```

Validation from `LABZ1-SD01`:

```powershell
whoami
hostname
ipconfig /all
nltest /dsgetdc:test.gell.one
Resolve-DnsName LABZ1-DC01.test.gell.one
Test-ComputerSecureChannel
```

Pass criteria:

- `Test-ComputerSecureChannel` returns `True`.
- `nltest` returns a DC in `test.gell.one`.
- DNS server points at the LabZ1 DC/DNS path.

## Phase 3: Disk Layout

If ProxmoxVEAutopilot gives `LABZ1-SD01` a second data disk, initialize it as
`E:`. If it only has `C:`, either add the data disk before continuing or adjust
the paths consistently.

```powershell
Get-Disk
Get-Volume

# Example only. Pick the real raw data disk.
$disk = Get-Disk | Where-Object PartitionStyle -eq 'RAW' | Sort-Object Number | Select-Object -First 1
Initialize-Disk -Number $disk.Number -PartitionStyle GPT
New-Partition -DiskNumber $disk.Number -UseMaximumSize -DriveLetter E |
  Format-Volume -FileSystem NTFS -NewFileSystemLabel 'DeployData' -Confirm:$false

New-Item -ItemType Directory -Path 'E:\SmartDeploy','E:\RemoteInstall','E:\SmartDeploy\Boot Media','E:\SmartDeploy\Scratch' -Force
```

Pass criteria:

- `E:\SmartDeploy` exists.
- `E:\RemoteInstall` exists.

## Phase 4: Install And Initialize WDS

Run elevated on `LABZ1-SD01`:

```powershell
Install-WindowsFeature -Name WDS -IncludeManagementTools

WDSUTIL.exe /Verbose /Progress /Initialize-Server /Server:LABZ1-SD01 /RemInst:"E:\RemoteInstall"
WDSUTIL.exe /Set-Server /AnswerClients:All
WDSUTIL.exe /Set-Server /WdsClientLogging /Enabled:Yes /LoggingLevel:Info
WDSUTIL.exe /Set-Server /PxePromptPolicy /Known:Noprompt /New:Noprompt

Restart-Service WDSServer
Get-Service WDSServer
WDSUTIL.exe /Get-Server /Show:Config
```

Expected DHCP/WDS posture for this lab:

- DHCP is not installed on `LABZ1-SD01`.
- Do not configure DHCP option 60.
- Do not configure DHCP options 66/67 for the first same-subnet test.
- WDS answers PXE directly on the shared L2 network.
- Known and new PXE clients do not require the F12 prompt in the lab.

Pass criteria:

- `WDSServer` is running.
- `WDSUTIL /Get-Server /Show:Config` shows initialized WDS configuration.
- No DHCP options were added just to make PXE work.
- `Answer clients` is `Yes`.
- Known and new client PXE prompt policy is `NoPrompt`.
- WDS client logging is enabled at `Info`.

## Phase 5: Install SmartDeploy

Run this as a manual installer step first. Capture the exact installer version
and install path.

Do not copy the inspected scratch tree over the real `E:\SmartDeploy` root as an
installation shortcut. The extracted tree is for reverse-engineering and
automation discovery only.

```powershell
$SmartDeployInstallerPath = '<fill-in>'
Get-Item $SmartDeployInstallerPath | Format-List FullName,Length,LastWriteTime

# Manual for v1:
# 1. Run the installer.
# 2. Sign in or license SmartDeploy.
# 3. Set or confirm the SmartDeploy content directory as E:\SmartDeploy.
# 4. Open the console once and confirm it can see the expected folders.
```

After install:

```powershell
Get-ChildItem 'E:\SmartDeploy' -Force
Get-Service | Where-Object DisplayName -like '*SmartDeploy*' | Format-Table Name,Status,StartType
Get-ChildItem 'E:\SmartDeploy\Logs' -ErrorAction SilentlyContinue
Get-Content 'C:\Program Files\SmartDeploy\SmartDeploy\Resources\Configuration\SmartDeploy.config'
```

Pass criteria:

- SmartDeploy console opens.
- SmartDeploy services, if installed, are running.
- `E:\SmartDeploy\Logs` exists or SmartDeploy logs are located and recorded.
- `C:\Program Files\SmartDeploy\SmartDeploy\Resources\Configuration\SmartDeploy.config`
  points the SmartDeploy directory at `E:\SmartDeploy`.
- The official install/licensing state is known, not inferred from the scratch
  copy.

## Phase 6: Prepare SmartDeploy Image And Answer File

For v1, use the repo-owned helper only as the repeatable ISO staging and
metadata generator. Do not treat the extracted Microsoft ISO `install.wim` as
the SmartDeploy deployment image. The Answer File Wizard requires a WIM created
with SmartDeploy Capture Wizard because SmartDeploy stores disk/partition
deployment metadata in WIM custom data.

```powershell
$ReleaseHelper = 'E:\SmartDeploy\Scratch\ProxmoxVEAutopilot\scripts\New-SmartDeployWindowsRelease.ps1'
```

Preview the release plan without changing files:

```powershell
& $ReleaseHelper `
  -IsoPath $Windows11IsoPath `
  -EditionName 'Windows 11 Enterprise' `
  -ReleaseName 'Win11-25H2-Business-x64' `
  -SmartDeployRoot $SmartDeployRoot `
  -RemoteInstallRoot $RemoteInstallRoot `
  -Architecture x64 `
  -WdsBootImageName 'SmartDeploy WinPE x64 - Win11 25H2 LabZ1' `
  -PlanOnly |
  ConvertTo-Json -Depth 8
```

Stage or refresh the full `install.wim` and metadata:

```powershell
& $ReleaseHelper `
  -IsoPath $Windows11IsoPath `
  -EditionName 'Windows 11 Enterprise' `
  -ReleaseName 'Win11-25H2-Business-x64' `
  -SmartDeployRoot $SmartDeployRoot `
  -RemoteInstallRoot $RemoteInstallRoot `
  -Architecture x64 `
  -WdsBootImageName 'SmartDeploy WinPE x64 - Win11 25H2 LabZ1'
```

Create the SmartDeploy-compatible image:

1. Build or select a Windows 11 25H2 reference VM.
2. Shut it down cleanly.
3. Use SmartDeploy Capture Wizard to capture a standard image to
   `E:\SmartDeploy\Images\<smartdeploy-captured-image>.wim`.
4. For WDS multicast-specific testing, select the Capture Wizard WDS option.
   This is not required just to create SmartDeploy WDS boot media.
5. Verify that the Answer File Wizard accepts the captured WIM and shows the
   expected image name.

After SmartDeploy has created or exported a lab answer file and a
Capture Wizard-created WIM exists, run the safe answer-file slice:

```powershell
& $ReleaseHelper `
  -EditionName 'Windows 11 Enterprise' `
  -ReleaseName 'Win11-25H2-Business-x64' `
  -SmartDeployRoot $SmartDeployRoot `
  -RemoteInstallRoot $RemoteInstallRoot `
  -AnswerFilePath 'E:\SmartDeploy\Answer Files\<base-answer-file>.xml' `
  -Architecture x64 `
  -WdsBootImageName 'SmartDeploy WinPE x64 - Win11 25H2 LabZ1' `
  -SmartDeployCapturedImagePath 'E:\SmartDeploy\Images\<smartdeploy-captured-image>.wim' `
  -SmartDeployShareUncRoot "\\$WdsServerName\SDShare" `
  -OrganizationalUnit $ClientOuDn `
  -UpdateAnswerFileOnly
```

Pass criteria:

- Raw ISO full install WIM exists under `E:\SmartDeploy\Images` for
  repeatable source/hash evidence.
- Metadata JSON says `hashes_match=true`.
- SmartDeploy Capture Wizard-created WIM exists under `E:\SmartDeploy\Images`.
- Answer File Wizard accepts the Capture Wizard-created WIM.
- New answer file exists under `E:\SmartDeploy\Answer Files`.
- `image_file` points at the captured image on the lab SmartDeploy share, not
  at the raw ISO `install.wim`.
- `organizational_unit` equals `$ClientOuDn`.

## Phase 7: Create SmartDeploy WDS Boot WIM

For the first end-to-end lab pass, use the SmartDeploy GUI to create WDS boot
media. This is intentional. The GUI-created WIM is the known-good baseline that
the later no-GUI automation must reproduce.

Current automation finding: the inspected `MediaWizard.exe` is a .NET 8 WPF
wizard. Help-style switches launched GUI processes instead of printing CLI
usage. Treat Media Wizard as manual until we either find vendor-supported
arguments or intentionally build a tested internal automation path.

Manual SmartDeploy Media Wizard choices:

- Media type: WDS boot media.
- Architecture: x64.
- Answer file: the LabZ1 answer file from Phase 5.
- Platform packs: include any VirtIO/platform pack needed for the Proxmox VM.
- Output WIM: `E:\SmartDeploy\Boot Media\Win11-25H2-LabZ1_x64_boot.wim`.

Pass criteria:

- Output WIM exists.
- Output WIM timestamp matches this run.
- SmartDeploy log records successful media creation.

## Phase 8: Import SmartDeploy Boot WIM Into WDS

Run elevated on `LABZ1-SD01`:

```powershell
$BootWim = 'E:\SmartDeploy\Boot Media\Win11-25H2-LabZ1_x64_boot.wim'
$BootImageName = 'SmartDeploy WinPE x64 - Win11 25H2 LabZ1'
$WdsRelativeBootImage = 'Boot\x64\Images\Win11-25H2-LabZ1_x64_boot.wim'

Get-WdsBootImage | Where-Object ImageName -eq $BootImageName | Remove-WdsBootImage
Import-WdsBootImage -Path $BootWim -NewImageName $BootImageName

WDSUTIL.exe /Set-Server /BootImage:"$WdsRelativeBootImage" /Architecture:x64
WDSUTIL.exe /Set-Server /BootImage:"$WdsRelativeBootImage" /Architecture:x64uefi

Get-WdsBootImage | Format-List *
WDSUTIL.exe /Get-Server /Show:Config
```

Pass criteria:

- `Get-WdsBootImage` lists the SmartDeploy LabZ1 boot image.
- `WDSUTIL /Get-Server /Show:Config` shows x64 and x64uefi default boot images.

## Phase 9: Create PXE Test VM

Create a blank UEFI x64 VM on the same Proxmox bridge/VLAN as `LABZ1-DC01` and
`LABZ1-SD01`.

Suggested VM:

- Name: `LABZ1-PXE01`
- Firmware: OVMF/UEFI
- Machine: q35
- NIC: virtio on the same bridge as LabZ1
- Disk: 64 GB or larger
- Boot order: network first for the initial PXE test
- Secure Boot: off for the first test unless SmartDeploy boot media is known to
  support the current Secure Boot policy

Record:

```text
PXE VMID: 123
PXE MAC: BC:24:11:74:10:37
PXE firmware: OVMF/UEFI, Secure Boot off
PXE bridge/VLAN: labz1
```

Pass criteria:

- VM reaches PXE.
- VM receives DHCP lease from `LABZ1-DC01`.
- VM downloads NBP from WDS.
- VM reaches Windows Boot Manager/SmartDeploy boot image.

## Phase 10: End-To-End PXE Test

Boot `LABZ1-PXE01` from network.

Expected sequence:

```text
PXE DHCP lease from LABZ1-DC01
  -> WDS NBP download from LABZ1-SD01
  -> Windows Boot Manager
  -> SmartDeploy boot image
  -> SmartDeploy reads LabZ1 answer file
  -> Windows image applies
  -> installed Windows boots
  -> domain join to test.gell.one
```

Pass criteria:

- No WDS error after NBP download.
- SmartDeploy starts without manual media selection.
- Deployment applies the intended Windows 11 image.
- Installed Windows is domain joined.
- Computer account lands in `$ClientOuDn`.

Client validation after deployment:

```powershell
hostname
whoami /fqdn
nltest /dsgetdc:test.gell.one
Test-ComputerSecureChannel
Get-ComputerInfo | Select-Object CsName,WindowsProductName,WindowsVersion,OsBuildNumber
```

Domain validation from `LABZ1-DC01`:

```powershell
Get-ADComputer -Identity '<deployed-client-name>' -Properties DistinguishedName,DNSHostName,OperatingSystem |
  Format-List Name,DNSHostName,OperatingSystem,DistinguishedName
```

## Phase 11: Evidence Bundle

Collect from `LABZ1-SD01` after each PXE test:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
$out = "C:\Temp\LabZ1-SmartDeploy-WDS-$stamp"
New-Item -ItemType Directory -Path $out -Force | Out-Null

WDSUTIL.exe /Get-Server /Show:Config > "$out\wds-server-config.txt"
Get-WdsBootImage | Format-List * > "$out\wds-boot-images.txt"
Get-ChildItem 'E:\RemoteInstall\Boot' -Recurse -File |
  Sort-Object FullName |
  Select-Object FullName,Length,LastWriteTime |
  Out-File "$out\remoteinstall-boot-files.txt"

Get-ChildItem 'E:\SmartDeploy' -Force |
  Format-Table -AutoSize |
  Out-File "$out\smartdeploy-root.txt"
Get-ChildItem 'E:\SmartDeploy\Logs' -File -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 30 FullName,Length,LastWriteTime |
  Out-File "$out\smartdeploy-logs-inventory.txt"
Get-ChildItem 'E:\SmartDeploy\Answer Files' -File -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  Select-Object FullName,Length,LastWriteTime |
  Out-File "$out\smartdeploy-answer-files.txt"
Get-ChildItem 'E:\SmartDeploy\Images' -File -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  Select-Object FullName,Length,LastWriteTime |
  Out-File "$out\smartdeploy-images.txt"

Get-WinEvent -LogName 'Microsoft-Windows-Deployment-Services-Diagnostics/Debug' -MaxEvents 200 -ErrorAction SilentlyContinue |
  Format-List TimeCreated,Id,ProviderName,Message |
  Out-File "$out\wds-debug-events.txt"
Get-WinEvent -LogName 'Microsoft-Windows-Deployment-Services-Server/Operational' -MaxEvents 200 -ErrorAction SilentlyContinue |
  Format-List TimeCreated,Id,ProviderName,Message |
  Out-File "$out\wds-operational-events.txt"

Compress-Archive -Path "$out\*" -DestinationPath "$out.zip" -Force
$out
```

Collect from `LABZ1-DC01`:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
$out = "C:\Temp\LabZ1-DHCP-AD-$stamp"
New-Item -ItemType Directory -Path $out -Force | Out-Null

Get-DhcpServerv4Scope | Format-List * > "$out\dhcp-scopes.txt"
Get-DhcpServerv4Lease -ScopeId '<scope-id>' |
  Sort-Object LeaseExpiryTime -Descending |
  Select-Object -First 50 |
  Format-Table -AutoSize > "$out\dhcp-recent-leases.txt"
Get-DhcpServerv4OptionValue -ScopeId '<scope-id>' |
  Format-List * > "$out\dhcp-scope-options.txt"
Get-DhcpServerv4OptionValue |
  Format-List * > "$out\dhcp-server-options.txt"

Compress-Archive -Path "$out\*" -DestinationPath "$out.zip" -Force
$out
```

## Windows Devbox Codex Prompt

Use this prompt in the Windows 11 devbox Codex session after
ProxmoxVEAutopilot has provisioned `LABZ1-SD01` and the AutopilotAgent is
online:

```text
We are building the LabZ1 SmartDeploy/WDS PXE test path from the ProxmoxVEAutopilot repo.

Read and follow:
C:\Path\To\ProxmoxVEAutopilot\docs\LABZ1_SMARTDEPLOY_WDS_RUNBOOK.md

Lab facts:
- Domain: test.gell.one
- DHCP/DNS/DC: LABZ1-DC01
- WDS/SmartDeploy target: LABZ1-SD01
- DHCP and WDS are on separate servers.
- DC, WDS server, and PXE client are on the same subnet/VLAN.
- Do not configure DHCP options 60, 66, or 67 unless evidence proves same-subnet WDS discovery cannot work.

Your job:
1. Validate LABZ1-SD01 is domain joined and healthy.
2. Install/initialize WDS to E:\RemoteInstall.
3. Help me install/configure SmartDeploy to E:\SmartDeploy.
4. Generate or validate the SmartDeploy answer file for the LabZ1 OU.
5. Import the SmartDeploy WDS boot WIM into WDS.
6. Set x64 and x64uefi default boot images.
7. Collect the evidence bundle from the runbook.

Do not skip validation. Before each destructive or state-changing command, tell me what it will change. If you need a value that is not in the runbook, ask me for that one value.
```

## Promotion To Automation

After the manual flow passes, promote in this order:

1. ProxmoxVEAutopilot lab profile creates `LABZ1-SD01` and `LABZ1-PXE01`.
2. AutopilotAgent work item installs and initializes WDS.
3. AutopilotAgent work item validates SmartDeploy install and folder layout.
4. SmartDeploy answer-file generation is copied into a repo-owned tool.
5. Manual SmartDeploy Media Wizard step is isolated as the only GUI dependency.
6. WDS boot image import/default selection becomes an agent work item.
7. PXE client VM launch and evidence collection become a repeatable E2E gate.

Do not automate DHCP options for this lab unless the same-subnet PXE test fails
with evidence that WDS discovery is not occurring.

## Reference Commands And Sources

- WDS install role: `Install-WindowsFeature -Name WDS -IncludeManagementTools`
- WDS initialization: `WDSUTIL.exe /Initialize-Server /RemInst:<path>`
- WDS answer clients: `WDSUTIL.exe /Set-Server /AnswerClients:All`
- Boot image import: `Import-WdsBootImage -Path <wim> -NewImageName <name>`
- Boot image inventory: `Get-WdsBootImage`
- WDS server config: `WDSUTIL.exe /Get-Server /Show:Config`

Primary references:

- Microsoft Learn: `Install-WindowsFeature`
  https://learn.microsoft.com/en-us/powershell/module/servermanager/install-windowsfeature?view=windowsserver2025-ps
- Microsoft Learn: `wdsutil /Initialize-Server`
  https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/wdsutil-initialize-server
- Microsoft Learn: WDS initialization example with `/AnswerClients:All`
  https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-10/deployment/deploy-windows-mdt/prepare-for-windows-deployment-with-mdt
- Microsoft Learn: `Import-WdsBootImage`
  https://learn.microsoft.com/en-us/powershell/module/wds/import-wdsbootimage?view=windowsserver2025-ps
- Microsoft Learn: `Get-WdsBootImage`
  https://learn.microsoft.com/en-us/powershell/module/wds/get-wdsbootimage?view=windowsserver2025-ps
- Microsoft Learn: `wdsutil /Set-Server`
  https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/wdsutil-set-server
