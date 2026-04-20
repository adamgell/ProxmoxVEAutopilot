"""Tests for web.answer_floppy_cache — content-addressed per-VM floppies."""
import pytest
from pathlib import Path


@pytest.fixture
def db_path(tmp_path):
    from web import sequences_db
    p = tmp_path / "cache.db"
    sequences_db.init(p)
    return p


def test_compute_hash_is_stable():
    from web import answer_floppy_cache
    h1 = answer_floppy_cache.compute_hash(b"<unattend>foo</unattend>")
    h2 = answer_floppy_cache.compute_hash(b"<unattend>foo</unattend>")
    assert h1 == h2
    assert len(h1) == 64


def test_compute_hash_differs_for_different_bytes():
    from web import answer_floppy_cache
    assert (answer_floppy_cache.compute_hash(b"a") !=
            answer_floppy_cache.compute_hash(b"b"))


def test_floppy_path_format():
    from web import answer_floppy_cache
    assert answer_floppy_cache.floppy_path("abc123def456abcd") == \
        "/var/lib/vz/snippets/autopilot-unattend-abc123def456abcd.img"


def test_qemu_args_token_shape():
    """The token must be attachable via QEMU's -drive if=floppy. The
    comma-separated option string MUST be single-quoted, because PVE's
    split_args parser otherwise treats commas as option boundaries and
    silently drops everything after the first comma."""
    from web import answer_floppy_cache
    out = answer_floppy_cache.qemu_args_token(
        "/var/lib/vz/snippets/autopilot-unattend-deadbeefdeadbeef.img",
    )
    assert out.startswith("-drive '")
    assert out.endswith("'")
    assert "if=floppy" in out and "format=raw" in out
    assert ("file=/var/lib/vz/snippets/"
            "autopilot-unattend-deadbeefdeadbeef.img") in out


def test_ensure_floppy_builds_on_cache_miss(db_path):
    from web import answer_floppy_cache
    calls = []

    def fake_ssh(cmd):
        calls.append(cmd)
        if cmd.startswith("test -f "):
            return (1, b"", b"")  # file absent
        return (0, b"built", b"")  # build succeeds

    path = answer_floppy_cache.ensure_floppy(
        db_path=db_path,
        unattend_bytes=b"<unattend>hello</unattend>",
        ssh=fake_ssh,
    )
    import hashlib
    short = hashlib.sha256(b"<unattend>hello</unattend>").hexdigest()[:16]
    assert path == f"/var/lib/vz/snippets/autopilot-unattend-{short}.img"

    # One test-f + one build command — that's it on a cache miss with
    # an empty DB (no pre-existing row → no file-exists check).
    assert len(calls) == 1  # just the build; no pre-check when DB row absent
    assert "mkfs.fat" in calls[0]
    assert "mcopy" in calls[0]
    assert "OEMDRV" in calls[0]

    # Row recorded.
    rows = answer_floppy_cache.list_cache(db_path, in_use_volids=set())
    assert len(rows) == 1
    assert rows[0]["volid"] == path


def test_ensure_floppy_short_circuits_on_cache_hit(db_path, monkeypatch):
    from web import answer_floppy_cache
    import hashlib
    payload = b"cached"
    full = hashlib.sha256(payload).hexdigest()
    short = full[:16]
    path = f"/var/lib/vz/snippets/autopilot-unattend-{short}.img"

    # Seed the cache row as if a previous provision built this floppy.
    answer_floppy_cache._insert(
        db_path, full_hash=full, short=short, volid=path,
    )

    ssh_calls = []
    def fake_ssh(cmd):
        ssh_calls.append(cmd)
        # The on-host file exists check returns 0 (present).
        if cmd.startswith("test -f "):
            return (0, b"", b"")
        # If a build command fires, the test fails — cache hit means no build.
        raise AssertionError(f"unexpected ssh call: {cmd}")

    out = answer_floppy_cache.ensure_floppy(
        db_path=db_path, unattend_bytes=payload, ssh=fake_ssh,
    )
    assert out == path
    assert len(ssh_calls) == 1  # just the test -f


