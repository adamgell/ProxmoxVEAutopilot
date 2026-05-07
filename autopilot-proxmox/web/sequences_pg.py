"""PostgreSQL compatibility store for legacy task-sequence runtime state.

This module intentionally preserves the public shape of ``sequences_db`` while
moving the live tables to the shared application Postgres database. Callers may
continue to pass the old ``db_path`` first argument; it is ignored unless it is
already a psycopg connection.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


SCHEMA = """
CREATE TABLE IF NOT EXISTS task_sequences (
    id bigserial PRIMARY KEY,
    name text NOT NULL UNIQUE,
    description text NOT NULL DEFAULT '',
    is_default boolean NOT NULL DEFAULT false,
    produces_autopilot_hash boolean NOT NULL DEFAULT false,
    target_os text NOT NULL DEFAULT 'windows',
    hash_capture_phase text NOT NULL DEFAULT 'oobe'
        CHECK (hash_capture_phase IN ('winpe','oobe')),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS task_sequence_steps (
    id bigserial PRIMARY KEY,
    sequence_id bigint NOT NULL REFERENCES task_sequences(id) ON DELETE CASCADE,
    order_index integer NOT NULL,
    step_type text NOT NULL,
    params_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    enabled boolean NOT NULL DEFAULT true,
    UNIQUE (sequence_id, order_index)
);

CREATE TABLE IF NOT EXISTS credentials (
    id bigserial PRIMARY KEY,
    name text NOT NULL UNIQUE,
    type text NOT NULL,
    encrypted_blob bytea NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS vm_provisioning (
    vmid integer PRIMARY KEY,
    sequence_id bigint REFERENCES task_sequences(id) ON DELETE SET NULL,
    provisioned_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS answer_iso_cache (
    hash text PRIMARY KEY,
    short_hash text NOT NULL UNIQUE,
    volid text NOT NULL,
    compiled_at timestamptz NOT NULL,
    last_used_at timestamptz NULL
);

CREATE TABLE IF NOT EXISTS provisioning_runs (
    id bigserial PRIMARY KEY,
    vmid integer NULL,
    sequence_id bigint NOT NULL REFERENCES task_sequences(id),
    provision_path text NOT NULL CHECK (provision_path IN ('clone','winpe')),
    state text NOT NULL,
    vm_uuid text NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NULL,
    last_error text NULL
);

CREATE INDEX IF NOT EXISTS idx_provisioning_runs_vm_uuid_state
    ON provisioning_runs(vm_uuid, state);

CREATE TABLE IF NOT EXISTS provisioning_run_steps (
    id bigserial PRIMARY KEY,
    run_id bigint NOT NULL REFERENCES provisioning_runs(id) ON DELETE CASCADE,
    order_index integer NOT NULL,
    phase text NOT NULL,
    kind text NOT NULL,
    params_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL,
    started_at timestamptz NULL,
    finished_at timestamptz NULL,
    error text NULL,
    UNIQUE (run_id, order_index)
);
"""


DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS provisioning_run_steps CASCADE;
DROP TABLE IF EXISTS provisioning_runs CASCADE;
DROP TABLE IF EXISTS answer_iso_cache CASCADE;
DROP TABLE IF EXISTS vm_provisioning CASCADE;
DROP TABLE IF EXISTS credentials CASCADE;
DROP TABLE IF EXISTS task_sequence_steps CASCADE;
DROP TABLE IF EXISTS task_sequences CASCADE;
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat(timespec="seconds")
    return str(value)


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _jsonb(value: Any) -> Jsonb:
    if isinstance(value, str):
        value = json.loads(value) if value else {}
    return Jsonb(value or {})


@contextmanager
def _connect(handle: Any = None) -> Iterator[Connection]:
    if isinstance(handle, Connection):
        yield handle
        return
    with db_pg.connection(db_pg.database_url()) as conn:
        yield conn
        conn.commit()


def init(conn: Connection | None = None) -> None:
    with _connect(conn) as c:
        c.execute(SCHEMA)
        c.commit()


def reset_for_tests(conn: Connection) -> None:
    conn.execute(DROP_SCHEMA_FOR_TESTS)
    conn.commit()


class CredentialInUse(Exception):
    """Raised when attempting to delete a credential referenced by a step."""

    def __init__(self, cred_id: int, sequence_ids: list[int]):
        super().__init__(f"Credential {cred_id} is referenced by sequences {sequence_ids}")
        self.cred_id = cred_id
        self.sequence_ids = sequence_ids


class SequenceInUse(Exception):
    """Raised when attempting to delete a sequence referenced by vm_provisioning."""

    def __init__(self, sequence_id: int, vmids: list[int]):
        super().__init__(f"Sequence {sequence_id} is referenced by VMs {vmids}")
        self.sequence_id = sequence_id
        self.vmids = vmids


def _row_to_credential_summary(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "type": row["type"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def create_credential(db_path, cipher, *, name: str, type: str, payload: dict) -> int:
    now = _now()
    blob = cipher.encrypt_json(payload)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            INSERT INTO credentials (name, type, encrypted_blob, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (name, type, blob, now, now),
        ).fetchone()
        return int(row["id"])


def update_credential(
    db_path,
    cipher,
    cred_id: int,
    *,
    name: Optional[str] = None,
    payload: Optional[dict] = None,
) -> None:
    updates: list[str] = []
    args: list[Any] = []
    if name is not None:
        updates.append("name = %s")
        args.append(name)
    if payload is not None:
        updates.append("encrypted_blob = %s")
        args.append(cipher.encrypt_json(payload))
    if not updates:
        return
    updates.append("updated_at = %s")
    args.append(_now())
    args.append(cred_id)
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE credentials SET {', '.join(updates)} WHERE id = %s",
            args,
        )


def get_credential(db_path, cipher, cred_id: int) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, name, type, encrypted_blob, created_at, updated_at
            FROM credentials
            WHERE id = %s
            """,
            (cred_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        **_row_to_credential_summary(row),
        "payload": cipher.decrypt_json(bytes(row["encrypted_blob"])),
    }


def list_credentials(db_path, type: Optional[str] = None) -> list[dict]:
    query = "SELECT id, name, type, created_at, updated_at FROM credentials"
    args: tuple[Any, ...] = ()
    if type is not None:
        query += " WHERE type = %s"
        args = (type,)
    query += " ORDER BY name ASC"
    with _connect(db_path) as conn:
        rows = conn.execute(query, args).fetchall()
    return [_row_to_credential_summary(row) for row in rows]


def _sequences_referencing_credential(conn: Connection, cred_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT sequence_id
        FROM task_sequence_steps
        WHERE (params_json ->> 'credential_id')::bigint = %s
        """,
        (cred_id,),
    ).fetchall()
    return sorted(int(row["sequence_id"]) for row in rows)


def delete_credential(db_path, cred_id: int) -> None:
    with _connect(db_path) as conn:
        referencing = _sequences_referencing_credential(conn, cred_id)
        if referencing:
            raise CredentialInUse(cred_id, referencing)
        conn.execute("DELETE FROM credentials WHERE id = %s", (cred_id,))


def _row_to_sequence(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "is_default": bool(row["is_default"]),
        "produces_autopilot_hash": bool(row["produces_autopilot_hash"]),
        "target_os": row["target_os"],
        "hash_capture_phase": row["hash_capture_phase"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _row_to_step(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "sequence_id": int(row["sequence_id"]),
        "order_index": int(row["order_index"]),
        "step_type": row["step_type"],
        "params": row["params_json"] or {},
        "enabled": bool(row["enabled"]),
    }


def create_sequence(
    db_path,
    *,
    name: str,
    description: str,
    is_default: bool = False,
    produces_autopilot_hash: bool = False,
    target_os: str = "windows",
    hash_capture_phase: str = "oobe",
    steps: Optional[list[dict]] = None,
) -> int:
    if hash_capture_phase not in ("winpe", "oobe"):
        raise ValueError(f"invalid hash_capture_phase: {hash_capture_phase!r}")
    now = _now()
    with _connect(db_path) as conn:
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO task_sequences (
                    name, description, is_default, produces_autopilot_hash,
                    target_os, hash_capture_phase, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    name,
                    description,
                    is_default,
                    produces_autopilot_hash,
                    target_os,
                    hash_capture_phase,
                    now,
                    now,
                ),
            ).fetchone()
            new_id = int(row["id"])
            if is_default:
                conn.execute(
                    "UPDATE task_sequences SET is_default = false WHERE id != %s",
                    (new_id,),
                )
            if steps is not None:
                _set_sequence_steps(conn, new_id, steps)
        return new_id


def update_sequence(
    db_path,
    seq_id: int,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_default: Optional[bool] = None,
    produces_autopilot_hash: Optional[bool] = None,
    target_os: Optional[str] = None,
    hash_capture_phase: Optional[str] = None,
) -> None:
    if hash_capture_phase is not None and hash_capture_phase not in ("winpe", "oobe"):
        raise ValueError(f"invalid hash_capture_phase: {hash_capture_phase!r}")
    updates: list[str] = []
    args: list[Any] = []
    for col, value in (
        ("name", name),
        ("description", description),
        ("is_default", is_default),
        ("produces_autopilot_hash", produces_autopilot_hash),
        ("target_os", target_os),
        ("hash_capture_phase", hash_capture_phase),
    ):
        if value is not None:
            updates.append(f"{col} = %s")
            args.append(value)
    if not updates:
        return
    updates.append("updated_at = %s")
    args.append(_now())
    args.append(seq_id)
    with _connect(db_path) as conn:
        with conn.transaction():
            conn.execute(
                f"UPDATE task_sequences SET {', '.join(updates)} WHERE id = %s",
                args,
            )
            if is_default:
                conn.execute(
                    "UPDATE task_sequences SET is_default = false WHERE id != %s",
                    (seq_id,),
                )


def get_sequence(db_path, seq_id: int) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM task_sequences WHERE id = %s",
            (seq_id,),
        ).fetchone()
        if row is None:
            return None
        seq = _row_to_sequence(row)
        steps = conn.execute(
            """
            SELECT *
            FROM task_sequence_steps
            WHERE sequence_id = %s
            ORDER BY order_index ASC
            """,
            (seq_id,),
        ).fetchall()
    seq["steps"] = [_row_to_step(step) for step in steps]
    return seq


def list_sequences(db_path) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.*,
                (SELECT COUNT(*) FROM task_sequence_steps WHERE sequence_id = s.id)
                    AS step_count
            FROM task_sequences s
            ORDER BY s.name ASC
            """
        ).fetchall()
    return [{**_row_to_sequence(row), "step_count": int(row["step_count"])} for row in rows]


def _set_sequence_steps(conn: Connection, seq_id: int, steps: list[dict]) -> None:
    conn.execute("DELETE FROM task_sequence_steps WHERE sequence_id = %s", (seq_id,))
    for idx, step in enumerate(steps):
        conn.execute(
            """
            INSERT INTO task_sequence_steps
                (sequence_id, order_index, step_type, params_json, enabled)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                seq_id,
                idx,
                step["step_type"],
                _jsonb(step.get("params", {})),
                bool(step.get("enabled", True)),
            ),
        )
    conn.execute(
        "UPDATE task_sequences SET updated_at = %s WHERE id = %s",
        (_now(), seq_id),
    )


def set_sequence_steps(db_path, seq_id: int, steps: list[dict]) -> None:
    with _connect(db_path) as conn:
        with conn.transaction():
            _set_sequence_steps(conn, seq_id, steps)


def delete_sequence(db_path, seq_id: int) -> None:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT vmid FROM vm_provisioning WHERE sequence_id = %s",
            (seq_id,),
        ).fetchall()
        if rows:
            raise SequenceInUse(seq_id, [int(row["vmid"]) for row in rows])
        conn.execute("DELETE FROM task_sequences WHERE id = %s", (seq_id,))


def duplicate_sequence(db_path, seq_id: int, *, new_name: str) -> int:
    seq = get_sequence(db_path, seq_id)
    if seq is None:
        raise ValueError(f"sequence {seq_id} not found")
    return create_sequence(
        db_path,
        name=new_name,
        description=seq["description"],
        is_default=False,
        produces_autopilot_hash=seq["produces_autopilot_hash"],
        target_os=seq.get("target_os", "windows"),
        hash_capture_phase=seq["hash_capture_phase"],
        steps=[
            {"step_type": s["step_type"], "params": s["params"], "enabled": s["enabled"]}
            for s in seq["steps"]
        ],
    )


def get_default_sequence_id(db_path) -> Optional[int]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM task_sequences WHERE is_default = true LIMIT 1"
        ).fetchone()
    return int(row["id"]) if row else None


def record_vm_provisioning(db_path, *, vmid: int, sequence_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO vm_provisioning (vmid, sequence_id, provisioned_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (vmid) DO UPDATE SET
                sequence_id = EXCLUDED.sequence_id,
                provisioned_at = EXCLUDED.provisioned_at
            """,
            (vmid, sequence_id, _now()),
        )


def get_vm_sequence_id(db_path, vmid: int) -> Optional[int]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT sequence_id FROM vm_provisioning WHERE vmid = %s",
            (vmid,),
        ).fetchone()
    return int(row["sequence_id"]) if row and row["sequence_id"] is not None else None


def get_vm_provisioning(db_path, *, vmid: int) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT vmid, sequence_id, provisioned_at FROM vm_provisioning WHERE vmid = %s",
            (vmid,),
        ).fetchone()
    if row is None:
        return None
    return {
        "vmid": int(row["vmid"]),
        "sequence_id": int(row["sequence_id"]) if row["sequence_id"] is not None else None,
        "provisioned_at": _iso(row["provisioned_at"]),
    }


def list_vm_provisioning_vmids(db_path) -> set[int]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT vmid FROM vm_provisioning").fetchall()
    return {int(row["vmid"]) for row in rows}


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


def create_provisioning_run(db_path, *, sequence_id: int, provision_path: str) -> int:
    if provision_path not in ("clone", "winpe"):
        raise ValueError(f"invalid provision_path: {provision_path!r}")
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            INSERT INTO provisioning_runs
                (sequence_id, provision_path, state, started_at)
            VALUES (%s, %s, 'queued', %s)
            RETURNING id
            """,
            (sequence_id, provision_path, _now()),
        ).fetchone()
        return int(row["id"])


def _row_to_run(row: dict | None) -> Optional[dict]:
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "vmid": int(row["vmid"]) if row["vmid"] is not None else None,
        "sequence_id": int(row["sequence_id"]),
        "provision_path": row["provision_path"],
        "state": row["state"],
        "vm_uuid": row["vm_uuid"],
        "started_at": _iso(row["started_at"]),
        "finished_at": _iso(row["finished_at"]),
        "last_error": row["last_error"],
    }


def get_provisioning_run(db_path, run_id: int) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM provisioning_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
    return _row_to_run(row)


def _normalize_uuid(vm_uuid: str) -> str:
    return (vm_uuid or "").strip().lower()


def set_provisioning_run_identity(
    db_path,
    *,
    run_id: int,
    vmid: int,
    vm_uuid: str,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE provisioning_runs
            SET vmid = %s, vm_uuid = %s, state = 'awaiting_winpe'
            WHERE id = %s AND state = 'queued'
            """,
            (vmid, _normalize_uuid(vm_uuid), run_id),
        )


