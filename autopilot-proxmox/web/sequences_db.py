"""SQLite-backed store for task sequences, steps, and credentials.

Mirrors web.devices_db: module-level SCHEMA string, init() via executescript,
context-managed connections with row_factory=sqlite3.Row.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_sequences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    is_default INTEGER NOT NULL DEFAULT 0,
    produces_autopilot_hash INTEGER NOT NULL DEFAULT 0,
    target_os TEXT NOT NULL DEFAULT 'windows',
    hash_capture_phase TEXT NOT NULL DEFAULT 'oobe' CHECK (hash_capture_phase IN ('winpe','oobe')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_sequence_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id INTEGER NOT NULL REFERENCES task_sequences(id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL,
    step_type TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE (sequence_id, order_index)
);

CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    encrypted_blob BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vm_provisioning (
    vmid INTEGER PRIMARY KEY,
    sequence_id INTEGER REFERENCES task_sequences(id) ON DELETE SET NULL,
    provisioned_at TEXT NOT NULL
);

-- Content-addressed cache for per-VM unattend ISOs. hash is the full
-- SHA-256 of the compiled autounattend.xml bytes; short_hash (first
-- 16 hex chars) appears in the filename on Proxmox storage.
CREATE TABLE IF NOT EXISTS answer_iso_cache (
    hash TEXT PRIMARY KEY,
    short_hash TEXT NOT NULL UNIQUE,
    volid TEXT NOT NULL,
    compiled_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS provisioning_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vmid INTEGER,
    sequence_id INTEGER NOT NULL REFERENCES task_sequences(id),
    provision_path TEXT NOT NULL CHECK (provision_path IN ('clone','winpe')),
    state TEXT NOT NULL,
    vm_uuid TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_provisioning_runs_vm_uuid_state
    ON provisioning_runs(vm_uuid, state);

CREATE TABLE IF NOT EXISTS provisioning_run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES provisioning_runs(id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL,
    phase TEXT NOT NULL,
    kind TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    UNIQUE (run_id, order_index)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(db_path: Path) -> None:
    """Create tables if they don't exist. Safe to call repeatedly."""
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # --- Migration: add target_os to task_sequences if missing (pre-existing DBs)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(task_sequences)")}
        if "target_os" not in cols:
            conn.execute(
                "ALTER TABLE task_sequences "
                "ADD COLUMN target_os TEXT NOT NULL DEFAULT 'windows'"
            )
            conn.execute(
                "UPDATE task_sequences SET target_os='windows' "
                "WHERE target_os IS NULL OR target_os=''"
            )
        # --- Migration: add hash_capture_phase column (pre-existing DBs)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(task_sequences)")}
        if "hash_capture_phase" not in cols:
            conn.execute(
                "ALTER TABLE task_sequences "
                "ADD COLUMN hash_capture_phase TEXT NOT NULL DEFAULT 'oobe' "
                "CHECK (hash_capture_phase IN ('winpe','oobe'))"
            )


class CredentialInUse(Exception):
    """Raised when attempting to delete a credential referenced by a step."""
    def __init__(self, cred_id: int, sequence_ids: list[int]):
        super().__init__(
            f"Credential {cred_id} is referenced by sequences {sequence_ids}"
        )
        self.cred_id = cred_id
        self.sequence_ids = sequence_ids


class SequenceInUse(Exception):
    """Raised when attempting to delete a sequence referenced by vm_provisioning."""
    def __init__(self, sequence_id: int, vmids: list[int]):
        super().__init__(
            f"Sequence {sequence_id} is referenced by VMs {vmids}"
        )
        self.sequence_id = sequence_id
        self.vmids = vmids


def _row_to_credential_summary(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_credential(db_path, cipher, *, name: str, type: str, payload: dict) -> int:
    now = _now()
    blob = cipher.encrypt_json(payload)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO credentials (name, type, encrypted_blob, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, type, blob, now, now),
        )
        return cur.lastrowid


