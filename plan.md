# Project: Ansible Automation for Windows Autopilot on Proxmox

## Goal
Convert the APHVTools PowerShell module into an Ansible project that provisions
Windows VMs on Proxmox with OEM-accurate SMBIOS fields, injects Autopilot
configuration, captures hardware hashes, and optionally uploads them to Intune.

## Source Material
The attached PowerShell files are the complete APHVTools module split into:
- Private functions (internal helpers): VM creation, SMBIOS assembly, guest
  agent wrappers, serial number generation, hash capture
- Public functions (exported): Config management, orchestration entry points

## Design Decisions

1. Guest communication: Proxmox QEMU Guest Agent API only (no WinRM).
   All in-guest operations (Autopilot JSON injection, hash capture script
   push/execute, file read-back) use Proxmox REST API guest agent endpoints.
   Ansible drives these via the `uri` module against the Proxmox API.

2. Provisioning paths: Three roles required.
   a) RAW VM FROM ISO — Create new VM with OVMF/Q35/VirtIO/vTPM, attach
      custom Windows ISO (VirtIO drivers baked in) + VirtIO guest tools ISO,
      boot, wait for guest agent, inject Autopilot config, capture hash.
   b) TEMPLATE BUILDER — Create VM from ISO, wait for unattended Windows
      install to complete (autounattend.xml gets to desktop + enables guest
      agent), install VirtIO guest agent/tools via guest-exec if needed,
      sysprep via guest-exec, stop VM, convert to Proxmox template via API.
      The Windows ISO has VirtIO storage/network drivers baked in already.
   c) CLONE FROM TEMPLATE — Full-clone from template built in (b),
      reconfigure SMBIOS/identity/serial, boot, inject Autopilot, capture hash.

3. Graph upload: NOT in Ansible. Ansible deposits hardware hash CSVs to a
   known directory on the controller. Upload is handled separately by calling
   the existing Import-VMHashToAutopilot PowerShell function via pwsh.

4. Controller OS: Linux VM or CT running on the same Proxmox host.
   Ansible runs from this Linux system. pwsh (PowerShell Core) is available
   on the controller for the Graph upload step.

5. Secrets management: Ansible Vault (built-in, free, AES-256).
   Vaulted secrets: Proxmox API token, Entra ID app registration credentials
   (AppId, TenantId, AppSecret).

## Key Technical Details

### SMBIOS1 String Format
Proxmox `smbios1` accepts comma-delimited key=value pairs. When OEM fields
contain spaces or special characters, they must be base64-encoded and the
string must include `base64=1`. Reference: `Merge-ProxmoxSmbios1.ps1`

Format: `base64=1,manufacturer=<b64>,product=<b64>,family=<b64>,serial=<b64>,sku=<b64>,uuid=<uuid>`

The UUID is NOT base64-encoded even when base64=1 is set.

### VM Identity
Each VM gets a deterministic UUID (GUID) and a disk serial formatted as
`APHV<6-digit-vmid><10-char-uuid-prefix>`. See `New-ProxmoxVmIdentity.ps1`.

### Serial Number Generation
Manufacturer-prefixed: Lenovo=PF, Dell=SVC, HP=CZC, Microsoft=MSF,
default=LAB, followed by dash and 8 hex chars. See `New-ProxmoxSerialNumber.ps1`.

### OEM Profiles
13 built-in profiles covering Lenovo, Dell, HP, Microsoft Surface, and generic
virtual devices. Each defines: manufacturer, product, family, SKU, chassisType.
See `Get-OemProfile.ps1`.

### VM Creation — ISO Path (New-ProxmoxDevice.ps1)
Creates from scratch: OVMF BIOS, Q35 machine, ostype=win11, cpu=host,
balloon=0, agent=enabled, scsihw=virtio-scsi-single, efidisk0 with
pre-enrolled-keys=1, tpmstate0 v2.0, VirtIO NIC, boot=order=ide2;scsi0.
Windows ISO on ide2, VirtIO ISO on ide3.

### VM Creation — Clone Path (New-ProxmoxCloneDevice.ps1)
Full-clone from template. Waits for clone task. Reads current scsi0 config,
updates with new name/cores/memory/cpu/balloon/agent/net0/smbios1/disk-serial.
Retries config update up to 6 times if config lock contention from clone.
Resizes disk if requested size > template disk size.

