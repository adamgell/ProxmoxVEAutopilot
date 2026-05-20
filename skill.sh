#!/usr/bin/env bash
# Codex/MCP helper for ProxmoxVEAutopilot.
#
# Usage:
#   ./skill.sh status
#   ./skill.sh docs "WinPE CloudOSD"
#   ./skill.sh read repo/docs/superpowers/plans/2026-05-04-winpe-orchestrated-deploy.md
#   ./skill.sh tunnel
#   ./skill.sh proxy
#   ./skill.sh proxy-install
#   ./skill.sh proxy-status
#   ./skill.sh smoke
#   ./skill.sh shell
#   ./skill.sh codex "Search the autopilot docs for WinPE and summarize the top result."
#   ./skill.sh config
#
# The MCP token is read from the live server .env and passed only in process
# environment to curl/codex. The token is never printed.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_HOST="${AUTOPILOT_MCP_REMOTE_HOST:-root@192.168.2.4}"
REMOTE_APP_DIR="${AUTOPILOT_MCP_REMOTE_APP_DIR:-/opt/ProxmoxVEAutopilot/autopilot-proxmox}"
REMOTE_ENV_FILE="${AUTOPILOT_MCP_REMOTE_ENV_FILE:-}"
LOCAL_PORT="${AUTOPILOT_MCP_LOCAL_PORT:-15050}"
REMOTE_PORT="${AUTOPILOT_MCP_REMOTE_PORT:-5050}"
LOCAL_URL="http://127.0.0.1:${LOCAL_PORT}/mcp"
PROXY_PORT="${AUTOPILOT_MCP_PROXY_PORT:-15051}"
PROXY_URL="http://127.0.0.1:${PROXY_PORT}/mcp"
ORIGIN="${AUTOPILOT_MCP_ORIGIN:-http://localhost}"
MCP_SERVER_NAME="${AUTOPILOT_MCP_SERVER_NAME:-proxmoxveautopilot}"
PROXY_LABEL="${AUTOPILOT_MCP_PROXY_LABEL:-com.proxmoxveautopilot.mcp-proxy}"
PROXY_PLIST="${HOME}/Library/LaunchAgents/${PROXY_LABEL}.plist"
PROXY_LOG_DIR="${HOME}/Library/Logs/ProxmoxVEAutopilot"

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
}

token() {
  if [[ -n "${AUTOPILOT_MCP_TOKEN:-}" ]]; then
    printf '%s\n' "${AUTOPILOT_MCP_TOKEN}"
    return 0
  fi

  ssh "${REMOTE_HOST}" \
    "REMOTE_APP_DIR='${REMOTE_APP_DIR}' REMOTE_ENV_FILE='${REMOTE_ENV_FILE}' bash -s" <<'SH'
set -euo pipefail

candidates=()
if [[ -n "${REMOTE_ENV_FILE}" ]]; then
  candidates+=("${REMOTE_ENV_FILE}")
fi
candidates+=(
  "${REMOTE_APP_DIR}/.env"
  "$(dirname "${REMOTE_APP_DIR}")/.env"
)

for env_file in "${candidates[@]}"; do
  if [[ ! -f "${env_file}" ]]; then
    continue
  fi
  awk -F= '
    $1 == "AUTOPILOT_MCP_TOKEN" {
      sub(/^[^=]*=/, "")
      value=$0
    }
    END {
      if (value != "") {
        print value
      } else {
        exit 1
      }
    }
  ' "${env_file}" && exit 0
done

if command -v docker >/dev/null 2>&1; then
  token_from_docker="$(
    docker inspect autopilot-mcp --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
      | sed -n 's/^AUTOPILOT_MCP_TOKEN=//p' \
      | tail -1
  )"
  if [[ -n "${token_from_docker}" ]]; then
    printf '%s\n' "${token_from_docker}"
    exit 0
  fi
fi

echo "error: AUTOPILOT_MCP_TOKEN not found in remote .env candidates" >&2
exit 1
SH
}

remote_mcp_reachable() {
  ssh "${REMOTE_HOST}" "REMOTE_PORT='${REMOTE_PORT}' python3 - <<'PY'
import os
import socket
import sys

sock = socket.socket()
sock.settimeout(2)
try:
    sock.connect(('127.0.0.1', int(os.environ['REMOTE_PORT'])))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY"
}