def find_run_by_uuid_state(db_path, *, vm_uuid: str, state: str) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM provisioning_runs
            WHERE vm_uuid = %s AND state = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (_normalize_uuid(vm_uuid), state),
        ).fetchone()
    return _row_to_run(row)


def get_latest_run_state_by_uuid(db_path, *, vm_uuid: str) -> Optional[str]:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT state
            FROM provisioning_runs
            WHERE vm_uuid = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (_normalize_uuid(vm_uuid),),
        ).fetchone()
    return row["state"] if row else None


def update_provisioning_run_state(
    db_path,
    *,
    run_id: int,
    state: str,
    last_error: Optional[str] = None,
) -> None:
    with _connect(db_path) as conn:
        if state in ("done", "failed"):
            conn.execute(
                """
                UPDATE provisioning_runs
                SET state = %s, last_error = %s, finished_at = %s
                WHERE id = %s
                """,
                (state, last_error, _now(), run_id),
            )
        else:
            conn.execute(
                """
                UPDATE provisioning_runs
                SET state = %s, last_error = %s
                WHERE id = %s
                """,
                (state, last_error, run_id),
            )


def _row_to_run_step(row: dict | None) -> Optional[dict]:
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "run_id": int(row["run_id"]),
        "order_index": int(row["order_index"]),
        "phase": row["phase"],
        "kind": row["kind"],
        "params_json": _json_text(row["params_json"]),
        "state": row["state"],
        "started_at": _iso(row["started_at"]),
        "finished_at": _iso(row["finished_at"]),
        "error": row["error"],
    }


