# UTM Bundle Format Reference

> **Spec version**: based on UTM `ConfigurationVersion` 4 (UTM ≥ 4.x on macOS/Apple Silicon).  
> **Source**: reverse-engineered from a live `Windows.utm/config.plist` on macOS 14 + Apple Silicon.  
> **Purpose**: this document is the authoritative schema the Ansible role implements against when materialising `.utm` skeleton bundles.

---

## 1. Bundle Layout

```
<VMName>.utm/                    ← "bundle" directory; UTM sees this as one document
├── config.plist                 ← Full VM configuration (XML or binary plist; XML preferred for diffs)
├── screenshot.png               ← Last screenshot (optional; omit in skeleton bundles)
└── Data/
    ├── <uuid>.qcow2             ← System disk image (Ansible creates this via qemu-img)
    └── <name>.iso               ← Installer ISO (symlinked or copied by Ansible)
```

UTM stores all bundles under:

```
~/Library/Containers/com.utmapp.UTM/Data/Documents/
```

Ansible must copy skeleton bundles into that path, **then** substitute UUIDs and wire up disk/ISO paths inside `config.plist`.

---

## 2. Top-Level Keys

| Key                  | Type      | Required | Notes |
|----------------------|-----------|----------|-------|
| `Backend`            | String    | ✅        | Always `"QEMU"` for QEMU-backed VMs |
| `ConfigurationVersion` | Integer | ✅       | `4` for UTM 4.x; UTM may migrate older versions on load |
| `Display`            | Array     | ✅        | One entry per virtual display |
| `Drive`              | Array     | ✅        | Ordered list of virtual drives (CDs and disks) |
| `Information`        | Dict      | ✅        | VM identity: name, UUID, icon |
| `Input`              | Dict      | ✅        | USB / input device settings |
| `Network`            | Array     | ✅        | One entry per virtual NIC |
| `QEMU`               | Dict      | ✅        | QEMU-specific flags (TPM, UEFI, hypervisor, etc.) |
| `Serial`             | Array     | ✅        | Virtual serial ports; empty array if none |
| `Sharing`            | Dict      | ✅        | Clipboard and directory sharing |
| `Sound`              | Array     | ✅        | Virtual audio devices |
| `System`             | Dict      | ✅        | CPU, memory, architecture |

---

## 3. Key-by-Key Reference

### 3.1 `Backend`

```xml
<key>Backend</key>
<string>QEMU</string>
```

- **Type**: String  
- **Required**: Yes  
- **Valid values**: `"QEMU"` (only supported value for our use case)

---

### 3.2 `ConfigurationVersion`

```xml
<key>ConfigurationVersion</key>
<integer>4</integer>
```

- **Type**: Integer  
- **Required**: Yes  
- **Notes**: Must be `4` for UTM 4.x. Do not increment manually; UTM manages migrations.

---

### 3.3 `Display`

Array of display adapter configurations. One entry is typical.

```xml
<key>Display</key>
<array>
    <dict>
        <key>DownscalingFilter</key>
        <string>Linear</string>
        <key>DynamicResolution</key>
        <true/>
        <key>Hardware</key>
        <string>virtio-ramfb-gl</string>
        <key>NativeResolution</key>
        <false/>
        <key>UpscalingFilter</key>
        <string>Nearest</string>
    </dict>
</array>
```

| Sub-key             | Type    | Required | Notes |
|---------------------|---------|----------|-------|
| `DownscalingFilter` | String  | ✅        | `"Linear"` (smooth) or `"Nearest"` (pixel-perfect) |
| `DynamicResolution` | Boolean | ✅        | `true` enables SPICE/VirtIO dynamic resize |
| `Hardware`          | String  | ✅        | GPU model. Use `"virtio-ramfb-gl"` for aarch64 with GPU acceleration |
| `NativeResolution`  | Boolean | ✅        | `true` = use display's native pixel density |
| `UpscalingFilter`   | String  | ✅        | `"Nearest"` recommended for crisp upscaling |

