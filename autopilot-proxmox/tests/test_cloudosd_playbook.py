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


def test_cloudosd_playbook_polling_handles_missing_json_response():
    inner = ROOT / "playbooks" / "_provision_proxmox_cloudosd_vm.yml"
    text = inner.read_text()

    assert ".json.run.state" not in text
    assert ".json.run.osdcloud_finished_at" not in text
    assert "get('json', {}).get('run', {}).get('state', '')" in text


def test_proxmox_clone_role_retries_automatic_vmid_collisions():
    clone = ROOT / "roles" / "proxmox_vm_clone" / "tasks" / "clone_vm.yml"
    attempt = ROOT / "roles" / "proxmox_vm_clone" / "tasks" / "clone_vm_attempt.yml"

    clone_text = clone.read_text()
    attempt_text = attempt.read_text()

    assert "clone_vm_attempt.yml" in clone_text
    assert "range(0, 20)" in clone_text
    assert "default(vm_vmid)" in clone_text
    assert "Capture first VMID candidate" in clone_text
    capture_block = clone_text.split("- name: Capture first VMID candidate", 1)[1].split(
        "- name: Clone template with automatic VMID collision retry", 1
    )[0]
    assert "_initial_auto_vmid: \"{{ vm_vmid | int }}\"" in capture_block
    assert "when:" not in capture_block
    assert "config file already exists" in attempt_text
    assert "Record successful automatic clone attempt" in attempt_text
    assert attempt_text.count("_clone_result is not defined") >= 3


def test_proxmox_clone_role_applies_uefi_for_secure_boot_clones():
    update_config = ROOT / "roles" / "proxmox_vm_clone" / "tasks" / "update_config.yml"
    text = update_config.read_text()

    assert "Resolve requested firmware features" in text
    assert "_uefi_requested" in text
    assert "Apply UEFI firmware when Secure Boot or TPM is requested" in text
    assert "'bios': 'ovmf'" in text
    assert "Add EFI disk when UEFI firmware is requested and missing" in text
    assert "efidisk0" in text
    assert "pre-enrolled-keys=" in text
    assert "Add TPM state when requested and missing" in text
    assert "tpmstate0" in text
    assert "_explicit_legacy_bios" in text
