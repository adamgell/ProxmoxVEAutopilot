"""CloudOSD compatibility extraction for legacy task sequences.

The legacy sequence editor is still the operator-facing source of intent for
AD join credentials. CloudOSD consumes only the subset that can be safely
staged through OSDCloud/Windows Setup today.
"""
from __future__ import annotations

from typing import Callable, Optional

from web.sequence_compiler import _split_domain_user


class CloudOSDSequenceError(ValueError):
    """Raised when a selected sequence cannot be used by CloudOSD."""


_SUPPORTED_ENABLED_STEPS = {"join_ad_domain"}


def compile_cloudosd_sequence_intent(
    sequence: Optional[dict],
    *,
    resolve_credential: Callable[[int], Optional[dict]],
) -> dict:
    """Return CloudOSD-compatible intent from a legacy sequence.

    The returned dict is safe to persist. It intentionally excludes join
    password and account username; those are resolved just-in-time for the PE
    package.
    """
    if not sequence:
        return {"domain_join": {"enabled": False}}

    unsupported = sorted({
        step.get("step_type", "")
        for step in sequence.get("steps", []) or []
        if step.get("enabled", True)
        and step.get("step_type") not in _SUPPORTED_ENABLED_STEPS
    })
    if unsupported:
        raise CloudOSDSequenceError(
            "CloudOSD selected sequence is not CloudOSD-compatible yet; "
            f"unsupported enabled step(s): {', '.join(unsupported)}",
        )

    joins = [
        step
        for step in sequence.get("steps", []) or []
        if step.get("enabled", True) and step.get("step_type") == "join_ad_domain"
    ]
    if not joins:
        return {"domain_join": {"enabled": False}}
    if len(joins) > 1:
        raise CloudOSDSequenceError(
            "CloudOSD selected sequence has multiple enabled join_ad_domain steps; "
            "use exactly one.",
        )

    step = joins[0]
    params = step.get("params") or {}
    credential_id = params.get("credential_id")
    if not credential_id:
        raise CloudOSDSequenceError(
            "CloudOSD join_ad_domain step has no credential_id set.",
        )
    credential = resolve_credential(int(credential_id))
    if not credential:
        raise CloudOSDSequenceError(
            f"CloudOSD join_ad_domain credential id={credential_id} was not found.",
        )
    payload = credential.get("payload") or credential
    domain_fqdn = (payload.get("domain_fqdn") or "").strip()
    raw_username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not (domain_fqdn and raw_username and password):
        raise CloudOSDSequenceError(
            "CloudOSD join_ad_domain credential is missing domain_fqdn, "
            "username, or password.",
        )

    user_domain, _user_bare = _split_domain_user(raw_username)
    credential_domain = user_domain or domain_fqdn
    ou_path = (params.get("ou_path") or payload.get("ou_hint") or "").strip()
    acceptable = _unique_nonempty([domain_fqdn, credential_domain])
    return {
        "domain_join": {
            "enabled": True,
            "source_sequence_id": int(sequence["id"]),
            "credential_id": int(credential_id),
            "domain_fqdn": domain_fqdn,
            "credential_domain": credential_domain,
            "ou_path": ou_path,
            "acceptable_domain_names": acceptable,
        },
    }


def _unique_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = str(value or "").strip()
        key = item.casefold()
        if not item or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
