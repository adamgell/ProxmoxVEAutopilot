# Chassis Type Support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make each VM report a real SMBIOS Type 3 chassis-type byte (3 = Desktop, 10 = Notebook, 31 = Convertible, etc.) so customers replicating SCCM OSD workloads can write `IsLaptop`-style task-sequence conditionals against Proxmox-provisioned VMs the same way they do against real hardware.

**Architecture:** QEMU's `-smbios type=3` option does **not** expose the chassis-type enum field. Instead we generate a minimal SMBIOS Type 3 binary per unique chassis-type value, upload it once to the Proxmox host's `local` storage as a snippet, and inject `args: -smbios file=/var/lib/vz/snippets/autopilot-chassis-type-N.bin` into the VM config at clone time. The binary is cached by chassis-type integer; one file serves every VM with that chassis-type regardless of OEM profile, because QEMU merges it with the existing `-smbios type=1` manufacturer/model data Proxmox already sets via `smbios1:`.

**Tech Stack:** Python `struct` + existing Proxmox API helpers + existing Ansible role patterns + FastAPI. No new Python or OS dependencies.

**Spec reference:** No standalone spec — this plan fully captures the design. The chassis-type field already exists in `files/oem_profiles.yml`; see its header comment. DMTF SMBIOS 2.7 Type 3 structure definition is the reference for the binary format.

**Out of scope:**

