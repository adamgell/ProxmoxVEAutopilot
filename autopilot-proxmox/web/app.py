import asyncio
import base64
import json
import os
import sqlite3
import subprocess
import time
import urllib3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.requests import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from web.jobs import JobManager
from web import devices_db
from web import jobs_db

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import re
import shlex
import socket
from urllib.parse import quote_plus

BASE_DIR = Path(__file__).resolve().parent.parent


def _redirect_with_error(path: str, error: str) -> RedirectResponse:
    """303-redirect to ``path`` with ``error`` safely percent-encoded.

    Use whenever rendering an exception message or user-supplied text into
    a redirect URL — raw f-string interpolation truncates at the first space
    or '#' and lets '&' smuggle extra params.
    """
    return RedirectResponse(f"{path}?error={quote_plus(str(error))}", status_code=303)


def _load_version() -> dict:
    """Read the build SHA + timestamp baked in at image build time."""
    version_file = os.environ.get("APP_VERSION_FILE", str(BASE_DIR / "VERSION"))
    sha = "unknown"
    build_time = "unknown"
    try:
        with open(version_file) as f:
            lines = f.read().splitlines()
            sha = (lines[0] if lines else "unknown").strip() or "unknown"
            build_time = (lines[1] if len(lines) > 1 else "unknown").strip() or "unknown"
    except Exception:
        pass
    return {"sha": sha, "sha_short": sha[:7] if sha != "unknown" else sha, "build_time": build_time}


_APP_VERSION = _load_version()
_LATEST_VERSION_CACHE: dict = {"fetched_at": 0, "sha": None, "sha_short": None, "error": None}

# Two flags, one per startup hook. /healthz gates on BOTH so
# docker-compose `depends_on: service_healthy` can't release
# builder/monitor after only one hook has completed — FastAPI runs
# startup hooks in registration order, so a single shared flag flipped
# True at the end of EITHER hook would leave a race window where the
# jobs.db migration (second hook) hasn't finished yet. Keeping
# sequences-init and jobs-init as separate functions matters because
# the jobs migration reads rows that sequences-init seeds.
_SEQUENCES_READY = False
_JOBS_READY = False


def _sanitize_input(value):
    """Reject input containing shell-dangerous characters."""
    if not re.match(r'^[\w\-\.]*$', str(value)):
        raise ValueError(f"Invalid input: {value!r} — only alphanumeric, hyphens, underscores, dots allowed")
    return str(value)


def _safe_path(base_dir, filename):
    """Resolve a filename and verify it stays inside base_dir. Raises ValueError on traversal."""
    resolved = (base_dir / filename).resolve()
    if not str(resolved).startswith(str(base_dir.resolve())):
        raise ValueError(f"Path traversal blocked: {filename}")
    return resolved
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
HASH_DIR = BASE_DIR / "output" / "hashes"
PLAYBOOK_DIR = BASE_DIR / "playbooks"
FILES_DIR = BASE_DIR / "files"
VARS_PATH = BASE_DIR / "inventory" / "group_vars" / "all" / "vars.yml"
SECRETS_DIR = BASE_DIR / "secrets"
SEQUENCES_DB = BASE_DIR / "output" / "sequences.db"
JOBS_DB = BASE_DIR / "output" / "jobs.db"
CREDENTIAL_KEY = SECRETS_DIR / "credential_key"
DEVICES_DB = BASE_DIR / "output" / "devices.db"
devices_db.init(DEVICES_DB)
DEVICE_MONITOR_DB = BASE_DIR / "output" / "device_monitor.db"

SETTINGS_SCHEMA = [
    # source defaults to "vars" for every field. Sections or individual
    # fields can set source="vault" to read/write inventory/group_vars/
    # all/vault.yml instead. Secret fields (type="secret") are rendered
    # as masked inputs, never echo their value to the browser, and
    # preserve the current value when the form submits blank.
    {"section": "Hypervisor Backend", "applies_to": "all", "fields": [
        {"key": "hypervisor_type", "label": "Hypervisor Type", "type": "text",
         "options": ["proxmox", "utm"],
         "labels": {"proxmox": "Proxmox VE (Linux/x86_64)",
                    "utm": "UTM + QEMU (macOS/ARM64)"},
         "help": "Proxmox = standard Docker deployment. UTM requires running the web service natively on macOS — see docs/UTM_MACOS_SETUP.md. Saving a new value reloads the page with the relevant settings sections."},
    ]},
    {"section": "UTM (macOS/ARM64) Configuration", "applies_to": "utm", "fields": [
        {"key": "utm_utmctl_path", "label": "utmctl Path", "type": "text",
         "help": "/Applications/UTM.app/Contents/MacOS/utmctl (ships with UTM)"},
        {"key": "utm_library_path", "label": "UTM Library Path", "type": "text",
         "help": "~/Library/Containers/com.utmapp.UTM/Data/Documents"},
        {"key": "utm_qemu_binary_path", "label": "QEMU Binary Path", "type": "text",
         "help": "/opt/homebrew/bin/qemu-system-aarch64 (only needed for direct QEMU operations)"},
        {"key": "utm_uefi_firmware_path", "label": "UEFI Firmware Path", "type": "text",
         "help": "/opt/homebrew/share/qemu/edk2-aarch64-code.fd"},
        {"key": "utm_template_vm_name", "label": "Template VM Name", "type": "text",
         "help": "Name or UUID of the Windows template VM registered in UTM (e.g., Windows11-Template-ARM64)"},
        {"key": "utm_exec_scratch_dir", "label": "Guest Scratch Directory", "type": "text",
         "help": "C:\\\\Users\\\\Public (writable path on guest used for exec output capture)"},
        {"key": "utm_iso_dir", "label": "ISO Directory", "type": "text",
         "help": "Host directory where Windows ARM64 installer ISOs live. Operator populates this manually."},
        {"key": "utm_skeleton_dir", "label": "Skeleton Bundle Directory", "type": "text",
         "help": "Where the repo-provided .utm skeleton bundles are stored. Usually leave at default."},
        {"key": "utm_qemu_img_path", "label": "qemu-img Path", "type": "text",
         "help": "Path to qemu-img. Install via `brew install qemu` if missing."},
        {"key": "utm_documents_dir", "label": "UTM Library Documents Dir", "type": "text",
         "help": "Where UTM.app stores its VM bundles."},
        {"key": "utm_windows_iso_name", "label": "Windows 11 ISO Filename", "type": "text",
         "help": "Filename (not path) of the Windows 11 ARM64 ISO inside utm_iso_dir."},
        {"key": "utm_windows_server_iso_name", "label": "Windows Server ISO Filename", "type": "text",
         "help": "Filename (not path) of the Windows Server ARM64 ISO inside utm_iso_dir."},
        {"key": "utm_network_mode", "label": "Network Mode", "type": "text",
         "options": ["shared", "bridged", "host"],
         "labels": {"shared": "Shared (NAT)", "bridged": "Bridged (LAN-visible)",
                    "host": "Host-Only"},
         "help": "Shared = macOS NAT (default, works everywhere). Bridged gets the VM its own LAN IP (required for Autopilot OOBE discovery on some networks). Host-Only = isolated subnet with the host."},
        {"key": "utm_bridge_interface", "label": "Bridge Interface", "type": "text",
         "help": "macOS network interface to bridge to, e.g. en0 for Wi-Fi or Thunderbolt Ethernet. Only used when Network Mode = bridged. Leave empty to let UTM pick the default."},
    ]},
    {"section": "Proxmox Connection", "applies_to": "proxmox", "fields": [
        {"key": "proxmox_host", "label": "Host", "type": "text"},
        {"key": "proxmox_port", "label": "Port", "type": "number"},
        {"key": "proxmox_node", "label": "Node", "type": "text"},
        {"key": "proxmox_validate_certs", "label": "Validate Certs", "type": "bool"},
    ]},
    {"section": "Credentials (vault.yml)", "source": "vault", "applies_to": "all", "fields": [
        {"key": "vault_proxmox_api_token_id",
         "label": "Proxmox API Token ID", "type": "text",
         "applies_to": "proxmox",
         "help": "e.g. autopilot@pve!ansible"},
        {"key": "vault_proxmox_api_token_secret",
         "label": "Proxmox API Token Secret", "type": "secret",
         "applies_to": "proxmox"},
        {"key": "vault_proxmox_root_username",
         "label": "Proxmox Root Username", "type": "text",
         "applies_to": "proxmox",
         "help": "Default: root@pam. Used only for the args PUT on VMs that need a chassis override."},
        {"key": "vault_proxmox_root_password",
         "label": "Proxmox Root Password", "type": "secret",
         "applies_to": "proxmox",
         "help": "Required for chassis-type overrides. Fetched just-in-time as a /access/ticket per provision; never leaves the container."},
        {"key": "vault_entra_app_id",
         "label": "Entra App (client) ID", "type": "text"},
        {"key": "vault_entra_tenant_id",
         "label": "Entra Tenant ID", "type": "text"},
        {"key": "vault_entra_app_secret",
         "label": "Entra App Secret", "type": "secret"},
    ]},
    {"section": "Storage & Networking", "applies_to": "proxmox", "fields": [
        {"key": "proxmox_storage", "label": "VM Storage", "type": "text"},
        {"key": "proxmox_iso_storage", "label": "ISO Storage", "type": "text"},
        {"key": "proxmox_bridge", "label": "Network Bridge", "type": "text"},
        {"key": "proxmox_vlan_tag", "label": "VLAN Tag", "type": "text"},
    ]},
    {"section": "ISO Paths", "applies_to": "proxmox", "fields": [
        {"key": "proxmox_windows_iso", "label": "Windows ISO", "type": "text"},
        {"key": "proxmox_virtio_iso", "label": "VirtIO ISO", "type": "text"},
        {"key": "proxmox_answer_iso", "label": "Answer ISO", "type": "text"},
    ]},
    {"section": "Template", "applies_to": "proxmox", "fields": [
        {"key": "proxmox_template_vmid", "label": "Template VMID", "type": "number"},
    ]},
    {"section": "VM Defaults", "applies_to": "all", "fields": [
        {"key": "vm_cores", "label": "CPU Cores", "type": "number"},
        {"key": "vm_memory_mb", "label": "Memory (MB)", "type": "number"},
        {"key": "vm_disk_size_gb", "label": "Disk Size (GB)", "type": "number"},
        {"key": "vm_ostype", "label": "OS Type", "type": "text"},
        {"key": "vm_count", "label": "Default VM Count", "type": "number"},
        {"key": "vm_name_prefix", "label": "Name Prefix", "type": "text"},
        {"key": "vm_start_after_create", "label": "Start After Create", "type": "bool"},
        {"key": "vm_oem_profile", "label": "Default OEM Profile", "type": "text"},
        {"key": "vm_serial_prefix", "label": "Serial Prefix (generated as <prefix>-HEX)", "type": "text"},
        {"key": "vm_custom_serial", "label": "Fixed Serial (one-VM override — leave blank)", "type": "text"},
        {"key": "vm_group_tag", "label": "Default Group Tag", "type": "text"},
    ]},
    {"section": "Autopilot", "fields": [
        {"key": "autopilot_skip", "label": "Skip Autopilot Inject", "type": "bool"},
        {"key": "capture_hardware_hash", "label": "Capture Hash", "type": "bool"},
    ]},
    {"section": "Timeouts", "applies_to": "all", "fields": [
        {"key": "guest_agent_timeout_seconds", "label": "Guest Agent Timeout (s)", "type": "number",
         "applies_to": "proxmox"},
        {"key": "guest_agent_poll_interval_seconds", "label": "Guest Agent Poll (s)", "type": "number",
         "applies_to": "proxmox"},
        {"key": "guest_exec_timeout_seconds", "label": "Guest Exec Timeout (s)", "type": "number"},
        {"key": "guest_exec_poll_interval_seconds", "label": "Guest Exec Poll (s)", "type": "number"},
        {"key": "hash_capture_timeout_seconds", "label": "Hash Capture Timeout (s)", "type": "number"},
        {"key": "task_poll_retries", "label": "Task Poll Retries", "type": "number",
         "applies_to": "proxmox"},
        {"key": "task_poll_delay_seconds", "label": "Task Poll Delay (s)", "type": "number",
         "applies_to": "proxmox"},
    ]},
]


def _load_vars():
    """Load vars.yml values only (not vault)."""
    if VARS_PATH.exists():
        with open(VARS_PATH) as f:
            data = yaml.safe_load(f)
        return data or {}
    return {}


VAULT_PATH = BASE_DIR / "inventory" / "group_vars" / "all" / "vault.yml"


def _load_vault():
    """Load vault.yml values only. Caller is responsible for never
    forwarding raw values to the browser — use _vault_presence() to
    report which keys are set without echoing them."""
    if VAULT_PATH.exists():
        with open(VAULT_PATH) as f:
            data = yaml.safe_load(f)
        return data or {}
    return {}


def _vault_presence() -> dict[str, bool]:
    """Return {key: True} for every vault key whose value is non-empty.
    Safe to render in HTML — carries no secret material."""
    return {k: bool(v) for k, v in _load_vault().items()}


def _fetch_settings_options():
    """Query Proxmox API to populate dropdown options for settings fields."""
    options = {}
    try:
        # Nodes
        nodes = _proxmox_api("/nodes")
        options["proxmox_node"] = sorted([n["node"] for n in nodes])

        cfg = _load_proxmox_config()
        node = cfg.get("proxmox_node", options["proxmox_node"][0] if options.get("proxmox_node") else "pve")

        # Storage — split by content type
        storages = _proxmox_api("/storage")
        disk_storages = sorted([s["storage"] for s in storages if "images" in s.get("content", "")])
        iso_storages = sorted([s["storage"] for s in storages if "iso" in s.get("content", "")])
        options["proxmox_storage"] = disk_storages
        options["proxmox_iso_storage"] = iso_storages

        # Bridges
        try:
            networks = _proxmox_api(f"/nodes/{node}/network")
            options["proxmox_bridge"] = sorted([n["iface"] for n in networks if n.get("type") in ("bridge", "OVSBridge")])
        except Exception:
            options["proxmox_bridge"] = ["vmbr0"]

        # ISOs
        for iso_store in iso_storages:
            try:
                isos = _proxmox_api(f"/nodes/{node}/storage/{iso_store}/content")
                iso_list = sorted([i["volid"] for i in isos if i.get("format") == "iso"])
                if iso_list:
                    options["proxmox_windows_iso"] = iso_list
                    options["proxmox_virtio_iso"] = iso_list
                    options["proxmox_answer_iso"] = iso_list
                break
            except Exception:
                pass

        # Templates (VMs marked as template)
        try:
            vms = _proxmox_api(f"/nodes/{node}/qemu")
            templates_list = [{"vmid": v["vmid"], "name": v.get("name", "")} for v in vms if v.get("template")]
            options["proxmox_template_vmid"] = [f"{t['vmid']}" for t in sorted(templates_list, key=lambda x: x["vmid"])]
            options["proxmox_template_vmid_labels"] = {str(t["vmid"]): f"{t['vmid']} ({t['name']})" for t in templates_list}
        except Exception:
            pass

        # OEM profiles
        profiles = load_oem_profiles()
        options["vm_oem_profile"] = list(profiles.keys())

    except Exception:
        pass  # If Proxmox is unreachable, fields stay as text inputs

    return options


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
    # Prefer single quotes when the string contains backslashes — double-quoted
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

app = FastAPI(title="Proxmox VE Autopilot")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
job_manager = JobManager(
    jobs_dir=str(BASE_DIR / "jobs"),
    jobs_db_path=JOBS_DB,
)

from web import sequences_db, crypto as _crypto
from web import sequence_compiler
from web import device_history_db, device_monitor
from web import auth as _auth


# ---------------------------------------------------------------------------
# Entra OIDC auth — every route except /auth/*, /healthz, /api/version,
# and /static/ requires a valid session.
# ---------------------------------------------------------------------------

_SESSION_SECRET_FILE = SECRETS_DIR / "session_secret"


def _load_or_create_session_secret() -> str:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    if _SESSION_SECRET_FILE.exists():
        return _SESSION_SECRET_FILE.read_text().strip()
    import secrets as _sec
    s = _sec.token_urlsafe(48)
    _SESSION_SECRET_FILE.write_text(s)
    try:
        os.chmod(_SESSION_SECRET_FILE, 0o600)
    except Exception:
        pass
    return s


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
    return {
        "tenant_id": cfg.get("vault_entra_tenant_id", ""),
        "client_id": cfg.get("vault_entra_app_id", ""),
        "client_secret": cfg.get("vault_entra_app_secret", ""),
        "redirect_uri": redirect_uri,
        "admin_group_id": cfg.get("vault_entra_admin_group_id") or None,
    }


# Tests set AUTOPILOT_AUTH_BYPASS=1 so the middleware doesn't redirect
# every fixture to /auth/login. Never set this in production — the
# fail-open behaviour is only safe inside pytest.
_AUTH_BYPASS = os.environ.get("AUTOPILOT_AUTH_BYPASS") == "1"


# NOTE on middleware order: Starlette runs the LAST-added middleware
# first, so we register _require_auth FIRST (becomes inner) and
# SessionMiddleware LAST (becomes outer). That way SessionMiddleware
# populates request.session before _require_auth tries to read it.
@app.middleware("http")
async def _require_auth(request: Request, call_next):
    """Reject un-authenticated requests before they reach any handler.
    Exempt routes get a free pass; everything else redirects to
    /auth/login (for HTML) or returns 401 (for JSON/API)."""
    if _AUTH_BYPASS:
        return await call_next(request)
    if _auth.is_exempt_path(request.url.path):
        return await call_next(request)
    if request.session.get("user"):
        return await call_next(request)
    accept = request.headers.get("accept", "")
    if request.url.path.startswith("/api/") or "application/json" in accept:
        return JSONResponse(
            status_code=401,
            content={"detail": "authentication required"},
        )
    next_url = request.url.path
    if request.url.query:
        next_url += "?" + request.url.query
    return RedirectResponse(
        url=f"/auth/login?next={next_url}", status_code=302,
    )


@app.get("/auth/login", response_class=HTMLResponse)
async def auth_login(request: Request, next: str = "/",
                     error: str = ""):
    """Branded landing page. Operator clicks 'Sign in with Microsoft'
    to actually hand off to Entra — see /auth/login/start for the
    redirect itself. The intermediate page is a deliberate UX choice
    so (a) operators see the app name before MS takes over, and
    (b) callback failures can surface a readable error banner
    instead of a raw HTML error page."""
    cfg = _auth_config()
    if not cfg["tenant_id"] or not cfg["client_id"]:
        return HTMLResponse(
            "<h1>Auth not configured</h1>"
            "<p>Set <code>vault_entra_tenant_id</code> and "
            "<code>vault_entra_app_id</code> in vault.yml, then run "
            "<code>scripts/ad/configure_entra_auth.py</code>.</p>",
            status_code=500,
        )
    # Short, human-readable tenant label — prefer ad_realm from
    # vars.yml, then the Kerberos realm baked into /etc/krb5.conf,
    # then fall back to the raw GUID so the page still renders.
    app_cfg = _load_proxmox_config()
    tenant_label = app_cfg.get("ad_realm") or ""
    if not tenant_label:
        try:
            for line in Path("/etc/krb5.conf").read_text().splitlines():
                line = line.strip()
                if line.lower().startswith("default_realm"):
                    _, _, v = line.partition("=")
                    tenant_label = v.strip()
                    break
        except Exception:
            pass
    if not tenant_label:
        tenant_label = cfg["tenant_id"]
    from urllib.parse import quote as _q
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        # URL-encode so a next path containing ?&= survives being
        # re-emitted into the Sign-in link's query string.
        "next_url": _q(_auth.safe_next_url(next), safe=""),
        "tenant_label": tenant_label,
        "build_sha": (_APP_VERSION.get("sha_short") or "unknown"),
        "build_time": _APP_VERSION.get("build_time", ""),
    })


@app.get("/auth/login/start")
async def auth_login_start(request: Request, next: str = "/"):
    """Actually hand off to Entra. Separate from /auth/login so the
    landing page can show the app name + 'Sign in with Microsoft'
    button first. State + PKCE are generated here, stashed in the
    session, and validated in /auth/callback."""
    cfg = _auth_config()
    if not cfg["tenant_id"] or not cfg["client_id"]:
        return RedirectResponse(
            url="/auth/login?error=auth+not+configured",
            status_code=302,
        )
    url = _auth.build_login_url(
        request,
        tenant_id=cfg["tenant_id"],
        client_id=cfg["client_id"],
        redirect_uri=cfg["redirect_uri"],
        next_url=_auth.safe_next_url(next),
    )
    return RedirectResponse(url=url, status_code=302)


def _login_error(message: str, *, next_url: str = "/") -> RedirectResponse:
    """Redirect back to the branded /auth/login page with an error
    banner, rather than dumping raw HTML from the middle of the
    auth flow. 303 so the browser follows cleanly on re-POST."""
    from urllib.parse import quote as _q
    return RedirectResponse(
        url=f"/auth/login?error={_q(message)}&next={_q(next_url)}",
        status_code=303,
    )


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "",
                        state: str = "", error: str = "",
                        error_description: str = ""):
    """Exchange the auth code for an ID token, validate, drop session."""
    next_url = _auth.safe_next_url(request.session.pop("oidc_next", "/"))
    if error:
        return _login_error(
            f"Sign-in cancelled ({error}): {error_description}",
            next_url=next_url,
        )
    expected_state = request.session.pop("oidc_state", None)
    code_verifier = request.session.pop("oidc_verifier", None)
    if not code or not expected_state or state != expected_state:
        return _login_error(
            "Sign-in flow got out of sync. Please try again.",
            next_url=next_url,
        )
    cfg = _auth_config()
    try:
        tok = _auth.exchange_code_for_token(
            tenant_id=cfg["tenant_id"],
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            redirect_uri=cfg["redirect_uri"],
            code=code, code_verifier=code_verifier,
        )
    except HTTPException as e:
        return _login_error(
            f"Token exchange failed: {e.detail}", next_url=next_url,
        )
    id_token = tok.get("id_token")
    if not id_token:
        return _login_error(
            "Token response didn't include an id_token — confirm the "
            "app reg has 'ID tokens' enabled.",
            next_url=next_url,
        )
    try:
        claims = _auth.validate_id_token(
            id_token,
            tenant_id=cfg["tenant_id"],
            client_id=cfg["client_id"],
        )
    except Exception as e:
        return _login_error(
            f"Token validation failed: {e}", next_url=next_url,
        )
    user = _auth.user_from_claims(claims)
    if not _auth.is_authorized(user, admin_group_id=cfg["admin_group_id"]):
        who = user.get("email") or user.get("upn") or "this user"
        return _login_error(
            f"{who} signed in but isn't a member of the required "
            f"admin group. Ask an admin to add them.",
            next_url=next_url,
        )
    request.session["user"] = user
    return RedirectResponse(url=next_url, status_code=302)


@app.get("/auth/logout")
async def auth_logout(request: Request):
    """Clear the session; operator's Entra browser session remains
    (they'd need to log out of Microsoft itself to fully sign out).
    After clearing, hand back to Entra's logout endpoint so MS clears
    too — best effort; stubbed out as a simple redirect to /."""
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)


# /healthz is in _EXEMPT_PREFIXES (see web/auth.py) so the compose
# healthcheck and depends_on: service_healthy gate can hit it without
# a session cookie. Anything leaked here should be limited to schema
# readiness — no device/job/secrets data.
@app.get("/healthz")
async def healthz():
    """Uptime probe — exempt from auth so external monitors can hit it.
    Gated on BOTH _SEQUENCES_READY and _JOBS_READY: returns 503 until
    every startup schema-init hook finishes, so docker-compose dependents
    (builder/monitor) don't start against a half-initialized DB."""
    if not (_SEQUENCES_READY and _JOBS_READY):
        raise HTTPException(503, "schema init not complete")
    return {"ok": True}


_BLISS_JPG = BASE_DIR / "files" / "bliss.jpg"
_LOGO_SVG = BASE_DIR / "files" / "logo.svg"


