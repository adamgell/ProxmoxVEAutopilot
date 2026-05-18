from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import shutil

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker is required for PostgreSQL-backed OSDeploy endpoint tests",
)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def osdeploy_client(pg_conn, monkeypatch):
    from web import agent_telemetry_pg, jobs_pg, osdeploy_cache, osdeploy_pg, sequences_pg, ts_engine_pg

    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)
    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    agent_telemetry_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)
    jobs_pg.reset_for_tests(pg_conn)
    jobs_pg.init(pg_conn)
    osdeploy_cache.reset_for_tests(pg_conn)
    osdeploy_cache.init(pg_conn)
    osdeploy_pg.reset_for_tests(pg_conn)
    osdeploy_pg.init(pg_conn)
    monkeypatch.setenv("AUTOPILOT_BASE_URL", "http://autopilot.test:5000")

    from web import app as web_app

    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "proxmox_node": "pve",
        "proxmox_iso_storage": "local",
        "proxmox_storage": "local-lvm",
        "proxmox_bridge": "vmbr0",
        "proxmox_virtio_iso": "local:iso/virtio-win.iso",
    })

    def fake_proxmox_api(path, *args, **kwargs):
        values = {
            "/cluster/nextid": 100,
            "/cluster/resources?type=vm": [],
            "/nodes": [{"node": "pve"}],
            "/storage": [
                {"storage": "local", "content": "iso"},
                {"storage": "local-lvm", "content": "images"},
            ],
            "/nodes/pve/storage/local/content": [
                {"volid": "local:iso/virtio-win.iso", "format": "iso"},
            ],
            "/nodes/pve/network": [{"iface": "vmbr0", "type": "bridge"}],
            "/nodes/pve/qemu": [],
        }
        if path not in values:
            raise RuntimeError(path)
        return values[path]

    monkeypatch.setattr(web_app, "_proxmox_api", fake_proxmox_api)
    return TestClient(web_app.app)


def _create_osdeploy_artifact(pg_conn, **overrides):
    from web import osdeploy_pg

    values = {
        "architecture": "amd64",
        "osdeploy_module_version": "26.1.30.5",
        "osdbuilder_module_version": "24.10.8.1",
        "adk_version": "10.1.26100.1",
        "build_sha": "osdeploytest",
        "iso_path": "/app/output/osdeploy-server-amd64-osdeploytest.iso",
        "wim_path": "/app/output/osdeploy-server-amd64-osdeploytest.wim",
        "manifest_path": "/app/output/osdeploy-server-amd64-osdeploytest.json",
        "iso_sha256": "c" * 64,
        "wim_sha256": "d" * 64,
        "source_media": "Windows Server 2025",
        "image_name": "Windows Server 2025 Datacenter",
        "image_index": 4,
        "os_version": "Windows Server 2025",
        "os_edition": "Datacenter",
        "os_language": "en-us",
        "built_by_host": "Adam.Gell@10.211.55.6",
        "proxmox_volid": "local:iso/osdeploy-server-amd64-osdeploytest.iso",
    }
    values.update(overrides)
    return osdeploy_pg.create_artifact(pg_conn, **values)


def _run_payload(artifact_id: str, **overrides):
    values = {
        "artifact_id": artifact_id,
        "vm_name": "Server Lab 001",
        "node": "pve",
        "iso_storage": "local",
        "storage": "local-lvm",
        "network_bridge": "vmbr0",
        "architecture": "amd64",
        "server_role": "base",
        "os_version": "Windows Server 2025",
        "os_edition": "Datacenter",
        "os_language": "en-us",
        "vm_cores": 4,
        "vm_memory_mb": 8192,
        "vm_disk_size_gb": 120,
        "secure_boot": True,
        "outbound_policy": {"mode": "blocked"},
    }
    values.update(overrides)
    return values


def _assert_local_admin(local_admin: dict):
    assert local_admin["username"] == "localadmin"
    password = local_admin["password"]
    assert 8 <= len(password) <= 12
    assert re.search(r"[A-Z]", password)
    assert re.search(r"[a-z]", password)
    assert re.search(r"[0-9]", password)
    assert re.search(r"[!#%+?]", password)
    assert not re.search(r"[O0Il1\"'`\s]", password)


def _file_server_options(**overrides):
    values = {
        "share_name": "Shared",
        "share_path": r"C:\Shares\Shared",
        "full_access_principals": ["HOME\\Domain Admins"],
        "change_access_principals": ["HOME\\Domain Users"],
        "read_access_principals": [],
    }
    values.update(overrides)
    return values


def _isolated_dc_options(**overrides):
    values = {
        "forest_fqdn": "lab.gell.one",
        "netbios_name": "LAB",
        "forest_admin_credential_id": 101,
        "dsrm_credential_id": 102,
    }
    values.update(overrides)
    return values


def _mecm_options(**overrides):
    values = {
        "prereq_profile": "site_server_foundation",
        "content_root": r"C:\MECMContent",
    }
    values.update(overrides)
    return values


def _lab_bundle_options(**overrides):
    values = {
        "bundle_name": "Lab Bundle 01",
        "domain_join_credential_id": 103,
        "children": [
            {
                "role": "isolated_domain_controller",
                "vm_name": "LAB-DC01",
                "role_options": _isolated_dc_options(),
            },
            {
                "role": "file_server",
                "vm_name": "LAB-FS01",
                "role_options": _file_server_options(),
            },
            {
                "role": "mecm_prereq",
                "vm_name": "LAB-CM01",
                "role_options": _mecm_options(),
            },
        ],
    }
    values.update(overrides)
    return values


def _create_role_credentials(pg_conn):
    from web import app as web_app, sequences_pg

    cipher = web_app._cipher()
    creds = {
        "forest_admin": sequences_pg.create_credential(
            pg_conn,
            cipher,
            name="lab-forest-admin",
            type="domain_join",
            payload={"username": "LAB\\Administrator", "password": "secret"},
        ),
        "dsrm": sequences_pg.create_credential(
            pg_conn,
            cipher,
            name="lab-dsrm",
            type="local_admin",
            payload={"username": "Administrator", "password": "secret"},
        ),
        "domain_join": sequences_pg.create_credential(
            pg_conn,
            cipher,
            name="lab-domain-join",
            type="domain_join",
            payload={"username": "LAB\\joiner", "password": "secret"},
        ),
    }
    pg_conn.commit()
    return creds


def test_osdeploy_schema_creates_maturity_parity_tables(pg_conn):
    from web import osdeploy_cache, osdeploy_pg

    osdeploy_pg.reset_for_tests(pg_conn)
    osdeploy_cache.reset_for_tests(pg_conn)
    osdeploy_pg.init(pg_conn)
    osdeploy_cache.init(pg_conn)

    tables = {
        row["tablename"]
        for row in pg_conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    }
    assert {
        "osdeploy_artifacts",
        "osdeploy_runs",
        "osdeploy_run_events",
        "osdeploy_readiness",
        "osdeploy_cache_entries",
    }.issubset(tables)


def test_osdeploy_artifact_list_preflight_and_run_create(osdeploy_client, pg_conn):
    artifact = _create_osdeploy_artifact(pg_conn)

    artifacts = osdeploy_client.get("/api/osdeploy/v1/artifacts")
    assert artifacts.status_code == 200
    assert artifacts.json()["artifacts"][0]["ready"] is True

    preflight = osdeploy_client.post(
        "/api/osdeploy/v1/preflight",
        json=_run_payload(artifact["id"]),
    )
    assert preflight.status_code == 200
    assert preflight.json()["launch_allowed"] is True

    created = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(artifact["id"]),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["run"]["server_role"] == "base"
    assert body["run"]["state"] == "created"
    assert body["run"]["expected_computer_name"] == "ServerLab001"

    detail = osdeploy_client.get(f"/api/osdeploy/v1/runs/{body['run']['run_id']}")
    assert detail.status_code == 200
    assert detail.json()["artifact"]["build_sha"] == "osdeploytest"
    assert detail.json()["readiness"]["state"] == "waiting_for_heartbeat"
    step_kinds = [step["kind"] for step in detail.json()["v2_steps"]]
    assert "osdeploy_preflight" in step_kinds
    assert "cloudosd_preflight" not in step_kinds


def test_osdeploy_artifact_list_includes_build_and_publish_job_links(osdeploy_client, pg_conn):
    _create_osdeploy_artifact(
        pg_conn,
        build_job_id="job-build-123",
        publish_job_id="job-publish-456",
    )

    response = osdeploy_client.get("/api/osdeploy/v1/artifacts")

    assert response.status_code == 200
    artifact = response.json()["artifacts"][0]
    assert artifact["build_job_url"] == "/jobs/job-build-123"
    assert artifact["build_log_url"] == "/api/jobs/job-build-123/log"
    assert artifact["publish_job_url"] == "/jobs/job-publish-456"
    assert artifact["publish_log_url"] == "/api/jobs/job-publish-456/log"


def test_osdeploy_builder_page_renders_operational_launcher(osdeploy_client, pg_conn):
    artifact = _create_osdeploy_artifact(pg_conn)

    response = osdeploy_client.get("/osdeploy/builder")

    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="osdeployRunForm"' in body
    assert artifact["id"] in body
    assert "/api/osdeploy/v1/preflight" in body
    assert "/api/osdeploy/v1/runs" in body
    assert "Launch OSDeploy VM" in body
    assert '<option value="pve" selected>pve</option>' in body
    assert '<option value="local" selected>local</option>' in body
    assert '<option value="local-lvm" selected>local-lvm</option>' in body
    assert '<option value="vmbr0" selected>vmbr0</option>' in body
    assert "&#34;" not in body


def test_osdeploy_v2_catalog_owns_osdeploy_step_kind():
    from web import osd_v2_catalog

    assert osd_v2_catalog.target_for_step_kind("osdeploy_preflight") == "windows"


def test_osdeploy_preflight_blocks_unpublished_artifact(osdeploy_client, pg_conn):
    artifact = _create_osdeploy_artifact(pg_conn, proxmox_volid=None)

    preflight = osdeploy_client.post(
        "/api/osdeploy/v1/preflight",
        json=_run_payload(artifact["id"]),
    )

    assert preflight.status_code == 200
    assert preflight.json()["launch_allowed"] is False
    assert {
        check["id"] for check in preflight.json()["blocking_checks"]
    } == {"artifact_not_published"}


def test_osdeploy_preflight_blocks_requested_os_that_does_not_match_artifact(
    osdeploy_client,
    pg_conn,
):
    artifact = _create_osdeploy_artifact(
        pg_conn,
        image_name="Windows 11 Enterprise Evaluation",
        os_version="Windows 11",
        os_edition="Enterprise",
    )

    preflight = osdeploy_client.post(
        "/api/osdeploy/v1/preflight",
        json=_run_payload(
            artifact["id"],
            os_version="Windows Server 2025",
            os_edition="Datacenter",
        ),
    )

    assert preflight.status_code == 200
    body = preflight.json()
    assert body["launch_allowed"] is False
    assert "artifact_os_mismatch" in {
        check["id"] for check in body["blocking_checks"]
    }

    created = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(
            artifact["id"],
            os_version="Windows Server 2025",
            os_edition="Datacenter",
        ),
    )
    assert created.status_code == 409


