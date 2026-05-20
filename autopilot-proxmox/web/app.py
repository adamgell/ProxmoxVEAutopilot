import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import secrets
import subprocess
import time
import urllib3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import requests
import psycopg
import yaml
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.requests import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from web.jobs import JobManager
from web import devices_pg as devices_db
from web import jobs_pg as jobs_db
from web.live import LiveHub, utc_now_iso
from web import machine_lifecycle_pg
from web import monitoring_evidence
from web import proxmox_permissions

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_sleep = time.sleep

import re
import shlex
import socket
from urllib.parse import quote_plus, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def _redirect_with_error(path: str, error: str) -> RedirectResponse:
    """303-redirect to ``path`` with ``error`` safely percent-encoded.

    Use whenever rendering an exception message or user-supplied text into
    a redirect URL — raw f-string interpolation truncates at the first space
    or '#' and lets '&' smuggle extra params.
    """
    return RedirectResponse(f"{path}?error={quote_plus(str(error))}", status_code=303)


def _is_unique_violation(exc: Exception) -> bool:
    return isinstance(exc, psycopg.errors.UniqueViolation) or "unique" in str(exc).lower()


def _load_version() -> dict:
    """Read the build SHA + timestamp baked in at image build time."""
    version_file = os.environ.get("APP_VERSION_FILE", str(BASE_DIR / "VERSION"))
    sha = (
        os.environ.get("APP_BUILD_SHA")
        or os.environ.get("GIT_SHA")
        or "unknown"
    ).strip() or "unknown"
    build_time = (
        os.environ.get("APP_BUILD_TIME")
        or os.environ.get("BUILD_TIME")
        or "unknown"
    ).strip() or "unknown"
    try:
        with open(version_file) as f:
            lines = f.read().splitlines()
            if sha == "unknown":
                sha = (lines[0] if lines else "unknown").strip() or "unknown"
            if build_time == "unknown":
                build_time = (lines[1] if len(lines) > 1 else "unknown").strip() or "unknown"
    except Exception:
        pass
    if sha == "unknown":
        for repo_path in (
            os.environ.get("HOST_REPO_MOUNT", "/host/repo"),
            os.environ.get("HOST_REPO_PATH", ""),
            str(BASE_DIR),
        ):
            if not repo_path:
                continue
            try:
                found = subprocess.check_output(
                    ["git", "-c", f"safe.directory={repo_path}", "-C", repo_path, "rev-parse", "HEAD"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                ).strip()
            except Exception:
                continue
            if found:
                sha = found
                if build_time == "unknown":
                    try:
                        build_time = subprocess.check_output(
                            ["git", "-c", f"safe.directory={repo_path}", "-C", repo_path, "show", "-s", "--format=%cI", "HEAD"],
                            text=True,
                            stderr=subprocess.DEVNULL,
                            timeout=3,
                        ).strip() or "unknown"
                    except Exception:
                        build_time = "unknown"
                break
    return {"sha": sha, "sha_short": sha[:7] if sha != "unknown" else sha, "build_time": build_time}


_APP_VERSION = _load_version()
_LATEST_VERSION_CACHE: dict = {"fetched_at": 0, "sha": None, "sha_short": None, "error": None}
_RUNTIME_LOG_SERVICES = {
    "autopilot",
    "autopilot-builder",
    "autopilot-monitor",
    "autopilot-mcp",
    "autopilot-postgres",
}
_RUNTIME_LOG_NAME_PREFIXES = (
    "autopilot",
    "autopilot-monitor",
    "autopilot-mcp",
    "autopilot-postgres",
    "autopilot-proxmox-autopilot-builder-",
)
_LOG_SECRET_RE = re.compile(
    r"(?i)(password|passwd|token|secret|credential|private[_-]?key)([=:]\s*)([^\s\"']+)"
)

# Two flags, one per startup hook. /healthz gates on BOTH so
# docker-compose `depends_on: service_healthy` can't release
# builder/monitor after only one hook has completed. FastAPI runs
# startup hooks in registration order, so keep sequence init and job init
# as separate checkpoints.
_SEQUENCES_READY = False
_JOBS_READY = False


def _sanitize_input(value):
    """Reject input containing shell-dangerous characters."""
    if not re.match(r'^[\w\-\.]*$', str(value)):
        raise ValueError(f"Invalid input: {value!r} — only alphanumeric, hyphens, underscores, dots allowed")
    return str(value)


def _optional_text(value) -> str:
    """Normalize optional form/default values without rendering YAML null as text."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in ("none", "null"):
        return ""
    return text


def _safe_path(base_dir, filename):
    """Resolve a filename and verify it stays inside base_dir. Raises ValueError on traversal."""
    resolved = (base_dir / filename).resolve()
    if not str(resolved).startswith(str(base_dir.resolve())):
        raise ValueError(f"Path traversal blocked: {filename}")
    return resolved


def _react_asset_tags() -> dict[str, list[str]]:
    """Return Vite-built React asset URLs for the protected shell.

    Tests and source checkouts may not have a built frontend yet, so the
    fallback shell renders without asset tags until ``frontend/dist`` is copied
    into ``web/static/react`` by the Docker build.
    """
    try:
        manifest = json.loads(REACT_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"scripts": [], "styles": []}

    entry = manifest.get("src/main.tsx") or manifest.get("index.html") or {}
    scripts: list[str] = []
    styles: list[str] = []
    if entry.get("file"):
        scripts.append(f"/static/react/{entry['file']}")
    for css_file in entry.get("css") or []:
        styles.append(f"/static/react/{css_file}")
    return {"scripts": scripts, "styles": styles}


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
REACT_DIST_DIR = Path(__file__).resolve().parent / "static" / "react"
REACT_MANIFEST_PATH = REACT_DIST_DIR / ".vite" / "manifest.json"
HASH_DIR = BASE_DIR / "output" / "hashes"
SCREENSHOT_STORE_DIR = Path(
    os.environ.get("AUTOPILOT_VM_SCREENSHOT_DIR", str(BASE_DIR / "output" / "vm-screenshots"))
)
FILE_SHELF_DIR = Path(
    os.environ.get("AUTOPILOT_FILE_SHELF_DIR", str(BASE_DIR / "output" / "files"))
)
SETUP_STATE_PATH = BASE_DIR / "output" / "setup" / "foundation_state.json"
AGENT_SEED_MANIFEST_PATH = BASE_DIR / "output" / "setup" / "agent-seed" / "manifest.json"
PLAYBOOK_DIR = BASE_DIR / "playbooks"
FILES_DIR = BASE_DIR / "files"
VARS_PATH = BASE_DIR / "inventory" / "group_vars" / "all" / "vars.yml"
SECRETS_DIR = BASE_DIR / "secrets"
# Legacy call sites still pass these handles into the compatibility layer;
# the backing store is Postgres via web.db_pg, not local database files.
SEQUENCES_DB = None
JOBS_DB = None
CREDENTIAL_KEY = SECRETS_DIR / "credential_key"
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
        {"key": "utm_snapshot_auto_before_sequence", "label": "Auto-Snapshot Before Sequence", "type": "bool",
         "applies_to": "utm",
         "help": "Reserved for future use: automatically create a qcow2 snapshot before running a sequence on a UTM VM. Requires the VM to be stopped. Not yet invoked — see TODO in roles/utm_vm_clone/README.md."},
    ]},
    {"section": "UTM Answer ISO", "applies_to": "utm", "fields": [
        {"key": "utm_answer_iso_enabled", "label": "Enable Answer ISO", "type": "bool",
         "help": "When true, utm_vm_clone with template_mode=true will auto-generate an answer ISO and attach it before boot."},
        {"key": "utm_answer_admin_user", "label": "Admin Username", "type": "text",
         "help": "Local administrator account name created during unattended install. Default: Administrator."},
        {"key": "utm_answer_locale", "label": "Locale", "type": "text",
         "help": "Windows locale code, e.g. en-US, en-GB, fr-FR."},
        {"key": "utm_answer_timezone", "label": "Timezone", "type": "text",
         "help": "Windows timezone name. Run `tzutil /l` on Windows for valid values. Default: Pacific Standard Time."},
        {"key": "utm_answer_windows_edition", "label": "Windows Edition", "type": "text",
         "help": "Edition name to select from the ISO's install.wim. Must match exactly, e.g. 'Windows 11 Pro' or 'Windows 11 Enterprise'."},
        {"key": "utm_answer_iso_dir", "label": "Answer ISO Output Directory", "type": "text",
         "help": "Directory where generated answer ISOs are written. Default: output/answer-isos relative to repo root."},
    ]},
    {"section": "UTM Answer ISO Credentials", "source": "vault", "applies_to": "utm", "fields": [
        {"key": "utm_answer_admin_pass", "label": "Admin Password", "type": "secret",
         "help": "Password for the local administrator account. Required for unattended template builds."},
        {"key": "utm_answer_product_key", "label": "Product Key", "type": "secret",
         "help": "Optional Windows product key. Leave blank to use the ISO's built-in default edition."},
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
         "help": "Default: root@pam. Used by the SSH bootstrap/runtime path for OEM/chassis operations."},
        {"key": "vault_proxmox_root_password",
         "label": "Proxmox Root Password", "type": "secret",
         "applies_to": "proxmox",
         "help": "Required for OEM/chassis operations. Prefer setting it through Proxmox Permission Bootstrap; never sent to the browser."},
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
    {"section": "OSDeploy Build Host", "applies_to": "proxmox", "fields": [
        {"key": "osdeploy_build_remote", "label": "Build SSH Target", "type": "text",
         "help": "Windows build host SSH target used for OSDeploy/OSDBuilder media builds, e.g. user@192.168.2.50."},
        {"key": "osdeploy_build_remote_root", "label": "Build Workspace Root", "type": "text",
         "help": "Windows path where the OSDeploy build wrapper stages source media, tools, manifests, and output."},
        {"key": "osdeploy_build_ssh_key_path", "label": "Build SSH Key Path", "type": "text",
         "help": "Private key path inside the web/builder container. Mount the key under /app/secrets and keep it out of git."},
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
    Safe to render in HTML -- carries no secret material."""
    return {k: bool(v) for k, v in _load_vault().items()}


def _settings_hypervisor_type(value: str | None) -> str:
    hv_type = (value or "proxmox").lower()
    return "proxmox" if hv_type == "pve" else hv_type


def _looks_like_jinja(value: str) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _bridge_winpe_vars_to_env() -> None:
    """Mirror WinPE-relevant settings into os.environ so the WinPE
    modules (which read env to stay decoupled from _load_vars at import
    time) see real values. Vault-rendered fields come from _load_vault
    directly because _load_vars returns Jinja strings literally."""
    raw_vars = _load_vars()
    vault = _load_vault()

    # Token secret lives in vault.yml (vars.yml only references it via
    # Jinja). Read straight from _load_vault to skip the unrendered
    # indirection. Use setdefault so tests that pre-seed the env are not
    # overwritten by the bridge.
    secret = vault.get("vault_autopilot_winpe_token_secret") or ""
    if secret:
        os.environ.setdefault("AUTOPILOT_WINPE_TOKEN_SECRET", secret)

    # Plain integer in vars.yml (no Jinja).
    blank = raw_vars.get("winpe_blank_template_vmid")
    if blank not in (None, "", "null"):
        os.environ.setdefault("AUTOPILOT_WINPE_BLANK_TEMPLATE_VMID", str(blank))

    # Plain string ("isos:iso/...") in vars.yml.
    iso = raw_vars.get("proxmox_winpe_iso") or ""
    if iso and not _looks_like_jinja(iso):
        os.environ.setdefault("AUTOPILOT_WINPE_ISO", iso)

    # Plain string in vars.yml.
    allow = raw_vars.get("autopilot_winpe_identity_allowlist") or ""
    if allow and not _looks_like_jinja(allow):
        os.environ.setdefault("AUTOPILOT_WINPE_IDENTITY_ALLOWLIST", allow)


def _winpe_enabled() -> bool:
    """The provision UI shows the WinPE option only when the inventory
    has wired up a blank template VMID, a WinPE ISO, and the token
    signing secret. The bridge in _bridge_winpe_vars_to_env() exports
    these as env vars at startup (see Task F2 step 4); we read the env
    so the test suite can flip the flag without monkeypatching the
    inventory loader."""
    return bool(
        os.environ.get("AUTOPILOT_WINPE_BLANK_TEMPLATE_VMID")
        and os.environ.get("AUTOPILOT_WINPE_ISO")
        and os.environ.get("AUTOPILOT_WINPE_TOKEN_SECRET")
    )


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
app.mount(
    "/static/react",
    StaticFiles(directory=str(REACT_DIST_DIR), check_dir=False),
    name="react-static",
)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["tojson"] = lambda x, indent=None: json.dumps(x, indent=indent)
job_manager = JobManager(
    jobs_dir=str(BASE_DIR / "jobs"),
)

from web import agent_telemetry_pg
from web import sequences_pg as sequences_db, crypto as _crypto
from web import sequence_compiler
from web import device_history_pg as device_history_db, device_monitor
from web import auth as _auth

from web.winpe_endpoints import (
    router as _winpe_router,
    api_router as _winpe_api_router,
    osd_router as _osd_router,
)
from web.osd_v2_endpoints import (
    router as _osd_v2_router,
    api_router as _osd_v2_api_router,
    content_api_router as _content_api_router,
)
from web.agent_v1_endpoints import router as _agent_v1_router
from web.cloudosd_endpoints import router as _cloudosd_router
try:
    from web.osdeploy_endpoints import router as _osdeploy_router
except ModuleNotFoundError:
    _osdeploy_router = None
_bridge_winpe_vars_to_env()
app.include_router(_winpe_router)
app.include_router(_osd_router)
app.include_router(_osd_v2_router)
app.include_router(_osd_v2_api_router)
app.include_router(_content_api_router)
app.include_router(_winpe_api_router)
app.include_router(_agent_v1_router)
app.include_router(_cloudosd_router)
if _osdeploy_router is not None:
    app.include_router(_osdeploy_router)


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
    entra_configured = bool(
        cfg.get("entra_configured")
        or (cfg.get("tenant_id") and cfg.get("client_id"))
    )
    configured_local = cfg.get("local_enabled")
    local_enabled = (not entra_configured) if configured_local is None else bool(configured_local)
    auth_mode = cfg.get("mode") or ("entra" if entra_configured else "local")
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
    safe_next = _auth.safe_next_url(next)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        # URL-encode so a next path containing ?&= survives being
        # re-emitted into the Sign-in link's query string.
        "next_url": _q(safe_next, safe=""),
        "tenant_label": tenant_label,
        "auth_configured": entra_configured or local_enabled,
        "entra_configured": entra_configured,
        "local_enabled": local_enabled,
        "auth_mode": auth_mode,
        "setup_url": "/setup",
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
    if not (
        cfg.get("entra_configured")
        or (cfg.get("tenant_id") and cfg.get("client_id"))
    ):
        return RedirectResponse(
            url="/auth/login?error=entra+auth+not+configured",
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


@app.post("/auth/local/start")
async def auth_local_start(request: Request, next: str = "/"):
    """Create a local operator session for first-run and lab installs."""
    cfg = _auth_config()
    next_url = _auth.safe_next_url(next)
    entra_configured = bool(
        cfg.get("entra_configured")
        or (cfg.get("tenant_id") and cfg.get("client_id"))
    )
    configured_local = cfg.get("local_enabled")
    local_enabled = (not entra_configured) if configured_local is None else bool(configured_local)
    if not local_enabled:
        return _login_error("Local operator sign-in is not enabled.", next_url=next_url)
    request.session["user"] = {
        "sub": "local-operator",
        "name": "Local Operator",
        "email": "",
        "groups": [],
        "provider": "local",
    }
    return RedirectResponse(url=next_url, status_code=303)


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


@app.get("/api/setup/v1/state")
async def setup_state_api():
    return _setup_readiness()


@app.get("/api/setup/v1/readiness")
async def setup_readiness_api():
    return _setup_readiness()


@app.get("/api/setup/v1/media")
async def setup_media_api():
    data = _setup_readiness()
    return data["media"]


class _BuildHostRepairBody(BaseModel):
    vmid: Optional[int] = None
    server_url: Optional[str] = None
    upgrade_agent: bool = True
    allow_reboot: bool = True


class _BuildHostSeedIsoBody(BaseModel):
    vmid: int = Field(ge=1)
    node: Optional[str] = None
    storage: Optional[str] = None
    controller_url: Optional[str] = None


class _BuildHostVmBody(BaseModel):
    vmid: Optional[int] = Field(default=None, ge=1)
    name: str = "autopilot-buildhost-01"
    node: Optional[str] = None
    iso_storage: Optional[str] = None
    disk_storage: Optional[str] = None
    network_bridge: Optional[str] = None
    windows_iso_volid: Optional[str] = None
    virtio_iso_volid: Optional[str] = None
    controller_url: Optional[str] = None
    cores: int = Field(default=4, ge=1)
    memory_mb: int = Field(default=8192, ge=2048)
    disk_size_gb: int = Field(default=160, ge=64)
    start: bool = True


class _BuildHostWorkloadsBody(BaseModel):
    kinds: list[str] = Field(default_factory=list)
    force: bool = False
    install_adk: bool = True


class _PromoteSetupArtifactsBody(BaseModel):
    node: Optional[str] = None
    storage: Optional[str] = None
    artifact_ids: list[str] = Field(default_factory=list)
    already_copied: bool = False


def _setup_promote_api_upload_max_bytes() -> int:
    raw = os.environ.get("AUTOPILOT_SETUP_PROMOTE_API_MAX_BYTES", "").strip()
    if not raw:
        return 1024 * 1024 * 1024
    try:
        return max(0, int(raw))
    except ValueError:
        return 1024 * 1024 * 1024


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _repo_git_metadata_for_bundle() -> dict:
    repo_root = _source_bundle_root()

    def run_git(*args: str) -> tuple[bool, str]:
        try:
            out = subprocess.check_output(
                ["git", "-c", f"safe.directory={repo_root}", *args],
                cwd=repo_root,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            return True, out
        except Exception:
            return False, ""

    sha_ok, sha_out = run_git("rev-parse", "HEAD")
    status_ok, status = run_git("status", "--porcelain")
    dirty_state = "dirty" if status else "clean"
    if not status_ok:
        dirty_state = "unknown"
    return {
        "schema_version": 1,
        "producer": "setup-source-bundle",
        "git_sha": sha_out if sha_ok and sha_out else _APP_VERSION.get("sha") or "unknown",
        "git_dirty": bool(status) if status_ok else None,
        "git_dirty_state": dirty_state,
        "git_status_available": status_ok,
        "build_time": datetime.now(timezone.utc).isoformat(),
        "source_root": str(repo_root),
    }


def _source_bundle_root() -> Path:
    configured = os.environ.get("AUTOPILOT_SOURCE_BUNDLE_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    host_repo = Path("/host/repo")
    if host_repo.is_dir():
        return host_repo.resolve()
    return BASE_DIR.parent.resolve()


def _source_bundle_controller_url() -> str:
    state = _read_json_file(SETUP_STATE_PATH)
    candidates = [
        state.get("controller_url"),
        state.get("base_url"),
        os.environ.get("AUTOPILOT_BASE_URL"),
    ]
    for candidate in candidates:
        value = str(candidate or "").strip().rstrip("/")
        if value:
            return value
    return ""


def _create_source_bundle_zip() -> bytes:
    import os
    import zipfile

    repo_root = _source_bundle_root()
    controller_url = _source_bundle_controller_url()
    skip_dirs = {
        ".claude",
        ".git",
        ".remember",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        ".venv-test",
        "__pycache__",
        "bin",
        "cache",
        "jobs",
        "migration",
        "node_modules",
        "obj",
        "output",
        "secrets",
        "utm-guest-tools-win",
    }
    skip_files = {
        ".env",
        ".env.local",
        "vault.yml",
    }
    skip_suffixes = {
        ".exe",
        ".gz",
        ".img",
        ".iso",
        ".key",
        ".msi",
        ".pem",
        ".p12",
        ".pfx",
        ".qcow2",
        ".vhd",
        ".vhdx",
        ".wim",
        ".zip",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "autopilot-source-manifest.json",
            json.dumps(_repo_git_metadata_for_bundle(), indent=2, sort_keys=True),
        )
        for root, dirnames, filenames in os.walk(repo_root):
            dirnames[:] = [
                dirname for dirname in dirnames
                if dirname not in skip_dirs
            ]
            root_path = Path(root)
            for filename in filenames:
                path = root_path / filename
                rel = path.relative_to(repo_root)
                if path.name in skip_files or path.suffix.lower() in skip_suffixes:
                    continue
                if rel.as_posix().endswith("tools/cloudosd-build/config.json") and controller_url:
                    try:
                        config = json.loads(path.read_text(encoding="utf-8"))
                    except Exception:
                        config = {}
                    if isinstance(config, dict):
                        config["flask_base_url"] = controller_url
                        zf.writestr(rel.as_posix(), json.dumps(config, indent=2, sort_keys=True))
                        continue
                zf.write(path, rel.as_posix())
    return buffer.getvalue()


@app.get("/api/setup/v1/source-bundle.zip")
async def setup_source_bundle():
    body = _create_source_bundle_zip()
    return Response(
        content=body,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="ProxmoxVEAutopilot-source.zip"',
            "X-Source-SHA": _repo_git_metadata_for_bundle().get("git_sha", "unknown"),
        },
    )


@app.get("/api/setup/v1/agent-seed/{rid}/AutopilotAgent.exe")
async def setup_seed_agent_exe(rid: str):
    safe_rid = rid.strip().lower()
    if safe_rid not in {"win-x64", "win-arm64"}:
        raise HTTPException(status_code=404, detail="unsupported runtime identifier")
    path = BASE_DIR / "output" / "setup" / "agent-seed" / safe_rid / "AutopilotAgent.exe"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="seed agent has not been built")
    return Response(
        content=path.read_bytes(),
        media_type="application/vnd.microsoft.portable-executable",
        headers={
            "Content-Disposition": 'attachment; filename="AutopilotAgent.exe"',
        },
    )


@app.get("/api/setup/v1/build-host")
async def setup_build_host_api():
    data = _setup_readiness()
    return {
        "schema_version": 1,
        "build_host": data["build_host"],
        "artifacts": data["artifacts"],
    }


def _read_build_host_bootstrap_token() -> str:
    token_path = SECRETS_DIR / "fleet-bootstrap-token"
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    if not token:
        token = _create_build_host_bootstrap_token(token_path)
    _ensure_build_host_bootstrap_hash(token)
    return token


def _build_host_env_file_candidates() -> list[Path]:
    candidates = [BASE_DIR / ".env"]
    host_mount = Path(os.environ.get("HOST_REPO_MOUNT", "/host/repo"))
    if host_mount.exists():
        candidates.append(host_mount / "autopilot-proxmox" / ".env")
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(candidate)
    return unique


def _write_env_value(env_path: Path, key: str, value: str) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    updated = False
    next_lines: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            next_lines.append(f"{key}={value}")
            updated = True
        else:
            next_lines.append(line)
    if not updated:
        next_lines.append(f"{key}={value}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")


def _ensure_build_host_bootstrap_hash(token: str) -> str:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    os.environ["AUTOPILOT_AGENT_BOOTSTRAP_TOKEN_SHA256"] = token_hash
    for env_path in _build_host_env_file_candidates():
        _write_env_value(env_path, "AUTOPILOT_AGENT_BOOTSTRAP_TOKEN_SHA256", token_hash)
    return token_hash


def _create_build_host_bootstrap_token(token_path: Path) -> str:
    token = secrets.token_urlsafe(48)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(f"{token}\n", encoding="utf-8")
    token_path.chmod(0o600)
    return token


def _build_host_agent_config(
    *,
    vmid: int,
    controller_url: str,
    bootstrap_token: str,
) -> dict:
    return {
        "serverUrl": controller_url.rstrip("/"),
        "bootstrapToken": bootstrap_token,
        "agentId": f"buildhost-{vmid}",
        "phase": "build-host",
        "role": "build-host",
        "capabilities": sorted(_BUILD_HOST_WORK_KINDS),
        "vmid": vmid,
        "heartbeatIntervalSeconds": 15,
    }


def _render_build_host_bootstrap_ps1(controller_url: str) -> str:
    server = controller_url.rstrip("/")
    return f"""$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$serverUrl = '{server}'
$root = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\\AutopilotAgent'
New-Item -ItemType Directory -Force -Path $root | Out-Null
$configPath = Join-Path $root 'agent.json'
$exePath = Join-Path $root 'AutopilotAgent.exe'
Copy-Item -Force -LiteralPath "$PSScriptRoot\\agent.json" -Destination $configPath
Set-ItemProperty -LiteralPath $configPath -Name IsReadOnly -Value $false
Invoke-WebRequest -UseBasicParsing -Uri "$serverUrl/api/setup/v1/agent-seed/win-x64/AutopilotAgent.exe" -OutFile $exePath
$action = New-ScheduledTaskAction -Execute $exePath
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'ProxmoxVEAutopilotBuildHostAgent' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'ProxmoxVEAutopilotBuildHostAgent'
"""


def _render_build_host_autounattend() -> str:
    return r"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="amd64" language="neutral" publicKeyToken="31bf3856ad364e35" versionScope="nonSxS">
      <SetupUILanguage><UILanguage>en-US</UILanguage></SetupUILanguage>
      <InputLocale>en-US</InputLocale>
      <SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage>
      <UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64" language="neutral" publicKeyToken="31bf3856ad364e35" versionScope="nonSxS">
      <DiskConfiguration>
        <Disk wcm:action="add" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
          <DiskID>0</DiskID>
          <WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Size>300</Size><Type>EFI</Type></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Size>16</Size><Type>MSR</Type></CreatePartition>
            <CreatePartition wcm:action="add"><Order>3</Order><Extend>true</Extend><Type>Primary</Type></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>FAT32</Format><Label>System</Label></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>2</PartitionID></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>3</Order><PartitionID>3</PartitionID><Format>NTFS</Format><Label>Windows</Label></ModifyPartition>
          </ModifyPartitions>
        </Disk>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>
          <InstallFrom>
            <MetaData wcm:action="add" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
              <Key>/IMAGE/INDEX</Key>
              <Value>1</Value>
            </MetaData>
          </InstallFrom>
          <InstallTo><DiskID>0</DiskID><PartitionID>3</PartitionID></InstallTo>
          <WillShowUI>OnError</WillShowUI>
        </OSImage>
      </ImageInstall>
      <UserData>
        <AcceptEula>true</AcceptEula>
        <ProductKey>
          <Key></Key>
          <WillShowUI>Never</WillShowUI>
        </ProductKey>
      </UserData>
    </component>
    <component name="Microsoft-Windows-PnpCustomizationsWinPE" processorArchitecture="amd64" language="neutral" publicKeyToken="31bf3856ad364e35" versionScope="nonSxS">
      <DriverPaths xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
        <PathAndCredentials wcm:action="add" wcm:keyValue="1"><Path>D:\vioscsi\w11\amd64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="2"><Path>E:\vioscsi\w11\amd64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="3"><Path>F:\vioscsi\w11\amd64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="4"><Path>G:\vioscsi\w11\amd64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="5"><Path>D:\NetKVM\w11\amd64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="6"><Path>E:\NetKVM\w11\amd64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="7"><Path>F:\NetKVM\w11\amd64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="8"><Path>G:\NetKVM\w11\amd64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="9"><Path>D:\vioserial\w11\amd64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="10"><Path>E:\vioserial\w11\amd64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="11"><Path>F:\vioserial\w11\amd64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="12"><Path>G:\vioserial\w11\amd64</Path></PathAndCredentials>
      </DriverPaths>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" language="neutral" publicKeyToken="31bf3856ad364e35" versionScope="nonSxS">
      <ComputerName>AUTOPILOT-BLD</ComputerName>
      <TimeZone>UTC</TimeZone>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" language="neutral" publicKeyToken="31bf3856ad364e35" versionScope="nonSxS">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
        <NetworkLocation>Work</NetworkLocation>
      </OOBE>
      <UserAccounts>
        <AdministratorPassword><Value>AutopilotBuildHost!2026</Value><PlainText>true</PlainText></AdministratorPassword>
      </UserAccounts>
      <AutoLogon>
        <Enabled>true</Enabled>
        <Username>Administrator</Username>
        <Password><Value>AutopilotBuildHost!2026</Value><PlainText>true</PlainText></Password>
        <LogonCount>1</LogonCount>
      </AutoLogon>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
          <Order>1</Order>
          <CommandLine>cmd.exe /c for %d in (D E F G) do (if exist %d:\vioserial\w11\amd64\vioser.inf pnputil /add-driver %d:\vioserial\w11\amd64\*.inf /install &amp; if exist %d:\Balloon\w11\amd64\balloon.inf pnputil /add-driver %d:\Balloon\w11\amd64\*.inf /install)</CommandLine>
          <Description>Install VirtIO serial and balloon drivers</Description>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
          <Order>2</Order>
          <CommandLine>cmd.exe /c for %d in (D E F G) do if exist %d:\guest-agent\qemu-ga-x86_64.msi msiexec /i %d:\guest-agent\qemu-ga-x86_64.msi /qn /norestart</CommandLine>
          <Description>Install QEMU Guest Agent</Description>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
          <Order>3</Order>
          <CommandLine>cmd.exe /c for %d in (D E F G) do if exist %d:\bootstrap-build-host.ps1 powershell.exe -ExecutionPolicy Bypass -File %d:\bootstrap-build-host.ps1</CommandLine>
          <Description>Bootstrap ProxmoxVEAutopilot build-host agent</Description>
        </SynchronousCommand>
      </FirstLogonCommands>
    </component>
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" language="neutral" publicKeyToken="31bf3856ad364e35" versionScope="nonSxS">
      <InputLocale>en-US</InputLocale>
      <SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage>
      <UserLocale>en-US</UserLocale>
    </component>
  </settings>
</unattend>
"""


def _build_oemdrv_iso(stage_dir: Path, iso_path: Path) -> None:
    subprocess.run(
        ["genisoimage", "-quiet", "-o", str(iso_path), "-J", "-r", "-V", "OEMDRV", str(stage_dir)],
        check=True,
        capture_output=True,
        text=True,
    )


def _resolve_build_host_controller_url(value: str | None = None) -> str:
    controller_url = (
        value
        or _setup_readiness()["controller"].get("url")
        or os.environ.get("AUTOPILOT_BASE_URL")
        or ""
    ).rstrip("/")
    if not controller_url:
        raise HTTPException(status_code=409, detail="controller URL is required for build-host seed media")
    return controller_url


def _write_build_host_seed_state(
    *,
    vmid: int,
    node: str,
    seed_iso_volid: str,
    vm_ready: bool = False,
    name: str = "autopilot-buildhost-01",
) -> None:
    state = _read_json_file(SETUP_STATE_PATH)
    state.update({
        "seed_iso_ready": True,
        "seed_iso_volid": seed_iso_volid,
        "build_host_unattend_ready": True,
        "build_host_agent_auto_approve": True,
        "build_host_expected_agent_id": f"buildhost-{vmid}",
        "build_host_expected_computer_name": "AUTOPILOT-BLD",
        "build_host_admin_user": "Administrator",
        "build_host_vmid": str(vmid),
        "build_host_node": node,
        "build_host_name": name,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    if vm_ready:
        state["build_host_vm_ready"] = True
    SETUP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETUP_STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _write_build_host_identity_state(
    *,
    vmid: int,
    node: str,
    expected_agent_id: str,
    expected_computer_name: str,
    name: str = "autopilot-buildhost-01",
) -> None:
    """Persist the approval identity used by build-host agent bootstrap.

    Controller rebuilds can regenerate foundation_state.json from the PVE side.
    The VM may still exist and be discoverable, but without these fields the
    fleet bootstrap endpoint cannot safely auto-approve the expected build host.
    """
    state = _read_json_file(SETUP_STATE_PATH)
    state.update({
        "build_host_agent_auto_approve": True,
        "build_host_expected_agent_id": expected_agent_id,
        "build_host_expected_computer_name": expected_computer_name,
        "build_host_vmid": str(vmid),
        "build_host_node": node,
        "build_host_name": name,
        "build_host_vm_ready": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    SETUP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETUP_STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _generate_and_upload_build_host_seed_iso(
    *,
    vmid: int,
    node: str,
    storage: str,
    controller_url: str,
    name: str = "autopilot-buildhost-01",
) -> dict:
    import tempfile

    bootstrap_token = _read_build_host_bootstrap_token()
    agent_config = _build_host_agent_config(
        vmid=vmid,
        controller_url=controller_url,
        bootstrap_token=bootstrap_token,
    )
    iso_name = f"autopilot-buildhost-seed-{vmid}.iso"
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        stage = root / "stage"
        stage.mkdir()
        (stage / "Autounattend.xml").write_text(_render_build_host_autounattend(), encoding="utf-8")
        (stage / "bootstrap-build-host.ps1").write_text(_render_build_host_bootstrap_ps1(controller_url), encoding="utf-8")
        (stage / "agent.json").write_text(json.dumps(agent_config, indent=2, sort_keys=True), encoding="utf-8")
        iso_path = root / iso_name
        try:
            _build_oemdrv_iso(stage, iso_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=409, detail="genisoimage is not installed in the runtime container") from exc
        except subprocess.CalledProcessError as exc:
            raise HTTPException(status_code=409, detail=f"genisoimage failed: {exc.stderr[:300]}") from exc
        with iso_path.open("rb") as handle:
            _proxmox_api(
                f"/nodes/{node}/storage/{storage}/upload",
                method="POST",
                data={"content": "iso"},
                files={"filename": (iso_name, handle, "application/octet-stream")},
            )
    volid = f"{storage}:iso/{iso_name}"
    _write_build_host_seed_state(
        vmid=vmid,
        node=node,
        seed_iso_volid=volid,
        name=name,
    )
    return {
        "ok": True,
        "seed_iso_volid": volid,
        "expected_agent_id": f"buildhost-{vmid}",
        "node": node,
        "storage": storage,
    }


def _send_build_host_cd_boot_key(
    node: str,
    vmid: int,
    *,
    attempts: int = 3,
    initial_delay_seconds: float = 10,
    delay_seconds: float = 2,
) -> dict:
    """Press through Windows BootMgr's CD/DVD prompt after VM start."""
    sent = 0
    errors: list[str] = []
    if initial_delay_seconds > 0:
        _sleep(initial_delay_seconds)
    for attempt in range(attempts):
        try:
            _proxmox_api(
                f"/nodes/{node}/qemu/{vmid}/sendkey",
                method="PUT",
                data={"key": "spc"},
            )
            sent += 1
        except Exception as exc:
            errors.append(str(exc))
        if attempt < attempts - 1 and delay_seconds > 0:
            _sleep(delay_seconds)
    if sent <= 0:
        detail = "; ".join(errors[-3:]) or "no sendkey attempts succeeded"
        raise HTTPException(status_code=502, detail=f"build-host CD boot key failed: {detail[:500]}")
    return {"sent": sent, "errors": errors}


@app.post("/api/setup/v1/build-host/seed-iso", status_code=202)
async def setup_build_host_seed_iso(body: _BuildHostSeedIsoBody):
    cfg = _load_vars()
    node = body.node or cfg.get("proxmox_node") or _configured_proxmox_node()
    storage = body.storage or cfg.get("proxmox_iso_storage") or "local"
    controller_url = _resolve_build_host_controller_url(body.controller_url)
    return _generate_and_upload_build_host_seed_iso(
        vmid=body.vmid,
        node=node,
        storage=storage,
        controller_url=controller_url,
    )


@app.post("/api/setup/v1/build-host/vm", status_code=202)
async def setup_create_build_host_vm(body: _BuildHostVmBody):
    cfg = _load_vars()
    state = _public_setup_state()
    vmid = body.vmid or int(_proxmox_api("/cluster/nextid"))
    node = body.node or cfg.get("proxmox_node") or _configured_proxmox_node()
    iso_storage = body.iso_storage or cfg.get("proxmox_iso_storage") or "local"
    disk_storage = body.disk_storage or cfg.get("proxmox_storage") or "local-lvm"
    bridge = body.network_bridge or cfg.get("proxmox_bridge") or "vmbr0"
    windows_iso = body.windows_iso_volid or state.get("windows_iso_volid")
    virtio_iso = body.virtio_iso_volid or state.get("virtio_iso_volid")
    if not windows_iso:
        raise HTTPException(status_code=409, detail="Windows ISO media is required before creating the build-host VM")
    if not virtio_iso:
        raise HTTPException(status_code=409, detail="VirtIO ISO media is required before creating the build-host VM")
    controller_url = _resolve_build_host_controller_url(body.controller_url)
    seed = _generate_and_upload_build_host_seed_iso(
        vmid=vmid,
        node=node,
        storage=iso_storage,
        controller_url=controller_url,
        name=body.name,
    )
    vm_data = {
        "vmid": vmid,
        "name": body.name,
        "memory": body.memory_mb,
        "cores": body.cores,
        "cpu": "host",
        "ostype": "win11",
        "machine": "q35",
        "bios": "ovmf",
        "agent": "enabled=1",
        "scsihw": "virtio-scsi-single",
        "net0": f"virtio,bridge={bridge}",
        "efidisk0": f"{disk_storage}:1,efitype=4m,pre-enrolled-keys=1",
        "tpmstate0": f"{disk_storage}:1,version=v2.0",
        "scsi0": f"{disk_storage}:{body.disk_size_gb},iothread=1,discard=on",
        "ide0": f"{seed['seed_iso_volid']},media=cdrom",
        "ide2": f"{windows_iso},media=cdrom",
        "ide3": f"{virtio_iso},media=cdrom",
        "boot": "order=ide2;scsi0;ide0",
    }
    _proxmox_api(f"/nodes/{node}/qemu", method="POST", data=vm_data)
    boot_key = {"sent": 0, "errors": []}
    if body.start:
        _proxmox_api(f"/nodes/{node}/qemu/{vmid}/status/start", method="POST", data={})
        boot_key = _send_build_host_cd_boot_key(node, vmid)
    _write_build_host_seed_state(
        vmid=vmid,
        node=node,
        seed_iso_volid=seed["seed_iso_volid"],
        vm_ready=True,
        name=body.name,
    )
    return {
        "ok": True,
        "vmid": vmid,
        "name": body.name,
        "node": node,
        "seed_iso_volid": seed["seed_iso_volid"],
        "expected_agent_id": seed["expected_agent_id"],
        "started": body.start,
        "boot_key_sent": boot_key["sent"],
        "boot_key_errors": boot_key["errors"],
    }


@app.post("/api/setup/v1/build-host/repair-agent")
async def setup_repair_build_host_agent(body: _BuildHostRepairBody | None = None):
    body = body or _BuildHostRepairBody()
    data = _setup_readiness()
    build_host = data["build_host"]
    vmid = int(body.vmid or build_host.get("vmid") or 0)
    if vmid <= 0:
        raise HTTPException(status_code=409, detail="build-host VMID is not published")
    node = build_host.get("node") or _resolve_vm_node(vmid)
    server_url = (body.server_url or data["controller"].get("url") or data["controller"].get("ip") or "").strip()
    if server_url and not server_url.startswith(("http://", "https://")):
        server_url = f"http://{server_url}:5000"
    if not server_url:
        raise HTTPException(status_code=409, detail="controller URL is not published")
    expected_agent = build_host.get("expected_agent_id") or f"buildhost-{vmid}"
    expected_computer = build_host.get("expected_computer_name") or "AUTOPILOT-BLD"
    _write_build_host_identity_state(
        vmid=vmid,
        node=str(node),
        expected_agent_id=expected_agent,
        expected_computer_name=expected_computer,
        name=build_host.get("name") or "autopilot-buildhost-01",
    )
    agent_url = f"{server_url.rstrip('/')}/api/setup/v1/agent-seed/win-x64/AutopilotAgent.exe"
    bootstrap_token = _read_build_host_bootstrap_token()
    upgrade_literal = "$true" if body.upgrade_agent else "$false"
    ps = f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$configPath = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\\AutopilotAgent\\agent.json'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $configPath) | Out-Null
if (Test-Path -LiteralPath $configPath) {{
  Set-ItemProperty -LiteralPath $configPath -Name IsReadOnly -Value $false
  $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
}} else {{
  $config = [pscustomobject]@{{}}
}}
$config | Add-Member -NotePropertyName serverUrl -NotePropertyValue {_ps_quote(server_url)} -Force
$config | Add-Member -NotePropertyName phase -NotePropertyValue 'build-host' -Force
$config | Add-Member -NotePropertyName role -NotePropertyValue 'build-host' -Force
$config | Add-Member -NotePropertyName vmid -NotePropertyValue {vmid} -Force
$config | Add-Member -NotePropertyName agentId -NotePropertyValue {_ps_quote(expected_agent)} -Force
$config | Add-Member -NotePropertyName bootstrapToken -NotePropertyValue {_ps_quote(bootstrap_token)} -Force
$config | Add-Member -NotePropertyName agentToken -NotePropertyValue $null -Force
$config | Add-Member -NotePropertyName capabilities -NotePropertyValue @('install_build_prerequisites','fetch_source_bundle','build_agent_msi','build_winpe','build_cloudosd','build_osdeploy','publish_artifacts') -Force
$config | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $configPath -Encoding UTF8
$service = Get-CimInstance Win32_Service -Filter "Name='AutopilotAgent'" -ErrorAction SilentlyContinue
$exe = 'C:\\Program Files\\ProxmoxVEAutopilot\\AutopilotAgent\\AutopilotAgent.exe'
if (-not (Test-Path -LiteralPath $exe) -and $service -and $service.PathName) {{
  $raw = $service.PathName.Trim()
  if ($raw.StartsWith('"')) {{ $exe = ($raw -replace '^"([^"]+)".*$', '$1') }}
}}
$programDataExe = Join-Path (Split-Path -Parent $configPath) 'AutopilotAgent.exe'
$targetExePaths = @($exe, $programDataExe)
$targetExePaths = $targetExePaths | Where-Object {{
  $_ -and (Test-Path -LiteralPath (Split-Path -Parent $_))
}} | Select-Object -Unique
if ({upgrade_literal} -and $targetExePaths.Count -gt 0) {{
  Stop-Service -Name AutopilotAgent -Force -ErrorAction SilentlyContinue
  Stop-ScheduledTask -TaskName 'ProxmoxVEAutopilotBuildHostAgent' -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2
  Get-Process -Name AutopilotAgent -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  $tmp = "$programDataExe.new"
  $curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
  if ($curl) {{
    & $curl --fail --silent --show-error --location --connect-timeout 15 --max-time 180 -o $tmp {_ps_quote(agent_url)}
    if ($LASTEXITCODE -ne 0) {{ throw "curl.exe failed to download seed agent with exit code $LASTEXITCODE" }}
  }} else {{
    Invoke-WebRequest -UseBasicParsing -Uri {_ps_quote(agent_url)} -OutFile $tmp
  }}
  if ((Get-Item -LiteralPath $tmp).Length -lt 1048576) {{ throw 'downloaded seed agent is unexpectedly small' }}
  foreach ($targetExe in $targetExePaths) {{
    Copy-Item -LiteralPath $tmp -Destination $targetExe -Force
  }}
  Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}}
$service = Get-CimInstance Win32_Service -Filter "Name='AutopilotAgent'" -ErrorAction SilentlyContinue
if ($service) {{
  Start-Service -Name AutopilotAgent
}} elseif (Get-ScheduledTask -TaskName 'ProxmoxVEAutopilotBuildHostAgent' -ErrorAction SilentlyContinue) {{
  Start-ScheduledTask -TaskName 'ProxmoxVEAutopilotBuildHostAgent'
}} else {{
  throw 'AutopilotAgent service or scheduled task was not found'
}}
[pscustomobject]@{{
  ok = $true
  vmid = {vmid}
  serverUrl = {_ps_quote(server_url)}
  expectedAgentId = {_ps_quote(expected_agent)}
  expectedComputerName = {_ps_quote(expected_computer)}
}} | ConvertTo-Json -Compress
""".strip()
    status = await asyncio.to_thread(_guest_exec_ps_status, str(node), vmid, ps, timeout_s=300)
    out = str(status.get("out") or "").strip()
    if not status.get("ok"):
        detail = str(status.get("error") or "build-host QGA repair failed")
        err = str(status.get("err") or "").strip()
        if err and err not in detail:
            detail = f"{detail}: {err[:400]}"
        qga_unavailable = any(
            marker in detail.casefold()
            for marker in (
                "guest agent is not running",
                "qemu guest agent is not running",
                "qga command",
                "agent/exec",
                "got timeout",
            )
        )
        if body.allow_reboot and qga_unavailable:
            try:
                _proxmox_api(
                    f"/nodes/{node}/qemu/{vmid}/status/reset",
                    method="POST",
                    data={},
                )
            except Exception as reboot_exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"{detail[:500]}; build-host reboot fallback also failed: {reboot_exc}",
                )
            return {
                "ok": True,
                "result": {
                    "rebooted": True,
                    "vmid": vmid,
                    "serverUrl": server_url,
                    "expectedAgentId": expected_agent,
                    "expectedComputerName": expected_computer,
                    "reason": "QGA was unavailable during build-host repair; Proxmox reset was requested",
                    "next_expected_state": "wait_for_qga_then_rerun_repair",
                },
            }
        raise HTTPException(status_code=502, detail=detail[:800])
    if not out:
        raise HTTPException(status_code=502, detail="build-host QGA repair did not return success")
    try:
        result = json.loads(out)
    except Exception:
        result = {"raw": out}
    return {"ok": True, "result": result}


_BUILD_HOST_WORK_KINDS = {
    "install_build_prerequisites",
    "fetch_source_bundle",
    "build_agent_msi",
    "build_winpe",
    "build_cloudosd",
    "build_osdeploy",
    "publish_artifacts",
}


@app.post("/api/setup/v1/build-host/workloads", status_code=202)
async def setup_queue_build_host_workloads(body: _BuildHostWorkloadsBody | None = None):
    body = body or _BuildHostWorkloadsBody()
    data = _setup_readiness()
    build_host = data["build_host"]
    agent_id = build_host.get("expected_agent_id")
    if not agent_id:
        raise HTTPException(status_code=409, detail="build-host agent identity is not published")
    vmid = int(build_host.get("vmid") or 0)
    kinds = body.kinds or [
        "install_build_prerequisites",
        "fetch_source_bundle",
        "build_agent_msi",
        "build_winpe",
        "build_cloudosd",
        "publish_artifacts",
    ]
    bad = [kind for kind in kinds if kind not in _BUILD_HOST_WORK_KINDS]
    if bad:
        raise HTTPException(status_code=400, detail=f"unsupported build-host work kinds: {bad}")
    controller_url = (data["controller"].get("url") or "").rstrip("/")
    request_base = {
        "controller_url": controller_url,
        "source_bundle_url": f"{controller_url}/api/setup/v1/source-bundle.zip",
        "work_root": r"C:\BuildRoot\ProxmoxVEAutopilot",
        "runtime_identifiers": ["win-x64", "win-arm64"],
        "install_adk": body.install_adk,
        "adk_url": "https://go.microsoft.com/fwlink/?linkid=2289980",
        "winpe_addon_url": "https://go.microsoft.com/fwlink/?linkid=2289981",
        "osdeploy_version": "26.1.30.5",
        "osdbuilder_version": "24.10.8.1",
        "adk_version": "10.1.26100.1",
        "image_name": "Windows 11 Enterprise Evaluation",
        "image_index": 1,
        "os_version": "Windows 11",
        "os_edition": "Enterprise",
        "os_language": "en-us",
        "native_media_build": True,
        "source_manifest": _repo_git_metadata_for_bundle(),
    }
    queued: list[dict] = []
    skipped: list[dict] = []
    from web import db_pg

    with db_pg.connection(_database_url()) as conn:
        agent_telemetry_pg.init(conn)
        if not agent_telemetry_pg.get_device(conn, agent_id):
            raise HTTPException(status_code=409, detail=f"build-host agent is not registered: {agent_id}")
        for kind in kinds:
            existing = conn.execute(
                """
                SELECT *
                FROM agent_work_items
                WHERE agent_id = %s AND kind = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (agent_id, kind),
            ).fetchone()
            if existing and existing["status"] in {"pending", "claimed"}:
                skipped.append({"kind": kind, "reason": existing["status"], "id": str(existing["id"])})
                continue
            if existing and existing["status"] == "complete" and not body.force:
                skipped.append({"kind": kind, "reason": "already_complete", "id": str(existing["id"])})
                continue
            row = agent_telemetry_pg.create_work_item(
                conn,
                agent_id=agent_id,
                kind=kind,
                vmid=vmid or None,
                request={**request_base, "kind": kind},
            )
            queued.append({
                "id": row["id"],
                "agent_id": row["agent_id"],
                "kind": row["kind"],
                "status": row["status"],
            })
    return {"ok": True, "queued": queued, "skipped": skipped}


@app.post("/api/setup/v1/artifacts/promote")
async def setup_promote_artifacts(body: _PromoteSetupArtifactsBody | None = None):
    from web import setup_artifacts

    body = body or _PromoteSetupArtifactsBody()
    data = _setup_readiness()
    node = body.node or data["state"].get("pve_node") or _configured_proxmox_node()
    storage = body.storage or data["state"].get("pve_iso_storage") or "local"
    selected = set(body.artifact_ids or [])
    if body.already_copied and not selected:
        raise HTTPException(
            status_code=400,
            detail="artifact_ids is required when already_copied is true",
        )
    promoted: list[dict] = []
    deferred: list[dict] = []
    artifacts = setup_artifacts.list_artifacts()
    max_api_upload_bytes = _setup_promote_api_upload_max_bytes()
    for artifact in artifacts:
        if selected and artifact.get("artifact_id") not in selected:
            continue
        if artifact.get("proxmox_volid"):
            continue
        if artifact.get("kind") not in {"winpe-iso", "cloudosd-iso", "osdeploy-iso"}:
            continue
        path = Path(artifact.get("path") or "")
        if not path.is_file():
            continue
        if not body.already_copied:
            size_bytes = path.stat().st_size
            if max_api_upload_bytes and size_bytes > max_api_upload_bytes:
                deferred.append({
                    "artifact_id": artifact.get("artifact_id"),
                    "kind": artifact.get("kind"),
                    "filename": path.name,
                    "size_bytes": size_bytes,
                    "reason": "pve_pull_required_for_large_artifact",
                    "max_api_upload_bytes": max_api_upload_bytes,
                })
                continue
            _proxmox_upload_file(
                f"/nodes/{node}/storage/{storage}/upload",
                path,
                data={"content": "iso"},
                field_name="filename",
                content_type="application/octet-stream",
            )
        volid = f"{storage}:iso/{path.name}"
        if artifact.get("kind") == "osdeploy-iso":
            _register_promoted_setup_osdeploy_artifact(
                artifact=artifact,
                volid=volid,
                rows=artifacts,
            )
        if artifact.get("kind") == "cloudosd-iso":
            _register_promoted_setup_cloudosd_artifact(
                artifact=artifact,
                volid=volid,
                rows=artifacts,
            )
        row = setup_artifacts.mark_promoted(artifact["artifact_id"], proxmox_volid=volid)
        if row:
            promoted.append(row)
    if promoted:
        state = _read_json_file(SETUP_STATE_PATH)
        state["promoted_artifacts_ready"] = True
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        SETUP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETUP_STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    return {"ok": True, "promoted": promoted, "deferred": deferred}


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    data = _setup_readiness()
    return templates.TemplateResponse("setup.html", {
        "request": request,
        "setup": data,
        "build_sha": (_APP_VERSION.get("sha_short") or "unknown"),
        "build_time": _APP_VERSION.get("build_time", ""),
    })


_BLISS_JPG = BASE_DIR / "files" / "bliss.jpg"
_LOGO_SVG = BASE_DIR / "files" / "logo.svg"
_QGA_RECOVERY_SCRIPT = BASE_DIR / "files" / "qga-recovery" / "recovery-script.ps1"


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


@app.get("/api/qga/recovery-script.ps1")
async def qga_recovery_script():
    if not _QGA_RECOVERY_SCRIPT.exists():
        raise HTTPException(404, "QGA recovery script not baked into image")
    return FileResponse(
        _QGA_RECOVERY_SCRIPT,
        media_type="text/plain; charset=utf-8",
        filename="recovery-script.ps1",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/qga/recovery-command.txt")
async def qga_recovery_command(request: Request):
    proto = (
        request.headers.get("x-forwarded-proto")
        or request.url.scheme
        or "https"
    ).split(",", 1)[0].strip()
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    ).split(",", 1)[0].strip()
    if host and host not in {"127.0.0.1:5000", "localhost:5000"}:
        base_url = f"{proto}://{host}"
    else:
        base_url = str(request.base_url).rstrip("/")
    script_url = f"{base_url}/api/qga/recovery-script.ps1"
    command = "\n".join([
        "$script = Join-Path $env:TEMP 'recovery-script.ps1'",
        (
            "Invoke-WebRequest -UseBasicParsing "
            f"-Uri '{script_url}' -OutFile $script"
        ),
        (
            "powershell.exe -ExecutionPolicy Bypass -NoProfile "
            "-File $script -TaskIntervalMinutes 5 -RestartIntervalMinutes 10"
        ),
        "",
    ])
    return Response(
        command,
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="QgaWatchdogRecovery-command.txt"',
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
    from web import db_pg

    with db_pg.connection(_database_url()) as conn:
        sequences_db.init(conn)
        sequences_db.seed_defaults(conn, _cipher())
    global _SEQUENCES_READY
    _SEQUENCES_READY = True


def _init_jobs_database() -> None:
    from web import db_pg, service_health_pg

    with db_pg.connection(_database_url()) as conn:
        jobs_db.init(conn)
        service_health_pg.init(conn)


@app.on_event("startup")
def _init_jobs_db_and_migrate() -> None:
    _init_jobs_database()
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


def _ts_engine_database_url() -> str:
    return os.environ.get("AUTOPILOT_TS_ENGINE_DATABASE_URL", "").strip()


def _database_url() -> str:
    from web import db_pg

    return db_pg.database_url()


def _init_app_database() -> None:
    from web import (
        agent_telemetry_pg,
        cloudosd_cache,
        cloudosd_pg,
        db_pg,
        deployment_health_pg,
        device_history_pg,
        devices_pg,
        lab_bubbles_pg,
        machine_lifecycle_pg,
        osdeploy_cache,
        osdeploy_pg,
        ts_engine_pg,
    )

    with db_pg.connection(_database_url()) as conn:
        ts_engine_pg.init(conn)
        device_history_pg.init(conn)
        devices_pg.init(conn)
        machine_lifecycle_pg.init(conn)
        agent_telemetry_pg.init(conn)
        cloudosd_pg.init(conn)
        cloudosd_cache.init(conn)
        osdeploy_pg.init(conn)
        osdeploy_cache.init(conn)
        deployment_health_pg.init(conn)
        lab_bubbles_pg.init(conn)


class _BubbleCreate(BaseModel):
    name: str
    description: str = ""
    lifecycle_state: str = "planned"
    domain_name: str = ""
    netbios_name: str = ""
    cidr: str = ""
    gateway_ip: str = ""
    planned_bridge: str = ""
    planned_vlan: Optional[int] = None
    isolation_status: str = "planned"
    dhcp_scope: str = ""
    dhcp_pool_start: str = ""
    dhcp_pool_end: str = ""


class _BubblePatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    lifecycle_state: Optional[str] = None
    domain_name: Optional[str] = None
    netbios_name: Optional[str] = None
    cidr: Optional[str] = None
    gateway_ip: Optional[str] = None
    planned_bridge: Optional[str] = None
    planned_vlan: Optional[int] = None
    isolation_status: Optional[str] = None
    dhcp_scope: Optional[str] = None
    dhcp_pool_start: Optional[str] = None
    dhcp_pool_end: Optional[str] = None
    dc_ready: Optional[bool] = None
    dns_ready: Optional[bool] = None
    dhcp_ready: Optional[bool] = None
    workload_ready: Optional[bool] = None


class _BubbleAssetCreate(BaseModel):
    asset_type: str
    asset_role: str
    vmid: Optional[int] = None
    vm_uuid: Optional[str] = None
    run_id: Optional[str] = None
    agent_id: Optional[str] = None
    service_id: Optional[str] = None
    membership_state: str = "active"
    evidence_state: str = "unknown"
    notes: str = ""


class _BubbleServiceCreate(BaseModel):
    service_kind: str
    service_name: str
    scope: str = "bubble_local"
    provider_asset_id: Optional[str] = None
    consumer_refs: list = Field(default_factory=list)
    readiness_state: str = "unknown"
    evidence_summary: dict = Field(default_factory=dict)


class _BubbleAssetPatch(BaseModel):
    asset_role: Optional[str] = None
    vmid: Optional[int] = None
    vm_uuid: Optional[str] = None
    run_id: Optional[str] = None
    agent_id: Optional[str] = None
    service_id: Optional[str] = None
    membership_state: Optional[str] = None
    evidence_state: Optional[str] = None
    notes: Optional[str] = None


class _BubbleAssetMove(BaseModel):
    target_bubble_id: str
    reason: str = ""


class _BubbleServicePatch(BaseModel):
    service_kind: Optional[str] = None
    service_name: Optional[str] = None
    scope: Optional[str] = None
    provider_asset_id: Optional[str] = None
    consumer_refs: Optional[list] = None
    readiness_state: Optional[str] = None
    evidence_summary: Optional[dict] = None


def _api_bubble_or_404(conn, lab_bubbles_pg, bubble_id: str) -> dict:
    bubble = lab_bubbles_pg.get_bubble(conn, bubble_id)
    if bubble is None:
        raise HTTPException(status_code=404, detail="bubble not found")
    return bubble


def _api_asset_in_bubble_or_404(conn, lab_bubbles_pg, bubble_id: str, asset_id: str) -> dict:
    for asset in lab_bubbles_pg.list_assets(conn, bubble_id):
        if asset["id"] == asset_id:
            return asset
    raise HTTPException(status_code=404, detail="asset not found in bubble")


def _api_service_in_bubble_or_404(conn, lab_bubbles_pg, bubble_id: str, service_id: str) -> dict:
    for service in lab_bubbles_pg.list_services(conn, bubble_id):
        if service["id"] == service_id:
            return service
    raise HTTPException(status_code=404, detail="service not found in bubble")


def _patch_fields(body: BaseModel, *, nullable: set[str]) -> dict:
    updates = body.model_dump(exclude_unset=True)
    invalid_nulls = sorted(
        key for key, value in updates.items()
        if value is None and key not in nullable
    )
    if invalid_nulls:
        joined = ", ".join(invalid_nulls)
        raise HTTPException(status_code=400, detail=f"null is not allowed for: {joined}")
    return updates


@app.get("/api/bubbles")
def api_bubbles_list():
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        return {"bubbles": lab_bubbles_pg.list_bubbles(conn)}


@app.post("/api/bubbles", status_code=201)
def api_bubbles_create(body: _BubbleCreate):
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        try:
            return lab_bubbles_pg.create_bubble(conn, **body.model_dump())
        except psycopg.errors.UniqueViolation as exc:
            raise HTTPException(status_code=409, detail="bubble already exists") from exc


@app.get("/api/bubbles/{bubble_id}")
def api_bubbles_get(bubble_id: str):
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        return _api_bubble_or_404(conn, lab_bubbles_pg, bubble_id)


@app.patch("/api/bubbles/{bubble_id}")
def api_bubbles_patch(bubble_id: str, body: _BubblePatch):
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        try:
            return lab_bubbles_pg.update_bubble(
                conn,
                bubble_id,
                **_patch_fields(body, nullable={"planned_vlan"}),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/api/bubbles/{bubble_id}", status_code=204)
def api_bubbles_delete(bubble_id: str):
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        if not lab_bubbles_pg.delete_bubble(conn, bubble_id):
            raise HTTPException(status_code=404, detail="bubble not found")
        return Response(status_code=204)


@app.get("/api/bubbles/{bubble_id}/readiness")
def api_bubbles_readiness(bubble_id: str):
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        bubble = _api_bubble_or_404(conn, lab_bubbles_pg, bubble_id)
        return {
            "bubble": bubble,
            "assets": lab_bubbles_pg.list_assets(conn, bubble_id),
            "services": lab_bubbles_pg.list_services(conn, bubble_id),
        }


@app.get("/api/bubbles/{bubble_id}/assets")
def api_bubbles_assets_list(bubble_id: str):
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        _api_bubble_or_404(conn, lab_bubbles_pg, bubble_id)
        return {"assets": lab_bubbles_pg.list_assets(conn, bubble_id)}


@app.post("/api/bubbles/{bubble_id}/assets", status_code=201)
def api_bubbles_assets_create(bubble_id: str, body: _BubbleAssetCreate):
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        _api_bubble_or_404(conn, lab_bubbles_pg, bubble_id)
        try:
            return lab_bubbles_pg.add_asset(
                conn,
                bubble_id,
                **body.model_dump(),
                actor="operator",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/bubbles/{bubble_id}/assets/{asset_id}")
def api_bubbles_assets_patch(bubble_id: str, asset_id: str, body: _BubbleAssetPatch):
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        try:
            _api_bubble_or_404(conn, lab_bubbles_pg, bubble_id)
            _api_asset_in_bubble_or_404(conn, lab_bubbles_pg, bubble_id, asset_id)
            return lab_bubbles_pg.update_asset(
                conn,
                asset_id,
                **_patch_fields(
                    body,
                    nullable={"vmid", "vm_uuid", "run_id", "agent_id", "service_id"},
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/bubbles/{bubble_id}/assets/{asset_id}/move")
def api_bubbles_assets_move(bubble_id: str, asset_id: str, body: _BubbleAssetMove):
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        try:
            _api_bubble_or_404(conn, lab_bubbles_pg, bubble_id)
            _api_asset_in_bubble_or_404(conn, lab_bubbles_pg, bubble_id, asset_id)
            _api_bubble_or_404(conn, lab_bubbles_pg, body.target_bubble_id)
            return lab_bubbles_pg.move_asset(
                conn,
                asset_id,
                body.target_bubble_id,
                reason=body.reason,
                actor="operator",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/bubbles/{bubble_id}/services")
def api_bubbles_services_list(bubble_id: str):
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        _api_bubble_or_404(conn, lab_bubbles_pg, bubble_id)
        return {"services": lab_bubbles_pg.list_services(conn, bubble_id)}


@app.post("/api/bubbles/{bubble_id}/services", status_code=201)
def api_bubbles_services_create(bubble_id: str, body: _BubbleServiceCreate):
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        _api_bubble_or_404(conn, lab_bubbles_pg, bubble_id)
        try:
            return lab_bubbles_pg.add_service(conn, bubble_id, **body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/bubbles/{bubble_id}/services/{service_id}")
def api_bubbles_services_patch(
    bubble_id: str,
    service_id: str,
    body: _BubbleServicePatch,
):
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        try:
            _api_bubble_or_404(conn, lab_bubbles_pg, bubble_id)
            _api_service_in_bubble_or_404(conn, lab_bubbles_pg, bubble_id, service_id)
            return lab_bubbles_pg.update_service(
                conn,
                service_id,
                **_patch_fields(body, nullable={"provider_asset_id"}),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/bubbles/{bubble_id}/services/{service_id}", status_code=204)
def api_bubbles_services_delete(bubble_id: str, service_id: str):
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        _api_bubble_or_404(conn, lab_bubbles_pg, bubble_id)
        _api_service_in_bubble_or_404(conn, lab_bubbles_pg, bubble_id, service_id)
        if not lab_bubbles_pg.delete_service(conn, service_id, actor="operator"):
            raise HTTPException(status_code=404, detail="service not found")
        return Response(status_code=204)


@app.get("/api/bubbles/{bubble_id}/audit-events")
def api_bubbles_audit_events(bubble_id: str):
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        _api_bubble_or_404(conn, lab_bubbles_pg, bubble_id)
        return {"events": lab_bubbles_pg.list_audit_events(conn, bubble_id)}


def _init_ts_engine_database_if_configured() -> bool:
    dsn = _ts_engine_database_url()
    if not dsn:
        return False
    from web import ts_engine_pg

    with ts_engine_pg.connect(dsn) as conn:
        ts_engine_pg.init(conn)
    return True


def _setup_artifact_peer(
    rows: list[dict],
    *,
    artifact: dict,
    kind: str,
) -> dict | None:
    work_item_id = artifact.get("work_item_id") or ""
    if not work_item_id:
        return None
    artifact_stem = Path(
        artifact.get("filename")
        or Path(str(artifact.get("path") or "")).name
    ).stem
    matches: list[dict] = []
    for row in rows:
        if row.get("artifact_id") == artifact.get("artifact_id"):
            continue
        if row.get("work_item_id") != work_item_id:
            continue
        if row.get("kind") == kind:
            matches.append(row)
    if not matches:
        return None
    if artifact_stem:
        for row in matches:
            row_stem = Path(
                row.get("filename")
                or Path(str(row.get("path") or "")).name
            ).stem
            if row_stem == artifact_stem:
                return row
    return matches[0]


def _manifest_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _register_promoted_setup_osdeploy_artifact(
    *,
    artifact: dict,
    volid: str,
    rows: list[dict],
) -> dict:
    from web import db_pg, osdeploy_pg

    manifest_row = _setup_artifact_peer(rows, artifact=artifact, kind="manifest")
    wim_row = _setup_artifact_peer(rows, artifact=artifact, kind="wim")
    if not manifest_row or not wim_row:
        raise HTTPException(
            status_code=409,
            detail="OSDeploy promotion requires manifest and WIM artifacts from the same work item",
        )
    manifest_path = Path(manifest_row.get("path") or "")
    wim_path = Path(wim_row.get("path") or "")
    iso_path = Path(artifact.get("path") or "")
    if not manifest_path.is_file() or not wim_path.is_file() or not iso_path.is_file():
        raise HTTPException(
            status_code=409,
            detail="OSDeploy promotion requires local ISO, WIM, and manifest files",
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"invalid OSDeploy manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=409, detail="invalid OSDeploy manifest")

    with db_pg.connection(_database_url()) as conn:
        osdeploy_pg.init(conn)
        return osdeploy_pg.create_artifact(
            conn,
            architecture=str(manifest.get("architecture") or osdeploy_pg.DEFAULT_ARCHITECTURE),
            osdeploy_module_version=str(
                manifest.get("osdeploy_module_version")
                or osdeploy_pg.DEFAULT_OSDEPLOY_MODULE_VERSION
            ),
            osdbuilder_module_version=str(
                manifest.get("osdbuilder_module_version")
                or osdeploy_pg.DEFAULT_OSDBUILDER_MODULE_VERSION
            ),
            adk_version=str(manifest.get("adk_version") or osdeploy_pg.DEFAULT_ADK_VERSION),
            build_sha=str(manifest.get("build_sha") or artifact.get("work_item_id") or "unknown"),
            iso_path=str(iso_path),
            wim_path=str(wim_path),
            manifest_path=str(manifest_path),
            iso_sha256=str(manifest.get("iso_sha256") or artifact.get("sha256") or ""),
            wim_sha256=str(manifest.get("wim_sha256") or wim_row.get("sha256") or ""),
            source_media=str(manifest.get("source_media") or "Windows Server media"),
            image_name=str(manifest.get("image_name") or osdeploy_pg.DEFAULT_OS_VERSION),
            image_index=int(manifest.get("image_index") or 1),
            os_version=str(manifest.get("os_version") or osdeploy_pg.DEFAULT_OS_VERSION),
            os_edition=str(manifest.get("os_edition") or osdeploy_pg.DEFAULT_OS_EDITION),
            os_language=str(manifest.get("os_language") or osdeploy_pg.DEFAULT_OS_LANGUAGE),
            built_by_host=str(
                manifest.get("built_by_host")
                or artifact.get("producer_agent_id")
                or "build-host"
            ),
            built_at=_manifest_datetime(manifest.get("built_at")),
            proxmox_volid=volid,
            build_job_id=str(artifact.get("work_item_id") or "") or None,
        )


def _register_promoted_setup_cloudosd_artifact(
    *,
    artifact: dict,
    volid: str,
    rows: list[dict],
    strict: bool = True,
) -> dict | None:
    from web import cloudosd_pg, db_pg

    manifest_row = _setup_artifact_peer(rows, artifact=artifact, kind="manifest")
    wim_row = _setup_artifact_peer(rows, artifact=artifact, kind="wim")
    if not manifest_row or not wim_row:
        if not strict:
            return None
        raise HTTPException(
            status_code=409,
            detail="CloudOSD promotion requires manifest and WIM artifacts from the same work item",
        )
    manifest_path = Path(manifest_row.get("path") or "")
    wim_path = Path(wim_row.get("path") or "")
    iso_path = Path(artifact.get("path") or "")
    if not manifest_path.is_file() or not wim_path.is_file() or not iso_path.is_file():
        if not strict:
            return None
        raise HTTPException(
            status_code=409,
            detail="CloudOSD promotion requires local ISO, WIM, and manifest files",
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        if not strict:
            return None
        raise HTTPException(status_code=409, detail=f"invalid CloudOSD manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        if not strict:
            return None
        raise HTTPException(status_code=409, detail="invalid CloudOSD manifest")

    iso_sha256 = str(manifest.get("iso_sha256") or artifact.get("sha256") or "").lower()
    wim_sha256 = str(manifest.get("wim_sha256") or wim_row.get("sha256") or "").lower()
    architecture = str(manifest.get("architecture") or cloudosd_pg.DEFAULT_ARCHITECTURE)
    build_sha = str(manifest.get("build_sha") or artifact.get("work_item_id") or "unknown")

    with db_pg.connection(_database_url()) as conn:
        cloudosd_pg.init(conn)
        existing = conn.execute(
            """
            SELECT *
            FROM cloudosd_artifacts
            WHERE iso_sha256 = %s OR proxmox_volid = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (iso_sha256, volid),
        ).fetchone()
        if existing:
            existing_id = str(existing["id"])
            if existing["proxmox_volid"] != volid:
                return cloudosd_pg.update_artifact_proxmox_volid(
                    conn,
                    artifact_id=existing_id,
                    proxmox_volid=volid,
                    publish_job_id=str(artifact.get("work_item_id") or "") or None,
                )
            return cloudosd_pg.get_artifact(conn, existing_id)
        return cloudosd_pg.create_artifact(
            conn,
            architecture=architecture,
            osdcloud_module_version=str(
                manifest.get("osdcloud_module_version")
                or cloudosd_pg.DEFAULT_OSDCLOUD_MODULE_VERSION
            ),
            build_sha=build_sha,
            iso_path=str(iso_path),
            wim_path=str(wim_path),
            manifest_path=str(manifest_path),
            iso_sha256=iso_sha256,
            wim_sha256=wim_sha256,
            built_by_host=str(
                manifest.get("built_by_host")
                or artifact.get("producer_agent_id")
                or "build-host"
            ),
            built_at=_manifest_datetime(manifest.get("built_at")),
            proxmox_volid=volid,
            build_job_id=str(artifact.get("work_item_id") or "") or None,
        )


def _reconcile_setup_cloudosd_artifacts() -> list[dict]:
    from web import setup_artifacts

    rows = setup_artifacts.list_artifacts()
    registered: list[dict] = []
    for artifact in rows:
        if artifact.get("kind") != "cloudosd-iso":
            continue
        volid = str(artifact.get("proxmox_volid") or "")
        if not volid:
            continue
        row = _register_promoted_setup_cloudosd_artifact(
            artifact=artifact,
            volid=volid,
            rows=rows,
            strict=False,
        )
        if row:
            registered.append(row)
    return registered


@app.on_event("startup")
def _init_ts_engine_pg() -> None:
    _init_app_database()


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
    return _APP_VERSION.get("sha_short") or "unknown"


@app.on_event("startup")
async def _start_health_heartbeat() -> None:
    import asyncio
    import logging as _logging
    from web import service_health_pg as service_health
    logger = _logging.getLogger("web.health")
    initialized = False

    async def _loop():
        nonlocal initialized
        while True:
            try:
                if not initialized:
                    service_health.init()
                    initialized = True
                service_health.heartbeat(
                    service_id="web",
                    service_type="web",
                    version_sha=_load_version_sha(),
                    detail="idle",
                )
            except Exception:
                logger.exception("heartbeat failed")
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
    device_history_db.update_keytab_probe(
        keytab_path=probe.keytab_path,
        keytab_mtime=probe.keytab_mtime,
        keytab_principal=probe.keytab_principal,
        keytab_kvno_local=probe.keytab_kvno_local,
        keytab_kvno_ad=probe.keytab_kvno_ad,
        last_probe_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        last_probe_status=probe.status,
        last_probe_message=probe.message,
        last_kinit_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        last_kinit_ok=probe.kinit_ok,
        last_kinit_error=probe.kinit_error,
    )
    _log.info("keytab probe: %s (%s)", probe.status, probe.message)

    # Refresh when:
    #   - probe says missing / broken / kvno-mismatch (anything but OK
    #     or STALE — STALE means refresher just slipped a bit, normal
    #     daily cadence will catch it), OR
    #   - last_refresh_at is >24h ago (daily cadence).
    current = device_history_db.get_keytab_health() or {}
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
            ok=False,
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
    try:
        return sequences_db.list_vm_provisioning_vmids(SEQUENCES_DB)
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


def _qga_detail_probing_enabled() -> bool:
    return str(os.environ.get("AUTOPILOT_MONITOR_QGA_DETAILS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


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
        #   1. PVE config/name for the "what we provisioned" baseline
        #      — always available, proves-out Intune lookups even for
        #      stopped VMs that match by serial.
        #   2. Optional guest-exec for authoritative Windows-side values
        #      (renames, OS build, dsreg). This is disabled by default:
        #      repeated background QGA RPCs can wedge Windows qemu-ga.
        raw = {}
        use_qga_details = _qga_detail_probing_enabled()

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

        if use_qga_details:
            try:
                raw = _fetch_guest_windows_details(node, vmid) or {}  # type: ignore[name-defined]
            except NameError:
                raw = {}
            except Exception:
                raw = {}

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

    def _graph_find_entra_device_by_device_id(device_id: str) -> list[dict]:
        q = device_monitor._quote_graph_string(device_id)
        url = (
            "https://graph.microsoft.com/v1.0/devices"
            f"?$filter=deviceId eq '{q}'"
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
        graph_find_entra_device_by_device_id=_graph_find_entra_device_by_device_id,
    )


def _resolve_ad_credential() -> dict:
    """Look up the configured AD credential. Returns
    ``{username, password}`` or ``{}`` if unconfigured."""
    s = device_history_db.get_settings()
    cred_id = s.ad_credential_id
    if not cred_id:
        return {}
    try:
        # sequences_db.get_credential signature is (db, cipher, id) —
        # earlier revision had (db, id, cipher), which raised a silent
        # database bind error and dropped all AD probes.
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


def _read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _auth_is_configured() -> bool:
    cfg = _auth_config()
    return bool(
        cfg.get("local_enabled")
        or cfg.get("entra_configured")
        or (cfg.get("tenant_id") and cfg.get("client_id"))
    )


def _public_setup_state() -> dict:
    raw = _read_json_file(SETUP_STATE_PATH)
    allowed_keys = {
        "schema_version",
        "phase",
        "updated_at",
        "clock",
        "base_url",
        "pve_host_ip",
        "pve_node",
        "pve_bridge",
        "pve_disk_storage",
        "pve_iso_storage",
        "pve_foundation_ready",
        "pve_host_clean_ready",
        "pve_runtime_absent",
        "pve_runtime_stopped",
        "migration_bundle",
        "migration_bundle_ready",
        "migration_postgres_dump_ready",
        "controller_name",
        "controller_vmid",
        "controller_ip",
        "controller_url",
        "controller_node",
        "controller_storage",
        "controller_bridge",
        "controller_cloud_image",
        "controller_vm_ready",
        "controller_source_synced",
        "controller_docker_ready",
        "controller_env_ready",
        "controller_repo_config_ready",
        "controller_seed_agent_ready",
        "controller_compose_ready",
        "controller_runtime_ready",
        "controller_auth_mode",
        "controller_migration_bundle",
        "controller_migration_bundle_restored",
        "docker_ready",
        "apt_ready",
        "repo_config_ready",
        "pve_permissions_ready",
        "seed_agent_ready",
        "postgres_database_ready",
        "web_image_ready",
        "web_image_tag",
        "compose_ready",
        "console_health_ready",
        "windows_iso_ready",
        "windows_iso_volid",
        "windows_iso_download_attempted",
        "windows_iso_download_source",
        "windows_iso_download_language",
        "windows_iso_download_product",
        "windows_iso_download_sku",
        "windows_iso_download_expires_at",
        "windows_iso_download_error",
        "virtio_iso_ready",
        "virtio_iso_volid",
        "media_ready",
        "bootstrap_media_ready",
        "build_host_creation_owner",
        "seed_iso_ready",
        "seed_iso_volid",
        "build_host_unattend_ready",
        "build_host_agent_auto_approve",
        "build_host_expected_agent_id",
        "build_host_expected_computer_name",
        "build_host_admin_user",
        "build_host_vm_ready",
        "build_host_vmid",
        "build_host_name",
        "build_host_node",
        "buildhost_vm_ready",
        "buildhost_vmid",
        "buildhost_node",
        "buildhost_seed_iso_volid",
        "build_host_agent_ready",
        "promoted_artifacts_ready",
        "web_image_git_sha",
        "web_image_build_time",
    }
    state = {key: raw.get(key) for key in sorted(allowed_keys) if key in raw}
    if "build_host_vm_ready" not in state and "buildhost_vm_ready" in state:
        state["build_host_vm_ready"] = state["buildhost_vm_ready"]
    if "build_host_vmid" not in state and "buildhost_vmid" in state:
        state["build_host_vmid"] = state["buildhost_vmid"]
    if "build_host_node" not in state and "buildhost_node" in state:
        state["build_host_node"] = state["buildhost_node"]
    if "seed_iso_volid" not in state and "buildhost_seed_iso_volid" in state:
        state["seed_iso_volid"] = state["buildhost_seed_iso_volid"]
    if "seed_iso_ready" not in state and state.get("seed_iso_volid"):
        state["seed_iso_ready"] = True
    return state


def _seed_manifest_summary() -> dict:
    manifest = _read_json_file(AGENT_SEED_MANIFEST_PATH)
    files = manifest.get("files") or []
    exe_files = [
        item for item in files
        if str(item.get("path", "")).lower().endswith("autopilotagent.exe")
    ]
    return {
        "exists": bool(manifest),
        "schema_version": manifest.get("schema_version"),
        "producer": manifest.get("producer"),
        "agent_version": manifest.get("agent_version"),
        "git_sha": manifest.get("git_sha"),
        "git_dirty": manifest.get("git_dirty"),
        "build_time": manifest.get("build_time"),
        "sdk_image": manifest.get("sdk_image"),
        "runtime_identifiers": manifest.get("runtime_identifiers") or [],
        "exe_count": len(exe_files),
    }


def _discover_build_host_vm(state: dict) -> dict:
    vmid = state.get("build_host_vmid")
    name = state.get("build_host_name") or "autopilot-buildhost-01"
    if vmid:
        return {
            "vmid": str(vmid),
            "name": name,
            "node": state.get("build_host_node") or state.get("pve_node"),
        }
    try:
        rows = _proxmox_api("/cluster/resources?type=vm") or []
    except Exception:
        return {}
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "qemu":
            continue
        if str(row.get("name") or "").casefold() != str(name).casefold():
            continue
        try:
            discovered_vmid = int(row.get("vmid"))
        except (TypeError, ValueError):
            continue
        return {
            "vmid": str(discovered_vmid),
            "name": str(row.get("name") or name),
            "node": row.get("node") or state.get("build_host_node") or state.get("pve_node"),
        }
    return {}


_BUILD_HOST_ACTIVE_WORK_TIMEOUT_SECONDS = {
    "install_build_prerequisites": 3 * 60 * 60,
    "fetch_source_bundle": 30 * 60,
    "build_agent_msi": 90 * 60,
    "build_winpe": 4 * 60 * 60,
    "build_cloudosd": 4 * 60 * 60,
    "build_osdeploy": 4 * 60 * 60,
    "publish_artifacts": 30 * 60,
}


def _setup_age_seconds(value) -> int | None:
    if not value:
        return None
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return None


def _latest_claimed_build_host_work(conn, agent_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, kind, status, claimed_at, created_at
            FROM agent_work_items
            WHERE agent_id = %s AND status = 'claimed'
              AND NOT EXISTS (
                SELECT 1
                FROM agent_work_items later
                WHERE later.agent_id = agent_work_items.agent_id
                  AND later.status IN ('complete', 'completed', 'failed', 'error', 'cancelled', 'canceled')
                  AND COALESCE(later.completed_at, later.updated_at, later.claimed_at, later.created_at)
                      > COALESCE(agent_work_items.claimed_at, agent_work_items.created_at)
              )
            ORDER BY claimed_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """,
            (agent_id,),
        )
        return cur.fetchone()


def _setup_build_host_status(state: dict) -> dict:
    discovered = _discover_build_host_vm(state)
    vmid = state.get("build_host_vmid") or discovered.get("vmid")
    expected_agent_id = (
        state.get("build_host_expected_agent_id")
        or (
            f"buildhost-{vmid}"
            if vmid
            else ""
        )
    )
    expected_computer = state.get("build_host_expected_computer_name") or "AUTOPILOT-BLD"
    out = {
        "vmid": vmid,
        "name": state.get("build_host_name") or discovered.get("name") or "autopilot-buildhost-01",
        "node": state.get("build_host_node") or discovered.get("node") or state.get("pve_node"),
        "expected_agent_id": expected_agent_id,
        "expected_computer_name": expected_computer,
        "agent_ready": False,
        "agent_state": "missing",
        "last_heartbeat_at": None,
        "last_heartbeat_age_seconds": None,
        "agent_version": None,
        "primary_ipv4": None,
        "active_work": None,
    }
    if not expected_agent_id:
        return out
    active_work = None
    try:
        from web import db_pg

        with db_pg.connection(_database_url()) as conn:
            agent_telemetry_pg.init(conn)
            latest = agent_telemetry_pg.latest_for_agent(conn, expected_agent_id)
            device = agent_telemetry_pg.get_device(conn, expected_agent_id)
            active_work = _latest_claimed_build_host_work(conn, expected_agent_id)
    except Exception:
        return out
    row = latest or device or {}
    if not row:
        return out
    heartbeat_at = row.get("received_at") or row.get("last_seen_at")
    age_seconds = _setup_age_seconds(heartbeat_at)
    computer_name = row.get("computer_name") or row.get("device_computer_name")
    vmid_match = not vmid or str(row.get("vmid") or row.get("device_vmid") or vmid) == str(vmid)
    computer_match = (
        not expected_computer
        or not computer_name
        or str(computer_name).casefold() == str(expected_computer).casefold()
    )
    fresh = age_seconds is not None and age_seconds <= 180
    active_work_summary = None
    active_work_fresh = False
    if active_work:
        work_kind = str(active_work.get("kind") or "")
        claimed_at = active_work.get("claimed_at")
        work_age_seconds = _setup_age_seconds(claimed_at)
        timeout_seconds = _BUILD_HOST_ACTIVE_WORK_TIMEOUT_SECONDS.get(work_kind, 60 * 60)
        active_work_fresh = (
            work_age_seconds is not None
            and work_age_seconds <= timeout_seconds
        )
        active_work_summary = {
            "id": active_work.get("id"),
            "kind": work_kind,
            "status": active_work.get("status"),
            "claimed_at": claimed_at.isoformat() if hasattr(claimed_at, "isoformat") else claimed_at,
            "age_seconds": work_age_seconds,
            "timeout_seconds": timeout_seconds,
            "within_timeout": active_work_fresh,
        }
    busy = bool(latest and active_work_fresh and vmid_match and computer_match)
    ready = bool(latest and (fresh or busy) and vmid_match and computer_match)
    out.update({
        "agent_ready": ready,
        "agent_state": (
            "ready"
            if latest and fresh
            else ("busy" if busy else ("stale" if latest else "registered"))
        ),
        "last_heartbeat_at": heartbeat_at.isoformat() if hasattr(heartbeat_at, "isoformat") else heartbeat_at,
        "last_heartbeat_age_seconds": age_seconds,
        "agent_version": row.get("agent_version") or row.get("device_agent_version"),
        "primary_ipv4": row.get("primary_ipv4"),
        "computer_name": computer_name,
        "active_work": active_work_summary,
    })
    return out


def _setup_artifact_summary() -> dict:
    from web import setup_artifacts

    summary = setup_artifacts.readiness_summary()
    try:
        from web import cloudosd_endpoints, cloudosd_pg, db_pg, osdeploy_endpoints, osdeploy_pg

        _reconcile_setup_cloudosd_artifacts()
        with db_pg.connection(_database_url()) as conn:
            cloudosd_rows = [
                cloudosd_endpoints.enrich_artifact(artifact)
                for artifact in cloudosd_pg.list_artifacts(conn, architecture="amd64")
            ]
            osdeploy_rows = [
                osdeploy_endpoints.enrich_artifact(artifact)
                for artifact in osdeploy_pg.list_artifacts(conn, architecture="amd64")
            ]
    except Exception:
        cloudosd_rows = []
        osdeploy_rows = []
    ready_cloudosd = [row for row in cloudosd_rows if row and row.get("ready")]
    ready_osdeploy = [row for row in osdeploy_rows if row and row.get("ready")]
    summary.update({
        "cloudosd_ready_count": len(ready_cloudosd),
        "osdeploy_ready_count": len(ready_osdeploy),
        "cloudosd_artifacts": cloudosd_rows[:10],
        "osdeploy_artifacts": osdeploy_rows[:10],
    })
    summary["ready"] = bool(summary.get("ready") or ready_cloudosd or ready_osdeploy)
    return summary


def _setup_readiness() -> dict:
    state = _public_setup_state()
    auth_ready = _auth_is_configured()
    controller_url = state.get("controller_url") or state.get("base_url")
    controller_runtime_ready = bool(state.get("controller_runtime_ready") or state.get("console_health_ready"))
    controller_auth_mode = state.get("controller_auth_mode") or _auth_config().get("mode") or "local"
    build_host_status = _setup_build_host_status(state)
    artifact_summary = _setup_artifact_summary()
    artifacts_ready = bool(state.get("promoted_artifacts_ready") or artifact_summary.get("ready"))
    if artifacts_ready:
        state["promoted_artifacts_ready"] = True
    if build_host_status.get("agent_ready"):
        state["build_host_agent_ready"] = True
    checks = [
        ("pve_foundation", "PVE foundation", state.get("pve_foundation_ready") or state.get("apt_ready"), "Install PVE bootstrap essentials and prepare source/secrets."),
        ("pve_permissions", "PVE permissions", state.get("pve_permissions_ready"), "Create API role/token/ACLs."),
        ("controller_vm", "Controller VM", state.get("controller_vm_ready"), "Create and boot the Ubuntu controller VM."),
        ("controller_runtime", "Controller runtime", controller_runtime_ready, "Run controller bootstrap and verify /healthz."),
        ("controller_auth", "Controller auth", auth_ready, "Use local first-run sign-in or configure an external identity provider."),
        ("seed_agent", "Seed agent", state.get("seed_agent_ready") or state.get("controller_seed_agent_ready"), "Build AutopilotAgent from source inside the controller SDK container."),
        ("virtio_iso", "VirtIO ISO", state.get("virtio_iso_ready"), "Download virtio-win.iso from the official virtio-win source."),
        ("windows_iso", "Windows ISO", state.get("windows_iso_ready"), "Upload a Windows ISO or provide an official Microsoft direct ISO URL."),
        ("build_host_unattend", "Build-host answer media", state.get("build_host_unattend_ready") or state.get("seed_iso_ready"), "Generate seed ISO with Autounattend.xml and agent bootstrap payload."),
        ("build_host_vm", "Build-host VM", state.get("build_host_vm_ready"), "Resume bootstrap after Windows and VirtIO ISO media are ready."),
        ("build_host_agent", "Build-host agent", build_host_status.get("agent_ready"), "Repair build-host agent URL and wait for a fresh controller heartbeat."),
        ("artifacts", "Operational artifacts", artifacts_ready, "Publish agent MSI, WinPE ISO, or OSDCloud ISO artifacts from the build host."),
    ]
    normalized = []
    for key, label, ready, next_step in checks:
        normalized.append({
            "key": key,
            "label": label,
            "ready": bool(ready),
            "state": "ready" if ready else "blocked",
            "next_step": next_step,
        })

    blocking = [item for item in normalized if not item["ready"]]
    windows_ready = bool(state.get("windows_iso_ready"))
    virtio_ready = bool(state.get("virtio_iso_ready"))
    build_host_ready = bool(state.get("build_host_vm_ready"))
    if artifacts_ready:
        phase = "operational"
        health = "ready"
    elif build_host_ready:
        phase = "bootstrap"
        health = "blocked"
    elif windows_ready and virtio_ready:
        phase = "media-ready"
        health = "ready"
    elif controller_runtime_ready:
        phase = "media-gated"
        health = "blocked"
    elif state.get("controller_vm_ready"):
        phase = "controller-bootstrap"
        health = "blocked"
    else:
        phase = "foundation"
        health = "blocked"

    return {
        "phase": phase,
        "health": health,
        "blocking_count": len(blocking),
        "checks": normalized,
        "state": state,
        "controller": {
            "name": state.get("controller_name") or "autopilot-controller-01",
            "vmid": state.get("controller_vmid"),
            "ip": state.get("controller_ip"),
            "url": controller_url,
            "node": state.get("controller_node") or state.get("pve_node"),
            "storage": state.get("controller_storage") or state.get("pve_disk_storage"),
            "bridge": state.get("controller_bridge") or state.get("pve_bridge"),
            "runtime_ready": controller_runtime_ready,
            "vm_ready": bool(state.get("controller_vm_ready")),
            "docker_ready": bool(state.get("controller_docker_ready") or state.get("docker_ready")),
            "compose_ready": bool(state.get("controller_compose_ready") or state.get("compose_ready")),
            "source_synced": bool(state.get("controller_source_synced")),
            "auth_mode": controller_auth_mode,
            "local_auth_active": controller_auth_mode == "local",
            "migration_bundle": state.get("controller_migration_bundle") or state.get("migration_bundle"),
            "migration_bundle_restored": bool(state.get("controller_migration_bundle_restored")),
        },
        "media": {
            "iso_storage": state.get("pve_iso_storage") or "local",
            "upload_directory": "/var/lib/vz/template/iso",
            "windows_iso_ready": windows_ready,
            "windows_iso_volid": state.get("windows_iso_volid"),
            "windows_iso_download_attempted": bool(state.get("windows_iso_download_attempted")),
            "windows_iso_download_source": state.get("windows_iso_download_source"),
            "windows_iso_download_language": state.get("windows_iso_download_language"),
            "windows_iso_download_product": state.get("windows_iso_download_product"),
            "windows_iso_download_sku": state.get("windows_iso_download_sku"),
            "windows_iso_download_expires_at": state.get("windows_iso_download_expires_at"),
            "windows_iso_download_error": state.get("windows_iso_download_error"),
            "virtio_iso_ready": virtio_ready,
            "virtio_iso_volid": state.get("virtio_iso_volid"),
        },
        "seed_agent": _seed_manifest_summary(),
        "commands": {
            "resume_foundation": "bash /opt/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase foundation --resume",
            "resume_bootstrap": "bash /opt/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase bootstrap --resume --download-virtio --non-interactive",
            "bootstrap_with_windows_url": "bash /opt/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase bootstrap --resume --download-virtio --windows-iso-url \"<official Microsoft direct ISO URL>\" --non-interactive",
            "generate_build_host_seed_iso": "curl -X POST http://127.0.0.1:5000/api/setup/v1/build-host/seed-iso -H 'Content-Type: application/json' -d '{\"vmid\":100}'",
            "create_build_host_vm": "curl -X POST http://127.0.0.1:5000/api/setup/v1/build-host/vm -H 'Content-Type: application/json' -d '{}'",
            "repair_build_host_agent": "curl -X POST http://127.0.0.1:5000/api/setup/v1/build-host/repair-agent",
            "queue_build_host_workloads": "curl -X POST http://127.0.0.1:5000/api/setup/v1/build-host/workloads",
        },
        "build_host": build_host_status,
        "artifacts": artifact_summary,
        "auth_configured": auth_ready,
        "build": _APP_VERSION,
    }


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


class _StreamingMultipartBody:
    _DEFAULT_READ_SIZE = 1024 * 1024

    def __init__(
        self,
        *,
        fields: dict[str, str],
        file_field: str,
        file_path: Path,
        content_type: str,
    ):
        self.boundary = f"----ProxmoxVEAutopilot{secrets.token_hex(16)}"
        field_parts: list[bytes] = []
        for name, value in fields.items():
            field_parts.append(
                (
                    f"--{self.boundary}\r\n"
                    f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        field_parts.append(
            (
                f"--{self.boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{file_field}\"; "
                f"filename=\"{file_path.name}\"\r\n"
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        self._prefix = io.BytesIO(b"".join(field_parts))
        self._file = file_path.open("rb")
        self._suffix = io.BytesIO(f"\r\n--{self.boundary}--\r\n".encode("utf-8"))
        self._segments = [self._prefix, self._file, self._suffix]
        self._index = 0
        self.length = sum(len(part) for part in field_parts) + file_path.stat().st_size + len(self._suffix.getvalue())
        self.content_type = f"multipart/form-data; boundary={self.boundary}"

    def __len__(self) -> int:
        return self.length

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._DEFAULT_READ_SIZE
        chunks: list[bytes] = []
        remaining = size
        while self._index < len(self._segments) and remaining != 0:
            segment = self._segments[self._index]
            chunk = segment.read(remaining)
            if chunk:
                chunks.append(chunk)
                remaining -= len(chunk)
                if remaining <= 0:
                    break
            else:
                self._index += 1
        return b"".join(chunks)

    def close(self) -> None:
        self._file.close()


def _proxmox_upload_file(
    path: str,
    file_path: Path,
    *,
    data: dict[str, str] | None = None,
    field_name: str = "filename",
    content_type: str = "application/octet-stream",
):
    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")
    url = f"https://{host}:{port}/api2/json{path}"
    body = _StreamingMultipartBody(
        fields={key: str(value) for key, value in (data or {}).items()},
        file_field=field_name,
        file_path=file_path,
        content_type=content_type,
    )
    headers = {
        "Authorization": f"PVEAPIToken={token_id}={token_secret}",
        "Content-Type": body.content_type,
        "Content-Length": str(len(body)),
    }
    try:
        resp = requests.post(url, headers=headers, data=body, verify=False, timeout=600)
        resp.raise_for_status()
        return resp.json().get("data", [])
    finally:
        body.close()


def _proxmox_node_ssh_host(node: str | None) -> str:
    """Resolve a Proxmox node name to the host/IP root SSH should target.

    The API endpoint can be a cluster member that is not the selected VM
    node. Host-local operations like `qm set` and `/var/lib/vz/snippets`
    must run on the node that owns the VM.
    """
    cfg = _load_proxmox_config()
    fallback = cfg.get("proxmox_host", "")
    node_name = (node or cfg.get("proxmox_node") or "").strip()
    if not node_name:
        return fallback
    try:
        for row in _proxmox_api("/cluster/status") or []:
            if row.get("type") == "node" and row.get("name") == node_name and row.get("ip"):
                return str(row["ip"])
    except Exception:
        pass
    try:
        for row in _proxmox_api("/cluster/config/nodes") or []:
            if (row.get("node") or row.get("name")) == node_name and row.get("ring0_addr"):
                return str(row["ring0_addr"])
    except Exception:
        pass
    return fallback


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

    Proxmox tickets are good for ~2 hours by default. Newer runtime
    paths prefer root SSH for host-local QEMU args work, but this helper
    remains for compatibility with older call sites and tests.
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


def _configured_proxmox_node() -> str:
    cfg = _load_proxmox_config()
    return cfg.get("proxmox_node", "pve")


def _resolve_vm_node(vmid: int) -> str:
    """Return the current Proxmox node for a VMID from cluster inventory.

    Operator actions must not assume the configured default node. In a
    multi-node cluster, stale/default node selection makes console and QMP
    calls fail with "configuration file ... does not exist" even when the VM
    exists on another node.
    """
    try:
        rows = _proxmox_api("/cluster/resources?type=vm") or []
    except Exception:
        return _configured_proxmox_node()

    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            row_vmid = int(row.get("vmid"))
        except (TypeError, ValueError):
            continue
        if row_vmid == int(vmid) and row.get("node"):
            return str(row["node"])
    raise ValueError(f"VM {vmid} not found in Proxmox cluster inventory")


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


def _guest_exec_ps_status(node: str, vmid: int, ps: str,
                          timeout_s: int = 20) -> dict:
    """Run PowerShell through the Proxmox guest agent and return raw status.

    Callers that need best-effort probing can use ``_guest_exec_ps`` below.
    Setup/repair endpoints use this status shape so guest-side failures are
    actionable instead of being collapsed into a generic empty response.
    """
    # POST exec with the script as an encoded command. Passing multiline
    # scripts through -Command is fragile once quoting and Proxmox API JSON
    # encoding get involved.
    encoded = base64.b64encode(ps.encode("utf-16le")).decode("ascii")
    try:
        exec_resp = _proxmox_api_post(
            f"/nodes/{node}/qemu/{vmid}/agent/exec",
            data={
                "command": [
                    "powershell.exe", "-NoProfile",
                    "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
                ],
            },
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": f"guest exec rejected: {exc}",
            "exitcode": None,
            "out": "",
            "err": "",
        }
    pid = (exec_resp or {}).get("pid")
    if pid is None:
        return {
            "ok": False,
            "error": f"guest exec response missing pid: {exec_resp!r}",
            "exitcode": None,
            "out": "",
            "err": "",
        }
    # Poll exec-status.
    import time as _time
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        try:
            status = _proxmox_api(
                f"/nodes/{node}/qemu/{vmid}/agent/exec-status?pid={pid}"
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": f"guest exec-status failed: {exc}",
                "exitcode": None,
                "out": "",
                "err": "",
            }
        if status and status.get("exited"):
            exitcode = status.get("exitcode", 1)
            out = (status.get("out-data") or "").strip()
            err = (status.get("err-data") or "").strip()
            return {
                "ok": exitcode == 0,
                "error": "" if exitcode == 0 else (err or out or f"guest exit {exitcode}"),
                "exitcode": exitcode,
                "out": out,
                "err": err,
                "raw": status,
            }
        _time.sleep(0.3)
    return {
        "ok": False,
        "error": f"guest exec timed out after {timeout_s}s",
        "exitcode": None,
        "out": "",
        "err": "",
    }


def _guest_exec_ps(node: str, vmid: int, ps: str,
                   timeout_s: int = 20) -> Optional[str]:
    """Run a PowerShell one-liner via the Proxmox guest agent and return
    the captured stdout (decoded). Returns None on any failure (agent
    unresponsive, exec rejected, non-zero exit). Best-effort — callers
    must handle absence gracefully."""
    status = _guest_exec_ps_status(node, vmid, ps, timeout_s=timeout_s)
    if not status.get("ok"):
        return None
    return str(status.get("out") or "").strip()


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

    use_qga_details = _qga_detail_probing_enabled()

    # Fetch guest agent hostnames for running VMs. Disabled by default
    # because even cheap QGA calls can contribute to instability when
    # Windows qemu-ga is wedged; monitor snapshots and DNS provide the
    # normal /vms data path.
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

    if use_qga_details and running_vmids:
        with ThreadPoolExecutor(max_workers=10) as pool:
            for vmid, hostname in pool.map(fetch_hostname, running_vmids):
                hostnames[vmid] = hostname

    # Enrich running VMs with Windows-side details via guest-exec +
    # PowerShell (domain membership, OS build, IP, Entra-join state).
    # Each call is a ~500ms round-trip; parallelize with the existing
    # ThreadPoolExecutor. Non-Windows VMs or VMs without guest agent
    # readiness return {} and render as blank — expected.
    guest_details: dict = {}
    if use_qga_details and running_vmids:
        with ThreadPoolExecutor(max_workers=10) as pool:
            for vmid, details in pool.map(
                lambda v: (v, _fetch_guest_windows_details(node, v)),
                running_vmids,
            ):
                guest_details[vmid] = details

    # Fallback IP sources, in order:
    #   1. AD DNS lookup against the DC — works even when the guest
    #      agent is entirely dead, as long as the VM is
    #      domain-joined and auto-registered its A record.
    #   2. Optional QEMU guest agent network-get-interfaces when
    #      AUTOPILOT_MONITOR_QGA_DETAILS is explicitly enabled.
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
    if use_qga_details and running_vmids:
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


def _proxmox_cluster_vm_rows() -> list[dict]:
    try:
        rows = _proxmox_api("/cluster/resources?type=vm") or []
    except Exception:
        return []
    out: list[dict] = []
    for row in rows:
        if row.get("type") != "qemu":
            continue
        try:
            vmid = int(row.get("vmid"))
        except (TypeError, ValueError):
            continue
        out.append({
            "vmid": vmid,
            "name": row.get("name") or "",
            "hostname": row.get("name") or "",
            "status": row.get("status") or "unknown",
            "node": row.get("node") or "",
            "target_os": "windows",
        })
    return sorted(out, key=lambda vm: vm["vmid"])


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
            # Use csv.reader so quoted fields with embedded commas
            # (rare but possible from agent-captured BIOS strings)
            # round-trip correctly into the displayed serial.
            with f.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.reader(fh))
            if len(rows) >= 2 and rows[1]:
                serial = rows[1][0]
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


def _safe_file_shelf_name(filename: str | None) -> str:
    raw_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    safe_name = re.sub(r"[^\w\-.]", "_", raw_name)
    safe_name = safe_name.lstrip(".")
    if not safe_name or Path(safe_name).suffix.lower() != ".msi":
        return ""
    return safe_name


def get_file_shelf_items():
    if not FILE_SHELF_DIR.exists():
        return []
    files = []
    for f in sorted(FILE_SHELF_DIR.glob("*.msi"), key=lambda item: item.name.lower()):
        if not f.is_file():
            continue
        stat = f.stat()
        files.append({
            "name": f.name,
            "url": f"/files/{f.name}",
            "size": f"{stat.st_size:,} bytes",
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
    # Older job rows were written with naive UTC timestamps; newer code
    # writes offset-aware. Treat naive values as UTC so subtraction
    # against datetime.now(timezone.utc) doesn't raise.
    def _as_utc(s: str) -> datetime:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    start = _as_utc(job["started"])
    if job.get("ended"):
        end = _as_utc(job["ended"])
    elif job["status"] == "running":
        end = datetime.now(timezone.utc)
    else:
        return None
    delta = end - start
    minutes, seconds = divmod(int(delta.total_seconds()), 60)
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


_JOB_EXPECTED_SECONDS = {
    "provision.yml":      20 * 60,
    "template.yml":       45 * 60,
    "capture.yml":         3 * 60,
    "upload.yml":          2 * 60,
    "bulk-capture.yml":   10 * 60,
    "cloud-delete.yml":    5 * 60,
}


def _job_target(args: dict) -> str:
    return (
        args.get("hostname_pattern")
        or args.get("vm_name")
        or args.get("template_name")
        or args.get("serial")
        or args.get("sequence_name")
        or ""
    )


def _job_paused(job: dict) -> bool:
    if job.get("status") != "running":
        return False
    if not (job.get("args") or {}).get("pause_enabled"):
        return False
    tail = job_manager.get_log(job["id"])[-4096:]
    return _detect_template_pause(job, tail)


def _job_table_rows(limit: int = 200) -> list[dict]:
    rows = []
    for job in job_manager.list_jobs()[:limit]:
        item = dict(job)
        item["duration"] = compute_duration(item)
        item["paused"] = _job_paused(item)
        rows.append(item)
    return rows


def _recent_jobs_payload(limit: int = 5) -> dict:
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 5
    out = []
    for j in job_manager.list_jobs()[:limit]:
        args = j.get("args") or {}
        out.append({
            "id": j["id"],
            "playbook": j.get("playbook"),
            "status": j.get("status"),
            "started": j.get("started"),
            "ended": j.get("ended"),
            "duration": compute_duration(j),
            "target": _job_target(args),
        })
    return {"jobs": out}


def _running_jobs_payload() -> dict:
    now = datetime.now(timezone.utc)
    running = []
    queued = 0
    for j in job_manager.list_jobs():
        status = j.get("status")
        if status == "pending":
            queued += 1
            continue
        if status != "running":
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
        exp = _JOB_EXPECTED_SECONDS.get(pb, 0)
        pct = min(99, int(elapsed / exp * 100)) if exp > 0 else 50
        running.append({
            "id": j["id"],
            "playbook": pb,
            "target": _job_target(j.get("args") or {}),
            "started": started_iso,
            "elapsed_seconds": elapsed,
            "progress_pct": pct,
            "paused": _job_paused(j),
        })
    return {
        "running": running,
        "running_count": len(running),
        "queued_count": queued,
    }


def _live_jobs_payload() -> dict:
    return {
        "running": _running_jobs_payload(),
        "recent": _recent_jobs_payload(limit=5),
        "table": {
            "jobs": _job_table_rows(),
        },
        "generated_at": utc_now_iso(),
    }


class ApiExtraModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class JobTableRowResponse(ApiExtraModel):
    id: str
    playbook: str | None = None
    status: str | None = None
    started: str | None = None
    ended: str | None = None
    duration: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    paused: bool = False


class RecentJobResponse(ApiExtraModel):
    id: str
    playbook: str | None = None
    status: str | None = None
    started: str | None = None
    ended: str | None = None
    duration: str | None = None
    target: str = ""


class RunningJobResponse(ApiExtraModel):
    id: str
    playbook: str = ""
    target: str = ""
    started: str | None = None
    elapsed_seconds: int = 0
    progress_pct: int = 0
    paused: bool = False


class RunningJobsResponse(BaseModel):
    running: list[RunningJobResponse] = Field(default_factory=list)
    running_count: int = 0
    queued_count: int = 0


class RecentJobsResponse(BaseModel):
    jobs: list[RecentJobResponse] = Field(default_factory=list)


class JobsTableResponse(BaseModel):
    jobs: list[JobTableRowResponse] = Field(default_factory=list)


class LiveJobsPayloadResponse(BaseModel):
    running: RunningJobsResponse
    recent: RecentJobsResponse
    table: JobsTableResponse
    generated_at: str


class ServicesResponse(BaseModel):
    services: list[dict[str, Any]] = Field(default_factory=list)
    available: bool = True
    error: str = ""


class RuntimeContainerResponse(ApiExtraModel):
    id: str = ""
    name: str = ""
    service: str = ""
    image: str = ""
    status: str = ""
    health: str = ""
    started_at: str = ""
    finished_at: str = ""
    restart_count: int = 0
    log_url: str = ""


class RuntimeServicesResponse(BaseModel):
    available: bool = True
    error: str = ""
    containers: list[RuntimeContainerResponse] = Field(default_factory=list)


class DeploymentSummaryResponse(ApiExtraModel):
    total: int = 0
    active: int = 0
    running: int = 0
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    stuck: int = 0
    regressed: int = 0
    slow: int = 0
    median_completion_seconds: int | None = None
    p95_completion_seconds: int | None = None
    recent_failure_rate: float = 0


class SignalMetricResponse(BaseModel):
    label: str
    value: str
    tone: str = "neutral"


class SignalSourceHealthResponse(BaseModel):
    runtime_available: bool = True
    setup_health: str = "unknown"
    keytab_status: str = ""


class OperatorSignalResponse(ApiExtraModel):
    id: str
    family: str
    label: str
    status: str
    tone: str = "neutral"
    summary: str
    count: str | int | None = None
    source: str = ""
    href: str = ""


class OperatorPathResponse(ApiExtraModel):
    id: str
    priority: int
    label: str
    status: str
    tone: str = "neutral"
    summary: str
    action_label: str
    href: str
    source: str = ""


class LifecycleLaneResponse(BaseModel):
    id: str
    label: str
    value: str
    detail: str = ""
    status: str = "unknown"
    tone: str = "neutral"


class DeploymentHealthDigestResponse(BaseModel):
    summary: DeploymentSummaryResponse = Field(default_factory=DeploymentSummaryResponse)
    active: list[dict[str, Any]] = Field(default_factory=list)
    recent_completions: list[dict[str, Any]] = Field(default_factory=list)
    bottlenecks: list[dict[str, Any]] = Field(default_factory=list)


class FleetSignalRowResponse(ApiExtraModel):
    vmid: int
    vm_name: str = ""
    node: str = ""
    lifecycle: str = ""
    tone: str = "neutral"
    pve_status: str = ""
    windows: str = ""
    serial: str = ""
    ad: str = ""
    entra: str = ""
    intune: str = ""
    last_checked: str = ""
    href: str = ""


class SignalsHubResponse(BaseModel):
    generated_at: str
    build: dict[str, Any] = Field(default_factory=dict)
    source_health: SignalSourceHealthResponse
    metrics: list[SignalMetricResponse] = Field(default_factory=list)
    signals: list[OperatorSignalResponse] = Field(default_factory=list)
    operator_paths: list[OperatorPathResponse] = Field(default_factory=list)
    lifecycle_lanes: list[LifecycleLaneResponse] = Field(default_factory=list)
    deployment_health: DeploymentHealthDigestResponse = Field(default_factory=DeploymentHealthDigestResponse)
    services: list[dict[str, Any]] = Field(default_factory=list)
    runtime: RuntimeServicesResponse = Field(default_factory=RuntimeServicesResponse)
    fleet_attention: list[FleetSignalRowResponse] = Field(default_factory=list)


class FleetSummaryResponse(ApiExtraModel):
    total: int = 0


class MonitoringSummaryResponse(BaseModel):
    devices: int = 0
    ad: int = 0
    entra: int = 0
    intune: int = 0


class CockpitSummaryResponse(BaseModel):
    readiness_score: int = 0
    jobs: RunningJobsResponse
    recent_jobs: list[RecentJobResponse] = Field(default_factory=list)
    services: ServicesResponse
    fleet: FleetSummaryResponse
    monitoring: MonitoringSummaryResponse


class VmFleetRowResponse(ApiExtraModel):
    vmid: int
    name: str = ""
    hostname: str = ""
    serial: str = ""
    status: str = "unknown"
    ip_address: str = ""
    os_caption: str = ""
    os_build: str = ""
    in_autopilot: bool = False
    in_intune: bool = False
    aad_joined: bool = False
    part_of_domain: bool = False
    hybrid_joined: bool = False
    entra_id_joined: bool = False
    has_hash: bool = False
    lifecycle_state: str = ""
    lifecycle_label: str = ""
    lifecycle_source: str = ""
    lifecycle_observed_at: str = ""
    lifecycle_domain_joined: bool = False
    lifecycle_entra_joined: bool = False
    lifecycle_intune_enrolled: bool = False
    lifecycle_autopilot_registered: bool = False
    target_os: str = "windows"
    sequence_name: str | None = None


class AgentFleetRowResponse(ApiExtraModel):
    agent_id: str
    approval_id: str = ""
    approval_status: str = ""
    vmid: int | None = None
    computer_name: str = ""
    serial_number: str = ""
    primary_ipv4: str = ""
    os_name: str = ""
    os_build: str = ""
    qga_state: str = ""
    domain_joined: bool | None = None
    entra_joined: bool | None = None
    lifecycle_state: str = ""
    lifecycle_label: str = ""
    lifecycle_source: str = ""
    lifecycle_observed_at: str = ""
    lifecycle_domain_joined: bool = False
    lifecycle_entra_joined: bool = False
    lifecycle_intune_enrolled: bool = False
    lifecycle_autopilot_registered: bool = False
    current_phase: str = ""
    current_run_id: str = ""
    agent_version: str = ""
    hash_capture_supported: bool = False
    last_heartbeat_at: str = ""
    last_seen_at: str = ""


class AutopilotDeviceFleetRowResponse(ApiExtraModel):
    id: str = ""
    serial: str = ""
    display_name: str = ""
    group_tag: str = ""
    profile_status: str = ""
    profile_ok: bool = False
    enrollment_state: str = ""
    manufacturer: str = ""
    model: str = ""
    last_contact: str = ""
    has_local_hash: bool = False


class VmsFleetResponse(BaseModel):
    vms: list[VmFleetRowResponse] = Field(default_factory=list)
    proxmox_vms: list[VmFleetRowResponse] = Field(default_factory=list)
    missing_vms: list[VmFleetRowResponse] = Field(default_factory=list)
    agents: list[AgentFleetRowResponse] = Field(default_factory=list)
    autopilot_devices: list[AutopilotDeviceFleetRowResponse] = Field(default_factory=list)
    bubble_topology: dict[str, Any] = Field(default_factory=dict)
    ap_error: str = ""
    cache_age_seconds: int | None = None
    cache_fetched_at_iso: str = ""
    cache_refreshing: bool = False
    monitor_sweep: dict[str, Any] | None = None
    generated_at: str


class VmScreenshotResponse(ApiExtraModel):
    vmid: int
    image_url: str
    content_type: str = "image/png"
    captured_at: str
    expires_at: str
    source: str = "manual"
    bytes: int = 0


class VmLinkageCheckResponse(ApiExtraModel):
    label: str
    ok: bool | None = None
    value: str = ""


class VmKnownCredentialResponse(ApiExtraModel):
    source: str = ""
    label: str = ""
    username: str = ""
    password_available: bool = False
    password_mask: str = ""
    vm_name: str = ""
    run_id: str = ""
    run_url: str = ""
    updated_at: str | None = None
    note: str = ""


class VmTimelineEventResponse(ApiExtraModel):
    at: str = ""
    source: str = ""
    type: str = ""
    severity: str = "event"
    summary: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class VmIdentitySyncResponse(BaseModel):
    source: str = "monitoring_sweep"
    last_checked_at: str = ""
    ad_count: int = 0
    entra_count: int = 0
    intune_count: int = 0


class VmDetailEvidenceResponse(BaseModel):
    vmid: int
    fleet_vm: VmFleetRowResponse | None = None
    pve: dict[str, Any] = Field(default_factory=dict)
    probe: dict[str, Any] = Field(default_factory=dict)
    ad_matches: list[dict[str, Any]] = Field(default_factory=list)
    entra_matches: list[dict[str, Any]] = Field(default_factory=list)
    intune_matches: list[dict[str, Any]] = Field(default_factory=list)
    linkage: list[VmLinkageCheckResponse] = Field(default_factory=list)
    known_credentials: list[VmKnownCredentialResponse] = Field(default_factory=list)
    latest_screenshot: VmScreenshotResponse | None = None
    screenshot_history: list[VmScreenshotResponse] = Field(default_factory=list)
    timeline: list[VmTimelineEventResponse] = Field(default_factory=list)
    history: dict[str, Any] = Field(default_factory=dict)
    identity_sync: VmIdentitySyncResponse = Field(default_factory=VmIdentitySyncResponse)


class InstallTrackingUpdate(BaseModel):
    status: str = Field(..., min_length=1, max_length=32)
    detail: str = Field("", max_length=2000)
    source: str = Field("", max_length=240)
    evidence: dict = Field(default_factory=dict)


class InstallTrackingRunCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=240)
    target: str = Field("", max_length=240)
    source: str = Field("", max_length=240)


def _install_tracking_payload(run_id: str | None = None) -> dict:
    from web import db_pg, install_tracking_pg

    with db_pg.connection(_database_url()) as conn:
        return install_tracking_pg.payload(conn, run_id)


def _install_tracking_refresh_snapshot() -> dict:
    snapshot: dict = {}
    readiness_fn = globals().get("_setup_readiness")
    if callable(readiness_fn):
        try:
            readiness = readiness_fn()
            snapshot["setup_readiness"] = {
                "status": readiness.get("status") or readiness.get("phase"),
                "phase": readiness.get("phase"),
                "ready": readiness.get("ready"),
                "detail": readiness.get("detail") or "setup readiness read from controller state",
                "source": "setup readiness",
            }
            build_host = readiness.get("build_host")
            if isinstance(build_host, dict):
                snapshot["build_host"] = {
                    **build_host,
                    "source": "setup readiness",
                }
            media = readiness.get("media")
            if isinstance(media, dict):
                snapshot["osdeploy_artifact"] = {
                    **media,
                    "source": "setup media",
                }
        except Exception:
            pass
    snapshot["controller_stack"] = {
        "healthy": True,
        "status": "healthy",
        "detail": "operator console process responded to install-tracking refresh",
        "source": "operator console",
    }
    return snapshot


# --- HTML Pages ---

def _primary_ui_redirect(path: str) -> RedirectResponse:
    return RedirectResponse(url=path, status_code=302)


def _render_legacy_dashboard(request: Request):
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


@app.get("/", include_in_schema=False)
async def home(request: Request):
    return _primary_ui_redirect("/react/dashboard")


@app.get("/legacy/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def legacy_dashboard(request: Request):
    return _render_legacy_dashboard(request)


def _render_react_shell(request: Request):
    assets = _react_asset_tags()
    return templates.TemplateResponse("react_shell.html", {
        "request": request,
        "asset_scripts": assets["scripts"],
        "asset_styles": assets["styles"],
        "build_sha": (_APP_VERSION.get("sha_short") or "unknown"),
        "build_time": _APP_VERSION.get("build_time", ""),
    })


@app.get("/react-shell", response_class=HTMLResponse, include_in_schema=False)
async def react_shell(request: Request):
    return _render_react_shell(request)


@app.get("/react/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def react_dashboard_shell(request: Request):
    return _render_react_shell(request)


@app.get("/react/jobs", response_class=HTMLResponse, include_in_schema=False)
async def react_jobs_shell(request: Request):
    return _render_react_shell(request)


@app.get("/react/monitoring", response_class=HTMLResponse, include_in_schema=False)
async def react_monitoring_shell(request: Request):
    return _render_react_shell(request)


@app.get("/react/vms", response_class=HTMLResponse, include_in_schema=False)
async def react_vms_shell(request: Request):
    return _render_react_shell(request)


@app.get("/react/vms/{vmid}", response_class=HTMLResponse, include_in_schema=False)
async def react_vm_detail_shell(request: Request, vmid: int):
    return _render_react_shell(request)


@app.get("/install-tracking", response_class=HTMLResponse)
async def install_tracking_page(request: Request, run_id: str | None = None):
    return templates.TemplateResponse("install_tracking.html", {
        "request": request,
        "tracking": _install_tracking_payload(run_id),
    })


@app.get("/api/install-tracking")
async def install_tracking_api(run_id: str | None = None):
    return _install_tracking_payload(run_id)


@app.get("/api/install-tracking/runs")
async def install_tracking_runs_api():
    from web import db_pg, install_tracking_pg

    with db_pg.connection(_database_url()) as conn:
        return {"schema_version": 1, "runs": install_tracking_pg.list_runs(conn)}


@app.post("/api/install-tracking/runs")
async def install_tracking_create_run(body: InstallTrackingRunCreate):
    from web import db_pg, install_tracking_pg

    with db_pg.connection(_database_url()) as conn:
        run = install_tracking_pg.create_run(
            conn,
            name=body.name,
            target=body.target,
            source=body.source,
        )
        return install_tracking_pg.payload(conn, run["run_id"])


@app.get("/api/install-tracking/runs/{run_id}")
async def install_tracking_run_api(run_id: str):
    from web import db_pg, install_tracking_pg

    with db_pg.connection(_database_url()) as conn:
        if not install_tracking_pg.get_run(conn, run_id):
            raise HTTPException(404, "install tracking run not found")
        return install_tracking_pg.payload(conn, run_id)


@app.post("/api/install-tracking/items/{item_id}")
async def install_tracking_update(item_id: str, update: InstallTrackingUpdate):
    return await install_tracking_run_item_update("pvetest-clean-install", item_id, update)


@app.post("/api/install-tracking/runs/{run_id}/items/{item_id}")
async def install_tracking_run_item_update(run_id: str, item_id: str, update: InstallTrackingUpdate):
    from web import db_pg, install_tracking_pg

    try:
        with db_pg.connection(_database_url()) as conn:
            item = install_tracking_pg.update_item(
                conn,
                item_id,
                run_id=run_id,
                status=update.status,
                detail=update.detail,
                evidence=update.evidence,
                source=update.source,
            )
            return {"item": item, "summary": install_tracking_pg.summary(conn, run_id)}
    except KeyError as exc:
        raise HTTPException(404, "install tracking item not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/install-tracking/runs/{run_id}/refresh-evidence")
async def install_tracking_refresh_evidence(run_id: str):
    from web import db_pg, install_tracking_pg

    with db_pg.connection(_database_url()) as conn:
        if not install_tracking_pg.get_run(conn, run_id):
            raise HTTPException(404, "install tracking run not found")
        return install_tracking_pg.refresh_evidence(
            conn,
            run_id,
            _install_tracking_refresh_snapshot(),
        )


@app.get("/provision", response_class=HTMLResponse)
async def provision_page(request: Request):
    from web import cloudosd_cache, cloudosd_endpoints, cloudosd_pg, cloudosd_sequence, db_pg, osdeploy_cache, osdeploy_endpoints, osdeploy_pg, ts_engine_pg

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
        "serial_prefix": _optional_text(cfg.get("vm_serial_prefix", "")),
        "group_tag":    _optional_text(cfg.get("vm_group_tag", "")),
        "oem_profile":  cfg.get("vm_oem_profile", ""),
        "template_vmid": cfg.get("proxmox_template_vmid", ""),
        "hostname_pattern": _optional_text(cfg.get("vm_hostname_pattern", "")) or "autopilot-{serial}",
    }
    cloudosd_catalog = cloudosd_endpoints.catalog_payload()
    cloudosd_options = cloudosd_endpoints.proxmox_options_payload()
    osdeploy_catalog = osdeploy_endpoints.catalog_payload()
    osdeploy_options = osdeploy_endpoints.proxmox_options_payload()
    cloudosd_artifacts = []
    osdeploy_artifacts = []
    ubuntu_v2_sequences = []
    cloudosd_batch_progress = {"schema_version": 1, "runs": []}
    cloudosd_cache_payload = {"schema_version": 1, "storage": {}, "entries": [], "summary": {}}
    osdeploy_cache_payload = {"schema_version": 1, "storage": {}, "entries": [], "summary": {}}
    try:
        with db_pg.connection(_database_url()) as conn:
            cloudosd_pg.init(conn)
            cloudosd_cache.init(conn)
            osdeploy_pg.init(conn)
            osdeploy_cache.init(conn)
            cloudosd_artifacts = [
                cloudosd_endpoints.enrich_artifact(artifact)
                for artifact in cloudosd_pg.list_artifacts(conn, architecture="amd64")
            ]
            osdeploy_artifacts = [
                osdeploy_endpoints.enrich_artifact(artifact)
                for artifact in osdeploy_pg.list_artifacts(conn, architecture="amd64")
            ]
            cloudosd_cache_payload = cloudosd_cache.payload(conn)
            osdeploy_cache_payload = osdeploy_cache.payload(conn)
            ubuntu_v2_sequences = [
                seq for seq in ts_engine_pg.list_sequences(conn)
                if (seq.get("target_os") or "windows") == "ubuntu"
            ]
    except Exception:
        cloudosd_artifacts = []
        osdeploy_artifacts = []
        ubuntu_v2_sequences = []
    try:
        cloudosd_batch_progress = cloudosd_endpoints.provision_progress_payload(limit=25)
    except Exception:
        cloudosd_batch_progress = {"schema_version": 1, "runs": []}
    sequences = sequences_db.list_sequences(SEQUENCES_DB)
    default_sequence_id = ""
    sequence_rows = []
    for sequence in sequences:
        if sequence.get("is_default"):
            default_sequence_id = str(sequence["id"])
        row = dict(sequence)
        full_sequence = sequences_db.get_sequence(SEQUENCES_DB, int(sequence["id"]))
        unsupported = cloudosd_sequence.unsupported_enabled_steps(full_sequence)
        cloudosd_supported = _cloudosd_supported_enabled_steps(full_sequence)
        row["cloudosd_unsupported_steps"] = unsupported
        row["cloudosd_compatible"] = bool(cloudosd_supported) and not unsupported
        row["boot_modes"] = _legacy_sequence_boot_modes(full_sequence)
        if row["boot_modes"]:
            sequence_rows.append(row)
    return templates.TemplateResponse("provision.html", {
        "request": request,
        "profiles": load_oem_profiles(),
        "defaults": defaults,
        "template_disk_gb": template_disk,
        "sequences": sequence_rows,
        "default_sequence_id": default_sequence_id,
        "winpe_enabled": _winpe_enabled(),
        "cloudosd_catalog": cloudosd_catalog,
        "cloudosd_options": cloudosd_options,
        "osdeploy_catalog": osdeploy_catalog,
        "osdeploy_options": osdeploy_options,
        "cloudosd_artifacts": cloudosd_artifacts,
        "osdeploy_artifacts": osdeploy_artifacts,
        "cloudosd_ready_artifacts": [
            artifact for artifact in cloudosd_artifacts if artifact.get("ready")
        ],
        "osdeploy_ready_artifacts": [
            artifact for artifact in osdeploy_artifacts if artifact.get("ready")
        ],
        "cloudosd_batch_progress": cloudosd_batch_progress,
        "cloudosd_cache": cloudosd_cache_payload,
        "osdeploy_cache": osdeploy_cache_payload,
        "ubuntu_v2_sequences": ubuntu_v2_sequences,
    })


@app.get("/osdcloud/artifacts", response_class=HTMLResponse)
@app.get("/osdcloud/cache", response_class=HTMLResponse)
@app.get("/osdcloud/builder", response_class=HTMLResponse)
@app.get("/osdcloud", response_class=HTMLResponse)
@app.get("/cloudosd/artifacts", response_class=HTMLResponse)
@app.get("/cloudosd/cache", response_class=HTMLResponse)
@app.get("/cloudosd/builder", response_class=HTMLResponse)
@app.get("/cloudosd", response_class=HTMLResponse)
async def cloudosd_page(request: Request, archived: str = "0"):
    from web import agent_telemetry_pg, cloudosd_cache, cloudosd_endpoints, cloudosd_pg, db_pg

    cfg = _load_vars()
    cloudosd_view = {
        "/osdcloud/builder": "builder",
        "/osdcloud/cache": "cache",
        "/osdcloud/artifacts": "artifacts",
        "/cloudosd/builder": "builder",
        "/cloudosd/cache": "cache",
        "/cloudosd/artifacts": "artifacts",
    }.get(request.url.path, "overview")
    show_archived = str(archived or "").lower() in {"1", "true", "yes", "on"}
    with db_pg.connection(_database_url()) as conn:
        cloudosd_pg.init(conn)
        cloudosd_cache.init(conn)
        agent_telemetry_pg.init(conn)
        artifacts = [
            cloudosd_endpoints.enrich_artifact(artifact)
            for artifact in cloudosd_pg.list_artifacts(conn, architecture="amd64")
        ]
        def enrich_run(run: dict) -> dict:
            heartbeat = agent_telemetry_pg.latest_for_run(conn, run["run_id"])
            heartbeat_name = heartbeat.get("computer_name") if heartbeat else None
            run["heartbeat_computer_name"] = heartbeat_name
            run["name_comparison"] = cloudosd_pg.name_comparison(
                requested_name=run.get("requested_vm_name") or run.get("vm_name"),
                pve_name=run.get("pve_vm_name"),
                heartbeat_name=heartbeat_name,
            )
            run["intune_evidence"] = cloudosd_endpoints.intune_evidence_for_run(
                run,
                heartbeat,
            )
            return run

        runs = [
            enrich_run(run)
            for run in cloudosd_pg.list_runs(
                conn,
                limit=25,
                include_archived=show_archived,
            )
        ]
        active_runs = [
            enrich_run(run)
            for run in cloudosd_pg.list_runs(conn, limit=10, active_only=True)
        ]
        stale_failed_runs = [
            enrich_run(run)
            for run in cloudosd_pg.list_runs(conn, limit=10, stale_failed_hours=12)
        ]
        cache_payload = cloudosd_cache.payload(conn)
    assets_status = cloudosd_endpoints.assets_status_payload()
    proxmox_options = cloudosd_endpoints.proxmox_options_payload()
    catalog = cloudosd_endpoints.catalog_payload()
    ready_artifacts = [artifact for artifact in artifacts if artifact and artifact.get("ready")]
    return templates.TemplateResponse("cloudosd.html", {
        "request": request,
        "artifacts": artifacts,
        "runs": runs,
        "ready_artifacts": ready_artifacts,
        "active_runs": active_runs,
        "stale_failed_runs": stale_failed_runs,
        "cloudosd_cache": cache_payload,
        "cloudosd_view": cloudosd_view,
        "show_archived": show_archived,
        "catalog": catalog,
        "assets_status": assets_status,
        "proxmox_options": proxmox_options,
        "proxmox_node": cfg.get("proxmox_node", "pve"),
    })


@app.get("/osdeploy/artifacts", response_class=HTMLResponse)
@app.get("/osdeploy/cache", response_class=HTMLResponse)
@app.get("/osdeploy/builder", response_class=HTMLResponse)
@app.get("/osdeploy", response_class=HTMLResponse)
async def osdeploy_page(request: Request, archived: str = "0"):
    from web import agent_telemetry_pg, db_pg, osdeploy_cache, osdeploy_endpoints, osdeploy_pg, sequences_pg

    cfg = _load_vars()
    osdeploy_view = {
        "/osdeploy/builder": "builder",
        "/osdeploy/cache": "cache",
        "/osdeploy/artifacts": "artifacts",
    }.get(request.url.path, "overview")
    show_archived = str(archived or "").lower() in {"1", "true", "yes", "on"}
    with db_pg.connection(_database_url()) as conn:
        osdeploy_pg.init(conn)
        osdeploy_cache.init(conn)
        agent_telemetry_pg.init(conn)
        artifacts = [
            osdeploy_endpoints.enrich_artifact(artifact)
            for artifact in osdeploy_pg.list_artifacts(conn, architecture="amd64")
        ]
        runs = osdeploy_pg.list_runs(conn, limit=25, include_archived=show_archived)
        active_runs = osdeploy_pg.list_runs(conn, limit=10, active_only=True)
        stale_failed_runs = osdeploy_pg.list_runs(conn, limit=10, stale_failed_hours=12)
        cache_payload = osdeploy_cache.payload(conn)
        credentials = sequences_pg.list_credentials(conn)
    catalog = osdeploy_endpoints.catalog_payload()
    proxmox_options = osdeploy_endpoints.proxmox_options_payload()
    build_defaults = osdeploy_endpoints.build_defaults_payload()
    ready_artifacts = [artifact for artifact in artifacts if artifact and artifact.get("ready")]
    return templates.TemplateResponse("osdeploy.html", {
        "request": request,
        "artifacts": artifacts,
        "runs": runs,
        "ready_artifacts": ready_artifacts,
        "active_runs": active_runs,
        "stale_failed_runs": stale_failed_runs,
        "osdeploy_cache": cache_payload,
        "osdeploy_view": osdeploy_view,
        "show_archived": show_archived,
        "catalog": catalog,
        "proxmox_options": proxmox_options,
        "osdeploy_build_defaults": build_defaults,
        "osdeploy_credentials": credentials,
        "proxmox_node": cfg.get("proxmox_node", "pve"),
    })


@app.get("/osdeploy/runs/{run_id}", response_class=HTMLResponse)
async def osdeploy_run_detail_page(request: Request, run_id: str):
    from web import agent_telemetry_pg, db_pg, osdeploy_endpoints, osdeploy_pg

    with db_pg.connection(_database_url()) as conn:
        osdeploy_pg.init(conn)
        agent_telemetry_pg.init(conn)
        run = osdeploy_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="OSDeploy run not found")
        artifact = osdeploy_endpoints.enrich_artifact(
            osdeploy_pg.get_artifact(conn, run["artifact_id"])
        )
        events = osdeploy_pg.list_events(conn, run_id)
        readiness = osdeploy_pg.get_readiness(conn, run_id)
        v2_steps = ts_engine_pg.list_run_steps(conn, run_id)
        heartbeat = agent_telemetry_pg.latest_for_run(conn, run_id)
    return templates.TemplateResponse("osdeploy_run_detail.html", {
        "request": request,
        "run": run,
        "artifact": artifact,
        "events": events,
        "readiness": readiness,
        "v2_steps": v2_steps,
        "heartbeat": heartbeat,
    })


@app.get("/osdcloud/runs/{run_id}", response_class=HTMLResponse)
@app.get("/cloudosd/runs/{run_id}", response_class=HTMLResponse)
async def cloudosd_run_detail_page(request: Request, run_id: str):
    from web import agent_telemetry_pg, cloudosd_endpoints, cloudosd_pg, db_pg

    with db_pg.connection(_database_url()) as conn:
        cloudosd_pg.init(conn)
        agent_telemetry_pg.init(conn)
        run = cloudosd_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="OSDCloud run not found")
        heartbeat = agent_telemetry_pg.latest_for_run(conn, run_id)
        if heartbeat and run["state"] != "complete":
            run = cloudosd_pg.mark_complete_from_heartbeat(
                conn,
                run_id=run_id,
                heartbeat_at=heartbeat["received_at"],
                heartbeat=heartbeat,
            )
        artifact = cloudosd_endpoints.enrich_artifact(
            cloudosd_pg.get_artifact(conn, run["artifact_id"]),
        )
        cloudosd_pg.sync_ts_progress_for_run(conn, run_id)
        raw_v2_steps = cloudosd_pg.ts_engine_pg.list_run_steps(conn, run_id)
        v2_steps = cloudosd_pg.enrich_v2_steps_for_operator(raw_v2_steps)
        v2_completion = cloudosd_pg.v2_completion_status(
            conn,
            run_id,
            domain_join=run.get("domain_join"),
        )
        v2_operator_status = cloudosd_pg.v2_operator_status(
            raw_v2_steps,
            v2_completion,
            heartbeat=heartbeat,
        )
        events = cloudosd_pg.list_events(conn, run_id)
        events = cloudosd_endpoints.events_with_related_jobs(run_id, events, run)
        autopilot_readiness = cloudosd_endpoints.autopilot_readiness_for_run(conn, run, heartbeat)
    heartbeat_name = heartbeat.get("computer_name") if heartbeat else None
    run["heartbeat_computer_name"] = heartbeat_name
    run["name_comparison"] = cloudosd_pg.name_comparison(
        requested_name=run.get("requested_vm_name") or run.get("vm_name"),
        pve_name=run.get("pve_vm_name"),
        heartbeat_name=heartbeat_name,
    )
    event_groups = cloudosd_pg.milestone_event_groups(events)
    intune_evidence = cloudosd_endpoints.intune_evidence_for_run(run, heartbeat)
    related_jobs = [
        job for job in job_manager.list_jobs()
        if (job.get("args") or {}).get("cloudosd_run_id") == run_id
    ]
    return templates.TemplateResponse("cloudosd_run_detail.html", {
        "request": request,
        "run": run,
        "artifact": artifact,
        "latest_heartbeat": heartbeat,
        "events": events,
        "event_groups": event_groups,
        "milestone_labels": cloudosd_pg.CLOUDOSD_MILESTONE_LABELS,
        "v2_steps": v2_steps,
        "v2_completion": v2_completion,
        "v2_operator_status": v2_operator_status,
        "intune_evidence": intune_evidence,
        "autopilot_readiness": autopilot_readiness,
        "related_jobs": related_jobs,
        "os_settings": cloudosd_pg.os_settings(run),
        "user_settings": cloudosd_pg.user_settings(run),
        "task": cloudosd_pg.task_settings(run),
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


@app.get("/files", response_class=HTMLResponse)
async def files_page(request: Request, uploaded: str = "", error: str = ""):
    return templates.TemplateResponse("files.html", {
        "request": request,
        "files": get_file_shelf_items(),
        "uploaded": uploaded,
        "error": error,
    })


def _render_legacy_jobs(request: Request):
    jobs = _job_table_rows()
    return templates.TemplateResponse("jobs.html", {
        "request": request,
        "jobs": jobs,
    })


@app.get("/jobs", include_in_schema=False)
async def jobs_page(request: Request):
    return _primary_ui_redirect("/react/jobs")


@app.get("/legacy/jobs", response_class=HTMLResponse, include_in_schema=False)
async def legacy_jobs_page(request: Request):
    return _render_legacy_jobs(request)


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
        transport = args.get("capture_transport") or "autopilot_agent"
        return {
            "title": f"Capture Autopilot hash for {len(vmids)} VM(s)",
            "summary": ("Queues a capture request for the installed "
                        "AutopilotAgent, which runs Get-WindowsAutopilotInfo "
                        "inside Windows and posts the CSV back to the controller."),
            "steps": [
                "Find the latest live AutopilotAgent heartbeat for the VM",
                "Queue an agent work item for hash capture",
                "AutopilotAgent downloads Get-WindowsAutopilotInfo and captures the CSV",
                "AutopilotAgent posts the hash back to the controller",
                "Save to /app/output/hashes/<serial>_hwid.csv",
            ],
            "end_goal": "One CSV per VM, ready for Intune bulk upload.",
            "metadata": [
                ("Target VMIDs", ", ".join(str(v) for v in vmids)),
                ("Capture transport", transport),
                ("Agent ID", args.get("agent_id") or ""),
                ("Work item", args.get("work_item_id") or ""),
            ],
        }

    if pb == "upload_after_capture":
        return {
            "title": "Capture + upload Autopilot hashes",
            "summary": ("Captures the hash then hands it straight to "
                        "Intune via Microsoft Graph."),
            "steps": [
                "Wait for AutopilotAgent hash-capture work items to complete",
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
    vault_values = _load_vault()
    merged_cfg = _load_proxmox_config()
    options = _fetch_settings_options()
    hv_type = _settings_hypervisor_type(current_vars.get("hypervisor_type"))
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
        "proxmox_bootstrap": {
            "enabled": hv_type == "proxmox",
            "host": merged_cfg.get("proxmox_host", ""),
            "disk_storage": merged_cfg.get("proxmox_storage", ""),
            "iso_storage": merged_cfg.get("proxmox_iso_storage", ""),
            "root_username": vault_values.get("vault_proxmox_root_username") or "root@pam",
            "root_password_set": vault_present.get("vault_proxmox_root_password", False),
            "default_token_id": proxmox_permissions.DEFAULT_API_TOKEN_ID,
            "snippet_storage": proxmox_permissions.DEFAULT_SNIPPET_STORAGE,
            "chassis_types": ",".join(str(v) for v in proxmox_permissions.DEFAULT_CHASSIS_TYPES),
        },
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


@app.post("/api/proxmox/bootstrap-permissions")
async def bootstrap_proxmox_permissions(request: Request):
    """Repair Proxmox permissions and host-local OEM prerequisites over SSH."""
    form = await request.form()
    cfg = _load_proxmox_config()
    host = (form.get("proxmox_host") or cfg.get("proxmox_host") or "").strip()
    if not host:
        raise HTTPException(status_code=400, detail="proxmox_host is required")
    root_username = (form.get("root_username") or "root@pam").strip() or "root@pam"
    root_password = (form.get("root_password") or "").strip()
    if not root_password:
        raise HTTPException(status_code=400, detail="root SSH password is required")
    api_token_id = (
        form.get("api_token_id")
        or cfg.get("vault_proxmox_api_token_id")
        or proxmox_permissions.DEFAULT_API_TOKEN_ID
    )
    snippet_storage = (
        form.get("snippet_storage")
        or proxmox_permissions.DEFAULT_SNIPPET_STORAGE
    )
    raw_chassis = (form.get("chassis_types") or "").strip()
    if raw_chassis:
        try:
            chassis_types = [
                int(part.strip()) for part in raw_chassis.split(",")
                if part.strip()
            ]
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="chassis_types must be comma-separated integers",
            ) from exc
    else:
        chassis_types = list(proxmox_permissions.DEFAULT_CHASSIS_TYPES)

    try:
        script = proxmox_permissions.build_bootstrap_script(
            api_token_id=api_token_id,
            disk_storage=cfg.get("proxmox_storage"),
            iso_storage=cfg.get("proxmox_iso_storage"),
            snippet_storage=snippet_storage,
            chassis_types=chassis_types,
        )
        result = proxmox_permissions.run_bootstrap_script(
            host=host,
            root_username=root_username,
            root_password=root_password,
            script=script,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "sshpass/ssh is not available in the web container; "
                "install the Proxmox SSH client dependencies and retry."
            ),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Proxmox SSH bootstrap failed: {exc}",
        ) from exc

    if not result.ok:
        detail = (result.stderr or result.stdout or "unknown SSH bootstrap failure").strip()
        raise HTTPException(status_code=502, detail=detail)

    save_credentials = str(form.get("save_root_credentials", "on")).lower() in {
        "1", "true", "yes", "on",
    }
    if save_credentials:
        _save_vault({
            "vault_proxmox_root_username": root_username,
            "vault_proxmox_root_password": root_password,
        })

    return {
        "ok": True,
        "host": host,
        "api_user": proxmox_permissions.api_token_user(api_token_id),
        "role": proxmox_permissions.AUTOPILOT_ROLE,
        "storages": [
            value for value in [
                cfg.get("proxmox_storage"),
                cfg.get("proxmox_iso_storage"),
                snippet_storage,
            ]
            if value
        ],
        "root_password_set": save_credentials,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }


@app.post("/api/settings")
async def save_settings(request: Request):
    form = await request.form()
    current_vars = _load_vars()
    # Honor the submitted hypervisor_type when filtering (so flipping
    # backends + saving in one submit applies correctly), falling back
    # to the on-disk value.
    hv_type = _settings_hypervisor_type(
        form.get("hypervisor_type") or current_vars.get("hypervisor_type")
    )
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


def _json_obj(value, default):
    if not value:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _vms_from_monitor_snapshot() -> list[dict]:
    """Build /vms rows from the monitor service's newest completed sweep.

    This avoids doing live QGA/guest-exec probes on the request path. The
    monitor owns slow enrichment; /vms should render from its last snapshot
    and let the explicit Refresh button do live collection when needed.
    """
    try:
        latest = device_history_db.latest_per_vmid()
    except Exception:
        return []
    try:
        agent_latest = agent_telemetry_pg.latest_by_vmid()
    except Exception:
        agent_latest = {}

    out: list[dict] = []
    for entry in latest:
        pve = entry.get("pve") or {}
        probe = entry.get("probe") or {}
        vmid = int(entry.get("vmid") or pve.get("vmid") or 0)
        name = pve.get("name") or probe.get("vm_name") or ""
        status = pve.get("status") or "unknown"
        tags = (pve.get("tags_csv") or "").replace(",", ";")
        smbios1 = pve.get("smbios1") or ""
        args = pve.get("args") or ""
        agent = agent_latest.get(vmid) or {}

        serial = probe.get("serial") or agent.get("serial_number") or ""
        if not serial:
            if "smbios file=" in args and name.lower().startswith("gell-"):
                serial = name
            elif smbios1:
                serial = _decode_smbios_serial(smbios1)

        dsreg = _json_obj(probe.get("dsreg_status"), {})
        aad_joined_raw = str(
            dsreg.get("AzureAdJoined")
            or dsreg.get("azureAdJoined")
            or dsreg.get("aad_joined")
            or ""
        ).strip().lower()
        aad_joined = (
            aad_joined_raw in {"yes", "true", "1"}
            or bool(int(probe.get("entra_found") or 0))
        )

        intune_matches = _json_obj(probe.get("intune_matches_json"), [])
        first_intune = intune_matches[0] if intune_matches else {}

        ad_matches = _json_obj(probe.get("ad_matches_json"), [])
        first_ad = ad_matches[0] if ad_matches else {}
        entra_matches = _json_obj(probe.get("entra_matches_json"), [])
        hybrid_joined = any(
            (m.get("trustType") or "").lower() == "serverad"
            for m in entra_matches
        )
        entra_id_joined = (
            aad_joined_raw in {"yes", "true", "1"}
            or any(
                (m.get("trustType") or "").lower() == "azuread"
                for m in entra_matches
            )
        )
        domain = (
            first_ad.get("domain")
            or first_ad.get("dnsDomain")
            or first_ad.get("userPrincipalName", "").split("@")[-1]
        )
        join_evidence = monitoring_evidence.hostname_join_evidence({"probe": probe})
        evidence_label = join_evidence["label"]
        evidence_is_domain = evidence_label == "domain"
        evidence_is_entra = evidence_label == "Entra ID"

        out.append({
            "vmid": vmid,
            "name": name,
            "status": status,
            "monitor_checked_at": pve.get("checked_at") or entry.get("last_checked") or "",
            "monitor_probed_at": probe.get("checked_at") or "",
            "serial": serial,
            "oem": "",
            "hostname": (
                probe.get("win_name")
                or agent.get("computer_name")
                or (name if status == "running" else "")
            ),
            "mem_mb": int(pve.get("memory_mb") or 0),
            "cpus": pve.get("cores") or "",
            "tags": tags,
            "has_guest_data": bool(probe),
            "domain": domain or ("domain" if evidence_is_domain else ""),
            "part_of_domain": evidence_is_domain,
            "hybrid_joined": hybrid_joined,
            "entra_joined": evidence_is_entra,
            "entra_id_joined": entra_id_joined,
            "hostname_join_label": join_evidence["label"],
            "hostname_join_title": join_evidence["title"],
            "hostname_join_source": join_evidence["source"],
            "aad_joined": aad_joined,
            "aad_tenant": dsreg.get("TenantName") or dsreg.get("tenant_name") or "",
            "in_intune": bool(int(probe.get("intune_found") or 0)),
            "intune_compliance": first_intune.get("complianceState") or "",
            "os_caption": "",
            "os_build": str(probe.get("os_build") or agent.get("os_build") or ""),
            "os_version": agent.get("os_version") or "",
            "ip_address": agent.get("primary_ipv4") or "",
            "last_boot": "",
            "agent_last_seen_at": agent.get("received_at") or "",
            "agent_qga_state": agent.get("qga_state") or "",
        })
    return sorted(out, key=lambda v: v["vmid"])


def _latest_monitor_sweep_status() -> dict | None:
    try:
        return device_history_db.latest_sweep_status()
    except Exception:
        return None


def _iso_or_blank(value) -> str:
    if not value:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _identity_key(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _identity_keys(*values) -> set[str]:
    return {key for key in (_identity_key(value) for value in values) if key}


def _ip_keys(*values) -> set[str]:
    return {
        str(value).strip()
        for value in values
        if value and re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", str(value).strip())
    }


def _agent_match_keys(agent: dict) -> set[str]:
    agent_id = str(agent.get("agent_id") or "")
    agent_id_suffix = re.sub(r"^agent-", "", agent_id, flags=re.IGNORECASE)
    return _identity_keys(
        agent.get("computer_name"),
        agent.get("serial_number"),
        agent_id,
        agent_id_suffix,
    )


def _agent_ip_match_keys(agent: dict) -> set[str]:
    return _ip_keys(agent.get("primary_ipv4"))


def _pve_vm_match_keys(vm: dict) -> set[str]:
    return _identity_keys(
        vm.get("name"),
        vm.get("hostname"),
        vm.get("serial"),
        vm.get("computer_name"),
    )


def _pve_vm_ip_match_keys(vm: dict) -> set[str]:
    return _ip_keys(vm.get("ip_address"), vm.get("primary_ipv4"))


def _infer_agent_vmid_from_pve(agent: dict, pve_vms: list[dict] | None = None):
    if agent.get("vmid"):
        return agent.get("vmid")
    agent_keys = _agent_match_keys(agent)
    agent_ips = _agent_ip_match_keys(agent)
    if not agent_keys and not agent_ips:
        return None

    matches: set[int] = set()
    for vm in pve_vms or []:
        vmid = vm.get("vmid")
        if not vmid:
            continue
        if (
            agent_keys.intersection(_pve_vm_match_keys(vm))
            or agent_ips.intersection(_pve_vm_ip_match_keys(vm))
        ):
            matches.add(int(vmid))

    if len(matches) == 1:
        return next(iter(matches))
    return None


def _enrich_agent_vmid_from_pve(agent: dict, pve_vms: list[dict] | None = None) -> dict:
    inferred = _infer_agent_vmid_from_pve(agent, pve_vms)
    if inferred:
        agent["vmid"] = inferred
    return agent


def _apply_lifecycle(row: dict, lifecycle: dict | None) -> dict:
    if not lifecycle:
        row.setdefault("lifecycle_state", "")
        row.setdefault("lifecycle_label", "")
        row.setdefault("lifecycle_source", "")
        row.setdefault("lifecycle_observed_at", "")
        row.setdefault("lifecycle_domain_joined", False)
        row.setdefault("lifecycle_entra_joined", False)
        row.setdefault("lifecycle_intune_enrolled", False)
        row.setdefault("lifecycle_autopilot_registered", False)
        return row
    row["lifecycle_state"] = lifecycle.get("state") or ""
    row["lifecycle_label"] = lifecycle.get("label") or ""
    row["lifecycle_source"] = lifecycle.get("source") or ""
    row["lifecycle_observed_at"] = lifecycle.get("last_observed_at") or ""
    row["lifecycle_domain_joined"] = bool(lifecycle.get("domain_joined"))
    row["lifecycle_entra_joined"] = bool(lifecycle.get("entra_joined"))
    row["lifecycle_intune_enrolled"] = bool(lifecycle.get("intune_enrolled"))
    row["lifecycle_autopilot_registered"] = bool(lifecycle.get("autopilot_registered"))
    return row


def _agent_inventory_rows() -> list[dict]:
    pve_vms = list(_VMS_CACHE.get("data") or [])
    try:
        rows = agent_telemetry_pg.latest_agents()
    except Exception:
        rows = []
    out: list[dict] = []
    seen_agent_ids: set[str] = set()
    for row in rows:
        seen_agent_ids.add(row.get("agent_id") or "")
        agent_version = (
            row.get("agent_version")
            or row.get("device_agent_version")
            or ""
        )
        out.append(_enrich_agent_vmid_from_pve({
            "agent_id": row.get("agent_id") or "",
            "approval_id": "",
            "approval_status": "active",
            "vmid": row.get("vmid") or row.get("device_vmid"),
            "computer_name": (
                row.get("computer_name")
                or row.get("device_computer_name")
                or ""
            ),
            "serial_number": (
                row.get("serial_number")
                or row.get("device_serial_number")
                or ""
            ),
            "primary_ipv4": row.get("primary_ipv4") or "",
            "os_name": row.get("os_name") or "",
            "os_version": row.get("os_version") or "",
            "os_build": row.get("os_build") or "",
            "qga_state": row.get("qga_state") or "",
            "domain_name": row.get("domain_name") or "",
            "domain_joined": row.get("domain_joined"),
            "entra_joined": row.get("entra_joined"),
            "current_phase": row.get("current_phase") or "",
            "current_run_id": row.get("current_run_id") or "",
            "agent_version": agent_version,
            "hash_capture_supported": _agent_supports_work_queue(agent_version),
            "last_heartbeat_at": _iso_or_blank(row.get("received_at")),
            "last_seen_at": _iso_or_blank(row.get("last_seen_at")),
        }, pve_vms))
    try:
        pending_rows = agent_telemetry_pg.pending_bootstrap_approvals()
    except Exception:
        pending_rows = []
    for row in pending_rows:
        agent_id = row.get("agent_id") or ""
        if agent_id in seen_agent_ids:
            for existing in out:
                if existing["agent_id"] == agent_id:
                    existing["approval_id"] = row.get("approval_id") or ""
                    if not existing.get("last_heartbeat_at"):
                        existing["approval_status"] = row.get("status") or ""
                    break
            continue
        out.append(_enrich_agent_vmid_from_pve({
            "agent_id": agent_id,
            "approval_id": row.get("approval_id") or "",
            "approval_status": row.get("status") or "pending",
            "vmid": row.get("vmid"),
            "computer_name": row.get("computer_name") or "",
            "serial_number": row.get("serial_number") or "",
            "primary_ipv4": "",
            "os_name": "",
            "os_version": "",
            "os_build": "",
            "qga_state": "",
            "domain_name": "",
            "domain_joined": None,
            "entra_joined": None,
            "current_phase": row.get("phase") or "",
            "current_run_id": row.get("created_from_run_id") or "",
            "agent_version": row.get("agent_version") or "",
            "hash_capture_supported": _agent_supports_work_queue(
                row.get("agent_version") or ""
            ),
            "last_heartbeat_at": "",
            "last_seen_at": _iso_or_blank(row.get("requested_at")),
        }, pve_vms))
    try:
        lifecycles = machine_lifecycle_pg.current_by_agents([
            agent["agent_id"] for agent in out if agent.get("agent_id")
        ])
    except Exception:
        lifecycles = {}
    return [
        _apply_lifecycle(agent, lifecycles.get(agent.get("agent_id") or ""))
        for agent in out
    ]


def _agent_row_vmid(agent: dict) -> int | None:
    try:
        vmid = agent.get("vmid")
        if vmid is None or vmid == "":
            return None
        return int(vmid)
    except (TypeError, ValueError):
        return None


def _hard_delete_agent_by_id(agent_id: str) -> bool:
    from web import agent_telemetry_pg, db_pg

    with db_pg.connection(_database_url()) as conn:
        agent_telemetry_pg.init(conn)
        return agent_telemetry_pg.hard_delete_agent(conn, agent_id)


def _filter_and_purge_agents_without_current_vm(
    agents: list[dict],
    vms: list[dict],
) -> list[dict]:
    current_vmids: set[int] = set()
    for vm in vms:
        try:
            vmid = vm.get("vmid")
            if vmid is not None and vmid != "":
                current_vmids.add(int(vmid))
        except (TypeError, ValueError):
            continue

    state = _read_json_file(SETUP_STATE_PATH)
    expected_build_host_agent = str(
        state.get("build_host_expected_agent_id") or ""
    ).strip()
    try:
        expected_build_host_vmid = int(state.get("build_host_vmid") or 0)
    except (TypeError, ValueError):
        expected_build_host_vmid = 0

    kept: list[dict] = []
    purge_agent_ids: list[str] = []
    for agent in agents:
        agent_id = agent.get("agent_id") or ""
        agent_vmid = _agent_row_vmid(agent)
        if (
            (expected_build_host_agent and agent_id == expected_build_host_agent)
            or (expected_build_host_vmid and agent_vmid == expected_build_host_vmid)
        ):
            kept.append(agent)
            continue
        if agent_vmid is not None and agent_vmid in current_vmids:
            kept.append(agent)
            continue
        if agent_id:
            purge_agent_ids.append(agent_id)

    if purge_agent_ids:
        import logging as _logging
        log = _logging.getLogger("web.vms")
        for agent_id in purge_agent_ids:
            try:
                _hard_delete_agent_by_id(agent_id)
            except Exception:
                log.exception(
                    "failed to purge agent without current VM",
                    extra={"agent_id": agent_id},
                )
    return kept


def _agent_inventory_rows_for_current_vms() -> list[dict]:
    pve_vms = list(_VMS_CACHE.get("data") or [])
    if not pve_vms:
        pve_vms = _vms_from_monitor_snapshot()
    return _filter_and_purge_agents_without_current_vm(_agent_inventory_rows(), pve_vms)


def _fetch_vms_payload_live():
    """Synchronous fetcher — calls every upstream the /vms page needs.
    Returns a dict ready to stash in _VMS_CACHE."""
    return {
        "data": get_autopilot_vms(),
        "devices": get_autopilot_devices(),
        "hash_serials": {f["serial"] for f in get_hash_files()},
    }


def _fetch_vms_payload():
    monitor_vms = _vms_from_monitor_snapshot()
    if monitor_vms:
        return {
            "data": monitor_vms,
            "devices": get_autopilot_devices(),
            "hash_serials": {f["serial"] for f in get_hash_files()},
        }
    return _fetch_vms_payload_live()


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


async def _run_monitor_sweep_and_refresh_vms_cache() -> None:
    """Run an operator-requested sweep, then replace the /vms cache from it."""
    import asyncio
    import logging as _logging
    from web import monitor_main

    _VMS_CACHE["refreshing"] = True
    try:
        await asyncio.to_thread(monitor_main._do_sweep_tick)
        _VMS_CACHE.update({
            "data": None,
            "devices": None,
            "hash_serials": None,
            "fetched_at": 0.0,
        })
        payload = await asyncio.to_thread(_fetch_vms_payload)
        _VMS_CACHE.update(payload)
        _VMS_CACHE["fetched_at"] = time.monotonic()
    except Exception:
        _logging.getLogger("web.vms").exception(
            "monitor sweep finished without refreshing /vms cache",
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
    # empty tables. Prefer the monitor snapshot if present so page
    # rendering is not coupled to slow/dead guest-agent probes.
    if _VMS_CACHE["data"] is None:
        payload = await asyncio.to_thread(_fetch_vms_payload)
        _VMS_CACHE.update(payload)
        _VMS_CACHE["fetched_at"] = time.monotonic()
        if payload["data"] and not _VMS_CACHE["refreshing"]:
            asyncio.create_task(_refresh_vms_cache_bg())
        return _VMS_CACHE, 0.0

    # Very stale — block rather than serve badly outdated data.
    if age >= _VMS_CACHE_STALE_SECONDS:
        payload = await asyncio.to_thread(_fetch_vms_payload)
        _VMS_CACHE.update(payload)
        _VMS_CACHE["fetched_at"] = time.monotonic()
        if payload["data"] and not _VMS_CACHE["refreshing"]:
            asyncio.create_task(_refresh_vms_cache_bg())
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


def _request_wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    content_type = request.headers.get("content-type", "")
    return "application/json" in accept or content_type.startswith("application/json")


def _action_response(
    request: Request,
    *,
    payload: dict,
    redirect: str = "/vms",
    status_code: int = 200,
):
    if _request_wants_json(request):
        return JSONResponse(payload, status_code=status_code)
    return RedirectResponse(redirect, status_code=303)


def _action_error_response(
    request: Request,
    *,
    message: str,
    redirect: str = "/vms",
    status_code: int = 500,
):
    if _request_wants_json(request):
        return JSONResponse({"ok": False, "error": message}, status_code=status_code)
    return _redirect_with_error(redirect, message)


async def _request_values(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    form = await request.form()
    values: dict = {}
    for key, value in form.multi_items():
        text = str(value)
        if key in values:
            current = values[key]
            if isinstance(current, list):
                current.append(text)
            else:
                values[key] = [current, text]
        else:
            values[key] = text
    return values


def _request_text(values: dict, key: str, default: str = "") -> str:
    value = values.get(key, default)
    if isinstance(value, list):
        value = value[0] if value else default
    if value is None:
        return default
    return str(value)


def _request_list(values: dict, key: str) -> list[str]:
    value = values.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


_SCREENSHOT_CACHE: dict[str, dict] = {}
_SCREENSHOT_TTL_SECONDS = 120
_VM_SCREENSHOT_HISTORY_LIMIT = max(
    1,
    int(os.environ.get("AUTOPILOT_VM_SCREENSHOT_HISTORY_LIMIT", "5") or "5"),
)
_VM_SCREENSHOT_STORE_TTL_SECONDS = max(
    60,
    int(os.environ.get("AUTOPILOT_VM_SCREENSHOT_TTL_SECONDS", "900") or "900"),
)
_VM_SCREENSHOT_MAX_BYTES = max(
    1024,
    int(os.environ.get("AUTOPILOT_VM_SCREENSHOT_MAX_BYTES", str(8 * 1024 * 1024)) or str(8 * 1024 * 1024)),
)
_LIVE_HUB: LiveHub | None = None
_LIVE_QGA_FAILURE_BACKOFF_SECONDS = 60.0
_LIVE_QGA_FAILURES: dict[int, dict] = {}


def _reset_runtime_ui_caches() -> dict:
    """Clear process-local caches that can make UI tables look stale.

    This does not delete durable Postgres inventory, jobs, telemetry, hashes,
    OSDCloud runs, or Graph sync state. It only forces the next page/API read
    to rebuild from the current durable sources.
    """
    _VMS_CACHE.update({
        "data": None,
        "devices": None,
        "hash_serials": None,
        "fetched_at": 0.0,
        "refreshing": False,
    })
    screenshots = len(_SCREENSHOT_CACHE)
    qga_failures = len(_LIVE_QGA_FAILURES)
    _SCREENSHOT_CACHE.clear()
    _LIVE_QGA_FAILURES.clear()
    _LATEST_VERSION_CACHE.update({
        "fetched_at": 0,
        "sha": None,
        "sha_short": None,
        "error": None,
    })
    return {
        "vms": "invalidated",
        "screenshots_cleared": screenshots,
        "qga_backoffs_cleared": qga_failures,
        "version": "invalidated",
    }


@app.post("/api/ui/clear-caches")
async def api_ui_clear_caches():
    return {"ok": True, "cleared": _reset_runtime_ui_caches()}


@app.post("/api/ui/reload-live-data")
async def api_ui_reload_live_data():
    """Clear UI caches, then refresh expensive live-backed table sources.

    Fleet refresh runs a monitor sweep and rebuilds /vms from its current
    snapshot. OSDCloud readiness asks the watcher to reconcile post-deploy
    Intune state, while still respecting Graph 429 backoff.
    """
    import asyncio
    from web import monitor_main

    cleared = _reset_runtime_ui_caches()
    refreshes: dict[str, dict] = {}

    try:
        await _run_monitor_sweep_and_refresh_vms_cache()
        refreshes["fleet"] = {"ok": True}
    except Exception as exc:
        refreshes["fleet"] = {"ok": False, "error": str(exc)}

    try:
        readiness = await asyncio.to_thread(
            monitor_main._do_cloudosd_readiness_tick,
            100,
        )
        refreshes["cloudosd_readiness"] = {"ok": True, "result": readiness}
    except Exception as exc:
        refreshes["cloudosd_readiness"] = {"ok": False, "error": str(exc)}

    return {"ok": True, "cleared": cleared, "refreshes": refreshes}


def _purge_expired_screenshots() -> None:
    now = time.monotonic()
    expired = [
        sid for sid, item in _SCREENSHOT_CACHE.items()
        if item["expires_at_monotonic"] <= now
    ]
    for sid in expired:
        _SCREENSHOT_CACHE.pop(sid, None)


def _store_screenshot(*, vmid: int, png_bytes: bytes) -> dict:
    _purge_expired_screenshots()
    screenshot_id = uuid4().hex
    captured_at = utc_now_iso()
    _SCREENSHOT_CACHE[screenshot_id] = {
        "vmid": vmid,
        "content": png_bytes,
        "content_type": "image/png",
        "captured_at": captured_at,
        "expires_at_monotonic": time.monotonic() + _SCREENSHOT_TTL_SECONDS,
    }
    try:
        _store_vm_screenshot_record(
            vmid=vmid,
            png_bytes=png_bytes,
            source="manual",
        )
    except Exception:
        pass
    return {
        "vmid": vmid,
        "image_url": f"/api/live/screenshots/{screenshot_id}",
        "content_type": "image/png",
        "captured_at": captured_at,
        "expires_at": datetime.fromtimestamp(
            time.time() + _SCREENSHOT_TTL_SECONDS, timezone.utc,
        ).isoformat(),
    }


def _vm_screenshot_dir(vmid: int) -> Path:
    return SCREENSHOT_STORE_DIR / str(int(vmid))


def _vm_screenshot_metadata_path(vmid: int) -> Path:
    return _vm_screenshot_dir(vmid) / "metadata.json"


def _parse_iso_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _public_vm_screenshot(item: dict) -> dict:
    return {
        "vmid": int(item.get("vmid") or 0),
        "image_url": str(item.get("image_url") or ""),
        "content_type": str(item.get("content_type") or "image/png"),
        "captured_at": str(item.get("captured_at") or ""),
        "expires_at": str(item.get("expires_at") or ""),
        "source": str(item.get("source") or "manual"),
        "bytes": int(item.get("bytes") or 0),
    }


def _read_vm_screenshot_metadata(vmid: int, *, prune: bool = True) -> dict:
    path = _vm_screenshot_metadata_path(vmid)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"vmid": int(vmid), "items": []}
    if not isinstance(raw, dict):
        return {"vmid": int(vmid), "items": []}
    items = [item for item in raw.get("items") or [] if isinstance(item, dict)]
    if not prune:
        return {"vmid": int(vmid), "items": items}

    now = datetime.now(timezone.utc)
    keep: list[dict] = []
    delete: list[dict] = []
    for item in items:
        expires_at = _parse_iso_datetime(item.get("expires_at"))
        filename = str(item.get("filename") or "")
        if not filename or (expires_at is not None and expires_at <= now):
            delete.append(item)
            continue
        if not (_vm_screenshot_dir(vmid) / filename).is_file():
            continue
        keep.append(item)
    keep = sorted(keep, key=lambda item: str(item.get("captured_at") or ""), reverse=True)[
        :_VM_SCREENSHOT_HISTORY_LIMIT
    ]
    keep_ids = {str(item.get("id") or "") for item in keep}
    delete.extend(item for item in items if str(item.get("id") or "") not in keep_ids)
    if len(keep) != len(items):
        _write_vm_screenshot_metadata(vmid, keep)
        for item in delete:
            filename = str(item.get("filename") or "")
            if filename:
                try:
                    (_vm_screenshot_dir(vmid) / filename).unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
    return {"vmid": int(vmid), "items": keep}


def _write_vm_screenshot_metadata(vmid: int, items: list[dict]) -> None:
    vm_dir = _vm_screenshot_dir(vmid)
    vm_dir.mkdir(parents=True, exist_ok=True)
    path = _vm_screenshot_metadata_path(vmid)
    payload = {
        "vmid": int(vmid),
        "latest_id": str(items[0].get("id") or "") if items else "",
        "items": items,
        "updated_at": utc_now_iso(),
    }
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, path)


def _store_vm_screenshot_record(*, vmid: int, png_bytes: bytes, source: str = "manual") -> dict:
    if len(png_bytes) > _VM_SCREENSHOT_MAX_BYTES:
        raise ValueError("screenshot exceeds configured size limit")
    _read_vm_screenshot_metadata(vmid, prune=True)
    screenshot_id = uuid4().hex
    captured_at_dt = datetime.now(timezone.utc)
    captured_at = captured_at_dt.isoformat()
    expires_at = (captured_at_dt + timedelta(seconds=_VM_SCREENSHOT_STORE_TTL_SECONDS)).isoformat()
    filename = f"{screenshot_id}.png"
    vm_dir = _vm_screenshot_dir(vmid)
    vm_dir.mkdir(parents=True, exist_ok=True)
    image_path = vm_dir / filename
    image_path.write_bytes(png_bytes)
    item = {
        "id": screenshot_id,
        "vmid": int(vmid),
        "filename": filename,
        "image_url": f"/api/vms/{int(vmid)}/screenshots/{screenshot_id}",
        "content_type": "image/png",
        "captured_at": captured_at,
        "expires_at": expires_at,
        "source": source or "manual",
        "bytes": len(png_bytes),
    }
    existing = _read_vm_screenshot_metadata(vmid, prune=False).get("items") or []
    items = [item, *[old for old in existing if old.get("id") != screenshot_id]]
    items = sorted(items, key=lambda old: str(old.get("captured_at") or ""), reverse=True)[
        :_VM_SCREENSHOT_HISTORY_LIMIT
    ]
    keep_files = {str(old.get("filename") or "") for old in items}
    _write_vm_screenshot_metadata(vmid, items)
    for old in existing:
        filename_old = str(old.get("filename") or "")
        if filename_old and filename_old not in keep_files:
            try:
                (vm_dir / filename_old).unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass
    return _public_vm_screenshot(item)


def _vm_screenshot_history(vmid: int) -> list[dict]:
    metadata = _read_vm_screenshot_metadata(vmid, prune=True)
    return [_public_vm_screenshot(item) for item in metadata.get("items") or []]


def _latest_vm_screenshot(vmid: int) -> dict | None:
    history = _vm_screenshot_history(vmid)
    return history[0] if history else None


def _running_vmids_for_screenshot_capture() -> list[int]:
    rows = _vms_from_monitor_snapshot()
    if not rows:
        rows = list(_VMS_CACHE.get("data") or [])
    vmids: list[int] = []
    for row in rows:
        if str(row.get("status") or "").lower() != "running":
            continue
        try:
            vmid = int(row.get("vmid"))
        except Exception:
            continue
        if vmid > 0:
            vmids.append(vmid)
    return sorted(set(vmids))


def _capture_running_vm_screenshots_once() -> dict:
    enabled = str(os.environ.get("AUTOPILOT_VM_SCREENSHOT_COLLECTOR", "1")).lower() not in {"0", "false", "no", "off"}
    if not enabled:
        return {"enabled": False, "running": 0, "captured": 0, "failed": 0}
    vmids = _running_vmids_for_screenshot_capture()
    captured = 0
    failed = 0
    for vmid in vmids:
        try:
            png = _capture_vm_screenshot_png(vmid)
            _store_vm_screenshot_record(
                vmid=vmid,
                png_bytes=png,
                source="collector",
            )
            captured += 1
        except Exception:
            failed += 1
    return {
        "enabled": True,
        "running": len(vmids),
        "captured": captured,
        "failed": failed,
    }


def _ppm_to_png(ppm_bytes: bytes) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(ppm_bytes)) as img:
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()


def _resolve_proxmox_node_host(node: str) -> str:
    try:
        rows = _proxmox_api("/cluster/status") or []
    except Exception:
        rows = []
    for row in rows:
        if (
            isinstance(row, dict)
            and row.get("type") == "node"
            and row.get("name") == node
            and row.get("ip")
        ):
            return str(row["ip"])
    cfg = _load_proxmox_config()
    if node == cfg.get("proxmox_node"):
        return cfg.get("proxmox_host", node)
    return node


def _capture_vm_screenshot_png(vmid: int) -> bytes:
    """Capture the VM display plane through QEMU monitor screendump.

    This deliberately does not use QGA so screenshots still work during
    WinPE, OOBE, boot failures, and bugcheck screens.
    """
    node = _resolve_vm_node(vmid)
    ssh = _make_root_ssh_runner(host=_resolve_proxmox_node_host(node))
    if ssh is None:
        raise RuntimeError(
            "root SSH is required for screenshot capture; configure "
            "vault_proxmox_root_password"
        )
    remote_path = f"/tmp/pveautopilot-screenshot-{vmid}-{uuid4().hex}.ppm"
    monitor_command = f"screendump {remote_path}"
    cmd = (
        f"pvesh create /nodes/{shlex.quote(str(node))}/qemu/{int(vmid)}/monitor "
        f"--command {shlex.quote(monitor_command)} >/dev/null "
        f"&& cat {shlex.quote(remote_path)}; "
        f"rc=$?; rm -f {shlex.quote(remote_path)}; exit $rc"
    )
    rc, stdout, stderr = ssh(cmd)
    if rc != 0:
        detail = (stderr or stdout or b"").decode(errors="replace").strip()
        raise RuntimeError(f"screendump failed for VM {vmid}: {detail or rc}")
    if not stdout.startswith(b"P6") and not stdout.startswith(b"P3"):
        raise RuntimeError("screendump did not return PPM image data")
    return _ppm_to_png(stdout)


async def _live_screenshot_handler(vmid: int, fmt: str) -> dict:
    if fmt.lower() != "png":
        raise ValueError("only png screenshots are supported")
    png = await asyncio.to_thread(_capture_vm_screenshot_png, vmid)
    return _store_screenshot(vmid=vmid, png_bytes=png)


def _live_first_ipv4(network_data: dict) -> str:
    interfaces = network_data.get("result", network_data)
    if not isinstance(interfaces, list):
        return ""
    for iface in interfaces:
        for addr in iface.get("ip-addresses", []) or []:
            ip = addr.get("ip-address") or ""
            if addr.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
                return ip
    return ""


async def _live_snapshot_provider(topics: set[str], vmids: set[int]) -> list[dict]:
    topics = topics or {"fleet"}
    messages: list[dict] = []
    if "fleet" in topics:
        cache, cache_age = await _get_vms_payload()
        rows = list(cache["data"] or [])
        if vmids:
            rows = [row for row in rows if int(row.get("vmid") or 0) in vmids]
        messages.append({
            "type": "snapshot",
            "topic": "fleet",
            "data": {
                "rows": rows,
                "monitor_sweep": _latest_monitor_sweep_status() or {},
                "cache_age_seconds": None if cache_age == float("inf") else round(cache_age, 1),
                "refreshing": bool(_VMS_CACHE["refreshing"]),
                "generated_at": utc_now_iso(),
            },
        })
    if "jobs" in topics:
        messages.append({
            "type": "snapshot",
            "topic": "jobs",
            "data": await asyncio.to_thread(_live_jobs_payload),
        })
    if "runs" in topics:
        messages.append({
            "type": "snapshot",
            "topic": "runs",
            "data": {
                "runs": await asyncio.to_thread(_live_recent_ts_runs),
                "generated_at": utc_now_iso(),
            },
        })
    if "agents" in topics:
        messages.append({
            "type": "snapshot",
            "topic": "agents",
            "data": {
                "agents": await asyncio.to_thread(_agent_inventory_rows_for_current_vms),
                "generated_at": utc_now_iso(),
            },
        })
    return messages


def _live_recent_ts_runs(limit: int = 50) -> list[dict]:
    try:
        from web import db_pg

        with db_pg.connection(_database_url()) as conn:
            rows = conn.execute(
                """
                SELECT id, state, phase, vmid, computer_name, serial_number,
                       cursor_step_id, started_at, finished_at, last_error
                FROM ts_provisioning_runs
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
    except Exception:
        return []
    return [
        {
            "id": str(row["id"]),
            "state": row["state"],
            "phase": row["phase"],
            "vmid": row["vmid"],
            "computer_name": row["computer_name"],
            "serial_number": row["serial_number"],
            "cursor_step_id": str(row["cursor_step_id"]) if row["cursor_step_id"] else None,
            "started_at": row["started_at"].isoformat() if row["started_at"] else None,
            "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
            "completed_at": row["finished_at"].isoformat() if row["finished_at"] else None,
            "last_error": row["last_error"],
        }
        for row in rows
    ]


async def _live_patch_provider(
    topics: set[str], vmids: set[int], include_qga: bool,
) -> list[dict]:
    messages: list[dict] = []
    if "fleet" in topics:
        rows = await asyncio.to_thread(_live_collect_fleet_patch, vmids, include_qga)
        if rows:
            messages.append({
                "type": "patch",
                "topic": "fleet",
                "rows": rows,
                "generated_at": utc_now_iso(),
            })
    if "jobs" in topics:
        messages.append({
            "type": "patch",
            "topic": "jobs",
            "data": await asyncio.to_thread(_live_jobs_payload),
        })
    if "runs" in topics:
        messages.append({
            "type": "patch",
            "topic": "runs",
            "runs": await asyncio.to_thread(_live_recent_ts_runs),
            "generated_at": utc_now_iso(),
        })
    if "agents" in topics:
        messages.append({
            "type": "patch",
            "topic": "agents",
            "agents": await asyncio.to_thread(_agent_inventory_rows_for_current_vms),
            "generated_at": utc_now_iso(),
        })
    return messages


def _live_collect_fleet_patch(vmids: set[int], include_qga: bool) -> list[dict]:
    cached_rows = list(_VMS_CACHE.get("data") or [])
    if not cached_rows:
        cached_rows = _vms_from_monitor_snapshot()
    if vmids:
        target_vmids = sorted(vmids)
    else:
        target_vmids = sorted(
            int(row.get("vmid") or 0) for row in cached_rows if row.get("vmid")
        )
    rows: list[dict] = []
    for vmid in target_vmids:
        row: dict = {"vmid": vmid}
        try:
            node = _resolve_vm_node(vmid)
            status = _proxmox_api(f"/nodes/{node}/qemu/{vmid}/status/current")
            if isinstance(status, dict):
                row.update({
                    "status": status.get("status"),
                    "qmpstatus": status.get("qmpstatus"),
                    "uptime": status.get("uptime", 0),
                    "cpu": status.get("cpu", 0),
                    "mem": status.get("mem", 0),
                    "maxmem": status.get("maxmem", 0),
                })
        except Exception as exc:
            row.update({"status_error": str(exc), "qga": "unknown"})
        if include_qga and row.get("status") == "running":
            failure = _LIVE_QGA_FAILURES.get(vmid) or {}
            now = time.monotonic()
            retry_at = float(failure.get("retry_at") or 0)
            if retry_at > now:
                row["qga"] = "unavailable"
                row["qga_error"] = failure.get("error") or "QGA unavailable"
                row["qga_retry_in_seconds"] = max(0, int(retry_at - now))
                rows.append(row)
                continue
            try:
                # Keep the background live loop to a cheap health check.
                # Deeper QGA calls such as guest-exec and
                # network-get-interfaces can wedge unstable Windows agents.
                _proxmox_api(f"/nodes/{node}/qemu/{vmid}/agent/info")
                _LIVE_QGA_FAILURES.pop(vmid, None)
                row["qga"] = "ready"
            except Exception as exc:
                row["qga"] = "unavailable"
                row["qga_error"] = str(exc)
                _LIVE_QGA_FAILURES[vmid] = {
                    "error": str(exc),
                    "failed_at": now,
                    "retry_at": now + _LIVE_QGA_FAILURE_BACKOFF_SECONDS,
                }
        elif include_qga:
            row["qga"] = "not_running"
        rows.append(row)
    return rows


async def _live_refresh_handler(scope: str) -> list[dict]:
    if scope == "fleet":
        await _run_monitor_sweep_and_refresh_vms_cache()
    elif scope in {"jobs", "runs"}:
        pass
    else:
        raise ValueError(f"unsupported refresh scope: {scope}")
    finished = {
        "type": "event",
        "topic": "fleet" if scope == "fleet" else scope,
        "event": "sweep_finished" if scope == "fleet" else "refresh_finished",
        "scope": scope,
        "generated_at": utc_now_iso(),
    }
    snapshots = await _live_snapshot_provider({scope} if scope != "fleet" else {"fleet"}, set())
    return [finished, *snapshots]


async def _live_qga_probe_handler(vmid: int) -> dict:
    node = _resolve_vm_node(vmid)
    failure = _LIVE_QGA_FAILURES.get(vmid) or {}
    now = time.monotonic()
    retry_at = float(failure.get("retry_at") or 0)
    if retry_at > now:
        return {
            "qga": "unavailable",
            "node": node,
            "vmid": vmid,
            "qga_error": failure.get("error") or "QGA unavailable",
            "qga_retry_in_seconds": max(0, int(retry_at - now)),
        }

    try:
        data = await asyncio.to_thread(
            _proxmox_api, f"/nodes/{node}/qemu/{vmid}/agent/info",
        )
    except Exception as exc:
        _LIVE_QGA_FAILURES[vmid] = {
            "error": str(exc),
            "failed_at": now,
            "retry_at": now + _LIVE_QGA_FAILURE_BACKOFF_SECONDS,
        }
        return {
            "qga": "unavailable",
            "node": node,
            "vmid": vmid,
            "qga_error": str(exc),
            "qga_retry_in_seconds": _LIVE_QGA_FAILURE_BACKOFF_SECONDS,
        }

    _LIVE_QGA_FAILURES.pop(vmid, None)
    result = data.get("result") if isinstance(data, dict) else {}
    if not isinstance(result, dict):
        result = {}
    supported = result.get("supported_commands") or []
    return {
        "qga": "ready",
        "node": node,
        "vmid": vmid,
        "version": result.get("version"),
        "supported_command_count": (
            len(supported) if isinstance(supported, list) else 0
        ),
    }


def _get_live_hub() -> LiveHub:
    global _LIVE_HUB
    if _LIVE_HUB is None:
        _LIVE_HUB = LiveHub(
            snapshot_provider=_live_snapshot_provider,
            patch_provider=_live_patch_provider,
            refresh_handler=_live_refresh_handler,
            qga_probe_handler=_live_qga_probe_handler,
            screenshot_handler=_live_screenshot_handler,
            # Keep recurring live updates off the QGA transport. Windows
            # qemu-ga can wedge under repeated background RPCs; explicit
            # qga_probe requests are still available for operator-driven checks.
            qga_interval_seconds=None,
        )
    return _LIVE_HUB


def _websocket_authenticated(websocket: WebSocket) -> bool:
    if _AUTH_BYPASS:
        return True
    try:
        return bool(websocket.session.get("user"))
    except Exception:
        return False


@app.websocket("/api/live/ws")
async def live_websocket(websocket: WebSocket):
    if not _websocket_authenticated(websocket):
        await websocket.close(code=1008, reason="authentication required")
        return
    await _get_live_hub().connect(websocket)


@app.get("/api/live/screenshots/{screenshot_id}")
async def live_screenshot(screenshot_id: str):
    _purge_expired_screenshots()
    item = _SCREENSHOT_CACHE.get(screenshot_id)
    if not item:
        raise HTTPException(status_code=404, detail="screenshot expired or not found")
    return Response(
        content=item["content"],
        media_type=item["content_type"],
        headers={
            "Cache-Control": "private, max-age=120",
            "X-VMID": str(item["vmid"]),
            "X-Captured-At": item["captured_at"],
        },
    )


@app.get("/api/vms/{vmid}/screenshots/latest", response_model=VmScreenshotResponse)
async def api_vm_latest_screenshot(vmid: int):
    latest = _latest_vm_screenshot(vmid)
    if not latest:
        raise HTTPException(status_code=404, detail="no screenshot available")
    return latest


@app.get("/api/vms/{vmid}/screenshots/{screenshot_id}")
async def api_vm_screenshot_image(vmid: int, screenshot_id: str):
    metadata = _read_vm_screenshot_metadata(vmid, prune=True)
    item = next(
        (
            candidate for candidate in metadata.get("items") or []
            if str(candidate.get("id") or "") == screenshot_id
        ),
        None,
    )
    if not item:
        raise HTTPException(status_code=404, detail="screenshot expired or not found")
    filename = str(item.get("filename") or "")
    if not filename or not re.fullmatch(r"[a-f0-9]{32}\.png", filename):
        raise HTTPException(status_code=404, detail="screenshot expired or not found")
    path = _vm_screenshot_dir(vmid) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="screenshot expired or not found")
    return FileResponse(
        path,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=60",
            "X-VMID": str(int(vmid)),
            "X-Captured-At": str(item.get("captured_at") or ""),
        },
    )


def _optional_agent_int(value) -> int | None:
    text = _optional_text(value)
    if not text:
        return None
    try:
        parsed = int(text)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid VMID: {value!r}")
    if parsed <= 0:
        raise ValueError(f"Invalid VMID: {value!r}")
    return parsed


def _agent_metadata_form(
    *,
    agent_id: str,
    vmid,
    computer_name: str,
    serial_number: str,
    agent_version: str,
    created_from_run_id: str = "",
) -> dict:
    agent_id = _sanitize_input(_optional_text(agent_id))
    if not agent_id:
        raise ValueError("agent_id is required")
    values = {
        "agent_id": agent_id,
        "vmid": _optional_agent_int(vmid),
        "computer_name": _optional_text(computer_name),
        "serial_number": _optional_text(serial_number),
        "agent_version": _optional_text(agent_version),
        "created_from_run_id": _optional_text(created_from_run_id) or None,
    }
    for key in ("computer_name", "serial_number", "agent_version"):
        if values[key]:
            values[key] = _sanitize_input(values[key])
    return values


@app.post("/api/agents")
async def create_agent_record(
    request: Request,
):
    try:
        payload = await _request_values(request)
        values = _agent_metadata_form(
            agent_id=_request_text(payload, "agent_id"),
            vmid=_request_text(payload, "vmid"),
            computer_name=_request_text(payload, "computer_name"),
            serial_number=_request_text(payload, "serial_number"),
            agent_version=_request_text(payload, "agent_version"),
            created_from_run_id=_request_text(payload, "created_from_run_id"),
        )
        from web import db_pg

        with db_pg.connection(_database_url()) as conn:
            agent_telemetry_pg.init(conn)
            agent_telemetry_pg.upsert_manual_agent(conn, **values)
    except Exception as exc:
        return _action_error_response(request, message=f"Create agent failed: {exc}")
    return _action_response(
        request,
        payload={"ok": True, "agent_id": values["agent_id"], "action": "create"},
    )


@app.post("/api/agents/{agent_id}/update")
async def update_agent_record(
    agent_id: str,
    request: Request,
):
    try:
        payload = await _request_values(request)
        values = _agent_metadata_form(
            agent_id=agent_id,
            vmid=_request_text(payload, "vmid"),
            computer_name=_request_text(payload, "computer_name"),
            serial_number=_request_text(payload, "serial_number"),
            agent_version=_request_text(payload, "agent_version"),
            created_from_run_id=_request_text(payload, "created_from_run_id"),
        )
        from web import db_pg

        with db_pg.connection(_database_url()) as conn:
            agent_telemetry_pg.init(conn)
            updated = agent_telemetry_pg.update_agent_metadata(conn, **values)
        if updated is None:
            return _action_error_response(request, message=f"Agent not found: {values['agent_id']}", status_code=404)
    except Exception as exc:
        return _action_error_response(request, message=f"Update agent failed: {exc}")
    return _action_response(
        request,
        payload={"ok": True, "agent_id": values["agent_id"], "action": "update"},
    )


@app.post("/api/agents/{agent_id}/delete")
async def delete_agent_record(agent_id: str, request: Request):
    try:
        agent_id = _sanitize_input(_optional_text(agent_id))
        if not agent_id:
            raise ValueError("agent_id is required")
        from web import db_pg

        with db_pg.connection(_database_url()) as conn:
            agent_telemetry_pg.init(conn)
            deleted = agent_telemetry_pg.hard_delete_agent(conn, agent_id)
        if not deleted:
            return _action_error_response(request, message=f"Agent not found: {agent_id}", status_code=404)
    except Exception as exc:
        return _action_error_response(request, message=f"Delete agent failed: {exc}")
    return _action_response(
        request,
        payload={"ok": True, "agent_id": agent_id, "action": "delete"},
    )


@app.post("/api/agent-approvals/{approval_id}/approve")
async def approve_agent_bootstrap(approval_id: str, request: Request):
    agent_token = None
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.json()
            if isinstance(body, dict):
                agent_token = body.get("agent_token") or None
    except Exception:
        agent_token = None
    try:
        from web import db_pg

        with db_pg.connection(_database_url()) as conn:
            agent_telemetry_pg.init(conn)
            approval = agent_telemetry_pg.approve_bootstrap_approval(
                conn,
                approval_id,
                agent_token=agent_token,
            )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not approval:
        raise HTTPException(status_code=404, detail="approval not found")
    return {
        "ok": True,
        "approval_id": approval["approval_id"],
        "agent_id": approval["agent_id"],
        "approval_status": approval["status"],
    }


def _cache_fetched_at_iso(cache_age: float | None) -> str:
    if cache_age is None or cache_age == float("inf"):
        return ""
    return datetime.fromtimestamp(
        time.time() - cache_age, tz=timezone.utc
    ).isoformat(timespec="seconds")


async def _vms_fleet_payload() -> dict:
    cache, cache_age = await _get_vms_payload()
    vms = [dict(vm) for vm in list(cache["data"] or [])]
    proxmox_vms = _proxmox_cluster_vm_rows() or vms
    vm_serials = {vm["serial"] for vm in vms if vm.get("serial")}
    devices, ap_error = cache["devices"] or ([], "")
    devices = [dict(device) for device in devices]
    hash_serials = cache["hash_serials"] or set()
    ap_serials = {d["serial"] for d in devices}
    serial_to_vm = {vm["serial"]: vm for vm in vms if vm.get("serial")}

    matched_devices = [d for d in devices if d["serial"] in vm_serials]
    for device in matched_devices:
        device["has_local_hash"] = device["serial"] in hash_serials
        if not device.get("display_name"):
            vm = serial_to_vm.get(device["serial"])
            if vm and vm.get("hostname"):
                device["display_name"] = vm["hostname"]

    for vm in vms:
        vm["in_autopilot"] = vm.get("serial", "") in ap_serials
        vm["in_intune"] = bool(vm.get("in_intune"))
        vm["has_hash"] = vm.get("serial", "") in hash_serials
        prov = sequences_db.get_vm_provisioning(SEQUENCES_DB, vmid=vm["vmid"])
        seq = None
        if prov and prov.get("sequence_id"):
            seq = sequences_db.get_sequence(SEQUENCES_DB, prov["sequence_id"])
        vm["target_os"] = (seq or {}).get("target_os") or "windows"
        vm["sequence_name"] = (seq or {}).get("name")

    autopilot_vms = [vm for vm in vms if vm.get("in_autopilot")]
    if autopilot_vms:
        try:
            from web import db_pg

            with db_pg.connection(_database_url()) as conn:
                for vm in autopilot_vms:
                    machine_lifecycle_pg.observe_autopilot_registration(
                        conn,
                        vmid=vm.get("vmid"),
                        serial_number=vm.get("serial") or "",
                        computer_name=vm.get("hostname") or vm.get("name") or "",
                        registered=True,
                        evidence={"serial": vm.get("serial") or ""},
                    )
                conn.commit()
        except Exception:
            pass

    try:
        lifecycles = machine_lifecycle_pg.current_by_vmids([
            int(vm["vmid"]) for vm in vms if vm.get("vmid") is not None
        ])
    except Exception:
        lifecycles = {}
    vms = [
        _apply_lifecycle(vm, lifecycles.get(int(vm["vmid"])))
        for vm in vms
    ]

    missing_vms = [
        vm for vm in vms
        if not vm["in_autopilot"] and not vm["in_intune"] and vm.get("serial")
    ]
    agents = _filter_and_purge_agents_without_current_vm(_agent_inventory_rows(), vms)
    bubble_topology = {
        "workstation_fleets": [],
        "critical_infrastructure": [],
        "connected_services": [],
        "unassigned_assets": [],
        "warnings": [],
        "gate_states": [],
    }
    try:
        from web import db_pg, lab_bubbles_pg

        with db_pg.connection(_database_url()) as conn:
            lab_bubbles_pg.init(conn)
            bubble_topology = lab_bubbles_pg.build_vm_page_payload(
                conn,
                vms=vms,
                agent_rows=agents,
            )
    except Exception:
        import logging as _logging

        _logging.getLogger("web.react.vms").exception("bubble topology unavailable")
        bubble_topology["warnings"] = ["Bubble topology is temporarily unavailable."]

    return {
        "vms": vms,
        "proxmox_vms": proxmox_vms,
        "missing_vms": missing_vms,
        "agents": agents,
        "autopilot_devices": matched_devices,
        "bubble_topology": bubble_topology,
        "ap_error": ap_error or "",
        "cache_age_seconds": None if cache_age == float("inf") else int(cache_age),
        "cache_fetched_at_iso": _cache_fetched_at_iso(cache_age),
        "cache_refreshing": bool(_VMS_CACHE["refreshing"]),
        "monitor_sweep": _latest_monitor_sweep_status(),
        "generated_at": utc_now_iso(),
    }


@app.get("/api/vms/fleet", response_model=VmsFleetResponse)
async def api_vms_fleet():
    return await _vms_fleet_payload()


async def _render_legacy_vms(request: Request, error: str = ""):
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

    fleet = await _vms_fleet_payload()

    return templates.TemplateResponse("vms.html", {
        "request": request,
        "vms": fleet["vms"],
        "devices": fleet["autopilot_devices"],
        "missing_vms": fleet["missing_vms"],
        "agent_devices": fleet["agents"],
        "ap_error": fleet["ap_error"],
        "error": error,
        # Surface to the footer so the operator can tell whether
        "cache_age_seconds": fleet["cache_age_seconds"],
        "cache_fetched_at_iso": fleet["cache_fetched_at_iso"],
        "cache_refreshing": fleet["cache_refreshing"],
        "monitor_sweep": fleet["monitor_sweep"],
    })


@app.get("/vms", include_in_schema=False)
async def vms_page(request: Request):
    return _primary_ui_redirect("/react/vms")


@app.get("/legacy/vms", response_class=HTMLResponse, include_in_schema=False)
async def legacy_vms_page(request: Request, error: str = ""):
    return await _render_legacy_vms(request, error=error)


# --- API Endpoints ---


def _launch_provision_job(playbook_path: str, extra_vars: dict | None = None):
    """Launch an ansible-playbook run for the WinPE provision path.

    Builds a ``-e key=value`` command list from extra_vars, enqueues a job
    via job_manager, and returns a JSONResponse so the WinPE branch of
    start_provision can early-return with HTTP 200. The test suite
    monkeypatches this function to intercept launches without touching the
    real Ansible or Proxmox infra."""
    extra_vars = extra_vars or {}
    cmd = ["ansible-playbook", str(BASE_DIR / playbook_path)]
    for key, value in extra_vars.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        cmd += ["-e", f"{key}={value}"]
    job = job_manager.start("provision_winpe", cmd, args=dict(extra_vars))
    return JSONResponse({"ok": True, "job_id": job["id"]})


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


def _form_flag(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _cloudosd_generate_serial(*, profile: str, serial_prefix: str) -> str:
    prefix = (serial_prefix or "").strip().rstrip("-")
    prefix = re.sub(r"[^A-Za-z0-9-]", "", prefix).strip("-")
    suffix = uuid4().hex[:8].upper()
    if not prefix:
        return suffix
    return f"{prefix}-{suffix}"


def _cloudosd_pattern_has_token(pattern: str, token: str) -> bool:
    return re.search(r"\{" + re.escape(token) + r"\}", pattern or "", re.IGNORECASE) is not None


_CLOUDOSD_VMID_RESERVATION_LOCK = "proxmoxveautopilot:cloudosd-vmid-reservation"


def _cloudosd_candidate_vmids(count: int, *, reserved_vmids: set[int] | None = None) -> list[int]:
    first = int(_proxmox_api("/cluster/nextid"))
    reserved = set(reserved_vmids or set())
    used: set[int] = set(reserved)
    try:
        for row in _proxmox_api("/cluster/resources?type=vm") or []:
            if row.get("vmid") is not None:
                used.add(int(row["vmid"]))
    except Exception:
        # /cluster/nextid is the authoritative first free ID. If the
        # broader resource read is unavailable, sequential candidates
        # still honor locally reserved IDs and keep the operator moving.
        used = set(reserved)
    vmids: list[int] = []
    candidate = first
    while len(vmids) < count:
        if candidate not in used:
            vmids.append(candidate)
            used.add(candidate)
        candidate += 1
    return vmids


def _cloudosd_active_reserved_vmids(conn) -> set[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT requested_vmid
        FROM cloudosd_runs
        WHERE requested_vmid IS NOT NULL
          AND state NOT IN ('complete', 'failed', 'cancelled', 'canceled')
        """,
    ).fetchall()
    return {int(row["requested_vmid"]) for row in rows if row.get("requested_vmid")}


def _cloudosd_lock_vmid_reservations(conn) -> None:
    conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (_CLOUDOSD_VMID_RESERVATION_LOCK,))


def _cloudosd_unlock_vmid_reservations(conn) -> None:
    try:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_CLOUDOSD_VMID_RESERVATION_LOCK,))
        conn.commit()
    except psycopg.errors.InFailedSqlTransaction:
        conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_CLOUDOSD_VMID_RESERVATION_LOCK,))
        conn.commit()


_LEGACY_WINDOWS_SEQUENCE_STEPS = {
    "set_oem_hardware",
    "autopilot_entra",
    "local_admin",
    "join_ad_domain",
    "rename_computer",
    "run_script",
    "install_module",
}


def _enabled_legacy_steps(sequence: dict | None) -> list[dict]:
    if not sequence:
        return []
    return [
        step
        for step in sequence.get("steps", []) or []
        if step.get("enabled", True)
    ]


def _cloudosd_supported_enabled_steps(sequence: dict | None) -> list[str]:
    return [
        step.get("step_type", "")
        for step in _enabled_legacy_steps(sequence)
        if step.get("step_type") == "join_ad_domain"
    ]


def _legacy_sequence_boot_modes(sequence: dict | None) -> list[str]:
    """Return /provision boot modes that may select this legacy sequence.

    Ubuntu now uses the v2 sequence selector, so legacy Ubuntu rows are not
    exposed in the shared Windows sequence dropdown. OSDCloud intentionally
    accepts only the small supported legacy intent surface it can own today;
    base OSDCloud deployment is represented by the blank option instead.
    """
    from web import cloudosd_sequence as _cloudosd_sequence

    if not sequence or (sequence.get("target_os") or "windows") != "windows":
        return []

    name = str(sequence.get("name") or "").strip().casefold()
    enabled_steps = _enabled_legacy_steps(sequence)
    enabled_types = {step.get("step_type", "") for step in enabled_steps}
    unsupported_cloudosd = _cloudosd_sequence.unsupported_enabled_steps(sequence)
    cloudosd_supported = _cloudosd_supported_enabled_steps(sequence)
    cloudosd_compatible = bool(cloudosd_supported) and not unsupported_cloudosd

    if cloudosd_compatible:
        return ["cloudosd"]
    if name.startswith("cloudosd"):
        return []
    if name.startswith("winpe"):
        return ["winpe"]
    if enabled_types and not enabled_types.issubset(_LEGACY_WINDOWS_SEQUENCE_STEPS):
        return []
    if "autopilot_hybrid" in enabled_types:
        return []
    return ["clone", "winpe"]


def _assert_sequence_allowed_for_boot_mode(sequence_id: int | None, boot_mode: str) -> dict | None:
    if not sequence_id:
        return None
    sequence = sequences_db.get_sequence(SEQUENCES_DB, int(sequence_id))
    if sequence is None:
        raise HTTPException(404, f"sequence {sequence_id} not found")
    allowed = _legacy_sequence_boot_modes(sequence)
    if boot_mode not in allowed:
        if boot_mode == "cloudosd":
            from web import cloudosd_sequence as _cloudosd_sequence

            unsupported = _cloudosd_sequence.unsupported_enabled_steps(sequence)
            if unsupported:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "OSDCloud selected sequence is not OSDCloud-compatible yet; "
                        f"unsupported enabled step(s): {', '.join(unsupported)}"
                    ),
                )
        labels = {
            "cloudosd": "OSDCloud",
            "osdeploy": "OSDeploy v2",
            "winpe": "WinPE",
            "clone": "Clone",
            "ubuntu": "Ubuntu v2",
        }
        raise HTTPException(
            status_code=400,
            detail=(
                f"Task sequence {sequence_id} is not available for "
                f"{labels.get(boot_mode, boot_mode)} boot mode."
            ),
        )
    return sequence


def _replace_cloudosd_token(pattern: str, token: str, value: str) -> str:
    return re.sub(
        r"\{" + re.escape(token) + r"\}",
        value,
        pattern,
        flags=re.IGNORECASE,
    )


def _cloudosd_batch_names(
    *,
    profile: str = "",
    hostname_pattern: str,
    count: int,
    serial_prefix: str,
    requested_vmids: list[int] | None = None,
) -> list[dict]:
    pattern = (hostname_pattern or "").strip() or "cloudosd-{index}"
    placeholders = set(re.findall(r"\{[^{}]*\}", pattern))
    allowed_placeholders = {"{index}", "{serial}", "{vmid}"}
    normalized_placeholders = {placeholder.lower() for placeholder in placeholders}
    invalid_placeholders = sorted(normalized_placeholders - allowed_placeholders)
    pattern_without_tokens = re.sub(r"\{[^{}]*\}", "", pattern)
    if (
        invalid_placeholders
        or "{" in pattern_without_tokens
        or "}" in pattern_without_tokens
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "OSDCloud hostname pattern contains an invalid placeholder. "
                "Use only {index}, {serial}, and {vmid}."
            ),
        )
    uses_index = _cloudosd_pattern_has_token(pattern, "index")
    uses_serial = _cloudosd_pattern_has_token(pattern, "serial")
    uses_vmid = _cloudosd_pattern_has_token(pattern, "vmid")
    if count > 1 and not (uses_index or uses_serial or uses_vmid):
        pattern = f"{pattern}-{{index}}"
        uses_index = True
    requested_vmids = requested_vmids or _cloudosd_candidate_vmids(count)
    if len(requested_vmids) != count:
        raise HTTPException(
            status_code=500,
            detail="OSDCloud VMID reservation returned the wrong number of VMIDs",
        )
    plans: list[dict] = []
    for index in range(1, count + 1):
        serial = _cloudosd_generate_serial(
            profile=profile,
            serial_prefix=serial_prefix,
        )
        requested_vmid = requested_vmids[index - 1]
        name = pattern
        name = _replace_cloudosd_token(name, "index", f"{index:02d}")
        name = _replace_cloudosd_token(
            name,
            "vmid",
            str(requested_vmid) if requested_vmid is not None else f"{index:02d}",
        )
        name = _replace_cloudosd_token(name, "serial", serial)
        if not name.strip():
            raise HTTPException(status_code=400, detail="OSDCloud VM name is empty")
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", name):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"OSDCloud hostname pattern produced invalid Proxmox VM "
                    f"name {name!r}. Use letters, numbers, and hyphens only; "
                    "the name cannot start or end with a hyphen."
                ),
            )
        plans.append({
            "name": name,
            "serial": serial,
            "requested_vmid": requested_vmid,
        })
    names = [plan["name"] for plan in plans]
    if len({name.lower() for name in names}) != len(names):
        raise HTTPException(
            status_code=400,
            detail="OSDCloud hostname pattern produced duplicate VM names",
        )
    return plans


def _cloudosd_root_ticket_for_batch(
    *,
    profile: str,
    chassis_type_override: int,
) -> tuple[bool, str | None, str | None]:
    from web import cloudosd_endpoints

    needs_root_ticket = (
        int(chassis_type_override or 0) > 0
        or cloudosd_endpoints._profile_chassis_type(profile) > 0
    )
    if not needs_root_ticket:
        return False, None, None

    cfg = _load_proxmox_config()
    if not cfg.get("vault_proxmox_root_password", ""):
        raise HTTPException(
            status_code=400,
            detail=(
                "OSDCloud OEM/chassis provisioning needs Proxmox root SSH "
                "for host-local SMBIOS staging and QEMU args. Run Settings "
                "-> Proxmox Permission Bootstrap to apply the hypervisor "
                "permissions and store the validated root SSH credential."
            ),
        )
    return True, None, None


def _start_cloudosd_provision_batch(
    *,
    request: Request,
    artifact_id: str,
    profile: str,
    count: int,
    serial_prefix: str,
    group_tag: str,
    cores: int,
    memory_mb: int,
    disk_size_gb: int,
    sequence_id: int | None,
    hostname_pattern: str,
    chassis_type_override: int,
    node: str,
    iso_storage: str,
    storage: str,
    network_bridge: str,
    os_version: str,
    os_activation: str,
    os_edition: str,
    os_language: str,
    tpm_enabled: bool,
    secure_boot: bool,
    firmware_updates_enabled: bool,
    driver_pack_policy: str,
    analytics_enabled: bool,
    outbound_policy_mode: str,
) -> RedirectResponse:
    from web import cloudosd_endpoints, cloudosd_pg, cloudosd_sequence, db_pg

    count = int(count or 1)
    if count < 1 or count > 50:
        raise HTTPException(status_code=400, detail="OSDCloud count must be between 1 and 50")
    if not artifact_id:
        raise HTTPException(status_code=400, detail="OSDCloud artifact_id is required")

    vm_cores = int(cores or 0) or cloudosd_pg.DEFAULT_VM_CORES
    vm_memory_mb = int(memory_mb or 0) or cloudosd_pg.DEFAULT_VM_MEMORY_MB
    vm_disk_size_gb = int(disk_size_gb or 0) or cloudosd_pg.DEFAULT_VM_DISK_SIZE_GB
    if vm_memory_mb < cloudosd_pg.MIN_VM_MEMORY_MB:
        raise HTTPException(
            status_code=400,
            detail=f"OSDCloud Proxmox VMs need at least {cloudosd_pg.MIN_VM_MEMORY_MB} MB RAM",
        )
    if vm_disk_size_gb < cloudosd_pg.MIN_VM_DISK_SIZE_GB:
        raise HTTPException(
            status_code=400,
            detail=f"OSDCloud VMs need at least {cloudosd_pg.MIN_VM_DISK_SIZE_GB} GB disk",
        )

    source_sequence_id = int(sequence_id) if sequence_id else None
    domain_join_intent = {"enabled": False}
    if source_sequence_id:
        seq = sequences_db.get_sequence(SEQUENCES_DB, source_sequence_id)
        if seq is None:
            raise HTTPException(404, f"sequence {source_sequence_id} not found")

        def _resolve_cloudosd_credential(cid: int):
            return sequences_db.get_credential(SEQUENCES_DB, _cipher(), cid)

        try:
            domain_join_intent = cloudosd_sequence.compile_cloudosd_sequence_intent(
                seq,
                resolve_credential=_resolve_cloudosd_credential,
            )["domain_join"]
        except cloudosd_sequence.CloudOSDSequenceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    jobs = []
    run_ids = []
    with db_pg.connection(_database_url()) as conn:
        cloudosd_pg.init(conn)
        _cloudosd_lock_vmid_reservations(conn)
        try:
            requested_vmids = _cloudosd_candidate_vmids(
                count,
                reserved_vmids=_cloudosd_active_reserved_vmids(conn),
            )
            batch_plan = _cloudosd_batch_names(
                profile=profile,
                hostname_pattern=hostname_pattern,
                count=count,
                serial_prefix=serial_prefix,
                requested_vmids=requested_vmids,
            )
            bodies: list[cloudosd_endpoints.RunCreateBody] = []
            for plan in batch_plan:
                name = plan["name"]
                bodies.append(cloudosd_endpoints.RunCreateBody(
                    artifact_id=artifact_id,
                    vm_name=name,
                    node=node or None,
                    iso_storage=iso_storage or None,
                    storage=storage or None,
                    network_bridge=network_bridge or None,
                    vmid=plan["requested_vmid"],
                    architecture=cloudosd_pg.DEFAULT_ARCHITECTURE,
                    os_version=os_version or cloudosd_pg.DEFAULT_OS_VERSION,
                    os_activation=os_activation or cloudosd_pg.DEFAULT_OS_ACTIVATION,
                    os_edition=os_edition or cloudosd_pg.DEFAULT_OS_EDITION,
                    os_language=os_language or cloudosd_pg.DEFAULT_OS_LANGUAGE,
                    vm_cores=vm_cores,
                    vm_memory_mb=vm_memory_mb,
                    vm_disk_size_gb=vm_disk_size_gb,
                    vm_group_tag=group_tag,
                    vm_oem_profile=profile,
                    chassis_type_override=int(chassis_type_override or 0),
                    source_surface="provision",
                    source_sequence_id=source_sequence_id,
                    tpm_enabled=tpm_enabled,
                    secure_boot=secure_boot,
                    firmware_updates_enabled=firmware_updates_enabled,
                    driver_pack_policy=driver_pack_policy or cloudosd_pg.DEFAULT_DRIVER_PACK_POLICY,
                    analytics_enabled=analytics_enabled,
                    outbound_policy={"mode": outbound_policy_mode or "blocked"},
                ))

            preflights = [cloudosd_endpoints.preflight_payload(body) for body in bodies]
            blockers = [
                check
                for preflight in preflights
                for check in preflight.get("blocking_checks", [])
            ]
            if blockers:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "OSDCloud blocking preflight checks failed",
                        "blocking_checks": blockers,
                    },
                )

            needs_root_ticket, root_ticket, root_csrf = _cloudosd_root_ticket_for_batch(
                profile=profile,
                chassis_type_override=int(chassis_type_override or 0),
            )

            for idx, (body, preflight) in enumerate(zip(bodies, preflights)):
                target = preflight["target"]
                artifact = cloudosd_pg.get_artifact(conn, body.artifact_id)
                if not artifact:
                    raise HTTPException(status_code=404, detail="OSDCloud artifact not found")
                if not artifact.get("proxmox_volid"):
                    raise HTTPException(
                        status_code=409,
                        detail="OSDCloud artifact is not uploaded to Proxmox ISO storage",
                    )
                try:
                    run = cloudosd_pg.create_run(
                        conn,
                        artifact_id=body.artifact_id,
                        vm_name=body.vm_name,
                        node=target["node"],
                        iso_storage=target["iso_storage"],
                        storage=target["storage"],
                        network_bridge=target["network_bridge"],
                        architecture=body.architecture,
                        os_version=body.os_version,
                        os_activation=body.os_activation,
                        os_edition=body.os_edition,
                        os_language=body.os_language,
                        vm_cores=body.vm_cores,
                        vm_memory_mb=body.vm_memory_mb,
                        vm_disk_size_gb=body.vm_disk_size_gb,
                        vm_group_tag=body.vm_group_tag.strip(),
                        vm_oem_profile=body.vm_oem_profile.strip(),
                        chassis_type_override=body.chassis_type_override,
                        source_surface="provision",
                        source_sequence_id=source_sequence_id,
                        requested_vmid=batch_plan[idx]["requested_vmid"],
                        tpm_enabled=body.tpm_enabled,
                        secure_boot=body.secure_boot,
                        firmware_updates_enabled=body.firmware_updates_enabled,
                        driver_pack_policy=body.driver_pack_policy,
                        analytics_enabled=body.analytics_enabled,
                        outbound_policy=body.outbound_policy,
                        domain_join=domain_join_intent,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                extra_vars = cloudosd_endpoints.cloudosd_provision_extra_vars(
                    run=run,
                    artifact=artifact,
                    request=request,
                    root_ticket=root_ticket,
                    root_csrf_token=root_csrf,
                    require_root_ticket=needs_root_ticket,
                )
                if serial_prefix:
                    extra_vars["vm_serial_prefix"] = serial_prefix
                extra_vars["vm_custom_serial"] = batch_plan[idx]["serial"]
                if source_sequence_id:
                    extra_vars["source_sequence_id"] = source_sequence_id
                cmd = [
                    "ansible-playbook",
                    str(PLAYBOOK_DIR / "provision_proxmox_cloudosd.yml"),
                ]
                for key, value in extra_vars.items():
                    cmd.extend(["-e", f"{key}={value}"])
                job = job_manager.start("provision_cloudosd", cmd, args=extra_vars)
                jobs.append(job)
                run_ids.append(run["run_id"])
        finally:
            _cloudosd_unlock_vmid_reservations(conn)

    first_run = run_ids[0] if run_ids else ""
    if len(run_ids) == 1:
        return RedirectResponse(f"/osdcloud/runs/{first_run}", status_code=303)
    return RedirectResponse(
        f"/osdcloud?created={len(run_ids)}&first_run={first_run}",
        status_code=303,
    )


def _start_osdeploy_provision_batch(
    *,
    request: Request,
    artifact_id: str,
    count: int,
    serial_prefix: str,
    cores: int,
    memory_mb: int,
    disk_size_gb: int,
    hostname_pattern: str,
    node: str,
    iso_storage: str,
    storage: str,
    network_bridge: str,
    server_role: str,
    os_version: str,
    os_edition: str,
    os_language: str,
    secure_boot: bool,
    outbound_policy_mode: str,
) -> RedirectResponse:
    from web import db_pg, osdeploy_endpoints, osdeploy_pg

    count = int(count or 1)
    if count < 1 or count > 50:
        raise HTTPException(status_code=400, detail="OSDeploy count must be between 1 and 50")
    if not artifact_id:
        raise HTTPException(status_code=400, detail="OSDeploy artifact_id is required")

    vm_cores = int(cores or 0) or osdeploy_pg.DEFAULT_VM_CORES
    vm_memory_mb = int(memory_mb or 0) or osdeploy_pg.RECOMMENDED_VM_MEMORY_MB
    vm_disk_size_gb = int(disk_size_gb or 0) or osdeploy_pg.DEFAULT_VM_DISK_SIZE_GB
    pattern = (hostname_pattern or "").strip() or "server-{index}"
    if count > 1 and "{index}" not in pattern.lower():
        pattern = f"{pattern}-{{index}}"

    run_ids: list[str] = []
    with db_pg.connection(_database_url()) as conn:
        osdeploy_pg.init(conn)
        artifact = osdeploy_pg.get_artifact(conn, artifact_id)
        if not artifact:
            raise HTTPException(status_code=400, detail="OSDeploy artifact was not found")
        for index in range(1, count + 1):
            name = _replace_cloudosd_token(pattern, "index", f"{index:02d}")
            name = _replace_cloudosd_token(name, "serial", serial_prefix or f"srv{index:02d}")
            name = _replace_cloudosd_token(name, "vmid", f"{index:02d}")
            name = name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="OSDeploy VM name is empty")
            body = osdeploy_endpoints.RunCreateBody(
                artifact_id=artifact_id,
                vm_name=name,
                node=node or None,
                iso_storage=iso_storage or None,
                storage=storage or None,
                network_bridge=network_bridge or None,
                architecture=osdeploy_pg.DEFAULT_ARCHITECTURE,
                server_role=server_role or "base",
                os_version=os_version or osdeploy_pg.DEFAULT_OS_VERSION,
                os_edition=os_edition or osdeploy_pg.DEFAULT_OS_EDITION,
                os_language=os_language or osdeploy_pg.DEFAULT_OS_LANGUAGE,
                vm_cores=vm_cores,
                vm_memory_mb=vm_memory_mb,
                vm_disk_size_gb=vm_disk_size_gb,
                secure_boot=secure_boot,
                outbound_policy={"mode": outbound_policy_mode or "blocked"},
            )
            preflight = osdeploy_endpoints.preflight_payload(body)
            if preflight["blocking_checks"]:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "OSDeploy blocking preflight checks failed",
                        "blocking_checks": preflight["blocking_checks"],
                    },
                )
            run = osdeploy_pg.create_run(
                conn,
                artifact_id=body.artifact_id,
                vm_name=body.vm_name,
                node=body.node,
                iso_storage=body.iso_storage,
                storage=body.storage,
                network_bridge=body.network_bridge,
                architecture=body.architecture,
                server_role=body.server_role,
                os_version=body.os_version,
                os_edition=body.os_edition,
                os_language=body.os_language,
                vm_cores=body.vm_cores,
                vm_memory_mb=body.vm_memory_mb,
                vm_disk_size_gb=body.vm_disk_size_gb,
                secure_boot=body.secure_boot,
                outbound_policy=body.outbound_policy,
            )
            run_ids.append(run["run_id"])
            extra_vars = osdeploy_endpoints.osdeploy_provision_extra_vars(
                run=run,
                artifact=artifact,
                request=request,
            )
            cmd = [
                "ansible-playbook",
                str(PLAYBOOK_DIR / "provision_proxmox_osdeploy.yml"),
            ]
            for key, value in extra_vars.items():
                cmd.extend(["-e", f"{key}={value}"])
            job = job_manager.start(
                "provision_osdeploy",
                cmd,
                args=extra_vars,
            )
            osdeploy_pg.append_event(
                conn,
                run_id=run["run_id"],
                phase="proxmox_playbook",
                event_type="provision_job_queued",
                message="OSDeploy provision job queued",
                data={"job_id": job["id"]},
            )
    if len(run_ids) == 1:
        return RedirectResponse(f"/osdeploy/runs/{run_ids[0]}", status_code=303)
    return RedirectResponse(
        f"/osdeploy?created={len(run_ids)}&first_run={run_ids[0] if run_ids else ''}",
        status_code=303,
    )


@app.post("/api/jobs/provision")
async def start_provision(
    request: Request,
    profile: str = Form(...),
    count: int = Form(1),
    serial_prefix: str = Form(""),
    group_tag: str = Form(""),
    cores: int = Form(0),
    memory_mb: int = Form(0),
    disk_size_gb: int = Form(0),
    sequence_id_raw: str | None = Form(None, alias="sequence_id"),
    ubuntu_v2_sequence_id: str = Form(""),
    ubuntu_template_vmid: int = Form(0),
    hostname_pattern: str = Form("autopilot-{serial}"),
    chassis_type_override: int = Form(0),
    boot_mode: str = Form("clone"),
    artifact_id: str = Form(""),
    osdeploy_artifact_id: str = Form(""),
    osdeploy_server_role: str = Form("base"),
    osdeploy_node: str = Form(""),
    osdeploy_iso_storage: str = Form(""),
    osdeploy_storage: str = Form(""),
    osdeploy_network_bridge: str = Form(""),
    osdeploy_os_version: str = Form(""),
    osdeploy_os_edition: str = Form(""),
    osdeploy_os_language: str = Form(""),
    node: str = Form(""),
    iso_storage: str = Form(""),
    storage: str = Form(""),
    network_bridge: str = Form(""),
    os_version: str = Form(""),
    os_activation: str = Form(""),
    os_edition: str = Form(""),
    os_language: str = Form(""),
    tpm_enabled: str = Form(""),
    secure_boot: str = Form(""),
    firmware_updates_enabled: str = Form(""),
    driver_pack_policy: str = Form(""),
    analytics_enabled: str = Form(""),
    outbound_policy_mode: str = Form("blocked"),
):
    profile = _sanitize_input(profile)
    sequence_id = int(str(sequence_id_raw).strip()) if str(sequence_id_raw or "").strip() else None
    boot_mode = (boot_mode or "clone").lower()
    if boot_mode not in ("clone", "winpe", "cloudosd", "osdeploy", "ubuntu"):
        raise HTTPException(
            status_code=400, detail=f"unknown boot_mode: {boot_mode!r}",
        )
    selected_sequence = None
    if boot_mode in ("clone", "winpe", "cloudosd"):
        selected_sequence = _assert_sequence_allowed_for_boot_mode(sequence_id, boot_mode)
    if boot_mode == "winpe":
        if not _winpe_enabled():
            raise HTTPException(
                status_code=400,
                detail=(
                    "WinPE provisioning is not configured. Set "
                    "winpe_blank_template_vmid, proxmox_winpe_iso, and "
                    "vault_autopilot_winpe_token_secret in inventory and "
                    "restart the container."
                ),
            )
        if not sequence_id:
            raise HTTPException(
                status_code=400,
                detail="WinPE provisioning requires a sequence_id",
            )
        if int(count) != 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    "WinPE provisioning supports count=1 in M1; "
                    "see docs/superpowers/plans/...-winpe-orchestrated-deploy.md"
                ),
            )
    serial_prefix = _optional_text(serial_prefix)
    group_tag = _optional_text(group_tag)
    if group_tag:
        group_tag = _sanitize_input(group_tag)
    if serial_prefix:
        serial_prefix = _sanitize_input(serial_prefix)
    # hostname_pattern contains literal { } tokens — don't _sanitize_input
    # (which strips special chars); just trim and fall back to the default.
    hostname_pattern = (hostname_pattern or "").strip() or "autopilot-{serial}"

    if boot_mode == "cloudosd":
        return _start_cloudosd_provision_batch(
            request=request,
            artifact_id=artifact_id.strip(),
            profile=profile,
            count=int(count or 1),
            serial_prefix=serial_prefix,
            group_tag=group_tag,
            cores=int(cores or 0),
            memory_mb=int(memory_mb or 0),
            disk_size_gb=int(disk_size_gb or 0),
            sequence_id=int(sequence_id) if sequence_id else None,
            hostname_pattern=hostname_pattern,
            chassis_type_override=int(chassis_type_override or 0),
            node=node.strip(),
            iso_storage=iso_storage.strip(),
            storage=storage.strip(),
            network_bridge=network_bridge.strip(),
            os_version=os_version.strip(),
            os_activation=os_activation.strip(),
            os_edition=os_edition.strip(),
            os_language=os_language.strip(),
            tpm_enabled=_form_flag(tpm_enabled),
            secure_boot=_form_flag(secure_boot),
            firmware_updates_enabled=_form_flag(firmware_updates_enabled),
            driver_pack_policy=driver_pack_policy.strip(),
            analytics_enabled=_form_flag(analytics_enabled),
            outbound_policy_mode=outbound_policy_mode.strip() or "blocked",
        )
    if boot_mode == "osdeploy":
        return _start_osdeploy_provision_batch(
            request=request,
            artifact_id=osdeploy_artifact_id.strip(),
            count=int(count or 1),
            serial_prefix=serial_prefix,
            cores=int(cores or 0),
            memory_mb=int(memory_mb or 0),
            disk_size_gb=int(disk_size_gb or 0),
            hostname_pattern=hostname_pattern,
            node=osdeploy_node.strip(),
            iso_storage=osdeploy_iso_storage.strip(),
            storage=osdeploy_storage.strip(),
            network_bridge=osdeploy_network_bridge.strip(),
            server_role=osdeploy_server_role.strip() or "base",
            os_version=osdeploy_os_version.strip(),
            os_edition=osdeploy_os_edition.strip(),
            os_language=osdeploy_os_language.strip(),
            secure_boot=_form_flag(secure_boot),
            outbound_policy_mode=outbound_policy_mode.strip() or "blocked",
        )
    if boot_mode == "ubuntu":
        sequence_id_v2 = (ubuntu_v2_sequence_id or "").strip()
        if not sequence_id_v2:
            raise HTTPException(status_code=400, detail="Ubuntu v2 provisioning requires an Ubuntu v2 sequence")
        result = api_ubuntu_v2_provision(_UbuntuV2ProvisionIn(
            sequence_id=sequence_id_v2,
            count=int(count or 1),
            vm_name_prefix=serial_prefix or profile or "ubuntu-v2",
            hostname_pattern=hostname_pattern,
            group_tag=group_tag,
            vm_cores=int(cores or 0) or 2,
            vm_memory_mb=int(memory_mb or 0) or 4096,
            vm_disk_size_gb=int(disk_size_gb or 0) or 80,
            proxmox_template_vmid=int(ubuntu_template_vmid or 0) or None,
            oem_profile=profile,
            chassis_type=str(chassis_type_override or ""),
        ))
        runs = result.get("runs") or []
        if len(runs) == 1:
            return RedirectResponse(f"/task-engine?ubuntu_run={runs[0]['run_id']}", status_code=303)
        return RedirectResponse(f"/task-engine?ubuntu_created={len(runs)}", status_code=303)

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
        _seq = selected_sequence or sequences_db.get_sequence(SEQUENCES_DB, int(sequence_id))
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
    # WinPE path uses a blank template VM and never sets the QEMU args
    # SMBIOS file, so this check is skipped entirely for winpe boot mode.
    if boot_mode != "winpe":
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
    needs_root_ticket = (boot_mode != "winpe" and bool(_chassis_types_to_stage)) or (
        bool(sequence_id) and boot_mode != "winpe"
    )
    if needs_root_ticket:
        _root_pw = cfg.get("vault_proxmox_root_password", "")
        if not _root_pw:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This provision needs Proxmox root SSH for host-local "
                    "SMBIOS/QEMU args work. Run Settings -> Proxmox "
                    "Permission Bootstrap to apply the hypervisor "
                    "permissions and store the validated root SSH "
                    "credential."
                ),
            )

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
        seq = selected_sequence or sequences_db.get_sequence(SEQUENCES_DB, int(sequence_id))
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

        _causes_reboot_count = compiled.causes_reboot_count
        if boot_mode != "winpe":
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
                        "Sequence-based provisioning needs Proxmox root SSH. "
                        "Run Settings -> Proxmox Permission Bootstrap to apply "
                        "the hypervisor permissions and store the validated root "
                        "SSH credential."
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

    # WinPE path: create run record and hand off to the WinPE playbook.
    # This early-return exits before any clone-specific logic below.
    if boot_mode == "winpe":
        run_id = sequences_db.create_provisioning_run(
            SEQUENCES_DB,
            sequence_id=int(sequence_id),
            provision_path="winpe",
        )
        winpe_extra = dict(resolved_vars)
        winpe_extra.update({
            "run_id": run_id,
            "sequence_id": int(sequence_id),
            "vm_count": int(count),
            "_skip_chassis_type_smbios_file": True,
            # The installed Windows image uses VirtIO devices and the
            # VirtIO ISO remains attached for QGA installation by the
            # OSD client, so let the playbook observe the guest after
            # first boot instead of declaring success at /winpe/done.
            "sequence_hash_capture_phase": seq.get("hash_capture_phase", "oobe"),
            "proxmox_winpe_expect_guest_agent": True,
            "autopilot_base_url": os.environ.get(
                "AUTOPILOT_BASE_URL", "http://127.0.0.1:5000"),
            "_causes_reboot_count": _causes_reboot_count,
        })
        # Form overrides last (highest precedence).
        winpe_extra["vm_oem_profile"] = profile
        for key, val in (
            ("vm_cores", cores), ("vm_memory_mb", memory_mb),
            ("vm_disk_size_gb", disk_size_gb),
            ("vm_serial_prefix", serial_prefix),
            ("vm_group_tag", group_tag),
            ("hostname_pattern", hostname_pattern),
        ):
            if val:
                winpe_extra[key] = val
        if chassis_type_override and int(chassis_type_override) > 0:
            winpe_extra["chassis_type_override"] = int(chassis_type_override)
        _launch_provision_job(
            playbook_path="playbooks/provision_proxmox_winpe.yml",
            extra_vars=winpe_extra,
        )
        # Aligns with WINPE_E2E_RUNBOOK step 4 ("Open /runs/<id>") so an
        # operator submitting the form lands on the timeline page instead
        # of seeing the raw JSON job-id payload the clone path returns.
        return RedirectResponse(f"/runs/{run_id}", status_code=303)
    # Existing clone path continues below unchanged.

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


@app.post("/api/utm/answer-iso/preview")
async def utm_answer_iso_preview(request: Request):
    """Return rendered autounattend.xml for QA without building the ISO.

    Accepts a JSON body with the same profile fields as ``build_answer_iso``:
    ``hostname``, ``locale``, ``timezone``, ``admin_user``, ``admin_pass``,
    ``org_name``, ``product_key``, ``windows_edition``, ``domain_join``,
    ``firstboot_cmds``.

    All fields are optional; omitted fields fall back to role defaults.
    Omitting ``admin_pass`` renders the OOBE section without auto-logon
    or a user-accounts block (safe for QA without exposing secrets).

    Returns 409 when ``hypervisor_type != utm``.
    Returns 200 with ``{"ok": true, "xml": "<rendered XML>"}`` on success.
    """
    c = _utm_check(_load_vars())
    if c:
        return c

    try:
        body = await request.json()
    except Exception:
        body = {}

    try:
        from web.answer_iso import render_arm64_unattend
        xml = render_arm64_unattend(body)
        return {"ok": True, "xml": xml}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"render failed: {exc}"},
            status_code=500,
        )


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


# ---------------------------------------------------------------------------
# UTM snapshot endpoints  (Phase 9 — qemu-img snapshot)
# ---------------------------------------------------------------------------

_SNAP_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _snap_validate_name(name: str):
    """Return a 400 JSONResponse if the snapshot name is invalid, else None."""
    if not _SNAP_NAME_RE.match(name):
        return JSONResponse(
            {"ok": False, "error": "Snapshot name must be 1–64 alphanumeric/._- characters"},
            status_code=400,
        )
    return None


@app.get("/api/utm/vms/{name}/snapshots")
async def utm_list_snapshots(name: str):
    """List qcow2 internal snapshots for a UTM VM.

    Uses qemu-img -l -U so it works on any VM state (including started).
    Returns 409 when hypervisor_type != utm.
    """
    v = _utm_validate_name(name)
    if v:
        return v
    c = _utm_check(_load_vars())
    if c:
        return c

    from web import utm_snapshots
    try:
        snaps = utm_snapshots.list_snapshots(name)
        return {"ok": True, "snapshots": snaps}
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/utm/vms/{name}/snapshots")
async def utm_create_snapshot(name: str, request: Request):
    """Create a named qcow2 snapshot.  VM must be stopped.

    Body: ``{"name": "snap-name", "description": "optional"}``
    Returns 409 when hypervisor_type != utm or VM is not stopped.
    """
    v = _utm_validate_name(name)
    if v:
        return v
    c = _utm_check(_load_vars())
    if c:
        return c

    try:
        body = await request.json()
    except Exception:
        body = {}

    snap_name = (body.get("name") or "").strip()
    if not snap_name:
        return JSONResponse(
            {"ok": False, "error": "Request body must include a non-empty 'name' field"},
            status_code=400,
        )
    sv = _snap_validate_name(snap_name)
    if sv:
        return sv

    description = (body.get("description") or "").strip()

    from web import utm_snapshots
    try:
        result = utm_snapshots.create_snapshot(name, snap_name, description)
        return result
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        msg = str(exc)
        status = 409 if "must be stopped" in msg else 500
        return JSONResponse({"ok": False, "error": msg}, status_code=status)


@app.post("/api/utm/vms/{name}/snapshots/{snap_name}/restore")
async def utm_restore_snapshot(name: str, snap_name: str):
    """Restore (apply) a named qcow2 snapshot.  VM must be stopped.

    Returns 409 when hypervisor_type != utm or VM is not stopped.
    """
    v = _utm_validate_name(name)
    if v:
        return v
    sv = _snap_validate_name(snap_name)
    if sv:
        return sv
    c = _utm_check(_load_vars())
    if c:
        return c

    from web import utm_snapshots
    try:
        result = utm_snapshots.restore_snapshot(name, snap_name)
        return result
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        msg = str(exc)
        status = 409 if "must be stopped" in msg else 500
        return JSONResponse({"ok": False, "error": msg}, status_code=status)


@app.delete("/api/utm/vms/{name}/snapshots/{snap_name}")
async def utm_delete_snapshot(name: str, snap_name: str):
    """Delete a named qcow2 snapshot.  VM must be stopped.

    Returns 409 when hypervisor_type != utm or VM is not stopped.
    """
    v = _utm_validate_name(name)
    if v:
        return v
    sv = _snap_validate_name(snap_name)
    if sv:
        return sv
    c = _utm_check(_load_vars())
    if c:
        return c

    from web import utm_snapshots
    try:
        result = utm_snapshots.delete_snapshot(name, snap_name)
        return result
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        msg = str(exc)
        status = 409 if "must be stopped" in msg else 500
        return JSONResponse({"ok": False, "error": msg}, status_code=status)


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
    try:
        node = _resolve_vm_node(vmid)
    except Exception:
        node = _configured_proxmox_node()
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
    try:
        node = _resolve_vm_node(vmid)
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
    node = websocket.query_params.get("node")
    if not node:
        try:
            node = _resolve_vm_node(vmid)
        except Exception as exc:
            await websocket.close(code=1008, reason=str(exc)[:100])
            return
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")

    await websocket.accept(subprotocol="binary")

    ssl_ctx = ssl.create_default_context()
    if not cfg.get("proxmox_validate_certs", False):
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    from urllib.parse import quote
    upstream_url = (
        f"wss://{host}:{pve_port}/api2/json/nodes/{quote(str(node), safe='')}/qemu/{vmid}/vncwebsocket"
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
async def vm_start(vmid: int, request: Request):
    try:
        node = _resolve_vm_node(vmid)
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/start")
    except Exception as e:
        return _action_error_response(request, message=f"Start failed: {e}")
    return _action_response(request, payload={"ok": True, "vmid": vmid, "action": "start"})


@app.post("/api/vms/{vmid}/shutdown")
async def vm_shutdown(vmid: int, request: Request):
    try:
        node = _resolve_vm_node(vmid)
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/shutdown")
    except Exception as e:
        return _action_error_response(request, message=f"Shutdown failed: {e}")
    return _action_response(request, payload={"ok": True, "vmid": vmid, "action": "shutdown"})


@app.post("/api/vms/{vmid}/stop")
async def vm_stop(vmid: int, request: Request):
    try:
        node = _resolve_vm_node(vmid)
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/stop")
    except Exception as e:
        return _action_error_response(request, message=f"Force stop failed: {e}")
    return _action_response(request, payload={"ok": True, "vmid": vmid, "action": "stop"})


@app.post("/api/vms/{vmid}/reset")
async def vm_reset(vmid: int, request: Request):
    try:
        node = _resolve_vm_node(vmid)
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/reset")
    except Exception as e:
        return _action_error_response(request, message=f"Reset failed: {e}")
    return _action_response(request, payload={"ok": True, "vmid": vmid, "action": "reset"})


@app.post("/api/vms/{vmid}/delete")
async def vm_delete(vmid: int, request: Request):
    import time
    try:
        node = _resolve_vm_node(vmid)
        # Stop VM first if running
        try:
            _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/stop")
            time.sleep(3)
        except Exception:
            pass  # Already stopped or doesn't matter
        _proxmox_api_delete(f"/nodes/{node}/qemu/{vmid}")
    except Exception as e:
        return _action_error_response(request, message=f"Delete failed: {e}")
    return _action_response(request, payload={"ok": True, "vmid": vmid, "action": "delete"})


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
    errors = []
    try:
        node = _resolve_vm_node(vmid)
    except Exception as e:
        return _redirect_with_error("/vms", f"Type failed: {e}")
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
    try:
        node = _resolve_vm_node(vmid)
        _proxmox_api_put(f"/nodes/{node}/qemu/{vmid}/sendkey", data={"key": key})
    except Exception as e:
        return _redirect_with_error("/vms", f"Sendkey failed: {e}")
    return RedirectResponse("/vms", status_code=303)


# ---- JSON endpoints for the embedded console page (no redirects) ----

@app.get("/api/vms/{vmid}/status-json")
async def vm_status_json(vmid: int):
    """Current VM status as JSON, for the console page to poll."""
    try:
        node = _resolve_vm_node(vmid)
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
    try:
        node = _resolve_vm_node(vmid)
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/{action}")
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True, "action": action}


@app.post("/api/vms/{vmid}/type")
async def vm_type_json(vmid: int, request: Request):
    """Type text via QMP sendkey. Returns JSON."""
    import time
    payload = await _request_values(request)
    text = _request_text(payload, "text")
    press_enter = _request_text(payload, "press_enter")
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
    try:
        node = _resolve_vm_node(vmid)
    except Exception as e:
        return {"ok": False, "sent": sent, "error": str(e)}
    for key in keys:
        try:
            _proxmox_api_put(f"/nodes/{node}/qemu/{vmid}/sendkey", data={"key": key})
            sent += 1
        except Exception as e:
            return {"ok": False, "sent": sent, "error": str(e)}
        time.sleep(0.05)
    return {"ok": True, "sent": sent, "skipped": skipped}


@app.post("/api/vms/{vmid}/key")
async def vm_key_json(vmid: int, request: Request):
    """Single QEMU keyname (e.g. 'ctrl-alt-delete'). Returns JSON."""
    payload = await _request_values(request)
    key = _request_text(payload, "key")
    if not key:
        return JSONResponse({"ok": False, "error": "key is required"}, status_code=400)
    try:
        node = _resolve_vm_node(vmid)
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
    try:
        node = _resolve_vm_node(vmid)
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
async def vm_rename(vmid: int, request: Request):
    """Rename the Windows computer inside the VM.

    ``new_name`` is the operator's final choice (maybe with a prefix,
    maybe custom); if omitted we fall back to the computed suggestion
    so the button can still be clicked from scripts without a form.
    Updates the Proxmox VM name to match so /vms, /devices, and
    monitoring stay in sync.
    """
    payload = await _request_values(request)
    try:
        node = _resolve_vm_node(vmid)
        target = _request_text(payload, "new_name").strip()
        if not target:
            target = _suggested_rename_for_vm(vmid, node)
        hostname = _sanitize_windows_hostname(target)
        if not hostname:
            return _action_error_response(
                request,
                message=f"Rename target '{target}' produced an empty hostname "
                "after sanitisation (Windows allows A-Z, 0-9, '-' only, "
                "max 15 chars).",
                status_code=400,
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
            warning = (
                f"Renamed VM {vmid} to {hostname} (PVE only; guest "
                "agent unreachable)"
            )
            if _request_wants_json(request):
                return JSONResponse({
                    "ok": True,
                    "vmid": vmid,
                    "action": "rename",
                    "hostname": hostname,
                    "warning": warning,
                })
            return _redirect_with_error(
                "/vms",
                f"Renamed VM {vmid} to {hostname} (PVE only — guest "
                "agent unreachable, run Rename-Computer manually inside "
                "Windows or reboot to pick up the new name).",
            )
    except Exception as e:
        return _action_error_response(request, message=f"Rename failed: {e}")
    message = f"Renamed VM {vmid} to {hostname}; restart required"
    if _request_wants_json(request):
        return JSONResponse({
            "ok": True,
            "vmid": vmid,
            "action": "rename",
            "hostname": hostname,
            "message": message,
        })
    return _redirect_with_error("/vms", f"Renamed VM {vmid} to {hostname} — restart required to apply")


def _latest_capture_agent(vmid: int) -> dict:
    from web import db_pg

    with db_pg.connection(_database_url()) as conn:
        agent_telemetry_pg.init(conn)
        latest = agent_telemetry_pg.latest_by_vmid(conn).get(int(vmid))
    if not latest:
        raise ValueError(
            f"No live AutopilotAgent heartbeat found for VMID {vmid}; "
            "install or repair AutopilotAgent before capturing the hardware hash."
        )
    if not latest.get("agent_id"):
        raise ValueError(
            f"AutopilotAgent heartbeat for VMID {vmid} is missing agent identity."
        )
    version = str(latest.get("agent_version") or "").strip()
    if not _agent_supports_work_queue(version):
        raise ValueError(
            f"AutopilotAgent on VMID {vmid} is version {version or 'unknown'}; "
            "upgrade the MSI to 0.1.1 or newer before agent-driven hash capture."
        )
    return latest


def _agent_supports_work_queue(version: str) -> bool:
    parts = [int(match) for match in re.findall(r"\d+", str(version))]
    if not parts:
        return False
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3]) >= (0, 1, 1)


def _agent_capabilities_from_latest(latest: dict | None) -> set[str]:
    raw = latest.get("raw_json") if isinstance(latest, dict) else {}
    capabilities = raw.get("capabilities") if isinstance(raw, dict) else []
    if not isinstance(capabilities, list):
        return set()
    return {str(item).strip() for item in capabilities if str(item).strip()}


def _latest_agent_for_capability(vmid: int, capability: str) -> dict:
    from web import db_pg

    with db_pg.connection(_database_url()) as conn:
        agent_telemetry_pg.init(conn)
        latest = agent_telemetry_pg.latest_by_vmid(conn).get(int(vmid))
    if not latest:
        raise ValueError(
            f"No live AutopilotAgent heartbeat found for VMID {vmid}; "
            f"install or repair AutopilotAgent before running {capability}."
        )
    if not latest.get("agent_id"):
        raise ValueError(
            f"AutopilotAgent heartbeat for VMID {vmid} is missing agent identity."
        )
    version = str(latest.get("agent_version") or "").strip()
    if not _agent_supports_work_queue(version):
        raise ValueError(
            f"AutopilotAgent on VMID {vmid} is version {version or 'unknown'}; "
            f"upgrade the MSI to 0.1.1 or newer before running {capability}."
        )
    capabilities = _agent_capabilities_from_latest(latest)
    if capability not in capabilities:
        raise ValueError(
            f"AutopilotAgent on VMID {vmid} has not advertised {capability}; "
            "upgrade/restart the MSI so the new capability appears in heartbeat."
        )
    return latest


def _start_agent_hash_capture_job(
    *,
    vmid: int,
    vm_name: str,
    group_tag: str = "",
) -> dict:
    from web import db_pg

    agent = _latest_capture_agent(vmid)
    request = {
        "vmid": int(vmid),
        "vm_name": vm_name,
        "group_tag": group_tag,
        "hash_script_url": "/api/agent/v1/hash-script",
        "hash_upload_url": "/api/agent/v1/hash",
    }
    with db_pg.connection(_database_url()) as conn:
        agent_telemetry_pg.init(conn)
        work = agent_telemetry_pg.create_work_item(
            conn,
            agent_id=agent["agent_id"],
            kind="capture_autopilot_hash",
            request=request,
            vmid=int(vmid),
        )
    wait_script = BASE_DIR / "scripts" / "wait_agent_work_item.py"
    cmd = [
        "python",
        str(wait_script),
        "--work-item",
        work["id"],
        "--timeout",
        "1800",
    ]
    args = {
        "vmid": int(vmid),
        "vm_name": vm_name,
        "group_tag": group_tag,
        "agent_id": agent["agent_id"],
        "work_item_id": work["id"],
        "capture_transport": "autopilot_agent",
    }
    job = job_manager.start("hash_capture", cmd, args=args)
    with db_pg.connection(_database_url()) as conn:
        agent_telemetry_pg.init(conn)
        agent_telemetry_pg.attach_work_item_job(conn, work["id"], job_id=job["id"])
    return job


def _start_agent_log_collection_job(
    *,
    vmid: int,
    vm_name: str,
) -> dict:
    from web import db_pg

    agent = _latest_agent_for_capability(vmid, "collect_logs")
    request = {
        "vmid": int(vmid),
        "vm_name": vm_name,
        "known_sources": "cmtraceopen-windows",
        "artifact_upload_url": "/api/agent/v1/artifacts",
    }
    with db_pg.connection(_database_url()) as conn:
        agent_telemetry_pg.init(conn)
        work = agent_telemetry_pg.create_work_item(
            conn,
            agent_id=agent["agent_id"],
            kind="collect_logs",
            request=request,
            vmid=int(vmid),
        )
    wait_script = BASE_DIR / "scripts" / "wait_agent_work_item.py"
    cmd = [
        "python",
        str(wait_script),
        "--work-item",
        work["id"],
        "--timeout",
        "1800",
    ]
    args = {
        "vmid": int(vmid),
        "vm_name": vm_name,
        "agent_id": agent["agent_id"],
        "work_item_id": work["id"],
        "collection_transport": "autopilot_agent",
    }
    job = job_manager.start("log_collection", cmd, args=args)
    with db_pg.connection(_database_url()) as conn:
        agent_telemetry_pg.init(conn)
        agent_telemetry_pg.attach_work_item_job(conn, work["id"], job_id=job["id"])
    return job


@app.post("/api/jobs/capture-and-upload")
async def start_capture_and_upload(
    request: Request,
):
    """Capture hashes for selected VMs in parallel, then upload all to Intune."""
    payload = await _request_values(request)
    missing_vmids = _request_list(payload, "missing_vmids")
    if not missing_vmids:
        missing_vmids = _request_list(payload, "vmids")
    group_tag = _request_text(payload, "group_tag")
    if group_tag:
        group_tag = _sanitize_input(group_tag)

    vm_list = []
    for entry in missing_vmids:
        vmid, name = entry.split(":", 1)
        _sanitize_input(vmid)
        _sanitize_input(name)
        vm_list.append({"vmid": vmid, "name": name})

    try:
        capture_jobs = [
            _start_agent_hash_capture_job(
                vmid=int(vm["vmid"]),
                vm_name=vm["name"],
                group_tag=group_tag,
            )
            for vm in vm_list
        ]
    except ValueError as exc:
        return _action_error_response(request, message=str(exc), status_code=400)

    import tempfile
    work_ids = [job["args"]["work_item_id"] for job in capture_jobs]
    wait_script = BASE_DIR / "scripts" / "wait_agent_work_item.py"
    upload_results_script = BASE_DIR / "scripts" / "upload_agent_hash_results.py"
    work_id_args = " ".join(f"--work-item {shlex.quote(str(work_id))}" for work_id in work_ids)
    upload_args = [
        f"--hash-dir {shlex.quote(str(HASH_DIR))}",
        f"--playbook {shlex.quote(str(PLAYBOOK_DIR / 'upload_hashes.yml'))}",
        work_id_args,
    ]
    if group_tag:
        upload_args.append(f"--group-tag {shlex.quote(group_tag)}")
    script_lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        f"echo 'Waiting for {len(vm_list)} capture job(s) to finish...'",
        "python "
        + shlex.quote(str(wait_script))
        + " --timeout 1800 "
        + work_id_args,
        "echo '=== All captures done, uploading captured hashes to Intune ==='",
        "python "
        + shlex.quote(str(upload_results_script))
        + " "
        + " ".join(upload_args),
    ]
    fd, script_file = tempfile.mkstemp(suffix=".sh", dir=str(BASE_DIR / "jobs"))
    os.close(fd)
    script_path = Path(script_file)
    script_path.write_text("\n".join(script_lines))
    script_path.chmod(0o755)

    upload_job = job_manager.start(
        "upload_after_capture",
        ["bash", str(script_path)],
        args={"vms": [v["name"] for v in vm_list], "work_item_ids": work_ids, "group_tag": group_tag, "upload": True},
    )

    return _action_response(
        request,
        payload={
            "ok": True,
            "action": "capture-and-upload",
            "capture_job_ids": [job["id"] for job in capture_jobs],
            "upload_job_id": upload_job["id"],
        },
        redirect="/jobs",
    )


@app.post("/api/jobs/bulk-capture")
async def start_bulk_capture(
    request: Request,
):
    """Capture hashes for multiple VMs in parallel (one job per VM)."""
    payload = await _request_values(request)
    vmids = _request_list(payload, "vmids")
    group_tag = _request_text(payload, "group_tag")
    if group_tag:
        group_tag = _sanitize_input(group_tag)

    try:
        jobs = []
        for entry in vmids:
            vmid, name = entry.split(":", 1)
            _sanitize_input(vmid)
            _sanitize_input(name)
            jobs.append(_start_agent_hash_capture_job(
                vmid=int(vmid),
                vm_name=name,
                group_tag=group_tag,
            ))
    except ValueError as exc:
        return _action_error_response(request, message=str(exc), status_code=400)

    return _action_response(
        request,
        payload={"ok": True, "action": "bulk-capture", "job_ids": [job["id"] for job in jobs]},
        redirect="/jobs",
    )


@app.post("/api/jobs/capture")
async def start_capture(
    request: Request,
):
    payload = await _request_values(request)
    vmid_raw = _request_text(payload, "vmid")
    try:
        vmid = int(vmid_raw)
    except (TypeError, ValueError):
        return _action_error_response(request, message="vmid is required", status_code=400)
    vm_name = _request_text(payload, "vm_name")
    group_tag = _request_text(payload, "group_tag")
    name = _sanitize_input(vm_name) if vm_name else f"autopilot-{vmid}"
    if group_tag:
        group_tag = _sanitize_input(group_tag)
    try:
        job = _start_agent_hash_capture_job(
            vmid=int(vmid),
            vm_name=name,
            group_tag=group_tag,
        )
    except ValueError as exc:
        return _action_error_response(request, message=str(exc), status_code=400)
    return _action_response(
        request,
        payload={"ok": True, "action": "capture", "job_id": job["id"], "redirect": f"/jobs/{job['id']}"},
        redirect=f"/jobs/{job['id']}",
    )


@app.post(
    "/api/jobs/collect-logs",
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["vmid"],
                        "properties": {
                            "vmid": {"type": "integer"},
                            "vm_name": {"type": "string"},
                        },
                    },
                },
                "application/x-www-form-urlencoded": {
                    "schema": {
                        "type": "object",
                        "required": ["vmid"],
                        "properties": {
                            "vmid": {"type": "integer"},
                            "vm_name": {"type": "string"},
                        },
                    },
                },
            },
            "required": True,
        },
        "responses": {
            "200": {
                "description": "Log collection job queued",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["ok", "action", "job_id", "work_item_id", "vmid", "job_type", "status_url", "web_url"],
                            "properties": {
                                "ok": {"type": "boolean"},
                                "action": {"type": "string"},
                                "job_id": {"type": "string"},
                                "work_item_id": {"type": "string"},
                                "vmid": {"type": "integer"},
                                "job_type": {"type": "string"},
                                "status_url": {"type": "string"},
                                "web_url": {"type": "string"},
                            },
                        },
                    },
                },
            },
            "400": {"description": "Invalid request or no capable AutopilotAgent is available"},
        },
    },
)
async def start_collect_logs(request: Request):
    payload = await _request_values(request)
    vmid_raw = _request_text(payload, "vmid")
    try:
        vmid = int(vmid_raw)
    except (TypeError, ValueError):
        return _action_error_response(request, message="vmid is required", status_code=400)
    vm_name = _request_text(payload, "vm_name")
    name = _sanitize_input(vm_name) if vm_name else f"vm-{vmid}"
    try:
        job = _start_agent_log_collection_job(
            vmid=int(vmid),
            vm_name=name,
        )
    except ValueError as exc:
        return _action_error_response(request, message=str(exc), status_code=400)
    work_item_id = str(job.get("args", {}).get("work_item_id", ""))
    job_id = str(job["id"])
    return _action_response(
        request,
        payload={
            "ok": True,
            "action": "collect-logs",
            "job_id": job_id,
            "work_item_id": work_item_id,
            "vmid": int(vmid),
            "job_type": "log_collection",
            "status_url": f"/api/jobs/{job_id}",
            "web_url": f"/react/jobs/{job_id}",
        },
        redirect=f"/jobs/{job_id}",
    )


@app.post("/api/jobs/upload")
async def start_upload(
    files: list[str] = Form(...),
    group_tags: list[str] = Form(...),
):
    """Upload selected hash files to Intune with per-file group tags."""
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
        cmd += ["-e", f"hash_file={file_path}"]

        job_manager.start("upload_hash", cmd, args={"file": filename, "group_tag": tag})

    return RedirectResponse("/hashes?uploaded=1", status_code=303)


@app.get("/api/jobs", response_model=list[JobTableRowResponse])
async def api_list_jobs():
    return _job_table_rows()


@app.post("/api/jobs/{job_id}/kill")
async def kill_job(job_id: str):
    """Request termination. Flips kill_requested=1 on the job row; the
    builder owning the job will see it on its next heartbeat cycle
    (~5s max) and SIGTERM the subprocess. Redirects to /jobs/<id>."""
    row = jobs_db.get_job(job_id)
    if row is None:
        raise HTTPException(404, f"job {job_id} not found")
    if row["status"] != "running":
        # Already done; ignore quietly so double-clicks don't 400.
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)
    jobs_db.request_kill(job_id)
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


@app.get("/api/jobs/recent", response_model=RecentJobsResponse)
async def api_recent_jobs(limit: int = 5):
    """Return the N most recently-started jobs, newest first.

    Used by the home-page dashboard. Keeps the payload small —
    only the fields the dashboard renders, plus a precomputed
    duration string so the client doesn't need to know about
    started/ended ISO parsing.
    """
    return _recent_jobs_payload(limit=limit)


@app.get("/api/jobs/running", response_model=RunningJobsResponse)
async def api_running_jobs():
    """Return currently-running jobs with elapsed seconds + a
    rough progress estimate (0-99).

    Used by the home-page Live-now module. Progress uses a
    per-playbook expected-seconds table; if the job type isn't
    known, we fall back to 50% so the bar still moves.
    """
    return _running_jobs_payload()


@app.get("/api/services", response_model=ServicesResponse)
async def api_services():
    """Read per-service heartbeats from the PostgreSQL service_health table."""
    from web import service_health_pg

    try:
        return {"services": service_health_pg.list_services(), "available": True, "error": ""}
    except Exception as exc:
        return {"services": [], "available": False, "error": str(exc)}


def _docker_client():
    try:
        import docker as _docker
    except Exception as exc:
        raise RuntimeError("docker-py is not installed in the image") from exc
    try:
        return _docker.from_env()
    except Exception as exc:
        raise RuntimeError(f"cannot reach Docker socket: {exc}") from exc


def _runtime_container_service(container) -> str:
    labels = getattr(container, "labels", {}) or {}
    service = labels.get("com.docker.compose.service") or ""
    if service:
        return service
    name = str(getattr(container, "name", "") or "").lstrip("/")
    if name.startswith("autopilot-proxmox-autopilot-builder-"):
        return "autopilot-builder"
    return name


def _runtime_container_allowed(container) -> bool:
    name = str(getattr(container, "name", "") or "").lstrip("/")
    service = _runtime_container_service(container)
    return service in _RUNTIME_LOG_SERVICES or name.startswith(_RUNTIME_LOG_NAME_PREFIXES)


def _container_health_status(container) -> str:
    try:
        state = (container.attrs or {}).get("State") or {}
        health = state.get("Health") or {}
        return str(health.get("Status") or "")
    except Exception:
        return ""


def _runtime_container_status() -> dict:
    try:
        client = _docker_client()
        containers = client.containers.list(all=True)
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "containers": [],
        }
    rows = []
    for container in containers:
        if not _runtime_container_allowed(container):
            continue
        attrs = container.attrs or {}
        state = attrs.get("State") or {}
        config = attrs.get("Config") or {}
        image = config.get("Image") or ""
        name = str(getattr(container, "name", "") or "").lstrip("/")
        rows.append({
            "id": str(getattr(container, "short_id", "") or ""),
            "name": name,
            "service": _runtime_container_service(container),
            "image": image,
            "status": str(getattr(container, "status", "") or state.get("Status") or "unknown"),
            "health": _container_health_status(container),
            "started_at": state.get("StartedAt") or "",
            "finished_at": state.get("FinishedAt") or "",
            "restart_count": attrs.get("RestartCount") or 0,
            "log_url": f"/api/monitoring/service-logs?container={quote_plus(name)}",
        })
    rows.sort(key=lambda row: (row["service"], row["name"]))
    return {"available": True, "error": "", "containers": rows}


def _redact_log_line(line: str) -> str:
    return _LOG_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", line)


@app.get("/api/monitoring/runtime-services", response_model=RuntimeServicesResponse)
async def api_monitoring_runtime_services():
    return _runtime_container_status()


@app.get("/api/monitoring/service-logs")
async def api_monitoring_service_logs(request: Request):
    container_name = (request.query_params.get("container") or "").strip()
    if not container_name:
        raise HTTPException(400, "container is required")
    tail_raw = request.query_params.get("tail") or "160"
    try:
        tail = max(20, min(int(tail_raw), 500))
    except ValueError:
        raise HTTPException(400, "tail must be an integer")
    try:
        client = _docker_client()
        container = client.containers.get(container_name)
    except Exception as exc:
        raise HTTPException(404, f"container not found or Docker unavailable: {exc}")
    if not _runtime_container_allowed(container):
        raise HTTPException(403, "container logs are not exposed")
    try:
        raw = container.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
    except Exception as exc:
        raise HTTPException(500, f"failed to read container logs: {exc}")
    lines = [_redact_log_line(line) for line in raw.splitlines()]
    return {
        "container": container.name,
        "service": _runtime_container_service(container),
        "tail": tail,
        "lines": lines,
    }


@app.get("/api/fleet/summary", response_model=FleetSummaryResponse)
async def api_fleet_summary():
    """Fleet enrollment counts + percentages from the most-recent
    sweep's ``device_probes`` rows.

    Only columns that actually exist in ``device_history_db`` are
    reported. MDE has no column today, so the ``mde_pct`` key is
    intentionally omitted rather than fabricated — the dashboard
    will simply render "—" for that cell.
    """
    try:
        return device_history_db.fleet_summary()
    except Exception:
        return {"total": 0}


@app.get("/api/cockpit/summary", response_model=CockpitSummaryResponse)
async def api_cockpit_summary():
    """Aggregate the existing dashboard data sources for the cockpit UI.

    This is intentionally a read-only composition endpoint: it reuses the
    jobs manager, service-health table, and monitoring DB instead of adding
    UI-specific persistence.
    """
    running = await api_running_jobs()
    recent = await api_recent_jobs(limit=6)
    services = await api_services()
    fleet = await api_fleet_summary()

    monitoring = {"devices": 0, "ad": 0, "entra": 0, "intune": 0}
    try:
        latest = device_history_db.latest_per_vmid()
        monitoring["devices"] = len(latest)
        for row in latest:
            probe = row.get("probe") or {}
            if probe.get("ad_found"):
                monitoring["ad"] += 1
            if probe.get("entra_found"):
                monitoring["entra"] += 1
            if probe.get("intune_found"):
                monitoring["intune"] += 1
    except Exception:
        pass

    service_rows = services.get("services") if isinstance(services, dict) else []
    service_count = len(service_rows or [])
    stale_services = 0
    for svc in service_rows or []:
        age = svc.get("age_seconds")
        if age is not None and age > 120:
            stale_services += 1

    readiness_parts = []
    if fleet.get("total"):
        for key in ("ad_joined_pct", "autopilot_pct", "intune_pct"):
            if key in fleet:
                readiness_parts.append(int(fleet[key]))
    if service_count:
        readiness_parts.append(max(0, round(100 * (service_count - stale_services) / service_count)))
    if running.get("running_count", 0) == 0:
        readiness_parts.append(100)

    readiness = round(sum(readiness_parts) / len(readiness_parts)) if readiness_parts else 0
    return {
        "readiness_score": readiness,
        "jobs": running,
        "recent_jobs": recent.get("jobs", []),
        "services": services,
        "fleet": fleet,
        "monitoring": monitoring,
    }


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        return {"error": "not found"}
    job["log"] = job_manager.get_log(job_id)
    return job


@app.post("/api/autopilot/delete")
async def delete_autopilot_device(request: Request):
    payload = await _request_values(request)
    device_id = _request_text(payload, "device_id")
    if not device_id:
        return _action_error_response(request, message="device_id is required", status_code=400)
    try:
        _graph_api(f"/deviceManagement/windowsAutopilotDeviceIdentities/{device_id}", method="DELETE")
    except Exception as exc:
        if _request_wants_json(request):
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return _action_response(
        request,
        payload={"ok": True, "action": "delete-autopilot", "device_id": device_id},
    )


@app.post("/api/autopilot/sync")
async def sync_autopilot(request: Request):
    try:
        _graph_api("/deviceManagement/windowsAutopilotSettings/sync", method="POST")
    except Exception as exc:
        if _request_wants_json(request):
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return _action_response(
        request,
        payload={"ok": True, "action": "sync-autopilot"},
    )


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


@app.get("/files/{filename}")
async def download_file_shelf_item(filename: str):
    if Path(filename).suffix.lower() != ".msi":
        return HTMLResponse("<h1>File not found</h1>", status_code=404)
    safe_name = _safe_file_shelf_name(filename)
    if not safe_name or safe_name != filename:
        return HTMLResponse("<h1>Forbidden</h1>", status_code=403)
    try:
        file_path = _safe_path(FILE_SHELF_DIR, safe_name)
    except ValueError:
        return HTMLResponse("<h1>Forbidden</h1>", status_code=403)
    if not file_path.exists() or file_path.suffix.lower() != ".msi":
        return HTMLResponse("<h1>File not found</h1>", status_code=404)
    return FileResponse(file_path, media_type="application/octet-stream", filename=safe_name)


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


@app.post("/api/files/upload")
async def upload_file_shelf_items(files: list[UploadFile] = File(...)):
    FILE_SHELF_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    for upload in files:
        safe_name = _safe_file_shelf_name(upload.filename)
        if not safe_name:
            continue
        dest = FILE_SHELF_DIR / safe_name
        content = await upload.read()
        if not content:
            continue
        dest.write_bytes(content)
        saved += 1
    if saved == 0:
        return _redirect_with_error("/files", "No valid MSI files found")
    return RedirectResponse(f"/files?uploaded={saved}", status_code=303)


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


class _UbuntuV2TemplateBuildIn(BaseModel):
    sequence_id: str
    cloud_image: Optional[str] = None
    node: Optional[str] = None
    template_vmid: Optional[int] = None


class _UbuntuV2ProvisionIn(BaseModel):
    sequence_id: str
    count: int = Field(default=1, ge=1, le=20)
    vm_name_prefix: str = "ubuntu-v2"
    hostname_pattern: str = "ubuntu-{vmid}"
    group_tag: str = ""
    vm_cores: int = Field(default=2, ge=1)
    vm_memory_mb: int = Field(default=4096, ge=1024)
    vm_disk_size_gb: int = Field(default=80, ge=20)
    proxmox_node: Optional[str] = None
    proxmox_storage: Optional[str] = None
    proxmox_bridge: Optional[str] = None
    proxmox_template_vmid: Optional[int] = None
    oem_profile: str = ""
    chassis_type: str = ""


def _v2_ubuntu_sequence_steps(conn, sequence_id: str) -> list[dict]:
    from web import ts_engine_pg
    from web.osd_v2_catalog import validate_steps_for_target_os
    from web.ubuntu_v2 import v2_plan_steps_to_ubuntu_steps

    seq = ts_engine_pg.get_sequence(conn, sequence_id)
    if not seq:
        raise HTTPException(status_code=404, detail="v2 sequence not found")
    if (seq.get("target_os") or "windows") != "ubuntu":
        raise HTTPException(status_code=400, detail="v2 sequence target_os is not ubuntu")
    nodes = ts_engine_pg.list_sequence_nodes(conn, sequence_id)
    validate_steps_for_target_os("ubuntu", nodes)
    return v2_plan_steps_to_ubuntu_steps(nodes)


def _v2_ubuntu_credentials(steps: list[dict]) -> dict[int, dict]:
    cipher = _cipher()
    credentials: dict[int, dict] = {}
    for cid in _referenced_credential_ids(steps):
        row = sequences_db.get_credential(SEQUENCES_DB, cipher, cid)
        if row is not None:
            credentials[cid] = row.get("payload") or {}
    return credentials


def _append_cloud_init_runcmd(user_data: str, commands: list[str]) -> str:
    doc = yaml.safe_load(user_data) or {}
    runcmd = list(doc.get("runcmd") or [])
    runcmd.extend(commands)
    doc["runcmd"] = runcmd
    return "#cloud-config\n" + yaml.safe_dump(doc, sort_keys=False)


def _merge_template_runtime_cloud_init_into_firstboot(
    template_user_data: str,
    firstboot_user_data: str,
) -> str:
    """Move runtime cloud-init contributions into the per-VM seed.

    v2 Ubuntu provisioning clones an existing template, then attaches a per-VM
    NoCloud seed. The compiler still emits install/package steps into its
    template-style user-data document. For cloned v2 runs, those runtime steps
    must execute from the per-VM first-boot seed before the Linux agent verifies
    readiness.
    """
    template_doc = yaml.safe_load(template_user_data) or {}
    firstboot_doc = yaml.safe_load(firstboot_user_data) or {}

    for key in ("bootcmd", "write_files", "packages", "users", "runcmd"):
        values = template_doc.get(key)
        if values:
            firstboot_doc.setdefault(key, []).extend(list(values))

    for key in ("apt", "package_update", "package_upgrade", "snap"):
        if key in template_doc and key not in firstboot_doc:
            firstboot_doc[key] = template_doc[key]

    return "#cloud-config\n" + yaml.safe_dump(firstboot_doc, sort_keys=False)


def _is_loopback_base_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    return (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}


def _derive_guest_reachable_base_url(config: dict) -> str:
    """Derive a host URL that a guest VM can reach.

    Builder/controller calls can use 127.0.0.1 because the containers run on
    the host network. Cloud-init runs inside the guest, so a loopback URL would
    point back at the VM and strand the Linux agent bootstrap.
    """
    candidates = [
        os.environ.get("AUTOPILOT_BASE_URL"),
        (_load_vars() or {}).get("autopilot_base_url"),
        (_load_vars() or {}).get("guest_base_url"),
        config.get("autopilot_base_url"),
        config.get("guest_base_url"),
        (_load_vars() or {}).get("web_base_url"),
        config.get("web_base_url"),
    ]
    for candidate in candidates:
        if candidate and not _is_loopback_base_url(str(candidate)):
            return str(candidate).rstrip("/")

    port = 5000
    for candidate in candidates:
        if candidate:
            parsed = urlparse(str(candidate))
            if parsed.port:
                port = parsed.port
                break

    probe_host = config.get("proxmox_host") or "8.8.8.8"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((str(probe_host), 8006))
            host_ip = sock.getsockname()[0]
        if host_ip and not host_ip.startswith("127."):
            return f"http://{host_ip}:{port}"
    except Exception:
        pass

    return "http://127.0.0.1:5000"


def _linux_agent_bootstrap_commands(*, run_id: str, vmid: int, hostname: str) -> list[str]:
    from web import winpe_token

    base_url = _derive_guest_reachable_base_url(_load_proxmox_config())
    token = winpe_token.sign(run_id=run_id, ttl_seconds=24 * 60 * 60)
    config = {
        "server_url": base_url.rstrip("/"),
        "run_id": run_id,
        "agent_id": f"linux-{vmid}",
        "phase": "verify",
        "bearer_token": token,
        "vmid": vmid,
        "hostname": hostname,
    }
    config_b64 = base64.b64encode(json.dumps(config).encode("utf-8")).decode("ascii")
    unit = """[Unit]
Description=ProxmoxVEAutopilot Linux v2 Agent
After=network-online.target cloud-init.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/proxmoxveautopilot/autopilot_linux_agent.py --config /etc/proxmoxveautopilot/linux-agent.json
Restart=on-failure
RestartSec=20

[Install]
WantedBy=multi-user.target
"""
    unit_b64 = base64.b64encode(unit.encode("utf-8")).decode("ascii")
    return [
        "install -d -m 700 /etc/proxmoxveautopilot /opt/proxmoxveautopilot",
        f"python3 - <<'PY'\nimport base64, pathlib\npathlib.Path('/etc/proxmoxveautopilot/linux-agent.json').write_bytes(base64.b64decode('{config_b64}'))\nPY",
        f"curl -fsSL {shlex.quote(base_url.rstrip('/') + '/osd/v2/ubuntu/linux-agent.py')} -o /opt/proxmoxveautopilot/autopilot_linux_agent.py",
        "chmod 0755 /opt/proxmoxveautopilot/autopilot_linux_agent.py",
        f"python3 - <<'PY'\nimport base64, pathlib\npathlib.Path('/etc/systemd/system/autopilot-linux-agent.service').write_bytes(base64.b64decode('{unit_b64}'))\nPY",
        "systemctl daemon-reload",
        "systemctl enable --now autopilot-linux-agent.service",
    ]


def _upload_seed_iso_to_proxmox(*, user_data: str, meta_data: str, iso_name: str) -> dict:
    import tempfile as _tmp
    from web.ubuntu_seed_iso import build_seed_iso

    cfg = _load_proxmox_config()
    iso_storage = cfg.get("proxmox_iso_storage") or "isos"
    node = cfg.get("proxmox_node", "pve")
    with _tmp.TemporaryDirectory() as td:
        iso_path = Path(td) / iso_name
        build_seed_iso(user_data=user_data, meta_data=meta_data, out_path=iso_path)
        host = cfg.get("proxmox_host", "")
        port = cfg.get("proxmox_port", 8006)
        token_id = cfg.get("vault_proxmox_api_token_id", "")
        token_secret = cfg.get("vault_proxmox_api_token_secret", "")
        url = f"https://{host}:{port}/api2/json/nodes/{node}/storage/{iso_storage}/upload"
        with open(iso_path, "rb") as fh:
            resp = requests.post(
                url,
                headers={"Authorization": f"PVEAPIToken={token_id}={token_secret}"},
                data={"content": "iso"},
                files={"filename": (iso_name, fh, "application/x-iso9660-image")},
                verify=cfg.get("proxmox_validate_certs", False),
                timeout=60,
            )
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Proxmox ISO upload failed: HTTP {resp.status_code}: {resp.text[:500]}")
    return {"iso": f"{iso_storage}:iso/{iso_name}", "storage": iso_storage, "node": node}


def _complete_ubuntu_v2_steps(
    conn,
    *,
    run_id: str,
    agent_id: str,
    message: str,
    data: dict | None = None,
    kinds: set[str] | None = None,
    phases: set[str] | None = None,
) -> list[dict]:
    from web import ts_engine_pg

    completed: list[dict] = []
    for step in ts_engine_pg.list_run_steps(conn, run_id):
        if step.get("state") not in ("pending", "running"):
            continue
        if kinds is not None and step.get("kind") not in kinds:
            continue
        if phases is not None and step.get("phase") not in phases:
            continue
        completed.append(ts_engine_pg.complete_step(
            conn,
            run_id=run_id,
            step_id=step["id"],
            agent_id=agent_id,
            status="success",
            message=message,
            data=data or {},
        ))
    return completed


@app.get("/osd/v2/ubuntu/linux-agent.py", response_class=FileResponse)
@app.get("/api/osd/v2/ubuntu/linux-agent.py", response_class=FileResponse)
def ubuntu_v2_linux_agent_script():
    path = BASE_DIR / "files" / "linux-agent" / "autopilot_linux_agent.py"
    if not path.exists():
        raise HTTPException(status_code=404, detail="linux agent asset missing")
    return FileResponse(path, media_type="text/x-python")


@app.post("/api/osd/v2/ubuntu/template-builds", status_code=202)
def api_ubuntu_v2_template_build(body: _UbuntuV2TemplateBuildIn):
    from web import db_pg, ts_engine_pg
    from web.ubuntu_compiler import UbuntuCompileError, compile_sequence

    with db_pg.connection(_database_url()) as conn:
        steps = _v2_ubuntu_sequence_steps(conn, body.sequence_id)
        version_id = ts_engine_pg.compile_sequence(conn, body.sequence_id, compiled_by="ubuntu-v2")
    try:
        user_data, meta_data, _fb_user, _fb_meta = compile_sequence(
            steps=steps,
            credentials=_v2_ubuntu_credentials(steps),
            instance_id=f"v2-seq-{body.sequence_id}",
            hostname="ubuntu-template",
        )
    except UbuntuCompileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    upload = _upload_seed_iso_to_proxmox(
        user_data=user_data,
        meta_data=meta_data,
        iso_name=f"ubuntu-v2-seed-{body.sequence_id}.iso",
    )
    cmd = ["ansible-playbook", str(PLAYBOOK_DIR / "build_template.yml")]
    cmd += ["-e", "target_os=ubuntu", "-e", f"ubuntu_v2_sequence_id={body.sequence_id}"]
    cmd += ["-e", f"ubuntu_seed_iso={upload['iso']}"]
    if body.cloud_image:
        cmd += ["-e", f"ubuntu_cloud_image={body.cloud_image}"]
    if body.template_vmid:
        cmd += ["-e", f"proxmox_template_vmid={body.template_vmid}"]
    if body.node:
        cmd += ["-e", f"proxmox_node={body.node}"]
    job = job_manager.start(
        "build_template_ubuntu_v2",
        cmd,
        args={"target_os": "ubuntu", "sequence_id": body.sequence_id, "sequence_version_id": version_id, **upload},
    )
    return {"ok": True, "job_id": job["id"], "sequence_version_id": version_id, **upload}


@app.post("/api/osd/v2/ubuntu/per-vm-seed")
def api_ubuntu_v2_per_vm_seed(vmid: int, run_id: str, hostname: str):
    from web import db_pg, ts_engine_pg
    from web.ubuntu_compiler import UbuntuCompileError, compile_sequence
    from web.ubuntu_v2 import v2_plan_steps_to_ubuntu_steps

    with db_pg.connection(_database_url()) as conn:
        try:
            run = ts_engine_pg.get_run(conn, run_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="v2 run not found")
        if run.get("target_os") != "ubuntu":
            raise HTTPException(status_code=400, detail="v2 run target_os is not ubuntu")
        steps = v2_plan_steps_to_ubuntu_steps(ts_engine_pg.list_run_steps(conn, run_id))
    try:
        template_user_data, _m, firstboot_user_data, firstboot_meta_data = compile_sequence(
            steps=steps,
            credentials=_v2_ubuntu_credentials(steps),
            instance_id=f"v2-run-{run_id}-vm-{vmid}",
            hostname=hostname,
        )
    except UbuntuCompileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    firstboot_user_data = _merge_template_runtime_cloud_init_into_firstboot(
        template_user_data,
        firstboot_user_data,
    )
    firstboot_user_data = _append_cloud_init_runcmd(
        firstboot_user_data,
        _linux_agent_bootstrap_commands(run_id=run_id, vmid=vmid, hostname=hostname),
    )
    upload = _upload_seed_iso_to_proxmox(
        user_data=firstboot_user_data,
        meta_data=firstboot_meta_data,
        iso_name=f"ubuntu-v2-per-vm-{vmid}.iso",
    )
    return {"ok": True, "vmid": vmid, "run_id": run_id, **upload}


@app.post("/api/osd/v2/ubuntu/runs/{run_id}/identity")
async def api_ubuntu_v2_record_identity(run_id: str, request: Request):
    from web import db_pg, ts_engine_pg

    body = await request.json()
    with db_pg.connection(_database_url()) as conn:
        try:
            run = ts_engine_pg.update_run_identity(
                conn,
                run_id=run_id,
                vmid=body.get("vmid"),
                vm_uuid=body.get("vm_uuid"),
                computer_name=body.get("computer_name") or body.get("hostname"),
                serial_number=body.get("serial_number"),
                deployment_target_patch=body,
            )
        except ValueError:
            raise HTTPException(status_code=404, detail="v2 run not found")
        created_steps = _complete_ubuntu_v2_steps(
            conn,
            run_id=run_id,
            agent_id="controller",
            kinds={"proxmox_clone_vm"},
            message="Ubuntu v2 VM created and identity recorded",
            data=body,
        )
    return {"ok": True, "run": run, "completed_steps": len(created_steps)}


@app.post("/api/osd/v2/ubuntu/runs/{run_id}/cloud-init-complete")
async def api_ubuntu_v2_cloud_init_complete(run_id: str, request: Request):
    from web import db_pg, ts_engine_pg
    from web.osd_v2_catalog import UBUNTU_COMPILE_STEP_KINDS

    body = await request.json()
    with db_pg.connection(_database_url()) as conn:
        try:
            run = ts_engine_pg.get_run(conn, run_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="v2 run not found")
        if run.get("target_os") != "ubuntu":
            raise HTTPException(status_code=400, detail="v2 run target_os is not ubuntu")
        completed_steps = _complete_ubuntu_v2_steps(
            conn,
            run_id=run_id,
            agent_id="controller",
            kinds=set(UBUNTU_COMPILE_STEP_KINDS),
            phases={"install", "first_boot"},
            message="Ubuntu cloud-init completed for compile-time and first-boot steps",
            data=body,
        )
    return {"ok": True, "run_id": run_id, "completed_steps": len(completed_steps)}


@app.post("/api/osd/v2/ubuntu/provision", status_code=202)
def api_ubuntu_v2_provision(body: _UbuntuV2ProvisionIn):
    from web import db_pg, ts_engine_pg

    with db_pg.connection(_database_url()) as conn:
        _v2_ubuntu_sequence_steps(conn, body.sequence_id)
        version_id = ts_engine_pg.compile_sequence(conn, body.sequence_id, compiled_by="ubuntu-v2")
        run_ids = []
        for index in range(1, body.count + 1):
            deployment_target = {
                "target_os": "ubuntu",
                "hostname_pattern": body.hostname_pattern,
                "group_tag": body.group_tag,
                "index": index,
            }
            run_id = ts_engine_pg.create_run_from_version(
                conn,
                sequence_version_id=version_id,
                deployment_target=deployment_target,
                run_variables={
                    "target_os": "ubuntu",
                    "group_tag": body.group_tag,
                    "source_surface": "provision",
                    "index": index,
                },
                created_by="ubuntu-v2-provision",
            )
            run_ids.append(run_id)

    jobs = []
    cfg = _load_proxmox_config()
    for index, run_id in enumerate(run_ids, start=1):
        cmd = ["ansible-playbook", str(PLAYBOOK_DIR / "provision_clone.yml")]
        extra = {
            "target_os": "ubuntu",
            "hypervisor_type": "proxmox",
            "vm_count": 1,
            "vm_name_prefix": body.vm_name_prefix,
            "hostname_pattern": body.hostname_pattern,
            "ubuntu_v2_run_id": run_id,
            "vm_cores": body.vm_cores,
            "vm_memory_mb": body.vm_memory_mb,
            "vm_disk_size_gb": body.vm_disk_size_gb,
            "proxmox_node": body.proxmox_node or cfg.get("proxmox_node", "pve"),
            "proxmox_storage": body.proxmox_storage or cfg.get("proxmox_storage", "local-lvm"),
            "proxmox_bridge": body.proxmox_bridge or cfg.get("proxmox_bridge", "vmbr0"),
            "group_tag": body.group_tag,
            "vm_oem_profile": body.oem_profile,
            "chassis_type_override": body.chassis_type,
        }
        if body.proxmox_template_vmid:
            extra["proxmox_template_vmid"] = body.proxmox_template_vmid
        for key, value in extra.items():
            if value is not None and value != "":
                cmd += ["-e", f"{key}={value}"]
        job = job_manager.start(
            "provision_ubuntu_v2",
            cmd,
            args={"target_os": "ubuntu", "run_id": run_id, "sequence_id": body.sequence_id, "index": index, **extra},
        )
        jobs.append({"job_id": job["id"], "run_id": run_id, "index": index})
    return {"ok": True, "sequence_version_id": version_id, "runs": jobs}


# --- Version / update check ------------------------------------------------

_GITHUB_REPO = "adamgell/ProxmoxVEAutopilot"
_LATEST_VERSION_TTL = 300  # seconds — cache GitHub response for 5 min


def _same_git_sha(left: str | None, right: str | None) -> bool:
    left = (left or "").strip().lower()
    right = (right or "").strip().lower()
    if not left or not right or "unknown" in {left, right}:
        return False
    if left == right:
        return True
    # Some production builds bake a short SHA while GitHub returns the full
    # commit. Treat a 7+ character prefix match as the same build.
    if len(left) >= 7 and right.startswith(left):
        return True
    if len(right) >= 7 and left.startswith(right):
        return True
    return False


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


def _build_update_sidecar_command(host_repo: str) -> str:
    host_repo_q = shlex.quote(host_repo)
    image_tag_q = shlex.quote("ghcr.io/adamgell/proxmox-autopilot:latest")
    return (
        "set -euo pipefail; "
        "apk add --no-cache git >/dev/null 2>&1 || apk add --no-cache git; "
        f"cd {host_repo_q} && "
        "echo '--- git pull ---' && git pull && "
        "cd autopilot-proxmox && "
        "GIT_SHA=\"$(git rev-parse HEAD 2>/dev/null || echo unknown)\" && "
        "BUILD_TIME=\"$(date -u +\"%Y-%m-%dT%H:%M:%SZ\")\" && "
        "echo \"--- docker build ${GIT_SHA} ---\" && "
        "DOCKER_BUILDKIT=1 docker build "
        "--security-opt apparmor=unconfined "
        f"-t {image_tag_q} "
        "--build-arg \"GIT_SHA=${GIT_SHA}\" "
        "--build-arg \"BUILD_TIME=${BUILD_TIME}\" "
        ". && "
        "echo '--- docker compose pull support images ---' && "
        "(docker compose pull autopilot-postgres || true) && "
        "echo '--- docker compose up -d ---' && "
        "services='autopilot autopilot-builder autopilot-monitor'; "
        "if docker compose ps -q autopilot-mcp >/dev/null 2>&1 && "
        "[ -n \"$(docker compose ps -q autopilot-mcp)\" ]; then "
        "services=\"$services autopilot-mcp\"; "
        "fi; "
        "docker compose up -d --force-recreate $services && "
        "echo '--- done ---'"
    )


@app.post("/api/update/run")
async def api_update_run():
    """Start a self-update. Spawns a detached sidecar container that
    runs `git pull`, locally rebuilds the app image with build metadata,
    and recreates web/builder/monitor containers
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
    # plugin). `apk add git` adds git (~5MB) on the fly. It builds
    # the app image locally so /app/VERSION is always stamped even
    # when GHCR `latest` is stale or a compose-only update path runs.
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
    cmd = _build_update_sidecar_command(host_repo)
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
            out["update_available"] = not _same_git_sha(running_sha, latest_sha)
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
            source="pve", object_id=str(vmid),
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
                source="pve", object_id=str(target_vmid),
                serial=item.get("serial", ""), display_name=item.get("display_name", ""),
                status="ok",
            )
            break
        if asyncio.get_event_loop().time() >= deadline:
            item["state"] = "unverified"
            item["message"] = f"VM still present in node listing after {attempts} attempt(s)"
            devices_db.record_deletion(
                source="pve", object_id=str(target_vmid),
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
            source=item["source"], object_id=item["object_id"],
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
            source=item["source"], object_id=item["object_id"],
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
            source=item["source"], object_id=item["object_id"],
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
            source=item["source"], object_id=item["object_id"],
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
    groups, extra = devices_db.list_grouped(windows_only=windows_only)

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
            "deletions": devices_db.list_deletions(limit=25),
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
    devices_db.upsert_autopilot(ap)
    devices_db.upsert_intune(it)
    devices_db.upsert_entra(en)
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
    except psycopg.IntegrityError as e:
        if _is_unique_violation(e):
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
    except psycopg.IntegrityError as e:
        if _is_unique_violation(e):
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
    hash_capture_phase: str = "oobe"
    steps: list[_StepIn] = []


class _SequenceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    target_os: Optional[str] = None
    is_default: Optional[bool] = None
    produces_autopilot_hash: Optional[bool] = None
    hash_capture_phase: Optional[str] = None
    steps: Optional[list[_StepIn]] = None


class _DuplicateReq(BaseModel):
    new_name: str


class _V2BuilderNodeIn(BaseModel):
    id: Optional[str] = None
    client_id: Optional[str] = None
    parent_id: Optional[str] = None
    node_type: str = "step"
    name: str
    description: str = ""
    kind: Optional[str] = None
    phase: str = "full_os"
    enabled: bool = True
    condition: dict = Field(default_factory=dict)
    variables: dict = Field(default_factory=dict)
    params: dict = Field(default_factory=dict)
    content_refs: list[str] = Field(default_factory=list)
    continue_on_error: bool = False
    retry_count: int = 0
    retry_delay_seconds: int = 10
    timeout_seconds: Optional[int] = None
    reboot_behavior: str = "none"


class _V2BuilderSequenceIn(BaseModel):
    name: str
    description: str = ""
    target_os: str = "windows"
    enabled: bool = True
    nodes: list[_V2BuilderNodeIn] = Field(default_factory=list)


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
            hash_capture_phase=body.hash_capture_phase,
        )
    except psycopg.IntegrityError as e:
        if _is_unique_violation(e):
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
            hash_capture_phase=body.hash_capture_phase,
        )
    except psycopg.IntegrityError as e:
        if _is_unique_violation(e):
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
    except psycopg.IntegrityError as e:
        if _is_unique_violation(e):
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


def _make_root_ssh_runner(host: str | None = None):
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
        host=host or cfg.get("proxmox_host", ""), password=pw, user=user or "root",
    )


def _winpe_actions_for_sequence(seq: dict | None) -> list[dict]:
    if not seq:
        return []
    if (seq.get("target_os") or "windows") != "windows":
        return []
    try:
        phase = sequence_compiler.compile_winpe(seq)
    except sequence_compiler.CompilerError:
        return []
    return phase.actions


def _sequence_rows_for_ui(seqs: list[dict]) -> list[dict]:
    rows = []
    for seq in seqs:
        actions = _winpe_actions_for_sequence(seq)
        rows.append({
            **seq,
            "winpe_actions": actions,
            "winpe_action_kinds": [a["kind"] for a in actions],
        })
    return rows


def _run_steps_for_display(run: dict, seq: dict | None) -> list[dict]:
    steps = sequences_db.list_run_steps(SEQUENCES_DB, run_id=run["id"])
    if steps or run.get("provision_path") != "winpe":
        return steps
    return [
        {
            "id": None,
            "run_id": run["id"],
            "order_index": idx,
            "phase": "winpe",
            "kind": action["kind"],
            "params_json": json.dumps(action.get("params") or {}, sort_keys=True),
            "state": "planned",
            "started_at": None,
            "finished_at": None,
            "error": None,
            "is_preview": True,
        }
        for idx, action in enumerate(_winpe_actions_for_sequence(seq))
    ]


def _step_counts(steps: list[dict]) -> dict:
    return {
        "total": len(steps),
        "ok": sum(1 for step in steps if step["state"] == "ok"),
        "running": sum(1 for step in steps if step["state"] == "running"),
        "error": sum(1 for step in steps if step["state"] == "error"),
        "planned": sum(1 for step in steps if step["state"] in ("planned", "pending")),
    }


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    runs = sequences_db.list_provisioning_runs(
        SEQUENCES_DB, provision_path="winpe",
    )
    return templates.TemplateResponse(
        "runs.html",
        {"request": request, "runs": runs},
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail_page(run_id: int, request: Request):
    run = sequences_db.get_provisioning_run(SEQUENCES_DB, run_id)
    if run is None:
        raise HTTPException(status_code=404)
    seq = sequences_db.get_sequence(SEQUENCES_DB, run["sequence_id"])
    steps = _run_steps_for_display(run, seq)
    run = {**run, "sequence_name": (seq or {}).get("name", "")}
    return templates.TemplateResponse(
        "run_detail.html",
        {
            "request": request,
            "run": run,
            "steps": steps,
            "step_counts": _step_counts(steps),
        },
    )


_V2_STEP_TEMPLATES = [
    {
        "kind": "capture_hash",
        "label": "Capture WinPE hardware hash",
        "phase": "pe",
        "category": "WinPE",
        "description": "Capture the Autopilot hardware hash before disk operations when a WinPE flow owns hash capture.",
    },
    {
        "kind": "partition_disk",
        "label": "Partition disk",
        "phase": "pe",
        "category": "WinPE",
        "description": "Create the Windows disk layout before image apply.",
    },
    {
        "kind": "apply_wim",
        "label": "Apply Windows image",
        "phase": "pe",
        "category": "WinPE",
        "description": "Apply a WIM image to the prepared Windows volume.",
        "timeout_seconds": 7200,
    },
    {
        "kind": "apply_driver_package",
        "label": "Apply driver package",
        "phase": "pe",
        "category": "WinPE",
        "description": "Inject storage, network, and platform drivers into the offline Windows image.",
    },
    {
        "kind": "prepare_windows_setup",
        "label": "Prepare Windows setup",
        "phase": "pe",
        "category": "WinPE",
        "description": "Stage setup files and unattend assets before boot handoff.",
    },
    {
        "kind": "bake_boot_entry",
        "label": "Bake boot entry",
        "phase": "pe",
        "category": "WinPE",
        "description": "Prepare the Windows boot entry for the installed OS volume.",
    },
    {
        "kind": "handoff_to_windows_setup",
        "label": "Handoff to Windows setup",
        "phase": "pe",
        "category": "WinPE",
        "description": "Complete WinPE work and reboot into the installed Windows setup flow.",
    },
    {
        "kind": "cloudosd_preflight",
        "label": "OSDCloud preflight",
        "phase": "pe",
        "category": "OSDCloud",
        "description": "Validate VM identity, network, artifact, catalog, and package inputs.",
    },
    {
        "kind": "cloudosd_deploy_os",
        "label": "Run OSDCloud workflow",
        "phase": "pe",
        "category": "OSDCloud",
        "description": "Let OSDCloud partition, apply Windows, and prepare boot files.",
        "timeout_seconds": 7200,
    },
    {
        "kind": "stage_ad_domain_join_unattend",
        "label": "Stage AD domain join unattend",
        "phase": "pe",
        "category": "Identity",
        "description": "Inject specialize-pass Microsoft-Windows-UnattendedJoin data without storing plaintext credentials.",
    },
    {
        "kind": "verify_ad_domain_join",
        "label": "Verify AD domain membership",
        "phase": "full_os",
        "category": "Identity",
        "description": "AutopilotAgent reports full-OS domain membership evidence.",
        "retry_count": 60,
        "retry_delay_seconds": 30,
    },
    {
        "kind": "cloudosd_validate_offline_os",
        "label": "Validate offline Windows",
        "phase": "pe",
        "category": "Validation",
        "description": "Check Windows volume, boot files, VirtIO drivers, and staged payloads before reboot.",
    },
    {
        "kind": "stage_osd_client",
        "label": "Stage OSD client",
        "phase": "pe",
        "category": "Payload",
        "description": "Copy the existing OSD client payload for first-boot use.",
    },
    {
        "kind": "stage_autopilot_agent",
        "label": "Stage AutopilotAgent",
        "phase": "pe",
        "category": "Payload",
        "description": "Copy the MSI, postinstall, run token, and first-boot chain.",
    },
    {
        "kind": "install_autopilot_agent",
        "label": "Install AutopilotAgent",
        "phase": "full_os",
        "category": "Autopilot",
        "description": "Install the persistent agent MSI and run postinstall with the run-scoped bootstrap token.",
    },
    {
        "kind": "rename_computer",
        "label": "Set computer name",
        "phase": "specialize",
        "category": "Identity",
        "description": "Apply the Windows name during specialize when the deployment path supports it.",
    },
    {
        "kind": "capture_autopilot_hash",
        "label": "Capture Autopilot hardware hash",
        "phase": "full_os",
        "category": "Autopilot",
        "description": "AutopilotAgent captures and posts the hardware hash with group tag evidence.",
        "retry_count": 2,
        "retry_delay_seconds": 20,
    },
    {
        "kind": "wait_agent_heartbeat",
        "label": "Wait for AutopilotAgent heartbeat",
        "phase": "full_os",
        "category": "Autopilot",
        "description": "Controller-side completion gate for the run-bound AutopilotAgent heartbeat.",
        "retry_count": 60,
        "retry_delay_seconds": 10,
    },
    {
        "kind": "install_qga",
        "label": "Install QEMU Guest Agent",
        "phase": "full_os",
        "category": "OSD Client",
        "description": "Install the QEMU Guest Agent in the full Windows OS.",
    },
    {
        "kind": "fix_recovery_partition",
        "label": "Fix recovery partition",
        "phase": "full_os",
        "category": "OSD Client",
        "description": "Repair Windows recovery partition state after deployment.",
    },
    {
        "kind": "verify_qga",
        "label": "Verify QEMU Guest Agent",
        "phase": "full_os",
        "category": "OSD Client",
        "description": "Confirm QEMU Guest Agent is installed and responsive.",
        "retry_count": 10,
        "retry_delay_seconds": 15,
    },
    {
        "kind": "install_qga_watchdog",
        "label": "Install QGA watchdog",
        "phase": "full_os",
        "category": "OSD Client",
        "description": "Install the scheduled task that keeps QEMU Guest Agent healthy.",
    },
    {
        "kind": "handoff_to_oobe",
        "label": "Handoff to OOBE",
        "phase": "full_os",
        "category": "OSD Client",
        "description": "Finish OSD client work and hand Windows back to OOBE.",
    },
    {
        "kind": "join_domain_role",
        "label": "Join isolated domain",
        "phase": "full_os",
        "category": "OSDeploy Role",
        "description": "Join a lab child server to the isolated domain using an operator-provided credential.",
    },
    {
        "kind": "configure_file_server_role",
        "label": "Configure File Server role",
        "phase": "full_os",
        "category": "OSDeploy Role",
        "description": "Install FS-FileServer, create the requested folder/share, and apply SMB/NTFS access.",
    },
    {
        "kind": "configure_isolated_domain_controller_role",
        "label": "Configure isolated domain controller",
        "phase": "full_os",
        "category": "OSDeploy Role",
        "description": "Install AD DS/DNS and promote a new isolated forest.",
    },
    {
        "kind": "verify_isolated_domain_controller_role",
        "label": "Verify isolated domain controller",
        "phase": "full_os",
        "category": "OSDeploy Role",
        "description": "Verify AD DS, DNS, SYSVOL, and NETLOGON evidence after promotion.",
    },
    {
        "kind": "configure_mecm_prereq_role",
        "label": "Configure MECM prereq baseline",
        "phase": "full_os",
        "category": "OSDeploy Role",
        "description": "Install the Windows feature prerequisite baseline for a future MECM site server.",
    },
    {
        "kind": "install_package",
        "label": "Install package",
        "phase": "full_os",
        "category": "Content",
        "description": "Resolve a content-library package and hand execution to the agent.",
    },
    {
        "kind": "run_script",
        "label": "Run PowerShell script",
        "phase": "full_os",
        "category": "Content",
        "description": "Execute a managed script step from the v2 plan.",
    },
    {
        "kind": "install_app",
        "label": "Install app",
        "phase": "full_os",
        "category": "Content",
        "description": "Application install step for conditional full-OS app deployment plans.",
    },
    {
        "kind": "proxmox_clone_vm",
        "label": "Clone Proxmox VM",
        "phase": "controller",
        "category": "Clone",
        "description": "Create a VM from the configured Proxmox template and apply base VM policy.",
    },
    {
        "kind": "apply_oem_profile",
        "label": "Apply OEM profile",
        "phase": "controller",
        "category": "Clone",
        "description": "Apply local OEM profile metadata, serial policy, and VM identity choices.",
    },
    {
        "kind": "set_smbios_chassis",
        "label": "Set SMBIOS chassis",
        "phase": "controller",
        "category": "Clone",
        "description": "Apply the requested chassis type through the Proxmox VM configuration path.",
    },
    {
        "kind": "wait_guest_agent",
        "label": "Wait for guest agent",
        "phase": "controller",
        "category": "Clone",
        "description": "Wait for QEMU Guest Agent evidence after the cloned or deployed OS boots.",
        "retry_count": 60,
        "retry_delay_seconds": 10,
    },
    {
        "kind": "install_ubuntu_core",
        "label": "Install Ubuntu core",
        "phase": "install",
        "category": "Ubuntu",
        "description": "Set locale, timezone, package update policy, QGA package, and optional apt proxy on the Ubuntu cloud image.",
    },
    {
        "kind": "create_ubuntu_user",
        "label": "Create Ubuntu user",
        "phase": "install",
        "category": "Ubuntu",
        "description": "Create the local sudo user from a local_admin credential reference.",
    },
    {
        "kind": "install_desktop_environment",
        "label": "Install desktop environment",
        "phase": "install",
        "category": "Ubuntu",
        "description": "Install the selected Ubuntu desktop metapackage on the template.",
        "timeout_seconds": 7200,
    },
    {
        "kind": "install_apt_packages",
        "label": "Install apt packages",
        "phase": "install",
        "category": "Ubuntu",
        "description": "Install apt packages during cloud-init template build.",
    },
    {
        "kind": "remove_apt_packages",
        "label": "Remove apt packages",
        "phase": "install",
        "category": "Ubuntu",
        "description": "Purge apt packages during cloud-init template build.",
    },
    {
        "kind": "install_snap_packages",
        "label": "Install snap packages",
        "phase": "install",
        "category": "Ubuntu",
        "description": "Install snap packages during cloud-init template build.",
    },
    {
        "kind": "install_intune_portal",
        "label": "Install Intune Portal",
        "phase": "install",
        "category": "Ubuntu Readiness",
        "description": "Configure the Microsoft Ubuntu repository and install intune-portal.",
    },
    {
        "kind": "install_edge",
        "label": "Install Microsoft Edge",
        "phase": "install",
        "category": "Ubuntu Readiness",
        "description": "Configure the Microsoft Edge repository and install microsoft-edge-stable.",
    },
    {
        "kind": "install_mde_linux",
        "label": "Install MDE for Linux",
        "phase": "install",
        "category": "Ubuntu Readiness",
        "description": "Install mdatp and run the Defender onboarding credential during cloud-init.",
    },
    {
        "kind": "run_late_command",
        "label": "Run Ubuntu install command",
        "phase": "install",
        "category": "Ubuntu",
        "description": "Run a shell command during Ubuntu template cloud-init.",
    },
    {
        "kind": "run_firstboot_script",
        "label": "Run Ubuntu first-boot script",
        "phase": "first_boot",
        "category": "Ubuntu",
        "description": "Run a shell command from the per-VM first-boot NoCloud seed.",
    },
    {
        "kind": "wait_cloud_init_complete",
        "label": "Wait for cloud-init complete",
        "phase": "verify",
        "category": "Ubuntu Verify",
        "description": "Linux agent reports cloud-init status evidence after first boot.",
        "retry_count": 30,
        "retry_delay_seconds": 20,
    },
    {
        "kind": "verify_qga_linux",
        "label": "Verify Linux QGA",
        "phase": "verify",
        "category": "Ubuntu Verify",
        "description": "Linux agent confirms qemu-guest-agent package and service state.",
        "retry_count": 10,
        "retry_delay_seconds": 15,
    },
    {
        "kind": "verify_intune_portal",
        "label": "Verify Intune Portal",
        "phase": "verify",
        "category": "Ubuntu Readiness",
        "description": "Linux agent reports intune-portal install/version and enrollment readiness.",
        "retry_count": 10,
        "retry_delay_seconds": 30,
    },
    {
        "kind": "verify_mde_linux",
        "label": "Verify MDE for Linux",
        "phase": "verify",
        "category": "Ubuntu Readiness",
        "description": "Linux agent reports mdatp health/onboarding state.",
        "retry_count": 10,
        "retry_delay_seconds": 30,
    },
    {
        "kind": "linux_agent_heartbeat",
        "label": "Linux agent heartbeat",
        "phase": "full_os",
        "category": "Ubuntu Verify",
        "description": "Completion gate that proves the Ubuntu v2 agent has registered from the installed OS.",
        "retry_count": 60,
        "retry_delay_seconds": 10,
    },
]

from web.osd_v2_catalog import target_for_step_kind

for _template in _V2_STEP_TEMPLATES:
    _template.setdefault("target_os", target_for_step_kind(_template["kind"]))


def _v2_template_node(kind: str, **overrides) -> dict:
    source = next((item for item in _V2_STEP_TEMPLATES if item["kind"] == kind), {})
    node = {
        "client_id": overrides.pop("client_id", f"template-{kind}"),
        "node_type": "step",
        "name": overrides.pop("name", source.get("label") or kind.replace("_", " ").title()),
        "description": overrides.pop("description", source.get("description") or ""),
        "kind": kind,
        "phase": overrides.pop("phase", source.get("phase") or "full_os"),
        "enabled": overrides.pop("enabled", True),
        "condition": overrides.pop("condition", {}),
        "variables": overrides.pop("variables", {}),
        "params": overrides.pop("params", {}),
        "content_refs": overrides.pop("content_refs", []),
        "continue_on_error": overrides.pop("continue_on_error", False),
        "retry_count": overrides.pop("retry_count", source.get("retry_count", 0)),
        "retry_delay_seconds": overrides.pop("retry_delay_seconds", source.get("retry_delay_seconds", 10)),
        "timeout_seconds": overrides.pop("timeout_seconds", source.get("timeout_seconds")),
        "reboot_behavior": overrides.pop("reboot_behavior", "none"),
    }
    node.update(overrides)
    return node


_V2_CURRENT_FLOW_TEMPLATES = [
    {
        "id": "cloudosd-desktop",
        "name": "OSDCloud Desktop Client",
        "target_os": "windows",
        "path": "OSDCloud",
        "status": "primary desktop path",
        "description": (
            "Deploy a Windows desktop client through OSDCloud, stage the OSD client "
            "and AutopilotAgent, capture the Autopilot hash, and keep completion gated "
            "on the run-bound agent heartbeat."
        ),
        "notes": [
            "Use this as the default Windows desktop client baseline.",
            "OS deployment evidence comes from PE and OSDCloud; full-OS readiness comes from AutopilotAgent v2.",
            "Autopilot hash capture and heartbeat remain explicit v2 steps.",
        ],
        "nodes": [
            _v2_template_node("cloudosd_preflight"),
            _v2_template_node("cloudosd_deploy_os"),
            _v2_template_node("cloudosd_validate_offline_os"),
            _v2_template_node("stage_osd_client"),
            _v2_template_node("stage_autopilot_agent"),
            _v2_template_node("capture_autopilot_hash"),
            _v2_template_node("wait_agent_heartbeat"),
        ],
    },
    {
        "id": "cloudosd-desktop-domain-join",
        "name": "OSDCloud Desktop Client + AD Domain Join",
        "target_os": "windows",
        "path": "OSDCloud",
        "status": "desktop identity path",
        "description": (
            "Deploy a Windows desktop client through OSDCloud and stage AD domain join "
            "through specialize unattend while ProxmoxVEAutopilot v2 owns verification."
        ),
        "notes": [
            "Domain credentials are resolved at package generation time and are not stored in the v2 plan.",
            "SetupComplete remains for agent/bootstrap follow-up, not the primary AD join timing.",
            "Completion requires heartbeat plus full-OS domain membership evidence.",
        ],
        "nodes": [
            _v2_template_node("cloudosd_preflight"),
            _v2_template_node("cloudosd_deploy_os"),
            _v2_template_node("stage_ad_domain_join_unattend"),
            _v2_template_node("cloudosd_validate_offline_os"),
            _v2_template_node("stage_osd_client"),
            _v2_template_node("stage_autopilot_agent"),
            _v2_template_node("capture_autopilot_hash"),
            _v2_template_node("verify_ad_domain_join"),
            _v2_template_node("wait_agent_heartbeat"),
        ],
    },
    {
        "id": "osdeploy-server-base",
        "name": "OSDeploy Windows Server Base",
        "target_os": "windows",
        "path": "OSDeploy",
        "status": "advanced Server base path",
        "description": (
            "Deploy a Windows Server base VM through OSDeploy v2 with artifact, "
            "cache, PE, full-OS agent, and heartbeat evidence."
        ),
        "notes": [
            "This is the first OSDeploy maturity gate before role automation.",
            "Completion requires the run-scoped AutopilotAgent heartbeat.",
        ],
        "nodes": [
            _v2_template_node("osdeploy_preflight", name="OSDeploy artifact and cache preflight"),
            _v2_template_node("apply_wim", name="Apply OSDeploy Server image"),
            _v2_template_node("apply_driver_package"),
            _v2_template_node("prepare_windows_setup"),
            _v2_template_node("bake_boot_entry"),
            _v2_template_node("stage_osd_client"),
            _v2_template_node("stage_autopilot_agent"),
            _v2_template_node("wait_agent_heartbeat"),
        ],
    },
    {
        "id": "osdeploy-file-server",
        "name": "OSDeploy File Server",
        "target_os": "windows",
        "path": "OSDeploy",
        "status": "server role template",
        "description": "Deploy Windows Server base and configure file server prerequisites through v2 role steps.",
        "notes": [
            "Role steps run after the OSDeploy base image and agent heartbeat path exists.",
        ],
        "nodes": [
            _v2_template_node("osdeploy_preflight", name="OSDeploy artifact and cache preflight"),
            _v2_template_node("apply_wim", name="Apply OSDeploy Server image"),
            _v2_template_node("apply_driver_package"),
            _v2_template_node("prepare_windows_setup"),
            _v2_template_node("stage_osd_client"),
            _v2_template_node("stage_autopilot_agent"),
            _v2_template_node("configure_file_server_role", name="Configure File Server role", params={"server_role": "file_server"}),
            _v2_template_node("wait_agent_heartbeat"),
        ],
    },
    {
        "id": "osdeploy-isolated-domain-controller",
        "name": "OSDeploy Isolated Domain Controller",
        "target_os": "windows",
        "path": "OSDeploy",
        "status": "isolated lab identity template",
        "description": "Deploy an isolated lab forest domain controller without mutating existing production domains.",
        "notes": [
            "Existing-domain operations require a separate approval-gated plan.",
        ],
        "nodes": [
            _v2_template_node("osdeploy_preflight", name="OSDeploy artifact and cache preflight"),
            _v2_template_node("apply_wim", name="Apply OSDeploy Server image"),
            _v2_template_node("apply_driver_package"),
            _v2_template_node("prepare_windows_setup"),
            _v2_template_node("stage_osd_client"),
            _v2_template_node("stage_autopilot_agent"),
            _v2_template_node("configure_isolated_domain_controller_role", name="Create isolated lab forest", params={"server_role": "isolated_domain_controller"}),
            _v2_template_node("verify_isolated_domain_controller_role", name="Verify isolated lab domain services"),
            _v2_template_node("wait_agent_heartbeat"),
        ],
    },
    {
        "id": "osdeploy-mecm-prereq",
        "name": "OSDeploy MECM Prereq Baseline",
        "target_os": "windows",
        "path": "OSDeploy",
        "status": "MECM foundation template",
        "description": "Deploy Windows Server and stage MECM prerequisites before a later dedicated site-server install milestone.",
        "notes": [
            "Full MECM site install is intentionally later than prerequisite validation.",
        ],
        "nodes": [
            _v2_template_node("osdeploy_preflight", name="OSDeploy artifact and cache preflight"),
            _v2_template_node("apply_wim", name="Apply OSDeploy Server image"),
            _v2_template_node("apply_driver_package"),
            _v2_template_node("prepare_windows_setup"),
            _v2_template_node("stage_osd_client"),
            _v2_template_node("stage_autopilot_agent"),
            _v2_template_node("configure_mecm_prereq_role", name="Install MECM prerequisites", params={"server_role": "mecm_prereq"}),
            _v2_template_node("wait_agent_heartbeat"),
        ],
    },
    {
        "id": "osdeploy-lab-in-a-box",
        "name": "OSDeploy Lab in a Box",
        "target_os": "windows",
        "path": "OSDeploy",
        "status": "multi-server lab template",
        "description": "Template shape for disposable isolated lab bundles composed from OSDeploy Server roles.",
        "notes": [
            "Creates child OSDeploy runs in dependency order and uses an operator-provided domain join credential.",
        ],
        "nodes": [
            _v2_template_node("osdeploy_preflight", name="OSDeploy artifact and cache preflight"),
            _v2_template_node("apply_wim", name="Apply OSDeploy Server image"),
            _v2_template_node("apply_driver_package"),
            _v2_template_node("prepare_windows_setup"),
            _v2_template_node("stage_osd_client"),
            _v2_template_node("stage_autopilot_agent"),
            _v2_template_node("configure_isolated_domain_controller_role", name="Create lab bundle domain controller", params={"server_role": "isolated_domain_controller"}),
            _v2_template_node("join_domain_role", name="Join lab child servers to domain"),
            _v2_template_node("verify_ad_domain_join", name="Verify lab child domain membership"),
            _v2_template_node("configure_file_server_role", name="Configure lab bundle file server", params={"server_role": "file_server"}),
            _v2_template_node("configure_mecm_prereq_role", name="Configure lab bundle MECM prereq server", params={"server_role": "mecm_prereq"}),
            _v2_template_node("wait_agent_heartbeat"),
        ],
    },
    {
        "id": "winpe-desktop-wim",
        "name": "WinPE Desktop WIM Deployment",
        "target_os": "windows",
        "path": "WinPE",
        "status": "desktop fallback path",
        "description": (
            "Deploy a Windows desktop client through the existing WinPE/WIM flow. This "
            "template documents the compatibility path without changing `/winpe/*` behavior."
        ),
        "notes": [
            "Keep this path available for fallback and regression checks.",
            "The existing WinPE orchestration remains independent from OSDCloud.",
        ],
        "nodes": [
            _v2_template_node("capture_hash"),
            _v2_template_node("partition_disk"),
            _v2_template_node("apply_wim"),
            _v2_template_node("apply_driver_package"),
            _v2_template_node("prepare_windows_setup"),
            _v2_template_node("bake_boot_entry"),
            _v2_template_node("handoff_to_windows_setup"),
        ],
    },
    {
        "id": "winpe-server-wim",
        "name": "WinPE Windows Server WIM Deployment",
        "target_os": "windows",
        "path": "WinPE",
        "status": "server path",
        "description": (
            "Deploy Windows Server through the WinPE/WIM substrate. OSDCloud remains "
            "desktop-focused; server builds stay on WinPE or clone workflows."
        ),
        "notes": [
            "No Autopilot hash capture is included by default for server builds.",
            "Use this as the v2 planning shape for Windows Server deployments.",
        ],
        "nodes": [
            _v2_template_node("partition_disk"),
            _v2_template_node("apply_wim", name="Apply Windows Server image"),
            _v2_template_node("apply_driver_package"),
            _v2_template_node("prepare_windows_setup"),
            _v2_template_node("bake_boot_entry"),
            _v2_template_node("handoff_to_windows_setup"),
        ],
    },
    {
        "id": "clone-desktop-template",
        "name": "Proxmox Clone Desktop from Template",
        "target_os": "windows",
        "path": "Clone",
        "status": "template clone path",
        "description": (
            "Clone from the existing Proxmox template path, apply identity metadata, "
            "wait for guest-agent evidence, and keep desktop post-boot work explicit."
        ),
        "notes": [
            "This documents the current clone experience as a v2-owned plan shape.",
            "Use WinPE or clone paths for server-oriented builds.",
        ],
        "nodes": [
            _v2_template_node("proxmox_clone_vm"),
            _v2_template_node("apply_oem_profile"),
            _v2_template_node("set_smbios_chassis"),
            _v2_template_node("rename_computer"),
            _v2_template_node("wait_guest_agent"),
            _v2_template_node("install_qga"),
            _v2_template_node("verify_qga"),
        ],
    },
    {
        "id": "ubuntu-desktop-plain",
        "name": "Ubuntu Desktop Plain",
        "target_os": "ubuntu",
        "path": "Ubuntu",
        "status": "desktop baseline",
        "description": (
            "Build and clone an Ubuntu 24.04 desktop client from the cloud-image "
            "NoCloud path, then verify cloud-init, QGA, and Linux agent heartbeat."
        ),
        "notes": [
            "No Autopilot hash or Intune upload is used for Ubuntu.",
            "The Linux v2 agent owns full-OS readiness evidence after first boot.",
        ],
        "nodes": [
            _v2_template_node("proxmox_clone_vm"),
            _v2_template_node("install_ubuntu_core"),
            _v2_template_node(
                "create_ubuntu_user",
                params={"local_admin_credential_id": 1},
            ),
            _v2_template_node(
                "install_desktop_environment",
                params={"flavor": "ubuntu-desktop"},
            ),
            _v2_template_node("wait_cloud_init_complete"),
            _v2_template_node("verify_qga_linux"),
            _v2_template_node("linux_agent_heartbeat"),
        ],
    },
    {
        "id": "ubuntu-desktop-intune-edge",
        "name": "Ubuntu Desktop Intune + Edge",
        "target_os": "ubuntu",
        "path": "Ubuntu",
        "status": "desktop readiness path",
        "description": (
            "Build an Ubuntu desktop with Intune Portal and Microsoft Edge installed, "
            "then report enrollment as waiting for interactive user sign-in."
        ),
        "notes": [
            "Intune Linux enrollment is not treated as an unattended deployment gate.",
            "The readiness state can remain waiting_for_user_signin without failing the run.",
        ],
        "nodes": [
            _v2_template_node("proxmox_clone_vm"),
            _v2_template_node("install_ubuntu_core"),
            _v2_template_node(
                "create_ubuntu_user",
                params={"local_admin_credential_id": 1},
            ),
            _v2_template_node("install_intune_portal"),
            _v2_template_node("install_edge"),
            _v2_template_node("install_desktop_environment", params={"flavor": "ubuntu-desktop"}),
            _v2_template_node("wait_cloud_init_complete"),
            _v2_template_node("verify_intune_portal"),
            _v2_template_node("linux_agent_heartbeat"),
        ],
    },
    {
        "id": "ubuntu-desktop-intune-mde",
        "name": "Ubuntu Desktop Intune + MDE",
        "target_os": "ubuntu",
        "path": "Ubuntu",
        "status": "LinuxESP-style path",
        "description": (
            "LinuxESP-style Ubuntu desktop with Intune Portal, Edge, and Defender "
            "for Endpoint onboarding tracked through v2 readiness evidence."
        ),
        "notes": [
            "MDE onboarding is resolved from a credential at compile time.",
            "Do not persist onboarding script content in v2 plans, job args, or UI JSON.",
        ],
        "nodes": [
            _v2_template_node("proxmox_clone_vm"),
            _v2_template_node("install_ubuntu_core"),
            _v2_template_node(
                "create_ubuntu_user",
                params={"local_admin_credential_id": 1},
            ),
            _v2_template_node("install_apt_packages", params={"packages": ["curl", "git", "wget", "gpg"]}),
            _v2_template_node("install_intune_portal"),
            _v2_template_node("install_edge"),
            _v2_template_node("install_mde_linux", params={"mde_onboarding_credential_id": 0}),
            _v2_template_node("install_snap_packages", params={"snaps": [{"name": "code", "classic": True}, {"name": "powershell", "classic": True}]}),
            _v2_template_node("install_desktop_environment", params={"flavor": "ubuntu-desktop"}),
            _v2_template_node("wait_cloud_init_complete"),
            _v2_template_node("verify_intune_portal"),
            _v2_template_node("verify_mde_linux"),
            _v2_template_node("linux_agent_heartbeat"),
        ],
    },
    {
        "id": "ubuntu-server-minimal",
        "name": "Ubuntu Server Minimal",
        "target_os": "ubuntu",
        "path": "Ubuntu",
        "status": "server baseline",
        "description": "Build and clone a minimal Ubuntu 24.04 server with QGA and Linux agent evidence.",
        "notes": [
            "This path is for Linux server-style workloads; Windows Server remains on WinPE or clone.",
        ],
        "nodes": [
            _v2_template_node("proxmox_clone_vm"),
            _v2_template_node("install_ubuntu_core"),
            _v2_template_node(
                "create_ubuntu_user",
                params={"local_admin_credential_id": 1},
            ),
            _v2_template_node("wait_cloud_init_complete"),
            _v2_template_node("verify_qga_linux"),
            _v2_template_node("linux_agent_heartbeat"),
        ],
    },
    {
        "id": "ubuntu-apt-cache-server",
        "name": "Ubuntu apt-cache Server",
        "target_os": "ubuntu",
        "path": "Ubuntu",
        "status": "lab infrastructure",
        "description": "Provision an Ubuntu apt-cache server and report QGA/Linux agent readiness through v2.",
        "notes": [
            "Use this before large Ubuntu desktop batches when the lab benefits from apt caching.",
        ],
        "nodes": [
            _v2_template_node("proxmox_clone_vm"),
            _v2_template_node("install_ubuntu_core"),
            _v2_template_node(
                "create_ubuntu_user",
                params={"local_admin_credential_id": 1},
            ),
            _v2_template_node("install_apt_packages", params={"packages": ["apt-cacher-ng"]}),
            _v2_template_node("run_late_command", params={"command": "systemctl enable --now apt-cacher-ng"}),
            _v2_template_node("wait_cloud_init_complete"),
            _v2_template_node("verify_qga_linux"),
            _v2_template_node("linux_agent_heartbeat"),
        ],
    },
]


def _v2_flow_templates() -> list[dict]:
    return [
        {
            **template,
            "step_count": len(template["nodes"]),
            "read_only": True,
        }
        for template in _V2_CURRENT_FLOW_TEMPLATES
    ]


def _v2_flow_template(template_id: str | None) -> dict | None:
    if not template_id:
        return None
    return next((item for item in _v2_flow_templates() if item["id"] == template_id), None)


def _legacy_step_to_v2_nodes(step: dict, index: int) -> list[dict]:
    step_type = step.get("step_type")
    params = dict(step.get("params") or {})
    enabled = bool(step.get("enabled", True))
    common = {
        "enabled": enabled,
        "params": params,
        "client_id": f"legacy-{step.get('id') or index}",
    }
    mapping = {
        "set_oem_hardware": ("Apply OEM hardware profile", "set_oem_hardware", "controller"),
        "local_admin": ("Configure local administrator", "local_admin", "specialize"),
        "autopilot_entra": ("Prepare Entra Autopilot", "autopilot_entra", "full_os"),
        "autopilot_hybrid": ("Prepare Hybrid Autopilot", "autopilot_hybrid", "full_os"),
        "rename_computer": ("Set computer name", "rename_computer", "specialize"),
        "run_script": ("Run PowerShell script", "run_script", "full_os"),
        "install_module": ("Install PowerShell module", "install_module", "full_os"),
        "wait_guest_agent": ("Wait for guest agent", "wait_guest_agent", "controller"),
        "install_ubuntu_core": ("Install Ubuntu core", "install_ubuntu_core", "install"),
        "create_ubuntu_user": ("Create Ubuntu user", "create_ubuntu_user", "install"),
        "install_apt_packages": ("Install apt packages", "install_apt_packages", "install"),
        "install_snap_packages": ("Install snap packages", "install_snap_packages", "install"),
        "remove_apt_packages": ("Remove apt packages", "remove_apt_packages", "install"),
        "install_intune_portal": ("Install Intune Portal", "install_intune_portal", "full_os"),
        "install_edge": ("Install Microsoft Edge", "install_edge", "full_os"),
        "install_mde_linux": ("Install MDE Linux", "install_mde_linux", "full_os"),
        "install_desktop_environment": ("Install desktop environment", "install_desktop_environment", "install"),
        "run_late_command": ("Run late command", "run_late_command", "install"),
        "run_firstboot_script": ("Run first-boot script", "run_firstboot_script", "full_os"),
    }
    if step_type == "join_ad_domain":
        return [
            {
                **common,
                "client_id": f"legacy-{step.get('id') or index}-stage",
                "node_type": "step",
                "name": "Stage AD domain join unattend",
                "kind": "stage_ad_domain_join_unattend",
                "phase": "pe",
            },
            {
                **common,
                "client_id": f"legacy-{step.get('id') or index}-verify",
                "node_type": "step",
                "name": "Verify AD domain membership",
                "kind": "verify_ad_domain_join",
                "phase": "full_os",
                "retry_count": 60,
                "retry_delay_seconds": 30,
            },
        ]
    label, kind, phase = mapping.get(
        step_type,
        (str(step_type or "Legacy step").replace("_", " ").title(), step_type or "legacy_step", "full_os"),
    )
    reboot_behavior = "required" if params.get("causes_reboot") else "none"
    return [{
        **common,
        "node_type": "step",
        "name": label,
        "kind": kind,
        "phase": phase,
        "reboot_behavior": reboot_behavior,
    }]


def _legacy_sequence_to_v2_nodes(legacy: dict) -> list[dict]:
    nodes: list[dict] = []
    for index, step in enumerate(legacy.get("steps") or []):
        nodes.extend(_legacy_step_to_v2_nodes(step, index))
    kinds = {node.get("kind") for node in nodes}
    if legacy.get("produces_autopilot_hash") and "capture_autopilot_hash" not in kinds:
        hash_node = {
            "client_id": "legacy-autopilot-hash",
            "node_type": "step",
            "name": "Capture Autopilot hardware hash",
            "kind": "capture_autopilot_hash",
            "phase": "full_os",
            "params": {"source": "legacy_sequence"},
            "retry_count": 2,
            "retry_delay_seconds": 20,
        }
        verify_index = next(
            (index for index, node in enumerate(nodes) if node.get("kind") == "verify_ad_domain_join"),
            len(nodes),
        )
        nodes.insert(verify_index, hash_node)
    return nodes


def _save_v2_builder_sequence(conn, sequence_id: str, body: _V2BuilderSequenceIn) -> str:
    from web import ts_engine_pg
    from web.osd_v2_catalog import validate_steps_for_target_os

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="sequence name is required")
    target_os = (body.target_os or "windows").strip().lower()
    try:
        validate_steps_for_target_os(
            target_os,
            [node.model_dump() for node in body.nodes],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        ts_engine_pg.update_sequence(
            conn,
            sequence_id,
            name=name,
            description=body.description,
            target_os=target_os,
            enabled=body.enabled,
            updated_by="ui-builder",
        )
        ts_engine_pg.replace_sequence_nodes(
            conn,
            sequence_id,
            [node.model_dump() for node in body.nodes],
            updated_by="ui-builder",
        )
        return ts_engine_pg.compile_sequence(conn, sequence_id, compiled_by="ui-builder")
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except psycopg.IntegrityError as exc:
        conn.rollback()
        if _is_unique_violation(exc):
            raise HTTPException(status_code=409, detail="v2 sequence name already exists")
        raise


@app.get("/task-engine", response_class=HTMLResponse)
def task_engine_page(request: Request):
    from web import cloudosd_pg, db_pg, ts_engine_pg

    cloudosd_step_kinds = {
        "cloudosd_preflight",
        "cloudosd_deploy_os",
        "stage_ad_domain_join_unattend",
        "cloudosd_validate_offline_os",
        "stage_osd_client",
        "stage_autopilot_agent",
        "capture_autopilot_hash",
        "wait_agent_heartbeat",
        "verify_ad_domain_join",
    }
    with db_pg.connection(_database_url()) as conn:
        cloudosd_pg.sync_all_ts_progress(conn)
        sequences = ts_engine_pg.list_sequences(conn)
        for seq in sequences:
            seq["steps"] = ts_engine_pg.list_sequence_steps(conn, seq["id"])
        runs = ts_engine_pg.list_runs(conn)
        cloudosd_runs = []
        for run in runs:
            steps = ts_engine_pg.list_run_steps(conn, run["id"])
            run["steps"] = steps
            sequence_name = str(run.get("sequence_name") or "")
            if (
                sequence_name.startswith("OSDCloud deployment")
                or sequence_name.startswith("CloudOSD deployment")
                or any(step.get("kind") in cloudosd_step_kinds for step in steps)
            ):
                cloudosd_runs.append(run)
        content_items = ts_engine_pg.list_content_items(conn)
        manifest_items = ts_engine_pg.list_recent_manifest_items(conn)
    legacy_sequences = sequences_db.list_sequences(SEQUENCES_DB)
    return templates.TemplateResponse(
        "task_engine.html",
        {
            "request": request,
            "sequences": sequences,
            "runs": runs,
            "cloudosd_runs": cloudosd_runs,
            "content_items": content_items,
            "manifest_items": manifest_items,
            "legacy_sequences": legacy_sequences,
            "flow_templates": _v2_flow_templates(),
        },
    )


@app.get("/task-engine/sequences/list", response_class=HTMLResponse)
def task_engine_sequences_list(request: Request, target_os: str | None = None):
    from web import db_pg, ts_engine_pg

    with db_pg.connection(_database_url()) as conn:
        sequences = ts_engine_pg.list_sequences(conn)
        target_filter = (target_os or "").strip().lower()
        if target_filter:
            sequences = [
                seq for seq in sequences
                if (seq.get("target_os") or "windows") == target_filter
            ]
        for seq in sequences:
            seq["steps"] = ts_engine_pg.list_sequence_steps(conn, seq["id"])
    templates_for_ui = _v2_flow_templates()
    if target_filter:
        templates_for_ui = [
            template for template in templates_for_ui
            if (template.get("target_os") or "windows") == target_filter
        ]
    return templates.TemplateResponse(
        "task_engine_sequences_list.html",
        {
            "request": request,
            "sequences": sequences,
            "flow_templates": templates_for_ui,
            "target_os_filter": target_filter,
        },
    )


@app.get("/task-engine/sequences/new", response_class=HTMLResponse)
def task_engine_sequence_new(
    request: Request,
    legacy_id: int | None = None,
    template_id: str | None = None,
):
    legacy = sequences_db.get_sequence(SEQUENCES_DB, legacy_id) if legacy_id else None
    flow_template = _v2_flow_template(template_id)
    if template_id and not flow_template:
        raise HTTPException(status_code=404, detail="v2 flow template not found")
    sequence = None
    nodes = []
    if flow_template:
        sequence = {
            "id": None,
            "name": f"{flow_template['name']} copy",
            "description": flow_template["description"],
            "target_os": flow_template.get("target_os", "windows"),
            "enabled": True,
        }
        nodes = flow_template["nodes"]
    elif legacy:
        sequence = {
            "id": None,
            "name": f"{legacy['name']} v2",
            "description": legacy.get("description") or "",
            "target_os": legacy.get("target_os") or "windows",
            "enabled": True,
        }
        nodes = _legacy_sequence_to_v2_nodes(legacy)
    return templates.TemplateResponse(
        "task_engine_builder.html",
        {
            "request": request,
            "sequence": sequence,
            "nodes": nodes,
            "step_templates": _V2_STEP_TEMPLATES,
            "legacy_sequences": sequences_db.list_sequences(SEQUENCES_DB),
            "legacy_source_id": legacy_id,
            "flow_templates": _v2_flow_templates(),
            "template_source": flow_template,
        },
    )


@app.get("/task-engine/sequences/templates/{template_id}", response_class=HTMLResponse)
def task_engine_sequence_template_detail(request: Request, template_id: str):
    flow_template = _v2_flow_template(template_id)
    if not flow_template:
        raise HTTPException(status_code=404, detail="v2 flow template not found")
    return templates.TemplateResponse(
        "task_engine_sequence_template.html",
        {
            "request": request,
            "template": flow_template,
        },
    )


@app.get("/task-engine/sequences/{sequence_id}/edit", response_class=HTMLResponse)
def task_engine_sequence_edit(request: Request, sequence_id: str):
    from web import db_pg, ts_engine_pg

    with db_pg.connection(_database_url()) as conn:
        sequence = ts_engine_pg.get_sequence(conn, sequence_id)
        if not sequence:
            raise HTTPException(status_code=404, detail="v2 sequence not found")
        nodes = ts_engine_pg.list_sequence_nodes(conn, sequence_id)
    return templates.TemplateResponse(
        "task_engine_builder.html",
        {
            "request": request,
            "sequence": sequence,
            "nodes": nodes,
            "step_templates": _V2_STEP_TEMPLATES,
            "legacy_sequences": sequences_db.list_sequences(SEQUENCES_DB),
            "legacy_source_id": None,
            "flow_templates": _v2_flow_templates(),
            "template_source": None,
        },
    )


@app.post("/api/osd/v2/builder/sequences", status_code=201)
def api_v2_builder_create_sequence(body: _V2BuilderSequenceIn):
    from web import db_pg, ts_engine_pg

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="sequence name is required")
    with db_pg.connection(_database_url()) as conn:
        try:
            sequence_id = ts_engine_pg.create_sequence(
                conn,
                name=name,
                description=body.description,
                target_os=(body.target_os or "windows").lower(),
                created_by="ui-builder",
            )
            version_id = _save_v2_builder_sequence(conn, sequence_id, body)
        except psycopg.IntegrityError as exc:
            conn.rollback()
            if _is_unique_violation(exc):
                raise HTTPException(status_code=409, detail="v2 sequence name already exists")
            raise
    return {"id": sequence_id, "current_version_id": version_id}


@app.put("/api/osd/v2/builder/sequences/{sequence_id}")
def api_v2_builder_update_sequence(sequence_id: str, body: _V2BuilderSequenceIn):
    from web import db_pg

    with db_pg.connection(_database_url()) as conn:
        version_id = _save_v2_builder_sequence(conn, sequence_id, body)
    return {"ok": True, "id": sequence_id, "current_version_id": version_id}


@app.post("/api/osd/v2/builder/import-legacy/{legacy_id}", status_code=201)
def api_v2_builder_import_legacy_sequence(legacy_id: int):
    from web import db_pg, ts_engine_pg

    legacy = sequences_db.get_sequence(SEQUENCES_DB, legacy_id)
    if legacy is None:
        raise HTTPException(status_code=404, detail="legacy sequence not found")
    body = _V2BuilderSequenceIn(
        name=f"{legacy['name']} v2",
        description=legacy.get("description") or "",
        target_os=legacy.get("target_os") or "windows",
        enabled=True,
        nodes=[_V2BuilderNodeIn(**node) for node in _legacy_sequence_to_v2_nodes(legacy)],
    )
    with db_pg.connection(_database_url()) as conn:
        try:
            sequence_id = ts_engine_pg.create_sequence(
                conn,
                name=body.name,
                description=body.description,
                target_os=(body.target_os or "windows").lower(),
                created_by=f"legacy:{legacy_id}",
            )
            version_id = _save_v2_builder_sequence(conn, sequence_id, body)
        except psycopg.IntegrityError as exc:
            conn.rollback()
            if _is_unique_violation(exc):
                raise HTTPException(status_code=409, detail="v2 sequence name already exists")
            raise
    return {"id": sequence_id, "current_version_id": version_id}


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
    except psycopg.IntegrityError as e:
        msg = "name already exists" if _is_unique_violation(e) else str(e)
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
    except psycopg.IntegrityError as e:
        msg = "name already exists" if _is_unique_violation(e) else str(e)
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
        "request": request,
        "sequences": _sequence_rows_for_ui(seqs),
        "error": error,
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
    except psycopg.IntegrityError as e:
        if _is_unique_violation(e):
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
    s = device_history_db.get_settings()
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
        for o in device_history_db.list_search_ous()
    ]


@app.post("/api/monitoring/search-ous", status_code=201)
def api_monitoring_search_ous_create(body: _SearchOuCreate):
    try:
        ou_id = device_history_db.add_search_ou(
            dn=body.dn, label=body.label,
            enabled=body.enabled, sort_order=body.sort_order,
        )
    except device_history_db.InvalidDn as e:
        raise HTTPException(400, str(e)) from e
    except psycopg.IntegrityError as e:
        raise HTTPException(
            409, f"search OU with dn={body.dn!r} already exists",
        ) from e
    return {"id": ou_id}


@app.put("/api/monitoring/search-ous/{ou_id}")
def api_monitoring_search_ous_update(ou_id: int, body: _SearchOuPatch):
    try:
        device_history_db.update_search_ou(
            ou_id,
            dn=body.dn, label=body.label,
            enabled=body.enabled, sort_order=body.sort_order,
        )
    except device_history_db.CannotDeleteLastOu as e:
        raise HTTPException(409, str(e)) from e
    except device_history_db.InvalidDn as e:
        raise HTTPException(400, str(e)) from e
    except psycopg.IntegrityError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@app.delete("/api/monitoring/search-ous/{ou_id}")
def api_monitoring_search_ous_delete(ou_id: int):
    try:
        device_history_db.delete_search_ou(ou_id)
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
    try:
        return device_history_db.ad_first_seen_map()
    except Exception:
        return {}


@app.get("/monitoring", response_class=HTMLResponse)
def page_monitoring(request: Request):
    from web import db_pg, deployment_health, monitoring_view, service_health_pg as service_health
    latest = device_history_db.latest_per_vmid()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = monitoring_view.build_dashboard_rows(
        latest,
        ad_first_seen=_ad_first_seen_map(),
        now_iso=now_iso,
    )
    settings = device_history_db.get_settings()
    keytab = device_history_db.get_keytab_health()
    svc_rows = service_health.list_services()
    runtime_services = _runtime_container_status()
    with db_pg.connection(_database_url()) as conn:
        deployment_payload = deployment_health.build_deployments_payload(conn, limit=25)
    return templates.TemplateResponse("monitoring.html", {
        "request": request,
        "rows": rows,
        "settings": settings,
        "search_ous": device_history_db.list_search_ous(),
        "keytab": keytab,
        "service_health": svc_rows,
        "runtime_services": runtime_services,
        "deployment_health": deployment_payload,
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
    if not probe_row:
        return []
    ad_matches = _json_obj(probe_row.get("ad_matches_json"), [])
    entra_matches = _json_obj(probe_row.get("entra_matches_json"), [])
    intune_matches = _json_obj(probe_row.get("intune_matches_json"), [])
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


def _known_credentials_for_vmid(vmid: int) -> list[dict]:
    """Return operator-visible credentials already attached to known runs.

    This intentionally does not decrypt the generic credential vault. It only
    surfaces credentials that deployment records already mark as UI-visible,
    such as CloudOSD local workgroup admins.
    """
    from web import db_pg

    def _time_value(value):
        if value is None:
            return None
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    credentials = []
    try:
        with db_pg.connection(_database_url()) as conn:
            try:
                cloudosd_rows = conn.execute(
                    """
                    SELECT
                        run_id,
                        workflow_name,
                        COALESCE(pve_vm_name, requested_vm_name, vm_name) AS vm_name,
                        COALESCE(vmid, requested_vmid) AS matched_vmid,
                        local_admin_json,
                        updated_at
                    FROM cloudosd_runs
                    WHERE COALESCE(vmid, requested_vmid) = %s
                      AND COALESCE(local_admin_json->>'username', '') <> ''
                      AND COALESCE(local_admin_json->>'password', '') <> ''
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT 10
                    """,
                    (vmid,),
                ).fetchall()
            except Exception:
                cloudosd_rows = []
            for row in cloudosd_rows:
                local_admin = row.get("local_admin_json") or {}
                username = str(local_admin.get("username") or "").strip()
                password = str(local_admin.get("password") or "").strip()
                if not (username and password):
                    continue
                credentials.append({
                    "source": "CloudOSD",
                    "label": "Local admin",
                    "username": username,
                    "password": password,
                    "vm_name": row.get("vm_name") or "",
                    "run_id": str(row.get("run_id") or ""),
                    "run_url": f"/cloudosd/runs/{row['run_id']}" if row.get("run_id") else "",
                    "updated_at": _time_value(row.get("updated_at")),
                    "note": "Visible workgroup credential from the deployment run.",
                })
            try:
                osdeploy_rows = conn.execute(
                    """
                    SELECT
                        run_id,
                        workflow_name,
                        COALESCE(pve_vm_name, requested_vm_name, vm_name) AS vm_name,
                        COALESCE(vmid, requested_vmid) AS matched_vmid,
                        local_admin_json,
                        updated_at
                    FROM osdeploy_runs
                    WHERE COALESCE(vmid, requested_vmid) = %s
                      AND COALESCE(local_admin_json->>'username', '') <> ''
                      AND COALESCE(local_admin_json->>'password', '') <> ''
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT 10
                    """,
                    (vmid,),
                ).fetchall()
            except Exception:
                osdeploy_rows = []
            for row in osdeploy_rows:
                local_admin = row.get("local_admin_json") or {}
                username = str(local_admin.get("username") or "").strip()
                password = str(local_admin.get("password") or "").strip()
                if not (username and password):
                    continue
                credentials.append({
                    "source": "OSDeploy",
                    "label": "Local admin",
                    "username": username,
                    "password": password,
                    "vm_name": row.get("vm_name") or "",
                    "run_id": str(row.get("run_id") or ""),
                    "run_url": f"/osdeploy/runs/{row['run_id']}" if row.get("run_id") else "",
                    "updated_at": _time_value(row.get("updated_at")),
                    "note": "Visible local administrator credential from the deployment run.",
                })
    except Exception:
        return []
    return credentials


def _masked_known_credentials_for_vmid(vmid: int) -> list[dict]:
    masked: list[dict] = []
    for item in _known_credentials_for_vmid(vmid):
        password = str(item.get("password") or "")
        masked.append({
            "source": str(item.get("source") or ""),
            "label": str(item.get("label") or ""),
            "username": str(item.get("username") or ""),
            "password_available": bool(password),
            "password_mask": "********" if password else "",
            "vm_name": str(item.get("vm_name") or ""),
            "run_id": str(item.get("run_id") or ""),
            "run_url": str(item.get("run_url") or ""),
            "updated_at": item.get("updated_at"),
            "note": str(item.get("note") or ""),
        })
    return masked


def _event_to_dict(event: object) -> dict:
    if isinstance(event, dict):
        return {
            "at": str(event.get("at") or ""),
            "source": str(event.get("source") or ""),
            "type": str(event.get("type") or ""),
            "severity": str(event.get("severity") or "event"),
            "summary": str(event.get("summary") or ""),
            "details": event.get("details") if isinstance(event.get("details"), dict) else {},
        }
    return {
        "at": str(getattr(event, "at", "") or ""),
        "source": str(getattr(event, "source", "") or ""),
        "type": str(getattr(event, "type", "") or ""),
        "severity": str(getattr(event, "severity", "event") or "event"),
        "summary": str(getattr(event, "summary", "") or ""),
        "details": getattr(event, "details", {}) if isinstance(getattr(event, "details", {}), dict) else {},
    }


def _screenshot_timeline_events(vmid: int) -> list[dict]:
    events: list[dict] = []
    for item in _vm_screenshot_history(vmid):
        events.append({
            "at": item["captured_at"],
            "source": "screenshot",
            "type": "screenshot-captured",
            "severity": "event",
            "summary": f"Screenshot captured by {item['source']}",
            "details": {
                "image_url": item["image_url"],
                "bytes": item["bytes"],
                "expires_at": item["expires_at"],
            },
        })
    return events


def _credential_timeline_events(credentials: list[dict]) -> list[dict]:
    events: list[dict] = []
    for credential in credentials:
        updated_at = credential.get("updated_at")
        if not updated_at:
            continue
        events.append({
            "at": str(updated_at),
            "source": str(credential.get("source") or "credentials"),
            "type": "credential-discovered",
            "severity": "event",
            "summary": (
                f"{credential.get('label') or 'Credential'} available for "
                f"{credential.get('username') or 'account'}"
            ),
            "details": {
                "username": credential.get("username") or "",
                "vm_name": credential.get("vm_name") or "",
                "run_url": credential.get("run_url") or "",
            },
        })
    return events


def _identity_sync_payload(probe: dict | None, pve: dict | None) -> dict:
    probe = probe or {}
    pve = pve or {}
    return {
        "source": "monitoring_sweep",
        "last_checked_at": probe.get("checked_at") or pve.get("checked_at") or "",
        "ad_count": int(probe.get("ad_match_count") or len(_json_obj(probe.get("ad_matches_json"), []))),
        "entra_count": int(probe.get("entra_match_count") or len(_json_obj(probe.get("entra_matches_json"), []))),
        "intune_count": int(probe.get("intune_match_count") or len(_json_obj(probe.get("intune_matches_json"), []))),
    }


def _identity_sync_timeline_event(sync: dict) -> dict | None:
    checked_at = sync.get("last_checked_at")
    if not checked_at:
        return None
    return {
        "at": str(checked_at),
        "source": "monitoring",
        "type": "identity-sync",
        "severity": "event",
        "summary": (
            f"Evidence sync: AD {sync.get('ad_count', 0)}, "
            f"Entra {sync.get('entra_count', 0)}, Intune {sync.get('intune_count', 0)}"
        ),
        "details": dict(sync),
    }


async def _vm_detail_evidence_payload(vmid: int) -> dict:
    from web import device_regression

    latest_pair = device_history_db.latest_completed_pair_for_vmid(vmid)
    pve_dict = (latest_pair or {}).get("pve") or {}
    probe_dict = (latest_pair or {}).get("probe") or {}

    fleet_vm = None
    if not pve_dict:
        try:
            fleet = await _vms_fleet_payload()
            fleet_vm = next((vm for vm in fleet.get("vms") or [] if int(vm.get("vmid") or 0) == vmid), None)
            if fleet_vm:
                pve_dict = {
                    "vmid": vmid,
                    "name": fleet_vm.get("name") or "",
                    "status": fleet_vm.get("status") or "",
                    "checked_at": fleet_vm.get("monitor_checked_at") or "",
                }
        except Exception:
            fleet_vm = None
    else:
        try:
            fleet = await _vms_fleet_payload()
            fleet_vm = next((vm for vm in fleet.get("vms") or [] if int(vm.get("vmid") or 0) == vmid), None)
        except Exception:
            fleet_vm = None

    if not pve_dict and not probe_dict and not fleet_vm:
        raise HTTPException(404, f"no VM evidence for vmid {vmid}")

    history = device_history_db.history_for_vmid(
        vmid,
        limit=50,
        completed_only=True,
    )
    timeline = [
        _event_to_dict(event)
        for event in device_regression.build_timeline(
            list(reversed(history["pve_snapshots"])),
            list(reversed(history["device_probes"])),
        )
    ]
    known_credentials = _masked_known_credentials_for_vmid(vmid)
    identity_sync = _identity_sync_payload(probe_dict, pve_dict)
    timeline.extend(_screenshot_timeline_events(vmid))
    timeline.extend(_credential_timeline_events(known_credentials))
    sync_event = _identity_sync_timeline_event(identity_sync)
    if sync_event:
        timeline.append(sync_event)
    timeline.sort(key=lambda event: str(event.get("at") or ""), reverse=True)

    ad_matches = _json_obj(probe_dict.get("ad_matches_json"), [])
    entra_matches = _json_obj(probe_dict.get("entra_matches_json"), [])
    intune_matches = _json_obj(probe_dict.get("intune_matches_json"), [])
    return {
        "vmid": int(vmid),
        "fleet_vm": fleet_vm,
        "pve": pve_dict,
        "probe": probe_dict,
        "ad_matches": ad_matches,
        "entra_matches": entra_matches,
        "intune_matches": intune_matches,
        "linkage": _linkage_health(pve_dict, probe_dict),
        "known_credentials": known_credentials,
        "latest_screenshot": _latest_vm_screenshot(vmid),
        "screenshot_history": _vm_screenshot_history(vmid),
        "timeline": timeline,
        "history": history,
        "identity_sync": identity_sync,
    }


@app.get("/api/vms/{vmid}/detail", response_model=VmDetailEvidenceResponse)
async def api_vm_detail_evidence(vmid: int):
    return await _vm_detail_evidence_payload(vmid)


@app.get("/devices/{vmid}", response_class=HTMLResponse)
def page_device_detail(request: Request, vmid: int):
    from web import device_regression, monitoring_view
    latest_pair = device_history_db.latest_completed_pair_for_vmid(vmid)
    if latest_pair is None:
        raise HTTPException(404, f"no monitoring data for vmid {vmid}")

    pve_dict = latest_pair.get("pve")
    probe_dict = latest_pair.get("probe")

    history = device_history_db.history_for_vmid(
        vmid, limit=50, completed_only=True,
    )
    # Detector wants oldest-first for correct transition ordering.
    timeline = device_regression.build_timeline(
        list(reversed(history["pve_snapshots"])),
        list(reversed(history["device_probes"])),
    )

    linkage = _linkage_health(pve_dict or {}, probe_dict or {})
    ad_matches = _json_obj((probe_dict or {}).get("ad_matches_json"), [])
    entra_matches = _json_obj((probe_dict or {}).get("entra_matches_json"), [])
    intune_matches = _json_obj((probe_dict or {}).get("intune_matches_json"), [])
    known_credentials = _known_credentials_for_vmid(vmid)
    return templates.TemplateResponse("device_detail.html", {
        "request": request,
        "vmid": vmid,
        "pve": pve_dict,
        "probe": probe_dict,
        "ad_matches": ad_matches,
        "entra_matches": entra_matches,
        "intune_matches": intune_matches,
        "linkage": linkage,
        "known_credentials": known_credentials,
        "timeline": timeline,
        "history": history,
    })


@app.get("/monitoring/settings", response_class=HTMLResponse)
def page_monitoring_settings(request: Request):
    settings = device_history_db.get_settings()
    ous = device_history_db.list_search_ous()
    # Only credentials of type domain_join are useful for AD.
    all_creds = sequences_db.list_credentials(SEQUENCES_DB)
    domain_creds = [c for c in all_creds if c.get("type") == "domain_join"]
    keytab = device_history_db.get_keytab_health()
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
    return device_history_db.get_keytab_health() or {}


@app.post("/api/monitoring/sweep-now", status_code=202)
async def api_monitoring_sweep_now(background_tasks: BackgroundTasks):
    """Queue one monitor sweep using the same helper as the monitor container."""
    background_tasks.add_task(
        _run_monitor_sweep_and_refresh_vms_cache,
    )
    return {"ok": True, "queued": True}


@app.get("/api/monitoring/keytab/health")
def api_monitoring_keytab_health():
    return device_history_db.get_keytab_health() or {}


def _deployment_summary_for_react(summary: dict) -> dict:
    return {
        **summary,
        "running": int(summary.get("active") or 0),
        "succeeded": int(summary.get("completed") or 0),
    }


def _signal_tone(status: str) -> str:
    normalized = (status or "").casefold()
    if normalized in {"ok", "healthy", "ready", "complete", "completed"}:
        return "good"
    if normalized in {"running", "busy", "active", "pending", "queued"}:
        return "active"
    if normalized in {"failed", "error", "blocked", "missing", "stale", "mismatch", "unavailable", "degraded"}:
        return "bad"
    return "neutral"


def _signals_metric(label: str, value: str | int, tone: str = "neutral") -> dict:
    return {"label": label, "value": str(value), "tone": tone}


def _monitoring_lane_payload() -> tuple[list[dict], list[dict], dict]:
    from web import monitoring_view

    try:
        latest = device_history_db.latest_per_vmid()
        rows = monitoring_view.build_dashboard_rows(
            latest,
            ad_first_seen=_ad_first_seen_map(),
            now_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    except Exception:
        return [], [], {
            "total": 0,
            "running": 0,
            "guest_identity": 0,
            "ad_ok": 0,
            "entra_ok": 0,
            "entra_pending": 0,
            "intune_ok": 0,
            "attention": 0,
        }

    total = len(rows)
    counts = {
        "total": total,
        "running": sum(1 for row in rows if row.pve_status == "running"),
        "guest_identity": sum(1 for row in rows if row.win_name or row.serial),
        "ad_ok": sum(1 for row in rows if row.ad_badge == "ok"),
        "entra_ok": sum(1 for row in rows if row.entra_badge == "ok"),
        "entra_pending": sum(1 for row in rows if row.entra_badge == "pending"),
        "intune_ok": sum(1 for row in rows if row.intune_badge == "ok"),
        "attention": 0,
    }

    def _lane(label: str, value: int, detail: str, lane_id: str) -> dict:
        ready = total > 0 and value == total
        return {
            "id": lane_id,
            "label": label,
            "value": f"{value}/{total}",
            "detail": detail,
            "status": "ready" if ready else "attention",
            "tone": "good" if ready else "active",
        }

    lanes = [
        _lane("Provisioned", counts["running"], "Running in Proxmox and visible to the monitor.", "provisioned"),
        _lane("Guest identity", counts["guest_identity"], "Windows name or serial captured from the guest.", "guest-identity"),
        _lane("AD joined", counts["ad_ok"], "Single active computer object found in scope.", "ad-joined"),
        _lane("Entra synced", counts["entra_ok"], f"{counts['entra_pending']} pending inside the expected sync window.", "entra-synced"),
        _lane("Intune compliant", counts["intune_ok"], "Endpoint record exists and reports compliant.", "intune-compliant"),
    ]

    attention_rows = []
    for row in rows:
        ready = row.ad_badge == "ok" and row.entra_badge == "ok" and row.intune_badge == "ok"
        row_attention = (
            row.ad_badge in {"warn", "missing", "error"}
            or row.entra_badge in {"warn", "missing", "error"}
            or row.intune_badge in {"warn", "missing", "error"}
        )
        if not row_attention and ready:
            continue
        counts["attention"] += 1
        attention_rows.append({
            "vmid": row.vmid,
            "vm_name": row.vm_name or "(unnamed)",
            "node": row.node or "",
            "lifecycle": "Needs check" if row_attention else "In progress",
            "tone": "bad" if row_attention else "active",
            "pve_status": row.pve_status or "unknown",
            "windows": row.win_name or "unknown",
            "serial": row.serial or "unknown",
            "ad": row.ad_badge,
            "entra": row.entra_badge,
            "intune": row.intune_badge,
            "last_checked": row.last_checked or "",
            "href": f"/devices/{row.vmid}",
        })
    return lanes, attention_rows[:8], counts


def _signals_hub_payload() -> dict:
    runtime = _runtime_container_status()
    runtime_containers = runtime.get("containers") or []
    unhealthy_runtime = [
        row for row in runtime_containers
        if (row.get("health") and row.get("health") != "healthy")
        or row.get("status") not in {"running", "healthy"}
    ]

    table_jobs = _job_table_rows()
    running_payload = _running_jobs_payload()
    failed_jobs = [
        row for row in table_jobs
        if str(row.get("status") or "").casefold() in {"failed", "orphaned", "stuck", "regressed"}
    ]

    try:
        readiness = _setup_readiness()
    except Exception as exc:
        readiness = {
            "phase": "unknown",
            "health": "unavailable",
            "blocking_count": 0,
            "build_host": {},
            "artifacts": {},
            "media": {},
            "error": str(exc),
        }
    build_host = readiness.get("build_host") if isinstance(readiness.get("build_host"), dict) else {}
    artifacts = readiness.get("artifacts") if isinstance(readiness.get("artifacts"), dict) else {}
    media = readiness.get("media") if isinstance(readiness.get("media"), dict) else {}
    blocking_count = int(readiness.get("blocking_count") or 0)

    try:
        keytab = device_history_db.get_keytab_health() or {}
    except Exception:
        keytab = {}
    keytab_status = str(keytab.get("status") or keytab.get("last_probe_status") or "unknown")
    keytab_detail = str(
        keytab.get("detail")
        or keytab.get("message")
        or keytab.get("last_probe_message")
        or "keytab health not reported"
    )

    try:
        fleet = device_history_db.fleet_summary()
    except Exception:
        fleet = {"total": 0}

    try:
        from web import service_health_pg as service_health
        service_rows = service_health.list_services()
    except Exception:
        service_rows = []

    lifecycle_lanes, fleet_attention, lane_counts = _monitoring_lane_payload()

    try:
        from web import db_pg, deployment_health

        with db_pg.connection(_database_url()) as conn:
            deployment_payload = deployment_health.build_deployments_payload(conn, limit=25)
    except Exception:
        deployment_payload = {
            "summary": {"total": 0, "active": 0, "completed": 0, "failed": 0},
            "active": [],
            "recent_completions": [],
            "bottlenecks": [],
        }
    deployments = deployment_payload.get("summary") or {}
    deployments = _deployment_summary_for_react(deployments)

    osdeploy_ready = int(artifacts.get("osdeploy_ready_count") or 0)
    cloudosd_ready = int(artifacts.get("cloudosd_ready_count") or 0)
    artifacts_ready = bool(artifacts.get("ready"))
    windows_ready = bool(media.get("windows_iso_ready"))
    virtio_ready = bool(media.get("virtio_iso_ready"))

    build_host_state = str(build_host.get("agent_state") or ("ready" if build_host.get("agent_ready") else "missing"))
    active_work = build_host.get("active_work") if isinstance(build_host.get("active_work"), dict) else None
    if active_work and active_work.get("within_timeout"):
        build_host_status = "busy"
    elif build_host.get("agent_ready"):
        build_host_status = "ready"
    else:
        build_host_status = build_host_state

    unhealthy_services = [
        row for row in service_rows
        if str(row.get("status") or "").casefold() not in {"ok", "healthy", "ready"}
    ]
    deployment_attention = int(deployments.get("failed") or 0) + int(deployments.get("stuck") or 0) + int(deployments.get("regressed") or 0)

    signals = [
        {
            "id": "runtime",
            "family": "runtime",
            "label": "Runtime containers",
            "status": "degraded" if unhealthy_runtime or not runtime.get("available") else "healthy",
            "tone": "bad" if unhealthy_runtime or not runtime.get("available") else "good",
            "summary": (
                runtime.get("error")
                if not runtime.get("available")
                else f"{len(runtime_containers)} runtime services visible; {len(unhealthy_runtime)} need attention"
            ),
            "count": len(runtime_containers),
            "source": "/api/monitoring/runtime-services",
            "href": "/react/monitoring#runtime",
        },
        {
            "id": "services",
            "family": "service_health",
            "label": "Service health",
            "status": "degraded" if unhealthy_services else "healthy",
            "tone": "bad" if unhealthy_services else "good",
            "summary": f"{len(service_rows)} services reporting; {len(unhealthy_services)} degraded or dead",
            "count": len(service_rows),
            "source": "service health table",
            "href": "/react/monitoring#services",
        },
        {
            "id": "jobs",
            "family": "jobs",
            "label": "Jobs and live work",
            "status": "failed" if failed_jobs else ("running" if running_payload.get("running_count") else "ready"),
            "tone": "bad" if failed_jobs else ("active" if running_payload.get("running_count") else "good"),
            "summary": f"{running_payload.get('running_count', 0)} running, {running_payload.get('queued_count', 0)} queued, {len(failed_jobs)} failed or orphaned",
            "count": len(table_jobs),
            "source": "/api/jobs and /api/live/ws",
            "href": "/react/jobs",
        },
        {
            "id": "build-host",
            "family": "build_host",
            "label": "Build host",
            "status": build_host_status,
            "tone": _signal_tone(build_host_status),
            "summary": (
                f"{build_host.get('name') or 'build host'} on {build_host.get('node') or '-'}; "
                f"agent {build_host.get('expected_agent_id') or '-'}; heartbeat age {build_host.get('last_heartbeat_age_seconds') if build_host.get('last_heartbeat_age_seconds') is not None else '-'}s"
            ),
            "count": 1 if build_host.get("expected_agent_id") else 0,
            "source": "/api/setup/v1/readiness",
            "href": "/setup",
        },
        {
            "id": "artifacts",
            "family": "artifacts",
            "label": "Operational artifacts",
            "status": "ready" if artifacts_ready else "missing",
            "tone": "good" if artifacts_ready else "bad",
            "summary": f"{cloudosd_ready} CloudOSD and {osdeploy_ready} OSDeploy artifacts ready",
            "count": cloudosd_ready + osdeploy_ready,
            "source": "/api/setup/v1/readiness",
            "href": "/osdeploy" if osdeploy_ready else "/setup",
        },
        {
            "id": "deploy-readiness",
            "family": "deploy_readiness",
            "label": "Deploy readiness",
            "status": "blocked" if blocking_count else str(readiness.get("health") or "unknown"),
            "tone": "bad" if blocking_count else _signal_tone(str(readiness.get("health") or "")),
            "summary": f"phase {readiness.get('phase') or 'unknown'}; {blocking_count} blocked checks",
            "count": blocking_count,
            "source": "/api/setup/v1/readiness",
            "href": "/setup",
        },
        {
            "id": "deployment-speed",
            "family": "deployment_speed",
            "label": "Deployment speed",
            "status": "attention" if deployment_attention else ("running" if deployments.get("active") else "ready"),
            "tone": "bad" if deployment_attention else ("active" if deployments.get("active") else "good"),
            "summary": (
                f"{deployments.get('active', 0)} active, {deployments.get('stuck', 0)} stuck, "
                f"{deployments.get('failed', 0)} failed, p95 {deployments.get('p95_completion_seconds') or '-'}s"
            ),
            "count": deployments.get("total", 0),
            "source": "/api/monitoring/deployments/runs",
            "href": "/react/monitoring#deployment-speed",
        },
        {
            "id": "agent",
            "family": "agent",
            "label": "AutopilotAgent triage",
            "status": build_host_status,
            "tone": _signal_tone(build_host_status),
            "summary": (
                f"{build_host.get('computer_name') or build_host.get('expected_computer_name') or 'agent'} "
                f"version {build_host.get('agent_version') or '-'} at {build_host.get('primary_ipv4') or '-'}"
            ),
            "count": 1 if build_host.get("expected_agent_id") else 0,
            "source": "setup readiness and agent telemetry",
            "href": "/react/vms",
        },
        {
            "id": "lifecycle",
            "family": "lifecycle",
            "label": "Lifecycle lanes",
            "status": "attention" if lane_counts["attention"] else "ready",
            "tone": "bad" if lane_counts["attention"] else "good",
            "summary": (
                f"{lane_counts['running']}/{lane_counts['total']} running; "
                f"{lane_counts['ad_ok']} AD, {lane_counts['entra_ok']} Entra, {lane_counts['intune_ok']} Intune ready"
            ),
            "count": lane_counts["attention"],
            "source": "/monitoring",
            "href": "/react/monitoring#lifecycle",
        },
        {
            "id": "identity",
            "family": "identity",
            "label": "AD, auth, and keytab",
            "status": keytab_status,
            "tone": _signal_tone(keytab_status),
            "summary": keytab_detail,
            "count": keytab_status,
            "source": "/api/monitoring/keytab/health",
            "href": "/monitoring/settings",
        },
        {
            "id": "fleet-evidence",
            "family": "fleet_evidence",
            "label": "Fleet evidence",
            "status": "ready" if int(fleet.get("total") or 0) else "empty",
            "tone": "good" if int(fleet.get("total") or 0) else "neutral",
            "summary": f"{lane_counts['total'] or int(fleet.get('total') or 0)} devices in latest sweep; {len(fleet_attention)} need review",
            "count": int(fleet.get("total") or 0),
            "source": "/api/fleet/summary",
            "href": "/devices",
        },
    ]

    operator_paths = []
    if not (windows_ready and virtio_ready):
        operator_paths.append({
            "id": "stage-bootstrap-media",
            "priority": 10,
            "label": "Stage Windows ISO and VirtIO media",
            "status": "blocked",
            "tone": "bad",
            "summary": "Bootstrap recovery media is not staged; fix this before rebuilding the build host.",
            "action_label": "Open setup",
            "href": "/setup",
            "source": "/api/setup/v1/media",
        })
    if failed_jobs:
        operator_paths.append({
            "id": "review-failed-jobs",
            "priority": 15,
            "label": "Review failed or orphaned jobs",
            "status": "failed",
            "tone": "bad",
            "summary": f"{len(failed_jobs)} jobs need operator review before more work is started.",
            "action_label": "Open jobs",
            "href": "/react/jobs",
            "source": "/api/jobs",
        })
    if deployment_attention:
        operator_paths.append({
            "id": "review-deployment-speed",
            "priority": 18,
            "label": "Review deployment timing regressions",
            "status": "attention",
            "tone": "bad",
            "summary": f"{deployment_attention} deployment timing signals are failed, stuck, or regressed.",
            "action_label": "Open legacy monitoring",
            "href": "/monitoring",
            "source": "/api/monitoring/deployments/runs",
        })
    if fleet_attention:
        operator_paths.append({
            "id": "review-fleet-lifecycle",
            "priority": 22,
            "label": "Review fleet lifecycle drift",
            "status": "attention",
            "tone": "bad",
            "summary": f"{len(fleet_attention)} devices need AD, Entra, Intune, or guest identity review.",
            "action_label": "Open devices",
            "href": "/devices",
            "source": "/monitoring",
        })
    if build_host_status in {"stale", "missing", "mismatch"}:
        operator_paths.append({
            "id": "repair-build-host-agent",
            "priority": 20,
            "label": "Repair build host agent",
            "status": build_host_status,
            "tone": "bad",
            "summary": "The build-host agent is not fresh; repair only after checking active work.",
            "action_label": "Open setup",
            "href": "/setup",
            "source": "/api/setup/v1/readiness",
        })
    elif build_host_status == "busy":
        operator_paths.append({
            "id": "watch-build-host",
            "priority": 25,
            "label": "Build host is busy",
            "status": "running",
            "tone": "active",
            "summary": "Active work is within timeout; watch the job instead of repairing the agent.",
            "action_label": "Watch jobs",
            "href": "/react/jobs",
            "source": "/api/setup/v1/readiness",
        })
    elif build_host_status == "ready":
        operator_paths.append({
            "id": "queue-build-host-work",
            "priority": 35,
            "label": "Build host agent is fresh and idle",
            "status": "ready",
            "tone": "good",
            "summary": "The build host can accept build workloads when new artifacts are needed.",
            "action_label": "Open setup",
            "href": "/setup",
            "source": "/api/setup/v1/readiness",
        })
    if osdeploy_ready:
        operator_paths.append({
            "id": "server-deploy-ready",
            "priority": 40,
            "label": "Windows Server OSDeploy artifact is available",
            "status": "ready",
            "tone": "good",
            "summary": "Open the existing OSDeploy execution flow with the promoted artifact ready.",
            "action_label": "Open server deploy",
            "href": "/osdeploy",
            "source": "/api/setup/v1/readiness",
        })
    if cloudosd_ready:
        operator_paths.append({
            "id": "desktop-deploy-ready",
            "priority": 45,
            "label": "CloudOSD desktop artifacts are available",
            "status": "ready",
            "tone": "good",
            "summary": "Desktop deployment can start from the existing CloudOSD flow.",
            "action_label": "Open desktop deploy",
            "href": "/cloudosd",
            "source": "/api/setup/v1/readiness",
        })
    if int(deployments.get("succeeded") or 0):
        operator_paths.append({
            "id": "review-fleet-evidence",
            "priority": 60,
            "label": "Review fleet evidence",
            "status": "ready",
            "tone": "good",
            "summary": "Confirm VM, device, hash, and artifact records after completed deployments.",
            "action_label": "Open devices",
            "href": "/devices",
            "source": "/api/fleet/summary",
        })
    operator_paths.sort(key=lambda item: (int(item.get("priority") or 100), str(item.get("label") or "")))

    bad_count = sum(1 for signal in signals if signal["tone"] == "bad")
    action_count = sum(1 for signal in signals if signal["tone"] in {"bad", "active"})
    ready_count = sum(1 for signal in signals if signal["tone"] == "good")

    return {
        "generated_at": utc_now_iso(),
        "build": _APP_VERSION,
        "source_health": {
            "runtime_available": bool(runtime.get("available")),
            "setup_health": str(readiness.get("health") or "unknown"),
            "keytab_status": keytab_status,
        },
        "metrics": [
            _signals_metric("Critical", bad_count, "bad" if bad_count else "good"),
            _signals_metric("Needs operator", action_count, "bad" if action_count else "good"),
            _signals_metric("Ready", ready_count, "good" if ready_count else "neutral"),
            _signals_metric("Runtime", "up" if runtime.get("available") else "down", "good" if runtime.get("available") else "bad"),
            _signals_metric("Fleet attention", len(fleet_attention), "bad" if fleet_attention else "good"),
        ],
        "signals": signals,
        "operator_paths": operator_paths,
        "lifecycle_lanes": lifecycle_lanes,
        "deployment_health": {
            "summary": deployments,
            "active": (deployment_payload.get("active") or [])[:6],
            "recent_completions": (deployment_payload.get("recent_completions") or [])[:6],
            "bottlenecks": (deployment_payload.get("bottlenecks") or [])[:6],
        },
        "services": service_rows,
        "runtime": runtime,
        "fleet_attention": fleet_attention,
    }


@app.get("/api/monitoring/deployments/summary", response_model=DeploymentSummaryResponse)
def api_monitoring_deployments_summary():
    from web import db_pg, deployment_health

    with db_pg.connection(_database_url()) as conn:
        summary = deployment_health.build_deployments_payload(conn, limit=1)["summary"]
    return _deployment_summary_for_react(summary)


@app.get("/api/monitoring/signals", response_model=SignalsHubResponse)
def api_monitoring_signals():
    return _signals_hub_payload()


@app.get("/api/monitoring/deployments/runs")
def api_monitoring_deployments_runs(limit: int = 100):
    from web import db_pg, deployment_health

    with db_pg.connection(_database_url()) as conn:
        payload = deployment_health.build_deployments_payload(conn, limit=limit)
    return {
        "schema_version": payload["schema_version"],
        "summary": payload["summary"],
        "runs": payload["runs"],
    }


@app.get("/api/monitoring/deployments/runs/{deployment_key:path}")
def api_monitoring_deployments_run_detail(deployment_key: str):
    from web import db_pg, deployment_health

    with db_pg.connection(_database_url()) as conn:
        detail = deployment_health.build_deployment_detail(conn, deployment_key)
    if detail["state"] == "missing":
        raise HTTPException(404, f"deployment run not found: {deployment_key}")
    return detail


@app.get("/api/monitoring/deployments/baselines")
def api_monitoring_deployments_baselines():
    from web import db_pg, deployment_health

    with db_pg.connection(_database_url()) as conn:
        return deployment_health.build_baselines_payload(conn)
