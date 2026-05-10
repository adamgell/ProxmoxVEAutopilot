from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _create_sequences_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE credentials (
                id integer primary key,
                name text not null,
                type text not null,
                encrypted_blob blob not null,
                created_at text not null,
                updated_at text not null
            );
            CREATE TABLE task_sequences (
                id integer primary key,
                name text not null,
                description text not null,
                is_default integer not null,
                produces_autopilot_hash integer not null,
                created_at text not null,
                updated_at text not null,
                target_os text not null,
                hash_capture_phase text not null
            );
            CREATE TABLE task_sequence_steps (
                id integer primary key,
                sequence_id integer not null,
                order_index integer not null,
                step_type text not null,
                params_json text not null,
                enabled integer not null
            );
            CREATE TABLE vm_provisioning (
                vmid integer primary key,
                sequence_id integer,
                provisioned_at text not null
            );
            CREATE TABLE answer_iso_cache (
                hash text primary key,
                short_hash text not null,
                volid text not null,
                compiled_at text not null,
                last_used_at text
            );
            CREATE TABLE provisioning_runs (
                id integer primary key,
                vmid integer,
                sequence_id integer not null,
                provision_path text not null,
                state text not null,
                vm_uuid text,
                started_at text not null,
                finished_at text,
                last_error text
            );
            CREATE TABLE provisioning_run_steps (
                id integer primary key,
                run_id integer not null,
                order_index integer not null,
                phase text not null,
                kind text not null,
                params_json text not null,
                state text not null,
                started_at text,
                finished_at text,
                error text
            );
            """
        )
        conn.execute(
            """
            INSERT INTO credentials
                (id, name, type, encrypted_blob, created_at, updated_at)
            VALUES
                (7, 'home', 'domain_join', ?, '2026-04-20T10:00:00+00:00',
                 '2026-04-21T10:00:00+00:00')
            """,
            (b"legacy-home",),
        )
        conn.execute(
            """
            INSERT INTO task_sequences
                (id, name, description, is_default, produces_autopilot_hash,
                 created_at, updated_at, target_os, hash_capture_phase)
            VALUES
                (7, 'Legacy WinPE', 'legacy sequence', 0, 1,
                 '2026-04-20T10:00:00+00:00', '2026-04-21T10:00:00+00:00',
                 'windows', 'winpe')
            """
        )
        conn.execute(
            """
            INSERT INTO task_sequence_steps
                (id, sequence_id, order_index, step_type, params_json, enabled)
            VALUES (9, 7, 0, 'domain_join', ?, 1)
            """,
            (json.dumps({"credential_id": 7}),),
        )
        conn.execute(
            """
            INSERT INTO vm_provisioning (vmid, sequence_id, provisioned_at)
            VALUES (101, 7, '2026-04-22T10:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO answer_iso_cache
                (hash, short_hash, volid, compiled_at, last_used_at)
            VALUES ('abcdef', 'abcdef', 'local:iso/answer.iso',
                    '2026-04-22T10:00:00+00:00',
                    '2026-04-22T10:30:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO provisioning_runs
                (id, vmid, sequence_id, provision_path, state, vm_uuid,
                 started_at, finished_at, last_error)
            VALUES
                (1, 101, 7, 'winpe', 'done', 'legacy-uuid',
                 '2026-04-22T11:00:00+00:00',
                 '2026-04-22T11:30:00+00:00', NULL)
            """
        )
        conn.execute(
            """
            INSERT INTO provisioning_run_steps
                (id, run_id, order_index, phase, kind, params_json, state,
                 started_at, finished_at, error)
            VALUES
                (1, 1, 0, 'winpe', 'domain_join', ?, 'ok',
                 '2026-04-22T11:05:00+00:00',
                 '2026-04-22T11:06:00+00:00', NULL)
            """,
            (json.dumps({"credential_id": 7}),),
        )


def _create_monitor_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE monitoring_settings (
                id integer primary key,
                enabled integer not null,
                interval_seconds integer not null,
                ad_credential_id integer not null,
                updated_at text not null
            );
            CREATE TABLE monitoring_search_ous (
                id integer primary key,
                dn text not null,
                label text not null,
                enabled integer not null,
                sort_order integer not null,
                created_at text not null,
                updated_at text not null
            );
            CREATE TABLE monitoring_sweeps (
                id integer primary key,
                started_at text not null,
                ended_at text,
                vm_count integer not null,
                errors_json text not null
            );
            CREATE TABLE pve_snapshots (
                id integer primary key,
                sweep_id integer not null,
                checked_at text not null,
                vmid integer not null,
                present integer not null,
                node text,
                name text,
                status text,
                tags_csv text,
                lock_mode text,
                cores integer,
                sockets integer,
                memory_mb integer,
                balloon_mb integer,
                machine text,
                bios text,
                smbios1 text,
                args text,
                vmgenid text,
                disks_json text,
                net_json text,
                config_digest text,
                probe_error text
            );
            CREATE TABLE device_probes (
                id integer primary key,
                sweep_id integer not null,
                checked_at text not null,
                vmid integer not null,
                vm_name text,
                win_name text,
                serial text,
                uuid text,
                os_build text,
                dsreg_status text,
                ad_found integer not null,
                ad_match_count integer not null,
                ad_matches_json text,
                entra_found integer not null,
                entra_match_count integer not null,
                entra_matches_json text,
                intune_found integer not null,
                intune_match_count integer not null,
                intune_matches_json text,
                probe_errors_json text
            );
            """
        )
        conn.execute(
            """
            INSERT INTO monitoring_settings
                (id, enabled, interval_seconds, ad_credential_id, updated_at)
            VALUES (1, 1, 300, 7, '2026-04-21T15:06:43+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO monitoring_search_ous
                (id, dn, label, enabled, sort_order, created_at, updated_at)
            VALUES
                (7, 'OU=WorkspaceLabs,DC=home,DC=gell,DC=one',
                 'WorkspaceLabs legacy', 1, 1,
                 '2026-04-20T10:00:00+00:00',
                 '2026-04-21T10:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO monitoring_sweeps
                (id, started_at, ended_at, vm_count, errors_json)
            VALUES
                (5, '2026-04-22T12:00:00+00:00',
                 '2026-04-22T12:01:00+00:00', 1, '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO pve_snapshots
                (id, sweep_id, checked_at, vmid, present, node, name, status,
                 tags_csv, lock_mode, cores, sockets, memory_mb, balloon_mb,
                 machine, bios, smbios1, args, vmgenid, disks_json, net_json,
                 config_digest, probe_error)
            VALUES
                (8, 5, '2026-04-22T12:00:10+00:00', 101, 1, 'pve1',
                 'LegacyVM', 'running', 'lab,winpe', NULL, 2, 1, 4096,
                 NULL, 'q35', 'ovmf', 'uuid=legacy', NULL, 'vmgen',
                 '[]', '[]', 'digest', NULL)
            """
        )
        conn.execute(
            """
            INSERT INTO device_probes
                (id, sweep_id, checked_at, vmid, vm_name, win_name, serial,
                 uuid, os_build, dsreg_status, ad_found, ad_match_count,
                 ad_matches_json, entra_found, entra_match_count,
                 entra_matches_json, intune_found, intune_match_count,
                 intune_matches_json, probe_errors_json)
            VALUES
                (9, 5, '2026-04-22T12:00:20+00:00', 101, 'LegacyVM',
                 'LEGACYVM', 'serial-1', 'legacy-uuid', '22631', '{}',
                 1, 1, '[]', 0, 0, '[]', 0, 0, '[]', '{}')
            """
        )


def _create_devices_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE deletions (
                id integer primary key,
                deleted_at text not null,
                source text not null,
                object_id text not null,
                serial text,
                display_name text,
                status text not null,
                message text
            );
            """
        )
        conn.execute(
            """
            INSERT INTO deletions
                (id, deleted_at, source, object_id, serial, display_name, status, message)
            VALUES
                (4, '2026-04-22T13:00:00+00:00', 'intune', 'object-1',
                 'serial-1', 'LegacyVM', 'ok', 'removed')
            """
        )


def _bootstrap_postgres(pg_conn) -> None:
    from web import device_history_pg, devices_pg, sequences_pg

    devices_pg.reset_for_tests(pg_conn)
    device_history_pg.reset_for_tests(pg_conn)
    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)
    device_history_pg.init(pg_conn)
    devices_pg.init(pg_conn)
    pg_conn.execute(
        """
        INSERT INTO credentials
            (id, name, type, encrypted_blob, created_at, updated_at)
        VALUES (1, 'default-local-admin', 'local_admin', %s, now(), now())
        """,
        (b"current-default",),
    )
    pg_conn.execute(
        """
        INSERT INTO task_sequences
            (id, name, description, is_default, produces_autopilot_hash,
             target_os, hash_capture_phase, created_at, updated_at)
        VALUES (1, 'Current Default', '', true, false, 'windows', 'oobe',
                now(), now())
        """
    )
    pg_conn.execute(
        """
        INSERT INTO provisioning_runs
            (id, vmid, sequence_id, provision_path, state, vm_uuid, started_at)
        VALUES (1, 900, 1, 'winpe', 'done', 'current-uuid',
                '2026-05-01T10:00:00+00:00')
        """
    )
    pg_conn.commit()


def test_sqlite_state_migration_repairs_legacy_runtime_state(tmp_path, pg_conn, pg_dsn):
    from scripts import migrate_sqlite_state_to_postgres as migrator

    _bootstrap_postgres(pg_conn)
    _create_sequences_db(tmp_path / "sequences.db")
    _create_monitor_db(tmp_path / "device_monitor.db")
    _create_devices_db(tmp_path / "devices.db")

    summary = migrator.migrate(output_dir=tmp_path, database_url=pg_dsn)

    assert summary["migration"]["committed"] == 1
    assert pg_conn.execute(
        "SELECT id, name, type FROM credentials WHERE name = 'home'"
    ).fetchone() == {"id": 7, "name": "home", "type": "domain_join"}
    assert pg_conn.execute(
        "SELECT ad_credential_id, interval_seconds FROM monitoring_settings WHERE id = 1"
    ).fetchone() == {"ad_credential_id": 7, "interval_seconds": 300}
    assert pg_conn.execute(
        "SELECT sequence_id FROM vm_provisioning WHERE vmid = 101"
    ).fetchone()["sequence_id"] == 7
    assert pg_conn.execute(
        "SELECT volid FROM answer_iso_cache WHERE hash = 'abcdef'"
    ).fetchone()["volid"] == "local:iso/answer.iso"

    migrated_run = pg_conn.execute(
        """
        SELECT id, vmid, sequence_id, state
        FROM provisioning_runs
        WHERE vm_uuid = 'legacy-uuid'
        """
    ).fetchone()
    assert migrated_run == {"id": 2, "vmid": 101, "sequence_id": 7, "state": "done"}
    step = pg_conn.execute(
        "SELECT run_id, params_json FROM provisioning_run_steps WHERE run_id = %s",
        (migrated_run["id"],),
    ).fetchone()
    assert step["params_json"]["credential_id"] == 7

    assert pg_conn.execute("SELECT COUNT(*) AS count FROM monitoring_sweeps").fetchone()["count"] == 1
    assert pg_conn.execute("SELECT COUNT(*) AS count FROM pve_snapshots").fetchone()["count"] == 1
    assert pg_conn.execute("SELECT COUNT(*) AS count FROM device_probes").fetchone()["count"] == 1
    assert pg_conn.execute("SELECT COUNT(*) AS count FROM deletions").fetchone()["count"] == 1

    migrator.migrate(output_dir=tmp_path, database_url=pg_dsn)
    assert pg_conn.execute("SELECT COUNT(*) AS count FROM provisioning_runs").fetchone()["count"] == 2
    assert pg_conn.execute("SELECT COUNT(*) AS count FROM provisioning_run_steps").fetchone()["count"] == 1
    assert pg_conn.execute("SELECT COUNT(*) AS count FROM deletions").fetchone()["count"] == 1


def test_sqlite_migration_remaps_conflicting_credential_id(tmp_path, pg_conn, pg_dsn):
    from scripts import migrate_sqlite_state_to_postgres as migrator

    _bootstrap_postgres(pg_conn)
    with sqlite3.connect(tmp_path / "sequences.db") as conn:
        conn.executescript(
            """
            CREATE TABLE credentials (
                id integer primary key,
                name text not null,
                type text not null,
                encrypted_blob blob not null,
                created_at text not null,
                updated_at text not null
            );
            CREATE TABLE task_sequences (
                id integer primary key,
                name text not null,
                description text not null,
                is_default integer not null,
                produces_autopilot_hash integer not null,
                created_at text not null,
                updated_at text not null,
                target_os text not null,
                hash_capture_phase text not null
            );
            CREATE TABLE task_sequence_steps (
                id integer primary key,
                sequence_id integer not null,
                order_index integer not null,
                step_type text not null,
                params_json text not null,
                enabled integer not null
            );
            """
        )
        conn.execute(
            """
            INSERT INTO credentials
                (id, name, type, encrypted_blob, created_at, updated_at)
            VALUES (1, 'legacy-domain', 'domain_join', ?,
                    '2026-04-20T10:00:00+00:00',
                    '2026-04-20T10:00:00+00:00')
            """,
            (b"domain",),
        )
    _create_monitor_db(tmp_path / "device_monitor.db")
    with sqlite3.connect(tmp_path / "device_monitor.db") as conn:
        conn.execute("UPDATE monitoring_settings SET ad_credential_id = 1 WHERE id = 1")

    migrator.migrate(
        output_dir=tmp_path,
        database_url=pg_dsn,
        include_monitor_history=False,
    )

    legacy_domain = pg_conn.execute(
        "SELECT id FROM credentials WHERE name = 'legacy-domain'"
    ).fetchone()
    assert legacy_domain["id"] != 1
    settings = pg_conn.execute(
        "SELECT ad_credential_id FROM monitoring_settings WHERE id = 1"
    ).fetchone()
    assert settings["ad_credential_id"] == legacy_domain["id"]


def test_sqlite_migration_can_rewrap_legacy_credential_key(tmp_path, pg_conn, pg_dsn):
    from scripts import migrate_sqlite_state_to_postgres as migrator
    from web.crypto import Cipher

    _bootstrap_postgres(pg_conn)
    legacy_key = tmp_path / "legacy.key"
    target_key = tmp_path / "target.key"
    legacy_cipher = Cipher(legacy_key)
    target_cipher = Cipher(target_key)
    legacy_blob = legacy_cipher.encrypt_json(
        {
            "username": "home\\svc-deploy",
            "password": "not-the-real-password",
        }
    )

    with sqlite3.connect(tmp_path / "sequences.db") as conn:
        conn.executescript(
            """
            CREATE TABLE credentials (
                id integer primary key,
                name text not null,
                type text not null,
                encrypted_blob blob not null,
                created_at text not null,
                updated_at text not null
            );
            """
        )
        conn.execute(
            """
            INSERT INTO credentials
                (id, name, type, encrypted_blob, created_at, updated_at)
            VALUES (7, 'home', 'domain_join', ?,
                    '2026-04-20T10:00:00+00:00',
                    '2026-04-20T10:00:00+00:00')
            """,
            (legacy_blob,),
        )
    _create_monitor_db(tmp_path / "device_monitor.db")

    migrator.migrate(
        output_dir=tmp_path,
        database_url=pg_dsn,
        include_monitor_history=False,
        legacy_credential_key=legacy_key,
        target_credential_key=target_key,
    )

    blob = pg_conn.execute(
        "SELECT encrypted_blob FROM credentials WHERE id = 7"
    ).fetchone()["encrypted_blob"]
    payload = target_cipher.decrypt_json(bytes(blob))
    assert payload["username"] == "home\\svc-deploy"
    assert payload["password"] == "not-the-real-password"