def append_run_step(db_path, *, run_id: int, phase: str, kind: str, params: dict) -> dict:
    with _connect(db_path) as conn:
        with conn.transaction():
            nxt = conn.execute(
                """
                SELECT COALESCE(MAX(order_index) + 1, 0) AS next_order
                FROM provisioning_run_steps
                WHERE run_id = %s
                """,
                (run_id,),
            ).fetchone()["next_order"]
            row = conn.execute(
                """
                INSERT INTO provisioning_run_steps
                    (run_id, order_index, phase, kind, params_json, state)
                VALUES (%s, %s, %s, %s, %s, 'pending')
                RETURNING *
                """,
                (run_id, int(nxt), phase, kind, _jsonb(params)),
            ).fetchone()
            return _row_to_run_step(row)


def update_run_step_state(
    db_path,
    *,
    step_id: int,
    state: str,
    error: Optional[str] = None,
) -> None:
    with _connect(db_path) as conn:
        if state == "running":
            conn.execute(
                """
                UPDATE provisioning_run_steps
                SET state = 'running',
                    started_at = COALESCE(started_at, %s),
                    finished_at = NULL,
                    error = NULL
                WHERE id = %s
                """,
                (_now(), step_id),
            )
        elif state in ("ok", "error"):
            conn.execute(
                """
                UPDATE provisioning_run_steps
                SET state = %s, finished_at = %s, error = %s
                WHERE id = %s
                """,
                (state, _now(), error, step_id),
            )
        else:
            raise ValueError(f"invalid step state: {state!r}")


