#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
proof_project="rust-proof-$(date +%s)-$$"
compose=(docker compose -p "$proof_project" -f docker-compose.test.yml)
cleanup() { "${compose[@]}" down --volumes --remove-orphans >/dev/null; }
trap cleanup EXIT
"${compose[@]}" up -d worker-1 worker-2 worker-3 observer
proof_owner=$("${compose[@]}" run --rm -T proof cancel-and-select)
case "$proof_owner" in worker-1|worker-2|worker-3) ;; *) exit 1 ;; esac
"${compose[@]}" kill -s SIGKILL "$proof_owner"
"${compose[@]}" run --rm -T proof verify
"${compose[@]}" run --rm -T --entrypoint cargo -e RUST_CONTROLLER_TEST_DATABASE_URL=postgresql://postgres:local-proof@127.0.0.1:5432/rust_controller_test proof test --offline --locked --manifest-path rust-controller/Cargo.toml -p ansible-adapter --lib --test native -- --test-threads=1
