"""Tests for web.answer_iso_cache — content-addressed per-VM ISOs."""
import pytest
from pathlib import Path


@pytest.fixture
def db_path(tmp_path):
    from web import sequences_db
    p = tmp_path / "cache.db"
    sequences_db.init(p)
    return p


def test_compute_hash_is_stable_for_same_bytes():
    from web import answer_iso_cache
    h1 = answer_iso_cache.compute_hash(b"<unattend>foo</unattend>")
    h2 = answer_iso_cache.compute_hash(b"<unattend>foo</unattend>")
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_compute_hash_changes_with_content():
    from web import answer_iso_cache
    h1 = answer_iso_cache.compute_hash(b"<unattend>foo</unattend>")
    h2 = answer_iso_cache.compute_hash(b"<unattend>bar</unattend>")
    assert h1 != h2


def test_short_hash_is_first_16_hex_chars():
    from web import answer_iso_cache
    full = "0" * 16 + "a" * 16 + "b" * 32
    assert answer_iso_cache.short_hash(full) == "0" * 16


def _fake_config():
    return {
        "proxmox_host": "10.0.0.1", "proxmox_port": 8006,
        "proxmox_node": "pve", "proxmox_iso_storage": "isos",
        "vault_proxmox_api_token_id": "user!tok",
        "vault_proxmox_api_token_secret": "secret",
        "proxmox_validate_certs": False,
    }


def test_ensure_iso_builds_and_records_on_cache_miss(db_path, monkeypatch):
    from web import answer_iso_cache

    built_bytes: list[bytes] = []
    uploaded_to: list[tuple] = []

    def fake_build(xml_bytes, out_iso):
        built_bytes.append(xml_bytes)
        out_iso.write_bytes(b"FAKE-ISO-PAYLOAD")

    def fake_upload(*, proxmox_config, node, iso_storage,
                    iso_path, iso_filename):
        uploaded_to.append((node, iso_storage, iso_filename,
                            iso_path.read_bytes()))

    def fake_api_get(path):
        # Cache-miss path must not depend on the listing call succeeding.
        return []

    monkeypatch.setattr(answer_iso_cache, "_build_iso", fake_build)
    monkeypatch.setattr(answer_iso_cache, "_upload", fake_upload)

    volid = answer_iso_cache.ensure_iso(
        db_path=db_path,
        unattend_bytes=b"<unattend>hello</unattend>",
        proxmox_config=_fake_config(),
        proxmox_api_get=fake_api_get,
    )

    # Returned volid uses the short hash + default storage.
    import hashlib
    short = hashlib.sha256(b"<unattend>hello</unattend>").hexdigest()[:16]
    assert volid == f"isos:iso/autopilot-unattend-{short}.iso"

    # Build + upload each happened exactly once.
    assert len(built_bytes) == 1
    assert built_bytes[0] == b"<unattend>hello</unattend>"
    assert len(uploaded_to) == 1
    assert uploaded_to[0][0] == "pve"
    assert uploaded_to[0][1] == "isos"
    assert uploaded_to[0][2] == f"autopilot-unattend-{short}.iso"

    # Row is recorded.
    rows = answer_iso_cache.list_cache(db_path, in_use_volids=set())
    assert len(rows) == 1
    assert rows[0]["short_hash"] == short
    assert rows[0]["volid"] == volid
    assert rows[0]["in_use"] is False


def test_ensure_iso_reuses_when_storage_still_has_the_file(db_path, monkeypatch):
    """Cache hit + storage still has the file → no rebuild, no upload."""
    from web import answer_iso_cache

    build_calls: list = []
    upload_calls: list = []

    monkeypatch.setattr(answer_iso_cache, "_build_iso",
                        lambda *a, **k: build_calls.append(a))
    monkeypatch.setattr(answer_iso_cache, "_upload",
                        lambda **k: upload_calls.append(k))

    def api_with_content(path):
        # Return the expected volid as already-present.
        return [{"volid": "isos:iso/autopilot-unattend-"
                          + "abc123" * 2 + "abcd.iso", "content": "iso"}]

    # First call: seed the cache with a deterministic hash.
    import hashlib, io
    fake_bytes = b"sentinel"
    full_h = hashlib.sha256(fake_bytes).hexdigest()
    short_h = full_h[:16]
    expected_volid = f"isos:iso/autopilot-unattend-{short_h}.iso"

    # Manually seed the table so we don't depend on a prior ensure_iso.
    answer_iso_cache._insert(
        db_path, full_hash=full_h, short=short_h, volid=expected_volid,
    )

    def api_returning_expected(path):
        return [{"volid": expected_volid, "content": "iso"}]

    volid = answer_iso_cache.ensure_iso(
        db_path=db_path,
        unattend_bytes=fake_bytes,
        proxmox_config=_fake_config(),
        proxmox_api_get=api_returning_expected,
    )
    assert volid == expected_volid
    assert build_calls == []  # no rebuild
    assert upload_calls == []  # no upload