def list_run_steps(db_path, run_id: int) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM provisioning_run_steps
            WHERE run_id = %s
            ORDER BY order_index
            """,
            (run_id,),
        ).fetchall()
    return [_row_to_run_step(row) for row in rows]


def reorder_run_phase_steps(
    db_path,
    *,
    run_id: int,
    phase: str,
    ordered_kinds: list[str],
) -> None:
    order = {kind: idx for idx, kind in enumerate(ordered_kinds)}
    with _connect(db_path) as conn:
        with conn.transaction():
            rows = conn.execute(
                """
                SELECT id, kind, order_index
                FROM provisioning_run_steps
                WHERE run_id = %s AND phase = %s
                ORDER BY order_index
                """,
                (run_id, phase),
            ).fetchall()
            if not rows:
                return
            base = min(int(row["order_index"]) for row in rows)
            sorted_rows = sorted(
                rows,
                key=lambda row: (order.get(row["kind"], len(order)), int(row["order_index"])),
            )
            for idx, row in enumerate(sorted_rows):
                conn.execute(
                    "UPDATE provisioning_run_steps SET order_index = %s WHERE id = %s",
                    (-(idx + 1), row["id"]),
                )
            for idx, row in enumerate(sorted_rows):
                conn.execute(
                    "UPDATE provisioning_run_steps SET order_index = %s WHERE id = %s",
                    (base + idx, row["id"]),
                )


def get_run_step(db_path, step_id: int) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM provisioning_run_steps WHERE id = %s",
            (step_id,),
        ).fetchone()
    return _row_to_run_step(row)


def sweep_stale_runs(db_path, *, ttl_seconds: int = 1800) -> int:
    cutoff = _now() - timedelta(seconds=ttl_seconds)
    flipped = 0
    with _connect(db_path) as conn:
        with conn.transaction():
            rows = conn.execute(
                """
                SELECT r.id AS run_id, MAX(s.started_at) AS last_started
                FROM provisioning_runs r
                JOIN provisioning_run_steps s ON s.run_id = r.id
                WHERE r.state IN (
                    'awaiting_winpe','awaiting_windows_setup',
                    'awaiting_osd_client','awaiting_specialize'
                )
                  AND s.state = 'running'
                GROUP BY r.id
                HAVING MAX(s.started_at) IS NOT NULL AND MAX(s.started_at) < %s
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE provisioning_runs
                    SET state = 'failed', last_error = %s, finished_at = %s
                    WHERE id = %s
                      AND state IN (
                        'awaiting_winpe','awaiting_windows_setup',
                        'awaiting_osd_client','awaiting_specialize'
                      )
                    """,
                    (
                        f"stale; no step update for >{ttl_seconds // 60} min",
                        _now(),
                        row["run_id"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE provisioning_run_steps
                    SET state = 'error', finished_at = %s, error = 'stale: agent silent'
                    WHERE run_id = %s AND state = 'running'
                    """,
                    (_now(), row["run_id"]),
                )
                flipped += 1
    return flipped


