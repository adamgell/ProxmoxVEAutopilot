# Device State Monitoring — Design Spec

**Date:** 2026-04-20
**Status:** Proposed — awaiting review
**Scope:** Autopilot-provisioned Windows VMs on the PVE cluster whose AD
computer object lives under one of the configured search OUs (seeded
with `OU=WorkspaceLabs,DC=home,DC=gell,DC=one`).

## Why

After a VM is provisioned we have no ongoing visibility into whether
it stays registered in the places it should be (PVE, AD, Entra,
Intune), or whether those registrations drift (the VM gets migrated
to another node, a hybrid object goes stale, compliance flips, a
device re-enrolls with a new Entra ID). We want a dashboard that
joins PVE VM state against all three directories on a schedule,
records every state change so regressions are visible, and exposes a
per-device page showing all four systems side-by-side.

## Non-goals

- Not a remediation engine. We record; we do not re-enroll, re-join, or
  delete stale objects.
- Not a compliance dashboard for non-VM endpoints. Physical laptops
  enrolled in the same tenant are invisible to this view.
- Not real-time. 15-minute polling is the design target; sub-minute
  latency is not required.

## Matching keys

Each VM has identifiers across four surfaces:

| Source       | Identifier                            | Read from                         |
|--------------|---------------------------------------|-----------------------------------|
| PVE          | `vmid` (stable across the VM's life)  | pvesh / qm list                   |
| PVE          | `name`, `node`, `status`, `tags`      | `qm config <vmid>` + cluster API  |
| PVE          | `vmgenid`, smbios1.uuid, smbios1.serial | `qm config <vmid>`              |
| PVE          | sequence_id, provision job_id         | local `vm_provisioning` table + `jobs` |
| Windows      | `Win32_BIOS.SerialNumber`             | guest-exec (already implemented)  |
| Windows      | `Win32_ComputerSystemProduct.UUID`    | guest-exec                        |
| Windows      | `Win32_ComputerSystem.Name`           | guest-exec                        |
| AD           | `objectGUID` (stable identity)        | LDAP under configured OUs         |
| AD           | `objectSid`                           | LDAP — joins AD ↔ Entra hybrid   |
| AD           | `distinguishedName`, `cn`, `sAMAccountName` | LDAP                        |
| Entra        | `id`, `deviceId`, `trustType`, `onPremisesSecurityIdentifier` | Graph `/v1.0/devices` |
| Intune       | `id`, `serialNumber`, `azureADDeviceId`, `complianceState` | Graph `/v1.0/deviceManagement/managedDevices` |

Join path end-to-end:

```
PVE.vmid
  → PVE.smbios1.serial == Windows.SerialNumber == Intune.serialNumber
  → Windows.ComputerName  == AD.cn / sAMAccountName (bare)
                            == Entra.displayName
  → AD.objectSid          == Entra.onPremisesSecurityIdentifier (hybrid only)
  → Intune.azureADDeviceId == Entra.deviceId
```

Within AD, `objectGUID` is the durable key (survives rename + OU
move). Across AD ↔ Entra, the SID is the hybrid-link key. Across
Entra ↔ Intune, `azureADDeviceId == Entra.deviceId` is the link.

## Scope restriction

AD LDAP searches run against every enabled entry in the configurable
`monitoring_search_ous` table (see schema below). Each DN uses
`SCOPE_SUBTREE`, results are unioned, and matches are tagged with the
source DN they came from so the UI can render "found under …". The
list is seeded with `OU=WorkspaceLabs,DC=home,DC=gell,DC=one` and the
settings UI lets operators add, remove, reorder, or disable entries —
with the invariant that at least one entry must remain enabled at all
times. Devices outside every listed subtree are not recorded.

Entra and Intune have no equivalent OU concept, so we instead:

1. Enumerate VMs in PVE (the universe of things we monitor).
2. For each VM, query Graph by the VM's own identifiers.
3. A matched Entra/Intune object is "in-scope" iff its linked AD device
   (by `physicalIds` / `onPremisesSyncEnabled`) resolves to a DN under
   the scope OU, **or** the VM itself is in PVE and matches the Entra
   device's displayName.

In practice, since we start from PVE VMIDs and key off SMBIOS serial
(which we set per-VM at clone time), every Entra/Intune match is
implicitly tied to a VM we own.

## Duplicate handling (required)

Hybrid-join and re-enrollment routinely leave multiple objects:

- Two AD computers with the same CN (one disabled, one active).
- Two Entra devices with the same `displayName`, different `deviceId`
  (one `Pending`, one `Registered`).
- One serial enrolled in Intune twice (old MDM leftovers).

The schema stores **all matches**, not just the first. A per-probe
`matches_json` column on each source row holds an array of the raw
objects returned from the directory, so the UI can show "2 AD objects
found" without the collector having to pick a winner.

**No assertions of uniqueness anywhere in the collector.** If a
duplicate appears during provisioning and triggers today's `len(...) == 1`
assertion somewhere, it's a bug — the whole monitor has to keep running.

## PVE lifecycle tracking

Every sweep also captures a snapshot of each in-scope VM's PVE-side
state. "In-scope" for PVE = VMs tagged `autopilot` OR VMs with a row
in the existing `vm_provisioning` table. Config-level changes between
consecutive snapshots produce lifecycle events on the timeline.

**Captured fields (per VM per sweep):**

- `vmid`, `node`, `name`, `status` (running / stopped / paused /
  suspended), `tags` (sorted CSV), `lock` (if set).
- `cores`, `sockets`, `memory_mb`, `balloon_mb`.
- `machine` (`pc-q35-10.1` etc.), `bios` (seabios / ovmf),
  `smbios1` (verbatim), `args` (verbatim — where the per-VM SMBIOS
  file path lives).
- `vmgenid`.
- Disks: JSON array of `{bus, index, storage, size_bytes, serial}` —
  serial matters because our sequence 2 sets a specific SCSI serial
  (e.g., `APHV000109FC718DFBB8`) for Autopilot stability.
- Network: JSON array of `{index, model, bridge, mac, firewall, vlan}`.
- Provisioning linkage (one-shot, joined at read time, not
  re-captured every sweep): `sequence_id` from `vm_provisioning`, the
  matching `provision` job id from `jobs`, and its `ended` time.

A `config_digest` = `sha256(canonical_json(snapshot))` lets the
transition detector cheaply say "no config change since last sweep"
without a field-by-field compare.

**PVE lifecycle events (computed read-time from consecutive PVE snapshots):**

| From → To                                     | Event type           |
|-----------------------------------------------|----------------------|
| status running → stopped                      | power-off            |
| status stopped → running                      | power-on             |
| status → paused / suspended                   | paused / suspended   |
| node A → node B                               | migration            |
| any config_digest change (not status-only)    | config-changed       |
| args string change (e.g., SMBIOS file path)   | smbios-reconfig      |
| disk added / removed                          | disk-attached / detached |
| tag `autopilot` removed                       | untagged (triggers scope-drop warning) |
| vm no longer present in `qm list`             | deleted              |

PVE events are stored alongside probe transitions on the per-device
timeline. A `deleted` event closes out the timeline but the
historical probe rows are retained indefinitely (append-only DB).

## Data model

Single new SQLite DB: `web/data/device_monitor.db`. Append-only except
for the `settings` row.

```sql
-- One row per monitoring sweep pass. A sweep is "try every running
-- Windows VM at time T"; individual per-VM probes link to it so we
-- can reason about "what did we know at 22:15Z".
CREATE TABLE monitoring_sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,  -- ISO8601 UTC
    ended_at   TEXT,
    vm_count   INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '{}'  -- high-level (e.g., Graph token failure)
);

-- One row per (sweep, VM). Captures PVE-side config state at sweep
-- time. Lifecycle events (power on/off, migration, config change)
-- are computed read-time by diffing consecutive snapshots for the
-- same vmid. config_digest lets the diff short-circuit when nothing
-- changed.
CREATE TABLE pve_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sweep_id   INTEGER NOT NULL REFERENCES monitoring_sweeps(id) ON DELETE CASCADE,
    checked_at TEXT    NOT NULL,
    vmid       INTEGER NOT NULL,
    present    INTEGER NOT NULL DEFAULT 1,  -- 0 = "no longer in qm list"
    node       TEXT,
    name       TEXT,
    status     TEXT,                         -- running / stopped / paused / suspended
    tags_csv   TEXT NOT NULL DEFAULT '',     -- sorted, comma-joined
    lock_mode  TEXT,                         -- null, migrate, backup, snapshot, etc.
    cores      INTEGER,
    sockets    INTEGER,
    memory_mb  INTEGER,
    balloon_mb INTEGER,
    machine    TEXT,
    bios       TEXT,
    smbios1    TEXT,                         -- raw
    args       TEXT,                         -- raw
    vmgenid    TEXT,
    disks_json TEXT NOT NULL DEFAULT '[]',   -- array of {bus,index,storage,size_bytes,serial}
    net_json   TEXT NOT NULL DEFAULT '[]',   -- array of {index,model,bridge,mac,firewall,vlan}
    config_digest TEXT NOT NULL,             -- sha256 of canonical snapshot JSON
    probe_error TEXT                          -- set if the qm config call failed
);
CREATE INDEX idx_pve_vmid_time ON pve_snapshots (vmid, checked_at DESC);

-- One row per (sweep, VM). The canonical "what's the state right now".
CREATE TABLE device_probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sweep_id   INTEGER NOT NULL REFERENCES monitoring_sweeps(id) ON DELETE CASCADE,
    checked_at TEXT    NOT NULL,
    vmid       INTEGER NOT NULL,
    vm_name    TEXT,
    -- From guest-exec (may be null if agent down)
    win_name   TEXT,
    serial     TEXT,
    uuid       TEXT,
    os_build   TEXT,
    dsreg_status TEXT,  -- JSON: selected lines from dsregcmd /status
    -- AD
    ad_found        INTEGER NOT NULL DEFAULT 0,
    ad_match_count  INTEGER NOT NULL DEFAULT 0,
    ad_matches_json TEXT    NOT NULL DEFAULT '[]',
    -- Entra
    entra_found        INTEGER NOT NULL DEFAULT 0,
    entra_match_count  INTEGER NOT NULL DEFAULT 0,
    entra_matches_json TEXT    NOT NULL DEFAULT '[]',
    -- Intune
    intune_found        INTEGER NOT NULL DEFAULT 0,
    intune_match_count  INTEGER NOT NULL DEFAULT 0,
    intune_matches_json TEXT    NOT NULL DEFAULT '[]',
    -- Error bag (per source, null if ok)
    probe_errors_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_probe_vmid_time ON device_probes (vmid, checked_at DESC);

-- Single-row scalar settings (id is a sentinel).
CREATE TABLE monitoring_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled          INTEGER NOT NULL DEFAULT 1,
    interval_seconds INTEGER NOT NULL DEFAULT 900,   -- 15 min
    ad_credential_id INTEGER NOT NULL DEFAULT 7,     -- home\adam_admin
    updated_at       TEXT    NOT NULL
);

-- Additive list of AD search OUs. Sweeps query each OU (SCOPE_SUBTREE)
-- and union the results. The "always at least one" invariant is
-- enforced at the API/DAL layer, not the schema (SQLite can't express
-- "row count >= 1" cleanly), but init seeds the default row and the
-- delete endpoint refuses to remove the last remaining entry.
CREATE TABLE monitoring_search_ous (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dn TEXT NOT NULL UNIQUE,       -- full DN, e.g. "OU=WorkspaceLabs,DC=home,DC=gell,DC=one"
    label TEXT NOT NULL DEFAULT '', -- optional human label shown in UI
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

### Search-OU list invariants

- `monitoring_search_ous` is seeded at init with one row:
  `dn='OU=WorkspaceLabs,DC=home,DC=gell,DC=one'`, `label='WorkspaceLabs'`.
- `dal.delete_search_ou(id)` raises `CannotDeleteLastOu` when the delete
  would leave the table empty, **OR** when it would leave zero `enabled=1`
  rows. The API returns 409 in that case.
- `dal.disable_search_ou(id)` follows the same rule — you can't disable
  the last enabled row.
- `sweep()` loads `SELECT dn FROM monitoring_search_ous WHERE enabled=1
  ORDER BY sort_order, id` and runs one LDAP search per DN; results are
  concatenated into `ad_matches_json` with each match tagged with its
  source OU so the UI can show "found under OU X".
- DN format is validated on insert/update with a lightweight regex
  (`^(?:OU|CN|DC)=[^,]+(?:,(?:OU|CN|DC)=[^,]+)*$`). Invalid → 400.

## Probes

### AD probe

Using `ldap3` (new dependency — pure-Python, no system libs). One bind
per sweep; one `search()` per enabled search-OU row:

```python
conn = Connection(Server('dns.home.gell.one'),
                  user='home\\adam_admin', password='...', auto_bind=True)
matches = []
for ou in dal.list_enabled_search_ous(db):
    conn.search(
        search_base=ou.dn,
        search_filter=f'(&(objectClass=computer)(name={escape_filter_chars(win_name)}))',
        search_scope=SUBTREE,
        attributes=[
            # Durable identity WITHIN AD — survives renames + OU moves.
            'objectGUID',
            # objectSid is the cross-directory key: Entra's hybrid
            # devices expose this exact SID as
            # onPremisesSecurityIdentifier, which is how we join
            # AD → Entra for trustType=ServerAd rows.
            'objectSid',
            # Name triplet — all three can change independently. We
            # store each so rename-in-place (CN/sAMAccountName edit)
            # and OU moves (DN change only) are both detectable.
            'distinguishedName', 'cn', 'sAMAccountName', 'name',
            # Lifecycle / health.
            'userAccountControl', 'whenCreated', 'whenChanged',
            'pwdLastSet', 'lastLogonTimestamp',
            # OS strings from the directory (may diverge from the
            # guest's actual Win32 values — divergence is a signal).
            'operatingSystem', 'operatingSystemVersion',
            # dNSHostName = FQDN registered in AD; useful for DNS
            # reconciliation separately from the computer name itself.
            'dNSHostName',
        ],
    )
    for e in conn.entries:
        matches.append({**_entry_to_dict(e), 'source_ou_dn': ou.dn,
                        'source_ou_label': ou.label})
```

`objectGUID` is the stable key for tracking the same AD object across
renames and OU moves. `sAMAccountName` (always ends with `$`), `cn`,
and the parent container inside `distinguishedName` are the three
axes of change the UI surfaces:

- **CN/sAM rename**: `objectGUID` unchanged, `cn` or `sAMAccountName`
  differs from the prior probe.
- **OU move**: `objectGUID` unchanged, DN prefix unchanged but the
  parent container (everything after the first comma) differs.
- **Replacement**: `objectGUID` changed — the name matched a new
  object (likely a disable-and-recreate, or a duplicate).

Return ALL matches from ALL OUs — the collector does not de-dup by DN
(the same computer object under two overlapping OUs will show twice,
which is correct: the UI can show that too). `userAccountControl` bit 2
(`ACCOUNTDISABLE`) is preserved so the UI can render "1 active + 1
disabled". If a search fails on one OU (permissions, typo), the error
is recorded in `probe_errors_json.ad_per_ou[<dn>]` and the remaining
OUs still run.

### Entra probe

```
GET https://graph.microsoft.com/v1.0/devices?$filter=displayName eq '<win_name>'
    &$select=id,displayName,deviceId,trustType,accountEnabled,
             approximateLastSignInDateTime,operatingSystem,
             operatingSystemVersion,registrationDateTime,deviceOwnership,
             onPremisesSyncEnabled,onPremisesLastSyncDateTime,
             onPremisesSecurityIdentifier,physicalIds,alternativeSecurityIds
```

`trustType` maps to:
- `AzureAd` → Entra-only joined
- `ServerAd` → **Hybrid-joined** (what we expect for sequence 2)
- `Workplace` → Registered (not joined)

**Hybrid linkage.** For `trustType=ServerAd`, the collector stores
`onPremisesSecurityIdentifier` (SID). On the AD side we already pull
`objectSid`. A link is considered strong when those two strings match
exactly; the `/monitoring/<vmid>` detail page renders the link as
"AD ↔ Entra linked" when they do, or "Entra displayName matches but
SID ≠ AD objectSid — investigate" when they don't (that's the shape of
a stale Entra object surviving an AD recreation).

All rows stored; `entra_match_count` > 1 is an info banner, not an
error.

**Sync-lag tolerance.** A newly-hybrid-joined device only appears in
Entra after AD→Entra Connect runs (~30 min cadence). `entra_found=0`
within 45 minutes of the first AD match for the same VMID is treated
as a transient "sync pending" state — UI renders ⏳ not ❌, and the
regression detector does **not** fire `entra_found=0 → 1` as a
"joined" progression until the ⏳ window expires or the device
actually lands in Entra. The 45-minute window is hard-coded in the
detector (not a user setting — shorter values fight the upstream
sync cadence).

### Intune probe

```
GET https://graph.microsoft.com/v1.0/deviceManagement/managedDevices?$filter=serialNumber eq '<serial>'
    &$select=id,deviceName,serialNumber,azureADDeviceId,enrolledDateTime,
             complianceState,lastSyncDateTime,operatingSystem,managedDeviceOwnerType
```

## Collector

New module `web/device_monitor.py`:

- `probe_pve(vmid, ctx) -> dict` — calls `qm config <vmid>` via
  Proxmox API, normalises into the `pve_snapshots` shape, computes
  the config_digest. Runs for every in-scope VM, whether the guest
  agent is up or not.
- `probe_vm(vmid, ctx) -> dict` — runs the AD / Entra / Intune
  probes for one VM. Any single-source exception is caught and
  stored in `probe_errors_json[source]`; the others still run.
  Guest-exec is only attempted if the VM's PVE status is `running`.
- `sweep(ctx) -> sweep_id` — enumerates in-scope VMs (tagged
  `autopilot` OR present in `vm_provisioning`). For each: calls
  `probe_pve`, then `probe_vm` when the VM is running. Inserts one
  `monitoring_sweeps` row + N `pve_snapshots` rows + N
  `device_probes` rows in one transaction. VMs that have dropped
  out of `qm list` but existed in the previous sweep get a
  `pve_snapshots` row with `present=0` so the timeline records the
  deletion.
- `start_background_loop(app)` — asyncio task registered in the
  FastAPI startup hook. Sleeps `interval_seconds` between sweeps,
  re-reads interval from DB on every loop so UI changes take effect
  on the next tick.
- Cancellation-safe: swallows `asyncio.CancelledError` cleanly on
  shutdown.

A Graph access token is cached per process and refreshed when it's
within 5 min of expiry; LDAP connections are per-sweep, closed after.
Proxmox API calls reuse the existing token-auth helper
(`web.app._proxmox_api`).

## Regression detection

Computed at read time (not stored). For the detail view, load the last
N probes for a VMID ordered by `checked_at`. Compare each probe to its
predecessor and flag transitions:

Transitions are matched **per AD object** using `objectGUID` as the
stable key, so a rename or OU move isn't misread as "gone + new".

| From → To                                                     | Severity   |
|---------------------------------------------------------------|------------|
| ad_found=1 → 0 (no object with same GUID anywhere)            | regression |
| ad: userAccountControl ACCOUNTDISABLE bit 0 → 1               | regression |
| ad: cn / sAMAccountName changed (same objectGUID)             | rename     |
| ad: parent DN changed (same objectGUID, different OU)         | ou-move    |
| ad: objectGUID changed for same VMID+winName                  | replacement (regression) |
| ad.objectSid ≠ entra.onPremisesSecurityIdentifier (trustType=ServerAd) | link-broken (regression) |
| entra_trustType ServerAd → (nothing, or AzureAd)              | regression |
| entra: deviceId changed for same VMID                         | replacement |
| entra_found=0 within 45 min of first AD match                 | sync-pending (informational, not a regression) |
| intune_found=1 → 0                                            | regression |
| complianceState compliant → noncompliant                      | regression |
| ad_found=0 → 1                                                | progression |
| entra_found=0 → ServerAd                                      | progression |
| intune_found=0 → 1                                            | progression |

The `/monitoring` page shows only the latest probe per VMID; the
`/monitoring/<vmid>` detail page shows the timeline with coloured
transitions and, for AD, a per-`objectGUID` track so a rename reads as
one object evolving rather than "deleted + created".

## UI

Three surfaces:

### `/monitoring` — dashboard

Table, one row per VM: VMID, VM name, PVE status (running / migrated /
deleted), last checked, AD ✅/⚠️/❌ (✅=1 active, ⚠️=1 disabled or >1
matches, ❌=none), Entra (same, plus ⏳ for sync-pending), Intune
(same), latest regression if any. Each directory cell is clickable
and opens a popover listing every match with its source OU (for AD) or
its raw Graph object (Entra / Intune).

### `/devices/<vmid>` — per-device side-by-side detail page

Replaces and extends the current `/vms/<vmid>` rendering. Top of page
is a four-column "current state" card, one column per system,
populated from the most-recent `pve_snapshots` row and the most-recent
`device_probes` row for this vmid. Fields shown:

| PVE column           | AD column                  | Entra column             | Intune column             |
|----------------------|----------------------------|--------------------------|---------------------------|
| vmid, name, node     | objectGUID (short)         | id, deviceId             | id                        |
| status + lock        | DN                         | displayName              | deviceName                |
| tags                 | cn / sAMAccountName        | trustType badge          | complianceState badge     |
| cores / memory / disk| OU container               | onPremisesSyncEnabled    | enrolledDateTime          |
| smbios.serial / uuid | objectSid                  | onPremisesSecurityIdentifier | serialNumber         |
| machine / bios       | userAccountControl flags   | accountEnabled           | managedDeviceOwnerType    |
| args (truncated)     | pwdLastSet, lastLogon      | registrationDateTime     | lastSyncDateTime          |
| sequence + job link  | whenCreated / whenChanged  | physicalIds (joined)     | azureADDeviceId           |

Cross-column health strip sits above the columns showing the linkage
keys and whether they match:

- SMBIOS serial → Intune serialNumber: ✅ / ❌
- Windows name → AD cn : ✅ / ❌ / ⚠️ (case-mismatch)
- AD.objectSid → Entra.onPremisesSecurityIdentifier (hybrid only): ✅ / ❌ / ⏳
- Entra.deviceId → Intune.azureADDeviceId: ✅ / ❌

Below the four columns: **unified timeline**. One chronological list
(newest first, paginated 50 per page) merging PVE lifecycle events,
directory transitions, and provision/job entries. Each row has a
coloured source icon (PVE / AD / Entra / Intune / Autopilot), a type
badge (power-on, rename, ou-move, compliance-flip, link-broken, …),
the old→new delta, and a click-to-expand with the raw JSON diff of the
underlying snapshot / probe rows.

At the bottom of the page: **raw data explorer** — collapsible
sections letting an operator see the last N `pve_snapshots` rows and
the last N `device_probes.*_matches_json` blobs for this vmid
directly, for when something funky is going on and the summary hides
the detail.

### Timestamp rendering

Every timestamp in the system is stored as an ISO 8601 string with an
explicit UTC offset (`2026-04-20T23:52:14+00:00`). The server never
renders a formatted date — it emits the raw UTC string inside a
`<time>` element with the UTC value also in a data attribute, e.g.

```html
<time datetime="2026-04-20T23:52:14+00:00"
      data-utc="2026-04-20T23:52:14+00:00">—</time>
```

A tiny page-level script (loaded on every monitoring + devices page)
walks every `time[data-utc]` on `DOMContentLoaded` and on history
change, replaces the text node with
`new Date(utc).toLocaleString(undefined, { ... })` using the
browser's resolved locale and timezone, and sets `title` to the raw
UTC string so hovering shows the source value. An `.relative` class
switches between absolute ("Apr 21 00:09:14") and relative
("12 min ago") formats — timeline rows use `.relative` by default,
the detail panels use absolute. Log exports keep the UTC original
verbatim regardless of what the browser rendered.

No server-side timezone config. The viewer's browser is authoritative
for presentation; UTC is authoritative for storage and diffs.

### `/monitoring/settings`

Two sections:

1. **Monitor configuration** — enable toggle, interval seconds
   (min 60, banner below 900: "not tested below 15 minutes"), AD
   credential selector.
2. **AD search OUs** — table of DN / label / enabled / sort order
   with add / edit / enable-toggle / delete / reorder controls. Save
   per-row writes `monitoring_search_ous`. The delete and disable
   controls are grayed out on the last remaining enabled row, with a
   tooltip explaining "at least one OU must stay enabled." A
   server-side 409 is the backstop if the UI state drifts. DN input
   is regex-validated client-side with the same pattern the DAL
   enforces.

## Failure modes

| Failure                              | Behaviour                                                 |
|--------------------------------------|-----------------------------------------------------------|
| Graph token refresh fails            | Sweep still runs; `errors_json.graph_auth` set; all Entra + Intune probes recorded as `skipped` |
| LDAP DC unreachable                  | `errors_json.ldap` set; AD probes skipped, Entra + Intune continue |
| Guest agent down on VM               | Only PVE-side fields (vmid, vm_name) populated; directory probes skipped for that VM, stored as `probe_errors_json.guest='agent_down'` |
| VM stopped                           | Still probed by name + last-known serial (if we've seen it before) so an unexpectedly-stopped VM doesn't drop out of the dashboard |
| Duplicate directory objects          | All stored in `*_matches_json`; UI marks as ⚠️              |
| Search-OU permission denied (one DN) | Recorded in `probe_errors_json.ad_per_ou[dn]`; remaining OUs still run |
| User tries to delete / disable the last enabled OU | DAL raises `CannotDeleteLastOu`; API returns 409; UI greys the button |
| Hybrid-joined in AD but not yet in Entra (sync lag) | UI renders ⏳ "sync pending"; regression detector suppresses the transition for the first 45 min after the first AD match |
| Entra displayName match but SID ≠ AD objectSid | Recorded as `link-broken`; UI flags "investigate"; common signature of a stale Entra device outliving a recreated AD object |
| Collector raises unhandled exception | Caught at the loop level; logged; next tick still runs    |

## Testing

- Unit: `device_monitor.probe_vm` given a fake PVE/LDAP/Graph context
  — assert matches arrays land with correct shapes for 0/1/2 matches.
- Unit: regression detector — feed pairs of probes, assert flag.
- Unit: `dal.delete_search_ou` / `disable_search_ou` raise
  `CannotDeleteLastOu` when called on the sole remaining enabled row.
- Unit: `sweep()` with two configured OUs runs two LDAP searches and
  merges results, tagging each match with its source DN.
- Unit: regression detector preserves AD-object continuity across a
  rename (same objectGUID, different cn) and an OU-move (same GUID,
  different parent), and emits `replacement` when the GUID does change.
- Unit: regression detector emits `link-broken` when the Entra
  `onPremisesSecurityIdentifier` ≠ the AD `objectSid` on the latest
  probe with `trustType=ServerAd`.
- Unit: regression detector suppresses `entra_found=0 → 1` within the
  45-min post-AD-join sync-pending window and renders ⏳ instead.
- Integration: against the live cluster, one sweep against
  `OU=WorkspaceLabs` — assert VM 109 and 116 appear with `ad_found=1`,
  `entra_trustType=ServerAd`.
- No live test runs in CI (cluster-local only).

## Delivery steps (commit boundaries)

1. `ldap3` dependency + `web/device_history_db.py` — schema (including
   `monitoring_search_ous`), DAL, seeding of the default OU, and the
   `CannotDeleteLastOu` invariant + tests.
2. `web/device_monitor.py` — probe functions (one LDAP search per
   enabled OU, unioned) + sweep; tests against a fake context
   exercising 1-OU and 2-OU configurations.
3. FastAPI startup hook + background loop + settings API endpoints
   (scalar settings + CRUD for search OUs, all guarded by the
   last-enabled invariant).
4. `/monitoring` list page + template — AD cell popover lists matches
   per source OU.
5. `/monitoring/<vmid>` timeline + regression detector.
6. `/monitoring/settings` page — scalar form + additive OU list editor
   with client-side "can't remove last enabled" affordances.
7. Live run against the cluster; adjust for whatever duplicates exist.

Each step commits independently and the system is usable after step 3
(UI added in 4-5).

## Open questions

None — scope (OU), interval default (15 min with UI override), duplicate
tolerance (store all), and credential sources (cred 7 for AD, existing
vault Entra app for Graph) all confirmed in prior conversation.

## Appendix A — /devices/<vmid> mockup

Timestamps below are shown in UTC for faithfulness to the server-side
storage. In the live UI every `<time>` element is converted to the
viewer's browser locale + timezone on load (see **Timestamp
rendering** above); a user in America/New_York sees `2026-04-20
19:52:14 EDT`, a user in Europe/Berlin sees `2026-04-21 01:52:14
CEST`, a user in UTC sees the stored value unchanged, and hover
always reveals the canonical UTC original.

### Healthy state (VM 116, real data from 2026-04-20 live run)

```
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│  ⌂ Devices › 116 › Gell-EC41E7EB                          [ Refresh ] [ Probe now ] [ ⚙ ] │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│  Linkage health                                                                              │
│   SMBIOS.serial ──✓── Intune.serialNumber                  Gell-EC41E7EB                     │
│   Windows.Name  ──✓── AD.cn                                GELL-EC41E7EB                     │
│   AD.objectSid  ──✓── Entra.onPremSecurityIdentifier      S-1-5-21-4163347863-…-4829        │
│   Entra.deviceId ──✓── Intune.azureADDeviceId             a6c91d4e-3f21-4891-9d44-…         │
├────────────────────────┬─────────────────────────┬─────────────────────────┬─────────────────┤
│ PVE                    │ Active Directory        │ Entra (Azure AD)        │ Intune          │
├────────────────────────┼─────────────────────────┼─────────────────────────┼─────────────────┤
│ vmid    116            │ objectGUID              │ id                      │ id              │
│ name    Gell-EC41E7EB  │  7d41fbb4-b239-9ce6-…   │  ab56021c-b01a-485e-…   │  f8153fe8-24e7… │
│ node    pve2           │                         │                         │                 │
│ status  ● running      │ DN                      │ displayName             │ deviceName      │
│ lock    —              │  CN=GELL-EC41E7EB,      │  GELL-EC41E7EB          │  GELL-EC41E7EB  │
│ tags    autopilot      │  OU=Devices,            │                         │                 │
│                        │  OU=WorkspaceLabs,      │ trustType               │ complianceState │
│ cores     2            │  DC=home,DC=gell,DC=one │  ServerAd (Hybrid) 🛡️   │  compliant ✅   │
│ memory    4096 MB      │                         │                         │                 │
│ disk      64 GB (scsi0)│ cn / sAM                │ onPremSyncEnabled true  │ enrolled        │
│                        │  GELL-EC41E7EB          │ onPremLastSync          │  2026-04-20     │
│ smbios                 │  GELL-EC41E7EB$         │  2026-04-20 23:52 UTC   │  21:14 UTC      │
│  serial  Gell-EC41E7EB │                         │                         │                 │
│  uuid    B092D2EB-7D41 │ OU container            │ onPremSecurityId        │ serialNumber    │
│         -4FB4-B239-…   │  OU=Devices,OU=…        │  S-1-5-21-…-4829        │  Gell-EC41E7EB  │
│                        │                         │                         │                 │
│ machine pc-q35-10.1    │ objectSid               │ accountEnabled    true  │ ownerType       │
│ bios    ovmf           │  S-1-5-21-…-4829        │ registrationDateTime    │  company        │
│                        │                         │  2026-04-20 23:52 UTC   │                 │
│ args                   │ UAC flags               │                         │ lastSync        │
│ -smbios                │  WORKSTATION_TRUST      │ physicalIds (3)         │  2026-04-21     │
│ file=/var/lib/vz/      │  (no ACCOUNTDISABLE)    │  [USER-HWID]:…          │  00:09 UTC      │
│ snippets/autopilot-    │                         │  [HWID]:h:…             │                 │
│ smbios-vm-116.bin      │ pwdLastSet              │  [OrderId]:GellNative   │ azureADDeviceId │
│                        │  2026-04-20 23:47 UTC   │                         │  a6c91d4e-…     │
│ Provisioned            │ lastLogonTimestamp      │                         │                 │
│  sequence #2           │  2026-04-21 00:01 UTC   │                         │                 │
│  "AD Domain Join—Home" │                         │                         │                 │
│  job 20260421-fb7d ↗  │ whenCreated             │                         │                 │
│  2026-04-20 23:48 UTC  │  2026-04-20 23:47 UTC   │                         │                 │
│                        │ whenChanged             │                         │                 │
│                        │  2026-04-21 00:01 UTC   │                         │                 │
└────────────────────────┴─────────────────────────┴─────────────────────────┴─────────────────┘

  Unified timeline                              [newest first · 50/page]  Filter: [All ▾]

  ● 2026-04-21 00:09 UTC  Intune · enrolled          🟢 progression
                         complianceState → compliant
  ● 2026-04-20 23:52 UTC  Entra · hybrid-synced      🟢 progression
                         sync-pending (⏳ 45m) → trustType=ServerAd
                         onPremSecurityIdentifier populated, link established
  ● 2026-04-20 23:47 UTC  AD · object-created         🟢 progression
                         new object in OU=Devices,OU=WorkspaceLabs
                         cn=GELL-EC41E7EB  objectGUID=7d41fbb4-…
  ● 2026-04-20 23:42 UTC  PVE · power-on              ⚪ event
                         stopped → running  (node=pve2)
  ● 2026-04-20 23:41 UTC  PVE · provisioned          🟢 event
                         sequence #2 via job 20260421-fb7d
                         smbios file=autopilot-smbios-vm-116.bin attached
  ● 2026-04-20 23:41 UTC  PVE · vm-created           ⚪ event
                         cloned from template 250   tagged: autopilot

  ▶ Raw data explorer (last 5 sweeps)
      pve_snapshots[0..4]       device_probes[0..4]
```

### Degraded state (illustrative — every failure mode surfaced)

```
Linkage health
 SMBIOS.serial ──✓── Intune.serialNumber
 Windows.Name  ──⚠── AD.cn            case-mismatch: gell-ec41e7eb vs GELL-EC41E7EB
 AD.objectSid  ──✗── Entra.onPremSecurityIdentifier   stale Entra device (different SID)
 Entra.deviceId ──⏳── Intune.azureADDeviceId         Intune not yet linked

AD column:  ⚠️ 2 matches
            [Active]   CN=GELL-EC41E7EB,OU=Devices,OU=WorkspaceLabs,…
            [Disabled] CN=GELL-EC41E7EB,OU=OldDevices,OU=WorkspaceLabs,…  ← duplicate
Entra column: ⚠️ 2 matches
            ServerAd   id=ab56021c-…  onPremSID matches AD
            AzureAd    id=07e2b1f9-…  Entra-only, different deviceId  ← stale

Timeline (most recent)
  ● 2026-04-21 02:15 UTC  AD · renamed                🟡 rename
                         cn: GELL-EC41E7EB → gell-ec41e7eb   (same objectGUID)
  ● 2026-04-21 01:58 UTC  Entra · link-broken         🔴 regression
                         trustType=ServerAd but onPremSecurityIdentifier ≠ AD.objectSid
  ● 2026-04-20 22:05 UTC  PVE · migration             ⚪ event
                         node pve2 → pve1
```

### Visual conventions

- **Column header dot** — `●` green = healthy, `⚠️` yellow = duplicate
  / disabled / case-mismatch, `❌` red = missing, `⏳` grey =
  sync-pending.
- **Linkage strip** at the top is the at-a-glance "are the four
  systems actually pointing at the same thing?" check — the single
  most useful piece of the page when something breaks.
- **Timeline is cross-system and chronological** — a rename on day 5
  appears next to an Intune compliance flip on the same day, not
  buried in per-source tabs.
