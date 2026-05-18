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


CONFIGMGR_WINPE_STEPS = [
    ("Partition Disk", "partition_disk", {}),
    ("Apply Windows Image", "apply_wim", {
        "image_index_metadata_name": "Windows 11 Enterprise",
    }),
    ("Apply Driver Package", "apply_driver_package", {
        "architecture": "amd64",
        "required_infs": [
            "vioscsi.inf",
            "viostor.inf",
            "netkvm.inf",
            "vioser.inf",
            "balloon.inf",
        ],
        "optional": True,
    }),
    ("Prepare Windows Setup", "prepare_windows_setup", {}),
    ("Stage OSD Client", "stage_osd_client", {}),
]


def test_osdeploy_agent_msi_resolves_from_host_repo_mount(tmp_path, monkeypatch):
    from web import osdeploy_endpoints

    host_repo = tmp_path / "host-repo"
    msi_path = host_repo / "autopilot-agent" / "artifacts" / "AutopilotAgent.msi"
    msi_path.parent.mkdir(parents=True)
    msi_path.write_bytes(b"fake-msi")
    monkeypatch.delenv("AUTOPILOT_AGENT_MSI_PATH", raising=False)
    monkeypatch.setenv("HOST_REPO_MOUNT", str(host_repo))
    monkeypatch.setattr(osdeploy_endpoints, "_APP_ROOT", tmp_path / "app")
    monkeypatch.setattr(osdeploy_endpoints, "_REPO_ROOT", tmp_path / "repo")

    assert osdeploy_endpoints._asset_path("autopilotagent.msi") == msi_path


def test_osdeploy_agent_msi_prefers_setup_registry_over_legacy_output(tmp_path, monkeypatch):
    from web import osdeploy_endpoints, setup_artifacts

    legacy_msi = tmp_path / "app" / "output" / "cloudosd" / "AutopilotAgent.msi"
    legacy_msi.parent.mkdir(parents=True)
    legacy_msi.write_bytes(b"legacy-msi")
    setup_msi = tmp_path / "setup-artifacts" / "agent-msi" / "AutopilotAgent-0.1.2-win-x64.msi"
    setup_msi.parent.mkdir(parents=True)
    setup_msi.write_bytes(b"current-msi")

    monkeypatch.delenv("AUTOPILOT_AGENT_MSI_PATH", raising=False)
    monkeypatch.setattr(osdeploy_endpoints, "_APP_ROOT", tmp_path / "app")
    monkeypatch.setattr(osdeploy_endpoints, "_REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(
        setup_artifacts,
        "list_artifacts",
        lambda kind=None: [{
            "kind": "agent-msi",
            "filename": setup_msi.name,
            "path": str(setup_msi),
        }],
    )

    assert osdeploy_endpoints._asset_path("autopilotagent.msi") == setup_msi


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

    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    monkeypatch.setenv("AUTOPILOT_TS_ENGINE_DATABASE_URL", pg_dsn)
    return TestClient(web_app.app)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_database_url_prefers_autopilot_database_url(monkeypatch):
    from web import osd_v2_endpoints

    monkeypatch.setenv("AUTOPILOT_TS_ENGINE_DATABASE_URL", "postgresql://old")
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", "postgresql://new")

    assert osd_v2_endpoints._database_url() == "postgresql://new"


def test_v2_sequence_create_accepts_target_os_ubuntu(osd_v2_client, pg_conn):
    r = osd_v2_client.post(
        "/api/osd/v2/sequences",
        json={"name": "Ubuntu v2", "description": "", "target_os": "ubuntu"},
    )

    assert r.status_code == 201, r.text
    assert r.json()["target_os"] == "ubuntu"


def test_ubuntu_v2_run_blocks_windows_only_steps(osd_v2_client, pg_conn):
    seq = osd_v2_client.post(
        "/api/osd/v2/sequences",
        json={"name": "Bad Ubuntu v2", "target_os": "ubuntu"},
    ).json()
    step = osd_v2_client.post(
        f"/api/osd/v2/sequences/{seq['id']}/steps",
        json={
            "name": "Apply Windows Image",
            "kind": "apply_wim",
            "phase": "pe",
            "position": 0,
        },
    )
    assert step.status_code == 201, step.text

    run = osd_v2_client.post(f"/api/osd/v2/sequences/{seq['id']}/runs", json={})

    assert run.status_code == 400
    assert "incompatible enabled step" in run.json()["detail"]

    compile_resp = osd_v2_client.post(f"/api/osd/v2/sequences/{seq['id']}/compile", json={})
    assert compile_resp.status_code == 400
    assert "incompatible enabled step" in compile_resp.json()["detail"]