def _row_to_answer_cache(row: dict | None) -> Optional[dict]:
    if row is None:
        return None
    return {
        "hash": row["hash"],
        "short_hash": row["short_hash"],
        "volid": row["volid"],
        "compiled_at": _iso(row["compiled_at"]),
        "last_used_at": _iso(row["last_used_at"]),
    }


def get_answer_iso_cache(db_path, full_hash: str) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM answer_iso_cache WHERE hash = %s",
            (full_hash,),
        ).fetchone()
    return _row_to_answer_cache(row)


def insert_answer_iso_cache(
    db_path,
    *,
    full_hash: str,
    short_hash: str,
    volid: str,
) -> None:
    now = _now()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO answer_iso_cache
                (hash, short_hash, volid, compiled_at, last_used_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (full_hash, short_hash, volid, now, now),
        )


def touch_answer_iso_cache(db_path, full_hash: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE answer_iso_cache SET last_used_at = %s WHERE hash = %s",
            (_now(), full_hash),
        )


def delete_answer_iso_cache(db_path, full_hash: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM answer_iso_cache WHERE hash = %s", (full_hash,))


def list_answer_iso_cache(db_path, *, in_use_volids: set[str]) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM answer_iso_cache ORDER BY compiled_at DESC"
        ).fetchall()
    out = []
    for row in rows:
        item = _row_to_answer_cache(row)
        item["in_use"] = item["volid"] in in_use_volids
        out.append(item)
    return out
