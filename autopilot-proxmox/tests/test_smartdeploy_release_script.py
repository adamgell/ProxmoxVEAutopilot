from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "New-SmartDeployWindowsRelease.ps1"


def script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_smartdeploy_release_script_exists_and_parses():
    assert SCRIPT.exists()

    command = (
        "$tokens=$null; $errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count) { $errors | ForEach-Object { $_.ToString() }; exit 1 }"
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_smartdeploy_release_script_exposes_repeatable_lab_parameters():
    text = script_text()

    for parameter in (
        "$IsoPath",
        "$EditionName",
        "$ReleaseName",
        "$SmartDeployRoot",
        "$AnswerFilePath",
        "$Architecture",
        "$SmartDeployCapturedImagePath",
        "$OrganizationalUnit",
        "$PlanOnly",
        "$UpdateAnswerFileOnly",
    ):
        assert parameter in text

    assert "ValidateSet('x64', 'arm64')" in text
    assert "$SmartDeployRoot = 'E:\\SmartDeploy'" in text


def test_smartdeploy_release_script_mounts_iso_and_cleans_up_in_finally():
    text = script_text()

    assert "Mount-DiskImage -ImagePath $IsoPath -PassThru" in text
    assert "finally" in text
    assert "Dismount-DiskImage -ImagePath $IsoPath | Out-Null" in text
    assert "sources\\install.wim" in text


def test_smartdeploy_release_script_copies_full_install_wim_with_hash_metadata():
    text = script_text()

    assert "Copy-Item -LiteralPath $sourceWim -Destination $targetWim" in text
    assert "Get-FileHash -Algorithm SHA256" in text
    assert "target_sha256" in text
    assert "hashes_match" in text
    assert ".metadata.json" in text
    assert "Get-WindowsImage -ImagePath $sourceWim" in text
    assert "raw_iso_install_wim" in text


def test_smartdeploy_release_script_marks_raw_iso_wim_as_not_answer_file_ready():
    text = script_text()

    assert "raw_iso_wim_answer_file_compatible = $false" in text
    assert "requires_smartdeploy_capture_wizard_image = $true" in text
    assert "smartdeploy_wim_customdata_required = $true" in text
    assert "smartdeploy_captured_image_path" in text


def test_smartdeploy_release_script_updates_answer_xml_structurally():
    text = script_text()

    assert "[xml]$answerXml = Get-Content" in text
    assert "SelectNodes('//*')" in text
    assert "-replace" not in text
    assert "image_file" in text
    assert "organizational_unit" in text


def test_smartdeploy_release_script_keeps_boot_media_out_of_scope():
    text = script_text()

    assert "boot_media_creation" in text
    assert "manual_smartdeploy_media_wizard" in text
    assert "MediaWizard.exe" not in text


def test_smartdeploy_release_script_uses_release_name_for_expected_boot_wim():
    text = script_text()

    assert '"$ReleaseName-boot.wim"' in text
    assert '"$ReleaseName-$Architecture-boot.wim"' not in text
