#!/bin/bash
# Fix Docker in Proxmox LXC and build the production app image.
# Run inside the LXC: pct exec 500 -- bash < scripts/fix_lxc_docker.sh
set -e

check_active_autopilot_work() {
  if ! docker ps --format '{{.Names}}' | grep -Eq '^(autopilot|autopilot-proxmox-autopilot-builder-)'; then
    return 0
  fi

  docker exec -i \
    -e ACTIVE_RUN_WINDOW_HOURS="${ACTIVE_RUN_WINDOW_HOURS:-8}" \
    autopilot python - <<'PY'
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

checks = []
active_run_window_hours = float(os.environ.get("ACTIVE_RUN_WINDOW_HOURS") or 8)
active_run_cutoff = datetime.now(timezone.utc) - timedelta(
    hours=active_run_window_hours,
)


def parse_dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def table_exists(conn, name):
    row = conn.execute("SELECT to_regclass(%s) AS table_name", (name,)).fetchone()
    return bool(row and row["table_name"])


dsn = (
    os.environ.get("AUTOPILOT_DATABASE_URL")
    or os.environ.get("AUTOPILOT_TS_ENGINE_DATABASE_URL")
    or ""
).strip()
if not dsn:
    print(
        "AUTOPILOT_DATABASE_URL or AUTOPILOT_TS_ENGINE_DATABASE_URL is required "
        "to check active Autopilot work",
        file=sys.stderr,
    )
    sys.exit(2)

try:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        if table_exists(conn, "jobs"):
            rows = conn.execute(
                "SELECT id, job_type, status FROM jobs "
                "WHERE status IN ('pending', 'running') "
                "ORDER BY created_at"
            ).fetchall()
            checks.extend(
                f"job {row['id']} {row['job_type']} {row['status']}"
                for row in rows
            )

        if table_exists(conn, "provisioning_runs"):
            rows = conn.execute(
                "SELECT id, state, started_at FROM provisioning_runs "
                "WHERE state NOT IN ('done', 'failed') "
                "ORDER BY started_at"
            ).fetchall()
            for row in rows:
                started = row["started_at"]
                if not hasattr(started, "tzinfo"):
                    started = parse_dt(started)
                elif started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if started and started >= active_run_cutoff:
                    checks.append(
                        f"run {row['id']} {row['state']} started_at={row['started_at']}"
                    )
except Exception as exc:
    print(f"failed to check active Autopilot work in Postgres: {exc}", file=sys.stderr)
    sys.exit(2)

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
