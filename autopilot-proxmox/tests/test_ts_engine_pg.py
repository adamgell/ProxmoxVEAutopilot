from __future__ import annotations

import os
import shutil
import subprocess
import time
from contextlib import closing

import pytest


pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker is required for PostgreSQL-backed task sequence tests",
)


@pytest.fixture(scope="module")
def pg_dsn():
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "-d",
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-e",
            "POSTGRES_DB=autopilot_test",
            "-p",
            "127.0.0.1::5432",
            "postgres:16-alpine",
        ],
        text=True,
    ).strip()
    try:
        port = subprocess.check_output(
            [
                "docker",
                "inspect",
                "-f",
                "{{(index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostPort}}",
                container,
            ],
            text=True,
        ).strip()
        dsn = (
            f"postgresql://postgres:postgres@127.0.0.1:{port}/"
            "autopilot_test"
        )
        import psycopg

        deadline = time.time() + 30
        while True:
            try:
                with psycopg.connect(dsn) as conn:
                    conn.execute("select 1")
                break
            except Exception:
                if time.time() > deadline:
                    logs = subprocess.run(
                        ["docker", "logs", container],
                        text=True,
                        capture_output=True,
                    ).stdout
                    raise RuntimeError(f"postgres did not start:\n{logs}")
                time.sleep(0.5)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False)


@pytest.fixture
def pg_conn(pg_dsn):
    import psycopg
    from psycopg.rows import dict_row
    from web import ts_engine_pg

    with closing(psycopg.connect(pg_dsn, row_factory=dict_row)) as conn:
        ts_engine_pg.reset_for_tests(conn)
        ts_engine_pg.init(conn)
        yield conn


def test_init_creates_postgres_engine_tables(pg_conn):
    rows = pg_conn.execute(
        """
        select table_name
        from information_schema.tables
        where table_schema = 'public'
        """
    ).fetchall()
    table_names = {row["table_name"] for row in rows}
    assert {
        "ts_engine_schema_migrations",
        "ts_task_sequences",
        "ts_task_sequence_nodes",
        "ts_task_sequence_versions",
        "ts_provisioning_runs",
        "ts_run_plan_steps",
        "ts_run_step_events",
        "ts_content_items",
        "ts_content_versions",
        "ts_run_content_manifest",
    } <= table_names


def test_sequence_target_os_round_trip(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(
        pg_conn,
        name="Ubuntu v2",
        target_os="ubuntu",
    )

    assert ts_engine_pg.get_sequence(pg_conn, sequence_id)["target_os"] == "ubuntu"
    rows = ts_engine_pg.list_sequences(pg_conn)
    assert rows[0]["target_os"] == "ubuntu"

    ts_engine_pg.update_sequence(pg_conn, sequence_id, target_os="windows")
    assert ts_engine_pg.get_sequence(pg_conn, sequence_id)["target_os"] == "windows"


def test_ubuntu_cloud_init_owned_steps_can_be_marked_done(pg_conn):
    from web import app as web_app
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(
        pg_conn,
        name="Ubuntu cloud-init",
        target_os="ubuntu",
    )
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Install core",
        kind="install_ubuntu_core",
        phase="install",
        position=0,
    )
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Linux heartbeat",
        kind="linux_agent_heartbeat",
        phase="full_os",
        position=1,
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn,
        sequence_version_id=version_id,
    )

    completed = web_app._complete_ubuntu_v2_steps(
        pg_conn,
        run_id=run_id,
        agent_id="controller",
        kinds={"install_ubuntu_core"},
        phases={"install"},
        message="cloud-init complete",
        data={"seed_detached": True},
    )

    assert len(completed) == 1
    steps = ts_engine_pg.list_run_steps(pg_conn, run_id)
    assert {step["kind"]: step["state"] for step in steps} == {
        "install_ubuntu_core": "done",
        "linux_agent_heartbeat": "pending",
    }
    assert ts_engine_pg.get_run(pg_conn, run_id)["state"] == "queued"