def test_osdeploy_preflight_blocks_missing_virtio_media(osdeploy_client, pg_conn, monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "proxmox_node": "pve",
        "proxmox_iso_storage": "local",
        "proxmox_storage": "local-lvm",
        "proxmox_bridge": "vmbr0",
    })
    monkeypatch.setattr(web_app, "_proxmox_api", lambda path, *args, **kwargs: {
        "/storage": [
            {"storage": "local", "content": "iso"},
            {"storage": "local-lvm", "content": "images"},
        ],
        "/nodes/pve/storage/local/content": [],
    }.get(path, []))
    artifact = _create_osdeploy_artifact(pg_conn)

    preflight = osdeploy_client.post(
        "/api/osdeploy/v1/preflight",
        json=_run_payload(artifact["id"]),
    )

    assert preflight.status_code == 200
    assert preflight.json()["launch_allowed"] is False
    assert "virtio_iso_missing" in {
        check["id"] for check in preflight.json()["blocking_checks"]
    }


def test_osdeploy_preflight_blocks_missing_disk_storage(
    osdeploy_client,
    pg_conn,
    monkeypatch,
):
    from web import app as web_app

    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "proxmox_node": "pve",
        "proxmox_iso_storage": "local",
        "proxmox_storage": "local-zfs",
        "proxmox_bridge": "vmbr0",
        "proxmox_virtio_iso": "local:iso/virtio-win.iso",
    })
    monkeypatch.setattr(web_app, "_proxmox_api", lambda path, *args, **kwargs: {
        "/storage": [
            {"storage": "local", "content": "iso"},
            {"storage": "local-zfs", "content": "images,rootdir"},
        ],
        "/nodes/pve/storage/local/content": [
            {"volid": "local:iso/virtio-win.iso", "format": "iso"},
        ],
    }.get(path, []))
    artifact = _create_osdeploy_artifact(pg_conn)

    preflight = osdeploy_client.post(
        "/api/osdeploy/v1/preflight",
        json=_run_payload(artifact["id"], storage="local-lvm"),
    )

    assert preflight.status_code == 200
    body = preflight.json()
    assert body["launch_allowed"] is False
    assert "disk_storage_missing" in {
        check["id"] for check in body["blocking_checks"]
    }

    created = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(artifact["id"], storage="local-lvm"),
    )
    assert created.status_code == 409


def test_osdeploy_preflight_and_provision_recover_stale_virtio_storage(
    osdeploy_client,
    pg_conn,
    monkeypatch,
):
    from web import app as web_app, jobs_pg

    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "proxmox_node": "pve",
        "proxmox_iso_storage": "local",
        "proxmox_storage": "local-lvm",
        "proxmox_bridge": "vmbr0",
        "proxmox_virtio_iso": "isos:iso/virtio-win-0.1.285.iso",
    })

    def fake_proxmox_api(path, *args, **kwargs):
        values = {
            "/storage": [
                {"storage": "local", "content": "iso"},
                {"storage": "local-lvm", "content": "images"},
            ],
            "/nodes/pve/storage/local/content": [
                {"volid": "local:iso/virtio-win.iso", "format": "iso"},
            ],
        }
        if path == "/nodes/pve/storage/isos/content":
            raise RuntimeError("storage 'isos' does not exist")
        return values.get(path, [])

    monkeypatch.setattr(web_app, "_proxmox_api", fake_proxmox_api)
    artifact = _create_osdeploy_artifact(pg_conn)

    preflight = osdeploy_client.post(
        "/api/osdeploy/v1/preflight",
        json=_run_payload(artifact["id"]),
    )

    assert preflight.status_code == 200, preflight.text
    body = preflight.json()
    assert body["launch_allowed"] is True
    assert "virtio_iso_recovered" in {check["id"] for check in body["warnings"]}

    run = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(artifact["id"], vm_name="OSDEPLOY-STALE-VIRTIO"),
    ).json()["run"]
    response = osdeploy_client.post(f"/api/osdeploy/v1/runs/{run['run_id']}/provision")

    assert response.status_code == 202, response.text
    job = jobs_pg.get_job(response.json()["job_id"])
    assert job["args"]["proxmox_virtio_iso"] == "local:iso/virtio-win.iso"


def test_osdeploy_preflight_uses_configured_node_for_virtio_when_node_omitted(
    osdeploy_client,
    pg_conn,
):
    artifact = _create_osdeploy_artifact(pg_conn)
    payload = _run_payload(artifact["id"])
    payload.pop("node")

    preflight = osdeploy_client.post(
        "/api/osdeploy/v1/preflight",
        json=payload,
    )

    assert preflight.status_code == 200, preflight.text
    body = preflight.json()
    assert body["launch_allowed"] is True
    assert "virtio_iso_missing" not in {
        check["id"] for check in body["blocking_checks"]
    }


def test_osdeploy_preflight_accepts_launchable_role_with_required_options(
    osdeploy_client,
    pg_conn,
):
    artifact = _create_osdeploy_artifact(pg_conn)

    catalog = osdeploy_client.get("/api/osdeploy/v1/catalog")
    assert catalog.status_code == 200
    assert "file_server" in catalog.json()["server_roles"]
    role_catalog = catalog.json()["role_catalog"]
    assert role_catalog["file_server"]["launchable"] is True
    assert role_catalog["file_server"]["step_kinds"] == ["configure_file_server_role"]
    assert role_catalog["file_server"]["readiness_status_name"] == "file_server_ready"

    preflight = osdeploy_client.post(
        "/api/osdeploy/v1/preflight",
        json=_run_payload(
            artifact["id"],
            server_role="file_server",
            role_options=_file_server_options(),
        ),
    )

    assert preflight.status_code == 200, preflight.text
    body = preflight.json()
    assert body["launch_allowed"] is True
    assert body["role"]["id"] == "file_server"

    created = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(
            artifact["id"],
            server_role="file_server",
            role_options=_file_server_options(),
        ),
    )
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    assert run["server_role"] == "file_server"
    assert run["role_options"]["share_name"] == "Shared"
    detail = osdeploy_client.get(f"/api/osdeploy/v1/runs/{run['run_id']}").json()
    assert [step["kind"] for step in detail["v2_steps"]][-2:] == [
        "configure_file_server_role",
        "wait_agent_heartbeat",
    ]


def test_osdeploy_preflight_rejects_role_missing_required_options(osdeploy_client, pg_conn):
    artifact = _create_osdeploy_artifact(pg_conn)

    preflight = osdeploy_client.post(
        "/api/osdeploy/v1/preflight",
        json=_run_payload(
            artifact["id"],
            server_role="file_server",
            role_options={"share_name": "Shared"},
        ),
    )

    assert preflight.status_code == 200
    body = preflight.json()
    assert body["launch_allowed"] is False
    assert {
        check["id"] for check in body["blocking_checks"]
    } >= {"role_option_missing_share_path", "role_option_missing_full_access_principals"}


def test_osdeploy_launches_all_role_sequences_with_typed_steps(osdeploy_client, pg_conn):
    artifact = _create_osdeploy_artifact(pg_conn)
    creds = _create_role_credentials(pg_conn)
    cases = {
        "file_server": (
            _file_server_options(),
            ["configure_file_server_role", "wait_agent_heartbeat"],
        ),
        "isolated_domain_controller": (
            _isolated_dc_options(
                forest_admin_credential_id=creds["forest_admin"],
                dsrm_credential_id=creds["dsrm"],
            ),
            [
                "configure_isolated_domain_controller_role",
                "verify_isolated_domain_controller_role",
                "wait_agent_heartbeat",
            ],
        ),
        "mecm_prereq": (
            _mecm_options(),
            ["configure_mecm_prereq_role", "wait_agent_heartbeat"],
        ),
    }

    for role, (role_options, expected_tail) in cases.items():
        created = osdeploy_client.post(
            "/api/osdeploy/v1/runs",
            json=_run_payload(
                artifact["id"],
                vm_name=f"OSDEPLOY-{role[:6]}",
                server_role=role,
                role_options=role_options,
            ),
        )
        assert created.status_code == 201, created.text
        run = created.json()["run"]
        detail = osdeploy_client.get(f"/api/osdeploy/v1/runs/{run['run_id']}").json()
        assert [step["kind"] for step in detail["v2_steps"]][-len(expected_tail):] == expected_tail


def test_osdeploy_cache_catalog_and_entry_lifecycle(osdeploy_client, pg_conn):
    from web import osdeploy_cache

    entry = osdeploy_cache.upsert_entry(pg_conn, {
        "entry_type": "server_image",
        "status": "ready",
        "windows_version": "Windows Server 2025",
        "architecture": "amd64",
        "edition": "Datacenter",
        "language": "en-us",
        "file_name": "server-2025-datacenter.wim",
        "source_url": "https://download.example.test/server-2025-datacenter.wim",
        "expected_size_bytes": 11,
        "expected_sha256": "e" * 64,
        "sha256": "e" * 64,
        "size_bytes": 11,
        "local_path": "/tmp/server-2025-datacenter.wim",
    })
    pg_conn.commit()

    payload = osdeploy_client.get("/api/osdeploy/v1/cache")
    assert payload.status_code == 200
    assert payload.json()["summary"]["ready"] == 1
    assert payload.json()["entries"][0]["id"] == entry["id"]

    verify = osdeploy_client.post(f"/api/osdeploy/v1/cache/{entry['id']}/verify")
    assert verify.status_code == 202
    assert verify.json()["job_type"] == "osdeploy_cache_verify"


def test_osdeploy_cache_refresh_verify_and_delete(pg_conn, tmp_path, monkeypatch):
    from hashlib import sha256
    from web import osdeploy_cache

    monkeypatch.setenv("AUTOPILOT_OSDEPLOY_CACHE_ROOT", str(tmp_path))
    osdeploy_cache.reset_for_tests(pg_conn)
    osdeploy_cache.init(pg_conn)

    first = osdeploy_cache.refresh_catalog(pg_conn)
    second = osdeploy_cache.refresh_catalog(pg_conn)
    entries = osdeploy_cache.list_entries(pg_conn)

    assert first["server_images"]
    assert len(entries) == len(second["server_images"]) + len(second["quality_updates"])
    assert {entry["windows_version"] for entry in entries} >= {
        "Windows Server 2025",
        "Windows Server 2022",
    }

    payload = b"server-media"
    media = tmp_path / "server-media.wim"
    media.write_bytes(payload)
    digest = sha256(payload).hexdigest()
    entry = osdeploy_cache.upsert_entry(pg_conn, {
        "entry_type": "server_image",
        "status": "warming",
        "windows_version": "Windows Server 2025",
        "architecture": "amd64",
        "edition": "Datacenter",
        "language": "en-us",
        "file_name": media.name,
        "source_url": media.as_uri(),
        "expected_size_bytes": len(payload),
        "expected_sha256": digest,
        "local_path": str(media),
    })
    pg_conn.commit()

    verified = osdeploy_cache.verify_entry(pg_conn, entry["id"])
    assert verified["status"] == "ready"
    assert verified["sha256"] == digest

    deleted = osdeploy_cache.delete_entry_file(pg_conn, entry["id"])
    assert deleted["status"] == "missing"
    assert not media.exists()


