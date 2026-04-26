"""Content-addressed artifact storage with a sqlite index.

Layout:
    <root>/
        store/<sha256>.<ext>     # registered build artifacts (permanent)
        cache/<sha256>.<ext>     # orchestrator-rendered per-VM blobs (LRU; not used in this plan)
        index.db                 # sqlite index

Schema:
    CREATE TABLE artifacts (
        sha256          TEXT PRIMARY KEY,
        kind            TEXT NOT NULL,
        size            INTEGER NOT NULL,
        relative_path   TEXT NOT NULL,
        metadata_json   TEXT NOT NULL,
        registered_at   TEXT NOT NULL,
        last_served_at  TEXT
    );
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from web.artifact_sidecar import ArtifactKind, Sidecar


@dataclass(frozen=True)
class ArtifactRecord:
    sha256: str
    kind: ArtifactKind
    size: int
    relative_path: str
    metadata: dict
    registered_at: str
    last_served_at: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    sha256          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    size            INTEGER NOT NULL,
    relative_path   TEXT NOT NULL,
    metadata_json   TEXT NOT NULL,
    registered_at   TEXT NOT NULL,
    last_served_at  TEXT
);
"""


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.store_dir = self.root / "store"
        self.cache_dir = self.root / "cache"
        self.db_path = self.root / "index.db"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    def register(self, src_path: Path, sidecar: Sidecar, *, extension: str) -> ArtifactRecord:
        """Verify, copy into the store, and index. Idempotent for matching sha."""
        src_path = Path(src_path)
        actual_size = src_path.stat().st_size
        if actual_size != sidecar.size:
            raise ValueError(f"size mismatch: sidecar={sidecar.size} actual={actual_size}")
        actual_sha = self._sha256(src_path)
        if actual_sha != sidecar.sha256:
            raise ValueError(f"sha256 mismatch: sidecar={sidecar.sha256} actual={actual_sha}")

        existing = self.lookup(actual_sha)
        if existing is not None:
            expected_path = self.root / existing.relative_path
            if expected_path.exists():
                return existing
            # File missing on disk — re-copy from src_path to restore it.
            shutil.copy2(src_path, expected_path)
            return existing

        rel = f"store/{actual_sha}.{extension.lstrip('.')}"
        dest = self.root / rel
        if not dest.exists():
            shutil.copy2(src_path, dest)

        registered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO artifacts (sha256, kind, size, relative_path, metadata_json, registered_at, last_served_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (sidecar.sha256, sidecar.kind.value, sidecar.size, rel,
                 json.dumps(sidecar.metadata), registered_at),
            )
        return ArtifactRecord(
            sha256=sidecar.sha256,
            kind=sidecar.kind,
            size=sidecar.size,
            relative_path=rel,
            metadata=sidecar.metadata,
            registered_at=registered_at,
            last_served_at=None,
        )

    def cache_blob(self, content: bytes, *, kind: ArtifactKind, extension: str) -> ArtifactRecord:
        """Stash an orchestrator-rendered per-VM blob in cache/, indexed alongside
        registered build artifacts so /winpe/content/<sha> serves both uniformly.

        Idempotent on content sha — a second call with identical bytes returns
        the existing record without rewriting the file.
        """
        import hashlib
        sha = hashlib.sha256(content).hexdigest()

        existing = self.lookup(sha)
        if existing is not None:
            # File on disk may also exist — preserve it; otherwise re-write.
            expected_path = self.root / existing.relative_path
            if not expected_path.exists():
                expected_path.parent.mkdir(parents=True, exist_ok=True)
                expected_path.write_bytes(content)
            return existing

        rel = f"cache/{sha}.{extension.lstrip('.')}"
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

        registered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO artifacts (sha256, kind, size, relative_path, metadata_json, registered_at, last_served_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (sha, kind.value, len(content), rel, json.dumps({"source": "cache_blob"}), registered_at),
            )
        return ArtifactRecord(
            sha256=sha,
            kind=kind,
            size=len(content),
            relative_path=rel,
            metadata={"source": "cache_blob"},
            registered_at=registered_at,
            last_served_at=None,
        )

    def lookup(self, sha256: str) -> ArtifactRecord | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT sha256, kind, size, relative_path, metadata_json, registered_at, last_served_at "
                "FROM artifacts WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
        if row is None:
            return None
        return ArtifactRecord(
            sha256=row[0],
            kind=ArtifactKind(row[1]),
            size=row[2],
            relative_path=row[3],
            metadata=json.loads(row[4]),
            registered_at=row[5],
            last_served_at=row[6],
        )

    def list_artifacts(self) -> list[ArtifactRecord]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT sha256, kind, size, relative_path, metadata_json, registered_at, last_served_at "
                "FROM artifacts ORDER BY registered_at DESC"
            ).fetchall()
        return [
            ArtifactRecord(
                sha256=r[0],
                kind=ArtifactKind(r[1]),
                size=r[2],
                relative_path=r[3],
                metadata=json.loads(r[4]),
                registered_at=r[5],
                last_served_at=r[6],
            )
            for r in rows
        ]

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