def test_root_sibling_positions_are_unique(pg_conn):
    from psycopg.errors import UniqueViolation
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Unique Positions")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Partition Disk",
        kind="partition_disk",
        phase="winpe",
        position=0,
    )

    with pytest.raises(UniqueViolation):
        ts_engine_pg.add_step(
            pg_conn,
            sequence_id=sequence_id,
            parent_id=None,
            name="Apply OS",
            kind="apply_os_image",
            phase="winpe",
            position=0,
        )

    pg_conn.rollback()


def test_compile_ordered_tree_into_immutable_run_plan(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Bare Metal")
    preflight = ts_engine_pg.add_group(
        pg_conn, sequence_id=sequence_id, name="Preflight", position=0
    )
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=preflight,
        name="Collect identity",
        kind="collect_identity",
        phase="winpe",
        position=0,
    )
    os_group = ts_engine_pg.add_group(
        pg_conn, sequence_id=sequence_id, name="Disk + OS", position=1
    )
    apply_step = ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=os_group,
        name="Apply OS",
        kind="apply_os_image",
        phase="winpe",
        position=0,
        params={"image": "win11-enterprise"},
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn,
        sequence_version_id=version_id,
        deployment_target={"vmid": 119, "vm_uuid": "vm-119"},
    )

    steps = ts_engine_pg.list_run_steps(pg_conn, run_id)
    assert [(s["ordinal"], s["path"], s["kind"]) for s in steps] == [
        (0, "Preflight / Collect identity", "collect_identity"),
        (1, "Disk + OS / Apply OS", "apply_os_image"),
    ]
    assert steps[1]["source_node_id"] == apply_step
    assert steps[1]["resolved_params_json"] == {"image": "win11-enterprise"}

    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=os_group,
        name="Late edit",
        kind="late_edit",
        phase="winpe",
        position=1,
    )
    assert [s["kind"] for s in ts_engine_pg.list_run_steps(pg_conn, run_id)] == [
        "collect_identity",
        "apply_os_image",
    ]


def test_builder_can_replace_sequence_nodes_and_compile_new_version(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(
        pg_conn,
        name="Desktop CloudOSD",
        description="Initial description",
    )
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Old step",
        kind="old_step",
        phase="full_os",
        position=0,
    )
    old_version = ts_engine_pg.compile_sequence(pg_conn, sequence_id)

    ts_engine_pg.update_sequence(
        pg_conn,
        sequence_id,
        name="Desktop CloudOSD v2",
        description="Smart builder draft",
        updated_by="pytest",
    )
    ts_engine_pg.replace_sequence_nodes(
        pg_conn,
        sequence_id,
        [
            {
                "client_id": "pe",
                "node_type": "group",
                "name": "PE deployment",
            },
            {
                "client_id": "deploy",
                "parent_id": "pe",
                "node_type": "step",
                "name": "Run OSDCloud workflow",
                "kind": "cloudosd_deploy_os",
                "phase": "pe",
                "params": {"workflow": "default"},
            },
            {
                "client_id": "hash",
                "node_type": "step",
                "name": "Capture Autopilot hardware hash",
                "kind": "capture_autopilot_hash",
                "phase": "full_os",
                "retry_count": 2,
                "retry_delay_seconds": 30,
            },
        ],
        updated_by="pytest",
    )
    new_version = ts_engine_pg.compile_sequence(pg_conn, sequence_id)

    assert new_version != old_version
    seq = ts_engine_pg.get_sequence(pg_conn, sequence_id)
    assert seq["name"] == "Desktop CloudOSD v2"
    assert seq["updated_by"] == "pytest"

    nodes = ts_engine_pg.list_sequence_nodes(pg_conn, sequence_id)
    assert [(n["node_type"], n["name"], n["kind"], n["phase"]) for n in nodes] == [
        ("group", "PE deployment", None, "any"),
        ("step", "Capture Autopilot hardware hash", "capture_autopilot_hash", "full_os"),
        ("step", "Run OSDCloud workflow", "cloudosd_deploy_os", "pe"),
    ]

    run_id = ts_engine_pg.create_run_from_version(
        pg_conn,
        sequence_version_id=new_version,
    )
    assert [(s["path"], s["kind"], s["phase"]) for s in ts_engine_pg.list_run_steps(pg_conn, run_id)] == [
        ("PE deployment / Run OSDCloud workflow", "cloudosd_deploy_os", "pe"),
        ("Capture Autopilot hardware hash", "capture_autopilot_hash", "full_os"),
    ]