@app.get("/auth/logo")
async def auth_logo():
    """Serve the app mark (SVG). Exempt from auth via the /auth/
    prefix so it renders on the login page. Same extension-less
    pattern as /auth/bliss — avoids the upstream nginx static-asset
    interceptor."""
    if not _LOGO_SVG.exists():
        raise HTTPException(404, "logo.svg not baked into image")
    return FileResponse(
        _LOGO_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@app.get("/favicon.ico")
async def favicon():
    """Browsers auto-fetch /favicon.ico; serve the SVG mark so tabs
    show the app icon. Exempt route via explicit exemption list in
    _require_auth — shipping as SVG (not ICO) is fine for modern
    browsers and saves bundling a raster version."""
    if not _LOGO_SVG.exists():
        raise HTTPException(404)
    return FileResponse(
        _LOGO_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@app.get("/auth/bliss")
async def auth_bliss():
    """Serve the login-page background. Extension-less path on
    purpose — our upstream nginx/NPM has a static-asset rule that
    502s .jpg paths without proxying to the backend. Returning
    image/jpeg content via a dynamic route sidesteps that."""
    if not _BLISS_JPG.exists():
        raise HTTPException(404, "bliss.jpg not baked into image")
    return FileResponse(
        _BLISS_JPG,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=86400, immutable",
        },
    )


# SessionMiddleware is the LAST middleware registered so it sits OUTER-
# most in Starlette's stack — runs before every other middleware +
# handler, so request.session is ready when _require_auth checks it.
_auth.install_session_middleware(
    app, secret=_load_or_create_session_secret(),
)


@app.on_event("startup")
def _init_sequences_db() -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    sequences_db.init(SEQUENCES_DB)
    sequences_db.seed_defaults(SEQUENCES_DB, _cipher())
    device_history_db.init(DEVICE_MONITOR_DB)
    global _SEQUENCES_READY
    _SEQUENCES_READY = True


@app.on_event("startup")
def _init_jobs_db_and_migrate() -> None:
    from web import jobs_db, jobs_migration
    jobs_db.init(JOBS_DB)
    jobs_migration.migrate_legacy_index(
        jobs_dir=Path(job_manager.jobs_dir),
        db_path=JOBS_DB,
    )
    global _JOBS_READY
    _JOBS_READY = True


@app.on_event("startup")
def _ensure_hypervisor_type_default() -> None:
    """Ensure hypervisor_type defaults to 'proxmox' if not already set.
    
    This migration runs at startup to support existing deployments
    that predate the multi-hypervisor feature.
    """
    current_vars = _load_vars()
    if current_vars.get("hypervisor_type") is None:
        current_vars["hypervisor_type"] = "proxmox"
        _save_vars({"hypervisor_type": "proxmox"})


# Note: the periodic device-monitor sweep + keytab-refresh loop used
# to live here as `_device_monitor_loop` + `_start_device_monitor_loop`.
# As of the microservice split (Task 15/16) those tasks are owned by
# the `monitor` container via `web/monitor_main.py`. Running them here
# too would cause double-sweeps and double keytab probes, so the web
# container no longer starts them. The `_run_keytab_checks`,
# `_build_live_monitor_context`, and `_vm_provisioning_vmids` helpers
# below are kept because `monitor_main` imports them (and the "refresh
# now" HTTP endpoint still calls `_run_keytab_checks` directly).


_HEALTH_TASK: Optional["asyncio.Task"] = None


def _load_version_sha() -> str:
    """Best-effort running git SHA. Matches the footer's buildSha."""
    try:
        path = BASE_DIR / "VERSION"
        if path.exists():
            return path.read_text().strip()[:7]
    except Exception:
        pass
    return "unknown"


@app.on_event("startup")
async def _start_health_heartbeat() -> None:
    import asyncio
    import logging as _logging
    from web import service_health
    service_health.init(DEVICE_MONITOR_DB)

    async def _loop():
        while True:
            try:
                service_health.heartbeat(
                    DEVICE_MONITOR_DB,
                    service_id="web",
                    service_type="web",
                    version_sha=_load_version_sha(),
                    detail="idle",
                )
            except Exception:
                _logging.getLogger("web.health").exception("heartbeat failed")
            await asyncio.sleep(10)

    global _HEALTH_TASK
    _HEALTH_TASK = asyncio.create_task(_loop())


@app.on_event("shutdown")
async def _stop_health_heartbeat() -> None:
    import asyncio
    if _HEALTH_TASK is None:
        return
    _HEALTH_TASK.cancel()
    try:
        await _HEALTH_TASK
    except (asyncio.CancelledError, Exception):
        pass


def _run_keytab_checks() -> None:
    """Runs in a worker thread. Never raises — all failures land in
    keytab_health.last_probe_* / last_refresh_*.

    Refresh cadence: once per calendar day, OR whenever the probe
    says the keytab is missing/broken/stale/kvno-mismatch."""
    from web import keytab_monitor
    import logging as _logging
    _log = _logging.getLogger("web.keytab_monitor.loop")

    cfg = _load_proxmox_config()
    keytab_path = cfg.get("ad_keytab_path", "/app/secrets/krb5.keytab")
    ldap_host = cfg.get("ldap_host", "dns.home.gell.one")
    realm = cfg.get("ad_realm", "HOME.GELL.ONE")
    gmsa_sam = cfg.get("ad_gmsa_sam", "svc-apmon")
    gmsa_dn = cfg.get(
        "ad_gmsa_dn",
        "CN=svc-apmon,CN=Managed Service Accounts,DC=home,DC=gell,DC=one",
    )
    principal = cfg.get("ad_kerberos_principal", f"{gmsa_sam}$@{realm}")

    probe = keytab_monitor.probe_keytab(
        keytab_path=keytab_path, principal=principal,
        ldap_host=ldap_host, gmsa_dn=gmsa_dn,
    )
    keytab_monitor.record_probe(DEVICE_MONITOR_DB, probe)
    _log.info("keytab probe: %s (%s)", probe.status, probe.message)

    # Refresh when:
    #   - probe says missing / broken / kvno-mismatch (anything but OK
    #     or STALE — STALE means refresher just slipped a bit, normal
    #     daily cadence will catch it), OR
    #   - last_refresh_at is >24h ago (daily cadence).
    current = device_history_db.get_keytab_health(DEVICE_MONITOR_DB) or {}
    last_refresh_at = current.get("last_refresh_at")
    last_refresh_ok = current.get("last_refresh_ok")
    needs_refresh = probe.status in (
        keytab_monitor.STATUS_MISSING,
        keytab_monitor.STATUS_BROKEN,
        keytab_monitor.STATUS_KVNO_MISMATCH,
    )
    if not needs_refresh:
        if not last_refresh_at:
            needs_refresh = True                         # never refreshed
        elif last_refresh_ok == 0:
            needs_refresh = True                         # last attempt failed — retry
        elif keytab_monitor._age_hours(last_refresh_at) >= 24:
            needs_refresh = True                         # daily cadence

    if not needs_refresh:
        return

    # Resolve the admin credential used to kinit for retrieving the
    # gMSA password. Reuses the monitoring_settings.ad_credential_id
    # (the same knob the AD-search config pointed at in the earlier
    # design; now repurposed as "refresher kinit cred").
    cred = _resolve_ad_credential()
    admin_user = cred.get("username") or ""
    admin_pw = cred.get("password") or ""
    if not admin_user or not admin_pw:
        device_history_db.update_keytab_refresh(
            DEVICE_MONITOR_DB, ok=False,
            message=(
                "no AD credential configured for keytab refresh "
                "(set monitoring_settings.ad_credential_id to a "
                "credential with Retrieve-Password rights on the gMSA)"
            ),
        )
        return

    # Normalise the username to Kerberos principal form:
    # "home\adam_admin" → "adam_admin@HOME.GELL.ONE"
    if "\\" in admin_user:
        _dom, bare = admin_user.split("\\", 1)
        kinit_principal = f"{bare}@{realm}"
    elif "@" in admin_user:
        kinit_principal = admin_user.upper()
        # Force UPPER realm half.
        user_part, _, realm_part = kinit_principal.partition("@")
        kinit_principal = f"{user_part.lower()}@{realm_part.upper()}"
    else:
        kinit_principal = f"{admin_user}@{realm}"

    ok, msg = keytab_monitor.refresh_keytab(
        db_path=DEVICE_MONITOR_DB,
        kinit_principal=kinit_principal,
        kinit_password=admin_pw,
        keytab_path=keytab_path,
        gmsa_dn=gmsa_dn,
        ldap_host=ldap_host,
        realm=realm,
        gmsa_sam=gmsa_sam,
    )
    _log.info("keytab refresh: ok=%s msg=%s", ok, msg)


def _vm_provisioning_vmids() -> set:
    """VMIDs that were provisioned through this system — stays
    in-scope for the monitor even if the ``autopilot`` tag was
    later stripped off."""
    import sqlite3
    try:
        conn = sqlite3.connect(SEQUENCES_DB)
        try:
            rows = conn.execute("SELECT vmid FROM vm_provisioning").fetchall()
        finally:
            conn.close()
        return {int(r[0]) for r in rows}
    except Exception:
        return set()


def _sid_bytes_to_str(b: bytes) -> str:
    """Convert a Windows NT SID binary blob to canonical 'S-r-a-s-s-...' form.

    Layout (see MS-DTYP 2.4.2.2):
      byte 0:    Revision (always 1)
      byte 1:    SubAuthorityCount (N)
      bytes 2-7: IdentifierAuthority (48-bit big-endian)
      bytes 8+:  SubAuthority[0..N-1] (each 32-bit little-endian)

    Used when copying ``objectSid`` out of AD so the value compares
    cleanly against Graph's ``onPremisesSecurityIdentifier`` (which
    uses the canonical string form). Before this, AD's SID was hex-
    encoded from raw bytes, which meant the linkage-health check on
    the device detail page always showed a red X even for correctly
    hybrid-joined devices.
    """
    if not b or len(b) < 8:
        return b.hex() if b else ""
    revision = b[0]
    # MS-DTYP 2.4.2.2: revision is always 1 in every shipped Windows.
    # Anything else means a corrupted or non-SID blob; fall back to
    # hex rather than emit nonsense canonical form.
    if revision != 1:
        return b.hex()
    sub_count = b[1]
    authority = int.from_bytes(b[2:8], "big")
    subs = [
        int.from_bytes(b[8 + 4 * i : 12 + 4 * i], "little")
        for i in range(sub_count)
    ]
    return "S-" + str(revision) + "-" + str(authority) + "".join(
        "-" + str(s) for s in subs
    )


def _guid_bytes_to_str(b: bytes) -> str:
    """Convert a Windows ``objectGUID`` (16 raw bytes in mixed-endian
    order) to the canonical hyphenated UUID string Graph returns. Same
    motivation as _sid_bytes_to_str — raw hex from AD never matched
    the UUID-format values other systems use.
    """
    import uuid as _uuid
    if not b or len(b) != 16:
        return b.hex() if b else ""
    try:
        return str(_uuid.UUID(bytes_le=b))
    except Exception:
        return b.hex()


def _build_live_monitor_context() -> "device_monitor.MonitorContext":
    """Wire the real I/O backends: Proxmox API for VM list + config,
    existing _guest_exec helper for Windows probes, ldap3 for AD,
    requests for Graph. Resolves credentials from vault.yml +
    sequences DB on every call so rotations take effect without a
    restart."""
    cfg = _load_proxmox_config()
    # --- Proxmox ---
    def _list_pve_vms() -> list[dict]:
        # Cluster-wide resources include node + status + tags + name.
        rows = _proxmox_api("/cluster/resources?type=vm") or []
        out = []
        for r in rows:
            if r.get("type") != "qemu":
                continue
            out.append({
                "vmid": int(r.get("vmid", 0)),
                "name": r.get("name", ""),
                "node": r.get("node", ""),
                "status": r.get("status", ""),
                "tags": r.get("tags", "") or "",
                "lock": r.get("lock"),
            })
        return out

    def _fetch_pve_config(vmid: int, node: str) -> dict:
        return _proxmox_api(f"/nodes/{node}/qemu/{vmid}/config") or {}

    def _fetch_guest_details(vmid: int, node: str):
        # Two-tier fallback so serial/name are populated even when the
        # guest agent is down:
        #   1. guest-exec for the authoritative Windows-side values
        #      (what the OS actually sees — catches renames, drift)
        #   2. PVE config.smbios1 for the "what we provisioned" baseline
        #      — always available, proves-out Intune lookups even for
        #      stopped VMs that match by serial.
        raw = {}
        try:
            raw = _fetch_guest_windows_details(node, vmid) or {}  # type: ignore[name-defined]
        except NameError:
            raw = {}
        except Exception:
            raw = {}
        # Fall back to PVE config when guest-exec is unavailable.
        # Note: for autopilot provisions the `smbios1` config field
        # holds the TEMPLATE's default values (e.g., Gell-1F02ADE6) —
        # the real per-VM SMBIOS serial lives in the binary file
        # referenced by `args: -smbios file=<…>.bin`, which we can't
        # cheaply parse here. But our provisioning convention makes
        # vm_name == serial (both `Gell-<hex>`), so trust that when
        # the VM is autopilot-tagged OR name matches the pattern.
        try:
            cfg = _proxmox_api(f"/nodes/{node}/qemu/{vmid}/config") or {}
        except Exception:
            cfg = {}
        smbios1 = cfg.get("smbios1", "")
        uuid_fallback = _decode_smbios_field(smbios1, "uuid")
        vm_name_fallback = cfg.get("name")
        tags = (cfg.get("tags") or "")
        autopilot_tagged = any(
            t.strip() == "autopilot"
            for t in tags.replace(";", ",").split(",")
        )
        if autopilot_tagged or (
            vm_name_fallback and vm_name_fallback.lower().startswith("gell-")
        ):
            serial_fallback = vm_name_fallback
        else:
            serial_fallback = _decode_smbios_field(smbios1, "serial")

        win_name = raw.get("Name") or raw.get("ComputerName")
        serial = raw.get("Serial") or raw.get("SerialNumber") or serial_fallback
        uuid = raw.get("UUID") or raw.get("ProductUUID") or uuid_fallback
        if not (win_name or serial or uuid or raw):
            # Nothing usable from either source — let the sweep record
            # a 'guest agent down' error and move on.
            return None
        return {
            # win_name defaults to the VM's PVE name when the guest can't
            # be asked — not perfect (won't catch a Windows-side rename)
            # but enough to drive AD lookup by cn.
            "win_name": win_name or vm_name_fallback,
            "serial": serial,
            "uuid": uuid,
            "os_build": raw.get("OSBuild") or raw.get("BuildNumber"),
            "dsreg": {
                "AzureAdJoined": raw.get("AadJoined"),
                "DomainJoined": raw.get("PartOfDomain"),
                "Domain": raw.get("Domain"),
                "TenantName": raw.get("AadTenant"),
            },
        }

    # --- LDAP (python-ldap + SASL/GSSAPI) ---
    ldap_host = cfg.get("ldap_host", "dns.home.gell.one")
    keytab_path = cfg.get("ad_keytab_path", "/app/secrets/krb5.keytab")
    ad_principal = cfg.get("ad_kerberos_principal", "svc-apmon$@HOME.GELL.ONE")

    def _ad_search(search_base: str, win_name: str) -> list[dict]:
        # python-ldap binds via libsasl2-modules-gssapi-mit, which
        # negotiates signing with the DC automatically (SASL SSF: 256).
        # GSSAPI auto-acquires credentials from the keytab via
        # KRB5_CLIENT_KTNAME + KRB5_CCNAME; we set both so the sweep
        # doesn't depend on any external kinit. The keytab is kept
        # fresh by the DC-side refresher cron (B.8); staleness
        # surfaces as a health probe signal (B.4).
        import os
        import ldap
        import ldap.sasl
        import ldap.filter

        if not Path(keytab_path).exists():
            raise RuntimeError(
                f"Kerberos keytab not found at {keytab_path}. Configure "
                "the DC-side refresher cron (docs/specs/...#auth-follow-up) "
                "or set ad_keytab_path in vars.yml."
            )
        # Point MIT Kerberos at our keytab + a private ccache per
        # process so concurrent sweeps don't clobber each other.
        env = os.environ.copy()
        os.environ["KRB5_CLIENT_KTNAME"] = keytab_path
        os.environ.setdefault("KRB5CCNAME", f"FILE:/tmp/krb5cc_ad_{os.getpid()}")
        try:
            l = ldap.initialize(f"ldap://{ldap_host}")
            l.set_option(ldap.OPT_REFERRALS, 0)
            l.set_option(ldap.OPT_PROTOCOL_VERSION, 3)
            l.set_option(ldap.OPT_NETWORK_TIMEOUT, 10)
            # sasl.gssapi("") uses the default (keytab-backed) principal.
            l.sasl_interactive_bind_s("", ldap.sasl.gssapi(""))
            try:
                fltr = ("(&(objectClass=computer)(name="
                        + ldap.filter.escape_filter_chars(win_name) + "))")
                raw = l.search_s(
                    search_base, ldap.SCOPE_SUBTREE, fltr,
                    [
                        "objectGUID", "objectSid",
                        "distinguishedName", "cn", "sAMAccountName", "name",
                        "userAccountControl", "whenCreated", "whenChanged",
                        "pwdLastSet", "lastLogonTimestamp",
                        "operatingSystem", "operatingSystemVersion",
                        "dNSHostName",
                    ],
                )
            finally:
                try:
                    l.unbind_s()
                except Exception:
                    pass
        finally:
            # Restore the prior env so other parts of the process
            # (Ansible runs, Graph probes) don't inherit our kerberos
            # config.
            os.environ.clear()
            os.environ.update(env)

        out: list[dict] = []
        for dn, attrs in raw:
            if dn is None:
                continue  # LDAP referral (we disabled referral chasing)
            flat: dict = {"distinguishedName": dn}
            for k, vlist in attrs.items():
                # python-ldap hands back lists of bytes; coerce to a
                # single JSON-safe value. Two binary attrs need special
                # handling to match how they appear in other systems:
                #   * objectSid is a raw NT SID blob — convert to the
                #     canonical "S-1-5-21-..." string so it compares
                #     cleanly with Entra.onPremisesSecurityIdentifier.
                #   * objectGUID is 16 raw bytes in Windows-GUID byte
                #     order — convert to the hyphenated UUID form that
                #     Graph returns for deviceId.
                # Everything else: utf-8 if possible, hex otherwise.
                v = vlist[0] if vlist else None
                if isinstance(v, bytes):
                    if k == "objectSid":
                        flat[k] = _sid_bytes_to_str(v)
                    elif k == "objectGUID":
                        flat[k] = _guid_bytes_to_str(v)
                    else:
                        try:
                            flat[k] = v.decode("utf-8")
                        except UnicodeDecodeError:
                            flat[k] = v.hex()
                else:
                    flat[k] = v
            out.append(flat)
        return out

    # --- Graph ---
    tenant = cfg.get("vault_entra_tenant_id", "")
    client = cfg.get("vault_entra_app_id", "")
    secret = cfg.get("vault_entra_app_secret", "")

    def _graph_find_entra_device(win_name: str) -> list[dict]:
        q = device_monitor._quote_graph_string(win_name)
        url = (
            "https://graph.microsoft.com/v1.0/devices"
            f"?$filter=displayName eq '{q}'"
            "&$select=id,displayName,deviceId,trustType,accountEnabled,"
            "approximateLastSignInDateTime,operatingSystem,"
            "operatingSystemVersion,registrationDateTime,deviceOwnership,"
            "onPremisesSyncEnabled,onPremisesLastSyncDateTime,"
            "onPremisesSecurityIdentifier,physicalIds,alternativeSecurityIds"
        )
        body = device_monitor._graph_get(
            url, tenant_id=tenant, client_id=client, client_secret=secret,
        )
        return list(body.get("value", []) or [])

    def _graph_find_intune_device(serial: str) -> list[dict]:
        q = device_monitor._quote_graph_string(serial)
        url = (
            "https://graph.microsoft.com/v1.0/deviceManagement/managedDevices"
            f"?$filter=serialNumber eq '{q}'"
            "&$select=id,deviceName,serialNumber,azureADDeviceId,"
            "enrolledDateTime,complianceState,lastSyncDateTime,"
            "operatingSystem,managedDeviceOwnerType"
        )
        body = device_monitor._graph_get(
            url, tenant_id=tenant, client_id=client, client_secret=secret,
        )
        return list(body.get("value", []) or [])

    return device_monitor.MonitorContext(
        db_path=DEVICE_MONITOR_DB,
        list_pve_vms=_list_pve_vms,
        fetch_pve_config=_fetch_pve_config,
        fetch_guest_details=_fetch_guest_details,
        ad_search=_ad_search,
        graph_find_entra_device=_graph_find_entra_device,
        graph_find_intune_device=_graph_find_intune_device,
    )


def _resolve_ad_credential() -> dict:
    """Look up the configured AD credential. Returns
    ``{username, password}`` or ``{}`` if unconfigured."""
    s = device_history_db.get_settings(DEVICE_MONITOR_DB)
    cred_id = s.ad_credential_id
    if not cred_id:
        return {}
    try:
        # sequences_db.get_credential signature is (db, cipher, id) —
        # earlier revision had (db, id, cipher), which raised a silent
        # sqlite3 bind error and dropped all AD probes.
        cred = sequences_db.get_credential(
            SEQUENCES_DB, _cipher(), int(cred_id),
        )
    except Exception:
        return {}
    if not cred:
        return {}
    payload = cred.get("payload", {}) or {}
    return {
        "username": payload.get("username", ""),
        "password": payload.get("password", ""),
    }


_CIPHER: Optional[_crypto.Cipher] = None


def _cipher() -> _crypto.Cipher:
    """Return the process-wide Cipher, constructing it lazily.

    The key file is read on first access. Tests that monkeypatch
    CREDENTIAL_KEY must also reset the cache: ``web.app._CIPHER = None``.
    """
    global _CIPHER
    if _CIPHER is None:
        _CIPHER = _crypto.Cipher(CREDENTIAL_KEY)
    return _CIPHER


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


def _proxmox_api(path, method="GET", data=None, files=None):
    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")
    url = f"https://{host}:{port}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
    resp = requests.request(
        method, url, headers=headers, data=data, files=files,
        verify=False,
        timeout=30 if files else 10,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def _proxmox_api_post(path, data=None):
    """POST to Proxmox API (for VM power actions and guest-exec)."""
    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")
    url = f"https://{host}:{port}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
    resp = requests.post(url, headers=headers, data=data, verify=False, timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", {})


def _proxmox_api_put(path, data=None):
    """PUT to Proxmox API (for VM config changes)."""
    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")
    url = f"https://{host}:{port}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
    resp = requests.put(url, headers=headers, data=data, verify=False, timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", {})


def _proxmox_root_ticket_fetch(cfg: dict) -> tuple[str, str]:
    """Exchange root@pam username/password for a (ticket, CSRF) pair.

    Proxmox tickets are good for ~2 hours by default — enough for a
    single provision job. The password is read fresh from the loaded
    config each call, so rotating in vault.yml + restarting the
    container is sufficient; no caching here.
    """
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    username = (cfg.get("vault_proxmox_root_username") or "root@pam").strip()
    # Proxmox /access/ticket demands <user>@<realm>. A bare 'root' gets
    # a 401 that looks like a wrong-password error; tolerate it by
    # defaulting to the @pam realm.
    if "@" not in username:
        username = f"{username}@pam"
    password = cfg.get("vault_proxmox_root_password") or ""
    if not password:
        raise ValueError("vault_proxmox_root_password is empty")
    url = f"https://{host}:{port}/api2/json/access/ticket"
    resp = requests.post(
        url,
        data={"username": username, "password": password},
        verify=cfg.get("proxmox_validate_certs", False),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    ticket = data.get("ticket")
    csrf = data.get("CSRFPreventionToken")
    if not (ticket and csrf):
        raise RuntimeError(
            f"/access/ticket response missing ticket or CSRF token: {data!r}"
        )
    return ticket, csrf


def _proxmox_api_delete(path):
    """DELETE to Proxmox API (for VM removal)."""
    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")
    url = f"https://{host}:{port}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
    resp = requests.delete(url, headers=headers, verify=False, timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", {})


def _decode_smbios_serial(smbios1):
    """Extract and decode the serial from a Proxmox smbios1 config string."""
    import base64 as b64mod
    if not smbios1:
        return ""
    is_base64 = "base64=1" in smbios1
    for part in smbios1.split(","):
        if part.startswith("serial="):
            val = part[7:]
            if is_base64:
                try:
                    return b64mod.b64decode(val).decode("utf-8")
                except Exception:
                    return val
            return val
    return ""


def _decode_smbios_field(smbios1, field):
    """Extract and decode any field from a Proxmox smbios1 config string."""
    import base64 as b64mod
    if not smbios1:
        return ""
    is_base64 = "base64=1" in smbios1
    for part in smbios1.split(","):
        if part.startswith(f"{field}="):
            val = part[len(field) + 1:]
            if is_base64 and field != "uuid":
                try:
                    return b64mod.b64decode(val).decode("utf-8")
                except Exception:
                    return val
            return val
    return ""


_GUEST_WINDOWS_DETAILS_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem
$bios = Get-CimInstance Win32_BIOS
$csp = Get-CimInstance Win32_ComputerSystemProduct
$ip = ((Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue) |
       Where-Object { $_.PrefixOrigin -ne 'WellKnown' -and $_.InterfaceAlias -notmatch 'Loopback' } |
       Select-Object -First 1).IPAddress
# dsregcmd output tells us whether the device is Azure AD / Entra joined.
$dsreg = dsregcmd /status 2>$null | Out-String
$aadJoined = $dsreg -match 'AzureAdJoined\s*:\s*YES'
$aadTenant = if ($dsreg -match 'TenantName\s*:\s*(\S.+)') { $Matches[1].Trim() } else { '' }
$domainRole = $cs.DomainRole  # 0=Standalone Workstation, 1=Member Workstation, ...
[PSCustomObject]@{
  Name = $cs.Name
  Domain = $cs.Domain
  PartOfDomain = [bool]$cs.PartOfDomain
  DomainRole = $domainRole
  AadJoined = [bool]$aadJoined
  AadTenant = $aadTenant
  OSCaption = $os.Caption
  OSBuild = $os.BuildNumber
  OSVersion = $os.Version
  IPAddress = $ip
  LastBootUpTime = if ($os.LastBootUpTime) { $os.LastBootUpTime.ToString('yyyy-MM-ddTHH:mm:ssK') } else { '' }
  # SMBIOS identifiers — Intune's managedDevices filter keys on
  # serialNumber, so without these the Intune probe always misses.
  Serial = $bios.SerialNumber
  UUID = $csp.UUID
  Manufacturer = $cs.Manufacturer
  Model = $cs.Model
} | ConvertTo-Json -Compress
""".strip()


def _guest_exec_ps(node: str, vmid: int, ps: str,
                   timeout_s: int = 20) -> Optional[str]:
    """Run a PowerShell one-liner via the Proxmox guest agent and return
    the captured stdout (decoded). Returns None on any failure (agent
    unresponsive, exec rejected, non-zero exit). Best-effort — callers
    must handle absence gracefully."""
    # POST exec with the script as a single argument to powershell.exe.
    try:
        exec_resp = _proxmox_api_post(
            f"/nodes/{node}/qemu/{vmid}/agent/exec",
            data={
                "command": [
                    "powershell.exe", "-NoProfile",
                    "-ExecutionPolicy", "Bypass", "-Command", ps,
                ],
            },
        )
    except Exception:
        return None
    pid = (exec_resp or {}).get("pid")
    if pid is None:
        return None
    # Poll exec-status.
    import time as _time
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        try:
            status = _proxmox_api(
                f"/nodes/{node}/qemu/{vmid}/agent/exec-status?pid={pid}"
            )
        except Exception:
            return None
        if status and status.get("exited"):
            if status.get("exitcode", 1) != 0:
                return None
            return (status.get("out-data") or "").strip()
        _time.sleep(0.3)
    return None


def _fetch_guest_windows_details(node: str, vmid: int) -> dict:
    """Return the parsed dict from _GUEST_WINDOWS_DETAILS_PS, or {}."""
    out = _guest_exec_ps(node, vmid, _GUEST_WINDOWS_DETAILS_PS, timeout_s=15)
    if not out:
        return {}
    # ConvertTo-Json may emit a lone object (no list wrapper for a single
    # PSCustomObject). Strip BOM if present.
    out = out.lstrip("\ufeff").strip()
    try:
        data = json.loads(out)
    except Exception:
        return {}
    # Normalize: always return a dict even if PS sent a list of 1.
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def get_autopilot_vms():
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        vms = _proxmox_api(f"/nodes/{node}/qemu")
    except Exception:
        return []

    # Filter to VMs tagged "autopilot", then batch-fetch configs
    autopilot_vms = [
        vm for vm in vms
        if not vm.get("template") and "autopilot" in (vm.get("tags", "") or "").split(";")
    ]

    # Fetch all configs in parallel using threads
    from concurrent.futures import ThreadPoolExecutor
    configs = {}
    def fetch_config(vmid):
        try:
            return vmid, _proxmox_api(f"/nodes/{node}/qemu/{vmid}/config")
        except Exception:
            return vmid, {}

    with ThreadPoolExecutor(max_workers=10) as pool:
        for vmid, config in pool.map(lambda vm: fetch_config(vm["vmid"]), autopilot_vms):
            configs[vmid] = config

    # Fetch guest agent hostnames for running VMs
    running_vmids = [vm["vmid"] for vm in autopilot_vms if vm.get("status") == "running"]
    hostnames = {}
    def fetch_hostname(vmid):
        try:
            data = _proxmox_api(f"/nodes/{node}/qemu/{vmid}/agent/get-host-name")
            # API returns {"result": {"host-name": "..."}} or {"host-name": "..."}
            if isinstance(data, dict):
                return vmid, data.get("result", data).get("host-name", "")
            return vmid, ""
        except Exception:
            return vmid, ""

    if running_vmids:
        with ThreadPoolExecutor(max_workers=10) as pool:
            for vmid, hostname in pool.map(fetch_hostname, running_vmids):
                hostnames[vmid] = hostname

    # Enrich running VMs with Windows-side details via guest-exec +
    # PowerShell (domain membership, OS build, IP, Entra-join state).
    # Each call is a ~500ms round-trip; parallelize with the existing
    # ThreadPoolExecutor. Non-Windows VMs or VMs without guest agent
    # readiness return {} and render as blank — expected.
    guest_details: dict = {}
    if running_vmids:
        with ThreadPoolExecutor(max_workers=10) as pool:
            for vmid, details in pool.map(
                lambda v: (v, _fetch_guest_windows_details(node, v)),
                running_vmids,
            ):
                guest_details[vmid] = details

    # Fallback IP sources, in order:
    #   1. QEMU guest agent's network-get-interfaces — cheaper than
    #      PowerShell exec, answers whenever the base agent is up.
    #   2. AD DNS lookup against the DC — works even when the guest
    #      agent is entirely dead, as long as the VM is
    #      domain-joined and auto-registered its A record.
    ip_fallback: dict[int, str] = {}
    def fetch_ip(vmid):
        try:
            data = _proxmox_api(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
        except Exception:
            return vmid, ""
        # Response: {"result": [{"name":"Ethernet", "ip-addresses":[{"ip-address":"…", "ip-address-type":"ipv4"}]}]}
        ifaces = (data or {}).get("result", data) or []
        if isinstance(ifaces, dict):
            ifaces = ifaces.get("result", []) or []
        for iface in ifaces:
            # Skip loopback + link-local + unwanted interfaces.
            name = (iface.get("name") or "").lower()
            if name in ("lo", "loopback") or name.startswith(("lo ", "lo\t")):
                continue
            for addr in iface.get("ip-addresses", []) or []:
                if addr.get("ip-address-type") != "ipv4":
                    continue
                ip = addr.get("ip-address") or ""
                if ip and not (ip.startswith("127.") or ip.startswith("169.254.")):
                    return vmid, ip
        return vmid, ""
    if running_vmids:
        with ThreadPoolExecutor(max_workers=10) as pool:
            for vmid, ip in pool.map(fetch_ip, running_vmids):
                ip_fallback[vmid] = ip

    # DNS fallback for VMs whose agent is entirely dead. Domain-
    # joined Windows VMs auto-register an A record with the AD-
    # integrated DNS server, so a simple A lookup against the DC
    # gives us the live IP without any agent involvement.
    def fetch_ip_from_dns(vm_name: str) -> str:
        if not vm_name:
            return ""
        try:
            import dns.resolver
            r = dns.resolver.Resolver(configure=False)
            r.nameservers = [socket.gethostbyname(cfg.get("ldap_host", "dns.home.gell.one"))]
            r.timeout = 3
            r.lifetime = 3
            fqdn = f"{vm_name}.{cfg.get('ad_realm', 'HOME.GELL.ONE').lower()}"
            answer = r.resolve(fqdn, "A")
            return str(answer[0]) if answer else ""
        except Exception:
            return ""

    dns_fallback: dict[int, str] = {}
    needs_dns = [vm["vmid"] for vm in autopilot_vms
                 if vm.get("status") == "running"
                 and not (guest_details.get(vm["vmid"], {}) or {}).get("IPAddress")
                 and not ip_fallback.get(vm["vmid"])]
    if needs_dns:
        with ThreadPoolExecutor(max_workers=10) as pool:
            names = [(v, next((x.get("name") for x in autopilot_vms
                              if x["vmid"] == v), "")) for v in needs_dns]
            for vmid, ip in pool.map(
                lambda pair: (pair[0], fetch_ip_from_dns(pair[1])),
                names,
            ):
                dns_fallback[vmid] = ip

    result = []
    for vm in autopilot_vms:
        config = configs.get(vm["vmid"], {})
        smbios1 = config.get("smbios1", "")
        args = config.get("args", "") or ""
        vm_name = vm.get("name", "") or ""
        guest = guest_details.get(vm["vmid"], {}) or {}

        # Serial — prefer the authoritative Windows-side Win32_BIOS.
        # SerialNumber; fall back to vm_name when the VM is autopilot-
        # provisioned (our convention: vm_name == real SMBIOS serial).
        # smbios1.serial is LAST because per-VM SMBIOS files
        # (`args: -smbios file=…`) leave smbios1 with the template
        # default (e.g., Gell-1F02ADE6) that matches nothing.
        smbios1_serial = _decode_smbios_serial(smbios1) if smbios1 else ""
        per_vm_smbios_active = ("smbios file=" in args)
        if guest.get("Serial"):
            serial = guest["Serial"]
        elif per_vm_smbios_active and vm_name.lower().startswith("gell-"):
            serial = vm_name
        else:
            serial = smbios1_serial

        # OEM / Hardware — guest-exec's WMI values first (real runtime
        # identity), then smbios1 as last resort. When per-VM SMBIOS
        # is active AND guest is unreachable, smbios1 shows the
        # template defaults; tag the display so operators know.
        if guest.get("Manufacturer") or guest.get("Model"):
            oem = f"{guest.get('Manufacturer','')} {guest.get('Model','')}".strip()
        else:
            manufacturer = _decode_smbios_field(smbios1, "manufacturer") if smbios1 else ""
            product = _decode_smbios_field(smbios1, "product") if smbios1 else ""
            oem = f"{manufacturer} {product}".strip()
            if per_vm_smbios_active and oem:
                oem = f"{oem} (template default)"
        result.append({
            "vmid": vm["vmid"],
            "name": vm_name,
            "status": vm.get("status", "unknown"),
            "serial": serial,
            "oem": oem,
            # Hostname: guest-agent get-host-name → guest-exec Name →
            # fall back to vm_name so the column isn't an empty dash.
            "hostname": (hostnames.get(vm["vmid"], "")
                         or guest.get("Name", "")
                         or (vm_name if vm.get("status") == "running" else "")),
            "mem_mb": int(vm.get("maxmem", 0) / 1024 / 1024),
            "cpus": vm.get("cpus", vm.get("maxcpu", "")),
            "tags": vm.get("tags", "") or "",
            # has_guest_data gates the domain/workgroup badge in the
            # template so a vm_name-fallback hostname doesn't produce
            # a misleading "workgroup" label for VMs we simply
            # couldn't reach over guest-exec.
            "has_guest_data": bool(guest),
            # Windows-side enrichment. Keys are present but empty when
            # the guest agent didn't respond in time or the VM isn't
            # running Windows (PowerShell exec returns None then).
            "domain": guest.get("Domain", ""),
            "part_of_domain": bool(guest.get("PartOfDomain", False)),
            "aad_joined": bool(guest.get("AadJoined", False)),
            "aad_tenant": guest.get("AadTenant", "") or "",
            "os_caption": guest.get("OSCaption", "") or "",
            "os_build": str(guest.get("OSBuild", "") or ""),
            "os_version": guest.get("OSVersion", "") or "",
            "ip_address": (guest.get("IPAddress", "")
                           or ip_fallback.get(vm["vmid"], "")
                           or dns_fallback.get(vm["vmid"], "")),
            "last_boot": guest.get("LastBootUpTime", "") or "",
        })
    return sorted(result, key=lambda v: v["vmid"])


def _graph_token():
    cfg = _load_proxmox_config()
    tenant = cfg.get("vault_entra_tenant_id", "")
    app_id = cfg.get("vault_entra_app_id", "")
    secret = cfg.get("vault_entra_app_secret", "")
    if not all([tenant, app_id, secret]):
        return None
    resp = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": app_id,
            "client_secret": secret,
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _graph_api(path, method="GET", json_body=None):
    token = _graph_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/beta{path}"
    if method == "GET":
        resp = requests.get(url, headers=headers, timeout=15)
    elif method == "DELETE":
        resp = requests.delete(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return {"status": resp.status_code}
    elif method == "POST":
        resp = requests.post(url, headers=headers, json=json_body, timeout=15,
        )
    resp.raise_for_status()
    return resp.json()


def _graph_api_all(path):
    """GET and follow @odata.nextLink pagination. Returns merged `value` list."""
    token = _graph_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/beta{path}"
    items = []
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        items.extend(payload.get("value", []))
        url = payload.get("@odata.nextLink")
    return items


def get_autopilot_devices():
    try:
        data = _graph_api(
            "/deviceManagement/windowsAutopilotDeviceIdentities"
        )
    except Exception as e:
        return [], str(e)
    if not data:
        return [], "No Entra credentials configured"
    _profile_labels = {
        "assigned": "Assigned",
        "assignedUnkownSyncState": "Assigned",
        "notAssigned": "Not Assigned",
        "assignedInSync": "Assigned (Synced)",
        "assignedOutOfSync": "Assigned (Out of Sync)",
        "pending": "Pending",
        "unknown": "Unknown",
    }
    devices = []
    for d in data.get("value", []):
        profile_status_raw = d.get("deploymentProfileAssignmentStatus", "unknown")
        devices.append({
            "id": d.get("id", ""),
            "serial": d.get("serialNumber", ""),
            "group_tag": d.get("groupTag", ""),
            "profile_status": _profile_labels.get(profile_status_raw, profile_status_raw),
            "profile_ok": profile_status_raw in ("assigned", "assignedUnkownSyncState", "assignedInSync"),
            "enrollment_state": d.get("enrollmentState", "unknown"),
            "last_contact": (d.get("lastContactedDateTime") or "")[:19],
            "manufacturer": d.get("manufacturer", ""),
            "model": d.get("model", ""),
            "display_name": d.get("displayName", ""),
        })
    return devices, None


def load_oem_profiles():
    profiles_path = FILES_DIR / "oem_profiles.yml"
    if not profiles_path.exists():
        return {}
    with open(profiles_path) as f:
        data = yaml.safe_load(f)
    return (data or {}).get("oem_profiles", {})


def _serial_to_oem(serial):
    prefix = serial.split("-")[0] if "-" in serial else ""
    return {"PF": "Lenovo", "SVC": "Dell", "CZC": "HP", "MSF": "Microsoft", "LAB": "Generic"}.get(prefix, "")


def get_hash_files():
    if not HASH_DIR.exists():
        return []
    files = []
    for f in sorted(HASH_DIR.glob("*.csv")):
        stat = f.stat()
        serial = ""
        try:
            lines = f.read_text().strip().splitlines()
            if len(lines) >= 2:
                serial = lines[1].split(",")[0]
        except Exception:
            pass
        vm_name = f.stem.replace("_hwid", "")
        files.append({
            "name": f.name,
            "vm_name": vm_name,
            "serial": serial,
            "oem": _serial_to_oem(serial),
            "size": f"{stat.st_size:,} bytes",
            # Raw numeric + ISO used as data-sort-value attributes on
            # the /hashes page so client-side column sort works on
            # the real values, not the display strings.
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M"),
            "modified_epoch": int(stat.st_mtime),
        })
    return files


def compute_duration(job):
    if not job.get("started"):
        return None
    start = datetime.fromisoformat(job["started"])
    if job.get("ended"):
        end = datetime.fromisoformat(job["ended"])
    elif job["status"] == "running":
        end = datetime.now(timezone.utc)
    else:
        return None
    delta = end - start
    minutes, seconds = divmod(int(delta.total_seconds()), 60)
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


# --- HTML Pages ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # Every data-bearing module on the dashboard fetches via JSON
    # endpoints so the page can refresh live. We only pass the
    # initial running/queued counts so the first paint shows real
    # numbers (no flash of "…").
    jobs = job_manager.list_jobs()
    running = [j for j in jobs if j.get("status") == "running"]
    current_vars = _load_vars()
    return templates.TemplateResponse("home.html", {
        "request": request,
        "initial_running_count": len(running),
        "initial_queued_count": 0,
        "hypervisor_type": current_vars.get("hypervisor_type", "proxmox"),
    })


@app.get("/provision", response_class=HTMLResponse)
async def provision_page(request: Request):
    cfg = _load_vars()
    # Best-effort: look up template disk size so the UI can show the minimum.
    template_disk = None
    try:
        tpl_vmid = cfg.get("proxmox_template_vmid")
        node = cfg.get("proxmox_node", "pve")
        if tpl_vmid:
            tpl_cfg = _proxmox_api(f"/nodes/{node}/qemu/{tpl_vmid}/config")
            scsi0 = tpl_cfg.get("scsi0", "")
            import re as _re
            m = _re.search(r"size=(\d+)G", scsi0)
            if m:
                template_disk = int(m.group(1))
    except Exception:
        pass
    defaults = {
        "cores":        cfg.get("vm_cores", 2),
        "memory_mb":    cfg.get("vm_memory_mb", 4096),
        "disk_size_gb": cfg.get("vm_disk_size_gb", 64),
        "count":        cfg.get("vm_count", 1),
        "serial_prefix": cfg.get("vm_serial_prefix", ""),
        "group_tag":    cfg.get("vm_group_tag", ""),
        "oem_profile":  cfg.get("vm_oem_profile", ""),
        "template_vmid": cfg.get("proxmox_template_vmid", ""),
        "hostname_pattern": cfg.get("vm_hostname_pattern", "autopilot-{serial}"),
    }
    return templates.TemplateResponse("provision.html", {
        "request": request,
        "profiles": load_oem_profiles(),
        "defaults": defaults,
        "template_disk_gb": template_disk,
        "sequences": sequences_db.list_sequences(SEQUENCES_DB),
    })


@app.get("/template", response_class=HTMLResponse)
async def template_page(request: Request):
    all_seqs = sequences_db.list_sequences(SEQUENCES_DB)
    ubuntu_sequences = [s for s in all_seqs if s.get("target_os") == "ubuntu"]
    current_vars = _load_vars()
    return templates.TemplateResponse("template.html", {
        "request": request,
        "profiles": load_oem_profiles(),
        "ubuntu_sequences": ubuntu_sequences,
        "hypervisor_type": current_vars.get("hypervisor_type", "proxmox"),
        "utm_iso_dir": current_vars.get("utm_iso_dir", "~/UTM-ISOs"),
    })


@app.get("/hashes", response_class=HTMLResponse)
async def hashes_page(request: Request, uploaded: str = "", error: str = ""):
    hash_files = get_hash_files()
    devices, _ = get_autopilot_devices()
    ap_serials = {d["serial"] for d in devices}
    for f in hash_files:
        f["in_intune"] = f["serial"] in ap_serials
    return templates.TemplateResponse("hashes.html", {
        "request": request,
        "hash_files": hash_files,
        "uploaded": uploaded,
        "error": error,
    })


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    jobs = job_manager.list_jobs()
    for job in jobs:
        job["duration"] = compute_duration(job)
        # Only running jobs can be paused; reading the log for finished
        # jobs is pointless (and the most recent N-lines read is cheap
        # enough for the handful that are actually live). Slicing to
        # the last 4 KiB keeps the scan O(1) regardless of log size.
        if job["status"] == "running" and (job.get("args") or {}).get("pause_enabled"):
            tail = job_manager.get_log(job["id"])[-4096:]
            job["paused"] = _detect_template_pause(job, tail)
        else:
            job["paused"] = False
    return templates.TemplateResponse("jobs.html", {
        "request": request,
        "jobs": jobs,
    })


def _format_memory(mb) -> str:
    try:
        mb = int(mb)
    except (TypeError, ValueError):
        return str(mb)
    if mb >= 1024 and mb % 1024 == 0:
        return f"{mb // 1024} GB"
    return f"{mb} MB"


def _oem_profile_label(key: str) -> str:
    """Human label for an OEM profile key, falling back to the key itself."""
    if not key:
        return ""
    try:
        profiles = load_oem_profiles()
    except Exception:
        return key
    p = profiles.get(key)
    if not p:
        return key
    parts = [p.get("manufacturer"), p.get("product")]
    pretty = " ".join(x for x in parts if x)
    return f"{pretty} ({key})" if pretty else key


def _describe_sequence_step(step: dict, creds_by_id: dict) -> str:
    """One-line, human description of a compiled step. No secrets."""
    t = step.get("step_type")
    p = step.get("params") or {}
    if t == "set_oem_hardware":
        prof = p.get("oem_profile") or "(inherit from form / vars.yml)"
        ct = p.get("chassis_type") or 0
        ct_s = f", chassis_type={ct}" if int(ct or 0) > 0 else ""
        return f"Set OEM hardware → {_oem_profile_label(prof) if prof and '(' not in prof else prof}{ct_s}"
    if t == "local_admin":
        cid = p.get("credential_id")
        cred = creds_by_id.get(int(cid)) if cid else None
        name = cred["name"] if cred else f"credential id={cid}"
        return f"Create local admin account (credential: {name})"
    if t == "autopilot_entra":
        return "Inject Entra-only AutopilotConfigurationFile.json (Autopilot Entra join)"
    if t == "autopilot_hybrid":
        return "Autopilot Hybrid (stub — not executable in v1)"
    if t == "join_ad_domain":
        cid = p.get("credential_id")
        cred = creds_by_id.get(int(cid)) if cid else None
        domain = cred["payload"].get("domain_fqdn") if cred else "(credential not resolved)"
        ou = p.get("ou_path") or (cred["payload"].get("ou_hint") if cred else "")
        name = cred["name"] if cred else f"credential id={cid}"
        ou_s = f" into {ou}" if ou else ""
        return f"Join AD domain {domain}{ou_s} (credential: {name})"
    if t == "rename_computer":
        src = (p.get("name_source") or "serial").lower()
        if src == "pattern":
            return f"Rename computer using pattern: {p.get('pattern','')} (triggers reboot)"
        return "Rename computer to its SMBIOS serial number (triggers reboot)"
    if t == "run_script":
        return f"Run PowerShell: {p.get('script', '')[:60]}..."
    return f"{t} ({p})"


def _build_job_plan(job: dict) -> Optional[dict]:
    """Produce a human-readable 'plan' for the job:
      {title, summary, steps: [str], end_goal: str, metadata: [(label, value)]}
    Returns None if we don't have a decoder for this playbook — the detail
    page falls back to the raw args dict in that case.
    """
    pb = job.get("playbook") or ""
    args = job.get("args") or {}

    if pb == "provision_clone":
        count = int(args.get("count") or 1)
        cores = args.get("cores") or 0
        mem = args.get("memory_mb") or 0
        disk = args.get("disk_size_gb") or 0
        prof_key = args.get("profile") or ""
        sid = args.get("sequence_id")
        serial_prefix = args.get("serial_prefix") or ""
        group_tag = args.get("group_tag") or ""
        chassis = args.get("chassis_type_override") or 0

        meta: list = []
        if prof_key:
            meta.append(("OEM profile", _oem_profile_label(prof_key)))
        if cores:   meta.append(("CPU cores", str(cores)))
        if mem:     meta.append(("Memory", _format_memory(mem)))
        if disk:    meta.append(("Disk", f"{disk} GB"))
        if serial_prefix: meta.append(("Serial prefix", serial_prefix))
        if group_tag:     meta.append(("Group tag", group_tag))
        if chassis:       meta.append(("Chassis-type override", str(chassis)))

        steps: list = []
        seq_name = ""
        seq_desc = ""
        produces_hash = False
        if sid:
            try:
                seq = sequences_db.get_sequence(SEQUENCES_DB, int(sid))
            except Exception:
                seq = None
            if seq:
                seq_name = seq["name"]
                seq_desc = seq.get("description", "")
                produces_hash = bool(seq.get("produces_autopilot_hash"))
                # Resolve credentials once for human-readable step descs.
                try:
                    creds = sequences_db.list_credentials(SEQUENCES_DB)
                    creds_by_id: dict = {}
                    for c in creds:
                        full = sequences_db.get_credential(
                            SEQUENCES_DB, _cipher(), c["id"],
                        )
                        if full:
                            creds_by_id[c["id"]] = full
                except Exception:
                    creds_by_id = {}
                for st in seq.get("steps", []):
                    if not st.get("enabled", True):
                        continue
                    steps.append(_describe_sequence_step(st, creds_by_id))
            meta.insert(0, ("Task sequence",
                            f"{seq_name}" + (
                                " — produces Autopilot hash" if produces_hash
                                else "")))
        else:
            meta.insert(0, ("Task sequence",
                            "(none — legacy provision path)"))

        # Assemble end goal from step signals.
        goals = [f"{count} VM(s) cloned from template"]
        step_types = set()
        if sid and seq:
            step_types = {s["step_type"] for s in seq.get("steps", [])
                          if s.get("enabled", True)}
        if "join_ad_domain" in step_types:
            goals.append("domain-joined during specialize")
        elif "autopilot_entra" in step_types:
            goals.append("Entra-joined via Autopilot")
        if "rename_computer" in step_types:
            goals.append("renamed to SMBIOS serial and rebooted")
        if produces_hash:
            goals.append("Autopilot hash captured and saved")
        end_goal = ", ".join(goals) + "."

        title = f"Provision {count} VM(s)" + (f" with sequence '{seq_name}'" if seq_name else "")
        return {
            "title": title,
            "summary": seq_desc,
            "steps": steps,
            "end_goal": end_goal,
            "metadata": meta,
        }

    if pb == "build_template":
        return {
            "title": "Build a Windows template",
            "summary": ("Create a fresh VM, install Windows unattended, "
                        "install the QEMU guest agent, sysprep "
                        "/generalize /oobe, delete Panther's answer-file "
                        "cache, convert the VM to a Proxmox template."),
            "steps": [
                "Create VM from Windows ISO + answer ISO + VirtIO drivers",
                "Boot into WinPE → partition disk → install Windows 11 Enterprise",
                "FirstLogonCommands install QEMU guest agent + enable RDP",
                "Run sysprep /generalize /oobe /quit",
                "Delete C:\\Windows\\Panther\\unattend.xml so clones consult the CD",
                "Shut down and convert VM to Proxmox template",
            ],
            "end_goal": (
                "A Proxmox template ready for clone provisioning — "
                "every clone will boot fresh OOBE and consume its own "
                "per-VM autounattend.xml from sata0."
            ),
            "metadata": [
                ("OEM profile", _oem_profile_label(args.get("profile", ""))),
            ],
        }

    if pb == "hash_capture":
        vmids = args.get("vmids") or ([args["vmid"]] if args.get("vmid") else [])
        return {
            "title": f"Capture Autopilot hash for {len(vmids)} VM(s)",
            "summary": ("Runs Get-WindowsAutopilotInfo via guest-exec to "
                        "produce the Autopilot hardware hash CSV."),
            "steps": [
                "Push the hash-capture PowerShell script to the VM",
                "Execute Get-WindowsAutopilotInfo → capture CSV on guest",
                "Retrieve CSV back to the controller",
                "Save to /app/output/hashes/<serial>_hwid.csv",
            ],
            "end_goal": "One CSV per VM, ready for Intune bulk upload.",
            "metadata": [("Target VMIDs", ", ".join(str(v) for v in vmids))],
        }

    if pb == "upload_after_capture":
        return {
            "title": "Capture + upload Autopilot hashes",
            "summary": ("Captures the hash then hands it straight to "
                        "Intune via Microsoft Graph."),
            "steps": [
                "Run hash_capture against the target VM",
                "Authenticate to Microsoft Graph using Entra app creds",
                "POST the hash to /deviceManagement/importedWindowsAutopilotDeviceIdentities",
                "Wait for Intune import to reach 'complete' state",
            ],
            "end_goal": "VM registered in Intune Autopilot, ready for the Autopilot-deployed identity.",
            "metadata": [],
        }

    return None


def _detect_template_pause(job: dict, log_content: str) -> bool:
    """Return True when a pause-enabled template build has actually reached
    the wait_for gate (not merely been submitted with the checkbox ticked).

    Source of truth: the Ansible task title we emit in
    roles/proxmox_template_builder/tasks/main.yml appears in the log as
    ``TASK [PAUSE — install software in VMID ...]`` the moment wait_for
    starts. We anchor on the ``TASK [`` prefix of the header line
    instead of the raw phrase, so operator debug output (e.g. an
    ``ansible.builtin.debug msg=...`` that happens to mention the pause
    text, or a later failure message that echoes task context) cannot
    false-trigger the PAUSED badge. We also check that the cleanup task
    (``Remove resume signal file (cleanup after pause)``) has NOT yet
    started — that task only fires after the operator clicks Resume,
    which is our signal that the pause is over.
    """
    if not (job.get("args") or {}).get("pause_enabled"):
        return False
    # Anchor on the Ansible TASK header line (start-of-line, bracketed)
    # so operator debug output that mentions the phrase doesn't
    # false-trigger.
    pause_marker = "TASK [PAUSE — install software in VMID"
    cleanup_marker = "TASK [Remove resume signal file (cleanup after pause)]"
    if pause_marker not in log_content:
        return False
    if cleanup_marker in log_content:
        return False
    return True


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail_page(request: Request, job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        return HTMLResponse("<h1>Job not found</h1>", status_code=404)
    plan = _build_job_plan(job)
    log_content = job_manager.get_log(job_id)
    job["paused"] = _detect_template_pause(job, log_content)
    return templates.TemplateResponse("job_detail.html", {
        "request": request,
        "job": job,
        "plan": plan,
        "log_content": log_content,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str = ""):
    current_vars = _load_vars()
    vault_present = _vault_presence()
    options = _fetch_settings_options()
    hv_type = (current_vars.get("hypervisor_type") or "proxmox").lower()
    sections = []
    for group in SETTINGS_SCHEMA:
        group_applies = group.get("applies_to", "all")
        if group_applies not in ("all", hv_type):
            continue
        source = group.get("source", "vars")
        fields = []
        for f in group["fields"]:
            field_applies = f.get("applies_to", "all")
            if field_applies not in ("all", hv_type):
                continue
            key = f["key"]
            if source == "vault":
                # Secret-safe: never surface the actual value. The UI
                # only learns whether each key has something set.
                fields.append({
                    **f,
                    "source": "vault",
                    "value": "",
                    "is_set": vault_present.get(key, False),
                    "readonly": False,
                    "options": [],
                    "labels": {},
                })
                continue
            val = current_vars.get(key, "")
            is_template = isinstance(val, str) and "{{" in str(val)
            field_options = options.get(key, [])
            labels = options.get(f"{key}_labels", {})
            fields.append({
                **f,
                "source": "vars",
                "value": val,
                "is_set": bool(val),
                "readonly": is_template,
                "options": field_options,
                "labels": labels,
            })
        if not fields:
            # every field in this section was filtered out for the
            # current hypervisor — skip the empty header entirely.
            continue
        sections.append({"section": group["section"], "fields": fields,
                         "source": source})
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "sections": sections,
        "saved": saved == "1",
        "hypervisor_type": hv_type,
    })


@app.get("/api/settings/node-options/{node}")
async def node_options(node: str):
    """Return available storage, bridges, ISOs, templates for a given node."""
    result = {}
    try:
        # Storage
        storages = _proxmox_api("/storage")
        result["disk_storage"] = sorted([s["storage"] for s in storages if "images" in s.get("content", "")])
        result["iso_storage"] = sorted([s["storage"] for s in storages if "iso" in s.get("content", "")])

        # Bridges
        try:
            networks = _proxmox_api(f"/nodes/{node}/network")
            result["bridges"] = sorted([n["iface"] for n in networks if n.get("type") in ("bridge", "OVSBridge")])
        except Exception:
            result["bridges"] = []

        # ISOs — scan all iso storages
        iso_list = []
        for iso_store in result["iso_storage"]:
            try:
                content = _proxmox_api(f"/nodes/{node}/storage/{iso_store}/content")
                iso_list.extend([i["volid"] for i in content if i.get("format") == "iso"])
            except Exception:
                pass
        result["isos"] = sorted(set(iso_list))

        # Templates
        try:
            vms = _proxmox_api(f"/nodes/{node}/qemu")
            result["templates"] = [
                {"vmid": v["vmid"], "name": v.get("name", "")}
                for v in sorted(vms, key=lambda x: x["vmid"])
                if v.get("template")
            ]
        except Exception:
            result["templates"] = []

    except Exception as e:
        result["error"] = str(e)

    return result


@app.post("/api/settings")
async def save_settings(request: Request):
    form = await request.form()
    current_vars = _load_vars()
    # Honor the submitted hypervisor_type when filtering (so flipping
    # backends + saving in one submit applies correctly), falling back
    # to the on-disk value.
    hv_type = (form.get("hypervisor_type")
               or current_vars.get("hypervisor_type")
               or "proxmox").lower()
    vars_updates: dict = {}
    vault_updates: dict = {}
    for group in SETTINGS_SCHEMA:
        group_applies = group.get("applies_to", "all")
        if group_applies not in ("all", hv_type):
            continue
        source = group.get("source", "vars")
        for f in group["fields"]:
            field_applies = f.get("applies_to", "all")
            if field_applies not in ("all", hv_type):
                continue
            key = f["key"]
            ftype = f["type"]

            if source == "vault":
                # Secret-safe update semantics: blank input keeps the
                # current value. Only non-empty submissions change the
                # file. This matches the credential edit flow operators
                # are already used to.
                raw = form.get(key, "")
                if isinstance(raw, str) and raw.strip() != "":
                    vault_updates[key] = raw
                continue

            # Skip Jinja2 template values — they're computed in vars.yml.
            cur_val = current_vars.get(key, "")
            if isinstance(cur_val, str) and "{{" in str(cur_val):
                continue
            if ftype == "bool":
                # Checkboxes: absent means unchecked only if the form
                # was an actual settings submit. If the field isn't in
                # the form at all (e.g. a partial AJAX save from the
                # hypervisor switcher), leave it untouched.
                if key in form or "_all_fields" in form:
                    vars_updates[key] = key in form
            elif ftype == "number":
                if key not in form:
                    continue
                raw = form.get(key, "")
                if raw == "" or raw == "null":
                    vars_updates[key] = None
                else:
                    try:
                        vars_updates[key] = int(raw)
                    except ValueError:
                        vars_updates[key] = raw
            else:
                if key not in form:
                    continue
                val = form.get(key, "")
                vars_updates[key] = val if val not in ("null", "") else None

    if vars_updates:
        _save_vars(vars_updates)
    if vault_updates:
        _save_vault(vault_updates)
    return RedirectResponse("/settings?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# /vms page — cached result + background refresh
# ---------------------------------------------------------------------------
# get_autopilot_vms() fires PVE config + PS guest-exec + agent calls
# for every autopilot-tagged VM. When guest agents are dead each
# exec hits its 15s timeout before falling through, stacking up to
# ~30s of hang on page load. Cache the result for 30s and refresh
# in the background so /vms renders instantly off the cached copy.

_VMS_CACHE: dict = {
    "data": None,                 # list[dict] once warmed
    "devices": None,              # (devices, ap_error) tuple
    "hash_serials": None,         # set[str]
    "fetched_at": 0.0,            # monotonic() when last updated
    "refreshing": False,          # True while a background refresh is in flight
}
_VMS_CACHE_TTL_SECONDS = 30.0
_VMS_CACHE_STALE_SECONDS = 5 * 60  # if older than this, refetch synchronously


def _fetch_vms_payload():
    """Synchronous fetcher — calls every upstream the /vms page needs.
    Returns a dict ready to stash in _VMS_CACHE."""
    return {
        "data": get_autopilot_vms(),
        "devices": get_autopilot_devices(),
        "hash_serials": {f["serial"] for f in get_hash_files()},
    }


async def _refresh_vms_cache_bg() -> None:
    """Background refresher — never raises; leaves old data in place
    if anything goes wrong."""
    import asyncio
    if _VMS_CACHE["refreshing"]:
        return
    _VMS_CACHE["refreshing"] = True
    try:
        payload = await asyncio.to_thread(_fetch_vms_payload)
        _VMS_CACHE.update(payload)
        _VMS_CACHE["fetched_at"] = time.monotonic()
    except Exception:
        # Leave the previous cache intact; next sweep will retry.
        import logging as _logging
        _logging.getLogger("web.vms").exception(
            "/vms background refresh failed; cache left stale",
        )
    finally:
        _VMS_CACHE["refreshing"] = False


async def _get_vms_payload():
    """Return (payload, age_seconds). Serves from cache when warm,
    triggers a background refresh when stale, and blocks only on a
    true cold start or when the cache is older than STALE_SECONDS."""
    import asyncio
    now = time.monotonic()
    age = now - _VMS_CACHE["fetched_at"] if _VMS_CACHE["fetched_at"] else float("inf")

    # Cold start — nobody has ever fetched. Block to avoid serving
    # empty tables.
    if _VMS_CACHE["data"] is None:
        payload = await asyncio.to_thread(_fetch_vms_payload)
        _VMS_CACHE.update(payload)
        _VMS_CACHE["fetched_at"] = time.monotonic()
        return _VMS_CACHE, 0.0

    # Very stale — block rather than serve badly outdated data.
    if age >= _VMS_CACHE_STALE_SECONDS:
        await _refresh_vms_cache_bg()
        return _VMS_CACHE, 0.0

    # Past TTL but within staleness — kick a background refresh,
    # return what we have.
    if age >= _VMS_CACHE_TTL_SECONDS and not _VMS_CACHE["refreshing"]:
        asyncio.create_task(_refresh_vms_cache_bg())

    return _VMS_CACHE, age


@app.post("/api/vms/refresh")
async def api_vms_refresh():
    """Manual refresh trigger. Always blocks until the new payload
    is in the cache so the caller's subsequent GET /vms sees fresh
    data."""
    await _refresh_vms_cache_bg()
    return {"ok": True, "fetched_at": _VMS_CACHE["fetched_at"]}


@app.get("/vms", response_class=HTMLResponse)
async def vms_page(request: Request, error: str = ""):
    current_vars = _load_vars()
    if current_vars.get("hypervisor_type") == "utm":
        from web import utm_cli
        utm_vms: list[dict] = []
        utm_error = ""
        try:
            utm_vms = utm_cli.list_vms()
        except RuntimeError as exc:
            utm_error = str(exc)
        return templates.TemplateResponse("utm_vms.html", {
            "request": request,
            "vms": utm_vms,
            "error": utm_error,
            "utmctl_path": utm_cli.utmctl_path(),
            "library_path": str(utm_cli.utm_library_path()),
        })

    cache, cache_age = await _get_vms_payload()
    vms = list(cache["data"] or [])
    vm_serials = {vm["serial"] for vm in vms if vm.get("serial")}
    devices, ap_error = cache["devices"] or ([], "")
    hash_serials = cache["hash_serials"] or set()
    ap_serials = {d["serial"] for d in devices}

    # Only show Autopilot devices that match a Proxmox VM serial
    serial_to_vm = {vm["serial"]: vm for vm in vms if vm.get("serial")}
    matched_devices = [d for d in devices if d["serial"] in vm_serials]
    for d in matched_devices:
        d["has_local_hash"] = d["serial"] in hash_serials
        # Use guest agent hostname as fallback for empty Intune display name
        if not d["display_name"]:
            vm = serial_to_vm.get(d["serial"])
            if vm and vm.get("hostname"):
                d["display_name"] = vm["hostname"]

    # Tag VMs with their Autopilot status, plus the target_os + sequence
    # name of the sequence that provisioned them (if any). The UI uses
    # target_os to conditionally show the Check Enrollment action for
    # Ubuntu VMs and disable Capture Hash for them (no Autopilot hash on
    # Linux).
    for vm in vms:
        vm["in_autopilot"] = vm.get("serial", "") in ap_serials
        vm["has_hash"] = vm.get("serial", "") in hash_serials
        prov = sequences_db.get_vm_provisioning(SEQUENCES_DB, vmid=vm["vmid"])
        seq = None
        if prov and prov.get("sequence_id"):
            seq = sequences_db.get_sequence(SEQUENCES_DB, prov["sequence_id"])
        vm["target_os"] = (seq or {}).get("target_os") or "windows"
        vm["sequence_name"] = (seq or {}).get("name")

    # VMs not yet in Autopilot (missing)
    missing_vms = [vm for vm in vms if not vm["in_autopilot"] and vm.get("serial")]

    return templates.TemplateResponse("vms.html", {
        "request": request,
        "vms": vms,
        "devices": matched_devices,
        "missing_vms": missing_vms,
        "ap_error": ap_error,
        "error": error,
        # Surface to the footer so the operator can tell whether
        "cache_age_seconds": int(cache_age),
        "cache_fetched_at_iso": (
            datetime.fromtimestamp(
                time.time() - cache_age, tz=timezone.utc
            ).isoformat(timespec="seconds")
            if cache_age is not None else ""
        ),
        "cache_refreshing": _VMS_CACHE["refreshing"],
    })


# --- API Endpoints ---

def _keys_in_extra_args(tokens: list) -> set[str]:
    """Return the set of Ansible variable keys already carried as -e pairs.

    Accepts the list-form of an ansible-playbook argv where each -e flag
    is followed by a single ``key=value`` element.
    """
    keys: set[str] = set()
    for t in tokens:
        if isinstance(t, str) and "=" in t and not t.startswith("-"):
            keys.add(t.split("=", 1)[0])
    return keys


# Only scrape VMIDs from the success-path debug line emitted by the
# proxmox_vm_clone role's final "Report cloned VM" task. The failure
# diagnostic line in the same role also mentions "VMID: N" but the role
# has already raised by then, so anchoring on the success pattern prevents
# writing vm_provisioning rows for clones that never completed.
_VMID_SUCCESS_RE = re.compile(
    r"Cloned VM\s+'[^']*'\s+\(VMID:\s+(\d+)\)\s+from template"
)


def _record_vms_for_sequence(job_dict: dict, sequence_id: int) -> None:
    """Callback body used by `_register_sequence_callbacks`.

    Runs in the job-runner thread. Only records VMIDs for successful jobs
    — a failed job whose log happens to mention a partially-allocated
    VMID must NOT be recorded as provisioned by this sequence.
    """
    if job_dict.get("status") not in ("complete", "success"):
        return
    log_path = Path(job_manager.jobs_dir) / f"{job_dict['id']}.log"
    if not log_path.exists():
        return
    text = log_path.read_text(errors="replace")
    for m in _VMID_SUCCESS_RE.finditer(text):
        try:
            sequences_db.record_vm_provisioning(
                SEQUENCES_DB, vmid=int(m.group(1)), sequence_id=sequence_id,
            )
        except Exception:
            # Can't raise from a worker-thread callback — would poison job
            # status. DAL constraint violations are effectively "no row
            # written" which is the outcome we want on error anyway.
            pass


def _register_sequence_callbacks(job_id: str, sequence_id: int) -> None:
    """Persist sequence_id on the job and register the vm_provisioning scraper."""
    job_manager.set_arg(job_id, "sequence_id", sequence_id)
    job_manager.add_on_complete(
        job_id, lambda job_dict, sid=sequence_id: _record_vms_for_sequence(job_dict, sid)
    )


@app.post("/api/jobs/provision")
async def start_provision(
    profile: str = Form(...),
    count: int = Form(1),
    serial_prefix: str = Form(""),
    group_tag: str = Form(""),
    cores: int = Form(0),
    memory_mb: int = Form(0),
    disk_size_gb: int = Form(0),
    sequence_id: int = Form(None),
    hostname_pattern: str = Form("autopilot-{serial}"),
    chassis_type_override: int = Form(0),
):
    profile = _sanitize_input(profile)
    if group_tag:
        group_tag = _sanitize_input(group_tag)
    if serial_prefix:
        serial_prefix = _sanitize_input(serial_prefix)
    # hostname_pattern contains literal { } tokens — don't _sanitize_input
    # (which strips special chars); just trim and fall back to the default.
    hostname_pattern = (hostname_pattern or "").strip() or "autopilot-{serial}"

    # Stage the chassis-type SMBIOS binary(ies) on the Proxmox host for
    # every possible source of the effective chassis type. The compiler
    # + precedence layer picks the final value; we optimistically upload
    # for whichever type any source could request so Ansible finds the
    # file when it emits args: -smbios file=... on the VM config.
    cfg = _load_proxmox_config()
    _node = cfg.get("proxmox_node", "pve")
    _snippets_storage = cfg.get("proxmox_snippets_storage", "local")
    _chassis_types_to_stage: set[int] = set()
    if chassis_type_override and int(chassis_type_override) > 0:
        _chassis_types_to_stage.add(int(chassis_type_override))
    if sequence_id:
        _seq = sequences_db.get_sequence(SEQUENCES_DB, int(sequence_id))
        if _seq is not None:
            for _step in _seq["steps"]:
                if _step["step_type"] == "set_oem_hardware" and _step.get("enabled"):
                    _ct = _step["params"].get("chassis_type")
                    if _ct and int(_ct) > 0:
                        _chassis_types_to_stage.add(int(_ct))
    _profiles = load_oem_profiles()
    _prof = _profiles.get(profile) if profile else None
    if _prof and _prof.get("chassis_type"):
        _chassis_types_to_stage.add(int(_prof["chassis_type"]))

    # Fail-fast if a chassis-type binary isn't staged on the Proxmox
    # host: otherwise Ansible will emit `args: -smbios file=<missing>`
    # and QEMU fails to start the VM with a confusing "cannot open".
    # Surface the real cause + seed instructions as a 400 here.
    from web import proxmox_snippets
    for _ct in _chassis_types_to_stage:
        try:
            proxmox_snippets.require_chassis_type_binary(
                node=_node, storage=_snippets_storage, chassis_type=_ct,
            )
        except proxmox_snippets.ChassisBinaryMissing as _e:
            raise HTTPException(status_code=400, detail=str(_e)) from _e
        except Exception as _e:
            raise HTTPException(
                status_code=502,
                detail=f"could not query chassis-type binary for type {_ct}: {_e}",
            ) from _e

    # Proxmox hardcodes 'args' as root-only (PVE::API2::Qemu
    # check_vm_modify_config_perm: literal eq match against 'root@pam').
    # API tokens — even root@pam!<name> with privsep=0 — never pass that
    # check because their authuser carries the token suffix. The only
    # way to PUT args is a /access/ticket response, which requires a
    # password. Preflight the password is set, then fetch the ticket.
    _proxmox_root_ticket: Optional[str] = None
    _proxmox_root_csrf_token: Optional[str] = None
    # The args PUT is needed for both chassis-type overrides (SMBIOS
    # file passthrough) and sequence-based provisions (per-VM answer
    # floppy attachment). Either path triggers the root@pam ticket
    # flow; both share the same vault_proxmox_root_password.
    if _chassis_types_to_stage or sequence_id:
        _root_pw = cfg.get("vault_proxmox_root_password", "")
        if not _root_pw:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This provision needs the QEMU 'args' field set "
                    "(chassis-type override and/or per-VM answer floppy), "
                    "which Proxmox restricts to root@pam password auth "
                    "(API tokens are rejected by PVE's literal eq check "
                    "against 'root@pam'). Set vault_proxmox_root_password "
                    "in vault.yml (Settings → Credentials) and restart the "
                    "container. The password drives a just-in-time "
                    "/access/ticket per provision; the ticket lives 2h "
                    "and is only used for the single PUT that writes "
                    "'args'. See docs/SETUP.md §5b for details."
                ),
            )
        try:
            _proxmox_root_ticket, _proxmox_root_csrf_token = \
                _proxmox_root_ticket_fetch(cfg)
        except Exception as _e:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Could not obtain a root@pam ticket from Proxmox: "
                    f"{_e}. Check vault_proxmox_root_username "
                    f"(default 'root@pam') and vault_proxmox_root_password."
                ),
            ) from _e

    # Build the common -e overrides shared between single and multi paths.
    # Zero means "don't override, let vars.yml defaults apply" — lets the
    # form omit a field without forcing it to a specific value.
    def _overrides(cmd_tokens: list[str] | None = None) -> list[str]:
        tokens = cmd_tokens if cmd_tokens is not None else []
        if cores > 0:        tokens += ["-e", f"vm_cores={cores}"]
        if memory_mb > 0:    tokens += ["-e", f"vm_memory_mb={memory_mb}"]
        if disk_size_gb > 0: tokens += ["-e", f"vm_disk_size_gb={disk_size_gb}"]
        if serial_prefix:    tokens += ["-e", f"vm_serial_prefix={serial_prefix}"]
        if group_tag:        tokens += ["-e", f"vm_group_tag={group_tag}"]
        if hostname_pattern: tokens += ["-e", f"hostname_pattern={hostname_pattern}"]
        if chassis_type_override and int(chassis_type_override) > 0:
            tokens += ["-e", f"chassis_type_override={int(chassis_type_override)}"]
        return tokens

    args = {
        "profile": profile, "count": count,
        "serial_prefix": serial_prefix, "group_tag": group_tag,
        "cores": cores, "memory_mb": memory_mb, "disk_size_gb": disk_size_gb,
        "hostname_pattern": hostname_pattern,
    }

    # Resolve sequence → Ansible vars (spec §12 precedence).
    resolved_vars: dict = {}
    _answer_floppy_path: Optional[str] = None
    _causes_reboot_count = 0
    if sequence_id:
        seq = sequences_db.get_sequence(SEQUENCES_DB, int(sequence_id))
        if seq is None:
            raise HTTPException(404, f"sequence {sequence_id} not found")

        # Resolver: lazy-decrypts credentials as compile visits steps
        # that reference them. Plaintext never lands on resolved_vars.
        def _resolve_cred(cid: int):
            rec = sequences_db.get_credential(SEQUENCES_DB, _cipher(), cid)
            return rec["payload"] if rec else None

        try:
            compiled = sequence_compiler.compile(
                seq, resolve_credential=_resolve_cred,
            )
        except sequence_compiler.CompilerError as e:
            raise HTTPException(400, f"sequence compile failed: {e}")
        form_overrides = {"vm_oem_profile": profile}
        resolved_vars = sequence_compiler.resolve_provision_vars(
            compiled,
            form_overrides=form_overrides,
            vars_yml=_load_vars(),
        )

        # Compile to a per-VM unattend and materialize it as a FAT12
        # floppy image on the Proxmox host. Windows Setup on a
        # sysprep-d clone lands on the floppy (position 4 of the
        # answer-file search) because position 5 (read-only CD) is
        # unreliable in online specialize. Two provisions whose
        # compiled unattend matches byte-for-byte share one floppy.
        from web import unattend_renderer, answer_floppy_cache
        _unattend_xml = unattend_renderer.render_unattend(compiled)
        _root_user = cfg.get("vault_proxmox_root_username") or "root@pam"
        _root_password = cfg.get("vault_proxmox_root_password") or ""
        _root_ssh_user = _root_user.split("@", 1)[0] or "root"
        if not _root_password:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Sequence-based provisioning needs vault_proxmox_root_password "
                    "set in vault.yml (Settings → Credentials). The web backend "
                    "SSHes to the Proxmox host as root to build the per-VM "
                    "answer floppy and to set the VM's args field."
                ),
            )
        _ssh_runner = answer_floppy_cache.make_sshpass_runner(
            host=cfg.get("proxmox_host", ""),
            password=_root_password,
            user=_root_ssh_user,
        )
        try:
            _answer_floppy_path = answer_floppy_cache.ensure_floppy(
                db_path=SEQUENCES_DB,
                unattend_bytes=_unattend_xml.encode("utf-8"),
                ssh=_ssh_runner,
            )
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"answer-floppy build failed: {e}",
            ) from e
        _causes_reboot_count = compiled.causes_reboot_count

    # Sequence-driven extras: per-VM answer floppy path + reboot-cycle
    # count + root ticket for the args PUT.
    _seq_extras: list[str] = []
    if _answer_floppy_path:
        _seq_extras += ["-e", f"_answer_floppy_path={_answer_floppy_path}"]
    # Always pass _causes_reboot_count, even when 0 — the playbook's
    # "Follow guest through N reboot(s)" task interpolates it in its
    # name string, and undefined triggers a Jinja error on EVERY run
    # (the when/loop have `| default(0)` but the name string does not,
    # and Ansible evaluates task names independently of when/loop).
    _seq_extras += ["-e", f"_causes_reboot_count={_causes_reboot_count}"]
    if _proxmox_root_ticket and _proxmox_root_csrf_token:
        _seq_extras += [
            "-e", f"_proxmox_root_ticket={_proxmox_root_ticket}",
            "-e", f"_proxmox_root_csrf_token={_proxmox_root_csrf_token}",
        ]

    if count <= 1:
        cmd = ["ansible-playbook", str(PLAYBOOK_DIR / "provision_clone.yml")]
        # Only emit the form profile if the operator actually set one; a
        # blank form lets the sequence (or vars.yml) supply vm_oem_profile.
        if profile:
            cmd += ["-e", f"vm_oem_profile={profile}"]
        cmd += ["-e", "vm_count=1"] + _overrides()
        if chassis_type_override and int(chassis_type_override) > 0:
            cmd += ["-e", f"chassis_type_override={int(chassis_type_override)}"]
        cmd += _seq_extras
        # Collect keys already present in cmd so sequence-compiled vars don't
        # stomp them (form/_overrides() win per spec §12).
        existing_keys = _keys_in_extra_args(cmd)
        for key, value in resolved_vars.items():
            if key not in existing_keys:
                cmd.extend(["-e", f"{key}={value}"])
        job = job_manager.start("provision_clone", cmd, args=args)
        if sequence_id:
            _register_sequence_callbacks(job["id"], int(sequence_id))
        return RedirectResponse(f"/jobs/{job['id']}", status_code=303)

    # Multiple VMs — run sequentially to avoid VMID race condition
    import tempfile
    playbook = shlex.quote(str(PLAYBOOK_DIR / "provision_clone.yml"))
    safe_profile = shlex.quote(profile)

    extra_tokens = " ".join(shlex.quote(t) for t in _overrides() + _seq_extras)

    # Append resolved sequence vars without duplicating keys already carried
    # by the form profile or _overrides(). Only lock out vm_oem_profile when
    # the form actually supplied one — otherwise the sequence is free to set it.
    existing_keys = set()
    if profile:
        existing_keys.add("vm_oem_profile")
    for t in _overrides():
        if "=" in t and not t.startswith("-"):
            existing_keys.add(t.split("=", 1)[0])
    seq_tokens = []
    for key, value in resolved_vars.items():
        if key not in existing_keys:
            seq_tokens += ["-e", f"{key}={value}"]
    if seq_tokens:
        seq_extra = " ".join(shlex.quote(t) for t in seq_tokens)
        extra_tokens = (extra_tokens + " " + seq_extra).strip()

    script_lines = ["#!/bin/bash", "set -e", ""]
    script_lines.append(f"echo 'Provisioning {count} VMs sequentially ({profile})'")

    for i in range(count):
        script_lines.append(f"echo '=== VM {i+1}/{count} ==='")
        cmd_line = f"ansible-playbook {playbook}"
        if profile:
            cmd_line += f" -e vm_oem_profile={safe_profile}"
        cmd_line += " -e vm_count=1"
        if extra_tokens:
            cmd_line += f" {extra_tokens}"
        script_lines.append(cmd_line)

    script_lines.append(f"echo '=== Done: {count} VMs provisioned ==='")

    script_content = "\n".join(script_lines)
    fd, script_file = tempfile.mkstemp(suffix=".sh", dir=str(BASE_DIR / "jobs"))
    os.close(fd)
    script_path = Path(script_file)
    script_path.write_text(script_content)
    script_path.chmod(0o755)

    job = job_manager.start("provision_clone", ["bash", str(script_path)], args=args)
    if sequence_id:
        _register_sequence_callbacks(job["id"], int(sequence_id))
    return RedirectResponse(f"/jobs/{job['id']}", status_code=303)


@app.post("/api/jobs/template")
async def start_template(
    request: Request,
    profile: str = Form(""),
    pause_before_sysprep: str = Form(""),
    vm_os_kind: str = Form(""),
    vm_name: str = Form(""),
    utm_iso_name: str = Form(""),
    vm_cpu_cores: str = Form("4"),
    vm_memory_mb: str = Form("8192"),
    vm_disk_gb: str = Form("80"),
):
    if _load_vars().get("hypervisor_type") == "utm":
        return await _enqueue_utm_template_job(
            vm_os_kind=vm_os_kind,
            vm_name=vm_name,
            utm_iso_name=utm_iso_name,
            vm_cpu_cores=vm_cpu_cores,
            vm_memory_mb=vm_memory_mb,
            vm_disk_gb=vm_disk_gb,
        )

    profile = _sanitize_input(profile)
    cmd = [
        "ansible-playbook", str(PLAYBOOK_DIR / "build_template.yml"),
        "-e", f"vm_oem_profile={profile}",
    ]
    args = {"profile": profile}

    # Optional pause-for-manual-software-install gate. The checkbox on
    # /template posts "on" when ticked, "" (or absent) otherwise. If
    # set, we generate a per-job resume-signal path and pass it into
    # Ansible; the build_template role waits on that path appearing
    # before it invokes sysprep. Operator touches the file via
    # POST /api/jobs/{id}/resume-template-build once they're done
    # installing apps in the VM.
    if pause_before_sysprep:
        import uuid
        signal_name = f"template-resume-{uuid.uuid4().hex}"
        signal_path = Path(job_manager.jobs_dir) / signal_name
        cmd.extend(["-e", f"template_pause_signal_path={signal_path}"])
        args["pause_signal_path"] = str(signal_path)
        args["pause_enabled"] = True

    job = job_manager.start("build_template", cmd, args=args)
    return RedirectResponse(f"/jobs/{job['id']}", status_code=303)


async def _enqueue_utm_template_job(
    *,
    vm_os_kind: str,
    vm_name: str,
    utm_iso_name: str,
    vm_cpu_cores: str,
    vm_memory_mb: str,
    vm_disk_gb: str,
):
    """Validate inputs and enqueue a UTM template-build job."""
    import re as _re

    valid_os_kinds = {"windows11", "windows_server"}
    if vm_os_kind not in valid_os_kinds:
        return JSONResponse(
            {"ok": False, "error": f"vm_os_kind must be one of {sorted(valid_os_kinds)}"},
            status_code=400,
        )

    if not _re.match(r'^[a-zA-Z0-9_-]{1,64}$', vm_name):
        return JSONResponse(
            {"ok": False, "error": "vm_name must match ^[a-zA-Z0-9_-]{1,64}$"},
            status_code=400,
        )

    current_vars = _load_vars()
    iso_dir_raw = current_vars.get("utm_iso_dir", "~/UTM-ISOs")
    iso_dir = Path(os.path.expanduser(iso_dir_raw)).resolve()
    iso_path = (iso_dir / utm_iso_name).resolve()

    if not str(iso_path).startswith(str(iso_dir)):
        return JSONResponse({"ok": False, "error": "Path traversal detected in utm_iso_name"}, status_code=400)

    if not iso_path.is_file():
        return JSONResponse(
            {"ok": False, "error": f"ISO file not found: {utm_iso_name} (looked in {iso_dir})"},
            status_code=400,
        )

    try:
        cpu_cores = int(vm_cpu_cores)
        memory_mb = int(vm_memory_mb)
        disk_gb = int(vm_disk_gb)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "cpu_cores, memory_mb, and disk_gb must be integers"}, status_code=400)

    cmd = [
        "ansible-playbook", str(PLAYBOOK_DIR / "build_utm_template.yml"),
        "-e", f"vm_name={vm_name}",
        "-e", f"vm_os_kind={vm_os_kind}",
        "-e", f"utm_iso_name={utm_iso_name}",
        "-e", f"vm_cpu_cores={cpu_cores}",
        "-e", f"vm_memory_mb={memory_mb}",
        "-e", f"vm_disk_gb={disk_gb}",
    ]
    args = {
        "vm_name": vm_name,
        "vm_os_kind": vm_os_kind,
        "utm_iso_name": utm_iso_name,
        "vm_cpu_cores": cpu_cores,
        "vm_memory_mb": memory_mb,
        "vm_disk_gb": disk_gb,
    }
    job = job_manager.start("build_utm_template", cmd, args=args)
    return RedirectResponse(f"/jobs/{job['id']}", status_code=303)


@app.post("/api/jobs/{job_id}/resume-template-build")
async def resume_template_build(job_id: str):
    """Touch the resume-signal file so the paused template build can
    continue into sysprep. Returns 404 if the job has no pause gate,
    409 if it already resumed (file already exists)."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(404, f"job {job_id} not found")
    signal_path = (job.get("args") or {}).get("pause_signal_path")
    if not signal_path:
        raise HTTPException(404, "this job does not have a pause gate")
    p = Path(signal_path)
    if p.exists():
        raise HTTPException(409, "already resumed")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    return {"ok": True, "signal_path": str(p)}


@app.post("/api/ubuntu/build-template")
async def build_ubuntu_template(sequence_id: int):
    """Kick off the Ubuntu template build playbook for the given sequence.
    Returns a JSON payload with the launched job id; the UI redirects to
    /jobs/{job_id} client-side."""
    if _load_vars().get("hypervisor_type") == "utm":
        return JSONResponse(
            {"ok": False, "error": "Ubuntu UTM templates not yet supported"},
            status_code=400,
        )
    seq = sequences_db.get_sequence(SEQUENCES_DB, sequence_id)
    if seq is None or seq.get("target_os") != "ubuntu":
        return JSONResponse(
            {"ok": False, "error": "Ubuntu sequence not found"},
            status_code=404,
        )
    cmd = [
        "ansible-playbook", str(PLAYBOOK_DIR / "build_template.yml"),
        "-e", "target_os=ubuntu",
        "-e", f"ubuntu_template_sequence_id={sequence_id}",
    ]
    args = {"target_os": "ubuntu", "sequence_id": sequence_id}
    job = job_manager.start("build_template_ubuntu", cmd, args=args)
    return {"ok": True, "job_id": job["id"]}


@app.get("/api/utm/isos")
async def utm_list_isos():
    """List Windows ARM64 ISOs in the configured utm_iso_dir.
    Returns 200 in all cases; uses a 'warning' field if the directory is absent."""
    current_vars = _load_vars()
    if current_vars.get("hypervisor_type") != "utm":
        return {"iso_dir": "", "isos": [], "note": "hypervisor_type is not utm"}

    iso_dir_raw = current_vars.get("utm_iso_dir", "~/UTM-ISOs")
    iso_dir = Path(os.path.expanduser(iso_dir_raw)).resolve()

    if not iso_dir.exists():
        return {
            "iso_dir": str(iso_dir),
            "isos": [],
            "warning": f"Directory not found — create it or update utm_iso_dir: {iso_dir}",
        }

    isos = []
    for entry in sorted(iso_dir.iterdir()):
        if entry.is_file() and entry.suffix.lower() == ".iso":
            stat = entry.stat()
            isos.append({
                "name": entry.name,
                "size_bytes": stat.st_size,
                "mtime": int(stat.st_mtime),
            })

    return {"iso_dir": str(iso_dir), "isos": isos}


@app.get("/api/utm/vms")
async def utm_list_vms():
    """List UTM VMs by running `utmctl list`.
    Returns 200 in all cases; uses an 'error' field on failure."""
    current_vars = _load_vars()
    if current_vars.get("hypervisor_type") != "utm":
        return {"vms": [], "note": "hypervisor_type is not utm"}

    utmctl = current_vars.get("utm_utmctl_path", "/Applications/UTM.app/Contents/MacOS/utmctl")
    try:
        result = subprocess.run(
            [utmctl, "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {"vms": [], "error": result.stderr.strip() or f"utmctl exited {result.returncode}"}

        vms = []
        lines = result.stdout.splitlines()
        for line in lines[1:]:  # skip header row
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 2)
            if len(parts) >= 3:
                vms.append({"uuid": parts[0], "status": parts[1], "name": parts[2]})
            elif len(parts) == 2:
                vms.append({"uuid": parts[0], "status": parts[1], "name": ""})

        return {"vms": vms}

    except subprocess.TimeoutExpired:
        return {"vms": [], "error": "utmctl list timed out"}
    except FileNotFoundError:
        return {"vms": [], "error": f"utmctl not found at {utmctl}"}
    except Exception as exc:
        return {"vms": [], "error": str(exc)}


import shutil
import signal

_UTM_NAME_RE = re.compile(
    r'^(?:[A-Za-z0-9._-]{1,64}|'
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$'
)


def _utm_check(vars_dict: dict):
    """Return a 409 JSONResponse if hypervisor_type != utm, else None."""
    if vars_dict.get("hypervisor_type") != "utm":
        return JSONResponse({"ok": False, "error": "hypervisor_type is not utm"}, status_code=409)
    return None


def _utm_validate_name(name: str):
    """Return a 400 JSONResponse if name is invalid, else None."""
    if not _UTM_NAME_RE.match(name):
        return JSONResponse(
            {"ok": False, "error": "name must be alphanumeric/._- (1-64 chars) or a UUID"},
            status_code=400,
        )
    return None


@app.get("/api/utm/vms/{name}")
async def utm_get_vm(name: str):
    """Return detail for one VM: uuid, status, name, ips, bundle_path."""
    v = _utm_validate_name(name)
    if v:
        return v
    cv = _load_vars()
    c = _utm_check(cv)
    if c:
        return c

    from web import utm_cli
    try:
        vms = utm_cli.list_vms()
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}

    vm = next((m for m in vms if m["uuid"] == name or m["name"] == name), None)
    if vm is None:
        return JSONResponse({"ok": False, "error": f"VM not found: {name!r}"}, status_code=404)

    ips = utm_cli.get_vm_ip(vm["uuid"])
    bundle = None
    try:
        bundle = str(utm_cli.bundle_path_for(name))
    except RuntimeError:
        pass

    return {"ok": True, **vm, "ips": ips, "bundle_path": bundle}


@app.get("/api/utm/vms/{name}/ip")
async def utm_get_vm_ip(name: str):
    """Return IP addresses for the named VM.  Best-effort."""
    v = _utm_validate_name(name)
    if v:
        return v
    c = _utm_check(_load_vars())
    if c:
        return c

    from web import utm_cli
    ips = utm_cli.get_vm_ip(name)
    result: dict = {"ips": ips}
    if not ips:
        result["error"] = "no IPs returned — guest agent may not be running"
    return result


@app.post("/api/utm/vms/{name}/start")
async def utm_start_vm(name: str):
    v = _utm_validate_name(name)
    if v:
        return v
    c = _utm_check(_load_vars())
    if c:
        return c

    from web import utm_cli
    rc, _out, stderr = utm_cli.run_utmctl(["start", name], timeout=15)
    if rc != 0:
        return {"ok": False, "error": stderr.strip() or f"utmctl start exited {rc}"}

    status_after = "unknown"
    try:
        vms = utm_cli.list_vms()
        vm = next((m for m in vms if m["uuid"] == name or m["name"] == name), None)
        if vm:
            status_after = vm["status"]
    except RuntimeError:
        pass

    return {"ok": True, "status_after": status_after}


@app.post("/api/utm/vms/{name}/suspend")
async def utm_suspend_vm(name: str):
    v = _utm_validate_name(name)
    if v:
        return v
    c = _utm_check(_load_vars())
    if c:
        return c

    from web import utm_cli
    rc, _out, stderr = utm_cli.run_utmctl(["suspend", name], timeout=15)
    if rc != 0:
        return {"ok": False, "error": stderr.strip() or f"utmctl suspend exited {rc}"}

    status_after = "unknown"
    try:
        vms = utm_cli.list_vms()
        vm = next((m for m in vms if m["uuid"] == name or m["name"] == name), None)
        if vm:
            status_after = vm["status"]
    except RuntimeError:
        pass

    return {"ok": True, "status_after": status_after}


@app.post("/api/utm/vms/{name}/stop")
async def utm_stop_vm(name: str):
    v = _utm_validate_name(name)
    if v:
        return v
    c = _utm_check(_load_vars())
    if c:
        return c

    from web import utm_cli
    rc, _out, stderr = utm_cli.run_utmctl(["stop", name], timeout=60)
    if rc == -2:
        return {"ok": False, "error": "graceful stop timed out — use force"}
    if rc != 0:
        return {"ok": False, "error": stderr.strip() or f"utmctl stop exited {rc}"}

    status_after = "unknown"
    try:
        vms = utm_cli.list_vms()
        vm = next((m for m in vms if m["uuid"] == name or m["name"] == name), None)
        if vm:
            status_after = vm["status"]
    except RuntimeError:
        pass

    return {"ok": True, "status_after": status_after}


@app.post("/api/utm/vms/{name}/force-stop")
async def utm_force_stop_vm(name: str):
    """SIGTERM the QEMU process backing this VM."""
    v = _utm_validate_name(name)
    if v:
        return v
    c = _utm_check(_load_vars())
    if c:
        return c

    from web import utm_cli
    try:
        bundle = utm_cli.bundle_path_for(name)
    except RuntimeError as exc:
        return {"ok": False, "error": f"Cannot resolve bundle path: {exc}"}

    bundle_str = str(bundle)

    try:
        ps = subprocess.run(
            ["ps", "-axo", "pid,command"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        return {"ok": False, "error": f"ps failed: {exc}"}

    killed_pid = None
    for line in ps.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        pid_str, cmd = parts
        if bundle_str in cmd and "qemu" in cmd.lower():
            try:
                pid = int(pid_str)
                os.kill(pid, signal.SIGTERM)
                killed_pid = pid
                break
            except (ValueError, ProcessLookupError, PermissionError) as exc:
                return {"ok": False, "error": f"kill({pid_str}) failed: {exc}"}

    if killed_pid is None:
        return {
            "ok": True,
            "killed_pid": None,
            "note": "No matching QEMU process found — VM may already be stopped",
        }
    return {"ok": True, "killed_pid": killed_pid}


@app.post("/api/utm/vms/{name}/delete")
async def utm_delete_vm(name: str):
    """Delete a stopped UTM VM by removing its .utm bundle from disk.

    Safety checks:
    (a) VM must be stopped.
    (b) Bundle path must be a direct child of utm_library_path and end in .utm.
    (c) Bundles >500 MB are deleted in a background thread (non-blocking).
    """
    v = _utm_validate_name(name)
    if v:
        return v
    c = _utm_check(_load_vars())
    if c:
        return c

    from web import utm_cli
    import asyncio

    try:
        vms = utm_cli.list_vms()
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    vm = next((m for m in vms if m["uuid"] == name or m["name"] == name), None)
    if vm is None:
        return JSONResponse({"ok": False, "error": f"VM not found: {name!r}"}, status_code=404)
    if vm["status"] != "stopped":
        return JSONResponse(
            {"ok": False, "error": f"VM must be stopped before deletion (current: {vm['status']})"},
            status_code=409,
        )

    try:
        bundle = utm_cli.bundle_path_for(name)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    try:
        du = subprocess.run(
            ["du", "-sk", str(bundle)],
            capture_output=True, text=True, timeout=30,
        )
        size_kb = int(du.stdout.split()[0]) if du.returncode == 0 else 0
    except Exception:
        size_kb = 0

    size_mb = size_kb // 1024

    try:
        await asyncio.to_thread(shutil.rmtree, bundle, False)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"rmtree failed: {exc}"}, status_code=500)

    return {"ok": True, "deleted": str(bundle), "size_mb": size_mb}


@app.post("/api/utm/vms/{name}/open-in-app")
async def utm_open_in_app(name: str):
    """Open the VM's .utm bundle in UTM.app via `open -a UTM <bundle>`."""
    v = _utm_validate_name(name)
    if v:
        return v
    c = _utm_check(_load_vars())
    if c:
        return c

    from web import utm_cli
    try:
        bundle = utm_cli.bundle_path_for(name)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        subprocess.run(
            ["open", "-a", "UTM", str(bundle)],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    return {"ok": True, "bundle": str(bundle)}


@app.get("/api/utm/host-summary")
async def utm_host_summary():
    """Return macOS host + VM health for the UTM dashboard card.

    Returns 200 in all cases (including partial failures).
    Returns 409 when hypervisor_type != utm.
    """
    cv = _load_vars()
    c = _utm_check(cv)
    if c:
        return c

    from web import utm_host_metrics, utm_vm_metrics

    utm_docs_dir = cv.get("utm_documents_dir") or cv.get("utm_library_path") or \
        "~/Library/Containers/com.utmapp.UTM/Data/Documents"

    try:
        host = utm_host_metrics.get_cached_host_summary(utm_docs_dir)
        ok_host = True
        host_err = None
    except Exception as exc:
        host = {}
        ok_host = False
        host_err = str(exc)

    try:
        vms = utm_vm_metrics.vm_summary()
        ok_vms = "error" not in vms
        vm_err = vms.pop("error", None)
    except Exception as exc:
        vms = {}
        ok_vms = False
        vm_err = str(exc)

    ok = ok_host and ok_vms
    result: dict = {
        "ok": ok,
        "hypervisor_type": "utm",
        "host": host,
        "vms": vms,
    }
    if not ok:
        errors = [e for e in [host_err, vm_err] if e]
        result["error"] = "; ".join(errors) if errors else "unknown error"
    return result


@app.get("/api/vms/{vmid}/console")
async def vm_console(vmid: int):
    """Redirect to the embedded noVNC console page."""
    return RedirectResponse(f"/vms/{vmid}/console")


@app.get("/vms/{vmid}/console", response_class=HTMLResponse)
async def vm_console_page(request: Request, vmid: int):
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    # VM 'name' in Proxmox is the device serial for provisioned VMs (the
    # provisioning flow renames it to the generated serial post-clone).
    serial = ""
    try:
        vm_cfg = _proxmox_api(f"/nodes/{node}/qemu/{vmid}/config")
        serial = (vm_cfg or {}).get("name", "") if isinstance(vm_cfg, dict) else ""
    except Exception:
        pass
    return templates.TemplateResponse("console.html", {
        "request": request,
        "vmid": vmid,
        "node": node,
        "serial": serial,
    })


@app.get("/api/vms/{vmid}/vnc-init")
async def vm_vnc_init(vmid: int):
    """Request a websocket-mode VNC ticket from Proxmox. Returns the
    port + short-lived ticket the browser needs to open the VNC stream
    (via our /api/vms/{vmid}/vnc-ws proxy)."""
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        # websocket=1 tells Proxmox to issue a ticket valid for the
        # /vncwebsocket endpoint (different from the default TCP ticket).
        data = _proxmox_api_post(
            f"/nodes/{node}/qemu/{vmid}/vncproxy",
            data={"websocket": "1"},
        )
    except Exception as e:
        return {"error": f"vncproxy failed: {e}"}
    return {
        "node": node,
        "vmid": vmid,
        "port": data.get("port"),
        "ticket": data.get("ticket"),
        "user": data.get("user"),
    }


@app.websocket("/api/vms/{vmid}/vnc-ws")
async def vm_vnc_websocket(websocket: WebSocket, vmid: int):
    """Bidirectional WebSocket proxy to Proxmox's vncwebsocket endpoint.

    Browsers can't send custom headers on a WebSocket handshake, so we
    authenticate the upstream connection with the stored API token and
    pipe frames in both directions. The inner VNC-level auth is handled
    by noVNC using the short-lived ticket as the password."""
    import ssl
    import websockets as ws_lib
    port = websocket.query_params.get("port")
    vncticket = websocket.query_params.get("vncticket")
    if not port or not vncticket:
        await websocket.close(code=1008, reason="missing port or vncticket")
        return

    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    pve_port = cfg.get("proxmox_port", 8006)
    node = cfg.get("proxmox_node", "pve")
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")

    await websocket.accept(subprotocol="binary")

    ssl_ctx = ssl.create_default_context()
    if not cfg.get("proxmox_validate_certs", False):
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    from urllib.parse import quote
    upstream_url = (
        f"wss://{host}:{pve_port}/api2/json/nodes/{node}/qemu/{vmid}/vncwebsocket"
        f"?port={quote(port)}&vncticket={quote(vncticket)}"
    )
    auth_header = [("Authorization", f"PVEAPIToken={token_id}={token_secret}")]

    try:
        async with ws_lib.connect(
            upstream_url,
            additional_headers=auth_header,
            ssl=ssl_ctx,
            subprotocols=["binary"],
            max_size=None,
            ping_interval=None,
        ) as upstream:
            async def browser_to_pve():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if "bytes" in msg and msg["bytes"] is not None:
                            await upstream.send(msg["bytes"])
                        elif "text" in msg and msg["text"] is not None:
                            await upstream.send(msg["text"])
                except Exception:
                    pass

            async def pve_to_browser():
                try:
                    async for frame in upstream:
                        if isinstance(frame, bytes):
                            await websocket.send_bytes(frame)
                        else:
                            await websocket.send_text(frame)
                except Exception:
                    pass

            await asyncio.gather(
                browser_to_pve(), pve_to_browser(), return_exceptions=True
            )
    except Exception as e:
        try:
            await websocket.close(code=1011, reason=str(e)[:100])
        except Exception:
            pass
        return
    try:
        await websocket.close()
    except Exception:
        pass


@app.post("/api/vms/{vmid}/start")
async def vm_start(vmid: int):
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/start")
    except Exception as e:
        return _redirect_with_error("/vms", f"Start failed: {e}")
    return RedirectResponse("/vms", status_code=303)


@app.post("/api/vms/{vmid}/shutdown")
async def vm_shutdown(vmid: int):
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/shutdown")
    except Exception as e:
        return _redirect_with_error("/vms", f"Shutdown failed: {e}")
    return RedirectResponse("/vms", status_code=303)


@app.post("/api/vms/{vmid}/stop")
async def vm_stop(vmid: int):
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/stop")
    except Exception as e:
        return _redirect_with_error("/vms", f"Force stop failed: {e}")
    return RedirectResponse("/vms", status_code=303)


@app.post("/api/vms/{vmid}/reset")
async def vm_reset(vmid: int):
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/reset")
    except Exception as e:
        return _redirect_with_error("/vms", f"Reset failed: {e}")
    return RedirectResponse("/vms", status_code=303)


@app.post("/api/vms/{vmid}/delete")
async def vm_delete(vmid: int):
    import time
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        # Stop VM first if running
        try:
            _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/stop")
            time.sleep(3)
        except Exception:
            pass  # Already stopped or doesn't matter
        _proxmox_api_delete(f"/nodes/{node}/qemu/{vmid}")
    except Exception as e:
        return _redirect_with_error("/vms", f"Delete failed: {e}")
    return RedirectResponse("/vms", status_code=303)


# Scancode mapping for typing text via QMP sendkey
_CHAR_TO_KEYS = {}
for c in 'abcdefghijklmnopqrstuvwxyz':
    _CHAR_TO_KEYS[c] = c
    _CHAR_TO_KEYS[c.upper()] = f"shift-{c}"
for i, c in enumerate('0123456789'):
    _CHAR_TO_KEYS[c] = c
_SHIFT_SYMBOLS = {
    '!': '1', '@': '2', '#': '3', '$': '4', '%': '5',
    '^': '6', '&': '7', '*': '8', '(': '9', ')': '0',
    '_': 'minus', '+': 'equal', '{': 'bracket_left',
    '}': 'bracket_right', '|': 'backslash', ':': 'semicolon',
    '"': 'apostrophe', '<': 'comma', '>': 'dot', '?': 'slash',
    '~': 'grave_accent',
}
for sym, base in _SHIFT_SYMBOLS.items():
    _CHAR_TO_KEYS[sym] = f"shift-{base}"
_PLAIN_SYMBOLS = {
    '-': 'minus', '=': 'equal', '[': 'bracket_left',
    ']': 'bracket_right', '\\': 'backslash', ';': 'semicolon',
    "'": 'apostrophe', ',': 'comma', '.': 'dot', '/': 'slash',
    '`': 'grave_accent', ' ': 'spc', '\t': 'tab',
}
for sym, key in _PLAIN_SYMBOLS.items():
    _CHAR_TO_KEYS[sym] = key


@app.post("/api/vms/{vmid}/typetext")
async def vm_typetext(vmid: int, text: str = Form(...)):
    """Type a string into a VM via QMP sendkey (works without guest agent)."""
    import time
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    errors = []
    for ch in text:
        key = _CHAR_TO_KEYS.get(ch)
        if not key:
            continue
        try:
            _proxmox_api_put(f"/nodes/{node}/qemu/{vmid}/sendkey", data={"key": key})
        except Exception as e:
            errors.append(f"{ch}: {e}")
            break
        time.sleep(0.05)
    if errors:
        return _redirect_with_error("/vms", f"Type failed: {errors[0]}")
    return RedirectResponse("/vms", status_code=303)


@app.post("/api/vms/{vmid}/sendkey")
async def vm_sendkey(vmid: int, key: str = Form(...)):
    """Send a single key combo to a VM (e.g. ctrl-alt-del, ret, tab)."""
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        _proxmox_api_put(f"/nodes/{node}/qemu/{vmid}/sendkey", data={"key": key})
    except Exception as e:
        return _redirect_with_error("/vms", f"Sendkey failed: {e}")
    return RedirectResponse("/vms", status_code=303)


# ---- JSON endpoints for the embedded console page (no redirects) ----

@app.get("/api/vms/{vmid}/status-json")
async def vm_status_json(vmid: int):
    """Current VM status as JSON, for the console page to poll."""
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        data = _proxmox_api(f"/nodes/{node}/qemu/{vmid}/status/current")
    except Exception as e:
        return {"error": str(e)}
    if not isinstance(data, dict):
        return {"error": "unexpected response"}
    return {
        "status": data.get("status"),
        "qmpstatus": data.get("qmpstatus"),
        "uptime": data.get("uptime", 0),
        "cpu": data.get("cpu", 0),
        "mem": data.get("mem", 0),
        "maxmem": data.get("maxmem", 0),
    }


_POWER_ACTIONS = {"start", "stop", "shutdown", "reboot", "reset", "suspend", "resume"}


@app.post("/api/vms/{vmid}/action/{action}")
async def vm_action_json(vmid: int, action: str):
    """Power action via Proxmox status endpoint. Returns JSON (no redirect)."""
    if action not in _POWER_ACTIONS:
        return {"error": f"invalid action: {action}"}
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/{action}")
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True, "action": action}


@app.post("/api/vms/{vmid}/type")
async def vm_type_json(vmid: int, text: str = Form(""), press_enter: str = Form("")):
    """Type text via QMP sendkey. Returns JSON."""
    import time
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    skipped = []
    keys = []
    for ch in text:
        k = _CHAR_TO_KEYS.get(ch)
        if k is None:
            skipped.append(ch)
        else:
            keys.append(k)
    if press_enter:
        keys.append("ret")
    sent = 0
    for key in keys:
        try:
            _proxmox_api_put(f"/nodes/{node}/qemu/{vmid}/sendkey", data={"key": key})
            sent += 1
        except Exception as e:
            return {"ok": False, "sent": sent, "error": str(e)}
        time.sleep(0.05)
    return {"ok": True, "sent": sent, "skipped": skipped}


@app.post("/api/vms/{vmid}/key")
async def vm_key_json(vmid: int, key: str = Form(...)):
    """Single QEMU keyname (e.g. 'ctrl-alt-delete'). Returns JSON."""
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        _proxmox_api_put(f"/nodes/{node}/qemu/{vmid}/sendkey", data={"key": key})
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True, "key": key}


def _suggested_rename_for_vm(vmid: int, node: str) -> str:
    """Compute the 'sensible default' target name for a rename.

    Same precedence as the /vms page uses for serial/hostname:
    guest-exec WMI Serial → vm_name (for autopilot-convention
    Gell-* names with per-VM SMBIOS files) → smbios1 serial as a
    last resort.
    """
    config = _proxmox_api(f"/nodes/{node}/qemu/{vmid}/config") or {}
    vm_name = (config.get("name") or "").strip()
    args = config.get("args") or ""
    per_vm_smbios = "smbios file=" in args
    smbios1 = config.get("smbios1") or ""
    # Prefer the Windows-side authoritative serial when the guest is
    # reachable. If the agent is dead, vm_name is the next-best
    # source — our autopilot flow makes vm_name == real serial.
    try:
        raw = _fetch_guest_windows_details(node, vmid) or {}
    except Exception:
        raw = {}
    if raw.get("Serial"):
        return raw["Serial"]
    if per_vm_smbios and vm_name.lower().startswith("gell-"):
        return vm_name
    smbios_serial = _decode_smbios_serial(smbios1)
    if smbios_serial:
        return smbios_serial
    return vm_name  # last resort — better than empty


def _sanitize_windows_hostname(raw: str) -> str:
    """Windows NetBIOS name constraints: 1-15 chars, no spaces, no
    reserved chars. Strip whitespace / dots / slashes / etc.; truncate
    to 15 chars. Returns '' if the result is empty."""
    return re.sub(r"[^A-Za-z0-9\-]", "", (raw or "").strip())[:15]


@app.get("/api/vms/{vmid}/rename-suggest")
async def vm_rename_suggest(vmid: int):
    """Return the suggested target name + source so the UI can show a
    preview before the operator commits. Free of side effects."""
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        suggested = _suggested_rename_for_vm(vmid, node)
    except Exception as e:
        raise HTTPException(500, f"probe failed: {e}") from e
    sanitized = _sanitize_windows_hostname(suggested)
    return {
        "suggested": suggested,
        "sanitized": sanitized,
        "max_length": 15,
    }


@app.post("/api/vms/{vmid}/rename")
async def vm_rename(vmid: int, new_name: str = Form("")):
    """Rename the Windows computer inside the VM.

    ``new_name`` is the operator's final choice (maybe with a prefix,
    maybe custom); if omitted we fall back to the computed suggestion
    so the button can still be clicked from scripts without a form.
    Updates the Proxmox VM name to match so /vms, /devices, and
    monitoring stay in sync.
    """
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        target = (new_name or "").strip()
        if not target:
            target = _suggested_rename_for_vm(vmid, node)
        hostname = _sanitize_windows_hostname(target)
        if not hostname:
            return _redirect_with_error(
                "/vms",
                f"Rename target '{target}' produced an empty hostname "
                "after sanitisation (Windows allows A-Z, 0-9, '-' only, "
                "max 15 chars).",
            )
        # Update Proxmox VM name (uses same sanitised form).
        _proxmox_api_put(
            f"/nodes/{node}/qemu/{vmid}/config", data={"name": hostname},
        )
        # Execute rename via guest agent — best effort; if the agent
        # is dead the PVE rename still stuck, which is useful.
        try:
            ps_cmd = f"Rename-Computer -NewName '{hostname}' -Force"
            _proxmox_api_post(
                f"/nodes/{node}/qemu/{vmid}/agent/exec",
                data={"command": "powershell.exe", "input-data": ps_cmd},
            )
        except Exception:
            # PVE rename succeeded, guest-side didn't. User sees the
            # VM-name change; Windows-side rename requires manual
            # action or agent recovery.
            return _redirect_with_error(
                "/vms",
                f"Renamed VM {vmid} to {hostname} (PVE only — guest "
                "agent unreachable, run Rename-Computer manually inside "
                "Windows or reboot to pick up the new name).",
            )
    except Exception as e:
        return _redirect_with_error("/vms", f"Rename failed: {e}")
    return _redirect_with_error(
        "/vms",
        f"Renamed VM {vmid} to {hostname} — restart required to apply",
    )


@app.post("/api/jobs/capture-and-upload")
async def start_capture_and_upload(
    missing_vmids: list[str] = Form(...),
    group_tag: str = Form(""),
):
    """Capture hashes for selected VMs in parallel, then upload all to Intune."""
    if group_tag:
        group_tag = _sanitize_input(group_tag)

    vm_list = []
    for entry in missing_vmids:
        vmid, name = entry.split(":", 1)
        _sanitize_input(vmid)
        _sanitize_input(name)
        vm_list.append({"vmid": vmid, "name": name})

    # Launch parallel capture jobs (one per VM)
    for vm in vm_list:
        cmd = [
            "ansible-playbook", str(PLAYBOOK_DIR / "retry_inject_hash.yml"),
            "-e", f"vm_vmid={vm['vmid']}",
            "-e", f"vm_name={vm['name']}",
            "-e", "autopilot_skip=true",
        ]
        if group_tag:
            cmd += ["-e", f"vm_group_tag={group_tag}"]
        job_manager.start("hash_capture", cmd, args={"vmid": vm["vmid"], "vm_name": vm["name"], "group_tag": group_tag})

    # Launch upload job that waits for captures to finish
    import tempfile
    script_lines = [
        "#!/bin/bash",
        f"echo 'Waiting for {len(vm_list)} capture job(s) to finish...'",
        "while true; do",
        "  RUNNING=$(ps aux | grep '[a]nsible-playbook.*retry_inject_hash' | wc -l)",
        "  [ \"$RUNNING\" -eq 0 ] && break",
        "  echo \"  $RUNNING capture(s) still running...\"",
        "  sleep 5",
        "done",
        "echo '=== All captures done, uploading hashes to Intune ==='",
        f"ansible-playbook {shlex.quote(str(PLAYBOOK_DIR / 'upload_hashes.yml'))}",
    ]
    fd, script_file = tempfile.mkstemp(suffix=".sh", dir=str(BASE_DIR / "jobs"))
    os.close(fd)
    script_path = Path(script_file)
    script_path.write_text("\n".join(script_lines))
    script_path.chmod(0o755)

    job_manager.start("upload_after_capture", ["bash", str(script_path)],
                      args={"vms": [v["name"] for v in vm_list], "group_tag": group_tag, "upload": True})

    return RedirectResponse("/jobs", status_code=303)


@app.post("/api/jobs/bulk-capture")
async def start_bulk_capture(
    vmids: list[str] = Form(...),
    group_tag: str = Form(""),
):
    """Capture hashes for multiple VMs in parallel (one job per VM)."""
    if group_tag:
        group_tag = _sanitize_input(group_tag)

    for entry in vmids:
        vmid, name = entry.split(":", 1)
        _sanitize_input(vmid)
        _sanitize_input(name)
        cmd = [
            "ansible-playbook", str(PLAYBOOK_DIR / "retry_inject_hash.yml"),
            "-e", f"vm_vmid={vmid}",
            "-e", f"vm_name={name}",
            "-e", "autopilot_skip=true",
        ]
        if group_tag:
            cmd += ["-e", f"vm_group_tag={group_tag}"]
        job_manager.start("hash_capture", cmd, args={"vmid": vmid, "vm_name": name, "group_tag": group_tag})

    return RedirectResponse("/jobs", status_code=303)


@app.post("/api/jobs/capture")
async def start_capture(
    vmid: int = Form(...),
    vm_name: str = Form(""),
    group_tag: str = Form(""),
):
    name = _sanitize_input(vm_name) if vm_name else f"autopilot-{vmid}"
    if group_tag:
        group_tag = _sanitize_input(group_tag)
    cmd = [
        "ansible-playbook", str(PLAYBOOK_DIR / "retry_inject_hash.yml"),
        "-e", f"vm_vmid={vmid}",
        "-e", f"vm_name={name}",
        "-e", "autopilot_skip=true",
    ]
    if group_tag:
        cmd += ["-e", f"vm_group_tag={group_tag}"]
    args = {"vmid": vmid, "vm_name": name, "group_tag": group_tag}
    job = job_manager.start("hash_capture", cmd, args=args)
    return RedirectResponse(f"/jobs/{job['id']}", status_code=303)


@app.post("/api/jobs/upload")
async def start_upload(
    files: list[str] = Form(...),
    group_tags: list[str] = Form(...),
):
    """Upload selected hash files to Intune with per-file group tags."""
    upload_playbook = shlex.quote(str(PLAYBOOK_DIR / "upload_hashes.yml"))

    for filename, tag in zip(files, group_tags):
        try:
            file_path = _safe_path(HASH_DIR, filename)
        except ValueError:
            continue
        if not file_path.exists():
            continue

        cmd = [
            "ansible-playbook", str(PLAYBOOK_DIR / "upload_hashes.yml"),
        ]
        if tag:
            tag = _sanitize_input(tag)
            cmd += ["-e", f"vm_group_tag={tag}"]
        cmd += ["-e", f"hash_file={shlex.quote(str(file_path))}"]

        job_manager.start("upload_hash", cmd, args={"file": filename, "group_tag": tag})

    return RedirectResponse("/hashes?uploaded=1", status_code=303)


@app.get("/api/jobs")
async def api_list_jobs():
    return job_manager.list_jobs()


@app.post("/api/jobs/{job_id}/kill")
async def kill_job(job_id: str):
    """Request termination. Flips kill_requested=1 on the job row; the
    builder owning the job will see it on its next heartbeat cycle
    (~5s max) and SIGTERM the subprocess. Redirects to /jobs/<id>."""
    row = jobs_db.get_job(JOBS_DB, job_id)
    if row is None:
        raise HTTPException(404, f"job {job_id} not found")
    if row["status"] != "running":
        # Already done; ignore quietly so double-clicks don't 400.
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)
    jobs_db.request_kill(JOBS_DB, job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


# Test-only harness — enabled when AUTOPILOT_ENABLE_TEST_JOBS=1.
# Used by the Phase 6 integration suite (kill path, scale, wedged-playbook
# survival). The playbook simply runs `sleep {{ duration }}` so tests can
# exercise the builder/kill machinery without spawning a real provision.
if os.environ.get("AUTOPILOT_ENABLE_TEST_JOBS") == "1":
    @app.post("/api/jobs/test-long-sleep")
    async def _enqueue_test_long_sleep(duration: str = Form("60")):
        cmd = ["ansible-playbook",
               str(PLAYBOOK_DIR / "_test_long_sleep.yml"),
               "-e", f"duration={duration}"]
        entry = job_manager.start("capture_hash", cmd,
                                  args={"duration": duration})
        return {"id": entry["id"]}


@app.get("/api/jobs/recent")
async def api_recent_jobs(limit: int = 5):
    """Return the N most recently-started jobs, newest first.

    Used by the home-page dashboard. Keeps the payload small —
    only the fields the dashboard renders, plus a precomputed
    duration string so the client doesn't need to know about
    started/ended ISO parsing.
    """
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 5
    jobs = job_manager.list_jobs()[:limit]
    out = []
    for j in jobs:
        args = j.get("args") or {}
        # Best-effort "target" label — the thing the job is acting
        # on. Different job types stash different keys, so try a
        # handful in priority order and fall back to "—".
        target = (
            args.get("hostname_pattern")
            or args.get("vm_name")
            or args.get("template_name")
            or args.get("serial")
            or args.get("sequence_name")
            or ""
        )
        out.append({
            "id": j["id"],
            "playbook": j.get("playbook"),
            "status": j.get("status"),
            "started": j.get("started"),
            "ended": j.get("ended"),
            "duration": compute_duration(j),
            "target": target,
        })
    return {"jobs": out}


@app.get("/api/jobs/running")
async def api_running_jobs():
    """Return currently-running jobs with elapsed seconds + a
    rough progress estimate (0-99).

    Used by the home-page Live-now module. Progress uses a
    per-playbook expected-seconds table; if the job type isn't
    known, we fall back to 50% so the bar still moves.
    """
    # Expected seconds per playbook type — rough order-of-magnitude
    # numbers pulled from recent successful runs. Accurate enough
    # for a progress bar; the dashboard is not a scheduler.
    expected = {
        "provision.yml":      20 * 60,
        "template.yml":       45 * 60,
        "capture.yml":         3 * 60,
        "upload.yml":          2 * 60,
        "bulk-capture.yml":   10 * 60,
        "cloud-delete.yml":    5 * 60,
    }
    now = datetime.now(timezone.utc)
    running = []
    queued = 0
    for j in job_manager.list_jobs():
        if j.get("status") != "running":
            continue
        started_iso = j.get("started")
        elapsed = 0
        if started_iso:
            try:
                elapsed = int(
                    (now - datetime.fromisoformat(started_iso)).total_seconds()
                )
            except Exception:
                elapsed = 0
        pb = j.get("playbook") or ""
        exp = expected.get(pb, 0)
        if exp > 0:
            pct = min(99, int(elapsed / exp * 100))
        else:
            pct = 50
        args = j.get("args") or {}
        target = (
            args.get("hostname_pattern")
            or args.get("vm_name")
            or args.get("template_name")
            or args.get("serial")
            or args.get("sequence_name")
            or ""
        )
        running.append({
            "id": j["id"],
            "playbook": pb,
            "target": target,
            "started": started_iso,
            "elapsed_seconds": elapsed,
            "progress_pct": pct,
        })
    return {
        "running": running,
        "running_count": len(running),
        "queued_count": queued,
    }


@app.get("/api/services")
async def api_services():
    """Read per-service heartbeats from the ``service_health`` table.

    The table is owned by the microservice-split PR — until that
    ships, the table won't exist. In that case we return
    ``{"services": [], "available": false}`` so the dashboard strip
    can hide itself gracefully without throwing a 500.
    """
    db_path = DEVICE_MONITOR_DB
    if not db_path.exists():
        return {"services": [], "available": False}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            exists = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='service_health'"
            ).fetchone()
            if not exists:
                return {"services": [], "available": False}
            rows = conn.execute(
                "SELECT service_id, service_type, version_sha, "
                "started_at, last_heartbeat, detail "
                "FROM service_health ORDER BY service_type, service_id"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        # Schema drift, lock contention, corrupted file — don't
        # blow up the dashboard. Silently degrade.
        return {"services": [], "available": False}

    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        age = None
        hb = r["last_heartbeat"]
        if hb:
            try:
                age = int((now - datetime.fromisoformat(hb)).total_seconds())
            except Exception:
                age = None
        out.append({
            "service_id":     r["service_id"],
            "service_type":   r["service_type"],
            "version_sha":    r["version_sha"],
            "started_at":     r["started_at"],
            "last_heartbeat": hb,
            "detail":         r["detail"],
            "age_seconds":    age,
        })
    return {"services": out, "available": True}


@app.get("/api/fleet/summary")
async def api_fleet_summary():
    """Fleet enrollment counts + percentages from the most-recent
    sweep's ``device_probes`` rows.

    Only columns that actually exist in ``device_history_db`` are
    reported. MDE has no column today, so the ``mde_pct`` key is
    intentionally omitted rather than fabricated — the dashboard
    will simply render "—" for that cell.
    """
    db_path = DEVICE_MONITOR_DB
    default = {"total": 0}
    if not db_path.exists():
        return default
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Is the table present? (Early in a fresh install the
            # monitor may not have run yet.)
            has = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='device_probes'"
            ).fetchone()
            if not has:
                return default
            # Latest sweep only — older sweeps would double-count.
            sweep_row = conn.execute(
                "SELECT MAX(sweep_id) AS sid FROM device_probes"
            ).fetchone()
            sweep_id = sweep_row["sid"] if sweep_row else None
            if not sweep_id:
                return default
            row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(ad_found)     AS ad, "
                "SUM(entra_found)  AS entra, "
                "SUM(intune_found) AS intune "
                "FROM device_probes WHERE sweep_id = ?",
                (sweep_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return default

    total = int(row["total"] or 0)
    if total == 0:
        return {"total": 0}
    out: dict = {"total": total}
    # Entra device == Autopilot-registered device in practice here;
    # the spec asks for "autopilot_pct" so we surface entra_found
    # under that key since we have no dedicated autopilot column.
    if row["ad"] is not None:
        out["ad_joined_pct"] = round(100 * int(row["ad"]) / total)
    if row["entra"] is not None:
        out["autopilot_pct"] = round(100 * int(row["entra"]) / total)
    if row["intune"] is not None:
        out["intune_pct"] = round(100 * int(row["intune"]) / total)
    # mde_pct intentionally omitted — no column for it yet.
    return out


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        return {"error": "not found"}
    job["log"] = job_manager.get_log(job_id)
    return job


@app.post("/api/autopilot/delete")
async def delete_autopilot_device(device_id: str = Form(...)):
    try:
        _graph_api(f"/deviceManagement/windowsAutopilotDeviceIdentities/{device_id}", method="DELETE")
    except Exception:
        pass
    return RedirectResponse("/vms", status_code=303)


@app.post("/api/autopilot/sync")
async def sync_autopilot():
    try:
        _graph_api("/deviceManagement/windowsAutopilotSettings/sync", method="POST")
    except Exception:
        pass
    return RedirectResponse("/vms", status_code=303)


@app.post("/api/hashes/delete")
async def delete_hashes(files: list[str] = Form(...)):
    for filename in files:
        try:
            file_path = _safe_path(HASH_DIR, filename)
        except ValueError:
            continue
        if file_path.exists() and file_path.suffix == ".csv":
            file_path.unlink()
    return RedirectResponse("/hashes", status_code=303)


@app.get("/api/hashes/{filename}")
async def download_hash(filename: str):
    try:
        file_path = _safe_path(HASH_DIR, filename)
    except ValueError:
        return HTMLResponse("<h1>Forbidden</h1>", status_code=403)
    if not file_path.exists() or not file_path.name.endswith(".csv"):
        return HTMLResponse("<h1>File not found</h1>", status_code=404)
    return FileResponse(file_path, filename=filename)


@app.post("/api/hashes/upload")
async def upload_hash_files(files: list[UploadFile] = File(...)):
    HASH_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    for upload in files:
        if not upload.filename or not upload.filename.endswith(".csv"):
            continue
        safe_name = re.sub(r'[^\w\-.]', '_', upload.filename)
        dest = HASH_DIR / safe_name
        content = await upload.read()
        # Basic CSV sanity check: must have at least a header and one data row
        text = content.decode("utf-8-sig", errors="replace")
        lines = text.strip().splitlines()
        if len(lines) < 2:
            continue
        dest.write_bytes(content)
        saved += 1
    if saved == 0:
        return _redirect_with_error("/hashes", "No valid CSV files found")
    return RedirectResponse(f"/hashes?uploaded={saved}", status_code=303)


# --- Answer ISO rebuild ----------------------------------------------------


@app.post("/api/answer-iso/rebuild")
async def rebuild_answer_iso():
    """Regenerate the Windows unattend answer ISO from files/autounattend.xml
    and upload it to Proxmox so the next template build picks up the current
    partition layout + config. Returns a JSON result."""
    import subprocess
    import tempfile as _tmp

    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    iso_storage = cfg.get("proxmox_iso_storage") or "isos"

    xml_src = FILES_DIR / "autounattend.xml"
    if not xml_src.exists():
        return {"ok": False, "error": f"missing {xml_src}"}

    with _tmp.TemporaryDirectory() as td:
        td_path = Path(td)
        stage = td_path / "stage"
        stage.mkdir()
        # OEMDRV volume label is what lets Windows Setup auto-discover the
        # answer file; the filename on the ISO root must stay autounattend.xml.
        (stage / "autounattend.xml").write_bytes(xml_src.read_bytes())
        iso_path = td_path / "autounattend.iso"
        try:
            subprocess.run(
                ["genisoimage", "-quiet", "-o", str(iso_path),
                 "-J", "-r", "-V", "OEMDRV", str(stage)],
                check=True, capture_output=True, text=True,
            )
        except FileNotFoundError:
            return {"ok": False, "error": "genisoimage not installed in container"}
        except subprocess.CalledProcessError as e:
            return {"ok": False, "error": f"genisoimage failed: {e.stderr[:300]}"}

        # Upload to Proxmox: POST /nodes/{node}/storage/{storage}/upload with
        # multipart form (content=iso, filename=autounattend.iso).
        host = cfg.get("proxmox_host", "")
        port = cfg.get("proxmox_port", 8006)
        token_id = cfg.get("vault_proxmox_api_token_id", "")
        token_secret = cfg.get("vault_proxmox_api_token_secret", "")
        url = f"https://{host}:{port}/api2/json/nodes/{node}/storage/{iso_storage}/upload"
        try:
            with open(iso_path, "rb") as fh:
                # PVE's upload endpoint takes 'content' (storage content type)
                # as a form field and 'filename' as the multipart file part.
                # The target name on disk is derived from the multipart file
                # tuple's filename; do NOT also pass 'filename' as a form field.
                resp = requests.post(
                    url,
                    headers={"Authorization": f"PVEAPIToken={token_id}={token_secret}"},
                    data={"content": "iso"},
                    files={"filename": ("autounattend.iso", fh, "application/x-iso9660-image")},
                    verify=cfg.get("proxmox_validate_certs", False),
                    timeout=60,
                )
        except Exception as e:
            return {"ok": False, "error": f"upload failed: {e}"}
        if resp.status_code == 403:
            return {
                "ok": False,
                "error": (
                    "403 Forbidden from Proxmox. The API token needs "
                    "Datastore.AllocateTemplate on /storage/"
                    f"{iso_storage} (role PVEDatastoreUser or similar). "
                    "In Proxmox UI: Datacenter → Permissions → API Tokens, "
                    "or Datacenter → Permissions → Add → API Token "
                    "Permission."
                ),
            }
        if resp.status_code >= 400:
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:500]}"}

    return {
        "ok": True,
        "bytes": xml_src.stat().st_size,
        "storage": iso_storage,
        "node": node,
        "path": f"{iso_storage}:iso/autounattend.iso",
    }


# --- Ubuntu seed ISO rebuild ----------------------------------------------


def _referenced_credential_ids(steps: list[dict]) -> set[int]:
    """Return the set of credential IDs referenced by any *_credential_id param."""
    out: set[int] = set()
    for s in steps:
        params = (s.get("params") or {})
        for k, v in params.items():
            if k.endswith("_credential_id") and isinstance(v, int) and v > 0:
                out.add(v)
    return out


@app.post("/api/ubuntu/rebuild-seed-iso")
async def rebuild_ubuntu_seed_iso(sequence_id: int):
    """Compile the given Ubuntu sequence, build a NoCloud seed ISO, and upload
    to Proxmox ISO storage. Mirrors /api/answer-iso/rebuild for the Windows
    unattend flow."""
    seq = sequences_db.get_sequence(SEQUENCES_DB, sequence_id)
    if seq is None:
        return JSONResponse(
            {"ok": False, "error": f"sequence {sequence_id} not found"},
            status_code=404,
        )
    if seq.get("target_os") != "ubuntu":
        return JSONResponse(
            {"ok": False,
             "error": f"sequence target_os is {seq.get('target_os')!r}, not ubuntu"},
            status_code=400,
        )

    # Imported lazily so a broken compiler dependency only breaks the Ubuntu
    # path (not the 404/400 early returns above).
    import tempfile as _tmp

    from web.ubuntu_compiler import compile_sequence, UbuntuCompileError
    from web.ubuntu_seed_iso import build_seed_iso

    # Decrypt referenced credentials up front so the compiler can inline secrets.
    cipher = _cipher()
    credentials: dict[int, dict] = {}
    for cid in _referenced_credential_ids(seq["steps"]):
        row = sequences_db.get_credential(SEQUENCES_DB, cipher, cid)
        if row is None:
            continue
        credentials[cid] = row.get("payload") or {}

    # Inject global Ubuntu-build overrides from vars.yml into step params.
    # Currently: ubuntu_apt_proxy — if the operator has a LAN apt-cacher,
    # every build uses it without the user having to edit each sequence.
    _vars = _load_vars() or {}
    _apt_proxy = (_vars.get("ubuntu_apt_proxy") or "").strip()
    steps = [dict(s) for s in seq["steps"]]  # shallow copy so we don't mutate DB snapshot
    if _apt_proxy:
        for s in steps:
            if s.get("step_type") == "install_ubuntu_core":
                params = dict(s.get("params") or {})
                params.setdefault("apt_proxy", _apt_proxy)
                s["params"] = params

    try:
        user_data, meta_data, _fb_user, _fb_meta = compile_sequence(
            steps=steps,
            credentials=credentials,
            instance_id=f"seq-{sequence_id}",
            hostname="ubuntu-template",
        )
    except UbuntuCompileError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    cfg = _load_proxmox_config()
    iso_storage = cfg.get("proxmox_iso_storage") or "isos"
    node = cfg.get("proxmox_node", "pve")
    iso_name = "ubuntu-seed.iso"

    with _tmp.TemporaryDirectory() as td:
        iso_path = Path(td) / iso_name
        try:
            build_seed_iso(
                user_data=user_data, meta_data=meta_data, out_path=iso_path,
            )
        except RuntimeError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

        host = cfg.get("proxmox_host", "")
        port = cfg.get("proxmox_port", 8006)
        token_id = cfg.get("vault_proxmox_api_token_id", "")
        token_secret = cfg.get("vault_proxmox_api_token_secret", "")
        url = (
            f"https://{host}:{port}/api2/json/nodes/{node}"
            f"/storage/{iso_storage}/upload"
        )
        try:
            with open(iso_path, "rb") as fh:
                resp = requests.post(
                    url,
                    headers={"Authorization": f"PVEAPIToken={token_id}={token_secret}"},
                    data={"content": "iso"},
                    files={"filename": (iso_name, fh, "application/x-iso9660-image")},
                    verify=cfg.get("proxmox_validate_certs", False),
                    timeout=60,
                )
        except Exception as e:
            return JSONResponse(
                {"ok": False, "error": f"upload failed: {e}"}, status_code=500,
            )
        if resp.status_code == 403:
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "403 Forbidden from Proxmox. The API token needs "
                        "Datastore.AllocateTemplate on /storage/"
                        f"{iso_storage} (role PVEDatastoreUser or similar)."
                    ),
                },
                status_code=403,
            )
        if resp.status_code >= 400:
            return JSONResponse(
                {"ok": False,
                 "error": f"HTTP {resp.status_code}: {resp.text[:500]}"},
                status_code=502,
            )

    return {
        "ok": True,
        "iso": f"{iso_storage}:iso/{iso_name}",
        "storage": iso_storage,
        "node": node,
    }


@app.post("/api/ubuntu/per-vm-seed")
async def build_per_vm_seed(vmid: int, sequence_id: int, hostname: str):
    """Build the per-clone NoCloud seed ISO for a specific VMID.

    Unlike /api/ubuntu/rebuild-seed-iso (which emits the template seed ISO
    carrying the full cloud-init user-data body), this endpoint emits a
    minimal cloud-init (hostname + firstboot runcmd) keyed to the target VM.
    Called by the Ansible provisioning role for each cloned VM.
    """
    seq = sequences_db.get_sequence(SEQUENCES_DB, sequence_id)
    if seq is None:
        return JSONResponse(
            {"ok": False, "error": f"sequence {sequence_id} not found"},
            status_code=404,
        )
    if seq.get("target_os") != "ubuntu":
        return JSONResponse(
            {"ok": False,
             "error": f"sequence target_os is {seq.get('target_os')!r}, not ubuntu"},
            status_code=400,
        )

    import tempfile as _tmp

    from web.ubuntu_compiler import compile_sequence, UbuntuCompileError
    from web.ubuntu_seed_iso import build_seed_iso

    cipher = _cipher()
    credentials: dict[int, dict] = {}
    for cid in _referenced_credential_ids(seq["steps"]):
        row = sequences_db.get_credential(SEQUENCES_DB, cipher, cid)
        if row is None:
            continue
        credentials[cid] = row.get("payload") or {}

    try:
        _u, _m, firstboot_user_data, firstboot_meta_data = compile_sequence(
            steps=seq["steps"],
            credentials=credentials,
            instance_id=f"vm-{vmid}",
            hostname=hostname,
        )
    except UbuntuCompileError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    cfg = _load_proxmox_config()
    iso_storage = cfg.get("proxmox_iso_storage") or "isos"
    node = cfg.get("proxmox_node", "pve")
    iso_name = f"ubuntu-per-vm-{vmid}.iso"

    with _tmp.TemporaryDirectory() as td:
        iso_path = Path(td) / iso_name
        try:
            build_seed_iso(
                user_data=firstboot_user_data,
                meta_data=firstboot_meta_data,
                out_path=iso_path,
            )
        except RuntimeError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

        host = cfg.get("proxmox_host", "")
        port = cfg.get("proxmox_port", 8006)
        token_id = cfg.get("vault_proxmox_api_token_id", "")
        token_secret = cfg.get("vault_proxmox_api_token_secret", "")
        url = (
            f"https://{host}:{port}/api2/json/nodes/{node}"
            f"/storage/{iso_storage}/upload"
        )
        try:
            with open(iso_path, "rb") as fh:
                resp = requests.post(
                    url,
                    headers={"Authorization": f"PVEAPIToken={token_id}={token_secret}"},
                    data={"content": "iso"},
                    files={"filename": (iso_name, fh, "application/x-iso9660-image")},
                    verify=cfg.get("proxmox_validate_certs", False),
                    timeout=60,
                )
        except Exception as e:
            return JSONResponse(
                {"ok": False, "error": f"upload failed: {e}"}, status_code=500,
            )
        if resp.status_code == 403:
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "403 Forbidden from Proxmox. The API token needs "
                        "Datastore.AllocateTemplate on /storage/"
                        f"{iso_storage} (role PVEDatastoreUser or similar)."
                    ),
                },
                status_code=403,
            )
        if resp.status_code >= 400:
            return JSONResponse(
                {"ok": False,
                 "error": f"HTTP {resp.status_code}: {resp.text[:500]}"},
                status_code=502,
            )

    return {
        "ok": True,
        "iso": f"{iso_storage}:iso/{iso_name}",
        "storage": iso_storage,
        "node": node,
        "vmid": vmid,
    }


