"""install_mde_linux step compiler."""
from __future__ import annotations

import base64

import pytest

from web.ubuntu_compiler import compile_step, UbuntuCompileError


def test_missing_credential_id_raises() -> None:
    with pytest.raises(UbuntuCompileError):
        compile_step("install_mde_linux", params={}, credentials={})


def test_credential_not_provided_raises() -> None:
    with pytest.raises(UbuntuCompileError):
        compile_step(
            "install_mde_linux",
            params={"mde_onboarding_credential_id": 99},
            credentials={},
        )


def test_emits_mdatp_install_plus_onboarding() -> None:
    script = b"#!/usr/bin/env python3\nprint('onboard')\n"
    creds = {
        7: {
            "filename": "MicrosoftDefenderATPOnboardingLinuxServer.py",
            "script_b64": base64.b64encode(script).decode("ascii"),
            "uploaded_at": "2026-04-19T00:00:00Z",
        }
    }
    out = compile_step(
        "install_mde_linux",
        params={"mde_onboarding_credential_id": 7},
        credentials=creds,
    )
    joined = "\n".join(out.runcmd)
    assert "apt-get install -y mdatp" in joined
    assert "/tmp/mde/onboard.py" in joined
    # Onboarding payload is embedded as base64
    assert creds[7]["script_b64"] in joined
    # Cleanup line deletes /tmp/mde
    assert any("rm -rf /tmp/mde" in line for line in out.runcmd)
    # No curtin wrapping — these commands run on the booted cloud image.
    assert all("curtin" not in line for line in out.runcmd)