def test_run_content_manifest_is_resolved_per_run(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Manifest Demo")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Apply OS",
        kind="apply_os_image",
        phase="winpe",
        position=0,
        content_refs=["windows-11-enterprise"],
    )
    item_id = ts_engine_pg.create_content_item(
        pg_conn, name="windows-11-enterprise", content_type="os_image"
    )
    version_id = ts_engine_pg.create_content_version(
        pg_conn,
        content_item_id=item_id,
        version="26100.1",
        sha256="a" * 64,
        source_uri="proxmox-iso://isos/Win11.iso",
        size_bytes=1024,
    )
    seq_version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn, sequence_version_id=seq_version_id
    )
    ts_engine_pg.add_manifest_item(
        pg_conn,
        run_id=run_id,
        content_version_id=version_id,
        logical_name="windows-11-enterprise",
        required_phase="winpe",
        staging_path="D:\\sources\\install.wim",
    )

    manifest = ts_engine_pg.list_run_manifest(pg_conn, run_id)
    assert manifest == [
        {
            "logical_name": "windows-11-enterprise",
            "content_type": "os_image",
            "version": "26100.1",
            "sha256": "a" * 64,
            "source_uri": "proxmox-iso://isos/Win11.iso",
            "required_phase": "winpe",
            "staging_path": "D:\\sources\\install.wim",
            "status": "pending",
        }
    ]


def test_content_records_validate_content_type_and_reboot_behavior(pg_conn):
    from web import ts_engine_pg

    with pytest.raises(ValueError, match="content_type must be one of"):
        ts_engine_pg.create_content_item(
            pg_conn, name="bad-content", content_type="blob"
        )

    item_id = ts_engine_pg.create_content_item(
        pg_conn, name="valid-package", content_type="package"
    )
    with pytest.raises(ValueError, match="reboot_behavior must be one of"):
        ts_engine_pg.create_content_version(
            pg_conn,
            content_item_id=item_id,
            version="1.0",
            sha256="f" * 64,
            source_uri="https://content.local/valid-package.msi",
            reboot_behavior="surprise",
        )


def test_content_manifest_can_be_pinned_from_step_content_refs(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Manifest Pin Demo")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Install QGA",
        kind="install_package",
        phase="full_os",
        position=0,
        content_refs=["qemu-guest-agent"],
    )
    item_id = ts_engine_pg.create_content_item(
        pg_conn, name="qemu-guest-agent", content_type="package"
    )
    older_version_id = ts_engine_pg.create_content_version(
        pg_conn,
        content_item_id=item_id,
        version="106.0",
        sha256="b" * 64,
        source_uri="https://content.local/qga-106.msi",
    )
    latest_version_id = ts_engine_pg.create_content_version(
        pg_conn,
        content_item_id=item_id,
        version="107.0",
        sha256="c" * 64,
        source_uri="https://content.local/qga-107.msi",
        metadata={"install_command": "msiexec.exe /i {path} /qn"},
    )
    assert older_version_id != latest_version_id
    seq_version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn, sequence_version_id=seq_version_id
    )

    ts_engine_pg.resolve_run_content_manifest(pg_conn, run_id)

    manifest = ts_engine_pg.list_run_manifest(pg_conn, run_id)
    assert manifest == [
        {
            "logical_name": "qemu-guest-agent",
            "content_type": "package",
            "version": "107.0",
            "sha256": "c" * 64,
            "source_uri": "https://content.local/qga-107.msi",
            "required_phase": "full_os",
            "staging_path": (
                "C:\\ProgramData\\ProxmoxVEAutopilot\\Content"
                "\\qemu-guest-agent\\107.0"
            ),
            "status": "pending",
            "metadata": {"install_command": "msiexec.exe /i {path} /qn"},
        }
    ]