### Disk Serial Injection
Appends `serial=<value>` to the scsi0 disk config string. 
See `Set-ProxmoxDiskSerial.ps1`.

### Guest Agent Operations
All use Proxmox REST API — no WinRM, no network dependency.

- Wait for agent: Poll `POST /nodes/{node}/qemu/{vmid}/agent/ping`
  with configurable timeout and interval.

- File write: `POST /nodes/{node}/qemu/{vmid}/agent/file-write`
  Body: `{"file": "<guest-path>", "content": "<data>"}`

- Command exec: `POST /nodes/{node}/qemu/{vmid}/agent/exec`
  Body: `{"command": ["program", "arg1", ...]}`
  Returns PID.

- Exec status poll: `GET /nodes/{node}/qemu/{vmid}/agent/exec-status?pid=<pid>`
  Poll until `exited: true`. Response has `out-data`, `err-data`, `exitcode`.

### Autopilot JSON Injection
Writes `AutopilotConfigurationFile.json` to
`C:\Windows\Provisioning\Autopilot\AutopilotConfigurationFile.json`
inside the guest via file-write API. Creates the directory first via
guest-exec `cmd.exe /c mkdir`. See `Publish-ProxmoxAutoPilotConfig.ps1`.

### Hardware Hash Capture
1. Push bundled `Get-WindowsAutopilotInfo.ps1` to guest via file-write
2. Push wrapper `CaptureHash.ps1` to guest via file-write
3. Execute wrapper via guest-exec, poll for completion
4. Wrapper outputs `HASH_CAPTURED:<path>` on success
5. Read CSV back from guest via guest-exec `Get-Content -Raw`
6. Save CSV to controller filesystem
See `Publish-ProxmoxHashCaptureScript.ps1` and `Get-ProxmoxVMHardwareHash.ps1`.

### Sysprep for Template Builder
Execute via guest-exec:
`cmd.exe /c C:\Windows\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown`
Wait for VM to reach stopped state.
Then convert to template: `POST /nodes/{node}/qemu/{vmid}/template`

### Proxmox API Auth
Header: `Authorization: PVEAPIToken=USER@REALM!TOKENID=UUID`
All API calls go to `https://<host>:8006/api2/json/...`
Self-signed certs expected — validate_certs: false in uri module calls.

### Chassis Type Limitation
Proxmox 9 does NOT support smbios2 (chassis type). Log a warning and skip.

### Autopilot Profile Retrieval
Uses Microsoft Graph beta endpoint:
`GET https://graph.microsoft.com/beta/deviceManagement/windowsAutopilotDeploymentProfiles`
Converts profile to Windows provisioning JSON format.
This step may remain PowerShell-driven on the controller since the
existing `Get-AutopilotPolicy.ps1` handles the full conversion logic.

## Proposed Structure
autopilot-proxmox/
├── inventory/
│   ├── hosts.yml
│   └── group_vars/
│       └── all/
│           ├── vars.yml        # Proxmox host, node, storage, bridge, OEM profiles
│           └── vault.yml       # Encrypted: API token, Entra creds
├── roles/
│   ├── proxmox_vm_iso/         # Role (a): Raw VM from ISO
│   ├── proxmox_template_builder/ # Role (b): Build + sysprep + templatize
│   ├── proxmox_vm_clone/       # Role (c): Clone from template
│   ├── autopilot_inject/       # Shared: Push Autopilot JSON via guest agent
│   └── hash_capture/           # Shared: Capture + retrieve hardware hash
├── playbooks/
│   ├── provision_iso.yml       # Uses roles: proxmox_vm_iso + autopilot_inject + hash_capture
│   ├── build_template.yml      # Uses role: proxmox_template_builder
│   ├── provision_clone.yml     # Uses roles: proxmox_vm_clone + autopilot_inject + hash_capture
│   └── upload_hashes.yml       # Calls pwsh Import-VMHashToAutopilot
├── files/
│   ├── oem_profiles.yml        # 13 OEM profiles as YAML
│   ├── Get-WindowsAutopilotInfo.ps1
│   └── AutopilotConfigurationFile.json  # Per-tenant, or fetched pre-run
├── filter_plugins/
│   └── smbios.py               # Custom Jinja2 filter: base64 SMBIOS encoding
└── README.md