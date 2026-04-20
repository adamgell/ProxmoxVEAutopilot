# Setup Guide

This is the detailed walkthrough. If you just want the short version, see the [Quick Start in the root README](../README.md#quick-start-5-steps).

Contents:

1. [Create a Proxmox API token](#1-create-a-proxmox-api-token)
2. [Deploy the container](#2-deploy-the-container)
3. [First-run configuration](#3-first-run-configuration)
4. [Build the answer ISO](#4-build-the-answer-iso)
5. [Build the Windows template](#5-build-the-windows-template)
6. [Provision devices and capture hashes](#6-provision-devices-and-capture-hashes)
7. [Task Sequences and Credentials](#task-sequences-and-credentials)
8. [Appendix A — Unattended install internals](#appendix-a--unattended-install-internals)
9. [Appendix B — Air-gapped / manual answer ISO build](#appendix-b--air-gapped--manual-answer-iso-build)
10. [Appendix C — Ansible CLI](#appendix-c--ansible-cli)

---

## 1. Create a Proxmox API token

Everything the app does on Proxmox happens through a single API token. The token needs a role with enough privileges to create VMs, read storage, use the guest agent, and upload ISOs.

### 1a. Create the role

On the Proxmox host shell:

```bash
pveum role add AutopilotProvisioner -privs \
VM.Allocate,VM.Clone,VM.Config.CPU,VM.Config.CDROM,VM.Config.Cloudinit,\
VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,\
VM.Config.Options,VM.Audit,VM.PowerMgmt,VM.Console,\
VM.Snapshot,VM.Snapshot.Rollback,\
VM.GuestAgent.Audit,VM.GuestAgent.FileRead,VM.GuestAgent.FileWrite,\
VM.GuestAgent.FileSystemMgmt,VM.GuestAgent.Unrestricted,\
Datastore.Allocate,Datastore.AllocateSpace,Datastore.AllocateTemplate,\
Datastore.Audit,Sys.Audit,Sys.Modify,SDN.Use
```

> The backslashes keep the command on one logical line. If you remove them, put the whole thing on one line.

> `Datastore.Allocate` is required for OEM profiles / sequences that set a **chassis type** override. Proxmox filters `snippets` volumes out of content listings unless the caller has this privilege (see [TROUBLESHOOTING](TROUBLESHOOTING.md#provision-fails-with-chassis-type-binary--is-not-present)). If you upgraded from an older setup and don't use chassis overrides, you can leave it off — but adding it is harmless.

### 1b. Create the service user and token

```bash
pveum user add autopilot@pve --comment "Autopilot provisioning"
pveum user token add autopilot@pve ansible --privsep=0 --comment "Automation"
```

Proxmox prints the token secret **once**. Copy both lines (the full token id like `autopilot@pve!ansible` and the hex secret). You'll paste them into `vault.yml` in the next step.

### 1c. Grant the role on your storages and SDN zone

The default commands in the README assume a pool called `ssdpool` and an ISO store called `isos`. Yours will be different. Find the real names first:

```bash
pvesm status                       # your storages
pvesh get /cluster/sdn/zones       # your SDN zones (if using SDN)
```

Then apply ACLs, substituting your names:

```bash
pveum acl modify / -user autopilot@pve -role AutopilotProvisioner
pveum acl modify /storage/<vm-disk-storage> -user autopilot@pve -role AutopilotProvisioner
pveum acl modify /storage/<iso-storage>     -user autopilot@pve -role AutopilotProvisioner
pveum acl modify /sdn/zones/<zone-name>     -user autopilot@pve -role AutopilotProvisioner
```

The `/storage/<iso-storage>` ACL is critical — without `Datastore.AllocateSpace` on the ISO storage, rebuilding the answer ISO will return **403 Forbidden**.

## 2. Deploy the container

### 2a. Clone and configure

```bash
git clone https://github.com/adamgell/ProxmoxVEAutopilot.git
cd ProxmoxVEAutopilot/autopilot-proxmox
cp inventory/group_vars/all/vault.yml.example inventory/group_vars/all/vault.yml
```

Edit `inventory/group_vars/all/vault.yml`:

```yaml
# Proxmox API token (from step 1b)
vault_proxmox_api_token_id: "autopilot@pve!ansible"
vault_proxmox_api_token_secret: "12345678-90ab-cdef-1234-567890abcdef"

# Entra ID app registration — ONLY needed for Intune hash upload.
# Leave blank to skip that feature.
vault_entra_app_id: ""
vault_entra_tenant_id: ""
vault_entra_app_secret: ""
```

Optional: encrypt the file with `ansible-vault encrypt inventory/group_vars/all/vault.yml`. The container reads it either way; encryption is a safeguard against accidental commits. `vault.yml` is already in `.gitignore`.

### 2b. Start the container

```bash
docker compose up -d
```

The UI is on **http://your-host:5000**.

### 2c. What the compose file mounts

```yaml
services:
  autopilot:
    image: ghcr.io/adamgell/proxmox-autopilot:latest
    network_mode: host
    volumes:
      - ./inventory/group_vars/all/vault.yml:/app/inventory/group_vars/all/vault.yml:ro
      - ./inventory/group_vars/all/vars.yml:/app/inventory/group_vars/all/vars.yml
      - ./output:/app/output
      - ./secrets:/app/secrets
      - autopilot-jobs:/app/jobs
      - autopilot-db:/app/db
```

| Mount | Purpose |
|-------|---------|
| `vault.yml` (ro) | Ansible vault — Proxmox and Entra credentials, read-only so the UI can't overwrite them |
| `vars.yml` (rw)  | Configuration — editable from the Settings page |
| `./output`       | Captured hash CSVs, persisted on the host |
| `./secrets`      | Holds `credential_key` — the Fernet key that encrypts rows in the **Credentials** page store. Auto-generated on first start if absent; persist this directory or you will lose access to stored AD/local-admin credentials after a container recreate |
| `autopilot-jobs` | Job logs, named volume |
| `autopilot-db`   | SQLite database for devices, task sequences, credentials, and VM↔sequence tracking |

> **Back up `./secrets/credential_key`.** It encrypts the credentials table. Lose it and every stored AD / local-admin / ODJ credential in the DB becomes unrecoverable.

## 3. First-run configuration

Open **Settings**. There's a chicken-and-egg problem to be aware of: the node/storage/ISO dropdowns populate by calling the Proxmox API, which needs a valid token. So do this in order:

1. Fill **Proxmox host**, **port** (usually 8006), and **node** name manually.
2. Save.
3. Refresh. The dropdowns for storage, ISO storage, Windows ISO, VirtIO ISO, and answer ISO will now populate from Proxmox.
4. Pick your values. Fill in VM defaults (CPU, memory, disk size, OEM profile).
5. Save again.

If the dropdowns still look empty, the token is wrong or missing the role — see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#settings-dropdowns-are-empty).

For reference, the relevant `vars.yml` fields and their shipping defaults:

| Field | Default | What it is |
|-------|---------|------------|
| `proxmox_host` | `192.168.2.200` | IP or hostname of the Proxmox node |
| `proxmox_port` | `8006` | API port |
| `proxmox_node` | `pve2` | Node name in the cluster |
| `proxmox_storage` | `ssdpool` | Where VM disks live |
| `proxmox_iso_storage` | `isos` | Where ISOs live |
| `proxmox_bridge` | `vmbr0` | Default NIC bridge |
| `proxmox_windows_iso` | `isos:iso/en-us_windows_11_...iso` | Full Proxmox storage path |
| `proxmox_virtio_iso` | `isos:iso/virtio-win-0.1.285.iso` | Full Proxmox storage path |
| `proxmox_answer_iso` | `isos:iso/autounattend.iso` | Created in step 4 |
| `proxmox_template_vmid` | `105` | VMID for the built template |
| `vm_oem_profile` | `generic-desktop` | Default SMBIOS profile |

## 4. Build the answer ISO

The unattended-install answer ISO is a tiny ISO containing `autounattend.xml`, labelled `OEMDRV` so Windows Setup picks it up automatically.

On the **Build Template** page, click **Rebuild Answer ISO**. The container reads `autopilot-proxmox/files/autounattend.xml`, generates the ISO, and uploads it to your configured ISO storage. Watch progress on **Jobs**. It usually takes under a minute.

You should rebuild the answer ISO whenever you change `autounattend.xml` (for example, to tweak regional settings or OOBE behaviour).

> No shell access needed. If you're air-gapped or the upload is failing, there's a manual recipe in [Appendix B](#appendix-b--air-gapped--manual-answer-iso-build).

## 5. Build the Windows template

On the **Build Template** page, click **Build Template**. The playbook:

1. Creates a fresh VM with SMBIOS, TPM, UEFI/OVMF, and the Windows + VirtIO + answer ISOs attached.
2. Starts the VM, sends a keypress at the OVMF prompt to boot from CD.
3. Windows Setup finds `autounattend.xml` on the OEMDRV volume and installs unattended.
4. FirstLogonCommands installs the QEMU guest agent from the VirtIO ISO.
5. Ansible detects the guest agent, runs sysprep with `/generalize /oobe /shutdown`, then converts the VM to a template.

Expect 20-30 minutes. Watch **Jobs** for live logs.

If it hangs on "waiting for boot" see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#template-build-hangs-at-waiting-for-boot).

## 5b. Enable chassis-type overrides (optional)

Skip this section if none of your OEM profiles or sequences set a **chassis type** override. You can tell by checking `autopilot-proxmox/files/oem_profiles.yml` — if every profile you use has `chassis_type: null`, you're good.

If you do use chassis-type overrides (e.g. `chassis_type: 10` for a laptop, `31` for a convertible, `35` for a mini-PC), QEMU needs two things the Proxmox API can't normally give us:

1. A small SMBIOS Type 3 binary for each chassis type, on `/var/lib/vz/snippets/` on every node that might host the VM.
2. A root@pam API token, because Proxmox hardcodes the VM `args:` config field to root (`args` is passed straight to QEMU and can expose host resources, so Proxmox denies it to any non-root token regardless of role).

### Seed the chassis binaries on each Proxmox node

```bash
# From a machine that has the repo checked out:
scp autopilot-proxmox/scripts/seed_chassis_binaries.py root@<node>:/tmp/

# On the Proxmox node:
ssh root@<node> 'python3 /tmp/seed_chassis_binaries.py'
ssh root@<node> 'pvesm set local --content backup,iso,import,vztmpl,snippets'
```

The script writes binaries for a common set of chassis types (desktop, laptop, mini-PC, convertible, tablet, all-in-one, …) into `/var/lib/vz/snippets/`. Pass explicit types (`python3 /tmp/seed_chassis_binaries.py 3 10 31`) to seed only specific ones.

### Create a root@pam API token scoped to the `args` PUT

On the Proxmox host:

```bash
pveum user token add root@pam autopilot-args --privsep=0 \
    --comment "Autopilot args field only"
```

Proxmox prints the secret **once**. Add both halves to `inventory/group_vars/all/vault.yml`:

```yaml
vault_proxmox_root_api_token_id: "root@pam!autopilot-args"
vault_proxmox_root_api_token_secret: "<the-secret-you-just-got>"
```

Then restart the container (`docker compose up -d`). If you request a provision with a chassis override and haven't configured this token, the UI returns a 400 telling you exactly what's missing; the job never starts.

> **Blast radius.** `root@pam` tokens bypass Proxmox's role-based perm model entirely — a leaked secret is equivalent to root on the cluster. Autopilot uses this token only for the one PUT that writes `args` on a freshly cloned VM. Rotate the token if the vault is ever exposed:
> ```bash
> pveum user token remove root@pam autopilot-args
> pveum user token add root@pam autopilot-args --privsep=0
> ```

### Verify from the Docker host

```bash
curl -k https://<PROXMOX_IP>:8006/api2/json/version \
  -H "Authorization: PVEAPIToken=root@pam!autopilot-args=<SECRET>"
```

A 200 means the token works. 401 means the secret is wrong.

If you later add a new Proxmox node, repeat the seeding there. If provisioning returns a `chassis-type binary ... is not present` error, the message tells you exactly which of **(a)** storage config, **(b)** token privilege, or **(c)** missing file is the cause.

## 6. Provision devices and capture hashes

Once the template exists:

- **Provision VMs** — clone N VMs from the template with an OEM profile and an optional group tag.
- **Devices** — each cloned VM boots to OOBE. Use **Capture Hash** (or select several and bulk-capture) to extract the Autopilot hardware hash via the guest agent.
- **Upload to Intune** — batch-upload all captured hashes (requires Entra credentials in `vault.yml`).
- **Hash Files** — browse, download, delete CSVs in `./output/hashes/`.

## Task Sequences and Credentials

A **task sequence** is an ordered list of typed steps that describe what happens during OOBE — which OEM identity to set, which local admin to create, whether to Entra-join via Autopilot, whether to join an Active Directory domain, what scripts to run afterward. Sequences are first-class, editable objects, managed from the **Sequences** page; secrets referenced by steps live in the **Credentials** page.

### Seeded defaults

On first container start, three sequences are inserted if the DB is empty:

| Sequence | Produces hash? | What it does |
|----------|----------------|--------------|
| **Entra Join (default)** | yes | `set_oem_hardware` → `local_admin` → `autopilot_entra`. Reproduces the pre-sequences flow byte-for-byte — no behavior change unless you pick a different one. |
| **AD Domain Join — Local Admin** | no | `set_oem_hardware` → `local_admin` → `join_ad_domain` → `rename_computer`. Needs a `domain_join` credential filled in before first use. |
| **Hybrid Autopilot (stub)** | yes | Placeholder for Hybrid Autopilot. Builder lets you save it; the compiler refuses at provision time with a clear error. Not usable in v1. |

The seed migration is idempotent — it skips rows that already exist, so you can rename or edit the seeded sequences without them being recreated.

### Credentials page

Three credential types, each encrypted at rest with the key in `./secrets/credential_key`:

| Type | Fields | Use |
|------|--------|-----|
| `local_admin` | Username, Password | Local admin account created during OOBE. Referenced by the `local_admin` step. |
| `domain_join` | Domain FQDN, Username (`user@domain` or `DOMAIN\user`), Password, optional OU hint | Account used by `<UnattendedJoin>` during the specialize pass. Referenced by the `join_ad_domain` step. Use a delegated account with only "join computer to domain" rights on the target OU. |
| `odj_blob` | Uploaded `.bin` from `djoin.exe /provision` | Pre-staged offline-domain-join blob. Consumed by the (stub) Hybrid Autopilot step. |

The list view shows name, type, created/updated timestamps. Passwords are never returned to the browser — edit forms show a "leave blank to keep current" field. Deleting a credential that's referenced by any step returns a 409 and lists the referencing sequences.

#### Test connection (domain-join only)

The `domain_join` create/edit form has a **Test connection** button. On click, the container performs DNS SRV lookup for the domain's DCs, connects (prefers LDAPS on 636, falls back to LDAP+StartTLS on 389), binds with the supplied username/password, reads `rootDSE`, and — if you supplied an OU hint — resolves the OU DN.

The UI renders each stage as green/red with elapsed ms. The password is never echoed to the response. Certificate validation is controlled by the new settings flag `ad_validate_certs` (default `false` for lab use; turn on for production).

> **What the test does not prove:** that the account has *"join computer to domain"* rights on the OU. That requires an actual `Add-Computer` attempt. The button verifies bind + OU visibility only.

### Sequences page

**List view** shows name, description, step count, whether it's the default, whether it produces an Autopilot hash, last-used timestamp, and per-row Edit / Duplicate / Delete actions.

**Builder** (new or edit):

- Header: name, description, **is default** checkbox, **produces Autopilot hash** checkbox.
- Step list: each step is a collapsible card with type + one-line summary. Reorder with ↑/↓ buttons.
- Per-step form matches the step's param schema. Credential fields render as a dropdown of credentials of the correct type, plus a "+ new credential" shortcut.
- **Add step** dropdown at the bottom lists all step types. `autopilot_hybrid` shows a "Coming soon" badge and is disabled for new adds.

The builder emits *warnings* (not errors) for mutually-exclusive combinations such as `autopilot_entra` + `join_ad_domain`, or missing required credentials.

#### Step types (v1)

| Step type | Purpose |
|-----------|---------|
| `set_oem_hardware` | Pick an SMBIOS identity from `oem_profiles.yml` |
| `local_admin` | Create the local admin account (references a `local_admin` credential) |
| `autopilot_entra` | Inject the Entra-only `AutopilotConfigurationFile.json` |
| `autopilot_hybrid` | **Stub** — visible but non-executable in v1 |
| `join_ad_domain` | Join an AD domain during the specialize pass (references a `domain_join` credential + OU path) |
| `run_script` | Arbitrary PowerShell at first logon |
| `install_module` | `Install-Module` one-liner |
| `rename_computer` | `Rename-Computer` to the VM serial or a pattern |
| `wait_guest_agent` | Explicit sync point — wait until QEMU guest agent responds |

### Provision page — selecting a sequence

The Provision page has a **Task Sequence** dropdown between OEM Profile and VM Count. It defaults to whichever sequence has *is default* set.

Precedence for any attribute that can be set in multiple places:

```
vars.yml default  →  sequence step param  →  provision-form field
(lowest)                                     (highest)
```

A **blank** provision-form field inherits from the sequence; a **blank** sequence param inherits from `vars.yml`. The small `default: X` hint under each provision-form field tells you the effective value coming from the selected sequence, e.g. `"default (from sequence 'Entra Join'): dell-latitude-5540"`.

### Devices page — when hash capture is hidden

For VMs provisioned with a sequence whose *produces Autopilot hash* is off (the seeded AD Domain Join sequence, for example), the **Capture Hash** and **Capture & Upload** actions render as disabled with a tooltip explaining the sequence isn't an Autopilot sequence. The VM↔sequence link is persisted at provision time.

### Accepted risk

Compiled artifacts (`unattend.xml`, `SetupComplete.cmd`) contain plaintext secrets while the VM specializes — Windows reads and then strips these files from `C:\Windows\Panther\` as part of standard OOBE behavior. This is the `<UnattendedJoin>` mechanism as designed by Microsoft. Mitigation: use a delegated AD account with only "join computer to OU" rights, and keep the per-job sequence artifacts directory (`/app/jobs/<job_id>/sequence/`) in the named volume — it's scrubbed at job completion.

## Appendix A — Unattended install internals

The `autounattend.xml` answer file is mounted as a separate tiny ISO alongside the Windows ISO and VirtIO ISO. When the VM boots:

1. OVMF starts, Ansible sends a keypress to boot from CD.
2. Windows Setup finds `autounattend.xml` on the mounted ISO (volume label `OEMDRV`).
3. **WindowsPE pass** — creates GPT partitions, loads VirtIO drivers from the VirtIO ISO.
4. **Specialize pass** — sets computer name and timezone.
5. **OOBE pass** — skips all prompts, creates Administrator account, auto-logs in.
6. **FirstLogonCommands** — installs the QEMU guest agent from the VirtIO ISO.
7. Ansible detects the guest agent and proceeds with hash capture or sysprep.

The stock Windows ISO is never modified.

## Appendix B — Air-gapped / manual answer ISO build

If the in-UI **Rebuild Answer ISO** button isn't usable (e.g. you haven't configured the ISO storage ACL yet, or you need to build the ISO on a network-isolated Proxmox host), build it by hand:

```bash
# Copy the answer file to the Proxmox host
scp autopilot-proxmox/files/autounattend.xml root@<PROXMOX_IP>:/tmp/autounattend.xml

# On the Proxmox host:
apt-get install -y genisoimage
STORAGE_PATH=$(pvesm path isos:iso/autounattend.iso | sed 's|/autounattend.iso$||')
mkdir -p /tmp/answeriso
cp /tmp/autounattend.xml /tmp/answeriso/autounattend.xml
genisoimage -o "${STORAGE_PATH}/autounattend.iso" -J -r -V "OEMDRV" /tmp/answeriso/
rm -rf /tmp/answeriso /tmp/autounattend.xml
```

The volume label **must** be `OEMDRV` or Windows Setup won't find it.

## Appendix C — Ansible CLI

The web UI runs Ansible playbooks under the hood. You can also run them directly:

```bash
cd autopilot-proxmox
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Build template (one-time)
ansible-playbook playbooks/build_template.yml -e vm_oem_profile=generic-desktop

# Provision VMs
ansible-playbook playbooks/provision_clone.yml -e vm_oem_profile=lenovo-t14 -e vm_count=5

# Upload hashes to Intune
ansible-playbook playbooks/upload_hashes.yml

# Re-capture hash on an existing VM
ansible-playbook playbooks/retry_inject_hash.yml -e vm_vmid=106 -e vm_name=autopilot-106
```

Run tests:

```bash
cd autopilot-proxmox
pip install pytest ansible ansible-lint
pytest tests/ -v
ansible-playbook --syntax-check playbooks/*.yml
```
