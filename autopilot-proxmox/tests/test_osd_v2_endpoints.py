from __future__ import annotations

import shutil
import subprocess
import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker is required for PostgreSQL-backed OSD v2 endpoint tests",
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


@pytest.fixture
def osd_v2_client(pg_dsn, monkeypatch):
    from web import app as web_app

    monkeypatch.setenv("AUTOPILOT_TS_ENGINE_DATABASE_URL", pg_dsn)
    return TestClient(web_app.app)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_run(pg_conn, *, winpe_only: bool = False) -> str:
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="OSD v2 Demo")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Partition Disk",
        kind="partition_disk",
        phase="winpe",
        position=0,
    )
    if not winpe_only:
        ts_engine_pg.add_step(
            pg_conn,
            sequence_id=sequence_id,
            parent_id=None,
            name="Install QGA",
            kind="install_qga",
            phase="full_os",
            position=1,
            content_refs=["qemu-guest-agent"],
        )
        item_id = ts_engine_pg.create_content_item(
            pg_conn, name="qemu-guest-agent", content_type="package"
        )
        ts_engine_pg.create_content_version(
            pg_conn,
            content_item_id=item_id,
            version="107.0",
            sha256="d" * 64,
            source_uri="https://content.local/qga-107.msi",
        )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn,
        sequence_version_id=version_id,
        deployment_target={"vmid": 119, "vm_uuid": "vm-119"},
    )
    ts_engine_pg.resolve_run_content_manifest(pg_conn, run_id)
    return run_id


def test_agent_register_next_logs_and_result_complete_step(osd_v2_client, pg_conn):
    from web import ts_engine_pg

    run_id = _create_run(pg_conn)
    reg = osd_v2_client.post(
        "/osd/v2/agent/register",
        json={
            "run_id": run_id,
            "agent_id": "winpe-1",
            "phase": "winpe",
            "build_sha": "dev",
            "capabilities": ["powershell"],
        },
    )
    assert reg.status_code == 200, reg.text
    token = reg.json()["bearer_token"]

    nxt = osd_v2_client.post(
        "/osd/v2/agent/next",
        json={"run_id": run_id, "agent_id": "winpe-1", "phase": "winpe"},
        headers=_bearer(token),
    )
    assert nxt.status_code == 200, nxt.text
    body = nxt.json()
    assert body["actions"][0]["kind"] == "partition_disk"
    assert body["actions"][0]["params"] == {}
    assert body["actions"][0]["content"] == []
    step_id = body["actions"][0]["step_id"]

    logs = osd_v2_client.post(
        f"/osd/v2/agent/step/{step_id}/logs",
        json={
            "run_id": run_id,
            "agent_id": "winpe-1",
            "stream": "stdout",
            "content": "disk partitioned",
        },
        headers=_bearer(token),
    )
    assert logs.status_code == 200, logs.text

    result = osd_v2_client.post(
        f"/osd/v2/agent/step/{step_id}/result",
        json={
            "run_id": run_id,
            "agent_id": "winpe-1",
            "phase": "winpe",
            "status": "success",
            "message": "ok",
        },
        headers=_bearer(token),
    )
    assert result.status_code == 200, result.text
    assert result.json()["step"]["state"] == "done"
    assert ts_engine_pg.list_run_steps(pg_conn, run_id)[0]["state"] == "done"


def test_full_os_agent_cannot_claim_winpe_only_step(osd_v2_client, pg_conn):
    run_id = _create_run(pg_conn, winpe_only=True)
    reg = osd_v2_client.post(
        "/osd/v2/agent/register",
        json={"run_id": run_id, "agent_id": "osd-1", "phase": "full_os"},
    ).json()

    nxt = osd_v2_client.post(
        "/osd/v2/agent/next",
        json={"run_id": run_id, "agent_id": "osd-1", "phase": "full_os"},
        headers=_bearer(reg["bearer_token"]),
    )

    assert nxt.status_code == 200, nxt.text
    assert nxt.json()["actions"] == []