**Common `Hardware` values:**

| Value              | Use case |
|--------------------|----------|
| `virtio-ramfb-gl`  | aarch64 with host GPU acceleration (recommended) |
| `virtio-ramfb`     | aarch64 software rendering |
| `virtio-vga-gl`    | x86_64 with GPU acceleration |
| `std`              | Basic VGA fallback |

---

### 3.4 `Drive`

Ordered array of virtual drives. UTM presents them to the guest in array order.

**Drive[0] — Installer ISO (CD)**:

```xml
<dict>
    <key>Identifier</key>
    <string>00000000-0000-0000-0000-000000000001</string>
    <key>ImageType</key>
    <string>CD</string>
    <key>Interface</key>
    <string>USB</string>
    <key>InterfaceVersion</key>
    <integer>1</integer>
    <key>ReadOnly</key>
    <true/>
</dict>
```

**Drive[1] — System Disk**:

```xml
<dict>
    <key>Identifier</key>
    <string>00000000-0000-0000-0000-000000000002</string>
    <key>ImageName</key>
    <string>SYSTEM_DISK_PLACEHOLDER.qcow2</string>
    <key>ImageType</key>
    <string>Disk</string>
    <key>Interface</key>
    <string>NVMe</string>
    <key>InterfaceVersion</key>
    <integer>1</integer>
    <key>ReadOnly</key>
    <false/>
</dict>
```

**Drive[2] — Secondary CD (answer ISO / unattend; may be unused in v1)**:

```xml
<dict>
    <key>Identifier</key>
    <string>00000000-0000-0000-0000-000000000003</string>
    <key>ImageType</key>
    <string>CD</string>
    <key>Interface</key>
    <string>USB</string>
    <key>InterfaceVersion</key>
    <integer>1</integer>
    <key>ReadOnly</key>
    <true/>
</dict>
```

| Sub-key            | Type    | Required         | Notes |
|--------------------|---------|------------------|-------|
| `Identifier`       | String  | ✅                | UUIDv4. **Must be unique per bundle and per drive.** Ansible substitutes at build time. |
| `ImageName`        | String  | Disk=✅ / CD=❌  | Filename (not full path) relative to the bundle's `Data/` directory. Omit for empty CD drives. |
| `ImageType`        | String  | ✅                | `"CD"` or `"Disk"` |
| `Interface`        | String  | ✅                | `"USB"` for CDs, `"NVMe"` for fast system disk, `"VirtIO"` also valid for disks |
| `InterfaceVersion` | Integer | ✅                | Always `1` |
| `ReadOnly`         | Boolean | ✅                | `true` for CD images, `false` for writable disks |

---

### 3.5 `Information`

```xml
<key>Information</key>
<dict>
    <key>Icon</key>
    <string>windows</string>
    <key>IconCustom</key>
    <false/>
    <key>Name</key>
    <string>Windows 11 ARM64</string>
    <key>UUID</key>
    <string>5CF3E95B-9508-4828-B91B-2046296F42AD</string>
</dict>
```

| Sub-key      | Type    | Required | Notes |
|--------------|---------|----------|-------|
| `Icon`       | String  | ✅        | Built-in icon slug. Known valid values: `"windows"`, `"linux"`, `"ubuntu"`, `"debian"`, `"freebsd"`, `"android"`. No verified `"server"` slug — fall back to `"windows"`. |
| `IconCustom` | Boolean | ✅        | `false` = use built-in icon; `true` = look for custom icon file in bundle |
| `Name`       | String  | ✅        | Human-readable VM name shown in UTM UI. Ansible sets this at build time. |
| `UUID`       | String  | ✅        | UUIDv4. **Must be globally unique.** UTM uses this as the VM's identity. Ansible must generate and substitute a fresh UUID for every VM instance. |

---

### 3.6 `Input`

```xml
<key>Input</key>
<dict>
    <key>MaximumUsbShare</key>
    <integer>3</integer>
    <key>UsbBusSupport</key>
    <string>3.0</string>
    <key>UsbSharing</key>
    <false/>
</dict>
```