# --- Version / update check ------------------------------------------------

_GITHUB_REPO = "adamgell/ProxmoxVEAutopilot"
_LATEST_VERSION_TTL = 300  # seconds — cache GitHub response for 5 min


def _fetch_latest_main_sha():
    """Return the SHA of origin/main from GitHub. Cached to respect rate limits."""
    now = time.time()
    cache = _LATEST_VERSION_CACHE
    if cache["sha"] and (now - cache["fetched_at"]) < _LATEST_VERSION_TTL:
        return cache
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{_GITHUB_REPO}/commits/main",
            headers={"Accept": "application/vnd.github+json"},
            timeout=8,
        )
        resp.raise_for_status()
        sha = resp.json().get("sha", "")
        cache.update({
            "fetched_at": now, "sha": sha, "sha_short": sha[:7],
            "error": None,
        })
    except Exception as e:
        cache.update({"fetched_at": now, "error": str(e)[:200]})
    return cache


# ---------------------------------------------------------------------------
# Self-update endpoint
# ---------------------------------------------------------------------------

_UPDATE_LOG = BASE_DIR / "output" / "update.log"
_UPDATE_SIDECAR_NAME = "autopilot-updater"


def _host_repo_path() -> str:
    """Absolute path on the host of the cloned repo. docker-compose.yml
    bind-mounts it at /host/repo inside us; the sidecar needs the
    *host-side* path because its docker.sock calls get translated
    against the host's filesystem namespace."""
    return os.environ.get("HOST_REPO_PATH", "/opt/ProxmoxVEAutopilot")