- Chassis sub-elements (contained elements array) — always emit zero count.
- Height/power-cord/thermal-state reporting — use SMBIOS-standard "unspecified" values.
- Retrofitting `vm_provisioning` rows to point at chassis types — these stay tied to sequences, not chassis.
- Exposing chassis-type on the Devices page — not a Phase B.2 need.
- Removing the legacy warn-and-skip comment in `oem_profiles.yml` until the binary path works end-to-end (Task 3.3 does this at the end of the plan, not the beginning — so a mid-plan abort leaves the old warning in place rather than a dangling reference to a feature that doesn't work yet).

---

## File Structure

**New files:**

- `autopilot-proxmox/web/smbios_builder.py` — pure function that returns the SMBIOS Type 3 binary bytes for a given chassis-type integer. No I/O.
- `autopilot-proxmox/web/proxmox_snippets.py` — idempotent uploader: "ensure `autopilot-chassis-type-N.bin` exists on the Proxmox host's `local` snippets storage; upload if missing; return the host path". One responsibility.
- `autopilot-proxmox/tests/test_smbios_builder.py`
- `autopilot-proxmox/tests/test_proxmox_snippets.py`

**Modified files:**

- `autopilot-proxmox/files/oem_profiles.yml` — header comment swap: drop the "skipped" warning, say chassis_type is honored via SMBIOS file passthrough.
- `autopilot-proxmox/roles/proxmox_vm_clone/tasks/main.yml` — replace the "Warn about unsupported chassis type" debug block with a fact-building block that sets `_smbios_args` when `_oem_profile.chassis_type` (or a new `chassis_type_override` form override) is present.
- `autopilot-proxmox/roles/proxmox_vm_clone/tasks/update_config.yml` — add `args` key to `_config_body` when `_smbios_args` is defined.
- `autopilot-proxmox/web/app.py` — `start_provision` uploads the needed snippet via `proxmox_snippets.ensure_chassis_type_binary()` before invoking ansible-playbook; resolves chassis-type via new form field and sequence override.
- `autopilot-proxmox/web/sequence_compiler.py` — `set_oem_hardware` handler reads an optional `chassis_type` step param and emits it into `ansible_vars`.
- `autopilot-proxmox/web/templates/provision.html` — add a "Chassis Type (override)" numeric field on the provision form.
- `autopilot-proxmox/tests/integration/test_live.py` — add one test that creates a VM-less fake clone and asserts the Ansible command includes `-e chassis_type_override=…`.

---

## Phase 1 — SMBIOS Type 3 binary builder

### Task 1.1: Core structure builder — failing tests

**Files:**
- Create: `autopilot-proxmox/tests/test_smbios_builder.py`

- [ ] **Step 1: Create the test file**

```python
"""Tests for web.smbios_builder — produces SMBIOS Type 3 (chassis) binary.

Reference: DMTF SMBIOS 2.7 spec §7.4 (System Enclosure or Chassis).
"""
import pytest


def test_build_type3_returns_bytes():
    from web import smbios_builder
    out = smbios_builder.build_type3_chassis(chassis_type=10)
    assert isinstance(out, bytes)
    assert len(out) > 0


def test_build_type3_structure_header():
    """First 4 bytes: type=3, length=0x15, handle=0x0300 little-endian."""
    from web import smbios_builder
    out = smbios_builder.build_type3_chassis(chassis_type=10)
    assert out[0] == 0x03          # Type field
    assert out[1] == 0x15          # Formatted-area length (21 bytes)
    assert out[2:4] == b"\x00\x03" # Handle 0x0300 little-endian


def test_build_type3_chassis_byte_at_offset_5():
    """The chassis type enum lives at byte 5 of the formatted area.
    This is the ONLY byte WMI Win32_SystemEnclosure.ChassisTypes reads
    that this builder controls — every other byte defaults to 'safe'
    or 'unspecified' values."""
    from web import smbios_builder
    for chassis in (3, 8, 9, 10, 14, 15, 30, 31, 32, 35):
        out = smbios_builder.build_type3_chassis(chassis_type=chassis)
        assert out[5] == chassis, f"chassis={chassis} not at byte 5"


def test_build_type3_string_indices_point_into_string_section():
    """Bytes 4, 6, 7, 8 are string-set indices for manufacturer,
    version, serial, asset tag. 1-based. Zero means 'no string'."""
    from web import smbios_builder
    out = smbios_builder.build_type3_chassis(chassis_type=10)
    assert out[4] == 1   # manufacturer string index
    assert out[6] == 2   # version string index
    assert out[7] == 3   # serial string index
    assert out[8] == 4   # asset tag string index


def test_build_type3_states_and_security():
    """Bytes 9-12: Boot-up / Power Supply / Thermal / Security — all
    set to 3 = 'Safe'/'None' which is the SMBIOS-standard value for
    'not reporting'."""
    from web import smbios_builder
    out = smbios_builder.build_type3_chassis(chassis_type=10)
    assert out[9] == 3
    assert out[10] == 3
    assert out[11] == 3
    assert out[12] == 3


def test_build_type3_oem_defined_and_trailing_fields():
    """Bytes 13-16: OEM Defined (4 bytes, 0).
    Byte 17: Height (0 = unspecified).
    Byte 18: Number of Power Cords (0 = unspecified).
    Bytes 19-20: Contained Element Count + Record Length (both 0)."""
    from web import smbios_builder
    out = smbios_builder.build_type3_chassis(chassis_type=10)
    assert out[13:17] == b"\x00\x00\x00\x00"
    assert out[17] == 0
    assert out[18] == 0
    assert out[19] == 0
    assert out[20] == 0


def test_build_type3_strings_section_double_null_terminated():
    """After the 21-byte formatted area: a set of null-terminated
    strings referenced by the string indices, then an additional null
    terminating the structure (so the section ends with \\0\\0)."""
    from web import smbios_builder
    out = smbios_builder.build_type3_chassis(chassis_type=10)
    strings = out[21:]
    # Must end with double-null
    assert strings.endswith(b"\x00\x00")
    # Must contain exactly 4 null-terminated strings (manufacturer,
    # version, serial, asset tag) + terminating null.
    non_empty = [s for s in strings.rstrip(b"\x00").split(b"\x00") if s]
    assert len(non_empty) == 4


def test_build_type3_default_string_values():
    """Default strings are deliberately vendor-neutral so the Type 3
    output merges cleanly with the Type 1 data Proxmox already sets
    (manufacturer/product come from type=1 which QEMU emits separately)."""
    from web import smbios_builder
    out = smbios_builder.build_type3_chassis(chassis_type=10)
    strings = out[21:].rstrip(b"\x00").split(b"\x00")
    # Order matches index 1..4
    assert strings[0] == b"QEMU"
    assert strings[1] == b"1.0"
    assert strings[2] == b"0"
    assert strings[3] == b""  # asset tag intentionally empty


def test_build_type3_rejects_invalid_chassis_type():
    from web import smbios_builder
    with pytest.raises(ValueError):
        smbios_builder.build_type3_chassis(chassis_type=0)
    with pytest.raises(ValueError):
        smbios_builder.build_type3_chassis(chassis_type=256)
    with pytest.raises(ValueError):
        smbios_builder.build_type3_chassis(chassis_type=-1)


def test_build_type3_total_length_is_deterministic():
    """Same input → same bytes. No randomness, no timestamps."""
    from web import smbios_builder
    a = smbios_builder.build_type3_chassis(chassis_type=10)
    b = smbios_builder.build_type3_chassis(chassis_type=10)
    assert a == b
```

- [ ] **Step 2: Confirm failure**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_smbios_builder.py -v`

Expected: all fail with `ModuleNotFoundError: No module named 'web.smbios_builder'`.

---

### Task 1.2: Implement the builder

**Files:**
- Create: `autopilot-proxmox/web/smbios_builder.py`

- [ ] **Step 1: Write the module**

```python
"""Build SMBIOS Type 3 (System Enclosure / Chassis) binaries.

The standard QEMU ``-smbios type=3`` command-line option exposes only
the manufacturer/version/serial/asset/sku string fields — it does not
let us set the Chassis Type enum byte that WMI
``Win32_SystemEnclosure.ChassisTypes`` reads. This module produces a
minimal raw SMBIOS Type 3 structure we can feed into QEMU via
``-smbios file=<path>``, which DOES control that byte.

Output is a single SMBIOS structure: 21-byte formatted area followed by
a null-terminated string set ending in double-null. QEMU's
``-smbios file=`` reads concatenated structures in exactly this format.

Reference: DMTF SMBIOS 2.7 §7.4.
"""
from __future__ import annotations

import struct


# Fixed 16-bit handle for the Type 3 structure. Arbitrary but unused —
# SMBIOS handles only need to be unique across the structures QEMU emits.
# Proxmox/QEMU default structures use handles 0x0000..0x00FF for Type 0 / 1,
# 0x0100-range for Type 2, etc. 0x0300 is clear.
_TYPE3_HANDLE = 0x0300


def build_type3_chassis(chassis_type: int) -> bytes:
    """Return the raw bytes of a SMBIOS Type 3 structure.

    ``chassis_type`` is the 1-byte enum value from SMBIOS §7.4.1
    (e.g., 3 = Desktop, 10 = Notebook, 31 = Convertible). Must fit in
    an unsigned byte and must be nonzero.
    """
    if not isinstance(chassis_type, int) or chassis_type < 1 or chassis_type > 255:
        raise ValueError(
            f"chassis_type must be an integer in 1..255, got {chassis_type!r}"
        )

    # Formatted area: 21 bytes (SMBIOS 2.6 layout, no SKU field).
    formatted = struct.pack(
        "<BBH"    # Type, Length, Handle
        "BB"      # Manufacturer string index, Chassis Type
        "BBB"     # Version, Serial, Asset Tag string indices
        "BBBB"    # Boot-up State, Power Supply State, Thermal State, Security Status
        "4s"      # OEM Defined (4 bytes of zero)
        "BBBB",   # Height, Power Cords, Contained Elt Count, Contained Elt Record Len
        0x03,     # Type = 3 (Chassis)
        0x15,     # Length = 21
        _TYPE3_HANDLE,
        1,        # Manufacturer index
        chassis_type,
        2,        # Version index
        3,        # Serial Number index
        4,        # Asset Tag index
        3, 3, 3, 3,  # All four states = Safe/None
        b"\x00\x00\x00\x00",  # OEM Defined
        0, 0, 0, 0,  # Height, Power Cords, Contained Elt Count, Contained Elt Record Len
    )
    assert len(formatted) == 21, f"formatted area len={len(formatted)}, expected 21"

    # String set: four null-terminated strings matching the indices above,
    # then an additional null terminating the structure. An empty string
    # becomes just "\0" (which is legal and means "no asset tag").
    strings = b"QEMU\x00" + b"1.0\x00" + b"0\x00" + b"\x00" + b"\x00"

    return formatted + strings
```

- [ ] **Step 2: Run tests**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_smbios_builder.py -v`

Expected: 10 passed.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/web/smbios_builder.py autopilot-proxmox/tests/test_smbios_builder.py
git commit -m "feat(smbios): add SMBIOS Type 3 chassis binary builder"
```

---

## Phase 2 — Proxmox snippet uploader

### Task 2.1: `ensure_chassis_type_binary` — failing tests

**Files:**
- Create: `autopilot-proxmox/tests/test_proxmox_snippets.py`

- [ ] **Step 1: Create the test file**

```python
"""Tests for web.proxmox_snippets — idempotent chassis-binary upload."""
from unittest.mock import MagicMock, patch


def test_ensure_uploads_when_absent():
    """If the Proxmox storage doesn't already list the expected filename,
    ensure_chassis_type_binary must POST to the upload endpoint and
    return the on-host path."""
    from web import proxmox_snippets

    # Fake _proxmox_api returns an empty content listing
    listing_calls = []
    upload_calls = []

    def fake_api(path, method="GET", data=None, files=None):
        if method == "GET" and path.endswith("/content"):
            listing_calls.append(path)
            return []  # empty listing
        if method == "POST" and path.endswith("/upload"):
            upload_calls.append({"path": path, "data": data, "files": files})
            return "UPID:fake:0000"
        raise AssertionError(f"unexpected call: {method} {path}")

    with patch("web.proxmox_snippets._proxmox_api", side_effect=fake_api):
        host_path = proxmox_snippets.ensure_chassis_type_binary(
            node="pve", storage="local", chassis_type=10,
        )

    assert host_path == "/var/lib/vz/snippets/autopilot-chassis-type-10.bin"
    assert len(listing_calls) == 1
    assert len(upload_calls) == 1
    # Upload was for content=snippets
    assert upload_calls[0]["data"]["content"] == "snippets"
    # The binary payload is non-empty and type-matches our builder
    from web import smbios_builder
    assert upload_calls[0]["files"]["filename"][1] == \
        smbios_builder.build_type3_chassis(chassis_type=10)


def test_ensure_skips_when_already_present():
    """If the content listing already shows the filename, no POST happens."""
    from web import proxmox_snippets

    def fake_api(path, method="GET", data=None, files=None):
        if method == "GET" and path.endswith("/content"):
            return [
                {"volid": "local:snippets/autopilot-chassis-type-10.bin",
                 "content": "snippets"},
            ]
        raise AssertionError(f"upload should not be called; got {method} {path}")

    with patch("web.proxmox_snippets._proxmox_api", side_effect=fake_api):
        host_path = proxmox_snippets.ensure_chassis_type_binary(
            node="pve", storage="local", chassis_type=10,
        )
    assert host_path == "/var/lib/vz/snippets/autopilot-chassis-type-10.bin"


def test_ensure_validates_chassis_type():
    from web import proxmox_snippets
    import pytest
    with pytest.raises(ValueError):
        proxmox_snippets.ensure_chassis_type_binary(
            node="pve", storage="local", chassis_type=0,
        )


def test_filename_uses_chassis_type_integer():
    from web import proxmox_snippets
    assert proxmox_snippets._binary_filename(10) == \
        "autopilot-chassis-type-10.bin"
    assert proxmox_snippets._binary_filename(31) == \
        "autopilot-chassis-type-31.bin"


def test_host_path_uses_local_default_root():
    """Proxmox's 'local' storage maps to /var/lib/vz; snippets live at
    /var/lib/vz/snippets/<name>. The helper returns the on-host path
    so the Ansible role can pass it to QEMU's -smbios file= option."""
    from web import proxmox_snippets
    assert proxmox_snippets._host_path_for("local", 10) == \
        "/var/lib/vz/snippets/autopilot-chassis-type-10.bin"
```

- [ ] **Step 2: Confirm failure**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_proxmox_snippets.py -v`

Expected: all fail with `ModuleNotFoundError: No module named 'web.proxmox_snippets'`.

---

### Task 2.2: Extend `_proxmox_api` to accept `files=` (for multipart upload)

**Files:**
- Modify: `autopilot-proxmox/web/app.py`

**Context:** The existing `_proxmox_api()` helper takes `path`, `method`, and `data` but not `files`. The snippet upload endpoint requires multipart/form-data with the binary as a file part. Add a `files` kwarg that passes straight through to `requests.request`.

- [ ] **Step 1: Find the current `_proxmox_api` signature**

Run: `grep -n "def _proxmox_api\b" autopilot-proxmox/web/app.py`

It should currently look like:

```python
def _proxmox_api(path, method="GET", data=None):
    ...
    resp = requests.request(method, url, headers=headers, data=data,
                            verify=..., timeout=10)
    ...
```

- [ ] **Step 2: Extend the signature**

Replace the function with:

```python
def _proxmox_api(path, method="GET", data=None, files=None):
    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")
    url = f"https://{host}:{port}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
    resp = requests.request(
        method, url, headers=headers, data=data, files=files,
        verify=cfg.get("proxmox_validate_certs", False),
        timeout=30 if files else 10,  # uploads need more headroom
    )
    resp.raise_for_status()
    return resp.json().get("data", [])
```

- [ ] **Step 3: Run existing tests to confirm no regression**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/ 2>&1 | tail -3`

Expected: all tests still pass (the `files` param defaults to None, existing callers unaffected).

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/web/app.py
git commit -m "feat(web): _proxmox_api accepts optional files= for multipart upload"
```

---

### Task 2.3: Implement `web/proxmox_snippets.py`

**Files:**
- Create: `autopilot-proxmox/web/proxmox_snippets.py`

- [ ] **Step 1: Create the module**

```python
"""Upload per-chassis SMBIOS binaries to a Proxmox node's snippet storage.

QEMU's ``-smbios file=`` option requires the binary to live on the
Proxmox host's local filesystem (not inside the autopilot container).
Proxmox exposes a filesystem-backed 'snippets' content type on the
``local`` storage, typically rooted at ``/var/lib/vz/snippets/``.

Upload is idempotent: before POSTing, we list the storage's ``content``
endpoint for a volid matching the expected filename and skip the upload
if it's already present.
"""
from __future__ import annotations

from web.smbios_builder import build_type3_chassis

# Proxmox's 'local' storage default root. Hard-coded here because the
# Proxmox API does not expose the on-host path directly in a form that
# QEMU can consume — we pass the literal path into ``args:`` so Proxmox
# can't rewrite it via a volid.
_LOCAL_STORAGE_ROOT = "/var/lib/vz"


def _binary_filename(chassis_type: int) -> str:
    return f"autopilot-chassis-type-{int(chassis_type)}.bin"


def _host_path_for(storage: str, chassis_type: int) -> str:
    # Supports the common 'local' storage today; other snippet-content
    # storages can be added if a user needs them later (they'd likely
    # map to a different directory, so this stays simple for now).
    if storage != "local":
        # Fail closed rather than assume a path we'd emit to QEMU.
        raise ValueError(
            f"only the 'local' Proxmox storage is supported for chassis "
            f"binaries (got {storage!r}); map the file manually or extend "
            f"_host_path_for()."
        )
    return f"{_LOCAL_STORAGE_ROOT}/snippets/{_binary_filename(chassis_type)}"


def ensure_chassis_type_binary(*, node: str, storage: str,
                               chassis_type: int) -> str:
    """Guarantee the chassis-type binary is present on the given storage.

    If the content listing shows the volid already, no upload happens.
    Otherwise the binary is generated in-memory and POSTed via the
    Proxmox storage upload endpoint.

    Returns the on-host path to pass to QEMU ``-smbios file=``.
    """
    if not isinstance(chassis_type, int) or chassis_type < 1 or chassis_type > 255:
        raise ValueError(f"chassis_type must be 1..255, got {chassis_type!r}")

    # Late import: this module must load in test environments that don't
    # have a Proxmox config available. We only need the API helper when
    # the function actually runs.
    from web.app import _proxmox_api

    filename = _binary_filename(chassis_type)
    target_path = _host_path_for(storage, chassis_type)

    # 1. Idempotency check — list existing snippets and skip if present.
    try:
        content = _proxmox_api(f"/nodes/{node}/storage/{storage}/content")
    except Exception:
        # If we can't list, proceed to upload and let it fail loudly there.
        content = []
    expected_volid = f"{storage}:snippets/{filename}"
    if any(entry.get("volid") == expected_volid for entry in content or []):
        return target_path

    # 2. Generate and upload.
    blob = build_type3_chassis(chassis_type=chassis_type)
    _proxmox_api(
        f"/nodes/{node}/storage/{storage}/upload",
        method="POST",
        data={"content": "snippets", "filename": filename},
        files={"filename": (filename, blob, "application/octet-stream")},
    )
    return target_path
```

- [ ] **Step 2: Run tests**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_proxmox_snippets.py -v`

Expected: 5 passed.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/web/proxmox_snippets.py autopilot-proxmox/tests/test_proxmox_snippets.py
git commit -m "feat(snippets): idempotent chassis-binary uploader"
```

---

## Phase 3 — Ansible role wiring

### Task 3.1: Replace the warn-and-skip with args construction

**Files:**
- Modify: `autopilot-proxmox/roles/proxmox_vm_clone/tasks/main.yml`

- [ ] **Step 1: Locate the current warning block**

Run: `grep -n "chassis_type skipped\|Warn about unsupported chassis type" autopilot-proxmox/roles/proxmox_vm_clone/tasks/main.yml`

You should find:

```yaml
- name: Warn about unsupported chassis type
  ansible.builtin.debug:
    msg: >-
      WARNING: Chassis type {{ _oem_profile.chassis_type }} skipped —
      Proxmox 9 does not support smbios2.
  when: _oem_profile.chassis_type is defined
```

- [ ] **Step 2: Replace with the SMBIOS-file-reference construction**

Replace the block above with:

```yaml
- name: Resolve effective chassis type
  ansible.builtin.set_fact:
    _effective_chassis_type: >-
      {{ chassis_type_override | default(_oem_profile.chassis_type | default(None), true) }}
  when: _oem_profile is defined or chassis_type_override is defined

- name: Build QEMU -smbios file= args for chassis type
  ansible.builtin.set_fact:
    _smbios_args: >-
      -smbios file=/var/lib/vz/snippets/autopilot-chassis-type-{{ _effective_chassis_type }}.bin
  when: _effective_chassis_type is defined
        and _effective_chassis_type | string | length > 0
        and (_effective_chassis_type | int) > 0
```

The `chassis_type_override` variable is set by the web layer via `-e chassis_type_override=N` when an operator supplies one; otherwise the profile's value wins. Blank or zero values are filtered out so no `args:` line is emitted for VMs that don't have a chassis type.

- [ ] **Step 3: Lint the YAML**

Run: `cd autopilot-proxmox && .venv/bin/python -c "import yaml; yaml.safe_load(open('roles/proxmox_vm_clone/tasks/main.yml')); print('ok')"`

Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/roles/proxmox_vm_clone/tasks/main.yml
git commit -m "feat(ansible): emit -smbios file= args when profile has chassis_type"
```

---

### Task 3.2: Propagate `args:` into the VM config update

**Files:**
- Modify: `autopilot-proxmox/roles/proxmox_vm_clone/tasks/update_config.yml`

- [ ] **Step 1: Add the `args:` injection**

After the existing "Add SMBIOS1 to config" task, add a new task before "Update cloned VM configuration with lock-contention retry":

```yaml
- name: Add args (SMBIOS type 3 file passthrough) to config
  ansible.builtin.set_fact:
    _config_body: "{{ _config_body | combine({'args': _smbios_args}) }}"
  when: _smbios_args is defined and _smbios_args | length > 0
```

- [ ] **Step 2: Lint YAML**

Run: `cd autopilot-proxmox && .venv/bin/python -c "import yaml; yaml.safe_load(open('roles/proxmox_vm_clone/tasks/update_config.yml')); print('ok')"`

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/roles/proxmox_vm_clone/tasks/update_config.yml
git commit -m "feat(ansible): set VM args line when chassis-type SMBIOS file is in use"
```

---

### Task 3.3: Swap the oem_profiles.yml header comment

**Files:**
- Modify: `autopilot-proxmox/files/oem_profiles.yml`

- [ ] **Step 1: Locate the outdated comment**

Run: `grep -n "chassis_type is logged as a warning" autopilot-proxmox/files/oem_profiles.yml`

- [ ] **Step 2: Replace the chassis-type paragraph**

Find:

```yaml
# chassis_type is logged as a warning and skipped — Proxmox 9 does not
# support smbios2 (chassis type) configuration.
```

Replace with:

```yaml
# chassis_type is honored via SMBIOS Type 3 file passthrough: the web
# layer generates a minimal Type 3 binary per unique chassis_type,
# uploads it to the Proxmox 'local' snippets storage, and adds a
# `-smbios file=...` entry to the VM's `args:` line. This is what
# WMI Win32_SystemEnclosure.ChassisTypes reports inside the guest.
# Common values: 3=Desktop, 8=Portable, 9=Laptop, 10=Notebook,
# 14=Sub Notebook, 15=Space-saving, 30=Tablet, 31=Convertible,
# 32=Detachable, 35=Mini PC.
```

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/files/oem_profiles.yml
git commit -m "docs(profiles): note chassis_type now applied via SMBIOS file"
```

---

## Phase 4 — Web layer + compiler + provision form

### Task 4.1: Compiler emits `chassis_type_override` when present

**Files:**
- Modify: `autopilot-proxmox/web/sequence_compiler.py`
- Modify: `autopilot-proxmox/tests/test_sequence_compiler.py`

- [ ] **Step 1: Append failing tests**

Add to `tests/test_sequence_compiler.py`:

```python
def test_set_oem_hardware_emits_chassis_type_override_when_set():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "lenovo-t14", "chassis_type": 10}},
    ])
    result = sequence_compiler.compile(seq)
    assert result.ansible_vars["chassis_type_override"] == "10"


