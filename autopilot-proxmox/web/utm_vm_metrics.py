"""UTM VM metrics aggregator for the dashboard.

Wraps utm_cli.list_vms() to produce aggregate counts and a lightweight
per-VM list.  get_vm_ip() is intentionally NOT called here — IPs are too
slow for a dashboard poll and are lazy-loaded by /api/utm/vms instead.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_BASE_DIR = Path(__file__).resolve().parent.parent
_VARS_PATH = _BASE_DIR / "inventory" / "group_vars" / "all" / "vars.yml"


def _template_vm_name() -> str:
    """Return utm_template_vm_name from vars.yml, or '' if unset."""
    try:
        if _VARS_PATH.exists():
            with open(_VARS_PATH) as f:
                data = yaml.safe_load(f) or {}
            name = data.get("utm_template_vm_name", "")
            if name and "{{" not in str(name):
                return str(name)
    except Exception:
        pass
    return ""


def vm_summary() -> dict:
    """Return aggregate VM counts and a lightweight per-VM list.

    Uses utm_cli.list_vms(). Returns:
        {
            total: int,
            running: int,
            stopped: int,
            suspended: int,
            paused: int,
            template: str | None,   # name of the configured template VM
            vms: list[{name, uuid, status, is_template: bool}],
        }

    On any error the function returns the above shape with all counts
    set to -1 and an additional ``error`` key.
    """
    from web import utm_cli  # lazy import — utm_cli drags in yaml/subprocess

    template_name = _template_vm_name()

    try:
        raw_vms = utm_cli.list_vms()
    except RuntimeError as exc:
        return {
            "total": -1,
            "running": -1,
            "stopped": -1,
            "suspended": -1,
            "paused": -1,
            "template": template_name or None,
            "vms": [],
            "error": str(exc),
        }

    counts: dict[str, int] = {
        "running": 0,
        "stopped": 0,
        "suspended": 0,
        "paused": 0,
    }

    # utmctl uses "started" / "stopped" / "suspended" / "paused".
    # Normalise "started" → "running" for dashboard consistency.
    _STATUS_MAP = {
        "started": "running",
        "running": "running",
        "stopped": "stopped",
        "suspended": "suspended",
        "paused": "paused",
    }

    enriched: list[dict] = []
    for vm in raw_vms:
        raw_status = (vm.get("status") or "").lower()
        status = _STATUS_MAP.get(raw_status, raw_status)
        is_template = bool(
            template_name and vm.get("name") == template_name
        )
        enriched.append(
            {
                "name": vm.get("name", ""),
                "uuid": vm.get("uuid", ""),
                "status": status,
                "is_template": is_template,
            }
        )
        if status in counts:
            counts[status] += 1

    return {
        "total": len(raw_vms),
        "running": counts["running"],
        "stopped": counts["stopped"],
        "suspended": counts["suspended"],
        "paused": counts["paused"],
        "template": template_name or None,
        "vms": enriched,
    }