def test_osdeploy_cache_download_serves_ready_entry(osdeploy_client, pg_conn, tmp_path):
    from web import osdeploy_cache

    payload = b"server-media-download"
    media = tmp_path / "server-media-download.wim"
    media.write_bytes(payload)
    entry = osdeploy_cache.upsert_entry(pg_conn, {
        "entry_type": "server_image",
        "status": "ready",
        "windows_version": "Windows Server 2025",
        "architecture": "amd64",
        "edition": "Datacenter",
        "language": "en-us",
        "file_name": media.name,
        "source_url": "https://download.example.test/server-media-download.wim",
        "size_bytes": len(payload),
        "local_path": str(media),
    })
    pg_conn.commit()

    head = osdeploy_client.head(
        f"/api/osdeploy/v1/cache/{entry['id']}/download/{media.name}",
    )
    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(payload))

    served = osdeploy_client.get(
        f"/api/osdeploy/v1/cache/{entry['id']}/download/{media.name}",
    )
    assert served.status_code == 200
    assert served.content == payload
    assert served.headers["cache-control"] == "private, no-store"

    updated = osdeploy_cache.get_entry(pg_conn, entry["id"])
    assert updated["served_count"] == 1
    assert updated["last_served_at"]

    mismatch = osdeploy_client.get(
        f"/api/osdeploy/v1/cache/{entry['id']}/download/not-the-file.wim",
    )
    assert mismatch.status_code == 404