| Sub-key           | Type    | Required | Notes |
|-------------------|---------|----------|-------|
| `MaximumUsbShare` | Integer | ✅        | Max USB devices shared from host. `3` is the default. |
| `UsbBusSupport`   | String  | ✅        | USB standard: `"3.0"` (xHCI) or `"2.0"` (EHCI) |
| `UsbSharing`      | Boolean | ✅        | `true` enables USB device sharing from host |

---

### 3.7 `Network`

Array of virtual NICs.

```xml
<key>Network</key>
<array>
    <dict>
        <key>Hardware</key>
        <string>virtio-net-pci</string>
        <key>IsolateFromHost</key>
        <false/>
        <key>Mode</key>
        <string>Shared</string>
        <key>PortForward</key>
        <array/>
    </dict>
</array>
```

| Sub-key          | Type    | Required | Notes |
|------------------|---------|----------|-------|
| `Hardware`       | String  | ✅        | NIC model. `"virtio-net-pci"` is optimal for VirtIO drivers; `"e1000-82545em"` for fallback |
| `IsolateFromHost`| Boolean | ✅        | `true` blocks host↔guest traffic (guest-only NAT) |
| `MacAddress`     | String  | ❌        | **Omit from skeleton bundles.** UTM auto-generates a random MAC on first load. If present, format is `"XX:XX:XX:XX:XX:XX"`. Committing a MAC would cause collisions between cloned VMs. |
| `Mode`           | String  | ✅        | `"Shared"` (NAT), `"Bridged"`, `"Host"` |
| `PortForward`    | Array   | ✅        | List of port-forward rules; empty array for none |

---

### 3.8 `QEMU`

```xml
<key>QEMU</key>
<dict>
    <key>AdditionalArguments</key>
    <array/>
    <key>BalloonDevice</key>
    <false/>
    <key>DebugLog</key>
    <false/>
    <key>Hypervisor</key>
    <true/>
    <key>PS2Controller</key>
    <false/>
    <key>RNGDevice</key>
    <true/>
    <key>RTCLocalTime</key>
    <true/>
    <key>TPMDevice</key>
    <true/>
    <key>TSO</key>
    <false/>
    <key>UEFIBoot</key>
    <true/>
</dict>
```

| Sub-key              | Type    | Required | Notes |
|----------------------|---------|----------|-------|
| `AdditionalArguments`| Array   | ✅        | Raw QEMU CLI args (strings). Empty for typical use. |
| `BalloonDevice`      | Boolean | ✅        | `true` enables VirtIO memory balloon (dynamic RAM sizing) |
| `DebugLog`           | Boolean | ✅        | `true` writes QEMU debug output; keep `false` in production |
| `Hypervisor`         | Boolean | ✅        | `true` enables Apple Hypervisor.framework (HVF) acceleration — **required for ARM64 perf** |
| `PS2Controller`      | Boolean | ✅        | `false` disables legacy PS/2; Windows ARM doesn't need it |
| `RNGDevice`          | Boolean | ✅        | `true` adds VirtIO RNG (entropy source) — recommended |
| `RTCLocalTime`       | Boolean | ✅        | `true` = RTC uses local time (Windows default); `false` = UTC (Linux default) |
| `TPMDevice`          | Boolean | ✅        | `true` adds emulated TPM 2.0 — **required for Windows 11** |
| `TSO`                | Boolean | ✅        | TCP Segmentation Offload (Apple Hypervisor feature). `false` unless explicitly needed. |
| `UEFIBoot`           | Boolean | ✅        | `true` = UEFI firmware; `false` = legacy BIOS. **Must be `true` for Windows 11 ARM.** |

---

### 3.9 `Serial`

```xml
<key>Serial</key>
<array/>
```

- **Type**: Array  
- **Required**: Yes (empty array is valid)  
- **Notes**: Virtual serial/console ports. Not needed for Windows guests; include as empty array.

---

### 3.10 `Sharing`