@app.post("/api/update/run")
async def api_update_run():
    """Start a self-update. Spawns a detached sidecar container that
    runs `git pull && docker compose pull && docker compose up -d`
    (no service arg — rolls web + builder + monitor together). The
    sidecar lives beyond our own restart — by the time docker-compose
    kills us and starts a new container, the sidecar has already
    finished and removed itself.

    Returns 202 with the sidecar container id. Poll /api/update/status
    for progress; on success the browser can reload to see the new
    build SHA in the footer.
    """
    try:
        import docker as _docker
    except ImportError:
        raise HTTPException(500, "docker-py not installed in image")

    # Don't double-spawn if one is already running.
    try:
        client = _docker.from_env()
        client.ping()
    except Exception as e:
        raise HTTPException(
            500,
            f"cannot reach docker socket ({e}); confirm "
            "/var/run/docker.sock is mounted into this container",
        ) from e

    for existing in client.containers.list(all=True, filters={"name": _UPDATE_SIDECAR_NAME}):
        # If it's still running, report back. If it already exited but
        # was left around, nuke the carcass so we can start fresh.
        if existing.status == "running":
            return JSONResponse(
                status_code=202,
                content={
                    "ok": True, "already_running": True,
                    "container_id": existing.short_id,
                },
            )
        try:
            existing.remove()
        except Exception:
            pass

    _UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    _UPDATE_LOG.write_text(
        f"update started {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
    )

    # The sidecar uses docker:27-cli (includes `docker` + compose
    # plugin). `apk add git` adds git (~5MB) on the fly.
    #
    # CRITICAL bind-mount note: the sidecar mounts the repo at the
    # SAME host path it lives at on the host. When `docker compose
    # up -d` inside the sidecar issues a container-create request
    # over the shared docker.sock, the daemon resolves the compose
    # file's `./inventory/...` relative paths against the SIDECAR's
    # CWD — but the resulting absolute paths are then sent to the
    # daemon, which interprets them against the HOST filesystem.
    # If the sidecar CWD was `/repo/autopilot-proxmox` the daemon
    # would try to bind `/repo/autopilot-proxmox/inventory/...`
    # which doesn't exist on the host. Mounting at the same path
    # makes the resolution agree with the host.
    host_repo = _host_repo_path()
    cmd = (
        "set -euo pipefail; "
        "apk add --no-cache git >/dev/null 2>&1 || apk add --no-cache git; "
        f"cd {host_repo} && "
        "echo '--- git pull ---' && git pull && "
        "cd autopilot-proxmox && "
        "echo '--- docker compose pull ---' && docker compose pull && "
        "echo '--- docker compose up -d ---' && docker compose up -d && "
        "echo '--- done ---'"
    )
    run_kwargs = dict(
        image="docker:27-cli",
        command=["sh", "-c", cmd],
        detach=True,
        remove=True,
        name=_UPDATE_SIDECAR_NAME,
        volumes={
            "/var/run/docker.sock": {
                "bind": "/var/run/docker.sock", "mode": "rw",
            },
            # Same-path bind — see note above. The sidecar sees the
            # repo at exactly the host path so compose's relative
            # mounts resolve identically on both sides of the socket.
            host_repo: {"bind": host_repo, "mode": "rw"},
        },
        # Host network so `git pull` resolves github.com the same way
        # our own container does. The default bridge network inherits
        # the docker daemon's DNS, which on the autopilot host doesn't
        # have /etc/resolv.conf pointed anywhere useful.
        network_mode="host",
        labels={"app": "autopilot-updater"},
    )
    try:
        container = client.containers.run(**run_kwargs)
    except _docker.errors.ImageNotFound:
        # First run — pull the sidecar image and retry.
        client.images.pull("docker:27-cli")
        container = client.containers.run(**run_kwargs)

    # Stream logs in a background thread until container finishes so
    # /api/update/status can tail /app/output/update.log.
    def _stream_logs(cid: str):
        try:
            c = client.containers.get(cid)
            for chunk in c.logs(stream=True, follow=True):
                try:
                    with open(_UPDATE_LOG, "ab") as f:
                        f.write(chunk)
                except Exception:
                    break
        except Exception:
            pass

    import threading
    threading.Thread(
        target=_stream_logs, args=(container.id,), daemon=True,
    ).start()

    return JSONResponse(
        status_code=202,
        content={"ok": True, "container_id": container.short_id},
    )