def test_claimed_package_step_includes_resolved_content_manifest(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Package Action")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Install package",
        kind="install_package",
        phase="full_os",
        position=0,
        params={"install_command": "msiexec.exe /i {path} /qn"},
        content_refs=["notepad-plus-plus"],
    )
    item_id = ts_engine_pg.create_content_item(
        pg_conn, name="notepad-plus-plus", content_type="package"
    )
    ts_engine_pg.create_content_version(
        pg_conn,
        content_item_id=item_id,
        version="8.6.0",
        sha256="9" * 64,
        source_uri="https://content.local/npp.msi",
        size_bytes=4096,
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn,
        sequence_version_id=version_id,
        resolve_content=True,
    )

    claim = ts_engine_pg.claim_next_step(
        pg_conn, run_id=run_id, phase="full_os", agent_id="osd-1"
    )

    assert claim["kind"] == "install_package"
    assert claim["params_json"] == {
        "install_command": "msiexec.exe /i {path} /qn",
    }
    assert len(claim["content"]) == 1
    content = claim["content"][0]
    assert content["logical_name"] == "notepad-plus-plus"
    assert content["content_type"] == "package"
    assert content["version"] == "8.6.0"
    assert content["sha256"] == "9" * 64
    assert content["source_uri"] == "https://content.local/npp.msi"
    assert content["required_phase"] == "full_os"
    assert content["staging_path"] == (
        "C:\\ProgramData\\ProxmoxVEAutopilot\\Content"
        "\\notepad-plus-plus\\8.6.0"
    )
    assert content["status"] == "pending"


def test_manifest_item_staging_state_tracks_attempts_and_errors(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Staging Demo")
    item_id = ts_engine_pg.create_content_item(
        pg_conn, name="qemu-guest-agent", content_type="package"
    )
    version_id = ts_engine_pg.create_content_version(
        pg_conn,
        content_item_id=item_id,
        version="107.0",
        sha256="d" * 64,
        source_uri="https://content.local/qga-107.msi",
    )
    seq_version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn, sequence_version_id=seq_version_id
    )
    manifest_id = ts_engine_pg.add_manifest_item(
        pg_conn,
        run_id=run_id,
        content_version_id=version_id,
        logical_name="qemu-guest-agent",
        required_phase="full_os",
    )

    staging = ts_engine_pg.mark_manifest_item_staging(
        pg_conn,
        manifest_id=manifest_id,
        run_id=run_id,
        status="staging",
        agent_id="osd-1",
        staging_path=(
            "C:\\ProgramData\\ProxmoxVEAutopilot\\Content"
            "\\qemu-guest-agent\\107.0"
        ),
    )
    assert staging["status"] == "staging"
    assert staging["staging_attempts"] == 1
    assert staging["last_error"] is None

    failed = ts_engine_pg.mark_manifest_item_staging(
        pg_conn,
        manifest_id=manifest_id,
        run_id=run_id,
        status="failed",
        agent_id="osd-1",
        error="download timed out",
    )
    assert failed["status"] == "failed"
    assert failed["staging_attempts"] == 1
    assert failed["last_error"] == "download timed out"

    retry = ts_engine_pg.mark_manifest_item_staging(
        pg_conn,
        manifest_id=manifest_id,
        run_id=run_id,
        status="staging",
        agent_id="osd-1",
    )
    assert retry["status"] == "staging"
    assert retry["staging_attempts"] == 2
    assert retry["last_error"] is None

    staged = ts_engine_pg.mark_manifest_item_staging(
        pg_conn,
        manifest_id=manifest_id,
        run_id=run_id,
        status="staged",
        agent_id="osd-1",
    )
    assert staged["status"] == "staged"
    assert staged["staging_attempts"] == 2
    assert staged["staged_at"] is not None


