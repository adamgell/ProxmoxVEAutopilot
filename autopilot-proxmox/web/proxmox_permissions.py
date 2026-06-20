"""Proxmox host bootstrap helpers.

The normal Autopilot API token handles day-to-day VM/storage work, but
some host setup is inherently hypervisor-local: creating roles/ACLs,
enabling snippet storage, and seeding SMBIOS chassis binaries. Those
operations are easiest and clearest over root SSH, which is also the
same capability used later for QEMU args/chassis handling.
"""
from __future__ import annotations

import base64
import shlex
from dataclasses import dataclass

from web import answer_floppy_cache


AUTOPILOT_ROLE = "AutopilotProvisioner"
AUTOPILOT_PRIVILEGES = (
    "VM.Allocate",
    "VM.Clone",
    "VM.Config.CPU",
    "VM.Config.CDROM",
    "VM.Config.Cloudinit",
    "VM.Config.Disk",
    "VM.Config.HWType",
    "VM.Config.Memory",
    "VM.Config.Network",
    "VM.Config.Options",
    "VM.Audit",
    "VM.PowerMgmt",
    "VM.Console",
    "VM.Snapshot",
    "VM.Snapshot.Rollback",
    "VM.GuestAgent.Audit",
    "VM.GuestAgent.FileRead",
    "VM.GuestAgent.FileWrite",
    "VM.GuestAgent.FileSystemMgmt",
    "VM.GuestAgent.Unrestricted",
    "Datastore.Allocate",
    "Datastore.AllocateSpace",
    "Datastore.AllocateTemplate",
    "Datastore.Audit",
    "Sys.Audit",
    "Sys.Modify",
    "SDN.Audit",
    "SDN.Allocate",
    "SDN.Use",
)

DEFAULT_API_TOKEN_ID = "autopilot@pve!ansible"
DEFAULT_SNIPPET_STORAGE = "local"
DEFAULT_CHASSIS_TYPES = (
    3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15,
    16, 17, 23, 24, 30, 31, 32, 35, 36,
)


@dataclass(frozen=True)
class BootstrapResult:
    ok: bool
    stdout: str
    stderr: str
    command: str


def api_token_user(token_id: str | None) -> str:
    """Return the Proxmox user portion of a token id.

    ``autopilot@pve!ansible`` becomes ``autopilot@pve``. If the operator
    supplies only a user id, keep it as-is so role repair still works.
    """
    raw = (token_id or DEFAULT_API_TOKEN_ID).strip() or DEFAULT_API_TOKEN_ID
    return raw.split("!", 1)[0].strip() or "autopilot@pve"


def root_ssh_user(root_username: str | None) -> str:
    raw = (root_username or "root@pam").strip() or "root@pam"
    return raw.split("@", 1)[0] or "root"


def _shell_array(values: list[str]) -> str:
    return " ".join(shlex.quote(v) for v in values)


def _validated_chassis_types(values: list[int] | tuple[int, ...]) -> list[int]:
    out: list[int] = []
    for value in values:
        number = int(value)
        if number < 1 or number > 255:
            raise ValueError(f"chassis type must be 1..255, got {value!r}")
        if number not in out:
            out.append(number)
    return out