def test_osdeploy_build_preflight_blocks_missing_key_and_unreachable_remote(
    osdeploy_client,
    monkeypatch,
):
    from web import osdeploy_endpoints

    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_ssh_key_exists", lambda path: False)
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_remote_ssh_reachable", lambda host: False)

    response = osdeploy_client.post(
        "/api/osdeploy/v1/artifacts/build/preflight",
        json={
            "remote": "Adam.Gell@10.211.55.6",
            "remote_root": r"F:\BuildRoot",
            "architecture": "amd64",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["build_allowed"] is False
    assert {
        check["id"] for check in body["blocking_checks"]
    } >= {"ssh_key_missing", "remote_ssh_unreachable"}
    assert body["target"]["remote_host"] == "10.211.55.6"


def test_osdeploy_build_endpoint_enforces_build_preflight(osdeploy_client, monkeypatch):
    from web import osdeploy_endpoints

    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_ssh_key_exists", lambda path: False)
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_remote_ssh_reachable", lambda host: True)

    response = osdeploy_client.post(
        "/api/osdeploy/v1/artifacts/build",
        json={
            "remote": "Adam.Gell@10.211.55.6",
            "remote_root": r"F:\BuildRoot",
            "architecture": "amd64",
        },
    )

    assert response.status_code == 409
    assert "ssh_key_missing" in {
        check["id"] for check in response.json()["detail"]["blocking_checks"]
    }


def test_osdeploy_build_preflight_uses_configured_build_defaults(
    osdeploy_client,
    monkeypatch,
):
    from web import app as web_app
    from web import osdeploy_endpoints

    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "proxmox_node": "pve",
        "proxmox_iso_storage": "local",
        "proxmox_storage": "local-lvm",
        "proxmox_bridge": "vmbr0",
        "proxmox_virtio_iso": "local:iso/virtio-win.iso",
        "osdeploy_build_remote": "builder@192.0.2.55",
        "osdeploy_build_remote_root": r"E:\OSDeployBuild",
        "osdeploy_build_ssh_key_path": "/app/secrets/custom-osdeploy-key",
    })
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_ssh_key_exists", lambda path: True)
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_remote_ssh_reachable", lambda host: True)

    response = osdeploy_client.post(
        "/api/osdeploy/v1/artifacts/build/preflight",
        json={},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["build_allowed"] is True
    assert body["target"]["remote"] == "builder@192.0.2.55"
    assert body["target"]["remote_host"] == "192.0.2.55"
    assert body["target"]["remote_root"] == r"E:\OSDeployBuild"
    assert body["target"]["ssh_key_path"] == "/app/secrets/custom-osdeploy-key"


def test_osdeploy_build_preflight_allows_ready_build_host_agent(
    osdeploy_client,
    monkeypatch,
):
    from web import app as web_app
    from web import osdeploy_endpoints

    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_ssh_key_exists", lambda path: False)
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_remote_ssh_reachable", lambda host: False)
    monkeypatch.setattr(web_app, "_setup_readiness", lambda: {
        "controller": {"url": "http://controller:5000"},
        "build_host": {
            "agent_ready": True,
            "agent_state": "ready",
            "expected_agent_id": "buildhost-100",
            "vmid": 100,
            "last_heartbeat_age_seconds": 12,
        },
    })

    response = osdeploy_client.post(
        "/api/osdeploy/v1/artifacts/build/preflight",
        json={
            "build_mode": "build_host_agent",
            "remote": "Adam.Gell@10.211.55.6",
            "remote_root": r"F:\BuildRoot",
            "architecture": "amd64",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["build_allowed"] is True
    assert body["target"]["selected_build_mode"] == "build_host_agent"
    assert body["target"]["build_host_agent"]["agent_id"] == "buildhost-100"
    assert body["target"]["build_host_agent"]["vmid"] == 100
    assert body["blocking_checks"] == []


def test_osdeploy_build_preflight_allows_explicit_registered_build_host_agent(
    osdeploy_client,
    pg_conn,
    monkeypatch,
):
    from web import agent_telemetry_pg
    from web import app as web_app
    from web import osdeploy_endpoints

    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="buildhost-explicit",
        token="buildhost-secret",
        vmid=116,
        computer_name="AUTOPILOT-BLD",
        agent_version="0.1.2.0",
    )
    agent_telemetry_pg.record_heartbeat(
        pg_conn,
        agent_id="buildhost-explicit",
        payload={
            "vmid": 116,
            "computer_name": "AUTOPILOT-BLD",
            "primary_ipv4": "192.168.2.104",
            "current_phase": "build-host",
            "server_url": "http://192.168.2.4:5000/",
            "agent_version": "0.1.2.0",
        },
    )
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_ssh_key_exists", lambda path: False)
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_remote_ssh_reachable", lambda host: False)
    monkeypatch.setattr(web_app, "_setup_readiness", lambda: {
        "controller": {"url": ""},
        "build_host": {
            "agent_ready": False,
            "agent_state": "missing",
            "expected_agent_id": "",
            "vmid": None,
        },
    })

    response = osdeploy_client.post(
        "/api/osdeploy/v1/artifacts/build/preflight",
        json={
            "build_mode": "build_host_agent",
            "build_host_agent_id": "buildhost-explicit",
            "architecture": "amd64",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["build_allowed"] is True
    assert body["target"]["selected_build_mode"] == "build_host_agent"
    assert body["target"]["build_host_agent"]["agent_id"] == "buildhost-explicit"
    assert body["target"]["build_host_agent"]["vmid"] == 116
    assert body["target"]["build_host_agent"]["agent_state"] == "ready"
    assert body["target"]["build_host_agent"]["source_bundle_url"] == (
        "http://192.168.2.4:5000/api/setup/v1/source-bundle.zip"
    )
    assert body["blocking_checks"] == []


def test_osdeploy_build_preflight_ignores_explicit_agent_loopback_server_url(
    osdeploy_client,
    pg_conn,
    monkeypatch,
):
    from web import agent_telemetry_pg
    from web import app as web_app
    from web import osdeploy_endpoints

    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="buildhost-explicit",
        token="buildhost-secret",
        vmid=116,
        computer_name="AUTOPILOT-BLD",
        agent_version="0.1.2.0",
    )
    agent_telemetry_pg.record_heartbeat(
        pg_conn,
        agent_id="buildhost-explicit",
        payload={
            "vmid": 116,
            "computer_name": "AUTOPILOT-BLD",
            "primary_ipv4": "192.168.2.104",
            "current_phase": "build-host",
            "server_url": "http://127.0.0.1:5000",
            "agent_version": "0.1.2.0",
        },
    )
    monkeypatch.setenv("AUTOPILOT_BASE_URL", "http://127.0.0.1:5000")
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_ssh_key_exists", lambda path: False)
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_remote_ssh_reachable", lambda host: False)
    monkeypatch.setattr(
        web_app,
        "_derive_guest_reachable_base_url",
        lambda config: "http://controller:5000",
    )
    monkeypatch.setattr(web_app, "_setup_readiness", lambda: {
        "controller": {"url": "http://127.0.0.1:5000"},
        "build_host": {
            "agent_ready": False,
            "agent_state": "missing",
            "expected_agent_id": "",
            "vmid": None,
        },
    })

    response = osdeploy_client.post(
        "/api/osdeploy/v1/artifacts/build/preflight",
        json={
            "build_mode": "build_host_agent",
            "build_host_agent_id": "buildhost-explicit",
            "architecture": "amd64",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["build_allowed"] is True
    assert body["target"]["build_host_agent"]["source_bundle_url"] == (
        "http://controller:5000/api/setup/v1/source-bundle.zip"
    )


def test_osdeploy_provision_base_url_uses_guest_reachable_url(monkeypatch):
    from web import app as web_app
    from web import osdeploy_endpoints

    monkeypatch.setenv("AUTOPILOT_BASE_URL", "http://127.0.0.1:5000")
    monkeypatch.setattr(
        web_app,
        "_derive_guest_reachable_base_url",
        lambda config: "http://controller:5000",
    )

    assert osdeploy_endpoints._base_url(None) == "http://controller:5000"


def test_osdeploy_build_preflight_reports_explicit_agent_wrong_phase(
    osdeploy_client,
    pg_conn,
    monkeypatch,
):
    from web import agent_telemetry_pg
    from web import app as web_app
    from web import osdeploy_endpoints

    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-bootstrap",
        token="agent-secret",
        vmid=116,
        computer_name="GELL-BOOTSTRAP",
        agent_version="0.1.2.0",
    )
    agent_telemetry_pg.record_heartbeat(
        pg_conn,
        agent_id="agent-bootstrap",
        payload={
            "vmid": 116,
            "computer_name": "GELL-BOOTSTRAP",
            "primary_ipv4": "192.168.2.104",
            "current_phase": "bootstrap",
            "agent_version": "0.1.2.0",
        },
    )
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_ssh_key_exists", lambda path: False)
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_remote_ssh_reachable", lambda host: False)
    monkeypatch.setattr(web_app, "_setup_readiness", lambda: {
        "controller": {"url": "http://controller:5000"},
        "build_host": {
            "agent_ready": False,
            "agent_state": "missing",
            "expected_agent_id": "",
            "vmid": None,
        },
    })

    response = osdeploy_client.post(
        "/api/osdeploy/v1/artifacts/build/preflight",
        json={
            "build_mode": "build_host_agent",
            "build_host_agent_id": "agent-bootstrap",
            "architecture": "amd64",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["build_allowed"] is False
    assert body["target"]["build_host_agent"]["agent_state"] == "wrong_phase"
    assert "build_host_agent_wrong_phase" in {
        check["id"] for check in body["blocking_checks"]
    }


def test_osdeploy_build_host_activate_queues_self_promotion_work_item(
    osdeploy_client,
    pg_conn,
):
    from web import agent_telemetry_pg
    from web import app as web_app

    assert any(
        getattr(route, "path", "") == "/api/osdeploy/v1/build-host/agents/{agent_id}/activate"
        for route in web_app.app.routes
    )

    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-builder-candidate",
        token="agent-builder-secret",
        vmid=116,
        computer_name="AUTOPILOT-BLD",
        agent_version="0.1.2.0",
    )
    agent_telemetry_pg.record_heartbeat(
        pg_conn,
        agent_id="agent-builder-candidate",
        payload={
            "vmid": 116,
            "computer_name": "AUTOPILOT-BLD",
            "primary_ipv4": "192.168.2.104",
            "current_phase": "bootstrap",
            "agent_version": "0.1.2.0",
            "capabilities": [
                "capture_autopilot_hash",
                "configure_build_host_role",
            ],
        },
    )

    response = osdeploy_client.post(
        "/api/osdeploy/v1/build-host/agents/agent-builder-candidate/activate",
        json={
            "confirm_build_host": True,
            "work_root": r"D:\OSDeployBuild",
        },
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["agent_id"] == "agent-builder-candidate"
    assert body["kind"] == "configure_build_host_role"
    assert body["next_expected_phase"] == "build-host"

    row = agent_telemetry_pg.get_work_item(pg_conn, body["work_item_id"])
    assert row["agent_id"] == "agent-builder-candidate"
    assert row["kind"] == "configure_build_host_role"
    assert row["status"] == "pending"
    assert row["vmid"] == 116
    assert row["request_json"]["phase"] == "build-host"
    assert row["request_json"]["role"] == "build-host"
    assert row["request_json"]["work_root"] == r"D:\OSDeployBuild"
    assert "build_osdeploy" in row["request_json"]["capabilities"]


def test_osdeploy_build_host_activate_blocks_agent_without_activation_capability(
    osdeploy_client,
    pg_conn,
):
    from web import agent_telemetry_pg

    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-old-builder",
        token="agent-old-secret",
        vmid=117,
        computer_name="AUTOPILOT-OLD",
        agent_version="0.1.2.0",
    )
    agent_telemetry_pg.record_heartbeat(
        pg_conn,
        agent_id="agent-old-builder",
        payload={
            "vmid": 117,
            "computer_name": "AUTOPILOT-OLD",
            "primary_ipv4": "192.168.2.117",
            "current_phase": "bootstrap",
            "agent_version": "0.1.2.0",
            "capabilities": ["capture_autopilot_hash"],
        },
    )

    response = osdeploy_client.post(
        "/api/osdeploy/v1/build-host/agents/agent-old-builder/activate",
        json={"confirm_build_host": True},
    )

    assert response.status_code == 409, response.text
    assert "configure_build_host_role" in response.json()["detail"]


def test_osdeploy_build_host_repair_preserves_selected_agent_identity(
    osdeploy_client,
    pg_conn,
    monkeypatch,
):
    from web import agent_telemetry_pg
    from web import app as web_app

    assert any(
        getattr(route, "path", "") == "/api/osdeploy/v1/build-host/agents/{agent_id}/repair"
        for route in web_app.app.routes
    )

    captured = {}
    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-existing-builder",
        token="agent-existing-secret",
        vmid=118,
        computer_name="GELL-BUILDER",
        agent_version="0.1.2.0",
    )
    agent_telemetry_pg.record_heartbeat(
        pg_conn,
        agent_id="agent-existing-builder",
        payload={
            "vmid": 118,
            "computer_name": "GELL-BUILDER",
            "primary_ipv4": "192.168.2.118",
            "current_phase": "bootstrap",
            "agent_version": "0.1.2.0",
            "capabilities": [],
        },
    )

    monkeypatch.setattr(web_app, "_resolve_vm_node", lambda vmid: "pve-test")

    def fake_guest_exec_ps_status(node, vmid, ps, timeout_s=300):
        captured.update({"node": node, "vmid": vmid, "ps": ps, "timeout_s": timeout_s})
        return {
            "ok": True,
            "out": '{"ok":true,"vmid":118,"agentId":"agent-existing-builder"}',
            "err": "",
        }

    monkeypatch.setattr(web_app, "_guest_exec_ps_status", fake_guest_exec_ps_status)

    response = osdeploy_client.post(
        "/api/osdeploy/v1/build-host/agents/agent-existing-builder/repair",
        json={
            "server_url": "http://controller:5000",
            "upgrade_agent": True,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert captured["node"] == "pve-test"
    assert captured["vmid"] == 118
    assert "agent-existing-builder" in captured["ps"]
    assert "buildhost-118" not in captured["ps"]
    assert "api/setup/v1/agent-seed/win-x64/AutopilotAgent.exe" in captured["ps"]
    assert "$programDataExe = Join-Path (Split-Path -Parent $configPath) 'AutopilotAgent.exe'" in captured["ps"]
    assert "$targetExePaths = @($exe, $programDataExe)" in captured["ps"]
    assert "configure_build_host_role" in captured["ps"]
    assert "$capabilities = @(" in captured["ps"]
    assert "ConvertFrom-Json" not in captured["ps"].partition("$capabilities = ")[2].splitlines()[0]


def test_osdeploy_build_host_repair_can_override_stale_heartbeat(
    osdeploy_client,
    pg_conn,
    monkeypatch,
):
    from web import agent_telemetry_pg
    from web import app as web_app
    from web import osdeploy_endpoints

    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-stale-builder",
        token="agent-stale-secret",
        vmid=119,
        computer_name="GELL-STALE",
        agent_version="0.1.2.0",
    )
    agent_telemetry_pg.record_heartbeat(
        pg_conn,
        agent_id="agent-stale-builder",
        payload={
            "vmid": 119,
            "computer_name": "GELL-STALE",
            "current_phase": "bootstrap",
            "capabilities": [],
        },
    )
    monkeypatch.setattr(osdeploy_endpoints, "_heartbeat_age_seconds", lambda value: 3600)
    monkeypatch.setattr(web_app, "_resolve_vm_node", lambda vmid: "pve-test")
    monkeypatch.setattr(
        web_app,
        "_guest_exec_ps_status",
        lambda node, vmid, ps, timeout_s=300: {
            "ok": True,
            "out": '{"ok":true,"vmid":119,"agentId":"agent-stale-builder"}',
            "err": "",
        },
    )

    blocked = osdeploy_client.post(
        "/api/osdeploy/v1/build-host/agents/agent-stale-builder/repair",
        json={"server_url": "http://controller:5000"},
    )
    allowed = osdeploy_client.post(
        "/api/osdeploy/v1/build-host/agents/agent-stale-builder/repair",
        json={"server_url": "http://controller:5000", "allow_stale": True},
    )

    assert blocked.status_code == 409, blocked.text
    assert allowed.status_code == 200, allowed.text


def test_osdeploy_build_defaults_report_key_status_and_public_key(
    osdeploy_client,
    monkeypatch,
    tmp_path,
):
    from web import app as web_app

    key = tmp_path / "osdeploy_devmachine_ed25519"
    key.write_text("private")
    key.chmod(0o600)
    key.with_name(key.name + ".pub").write_text("ssh-ed25519 AAAATEST osdeploy\n")

    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "osdeploy_build_remote": "builder@192.0.2.55",
        "osdeploy_build_remote_root": r"E:\OSDeployBuild",
        "osdeploy_build_ssh_key_path": str(key),
    })

    response = osdeploy_client.get("/api/osdeploy/v1/artifacts/build/defaults")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ssh_key_path"] == str(key)
    assert body["ssh_key_exists"] is True
    assert body["ssh_public_key_path"] == str(key) + ".pub"
    assert body["ssh_public_key"] == "ssh-ed25519 AAAATEST osdeploy"
    assert body["image_name"] == "Windows Server 2022 Datacenter (Desktop Experience)"
    assert body["os_version"] == "Windows Server 2022"


def test_osdeploy_build_iso_endpoint_enqueues_ssh_wrapper(osdeploy_client, monkeypatch):
    from web import osdeploy_endpoints
    from pathlib import Path
    from web import jobs_pg

    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_ssh_key_exists", lambda path: True)
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_remote_ssh_reachable", lambda host: True)

    response = osdeploy_client.post(
        "/api/osdeploy/v1/artifacts/build",
        json={
            "remote": "Adam.Gell@10.211.55.6",
            "remote_root": r"F:\BuildRoot",
            "architecture": "amd64",
            "osdeploy_module_version": "26.1.30.5",
            "osdbuilder_module_version": "24.10.8.1",
            "adk_version": "10.1.26100.1",
        },
    )

    assert response.status_code == 202, response.text
    job = jobs_pg.get_job(response.json()["job_id"])
    assert job["job_type"] == "osdeploy_build_iso"
    assert "osdeploy_remote_build.py" in " ".join(job["cmd"])
    repo_arg = job["cmd"][job["cmd"].index("--repo-root") + 1]
    assert (Path(repo_arg) / "tools" / "osdeploy-build" / "build-osdeploy.ps1").exists()
    assert job["args"]["remote"] == "Adam.Gell@10.211.55.6"


def test_osdeploy_build_iso_endpoint_queues_build_host_agent_work_item(
    osdeploy_client,
    pg_conn,
    monkeypatch,
):
    from web import agent_telemetry_pg
    from web import app as web_app
    from web import osdeploy_endpoints

    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="buildhost-100",
        token="buildhost-secret",
        vmid=100,
        computer_name="AUTOPILOT-BLD",
        agent_version="0.1.2.0",
    )
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_ssh_key_exists", lambda path: False)
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_remote_ssh_reachable", lambda host: False)
    monkeypatch.setattr(web_app, "_setup_readiness", lambda: {
        "controller": {"url": "http://controller:5000"},
        "build_host": {
            "agent_ready": True,
            "agent_state": "ready",
            "expected_agent_id": "buildhost-100",
            "vmid": 100,
            "last_heartbeat_age_seconds": 12,
        },
    })

    response = osdeploy_client.post(
        "/api/osdeploy/v1/artifacts/build",
        json={
            "build_mode": "build_host_agent",
            "architecture": "amd64",
            "osdeploy_module_version": "26.1.30.5",
            "osdbuilder_module_version": "24.10.8.1",
            "adk_version": "10.1.26100.1",
            "source_media_path": r"E:\ISOs\SERVER_EVAL_x64FRE_en-us.iso",
            "image_name": "Windows Server 2025 Datacenter Evaluation (Desktop Experience)",
            "image_index": 4,
            "os_version": "Windows Server 2025",
            "os_edition": "Datacenter",
            "os_language": "en-us",
        },
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["job_type"] == "osdeploy_build_host_agent"
    assert body["agent_id"] == "buildhost-100"
    assert [item["kind"] for item in body["dependencies"]] == [
        "install_build_prerequisites",
        "fetch_source_bundle",
    ]
    assert all(item["queued"] is True for item in body["dependencies"])
    row = agent_telemetry_pg.get_work_item(pg_conn, body["work_item_id"])
    assert row["kind"] == "build_osdeploy"
    assert row["status"] == "pending"
    assert row["vmid"] == 100
    assert row["request_json"]["source_bundle_url"] == (
        "http://controller:5000/api/setup/v1/source-bundle.zip"
    )
    assert row["request_json"]["osdeploy_version"] == "26.1.30.5"
    assert row["request_json"]["osdbuilder_version"] == "24.10.8.1"
    assert row["request_json"]["adk_version"] == "10.1.26100.1"
    assert row["request_json"]["source_media_path"] == r"E:\ISOs\SERVER_EVAL_x64FRE_en-us.iso"
    assert row["request_json"]["image_name"] == (
        "Windows Server 2025 Datacenter Evaluation (Desktop Experience)"
    )
    assert row["request_json"]["image_index"] == 4
    assert row["request_json"]["os_version"] == "Windows Server 2025"
    assert row["request_json"]["os_edition"] == "Datacenter"
    assert row["request_json"]["os_language"] == "en-us"
    rows = pg_conn.execute(
        """
        SELECT kind, status
        FROM agent_work_items
        WHERE agent_id = %s
        ORDER BY created_at ASC, id ASC
        """,
        ("buildhost-100",),
    ).fetchall()
    assert [(item["kind"], item["status"]) for item in rows] == [
        ("install_build_prerequisites", "pending"),
        ("fetch_source_bundle", "pending"),
        ("build_osdeploy", "pending"),
    ]


def test_osdeploy_build_iso_endpoint_requeues_stale_claimed_source_bundle_dependency(
    osdeploy_client,
    pg_conn,
    monkeypatch,
):
    from web import agent_telemetry_pg
    from web import app as web_app
    from web import osdeploy_endpoints

    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="buildhost-100",
        token="buildhost-secret",
        vmid=100,
        computer_name="AUTOPILOT-BLD",
        agent_version="0.1.2.0",
    )
    stale = agent_telemetry_pg.create_work_item(
        pg_conn,
        agent_id="buildhost-100",
        kind="fetch_source_bundle",
        vmid=100,
        request={"kind": "fetch_source_bundle", "source_bundle_url": "http://old/source.zip"},
    )
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    pg_conn.execute(
        """
        UPDATE agent_work_items
        SET status = 'claimed', claimed_at = %s, updated_at = %s
        WHERE id = %s
        """,
        (stale_time, stale_time, stale["id"]),
    )
    pg_conn.commit()
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_ssh_key_exists", lambda path: False)
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_remote_ssh_reachable", lambda host: False)
    monkeypatch.setattr(web_app, "_setup_readiness", lambda: {
        "controller": {"url": "http://controller:5000"},
        "build_host": {
            "agent_ready": True,
            "agent_state": "ready",
            "expected_agent_id": "buildhost-100",
            "vmid": 100,
            "last_heartbeat_age_seconds": 12,
        },
    })

    response = osdeploy_client.post(
        "/api/osdeploy/v1/artifacts/build",
        json={"build_mode": "build_host_agent", "architecture": "amd64"},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    fetch_dependency = [
        item for item in body["dependencies"] if item["kind"] == "fetch_source_bundle"
    ][0]
    assert fetch_dependency["queued"] is True
    assert fetch_dependency["id"] != str(stale["id"])
    rows = pg_conn.execute(
        """
        SELECT id, status
        FROM agent_work_items
        WHERE agent_id = %s AND kind = 'fetch_source_bundle'
        ORDER BY created_at ASC, id ASC
        """,
        ("buildhost-100",),
    ).fetchall()
    assert [(str(item["id"]), item["status"]) for item in rows] == [
        (str(stale["id"]), "claimed"),
        (fetch_dependency["id"], "pending"),
    ]


def test_osdeploy_build_iso_endpoint_requeues_completed_old_prerequisite_contract(
    osdeploy_client,
    pg_conn,
    monkeypatch,
):
    from web import agent_telemetry_pg
    from web import app as web_app
    from web import osdeploy_endpoints

    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="buildhost-100",
        token="buildhost-secret",
        vmid=100,
        computer_name="AUTOPILOT-BLD",
        agent_version="0.1.2.0",
    )
    old = agent_telemetry_pg.create_work_item(
        pg_conn,
        agent_id="buildhost-100",
        kind="install_build_prerequisites",
        vmid=100,
        request={"kind": "install_build_prerequisites"},
    )
    agent_telemetry_pg.complete_work_item(
        pg_conn,
        old["id"],
        agent_id="buildhost-100",
        result={"stdout": "{\"dotnet_version\":\"8.0.421\"}"},
    )
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_ssh_key_exists", lambda path: False)
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_remote_ssh_reachable", lambda host: False)
    monkeypatch.setattr(web_app, "_setup_readiness", lambda: {
        "controller": {"url": "http://controller:5000"},
        "build_host": {
            "agent_ready": True,
            "agent_state": "ready",
            "expected_agent_id": "buildhost-100",
            "vmid": 100,
            "last_heartbeat_age_seconds": 12,
        },
    })

    response = osdeploy_client.post(
        "/api/osdeploy/v1/artifacts/build",
        json={"build_mode": "build_host_agent", "architecture": "amd64"},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    prereq_dependency = [
        item for item in body["dependencies"] if item["kind"] == "install_build_prerequisites"
    ][0]
    assert prereq_dependency["queued"] is True
    assert prereq_dependency["id"] != str(old["id"])
    row = agent_telemetry_pg.get_work_item(pg_conn, prereq_dependency["id"])
    assert row["request_json"]["build_contract_version"] == 11


def test_osdeploy_build_iso_endpoint_queues_explicit_build_host_agent_work_item(
    osdeploy_client,
    pg_conn,
    monkeypatch,
):
    from web import agent_telemetry_pg
    from web import app as web_app
    from web import osdeploy_endpoints

    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="buildhost-explicit",
        token="buildhost-secret",
        vmid=116,
        computer_name="AUTOPILOT-BLD",
        agent_version="0.1.2.0",
    )
    agent_telemetry_pg.record_heartbeat(
        pg_conn,
        agent_id="buildhost-explicit",
        payload={
            "vmid": 116,
            "computer_name": "AUTOPILOT-BLD",
            "primary_ipv4": "192.168.2.104",
            "current_phase": "build-host",
            "agent_version": "0.1.2.0",
        },
    )
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_ssh_key_exists", lambda path: False)
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_remote_ssh_reachable", lambda host: False)
    monkeypatch.setattr(web_app, "_setup_readiness", lambda: {
        "controller": {"url": ""},
        "build_host": {"agent_ready": False, "expected_agent_id": "", "vmid": None},
    })

    response = osdeploy_client.post(
        "/api/osdeploy/v1/artifacts/build",
        json={
            "build_mode": "build_host_agent",
            "build_host_agent_id": "buildhost-explicit",
            "architecture": "amd64",
        },
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["job_type"] == "osdeploy_build_host_agent"
    assert body["agent_id"] == "buildhost-explicit"
    assert [item["kind"] for item in body["dependencies"]] == [
        "install_build_prerequisites",
        "fetch_source_bundle",
    ]
    row = agent_telemetry_pg.get_work_item(pg_conn, body["work_item_id"])
    assert row["agent_id"] == "buildhost-explicit"
    assert row["vmid"] == 116
    assert row["request_json"]["source_bundle_url"] == (
        "http://autopilot.test:5000/api/setup/v1/source-bundle.zip"
    )


def test_osdeploy_build_endpoint_passes_configured_ssh_key_to_worker(
    osdeploy_client,
    monkeypatch,
):
    from web import app as web_app
    from web import jobs_pg, osdeploy_endpoints

    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "proxmox_node": "pve",
        "proxmox_iso_storage": "local",
        "proxmox_storage": "local-lvm",
        "proxmox_bridge": "vmbr0",
        "proxmox_virtio_iso": "local:iso/virtio-win.iso",
        "osdeploy_build_remote": "builder@192.0.2.55",
        "osdeploy_build_remote_root": r"E:\OSDeployBuild",
        "osdeploy_build_ssh_key_path": "/app/secrets/custom-osdeploy-key",
    })
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_ssh_key_exists", lambda path: True)
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_remote_ssh_reachable", lambda host: True)

    response = osdeploy_client.post(
        "/api/osdeploy/v1/artifacts/build",
        json={},
    )

    assert response.status_code == 202, response.text
    job = jobs_pg.get_job(response.json()["job_id"])
    assert job["args"]["remote"] == "builder@192.0.2.55"
    assert job["args"]["remote_root"] == r"E:\OSDeployBuild"
    assert job["args"]["ssh_key_path"] == "/app/secrets/custom-osdeploy-key"
    assert job["cmd"][job["cmd"].index("--ssh-key") + 1] == "/app/secrets/custom-osdeploy-key"


def test_osdeploy_build_endpoint_passes_server_image_inputs_to_ssh_worker(
    osdeploy_client,
    monkeypatch,
):
    from web import jobs_pg, osdeploy_endpoints

    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_ssh_key_exists", lambda path: True)
    monkeypatch.setattr(osdeploy_endpoints, "_osdeploy_remote_ssh_reachable", lambda host: True)

    response = osdeploy_client.post(
        "/api/osdeploy/v1/artifacts/build",
        json={
            "build_mode": "ssh",
            "remote": "builder@192.0.2.55",
            "remote_root": r"E:\OSDeployBuild",
            "architecture": "amd64",
            "source_media_path": r"E:\ISOs\SERVER_EVAL_x64FRE_en-us.iso",
            "image_name": "Windows Server 2025 Datacenter Evaluation (Desktop Experience)",
            "image_index": 4,
            "os_version": "Windows Server 2025",
            "os_edition": "Datacenter",
            "os_language": "en-us",
        },
    )

    assert response.status_code == 202, response.text
    job = jobs_pg.get_job(response.json()["job_id"])
    assert job["args"]["source_media_path"] == r"E:\ISOs\SERVER_EVAL_x64FRE_en-us.iso"
    assert job["args"]["image_name"] == "Windows Server 2025 Datacenter Evaluation (Desktop Experience)"
    assert job["args"]["image_index"] == 4
    assert job["args"]["os_version"] == "Windows Server 2025"
    assert job["args"]["os_edition"] == "Datacenter"
    assert job["args"]["os_language"] == "en-us"
    assert job["cmd"][job["cmd"].index("--source-media-path") + 1] == (
        r"E:\ISOs\SERVER_EVAL_x64FRE_en-us.iso"
    )
    assert job["cmd"][job["cmd"].index("--image-index") + 1] == "4"
    assert job["cmd"][job["cmd"].index("--os-edition") + 1] == "Datacenter"


def test_osdeploy_publish_endpoint_enqueues_real_upload_job(osdeploy_client, pg_conn, tmp_path):
    from web import jobs_pg, osdeploy_pg

    iso = tmp_path / "osdeploy-server-amd64-publish.iso"
    iso.write_bytes(b"iso")
    artifact = _create_osdeploy_artifact(
        pg_conn,
        iso_path=str(iso),
        proxmox_volid=None,
    )

    response = osdeploy_client.post(
        f"/api/osdeploy/v1/artifacts/{artifact['id']}/publish",
        json={"node": "pve", "storage": "local"},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["job_type"] == "osdeploy_publish_iso"
    assert body["artifact"]["ready"] is False
    assert body["artifact"]["proxmox_volid"] is None
    assert body["artifact"]["publish_job_url"] == f"/jobs/{body['job_id']}"
    assert body["artifact"]["publish_log_url"] == f"/api/jobs/{body['job_id']}/log"
    assert body["target_volid"] == "local:iso/osdeploy-server-amd64-publish.iso"

    job = jobs_pg.get_job(body["job_id"])
    assert job["job_type"] == "osdeploy_publish_iso"
    assert "osdeploy_publish_job.py" in " ".join(job["cmd"])
    assert "--artifact-id" in job["cmd"]
    assert artifact["id"] in job["cmd"]
    assert job["args"]["target_volid"] == "local:iso/osdeploy-server-amd64-publish.iso"

    persisted = osdeploy_pg.get_artifact(pg_conn, artifact["id"])
    assert persisted["proxmox_volid"] is None
    assert persisted["publish_job_id"] == body["job_id"]


def test_osdeploy_publish_endpoint_blocks_missing_local_iso(osdeploy_client, pg_conn, tmp_path):
    artifact = _create_osdeploy_artifact(
        pg_conn,
        iso_path=str(tmp_path / "missing-osdeploy-server.iso"),
        proxmox_volid=None,
    )

    response = osdeploy_client.post(
        f"/api/osdeploy/v1/artifacts/{artifact['id']}/publish",
        json={"node": "pve", "storage": "local"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "OSDeploy artifact ISO is missing from local output storage."


def test_osdeploy_artifact_status_includes_build_and_publish_job_state(osdeploy_client, pg_conn):
    from web import jobs_pg

    artifact = _create_osdeploy_artifact(
        pg_conn,
        proxmox_volid=None,
        build_job_id="job-build-status",
        publish_job_id="job-publish-status",
    )
    jobs_pg.enqueue(
        job_id="job-build-status",
        job_type="osdeploy_build_iso",
        playbook="osdeploy_remote_build",
        cmd=["python", "osdeploy_remote_build.py"],
        args={"artifact_id": artifact["id"]},
    )
    jobs_pg.enqueue(
        job_id="job-publish-status",
        job_type="osdeploy_publish_iso",
        playbook="osdeploy_publish_iso",
        cmd=["python", "osdeploy_publish_job.py"],
        args={"artifact_id": artifact["id"]},
    )

    response = osdeploy_client.get(f"/api/osdeploy/v1/artifacts/{artifact['id']}/status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["artifact"]["id"] == artifact["id"]
    assert body["artifact"]["readiness"] == "missing_proxmox_volid"
    assert body["build_job"]["id"] == "job-build-status"
    assert body["build_job"]["status"] == "pending"
    assert body["build_job"]["url"] == "/jobs/job-build-status"
    assert body["build_job"]["log_url"] == "/api/jobs/job-build-status/log"
    assert body["publish_job"]["id"] == "job-publish-status"
    assert body["publish_job"]["status"] == "pending"
    assert body["publish_job"]["url"] == "/jobs/job-publish-status"
    assert body["publish_job"]["log_url"] == "/api/jobs/job-publish-status/log"


def test_osdeploy_run_identity_and_pe_callbacks(osdeploy_client, pg_conn):
    artifact = _create_osdeploy_artifact(pg_conn)
    created = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(artifact["id"]),
    ).json()["run"]

    identity = osdeploy_client.post(
        f"/api/osdeploy/v1/runs/{created['run_id']}/identity",
        json={
            "vmid": 123,
            "vm_uuid": "uuid-123",
            "mac": "AA:BB:CC:DD:EE:FF",
            "node": "pve",
            "computer_name": "ServerLab001",
        },
    )
    assert identity.status_code == 200, identity.text
    assert identity.json()["run"]["vmid"] == 123

    registered = osdeploy_client.post(
        f"/api/osdeploy/v1/runs/{created['run_id']}/pe/register",
        json={"client_version": "0.1.0"},
    )
    assert registered.status_code == 200, registered.text
    assert registered.json()["run"]["state"] == "pe_registered"
    assert registered.json()["bearer_token"]
    assert registered.json()["package_url"] == (
        f"/api/osdeploy/v1/pe/package/{created['run_id']}"
    )

    package = osdeploy_client.get(
        f"/api/osdeploy/v1/pe/package/{created['run_id']}",
        headers=_bearer(registered.json()["bearer_token"]),
    )
    assert package.status_code == 200, package.text
    package_body = package.json()
    assert package_body["schema_version"] == 1
    assert package_body["run_id"] == created["run_id"]
    assert package_body["workflow_name"] == created["workflow_name"]
    assert package_body["deployment"]["path"] == "osdeploy_v2"
    assert package_body["identity"]["computer_name"] == "ServerLab001"
    assert package_body["artifact"]["source_image_index"] == 4
    assert package_body["artifact"]["apply_image_index"] == 4
    assert package_body["server_settings"]["role"] == "base"
    assert package_body["server_settings"]["os_version"] == "Windows Server 2025"
    assert package_body["payloads"]["osd_client"]["url"].endswith(
        f"/osd/v2/agent/package/{created['run_id']}?phase=full_os"
    )
    assert package_body["agent"]["phase"] == "full_os"
    assert package_body["agent"]["role"] == "base"
    assert package_body["agent"]["bootstrap_token"]
    _assert_local_admin(package_body["local_admin"])
    detail = osdeploy_client.get(f"/api/osdeploy/v1/runs/{created['run_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["run"]["local_admin"] == package_body["local_admin"]

    event = osdeploy_client.post(
        f"/api/osdeploy/v1/runs/{created['run_id']}/events",
        headers=_bearer(registered.json()["bearer_token"]),
        json={
            "phase": "pe",
            "event_type": "image_apply_started",
            "message": "Applying Server image",
        },
    )
    assert event.status_code == 200, event.text


def test_osdeploy_pe_package_uses_output_image_index_when_manifest_records_one(
    osdeploy_client,
    pg_conn,
    tmp_path,
):
    manifest = tmp_path / "osdeploy-manifest.json"
    manifest.write_text('{"image_index":4,"output_image_index":1}', encoding="utf-8")
    artifact = _create_osdeploy_artifact(pg_conn, manifest_path=str(manifest))
    created = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(artifact["id"]),
    ).json()["run"]
    osdeploy_client.post(
        f"/api/osdeploy/v1/runs/{created['run_id']}/identity",
        json={"vmid": 123, "vm_uuid": "uuid-123", "mac": "AA:BB:CC:DD:EE:FF", "node": "pve"},
    )
    registered = osdeploy_client.post(
        f"/api/osdeploy/v1/runs/{created['run_id']}/pe/register",
        json={"client_version": "0.1.0"},
    )
    assert registered.status_code == 200, registered.text

    package = osdeploy_client.get(
        f"/api/osdeploy/v1/pe/package/{created['run_id']}",
        headers=_bearer(registered.json()["bearer_token"]),
    )

    assert package.status_code == 200, package.text
    artifact_payload = package.json()["artifact"]
    assert artifact_payload["source_image_index"] == 4
    assert artifact_payload["output_image_index"] == 1
    assert artifact_payload["apply_image_index"] == 1


def test_osdeploy_pe_events_require_run_scoped_bearer(osdeploy_client, pg_conn):
    artifact = _create_osdeploy_artifact(pg_conn)
    created = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(artifact["id"], vm_name="OSDEPLOY-EVENT-AUTH"),
    ).json()["run"]

    missing = osdeploy_client.post(
        f"/api/osdeploy/v1/runs/{created['run_id']}/events",
        json={
            "phase": "pe",
            "event_type": "unauthenticated_event",
        },
    )
    assert missing.status_code == 401

    wrong_token = osdeploy_client.post(
        f"/api/osdeploy/v1/runs/{created['run_id']}/events",
        headers=_bearer("not-a-valid-token"),
        json={
            "phase": "pe",
            "event_type": "wrong_token_event",
        },
    )
    assert wrong_token.status_code == 401


def test_osdeploy_pe_events_advance_v2_plan_steps(osdeploy_client, pg_conn):
    artifact = _create_osdeploy_artifact(pg_conn)
    created = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(artifact["id"], vm_name="OSDEPLOY-PE-STEPS"),
    ).json()["run"]
    osdeploy_client.post(
        f"/api/osdeploy/v1/runs/{created['run_id']}/identity",
        json={
            "vmid": 125,
            "vm_uuid": "uuid-125",
            "mac": "AA:BB:CC:DD:EE:11",
            "node": "pve",
            "computer_name": "OSDEPLOY-PE",
        },
    )

    registered = osdeploy_client.post(
        f"/api/osdeploy/v1/runs/{created['run_id']}/pe/register",
        json={"client_version": "0.1.0"},
    )
    token = registered.json()["bearer_token"]
    detail = osdeploy_client.get(f"/api/osdeploy/v1/runs/{created['run_id']}").json()
    states = {step["kind"]: step["state"] for step in detail["v2_steps"]}
    assert states["osdeploy_preflight"] == "done"
    assert states["apply_wim"] == "pending"

    for event_type in [
        "osdeploy_image_applied",
        "osdeploy_drivers_applied",
        "osdeploy_setupcomplete_staged",
        "osdeploy_boot_files_staged",
    ]:
        response = osdeploy_client.post(
            f"/api/osdeploy/v1/runs/{created['run_id']}/events",
            headers=_bearer(token),
            json={
                "phase": "pe",
                "event_type": event_type,
                "message": event_type,
            },
        )
        assert response.status_code == 200, response.text

    detail = osdeploy_client.get(f"/api/osdeploy/v1/runs/{created['run_id']}").json()
    assert detail["run"]["osdeploy_started_at"]
    assert detail["run"]["osdeploy_finished_at"]
    states = {step["kind"]: step["state"] for step in detail["v2_steps"]}
    for kind in [
        "apply_wim",
        "apply_driver_package",
        "prepare_windows_setup",
        "bake_boot_entry",
        "stage_osd_client",
        "stage_autopilot_agent",
    ]:
        assert states[kind] == "done"
    assert states["wait_agent_heartbeat"] == "pending"


def test_osdeploy_agent_heartbeat_completes_server_base_readiness(osdeploy_client, pg_conn):
    artifact = _create_osdeploy_artifact(pg_conn)
    created = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(artifact["id"], vm_name="OSDEPLOY-HB"),
    ).json()["run"]
    osdeploy_client.post(
        f"/api/osdeploy/v1/runs/{created['run_id']}/identity",
        json={
            "vmid": 124,
            "vm_uuid": "uuid-124",
            "mac": "AA:BB:CC:DD:EE:00",
            "node": "pve",
            "computer_name": "OSDEPLOY-HB",
        },
    )
    registered = osdeploy_client.post(
        f"/api/osdeploy/v1/runs/{created['run_id']}/pe/register",
        json={"client_version": "0.1.0"},
    )
    package = osdeploy_client.get(
        f"/api/osdeploy/v1/pe/package/{created['run_id']}",
        headers=_bearer(registered.json()["bearer_token"]),
    ).json()
    bootstrap = osdeploy_client.post(
        "/api/agent/v1/bootstrap",
        headers=_bearer(package["agent"]["bootstrap_token"]),
        json={
            "agent_id": "agent-osdeploy-hb",
            "run_id": created["run_id"],
            "phase": "osdeploy",
            "vmid": 124,
            "vm_uuid": "uuid-124",
            "computer_name": "OSDEPLOY-HB",
            "agent_version": "0.1.0",
        },
    )
    assert bootstrap.status_code == 200, bootstrap.text

    heartbeat = osdeploy_client.post(
        "/api/agent/v1/heartbeat",
        headers=_bearer(bootstrap.json()["agent_token"]),
        json={
            "agent_id": "agent-osdeploy-hb",
            "vmid": 124,
            "vm_uuid": "uuid-124",
            "computer_name": "OSDEPLOY-HB",
            "os_name": "Microsoft Windows Server 2025 Datacenter",
            "qga_service_name": "QEMU-GA",
            "qga_state": "running",
            "current_run_id": created["run_id"],
            "current_phase": "full_os",
            "agent_version": "0.1.0",
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text

    detail = osdeploy_client.get(f"/api/osdeploy/v1/runs/{created['run_id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["run"]["state"] == "complete"
    assert body["run"]["first_heartbeat_at"]
    assert body["readiness"]["state"] == "complete"
    assert body["readiness"]["agent_status"] == "online"
    assert body["readiness"]["qga_status"] == "running"
    assert {step["state"] for step in body["v2_steps"]} == {"done"}


def test_osdeploy_heartbeat_does_not_complete_future_role_step(pg_conn):
    from web import agent_telemetry_pg, osdeploy_cache, osdeploy_pg, sequences_pg, ts_engine_pg

    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)
    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    agent_telemetry_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)
    osdeploy_cache.reset_for_tests(pg_conn)
    osdeploy_cache.init(pg_conn)
    osdeploy_pg.reset_for_tests(pg_conn)
    osdeploy_pg.init(pg_conn)
    artifact = _create_osdeploy_artifact(pg_conn)
    run = osdeploy_pg.create_run(
        pg_conn,
        artifact_id=artifact["id"],
        vm_name="OSDEPLOY-FILE",
        node="pve",
        iso_storage="local",
        storage="local-lvm",
        network_bridge="vmbr0",
        server_role="file_server",
    )

    completed = osdeploy_pg.mark_complete_from_heartbeat(
        pg_conn,
        run_id=run["run_id"],
        agent_id="agent-role-pending",
        heartbeat={
            "computer_name": "OSDEPLOY-FILE",
            "os_name": "Microsoft Windows Server 2025 Datacenter",
            "qga_state": "running",
            "current_phase": "full_os",
        },
    )

    assert completed["state"] == "role_pending"
    readiness = osdeploy_pg.get_readiness(pg_conn, run["run_id"])
    assert readiness["state"] == "role_pending"
    assert readiness["server_role_status"] == "pending"
    states = {
        step["kind"]: step["state"]
        for step in ts_engine_pg.list_run_steps(pg_conn, run["run_id"])
    }
    assert states["wait_agent_heartbeat"] == "done"
    assert states["configure_file_server_role"] == "pending"


def test_osdeploy_role_step_success_marks_role_ready(osdeploy_client, pg_conn):
    from web import osdeploy_pg

    artifact = _create_osdeploy_artifact(pg_conn)
    created = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(
            artifact["id"],
            vm_name="OSDEPLOY-FILE",
            server_role="file_server",
            role_options=_file_server_options(),
        ),
    )
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    osdeploy_pg.mark_complete_from_heartbeat(
        pg_conn,
        run_id=run["run_id"],
        agent_id="agent-osdeploy-file",
        heartbeat={
            "computer_name": "OSDEPLOY-FILE",
            "os_name": "Microsoft Windows Server 2025 Datacenter",
            "qga_state": "running",
            "current_phase": "full_os",
        },
    )

    registered = osdeploy_client.post(
        "/osd/v2/agent/register",
        json={
            "run_id": run["run_id"],
            "agent_id": "agent-osdeploy-file",
            "phase": "full_os",
            "capabilities": ["configure_file_server_role"],
        },
    )
    assert registered.status_code == 200, registered.text
    bearer = registered.json()["bearer_token"]
    next_step = osdeploy_client.post(
        "/osd/v2/agent/next",
        headers=_bearer(bearer),
        json={
            "run_id": run["run_id"],
            "agent_id": "agent-osdeploy-file",
            "phase": "full_os",
        },
    )
    assert next_step.status_code == 200, next_step.text
    action = next_step.json()["actions"][0]
    assert action["kind"] == "configure_file_server_role"
    assert action["params"]["share_name"] == "Shared"

    result = osdeploy_client.post(
        f"/osd/v2/agent/step/{action['step_id']}/result",
        headers=_bearer(bearer),
        json={
            "run_id": run["run_id"],
            "agent_id": "agent-osdeploy-file",
            "phase": "full_os",
            "status": "success",
            "message": "File Server role configured",
            "data": {"share_name": "Shared", "share_path": r"C:\Shares\Shared"},
        },
    )
    assert result.status_code == 200, result.text
    readiness = osdeploy_pg.get_readiness(pg_conn, run["run_id"])
    assert readiness["state"] == "complete"
    assert readiness["server_role_status"] == "file_server_ready"

    pg_conn.execute(
        "UPDATE osdeploy_runs SET state = 'role_pending' WHERE run_id = %s",
        (run["run_id"],),
    )
    pg_conn.execute(
        """
        UPDATE osdeploy_readiness
        SET state = 'role_pending',
            server_role_status = 'pending'
        WHERE run_id = %s
        """,
        (run["run_id"],),
    )
    pg_conn.commit()

    osdeploy_pg.mark_complete_from_heartbeat(
        pg_conn,
        run_id=run["run_id"],
        agent_id="agent-osdeploy-file",
        heartbeat={
            "computer_name": "OSDEPLOY-FILE",
            "os_name": "Microsoft Windows Server 2025 Datacenter",
            "qga_state": "running",
            "current_phase": "full_os",
        },
    )
    readiness = osdeploy_pg.get_readiness(pg_conn, run["run_id"])
    assert readiness["state"] == "complete"
    assert readiness["server_role_status"] == "file_server_ready"


def test_osdeploy_full_os_client_does_not_claim_role_steps(osdeploy_client, pg_conn):
    from web import osdeploy_pg, ts_engine_pg

    artifact = _create_osdeploy_artifact(pg_conn)
    created = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(
            artifact["id"],
            vm_name="OSDEPLOY-FILE",
            server_role="file_server",
            role_options=_file_server_options(),
        ),
    )
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    osdeploy_pg.mark_complete_from_heartbeat(
        pg_conn,
        run_id=run["run_id"],
        agent_id="agent-osdeploy-file",
        heartbeat={
            "computer_name": "OSDEPLOY-FILE",
            "os_name": "Microsoft Windows Server 2025 Datacenter",
            "qga_state": "running",
            "current_phase": "full_os",
        },
    )
    ts_engine_pg.mark_steps_done_by_kind(
        pg_conn,
        run_id=run["run_id"],
        kinds=["install_autopilot_agent"],
        agent_id="osd-fullos-test",
    )

    registered = osdeploy_client.post(
        "/osd/v2/agent/register",
        json={
            "run_id": run["run_id"],
            "agent_id": f"osd-fullos-{run['run_id'][:8]}",
            "phase": "full_os",
            "capabilities": ["install_autopilot_agent", "wait_agent_heartbeat"],
        },
    )
    assert registered.status_code == 200, registered.text
    bearer = registered.json()["bearer_token"]
    next_step = osdeploy_client.post(
        "/osd/v2/agent/next",
        headers=_bearer(bearer),
        json={
            "run_id": run["run_id"],
            "agent_id": f"osd-fullos-{run['run_id'][:8]}",
            "phase": "full_os",
            "capabilities": ["install_autopilot_agent", "wait_agent_heartbeat"],
        },
    )
    assert next_step.status_code == 200, next_step.text
    assert next_step.json()["actions"] == []

    role_agent = osdeploy_client.post(
        "/osd/v2/agent/register",
        json={
            "run_id": run["run_id"],
            "agent_id": "agent-osdeploy-file",
            "phase": "full_os",
            "capabilities": ["configure_file_server_role"],
        },
    )
    assert role_agent.status_code == 200, role_agent.text
    role_bearer = role_agent.json()["bearer_token"]
    role_next = osdeploy_client.post(
        "/osd/v2/agent/next",
        headers=_bearer(role_bearer),
        json={
            "run_id": run["run_id"],
            "agent_id": "agent-osdeploy-file",
            "phase": "full_os",
            "capabilities": ["configure_file_server_role"],
        },
    )
    assert role_next.status_code == 200, role_next.text
    assert role_next.json()["actions"][0]["kind"] == "configure_file_server_role"


