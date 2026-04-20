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

    # Install the desktop metapackage via `runcmd`, NOT cloud-init's
    # `packages:` list. Installing ubuntu-desktop (~1.5GB) through the
    # packages: module is unreliable — we've observed cloud-init marking
    # itself "done" even when apt/dpkg has only downloaded the debs but
    # failed to unpack/install them, leaving the template without a GUI.
    # runcmd runs sequentially and blocks on completion, which is what we
    # want for a big metapackage install.
    #
    # Set `graphical.target` so the installed system boots to GUI.
    # NOTE: each runcmd entry is a separate shell invocation, so `export
    # DEBIAN_FRONTEND=noninteractive` on one line does not affect the next.
    # We inline the env var on the apt-get call to ensure the install is
    # truly non-interactive; without this, ubuntu-desktop's dep chain can
    # block on a debconf prompt and get killed mid-unpack.
    return StepOutput(
        runcmd=[
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y {flavor}",
            "systemctl set-default graphical.target",
        ],
    )
