"""OSDeploy server role catalog, validation, and step metadata."""
from __future__ import annotations

from typing import Any, Callable


ROLE_STEP_KINDS = {
    "file_server": ["configure_file_server_role"],
    "isolated_domain_controller": [
        "configure_isolated_domain_controller_role",
        "verify_isolated_domain_controller_role",
    ],
    "mecm_prereq": ["configure_mecm_prereq_role"],
}

DOMAIN_JOIN_STEP_KINDS = ["join_domain_role", "verify_ad_domain_join"]

FULL_OS_ROLE_ACTION_KINDS = sorted({
    *DOMAIN_JOIN_STEP_KINDS,
    *[kind for kinds in ROLE_STEP_KINDS.values() for kind in kinds],
})


ROLE_CATALOG = {
    "base": {
        "id": "base",
        "name": "Windows Server Base",
        "launchable": True,
        "required_fields": [],
        "credential_fields": [],
        "step_kinds": [],
        "readiness_status": "base_ready",
    },
    "file_server": {
        "id": "file_server",
        "name": "File Server",
        "launchable": True,
        "required_fields": [
            "share_name",
            "share_path",
            "full_access_principals",
            "change_access_principals",
            "read_access_principals",
        ],
        "credential_fields": [],
        "step_kinds": ROLE_STEP_KINDS["file_server"],
        "readiness_status": "file_server_ready",
    },
    "isolated_domain_controller": {
        "id": "isolated_domain_controller",
        "name": "Isolated Domain Controller",
        "launchable": True,
        "required_fields": [
            "forest_fqdn",
            "netbios_name",
            "forest_admin_credential_id",
            "dsrm_credential_id",
        ],
        "credential_fields": [
            "forest_admin_credential_id",
            "dsrm_credential_id",
        ],
        "step_kinds": ROLE_STEP_KINDS["isolated_domain_controller"],
        "readiness_status": "isolated_domain_controller_ready",
    },
    "mecm_prereq": {
        "id": "mecm_prereq",
        "name": "MECM Prereq Baseline",
        "launchable": True,
        "required_fields": ["prereq_profile", "content_root"],
        "credential_fields": [],
        "step_kinds": ROLE_STEP_KINDS["mecm_prereq"],
        "readiness_status": "mecm_prereq_ready",
    },
    "lab_in_a_box": {
        "id": "lab_in_a_box",
        "name": "Lab in a Box",
        "launchable": True,
        "required_fields": ["bundle_name", "domain_join_credential_id", "children"],
        "credential_fields": ["domain_join_credential_id"],
        "step_kinds": [
            *ROLE_STEP_KINDS["isolated_domain_controller"],
            *DOMAIN_JOIN_STEP_KINDS,
            *ROLE_STEP_KINDS["file_server"],
            *DOMAIN_JOIN_STEP_KINDS,
            *ROLE_STEP_KINDS["mecm_prereq"],
        ],
        "readiness_status": "bundle_ready",
        "bundle": True,
    },
}


def catalog_payload() -> dict[str, dict]:
    payload: dict[str, dict] = {}
    for key, value in ROLE_CATALOG.items():
        row = dict(value)
        row["readiness_status_name"] = row.get("readiness_status")
        payload[key] = row
    return payload


def role_step_kinds(server_role: str) -> list[str]:
    return list(ROLE_STEP_KINDS.get(server_role, []))


def generated_step_kinds(server_role: str, role_options: dict | None = None) -> list[str]:
    kinds: list[str] = []
    if _domain_join_enabled((role_options or {}).get("domain_join")):
        kinds.extend(DOMAIN_JOIN_STEP_KINDS)
    kinds.extend(role_step_kinds(server_role))
    return kinds


def final_role_step_kind(server_role: str) -> str | None:
    kinds = role_step_kinds(server_role)
    return kinds[-1] if kinds else None


def role_readiness_status(server_role: str) -> str:
    return str(ROLE_CATALOG.get(server_role, {}).get("readiness_status") or f"{server_role}_ready")


def _blocking(check_id: str, message: str) -> dict:
    return {"id": check_id, "message": message, "severity": "block"}


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return False
    return False


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    return [str(item).strip() for item in values if str(item).strip()]


def _sanitize_domain_join(value: Any) -> dict:
    raw = dict(value or {}) if isinstance(value, dict) else {}
    credential_id = _positive_int(raw.get("credential_id"))
    domain_fqdn = str(raw.get("domain_fqdn") or "").strip().lower()
    credential_domain = str(raw.get("credential_domain") or "").strip()
    domain_controller_ipv4 = str(raw.get("domain_controller_ipv4") or "").strip()
    acceptable = _string_list(raw.get("acceptable_domain_names"))
    if domain_fqdn:
        acceptable.extend([domain_fqdn, domain_fqdn.split(".", 1)[0]])
    if credential_domain:
        acceptable.append(credential_domain)
    return {
        "enabled": bool(credential_id and domain_fqdn),
        "credential_id": credential_id,
        "domain_fqdn": domain_fqdn,
        "credential_domain": credential_domain,
        "domain_controller_ipv4": domain_controller_ipv4,
        "acceptable_domain_names": sorted(set(item for item in acceptable if item), key=str.lower),
    }


