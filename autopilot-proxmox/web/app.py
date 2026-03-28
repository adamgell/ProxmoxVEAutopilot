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

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
HASH_DIR = BASE_DIR / "output" / "hashes"
PLAYBOOK_DIR = BASE_DIR / "playbooks"
FILES_DIR = BASE_DIR / "files"

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
            result.append({
                "vmid": vm["vmid"],
                "name": name,
                "status": vm.get("status", "unknown"),
                "mem_mb": int(vm.get("maxmem", 0) / 1024 / 1024),
                "cpus": vm.get("cpus", vm.get("maxcpu", "")),
            })
    return sorted(result, key=lambda v: v["vmid"])


def load_oem_profiles():
    profiles_path = FILES_DIR / "oem_profiles.yml"
    with open(profiles_path) as f:
        data = yaml.safe_load(f)
    return data.get("oem_profiles", {})


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


@app.get("/vms", response_class=HTMLResponse)
async def vms_page(request: Request):
    return templates.TemplateResponse("vms.html", {
        "request": request,
        "vms": get_autopilot_vms(),
    })


# --- API Endpoints ---

@app.post("/api/jobs/provision")
async def start_provision(
    profile: str = Form(...),
    count: int = Form(1),
    group_tag: str = Form(""),
):
    cmd = [
        "ansible-playbook", str(PLAYBOOK_DIR / "provision_clone.yml"),
        "-e", f"vm_oem_profile={profile}",
        "-e", f"vm_count={count}",
    ]
    if group_tag:
        cmd += ["-e", f"vm_group_tag={group_tag}"]
    args = {"profile": profile, "count": count, "group_tag": group_tag}
    job = job_manager.start("provision_clone", cmd, args=args)
    return RedirectResponse(f"/jobs/{job['id']}", status_code=303)


@app.post("/api/jobs/template")
async def start_template(profile: str = Form(...)):
    cmd = [
        "ansible-playbook", str(PLAYBOOK_DIR / "build_template.yml"),
        "-e", f"vm_oem_profile={profile}",
    ]
    args = {"profile": profile}
    job = job_manager.start("build_template", cmd, args=args)
    return RedirectResponse(f"/jobs/{job['id']}", status_code=303)


@app.post("/api/jobs/capture")
async def start_capture(
    vmid: int = Form(...),
    vm_name: str = Form(""),
    group_tag: str = Form(""),
):
    name = vm_name or f"autopilot-{vmid}"
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


@app.post("/api/hashes/delete")
async def delete_hashes(files: list[str] = Form(...)):
    for filename in files:
        file_path = HASH_DIR / filename
        if file_path.exists() and file_path.suffix == ".csv":
            file_path.unlink()
    return RedirectResponse("/hashes", status_code=303)


@app.get("/api/hashes/{filename}")
async def download_hash(filename: str):
    file_path = HASH_DIR / filename
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
