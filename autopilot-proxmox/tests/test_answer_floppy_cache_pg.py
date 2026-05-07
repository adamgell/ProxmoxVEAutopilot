from __future__ import annotations


def test_ensure_floppy_uses_postgres_cache(pg_conn):
    from web import answer_floppy_cache, sequences_pg

    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)
    calls: list[str] = []

    def fake_ssh(cmd: str):
        calls.append(cmd)
        return (0, b"", b"")

    path = answer_floppy_cache.ensure_floppy(
        db_path=None,
        unattend_bytes=b"<unattend>hello</unattend>",
        ssh=fake_ssh,
    )

    assert path.endswith(".img")
    assert len(calls) == 1
    rows = answer_floppy_cache.list_cache(None, in_use_volids={path})
    assert rows[0]["volid"] == path
    assert rows[0]["in_use"] is True


def test_ensure_floppy_short_circuits_pg_cache_hit(pg_conn):
    from web import answer_floppy_cache, sequences_pg

    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)
    first_calls: list[str] = []
    payload = b"cached"

    def first_ssh(cmd: str):
        first_calls.append(cmd)
        return (0, b"", b"")

    path = answer_floppy_cache.ensure_floppy(
        db_path=None,
        unattend_bytes=payload,
        ssh=first_ssh,
    )

    second_calls: list[str] = []

    def second_ssh(cmd: str):
        second_calls.append(cmd)
        if cmd.startswith("test -f "):
            return (0, b"", b"")
        raise AssertionError(f"unexpected build command: {cmd}")

    assert answer_floppy_cache.ensure_floppy(
        db_path=None,
        unattend_bytes=payload,
        ssh=second_ssh,
    ) == path
    assert len(second_calls) == 1
