from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROXMOX_GUEST_EXEC = (
    REPO_ROOT / "roles" / "common" / "tasks" / "_proxmox_guest_exec.yml"
)


def test_proxmox_guest_exec_retries_transient_initial_exec_timeout():
    content = PROXMOX_GUEST_EXEC.read_text()

    assert "register: _guest_exec_result" in content
    assert "_guest_exec_result.status == 200" in content
    assert "_guest_exec_result.json.data.pid is defined" in content
    assert "_exec_start_retries | default(6)" in content
    assert "_exec_start_delay | default(5)" in content
