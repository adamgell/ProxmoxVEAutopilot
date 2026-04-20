"""install_mde_linux: install mdatp apt package and run onboarding script.

Runs on the booted cloud image via cloud-init runcmd. Expects the Microsoft
prod apt repo to have been set up earlier in the sequence (either by
install_intune_portal or a run_late_command step that configures it).
"""
from __future__ import annotations

from ..registry import register
from ..types import StepOutput, UbuntuCompileError


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
        # mdatp comes from the Microsoft production apt repo set up by the
        # intune step. If install_mde_linux is used standalone, the user must
        # add a run_late_command step that configures the MS repo first.
        "apt-get install -y mdatp",
        "mkdir -p /tmp/mde",
        # Embed the onboarding script as base64 to preserve exact bytes.
        f"bash -c 'echo \"{script_b64}\" | base64 -d > /tmp/mde/onboard.py'",
        "chmod +x /tmp/mde/onboard.py",
        "python3 /tmp/mde/onboard.py",
        "rm -rf /tmp/mde",
    ]
    return StepOutput(runcmd=cmds)