def update_credential(db_path, cipher, cred_id: int, *,
                      name: Optional[str] = None,
                      payload: Optional[dict] = None) -> None:
    now = _now()
    with _connect(db_path) as conn:
        updates, args = [], []
        if name is not None:
            updates.append("name = ?")
            args.append(name)
        if payload is not None:
            updates.append("encrypted_blob = ?")
            args.append(cipher.encrypt_json(payload))
        if not updates:
            return
        updates.append("updated_at = ?")
        args.append(now)
        args.append(cred_id)
        conn.execute(
            f"UPDATE credentials SET {', '.join(updates)} WHERE id = ?", args
        )


def get_credential(db_path, cipher, cred_id: int) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, name, type, encrypted_blob, created_at, updated_at "
            "FROM credentials WHERE id = ?",
            (cred_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        **_row_to_credential_summary(row),
        "payload": cipher.decrypt_json(row["encrypted_blob"]),
    }


def list_credentials(db_path, type: Optional[str] = None) -> list[dict]:
    query = "SELECT id, name, type, created_at, updated_at FROM credentials"
    args: tuple = ()
    if type is not None:
        query += " WHERE type = ?"
        args = (type,)
    query += " ORDER BY name ASC"
    with _connect(db_path) as conn:
        return [_row_to_credential_summary(r) for r in conn.execute(query, args)]


def delete_credential(db_path, cred_id: int) -> None:
    """Delete a credential. Raises CredentialInUse if referenced by any step."""
    with _connect(db_path) as conn:
        referencing = _sequences_referencing_credential(conn, cred_id)
        if referencing:
            raise CredentialInUse(cred_id, referencing)
        conn.execute("DELETE FROM credentials WHERE id = ?", (cred_id,))


def _sequences_referencing_credential(conn: sqlite3.Connection, cred_id: int) -> list[int]:
    """Scan task_sequence_steps.params_json for credential_id references.

    Uses json_extract (SQLite 3.38+). Returns distinct sequence IDs.
    """
    rows = conn.execute(
        "SELECT DISTINCT sequence_id FROM task_sequence_steps "
        "WHERE json_extract(params_json, '$.credential_id') = ?",
        (cred_id,),
    ).fetchall()
    return sorted(r["sequence_id"] for r in rows)


def _row_to_sequence(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "is_default": bool(row["is_default"]),
        "produces_autopilot_hash": bool(row["produces_autopilot_hash"]),
        "target_os": row["target_os"],
        "hash_capture_phase": row["hash_capture_phase"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_step(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "sequence_id": row["sequence_id"],
        "order_index": row["order_index"],
        "step_type": row["step_type"],
        "params": json.loads(row["params_json"]) if row["params_json"] else {},
        "enabled": bool(row["enabled"]),
    }


def create_sequence(db_path, *, name: str, description: str,
                    is_default: bool = False,
                    produces_autopilot_hash: bool = False,
                    target_os: str = "windows",
                    hash_capture_phase: str = "oobe",
                    steps: Optional[list[dict]] = None) -> int:
    if hash_capture_phase not in ("winpe", "oobe"):
        raise ValueError(f"invalid hash_capture_phase: {hash_capture_phase!r}")
    now = _now()
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO task_sequences "
            "(name, description, is_default, produces_autopilot_hash, "
            " target_os, hash_capture_phase, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, description, int(is_default), int(produces_autopilot_hash),
             target_os, hash_capture_phase, now, now),
        )
        new_id = cur.lastrowid
        # Demote any other defaults AFTER the insert succeeds so a failed
        # insert (e.g., UNIQUE constraint) can't leave the DB with zero
        # defaults.
        if is_default:
            conn.execute(
                "UPDATE task_sequences SET is_default = 0 WHERE id != ?",
                (new_id,),
            )
    # Optional steps insertion — same-connection transaction already committed,
    # so set_sequence_steps opens its own connection.
    if steps is not None:
        set_sequence_steps(db_path, new_id, steps)
    return new_id