def test_ensure_floppy_rebuilds_when_remote_file_disappeared(db_path):
    """If the DB has a row but the .img is gone from /var/lib/vz/snippets,
    rebuild and re-record. Don't PK-collide on the existing row."""
    from web import answer_floppy_cache
    import hashlib
    payload = b"drifted"
    full = hashlib.sha256(payload).hexdigest()
    short = full[:16]
    path = f"/var/lib/vz/snippets/autopilot-unattend-{short}.img"
    answer_floppy_cache._insert(
        db_path, full_hash=full, short=short, volid=path,
    )

    def fake_ssh(cmd):
        if cmd.startswith("test -f "):
            return (1, b"", b"")  # file missing
        return (0, b"", b"")  # build succeeds

    answer_floppy_cache.ensure_floppy(
        db_path=db_path, unattend_bytes=payload, ssh=fake_ssh,
    )
    # Row is re-inserted (row count still 1, not 2).
    rows = answer_floppy_cache.list_cache(db_path, in_use_volids=set())
    assert len(rows) == 1


def test_ensure_floppy_raises_on_build_failure(db_path):
    from web import answer_floppy_cache

    def fake_ssh(cmd):
        if cmd.startswith("test -f "):
            return (1, b"", b"")
        return (2, b"", b"mkfs.fat: permission denied")

    with pytest.raises(RuntimeError) as exc:
        answer_floppy_cache.ensure_floppy(
            db_path=db_path, unattend_bytes=b"x", ssh=fake_ssh,
        )
    assert "exit 2" in str(exc.value)


def test_list_cache_marks_in_use(db_path):
    from web import answer_floppy_cache
    answer_floppy_cache._insert(
        db_path, full_hash="a" * 64, short="a" * 16,
        volid="/var/lib/vz/snippets/autopilot-unattend-aaaaaaaaaaaaaaaa.img",
    )
    answer_floppy_cache._insert(
        db_path, full_hash="b" * 64, short="b" * 16,
        volid="/var/lib/vz/snippets/autopilot-unattend-bbbbbbbbbbbbbbbb.img",
    )
    rows = answer_floppy_cache.list_cache(
        db_path,
        in_use_volids={"/var/lib/vz/snippets/autopilot-unattend-aaaaaaaaaaaaaaaa.img"},
    )
    by_hash = {r["hash"]: r for r in rows}
    assert by_hash["a" * 64]["in_use"] is True
    assert by_hash["b" * 64]["in_use"] is False


def test_prune_removes_row_and_invokes_ssh_rm(db_path):
    from web import answer_floppy_cache
    answer_floppy_cache._insert(
        db_path, full_hash="c" * 64, short="c" * 16,
        volid="/var/lib/vz/snippets/autopilot-unattend-cccccccccccccccc.img",
    )
    ssh_calls = []
    def fake_ssh(cmd):
        ssh_calls.append(cmd)
        return (0, b"", b"")

    removed = answer_floppy_cache.prune(
        db_path=db_path, hashes_to_delete=["c" * 64], ssh=fake_ssh,
    )
    assert removed == ["c" * 64]
    # The rm command includes the volid we registered.
    assert any("rm -f" in c and "cccccccccccccccc" in c for c in ssh_calls)
    assert answer_floppy_cache.list_cache(db_path, in_use_volids=set()) == []


def test_build_remote_script_escapes_target_path():
    """shlex.quote shields the target path from shell injection when the
    caller is ever tempted to put a weird char in SNIPPETS_DIR."""
    from web import answer_floppy_cache
    script = answer_floppy_cache._build_remote_script(
        b"some xml", "/var/lib/vz/snippets/autopilot-unattend-abc.img",
    )
    # Path appears quoted and the xml is embedded as base64.
    import base64
    assert base64.b64encode(b"some xml").decode() in script
    # Script is a single-quoted sh -c so the outer shell doesn't
    # interpret $TMP etc. on the controller side.
    assert script.startswith("sh -c '")
    assert script.endswith("'")