```xml
<key>Sharing</key>
<dict>
    <key>ClipboardSharing</key>
    <true/>
    <key>DirectoryShareMode</key>
    <string>WebDAV</string>
    <key>DirectoryShareReadOnly</key>
    <false/>
</dict>
```

| Sub-key                  | Type    | Required | Notes |
|--------------------------|---------|----------|-------|
| `ClipboardSharing`       | Boolean | ✅        | `true` enables SPICE clipboard sync between host and guest |
| `DirectoryShareMode`     | String  | ✅        | `"WebDAV"` (SPICE WebDAV) or `"VirtFS"` (9P/VirtFS). `"WebDAV"` works with Windows SPICE guest tools. |
| `DirectoryShareReadOnly` | Boolean | ✅        | `true` makes shared directories read-only in guest |

---

### 3.11 `Sound`

```xml
<key>Sound</key>
<array>
    <dict>
        <key>Hardware</key>
        <string>intel-hda</string>
    </dict>
</array>
```

| Sub-key    | Type   | Required | Notes |
|------------|--------|----------|-------|
| `Hardware` | String | ✅        | Audio device model. `"intel-hda"` is well-supported in Windows. |

---

### 3.12 `System`

```xml
<key>System</key>
<dict>
    <key>Architecture</key>
    <string>aarch64</string>
    <key>CPU</key>
    <string>default</string>
    <key>CPUCount</key>
    <integer>4</integer>
    <key>CPUFlagsAdd</key>
    <array/>
    <key>CPUFlagsRemove</key>
    <array/>
    <key>ForceMulticore</key>
    <false/>
    <key>JITCacheSize</key>
    <integer>0</integer>
    <key>MemorySize</key>
    <integer>8192</integer>
    <key>Target</key>
    <string>virt</string>
</dict>
```

| Sub-key         | Type    | Required | Notes |
|-----------------|---------|----------|-------|
| `Architecture`  | String  | ✅        | `"aarch64"` for ARM64; `"x86_64"` for Intel/AMD |
| `CPU`           | String  | ✅        | QEMU CPU model. `"default"` uses the host CPU model (best for HVF). |
| `CPUCount`      | Integer | ✅        | Number of vCPUs. `4` is a reasonable default; up to host core count. |
| `CPUFlagsAdd`   | Array   | ✅        | Additional QEMU CPU feature flags to enable (strings). Empty for typical use. |
| `CPUFlagsRemove`| Array   | ✅        | CPU feature flags to disable. Empty for typical use. |
| `ForceMulticore`| Boolean | ✅        | `true` exposes CPUs as separate sockets. `false` = single socket, multiple cores. |
| `JITCacheSize`  | Integer | ✅        | JIT translation cache in MB. `0` = QEMU default. Only relevant when `Hypervisor=false`. |
| `MemorySize`    | Integer | ✅        | RAM in **megabytes**. Minimum 4096 for Windows 11; use 8192 for comfort. |
| `Target`        | String  | ✅        | QEMU machine type. `"virt"` for aarch64 (standard ARM virt board); `"q35"` for x86_64. |

---

## 4. UUIDs

Two categories of UUID are present in every bundle:

| Location                  | Key          | Scope      | Notes |
|---------------------------|--------------|------------|-------|
| `Information.UUID`        | VM identity  | Global     | Must be unique across all UTM VMs on all machines. UTM tracks VMs by this ID. Collisions cause UTM to refuse to load one of the VMs. |
| `Drive[n].Identifier`     | Drive slot   | Per-bundle | Must be unique within the bundle and ideally unique globally. UTM references drives by this ID internally. |

### Generation

Use `uuidgen` (macOS built-in):

```bash
uuidgen          # generates a fresh RFC 4122 UUID, e.g. A3F1C9D2-4E67-4B1A-9C3D-0E8F7A2B1C5D
```

### Skeleton Placeholder UUIDs

These values are committed in the skeleton bundles and **must be replaced by Ansible** before the bundle is loaded in UTM:

| Location                  | Placeholder Value                    |
|---------------------------|--------------------------------------|
| `Information.UUID`        | `00000000-0000-0000-0000-000000000000` |
| `Drive[0].Identifier`     | `00000000-0000-0000-0000-000000000001` |
| `Drive[1].Identifier`     | `00000000-0000-0000-0000-000000000002` |
| `Drive[2].Identifier`     | `00000000-0000-0000-0000-000000000003` |

If `plutil` reads `00000000-0000-0000-0000-000000000000` in production, Ansible substitution did not run. Fail loudly.

---

## 5. How Ansible Will Substitute Values at Build Time

After copying the skeleton bundle to UTM's Documents directory, Ansible runs `plutil` to patch `config.plist` in-place.

> **Note**: `plutil -replace` operates on XML or binary plists interchangeably. The skeleton bundles are stored as XML; `plutil` will write back in the same format it read.

### 5.1 Set `Information.UUID`

```bash
plutil -replace Information.UUID \
  -string "$(uuidgen)" \
  config.plist
```

### 5.2 Set `Information.Name`

```bash
plutil -replace Information.Name \
  -string "Windows 11 ARM64 - {{ inventory_hostname }}" \
  config.plist
```

### 5.3 Set `Drive[n].Identifier`

`plutil` uses a dot-notation path where array elements are zero-indexed:

```bash
# Drive 0 (installer ISO CD)
plutil -replace Drive.0.Identifier \
  -string "$(uuidgen)" \
  config.plist

# Drive 1 (system disk)
plutil -replace Drive.1.Identifier \
  -string "$(uuidgen)" \
  config.plist

# Drive 2 (answer ISO CD)
plutil -replace Drive.2.Identifier \
  -string "$(uuidgen)" \
  config.plist
```

> **Tip**: Store each generated UUID in a variable before substitution so the same UUID can be used for both the `Drive[n].Identifier` field and the matching `Data/<uuid>.qcow2` filename:
> ```bash
> DISK_UUID=$(uuidgen)
> plutil -replace Drive.1.Identifier -string "$DISK_UUID" config.plist
> plutil -replace Drive.1.ImageName  -string "${DISK_UUID}.qcow2" config.plist
> ```

### 5.4 Set System Disk `Drive[1].ImageName`

```bash
DISK_UUID=$(uuidgen)
plutil -replace Drive.1.ImageName \
  -string "${DISK_UUID}.qcow2" \
  config.plist
```

The matching qcow2 must then be created at `Data/${DISK_UUID}.qcow2`:

```bash
qemu-img create -f qcow2 "Data/${DISK_UUID}.qcow2" 64G
```

### 5.5 Set Installer ISO `Drive[0].ImageName`

The installer ISO CD (Drive[0]) has no `ImageName` in the skeleton. Ansible **adds** the key:

```bash
plutil -insert Drive.0.ImageName \
  -string "en-us_windows_11_arm64.iso" \
  config.plist
```

> Use `-insert` (not `-replace`) because the key does not exist in the skeleton. Use `-replace` if a prior run may have already added it.

### 5.6 Set Answer ISO `Drive[2].ImageName` (Optional)

```bash
plutil -insert Drive.2.ImageName \
  -string "autounattend.iso" \
  config.plist
```

### 5.7 Full Ansible Task Sequence (Pseudocode)