def test_global_content_manifest_v1_uses_latest_enabled_versions(pg_conn):
    from web import ts_engine_pg
    from web.content_manifest import manifest_digest

    disabled_id = ts_engine_pg.create_content_item(
        pg_conn, name="disabled-app", content_type="package"
    )
    ts_engine_pg.create_content_version(
        pg_conn,
        content_item_id=disabled_id,
        version="1.0",
        sha256="1" * 64,
        source_uri="https://content.local/disabled.msi",
        size_bytes=1,
    )
    pg_conn.execute(
        "UPDATE ts_content_items SET enabled = false WHERE id = %s",
        (disabled_id,),
    )
    pg_conn.commit()

    item_id = ts_engine_pg.create_content_item(
        pg_conn, name="qemu-guest-agent", content_type="package",
        description="QGA MSI",
    )
    ts_engine_pg.create_content_version(
        pg_conn,
        content_item_id=item_id,
        version="106.0",
        sha256="2" * 64,
        source_uri="https://content.local/qga-106.msi",
        size_bytes=1024,
    )
    ts_engine_pg.create_content_version(
        pg_conn,
        content_item_id=item_id,
        version="107.0",
        sha256="A" * 64,
        source_uri="https://content.local/qga-107.msi",
        size_bytes=2048,
        architecture="x64",
        target_os="windows",
        reboot_behavior="deferred",
        conditions={"phase": "full_os", "min_build": 22631},
        metadata={"install_command": "msiexec.exe /i {path} /qn"},
    )

    response = ts_engine_pg.build_content_manifest_v1(pg_conn)

    assert response["manifest"] == {
        "schema_version": 1,
        "items": [
            {
                "id": "qemu-guest-agent",
                "kind": "package",
                "name": "qemu-guest-agent",
                "version": "107.0",
                "source_uri": "https://content.local/qga-107.msi",
                "sha256": "a" * 64,
                "size_bytes": 2048,
                "architecture": "x64",
                "target_os": "windows",
                "reboot_behavior": "deferred",
                "conditions": {"phase": "full_os", "min_build": 22631},
                "metadata": {"install_command": "msiexec.exe /i {path} /qn"},
            }
        ],
    }
    assert response["digest"] == manifest_digest(response["manifest"])
    assert response["item_count"] == 1


def test_create_run_can_require_and_pin_content_manifest(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Required Content")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Install app",
        kind="install_package",
        phase="full_os",
        position=0,
        content_refs=["required-app"],
    )
    item_id = ts_engine_pg.create_content_item(
        pg_conn, name="required-app", content_type="package"
    )
    ts_engine_pg.create_content_version(
        pg_conn,
        content_item_id=item_id,
        version="1.0.0",
        sha256="e" * 64,
        source_uri="https://content.local/required-app.msi",
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)

    run_id = ts_engine_pg.create_run_from_version(
        pg_conn,
        sequence_version_id=version_id,
        resolve_content=True,
    )

    assert ts_engine_pg.list_run_manifest(pg_conn, run_id)[0]["logical_name"] == (
        "required-app"
    )


def test_create_run_rejects_missing_required_content_when_requested(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Missing Content")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Install missing app",
        kind="install_package",
        phase="full_os",
        position=0,
        content_refs=["missing-app"],
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)

    with pytest.raises(ValueError, match="content reference has no version"):
        ts_engine_pg.create_run_from_version(
            pg_conn,
            sequence_version_id=version_id,
            resolve_content=True,
        )


def test_conditions_skip_steps_when_run_is_created(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Conditional Apps")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Install app bundle",
        kind="install_app",
        phase="full_os",
        position=0,
        condition={"eq": ["variables.install_apps", True]},
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn,
        sequence_version_id=version_id,
        run_variables={"install_apps": False},
    )

    steps = ts_engine_pg.list_run_steps(pg_conn, run_id)
    assert steps[0]["state"] == "skipped"
    assert steps[0]["condition_result_json"] == {
        "matched": False,
        "reason": "eq:variables.install_apps",
    }


