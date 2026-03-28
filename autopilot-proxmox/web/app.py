import asyncio
import os
import urllib3
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from fastapi import FastAPI, Form, WebSocket, WebSocketDisconnect
from fastapi.requests import Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from web.jobs import JobManager

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
    # Use yaml.dump for safe string escaping
    return yaml.dump(value, default_flow_style=True).strip()


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
    result = []
    for vm in vms:
        if vm.get("template"):
            continue
        name = vm.get("name", "")
        if "autopilot" in name.lower():
            # Fetch VM config for SMBIOS info
            serial = ""
            oem = ""
            try:
                config = _proxmox_api(f"/nodes/{node}/qemu/{vm['vmid']}/config")
                smbios1 = config.get("smbios1", "")
                serial = _decode_smbios_serial(smbios1)
                manufacturer = _decode_smbios_field(smbios1, "manufacturer")
                product = _decode_smbios_field(smbios1, "product")
                oem = f"{manufacturer} {product}".strip()
            except Exception:
                pass
            result.append({
                "vmid": vm["vmid"],
                "name": name,
                "status": vm.get("status", "unknown"),
                "serial": serial,
                "oem": oem,
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


def get_autopilot_devices():
    try:
        data = _graph_api(
            "/deviceManagement/windowsAutopilotDeviceIdentities"
        )
    except Exception as e:
        return [], str(e)
    if not data:
        return [], "No Entra credentials configured"
    devices = []
    for d in data.get("value", []):
        profile_status = d.get("deploymentProfileAssignmentStatus", "unknown")
        devices.append({
            "id": d.get("id", ""),
            "serial": d.get("serialNumber", ""),
            "group_tag": d.get("groupTag", ""),
            "profile_status": profile_status,
            "profile_ok": profile_status in ("assigned", "assignedUnkownSyncState"),
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


@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    return templates.TemplateResponse("upload.html", {
        "request": request,
        "hash_files": get_hash_files(),
    })


@app.get("/hashes", response_class=HTMLResponse)
async def hashes_page(request: Request):
    return templates.TemplateResponse("hashes.html", {
        "request": request,
        "hash_files": get_hash_files(),
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
async def vms_page(request: Request):
    vms = get_autopilot_vms()
    vm_serials = {vm["serial"] for vm in vms if vm.get("serial")}
    devices, ap_error = get_autopilot_devices()
    hash_serials = {f["serial"] for f in get_hash_files()}
    ap_serials = {d["serial"] for d in devices}

    # Only show Autopilot devices that match a Proxmox VM serial
    matched_devices = [d for d in devices if d["serial"] in vm_serials]
    for d in matched_devices:
        d["has_local_hash"] = d["serial"] in hash_serials

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
    })


# --- API Endpoints ---

@app.post("/api/jobs/provision")
async def start_provision(
    profile: str = Form(...),
    count: int = Form(1),
    group_tag: str = Form(""),
):
    profile = _sanitize_input(profile)
    if group_tag:
        group_tag = _sanitize_input(group_tag)

    if count <= 1:
        cmd = [
            "ansible-playbook", str(PLAYBOOK_DIR / "provision_clone.yml"),
            "-e", f"vm_oem_profile={profile}",
            "-e", "vm_count=1",
        ]
        if group_tag:
            cmd += ["-e", f"vm_group_tag={group_tag}"]
        args = {"profile": profile, "count": 1, "group_tag": group_tag}
        job = job_manager.start("provision_clone", cmd, args=args)
        return RedirectResponse(f"/jobs/{job['id']}", status_code=303)

    # Multiple VMs — run sequentially to avoid VMID race condition
    import tempfile
    playbook = shlex.quote(str(PLAYBOOK_DIR / "provision_clone.yml"))
    safe_profile = shlex.quote(profile)
    safe_tag = shlex.quote(group_tag) if group_tag else ""

    script_lines = ["#!/bin/bash", "set -e", ""]
    script_lines.append(f"echo 'Provisioning {count} VMs sequentially ({profile})'")

    for i in range(count):
        script_lines.append(f"echo '=== VM {i+1}/{count} ==='")
        cmd_line = f"ansible-playbook {playbook} -e vm_oem_profile={safe_profile} -e vm_count=1"
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

    args = {"profile": profile, "count": count, "group_tag": group_tag}
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
    """Redirect to Proxmox noVNC console for this VM."""
    cfg = _load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    node = cfg.get("proxmox_node", "pve")
    novnc_url = f"https://{host}:{port}/?console=kvm&novnc=1&vmid={vmid}&node={node}"
    return RedirectResponse(novnc_url)


@app.post("/api/jobs/capture-and-upload")
async def start_capture_and_upload(
    missing_vmids: list[str] = Form(...),
    group_tag: str = Form(""),
):
    """Capture hashes for selected VMs then upload all to Intune."""
    if group_tag:
        group_tag = _sanitize_input(group_tag)

    vm_list = []
    for entry in missing_vmids:
        vmid, name = entry.split(":", 1)
        _sanitize_input(vmid)
        _sanitize_input(name)
        vm_list.append({"vmid": vmid, "name": name})

    playbook = shlex.quote(str(PLAYBOOK_DIR / "retry_inject_hash.yml"))
    upload_playbook = shlex.quote(str(PLAYBOOK_DIR / "upload_hashes.yml"))
    safe_tag = shlex.quote(group_tag) if group_tag else ""

    script_lines = ["#!/bin/bash", "set -e"]
    for vm in vm_list:
        script_lines.append(f"echo '=== Capturing hash for {vm['name']} (VMID {vm['vmid']}) ==='")
        cmd = f"ansible-playbook {playbook} -e vm_vmid={shlex.quote(vm['vmid'])} -e vm_name={shlex.quote(vm['name'])} -e autopilot_skip=true"
        if safe_tag:
            cmd += f" -e vm_group_tag={safe_tag}"
        script_lines.append(cmd)

    script_lines.append("echo '=== Uploading all hashes to Intune ==='")
    script_lines.append(f"ansible-playbook {upload_playbook}")

    script_content = "\n".join(script_lines)
    import tempfile
    fd, script_file = tempfile.mkstemp(suffix=".sh", dir=str(BASE_DIR / "jobs"))
    os.close(fd)
    script_path = Path(script_file)
    script_path.write_text(script_content)
    script_path.chmod(0o755)

    args = {"vms": [v["name"] for v in vm_list], "group_tag": group_tag, "upload": True}
    job = job_manager.start("capture_and_upload", ["bash", str(script_path)], args=args)
    return RedirectResponse(f"/jobs/{job['id']}", status_code=303)


@app.post("/api/jobs/bulk-capture")
async def start_bulk_capture(
    vmids: list[str] = Form(...),
    group_tag: str = Form(""),
):
    """Capture hashes for multiple VMs sequentially in one job."""
    if group_tag:
        group_tag = _sanitize_input(group_tag)

    vm_list = []
    for entry in vmids:
        vmid, name = entry.split(":", 1)
        _sanitize_input(vmid)
        _sanitize_input(name)
        vm_list.append({"vmid": vmid, "name": name})

    playbook = shlex.quote(str(PLAYBOOK_DIR / "retry_inject_hash.yml"))
    safe_tag = shlex.quote(group_tag) if group_tag else ""

    script_lines = ["#!/bin/bash", "set -e"]
    for vm in vm_list:
        script_lines.append(f"echo '=== Capturing hash for {vm['name']} (VMID {vm['vmid']}) ==='")
        cmd = f"ansible-playbook {playbook} -e vm_vmid={shlex.quote(vm['vmid'])} -e vm_name={shlex.quote(vm['name'])} -e autopilot_skip=true"
        if safe_tag:
            cmd += f" -e vm_group_tag={safe_tag}"
        script_lines.append(cmd)

    script_content = "\n".join(script_lines)
    import tempfile
    fd, script_file = tempfile.mkstemp(suffix=".sh", dir=str(BASE_DIR / "jobs"))
    os.close(fd)
    script_path = Path(script_file)
    script_path.write_text(script_content)
    script_path.chmod(0o755)

    args = {"vms": [v["name"] for v in vm_list], "group_tag": group_tag}
    job = job_manager.start("bulk_hash_capture", ["bash", str(script_path)], args=args)
    return RedirectResponse(f"/jobs/{job['id']}", status_code=303)


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
async def start_upload():
    cmd = [
        "ansible-playbook", str(PLAYBOOK_DIR / "upload_hashes.yml"),
    ]
    job = job_manager.start("upload_hashes", cmd)
    return RedirectResponse(f"/jobs/{job['id']}", status_code=303)


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
