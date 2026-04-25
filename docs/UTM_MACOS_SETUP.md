# Running ProxmoxVEAutopilot natively on macOS (UTM backend)

The UTM backend talks to `utmctl`, which ships **inside** UTM.app at
`/Applications/UTM.app/Contents/MacOS/utmctl`. `utmctl` is a native
macOS binary — it cannot run inside the project's Linux Docker
container. For the UTM hypervisor mode, the web service must run
**directly on the macOS host**.

> The Docker deployment path remains the recommended option for the
> Proxmox backend. Docker is only unsupported when `hypervisor_type`
> is set to `utm`.

## Interactive launcher (recommended)

For a one-screen workflow, use the curses-based TUI that ships with
the repo:

```bash
cd autopilot-proxmox
./scripts/tui.sh
```

It starts/stops the `web`, `builder`, and `monitor` processes, tails
each service's log, and defaults to port **5055** to sidestep the
macOS AirPlay Receiver collision on :5000 (see Known issues below).
It also preflights the venv, so a missing package surfaces as a
banner instead of a mystery crash.

## Prerequisites

| Component | Minimum | Install |
|-----------|---------|---------|
| macOS | 13 (Ventura) or newer, Apple Silicon | — |
| UTM.app | 4.5+ | `brew install --cask utm` or https://mac.getutm.app/ |
| Homebrew | latest | https://brew.sh |
| Python | 3.12 | `brew install python@3.12` |
| Ansible | `ansible-core >= 2.17` | installed via pip below |
| PowerShell (optional) | 7.4+ | `brew install --cask powershell` — only needed for Autopilot hash capture parity with the Linux container |
| A prepared Windows ARM64 UTM template VM | — | see "Template VM" below |

## One-time setup

```bash
git clone https://github.com/adamgell/ProxmoxVEAutopilot.git
cd ProxmoxVEAutopilot/autopilot-proxmox

# Isolated venv so the system Python stays clean.
python3.12 -m venv .venv
source .venv/bin/activate

# Install Python deps. A few dependencies (python-ldap, gssapi,
# impacket) need OpenLDAP + Kerberos headers. If you do NOT plan to
# use the AD monitoring features, install the trimmed requirements
# file (see macOS section below) — the web UI will still work and
# UTM provisioning will run.
pip install -r requirements.txt
```

If `pip install` fails building `python-ldap` or `gssapi`, install the
system libraries first:

```bash
brew install openldap cyrus-sasl krb5
export LDFLAGS="-L$(brew --prefix openldap)/lib -L$(brew --prefix cyrus-sasl)/lib -L$(brew --prefix krb5)/lib"
export CPPFLAGS="-I$(brew --prefix openldap)/include -I$(brew --prefix cyrus-sasl)/include -I$(brew --prefix krb5)/include"
pip install -r requirements.txt
```

## Starting the service

```bash
cd autopilot-proxmox
source .venv/bin/activate
python -m web.entrypoint web
```

The web UI listens on `http://localhost:5000`. First-run:

1. Open Settings → set `hypervisor_type` to `utm`.
2. Fill in the UTM section:
   - `utm_utmctl_path` — leave default unless UTM is installed elsewhere.
   - `utm_template_vm_name` — the UTM VM to clone from.
   - `utm_exec_scratch_dir` — leave default (`C:\Users\Public`).
3. Save. The dispatcher in `roles/common/tasks/` now routes every
   guest call to the `_utm_*.yml` variants automatically.

## Template VM

At the time of writing, `utmctl clone` does not expose SMBIOS /
chassis / disk-size knobs — so the template VM you clone from should
already be prepared exactly the way you want new VMs to look:

