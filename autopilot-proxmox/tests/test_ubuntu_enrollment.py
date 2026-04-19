"""ubuntu_enrollment: parse guest-exec output for intune-portal + mdatp."""
from __future__ import annotations

from web.ubuntu_enrollment import parse_enrollment_output, tags_for


def test_parses_healthy_intune_and_mdatp() -> None:
    out = parse_enrollment_output(
        intune_stdout="intune-portal v1.2.3\n", intune_rc=0,
        mdatp_stdout="healthy: true\n", mdatp_rc=0,
    )
    assert out["intune"] == "healthy"
    assert out["mde"] == "healthy"


def test_intune_missing_when_rc_nonzero() -> None:
    out = parse_enrollment_output(
        intune_stdout="", intune_rc=127,
        mdatp_stdout="", mdatp_rc=127,
    )
    assert out["intune"] == "missing"
    assert out["mde"] == "missing"


def test_mdatp_not_configured_when_installed_but_unhealthy() -> None:
    out = parse_enrollment_output(
        intune_stdout="v1.2.3", intune_rc=0,
        mdatp_stdout="healthy: false\nissues: not onboarded\n", mdatp_rc=0,
    )
    assert out["mde"] == "not-configured"


def test_tags_for_produces_proxmox_tag_strings() -> None:
    tags = tags_for({"intune": "healthy", "mde": "missing"})
    assert "enroll-intune-healthy" in tags
    assert "enroll-mde-missing" in tags