@app.get("/api/update/status")
def api_update_status():
    """Report on the most recent update run. Returns the tail of
    update.log + whether the sidecar is still running."""
    log_text = ""
    if _UPDATE_LOG.exists():
        try:
            log_text = _UPDATE_LOG.read_text()[-4000:]
        except Exception as e:
            log_text = f"(log read failed: {e})"
    running = False
    try:
        import docker as _docker
        client = _docker.from_env()
        for c in client.containers.list(filters={"name": _UPDATE_SIDECAR_NAME}):
            if c.status == "running":
                running = True
                break
    except Exception:
        pass
    # "done" heuristic: the sidecar's final echo line lands in the log.
    finished = "--- done ---" in log_text
    return {
        "running": running,
        "finished": finished and not running,
        "log": log_text,
    }


@app.get("/api/version")
async def api_version(check: bool = False):
    """Return running build SHA + timestamp. With ?check=1, also query GitHub
    for origin/main HEAD and report whether an update is available."""
    out = {
        "running": _APP_VERSION,
    }
    if check:
        latest = _fetch_latest_main_sha()
        out["latest"] = {
            "sha": latest.get("sha"),
            "sha_short": latest.get("sha_short"),
            "fetched_at": latest.get("fetched_at"),
            "error": latest.get("error"),
        }
        running_sha = _APP_VERSION.get("sha", "")
        latest_sha = latest.get("sha") or ""
        if running_sha and latest_sha and running_sha != "unknown":
            out["update_available"] = running_sha != latest_sha
        else:
            out["update_available"] = None
    return out


