# Task Sequences — Design Spec

**Date:** 2026-04-19
**Status:** Draft, pending user review
**Owner:** Adam

## 1. Context

Today the provisioning flow is hardwired to one OOBE outcome: Entra-joined via Windows Autopilot, with a single static `AutopilotConfigurationFile.json` and a single static `unattend_oobe.xml`. The hardware-identity dimension (which Lenovo/Dell model a VM pretends to be) is already profile-based via `oem_profiles.yml`; the OOBE/join dimension is not.

This spec introduces **Task Sequences** — a named, ordered list of typed steps that together walk a VM from "blank clone" to "fully provisioned and joined." The current hardcoded Entra-join flow becomes one seeded sequence, preserving today's behavior as the default.

## 2. Goals

- Support four OOBE outcomes in one tool: Entra Join (Autopilot), Hybrid Autopilot *(stub only — see §11)*, AD Domain Join, Workgroup / local-only.
- Make sequences first-class, CRUD-editable objects via the web UI.
- Keep AD domain credentials and ODJ blobs out of source and out of logs, encrypted at rest.
- Preserve zero-migration compatibility: an install with no sequences configured behaves identically to today.
- Keep hardware identity (`oem_profiles.yml`) and OOBE intent (sequences) orthogonal.

## 3. Non-goals

- Autopilot Self-Deploying / Pre-Provisioning modes (TPM attestation not fakeable on Proxmox).
- Conditional / branching steps.
- Re-running a sequence against an already-provisioned VM.
- Version history / audit log on sequence edits.
- Per-VM override of a sequence's step params for one run (duplicate the sequence for now).
- Sequence export/import as YAML/JSON.
- Localization beyond `en-US`.

## 4. Core concept

A **Task Sequence** is a persisted list of **Steps**. Each step is a typed action with its own parameter schema. Running a sequence against a VM means: for each step in order, compile it into an artifact (unattend fragment, Autopilot JSON, SMBIOS string, post-boot script line) or action (API call, wait), then apply it via the existing Ansible roles.

Steps are *additive, not mutually exclusive*. Some combinations don't make sense (`autopilot_entra` + `join_ad_domain`); the builder UI warns but does not forbid.

## 5. Step types (v1)

| Step type | Purpose | Compiles to | `causes_reboot` | Notes |
|---|---|---|---|---|
| `set_oem_hardware` | Pick SMBIOS identity from `oem_profiles.yml` | `_smbios1_string` on clone | no | Overridable by provision-UI OEM field (§12) |
| `local_admin` | Create local admin account during OOBE | `<UserAccounts>` + `<AutoLogon>` in oobeSystem | no | Password is a credential of type `local_admin` |
| `autopilot_entra` | Inject Entra-only `AutopilotConfigurationFile.json` | File written via `autopilot_inject` role | no | Today's default path |
| `autopilot_hybrid` | **Stub, v1.** Step visible in builder with "Coming soon" badge, not executable | — | — | §11 |
| `join_ad_domain` | Join computer to AD during OOBE specialize pass | `<Identification><JoinDomain>` + `<Credentials>` block in unattend specialize pass | no | Join happens *during* the specialize→OOBE reboot that Windows already performs — no extra reboot added. Uses `domain_join` credential + `ou_path` step param. |
| `run_script` | Arbitrary PowerShell at first logon | Script file + `RunOnce` key | caller-declared | |
| `install_module` | `Install-Module` one-liner | PS snippet in RunOnce | no | |
| `rename_computer` | `Rename-Computer` to serial or pattern | PS snippet | yes | |
| `wait_guest_agent` | Sync point — wait until QEMU GA responds | Ansible `wait_for` | no | Implicit between reboot steps, manual step available for clarity |

## 6. Data model

New SQLite tables, co-located with existing `devices_db.py` DB file:

