"""Repo-root-relative path helpers.

Used to locate writable data directories in both Docker and native-macOS
deployments:

- In Docker, ``__file__`` resolves to ``/app/web/paths.py``, so
  ``REPO_ROOT`` is ``/app`` and the computed defaults match the Docker
  volume-mount points exactly (``/app/output``, ``/app/jobs``,
  ``/app/secrets``).
- On macOS (native / TUI launch), ``REPO_ROOT`` resolves to the
  checkout's ``autopilot-proxmox/`` directory, so all dirs land inside
  the repo tree and remain user-writable.

Every directory can be overridden via the documented env vars; the TUI
and ``run_macos_native.sh`` set them automatically.  See
``docs/UTM_PATH_SWEEP.md`` for the full env-var reference.
"""
from __future__ import annotations

import os
from pathlib import Path

# autopilot-proxmox/ — one level above web/
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

# Writable output dir (job logs, DB files, hashes).
# Env: AUTOPILOT_OUTPUT_DIR  (TUI / run_macos_native.sh set this automatically)
OUTPUT_DIR: Path = Path(
    os.environ.get("AUTOPILOT_OUTPUT_DIR", str(REPO_ROOT / "output"))
)

# Ephemeral job-spec drop directory consumed by builder workers.
# Env: AUTOPILOT_JOBS_DIR
JOBS_DIR: Path = Path(
    os.environ.get("AUTOPILOT_JOBS_DIR", str(REPO_ROOT / "jobs"))
)

# Secrets directory (keytab, TLS certs, etc.).
# Env: AUTOPILOT_SECRETS_DIR
SECRETS_DIR: Path = Path(
    os.environ.get("AUTOPILOT_SECRETS_DIR", str(REPO_ROOT / "secrets"))
)