def _create_run(
    pg_conn,
    *,
    winpe_only: bool = False,
    reboot_behavior: str = "none",
    retry_count: int = 0,
    retry_delay_seconds: int = 10,
) -> str:
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="OSD v2 Demo")
    for position, (name, kind, params) in enumerate(CONFIGMGR_WINPE_STEPS):
        ts_engine_pg.add_step(
            pg_conn,
            sequence_id=sequence_id,
            parent_id=None,
            name=name,
            kind=kind,
            phase="winpe",
            position=position,
            params=params,
        )
    if not winpe_only:
        ts_engine_pg.add_step(
            pg_conn,
            sequence_id=sequence_id,
            parent_id=None,
            name="Install QGA",
            kind="install_qga",
            phase="full_os",
            position=len(CONFIGMGR_WINPE_STEPS),
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            reboot_behavior=reboot_behavior,
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
            metadata={
                "install_command": (
                    "msiexec.exe /i {path} /qn /norestart"
                )
            },
        )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn,
        sequence_version_id=version_id,
        deployment_target={"vmid": 119, "vm_uuid": "vm-119"},
    )
    ts_engine_pg.resolve_run_content_manifest(pg_conn, run_id)
    return run_id


def test_v2_agent_package_returns_server_authored_config(osd_v2_client, pg_conn):
    run_id = _create_run(pg_conn, winpe_only=False)
    reg = osd_v2_client.post(
        "/osd/v2/agent/register",
        json={"run_id": run_id, "agent_id": "osd-1", "phase": "full_os"},
    )
    assert reg.status_code == 200, reg.text
    response = osd_v2_client.get(
        f"/osd/v2/agent/package/{run_id}?phase=full_os",
        headers=_bearer(reg.json()["bearer_token"]),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == 2
    assert body["engine"] == "v2"
    assert body["api_version"] == 2
    assert body["run_id"] == run_id
    assert body["phase"] == "full_os"
    assert body["agent_id"].startswith("osd-fullos-")
    assert body["bearer_token"]
    assert body["config_path"] == (
        r"V:\ProgramData\ProxmoxVEAutopilot\OSD\osd-config.json"
    )
    assert body["config"]["engine"] == "v2"
    assert body["config"]["api_version"] == 2
    assert body["config"]["run_id"] == run_id
    assert body["config"]["phase"] == "full_os"
    assert body["config"]["agent_id"].startswith("osd-fullos-")
    assert body["config"]["bearer_token"]
    assert body["config"]["flask_base_url"] == ""
    assert any(file["path"].endswith("OsdClient.ps1") for file in body["files"])
    for file in body["files"]:
        assert file["sha256"]
        assert len(file["sha256"]) == 64
        assert file["size_bytes"] > 0


def test_osdeploy_v2_agent_package_is_self_contained_for_full_os(
    osd_v2_client,
    pg_conn,
    monkeypatch,
):
    from web import osdeploy_endpoints, osdeploy_pg

    osdeploy_pg.reset_for_tests(pg_conn)
    osdeploy_pg.init(pg_conn)
    monkeypatch.setenv("AUTOPILOT_BASE_URL", "http://controller.local:5000")
    monkeypatch.setattr(
        osdeploy_endpoints,
        "_asset_metadata",
        lambda name, required=True: {
            "sha256": f"sha-{name}",
            "available": True,
        },
        raising=False,
    )
    artifact = osdeploy_pg.create_artifact(
        pg_conn,
        build_sha="selfcontained",
        iso_path="/app/output/osdeploy.iso",
        wim_path="/app/output/osdeploy.wim",
        manifest_path="/app/output/osdeploy.json",
        iso_sha256="a" * 64,
        wim_sha256="b" * 64,
        source_media="/isos/windows-server.iso",
        image_name="Windows Server 2025 Standard",
        image_index=1,
        built_by_host="builder",
        proxmox_volid="isos:iso/osdeploy.iso",
    )
    run = osdeploy_pg.create_run(
        pg_conn,
        artifact_id=artifact["id"],
        vm_name="OSDeploy-FS-01",
        requested_vmid=9101,
        server_role="file_server",
        role_options={
            "share_name": "Data",
            "share_path": r"C:\Shares\Data",
            "full_access_principals": ["Administrators"],
            "change_access_principals": ["Authenticated Users"],
            "read_access_principals": ["Everyone"],
        },
    )
    reg = osd_v2_client.post(
        "/osd/v2/agent/register",
        json={"run_id": run["run_id"], "agent_id": "osd-1", "phase": "full_os"},
    )
    assert reg.status_code == 200, reg.text

    response = osd_v2_client.get(
        f"/osd/v2/agent/package/{run['run_id']}?phase=full_os",
        headers=_bearer(reg.json()["bearer_token"]),
    )

    assert response.status_code == 200, response.text
    config = response.json()["config"]
    assert config["flask_base_url"] == "http://controller.local:5000"
    assert config["osdeploy_agent"]["phase"] == "full_os"
    assert config["osdeploy_agent"]["role"] == "file_server"
    assert config["osdeploy_agent"]["run_id"] == run["run_id"]
    assert config["autopilotagent_msi"] == {
        "url": "http://controller.local:5000/api/cloudosd/assets/autopilotagent.msi",
        "sha256": "sha-autopilotagent.msi",
        "available": True,
    }
    assert config["autopilotagent_postinstall"]["url"].endswith(
        "/api/cloudosd/assets/autopilotagent-postinstall.ps1"
    )


def test_v2_agent_package_requires_bearer(osd_v2_client, pg_conn):
    run_id = _create_run(pg_conn, winpe_only=False)

    missing = osd_v2_client.get(
        f"/osd/v2/agent/package/{run_id}?phase=full_os"
    )
    assert missing.status_code == 401
    assert missing.json()["detail"] == "missing bearer"

    invalid = osd_v2_client.get(
        f"/osd/v2/agent/package/{run_id}?phase=full_os",
        headers=_bearer("not-a-token"),
    )
    assert invalid.status_code == 401
    assert invalid.json()["detail"] == "invalid token"


def test_v2_agent_package_rejects_token_for_another_run(osd_v2_client, pg_conn):
    run_id = _create_run(pg_conn, winpe_only=True)
    other_run_id = _create_run(pg_conn, winpe_only=True)
    reg = osd_v2_client.post(
        "/osd/v2/agent/register",
        json={
            "run_id": other_run_id,
            "agent_id": "osd-1",
            "phase": "full_os",
        },
    )
    assert reg.status_code == 200, reg.text

    response = osd_v2_client.get(
        f"/osd/v2/agent/package/{run_id}?phase=full_os",
        headers=_bearer(reg.json()["bearer_token"]),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "token/run mismatch"


def test_v2_agent_hash_persists_hash_for_uuid_run(
    osd_v2_client,
    pg_conn,
    monkeypatch,
    tmp_path,
):
    from web import app as web_app
    from web import winpe_token

    monkeypatch.setattr(web_app, "HASH_DIR", tmp_path)
    run_id = _create_run(pg_conn, winpe_only=True)
    token = winpe_token.sign(run_id=run_id, ttl_seconds=60)

    response = osd_v2_client.post(
        "/osd/v2/agent/hash",
        headers=_bearer(token),
        json={
            "serial_number": "CLOUDOSD-SERIAL",
            "product_id": "PRODUCT-ID",
            "hardware_hash": "HASH-VALUE",
            "group_tag": "GellNative",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["hash_filename"].endswith("-osd-v2_hwid.csv")
    files = list(tmp_path.glob("*.csv"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "Device Serial Number,Windows Product ID,Hardware Hash" in text
    assert "Group Tag" in text
    assert "CLOUDOSD-SERIAL,PRODUCT-ID,HASH-VALUE,GellNative" in text
    assert "-vm119-" in files[0].name
    assert files[0].name.endswith("-osd-v2_hwid.csv")


def test_cloudosd_v2_agent_hash_queues_autopilot_upload(
    osd_v2_client,
    pg_conn,
    monkeypatch,
    tmp_path,
):
    from web import app as web_app
    from web import cloudosd_pg, jobs_pg, winpe_token

    jobs_pg.reset_for_tests(pg_conn)
    jobs_pg.init(pg_conn)
    cloudosd_pg.reset_for_tests(pg_conn)
    cloudosd_pg.init(pg_conn)
    monkeypatch.setattr(web_app, "HASH_DIR", tmp_path)
    artifact = cloudosd_pg.create_artifact(
        pg_conn,
        architecture="amd64",
        osdcloud_module_version="26.4.17.1",
        build_sha="osdv2autopilot",
        iso_path="/app/output/cloudosd-autopilot-amd64-osdv2autopilot.iso",
        wim_path="/app/output/cloudosd-autopilot-amd64-osdv2autopilot.wim",
        manifest_path="/app/output/cloudosd-autopilot-amd64-osdv2autopilot.json",
        iso_sha256="a" * 64,
        wim_sha256="b" * 64,
        built_by_host="Adam.Gell@10.211.55.6",
        proxmox_volid="local:iso/cloudosd-autopilot-amd64-osdv2autopilot.iso",
    )
    run = cloudosd_pg.create_run(
        pg_conn,
        artifact_id=artifact["id"],
        vm_name="Gell-247-AP1",
        node="pve",
        storage="local-lvm",
        network_bridge="vmbr0",
        vm_group_tag="GellNative",
    )
    cloudosd_pg.set_run_identity(
        pg_conn,
        run_id=run["run_id"],
        vmid=247,
        vm_uuid="dddddddd-bbbb-cccc-dddd-eeeeeeeeeeee",
        mac="52:54:00:aa:bb:f2",
        node="pve",
        computer_name="Gell-247-AP1",
    )
    token = winpe_token.sign(run_id=run["run_id"], ttl_seconds=60)

    response = osd_v2_client.post(
        "/osd/v2/agent/hash",
        headers=_bearer(token),
        json={
            "serial_number": "Gell-247-AP1",
            "product_id": "PRODUCT-ID",
            "hardware_hash": "HASH-VALUE",
            "group_tag": "GellNative",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["hash_filename"]
    assert body["autopilot_upload"]["queued"] is True
    assert body["autopilot_upload"]["job_id"]
    files = list(tmp_path.glob("*.csv"))
    assert len(files) == 1
    job = jobs_pg.get_job(body["autopilot_upload"]["job_id"])
    assert job["job_type"] == "upload_hash"
    assert job["args"]["cloudosd_run_id"] == run["run_id"]
    assert job["args"]["vmid"] == 247
    assert job["args"]["file"] == files[0].name
    assert job["args"]["file"] == body["hash_filename"]
    assert job["args"]["group_tag"] == "GellNative"


def test_osdeploy_package_includes_persistent_agent_install_payloads(monkeypatch):
    from web import osdeploy_endpoints

    def fake_asset_metadata(name: str, *, required: bool = True):
        return {
            "sha256": f"sha-{name}",
            "available": name != "autopilotagent.msi" or required is False,
        }

    monkeypatch.setattr(
        osdeploy_endpoints,
        "_asset_metadata",
        fake_asset_metadata,
        raising=False,
    )
    run = {
        "run_id": "11111111-2222-3333-4444-555555555555",
        "workflow_name": "pveautopilot-osdeploy-test",
        "vmid": 102,
        "vm_uuid": "vm-102",
        "mac": "52:54:00:aa:bb:cc",
        "node": "pve",
        "requested_vm_name": "AUTOPILOT-E2E-01",
        "vm_name": "AUTOPILOT-E2E-01",
        "pve_vm_name": "AUTOPILOT-E2E-01",
        "expected_computer_name": "AUTOPILOT-E2E",
        "server_role": "base",
        "os_version": "Windows 11",
        "os_edition": "Enterprise",
        "os_language": "en-us",
        "secure_boot": True,
        "outbound_policy": {},
    }
    artifact = {
        "id": "artifact-1",
        "image_index": 1,
        "iso_sha256": "a" * 64,
        "wim_sha256": "b" * 64,
        "proxmox_volid": "local:iso/osdeploy.iso",
    }

    body = osdeploy_endpoints._package_response(
        run=run,
        artifact=artifact,
        server_base_url="http://autopilot.test:5000",
    )

    assert body["payloads"]["autopilotagent_msi"] == {
        "url": "http://autopilot.test:5000/api/cloudosd/assets/autopilotagent.msi",
        "sha256": "sha-autopilotagent.msi",
        "available": True,
    }
    assert body["payloads"]["autopilotagent_postinstall"] == {
        "url": (
            "http://autopilot.test:5000/api/cloudosd/assets/"
            "autopilotagent-postinstall.ps1"
        ),
        "sha256": "sha-autopilotagent-postinstall.ps1",
    }
    assert body["agent"]["phase"] == "full_os"
    assert body["agent"]["role"] == "base"
    assert body["agent"]["agent_id"] == "agent-autopilot-e2e"


def test_osdeploy_task_engine_sequence_installs_agent_before_heartbeat(pg_conn):
    from web import osdeploy_pg, ts_engine_pg

    osdeploy_pg.reset_for_tests(pg_conn)
    osdeploy_pg.init(pg_conn)
    artifact = osdeploy_pg.create_artifact(
        pg_conn,
        build_sha="agentinstall",
        iso_path="/app/output/osdeploy-server-amd64-agentinstall.iso",
        wim_path="/app/output/osdeploy-server-amd64-agentinstall.wim",
        manifest_path="/app/output/osdeploy-server-amd64-agentinstall.json",
        iso_sha256="a" * 64,
        wim_sha256="b" * 64,
        source_media="/isos/windows.iso",
        image_name="Windows 11 Enterprise Evaluation",
        image_index=1,
        os_version="Windows 11",
        os_edition="Enterprise Evaluation",
        built_by_host="buildhost-101",
        proxmox_volid="local:iso/osdeploy-server-amd64-agentinstall.iso",
    )

    run = osdeploy_pg.create_run(
        pg_conn,
        artifact_id=artifact["id"],
        vm_name="AUTOPILOT-E2E-01",
        os_version="Windows 11",
        os_edition="Enterprise Evaluation",
    )

    kinds = [step["kind"] for step in ts_engine_pg.list_run_steps(pg_conn, run["run_id"])]
    assert kinds == [
        "osdeploy_preflight",
        "apply_wim",
        "apply_driver_package",
        "prepare_windows_setup",
        "bake_boot_entry",
        "stage_osd_client",
        "stage_autopilot_agent",
        "install_autopilot_agent",
        "wait_agent_heartbeat",
    ]
    assert kinds.index("install_autopilot_agent") < kinds.index("wait_agent_heartbeat")


@pytest.mark.parametrize(
    "path",
    [
        "/winpe/unattend/{run_id}",
        "/winpe/autopilot-config/{run_id}",
    ],
)
def test_legacy_winpe_payload_routes_reject_v2_uuid_runs_after_auth(
    osd_v2_client,
    pg_conn,
    monkeypatch,
    path,
):
    from web import winpe_token

    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "test-token-secret")
    run_id = _create_run(pg_conn, winpe_only=True)
    token = winpe_token.sign(run_id=run_id, ttl_seconds=60)

    response = osd_v2_client.get(
        path.format(run_id=run_id),
        headers=_bearer(token),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "legacy WinPE payload route does not support v2 UUID runs"
    )


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


def test_winpe_agent_next_exposes_configmgr_osd_spine(osd_v2_client, pg_conn):
    run_id = _create_run(pg_conn)
    reg = osd_v2_client.post(
        "/osd/v2/agent/register",
        json={"run_id": run_id, "agent_id": "winpe-1", "phase": "winpe"},
    ).json()

    nxt = osd_v2_client.post(
        "/osd/v2/agent/next",
        json={
            "run_id": run_id,
            "agent_id": "winpe-1",
            "phase": "winpe",
            "batch_size": 10,
        },
        headers=_bearer(reg["bearer_token"]),
    )

    assert nxt.status_code == 200, nxt.text
    actions = nxt.json()["actions"]
    assert [action["kind"] for action in actions] == [
        kind for _, kind, _ in CONFIGMGR_WINPE_STEPS
    ]
    assert actions[1]["params"]["image_index_metadata_name"] == (
        "Windows 11 Enterprise"
    )
    assert actions[2]["params"]["required_infs"][:2] == [
        "vioscsi.inf",
        "viostor.inf",
    ]


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
    assert action["phase"] == "full_os"
    assert action["reboot_behavior"] == "none"
    assert action["retry_count"] == 0
    assert action["retry_delay_seconds"] == 10
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
            "metadata": {
                "install_command": (
                    "msiexec.exe /i {path} /qn /norestart"
                )
            },
        }
    ]


def test_agent_can_report_content_staging_status(osd_v2_client, pg_conn):
    from web import ts_engine_pg

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
    manifest_id = nxt.json()["actions"][0]["content"][0]["id"]

    staging = osd_v2_client.post(
        f"/osd/v2/agent/content/{manifest_id}/stage",
        json={
            "run_id": run_id,
            "agent_id": "osd-1",
            "phase": "full_os",
            "status": "staging",
            "staging_path": (
                "C:\\ProgramData\\ProxmoxVEAutopilot\\Content"
                "\\qemu-guest-agent\\107.0"
            ),
        },
        headers=_bearer(reg["bearer_token"]),
    )
    assert staging.status_code == 200, staging.text
    assert staging.json()["status"] == "staging"
    assert staging.json()["staging_attempts"] == 1

    staged = osd_v2_client.post(
        f"/osd/v2/agent/content/{manifest_id}/stage",
        json={
            "run_id": run_id,
            "agent_id": "osd-1",
            "phase": "full_os",
            "status": "staged",
        },
        headers=_bearer(reg["bearer_token"]),
    )
    assert staged.status_code == 200, staged.text
    assert staged.json()["status"] == "staged"
    assert staged.json()["staged_at"] is not None
    assert ts_engine_pg.list_run_manifest(pg_conn, run_id)[0]["status"] == "staged"


def test_agent_can_fetch_full_content_manifest(osd_v2_client, pg_conn):
    run_id = _create_run(pg_conn)
    reg = osd_v2_client.post(
        "/osd/v2/agent/register",
        json={"run_id": run_id, "agent_id": "osd-1", "phase": "full_os"},
    ).json()

    manifest = osd_v2_client.get(
        f"/osd/v2/agent/content-manifest/{run_id}",
        headers=_bearer(reg["bearer_token"]),
    )

    assert manifest.status_code == 200, manifest.text
    body = manifest.json()
    assert body["schema_version"] == 1
    assert body["run_id"] == run_id
    assert body["items"][0]["logical_name"] == "qemu-guest-agent"
    assert body["items"][0]["sha256"] == "d" * 64
    assert body["items"][0]["metadata"]["install_command"].startswith(
        "msiexec.exe"
    )


def test_operator_can_fetch_run_content_manifest(osd_v2_client, pg_conn):
    run_id = _create_run(pg_conn)

    manifest = osd_v2_client.get(
        f"/api/osd/v2/runs/{run_id}/content-manifest",
    )

    assert manifest.status_code == 200, manifest.text
    body = manifest.json()
    assert body["schema_version"] == 1
    assert body["run_id"] == run_id
    assert body["items"][0]["logical_name"] == "qemu-guest-agent"


def test_operator_can_fetch_run_content_staging_status(osd_v2_client, pg_conn):
    from web import ts_engine_pg

    run_id = _create_run(pg_conn)
    manifest_id = str(
        pg_conn.execute(
            "SELECT id FROM ts_run_content_manifest WHERE run_id = %s",
            (run_id,),
        ).fetchone()["id"]
    )
    ts_engine_pg.mark_manifest_item_staging(
        pg_conn,
        manifest_id=manifest_id,
        run_id=run_id,
        status="failed",
        agent_id="osd-1",
        error="download timed out",
    )

    status = osd_v2_client.get(
        f"/api/osd/v2/runs/{run_id}/content-staging",
    )

    assert status.status_code == 200, status.text
    body = status.json()
    assert body["schema_version"] == 1
    assert body["run_id"] == run_id
    assert body["items"] == [
        {
            "id": manifest_id,
            "logical_name": "qemu-guest-agent",
            "content_type": "package",
            "version": "107.0",
            "status": "failed",
            "staging_path": (
                "C:\\ProgramData\\ProxmoxVEAutopilot\\Content"
                "\\qemu-guest-agent\\107.0"
            ),
            "staging_attempts": 0,
            "staged_by": "osd-1",
            "staged_at": None,
            "last_error": "download timed out",
        }
    ]


def test_action_includes_required_reboot_behavior(osd_v2_client, pg_conn):
    run_id = _create_run(pg_conn, reboot_behavior="required")
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
    assert nxt.json()["actions"][0]["reboot_behavior"] == "required"


def test_action_includes_retry_policy(osd_v2_client, pg_conn):
    run_id = _create_run(pg_conn, retry_count=2, retry_delay_seconds=30)
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
    assert action["retry_count"] == 2
    assert action["retry_delay_seconds"] == 30


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
    pg_conn.execute(
        "UPDATE ts_provisioning_runs SET state = 'role_pending' WHERE id = %s",
        (run_id,),
    )
    pg_conn.commit()

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


def test_content_api_creates_lists_and_versions(osd_v2_client, pg_conn):
    item = osd_v2_client.post(
        "/api/osd/v2/content/items",
        json={
            "name": "notepad-plus-plus",
            "content_type": "package",
            "description": "Notepad++ installer",
        },
    )
    assert item.status_code == 201, item.text
    item_body = item.json()
    assert item_body["name"] == "notepad-plus-plus"
    assert item_body["content_type"] == "package"
    assert item_body["latest_version"] is None

    version = osd_v2_client.post(
        f"/api/osd/v2/content/items/{item_body['id']}/versions",
        json={
            "version": "8.6.7",
            "sha256": "f" * 64,
            "size_bytes": 4096,
            "source_uri": "https://content.local/npp.8.6.7.x64.msi",
            "metadata": {"install_command": "msiexec.exe /i npp.msi /qn"},
        },
    )
    assert version.status_code == 201, version.text
    assert version.json()["version"] == "8.6.7"
    assert version.json()["metadata"] == {
        "install_command": "msiexec.exe /i npp.msi /qn"
    }

    listing = osd_v2_client.get("/api/osd/v2/content/items")
    assert listing.status_code == 200, listing.text
    assert listing.json()["items"] == [
        {
            "id": item_body["id"],
            "name": "notepad-plus-plus",
            "content_type": "package",
            "description": "Notepad++ installer",
            "enabled": True,
            "latest_version": {
                "id": version.json()["id"],
                "version": "8.6.7",
                "sha256": "f" * 64,
                "size_bytes": 4096,
                "source_uri": "https://content.local/npp.8.6.7.x64.msi",
                "metadata": {"install_command": "msiexec.exe /i npp.msi /qn"},
            },
        }
    ]


def test_content_manifest_api_returns_v1_manifest(osd_v2_client, pg_conn):
    item = osd_v2_client.post(
        "/api/content/items",
        json={
            "name": "qemu-guest-agent",
            "content_type": "package",
            "description": "QGA MSI",
        },
    )
    assert item.status_code == 201, item.text

    version = osd_v2_client.post(
        f"/api/content/items/{item.json()['id']}/versions",
        json={
            "version": "107.0",
            "sha256": "D" * 64,
            "size_bytes": 2048,
            "source_uri": "https://content.local/qga-107.msi",
            "architecture": "x64",
            "target_os": "windows",
            "reboot_behavior": "deferred",
            "conditions": {"phase": "full_os"},
            "metadata": {"install_command": "msiexec.exe /i {path} /qn"},
        },
    )
    assert version.status_code == 201, version.text

    manifest = osd_v2_client.get("/api/content/manifest")

    assert manifest.status_code == 200, manifest.text
    body = manifest.json()
    assert body["schema_version"] == 1
    assert body["item_count"] == 1
    assert len(body["digest"]) == 64
    assert body["manifest"] == {
        "schema_version": 1,
        "items": [
            {
                "id": "qemu-guest-agent",
                "kind": "package",
                "name": "qemu-guest-agent",
                "version": "107.0",
                "source_uri": "https://content.local/qga-107.msi",
                "sha256": "d" * 64,
                "size_bytes": 2048,
                "architecture": "x64",
                "target_os": "windows",
                "reboot_behavior": "deferred",
                "conditions": {"phase": "full_os"},
                "metadata": {"install_command": "msiexec.exe /i {path} /qn"},
            }
        ],
    }


def test_sequence_api_creates_package_run_with_resolved_content(osd_v2_client, pg_conn):
    item = osd_v2_client.post(
        "/api/content/items",
        json={
            "name": "notepad-plus-plus",
            "content_type": "package",
            "description": "Notepad++ installer",
        },
    )
    assert item.status_code == 201, item.text
    version = osd_v2_client.post(
        f"/api/content/items/{item.json()['id']}/versions",
        json={
            "version": "8.6.7",
            "sha256": "f" * 64,
            "size_bytes": 4096,
            "source_uri": "https://content.local/npp.8.6.7.x64.msi",
            "metadata": {"install_command": "msiexec.exe /i {path} /qn"},
        },
    )
    assert version.status_code == 201, version.text

    sequence = osd_v2_client.post(
        "/api/osd/v2/sequences",
        json={"name": "Install Apps", "description": "Package smoke"},
    )
    assert sequence.status_code == 201, sequence.text
    step = osd_v2_client.post(
        f"/api/osd/v2/sequences/{sequence.json()['id']}/steps",
        json={
            "name": "Install Notepad++",
            "kind": "install_package",
            "phase": "full_os",
            "position": 0,
            "params": {"install_command": "msiexec.exe /i {path} /qn"},
            "content_refs": ["notepad-plus-plus"],
            "retry_count": 1,
            "retry_delay_seconds": 5,
        },
    )
    assert step.status_code == 201, step.text
    assert step.json()["content_refs"] == ["notepad-plus-plus"]

    run = osd_v2_client.post(
        f"/api/osd/v2/sequences/{sequence.json()['id']}/runs",
        json={
            "resolve_content": True,
            "deployment_target": {"vmid": 121, "vm_uuid": "vm-121"},
        },
    )
    assert run.status_code == 201, run.text
    assert run.json()["content_items"] == 1

    reg = osd_v2_client.post(
        "/osd/v2/agent/register",
        json={
            "run_id": run.json()["run_id"],
            "agent_id": "osd-1",
            "phase": "full_os",
        },
    ).json()
    nxt = osd_v2_client.post(
        "/osd/v2/agent/next",
        json={
            "run_id": run.json()["run_id"],
            "agent_id": "osd-1",
            "phase": "full_os",
        },
        headers=_bearer(reg["bearer_token"]),
    )
    assert nxt.status_code == 200, nxt.text
    action = nxt.json()["actions"][0]
    assert action["kind"] == "install_package"
    assert action["retry_count"] == 1
    assert action["retry_delay_seconds"] == 5
    assert action["content"] == [
        {
            "id": action["content"][0]["id"],
            "logical_name": "notepad-plus-plus",
            "content_type": "package",
            "version": "8.6.7",
            "sha256": "f" * 64,
            "source_uri": "https://content.local/npp.8.6.7.x64.msi",
            "required_phase": "full_os",
            "staging_path": (
                "C:\\ProgramData\\ProxmoxVEAutopilot\\Content"
                "\\notepad-plus-plus\\8.6.7"
            ),
            "status": "pending",
            "metadata": {"install_command": "msiexec.exe /i {path} /qn"},
        }
    ]


def test_content_api_rejects_invalid_sha(osd_v2_client, pg_conn):
    item = osd_v2_client.post(
        "/api/osd/v2/content/items",
        json={"name": "bad-hash-app", "content_type": "package"},
    )
    assert item.status_code == 201, item.text

    version = osd_v2_client.post(
        f"/api/osd/v2/content/items/{item.json()['id']}/versions",
        json={
            "version": "1.0",
            "sha256": "not-a-sha",
            "source_uri": "https://content.local/bad.msi",
        },
    )

    assert version.status_code == 422


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
        "/osd/v2/agent/content/1/stage",
    ):
        assert auth.is_exempt_path(path)