def test_osdeploy_lab_bundle_creates_child_runs_in_dependency_order(
    osdeploy_client,
    pg_conn,
):
    artifact = _create_osdeploy_artifact(pg_conn)
    creds = _create_role_credentials(pg_conn)
    lab_options = _lab_bundle_options(
        domain_join_credential_id=creds["domain_join"],
        children=[
            {
                "role": "isolated_domain_controller",
                "vm_name": "LAB-DC01",
                "role_options": _isolated_dc_options(
                    forest_admin_credential_id=creds["forest_admin"],
                    dsrm_credential_id=creds["dsrm"],
                ),
            },
            {
                "role": "file_server",
                "vm_name": "LAB-FS01",
                "role_options": _file_server_options(),
            },
            {
                "role": "mecm_prereq",
                "vm_name": "LAB-CM01",
                "role_options": _mecm_options(),
            },
        ],
    )

    response = osdeploy_client.post(
        "/api/osdeploy/v1/bundles",
        json=_run_payload(
            artifact["id"],
            server_role="lab_in_a_box",
            role_options=lab_options,
        ),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["bundle"]["server_role"] == "lab_in_a_box"
    children = body["children"]
    assert [child["server_role"] for child in children] == [
        "isolated_domain_controller",
        "file_server",
        "mecm_prereq",
    ]
    assert [child["dependency_order"] for child in children] == [1, 2, 3]
    child_steps = {}
    for child in children:
        detail = osdeploy_client.get(f"/api/osdeploy/v1/runs/{child['child_run_id']}")
        assert detail.status_code == 200, detail.text
        child_steps[child["server_role"]] = [
            step["kind"] for step in detail.json()["v2_steps"]
        ]
    assert child_steps["file_server"].index("join_domain_role") < child_steps["file_server"].index("configure_file_server_role")
    assert child_steps["file_server"].index("verify_ad_domain_join") < child_steps["file_server"].index("configure_file_server_role")
    assert child_steps["mecm_prereq"].index("join_domain_role") < child_steps["mecm_prereq"].index("configure_mecm_prereq_role")
    assert child_steps["mecm_prereq"].index("verify_ad_domain_join") < child_steps["mecm_prereq"].index("configure_mecm_prereq_role")


def test_osdeploy_lab_domain_join_step_resolves_secret_only_at_claim(
    osdeploy_client,
    pg_conn,
):
    from web import osdeploy_pg

    artifact = _create_osdeploy_artifact(pg_conn)
    creds = _create_role_credentials(pg_conn)
    lab_options = _lab_bundle_options(
        domain_join_credential_id=creds["domain_join"],
        children=[
            {
                "role": "isolated_domain_controller",
                "vm_name": "LAB-DC01",
                "role_options": _isolated_dc_options(
                    forest_admin_credential_id=creds["forest_admin"],
                    dsrm_credential_id=creds["dsrm"],
                ),
            },
            {
                "role": "file_server",
                "vm_name": "LAB-FS01",
                "role_options": _file_server_options(),
            },
            {
                "role": "mecm_prereq",
                "vm_name": "LAB-CM01",
                "role_options": _mecm_options(),
            },
        ],
    )
    created = osdeploy_client.post(
        "/api/osdeploy/v1/bundles",
        json=_run_payload(
            artifact["id"],
            server_role="lab_in_a_box",
            role_options=lab_options,
        ),
    )
    assert created.status_code == 201, created.text
    file_child = next(
        child for child in created.json()["children"]
        if child["server_role"] == "file_server"
    )
    run = osdeploy_pg.get_run(pg_conn, file_child["child_run_id"])
    assert "password" not in json.dumps(run["role_options"]).lower()

    osdeploy_pg.mark_complete_from_heartbeat(
        pg_conn,
        run_id=file_child["child_run_id"],
        agent_id="agent-lab-fs",
        heartbeat={
            "computer_name": "LAB-FS01",
            "os_name": "Microsoft Windows Server 2025 Datacenter",
            "qga_state": "running",
            "current_phase": "full_os",
        },
    )
    states = {
        step["kind"]: step["state"]
        for step in osdeploy_pg.ts_engine_pg.list_run_steps(pg_conn, file_child["child_run_id"])
    }
    assert states["join_domain_role"] == "pending"
    assert states["verify_ad_domain_join"] == "pending"
    registered = osdeploy_client.post(
        "/osd/v2/agent/register",
        json={
            "run_id": file_child["child_run_id"],
            "agent_id": "agent-lab-fs",
            "phase": "full_os",
            "capabilities": ["join_domain_role"],
        },
    )
    assert registered.status_code == 200, registered.text
    next_step = osdeploy_client.post(
        "/osd/v2/agent/next",
        headers=_bearer(registered.json()["bearer_token"]),
        json={
            "run_id": file_child["child_run_id"],
            "agent_id": "agent-lab-fs",
            "phase": "full_os",
        },
    )
    assert next_step.status_code == 200, next_step.text
    action = next_step.json()["actions"][0]
    assert action["kind"] == "join_domain_role"
    assert action["params"]["domain_fqdn"] == "lab.gell.one"
    assert action["params"]["domain_join_username"] == "LAB\\joiner"
    assert action["params"]["domain_join_password"] == "secret"
    refreshed = osdeploy_pg.get_run(pg_conn, file_child["child_run_id"])
    assert "password" not in json.dumps(refreshed["role_options"]).lower()


def test_osdeploy_lab_bundle_state_rolls_up_from_child_runs(
    osdeploy_client,
    pg_conn,
):
    from web import osdeploy_pg

    artifact = _create_osdeploy_artifact(pg_conn)
    creds = _create_role_credentials(pg_conn)
    created = osdeploy_client.post(
        "/api/osdeploy/v1/bundles",
        json=_run_payload(
            artifact["id"],
            server_role="lab_in_a_box",
            role_options=_lab_bundle_options(
                domain_join_credential_id=creds["domain_join"],
                children=[
                    {
                        "role": "isolated_domain_controller",
                        "vm_name": "LAB-DC01",
                        "role_options": _isolated_dc_options(
                            forest_admin_credential_id=creds["forest_admin"],
                            dsrm_credential_id=creds["dsrm"],
                        ),
                    },
                    {
                        "role": "file_server",
                        "vm_name": "LAB-FS01",
                        "role_options": _file_server_options(),
                    },
                    {
                        "role": "mecm_prereq",
                        "vm_name": "LAB-CM01",
                        "role_options": _mecm_options(),
                    },
                ],
            ),
        ),
    )
    assert created.status_code == 201, created.text
    bundle_id = created.json()["bundle"]["id"]
    children = created.json()["children"]

    final_steps = {
        "isolated_domain_controller": "verify_isolated_domain_controller_role",
        "file_server": "configure_file_server_role",
        "mecm_prereq": "configure_mecm_prereq_role",
    }
    for child in children:
        osdeploy_pg.mark_role_step_result(
            pg_conn,
            run_id=child["child_run_id"],
            step_kind=final_steps[child["server_role"]],
            agent_id=f"agent-{child['server_role']}",
            status="success",
        )

    row = pg_conn.execute(
        "SELECT state FROM osdeploy_role_bundles WHERE id=%s",
        (bundle_id,),
    ).fetchone()
    assert row["state"] == "complete"


def test_osdeploy_pe_register_matches_recorded_vm_identity(osdeploy_client, pg_conn):
    artifact = _create_osdeploy_artifact(pg_conn, build_sha="osdeploype")
    created = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(artifact["id"], vm_name="OSDEPLOY-PE"),
    ).json()["run"]
    osdeploy_client.post(
        f"/api/osdeploy/v1/runs/{created['run_id']}/identity",
        json={
            "vmid": 125,
            "vm_uuid": "ABCDEF12-3456-7890-ABCD-EF1234567890",
            "mac": "52:54:00:12:34:56",
            "node": "pve",
            "computer_name": "OSDEPLOY-PE",
        },
    )

    registered = osdeploy_client.post(
        "/api/osdeploy/v1/pe/register",
        json={
            "vm_uuid": "abcdef12-3456-7890-abcd-ef1234567890",
            "mac": "52-54-00-12-34-56",
            "architecture": "amd64",
            "build_sha": "osdeploype",
            "client_version": "0.1.0",
        },
    )

    assert registered.status_code == 200, registered.text
    body = registered.json()
    assert body["run_id"] == created["run_id"]
    assert body["workflow_name"] == created["workflow_name"]
    assert body["bearer_token"]
    assert body["package_url"] == f"/api/osdeploy/v1/pe/package/{created['run_id']}"


