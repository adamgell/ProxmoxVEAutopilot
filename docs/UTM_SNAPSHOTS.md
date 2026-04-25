# UTM Snapshot Support — Feasibility & Implementation

**Phase 9 of the UTM macOS/ARM64 port.**  
**Decision: IMPLEMENT via `qemu-img snapshot`.**

---

## 1. Smoke-Test Results (collected on this macOS/ARM64 host)

### 1.1 `utmctl` snapshot capability

```
$ /Applications/UTM.app/Contents/MacOS/utmctl --help | grep -i snap
(no output — exit 1)
```

`utmctl` **does not expose any snapshot subcommands**.  The full subcommand
list is: `version list status start suspend stop attach file exec ip-address
clone delete usb`.  No `snapshot`, `savevm`, `checkpoint`, or related verbs
exist at this UTM version.

### 1.2 `qemu-img` snapshot capability

```
$ qemu-img --version
qemu-img version 11.0.0
Copyright (c) 2003-2026 Fabrice Bellard and the QEMU Project developers

$ which qemu-img
/opt/homebrew/bin/qemu-img

$ qemu-img snapshot --help
Usage:
  qemu-img snapshot [-f FMT | --image-opts] [-l | -a|-c|-d SNAPSHOT]
        [-U] [-q] [--object OBJDEF] FILE

List or manipulate snapshots in the image.
  -l   list snapshots
  -c   create named snapshot
  -a   apply named snapshot to the base
  -d   delete named snapshot
  -U   open image in shared mode (force-share, for read-only ops on locked images)
```

`qemu-img snapshot` is **fully functional** at `/opt/homebrew/bin/qemu-img`.

### 1.3 File-lock behaviour (running vs stopped VM)

```
# VM "Windows" is in status=started
$ qemu-img snapshot -l ~/Library/Containers/com.utmapp.UTM/Data/Documents/\
Windows.utm/Data/9FCD54A1-26BF-41BE-90AF-6E063BE190F3.qcow2

qemu-img: Could not open '...9FCD54A1.qcow2': Failed to get shared "write" lock
Is another process using the image [...]?

# With -U (force-share / read-only):
$ qemu-img snapshot -l -U ...9FCD54A1.qcow2
(empty snapshot list — no snapshots yet, but command succeeded with rc=0)
```

**Conclusion:** `-l -U` (list + force-share) works on a running VM.  
`-c`, `-a`, `-d` (write operations) require the VM to be **stopped**.

---

## 2. UTM Bundle Structure

```
Windows.utm/
├── config.plist          ← Defines drives; ImageName key → filename in Data/
├── screenshot.png
└── Data/
    ├── 9FCD54A1-26BF-41BE-90AF-6E063BE190F3.qcow2   ← writable NVMe disk
    └── efi_vars.fd
```

The `config.plist` `Drive` array entries have:
- `ImageType`: `"Disk"` (writable) or `"CD"` (ROM, no qcow2 snapshot support)
- `ReadOnly`: `true`/`false`
- `ImageName`: filename relative to `Data/`

Only `ImageType == "Disk"` and `ReadOnly == false` entries are snapshotted.

---

## 3. Feasibility Matrix

| Operation          | `utmctl`        | `qemu-img snapshot` | UTM GUI snapshots |
|--------------------|-----------------|---------------------|-------------------|
| **List snapshots** | ❌ not supported | ✅ `-l -U` (any state) | ⚠️ in-app only, no API |
| **Create snapshot**| ❌ not supported | ✅ `-c` (stopped only) | ⚠️ in-app only |
| **Restore snapshot**| ❌ not supported | ✅ `-a` (stopped only) | ⚠️ in-app only |
| **Delete snapshot**| ❌ not supported | ✅ `-d` (stopped only) | ⚠️ in-app only |
| **Snapshot on running VM** | ❌ | ❌ (write lock) | ⚠️ possible via UTM internal QEMU monitor, not exposed |

---

## 4. Recommended Path

**Use `qemu-img snapshot` on the `.utm/Data/*.qcow2` disk images.**

