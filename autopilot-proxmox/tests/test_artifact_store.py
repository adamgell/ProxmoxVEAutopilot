import hashlib
from pathlib import Path

import pytest

from web.artifact_sidecar import ArtifactKind, Sidecar
from web.artifact_store import ArtifactStore


def _make_blob(path: Path, content: bytes) -> tuple[Path, str]:
    path.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    return path, sha


def _make_sidecar(sha: str, size: int, kind: ArtifactKind = ArtifactKind.INSTALL_WIM, **extra) -> Sidecar:
    return Sidecar(kind=kind, sha256=sha, size=size, metadata=extra)


def test_init_creates_directories(tmp_path):
    store = ArtifactStore(tmp_path)
    assert (tmp_path / "store").is_dir()
    assert (tmp_path / "cache").is_dir()
    assert (tmp_path / "index.db").exists()


def test_register_copies_into_store_and_indexes(tmp_path):
    store = ArtifactStore(tmp_path)
    src = tmp_path / "src.wim"
    src, sha = _make_blob(src, b"hello world\n")
    sidecar = _make_sidecar(sha, len(b"hello world\n"), buildHost="x")

    record = store.register(src, sidecar, extension="wim")

    expected_path = tmp_path / "store" / f"{sha}.wim"
    assert expected_path.exists()
    assert record.sha256 == sha
    assert record.relative_path == f"store/{sha}.wim"


def test_register_is_idempotent(tmp_path):
    store = ArtifactStore(tmp_path)
    src = tmp_path / "src.wim"
    src, sha = _make_blob(src, b"abc")
    sidecar = _make_sidecar(sha, 3)
    store.register(src, sidecar, extension="wim")
    store.register(src, sidecar, extension="wim")  # second call is a no-op
    rows = store.list_artifacts()
    assert len(rows) == 1


def test_register_rejects_sha_mismatch(tmp_path):
    store = ArtifactStore(tmp_path)
    src, sha = _make_blob(tmp_path / "src.wim", b"abc")
    bad_sidecar = _make_sidecar("0" * 64, 3)
    with pytest.raises(ValueError, match="sha256 mismatch"):
        store.register(src, bad_sidecar, extension="wim")


def test_register_rejects_size_mismatch(tmp_path):
    store = ArtifactStore(tmp_path)
    src, sha = _make_blob(tmp_path / "src.wim", b"abc")
    bad_sidecar = _make_sidecar(sha, 999)
    with pytest.raises(ValueError, match="size mismatch"):
        store.register(src, bad_sidecar, extension="wim")


def test_lookup_returns_record(tmp_path):
    store = ArtifactStore(tmp_path)
    src, sha = _make_blob(tmp_path / "src.wim", b"abc")
    sidecar = _make_sidecar(sha, 3, buildHost="b")
    store.register(src, sidecar, extension="wim")

    rec = store.lookup(sha)
    assert rec is not None
    assert rec.sha256 == sha
    assert rec.metadata["buildHost"] == "b"


def test_lookup_returns_none_for_unknown(tmp_path):
    store = ArtifactStore(tmp_path)
    assert store.lookup("0" * 64) is None
