"""install_mde_linux: install mdatp apt package and run onboarding script."""
from __future__ import annotations

from ..registry import register
from ..types import StepOutput, UbuntuCompileError

_CURTIN = "curtin in-target --target=/target --"


@register("install_mde_linux")
def compile_install_mde_linux(params, credentials) -> StepOutput:
    cred_id = params.get("mde_onboarding_credential_id")
    if cred_id is None:
        raise UbuntuCompileError(
            "install_mde_linux: params.mde_onboarding_credential_id is required"
        )
    cred = credentials.get(cred_id)
    if cred is None:
        raise UbuntuCompileError(
            f"install_mde_linux: mde_onboarding credential {cred_id} not provided"
        )
    script_b64 = cred.get("script_b64")
    if not script_b64:
        raise UbuntuCompileError(
            "install_mde_linux: credential missing script_b64"
        )

    cmds = [
        # mdatp comes from the Microsoft production apt repo the intune step set up.
        # If install_mde_linux is used without install_intune_portal, the user must
        # add a run_late_command step that sets up the MS repo first.
        f"{_CURTIN} apt-get install -y mdatp",
        f"{_CURTIN} mkdir -p /tmp/mde",
        # Embed the onboarding script as base64 to preserve exact bytes.
        f"{_CURTIN} bash -c 'echo \"{script_b64}\" | base64 -d > /tmp/mde/onboard.py'",
        f"{_CURTIN} chmod +x /tmp/mde/onboard.py",
        f"{_CURTIN} python3 /tmp/mde/onboard.py",
        f"{_CURTIN} rm -rf /tmp/mde",
    ]
    return StepOutput(late_commands=cmds)
