"""Parse Ubuntu enrollment check results and render them as Proxmox tags."""
from __future__ import annotations


def parse_enrollment_output(
    *,
    intune_stdout: str,
    intune_rc: int,
    mdatp_stdout: str,
    mdatp_rc: int,
) -> dict[str, str]:
    """Return {'intune': state, 'mde': state} where state is one of
    'healthy', 'missing', or (mde only) 'not-configured'."""
    if intune_rc != 0:
        intune = "missing"
    elif intune_stdout.strip():
        intune = "healthy"
    else:
        intune = "missing"

    if mdatp_rc != 0:
        mde = "missing"
    elif "healthy: true" in mdatp_stdout.lower():
        mde = "healthy"
    elif "healthy: false" in mdatp_stdout.lower():
        mde = "not-configured"
    else:
        mde = "missing"

    return {"intune": intune, "mde": mde}


def tags_for(status: dict[str, str]) -> list[str]:
    """Produce Proxmox tag strings for the given status. Intended to replace
    any existing enroll-* tags on the VM."""
    return [f"enroll-intune-{status['intune']}", f"enroll-mde-{status['mde']}"]


def merge_tags(existing: list[str], new_enroll_tags: list[str]) -> list[str]:
    """Replace all enroll-* tags in `existing` with `new_enroll_tags`,
    preserving order of non-enroll tags."""
    kept = [t for t in existing if not t.startswith("enroll-")]
    return kept + new_enroll_tags
