from __future__ import annotations

from datetime import datetime, timedelta, timezone

import json

import pytest


@pytest.fixture
def key_path(tmp_path):
    from web import crypto

    key = tmp_path / "credential_key"
    crypto.load_or_generate_key(key)
    return key


def _table_names(conn) -> set[str]:
    rows = conn.execute(
        """
        select table_name
        from information_schema.tables
        where table_schema = 'public'
        """
    ).fetchall()
    return {row["table_name"] for row in rows}


def test_init_creates_legacy_compatibility_tables(pg_conn):
    from web import sequences_pg

    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)

    assert {
        "task_sequences",
        "task_sequence_steps",
        "credentials",
        "vm_provisioning",
        "answer_iso_cache",
        "provisioning_runs",
        "provisioning_run_steps",
    } <= _table_names(pg_conn)


def test_sequence_credentials_and_vm_provisioning_round_trip(pg_conn, key_path):
    from web import crypto, sequences_pg

    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)
    cipher = crypto.Cipher(key_path)

    cred_id = sequences_pg.create_credential(
        None,
        cipher,
        name="domain-join",
        type="domain_join",
        payload={"username": "ACME\\svc", "password": "secret"},
    )
    seq_id = sequences_pg.create_sequence(
        None,
        name="WinPE",
        description="Postgres compat",
        is_default=True,
        produces_autopilot_hash=True,
        hash_capture_phase="winpe",
        steps=[
            {
                "step_type": "join_ad_domain",
                "params": {"credential_id": cred_id, "ou_path": "OU=PCs"},
                "enabled": True,
            }
        ],
    )

    seq = sequences_pg.get_sequence(None, seq_id)
    assert seq["is_default"] is True
    assert seq["produces_autopilot_hash"] is True
    assert seq["hash_capture_phase"] == "winpe"
    assert seq["steps"][0]["params"] == {
        "credential_id": cred_id,
        "ou_path": "OU=PCs",
    }

    cred = sequences_pg.get_credential(None, cipher, cred_id)
    assert cred["payload"]["password"] == "secret"
    assert "payload" not in sequences_pg.list_credentials(None)[0]

    sequences_pg.record_vm_provisioning(None, vmid=120, sequence_id=seq_id)
    assert sequences_pg.get_vm_sequence_id(None, 120) == seq_id
    assert sequences_pg.get_vm_provisioning(None, vmid=120)["sequence_id"] == seq_id
    assert sequences_pg.list_vm_provisioning_vmids(None) == {120}


def test_winpe_run_steps_preserve_legacy_route_shape(pg_conn):
    from web import sequences_pg

    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)
    seq_id = sequences_pg.create_sequence(None, name="S", description="")
    run_id = sequences_pg.create_provisioning_run(
        None,
        sequence_id=seq_id,
        provision_path="winpe",
    )

    sequences_pg.set_provisioning_run_identity(
        None,
        run_id=run_id,
        vmid=101,
        vm_uuid="ABCDEF12-3456-7890-ABCD-EF1234567890",
    )
    run = sequences_pg.find_run_by_uuid_state(
        None,
        vm_uuid="abcdef12-3456-7890-abcd-ef1234567890",
        state="awaiting_winpe",
    )
    assert run["id"] == run_id
    assert run["vm_uuid"] == "abcdef12-3456-7890-abcd-ef1234567890"

    step = sequences_pg.append_run_step(
        None,
        run_id=run_id,
        phase="winpe",
        kind="apply_wim",
        params={"image": "win11"},
    )
    assert json.loads(step["params_json"]) == {"image": "win11"}
    sequences_pg.update_run_step_state(None, step_id=step["id"], state="running")
    sequences_pg.update_run_step_state(None, step_id=step["id"], state="ok")

    steps = sequences_pg.list_run_steps(None, run_id=run_id)
    assert steps[0]["state"] == "ok"
    assert json.loads(steps[0]["params_json"]) == {"image": "win11"}


def test_sweep_stale_runs_marks_active_runs_failed(pg_conn):
    from web import sequences_pg

    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)
    seq_id = sequences_pg.create_sequence(None, name="S", description="")
    run_id = sequences_pg.create_provisioning_run(
        None,
        sequence_id=seq_id,
        provision_path="winpe",
    )
    sequences_pg.update_provisioning_run_state(
        None,
        run_id=run_id,
        state="awaiting_winpe",
    )
    step = sequences_pg.append_run_step(
        None,
        run_id=run_id,
        phase="winpe",
        kind="apply_wim",
        params={},
    )
    stale_started = datetime.now(timezone.utc) - timedelta(hours=1)
    pg_conn.execute(
        """
        update provisioning_run_steps
        set state = 'running', started_at = %s
        where id = %s
        """,
        (stale_started, step["id"]),
    )
    pg_conn.commit()

    assert sequences_pg.sweep_stale_runs(None, ttl_seconds=60) == 1
    assert sequences_pg.get_provisioning_run(None, run_id)["state"] == "failed"
    assert sequences_pg.get_run_step(None, step["id"])["state"] == "error"


def test_answer_iso_cache_round_trip(pg_conn):
    from web import sequences_pg

    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)

    sequences_pg.insert_answer_iso_cache(
        None,
        full_hash="a" * 64,
        short_hash="a" * 16,
        volid="/var/lib/vz/snippets/autopilot-unattend-aaaaaaaaaaaaaaaa.img",
    )
    row = sequences_pg.get_answer_iso_cache(None, "a" * 64)
    assert row["short_hash"] == "a" * 16

    sequences_pg.touch_answer_iso_cache(None, "a" * 64)
    rows = sequences_pg.list_answer_iso_cache(
        None,
        in_use_volids={
            "/var/lib/vz/snippets/autopilot-unattend-aaaaaaaaaaaaaaaa.img"
        },
    )
    assert rows[0]["in_use"] is True

    sequences_pg.delete_answer_iso_cache(None, "a" * 64)
    assert sequences_pg.list_answer_iso_cache(None, in_use_volids=set()) == []