# --- Cloud device inventory (Autopilot / Intune / Entra) -------------------

_GRAPH_DELETE_PATHS = {
    "autopilot": "/deviceManagement/windowsAutopilotDeviceIdentities/{id}",
    "intune":    "/deviceManagement/managedDevices/{id}",
    "entra":     "/devices/{id}",
}
# Deletion order matters for managed devices:
#   0. Proxmox VM first — stops the device agent before we start removing
#      cloud records, so it can't re-check-in and recreate Entra/Intune state
#   1. Intune retire/delete tears down the MDM channel
#   2. Autopilot deviceIdentity removes the deployment record
#   3. Entra directory object goes last (Intune sync can recreate it otherwise)
_CLOUD_DELETE_ORDER = {"pve": 0, "intune": 1, "autopilot": 2, "entra": 3}
# Verification budget (seconds) per source. Autopilot DELETE is 202-Accepted
# and async — Microsoft's background workflow can take anywhere from a few
# minutes to well over an hour to propagate. Intune and Entra are typically
# 204-No Content and near-immediate, but we still give a generous buffer.
# Users can close the browser; the job keeps running in the background.
_VERIFY_MAX_SECONDS = {
    "autopilot": 7200,   # 2 hours
    "intune":    600,    # 10 minutes
    "entra":     600,    # 10 minutes
}
# Cap for the polling backoff. Long-running Autopilot verifications settle
# into a steady 60s cadence so we're kind to Graph without slowing detection
# of a delete that lands at minute 45.
_VERIFY_BACKOFF_CAP = 60.0

