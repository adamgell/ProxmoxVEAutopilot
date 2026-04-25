# Python Version Support

This document records the Python version policy for ProxmoxVEAutopilot, the tested CI matrix, known incompatibilities, and macOS-specific install guidance.

## Minimum Supported Version

**Python 3.10**

The effective floor is set by `ansible-core>=2.17`, which [requires Python ≥3.10](https://docs.ansible.com/ansible/latest/reference_appendices/release_and_maintenance.html#ansible-core-support-matrix).  Other key runtime packages are compatible at or below that floor:

| Package | Minimum Python |
|---------|---------------|
| `fastapi==0.115.*` | 3.8 |
| `uvicorn[standard]==0.34.*` | 3.8 |
| `ansible-core>=2.17` | **3.10** ← binding constraint |
| `ansible-lint` (dev / CI) | 3.10 |

## Tested Versions (CI Matrix)

| Python | Platform | Job | Notes |
|--------|----------|-----|-------|
| **3.12** | `macos-latest` (ARM64) | `utm-validation` | UTM code-path smoke test and role lint |
| *(none yet)* | `ubuntu-latest` | *(planned)* | Proxmox/Docker path — tracked separately |

The UTM CI job (`.github/workflows/utm-ci.yml`) is the primary gate for the macOS/ARM64 code paths.

## Syntax Features Used

All modules that use PEP 604 `X | Y` union syntax in annotations include `from __future__ import annotations` at the top, making them compatible back to Python 3.7 at the AST level.  No `match`/`case` statements are used.

- **`str | None` in type annotations** — safe with `from __future__ import annotations` (deferred evaluation); present in `web/builder.py`, `web/jobs_db.py`, `web/utm_vm_metrics.py`, and others.
- **`asyncio` usage** — `asyncio.get_event_loop()` patterns avoided in favour of `asyncio.get_running_loop()` / task-based APIs.
- **`subprocess.run()`** — used with `capture_output=True` (Python 3.7+).

## Known Incompatibilities and Concerns

### Python 3.13

- `asyncio` event-loop policy deprecations from 3.10 are enforced more strictly in 3.13.  FastAPI / Starlette (0.115.x) support 3.13; runtime warnings under `pytest` are suppressed in `pyproject.toml` (`filterwarnings`).
- `ssl.wrap_socket()` removed in 3.12 — no direct usage in this codebase.

### Python 3.14 (preview)

- **`asyncio` task cancellation changes**: `asyncio.Task.cancel()` propagation semantics tightened (PEP 745 follow-on).  Not yet tested against 3.14 pre-releases.
- **`typing` module churn**: `typing.get_type_hints()` behaviour with `from __future__ import annotations` evolves across 3.11–3.14.  Current code does not introspect type hints at runtime so this is low risk.
- **`subprocess`**: No breaking changes expected for the patterns used here (`subprocess.run`, `Popen`).
- **`impacket` / `cryptography` / `ldap3`**: Third-party packages may lag on 3.14 wheels.  Track upstream release notes before upgrading.
- **Recommendation**: Do not target 3.14 in CI until it reaches RC status and key deps publish compatible wheels.

## macOS-Specific Notes

macOS ships a system `python3` whose version is tied to the Xcode Command Line Tools (CLT) vintage:

| macOS / CLT vintage | Bundled python3 |
|---------------------|-----------------|
| macOS 13 Ventura (CLT 15) | 3.9 |
| macOS 14 Sonoma (CLT 16) | 3.11 (some CLT builds) |
| macOS 15 Sequoia (CLT 17) | 3.13 (via Xcode 16) |

The system python3 is **not suitable** for running the service because:
1. It is `externally-managed` (PEP 668) and refuses bare `pip install`.
2. Its version changes with CLT updates, making reproducibility hard.

**Recommended approach for macOS operators:**

```bash
# Option A — pyenv (version-pinned, shell-scoped)
brew install pyenv
pyenv install 3.12.9
pyenv local 3.12.9
pip install -r autopilot-proxmox/requirements.txt

# Option B — uv (fast, drop-in pip/venv replacement)
brew install uv
cd autopilot-proxmox
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt
```

The UTM service must be run natively on macOS (not inside Docker) because `utmctl` is a macOS host binary.  See [`docs/UTM_MACOS_SETUP.md`](UTM_MACOS_SETUP.md) for the full setup walkthrough.