def build_bootstrap_script(
    *,
    api_token_id: str | None,
    disk_storage: str | None,
    iso_storage: str | None,
    snippet_storage: str | None = DEFAULT_SNIPPET_STORAGE,
    chassis_types: list[int] | tuple[int, ...] = DEFAULT_CHASSIS_TYPES,
) -> str:
    """Build an idempotent shell script to run on a Proxmox node."""
    user = api_token_user(api_token_id)
    storages = []
    for storage in (disk_storage, iso_storage, snippet_storage):
        value = (storage or "").strip()
        if value and value not in storages:
            storages.append(value)
    snippet = (snippet_storage or DEFAULT_SNIPPET_STORAGE).strip() or DEFAULT_SNIPPET_STORAGE
    chassis = _validated_chassis_types(chassis_types)
    privileges = ",".join(AUTOPILOT_PRIVILEGES)
    chassis_args = " ".join(str(v) for v in chassis)
    storage_array = _shell_array(storages)

    return f"""#!/usr/bin/env bash
set -euo pipefail

ROLE={shlex.quote(AUTOPILOT_ROLE)}
API_USER={shlex.quote(user)}
SNIPPETS_STORAGE={shlex.quote(snippet)}
PRIVS={shlex.quote(privileges)}
STORAGES=({storage_array})
CHASSIS_TYPES=({chassis_args})

if pveum role add "$ROLE" -privs "$PRIVS" >/dev/null 2>&1; then
  echo "role_created=$ROLE"
else
  pveum role modify "$ROLE" -privs "$PRIVS" >/dev/null
  echo "role_updated=$ROLE"
fi

if pveum user add "$API_USER" --comment "Autopilot provisioning" >/dev/null 2>&1; then
  echo "user_created=$API_USER"
else
  pveum user modify "$API_USER" --comment "Autopilot provisioning" >/dev/null 2>&1 || true
  echo "user_present=$API_USER"
fi

pveum acl modify / -user "$API_USER" -role "$ROLE" >/dev/null
echo "acl=/"

for storage in "${{STORAGES[@]}}"; do
  pveum acl modify "/storage/$storage" -user "$API_USER" -role "$ROLE" >/dev/null
  echo "acl=/storage/$storage"
done

if pvesm status | awk 'NR > 1 {{print $1}}' | grep -Fxq "$SNIPPETS_STORAGE"; then
  current="$(python3 - "$SNIPPETS_STORAGE" <<'PY'
import sys

storage = sys.argv[1]
current = None
in_block = False

try:
    with open("/etc/pve/storage.cfg", encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not raw[0].isspace() and ":" in raw:
                rest = raw.split(":", 1)[1].strip()
                in_block = bool(rest and rest.split()[0] == storage)
                continue
            if in_block:
                parts = stripped.split(None, 1)
                if parts and parts[0] == "content":
                    current = parts[1].strip() if len(parts) > 1 else ""
                    break
except FileNotFoundError:
    pass

if current is None:
    current = "backup,iso,vztmpl" if storage == "local" else ""
print(current)
PY
)"
  case ",$current," in
    *,snippets,*) next="$current" ;;
    *) next="${{current:+$current,}}snippets" ;;
  esac
  pvesm set "$SNIPPETS_STORAGE" --content "$next" >/dev/null
  mkdir -p /var/lib/vz/snippets
  echo "snippets_enabled=$SNIPPETS_STORAGE"
else
  echo "snippets_storage_missing=$SNIPPETS_STORAGE"
fi

python3 - "${{CHASSIS_TYPES[@]}}" <<'PY'
import os
import struct
import sys

snippets_dir = "/var/lib/vz/snippets"
type3_handle = 0x0300

def build_type3(chassis_type: int) -> bytes:
    formatted = struct.pack(
        "<BBHBBBBBBBBB4sBBBB",
        0x03, 0x15, type3_handle,
        1, chassis_type,
        2, 3, 4,
        3, 3, 3, 3,
        b"\\x00\\x00\\x00\\x00",
        0, 0, 0, 0,
    )
    strings = b"QEMU\\x00" + b"1.0\\x00" + b"0\\x00" + b"\\x00" + b"Default\\x00" + b"\\x00"
    return formatted + strings

os.makedirs(snippets_dir, exist_ok=True)
for raw in sys.argv[1:]:
    chassis_type = int(raw)
    path = os.path.join(snippets_dir, f"autopilot-chassis-type-{{chassis_type}}.bin")
    with open(path, "wb") as fh:
        fh.write(build_type3(chassis_type))
    os.chmod(path, 0o644)
    print(f"seeded={{path}}")
PY

echo "AUTOPILOT_BOOTSTRAP_OK"
"""


def run_bootstrap_script(
    *,
    host: str,
    root_username: str,
    root_password: str,
    script: str,
) -> BootstrapResult:
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    command = f"printf %s {shlex.quote(payload)} | base64 -d | bash"
    ssh = answer_floppy_cache.make_sshpass_runner(
        host=host,
        password=root_password,
        user=root_ssh_user(root_username),
    )
    rc, stdout, stderr = ssh(command)
    out_text = stdout.decode(errors="replace")
    err_text = stderr.decode(errors="replace")
    return BootstrapResult(
        ok=rc == 0 and "AUTOPILOT_BOOTSTRAP_OK" in out_text,
        stdout=out_text,
        stderr=err_text,
        command=command,
    )
