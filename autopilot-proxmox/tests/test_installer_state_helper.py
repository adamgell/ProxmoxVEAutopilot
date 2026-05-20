import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts" / "installer_state.py"

spec = importlib.util.spec_from_file_location("installer_state", HELPER_PATH)
installer_state = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = installer_state
spec.loader.exec_module(installer_state)


def test_clean_state_recommends_foundation():
    detection = installer_state.classify_state(
        {}, {}, allow_windows_download=False, allow_virtio_download=False
    )

    assert detection.classification == "clean"
    assert detection.confidence == "high"
    assert detection.recommended_action == "foundation"
    assert detection.recommended_phases == ["foundation"]
    assert detection.safe_to_auto_run is True
    assert detection.current_step_id == "foundation.start"


def test_foundation_complete_state_recommends_bootstrap_without_silent_windows_download():
    detection = installer_state.classify_state(
        {
            "controller_vm_ready": True,
            "controller_runtime_ready": True,
            "windows_iso_ready": False,
            "virtio_iso_ready": True,
            "media_ready": False,
        },
        {},
        allow_windows_download=False,
        allow_virtio_download=False,
    )

    assert detection.classification == "partial_install"
    assert detection.recommended_action == "bootstrap"
    assert detection.recommended_phases == ["bootstrap"]
    assert detection.safe_to_auto_run is False
    assert detection.failed_check_id == "bootstrap.media.windows_iso_missing"
    assert "--download-windows" not in " ".join(detection.planned_commands)


def test_foundation_complete_state_with_download_flag_can_auto_run_bootstrap():
    detection = installer_state.classify_state(
        {
            "controller_vm_ready": True,
            "controller_runtime_ready": True,
            "windows_iso_ready": False,
            "virtio_iso_ready": True,
            "media_ready": False,
        },
        {},
        allow_windows_download=True,
        allow_virtio_download=True,
    )

    assert detection.safe_to_auto_run is True
    assert "--download-windows" in " ".join(detection.planned_commands)


def test_foundation_complete_state_with_direct_url_uses_url_not_resolver():
    detection = installer_state.classify_state(
        {
            "controller_vm_ready": True,
            "controller_runtime_ready": True,
            "windows_iso_ready": False,
            "virtio_iso_ready": True,
            "media_ready": False,
        },
        {},
        allow_windows_download=True,
        allow_virtio_download=False,
        windows_iso_url="https://example.test/windows.iso",
    )

    planned = " ".join(detection.planned_commands)
    assert detection.safe_to_auto_run is True
    assert "--windows-iso-url https://example.test/windows.iso" in planned
    assert "--download-windows" not in planned


def test_bootstrap_complete_state_recommends_operational():
    detection = installer_state.classify_state(
        {
            "controller_runtime_ready": True,
            "windows_iso_ready": True,
            "virtio_iso_ready": True,
            "media_ready": True,
            "promoted_artifacts_ready": False,
        },
        {},
        allow_windows_download=False,
        allow_virtio_download=False,
    )

    assert detection.recommended_action == "operational"
    assert detection.recommended_phases == ["operational"]
    assert detection.failed_check_id == "operational.artifacts.not_promoted"


def test_ready_but_runtime_config_stale_recommends_runtime_config_repair():
    detection = installer_state.classify_state(
        {
            "operational_ready": True,
            "controller_runtime_config_synced": False,
        },
        {},
        allow_windows_download=False,
        allow_virtio_download=False,
    )

    assert detection.classification == "drifted"
    assert detection.recommended_action == "runtime-config"
    assert detection.safe_to_auto_run is True


def test_controller_identity_conflict_blocks_auto_run():
    detection = installer_state.classify_state(
        {"controller_vmid": "181", "controller_vm_name": "autopilot-controller-01"},
        {"controller_vmid": "182", "controller_vm_name": "autopilot-controller-01"},
        allow_windows_download=False,
        allow_virtio_download=False,
    )

    assert detection.classification == "conflicted"
    assert detection.confidence == "low"
    assert detection.safe_to_auto_run is False
    assert detection.conflicts
    assert detection.failed_check_id == "foundation.controller_vm.identity_conflict"


def test_name_only_discovery_is_not_high_confidence():
    detection = installer_state.classify_state(
        {},
        {"controller_vm_name": "autopilot-controller-01"},
        allow_windows_download=False,
        allow_virtio_download=False,
    )

    assert detection.confidence in {"medium", "low"}
    assert detection.safe_to_auto_run is False


def test_missing_virtio_media_without_download_flag_blocks_recommended_auto_run():
    detection = installer_state.classify_state(
        {
            "controller_runtime_ready": True,
            "windows_iso_ready": True,
            "virtio_iso_ready": False,
            "media_ready": False,
        },
        {},
        allow_windows_download=False,
        allow_virtio_download=False,
    )

    assert detection.recommended_action == "bootstrap"
    assert detection.safe_to_auto_run is False
    assert "--download-virtio" not in " ".join(detection.planned_commands)


def test_redaction_removes_known_secret_patterns():
    text = """
Authorization: Bearer abc123
Cookie: session=abc
PVEAPIToken=root@pam!autopilot=supersecret
AUTOPILOT_POSTGRES_PASSWORD=secret
{"api_token": "json-secret"}
-----BEGIN OPENSSH PRIVATE KEY-----
abc
-----END OPENSSH PRIVATE KEY-----
"""
    redacted, matches = installer_state.redact_text(text)

    assert "abc123" not in redacted
    assert "supersecret" not in redacted
    assert "json-secret" not in redacted
    assert "AUTOPILOT_POSTGRES_PASSWORD=[REDACTED]" in redacted
    assert "OPENSSH PRIVATE KEY" not in redacted
    assert matches
    assert installer_state.has_residual_secret(redacted) is False


def test_support_bundle_fails_closed_when_residual_secret_remains(tmp_path):
    suspicious = "token = still-present-secret-value"
    assert installer_state.has_residual_secret(suspicious) is True

    detection_file = tmp_path / "detect.json"
    failure_file = tmp_path / "failure.json"
    log_file = tmp_path / "install.log"
    output_dir = tmp_path / "support"
    detection_file.write_text(
        '{"schema":1,"classification":"partial_install","confidence":"high","recommended_action":"bootstrap"}',
        encoding="utf-8",
    )
    failure_file.write_text("{}", encoding="utf-8")
    log_file.write_text(suspicious, encoding="utf-8")

    rc = installer_state.write_support_outputs(
        detection_file=detection_file,
        failure_file=failure_file,
        log_file=log_file,
        output_dir=output_dir,
        no_bundle=False,
        print_draft=False,
        include_environment=False,
    )

    assert rc == 0
    assert not list(output_dir.glob("support-bundle-*.tar.gz"))
    assert list(output_dir.glob("github-issue-*.md"))


def test_issue_draft_includes_step_and_check_ids():
    detection = installer_state.clean_detection()
    draft = installer_state.build_issue_draft(
        detection,
        {
            "action": "recommended",
            "phase": "foundation",
            "step_id": "foundation.start",
            "check_id": "foundation.start.no_state",
            "exit_code": 1,
        },
        "recent log",
        include_environment=False,
        support_bundle_path=None,
    )

    assert "Step ID: foundation.start" in draft
    assert "Check ID: foundation.start.no_state" in draft
