# Copilot Instructions for ProxmoxVEAutopilot

## Build & Test Commands

### Setup
```bash
cd autopilot-proxmox
make setup  # Creates .venv, installs dev dependencies
```

### Testing
- **Full suite**: `make test` or `make test` from `autopilot-proxmox/`
- **Unit tests only** (no FastAPI integration): `make test-unit`
- **Ubuntu compiler tests**: `make test-compiler`
- **API/integration tests**: `make test-api`
- **Single test file**: `pytest tests/test_crypto.py -v` or `./.venv/bin/pytest`

### Linting
- **Ansible**: `make lint` — runs syntax check on all playbooks
- **ansible-lint (production)**: `.venv/bin/ansible-lint playbooks/ roles/`

### Building & Running
```bash
# Build container (from repo root)
docker build -t proxmox-autopilot:dev -f autopilot-proxmox/Dockerfile autopilot-proxmox/

# Run via docker-compose
docker compose up -d  # Starts web server on :5000

# Run Python module directly (dev only)
python -m web.entrypoint web    # FastAPI web server (default)
python -m web.entrypoint builder # Job builder worker
python -m web.entrypoint monitor # Device monitor background daemon
```

## High-Level Architecture

### Three-Container Pattern
The system runs as three Docker services sharing a volume:
1. **web** — FastAPI web UI and REST API on :5000
2. **builder** — Ansible job executor (claims jobs from queue)
3. **monitor** — Continuous device-state polling (Proxmox, Intune, AD)

All three read/write to shared SQLite databases in `/app/output/`:
- `sequences.db` — Task sequences, steps, credentials (encrypted)
- `jobs.db` — Job queue with per-type concurrency caps
- `devices.db` — Proxmox VMs registered to Autopilot
- `device_monitor.db` — Device state cache (Intune, AD, Proxmox agent status)

### Data Flow for VM Provisioning

```
Web UI (Provision VMs)
    ↓ (POST /api/provision)
    → enqueue job: "provision_clone" (job_db)
    ↓
Builder claims job, runs:
    playbooks/provision_clone.yml
    └─ role: proxmox_vm_clone
       └─ role: autopilot_inject (pushes JSON config via guest agent)
       └─ role: hash_capture (executes capture script, retrieves CSV)
    ↓ (job completes, exit_code=0)
Web UI (Devices/Upload to Intune)
    → CSV upload via Microsoft.Graph.Authentication PowerShell
```

### Critical Modules

#### `web/app.py` (~5600 LOC)
Main FastAPI application. Patterns:
- **Error redirects**: Use `_redirect_with_error()` for safe URL encoding of error messages (prevents truncation at spaces/`#`)
- **Path safety**: Always use `_safe_path()` to prevent directory traversal on user input
- **Input sanitization**: Use `_sanitize_input()` for alphanumeric+hyphen/underscore/dot filenames only
- **Startup hooks**: Two separate flags (`_SEQUENCES_READY`, `_JOBS_READY`) ensure DB migrations complete before `/healthz` returns; builder/monitor wait on this before running

#### `web/sequences_db.py` & `web/jobs_db.py`
SQLite wrapper modules. **Pattern**:
- Module-level `SCHEMA` string
- `init(db_path)` runs `executescript()` with schema (idempotent)
- Context-managed `@contextmanager` for connections, always set `row_factory = sqlite3.Row`
- Helper: `_row_to_dict()` converts Row objects to plain dicts

#### `web/jobs.py` — JobManager
In-process job executor for the builder. Runs Ansible playbooks and tracks UPID completion. Do not confuse with `jobs_db` (queue).

#### `web/sequence_compiler.py`
Converts task sequences to Ansible playbooks and Windows/Ubuntu scripts:
- PowerShell literals must be escaped with `sequence_compiler._ps_escape()` for single-quoted strings
- Generates `autounattend.xml` (Windows answer file) and Ubuntu cloud-init scripts
- Task sequence steps are ordered by `order_index` in DB, iterated at runtime

#### `web/auth.py`
Entra OIDC + AD LDAP probe support:
- **AD probe**: Canonicalizes LDAP byte attributes (objectSid → SID string, objectGUID → UUID string)
- Session tokens, user roles, credential test endpoints

### Database Patterns

All three DB modules (`sequences_db`, `jobs_db`, `devices_db`) follow this pattern:
```python
SCHEMA = """CREATE TABLE ..."""

def init(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)

@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # Return rows as dicts
    try:
        yield conn
    finally:
        conn.close()

def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)
```

### Job Queue Concurrency