def update_sequence(db_path, seq_id: int, *,
                    name: Optional[str] = None,
                    description: Optional[str] = None,
                    is_default: Optional[bool] = None,
                    produces_autopilot_hash: Optional[bool] = None,
                    target_os: Optional[str] = None,
                    hash_capture_phase: Optional[str] = None) -> None:
    if hash_capture_phase is not None and hash_capture_phase not in ("winpe", "oobe"):
        raise ValueError(f"invalid hash_capture_phase: {hash_capture_phase!r}")
    now = _now()
    updates, args = [], []
    if name is not None:
        updates.append("name = ?"); args.append(name)
    if description is not None:
        updates.append("description = ?"); args.append(description)
    if produces_autopilot_hash is not None:
        updates.append("produces_autopilot_hash = ?")
        args.append(int(produces_autopilot_hash))
    if is_default is not None:
        updates.append("is_default = ?"); args.append(int(is_default))
    if target_os is not None:
        updates.append("target_os = ?"); args.append(target_os)
    if hash_capture_phase is not None:
        updates.append("hash_capture_phase = ?"); args.append(hash_capture_phase)
    if not updates:
        return
    updates.append("updated_at = ?"); args.append(now)
    args.append(seq_id)
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE task_sequences SET {', '.join(updates)} WHERE id = ?", args
        )
        # Demote other defaults AFTER the targeted update succeeds — if the
        # above UPDATE raised (e.g., UNIQUE name conflict), we'd otherwise
        # leave the DB with zero defaults.
        if is_default:
            conn.execute(
                "UPDATE task_sequences SET is_default = 0 WHERE id != ?",
                (seq_id,),
            )


def get_sequence(db_path, seq_id: int) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM task_sequences WHERE id = ?", (seq_id,)
        ).fetchone()
        if row is None:
            return None
        seq = _row_to_sequence(row)
        step_rows = conn.execute(
            "SELECT * FROM task_sequence_steps WHERE sequence_id = ? "
            "ORDER BY order_index ASC",
            (seq_id,),
        ).fetchall()
        seq["steps"] = [_row_to_step(r) for r in step_rows]
        return seq


def list_sequences(db_path) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT s.*, "
            "  (SELECT COUNT(*) FROM task_sequence_steps "
            "   WHERE sequence_id = s.id) AS step_count "
            "FROM task_sequences s ORDER BY s.name ASC"
        ).fetchall()
    return [{**_row_to_sequence(r), "step_count": r["step_count"]} for r in rows]


def set_sequence_steps(db_path, seq_id: int, steps: list[dict]) -> None:
    """Replace the full step list for a sequence atomically.

    Each step dict: {step_type, params, enabled}. order_index is assigned
    from list position.
    """
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM task_sequence_steps WHERE sequence_id = ?", (seq_id,)
        )
        for idx, step in enumerate(steps):
            conn.execute(
                "INSERT INTO task_sequence_steps "
                "(sequence_id, order_index, step_type, params_json, enabled) "
                "VALUES (?, ?, ?, ?, ?)",
                (seq_id, idx, step["step_type"],
                 json.dumps(step.get("params", {}), separators=(",", ":")),
                 int(step.get("enabled", True))),
            )
        conn.execute(
            "UPDATE task_sequences SET updated_at = ? WHERE id = ?",
            (_now(), seq_id),
        )


