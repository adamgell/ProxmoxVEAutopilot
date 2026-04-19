"""install_intune_portal / install_edge: Microsoft apt repos + packages."""
from __future__ import annotations

from ..registry import register
from ..types import StepOutput

_CURTIN = "curtin in-target --target=/target --"


def _ms_key_setup() -> list[str]:
    """Shared Microsoft GPG-key install commands. Safe to emit multiple times
    if multiple MS-repo steps run — apt dedupes the keyring file."""
    return [
        f"{_CURTIN} mkdir -p /tmp/microsoft",
        f"{_CURTIN} sh -c 'cd /tmp/microsoft && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > microsoft.gpg'",
        f"{_CURTIN} install -o root -g root -m 644 /tmp/microsoft/microsoft.gpg /usr/share/keyrings/microsoft.gpg",
    ]


@register("install_intune_portal")
def compile_install_intune_portal(params, credentials) -> StepOutput:
    release = params.get("ubuntu_release", "noble")
    release_version = params.get("ubuntu_release_version", "24.04")
    cmds = _ms_key_setup()
    cmds += [
        f"{_CURTIN} sh -c 'echo \"deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/ubuntu/{release_version}/prod {release} main\" > /etc/apt/sources.list.d/microsoft-ubuntu-{release}-prod.list'",
        f"{_CURTIN} apt-get update",
        f"{_CURTIN} apt-get install -y intune-portal",
    ]
    return StepOutput(late_commands=cmds)


@register("install_edge")
def compile_install_edge(params, credentials) -> StepOutput:
    cmds = _ms_key_setup()
    cmds += [
        f"{_CURTIN} sh -c 'echo \"deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/edge stable main\" > /etc/apt/sources.list.d/microsoft-edge.list'",
        f"{_CURTIN} apt-get update",
        f"{_CURTIN} apt-get install -y microsoft-edge-stable",
    ]
    return StepOutput(late_commands=cmds)