- Windows 11 ARM64 installed and sysprep'd.
- SPICE guest tools installed (file push/pull relies on the SPICE
  agent; this ships with UTM's Windows tools ISO).
- At least one writable directory matching `utm_exec_scratch_dir`
  (default `C:\Users\Public` — always exists on Windows).

The `whoami`/`hostname` smoke test in `playbooks/_test_utm_dispatch.yml`
is the quickest way to confirm a template is wired correctly.

## Known limitations

- **No stdout streaming.** `utmctl exec` does not return guest stdout
  on Windows ARM64 (upstream issue
  [utmapp/UTM#5134](https://github.com/utmapp/UTM/issues/5134) —
  qemu-ga is not ported). The backend works around this by
  redirecting guest output to a scratch file and pulling it back
  with `utmctl file pull`. Latency is ~1 extra second per exec.
- **No SMBIOS overrides yet.** OEM branding / chassis-type flags that
  Proxmox's VM config exposes cannot be tweaked via `utmctl`. Bake
  them into the template VM itself for now.
- **Single hypervisor per process.** `hypervisor_type` is a global
  setting. Running Proxmox and UTM side-by-side needs two separate
  deployments.
- **AirPlay Receiver owns :5000 on macOS.** Ventura+ binds Control
  Center's AirPlay Receiver to port 5000, so `python -m web.entrypoint`
  will either fail to bind or collide with AirTunes and return HTTP
  403 to browsers. Two fixes: (a) disable **AirPlay Receiver** under
  System Settings → General → AirDrop & Handoff, or (b) leave the TUI
  default of 5055 (also set by `scripts/run_macos_native.sh` and
  overridable via `AUTOPILOT_WEB_PORT`).

## Building your first UTM template

### Why UTM templates are different from Proxmox

With Proxmox you clone a template VMID. `utmctl` has **no `create` or `import`
command** — UTM VMs are `.utm` bundles stored as directories on disk. Autopilot
ships **skeleton bundles** under `assets/utm-templates/` (one per OS flavour)
which the build playbook copies, rewires with a fresh UUID, attaches your
installer ISO, and registers with UTM.app by moving the bundle into UTM's
Documents directory. You then complete Windows OOBE manually and let Autopilot
run sysprep to seal the image.

### Prerequisites

Install `qemu-img` so the playbook can resize the disk image inside the bundle:

```bash
brew install qemu
```

Verify it is accessible at the default path:

```bash
/opt/homebrew/bin/qemu-img --version
```

If you installed QEMU via a different method, update **Settings → UTM →
qemu-img Path** (`utm_qemu_img_path`) to match.

### Procuring a Windows 11 ARM64 ISO

1. Enrol in the Windows Insider Programme (free Microsoft account required).
2. Download from:
   <https://www.microsoft.com/software-download/windowsinsiderpreviewARM64>
3. Save the `.iso` file into your ISO directory (default `~/UTM-ISOs/`).
4. In **Settings → UTM → Windows 11 ISO Filename** set the exact filename,
   e.g. `Windows11_InsiderPreview_Client_ARM64_en-us_26100.iso`.

### Procuring a Windows Server ARM64 ISO

1. Visit the Microsoft Evaluation Center:
   <https://www.microsoft.com/en-us/evalcenter/evaluate-windows-server-2025>
2. Select **ARM64 ISO** and download.
3. Save into `~/UTM-ISOs/` and update **Settings → UTM → Windows Server ISO
   Filename** (`utm_windows_server_iso_name`).

### Configuring ISO and skeleton paths

| Setting key | Default | Purpose |
|---|---|---|
| `utm_iso_dir` | `~/UTM-ISOs` | Directory where you drop installer ISOs |
| `utm_skeleton_dir` | `assets/utm-templates` (repo-relative) | Shipped skeleton bundles |
| `utm_qemu_img_path` | `/opt/homebrew/bin/qemu-img` | Disk resize tool |
| `utm_documents_dir` | `~/Library/Containers/com.utmapp.UTM/Data/Documents` | Where UTM stores VM bundles |

All paths are configurable under **Settings → UTM (macOS/ARM64) Configuration**.

### Step-by-step: building a template via the web UI

1. **Open `/template`** in the web UI. If `hypervisor_type` is `utm` you will
   see the *UTM Template Builder* card instead of the Proxmox form.

2. **Select OS** — *Windows 11 ARM64* or *Windows Server ARM64*.

3. **Enter a Template Name** — alphanumeric, hyphens and underscores only,
   e.g. `win11-arm64-template`. This becomes the UTM VM name.

4. **Select ISO** — the dropdown is populated from `GET /api/utm/isos` and
   lists every `.iso` file found in your `utm_iso_dir`.  
   If the list is empty, the card shows:
   > *No ISOs found in ~/UTM-ISOs. Drop a Windows ARM64 ISO there and reload.*  
   Drop the ISO into that directory and reload the page.

5. **Set CPU / RAM / Disk** — defaults are 4 cores / 8 192 MB / 80 GB.
   Raise RAM to 16 384 MB for Windows Server builds to avoid OOM during install.

6. **Click Build UTM Template** — Autopilot enqueues a
   `build_utm_template.yml` job and redirects you to the job log page.

7. **Watch the job log** — the playbook copies the skeleton bundle, rewires its
   UUID, attaches the ISO, and calls `utmctl start <name>`.

8. **Complete Windows OOBE manually in UTM.app** — language, keyboard, EULA,
   local account.  Leave the VM running when OOBE finishes.

9. *(Optional)* Install any machine-scoped software you want baked into every
   clone before sysprep seals the image.

10. **Resume** — the playbook waits for a resume signal. On the job detail page
    click **Resume & finalize** (or `POST /api/jobs/{id}/resume-template-build`).
    Sysprep runs inside the VM, shuts it down, and marks the bundle ready.

11. **Note the VM name** and copy it into **Settings → UTM → Template VM Name**
    (`utm_template_vm_name`) so the provision flow knows what to clone.

### Troubleshooting

**`plutil -lint` fails on the bundle plist**  
The skeleton bundle's `config.plist` must be valid XML plist. If the playbook
reports a lint failure, open the bundle directory
(`~/Library/Containers/com.utmapp.UTM/Data/Documents/<name>.utm/`) in Finder,
right-click `config.plist → Open With → Xcode` and look for stray characters
near the error line.

**VM doesn't appear in UTM.app after the build job completes**  
UTM.app does not hot-reload its library. Quit and reopen UTM.app, or run:

```bash
/Applications/UTM.app/Contents/MacOS/utmctl list
```

If the VM is missing from `utmctl list`, check that the bundle landed in the
correct Documents directory:

```bash
ls ~/Library/Containers/com.utmapp.UTM/Data/Documents/
```

**Resetting a botched bundle**  
Delete the bundle from UTM's Documents directory and from UTM.app (right-click
→ Remove), then re-run the build job:

```bash
rm -rf ~/Library/Containers/com.utmapp.UTM/Data/Documents/<name>.utm
```

**Viewing UTM's own logs**  
UTM writes to the macOS unified log:

```bash
log stream --predicate 'subsystem == "com.utmapp.UTM"' --level debug
```

Or open Console.app, filter by Process = `UTM`.

**`utmctl start` hangs or returns non-zero**  
Make sure UTM.app is open and no other VM is holding the QEMU process limit.
`utmctl list` must show the VM before `start` will work.