ensure_tunnel() {
  if ! remote_mcp_reachable >/dev/null 2>&1; then
    echo "error: ${REMOTE_HOST} is not accepting MCP connections on 127.0.0.1:${REMOTE_PORT}" >&2
    echo "error: start/redeploy the autopilot-mcp sidecar before using ${LOCAL_URL}" >&2
    return 1
  fi
  if nc -z 127.0.0.1 "${LOCAL_PORT}" >/dev/null 2>&1; then
    return 0
  fi
  ssh -o ExitOnForwardFailure=yes -N -L "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "${REMOTE_HOST}" &
  local pid=$!
  for _ in $(seq 1 30); do
    if nc -z 127.0.0.1 "${LOCAL_PORT}" >/dev/null 2>&1; then
      echo "[skill] tunnel started pid=${pid} ${LOCAL_URL}" >&2
      return 0
    fi
    if ! kill -0 "${pid}" >/dev/null 2>&1; then
      echo "error: ssh tunnel exited before opening 127.0.0.1:${LOCAL_PORT}" >&2
      return 1
    fi
    sleep 0.2
  done
  kill "${pid}" 2>/dev/null || true
  echo "error: tunnel did not open on 127.0.0.1:${LOCAL_PORT}" >&2
  return 1
}

ensure_forward() {
  if nc -z 127.0.0.1 "${LOCAL_PORT}" >/dev/null 2>&1; then
    return 0
  fi
  ssh -o ExitOnForwardFailure=yes -N -L "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "${REMOTE_HOST}" &
  local pid=$!
  for _ in $(seq 1 30); do
    if nc -z 127.0.0.1 "${LOCAL_PORT}" >/dev/null 2>&1; then
      echo "[skill] tunnel started pid=${pid} ${LOCAL_URL}" >&2
      return 0
    fi
    if ! kill -0 "${pid}" >/dev/null 2>&1; then
      echo "error: ssh tunnel exited before opening 127.0.0.1:${LOCAL_PORT}" >&2
      return 1
    fi
    sleep 0.2
  done
  kill "${pid}" 2>/dev/null || true
  echo "error: tunnel did not open on 127.0.0.1:${LOCAL_PORT}" >&2
  return 1
}

rpc() {
  local method="$1"
  local params="${2-}"
  if [[ $# -lt 2 || -z "${params}" ]]; then
    params="{}"
  fi
  local t
  t="$(token)"
  AUTOPILOT_MCP_TOKEN="${t}" \
  AUTOPILOT_MCP_URL="${LOCAL_URL}" \
  AUTOPILOT_MCP_ORIGIN_HEADER="${ORIGIN}" \
  AUTOPILOT_MCP_METHOD="${method}" \
  AUTOPILOT_MCP_PARAMS="${params}" \
    python3 - <<'PY'
import json
import os
import sys
import urllib.request

try:
    params = json.loads(os.environ["AUTOPILOT_MCP_PARAMS"])
except json.JSONDecodeError as exc:
    raise SystemExit(f"invalid JSON params: {exc}") from exc

body = json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "method": os.environ["AUTOPILOT_MCP_METHOD"],
    "params": params,
}).encode("utf-8")
request = urllib.request.Request(
    os.environ["AUTOPILOT_MCP_URL"],
    data=body,
    method="POST",
    headers={
        "Authorization": f"Bearer {os.environ['AUTOPILOT_MCP_TOKEN']}",
        "Origin": os.environ["AUTOPILOT_MCP_ORIGIN_HEADER"],
        "Content-Type": "application/json",
    },
)
with urllib.request.urlopen(request, timeout=60) as response:
    sys.stdout.write(response.read().decode("utf-8"))
PY
}

tool_call() {
  local name="$1"
  local args="${2-}"
  if [[ $# -lt 2 || -z "${args}" ]]; then
    args="{}"
  fi
  local params
  params="$(
    AUTOPILOT_MCP_TOOL_NAME="${name}" \
    AUTOPILOT_MCP_TOOL_ARGS="${args}" \
      python3 - <<'PY'
import json
import os

arguments = json.loads(os.environ["AUTOPILOT_MCP_TOOL_ARGS"])
print(json.dumps({
    "name": os.environ["AUTOPILOT_MCP_TOOL_NAME"],
    "arguments": arguments,
}, separators=(",", ":")))
PY
  )"
  rpc "tools/call" "${params}"
}

json_string() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
}

cmd_status() {
  echo "[skill] remote containers"
  ssh "${REMOTE_HOST}" "docker ps --filter name=autopilot --format 'table {{.Names}}\t{{.Status}}'"
  ensure_tunnel
  echo "[skill] mcp tools"
  rpc "tools/list" | python3 -c 'import json,sys; tools=json.load(sys.stdin)["result"]["tools"]; print("tool_count", len(tools)); print("docs_tools", sorted(t["name"] for t in tools if t["name"].startswith("autopilot_docs.")))'
  echo "[skill] docs"
  tool_call "autopilot_docs.list" '{"limit":500}' | python3 -c 'import json,sys; data=json.load(sys.stdin)["result"]["structuredContent"]; print("doc_count", data["count"]); print("sample", [d["doc_id"] for d in data["docs"][:5]])'
}

