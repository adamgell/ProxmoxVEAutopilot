import asyncio
import os
import urllib3
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.requests import Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from web.jobs import JobManager
from web import devices_db

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import re
import shlex

BASE_DIR = Path(__file__).resolve().parent.parent


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
DEVICES_DB = BASE_DIR / "output" / "devices.db"
devices_db.init(DEVICES_DB)

SETTINGS_SCHEMA = [
    {"section": "Proxmox Connection", "fields": [
        {"key": "proxmox_host", "label": "Host", "type": "text"},
        {"key": "proxmox_port", "label": "Port", "type": "number"},
        {"key": "proxmox_node", "label": "Node", "type": "text"},
        {"key": "proxmox_validate_certs", "label": "Validate Certs", "type": "bool"},
    ]},
    {"section": "Storage & Networking", "fields": [
        {"key": "proxmox_storage", "label": "VM Storage", "type": "text"},
        {"key": "proxmox_iso_storage", "label": "ISO Storage", "type": "text"},
        {"key": "proxmox_bridge", "label": "Network Bridge", "type": "text"},
        {"key": "proxmox_vlan_tag", "label": "VLAN Tag", "type": "text"},
    ]},
    {"section": "ISO Paths", "fields": [
        {"key": "proxmox_windows_iso", "label": "Windows ISO", "type": "text"},
        {"key": "proxmox_virtio_iso", "label": "VirtIO ISO", "type": "text"},
        {"key": "proxmox_answer_iso", "label": "Answer ISO", "type": "text"},
    ]},
    {"section": "Template", "fields": [
        {"key": "proxmox_template_vmid", "label": "Template VMID", "type": "number"},
    ]},
    {"section": "VM Defaults", "fields": [
        {"key": "vm_cores", "label": "CPU Cores", "type": "number"},
        {"key": "vm_memory_mb", "label": "Memory (MB)", "type": "number"},
        {"key": "vm_disk_size_gb", "label": "Disk Size (GB)", "type": "number"},
        {"key": "vm_ostype", "label": "OS Type", "type": "text"},
        {"key": "vm_count", "label": "Default VM Count", "type": "number"},
        {"key": "vm_name_prefix", "label": "Name Prefix", "type": "text"},
        {"key": "vm_start_after_create", "label": "Start After Create", "type": "bool"},
        {"key": "vm_oem_profile", "label": "Default OEM Profile", "type": "text"},
        {"key": "vm_serial_prefix", "label": "Serial Prefix", "type": "text"},
        {"key": "vm_custom_serial", "label": "Default Serial", "type": "text"},
        {"key": "vm_group_tag", "label": "Default Group Tag", "type": "text"},
    ]},
    {"section": "Autopilot", "fields": [
        {"key": "autopilot_skip", "label": "Skip Autopilot Inject", "type": "bool"},
        {"key": "capture_hardware_hash", "label": "Capture Hash", "type": "bool"},
    ]},
    {"section": "Timeouts", "fields": [
        {"key": "guest_agent_timeout_seconds", "label": "Guest Agent Timeout (s)", "type": "number"},
        {"key": "guest_agent_poll_interval_seconds", "label": "Guest Agent Poll (s)", "type": "number"},
        {"key": "guest_exec_timeout_seconds", "label": "Guest Exec Timeout (s)", "type": "number"},
        {"key": "guest_exec_poll_interval_seconds", "label": "Guest Exec Poll (s)", "type": "number"},
        {"key": "hash_capture_timeout_seconds", "label": "Hash Capture Timeout (s)", "type": "number"},
        {"key": "task_poll_retries", "label": "Task Poll Retries", "type": "number"},
        {"key": "task_poll_delay_seconds", "label": "Task Poll Delay (s)", "type": "number"},
    ]},
]


def _load_vars():
    """Load vars.yml values only (not vault)."""
    if VARS_PATH.exists():
        with open(VARS_PATH) as f:
            data = yaml.safe_load(f)
        return data or {}
    return {}


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
    return f'"{s}"' if needs_quotes else s


def _save_vars(updates):
    """Update vars.yml by line-level replacement to preserve comments."""
    if not VARS_PATH.exists():
        VARS_PATH.parent.mkdir(parents=True, exist_ok=True)
        VARS_PATH.write_text("---\n")

    lines = VARS_PATH.read_text().splitlines()
    matched_keys = set()
    new_lines = []
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

    # Append keys not already in the file
    for key, value in updates.items():
        if key not in matched_keys:
            new_lines.append(f"{key}: {_format_yaml_value(value)}")

    VARS_PATH.write_text("\n".join(new_lines) + "\n")

