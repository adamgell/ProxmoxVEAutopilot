# Device State Monitoring — Design Spec

**Date:** 2026-04-20
**Status:** Proposed — awaiting review
**Scope:** Autopilot-provisioned Windows VMs on the PVE cluster whose AD
computer object lives under `OU=WorkspaceLabs,DC=home,DC=gell,DC=one`.

## Why

After a VM is provisioned we have no ongoing visibility into whether
it stays registered in the places it should be (AD, Entra, Intune), or
whether those registrations drift (hybrid object goes stale,
compliance flips, device re-enrolls with a new Entra ID). We want a
dashboard that joins all three directories against PVE VMIDs on a
schedule and records every state change so regressions are visible.

## Non-goals

- Not a remediation engine. We record; we do not re-enroll, re-join, or
  delete stale objects.
- Not a compliance dashboard for non-VM endpoints. Physical laptops
  enrolled in the same tenant are invisible to this view.
- Not real-time. 15-minute polling is the design target; sub-minute
  latency is not required.

## Matching keys

Each VM has up to four identifiers that link the four surfaces:

| Source       | Identifier                            | Read from                         |
|--------------|---------------------------------------|-----------------------------------|
| PVE          | `vmid`, `name` (e.g. `Gell-EC41E7EB`) | `qm list`                         |
| Windows      | `Win32_BIOS.SerialNumber`             | guest-exec (already implemented)  |
| Windows      | `Win32_ComputerSystemProduct.UUID`    | guest-exec                        |
| Windows      | `Win32_ComputerSystem.Name`           | guest-exec                        |
| AD           | `distinguishedName`                   | LDAP under scope OU               |
| Entra        | `deviceId`, `trustType`               | Graph `/v1.0/devices`             |
| Intune       | `id`, `serialNumber`, `complianceState` | Graph `/v1.0/deviceManagement/managedDevices` |

The **joining key** is the Windows computer name for AD + Entra, and
the SMBIOS serial for Intune. The per-VM UUID is used as a tiebreaker
when names collide (expected for re-enrolled / duplicated devices).

## Scope restriction

All AD LDAP searches use `searchBase = OU=WorkspaceLabs,DC=home,DC=gell,DC=one`
with `SCOPE_SUBTREE`. Devices outside that subtree are not recorded.

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

-- Single-row settings table (id is a sentinel).
CREATE TABLE monitoring_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled          INTEGER NOT NULL DEFAULT 1,
    interval_seconds INTEGER NOT NULL DEFAULT 900,   -- 15 min
    scope_ou_dn      TEXT    NOT NULL DEFAULT 'OU=WorkspaceLabs,DC=home,DC=gell,DC=one',
    ad_credential_id INTEGER NOT NULL DEFAULT 7,     -- home\adam_admin
    updated_at       TEXT    NOT NULL
);
```

## Probes

### AD probe

Using `ldap3` (new dependency — pure-Python, no system libs):

```
conn = Connection(Server('dns.home.gell.one'), user='home\\adam_admin', password='...', auto_bind=True)
conn.search(
    search_base='OU=WorkspaceLabs,DC=home,DC=gell,DC=one',
    search_filter=f'(&(objectClass=computer)(name={escape_filter_chars(win_name)}))',
    search_scope=SUBTREE,
    attributes=['distinguishedName', 'objectGUID', 'pwdLastSet', 'operatingSystem',
                'operatingSystemVersion', 'userAccountControl', 'whenCreated'],
)
```

Return ALL matches — the collector does not de-dup. `userAccountControl`
bit 2 (`ACCOUNTDISABLE`) is preserved in `ad_matches_json` so the UI
can render "1 active + 1 disabled".

### Entra probe

```
GET https://graph.microsoft.com/v1.0/devices?$filter=displayName eq '<win_name>'
    &$select=id,displayName,deviceId,trustType,accountEnabled,approximateLastSignInDateTime,
             operatingSystem,operatingSystemVersion,registrationDateTime,deviceOwnership
```

`trustType` maps to:
- `AzureAd` → Entra-only joined
- `ServerAd` → **Hybrid-joined** (what we expect for sequence 2)
- `Workplace` → Registered (not joined)

All rows stored; `entra_match_count` > 1 is an info banner, not an
error.

### Intune probe

```
GET https://graph.microsoft.com/v1.0/deviceManagement/managedDevices?$filter=serialNumber eq '<serial>'
    &$select=id,deviceName,serialNumber,azureADDeviceId,enrolledDateTime,
             complianceState,lastSyncDateTime,operatingSystem,managedDeviceOwnerType