def test_step_completion_and_reboot_resume_are_recorded(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Reboot Demo")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Install Windows update",
        kind="install_package",
        phase="full_os",
        position=0,
        reboot_behavior="required",
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn, sequence_version_id=version_id
    )
    claim = ts_engine_pg.claim_next_step(
        pg_conn, run_id=run_id, phase="full_os", agent_id="osd-1"
    )

    ts_engine_pg.append_step_log(
        pg_conn,
        run_id=run_id,
        step_id=claim["id"],
        agent_id="osd-1",
        stream="stdout",
        content="Package requested reboot",
    )
    reboot_result = ts_engine_pg.complete_step(
        pg_conn,
        run_id=run_id,
        step_id=claim["id"],
        agent_id="osd-1",
        status="reboot_required",
        message="Reboot required",
    )
    assert reboot_result["state"] == "awaiting_reboot"
    assert ts_engine_pg.get_run(pg_conn, run_id)["state"] == "awaiting_reboot"

    resumed = ts_engine_pg.mark_reboot_complete(
        pg_conn, run_id=run_id, step_id=claim["id"], agent_id="osd-1"
    )
    assert resumed["state"] == "done"
    assert ts_engine_pg.get_run(pg_conn, run_id)["state"] == "done"


def test_required_reboot_behavior_converts_success_to_awaiting_reboot(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Required Reboot")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Install package requiring reboot",
        kind="install_package",
        phase="full_os",
        position=0,
        reboot_behavior="required",
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn, sequence_version_id=version_id
    )
    claim = ts_engine_pg.claim_next_step(
        pg_conn, run_id=run_id, phase="full_os", agent_id="osd-1"
    )

    result = ts_engine_pg.complete_step(
        pg_conn,
        run_id=run_id,
        step_id=claim["id"],
        agent_id="osd-1",
        status="success",
        message="Install succeeded and reboot is required by policy",
    )

    assert result["state"] == "awaiting_reboot"
    assert ts_engine_pg.get_run(pg_conn, run_id)["state"] == "awaiting_reboot"


def test_late_failure_does_not_downgrade_awaiting_reboot_step(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Late Shutdown Failure")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Join domain",
        kind="join_domain_role",
        phase="full_os",
        position=0,
        reboot_behavior="required",
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn, sequence_version_id=version_id
    )
    claim = ts_engine_pg.claim_next_step(
        pg_conn, run_id=run_id, phase="full_os", agent_id="osd-1"
    )
    ts_engine_pg.complete_step(
        pg_conn,
        run_id=run_id,
        step_id=claim["id"],
        agent_id="osd-1",
        status="success",
        message="Domain join succeeded and reboot is required",
    )

    late = ts_engine_pg.complete_step(
        pg_conn,
        run_id=run_id,
        step_id=claim["id"],
        agent_id="osd-1",
        status="failed",
        message="shutdown.exe failed with exit 1115",
    )

    assert late["state"] == "awaiting_reboot"
    assert ts_engine_pg.get_run(pg_conn, run_id)["state"] == "awaiting_reboot"
    events = ts_engine_pg.list_run_events(pg_conn, run_id)
    assert events[0]["event_type"] == "step_late_failure_ignored"
    assert "1115" in events[0]["message"]


def test_failed_step_retries_until_retry_count_is_exhausted(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Retry Package")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Install retryable package",
        kind="install_package",
        phase="full_os",
        position=0,
        retry_count=1,
        retry_delay_seconds=5,
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn, sequence_version_id=version_id
    )

    first_claim = ts_engine_pg.claim_next_step(
        pg_conn, run_id=run_id, phase="full_os", agent_id="osd-1"
    )
    retry_result = ts_engine_pg.complete_step(
        pg_conn,
        run_id=run_id,
        step_id=first_claim["id"],
        agent_id="osd-1",
        status="failed",
        message="transient download failure",
    )
    assert retry_result["state"] == "pending"
    assert retry_result["attempt"] == 1
    assert retry_result["last_error"] == "transient download failure"
    assert ts_engine_pg.get_run(pg_conn, run_id)["state"] == "running_full_os"

    second_claim = ts_engine_pg.claim_next_step(
        pg_conn, run_id=run_id, phase="full_os", agent_id="osd-1"
    )
    assert second_claim["id"] == first_claim["id"]
    assert second_claim["attempt"] == 2
    exhausted_result = ts_engine_pg.complete_step(
        pg_conn,
        run_id=run_id,
        step_id=second_claim["id"],
        agent_id="osd-1",
        status="failed",
        message="permanent install failure",
    )

    assert exhausted_result["state"] == "failed"
    assert exhausted_result["attempt"] == 2
    assert exhausted_result["last_error"] == "permanent install failure"
    assert ts_engine_pg.get_run(pg_conn, run_id)["state"] == "failed"


