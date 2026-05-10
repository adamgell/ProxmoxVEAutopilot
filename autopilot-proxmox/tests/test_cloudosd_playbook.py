from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cloudosd_playbooks_are_additive_and_use_cloudosd_routes():
    wrapper = ROOT / "playbooks" / "provision_proxmox_cloudosd.yml"
    inner = ROOT / "playbooks" / "_provision_proxmox_cloudosd_vm.yml"

    assert wrapper.exists()
    assert inner.exists()
    assert "provision_proxmox_winpe" not in wrapper.read_text()

    text = inner.read_text()
    assert "/api/cloudosd/runs/{{ cloudosd_run_id }}/identity" in text
    assert "computer_name: \"{{ vm_name }}\"" in text
    assert "/api/cloudosd/runs/{{ cloudosd_run_id }}" in text
    assert "cloudosd_artifact_volid" in text
    assert "boot: \"order=ide2;scsi0\"" in text
    assert "Wait for CloudOSD PE phase completion" in text
    assert "delete: \"ide2\"" in text
    assert "delete: \"ide3\"" in text
    assert "boot: \"order=scsi0\"" in text
    assert "Start installed Windows from disk" in text
    assert "complete" in text