def delete_sequence(db_path, seq_id: int) -> None:
    """Delete a sequence. Raises SequenceInUse if referenced by vm_provisioning."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT vmid FROM vm_provisioning WHERE sequence_id = ?", (seq_id,)
        ).fetchall()
        if rows:
            raise SequenceInUse(seq_id, [r["vmid"] for r in rows])
        conn.execute("DELETE FROM task_sequences WHERE id = ?", (seq_id,))


def duplicate_sequence(db_path, seq_id: int, *, new_name: str) -> int:
    seq = get_sequence(db_path, seq_id)
    if seq is None:
        raise ValueError(f"sequence {seq_id} not found")
    new_id = create_sequence(
        db_path, name=new_name, description=seq["description"],
        is_default=False,
        produces_autopilot_hash=seq["produces_autopilot_hash"],
        target_os=seq.get("target_os", "windows"),
        hash_capture_phase=seq["hash_capture_phase"],
    )
    set_sequence_steps(db_path, new_id, [
        {"step_type": s["step_type"], "params": s["params"], "enabled": s["enabled"]}
        for s in seq["steps"]
    ])
    return new_id


def get_default_sequence_id(db_path) -> Optional[int]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM task_sequences WHERE is_default = 1 LIMIT 1"
        ).fetchone()
    return row["id"] if row else None


def record_vm_provisioning(db_path, *, vmid: int, sequence_id: int) -> None:
    now = _now()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO vm_provisioning (vmid, sequence_id, provisioned_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(vmid) DO UPDATE SET "
            "  sequence_id = excluded.sequence_id, "
            "  provisioned_at = excluded.provisioned_at",
            (vmid, sequence_id, now),
        )


def get_vm_sequence_id(db_path, vmid: int) -> Optional[int]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT sequence_id FROM vm_provisioning WHERE vmid = ?", (vmid,)
        ).fetchone()
    return row["sequence_id"] if row else None


def get_vm_provisioning(db_path, *, vmid: int) -> Optional[dict]:
    """Return the vm_provisioning row for vmid, or None.

    Shape: {vmid, sequence_id, provisioned_at}. Used by the Devices page
    to look up the target_os of the sequence that provisioned a VM so the
    UI can show the right actions (e.g. Check Enrollment for Ubuntu VMs).
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT vmid, sequence_id, provisioned_at "
            "FROM vm_provisioning WHERE vmid = ?",
            (vmid,),
        ).fetchone()
    if row is None:
        return None
    return {
        "vmid": row["vmid"],
        "sequence_id": row["sequence_id"],
        "provisioned_at": row["provisioned_at"],
    }


# Seed data is defined here rather than a separate file so version-controlled
# changes to seeded sequences are obvious in the diff.

_SEED_SEQUENCES = [
    {
        "name": "Entra Join (default)",
        "description": "Entra-joined via Windows Autopilot. Matches the pre-sequence hardcoded flow.",
        "is_default": True,
        "produces_autopilot_hash": True,
        "steps": [
            {"step_type": "set_oem_hardware",
             "params": {"oem_profile": ""},
             "enabled": True},
            {"step_type": "local_admin",
             "params": {"credential_name": "default-local-admin"},
             # Disabled until Phase B.2 wires the unattend ISO mechanism
             # that actually consumes the local-admin output. The local-admin
             # credentials today come from the template's baked unattend.
             "enabled": False},
            {"step_type": "autopilot_entra", "params": {}, "enabled": True},
        ],
    },
    {
        "name": "AD Domain Join — Local Admin",
        "description": "Local admin created at OOBE, computer joined to AD during specialize pass.",
        "is_default": False,
        "produces_autopilot_hash": False,
        "steps": [
            {"step_type": "set_oem_hardware",
             "params": {"oem_profile": ""}, "enabled": True},
            {"step_type": "local_admin",
             "params": {"credential_name": "default-local-admin"},
             "enabled": False},
            {"step_type": "join_ad_domain",
             "params": {"credential_id": 0, "ou_path": ""},
             "enabled": False},
            {"step_type": "rename_computer",
             "params": {"pattern": "{serial}"}, "enabled": False},
        ],
    },
    {
        "name": "Hybrid Autopilot (stub)",
        "description": "NOT IMPLEMENTED in v1 — scaffolded for future work.",
        "is_default": False,
        "produces_autopilot_hash": True,
        "steps": [
            {"step_type": "autopilot_hybrid", "params": {}, "enabled": True},
        ],
    },
]


