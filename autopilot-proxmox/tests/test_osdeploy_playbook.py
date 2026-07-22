from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_osdeploy_playbooks_exist_and_use_osdeploy_routes():
    wrapper = ROOT / "playbooks" / "provision_proxmox_osdeploy.yml"
    inner = ROOT / "playbooks" / "_provision_proxmox_osdeploy_vm.yml"

    assert wrapper.exists()
    assert inner.exists()
    assert "provision_proxmox_winpe" not in wrapper.read_text()

    text = inner.read_text()
    assert "/api/osdeploy/v1/runs/{{ osdeploy_run_id }}/identity" in text
    assert "computer_name: \"{{ vm_name }}\"" in text
    assert "/api/osdeploy/v1/runs/{{ osdeploy_run_id }}" in text
    assert "osdeploy_artifact_volid" in text
    assert "_skip_chassis_type_smbios_file: true" in text
    assert "boot: \"order=ide2;scsi0\"" in text
    assert "Wait for OSDeploy PE phase completion" in text
    assert "osdeploy_finished_at" in text
    assert "delete: \"ide2\"" in text
    assert "delete: \"ide3\"" in text
    assert "boot: \"order=scsi0\"" in text
    assert "Start installed Windows from disk" in text
    assert "AutopilotAgent heartbeat" in text


def test_osdeploy_playbook_keeps_install_media_until_pe_phase_completes():
    inner = ROOT / "playbooks" / "_provision_proxmox_osdeploy_vm.yml"
    text = inner.read_text()

    wait_index = text.index("Wait for OSDeploy PE phase completion")
    detach_index = text.index("Detach OSDeploy ISO before disk boot")
    assert wait_index < detach_index


def test_upload_hash_playbook_resolves_selected_entra_vault_var_names():
    playbook = yaml.safe_load((ROOT / "playbooks" / "upload_hashes.yml").read_text())
    upload_task = next(
        task for task in playbook[0]["tasks"]
        if task.get("name") == "Upload hashes to Autopilot"
    )

    env = upload_task["environment"]
    assert "upload_entra_app_id_var" in env["ENTRA_APP_ID"]
    assert "vault_entra_app_id" in env["ENTRA_APP_ID"]
    assert "upload_entra_tenant_id_var" in env["ENTRA_TENANT_ID"]
    assert "vault_entra_tenant_id" in env["ENTRA_TENANT_ID"]
    assert "upload_entra_app_secret_var" in env["ENTRA_APP_SECRET"]
    assert "vault_entra_app_secret" in env["ENTRA_APP_SECRET"]