def test_ensure_iso_rebuilds_when_storage_drifted(db_path, monkeypatch):
    """Cache has a row but the ISO is gone from storage → rebuild."""
    from web import answer_iso_cache

    monkeypatch.setattr(answer_iso_cache, "_build_iso",
                        lambda b, p: p.write_bytes(b"ISO"))
    uploaded = []
    monkeypatch.setattr(answer_iso_cache, "_upload",
                        lambda **k: uploaded.append(k))

    import hashlib
    fake_bytes = b"bytes"
    full_h = hashlib.sha256(fake_bytes).hexdigest()
    answer_iso_cache._insert(
        db_path, full_hash=full_h, short=full_h[:16],
        volid=f"isos:iso/autopilot-unattend-{full_h[:16]}.iso",
    )

    # API reports the storage is EMPTY — the row is a ghost.
    answer_iso_cache.ensure_iso(
        db_path=db_path, unattend_bytes=fake_bytes,
        proxmox_config=_fake_config(),
        proxmox_api_get=lambda path: [],
    )
    assert len(uploaded) == 1
    rows = answer_iso_cache.list_cache(db_path, in_use_volids=set())
    assert len(rows) == 1  # one active row, the ghost was replaced


def test_list_cache_marks_in_use(db_path):
    from web import answer_iso_cache
    answer_iso_cache._insert(
        db_path, full_hash="a" * 64, short="a" * 16,
        volid="isos:iso/autopilot-unattend-aaaaaaaaaaaaaaaa.iso",
    )
    answer_iso_cache._insert(
        db_path, full_hash="b" * 64, short="b" * 16,
        volid="isos:iso/autopilot-unattend-bbbbbbbbbbbbbbbb.iso",
    )
    rows = answer_iso_cache.list_cache(
        db_path,
        in_use_volids={"isos:iso/autopilot-unattend-aaaaaaaaaaaaaaaa.iso"},
    )
    by_hash = {r["hash"]: r for r in rows}
    assert by_hash["a" * 64]["in_use"] is True
    assert by_hash["b" * 64]["in_use"] is False


def test_prune_deletes_rows_and_calls_api(db_path):
    from web import answer_iso_cache
    answer_iso_cache._insert(
        db_path, full_hash="a" * 64, short="a" * 16,
        volid="isos:iso/autopilot-unattend-aaaaaaaaaaaaaaaa.iso",
    )
    answer_iso_cache._insert(
        db_path, full_hash="b" * 64, short="b" * 16,
        volid="isos:iso/autopilot-unattend-bbbbbbbbbbbbbbbb.iso",
    )

    deletes: list[str] = []
    def fake_delete(path):
        deletes.append(path)

    removed = answer_iso_cache.prune(
        db_path=db_path, hashes_to_delete=["a" * 64],
        proxmox_config=_fake_config(),
        proxmox_api_delete=fake_delete,
    )
    assert removed == ["a" * 64]
    assert len(deletes) == 1
    assert "autopilot-unattend-aaaaaaaaaaaaaaaa.iso" in deletes[0]

    rows = answer_iso_cache.list_cache(db_path, in_use_volids=set())
    assert len(rows) == 1
    assert rows[0]["hash"] == "b" * 64


def test_prune_silent_on_storage_failure(db_path):
    """A Proxmox DELETE that raises shouldn't leave a dangling row."""
    from web import answer_iso_cache
    answer_iso_cache._insert(
        db_path, full_hash="c" * 64, short="c" * 16,
        volid="isos:iso/autopilot-unattend-cccccccccccccccc.iso",
    )
    def failing_delete(path):
        raise RuntimeError("storage unreachable")
    removed = answer_iso_cache.prune(
        db_path=db_path, hashes_to_delete=["c" * 64],
        proxmox_config=_fake_config(),
        proxmox_api_delete=failing_delete,
    )
    assert removed == ["c" * 64]
    rows = answer_iso_cache.list_cache(db_path, in_use_volids=set())
    assert rows == []


def test_prune_skips_unknown_hashes(db_path):
    from web import answer_iso_cache
    removed = answer_iso_cache.prune(
        db_path=db_path, hashes_to_delete=["nonexistent"],
        proxmox_config=_fake_config(),
        proxmox_api_delete=lambda p: None,
    )
    assert removed == []