def test_osdeploy_public_bridge_routes_are_additive():
    from web import auth

    assert auth.is_exempt_path("/api/osdeploy/v1/pe/package/run-1")
    assert auth.is_exempt_path("/api/osdeploy/v1/runs/run-1")
    assert auth.is_exempt_path("/api/osdeploy/v1/runs/run-1/identity")
    assert auth.is_exempt_path("/api/osdeploy/v1/runs/run-1/events")
    assert not auth.is_exempt_path("/api/osdeploy/v1/runs")
    assert not auth.is_exempt_path("/api/osdeploy/v1/runs/run-1/provision")


def test_osdeploy_provision_endpoint_enqueues_dedicated_playbook(osdeploy_client, pg_conn):
    from web import jobs_pg

    artifact = _create_osdeploy_artifact(pg_conn)
    run = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(artifact["id"], vm_name="OSDEPLOY-PROVISION"),
    ).json()["run"]

    response = osdeploy_client.post(f"/api/osdeploy/v1/runs/{run['run_id']}/provision")

    assert response.status_code == 202, response.text
    job = jobs_pg.get_job(response.json()["job_id"])
    assert job["job_type"] == "provision_osdeploy"
    assert "provision_proxmox_osdeploy.yml" in " ".join(job["cmd"])
    assert job["args"]["osdeploy_run_id"] == run["run_id"]
    assert job["args"]["osdeploy_artifact_volid"] == artifact["proxmox_volid"]
    assert job["args"]["proxmox_node"] == "pve"
    assert job["args"]["proxmox_storage"] == "local-lvm"
    assert job["args"]["proxmox_bridge"] == "vmbr0"
    assert job["args"]["proxmox_virtio_iso"] == "local:iso/virtio-win.iso"


