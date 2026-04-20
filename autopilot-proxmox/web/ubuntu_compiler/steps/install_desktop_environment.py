"""install_desktop_environment: apt-install a GNOME/XFCE/KDE desktop on top
of the Ubuntu server cloud image.

Canonical's cloud images are all server-flavored (headless). To reproduce
LinuxESP's workstation experience we install the desktop metapackage at
template-build time; clones inherit the desktop, so per-clone boot is still
fast.
"""
from __future__ import annotations

from ..registry import register
from ..types import StepOutput, UbuntuCompileError


# Flavors we accept. The value is the apt metapackage name.
_ALLOWED = {
    "ubuntu-desktop",          # full default Ubuntu Desktop (GNOME)
    "ubuntu-desktop-minimal",  # GNOME core, no office/email/games
    "xubuntu-desktop",         # XFCE
    "kubuntu-desktop",         # KDE
    "lubuntu-desktop",         # LXQt
    "ubuntu-mate-desktop",     # MATE
}


@register("install_desktop_environment")
def compile_install_desktop_environment(params, credentials) -> StepOutput:
    flavor = params.get("flavor", "ubuntu-desktop")
    if flavor not in _ALLOWED:
        raise UbuntuCompileError(
            f"install_desktop_environment: unknown flavor {flavor!r}. "
            f"Pick one of: {sorted(_ALLOWED)}"
        )

    # Install the metapackage via cloud-init's `packages:` and force the
    # graphical systemd target so the VM boots to the GUI login on first
    # boot instead of the server's multi-user.target.
    return StepOutput(
        cloud_config={"packages": [flavor]},
        runcmd=["systemctl set-default graphical.target"],
    )
