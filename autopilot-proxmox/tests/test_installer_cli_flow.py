import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-proxmox-ve.sh"


def installer_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        **os.environ,
        "INSTALLER_STATE_FILE": str(tmp_path / "foundation_state.json"),
        "INSTALLER_DETECT_FILE": str(tmp_path / "installer_detect.json"),
        "INSTALLER_FAILURE_FILE": str(tmp_path / "install-last-failure.json"),
        "INSTALLER_LOG_FILE": str(tmp_path / "install.log"),
        "INSTALLER_SUPPORT_DIR": str(tmp_path / "support"),
    }
    env.update(extra)
    return env


def run_installer(tmp_path: Path, *args: str, check: bool = True, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER), *args],
        check=check,
        capture_output=True,
        text=True,
        env=installer_env(tmp_path, **env),
    )


def write_state(tmp_path: Path, payload: dict[str, object]) -> Path:
    state = tmp_path / "foundation_state.json"
    state.write_text(json.dumps(payload), encoding="utf-8")
    return state


def test_recommended_refuses_unsafe_missing_media_without_download_consent(tmp_path):
    write_state(
        tmp_path,
        {
            "controller_vm_ready": True,
            "controller_runtime_ready": True,
            "windows_iso_ready": False,
            "virtio_iso_ready": True,
            "media_ready": False,
        },
    )

    result = run_installer(tmp_path, "--action", "recommended", "--yes", "--dry-run", check=False)

    assert result.returncode == 2
    assert "Recommended repair is not safe to run automatically" in result.stdout
    assert "--phase bootstrap" not in result.stdout
    detect = json.loads((tmp_path / "installer_detect.json").read_text(encoding="utf-8"))
    assert detect["safe_to_auto_run"] is False
    assert detect["failed_check_id"] == "bootstrap.media.windows_iso_missing"


def test_recommended_direct_url_uses_url_without_resolver_download(tmp_path):
    write_state(
        tmp_path,
        {
            "controller_vm_ready": True,
            "controller_runtime_ready": True,
            "windows_iso_ready": False,
            "virtio_iso_ready": True,
            "media_ready": False,
        },
    )

    result = run_installer(
        tmp_path,
        "--action",
        "recommended",
        "--yes",
        "--dry-run",
        "--windows-iso-url",
        "https://example.test/windows.iso",
    )

    assert "--phase bootstrap" in result.stdout
    assert "--windows-iso-url https://example.test/windows.iso" in result.stdout
    assert "--download-windows" not in result.stdout


def test_status_handles_corrupt_state_without_traceback_or_mutation(tmp_path):
    state = tmp_path / "foundation_state.json"
    state.write_text("{not-json", encoding="utf-8")

    result = run_installer(tmp_path, "--action", "status", "--dry-run")

    assert result.returncode == 0
    assert "State file is unreadable" in result.stdout
    assert "Traceback" not in result.stderr
    detect = json.loads((tmp_path / "installer_detect.json").read_text(encoding="utf-8"))
    assert detect["confidence"] == "low"
    assert detect["safe_to_auto_run"] is False


def test_failed_phase_records_failure_context_and_footer(tmp_path):
    fake_init = tmp_path / "fake-init.sh"
    fake_init.write_text(
        "#!/usr/bin/env bash\n"
        "echo fake init \"$@\"\n"
        "exit 20\n",
        encoding="utf-8",
    )
    fake_init.chmod(0o755)

    result = run_installer(
        tmp_path,
        "--action",
        "bootstrap",
        "--yes",
        check=False,
        INSTALLER_INIT_SCRIPT=str(fake_init),
    )

    assert result.returncode == 20
    assert "Step ID: bootstrap.media" in result.stdout
    assert "Check ID: bootstrap.media.windows_iso_missing" in result.stdout
    failure = json.loads((tmp_path / "install-last-failure.json").read_text(encoding="utf-8"))
    assert failure["phase"] == "bootstrap"
    assert failure["exit_code"] == 20
    assert failure["check_id"] == "bootstrap.media.windows_iso_missing"


def test_support_action_redacts_logs_and_prints_issue_draft(tmp_path):
    write_state(tmp_path, {})
    (tmp_path / "install-last-failure.json").write_text(
        '{"action":"recommended","phase":"foundation","step_id":"foundation.start","check_id":"foundation.start.no_state","exit_code":1}',
        encoding="utf-8",
    )
    (tmp_path / "install.log").write_text(
        "Authorization: Bearer abc123\nPVEAPIToken=root@pam!x=secret\n",
        encoding="utf-8",
    )

    result = run_installer(
        tmp_path,
        "--action",
        "support",
        "--support-no-bundle",
        "--support-print",
    )

    assert "Issue draft:" in result.stdout
    assert "abc123" not in result.stdout
    assert "secret" not in result.stdout
    assert "Authorization: [REDACTED]" in result.stdout
    assert list((tmp_path / "support").glob("github-issue-*.md"))


def test_reset_dev_lab_still_requires_yes_confirmation(tmp_path):
    result = run_installer(tmp_path, "--action", "reset-dev-lab", "--dry-run")

    assert "Reset cancelled." in result.stdout
    assert "--phase reset-dev-lab" not in result.stdout