_SEED_LOCAL_ADMIN_PAYLOAD = {
    "username": "Administrator",
    # The hardcoded password from files/unattend_oobe.xml, preserved so the
    # default sequence reproduces today's byte-identical output.
    "password": "Nsta1200!!",
}


def seed_defaults(db_path, cipher) -> None:
    """Insert the default credential and starter sequences if absent.

    Idempotent: rows keyed on name are skipped if already present.
    Resolves credential_name references to actual credential IDs.
    Seeds Windows sequences plus Ubuntu LinuxESP and Ubuntu Plain.
    """
    # 1. Credential first — other seeds reference it by name.
    if not any(c["name"] == "default-local-admin"
               for c in list_credentials(db_path, type="local_admin")):
        create_credential(
            db_path, cipher,
            name="default-local-admin", type="local_admin",
            payload=_SEED_LOCAL_ADMIN_PAYLOAD,
        )
    la_id = next(c["id"] for c in list_credentials(db_path, type="local_admin")
                 if c["name"] == "default-local-admin")

    # 2. Windows sequences.
    existing_names = {s["name"] for s in list_sequences(db_path)}
    for seq in _SEED_SEQUENCES:
        if seq["name"] in existing_names:
            continue
        sid = create_sequence(
            db_path,
            name=seq["name"], description=seq["description"],
            is_default=seq["is_default"],
            produces_autopilot_hash=seq["produces_autopilot_hash"],
        )
        # Resolve credential_name → credential_id
        resolved_steps = []
        for step in seq["steps"]:
            params = dict(step["params"])
            if params.pop("credential_name", None) == "default-local-admin":
                params["credential_id"] = la_id
            resolved_steps.append({
                "step_type": step["step_type"],
                "params": params,
                "enabled": step["enabled"],
            })
        set_sequence_steps(db_path, sid, resolved_steps)

    # 3. Ubuntu sequences. Use `la_id` as the default local admin credential
    #    reference; the MDE onboarding credential is left as 0 so the operator
    #    must wire up a real credential before provisioning.
    default_admin_id = la_id
    existing_names = {s["name"] for s in list_sequences(db_path)}

    # ORDERING: ubuntu-desktop is installed LAST because it pulls
    # NetworkManager, which takes over networking from cloud-init's
    # systemd-networkd. Any apt fetch after that (Microsoft repos, MDE,
    # bloat purge) hits "Network is unreachable" during the handover.
    # Putting MS repo setup + package installs before ubuntu-desktop
    # avoids the problem entirely.
    ubuntu_linuxesp_steps = [
        {"step_type": "install_ubuntu_core",
         "params": {"locale": "en_US.UTF-8", "timezone": "UTC",
                    "keyboard_layout": "us", "storage_layout": "lvm"},
         "enabled": True},
        {"step_type": "create_ubuntu_user",
         "params": {"local_admin_credential_id": default_admin_id},
         "enabled": True},
        {"step_type": "install_apt_packages",
         "params": {"packages": ["curl", "git", "wget", "gpg"]},
         "enabled": True},
        {"step_type": "install_intune_portal", "params": {}, "enabled": True},
        {"step_type": "install_edge", "params": {}, "enabled": True},
        {"step_type": "install_mde_linux",
         "params": {"mde_onboarding_credential_id": 0},  # user must fill in
         "enabled": True},
        {"step_type": "remove_apt_packages",
         "params": {"packages": ["libreoffice-common", "libreoffice*",
                                 "remmina*", "transmission*"]},
         "enabled": True},
        {"step_type": "install_snap_packages",
         "params": {"snaps": [
             {"name": "code", "classic": True},
             {"name": "postman"},
             {"name": "powershell", "classic": True},
         ]},
         "enabled": True},
        {"step_type": "install_desktop_environment",
         "params": {"flavor": "ubuntu-desktop"},
         "enabled": True},
    ]

    ubuntu_plain_steps = [
        {"step_type": "install_ubuntu_core", "params": {}, "enabled": True},
        {"step_type": "create_ubuntu_user",
         "params": {"local_admin_credential_id": default_admin_id},
         "enabled": True},
        # Give Ubuntu Plain a full desktop. Cloud images are headless; without
        # this the VM boots to a tty. ubuntu-desktop is ~1.5GB but the LAN
        # apt proxy makes repeat builds cheap.
        {"step_type": "install_desktop_environment",
         "params": {"flavor": "ubuntu-desktop"},
         "enabled": True},
    ]

    # LAN apt cache: a small, always-on VM running apt-cacher-ng. When
    # `ubuntu_apt_proxy` is set in vars.yml to http://<this-vm-ip>:3142, every
    # future Ubuntu template build pulls debs through the cache. First cache
    # hit fills it; subsequent builds finish dramatically faster.
    # Workstation flavor of the LinuxESP sequence without MDE. Useful for
    # validating the Intune + Edge half without needing an onboarding script
    # uploaded to the Credentials page first.
    #
    # ORDERING NOTE: install_desktop_environment must come AFTER
    # install_intune_portal + install_edge. ubuntu-desktop drags in
    # NetworkManager, which takes over networking from cloud-init's
    # systemd-networkd; if MS repo fetches happen after that, they fail
    # with "Network is unreachable" because the DHCP lease is mid-handover.
    ubuntu_intune_edge_steps = [
        {"step_type": "install_ubuntu_core", "params": {}, "enabled": True},
        {"step_type": "create_ubuntu_user",
         "params": {"local_admin_credential_id": default_admin_id},
         "enabled": True},
        {"step_type": "install_intune_portal", "params": {}, "enabled": True},
        {"step_type": "install_edge", "params": {}, "enabled": True},
        {"step_type": "install_desktop_environment",
         "params": {"flavor": "ubuntu-desktop"},
         "enabled": True},
    ]

    ubuntu_apt_cache_steps = [
        {"step_type": "install_ubuntu_core", "params": {}, "enabled": True},
        {"step_type": "create_ubuntu_user",
         "params": {"local_admin_credential_id": default_admin_id},
         "enabled": True},
        # Install apt-cacher-ng at FIRST BOOT on the clone. We can't rely on
        # install_apt_packages here because that emits cloud-config.packages,
        # which only runs during template build. When you clone from the
        # standard Ubuntu Plain template, the per-VM NoCloud seed only
        # carries firstboot runcmd — so the install has to live there.
        #
        # Tunnel-enable is preseeded true so apt-cacher-ng allows CONNECT to
        # HTTPS backends. Microsoft's packages.microsoft.com is HTTPS-only,
        # and apt sends CONNECT through the proxy for HTTPS repos. Without
        # tunnels, the proxy returns "403 CONNECT denied" and HTTPS apt
        # sources fail. Also adds PassThroughPattern as belt-and-suspenders.
        {"step_type": "run_firstboot_script",
         "params": {
             "command": (
                 "set -e\n"
                 "if ! dpkg -s apt-cacher-ng >/dev/null 2>&1; then\n"
                 "  echo 'apt-cacher-ng apt-cacher-ng/tunnelenable boolean true' "
                 "| debconf-set-selections\n"
                 "  export DEBIAN_FRONTEND=noninteractive\n"
                 "  apt-get update\n"
                 "  apt-get install -y apt-cacher-ng\n"
                 "fi\n"
                 "# Explicitly allow HTTPS pass-through to any host — safer\n"
                 "# than tunnelenable alone and survives future reconfigures.\n"
                 "# Unquoted — apt-cacher-ng treats the value as a regex; "
                 "quotes become literal regex characters and break matching.\n"
                 "grep -q '^PassThroughPattern' /etc/apt-cacher-ng/acng.conf || "
                 "echo 'PassThroughPattern: .*' >> /etc/apt-cacher-ng/acng.conf\n"
                 "systemctl enable --now apt-cacher-ng\n"
                 "systemctl restart apt-cacher-ng"
             ),
         },
         "enabled": True},
    ]

    if "Ubuntu Intune + MDE (LinuxESP)" not in existing_names:
        create_sequence(
            db_path,
            name="Ubuntu Intune + MDE (LinuxESP)",
            description=("Ubuntu 24.04 with Intune Portal, Edge, and MDE "
                         "(from adamgell/LinuxESP). Set an mde_onboarding "
                         "credential before first use."),
            is_default=False,
            produces_autopilot_hash=False,
            target_os="ubuntu",
            steps=ubuntu_linuxesp_steps,
        )

    if "Ubuntu Plain" not in existing_names:
        create_sequence(
            db_path,
            name="Ubuntu Plain",
            description=("Minimal Ubuntu 24.04 — no Intune, no MDE. Good "
                         "starting point for a custom sequence."),
            is_default=False,
            produces_autopilot_hash=False,
            target_os="ubuntu",
            steps=ubuntu_plain_steps,
        )

    if "Ubuntu Intune + Edge (no MDE)" not in existing_names:
        create_sequence(
            db_path,
            name="Ubuntu Intune + Edge (no MDE)",
            description=("Ubuntu 24.04 workstation with Intune Portal and "
                         "Microsoft Edge, minus the MDE step. Good for "
                         "validating the Microsoft stack without an "
                         "mde_onboarding credential uploaded."),
            is_default=False,
            produces_autopilot_hash=False,
            target_os="ubuntu",
            steps=ubuntu_intune_edge_steps,
        )

    if "Ubuntu apt-cache server" not in existing_names:
        create_sequence(
            db_path,
            name="Ubuntu apt-cache server",
            description=("LAN-local apt-cacher-ng on Ubuntu 24.04. Provision "
                         "one VM from this sequence, then point "
                         "`ubuntu_apt_proxy` in vars.yml at "
                         "http://<vm-ip>:3142 to accelerate every future "
                         "Ubuntu template build."),
            is_default=False,
            produces_autopilot_hash=False,
            target_os="ubuntu",
            steps=ubuntu_apt_cache_steps,
        )