def test_full_os_action_includes_manifest_content(osd_v2_client, pg_conn):
    run_id = _create_run(pg_conn)
    reg = osd_v2_client.post(
        "/osd/v2/agent/register",
        json={"run_id": run_id, "agent_id": "osd-1", "phase": "full_os"},
    ).json()

    nxt = osd_v2_client.post(
        "/osd/v2/agent/next",
        json={"run_id": run_id, "agent_id": "osd-1", "phase": "full_os"},
        headers=_bearer(reg["bearer_token"]),
    )

    assert nxt.status_code == 200, nxt.text
    action = nxt.json()["actions"][0]
    assert action["kind"] == "install_qga"
    assert action["content"] == [
        {
            "id": action["content"][0]["id"],
            "logical_name": "qemu-guest-agent",
            "content_type": "package",
            "version": "107.0",
            "sha256": "d" * 64,
            "source_uri": "https://content.local/qga-107.msi",
            "required_phase": "full_os",
            "staging_path": (
                "C:\\ProgramData\\ProxmoxVEAutopilot\\Content"
                "\\qemu-guest-agent\\107.0"
            ),
            "status": "pending",
        }
    ]


def test_agent_register_after_reboot_resumes_cursor(osd_v2_client, pg_conn):
    from web import ts_engine_pg

    run_id = _create_run(pg_conn)
    reg = osd_v2_client.post(
        "/osd/v2/agent/register",
        json={"run_id": run_id, "agent_id": "winpe-1", "phase": "winpe"},
    ).json()
    nxt = osd_v2_client.post(
        "/osd/v2/agent/next",
        json={"run_id": run_id, "agent_id": "winpe-1", "phase": "winpe"},
        headers=_bearer(reg["bearer_token"]),
    ).json()
    step_id = nxt["actions"][0]["step_id"]

    rebooting = osd_v2_client.post(
        "/osd/v2/agent/rebooting",
        json={
            "run_id": run_id,
            "agent_id": "winpe-1",
            "phase": "winpe",
            "step_id": step_id,
        },
        headers=_bearer(reg["bearer_token"]),
    )
    assert rebooting.status_code == 200, rebooting.text
    assert ts_engine_pg.get_run(pg_conn, run_id)["state"] == "awaiting_reboot"

    resumed = osd_v2_client.post(
        "/osd/v2/agent/register",
        json={"run_id": run_id, "agent_id": "osd-1", "phase": "full_os"},
        headers=_bearer(reg["bearer_token"]),
    )

    assert resumed.status_code == 200, resumed.text
    assert ts_engine_pg.get_run(pg_conn, run_id)["state"] == "queued"
    assert ts_engine_pg.list_run_steps(pg_conn, run_id)[0]["state"] == "done"


def test_agent_token_cannot_operate_on_another_run(osd_v2_client, pg_conn):
    run_1 = _create_run(pg_conn, winpe_only=True)
    run_2 = _create_run(pg_conn, winpe_only=True)
    reg = osd_v2_client.post(
        "/osd/v2/agent/register",
        json={"run_id": run_1, "agent_id": "winpe-1", "phase": "winpe"},
    ).json()

    nxt = osd_v2_client.post(
        "/osd/v2/agent/next",
        json={"run_id": run_2, "agent_id": "winpe-2", "phase": "winpe"},
        headers=_bearer(reg["bearer_token"]),
    )

    assert nxt.status_code == 403


def test_step_result_for_wrong_phase_is_rejected(osd_v2_client, pg_conn):
    run_id = _create_run(pg_conn, winpe_only=True)
    reg = osd_v2_client.post(
        "/osd/v2/agent/register",
        json={"run_id": run_id, "agent_id": "winpe-1", "phase": "winpe"},
    ).json()
    nxt = osd_v2_client.post(
        "/osd/v2/agent/next",
        json={"run_id": run_id, "agent_id": "winpe-1", "phase": "winpe"},
        headers=_bearer(reg["bearer_token"]),
    ).json()
    step_id = nxt["actions"][0]["step_id"]

    result = osd_v2_client.post(
        f"/osd/v2/agent/step/{step_id}/result",
        json={
            "run_id": run_id,
            "agent_id": "osd-1",
            "phase": "full_os",
            "status": "success",
        },
        headers=_bearer(reg["bearer_token"]),
    )

    assert result.status_code == 409


def test_auth_exempts_osd_v2_machine_callbacks():
    from web import auth

    for path in (
        "/osd/v2/agent/register",
        "/osd/v2/agent/next",
        "/osd/v2/agent/step/1/result",
        "/osd/v2/agent/step/1/logs",
        "/osd/v2/agent/rebooting",
        "/osd/v2/agent/phase-complete",
        "/osd/v2/content/1",
    ):
        assert auth.is_exempt_path(path)
