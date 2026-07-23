"""Config + vault read/write helpers extracted from web.app.

Leaf module: depends only on stdlib + yaml (and a lazy ``web.db_pg`` import
inside ``_database_url``). It must never import ``web.app`` - that would
recreate the import cycle these extractions exist to break. ``web.app``
re-exports every public name here so existing ``web_app._load_proxmox_config``
call sites and test monkeypatches keep resolving through the web.app namespace.

Landmine to remember: ``VAULT_PATH`` / ``VARS_PATH`` are read *inside* the
moved ``_load_vault`` / ``_save_vault`` / ``_load_vars`` functions, so a test
that redirects the vault file must patch ``web.config_store.VAULT_PATH``, not
``web.app.VAULT_PATH`` (see test_settings_vault). ``BASE_DIR`` is intentionally
duplicated here and in web.app; both compute the same repo root.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
VARS_PATH = BASE_DIR / "inventory" / "group_vars" / "all" / "vars.yml"
VAULT_PATH = BASE_DIR / "inventory" / "group_vars" / "all" / "vault.yml"


def _load_vars():
    """Load vars.yml values only (not vault)."""
    if VARS_PATH.exists():
        with open(VARS_PATH) as f:
            data = yaml.safe_load(f)
        return data or {}
    return {}


def _load_vault():
    """Load vault.yml values only. Caller is responsible for never
    forwarding raw values to the browser - use _vault_presence() to
    report which keys are set without echoing them."""
    if VAULT_PATH.exists():
        with open(VAULT_PATH) as f:
            data = yaml.safe_load(f)
        return data or {}
    return {}


def _vault_presence() -> dict[str, bool]:
    """Return {key: True} for every vault key whose value is non-empty.
    Safe to render in HTML -- carries no secret material."""
    return {k: bool(v) for k, v in _load_vault().items()}


def _format_yaml_value(value):
    """Format a Python value as a safe YAML scalar."""
    if value is None or value == "null" or value == "":
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    s = str(value)
    # Quote strings that contain YAML-special characters or look like non-string types
    needs_quotes = (
        not s
        or s[0] in ("'", '"', '{', '[', '&', '*', '!', '|', '>', '%', '@', '`')
        or ':' in s or '#' in s or ',' in s
        or s.lower() in ('true', 'false', 'yes', 'no', 'null', 'on', 'off')
    )
    if not needs_quotes:
        return s
    # Prefer single quotes when the string contains backslashes - double-quoted
    # YAML processes escape sequences (`\U`, `\n`, etc.), which would mangle
    # Windows paths like 'C:\Users\Public' on save. Single-quoted scalars are
    # literal except for ' -> '' doubling.
    if '\\' in s and "'" not in s:
        return "'" + s + "'"
    # Fall back to double-quoted with backslash + double-quote escaped.
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _save_yaml_file(path: Path, updates: dict) -> None:
    """Update a YAML file by line-level replacement, preserving comments
    and any lines for keys outside the update set. Missing keys are
    appended. Used for both vars.yml and vault.yml."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\n")

    lines = path.read_text().splitlines()
    matched_keys: set = set()
    new_lines: list = []
    for line in lines:
        replaced = False
        for key, value in updates.items():
            pattern = rf'^(\s*{re.escape(key)}\s*:)\s*.*$'
            m = re.match(pattern, line)
            if m:
                new_lines.append(f"{m.group(1)} {_format_yaml_value(value)}")
                matched_keys.add(key)
                replaced = True
                break
        if not replaced:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in matched_keys:
            new_lines.append(f"{key}: {_format_yaml_value(value)}")

    path.write_text("\n".join(new_lines) + "\n")


def _save_vars(updates):
    """Update vars.yml. Wrapper for backward compat with callers."""
    _save_yaml_file(VARS_PATH, updates)


def _save_vault(updates):
    """Update vault.yml. Caller must already have stripped out empty
    secret values that mean 'keep current'."""
    _save_yaml_file(VAULT_PATH, updates)


def _load_proxmox_config():
    vars_path = BASE_DIR / "inventory" / "group_vars" / "all" / "vars.yml"
    vault_path = BASE_DIR / "inventory" / "group_vars" / "all" / "vault.yml"
    config = {}
    for p in [vars_path, vault_path]:
        if p.exists():
            with open(p) as f:
                data = yaml.safe_load(f)
                if data:
                    config.update(data)
    return config


def _database_url() -> str:
    from web import db_pg

    return db_pg.database_url()


def _auth_config() -> dict:
    cfg = _load_proxmox_config()
    # Env override wins over the vault setting so a single production
    # vault.yml can be shared across prod + local dev without the
    # local instance redirecting into production after login. Local
    # dev / macOS-native sets AUTOPILOT_AUTH_REDIRECT_URI to the
    # http://localhost:<port>/auth/callback registered against the
    # same Entra app as an additional Redirect URI.
    env_redirect = os.environ.get("AUTOPILOT_AUTH_REDIRECT_URI")
    redirect_uri = env_redirect or cfg.get(
        "auth_redirect_uri",
        "https://autopilot.gell.one/auth/callback",
    )
    requested_mode = (
        os.environ.get("AUTOPILOT_AUTH_MODE")
        or cfg.get("auth_mode")
        or "auto"
    ).strip().lower()
    if requested_mode not in {"auto", "entra", "local"}:
        requested_mode = "auto"
    tenant_id = cfg.get("vault_entra_tenant_id", "")
    client_id = cfg.get("vault_entra_app_id", "")
    entra_configured = bool(tenant_id and client_id)
    active_mode = requested_mode
    if active_mode == "auto":
        active_mode = "entra" if entra_configured else "local"
    return {
        "mode": active_mode,
        "requested_mode": requested_mode,
        "entra_configured": entra_configured,
        "local_enabled": active_mode == "local",
        "tenant_id": tenant_id,
        "client_id": client_id,
        "client_secret": cfg.get("vault_entra_app_secret", ""),
        "redirect_uri": redirect_uri,
        "admin_group_id": cfg.get("vault_entra_admin_group_id") or None,
    }
