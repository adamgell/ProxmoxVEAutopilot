"""Content-addressed per-VM answer floppies.

Why floppy, not CD: Windows Setup's answer-file search order on a
sysprep-d clone boot puts removable read/write media (floppy/USB,
position 4) above removable read-only media (CD, position 5). When
the template's ``%WINDIR%\\Panther\\unattend.xml`` is cached and
annotated "already processed for pass X," Windows falls through the
precedence chain to lower-precedence answer files — but position 4
is the last reliable stop before Windows gives up. The CD at
position 5 is routinely ignored when sysprep's online specialize is
running. flexVDI deploys Windows clones the same way for the same
reason. See
https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/windows-setup-automation-overview

Each compiled ``autounattend.xml`` is hashed; the first 16 hex of the
digest appear in the filename ``autopilot-unattend-<short>.img`` on
the Proxmox host (``/var/lib/vz/snippets/``). Two VMs whose compiled
unattend matches bit-for-bit share one floppy image.

The module SSHes to the Proxmox host to build and stash the image.
mkfs.fat + mcopy live on the host. The caller supplies the SSH
runner so unit tests can mock it.
"""
from __future__ import annotations

import base64
import hashlib
import shlex
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional


SNIPPETS_DIR = "/var/lib/vz/snippets"
_FILENAME_PREFIX = "autopilot-unattend-"
_FILENAME_SUFFIX = ".img"


def compute_hash(unattend_bytes: bytes) -> str:
    return hashlib.sha256(unattend_bytes).hexdigest()


def short_hash(full_hash: str) -> str:
    return full_hash[:16]


def floppy_path(short: str) -> str:
    return f"{SNIPPETS_DIR}/{_FILENAME_PREFIX}{short}{_FILENAME_SUFFIX}"


def qemu_args_token(path: str) -> str:
    """Return the QEMU args fragment that attaches ``path`` as virtual
    floppy A:. Windows sees it as removable R/W, position 4 in the
    answer-file search order."""
    return f"-drive if=floppy,format=raw,file={path}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DB bookkeeping — shares the answer_iso_cache table (same semantics:
# content-addressed per-VM answer files). Schema in sequences_db.SCHEMA.
# ---------------------------------------------------------------------------


def _lookup(db_path: Path, full_hash: str) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM answer_iso_cache WHERE hash = ?", (full_hash,),
        ).fetchone()
    return dict(row) if row else None


def _insert(db_path: Path, *, full_hash: str, short: str, volid: str) -> None:
    with _connect(db_path) as conn:
        now = _now()
        conn.execute(
            "INSERT INTO answer_iso_cache "
            "(hash, short_hash, volid, compiled_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (full_hash, short, volid, now, now),
        )


def _touch(db_path: Path, full_hash: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE answer_iso_cache SET last_used_at = ? WHERE hash = ?",
            (_now(), full_hash),
        )


def _delete_row(db_path: Path, full_hash: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM answer_iso_cache WHERE hash = ?", (full_hash,),
        )


def list_cache(db_path: Path, *, in_use_volids: set[str]) -> list[dict]:
    with _connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM answer_iso_cache ORDER BY compiled_at DESC",
        )]
    for r in rows:
        r["in_use"] = r["volid"] in in_use_volids
    return rows


# ---------------------------------------------------------------------------
# SSH-driven build on the Proxmox host
# ---------------------------------------------------------------------------


SshRunner = Callable[[str], tuple[int, bytes, bytes]]
"""Run a single shell command on the Proxmox host. Returns
(exit_code, stdout, stderr). The command must be fully self-
contained (no stdin piping) — callers pass unattend bytes inline
via base64 encoding in the command string."""


def make_sshpass_runner(*, host: str, password: str,
                        user: str = "root") -> SshRunner:
    """Concrete runner that SSHes with sshpass + password."""
    def run(cmd: str) -> tuple[int, bytes, bytes]:
        argv = [
            "sshpass", "-e", "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            f"{user}@{host}", cmd,
        ]
        p = subprocess.run(
            argv,
            env={
                "SSHPASS": password,
                "PATH": "/usr/bin:/bin:/usr/local/bin",
            },
            capture_output=True, timeout=120,
        )
        return p.returncode, p.stdout, p.stderr
    return run


def _remote_file_exists(ssh: SshRunner, path: str) -> bool:
    ec, _, _ = ssh(f"test -f {shlex.quote(path)}")
    return ec == 0


def _build_remote_script(unattend_bytes: bytes, target_path: str) -> str:
    """Return a self-contained shell command that, when executed on
    the Proxmox host, creates a 1.44 MB FAT12 floppy at ``target_path``
    with ``unattend_bytes`` written to ``Autounattend.xml`` at its root.
    The command has no stdin dependency — the unattend payload is
    embedded as base64.
    """
    b64 = base64.b64encode(unattend_bytes).decode("ascii")
    t = shlex.quote(target_path)
    # Wrap in sh -c '...' so a single remote shell invocation runs the
    # whole pipeline. Using single quotes means $VAR expansion runs on
    # the Proxmox host, not in Python.
    return (
        "sh -c '"
        "set -eu; "
        f"mkdir -p {shlex.quote(SNIPPETS_DIR)}; "
        "TMP=$(mktemp -d); "
        'trap "rm -rf $TMP" EXIT; '
        f"echo {b64} | base64 -d > $TMP/unattend.xml; "
        "dd if=/dev/zero of=$TMP/fd.img bs=512 count=2880 status=none; "
        "mkfs.fat -F 12 -n OEMDRV $TMP/fd.img >/dev/null; "
        "mcopy -i $TMP/fd.img $TMP/unattend.xml ::Autounattend.xml; "
        f"mv $TMP/fd.img {t}; "
        f"chmod 644 {t}"
        "'"
    )


def ensure_floppy(*, db_path: Path, unattend_bytes: bytes,
                  ssh: SshRunner) -> str:
    """Build-if-missing. Return the on-host path of the per-VM answer
    floppy ready to attach via QEMU args."""
    full_hash = compute_hash(unattend_bytes)
    short = short_hash(full_hash)
    target = floppy_path(short)

    row = _lookup(db_path, full_hash)
    if row is not None:
        if row["volid"] == target and _remote_file_exists(ssh, target):
            _touch(db_path, full_hash)
            return target
        # Row is stale — either the on-host file is gone, or the volid
        # points at a legacy ISO path (old cache format). Either way,
        # drop the row so the INSERT below doesn't hit the UNIQUE
        # constraint on short_hash.
        _delete_row(db_path, full_hash)

    cmd = _build_remote_script(unattend_bytes, target)
    ec, out, err = ssh(cmd)
    if ec != 0:
        raise RuntimeError(
            f"floppy build on Proxmox host failed (exit {ec}): "
            f"stderr={err!r}"
        )
    _insert(db_path, full_hash=full_hash, short=short, volid=target)
    return target


def prune(*, db_path: Path, hashes_to_delete: list[str],
          ssh: SshRunner) -> list[str]:
    """Delete specified cache rows + the on-host .img files. Missing
    rows/files are silently skipped."""
    removed: list[str] = []
    for full_hash in hashes_to_delete:
        row = _lookup(db_path, full_hash)
        if not row:
            continue
        path = row["volid"]
        ssh(f"rm -f {shlex.quote(path)}")
        _delete_row(db_path, full_hash)
        removed.append(full_hash)
    return removed