```

## Collector

New module `web/device_monitor.py`:

- `probe_vm(vmid, ctx) -> dict` — runs the four probes for one VM.
  Any single-source exception is caught and stored in
  `probe_errors_json[source]`; the others still run.
- `sweep(ctx) -> sweep_id` — enumerates running Windows VMs via
  `qm list --full`, calls `probe_vm` for each, inserts one
  `monitoring_sweeps` row + N `device_probes` rows in one transaction.
- `start_background_loop(app)` — asyncio task registered in the
  FastAPI startup hook. Sleeps `interval_seconds` between sweeps,
  re-reads interval from DB on every loop so UI changes take effect
  on the next tick.
- Cancellation-safe: swallows `asyncio.CancelledError` cleanly on
  shutdown.

A Graph access token is cached per process and refreshed when it's
within 5 min of expiry; LDAP connections are per-sweep, closed after.

## Regression detection

Computed at read time (not stored). For the detail view, load the last
N probes for a VMID ordered by `checked_at`. Compare each probe to its
predecessor and flag transitions:

| From → To                        | Severity  |
|----------------------------------|-----------|
| ad_found=1 → 0                   | regression |
| entra_trustType ServerAd → (nothing, or AzureAd) | regression |
| intune_found=1 → 0               | regression |
| complianceState compliant → noncompliant | regression |
| ad_found=0 → 1                   | progression |
| entra_found=0 → ServerAd          | progression |
| intune_found=0 → 1               | progression |

The `/monitoring` page shows only the latest probe per VMID; the
`/monitoring/<vmid>` detail page shows the timeline with coloured
transitions.

## UI

- `/monitoring` — table: VMID, VM name, last checked, AD ✅/⚠️/❌
  (✅=1 active, ⚠️=1 disabled or >1 matches, ❌=none), Entra (same),
  Intune (same), latest regression if any.
- `/monitoring/<vmid>` — top card: current full state. Below, a
  timeline of probes (newest first, paginated 50 per page) with each
  transition flagged and clickable to show the raw JSON diff.
- `/monitoring/settings` — form: enable toggle, interval seconds (min
  60, warn below 900), scope OU DN, AD credential selector. Save →
  writes `monitoring_settings` row; background loop picks up on next
  iteration.

## Failure modes

| Failure                              | Behaviour                                                 |
|--------------------------------------|-----------------------------------------------------------|
| Graph token refresh fails            | Sweep still runs; `errors_json.graph_auth` set; all Entra + Intune probes recorded as `skipped` |
| LDAP DC unreachable                  | `errors_json.ldap` set; AD probes skipped, Entra + Intune continue |
| Guest agent down on VM               | Only PVE-side fields (vmid, vm_name) populated; directory probes skipped for that VM, stored as `probe_errors_json.guest='agent_down'` |
| VM stopped                           | Still probed by name + last-known serial (if we've seen it before) so an unexpectedly-stopped VM doesn't drop out of the dashboard |
| Duplicate directory objects          | All stored in `*_matches_json`; UI marks as ⚠️              |
| Collector raises unhandled exception | Caught at the loop level; logged; next tick still runs    |

## Testing

- Unit: `device_monitor.probe_vm` given a fake PVE/LDAP/Graph context
  — assert matches arrays land with correct shapes for 0/1/2 matches.
- Unit: regression detector — feed pairs of probes, assert flag.
- Integration: against the live cluster, one sweep against
  `OU=WorkspaceLabs` — assert VM 109 and 116 appear with `ad_found=1`,
  `entra_trustType=ServerAd`.
- No live test runs in CI (cluster-local only).

## Delivery steps (commit boundaries)

1. `ldap3` dependency + `web/device_history_db.py` (schema, DAL, tests)
2. `web/device_monitor.py` — probe functions + sweep; tests against
   fake context
3. FastAPI startup hook + background loop + settings endpoints
4. `/monitoring` list page + template
5. `/monitoring/<vmid>` timeline + regression detector
6. Live run against the cluster; adjust for whatever duplicates exist

Each step commits independently and the system is usable after step 3
(UI added in 4-5).

## Open questions

None — scope (OU), interval default (15 min with UI override), duplicate
tolerance (store all), and credential sources (cred 7 for AD, existing
vault Entra app for Graph) all confirmed in prior conversation.
