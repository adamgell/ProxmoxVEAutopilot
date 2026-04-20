"""install_ubuntu_core: locale, timezone, baseline packages (cloud-init flavor).

Ubuntu cloud images already handle keyboard layout, disk layout, updates policy,
SSH server, and shutdown behaviour via their default configuration. This step
only needs to set locale + timezone and ensure qemu-guest-agent is present.
"""
from __future__ import annotations

from ..registry import register
from ..types import StepOutput


@register("install_ubuntu_core")
def compile_install_ubuntu_core(params, credentials) -> StepOutput:
    locale = params.get("locale", "en_US.UTF-8")
    timezone = params.get("timezone", "UTC")
    # keyboard_layout / storage_layout are accepted for backwards-compat with
    # existing seeded sequences but ignored — the cloud image handles these.
    _ = params.get("keyboard_layout")
    _ = params.get("storage_layout")
    # Optional apt proxy: when set, cloud-init routes all apt traffic through
    # this URL. Useful with a LAN apt-cacher-ng / Squid proxy — dramatically
    # speeds up template builds when rebuilding after cache warming.
    apt_proxy = params.get("apt_proxy")  # e.g. "http://apt-cache.lan:3142"

    cloud_config: dict = {
        "timezone": timezone,
        "locale": locale,
        # Refresh apt index before any `packages:` install runs. Cloud
        # images ship with a stale apt cache.
        "package_update": True,
        "package_upgrade": False,
        # qemu-guest-agent is required for Proxmox's agent/exec API.
        # Ubuntu cloud images generally include it, but we list it here
        # to be explicit and to cover minimal images that don't.
        "packages": ["qemu-guest-agent"],
    }
    if apt_proxy:
        # cloud-init's apt module accepts `http_proxy` / `https_proxy` keys.
        # They're written to /etc/apt/apt.conf.d/90cloud-init-aptproxy and
        # all subsequent apt-get runs route through the proxy.
        cloud_config["apt"] = {
            "http_proxy": apt_proxy,
            "https_proxy": apt_proxy,
        }

    return StepOutput(
        cloud_config=cloud_config,
        # Belt-and-suspenders: ensure the service is enabled even if the
        # package was already installed (no apt install needed). `|| true`
        # so a missing unit doesn't fail cloud-init.
        runcmd=[
            "systemctl enable --now qemu-guest-agent || true",
        ],
    )
