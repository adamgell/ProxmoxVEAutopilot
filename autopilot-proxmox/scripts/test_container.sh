#!/bin/bash
# Smoke test for the Docker container. Run after build:
#   docker run --rm autopilot bash /app/scripts/test_container.sh
set -e
PASS=0
FAIL=0

check() {
    local desc="$1"
    shift
    if "$@" > /dev/null 2>&1; then
        echo "PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $desc"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== Container Smoke Tests ==="
echo ""

check "ansible-playbook is on PATH" which ansible-playbook
check "ansible --version runs" ansible --version
check "pwsh is on PATH" which pwsh
check "pwsh runs" pwsh -NonInteractive -Command "exit 0"
check "pwsh has Microsoft.Graph.Authentication" pwsh -NonInteractive -Command "Import-Module Microsoft.Graph.Authentication"
check "pwsh has WindowsAutopilotIntune" pwsh -NonInteractive -Command "Import-Module WindowsAutopilotIntune"
check "python3 is available" python3 --version
check "uvicorn is installed" python3 -c "import uvicorn"
check "fastapi is installed" python3 -c "import fastapi"
check "yaml is installed" python3 -c "import yaml"
check "ansible.cfg exists" test -f /app/ansible.cfg
check "playbooks exist" test -f /app/playbooks/provision_clone.yml
check "roles exist" test -d /app/roles/proxmox_vm_iso
check "filter_plugins exist" test -f /app/filter_plugins/smbios.py
check "oem_profiles.yml exists" test -f /app/files/oem_profiles.yml
check "web app importable" python3 -c "from web.app import app"
check "jobs dir exists" test -d /app/jobs
check "output dir exists" test -d /app/output/hashes

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