def create_provisioning_run(db_path, *,
                            sequence_id: int,
                            provision_path: str) -> int:
    if provision_path not in ("clone", "winpe"):
        raise ValueError(f"invalid provision_path: {provision_path!r}")
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO provisioning_runs "
            "(sequence_id, provision_path, state, started_at) "
            "VALUES (?, ?, 'queued', ?)",
            (sequence_id, provision_path, _now()),
        )
        return cur.lastrowid


def get_provisioning_run(db_path, run_id: int) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM provisioning_runs WHERE id=?", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def _normalize_uuid(vm_uuid: str) -> str:
    """Canonical UUID form for DB writes and lookups: lowercase, stripped.
    The Ansible role's _vm_identity.uuid is upper-case; WMI in WinPE
    typically returns lower-case but is documented as case-insensitive.
    Persist one form so an exact-match WHERE clause can be used."""
    return (vm_uuid or "").strip().lower()


def set_provisioning_run_identity(db_path, *,
                                  run_id: int,
                                  vmid: int,
                                  vm_uuid: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE provisioning_runs "
            "SET vmid=?, vm_uuid=?, state='awaiting_winpe' "
            "WHERE id=? AND state='queued'",
            (vmid, _normalize_uuid(vm_uuid), run_id),
        )


def find_run_by_uuid_state(db_path, *,
                           vm_uuid: str,
                           state: str) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM provisioning_runs "
            "WHERE vm_uuid=? AND state=? "
            "ORDER BY id DESC LIMIT 1",
            (_normalize_uuid(vm_uuid), state),
        ).fetchone()
    return dict(row) if row else None