```
task_sequences
  id INTEGER PRIMARY KEY
  name TEXT NOT NULL UNIQUE
  description TEXT
  is_default INTEGER NOT NULL DEFAULT 0          -- only one row may have is_default=1
  produces_autopilot_hash INTEGER NOT NULL DEFAULT 0
  created_at TIMESTAMP
  updated_at TIMESTAMP

task_sequence_steps
  id INTEGER PRIMARY KEY
  sequence_id INTEGER NOT NULL REFERENCES task_sequences(id) ON DELETE CASCADE
  order_index INTEGER NOT NULL
  step_type TEXT NOT NULL
  params_json TEXT NOT NULL DEFAULT '{}'
  enabled INTEGER NOT NULL DEFAULT 1
  UNIQUE (sequence_id, order_index)

credentials
  id INTEGER PRIMARY KEY
  name TEXT NOT NULL UNIQUE
  type TEXT NOT NULL                              -- 'domain_join' | 'local_admin' | 'odj_blob'
  encrypted_blob BLOB NOT NULL                    -- Fernet-encrypted JSON payload
  created_at TIMESTAMP
  updated_at TIMESTAMP
```

Access layer: `web/sequences_db.py` mirroring the `devices_db.py` pattern.

## 7. Credentials

**Encryption key location.** Separate from Ansible vault: `/app/secrets/credential_key` — a file containing a single Fernet key (base64, 44 bytes). Mounted into the container like `vault.yml` is today. Bootstrap: auto-generated on first container start if absent, with a WARNING logged and a `docker-compose.yml` snippet printed showing how to persist it. Rotating means replacing the file and re-running a migration command that decrypts/re-encrypts all rows.

**Credential payloads by type.**

- `domain_join` — `{domain_fqdn, username, password}`. Username is typically `user@domain` or `DOMAIN\user`. Delegated account with only "join computer to domain" rights on the target OU is recommended in the README.
- `local_admin` — `{username, password}`. Stored between runs so the same OOBE local-admin creds persist across provisions.
- `odj_blob` — `{blob_b64, source_hostname, generated_at}`. Uploaded `.bin` from `djoin.exe /provision`. Unused in v1 (hybrid stub).

**Redaction.**

