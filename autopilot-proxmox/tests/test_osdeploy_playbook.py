from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_osdeploy_playbooks_are_additive_and_do_not_call_cloudosd_or_legacy_winpe():
    wrapper = ROOT / "playbooks" / "provision_proxmox_osdeploy.yml"
    inner = ROOT / "playbooks" / "_provision_proxmox_osdeploy_vm.yml"

    assert wrapper.exists()
    assert inner.exists()
    text = wrapper.read_text(encoding="utf-8") + "\n" + inner.read_text(encoding="utf-8")
    assert "cloudosd" not in text.lower()
    assert "winpe" not in text.lower()
    assert "_provision_proxmox_osdeploy_vm.yml" in wrapper.read_text(encoding="utf-8")
    assert "osdeploy_run_id" in text
    assert "OSDeploy v2 Proxmox Server provisioning placeholder" not in text
    assert "/api/osdeploy/v1/runs/{{ osdeploy_run_id }}/identity" in text
    assert "/api/osdeploy/v1/runs/{{ osdeploy_run_id }}/pe/register" in text
    assert "osdeploy_artifact_volid" in text
    assert "proxmox_virtio_iso is defined" in text
    assert "proxmox_virtio_iso | length > 0" in text


def test_osdeploy_worker_entrypoints_exist_for_queued_jobs():
    scripts = ROOT / "scripts"

    assert (scripts / "osdeploy_build_job.py").exists()
    assert (scripts / "osdeploy_cache_job.py").exists()
    assert (scripts / "osdeploy_publish_job.py").exists()


def test_osdeploy_keeps_virtio_iso_until_after_first_boot_readiness():
    inner = ROOT / "playbooks" / "_provision_proxmox_osdeploy_vm.yml"
    text = inner.read_text(encoding="utf-8")

    detach_name = "Detach VirtIO ISO after OSDeploy first-boot readiness"
    completion_name = "Wait for installed OSDeploy agent completion"
    disk_boot_name = "Start installed Windows Server from disk"

    assert detach_name in text
    assert text.index(disk_boot_name) < text.index(completion_name) < text.index(detach_name)


def test_osdeploy_repo_plan_exists_for_build_publish_and_e2e():
    plan = ROOT.parent / "docs" / "OSDEPLOY_V2_MATURITY_PLAN.md"

    text = plan.read_text(encoding="utf-8")

    assert "OSDeploy Windows Server Base" in text
    assert "build/publish artifact" in text
    assert "proxmox_virtio_iso" in text
    assert "osdeploy_build_remote" in text
    assert "osdeploy_build_ssh_key_path" in text
    assert "Live Proxmox E2E gate" in text
    assert "OSDeploy File Server" in text
    assert "OSDeploy Isolated Domain Controller" in text
    assert "OSDeploy MECM Prereq Baseline" in text
    assert "OSDeploy Lab in a Box" in text