def _domain_join_enabled(value: Any) -> bool:
    return bool(isinstance(value, dict) and value.get("enabled"))


def sanitize_role_options(server_role: str, role_options: dict | None) -> dict:
    raw = dict(role_options or {})
    if server_role == "base":
        return {}
    if server_role == "file_server":
        out = {
            "share_name": str(raw.get("share_name") or "").strip(),
            "share_path": str(raw.get("share_path") or "").strip(),
            "full_access_principals": _string_list(raw.get("full_access_principals")),
            "change_access_principals": _string_list(raw.get("change_access_principals")),
            "read_access_principals": _string_list(raw.get("read_access_principals")),
        }
        domain_join = _sanitize_domain_join(raw.get("domain_join"))
        if domain_join["enabled"]:
            out["domain_join"] = domain_join
        return out
    if server_role == "isolated_domain_controller":
        return {
            "forest_fqdn": str(raw.get("forest_fqdn") or "").strip().lower(),
            "netbios_name": str(raw.get("netbios_name") or "").strip().upper(),
            "forest_admin_credential_id": _positive_int(raw.get("forest_admin_credential_id")),
            "dsrm_credential_id": _positive_int(raw.get("dsrm_credential_id")),
        }
    if server_role == "mecm_prereq":
        out = {
            "prereq_profile": str(raw.get("prereq_profile") or "").strip(),
            "content_root": str(raw.get("content_root") or "").strip(),
        }
        domain_join = _sanitize_domain_join(raw.get("domain_join"))
        if domain_join["enabled"]:
            out["domain_join"] = domain_join
        return out
    if server_role == "lab_in_a_box":
        children = raw.get("children") if isinstance(raw.get("children"), list) else []
        sanitized_children = []
        for child in children:
            if not isinstance(child, dict):
                continue
            role = str(child.get("role") or "").strip()
            sanitized_children.append({
                "role": role,
                "vm_name": str(child.get("vm_name") or "").strip(),
                "role_options": sanitize_role_options(role, child.get("role_options") or {}),
            })
        return {
            "bundle_name": str(raw.get("bundle_name") or "").strip(),
            "domain_join_credential_id": _positive_int(raw.get("domain_join_credential_id")),
            "children": sanitized_children,
        }
    return raw


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def validate_role_options(
    server_role: str,
    role_options: dict | None,
    *,
    credential_exists: Callable[[int], bool] | None = None,
) -> list[dict]:
    if server_role not in ROLE_CATALOG:
        return [_blocking("unsupported_server_role", f"Unsupported OSDeploy server role: {server_role}")]
    role = ROLE_CATALOG[server_role]
    if not role.get("launchable"):
        return [_blocking("server_role_not_ready", f"OSDeploy server role is not launchable yet: {server_role}")]
    options = sanitize_role_options(server_role, role_options)
    checks: list[dict] = []
    for field in role.get("required_fields", []):
        if _is_blank(options.get(field)):
            checks.append(_blocking(f"role_option_missing_{field}", f"Role option '{field}' is required for {server_role}."))
    for field in role.get("credential_fields", []):
        cred_id = options.get(field)
        if not cred_id:
            continue
        if credential_exists is not None and not credential_exists(int(cred_id)):
            checks.append(_blocking(f"role_credential_missing_{field}", f"Credential id {cred_id} for '{field}' was not found."))
    if server_role == "file_server" and not options.get("full_access_principals"):
        checks.append(_blocking(
            "role_option_missing_full_access_principals",
            "Role option 'full_access_principals' is required for file_server.",
        ))
    if server_role == "mecm_prereq" and options.get("prereq_profile") not in {"site_server_foundation"}:
        checks.append(_blocking("role_option_invalid_prereq_profile", "MECM prereq profile must be 'site_server_foundation'."))
    domain_join = options.get("domain_join")
    if _domain_join_enabled(domain_join):
        cred_id = domain_join.get("credential_id")
        if credential_exists is not None and not credential_exists(int(cred_id)):
            checks.append(_blocking("role_credential_missing_domain_join", f"Credential id {cred_id} for domain join was not found."))
    if server_role == "lab_in_a_box":
        checks.extend(_validate_lab_children(options, credential_exists=credential_exists))
    return checks


def _validate_lab_children(
    options: dict,
    *,
    credential_exists: Callable[[int], bool] | None,
) -> list[dict]:
    checks: list[dict] = []
    children = options.get("children") or []
    expected_roles = ["isolated_domain_controller", "file_server", "mecm_prereq"]
    actual_roles = [child.get("role") for child in children]
    if actual_roles != expected_roles:
        checks.append(_blocking(
            "role_option_invalid_children",
            "Lab in a Box children must be isolated_domain_controller, file_server, then mecm_prereq.",
        ))
        return checks
    for index, child in enumerate(children, start=1):
        if _is_blank(child.get("vm_name")):
            checks.append(_blocking(f"role_option_missing_child_{index}_vm_name", "Each Lab in a Box child requires vm_name."))
        checks.extend(validate_role_options(
            str(child.get("role") or ""),
            child.get("role_options") or {},
            credential_exists=credential_exists,
        ))
    return checks