- DB column `encrypted_blob` never returned to clients; UI gets `{id, name, type, created_at}` only.
- Ansible tasks that consume decrypted values run with `no_log: true`.
- Job log viewer redacts known secret shapes server-side before streaming: patterns for base64 ODJ blobs, `Password=` entries, and `password` JSON keys.
- Compiled artifacts (`unattend.xml`, `SetupComplete.cmd`) *do* contain plaintext secrets while the VM is specializing — Windows reads and then strips these files from `C:\Windows\Panther\` as part of standard OOBE behavior. Document this in the README as an accepted risk of the `UnattendedJoin` mechanism and recommend delegated accounts.

## 8. Web UI

### 8.1 `/sequences` — list

Columns: Name · Description · # Steps · Default? · Produces hash? · Last used · Actions (Edit / Duplicate / Delete).

"New sequence" button opens the builder.

### 8.2 `/sequences/new` and `/sequences/{id}/edit` — builder

- Header: name, description, `is_default` checkbox, `produces_autopilot_hash` checkbox.
- Step list: each step is a collapsible card. Header shows type + one-line summary of params. Reordering via ↑/↓ buttons (no drag in v1 — simpler, keyboard-accessible).
- Per-step form matches that step's param schema. Credential-type params render as a dropdown of existing credentials of the right type + a "+ new credential" shortcut that opens a modal.
- "Add step" dropdown at bottom with all step types; `autopilot_hybrid` shows a "Coming soon — not implemented" badge and is disabled for new adds but editable if already present (future-proofs existing sequences).
- Validation warnings (not errors): mutually exclusive steps (Autopilot + AD join), missing credentials, empty required params.
- Save is atomic — whole sequence (including step diffs) committed in one transaction.

### 8.3 `/credentials` — CRUD

- List columns: Name · Type · Created · Updated · Actions (Edit / Delete).
- New / Edit form is type-specific:
  - `domain_join`: Domain FQDN, Username, Password (new or "unchanged"), OU path hint (stored as a *suggestion*; actual OU lives on the step). **"Test connection" button** — see §8.6.
  - `local_admin`: Username, Password.
  - `odj_blob`: Upload `.bin` file; form shows file size + upload timestamp after save (blob internals are opaque / not parsed).
- Delete protected if referenced by any step's `params_json` → returns 409 with list of referencing sequences.

### 8.4 `/provision` — additions

New field between "OEM Profile" and "VM Count":

- **Task Sequence** dropdown — options are all sequences. Pre-selected: the row with `is_default=1`. Small inline text: `"default: <name>"`.

Existing "OEM Profile" dropdown stays. Behavior change: per §12, the provision-UI OEM value (if set) overrides any `set_oem_hardware` step in the sequence. Blank OEM field inherits from the sequence step.

### 8.5 Nav bar

Add `Sequences` and `Credentials` links alongside existing entries.

### 8.6 "Test connection" — `domain_join` credentials

A button on both the *new* and *edit* `domain_join` forms. On click, the browser POSTs to `/api/credentials/test-domain-join` with either:

- an unsaved form payload (when used on *new* before first save), or
- a `credential_id` (when used on *edit* — the stored password is decrypted server-side).

The test runs in the web container (which is on the LAN via `network_mode: host`) and performs:

1. **DNS SRV lookup** for `_ldap._tcp.<domain_fqdn>` → list of DC hostnames.
2. **Connect** to the first responsive DC, preferring LDAPS (636) then LDAP+StartTLS (389). Certificate validation is controlled by a new settings flag `ad_validate_certs` (default `false` for lab use; on for production).
3. **Bind** using the supplied username + password. Username may be `user@domain.fqdn` or `DOMAIN\user`; both are accepted.
4. **Read `rootDSE`** to confirm the server is a healthy directory service; record `defaultNamingContext` and `dnsHostName`.
5. **If** an OU path was supplied in the form, perform a scoped LDAP search for the OU DN to confirm it exists and is visible to the test account.

The response JSON reports stages that succeeded/failed with elapsed time per stage:

```json
{
  "ok": true,
  "dns": { "ok": true, "servers": ["dc01.example.local", ...], "elapsed_ms": 12 },
  "connect": { "ok": true, "server": "dc01.example.local", "tls": "ldaps", "elapsed_ms": 48 },
  "bind": { "ok": true, "elapsed_ms": 61 },
  "rootdse": { "ok": true, "defaultNamingContext": "DC=example,DC=local", "dnsHostName": "dc01.example.local" },
  "ou": { "ok": true, "dn": "OU=Workstations,DC=example,DC=local", "elapsed_ms": 37 }
}
```

The UI renders this as a stage-by-stage checklist (each stage green/red with its elapsed_ms). Errors show the server's error text (e.g. `invalidCredentials`, `noSuchObject`) but **never echo the submitted password**.

**Implementation:** new dependency `ldap3` (pure-Python, BSD). Request timeout: 8 seconds per stage, 30 seconds total. Test calls run with `no_log`-equivalent at the HTTP layer — the password on the submitted form is not written to access logs. DNS SRV lookup uses `dnspython` (already a transitive dep of `ldap3` in recent versions; explicit pin to be confirmed at implementation time).

**Scope cap:** the test proves *bind works and OU is visible*. It does **not** prove the account has "join computer to domain" rights on the OU — the only reliable check for that is an actual `Add-Computer` attempt, which is out of scope for a "test connection" button. The UI tooltip states this limitation explicitly.

### 8.7 Devices page — compatibility

For VMs that were provisioned with a sequence where `produces_autopilot_hash=false`:

- Both "Capture Hash" and "Capture & Upload" actions render as **disabled** with a tooltip: "Sequence '<name>' is not an Autopilot sequence — no hash is expected."
- `vmid → sequence_id` is persisted on provision via a new lookup table `vm_provisioning (vmid PK, sequence_id, provisioned_at)`. The Devices page joins on this table when rendering the capture actions.

## 9. Execution flow

When the user clicks **Provision**:

1. Web backend resolves `sequence_id` → ordered, enabled step rows.
2. **Compiler** iterates steps, each emitting into three buckets:
   - **Pre-clone args** — vars passed to Ansible (SMBIOS, disk size, counts).
   - **Unattend fragments** — a `{pass_name: [xml_snippet, ...]}` dict merged into `files/unattend_oobe.xml.j2`.
   - **Post-OOBE script lines** — Python list merged into a generated `SetupComplete.cmd`.
3. Credentials referenced by any step are **decrypted at this step** and written into the compiled artifacts. Plaintext values never touch DB rows or logs.
4. Compiled artifacts are written to a **per-job temp directory** (`/app/jobs/<job_id>/sequence/`): `unattend.xml`, `SetupComplete.cmd`, `vars.json` (non-secret step params). Secret-bearing artifacts are marked `0600`.
5. Backend invokes the existing `playbooks/provision_clone.yml` with `-e _sequence_artifacts_dir=<path>`.
6. `proxmox_vm_clone` role is extended to:
   - Use the compiled `unattend.xml` in place of the static `files/unattend_oobe.xml`.
   - Drop the compiled `SetupComplete.cmd` into the guest alongside the existing FixRecoveryPartition script.
   - Invoke `autopilot_inject` only when the sequence included `autopilot_entra` (tracked via `vars.json["autopilot"]["enabled"]`).
7. Per-step Ansible includes are gated off `vars.json` → no dead code runs for steps the sequence omits.
8. After the clone + Ansible run completes, the **reboot-aware waiter** (§10) supervises the remaining boot cycles.
9. Job log records each step's compile result and, where applicable, its Ansible task result.
10. On job completion the per-job sequence directory is scrubbed (artifacts containing secrets deleted).

## 10. Reboot tracking

Any step declaring `causes_reboot=true` tells the post-provision waiter: "expect the guest to drop off the agent once, then come back."

Implementation: a new helper `common/tasks/wait_reboot_cycle.yml` that:

1. Reads the current guest-agent `get-time` response (treated as a pre-reboot marker — monotonic boot-time in the guest).
2. Polls `get-time` until either (a) it fails consecutively N times (agent gone = rebooting), or (b) returned boot-time jumps backward / agent reports fresh session.
3. Then calls existing `wait_guest_agent.yml` to wait for the post-reboot agent.
4. Configurable total budget via `reboot_wait_timeout_seconds` (default 600).

The waiter is invoked once per `causes_reboot` step in sequence order. Steps marked `causes_reboot=true` in v1: `rename_computer`, plus any `run_script` where the user sets the step's `causes_reboot` param. `join_ad_domain` piggybacks on the specialize→OOBE reboot Windows already performs, so it does not add a waiter invocation.

## 11. Hybrid Autopilot — stub scope

Step type `autopilot_hybrid` is registered in the builder and accepts step params (`odj_blob` credential reference). A sequence *containing* a hybrid step can be saved, but at **provision time** the compiler refuses and returns a clear error: `"autopilot_hybrid step is not yet implemented in v1 — remove it or substitute autopilot_entra."`

Rationale: no AD+Entra tenant available to validate against. The UI affordance preserves the concept in users' minds and keeps the data model stable; the executable half lands in a follow-up.

## 12. Precedence rules

For any attribute that can be set in multiple places (OEM hardware, VM count, cores, memory, disk, serial prefix, group tag):

```
vars.yml default   →  sequence step param   →  provision-form field
(lowest)                                         (highest)
```

A filled-in provision-form field overrides the sequence. A **blank** form field does **not** count as "set" — it falls through to the sequence value. A blank sequence-step param falls through to `vars.yml`.

The provision page's existing per-field `<small>default: X</small>` hints are extended to show the effective default from the selected sequence, e.g. `"default (from sequence 'Entra Join'): dell-latitude-5540"`.

## 13. Seeded content

On first boot after the migration, three sequences are inserted if the `task_sequences` table is empty:

1. **"Entra Join (default)"** — `is_default=1`, `produces_autopilot_hash=1`. Steps: `set_oem_hardware` (from `vars.yml` `vm_oem_profile`), `local_admin` (references auto-seeded credential `"default-local-admin"` with username `Administrator` and the password currently hardcoded in `files/unattend_oobe.xml`), `autopilot_entra`. Compiled output is **byte-identical** to today's hardcoded flow. This is the no-regression guarantee.
2. **"AD Domain Join — Local Admin"** — `produces_autopilot_hash=0`. Steps: `set_oem_hardware`, `local_admin` (same default credential), `join_ad_domain` (empty `domain_join` credential reference; user must fill it in before first use), `rename_computer` (to serial).
3. **"Hybrid Autopilot (stub)"** — `produces_autopilot_hash=1`. Single `autopilot_hybrid` step. Non-executable per §11; present as a shape.

Seed migration is idempotent: keyed on sequence `name`, skips if already present.

## 14. Compatibility

- **Existing `vars.yml` installs with no sequences configured** — the seed runs, "Entra Join (default)" becomes the default sequence, provisioning output unchanged.
- **Existing `AutopilotConfigurationFile.json`** — kept as-is; `autopilot_inject` still reads from `files/` path when invoked by the `autopilot_entra` step.
- **Existing `oem_profiles.yml`** — unchanged; referenced by the `set_oem_hardware` step.
- **Existing `unattend_oobe.xml`** — becomes `unattend_oobe.xml.j2` with named Jinja blocks. A script in the implementation PR diff-checks rendered output against the old file using the default sequence, asserting byte-identical output.
- **Devices page "Capture Hash" actions** — conditionally disabled for non-Autopilot sequences (§8.6).

## 15. Testing

**Unit (pytest, `tests/` directory):**
- One test per step type: fixture input → expected compiled output. Covers the three buckets (pre-clone args, unattend fragments, post-OOBE lines).
- Compiler conflict-detection: sequences with mutually exclusive steps produce the warning list.
- Credential encrypt → store → decrypt round-trip.
- Delete-on-reference protection: attempting to delete a credential referenced by a step returns 409 + reference list.

**Integration (pytest + httpx against FastAPI app):**
- Save sequence via API → reload → compiled artifacts match expected.
- Seed migration on empty DB produces exactly 3 sequences.
- Provision-UI precedence: override OEM field on form → compiled SMBIOS matches the override, not the step.

**No-regression smoke test:**
- Using the seeded "Entra Join (default)" sequence and today's `vars.yml`, compiled `unattend.xml` and `SetupComplete.cmd` are byte-identical to `files/unattend_oobe.xml` and the current `SetupComplete.cmd` respectively.

## 16. Implementation slicing (for the subsequent plan)

Rough order, each a shippable slice:

1. Data model + `sequences_db.py` + migration + credentials encryption helper.
2. `/credentials` CRUD page + API.
3. `/sequences` CRUD pages + API (without compiler wiring yet).
4. Seed migration running on container start.
5. Compiler: `set_oem_hardware`, `local_admin`, `autopilot_entra` — enough to reproduce today's default byte-identically.
6. Wire `provision_clone.yml` to accept `_sequence_artifacts_dir`; drop static unattend usage.
7. Compiler: `join_ad_domain`, `rename_computer`, `run_script`, `install_module`.
8. Reboot-aware waiter (`wait_reboot_cycle.yml`).
9. Devices-page capture-action conditional disable.
10. `autopilot_hybrid` stub affordance.
11. "Test connection" for `domain_join` credentials (§8.7).
12. Tests throughout.

## 17. Open items flagged during brainstorming (decided, recorded here)

| # | Decision |
|---|---|
| Q1 | Hybrid is stub-only in v1 (UI present, compiler refuses). |
| Q2 | AD domain join via `<UnattendedJoin>` in unattend specialize pass. DNS/LDAP on this network resolves quickly. |
| Q3 | Hash capture actions shown but disabled for non-Autopilot sequences (tooltip). |
| Q4 | Reboot detection via QEMU GA boot-time cycle, not fixed timeout bump. |
| Q5 | Credential key at `/app/secrets/credential_key`, mounted separately from Ansible vault. |
| Q6 | Local admin password is a first-class `local_admin` credential, reused across runs. |
| Q7 | Deleting a referenced credential/sequence is blocked with explicit unlink message. |
| Q8 | Precedence: `vars.yml` → sequence step → provision UI. Blank inherits. |