def test_claim_next_step_honors_phase_and_advances_one_step(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Claim Demo")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="WinPE only",
        kind="partition_disk",
        phase="winpe",
        position=0,
    )
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Full OS only",
        kind="install_qga",
        phase="full_os",
        position=1,
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn, sequence_version_id=version_id
    )

    full_os_claim = ts_engine_pg.claim_next_step(
        pg_conn, run_id=run_id, phase="full_os", agent_id="osd-1"
    )
    assert full_os_claim["kind"] == "install_qga"
    assert full_os_claim["state"] == "running"

    winpe_claim = ts_engine_pg.claim_next_step(
        pg_conn, run_id=run_id, phase="winpe", agent_id="winpe-1"
    )
    assert winpe_claim["kind"] == "partition_disk"
    assert winpe_claim["state"] == "running"

    assert ts_engine_pg.claim_next_step(
        pg_conn, run_id=run_id, phase="winpe", agent_id="winpe-1"
    ) is None


def test_claim_next_step_does_not_skip_running_same_phase_step(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Ordered Full OS")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="First",
        kind="first_full_os",
        phase="full_os",
        position=0,
    )
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Second",
        kind="second_full_os",
        phase="full_os",
        position=1,
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn, sequence_version_id=version_id
    )

    first = ts_engine_pg.claim_next_step(
        pg_conn, run_id=run_id, phase="full_os", agent_id="agent-1"
    )
    assert first["kind"] == "first_full_os"

    assert ts_engine_pg.claim_next_step(
        pg_conn, run_id=run_id, phase="full_os", agent_id="agent-2"
    ) is None

    ts_engine_pg.complete_step(
        pg_conn,
        run_id=run_id,
        step_id=first["id"],
        agent_id="agent-1",
        status="success",
    )
    second = ts_engine_pg.claim_next_step(
        pg_conn, run_id=run_id, phase="full_os", agent_id="agent-2"
    )
    assert second["kind"] == "second_full_os"


def test_claim_next_step_filters_by_supported_kinds(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Capability Filter")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Role step",
        kind="configure_file_server_role",
        phase="full_os",
        position=0,
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn, sequence_version_id=version_id
    )

    assert ts_engine_pg.claim_next_step(
        pg_conn,
        run_id=run_id,
        phase="full_os",
        agent_id="osd-fullos",
        supported_kinds={"install_autopilot_agent"},
    ) is None

    claim = ts_engine_pg.claim_next_step(
        pg_conn,
        run_id=run_id,
        phase="full_os",
        agent_id="agent-role",
        supported_kinds={"configure_file_server_role"},
    )
    assert claim["kind"] == "configure_file_server_role"


def test_claim_next_step_stops_after_failed_same_phase_step(pg_conn):
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Failed Step Gate")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="First",
        kind="first_full_os",
        phase="full_os",
        position=0,
    )
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Second",
        kind="second_full_os",
        phase="full_os",
        position=1,
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn, sequence_version_id=version_id
    )
    first = ts_engine_pg.claim_next_step(
        pg_conn, run_id=run_id, phase="full_os", agent_id="agent-1"
    )
    ts_engine_pg.complete_step(
        pg_conn,
        run_id=run_id,
        step_id=first["id"],
        agent_id="agent-1",
        status="failed",
        message="boom",
    )

    assert ts_engine_pg.claim_next_step(
        pg_conn, run_id=run_id, phase="full_os", agent_id="agent-2"
    ) is None
