# Web UI for Proxmox VE Autopilot

## Overview

A lightweight web UI and REST API for kicking off and monitoring Ansible playbooks. Runs in a Docker container alongside Ansible on the Proxmox host. The UI is optional — the CLI continues to work standalone.

## Architecture

Single Docker container with two components:
- **FastAPI** (Python) — serves HTML pages, REST API, and websocket streams
- **Ansible** — runs as child processes spawned by the API

No database. Job state is in-memory (active) and on-disk (logs). Hash CSVs are already on disk.

```
┌─ Docker Container (port 5000) ──────────────────────┐
│                                                       │
│  FastAPI ──websocket──> Browser (90s HTML)            │
│     │                                                 │
│     ├── POST /api/jobs/* → spawns ansible-playbook    │
│     ├── GET  /api/jobs   → list active + past jobs    │
│     ├── WS   /api/jobs/{id}/stream → live output      │
│     └── GET  /api/hashes → list/download CSVs         │
│                                                       │
│  /app/                                                │
│    ├── web/              # FastAPI app + HTML pages    │
│    ├── jobs/             # Job logs (persisted)        │
│    ├── output/hashes/    # Captured CSVs               │
│    └── (ansible roles, playbooks, etc.)               │
│                                                       │
│  Mounted volume: vault.yml (plain text, not encrypted)│
└───────────────────────────────────────────────────────┘
```

CLI access: `docker exec -it autopilot bash` then run `ansible-playbook` directly.

## UI Design

90s aesthetic: Times New Roman, `<hr>` dividers, `<table border=1>`, blue links, gray background. No CSS framework. No JavaScript framework. Vanilla HTML with minimal inline JS for the websocket on the job detail page.

### Pages

**Home (`/`)** — Title, nav links, status summary (running jobs, hash file count).

**Provision VMs (`/provision`)** — Form: OEM profile dropdown (13 options), VM count number input (default 1), group tag text input (optional). Submit starts `provision_clone.yml` and redirects to job page.

**Build Template (`/template`)** — Form: OEM profile dropdown. Submit starts `build_template.yml` and redirects to job page.

**Upload Hashes (`/upload`)** — Lists CSV files in `output/hashes/` with size and date. "Upload All to Intune" button starts `upload_hashes.yml`.

**Hash Files (`/hashes`)** — Table of all CSVs: filename, size, date. Click to download. Link to upload page.

**Jobs (`/jobs`)** — Table: job ID, playbook, status (running/complete/failed), started, duration. Click to see output.

**Job Detail (`/jobs/{id}`)** — `<pre>` tag with raw terminal output, black background, green monospace text. Auto-scrolls via websocket while running. Shows exit code when done.

## API Endpoints

**HTML pages:**
- `GET /` — home
- `GET /provision` — provision form
- `GET /template` — template build form
- `GET /upload` — upload page with file list
- `GET /hashes` — hash file browser
- `GET /jobs` — job history
- `GET /jobs/{id}` — job detail with live output

**JSON API:**
- `POST /api/jobs/provision` — start clone playbook. Body: `{"profile": "lenovo-t14", "count": 1, "group_tag": ""}`
- `POST /api/jobs/template` — start template build. Body: `{"profile": "generic-desktop"}`
- `POST /api/jobs/upload` — start hash upload. No body.
- `GET /api/jobs` — list all jobs as JSON
- `GET /api/jobs/{id}` — job detail as JSON (status, exit code, log)
- `GET /api/hashes/{filename}` — download a CSV file

**Websocket:**
- `WS /api/jobs/{id}/stream` — streams raw stdout/stderr lines in real-time

## Job Management

- Job ID format: `YYYYMMDD-XXXX` (date + 4 random hex chars)
- Job metadata stored in `jobs/index.json`: array of `{id, playbook, status, started, ended, exit_code, args}`
- Job output logged to `jobs/{id}.log`
- Active jobs tracked in-memory with their subprocess handle and output buffer
- Websocket clients receive lines as they're written to the log
- Status transitions: `running` -> `complete` (exit 0) or `failed` (exit non-zero)

## Docker

**Base image:** `python:3.12-slim`

**Installed in image:**
- Python: FastAPI, uvicorn, websockets
- Ansible (pip)
- pwsh + Microsoft.Graph.Authentication + WindowsAutopilotIntune modules

**File layout in container:**
```
/app/
├── web/
│   ├── app.py           # FastAPI application
│   └── templates/       # HTML templates (Jinja2)
├── playbooks/
├── roles/
├── filter_plugins/
├── files/
├── scripts/
├── inventory/
├── ansible.cfg
├── jobs/                # Created at runtime
└── output/hashes/       # Created at runtime
```

**docker-compose.yml:**
```yaml
services:
  autopilot:
    build: .
    ports:
      - "5000:5000"
    volumes:
      - ./autopilot-proxmox/inventory/group_vars/all/vault.yml:/app/inventory/group_vars/all/vault.yml
      - autopilot-data:/app/output
      - autopilot-jobs:/app/jobs
volumes:
  autopilot-data:
  autopilot-jobs:
```

**Entrypoint:** `uvicorn web.app:app --host 0.0.0.0 --port 5000`

## Security

- No authentication (single admin tool on local network)
- Vault.yml mounted as plain text volume (not encrypted)
- Secrets never leave the container
- No HTTPS (runs behind Proxmox on local network)

## What's NOT included

- No auth/login
- No multi-user support
- No persistent database
- No CSS framework or JS framework
- No Autopilot JSON injection UI (skip by default, CLI opt-in)