def test_set_oem_hardware_omits_chassis_type_when_missing():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware", "params": {"oem_profile": "lenovo-t14"}},
    ])
    result = sequence_compiler.compile(seq)
    assert "chassis_type_override" not in result.ansible_vars


def test_set_oem_hardware_ignores_zero_chassis_type():
    """0 means 'inherit from profile', not 'override with 0'."""
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "lenovo-t14", "chassis_type": 0}},
    ])
    result = sequence_compiler.compile(seq)
    assert "chassis_type_override" not in result.ansible_vars
```

Run and confirm they fail:

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_sequence_compiler.py -v -k chassis_type`

Expected: 3 fail.

- [ ] **Step 2: Update the handler**

Find `_handle_set_oem_hardware` in `web/sequence_compiler.py` and replace with:

```python
def _handle_set_oem_hardware(params: dict, out: CompiledSequence) -> None:
    profile = (params.get("oem_profile") or "").strip()
    if profile:
        out.ansible_vars["vm_oem_profile"] = profile
    # Optional chassis-type override. 0 / None / missing all mean "inherit
    # from the profile"; only positive integers emit the Ansible var.
    ct = params.get("chassis_type")
    try:
        ct_int = int(ct) if ct is not None else 0
    except (TypeError, ValueError):
        ct_int = 0
    if ct_int > 0:
        out.ansible_vars["chassis_type_override"] = str(ct_int)
```