`jobs_db.claim_next_job()` enforces per-job-type concurrency:
- Reads `job_type_limits` table (e.g., `provision_clone: max_concurrent=1`)
- Uses `LEFT JOIN + COALESCE` with `_DEFAULT_CAP=1` for unseeded job types
- Job types not in the table degrade gracefully (fall back to default, don't silently skip)

### Proxmox Integration

Roles execute via Ansible + Proxmox REST API:
- **`proxmox_vm_clone`** — Clone from template, reconfigure SMBIOS, resize disk
- **`hash_capture`** — Push scripts to guest agent, execute, retrieve output
- **All task UPIDs** (VM creation, conversion to template) are followed by `include_tasks common/tasks/wait_task.yml` with `_task_upid=json.data` to ensure completion before proceeding

### UI Colors & Theming

CSS variables in `web/templates/base.html`:
- Light mode: CSS vars prefixed `--color-*`
- Dark mode: Enabled when `html[data-theme="dark"]` (toggled from localStorage)

### Log Streaming

- `JobManager.get_log(job_id)` reads entire `<job_id>.log` file to memory (no tail/streaming)
- WebSocket `/ws/jobs/<job_id>` streams log updates live (polling fallback for older browsers)

## Key Conventions

### Naming
- **Job types** in code must match strings passed to `job_manager.start()` in `app.py` (e.g., `"provision_clone"`, `"hash_capture"`)
- **Task sequence step types** are defined in `sequences_db` and must be understood by the compiler
- **VM naming**: Sequences can rename Windows VMs to `{serial_number}` (DNS-friendly, generated from OEM profile)

### Validations
- **SMBIOS serials** are generated with manufacturer prefixes: Lenovo=PF, Dell=SVC, HP=CZC, Microsoft=MSF
- **UUIDs** for disk serials follow format `APHV{vmid:06d}{uuid_hex[:10]}`
- **Filenames**: Only alphanumeric + hyphen/underscore/dot allowed in user input (enforced by `_sanitize_input()`)

### Encryption
- Credentials stored in `sequences_db.credentials` as encrypted blobs
- Encryption key: `secrets/credential_key` (AES, loaded at startup)
- See `web/crypto.py` for encryption/decryption patterns

### Ansible Patterns
- **Internal variables**: Prefixed with `_` (e.g., `_task_upid`, `_vm_oem_profile`) — intentionally not role-prefixed
- **Filters** in `filter_plugins/smbios.py`: `proxmox_smbios1`, `proxmox_disk_serial`, `generate_serial_number`, `generate_vm_identity`
- **Guest agent tasks**: Common fragments in `roles/common/tasks/` — `wait_agent.yml`, `guest_exec.yml`, `file_write.yml`, `wait_task.yml`

### Code Style
- **Minimal comments**: Only clarify non-obvious logic; avoid stating what the code does
- **Error messages**: Use percent-encoding or `_redirect_with_error()` to safely embed user text in URLs
- **Python typing**: Full type hints expected throughout (checked via pyproject.toml)

### Testing
- Tests run from either `autopilot-proxmox/` or repo root (pythonpath set in `pyproject.toml`)
- Fixtures in `tests/fixtures/` for reusable test data
- Integration tests in `tests/integration/`
- Use `pytest` directly or `make` targets for grouped suites

### PowerShell in Sequences
- Use `sequence_compiler._ps_escape()` for single-quoted PowerShell strings
- When injected into VMs via guest agent, PowerShell scripts are written as Unicode (UTF-16LE for Windows)

## Common Tasks

### Add a new Task Sequence Step Type
1. Define step in `sequences_db` (create migration if needed)
2. Add handler in `sequence_compiler.py` — render to Ansible task or shell command
3. Test via `test_sequences_api.py` and `test_ubuntu_compiler_*.py`

### Add a new Job Type
1. Create playbook in `playbooks/`
2. Call `job_manager.start("job_type_name", ...)` from `app.py`
3. Add to `job_type_limits` table or use default cap
4. Add test in `tests/test_jobs.py`

### Add a new Ansible Role
1. Create role directory in `roles/`
2. Define tasks, handlers, vars
3. Document in `autopilot-proxmox/README.md` Roles table
4. Run `ansible-lint` to validate

### Debug a Failed Job
1. Check **Jobs** page → live log via WebSocket or tail via GET `/api/jobs/<id>/log`
2. Correlate with Proxmox task ID if UPID is in logs
3. Device state in **Devices** page or `queries/device_*.sql` in device_monitor.db

## Container Environment

**From Dockerfile**:
- Base: `python:3.12-slim-bookworm`
- Kerberos + LDAP stack for AD probing (GSSAPI/SASL signing)
- PowerShell 7 + Microsoft.Graph.Authentication module
- OpenSSL legacy provider enabled for MD4 (NTLM)

**Startup modes**:
- Default: `python -m web.entrypoint web` → FastAPI on :5000
- Builder: `python -m web.entrypoint builder` → Job executor loop
- Monitor: `python -m web.entrypoint monitor` → Device state poller

**Volumes**:
- `/app/output/` — SQLite DBs, hash CSVs, job logs
- `/app/secrets/` — Encryption key, OAuth creds
- `/app/jobs/` — Job working directories

## MCP Servers (Optional Enhancements)

### sqlite-mcp
Query the SQLite databases directly without spawning `sqlite3` CLI:
```json
{
  "mcpServers": {
    "sqlite": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-sqlite"],
      "env": {
        "MCP_SQLITE_DB_PATH": "/path/to/output/"
      }
    }
  }
}
```

Useful for querying `sequences.db`, `jobs.db`, `devices.db` during debugging. Common queries:
- `SELECT * FROM jobs WHERE status='failed'` (check failed jobs)
- `SELECT * FROM task_sequences` (list sequences)
- `SELECT * FROM credentials` (list encrypted credential slots)

### playwright-mcp
Test the FastAPI web UI in a real browser context:
```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-playwright"]
    }
  }
}
```

Useful for:
- Verifying UI workflow after changes (e.g., Provision VMs → Jobs page log streaming)
- Capturing screenshots or debugging layout issues
- Testing WebSocket reconnection behavior