def test_osdeploy_provision_endpoint_passes_legacy_bios_for_non_secure_boot(
    osdeploy_client,
    pg_conn,
):
    from web import jobs_pg

    artifact = _create_osdeploy_artifact(pg_conn)
    run = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(
            artifact["id"],
            vm_name="OSDEPLOY-LEGACY",
            secure_boot=False,
        ),
    ).json()["run"]

    response = osdeploy_client.post(f"/api/osdeploy/v1/runs/{run['run_id']}/provision")

    assert response.status_code == 202, response.text
    job = jobs_pg.get_job(response.json()["job_id"])
    assert job["args"]["secure_boot"] is False
    assert job["args"]["vm_bios"] == "seabios"
    assert "-e" in job["cmd"]
    assert "vm_bios=seabios" in job["cmd"]


def test_osdeploy_provision_endpoint_blocks_missing_virtio_media(
    osdeploy_client,
    pg_conn,
    monkeypatch,
):
    from web import app as web_app

    artifact = _create_osdeploy_artifact(pg_conn)
    run = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(artifact["id"], vm_name="OSDEPLOY-NOVIRTIO"),
    ).json()["run"]
    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "proxmox_node": "pve",
        "proxmox_iso_storage": "local",
        "proxmox_storage": "local-lvm",
        "proxmox_bridge": "vmbr0",
    })
    monkeypatch.setattr(web_app, "_proxmox_api", lambda path, *args, **kwargs: {
        "/storage": [{"storage": "local", "content": "iso"}],
        "/nodes/pve/storage/local/content": [],
    }.get(path, []))

    response = osdeploy_client.post(f"/api/osdeploy/v1/runs/{run['run_id']}/provision")

    assert response.status_code == 409
    assert "VirtIO" in response.json()["detail"]


def test_osdeploy_run_archive_and_progress(osdeploy_client, pg_conn):
    artifact = _create_osdeploy_artifact(pg_conn)
    created = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json=_run_payload(artifact["id"]),
    ).json()["run"]

    archived = osdeploy_client.post(
        f"/api/osdeploy/v1/runs/{created['run_id']}/archive",
        params={"reason": "reviewed"},
    )
    assert archived.status_code == 200
    assert archived.json()["run"]["archived"] is True

    progress = osdeploy_client.get("/api/osdeploy/v1/progress", params={"include_archived": "1"})
    assert progress.status_code == 200
    assert progress.json()["summary"]["total"] == 1

    restored = osdeploy_client.post(f"/api/osdeploy/v1/runs/{created['run_id']}/unarchive")
    assert restored.status_code == 200
    assert restored.json()["run"]["archived"] is False