- [ ] **Step 3: Also extend `resolve_provision_vars` to forward a form-supplied `chassis_type_override`**

Find the `for key in ("vm_oem_profile",):` line near the top of `resolve_provision_vars`. Change it to:

```python
    for key in ("vm_oem_profile", "chassis_type_override"):
        if vars_yml.get(key):
            merged[key] = vars_yml[key]
```

This lets `vars.yml` carry a baseline chassis-type if the operator ever wants one (sequence + form still override).

- [ ] **Step 4: Run tests**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_sequence_compiler.py -v`

Expected: all pass (existing 13 + 3 new = 16).

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/sequence_compiler.py autopilot-proxmox/tests/test_sequence_compiler.py
git commit -m "feat(compiler): set_oem_hardware forwards chassis_type override"
```

---

### Task 4.2: Provision handler uploads the snippet + accepts the form field

**Files:**
- Modify: `autopilot-proxmox/web/app.py`

- [ ] **Step 1: Add the form parameter**

Find the `start_provision` signature. Append one parameter:

```python
    chassis_type_override: int = Form(0),
```

(0 is the "not set" sentinel — see the compiler's behavior.)

- [ ] **Step 2: Add the snippet-upload step**

Immediately after the form-sanitization block (near the top of `start_provision`, before any command construction), add:

```python
    # Ensure the chassis-type SMBIOS binary is on the Proxmox host for
    # the *effective* chassis type. The compiler/precedence layer picks
    # the final value; here we optimistically upload for whichever type
    # any source could request.
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    storage = cfg.get("proxmox_snippets_storage", "local")
    chassis_types_to_stage: set[int] = set()
    if chassis_type_override and chassis_type_override > 0:
        chassis_types_to_stage.add(int(chassis_type_override))
    if sequence_id:
        _seq = sequences_db.get_sequence(SEQUENCES_DB, int(sequence_id))
        if _seq is not None:
            for step in _seq["steps"]:
                if step["step_type"] == "set_oem_hardware" and step.get("enabled"):
                    ct = step["params"].get("chassis_type")
                    if ct and int(ct) > 0:
                        chassis_types_to_stage.add(int(ct))
    # Profile's default chassis_type (from oem_profiles.yml) — look it up
    # lazily; if not found, nothing to stage from this source.
    _profiles = load_oem_profiles()
    prof_for_type = _profiles.get(profile) if profile else None
    if prof_for_type and prof_for_type.get("chassis_type"):
        chassis_types_to_stage.add(int(prof_for_type["chassis_type"]))

    from web import proxmox_snippets
    for ct in chassis_types_to_stage:
        try:
            proxmox_snippets.ensure_chassis_type_binary(
                node=node, storage=storage, chassis_type=ct,
            )
        except Exception as e:
            # Don't block the provision; Ansible will still try the args
            # line and QEMU will log a clear "file not found" if the
            # upload actually failed (vs. just the listing query).
            job_manager_log_warn(f"chassis-type binary upload failed for {ct}: {e}")
```

Note: `job_manager_log_warn` doesn't exist yet — substitute whatever the existing "log a warning to stderr / job log" pattern is in `app.py`. If there's no such helper, use `print(f"[warn] ...", flush=True)` — the uvicorn wrapper captures stdout.

- [ ] **Step 3: Pass the override into the command**

In the single-VM `cmd` construction, add (after the existing `-e vm_count=1` block):

```python
    if chassis_type_override and chassis_type_override > 0:
        cmd += ["-e", f"chassis_type_override={chassis_type_override}"]
```

In the multi-VM branch's `_overrides()` similarly:

```python
        if chassis_type_override > 0:
            tokens += ["-e", f"chassis_type_override={chassis_type_override}"]
```

- [ ] **Step 4: Run existing tests**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/ 2>&1 | tail -3`

Expected: all pass — the form param defaults to 0 so existing tests that don't supply it still work.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/app.py
git commit -m "feat(web): stage chassis-type binary + accept chassis_type_override form field"
```

---

### Task 4.3: Add the chassis-type field to the provision form

**Files:**
- Modify: `autopilot-proxmox/web/templates/provision.html`

- [ ] **Step 1: Find the row for OEM Profile in `provision.html`**

Run: `grep -n "OEM Profile" autopilot-proxmox/web/templates/provision.html`

- [ ] **Step 2: Add a new row below it**

Insert immediately after the OEM Profile row (but before VM Count):

```html
<tr>
  <td><b>Chassis Type override:</b></td>
  <td><select name="chassis_type_override">
    <option value="0">(use profile default)</option>
    <option value="3">3 — Desktop</option>
    <option value="8">8 — Portable</option>
    <option value="9">9 — Laptop</option>
    <option value="10">10 — Notebook</option>
    <option value="14">14 — Sub Notebook</option>
    <option value="15">15 — Space-saving (tiny/mini)</option>
    <option value="30">30 — Tablet</option>
    <option value="31">31 — Convertible</option>
    <option value="32">32 — Detachable</option>
    <option value="35">35 — Mini PC</option>
  </select>
  <small style="color:#999;">Overrides the OEM profile's chassis_type. Applied to the guest via SMBIOS Type 3; reports through WMI Win32_SystemEnclosure.</small>
  </td>
</tr>
```

- [ ] **Step 3: Smoke — verify the page still renders**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_web.py::test_provision_page_renders -v`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/web/templates/provision.html
git commit -m "feat(ui): add Chassis Type override field on provision form"
```

---

## Phase 5 — Tests + smoke + PR

### Task 5.1: Integration test confirms the override reaches the playbook

**Files:**
- Modify: `autopilot-proxmox/tests/test_sequences_api.py`

- [ ] **Step 1: Append a new unit test (uses TestClient + mocked job_manager, no live host)**

```python
def test_provision_passes_chassis_type_override_to_ansible(app_env):
    """POST /api/jobs/provision with chassis_type_override must put
    -e chassis_type_override=N into the ansible command."""
    from web import sequences_db, crypto
    from web.app import SEQUENCES_DB, CREDENTIAL_KEY, job_manager
    cipher = crypto.Cipher(CREDENTIAL_KEY)

    # Minimal sequence so the compile path runs
    seq_id = sequences_db.create_sequence(
        SEQUENCES_DB, name="test-chassis", description="",
    )
    sequences_db.set_sequence_steps(SEQUENCES_DB, seq_id, [
        {"step_type": "autopilot_entra", "params": {}, "enabled": True},
    ])

    captured = {}
    def fake_start(name, cmd, args=None):
        captured["cmd"] = list(cmd)
        return {"id": "fake-job"}
    job_manager.start.side_effect = fake_start
    job_manager.set_arg = lambda *a, **k: None
    job_manager.add_on_complete = lambda *a, **k: None

    # Mock out the snippet uploader to avoid needing a real Proxmox node.
    from unittest.mock import patch
    with patch("web.proxmox_snippets.ensure_chassis_type_binary") as mock_ensure:
        mock_ensure.return_value = "/var/lib/vz/snippets/fake.bin"
        r = app_env.post("/api/jobs/provision", data={
            "profile": "",
            "count": "1",
            "cores": "2",
            "memory_mb": "4096",
            "disk_size_gb": "64",
            "serial_prefix": "",
            "group_tag": "",
            "sequence_id": str(seq_id),
            "chassis_type_override": "31",  # Convertible
        }, follow_redirects=False)
    assert r.status_code == 303
    cmd = captured["cmd"]
    assert "chassis_type_override=31" in cmd
    # And the snippet helper was called for the override value
    mock_ensure.assert_called_with(node="pve", storage="local", chassis_type=31)
```

- [ ] **Step 2: Run**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_sequences_api.py -v -k chassis_type`

Expected: PASS.

- [ ] **Step 3: Full suite**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/ 2>&1 | tail -3`

Expected: all pass (94 + 10 compiler + 5 snippets + 3 compiler-chassis + 1 api-chassis = 113 or so).

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/tests/test_sequences_api.py
git commit -m "test(api): provision forwards chassis_type_override + stages the snippet"
```

---

### Task 5.2: Live-harness smoke check

**Files:**
- Modify: `autopilot-proxmox/tests/integration/test_live.py`

- [ ] **Step 1: Append a single assertion test**

```python
def test_provision_page_has_chassis_type_override_dropdown(session, base_url):
    """Phase-B.2 addition: provision form gains a chassis_type_override
    select. Verifies the UI ships the new field against the live deploy."""
    r = session.get(base_url + "/provision", timeout=10)
    assert r.status_code == 200
    assert 'name="chassis_type_override"' in r.text
    assert "Notebook" in r.text  # at least one option label visible
```

This is only a render-level check — the harness doesn't trigger a real provision.

- [ ] **Step 2: Run the full live harness (will still pass against the current deploy that lacks the new field — the test will fail until the code ships)**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/integration -v --run-integration -k chassis_type`

Expected on current deploy: FAIL (missing field). After deploy (below): PASS.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/tests/integration/test_live.py
git commit -m "test(integration): assert chassis_type_override dropdown on provision form"
```

---

### Task 5.3: Manual QEMU verification on the autopilot-docker box

This step is optional but valuable — it proves the generated binary actually lands in a running VM's SMBIOS before we commit to the approach.

- [ ] **Step 1: SSH to autopilot-docker, generate the binary locally, verify its bytes**

```
ssh root@192.168.2.4 'python3 -c "
import struct
# Reproduce what the builder does for chassis_type=10 (Notebook).
fmt = struct.pack(
  \"<BBHBBBBBBBBB4sBBBB\",
  3, 0x15, 0x0300,
  1, 10, 2, 3, 4,
  3, 3, 3, 3,
  b\"\x00\x00\x00\x00\",
  0, 0, 0, 0,
)
print(len(fmt), fmt.hex())
"'
```

Confirm the output is 21 bytes and byte 5 (zero-indexed — byte at position 5) is `0x0a` (10 = Notebook).

- [ ] **Step 2: If a test VM exists, read its SMBIOS inside the guest**

```
# Inside a running Windows guest:
wmic systemenclosure get chassistypes
# Expected: {10}
```

Or on Linux:

```
sudo dmidecode -t 3 | grep 'Type:'
```

If the chassis type reported by the guest doesn't match what was written, something in the path is wrong and we debug at that point before going further.

- [ ] **Step 3: If manual verification passes, push the branch and open the PR**

```bash
git push -u origin feat/chassis-type-support
gh pr create --base main --head feat/chassis-type-support \
  --title "feat(profiles): real chassis_type reporting via SMBIOS Type 3 passthrough" \
  --body "$(cat <<'EOF'
## Summary

OEM profiles' \`chassis_type\` field now actually lands in the guest's SMBIOS, which is what WMI \`Win32_SystemEnclosure.ChassisTypes\` reports and what SCCM-style OSD task sequences check for \`IsLaptop\`-style conditionals.

## How it works

QEMU's documented \`-smbios type=3\` option doesn't expose the chassis-type enum byte. Workaround: hand-build a minimal SMBIOS Type 3 binary with the right byte, upload once per chassis-type to the Proxmox host's \`local\` snippets storage, reference it via \`args: -smbios file=...\` on the VM config.

- New \`web/smbios_builder.py\` — pure-function binary builder (DMTF SMBIOS 2.6 Type 3, 21-byte formatted area + string set + double-null)
- New \`web/proxmox_snippets.py\` — idempotent uploader; checks the \`content\` listing before POSTing to \`/nodes/{node}/storage/{storage}/upload\`
- \`proxmox_vm_clone\` role — emits \`args: -smbios file=...\` on the VM config when \`_oem_profile.chassis_type\` or \`chassis_type_override\` is set
- New \`chassis_type_override\` form field on \`/provision\` + compiler plumbing for \`set_oem_hardware.params.chassis_type\`

## Test plan

- [ ] pytest tests/ passes
- [ ] live harness \`pytest tests/integration -v --run-integration\` passes on the deployed box
- [ ] In a guest Windows VM cloned with profile having \`chassis_type: 10\`, \`wmic systemenclosure get chassistypes\` reports \`{10}\`
- [ ] Changing the form \`chassis_type_override\` to 31 (Convertible) produces a guest that reports \`{31}\` on a fresh clone

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Coverage vs. goal:**

- ✅ Guest reports real chassis-type byte through WMI / dmidecode (binary builder + file passthrough + Ansible role args)
- ✅ Profile's `chassis_type` field applied (was warn-and-skip, now actively used in Task 3.1)
- ✅ Per-provision override (form field in Task 4.3 + compiler support in Task 4.1 + handler in Task 4.2)
- ✅ Sequence-carried chassis override (Task 4.1 handler change)
- ✅ Idempotent uploads (Task 2.3 listing check)
- ✅ No-regression for existing provisions without chassis data (all new vars are optional with sane zero/empty defaults)
- ✅ Test coverage at every layer: 10 unit tests on the binary builder, 5 on the uploader, 3 on the compiler change, 1 integration test for the HTTP path, 1 smoke against the live deploy

**Placeholder scan:** no TBD, no "implement later", all code blocks complete.

**Type consistency:** `ensure_chassis_type_binary(node, storage, chassis_type)` kwargs match between tests (2.1) and implementation (2.3). `build_type3_chassis(chassis_type=...)` kwarg-only API consistent. `chassis_type_override` var name consistent across compiler, form, Ansible role, and command args.

**Known rough spots:**

- Task 4.2 uses `job_manager_log_warn` which isn't a real helper — flagged inline; implementer must substitute the existing pattern or add a one-line helper.
- The generated binary's strings (`"QEMU", "1.0", "0", ""`) are vendor-neutral on purpose. Autopilot / Intune assignment rules keying on Type-3-specific string fields (not common) would need the builder extended to accept them. Not in scope.
- `_host_path_for()` hardcodes `local` storage's `/var/lib/vz/snippets/` root. Proxmox clusters with a non-`local` snippets storage need extending the helper; flagged in the code comment.
- The integration test in Task 5.2 only checks the UI renders the new field. It doesn't verify the full clone→SMBIOS path, which is what manual Task 5.3 does. If we want an automated end-to-end check, that's a separate, much larger test harness investment (spinning up a real VM, reading its WMI via guest agent) — deferred.
