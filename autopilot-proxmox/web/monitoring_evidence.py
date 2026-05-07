"""Pure evidence helpers for monitor-backed UI state.

The helpers in this module interpret already-collected monitor rows.
They do not fetch live data and are intentionally separate from app.py
and templates so later UI wiring can reuse one explicit contract.
"""
from __future__ import annotations

import json
from typing import Any


def _parse_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[dict]:
    parsed = _parse_json(value, [])
    return parsed if isinstance(parsed, list) else []


def _truthy_join_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"yes", "true", "1"}


def _probe_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Accept either a latest_per_vmid entry or a flat probe row."""
    probe = row.get("probe")
    return probe if isinstance(probe, dict) else row


def _entra_trust_types(entra_matches: list[dict]) -> set[str]:
    return {
        str(match.get("trustType") or "").strip().lower()
        for match in entra_matches
        if isinstance(match, dict)
    }


def _has_intune_entra_device_id_intersection(
    entra_matches: list[dict],
    intune_matches: list[dict],
) -> bool:
    entra_ids = {
        match.get("deviceId") or match.get("device_id")
        for match in entra_matches
        if isinstance(match, dict)
    }
    intune_ids = {
        match.get("azureADDeviceId") or match.get("azure_ad_device_id")
        for match in intune_matches
        if isinstance(match, dict)
    }
    return bool({device_id for device_id in entra_ids if device_id} &
                {device_id for device_id in intune_ids if device_id})


def hostname_join_evidence(row: dict[str, Any]) -> dict[str, Any]:
    """Explain the /vms hostname bubble join evidence.

    Returns a stable dict contract for later rendering:
    ``label``, ``title``, ``source``, ``priority``, and ``is_joined``.
    Labels intentionally stay aligned with the current /vms bubble text:
    ``domain``, ``Entra ID``, or ``workgroup``.
    """
    probe = _probe_fields(row)
    ad_matches = _as_list(probe.get("ad_matches_json"))
    entra_matches = _as_list(probe.get("entra_matches_json"))
    intune_matches = _as_list(probe.get("intune_matches_json"))
    trust_types = _entra_trust_types(entra_matches)

    ad_count = int(probe.get("ad_match_count") or 0)
    ad_found = bool(int(probe.get("ad_found") or 0))
    if ad_found or ad_count > 0 or ad_matches:
        return {
            "label": "domain",
            "title": "Active Directory domain-joined",
            "source": "ad",
            "priority": 100,
            "is_joined": True,
        }

    if "serverad" in trust_types:
        return {
            "label": "domain",
            "title": "Hybrid Entra join reported by Entra trustType=ServerAd",
            "source": "entra_trust_serverad",
            "priority": 90,
            "is_joined": True,
        }

    if _has_intune_entra_device_id_intersection(entra_matches, intune_matches):
        return {
            "label": "Entra ID",
            "title": "Entra ID joined via Intune azureADDeviceId -> Entra deviceId",
            "source": "intune_entra_device_id",
            "priority": 85,
            "is_joined": True,
        }

    if "azuread" in trust_types:
        return {
            "label": "Entra ID",
            "title": "Entra ID joined",
            "source": "entra_trust_azuread",
            "priority": 80,
            "is_joined": True,
        }

    dsreg = _parse_json(probe.get("dsreg_status"), {})
    if isinstance(dsreg, dict):
        if _truthy_join_value(
            dsreg.get("AzureAdJoined")
            or dsreg.get("azureAdJoined")
            or dsreg.get("aad_joined")
        ):
            return {
                "label": "Entra ID",
                "title": "Entra ID joined from dsreg AzureAdJoined",
                "source": "dsreg_azure_ad_joined",
                "priority": 70,
                "is_joined": True,
            }

    return {
        "label": "workgroup",
        "title": "Not joined to an AD domain",
        "source": "none",
        "priority": 0,
        "is_joined": False,
    }