def update_provisioning_run_state(db_path, *,
                                  run_id: int,
                                  state: str,
                                  last_error: Optional[str] = None) -> None:
    with _connect(db_path) as conn:
        if state in ("done", "failed"):
            conn.execute(
                "UPDATE provisioning_runs "
                "SET state=?, last_error=?, finished_at=? "
                "WHERE id=?",
                (state, last_error, _now(), run_id),
            )
        else:
            conn.execute(
                "UPDATE provisioning_runs "
                "SET state=?, last_error=? WHERE id=?",
                (state, last_error, run_id),
            )


def append_run_step(db_path, *,
                    run_id: int,
                    phase: str,
                    kind: str,
                    params: dict) -> dict:
    with _connect(db_path) as conn:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(order_index)+1, 0) "
            "FROM provisioning_run_steps WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO provisioning_run_steps "
            "(run_id, order_index, phase, kind, params_json, state) "
            "VALUES (?, ?, ?, ?, ?, 'pending')",
            (run_id, nxt, phase, kind, json.dumps(params, sort_keys=True)),
        )
        sid = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM provisioning_run_steps WHERE id=?", (sid,)
        ).fetchone()
        return dict(row)


def update_run_step_state(db_path, *,
                          step_id: int,
                          state: str,
                          error: Optional[str] = None) -> None:
    with _connect(db_path) as conn:
        if state == "running":
            conn.execute(
                "UPDATE provisioning_run_steps "
                "SET state='running', started_at=COALESCE(started_at, ?), "
                "finished_at=NULL, error=NULL "
                "WHERE id=?",
                (_now(), step_id),
            )
        elif state in ("ok", "error"):
            conn.execute(
                "UPDATE provisioning_run_steps "
                "SET state=?, finished_at=?, error=? WHERE id=?",
                (state, _now(), error, step_id),
            )
        else:
            raise ValueError(f"invalid step state: {state!r}")


