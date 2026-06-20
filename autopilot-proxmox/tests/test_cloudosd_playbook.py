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


def test_proxmox_clone_role_retries_storage_lock_clone_failures():
    main = ROOT / "roles" / "proxmox_vm_clone" / "tasks" / "main.yml"
    wait = ROOT / "roles" / "common" / "tasks" / "wait_task.yml"
    retry = ROOT / "roles" / "proxmox_vm_clone" / "tasks" / "clone_storage_lock_retry.yml"

    main_text = main.read_text()
    wait_text = wait.read_text()
    retry_text = retry.read_text()

    assert "task_fail_on_error: false" in main_text
    assert "clone_storage_lock_retry.yml" in main_text
    assert "clone_storage_lock_retries" in main_text
    assert "'lock file' in" in main_text
    assert "task_fail_on_error | default(true) | bool" in wait_text
    assert "Retry clone after storage lock contention" in retry_text
    assert "Remove failed clone shell before storage-lock retry" in retry_text
    assert "Wait for retried clone task" in retry_text


def test_proxmox_clone_storage_lock_retry_window_handles_concurrent_clone_pressure():
    main = ROOT / "roles" / "proxmox_vm_clone" / "tasks" / "main.yml"
    retry = ROOT / "roles" / "proxmox_vm_clone" / "tasks" / "clone_storage_lock_retry.yml"

    main_text = main.read_text()
    retry_text = retry.read_text()

    assert "clone_storage_lock_retries | default(12)" in main_text
    assert "loop_var: clone_storage_lock_attempt" in main_text
    assert "clone_storage_lock_attempt | int + 1" in main_text
    assert "clone_storage_lock_retry_backoff_seconds | default(10)" in retry_text
    assert "clone_storage_lock_retry_jitter_seconds | default(3)" in retry_text
    assert "(vm_vmid | int) % 7" in retry_text
    assert "{{ item | int + 1 }}" not in retry_text


def test_cloudosd_playbook_retries_proxmox_config_lock_contention():
    inner = ROOT / "playbooks" / "_provision_proxmox_cloudosd_vm.yml"
    text = inner.read_text()

    config_task_names = [
        "Attach CloudOSD ISO at ide2",
        "Attach VirtIO drivers ISO at ide3",
        "Set boot order to CloudOSD ISO first",
        "Detach CloudOSD ISO after PE registration",
        "Ensure CloudOSD ISO is detached before disk boot",
        "Detach VirtIO ISO before disk boot",
        "Set boot order to installed Windows disk",
    ]

    for name in config_task_names:
        block = text.split(f"- name: {name}", 1)[1].split("\n    - name:", 1)[0]
        assert "method: PUT" in block
        assert "register: _cloudosd_" in block
        assert "cloudosd_config_lock_retries" in block
        assert "cloudosd_config_lock_delay_seconds" in block
        assert "until:" in block
        assert "status | default(0) | int) == 200" in block


def test_cloudosd_playbook_verifies_vm_start_tasks_before_waiting_for_guest_callbacks():
    inner = ROOT / "playbooks" / "_provision_proxmox_cloudosd_vm.yml"
    text = inner.read_text()

    iso_start_block = text.split("- name: Start CloudOSD VM", 1)[1].split(
        "\n    - name: Wait for CloudOSD PE registration",
        1,
    )[0]
    disk_start_block = text.split("- name: Start installed Windows from disk", 1)[1].split(
        "\n    - name: Wait for installed AutopilotAgent heartbeat completion",
        1,
    )[0]

    assert "register: _cloudosd_start_iso_boot" in iso_start_block
    assert "Fail if CloudOSD ISO boot start returned no Proxmox task" in iso_start_block
    assert "/tasks/{{ _cloudosd_start_iso_boot.json.data }}/status" in iso_start_block
    assert "cloudosd_start_task_retries | default(600)" in iso_start_block
    assert "cloudosd_start_task_delay_seconds | default(1)" in iso_start_block
    assert "Verify CloudOSD ISO boot start task succeeded" in iso_start_block
    assert "_cloudosd_iso_boot_running.json.data.status | default('') != 'running'" in iso_start_block
    assert "Wait for CloudOSD VM to be running after ISO boot start" in iso_start_block
    assert "/qemu/{{ vm_vmid }}/status/current" in iso_start_block

    assert "register: _cloudosd_start_disk_boot" in disk_start_block
    assert "Fail if installed Windows start returned no Proxmox task" in disk_start_block
    assert "/tasks/{{ _cloudosd_start_disk_boot.json.data }}/status" in disk_start_block
    assert "cloudosd_start_task_retries | default(600)" in disk_start_block
    assert "cloudosd_start_task_delay_seconds | default(1)" in disk_start_block
    assert "Verify installed Windows start task succeeded" in disk_start_block
    assert "_cloudosd_disk_boot_running.json.data.status | default('') != 'running'" in disk_start_block
    assert "Wait for CloudOSD VM to be running after disk boot start" in disk_start_block
    assert "/qemu/{{ vm_vmid }}/status/current" in disk_start_block


def test_resize_disk_waits_for_proxmox_task_to_release_config_lock():
    """PVE's qemu/{vmid}/resize endpoint returns a task UPID immediately and
    runs the actual resize in the background. The background task holds
    lock-{vmid}.conf for its entire duration (4-5 minutes seen on a busy ZFS
    pool under parallel-fan-out load). If resize_disk.yml does not wait for
    the UPID before returning to the caller, the next config-modifying tasks
    (Attach CloudOSD ISO, Attach VirtIO ISO, Set boot order) race against the
    still-held lock and exhaust their 12 x 5s retry budget with HTTP 500
    'can't lock file ... got timeout' errors. This is the root cause of the
    E2E40-WK-03 exit_code=2 failure observed at 2026-05-26 22:31:17 UTC.
    """
    resize = ROOT / "roles" / "proxmox_vm_clone" / "tasks" / "resize_disk.yml"
    text = resize.read_text()

    resize_task_block = text.split("Resize cloned VM disk if larger than template", 1)[1]

    assert "register: _resize_result" in resize_task_block, (
        "resize PUT must register the response so we can wait on the returned UPID; "
        "without this, the background resize task holds the config lock past the "
        "playbook's retry budget."
    )

    assert "Wait for Proxmox resize task to complete" in text, (
        "resize_disk.yml must explicitly wait for the resize UPID before returning."
    )

    wait_block = text.split("Wait for Proxmox resize task to complete", 1)[1]
    assert "wait_task.yml" in wait_block
    assert "_task_upid: \"{{ _resize_result.json.data }}\"" in wait_block, (
        "wait_task.yml must be passed the resize UPID via _task_upid."
    )
    assert "_resize_result.status | default(0) | int == 200" in wait_block, (
        "wait must be gated on a successful resize PUT (skip when the resize was "
        "not issued because vm_disk_size_gb <= template_disk_size_gb)."
    )


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
