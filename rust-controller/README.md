# Rust Controller Foundation

This local foundation provides a PostgreSQL journal, fenced scheduler, compatibility
normalizer, fake PVE evidence, one synthetic Ansible adapter, and a persistent
HTTP health service. It has **no native PVE mutation and is not production-ready**.
Synthetic success says nothing about OOBE, enrollment, ESP, or usable-device readiness.

## Local proof

Run from the repository root with Rust 1.92, Docker/Compose, and the approved native
Ansible installation available. Dependency resolution is pinned by Cargo.lock.

```bash
cargo fmt --manifest-path rust-controller/Cargo.toml --all -- --check
cargo clippy --locked --manifest-path rust-controller/Cargo.toml --workspace --all-targets --all-features -- -D warnings
cargo test --locked --manifest-path rust-controller/Cargo.toml --workspace --all-features --no-fail-fast
cargo install cargo-deny --version 0.19.0 --locked
cargo deny --manifest-path rust-controller/Cargo.toml check
docker pull postgres:16-alpine
docker build --platform linux/amd64 --build-arg CONTROLLER_GIT_SHA="$(git rev-parse HEAD)" -f rust-controller/Dockerfile.test -t rust-controller-task9:local .
docker compose -f rust-controller/docker-compose.test.yml config --quiet
bash rust-controller/scripts/compose-proof.sh
```

The script creates a unique Compose project, PostgreSQL 16 in tmpfs, three real
adapter workers, and a SELECT-only observer. The workers and PostgreSQL share an
isolated network namespace so all targets remain literal loopback addresses.
The network is internal; published health ports bind only host loopback.
No Docker socket is exposed inside containers. The script proves three ready
workers compete, at most two leases are active, each selected operation has one
attempt, unrelated fingerprints stay pending, cancellation reaches unknown,
and a killed running worker reaches unknown after actual 30-second lease expiry.
It then executes Linux native adapter and /proc descendant-cleanup tests.
An EXIT trap removes the project's containers, network, and disposable data.
The local image and build cache remain; nothing is published.

The image pins the official Rust 1.92 Bookworm AMD64 digest and the Ansible/Python
database-driver package versions. Debian transitive packages and CI bootstrap
tools resolve through their package managers; those are not a fully hermetic OS
snapshot. Ansible is installed only in this disposable image on Linux.

## Observe and health

After building the local image, an isolated observer can be inspected directly:

```bash
docker compose -p rust-local -f rust-controller/docker-compose.test.yml up -d observer
docker compose -p rust-local -f rust-controller/docker-compose.test.yml exec observer curl -fsS http://127.0.0.1:9094/healthz
docker compose -p rust-local -f rust-controller/docker-compose.test.yml exec observer curl -fsS http://127.0.0.1:9094/readyz
docker compose -p rust-local -f rust-controller/docker-compose.test.yml down --volumes
```

The separate seed example performs disposable schema/authority bootstrap. Service
startup never migrates or initializes authority. Observe mode verifies a
non-superuser SELECT-only jobs role inside a read-only transaction, normalizes one
pending synthetic compatibility row, rolls back, and reads typed aggregate health.
It never constructs a scheduler/adapter or spawns a process. An empty jobs table
produces rejected compatibility without claiming work. The observer's successful
sweep timestamp records completion of those actual reads.

GET /healthz is process liveness, independent of PostgreSQL. GET /readyz queries
the store with a two-second bound and returns structured JSON. It is 503 when the
database/schema is unavailable, authority is absent or mismatched, the latest sweep
failed or is older than 15 seconds, the outbox exceeds 1,000 pending events, or its
oldest undelivered event exceeds 300 seconds. Unavailable counts are null. The
outbox fields report backlog health; no external outbox delivery sink is wired in
this foundation, so a long-lived undrained deployment eventually becomes unready.

Health includes version, build Git SHA, mode, actual authority executor/generation,
configured generation, database observation time, lease counts, oldest pending age,
outbox counts/age, successful sweep count/time, validated adapter version, transport
kind, and blocked/unknown/conflicted counts. It excludes DSNs, URLs, tokens,
worker/VM/operation identities, and payloads. Build Git SHA identifies the checked-out
revision; precommit local builds may also contain working-tree edits. Startup and
runtime failures never print raw dependency error chains.

## Service configuration and adapter limits

Required in every mode:

- RUST_CONTROLLER_MODE: observe or adapter; parsed native fails startup.
- RUST_CONTROLLER_DATABASE_URL: PostgreSQL URL with literal loopback host.
- RUST_CONTROLLER_PVE_BASE_URL: validated HTTP(S) URL with literal loopback host.
- RUST_CONTROLLER_AUTHORITY_GENERATION: positive expected generation; never bootstrapped.

RUST_CONTROLLER_LISTEN defaults to 127.0.0.1:9090 and must be loopback.
RUST_CONTROLLER_PVE_TRANSPORT defaults to fake; any other value fails startup.
The service constructs only the in-memory fake and performs no PVE network request.
Fake transport labels are not device-readiness evidence.

Adapter additionally requires RUST_CONTROLLER_WORKER_ID,
RUST_CONTROLLER_SYNTHETIC_CAP (1–32, identical on workers sharing a workflow), and
RUST_CONTROLLER_SYNTHETIC_JOB (a sanitized JSON job fixture up to 64 KiB).
The normalized fixture is bound to its exact job identity, duration, contract, and
canonical fingerprint. The atomic claim filters by fingerprint and contract;
the existing start transaction verifies the binding again. No arbitrary queued
payload or command can become an executable. A general plan store/intake API is
outside this foundation; changing the fixture selects a different plan.

The sole registry entry is ansible:/app/playbooks/_test_long_sleep.yml@1 with an
integer duration from 0 through 20. macOS uses /opt/homebrew/bin/ansible-playbook;
Linux uses /usr/bin/ansible-playbook, with a concrete trusted Python 3 shebang.
The unchanged playbook must exist at its compiled repository path. SIGTERM/Ctrl-C
stop new claims and request bounded in-flight cancellation; a lost worker relies on
current-authority lease recovery. The descendant boundary is trusted installed
Ansible, not hostile arbitrary programs.

## Network and CI boundaries

```bash
docker run --rm --network none --platform linux/amd64 rust-controller-task9:local cargo test --offline --locked --manifest-path rust-controller/Cargo.toml -p pve-port
```

The network-deny tests reject remote targets before connection, including the
production address 192.168.2.4. Existing configuration permits explicit remote
reads only when observe and RUST_CONTROLLER_ALLOW_PRODUCTION_READS=true are both
set. This is not enabled by any test or Compose service. Adapter/native remote
targets always fail. No production, tenant, VM, or deployment operation belongs
to this proof.

The workflow configures fmt, Clippy, all tests, cargo-deny, repeated PostgreSQL
races/crash tests, Linux AMD64 release and Compose proof, and macOS ARM64 native
tests. The macOS job boots disposable Colima/QEMU for PostgreSQL. Hosted workflow
execution and local execution are separate claims; see the checked-in Task 9
evidence report for what actually ran. No workflow publishes an image or package.