def list_run_steps(db_path, run_id: int) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM provisioning_run_steps "
            "WHERE run_id=? ORDER BY order_index",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_run_step(db_path, step_id: int) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM provisioning_run_steps WHERE id=?", (step_id,)
        ).fetchone()
    return dict(row) if row else None


def sweep_stale_runs(db_path, *, ttl_seconds: int = 1800) -> int:
    """Mark any run whose newest step has been 'running' for longer
    than ttl_seconds as failed. Returns the count of runs flipped.

    Called inline by /api/runs/<id> reads and /winpe/register so we
    do not need a background reaper. ttl_seconds = 30 min by default
    (covers a slow apply_wim + driver inject on cold storage; tighten
    if your hardware allows).
    """
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
    cutoff_iso = cutoff.isoformat(timespec="seconds")
    flipped = 0
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT r.id AS run_id, MAX(s.started_at) AS last_started "
            "FROM provisioning_runs r "
            "JOIN provisioning_run_steps s ON s.run_id = r.id "
            "WHERE r.state IN ("
            "'awaiting_winpe','awaiting_windows_setup',"
            "'awaiting_osd_client','awaiting_specialize') "
            "  AND s.state = 'running' "
            "GROUP BY r.id "
            "HAVING last_started IS NOT NULL AND last_started < ?",
            (cutoff_iso,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE provisioning_runs "
                "SET state='failed', last_error=?, finished_at=? "
                "WHERE id=? AND state IN ("
                "'awaiting_winpe','awaiting_windows_setup',"
                "'awaiting_osd_client','awaiting_specialize')",
                (
                    f"stale; no step update for >{ttl_seconds//60} min",
                    _now(), row["run_id"],
                ),
            )
            conn.execute(
                "UPDATE provisioning_run_steps "
                "SET state='error', finished_at=?, error='stale: agent silent' "
                "WHERE run_id=? AND state='running'",
                (_now(), row["run_id"]),
            )
            flipped += 1
    return flipped