cmd_docs() {
  ensure_tunnel
  local query="${1:-WinPE CloudOSD}"
  local query_json
  query_json="$(json_string "${query}")"
  tool_call "autopilot_docs.search" "{\"query\":${query_json},\"limit\":5}" \
    | python3 -c 'import json,sys; data=json.load(sys.stdin)["result"]["structuredContent"]; print(json.dumps(data, indent=2))'
}

cmd_read() {
  ensure_tunnel
  local doc_id="${1:?doc_id required}"
  local max_chars="${2:-12000}"
  local doc_json
  doc_json="$(json_string "${doc_id}")"
  tool_call "autopilot_docs.read" "{\"doc_id\":${doc_json},\"max_chars\":${max_chars}}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["structuredContent"]["content"])'
}

cmd_smoke() {
  ensure_tunnel
  echo "[skill] smoke docs"
  tool_call "autopilot_docs.search" '{"query":"OSDeploy setup build host","limit":1}' \
    | python3 -c 'import json,sys; data=json.load(sys.stdin)["result"]["structuredContent"]; print("top_doc", (data.get("results") or [{}])[0].get("doc_id"))'

  echo "[skill] smoke setup"
  tool_call "setup.get_readiness" '{}' \
    | python3 -c 'import json,sys; data=json.load(sys.stdin)["result"]["structuredContent"]; print("phase", data.get("phase"), "health", data.get("health"), "blocking", data.get("blocking_count"))'

  echo "[skill] smoke osdeploy"
  tool_call "osdeploy.get_catalog" '{}' \
    | python3 -c 'import json,sys; data=json.load(sys.stdin)["result"]["structuredContent"]; print("schema_version", data.get("schema_version"), "server_roles", len(data.get("server_roles") or []))'
  tool_call "osdeploy.get_proxmox_options" '{}' \
    | python3 -c 'import json,sys; data=json.load(sys.stdin)["result"]["structuredContent"]; print("defaults", data.get("defaults", {}))'
  tool_call "osdeploy.build_preflight" '{}' \
    | python3 -c 'import json,sys; data=json.load(sys.stdin)["result"]["structuredContent"]; print("build_preflight_keys", sorted(data.keys())[:8])'

  echo "[skill] smoke approval"
  local approval_id
  approval_id="$(
    tool_call "pve_autopilot.write_settings" '{"setting":"mcp-smoke","secret":"must-redact"}' \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["structuredContent"]["approval_id"])'
  )"
  echo "approval_id ${approval_id}"
  tool_call "pve_autopilot.get_approval" "{\"approval_id\":\"${approval_id}\"}" \
    | python3 -c 'import json,sys; data=json.load(sys.stdin)["result"]["structuredContent"]["approval"]; print("approval_status", data.get("status"), "secret", data.get("arguments", {}).get("secret"))'
  tool_call "pve_autopilot.reject_action" "{\"approval_id\":\"${approval_id}\",\"reason\":\"mcp smoke complete\"}" \
    | python3 -c 'import json,sys; data=json.load(sys.stdin)["result"]["structuredContent"]["approval"]; print("rejected_status", data.get("status"))'

  echo "[skill] smoke diagnostic"
  local agent_id
  agent_id="$(
    tool_call "autopilot_agent.list_agents" '{}' \
      | python3 -c 'import json,sys; agents=json.load(sys.stdin)["result"]["structuredContent"].get("agents") or []; print((agents[0] or {}).get("agent_id","") if agents else "")'
  )"
  if [[ -n "${agent_id}" ]]; then
    tool_call "autopilot_agent.run_diagnostic" "{\"agent_id\":\"${agent_id}\",\"diagnostic\":\"system_summary\"}" \
      | python3 -c 'import json,sys; item=json.load(sys.stdin)["result"]["structuredContent"]["work_item"]; print("diagnostic_work_item", item.get("id"), item.get("status"))'
  else
    echo "diagnostic_work_item skipped_no_agents"
  fi

  echo "[skill] smoke audit"
  ssh "${REMOTE_HOST}" "docker exec autopilot-postgres psql -U autopilot -d autopilot -tAc \"SELECT count(*) FROM mcp_call_audit WHERE created_at > now() - interval '10 minutes';\""
}