Rationale:
- `utmctl` has zero snapshot support.
- `qemu-img` (Homebrew QEMU 11) is already a documented dependency.
- The qcow2 format stores internal snapshots; they are self-contained in the
  disk file and survive bundle moves.
- Stopping the VM before create/restore/delete is safe and automatable; the
  API returns HTTP 409 if the VM is not stopped, with a clear message.
- UTM's GUI snapshot feature writes to the same qcow2 file, so snapshots
  created via `qemu-img` are visible in UTM's GUI and vice-versa.

**Implemented in:** `web/utm_snapshots.py` + four `/api/utm/vms/<uuid>/snapshots` endpoints.

---

## 5. VM Lifecycle for Snapshot Operations

```
                ┌─────────────────────────────────────────────────────┐
                │            CREATE / RESTORE / DELETE                 │
                └─────────────────────────────────────────────────────┘

  [started] ──→ user calls /stop ──→ [stopped] ──→ POST /snapshots ──→ [snapshot created]
                                                 └──→ POST /snapshots/{name}/restore ──→ [disk rolled back]
                                                 └──→ DELETE /snapshots/{name} ──→ [snapshot removed]
                    then user calls /start ──→ [started]

                ┌─────────────────────────────────────────────────────┐
                │                    LIST                              │
                └─────────────────────────────────────────────────────┘

  Any state ──→ GET /snapshots ──→ list (uses qemu-img -l -U)

IMPORTANT:
  - `utmctl suspend` puts the VM in "suspended" state but QEMU still holds
    a write lock on the disk (the QEMU process is paused, not terminated).
    Therefore write operations require status == "stopped", NOT just "paused"
    or "suspended".
  - The API enforces this and returns HTTP 409 with a descriptive message.
  - Auto-stop on snapshot is intentionally NOT implemented; the caller
    (operator or future playbook) decides the lifecycle.
```

---

## 6. Manual Recipes for Power Users

If you prefer to operate `qemu-img` directly (e.g. in a script):

```bash
BUNDLE=~/Library/Containers/com.utmapp.UTM/Data/Documents/MyVM.utm
DISK=$BUNDLE/Data/<uuid>.qcow2   # find with: ls $BUNDLE/Data/*.qcow2

# List snapshots (works on running VM):
qemu-img snapshot -l -U "$DISK"

# Create (VM must be stopped):
qemu-img snapshot -c "before-update" "$DISK"

# Restore:
qemu-img snapshot -a "before-update" "$DISK"

# Delete:
qemu-img snapshot -d "before-update" "$DISK"
```

---

## 7. Settings

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `utm_snapshot_auto_before_sequence` | bool | `false` | Reserved: auto-snapshot before sequence runs. Wired in settings schema; not yet invoked. See TODO in `roles/utm_vm_clone/README.md`. |

---

## 8. Implementation Summary

- **New module:** `autopilot-proxmox/web/utm_snapshots.py`
- **New endpoints:**
  - `GET  /api/utm/vms/<uuid>/snapshots` — list
  - `POST /api/utm/vms/<uuid>/snapshots` — create `{name, description}`
  - `POST /api/utm/vms/<uuid>/snapshots/<name>/restore` — restore
  - `DELETE /api/utm/vms/<uuid>/snapshots/<name>` — delete
- **UI:** Snapshot button per row in `utm_vms.html` (opens inline modal; disabled when VM is not stopped)
- **Settings:** `utm_snapshot_auto_before_sequence` added to SETTINGS_SCHEMA

---

## 9. Follow-up Conditions / Future Work

- **utmctl snapshot support:** If a future UTM release adds `utmctl snapshot`,
  we should prefer it (live snapshots without stopping).
- **Multi-disk consistency:** Currently all disks in the bundle are snapshotted
  sequentially; for true crash-consistency, a future version could use
  `qemu-img snapshot` with a QEMU external snapshot protocol.
- **Auto-snapshot before sequence:** Wire `utm_snapshot_auto_before_sequence`
  into `utm_vm_clone` role once the sequence runner is stabilised.
- **Snapshot description storage:** `qemu-img` does not store a description
  field; a future version could maintain a sidecar JSON in
  `bundle/Data/.autopilot-snapshots.json`.