app = FastAPI(title="Proxmox VE Autopilot")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
job_manager = JobManager(jobs_dir=str(BASE_DIR / "jobs"))


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


def _proxmox_api(path):
    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")
    url = f"https://{host}:{port}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
    resp = requests.get(url, headers=headers, verify=False, timeout=10)
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

    result = []
    for vm in autopilot_vms:
        config = configs.get(vm["vmid"], {})
        smbios1 = config.get("smbios1", "")
        serial = _decode_smbios_serial(smbios1) if smbios1 else ""
        manufacturer = _decode_smbios_field(smbios1, "manufacturer") if smbios1 else ""
        product = _decode_smbios_field(smbios1, "product") if smbios1 else ""
        oem = f"{manufacturer} {product}".strip()
        result.append({
            "vmid": vm["vmid"],
            "name": vm.get("name", ""),
            "status": vm.get("status", "unknown"),
            "serial": serial,
            "oem": oem,
            "hostname": hostnames.get(vm["vmid"], ""),
            "mem_mb": int(vm.get("maxmem", 0) / 1024 / 1024),
            "cpus": vm.get("cpus", vm.get("maxcpu", "")),
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
            "modified": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M"),
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
    jobs = job_manager.list_jobs()
    running = sum(1 for j in jobs if j["status"] == "running")
    return templates.TemplateResponse("home.html", {
        "request": request,
        "running_count": running,
        "hash_count": len(get_hash_files()),
        "total_jobs": len(jobs),
    })


@app.get("/provision", response_class=HTMLResponse)
async def provision_page(request: Request):
    return templates.TemplateResponse("provision.html", {
        "request": request,
        "profiles": load_oem_profiles(),
    })


@app.get("/template", response_class=HTMLResponse)
async def template_page(request: Request):
    return templates.TemplateResponse("template.html", {
        "request": request,
        "profiles": load_oem_profiles(),
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
    return templates.TemplateResponse("jobs.html", {
        "request": request,
        "jobs": jobs,
    })


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail_page(request: Request, job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        return HTMLResponse("<h1>Job not found</h1>", status_code=404)
    return templates.TemplateResponse("job_detail.html", {
        "request": request,
        "job": job,
        "log_content": job_manager.get_log(job_id),
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str = ""):
    current = _load_vars()
    options = _fetch_settings_options()
    # Build schema with current values, readonly flag, and dropdown options
    sections = []
    for group in SETTINGS_SCHEMA:
        fields = []
        for f in group["fields"]:
            val = current.get(f["key"], "")
            is_template = isinstance(val, str) and "{{" in str(val)
            field_options = options.get(f["key"], [])
            labels = options.get(f"{f['key']}_labels", {})
            fields.append({
                **f,
                "value": val,
                "readonly": is_template,
                "options": field_options,
                "labels": labels,
            })
        sections.append({"section": group["section"], "fields": fields})
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "sections": sections,
        "saved": saved == "1",
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
    current = _load_vars()
    updates = {}
    for group in SETTINGS_SCHEMA:
        for f in group["fields"]:
            key = f["key"]
            # Skip Jinja2 template values
            cur_val = current.get(key, "")
            if isinstance(cur_val, str) and "{{" in str(cur_val):
                continue
            if f["type"] == "bool":
                updates[key] = key in form
            elif f["type"] == "number":
                raw = form.get(key, "")
                if raw == "" or raw == "null":
                    updates[key] = None
                else:
                    try:
                        updates[key] = int(raw)
                    except ValueError:
                        updates[key] = raw
            else:
                val = form.get(key, "")
                updates[key] = val if val != "null" and val != "" else None
    _save_vars(updates)
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/vms", response_class=HTMLResponse)
async def vms_page(request: Request, error: str = ""):
    vms = get_autopilot_vms()
    vm_serials = {vm["serial"] for vm in vms if vm.get("serial")}
    devices, ap_error = get_autopilot_devices()
    hash_serials = {f["serial"] for f in get_hash_files()}
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

    # Tag VMs with their Autopilot status
    for vm in vms:
        vm["in_autopilot"] = vm.get("serial", "") in ap_serials
        vm["has_hash"] = vm.get("serial", "") in hash_serials

    # VMs not yet in Autopilot (missing)
    missing_vms = [vm for vm in vms if not vm["in_autopilot"] and vm.get("serial")]

    return templates.TemplateResponse("vms.html", {
        "request": request,
        "vms": vms,
        "devices": matched_devices,
        "missing_vms": missing_vms,
        "ap_error": ap_error,
        "error": error,
    })


# --- API Endpoints ---

@app.post("/api/jobs/provision")
async def start_provision(
    profile: str = Form(...),
    count: int = Form(1),
    serial_prefix: str = Form(""),
    group_tag: str = Form(""),
):
    profile = _sanitize_input(profile)
    if group_tag:
        group_tag = _sanitize_input(group_tag)
    if serial_prefix:
        serial_prefix = _sanitize_input(serial_prefix)

    if count <= 1:
        cmd = [
            "ansible-playbook", str(PLAYBOOK_DIR / "provision_clone.yml"),
            "-e", f"vm_oem_profile={profile}",
            "-e", "vm_count=1",
        ]
        if serial_prefix:
            cmd += ["-e", f"vm_serial_prefix={serial_prefix}"]
        if group_tag:
            cmd += ["-e", f"vm_group_tag={group_tag}"]
        args = {"profile": profile, "count": 1, "serial_prefix": serial_prefix, "group_tag": group_tag}
        job = job_manager.start("provision_clone", cmd, args=args)
        return RedirectResponse(f"/jobs/{job['id']}", status_code=303)

    # Multiple VMs — run sequentially to avoid VMID race condition
    import tempfile
    playbook = shlex.quote(str(PLAYBOOK_DIR / "provision_clone.yml"))
    safe_profile = shlex.quote(profile)
    safe_prefix = shlex.quote(serial_prefix) if serial_prefix else ""
    safe_tag = shlex.quote(group_tag) if group_tag else ""

    script_lines = ["#!/bin/bash", "set -e", ""]
    script_lines.append(f"echo 'Provisioning {count} VMs sequentially ({profile})'")

    for i in range(count):
        script_lines.append(f"echo '=== VM {i+1}/{count} ==='")
        cmd_line = f"ansible-playbook {playbook} -e vm_oem_profile={safe_profile} -e vm_count=1"
        if safe_prefix:
            cmd_line += f" -e vm_serial_prefix={safe_prefix}"
        if safe_tag:
            cmd_line += f" -e vm_group_tag={safe_tag}"
        script_lines.append(cmd_line)

    script_lines.append(f"echo '=== Done: {count} VMs provisioned ==='")

    script_content = "\n".join(script_lines)
    fd, script_file = tempfile.mkstemp(suffix=".sh", dir=str(BASE_DIR / "jobs"))
    os.close(fd)
    script_path = Path(script_file)
    script_path.write_text(script_content)
    script_path.chmod(0o755)

    args = {"profile": profile, "count": count, "serial_prefix": serial_prefix, "group_tag": group_tag}
    job = job_manager.start("provision_clone", ["bash", str(script_path)], args=args)
    return RedirectResponse(f"/jobs/{job['id']}", status_code=303)


@app.post("/api/jobs/template")
async def start_template(profile: str = Form(...)):
    profile = _sanitize_input(profile)
    cmd = [
        "ansible-playbook", str(PLAYBOOK_DIR / "build_template.yml"),
        "-e", f"vm_oem_profile={profile}",
    ]
    args = {"profile": profile}
    job = job_manager.start("build_template", cmd, args=args)
    return RedirectResponse(f"/jobs/{job['id']}", status_code=303)


@app.get("/api/vms/{vmid}/console")
async def vm_console(vmid: int):
    """Redirect to Proxmox noVNC console (requires active Proxmox login)."""
    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    node = cfg.get("proxmox_node", "pve")
    novnc_url = f"https://{host}:{port}/?console=kvm&novnc=1&vmid={vmid}&node={node}"
    return RedirectResponse(novnc_url)


@app.post("/api/vms/{vmid}/start")
async def vm_start(vmid: int):
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/start")
    except Exception as e:
        return RedirectResponse(f"/vms?error=Start failed: {e}", status_code=303)
    return RedirectResponse("/vms", status_code=303)


@app.post("/api/vms/{vmid}/shutdown")
async def vm_shutdown(vmid: int):
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/shutdown")
    except Exception as e:
        return RedirectResponse(f"/vms?error=Shutdown failed: {e}", status_code=303)
    return RedirectResponse("/vms", status_code=303)


@app.post("/api/vms/{vmid}/stop")
async def vm_stop(vmid: int):
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/stop")
    except Exception as e:
        return RedirectResponse(f"/vms?error=Force stop failed: {e}", status_code=303)
    return RedirectResponse("/vms", status_code=303)


@app.post("/api/vms/{vmid}/reset")
async def vm_reset(vmid: int):
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/status/reset")
    except Exception as e:
        return RedirectResponse(f"/vms?error=Reset failed: {e}", status_code=303)
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
        return RedirectResponse(f"/vms?error=Delete failed: {e}", status_code=303)
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
        return RedirectResponse(f"/vms?error=Type failed: {errors[0]}", status_code=303)
    return RedirectResponse("/vms", status_code=303)


@app.post("/api/vms/{vmid}/sendkey")
async def vm_sendkey(vmid: int, key: str = Form(...)):
    """Send a single key combo to a VM (e.g. ctrl-alt-del, ret, tab)."""
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        _proxmox_api_put(f"/nodes/{node}/qemu/{vmid}/sendkey", data={"key": key})
    except Exception as e:
        return RedirectResponse(f"/vms?error=Sendkey failed: {e}", status_code=303)
    return RedirectResponse("/vms", status_code=303)


@app.post("/api/vms/{vmid}/rename")
async def vm_rename(vmid: int):
    """Rename the Windows computer inside the VM to match its SMBIOS serial."""
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        # Get the serial from the VM config
        config = _proxmox_api(f"/nodes/{node}/qemu/{vmid}/config")
        smbios1 = config.get("smbios1", "") if isinstance(config, dict) else ""
        serial = _decode_smbios_serial(smbios1)
        if not serial:
            return RedirectResponse(f"/vms?error=VM {vmid} has no serial number configured", status_code=303)
        # Windows hostnames max 15 chars, no special chars
        hostname = re.sub(r'[^A-Za-z0-9\-]', '', serial)[:15]
        if not hostname:
            return RedirectResponse(f"/vms?error=Serial '{serial}' produces invalid hostname", status_code=303)
        # Update Proxmox VM name to include the serial
        pve_name = re.sub(r'[^A-Za-z0-9\-]', '', serial)
        _proxmox_api_put(f"/nodes/{node}/qemu/{vmid}/config", data={"name": pve_name})
        # Execute rename via guest agent
        ps_cmd = f"Rename-Computer -NewName '{hostname}' -Force"
        _proxmox_api_post(f"/nodes/{node}/qemu/{vmid}/agent/exec", data={
            "command": "powershell.exe",
            "input-data": ps_cmd,
        })
    except Exception as e:
        return RedirectResponse(f"/vms?error=Rename failed: {e}", status_code=303)
    return RedirectResponse(f"/vms?error=Renamed VM {vmid} to {hostname} — restart required to apply", status_code=303)


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
    killed = job_manager.kill(job_id)
    if not killed:
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


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
        return RedirectResponse("/hashes?error=No+valid+CSV+files+found", status_code=303)
    return RedirectResponse(f"/hashes?uploaded={saved}", status_code=303)


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


def _find_pve_vm_by_serial(serial: str) -> dict | None:
    """Return the Proxmox VM whose name matches `serial` and carries the
    'autopilot' tag. Returns None if no match — refusing to touch VMs that
    weren't provisioned by us guards against name collisions."""
    if not serial:
        return None
    cfg = _load_proxmox_config()
    node = cfg.get("proxmox_node", "pve")
    try:
        vms = _proxmox_api(f"/nodes/{node}/qemu")
    except Exception:
        return None
    for vm in vms:
        if vm.get("template"):
            continue
        if vm.get("name") != serial:
            continue
        tags = (vm.get("tags") or "").split(";")
        if "autopilot" not in tags:
            continue
        return {"vmid": vm["vmid"], "name": vm.get("name", ""), "status": vm.get("status", "")}
    return None


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
        return RedirectResponse(f"/cloud?error={str(e)[:200]}", status_code=303)
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
