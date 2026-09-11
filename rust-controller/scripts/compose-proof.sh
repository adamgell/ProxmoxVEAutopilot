#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
proof_project="rust-proof-$(date +%s)-$$"
compose=(docker compose -p "$proof_project" -f docker-compose.test.yml)
cleanup() { "${compose[@]}" down --volumes --remove-orphans >/dev/null; }
trap cleanup EXIT
"${compose[@]}" up -d worker-1 worker-2 worker-3 observer
proof_owner=$("${compose[@]}" run --rm -T proof cancel-and-select)
selection_deadline=$((SECONDS + 45))
while true; do
  case "$proof_owner" in worker-1|worker-2|worker-3) ;; *) exit 1 ;; esac
  # Freeze the entire selected container before confirming the running lease:
  # a short synthetic child can otherwise finish during Docker command startup.
  "${compose[@]}" pause "$proof_owner"
  if "${compose[@]}" run --rm -T proof confirm-paused-running "$proof_owner"; then
    break
  else
    selection_status=$?
    "${compose[@]}" unpause "$proof_owner"
    # Only an already-finished selection is retryable, never a safety failure.
    if [[ "$selection_status" != 3 || "$SECONDS" -ge "$selection_deadline" ]]; then
      exit 1
    fi
    proof_owner=$("${compose[@]}" run --rm -T proof select-running)
  fi
done
"${compose[@]}" kill -s SIGKILL "$proof_owner"
"${compose[@]}" run --rm -T proof verify
"${compose[@]}" run --rm -T --entrypoint cargo -e RUST_CONTROLLER_TEST_DATABASE_URL=postgresql://postgres:local-proof@127.0.0.1:5432/rust_controller_test proof test --offline --locked --manifest-path rust-controller/Cargo.toml -p ansible-adapter --lib --test native -- --test-threads=1
