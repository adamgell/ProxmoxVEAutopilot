from __future__ import annotations


def test_remote_manifest_parser_accepts_windows_paths():
    from scripts import cloudosd_remote_build as remote_build

    lines = [
        "noise",
        r"F:\BuildRoot\outputs\cloudosd-autopilot-amd64-1234abcd.json",
        r"F:\BuildRoot\outputs\cloudosd-autopilot-amd64-1234abcd.wim",
        r"F:\BuildRoot\outputs\cloudosd-autopilot-amd64-1234abcd.iso",
    ]

    assert remote_build.parse_manifest_path(lines) == (
        r"F:\BuildRoot\outputs\cloudosd-autopilot-amd64-1234abcd.json"
    )


def test_windows_path_to_scp_path_normalizes_drive_paths():
    from scripts import cloudosd_remote_build as remote_build

    assert remote_build.windows_path_to_scp_path(
        r"F:\BuildRoot\outputs\cloudosd.iso",
    ) == "F:/BuildRoot/outputs/cloudosd.iso"


def test_remote_basename_handles_windows_drive_paths():
    from scripts import cloudosd_remote_build as remote_build

    assert (
        remote_build.remote_basename(
            r"F:\BuildRoot\outputs\cloudosd-autopilot-amd64-test.iso",
        )
        == "cloudosd-autopilot-amd64-test.iso"
    )


def test_remote_command_uses_adk_host_paths_and_pinned_osdcloud():
    from scripts import cloudosd_remote_build as remote_build

    script = remote_build.build_remote_script(
        job_id="job-1",
        remote_root=r"F:\BuildRoot",
        archive_name="cloudosd-src-job-1.tar.gz",
        arch="amd64",
        osdcloud_version="26.4.17.1",
    )
    command = remote_build.build_remote_command(script)

    assert r"F:\BuildRoot\work\cloudosd-src-job-1" in script
    assert r"tools\cloudosd-build\build-cloudosd.ps1" in script
    assert "-OSDCloudVersion '26.4.17.1'" in script
    assert "-Arch amd64" in script
    assert "-EncodedCommand" in command


def test_ssh_options_are_non_interactive_and_use_persistent_known_hosts(tmp_path):
    from scripts import cloudosd_remote_build as remote_build

    options = remote_build.build_ssh_options(output_dir=tmp_path)

    assert "BatchMode=yes" in options
    assert "StrictHostKeyChecking=accept-new" in options
    assert any(str(tmp_path / "cloudosd_known_hosts") in item for item in options)


def test_app_cloudosd_tool_copy_stays_in_sync():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    repo_root = app_root.parent
    filenames = [
        "Invoke-CloudOSDBridge.ps1",
        "PVEAutopilot-FirstBoot.ps1",
        "build-cloudosd.ps1",
        "config.json",
        "startnet.cmd",
    ]

    for filename in filenames:
        assert (
            app_root / "tools" / "cloudosd-build" / filename
        ).read_bytes() == (
            repo_root / "tools" / "cloudosd-build" / filename
        ).read_bytes()