```yaml
- name: Generate UUIDs for UTM bundle
  set_fact:
    utm_vm_uuid:    "{{ lookup('pipe', 'uuidgen') }}"
    utm_cd0_uuid:   "{{ lookup('pipe', 'uuidgen') }}"
    utm_disk_uuid:  "{{ lookup('pipe', 'uuidgen') }}"
    utm_cd2_uuid:   "{{ lookup('pipe', 'uuidgen') }}"

- name: Copy skeleton bundle
  copy:
    src: "utm-templates/windows11-arm64.utm"
    dest: "~/Library/Containers/com.utmapp.UTM/Data/Documents/{{ vm_name }}.utm"

- name: Substitute VM UUID
  command: >
    plutil -replace Information.UUID -string "{{ utm_vm_uuid }}"
    "~/Library/Containers/com.utmapp.UTM/Data/Documents/{{ vm_name }}.utm/config.plist"

- name: Substitute VM name
  command: >
    plutil -replace Information.Name -string "{{ vm_name }}"
    ".../config.plist"

- name: Substitute drive identifiers
  command: >
    plutil -replace Drive.0.Identifier -string "{{ utm_cd0_uuid }}" ".../config.plist"

- name: Substitute disk identifier and ImageName
  command: |
    plutil -replace Drive.1.Identifier -string "{{ utm_disk_uuid }}" ".../config.plist"
    plutil -replace Drive.1.ImageName  -string "{{ utm_disk_uuid }}.qcow2" ".../config.plist"

- name: Wire installer ISO
  command: >
    plutil -insert Drive.0.ImageName -string "{{ windows_iso_filename }}" ".../config.plist"

- name: Create system disk
  command: >
    qemu-img create -f qcow2
    ".../Data/{{ utm_disk_uuid }}.qcow2" 64G
```

---

## 6. Schema Gotchas and Maintainer Notes

1. **Binary vs XML plist**: UTM accepts both. We store XML for Git diff-ability. Do **not** let macOS tooling (e.g., Xcode, `defaults write`) silently convert back to binary — always verify with `file config.plist` or check for the `<?xml` header.

2. **`plutil -insert` vs `-replace`**: Use `-insert` when a key doesn't exist in the skeleton (e.g., CD `ImageName`), `-replace` when it does. Using `-replace` on a missing key, or `-insert` on an existing key, both produce a fatal error.

3. **`Drive[n].ImageName` for empty CDs**: The key must be **absent** (not empty string) for UTM to show the drive as "no media inserted." An empty string value causes UTM to fail loading the bundle.

4. **MAC address omission**: Omitting `MacAddress` from a `Network` entry causes UTM to generate a random MAC on first load. This is the desired behaviour for cloned VMs. Never commit a real MAC address in skeleton bundles.

5. **UUID case**: UTM generates uppercase UUIDs (e.g., `5CF3E95B-...`). `plutil` and UTM both accept lowercase. Stick to uppercase for consistency with UTM's own output.

6. **`InterfaceVersion`**: Always `1`. This field exists in the schema but has no documented effect beyond `1`. Do not omit it — UTM may add warnings or default it unexpectedly.

7. **`ConfigurationVersion` must not be manually incremented**: UTM manages schema migrations. Setting an unsupported version will cause UTM to refuse to load the bundle.

8. **TPM + UEFI for Windows 11**: Both `QEMU.TPMDevice = true` and `QEMU.UEFIBoot = true` are **required** for Windows 11 ARM to install and boot. Removing either causes the Windows 11 installer to reject the hardware.

9. **`Hypervisor = true`**: Requires Apple Silicon hardware. On Intel Macs, this must be `false` (JIT mode). Our templates target Apple Silicon exclusively; document this constraint in the role README.

10. **Array key path syntax in `plutil`**: Arrays use zero-based numeric indices in dot notation: `Drive.0.Identifier`, `Drive.1.ImageName`. Negative indices are not supported.

---

## 7. Validation Checklist

Before committing or deploying a `config.plist`:

```bash
# 1. Plist is syntactically valid XML
plutil -lint config.plist

# 2. No placeholder UUIDs remain (should output nothing after Ansible substitution)
grep "00000000-0000-0000-0000-00000000000" config.plist

# 3. VM UUID is set
plutil -extract Information.UUID raw config.plist

# 4. System disk ImageName is not the placeholder
plutil -extract Drive.1.ImageName raw config.plist | grep -v "SYSTEM_DISK_PLACEHOLDER"

# 5. Installer ISO is wired
plutil -extract Drive.0.ImageName raw config.plist
```

---

*Last updated: based on UTM 4.x `ConfigurationVersion` 4 schema.*
