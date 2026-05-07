from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FIX_LXC_DOCKER = REPO_ROOT / "scripts" / "fix_lxc_docker.sh"


def test_lxc_production_build_keeps_default_bridge_network_working():
    script = FIX_LXC_DOCKER.read_text()

    assert "check_active_autopilot_work" in script
    assert "status IN ('pending', 'running')" in script
    assert "state NOT IN ('done', 'failed')" in script
    assert "FORCE_DOCKER_RESTART=1" in script
    assert "Refusing to restart Docker while Autopilot work is active" in script
    assert '"iptables": true' in script
    assert '"ip-masq": true' in script
    assert "--network host" not in script
    assert "ghcr.io/adamgell/proxmox-autopilot:latest" in script
    assert "--build-arg \"GIT_SHA=${GIT_SHA}\"" in script
    assert "--build-arg \"BUILD_TIME=${BUILD_TIME}\"" in script
    assert "default BuildKit network unable to reach Debian apt repositories" in script
    assert "deb.debian.org" in script
