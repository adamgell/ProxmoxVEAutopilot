from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "osdeploy-build-host-key.sh"


def test_osdeploy_build_host_key_script_contract():
    text = SCRIPT.read_text(encoding="utf-8")

    assert SCRIPT.exists()
    assert "osdeploy_devmachine_ed25519" in text
    assert "ssh-keygen -q -t ed25519 -a 64" in text
    assert "proxmoxveautopilot-osdeploy-build" in text
    assert "chmod 600" in text
    assert "chmod 644" in text
    assert "/app/secrets/osdeploy_devmachine_ed25519" in text
    assert "osdeploy_build_ssh_key_path" in text
    assert "authorized_keys" in text


def test_osdeploy_build_host_key_script_has_noninteractive_print_mode():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "--print-public-key-only" in text
    assert "PRINT_PUBLIC_KEY_ONLY=1" in text
    assert "cat \"${KEY_PATH}.pub\"" in text