# In-memory job store for cloud deletion runs.
_cloud_delete_jobs: dict[str, dict] = {}


def _graph_get_raw(path):
    """GET returning (status_code, json). Used for post-delete verification
    — a 404 proves the object is really gone from Graph."""
    token = _graph_token()
    if not token:
        return 0, None
    resp = requests.get(
        f"https://graph.microsoft.com/beta{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    try:
        return resp.status_code, resp.json() if resp.content else None
    except ValueError:
        return resp.status_code, None


def _graph_delete_raw(path):
    """DELETE returning (status_code, error_text). Does not raise so the
    caller can distinguish between 'already gone' (404), 'delete pending
    from a prior request' (400 on Autopilot), and real failures."""
    token = _graph_token()
    if not token:
        return 0, "no credentials configured"
    resp = requests.delete(
        f"https://graph.microsoft.com/beta{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    err = ""
    if resp.status_code >= 400 and resp.content:
        try:
            j = resp.json()
            err = j.get("error", {}).get("message", "") or resp.text[:300]
        except ValueError:
            err = resp.text[:300]
    return resp.status_code, err


def _pve_autopilot_vms_by_serial() -> dict[str, dict]:
    """Return {serial: {vmid, name, status}} for all autopilot-tagged, non-
    template VMs on the configured node. Serial is matched against VM name
    (lab VMs are provisioned with name == device serial). One API call."""
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        vms = _proxmox_api(f"/nodes/{node}/qemu")
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for vm in vms:
        if vm.get("template"):
            continue
        tags = (vm.get("tags") or "").split(";")
        if "autopilot" not in tags:
            continue
        name = vm.get("name", "")
        if not name:
            continue
        out[name] = {
            "vmid": vm["vmid"],
            "name": name,
            "status": vm.get("status", ""),
        }
    return out


def _find_pve_vm_by_serial(serial: str) -> dict | None:
    """Return the Proxmox VM whose name matches `serial` and carries the
    'autopilot' tag. Returns None if no match — refusing to touch VMs that
    weren't provisioned by us guards against name collisions."""
    if not serial:
        return None
    return _pve_autopilot_vms_by_serial().get(serial)


async def _delete_pve_item(item: dict) -> None:
    """Stop (if running) + delete a Proxmox VM. Sets state to verifying on
    success; the verify step confirms the VM is gone from the node listing."""
    import asyncio
    item["state"] = "deleting"
    item["started_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    vmid = item["object_id"]
    try:
        # Best-effort stop — ignore failures (VM may already be stopped).
        try:
            await asyncio.to_thread(
                _proxmox_api_post, f"/nodes/{node}/qemu/{vmid}/status/stop"
            )
            await asyncio.sleep(3)
        except Exception:
            pass
        await asyncio.to_thread(_proxmox_api_delete, f"/nodes/{node}/qemu/{vmid}")
        item["state"] = "verifying"
        item["message"] = "delete submitted, verifying…"
    except Exception as e:
        item["state"] = "error"
        item["message"] = f"Proxmox delete failed: {str(e)[:400]}"
        devices_db.record_deletion(
            DEVICES_DB, source="pve", object_id=str(vmid),
            serial=item.get("serial", ""), display_name=item.get("display_name", ""),
            status="error", message=item["message"],
        )


async def _verify_pve_item(item: dict) -> None:
    """Poll the node's VM list until the VMID is gone."""
    import asyncio
    try:
        target_vmid = int(item["object_id"])
    except (TypeError, ValueError):
        item["state"] = "error"
        item["message"] = f"invalid VMID {item.get('object_id')!r}"
        return
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    deadline = asyncio.get_event_loop().time() + 180  # 3 minutes
    delay = 2.0
    attempts = 0
    while True:
        await asyncio.sleep(delay)
        attempts += 1
        try:
            vms = await asyncio.to_thread(_proxmox_api, f"/nodes/{node}/qemu")
            still_there = any(v.get("vmid") == target_vmid for v in vms)
        except Exception:
            still_there = False  # API error — assume gone; next sync will clarify
        elapsed = int(180 - (deadline - asyncio.get_event_loop().time()))
        item["message"] = f"verifying… attempt {attempts}, {elapsed}s elapsed"
        if not still_there:
            item["state"] = "deleted"
            item["message"] = f"VM removed, verified after {attempts} attempt(s)"
            devices_db.record_deletion(
                DEVICES_DB, source="pve", object_id=str(target_vmid),
                serial=item.get("serial", ""), display_name=item.get("display_name", ""),
                status="ok",
            )
            break
        if asyncio.get_event_loop().time() >= deadline:
            item["state"] = "unverified"
            item["message"] = f"VM still present in node listing after {attempts} attempt(s)"
            devices_db.record_deletion(
                DEVICES_DB, source="pve", object_id=str(target_vmid),
                serial=item.get("serial", ""), display_name=item.get("display_name", ""),
                status="ok", message=item["message"],
            )
            break
        delay = min(delay * 1.5, 15.0)
    item["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _delete_item(item: dict) -> None:
    """Fire the Graph DELETE and transition the item to verifying/deleted/error.
    Does not verify — call _verify_item afterwards if state is 'verifying'."""
    import asyncio
    if item["source"] == "pve":
        return await _delete_pve_item(item)
    item["state"] = "deleting"
    item["started_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path_tmpl = _GRAPH_DELETE_PATHS.get(item["source"])
    if not path_tmpl:
        item["state"] = "error"
        item["message"] = f"unknown source {item['source']}"
        return
    path = path_tmpl.format(id=item["object_id"])
    status, err_text = await asyncio.to_thread(_graph_delete_raw, path)

    if status == 404:
        item["state"] = "deleted"
        item["message"] = "already gone (404)"
        item["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        devices_db.record_deletion(
            DEVICES_DB, source=item["source"], object_id=item["object_id"],
            serial=item.get("serial", ""), display_name=item.get("display_name", ""),
            status="ok", message="already gone",
        )
    elif 200 <= status < 300:
        item["state"] = "verifying"
        item["message"] = f"DELETE returned {status}, verifying…"
    elif status == 400:
        # Graph returns 400 when a prior async DELETE on the same object is
        # still pending. Not a real failure — drop into the verify loop.
        item["state"] = "verifying"
        item["message"] = f"DELETE returned 400 (likely pending from prior request: {err_text[:120]}); verifying…"
    else:
        item["state"] = "error"
        item["message"] = f"HTTP {status}: {err_text[:400]}"
        devices_db.record_deletion(
            DEVICES_DB, source=item["source"], object_id=item["object_id"],
            serial=item.get("serial", ""), display_name=item.get("display_name", ""),
            status="error", message=item["message"],
        )


async def _verify_item(item: dict) -> None:
    """Poll Graph until the object 404s (verified deleted) or we exhaust the
    per-source budget. Only called when _delete_item left state='verifying'."""
    import asyncio
    if item["source"] == "pve":
        return await _verify_pve_item(item)
    path = _GRAPH_DELETE_PATHS[item["source"]].format(id=item["object_id"])
    budget = _VERIFY_MAX_SECONDS.get(item["source"], 180)
    deadline = asyncio.get_event_loop().time() + budget
    delay = 2.0
    attempts = 0
    verified = False
    final_status = 0
    while True:
        await asyncio.sleep(delay)
        attempts += 1
        elapsed = int(budget - (deadline - asyncio.get_event_loop().time()))
        item["message"] = f"verifying… attempt {attempts}, {elapsed}s elapsed"
        status, _ = await asyncio.to_thread(_graph_get_raw, path)
        final_status = status
        if status == 404:
            verified = True
            break
        if asyncio.get_event_loop().time() >= deadline:
            break
        delay = min(delay * 2, _VERIFY_BACKOFF_CAP)

    if verified:
        item["state"] = "deleted"
        item["message"] = f"verified 404 after {attempts} attempt(s)"
        devices_db.record_deletion(
            DEVICES_DB, source=item["source"], object_id=item["object_id"],
            serial=item.get("serial", ""), display_name=item.get("display_name", ""),
            status="ok",
        )
    else:
        item["state"] = "unverified"
        item["message"] = (
            f"delete accepted but GET still returned {final_status} "
            f"after {attempts} attempt(s) over {budget}s"
        )
        devices_db.record_deletion(
            DEVICES_DB, source=item["source"], object_id=item["object_id"],
            serial=item.get("serial", ""), display_name=item.get("display_name", ""),
            status="ok", message=item["message"],
        )
    item["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _run_cloud_delete(job_id: str):
    """Execute a cloud-delete job with per-source phasing and parallelism.

    Ordering: Intune → Autopilot → Entra. Within each phase all DELETEs fire
    in parallel; we wait only for the DELETE responses before advancing to
    the next phase. Verification polling from an earlier phase runs as
    background tasks alongside the next phase's DELETEs.
    """
    import asyncio
    job = _cloud_delete_jobs[job_id]
    job["state"] = "running"

    # Bucket items by source, preserving input order within each bucket.
    by_source: dict[str, list[dict]] = {"pve": [], "intune": [], "autopilot": [], "entra": []}
    for item in job["items"]:
        if item["source"] in by_source:
            by_source[item["source"]].append(item)

    verify_tasks: list[asyncio.Task] = []
    try:
        for source_name in ("pve", "intune", "autopilot", "entra"):
            phase = by_source[source_name]
            if not phase:
                continue
            # Fire all DELETEs in this phase concurrently and wait for the
            # HTTP responses. This is seconds, not minutes.
            await asyncio.gather(*[_delete_item(item) for item in phase])
            # Launch verification tasks without awaiting so the next phase
            # can start while prior verifications keep polling in parallel.
            for item in phase:
                if item["state"] == "verifying":
                    verify_tasks.append(asyncio.create_task(_verify_item(item)))

        if verify_tasks:
            await asyncio.gather(*verify_tasks, return_exceptions=True)
    finally:
        job["state"] = "done"
        job["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.get("/cloud", response_class=HTMLResponse)
async def cloud_page(request: Request):
    # ?all=1 disables the Windows-only filter so iOS/Android/macOS records
    # can be inspected or deleted if needed.
    windows_only = request.query_params.get("all") != "1"
    groups, extra = devices_db.list_grouped(DEVICES_DB, windows_only=windows_only)

    # One Proxmox API call to map serial → {vmid, status}. Attach to each
    # group so the UI can show a 'pve' line alongside the other IDs.
    pve_by_serial = _pve_autopilot_vms_by_serial()
    for g in groups:
        g["pve"] = pve_by_serial.get(g["serial"])

    return templates.TemplateResponse(
        "devices.html",
        {
            "request": request,
            "groups": groups,
            "unmatched": extra["unmatched"],
            "meta": extra["meta"],
            "windows_only": windows_only,
            "deletions": devices_db.recent_deletions(DEVICES_DB, limit=25),
        },
    )


@app.post("/api/cloud/sync")
async def cloud_sync():
    try:
        ap = _graph_api_all("/deviceManagement/windowsAutopilotDeviceIdentities") or []
        it = _graph_api_all("/deviceManagement/managedDevices") or []
        en = _graph_api_all("/devices") or []
    except Exception as e:
        return _redirect_with_error("/cloud", str(e)[:200])
    devices_db.upsert_autopilot(DEVICES_DB, ap)
    devices_db.upsert_intune(DEVICES_DB, it)
    devices_db.upsert_entra(DEVICES_DB, en)
    return RedirectResponse(
        f"/cloud?synced=autopilot:{len(ap)},intune:{len(it)},entra:{len(en)}",
        status_code=303,
    )


@app.post("/api/cloud/delete")
async def cloud_delete(
    targets: list[str] = Form(default=[]),
    pve_serials: list[str] = Form(default=[]),
):
    """Start a background deletion job. Returns {job_id}; poll /api/cloud/delete/{job_id}.

    `pve_serials` is an optional list of device serials — for each one, we
    look up the matching Proxmox VM (by name and 'autopilot' tag) and
    prepend a 'pve' deletion item so the VM is stopped and destroyed before
    cloud records are torn down.
    """
    import asyncio, uuid

    # Parse targets, dedupe, sort by required Intune → Autopilot → Entra order.
    parsed: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for raw in targets:
        if ":" not in raw:
            continue
        source, object_id = raw.split(":", 1)
        if source not in _CLOUD_DELETE_ORDER or not object_id or source == "pve":
            continue  # 'pve' targets only come in via pve_serials lookup
        key = (source, object_id)
        if key in seen:
            continue
        seen.add(key)
        parsed.append({
            "source": source,
            "object_id": object_id,
            "state": "pending",
            "message": "",
        })

    # Look up Proxmox VMs for each unique serial (silently skip serials with
    # no autopilot-tagged match — real/physical devices don't have a VM).
    unique_serials = sorted({s for s in pve_serials if s})
    for serial in unique_serials:
        vm = _find_pve_vm_by_serial(serial)
        if not vm:
            continue
        key = ("pve", str(vm["vmid"]))
        if key in seen:
            continue
        seen.add(key)
        parsed.append({
            "source": "pve",
            "object_id": str(vm["vmid"]),
            "serial": serial,
            "display_name": f"VM {vm['vmid']} ({vm['name']})",
            "state": "pending",
            "message": "",
        })

    parsed.sort(key=lambda x: _CLOUD_DELETE_ORDER[x["source"]])

    job_id = uuid.uuid4().hex[:12]
    _cloud_delete_jobs[job_id] = {
        "id": job_id,
        "state": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": parsed,
    }
    asyncio.create_task(_run_cloud_delete(job_id))
    return {"job_id": job_id, "count": len(parsed), "items": parsed}


@app.get("/api/cloud/delete/{job_id}")
async def cloud_delete_status(job_id: str):
    job = _cloud_delete_jobs.get(job_id)
    if not job:
        return {"error": "not found"}
    items = job["items"]
    summary = {
        "pending":    sum(1 for i in items if i["state"] == "pending"),
        "deleting":   sum(1 for i in items if i["state"] == "deleting"),
        "verifying":  sum(1 for i in items if i["state"] == "verifying"),
        "deleted":    sum(1 for i in items if i["state"] == "deleted"),
        "unverified": sum(1 for i in items if i["state"] == "unverified"),
        "error":      sum(1 for i in items if i["state"] == "error"),
        "total":      len(items),
    }
    return {**job, "summary": summary}


@app.websocket("/api/jobs/{job_id}/stream")
async def job_stream(websocket: WebSocket, job_id: str):
    await websocket.accept()
    log_path = Path(job_manager.jobs_dir) / f"{job_id}.log"
    offset = 0
    try:
        while True:
            if log_path.exists():
                with open(log_path) as f:
                    f.seek(offset)
                    new_data = f.read()
                    if new_data:
                        await websocket.send_text(new_data)
                        offset += len(new_data)
            if not job_manager.is_running(job_id):
                if log_path.exists():
                    with open(log_path) as f:
                        f.seek(offset)
                        remaining = f.read()
                        if remaining:
                            await websocket.send_text(remaining)
                await websocket.close()
                break
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass


class _CredentialCreate(BaseModel):
    name: str
    type: str
    payload: dict


class _CredentialUpdate(BaseModel):
    name: Optional[str] = None
    payload: Optional[dict] = None


@app.get("/api/credentials")
def api_credentials_list(type: Optional[str] = None):
    return sequences_db.list_credentials(SEQUENCES_DB, type=type)


@app.get("/api/credentials/{cred_id}")
def api_credentials_get(cred_id: int):
    cred = sequences_db.get_credential(SEQUENCES_DB, _cipher(), cred_id)
    if cred is None:
        raise HTTPException(404, "credential not found")
    return cred


@app.post("/api/credentials", status_code=201)
async def api_credentials_create(request: Request):
    """Create a credential.

    Accepts either JSON ``{name, type, payload}`` (existing types) or
    multipart/form-data with a file field (``mde_onboarding`` uploads the
    onboarding .py script directly). For multipart the file is base64-encoded
    and wrapped in the canonical payload shape before encryption.
    """
    ctype = (request.headers.get("content-type") or "").lower()
    if ctype.startswith("multipart/"):
        form = await request.form()
        name = form.get("name", "")
        cred_type = form.get("type", "")
        if not name or not cred_type:
            raise HTTPException(400, "name and type are required")
        try:
            payload = await _payload_from_form(cred_type, form)
        except ValueError as e:
            raise HTTPException(400, str(e))
    else:
        body = _CredentialCreate.model_validate(await request.json())
        name = body.name
        cred_type = body.type
        payload = body.payload

    if cred_type not in {"domain_join", "local_admin", "odj_blob", "mde_onboarding"}:
        raise HTTPException(400, f"unknown credential type: {cred_type}")
    try:
        cid = sequences_db.create_credential(
            SEQUENCES_DB, _cipher(),
            name=name, type=cred_type, payload=payload,
        )
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"credential name already exists: {name}")
        raise
    return {"id": cid}


@app.patch("/api/credentials/{cred_id}")
def api_credentials_update(cred_id: int, body: _CredentialUpdate):
    existing = sequences_db.get_credential(SEQUENCES_DB, _cipher(), cred_id)
    if existing is None:
        raise HTTPException(404, "credential not found")
    try:
        sequences_db.update_credential(
            SEQUENCES_DB, _cipher(), cred_id,
            name=body.name, payload=body.payload,
        )
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"credential name already exists: {body.name}")
        raise
    return {"ok": True}


@app.delete("/api/credentials/{cred_id}")
def api_credentials_delete(cred_id: int):
    try:
        sequences_db.delete_credential(SEQUENCES_DB, cred_id)
    except sequences_db.CredentialInUse as e:
        return JSONResponse(status_code=409, content={
            "error": "credential is in use",
            "sequence_ids": e.sequence_ids,
        })
    return {"ok": True}


class _StepIn(BaseModel):
    step_type: str
    params: dict = {}
    enabled: bool = True


class _SequenceCreate(BaseModel):
    name: str
    description: str = ""
    target_os: str = "windows"
    is_default: bool = False
    produces_autopilot_hash: bool = False
    steps: list[_StepIn] = []


class _SequenceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    target_os: Optional[str] = None
    is_default: Optional[bool] = None
    produces_autopilot_hash: Optional[bool] = None
    steps: Optional[list[_StepIn]] = None


class _DuplicateReq(BaseModel):
    new_name: str


@app.get("/api/sequences")
def api_sequences_list():
    return sequences_db.list_sequences(SEQUENCES_DB)


@app.get("/api/sequences/{seq_id}")
def api_sequences_get(seq_id: int):
    seq = sequences_db.get_sequence(SEQUENCES_DB, seq_id)
    if seq is None:
        raise HTTPException(404, "sequence not found")
    return seq


@app.post("/api/sequences", status_code=201)
def api_sequences_create(body: _SequenceCreate):
    try:
        sid = sequences_db.create_sequence(
            SEQUENCES_DB,
            name=body.name, description=body.description,
            is_default=body.is_default,
            produces_autopilot_hash=body.produces_autopilot_hash,
            target_os=body.target_os,
        )
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"sequence name already exists: {body.name}")
        raise
    sequences_db.set_sequence_steps(
        SEQUENCES_DB, sid,
        [s.model_dump() for s in body.steps],
    )
    return {"id": sid}


@app.put("/api/sequences/{seq_id}")
def api_sequences_update(seq_id: int, body: _SequenceUpdate):
    existing = sequences_db.get_sequence(SEQUENCES_DB, seq_id)
    if existing is None:
        raise HTTPException(404, "sequence not found")
    try:
        sequences_db.update_sequence(
            SEQUENCES_DB, seq_id,
            name=body.name, description=body.description,
            is_default=body.is_default,
            produces_autopilot_hash=body.produces_autopilot_hash,
            target_os=body.target_os,
        )
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"sequence name already exists: {body.name}")
        raise
    if body.steps is not None:
        sequences_db.set_sequence_steps(
            SEQUENCES_DB, seq_id,
            [s.model_dump() for s in body.steps],
        )
    return {"ok": True}


@app.post("/api/sequences/{seq_id}/duplicate", status_code=201)
def api_sequences_duplicate(seq_id: int, body: _DuplicateReq):
    existing = sequences_db.get_sequence(SEQUENCES_DB, seq_id)
    if existing is None:
        raise HTTPException(404, "sequence not found")
    try:
        new_id = sequences_db.duplicate_sequence(
            SEQUENCES_DB, seq_id, new_name=body.new_name,
        )
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"sequence name already exists: {body.new_name}")
        raise
    return {"id": new_id}


@app.delete("/api/sequences/{seq_id}")
def api_sequences_delete(seq_id: int):
    try:
        sequences_db.delete_sequence(SEQUENCES_DB, seq_id)
    except sequences_db.SequenceInUse as e:
        raise HTTPException(409, detail={
            "error": "sequence is referenced by provisioned VMs",
            "vmids": e.vmids,
        })
    return {"ok": True}


def _referenced_answer_floppy_paths() -> set[str]:
    """Return every per-VM answer-floppy path currently referenced by
    an ``args: -drive if=floppy,...,file=<path>`` in any autopilot-
    tagged VM config. Non-fatal on API errors."""
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        vms = _proxmox_api(f"/nodes/{node}/qemu") or []
    except Exception:
        return set()
    refs: set[str] = set()
    import re
    pat = re.compile(r"file=(/var/lib/vz/snippets/autopilot-unattend-[0-9a-f]+\.img)")
    for vm in vms:
        vmid = vm.get("vmid")
        if vmid is None:
            continue
        try:
            conf = _proxmox_api(f"/nodes/{node}/qemu/{vmid}/config") or {}
        except Exception:
            continue
        args_str = conf.get("args") or ""
        for m in pat.finditer(args_str):
            refs.add(m.group(1))
    return refs


def _make_root_ssh_runner():
    """Construct the SshRunner used by the floppy cache. None when the
    root password isn't configured (prune/list still work, just without
    the ability to verify/remove remote files)."""
    cfg = _load_proxmox_config()
    pw = cfg.get("vault_proxmox_root_password") or ""
    if not pw:
        return None
    from web import answer_floppy_cache
    user = (cfg.get("vault_proxmox_root_username") or "root@pam").split("@", 1)[0]
    return answer_floppy_cache.make_sshpass_runner(
        host=cfg.get("proxmox_host", ""), password=pw, user=user or "root",
    )


@app.get("/answer-isos", response_class=HTMLResponse)
def page_answer_isos(request: Request, error: str = ""):
    # Route name kept for URL stability; content is the answer-floppy
    # cache now. The template adapts — the row shape hasn't changed.
    from web import answer_floppy_cache
    rows = answer_floppy_cache.list_cache(
        SEQUENCES_DB, in_use_volids=_referenced_answer_floppy_paths(),
    )
    return templates.TemplateResponse("answer_isos.html", {
        "request": request, "rows": rows, "error": error,
    })


@app.post("/answer-isos/prune")
async def submit_answer_isos_prune(request: Request):
    form = await request.form()
    hashes = form.getlist("hash") if hasattr(form, "getlist") else [form.get("hash")]
    hashes = [h for h in hashes if h]
    if not hashes:
        return RedirectResponse("/answer-isos", status_code=303)

    from web import answer_floppy_cache
    ssh = _make_root_ssh_runner()
    if ssh is None:
        return _redirect_with_error(
            "/answer-isos",
            "Cannot prune floppies: vault_proxmox_root_password is not set "
            "(Settings → Credentials). Prune also needs SSH to remove the "
            ".img files from /var/lib/vz/snippets/ on the Proxmox host.",
        )
    try:
        answer_floppy_cache.prune(
            db_path=SEQUENCES_DB, hashes_to_delete=hashes, ssh=ssh,
        )
    except Exception as e:
        return _redirect_with_error("/answer-isos", str(e))
    return RedirectResponse("/answer-isos", status_code=303)


@app.get("/api/answer-isos")
def api_answer_isos_list():
    from web import answer_floppy_cache
    return answer_floppy_cache.list_cache(
        SEQUENCES_DB, in_use_volids=_referenced_answer_floppy_paths(),
    )


@app.post("/api/answer-isos/prune")
async def api_answer_isos_prune(request: Request):
    body = await request.json()
    hashes = body.get("hashes") or []
    if not isinstance(hashes, list):
        raise HTTPException(400, "body.hashes must be a list")
    from web import answer_floppy_cache
    ssh = _make_root_ssh_runner()
    if ssh is None:
        raise HTTPException(
            400, "vault_proxmox_root_password not set — cannot SSH to "
            "Proxmox host to remove floppy files.",
        )
    removed = answer_floppy_cache.prune(
        db_path=SEQUENCES_DB, hashes_to_delete=hashes, ssh=ssh,
    )
    return {"removed": removed}


@app.get("/credentials", response_class=HTMLResponse)
def page_credentials(request: Request, error: str = ""):
    creds = sequences_db.list_credentials(SEQUENCES_DB)
    return templates.TemplateResponse("credentials.html", {
        "request": request,
        "credentials": creds,
        "error": error,
    })


@app.get("/credentials/new", response_class=HTMLResponse)
def page_credential_new(request: Request, error: str = ""):
    return templates.TemplateResponse("credential_edit.html", {
        "request": request, "cred": None, "error": error,
    })


@app.post("/credentials/new")
async def submit_credential_new(request: Request):
    form = await request.form()
    cred_type = form.get("type", "")
    try:
        payload = await _payload_from_form(cred_type, form)
        sequences_db.create_credential(
            SEQUENCES_DB, _cipher(),
            name=form["name"], type=cred_type, payload=payload,
        )
    except sqlite3.IntegrityError as e:
        msg = "name already exists" if "UNIQUE" in str(e) else str(e)
        return _redirect_with_error("/credentials/new", msg)
    except ValueError as e:
        return _redirect_with_error("/credentials/new", str(e))
    return RedirectResponse("/credentials", status_code=303)


@app.get("/credentials/{cred_id}/edit", response_class=HTMLResponse)
def page_credential_edit(request: Request, cred_id: int, error: str = ""):
    cred = sequences_db.get_credential(SEQUENCES_DB, _cipher(), cred_id)
    if cred is None:
        raise HTTPException(404, "credential not found")
    return templates.TemplateResponse("credential_edit.html", {
        "request": request, "cred": cred, "error": error,
    })


@app.post("/credentials/{cred_id}/edit")
async def submit_credential_edit(request: Request, cred_id: int):
    cred = sequences_db.get_credential(SEQUENCES_DB, _cipher(), cred_id)
    if cred is None:
        raise HTTPException(404, "credential not found")
    form = await request.form()
    try:
        new_payload = await _payload_from_form(cred["type"], form, existing=cred["payload"])
        sequences_db.update_credential(
            SEQUENCES_DB, _cipher(), cred_id,
            name=form["name"], payload=new_payload,
        )
    except sqlite3.IntegrityError as e:
        msg = "name already exists" if "UNIQUE" in str(e) else str(e)
        return _redirect_with_error(f"/credentials/{cred_id}/edit", msg)
    except ValueError as e:
        return _redirect_with_error(f"/credentials/{cred_id}/edit", str(e))
    return RedirectResponse("/credentials", status_code=303)


@app.post("/credentials/{cred_id}/delete")
def submit_credential_delete(cred_id: int):
    try:
        sequences_db.delete_credential(SEQUENCES_DB, cred_id)
    except sequences_db.CredentialInUse as e:
        msg = f"in use by sequence(s) {e.sequence_ids}"
        return _redirect_with_error("/credentials", msg)
    return RedirectResponse("/credentials", status_code=303)


async def _payload_from_form(cred_type: str, form, existing: Optional[dict] = None) -> dict:
    """Build a payload dict from the per-type HTML form fields."""
    if cred_type == "domain_join":
        pw = form.get("password", "")
        payload = {
            "domain_fqdn": form.get("domain_fqdn", "").strip(),
            "username": form.get("username", "").strip(),
            "password": pw if pw else (existing or {}).get("password", ""),
            "ou_hint": form.get("ou_hint", "").strip(),
        }
        if not payload["password"]:
            raise ValueError("password is required")
        return payload
    if cred_type == "local_admin":
        pw = form.get("la_password", "")
        payload = {
            "username": form.get("la_username", "").strip(),
            "password": pw if pw else (existing or {}).get("password", ""),
        }
        if not payload["password"]:
            raise ValueError("password is required")
        return payload
    if cred_type == "odj_blob":
        upload = form.get("odj_file")
        if upload and hasattr(upload, "read"):
            blob = await upload.read()
            return {
                "blob_b64": base64.b64encode(blob).decode("ascii"),
                "generated_at": _now_iso(),
            }
        if existing:
            return existing
        raise ValueError("ODJ blob file is required")
    if cred_type == "mde_onboarding":
        upload = form.get("onboarding_file")
        if upload and hasattr(upload, "read"):
            raw = await upload.read()
            if not raw:
                # Empty file upload — treat as "no file" on edit, error on create.
                if existing:
                    return existing
                raise ValueError("onboarding_file required")
            filename = getattr(upload, "filename", None) or "onboarding.py"
            return {
                "filename": filename,
                "script_b64": base64.b64encode(raw).decode("ascii"),
                "uploaded_at": _now_iso(),
            }
        if existing:
            return existing
        raise ValueError("onboarding_file required")
    raise ValueError(f"unknown credential type: {cred_type}")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.get("/sequences", response_class=HTMLResponse)
def page_sequences(request: Request, error: str = ""):
    seqs = sequences_db.list_sequences(SEQUENCES_DB)
    return templates.TemplateResponse("sequences.html", {
        "request": request, "sequences": seqs, "error": error,
    })


@app.post("/sequences/{seq_id}/delete")
def submit_sequence_delete(seq_id: int):
    try:
        sequences_db.delete_sequence(SEQUENCES_DB, seq_id)
    except sequences_db.SequenceInUse as e:
        msg = f"in use by VMs {e.vmids}"
        return _redirect_with_error("/sequences", msg)
    return RedirectResponse("/sequences", status_code=303)


@app.post("/sequences/{seq_id}/duplicate")
async def submit_sequence_duplicate(request: Request, seq_id: int):
    form = await request.form()
    new_name = form.get("new_name", "").strip() or "Copy"
    try:
        sequences_db.duplicate_sequence(SEQUENCES_DB, seq_id, new_name=new_name)
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            return _redirect_with_error(
                "/sequences", f"name '{new_name}' already exists")
        raise
    return RedirectResponse("/sequences", status_code=303)


def _load_oem_profiles_dict() -> dict:
    path = FILES_DIR / "oem_profiles.yml"
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("oem_profiles", {})


@app.post("/api/vm-provisioning")
async def record_vm_provisioning_route(request: Request):
    """Record vmid -> sequence_id mapping. Called by the provisioning playbook."""
    data = await request.json()
    vmid = int(data.get("vmid", 0))
    sequence_id = int(data.get("sequence_id", 0))
    if vmid == 0 or sequence_id == 0:
        return JSONResponse({"ok": False, "error": "vmid and sequence_id required"}, status_code=400)
    sequences_db.record_vm_provisioning(SEQUENCES_DB, vmid=vmid, sequence_id=sequence_id)
    return {"ok": True}


@app.post("/api/ubuntu/check-enrollment/{vmid}")
async def check_ubuntu_enrollment(vmid: int):
    """Guest-exec intune-portal + mdatp checks on the Ubuntu VM, parse,
    persist status as Proxmox tags, return the parsed status."""
    from web.ubuntu_enrollment import parse_enrollment_output, tags_for, merge_tags

    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    base = cfg.get("proxmox_api_base") or f"https://{cfg.get('proxmox_host')}:{cfg.get('proxmox_port',8006)}/api2/json"
    auth = f"PVEAPIToken={cfg.get('vault_proxmox_api_token_id')}={cfg.get('vault_proxmox_api_token_secret')}"
    verify = cfg.get("proxmox_validate_certs", False)

    def exec_cmd(cmd: list) -> tuple:
        """Synchronous guest-exec + poll for completion; return (rc, stdout)."""
        try:
            r = requests.post(
                f"{base}/nodes/{node}/qemu/{vmid}/agent/exec",
                headers={"Authorization": auth},
                json={"command": cmd},
                verify=verify, timeout=30,
            )
            r.raise_for_status()
            pid = r.json()["data"]["pid"]
            # Poll exec-status
            for _ in range(30):
                s = requests.get(
                    f"{base}/nodes/{node}/qemu/{vmid}/agent/exec-status",
                    headers={"Authorization": auth},
                    params={"pid": pid},
                    verify=verify, timeout=10,
                )
                d = s.json().get("data", {})
                if d.get("exited"):
                    return int(d.get("exitcode", 0)), d.get("out-data", "") or ""
                time.sleep(1)
        except Exception:
            pass
        return 127, ""

    intune_rc, intune_out = exec_cmd(["/bin/sh", "-c", "intune-portal --version 2>/dev/null || echo MISSING; exit $?"])
    mdatp_rc, mdatp_out = exec_cmd(["/bin/sh", "-c", "mdatp health 2>/dev/null || echo MISSING; exit $?"])

    status = parse_enrollment_output(
        intune_stdout=intune_out, intune_rc=intune_rc,
        mdatp_stdout=mdatp_out, mdatp_rc=mdatp_rc,
    )
    new_tags = tags_for(status)

    # Fetch current tags, merge, persist
    try:
        cfg_resp = requests.get(
            f"{base}/nodes/{node}/qemu/{vmid}/config",
            headers={"Authorization": auth}, verify=verify, timeout=10,
        )
        existing = (cfg_resp.json().get("data", {}).get("tags") or "").split(";")
        existing = [t for t in existing if t]
        merged = merge_tags(existing, new_tags)
        requests.post(
            f"{base}/nodes/{node}/qemu/{vmid}/config",
            headers={"Authorization": auth},
            data={"tags": ";".join(merged)},
            verify=verify, timeout=10,
        )
    except Exception:
        pass  # tag persistence is best-effort

    return {"ok": True, "status": status, "tags": new_tags}


@app.get("/sequences/new", response_class=HTMLResponse)
def page_sequence_new(request: Request):
    return templates.TemplateResponse("sequence_edit.html", {
        "request": request, "seq": None,
        "oem_profiles": load_oem_profiles(),
    })


@app.get("/sequences/{seq_id}/edit", response_class=HTMLResponse)
def page_sequence_edit(request: Request, seq_id: int):
    seq = sequences_db.get_sequence(SEQUENCES_DB, seq_id)
    if seq is None:
        raise HTTPException(404, "sequence not found")
    return templates.TemplateResponse("sequence_edit.html", {
        "request": request, "seq": seq,
        "oem_profiles": load_oem_profiles(),
    })



# ---------------------------------------------------------------------------
# Device state monitoring — REST API
# ---------------------------------------------------------------------------

class _MonitoringSettingsPatch(BaseModel):
    enabled: Optional[bool] = None
    interval_seconds: Optional[int] = None
    ad_credential_id: Optional[int] = None


class _SearchOuCreate(BaseModel):
    dn: str
    label: str = ""
    enabled: bool = True
    sort_order: Optional[int] = None


class _SearchOuPatch(BaseModel):
    dn: Optional[str] = None
    label: Optional[str] = None
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None


@app.get("/api/monitoring/settings")
def api_monitoring_settings_get():
    s = device_history_db.get_settings(DEVICE_MONITOR_DB)
    return {
        "enabled": s.enabled,
        "interval_seconds": s.interval_seconds,
        "ad_credential_id": s.ad_credential_id,
        "updated_at": s.updated_at,
    }


@app.put("/api/monitoring/settings")
def api_monitoring_settings_update(body: _MonitoringSettingsPatch):
    try:
        device_history_db.update_settings(
            DEVICE_MONITOR_DB,
            enabled=body.enabled,
            interval_seconds=body.interval_seconds,
            ad_credential_id=body.ad_credential_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return api_monitoring_settings_get()


@app.get("/api/monitoring/search-ous")
def api_monitoring_search_ous_list():
    return [
        {"id": o.id, "dn": o.dn, "label": o.label,
         "enabled": o.enabled, "sort_order": o.sort_order,
         "created_at": o.created_at, "updated_at": o.updated_at}
        for o in device_history_db.list_search_ous(DEVICE_MONITOR_DB)
    ]


@app.post("/api/monitoring/search-ous", status_code=201)
def api_monitoring_search_ous_create(body: _SearchOuCreate):
    try:
        ou_id = device_history_db.add_search_ou(
            DEVICE_MONITOR_DB,
            dn=body.dn, label=body.label,
            enabled=body.enabled, sort_order=body.sort_order,
        )
    except device_history_db.InvalidDn as e:
        raise HTTPException(400, str(e)) from e
    except sqlite3.IntegrityError as e:
        raise HTTPException(
            409, f"search OU with dn={body.dn!r} already exists",
        ) from e
    return {"id": ou_id}


@app.put("/api/monitoring/search-ous/{ou_id}")
def api_monitoring_search_ous_update(ou_id: int, body: _SearchOuPatch):
    try:
        device_history_db.update_search_ou(
            DEVICE_MONITOR_DB, ou_id,
            dn=body.dn, label=body.label,
            enabled=body.enabled, sort_order=body.sort_order,
        )
    except device_history_db.CannotDeleteLastOu as e:
        raise HTTPException(409, str(e)) from e
    except device_history_db.InvalidDn as e:
        raise HTTPException(400, str(e)) from e
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@app.delete("/api/monitoring/search-ous/{ou_id}")
def api_monitoring_search_ous_delete(ou_id: int):
    try:
        device_history_db.delete_search_ou(DEVICE_MONITOR_DB, ou_id)
    except device_history_db.CannotDeleteLastOu as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


# ---------------------------------------------------------------------------
# Device state monitoring — UI pages
# ---------------------------------------------------------------------------

def _ad_first_seen_map() -> dict[int, str]:
    """For each vmid that has an AD match in any historical probe,
    return the earliest checked_at at which ad_found=1. Used to
    decide whether "Entra missing" is a sync-pending ⏳ or a real ❌."""
    import sqlite3
    conn = sqlite3.connect(DEVICE_MONITOR_DB)
    try:
        rows = conn.execute(
            "SELECT vmid, MIN(checked_at) FROM device_probes "
            "WHERE ad_found = 1 GROUP BY vmid"
        ).fetchall()
    finally:
        conn.close()
    return {int(vmid): ts for vmid, ts in rows}


@app.get("/monitoring", response_class=HTMLResponse)
def page_monitoring(request: Request):
    from web import monitoring_view, service_health
    latest = device_history_db.latest_per_vmid(DEVICE_MONITOR_DB)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = monitoring_view.build_dashboard_rows(
        latest,
        ad_first_seen=_ad_first_seen_map(),
        now_iso=now_iso,
    )
    settings = device_history_db.get_settings(DEVICE_MONITOR_DB)
    keytab = device_history_db.get_keytab_health(DEVICE_MONITOR_DB)
    try:
        svc_rows = service_health.list_services(DEVICE_MONITOR_DB)
    except sqlite3.OperationalError:
        # service_health table may not exist yet on a freshly-initialized
        # device_monitor.db (init() is called from the startup hook).
        svc_rows = []
    return templates.TemplateResponse("monitoring.html", {
        "request": request,
        "rows": rows,
        "settings": settings,
        "search_ous": device_history_db.list_search_ous(DEVICE_MONITOR_DB),
        "keytab": keytab,
        "service_health": svc_rows,
    })


def _latest_match_or_none(json_str: str) -> dict:
    """Pick the first match from a ``*_matches_json`` blob, or {}."""
    import json as _json
    try:
        matches = _json.loads(json_str or "[]")
    except (TypeError, ValueError):
        return {}
    return matches[0] if matches else {}


def _linkage_health(pve_row: dict, probe_row: dict) -> list[dict]:
    """Return the four cross-system checks for the top-of-page strip."""
    import json as _json
    if not probe_row:
        return []
    ad_matches = _json.loads(probe_row.get("ad_matches_json") or "[]")
    entra_matches = _json.loads(probe_row.get("entra_matches_json") or "[]")
    intune_matches = _json.loads(probe_row.get("intune_matches_json") or "[]")
    serial = (probe_row.get("serial") or "").strip()
    win_name = (probe_row.get("win_name") or "").strip()

    checks = []

    # SMBIOS serial ↔ Intune serialNumber.
    if serial and intune_matches:
        match = any(m.get("serialNumber") == serial for m in intune_matches)
        checks.append({
            "label": "SMBIOS.serial → Intune.serialNumber",
            "ok": match,
            "value": serial,
        })
    else:
        checks.append({
            "label": "SMBIOS.serial → Intune.serialNumber",
            "ok": None,  # not yet resolvable
            "value": serial or "—",
        })

    # Windows name ↔ AD cn. Windows AD computer names are canonically
    # case-insensitive — AD upper-cases the CN regardless of what the
    # guest reports for its hostname. Treat a case-insensitive match
    # as a green check; exact case only matters for cosmetics.
    if win_name and ad_matches:
        cns = [m.get("cn") or "" for m in ad_matches]
        ci = any(c.lower() == win_name.lower() for c in cns)
        checks.append({
            "label": "Windows.Name → AD.cn",
            "ok": ci,
            "value": win_name,
        })
    else:
        checks.append({
            "label": "Windows.Name → AD.cn",
            "ok": None, "value": win_name or "—",
        })

    # AD.objectSid ↔ Entra.onPremisesSecurityIdentifier (hybrid only).
    server_ad = [m for m in entra_matches if m.get("trustType") == "ServerAd"]
    if server_ad:
        ad_sids = {m.get("objectSid") for m in ad_matches if m.get("objectSid")}
        entra_sid = server_ad[0].get("onPremisesSecurityIdentifier")
        checks.append({
            "label": "AD.objectSid → Entra.onPremSecurityIdentifier",
            "ok": bool(entra_sid and entra_sid in ad_sids),
            "value": entra_sid or "—",
        })
    else:
        checks.append({
            "label": "AD.objectSid → Entra.onPremSecurityIdentifier",
            "ok": None, "value": "—",
        })

    # Entra.deviceId ↔ Intune.azureADDeviceId.
    if entra_matches and intune_matches:
        e_ids = {m.get("deviceId") for m in entra_matches if m.get("deviceId")}
        i_ids = {m.get("azureADDeviceId") for m in intune_matches if m.get("azureADDeviceId")}
        checks.append({
            "label": "Entra.deviceId → Intune.azureADDeviceId",
            "ok": bool(e_ids & i_ids),
            "value": next(iter(e_ids & i_ids), "—") if (e_ids & i_ids) else "—",
        })
    else:
        checks.append({
            "label": "Entra.deviceId → Intune.azureADDeviceId",
            "ok": None, "value": "—",
        })

    return checks


@app.get("/devices/{vmid}", response_class=HTMLResponse)
def page_device_detail(request: Request, vmid: int):
    from web import device_regression, monitoring_view
    pve = device_history_db.latest_pve_snapshot(DEVICE_MONITOR_DB, vmid)
    probe = device_history_db.latest_device_probe(DEVICE_MONITOR_DB, vmid)
    if pve is None and probe is None:
        raise HTTPException(404, f"no monitoring data for vmid {vmid}")

    pve_dict = dict(pve) if pve else None
    probe_dict = dict(probe) if probe else None

    history = device_history_db.history_for_vmid(DEVICE_MONITOR_DB, vmid, limit=50)
    # Detector wants oldest-first for correct transition ordering.
    timeline = device_regression.build_timeline(
        list(reversed(history["pve_snapshots"])),
        list(reversed(history["device_probes"])),
    )

    linkage = _linkage_health(pve_dict or {}, probe_dict or {})
    import json as _json
    ad_matches = _json.loads((probe_dict or {}).get("ad_matches_json") or "[]")
    entra_matches = _json.loads((probe_dict or {}).get("entra_matches_json") or "[]")
    intune_matches = _json.loads((probe_dict or {}).get("intune_matches_json") or "[]")
    return templates.TemplateResponse("device_detail.html", {
        "request": request,
        "vmid": vmid,
        "pve": pve_dict,
        "probe": probe_dict,
        "ad_matches": ad_matches,
        "entra_matches": entra_matches,
        "intune_matches": intune_matches,
        "linkage": linkage,
        "timeline": timeline,
        "history": history,
    })


@app.get("/monitoring/settings", response_class=HTMLResponse)
def page_monitoring_settings(request: Request):
    settings = device_history_db.get_settings(DEVICE_MONITOR_DB)
    ous = device_history_db.list_search_ous(DEVICE_MONITOR_DB)
    # Only credentials of type domain_join are useful for AD.
    all_creds = sequences_db.list_credentials(SEQUENCES_DB)
    domain_creds = [c for c in all_creds if c.get("type") == "domain_join"]
    keytab = device_history_db.get_keytab_health(DEVICE_MONITOR_DB)
    return templates.TemplateResponse("monitoring_settings.html", {
        "request": request,
        "settings": settings,
        "search_ous": ous,
        "domain_creds": domain_creds,
        "keytab": keytab,
    })


@app.post("/api/monitoring/keytab/refresh-now")
async def api_monitoring_keytab_refresh_now():
    """Operator-triggered immediate refresh — unblocks 'broken' state
    without waiting for the next sweep tick."""
    import asyncio
    try:
        await asyncio.to_thread(_run_keytab_checks)
    except Exception as e:
        raise HTTPException(500, f"refresh failed: {e}") from e
    return device_history_db.get_keytab_health(DEVICE_MONITOR_DB) or {}


@app.get("/api/monitoring/keytab/health")
def api_monitoring_keytab_health():
    return device_history_db.get_keytab_health(DEVICE_MONITOR_DB) or {}
