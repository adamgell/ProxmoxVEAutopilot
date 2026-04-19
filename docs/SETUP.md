# Setup

Supplementary setup notes for Proxmox VE Autopilot. See the main
[README](../README.md) for the primary Quick Start.

## Task Sequences and Credentials

Task sequences let you compose reusable, per-OS build recipes (step lists with
credential references) that drive the Build Template and Provision VMs flows.
Sequences are managed on the **Sequences** page; credentials (API tokens,
onboarding scripts, domain-join accounts) live on the **Credentials** page and
are referenced by id from sequence steps.

### Ubuntu path

The seeded **Ubuntu Intune + MDE (LinuxESP)** sequence reproduces the upstream
[LinuxESP](https://github.com/ugurkocde/LinuxESP) flow: Ubuntu 24.04 autoinstall
with Intune Portal, Microsoft Edge, Microsoft Defender for Endpoint (MDE), and a
customisable apt/snap package set + bloat purge.

**One-time prep on Proxmox:**

1. Upload `ubuntu-24.04-live-server-amd64.iso` to your Proxmox ISO storage.
2. In Settings, confirm `ubuntu_iso` points at it (default:
   `isos:iso/ubuntu-24.04-live-server-amd64.iso`).

**Before first use:**

- Create an `mde_onboarding` credential on the **Credentials** page. Upload the
  `MicrosoftDefenderATPOnboardingLinuxServer.py` script downloaded from the
  Defender portal.
- Open the **Ubuntu Intune + MDE (LinuxESP)** sequence and set the
  `install_mde_linux` step's credential reference to the one you created.

**Build the template:**

1. **Build Template → toggle Ubuntu.**
2. Pick your Ubuntu sequence from the dropdown.
3. Click **Rebuild Ubuntu Seed ISO** — compiles the sequence into
   `autoinstall.yaml` + `meta-data`, wraps in a NoCloud seed ISO (volume label
   `cidata`), and uploads to Proxmox as `ubuntu-seed.iso`.
4. Click **Build Ubuntu Template** — boots a fresh VM against the Ubuntu ISO +
   seed ISO, waits for autoinstall (~20 min), runs `cloud-init clean` as the
   sysprep analogue, then converts the VM to a template.

**Provision devices:**

- On **Provision VMs**, pick the Ubuntu sequence and (optionally) a hostname
  pattern — defaults to `autopilot-{serial}`. Clone count, OEM profile, and
  group tag work identically to the Windows flow.
- Each clone boots with a per-VM NoCloud seed ISO containing just the hostname
  and any `run_firstboot_script` runcmd. Cloud-init regenerates `/etc/machine-id`
  and SSH host keys automatically.

**Check enrollment:**

- On **Devices**, the **Check** button next to each Ubuntu VM runs
  `intune-portal --version` and `mdatp health` via guest-exec, persists status
  chips as Proxmox tags (`enroll-intune-healthy`, `enroll-mde-missing`, …), and
  reloads the page. The chips render alongside the VM row.
- **Capture Hash** is disabled for Ubuntu VMs — there is no Autopilot hardware
  hash for Linux; Intune enrollment completes when the user signs into the
  intune-portal app for the first time.

**New `vars.yml` keys (all optional):**

```yaml
ubuntu_release: "noble"          # 24.04 codename
ubuntu_locale: "en_US.UTF-8"
ubuntu_timezone: "UTC"
ubuntu_keyboard_layout: "us"
ubuntu_storage_layout: "lvm"
ubuntu_iso: "isos:iso/ubuntu-24.04-live-server-amd64.iso"
ubuntu_seed_iso: "isos:iso/ubuntu-seed.iso"
ubuntu_per_vm_seed_pattern: "isos:iso/ubuntu-per-vm-{{ vmid }}.iso"
```

**Accepted risk:** The compiled `autoinstall.yaml` on the seed ISO contains the
local-admin password hash (via `crypt.SHA512`) and, for the LinuxESP sequence,
the base64-encoded MDE onboarding script. Standard cloud-init behaviour strips
these after install. Mitigation: use a lab-only onboarding script and rotate
the `credential_key` if you suspect exposure.
