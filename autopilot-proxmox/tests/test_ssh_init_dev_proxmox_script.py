from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ssh-init-dev-proxmox.sh"


def test_ssh_init_dev_proxmox_script_contract():
    text = SCRIPT.read_text(encoding="utf-8")

    assert SCRIPT.exists()
    assert "ssh-keyscan" in text
    assert "ssh-keygen -lf" in text
    assert "StrictHostKeyChecking yes" in text
    assert "StrictHostKeyChecking=no" not in text
    assert "UserKnownHostsFile" in text
    assert "# >>> proxmoxveautopilot ${ALIAS}" in text
    assert "grep -qxF" in text
    assert "init-proxmox-ve.sh --phase foundation --resume" in text


def test_ssh_init_dev_proxmox_script_prints_absolute_copy_source():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "REPO_ROOT=" in text
    assert "rsync -a --delete --no-owner --no-group \\" in text
    assert "'${REPO_ROOT}/' ${ALIAS}:/opt/ProxmoxVEAutopilot/" in text
    assert "--exclude 'autopilot-proxmox/.env'" in text
    assert "--exclude 'autopilot-proxmox/secrets/'" in text
    assert "--exclude 'autopilot-proxmox/output/'" in text
    assert "chown -R root:root /opt/ProxmoxVEAutopilot" in text