cmd_tunnel() {
  echo "[skill] opening ${LOCAL_URL}; Ctrl+C closes it" >&2
  exec ssh -o ExitOnForwardFailure=yes -N -L "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "${REMOTE_HOST}"
}

cmd_proxy() {
  ensure_forward
  local t
  t="$(token)"
  echo "[skill] proxy listening at ${PROXY_URL}; Ctrl+C closes it" >&2
  AUTOPILOT_MCP_TOKEN="${t}" \
  AUTOPILOT_MCP_PROXY_TARGET="http://127.0.0.1:${LOCAL_PORT}" \
  AUTOPILOT_MCP_PROXY_PORT="${PROXY_PORT}" \
  AUTOPILOT_MCP_ORIGIN="${ORIGIN}" \
    exec python3 "${ROOT_DIR}/autopilot-proxmox/scripts/mcp_token_proxy.py"
}

cmd_proxy_install() {
  mkdir -p "${HOME}/Library/LaunchAgents" "${PROXY_LOG_DIR}"
  python3 - "${PROXY_PLIST}" "${ROOT_DIR}/skill.sh" "${PROXY_LOG_DIR}" "${PROXY_LABEL}" <<'PY'
import pathlib
import plistlib
import sys

plist_path = pathlib.Path(sys.argv[1])
skill_path = sys.argv[2]
log_dir = pathlib.Path(sys.argv[3])
label = sys.argv[4]
plist = {
    "Label": label,
    "ProgramArguments": [skill_path, "proxy"],
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": str(log_dir / "mcp-proxy.out.log"),
    "StandardErrorPath": str(log_dir / "mcp-proxy.err.log"),
    "WorkingDirectory": str(pathlib.Path(skill_path).parent),
}
plist_path.write_bytes(plistlib.dumps(plist, sort_keys=False))
PY
  launchctl bootout "gui/$(id -u)" "${PROXY_PLIST}" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "${PROXY_PLIST}"
  launchctl kickstart -k "gui/$(id -u)/${PROXY_LABEL}" >/dev/null 2>&1 || true
  echo "[skill] installed ${PROXY_LABEL}"
  echo "[skill] proxy URL: ${PROXY_URL}"
}

cmd_proxy_status() {
  echo "[skill] launchd"
  launchctl print "gui/$(id -u)/${PROXY_LABEL}" 2>/dev/null | sed -n '1,40p' || true
  echo "[skill] listener"
  lsof -nP -iTCP:"${PROXY_PORT}" -sTCP:LISTEN || true
  echo "[skill] backend forward"
  lsof -nP -iTCP:"${LOCAL_PORT}" -sTCP:LISTEN || true
}

cmd_shell() {
  if ! nc -z 127.0.0.1 "${PROXY_PORT}" >/dev/null 2>&1; then
    echo "error: proxy is not listening on ${PROXY_URL}; run ./skill.sh proxy-install" >&2
    return 1
  fi
  export AUTOPILOT_MCP_URL="${PROXY_URL}"
  echo "[skill] spawned shell with ${PROXY_URL} reachable" >&2
  exec "${SHELL:-/bin/zsh}" -l
}

cmd_codex() {
  ensure_tunnel
  local bearer_token
  bearer_token="$(token)"
  local prompt="${1:-Use the proxmoxveautopilot MCP server. Search autopilot_docs for WinPE CloudOSD and read the top result.}"
  AUTOPILOT_MCP_TOKEN="${bearer_token}" codex exec --ephemeral --skip-git-repo-check \
    -c "mcp_servers.${MCP_SERVER_NAME}.url=\"${LOCAL_URL}\"" \
    -c "mcp_servers.${MCP_SERVER_NAME}.bearer_token_env_var=\"AUTOPILOT_MCP_TOKEN\"" \
    "${prompt}"
}

cmd_config() {
  cat <<EOF
[mcp_servers.${MCP_SERVER_NAME}]
url = "${PROXY_URL}"
EOF
}

main() {
  cd "${ROOT_DIR}"
  local cmd="${1:-status}"
  shift || true
  case "${cmd}" in
    status) cmd_status "$@" ;;
    docs|search) cmd_docs "$@" ;;
    read) cmd_read "$@" ;;
    tunnel) cmd_tunnel "$@" ;;
    proxy) cmd_proxy "$@" ;;
    proxy-install) cmd_proxy_install "$@" ;;
    proxy-status) cmd_proxy_status "$@" ;;
    smoke) cmd_smoke "$@" ;;
    shell) cmd_shell "$@" ;;
    codex) cmd_codex "$@" ;;
    config) cmd_config "$@" ;;
    help|-h|--help) usage ;;
    *) usage >&2; exit 2 ;;
  esac
}

main "$@"
