# Running ProxmoxVEAutopilot natively on macOS (UTM backend)

The UTM backend talks to `utmctl`, which ships **inside** UTM.app at
`/Applications/UTM.app/Contents/MacOS/utmctl`. `utmctl` is a native
macOS binary — it cannot run inside the project's Linux Docker
container. For the UTM hypervisor mode, the web service must run
**directly on the macOS host**.

> The Docker deployment path remains the recommended option for the
> Proxmox backend. Docker is only unsupported when `hypervisor_type`
> is set to `utm`.

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
