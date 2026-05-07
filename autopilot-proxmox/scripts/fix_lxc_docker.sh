#!/bin/bash
# Fix Docker in Proxmox LXC and build the production app image.
# Run inside the LXC: pct exec 500 -- bash < scripts/fix_lxc_docker.sh
set -e

check_active_autopilot_work() {
  if ! docker ps --format '{{.Names}}' | grep -Eq '^(autopilot|autopilot-proxmox-autopilot-builder-)'; then
    return 0
  fi

  docker exec autopilot python - <<'PY'
import sqlite3
import sys
from pathlib import Path

checks = []

jobs_db = Path("/app/output/jobs.db")
if jobs_db.exists():
    with sqlite3.connect(jobs_db) as conn:
        rows = conn.execute(
            "SELECT id, job_type, status FROM jobs "
            "WHERE status IN ('pending', 'running') "
            "ORDER BY created_at"
        ).fetchall()
    checks.extend(
        f"job {job_id} {job_type} {status}"
        for job_id, job_type, status in rows
    )

sequences_db = Path("/app/output/sequences.db")
if sequences_db.exists():
    with sqlite3.connect(sequences_db) as conn:
        rows = conn.execute(
            "SELECT id, state FROM provisioning_runs "
            "WHERE state NOT IN ('done', 'failed') "
            "ORDER BY created_at"
        ).fetchall()
    checks.extend(f"run {run_id} {state}" for run_id, state in rows)

if checks:
    print("\n".join(checks))
    sys.exit(2)
PY
}

if [ "${FORCE_DOCKER_RESTART:-}" != "1" ]; then
  echo "=== Checking for active Autopilot work before Docker restart ==="
  if ! active_work="$(check_active_autopilot_work)"; then
    echo "Refusing to restart Docker while Autopilot work is active:"
    echo "${active_work}"
    echo "Set FORCE_DOCKER_RESTART=1 only if you intentionally want to interrupt it."
    exit 1
  fi
fi

echo "=== Fixing Docker daemon config ==="
cat > /etc/docker/daemon.json << 'EOF'
{
  "iptables": true,
  "ip-masq": true
}
EOF
systemctl restart docker
docker info > /dev/null 2>&1 && echo "Docker is running" || { echo "Docker FAILED"; exit 1; }

echo "=== Building image (apparmor=unconfined) ==="
cd /opt/ProxmoxVEAutopilot/autopilot-proxmox
GIT_SHA="${GIT_SHA:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
BUILD_TIME="${BUILD_TIME:-$(date -u +"%Y-%m-%dT%H:%M:%SZ")}"
IMAGE_TAG="${IMAGE_TAG:-ghcr.io/adamgell/proxmox-autopilot:latest}"

# Docker bridge builds need daemon-managed iptables masquerading. Disabling it
# leaves the default BuildKit network unable to reach Debian apt repositories
# even when host networking can reach deb.debian.org.
DOCKER_BUILDKIT=1 docker build \
  --security-opt apparmor=unconfined \
  -t "${IMAGE_TAG}" \
  --build-arg "GIT_SHA=${GIT_SHA}" \
  --build-arg "BUILD_TIME=${BUILD_TIME}" \
  .

echo "=== Starting container ==="
docker compose up -d

echo "=== Done! UI at http://$(hostname -I | awk '{print $1}'):5000 ==="
