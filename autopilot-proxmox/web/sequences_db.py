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
                    steps: Optional[list[dict]] = None) -> int:
    now = _now()
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO task_sequences "
            "(name, description, is_default, produces_autopilot_hash, "
            " target_os, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, description, int(is_default), int(produces_autopilot_hash),
             target_os, now, now),
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
                    target_os: Optional[str] = None) -> None:
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
        {"step_type": "install_desktop_environment",
         "params": {"flavor": "ubuntu-desktop"},
         "enabled": True},
        {"step_type": "install_snap_packages",
         "params": {"snaps": [
             {"name": "code", "classic": True},
             {"name": "postman"},
             {"name": "powershell", "classic": True},
         ]},
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
    ubuntu_intune_edge_steps = [
        {"step_type": "install_ubuntu_core", "params": {}, "enabled": True},
        {"step_type": "create_ubuntu_user",
         "params": {"local_admin_credential_id": default_admin_id},
         "enabled": True},
        {"step_type": "install_desktop_environment",
         "params": {"flavor": "ubuntu-desktop"},
         "enabled": True},
        {"step_type": "install_intune_portal", "params": {}, "enabled": True},
        {"step_type": "install_edge", "params": {}, "enabled": True},
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
        # Debconf preseed + noninteractive frontend prevents the install
        # from hanging on the "Allow HTTP tunnels?" modal.
        {"step_type": "run_firstboot_script",
         "params": {
             "command": (
                 "set -e\n"
                 "if ! dpkg -s apt-cacher-ng >/dev/null 2>&1; then\n"
                 "  echo 'apt-cacher-ng apt-cacher-ng/tunnelenable boolean false' "
                 "| debconf-set-selections\n"
                 "  export DEBIAN_FRONTEND=noninteractive\n"
                 "  apt-get update\n"
                 "  apt-get install -y apt-cacher-ng\n"
                 "fi\n"
                 "systemctl enable --now apt-cacher-ng"
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
