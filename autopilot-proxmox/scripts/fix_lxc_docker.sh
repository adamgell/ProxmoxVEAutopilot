#!/bin/bash
# Fix Docker in Proxmox LXC and build the app.
# Run inside the LXC: pct exec 500 -- bash < scripts/fix_lxc_docker.sh
set -e

echo "=== Fixing Docker daemon config ==="
cat > /etc/docker/daemon.json << 'EOF'
{"iptables": false}
EOF
systemctl restart docker
docker info > /dev/null 2>&1 && echo "Docker is running" || { echo "Docker FAILED"; exit 1; }

echo "=== Building image (apparmor=unconfined) ==="
cd /opt/ProxmoxVEAutopilot/autopilot-proxmox
DOCKER_BUILDKIT=1 docker build --security-opt apparmor=unconfined -t autopilot .

echo "=== Starting container ==="
docker compose up -d

echo "=== Done! UI at http://$(hostname -I | awk '{print $1}'):5000 ==="
