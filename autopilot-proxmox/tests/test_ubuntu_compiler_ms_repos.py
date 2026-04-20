"""install_intune_portal / install_edge."""
from __future__ import annotations

from web.ubuntu_compiler import compile_step


def test_intune_portal_emits_repo_setup_and_apt_install() -> None:
    out = compile_step("install_intune_portal", params={}, credentials={})
    joined = "\n".join(out.runcmd)
    assert "microsoft.asc" in joined
    assert "microsoft.gpg" in joined
    assert "packages.microsoft.com/ubuntu/24.04/prod noble main" in joined
    assert "apt-get install -y intune-portal" in joined
    # No curtin wrapping — cloud-init runs commands directly.
    assert all("curtin" not in line for line in out.runcmd)


def test_intune_portal_release_override() -> None:
    out = compile_step(
        "install_intune_portal",
        params={"ubuntu_release": "jammy", "ubuntu_release_version": "22.04"},
        credentials={},
    )
    joined = "\n".join(out.runcmd)
    assert "packages.microsoft.com/ubuntu/22.04/prod jammy main" in joined


def test_edge_emits_repo_setup_and_apt_install() -> None:
    out = compile_step("install_edge", params={}, credentials={})
    joined = "\n".join(out.runcmd)
    assert "packages.microsoft.com/repos/edge stable main" in joined
    assert "apt-get install -y microsoft-edge-stable" in joined
