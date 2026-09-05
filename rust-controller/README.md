# Rust Controller: Authenticated Visibility and Fake Native Workflow

The library now has a durable fake-only native clone/configure/start workflow,
alongside the PostgreSQL journal, fenced scheduler, compatibility normalizer,
synthetic Ansible adapter, and persistent HTTP health service. Native mode and
literal `real` transport remain unavailable in the service. Observe mode can use
authenticated `http-observe` cluster visibility. Only the sealed in-memory fake
implements native mutation; the authenticated HTTP observer has read capabilities.
This is a local proof, not a production controller replacement.
Synthetic success says nothing about OOBE, enrollment, ESP, or usable-device readiness.

## Fake native proof

From the repository root, with the existing local Unix Docker endpoint and cached
`postgres:16-alpine` image available:

```bash
cargo run --locked --manifest-path rust-controller/Cargo.toml -p operation-controller --example native_fake
```

The example owns its disposable PostgreSQL container and in-memory PVE. It accepts
no DB/PVE destination, credential, CLI mode, or live transport selector; it rejects
`DOCKER_HOST` overrides and derives the database port only from its verified owned
container's literal loopback publication. The measured result is:

```json
{"final_power":"running","mutation_requests":3,"native_operations":3,"os_readiness_proven":false,"reservations":1,"satisfied_operations":3,"transport":"in_memory_fake"}
```

Success requires exact requested identity/configuration, unchanged source and boot
disk ownership, three separate satisfied operations with one send apiece, retained
reservation, and confirmed disposable-container cleanup. The budget is 30 seconds
of async work plus up to 200ms child cancellation/reaping and one three-second
container cleanup budget: 33.2 seconds total apart from OS scheduling delays.
Unconfirmed cleanup fails the proof; a possibly created container can remain when
ownership cannot be verified within that bound.

The native store and controller test fixtures share this private owned-container
lifecycle. Each fixture also bounds its complete setup, including database
readiness, connections, migrations, and authority initialization, to 30 seconds
of async work plus the same finite cancellation and cleanup grace.

## Source and verification mapping

All paths below are repository-relative. Test names identify executable local
evidence; YAML/Python references establish compatibility intent. These sources do
not independently confirm every response schema or the installed PVE release.

| Contract / approved requirement | Local source used | Verification in this slice |
|---|---|---|
| Clone form and target identity | `autopilot-proxmox/roles/proxmox_vm_clone/tasks/clone_vm.yml` | `native_contract::fixed_requests_apply_identity_preserve_disk_and_provenance` checks the exact clone form; `concurrent_identity_reservation_has_one_winner` and `occupied_foreign_vmid_is_preserved_without_send` check fixed reservation/collision behavior. The YAML's automatic alternative-VMID attempts are intentionally not migrated. |
| Typed config and separate root capability | `autopilot-proxmox/roles/proxmox_vm_clone/tasks/update_config.yml` | `fixed_requests_apply_identity_preserve_disk_and_provenance` checks seven config form keys, including digest; `changed_digest_alone_rejects_configure_without_refresh_or_resend` and `retained_marker_does_not_hide_changed_intermediate_identity_or_disk` reject conflicts. No args/delete/SSH capability exists. EFI/TPM/media and the source's root SSH args path are outside this subset. |
| Clone/config/start order | `autopilot-proxmox/roles/proxmox_vm_clone_linux/tasks/main.yml` | `full_slice_duplicate_intake_has_three_exact_sends` checks three separate durable operations and exact sends; `typed_configure_and_start_dispatches_use_owned_requests_and_receipts` checks prerequisite ownership. Source cloud-init/media and first-boot stages are not migrated. |
| API token wire header | `autopilot-proxmox/web/proxmox_client.py` (`_proxmox_api`) | `token_is_sent_once_only_in_sensitive_authorization_header`, `token_validation_and_debug_never_echo_input`, and `redirect_cannot_forward_token_or_issue_second_request` pass against local listeners. The Python source supplies header shape only; Rust transport tests and its verified rustls builder establish local TLS behavior, not Python's `verify=False`. |
| Observation freshness and task identity | `rust-controller/crates/pve-port/src/{lib,model,observer}.rs` | Existing observer suite plus `every_snapshot_enforces_thirty_second_freshness_and_rejects_future_facts`, `outcome_observations_must_postdate_dispatch_including_absence_and_power`, and `coherent_receipt_and_task_substitution_cannot_replace_persisted_upid`. |
| Immutable plan and fenced authority | `rust-controller/crates/postgres-store/src/{store,scheduler}.rs` | `semantic_plan_cannot_be_substituted_after_claim_or_by_concurrent_intake` and `authority_generation_cannot_aba_back_to_an_old_grant`, plus `concurrent_identity_reservation_has_one_winner`, `concurrent_cancel_and_dispatch_never_allow_downstream_send`, `old_generation_cannot_record_an_active_native_decision`, and `native_decision_checks_revision_and_generation`. |
| No public execution-event injection | `PgStore::append_event` in `store.rs` | Original public append restrictions plus native `public_append_remains_evidence_only`; only EvidenceRecorded can enter through that API. |
| Unknown and late results | Replacement design sections 8–10 | `crash_after_dispatch_never_grants_second_send`, `clone_response_loss_stays_unknown_and_never_advances`, `unknown_reconciliation_uses_current_generation_original_dispatch_and_receipt`, and `late_success_preserves_newer_conflict`. |
| Production no-touch | Replacement design sections 16, 20–21 | `native_startup_fails_before_local_io_even_with_read_permission`, `real_transport_startup_fails_before_local_io_even_with_read_permission`, and `remote_pve_and_database_startup_targets_are_denied`; credential denial has zero-request and private zero-client-build counters. Four compile-fail cases reject observer clone/configure/start and downstream mutation implementations. |

New node/storage/network/resource envelopes and fake clone provenance remain
synthetic contracts. Pinned official public source supports the selected
[node uptime shape](https://github.com/proxmox/pve-manager/blob/728286c79bd12f5bee5c9614651fe6e469da723a/PVE/API2/Nodes.pm#L501),
[usable-storage shape](https://github.com/proxmox/pve-storage/blob/7c6a03839920d4939a8ae725a2b0ef91c0cbc6c9/src/PVE/API2/Storage/Status.pm#L348),
and [`qmclone` source-VMID worker binding](https://github.com/proxmox/qemu-server/blob/6c0127e612f6c576888a13f9bfb30874911b804d/src/PVE/API2/Qemu.pm#L4749).
This does not verify the installed release. The separate node/network visibility
contracts preserve [optional network `active`](https://github.com/proxmox/pve-manager/blob/728286c79bd12f5bee5c9614651fe6e469da723a/PVE/API2/Network.pm#L282).
The separate visibility parser accepts mixed QEMU/LXC and RRD-incomplete inventories.
Because of [`VM.Audit` visibility filtering](https://github.com/proxmox/pve-manager/blob/728286c79bd12f5bee5c9614651fe6e469da723a/PVE/API2/Cluster.pm#L587),
visibility coverage always remains `unverified`. Existing native
preflight rejection remains conservative. Official schema verification or
sanitized observation within the existing read-only authorization is still needed
before a future real-PVE artifact. The fake provenance marker has no live wire
representation; a real clone ownership mechanism remains unimplemented. No raw
production inventory, payload, credential reference, or secret is copied into fixtures.

`PveInfrastructureVisibilityReadPort` exposes only `node_visibility` and
`network_visibility`. Its HTTP observer makes one authenticated GET per call to
the validated node's `/status` or `/network` endpoint, with no query, discovery,
retry, redirect, or proxy. Request, body collection, and envelope decoding share
a two-second timeout even with a longer configured client timeout; the body is
limited to 1 MiB and network data to 1,024 rows. Cancellation drops collection.
Node uptime may be absent or zero. Network observations preserve OVS bridges and
unknown activity, count malformed rows, and reject duplicate or cross-node
identities. Both reports remain `unverified` and confer no native execution
authority. The `infrastructure_visibility_http` loopback suite exercises these
contracts through the narrow trait; the Dockerfile's full PVE test command also
discovers this suite. Service collection additionally supports one explicitly
selected node, with independent bounded scheduling and per-component freshness.
Storage visibility and activation remain outside this contract.

Local macOS ARM64 Task 6 validation passed 308 unique workspace tests, including
11 compile-fail checks (4 at the PVE capability boundary), with no failures or
ignored tests. The named mapping tests above all passed in that run. Producer and
proof regressions passed 8 Python tests; the coordination file requires unittest
discovery because running it directly does not execute its test. The example
command took 4.25 seconds including 1.90 seconds compilation in this run.

## Declared artifact identity and supplied bytes

The pure `artifact-index` native-v1 library admits non-nil opaque artifact UUIDs,
literal `amd64`, a 14-ASCII-digit build label, normalized SHA-256 declarations,
and positive image indices. The current builder's timestamp-shaped `build_sha`
label is neither a source digest nor a content digest; this library validates its
shape without calendar authority. It does not parse or tighten existing API rows
or legacy manifests.

A present output index controls the apply index; a missing output falls back to
the source index, and a present zero is invalid. Canonical fingerprints bind the
complete serialized descriptor, including index provenance, contract version 1,
and fixed `declared_only` evidence. Private fields and serialization-only outputs
preserve construction ownership; they are not a security boundary against
malicious code running in the same process.

Comparing supplied nonempty ISO/WIM slices checks their SHA-256 values directly.
The report retains only the descriptor fingerprint and exact lengths, with fixed
`supplied_bytes_matched` evidence and `publication_verified: false`. This proves
agreement of those slices with the declarations, without establishing origin,
file stability, PVE publication, WIM validity, bootability, or readiness. Declared
hashes, supplied-byte matches, published locations, and boot/readiness proofs
remain separate claims. Neither output confers native execution authority or
activates a service.

Run the focused offline suite, including construction-boundary compile-fail docs:

```bash
cargo test --offline --locked --manifest-path rust-controller/Cargo.toml -p artifact-index
```

The test image includes this pure suite. Actual Linux execution and exact artifact
acceptance require the separate committed-source artifact gate.

## Local proof

Run from the repository root with Rust 1.92, Docker/Compose, and the approved native
Ansible installation available. Dependency resolution is pinned by Cargo.lock.

```bash
cargo fmt --manifest-path rust-controller/Cargo.toml --all -- --check
cargo clippy --locked --manifest-path rust-controller/Cargo.toml --workspace --all-targets --all-features -- -D warnings
RUST_TEST_THREADS=2 cargo test --locked --manifest-path rust-controller/Cargo.toml --workspace --all-features --no-fail-fast
cargo deny --manifest-path rust-controller/Cargo.toml check
python3 rust-controller/scripts/test_producer_contract.py
python3 rust-controller/scripts/test_proof_wait.py
python3 rust-controller/scripts/test_proof_coordination.py
python3 -m unittest discover -s rust-controller/scripts -p test_proof_coordination.py -v
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

Task 6 local macOS ARM64 validation runs the whole workspace, including actual
owned PostgreSQL native tests. `RUST_TEST_THREADS=2` bounds fixture contention;
explicit competing-worker and transaction-race tests still exercise concurrency.
The Linux AMD64 image compiles every workspace test target and release example,
then executes PVE tests, pure `operation-controller --test decision`, and exactly
`operation-controller --test postgres_native support:: -- --test-threads=1`.
The nine filtered support tests use fixed local Unix child helpers and no Docker.
The unfiltered native PostgreSQL binary and `native_fake` example require their
own Docker fixture and are not executed inside the image. Linux actual database
execution here is the foundation Compose/native adapter proof; Linux native
controller PostgreSQL end-to-end remains a separate artifact gate. No host Docker
socket is mounted. Exact committed-source checksums, image/binary hashes, test
counts, logs and cleanup evidence are retained in the native Task 6 handoff report.
Cargo-deny can emit the known duplicate-dependency baseline warnings; passing
policy checks do not mean warning-free output.

The authenticated observation Dockerfile also selects service configuration,
credential, health, GET-loop cadence/cancellation, startup-denial and loopback HTTP
tests, including selected-node collector, component freshness, cadence and actual
HTTP service behavior with an unavailable database. It explicitly excludes both
service+owned-PostgreSQL HTTP tests, which need Docker on their host. Those tests
provide macOS-local evidence;
Linux compilation and the existing Compose database proof do not establish Linux
HTTP-service PostgreSQL end-to-end acceptance. Rebuild and run the final committed
image after review before making an exact artifact claim.

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
produces rejected compatibility without claiming work. A SQLx transaction guard
and an outer connection guard recycle a session only after acknowledged rollback;
cancellation during BEGIN, a read, or ROLLBACK discards the connection.
The observer's successful sweep timestamp records completion of those actual reads.

GET /healthz is process liveness, independent of PostgreSQL. GET /readyz queries
the store with a two-second bound and returns structured JSON. It is 503 when the
database/schema is unavailable, authority is absent or mismatched, the latest sweep
failed or is older than 15 seconds, the outbox exceeds 1,000 pending events, or its
oldest undelivered event exceeds 300 seconds. Unavailable counts are null. The
fixed operational_failure field also makes readiness503 after unexpected adapter
preparation, spawn, process, or worker-join failures. This failure stays latched
until restart after repair; subsequent successful reads cannot clear it. New
claims stop while current-authority reaping and health reads continue. Normal
cancellation, timeout, and authority-loss reports retain their execution semantics.
The outbox fields report backlog health; no external outbox delivery sink is wired in
this foundation, so a long-lived undrained deployment eventually becomes unready.

Health includes version, build Git SHA, mode, actual authority executor/generation,
configured generation, database observation time, lease counts, oldest pending age,
outbox counts/age, successful sweep count/time, validated adapter version, transport
kind, and blocked/unknown/conflicted counts. It excludes DSNs, URLs, tokens,
worker/VM/operation identities, and payloads. Build Git SHA identifies the checked-out
revision; precommit local builds may also contain working-tree edits. Startup and
runtime failures never print raw dependency error chains.
Network-boundary startup rejection emits the fixed label
`controller startup rejected by network boundary`; other startup/runtime failures
retain the generic sanitized message. The remote startup matrix requires this
specific category, so a later missing adapter setting cannot masquerade as target
rejection. Neither label includes an address, credential, or dependency error.

## Service configuration and adapter limits

Required in every mode:

- RUST_CONTROLLER_MODE: observe or adapter; parsed native fails startup.
- RUST_CONTROLLER_DATABASE_URL: PostgreSQL URL; literal loopback without the explicit observe read opt-in described below.
- RUST_CONTROLLER_PVE_BASE_URL: validated HTTP(S) URL; the same target restrictions apply.
- RUST_CONTROLLER_AUTHORITY_GENERATION: positive expected generation; never bootstrapped.

RUST_CONTROLLER_LISTEN defaults to 127.0.0.1:9090 and must be loopback.
RUST_CONTROLLER_PVE_TRANSPORT defaults to fake.
The additional exact value `http-observe` is accepted only in observe mode.
The literal `real`, unknown selectors, and native mode remain denied. Labels report
the constructed capability. Fake transport labels are not device-readiness evidence.

`http-observe` requires `RUST_CONTROLLER_PVE_TOKEN_FILE`, a dedicated regular JSON
file with exactly `token_id` and `secret`, owned by the service's effective user,
with no group/other permissions (normally mode 0600), and at most 8192 bytes.
The final symlink, FIFO, directory, duplicate/unknown field and oversized-file
cases are rejected. The file is opened once after mode and destination validation;
there is no raw-secret environment fallback or credential reload. Remote observation
requires the existing explicit read opt-in and HTTPS; literal loopback HTTP serves
owned fixtures. No redirect, proxy, insecure TLS or additional PVE endpoint is enabled.

The isolated HTTP loop issues one GET of `/api2/json/cluster/resources?type=vm`
every 5 seconds, skips missed ticks, never overlaps collections and drops an in-flight
collection on shutdown. Request and whole-collection bounds are 2 and 3 seconds;
bodies are capped at 1048576 bytes and inventory at 1024 rows. It owns only the
visibility-read capability and sanitized in-memory progress. The existing database
compatibility reads continue independently with SELECT-only role checks and rollback.

`pve_observation` reports current classified status, nullable counts/timestamp,
`coverage: unverified`, monotonic freshness (15 seconds), and a retained last-success
timestamp. Failed results have null counts/timestamp; an old success cannot make
the current failure ready. `observation_ready` requires a current fresh complete
summary and can be true while the database is unhealthy. Rejected rows are degraded.
This proves visible inventory only, never absence, uniqueness, ownership, complete
permission coverage, native evidence or execution authority.

The optional `RUST_CONTROLLER_PVE_OBSERVE_NODE` selects exactly one validated node
in observe/http-observe mode. Absence preserves cluster-only collection. Empty,
invalid and non-Unicode values, or any value with fake transport, fail before
credential or client I/O. The existing protected token load is shared by cloning
the loaded token; selection never reopens the credential file.

With selection, a separate immediate-first-tick loop runs every 10 seconds with
missed ticks skipped. It issues only the selected node's status GET followed by its
network GET, at most once each per collection, with 2-second component and 5-second
whole-pair bounds. It never overlaps collections and drops an in-flight read on
shutdown. The cluster loop retains its 3/5/15-second collection/cadence/freshness
policy and proceeds independently of this loop and database reads.

`infrastructure_observation` contains sanitized `node` and `network` components,
each with current status, nullable observed timestamp and summary, monotonic
`fresh`, and retained `last_success`. Node summary reports only whether uptime
was known; network summary contains interface-kind/activity/rejection counts.
Each component expires 30 seconds after its own response completion. Current
failure or degraded rows cannot inherit freshness from a retained success.
`infrastructure_observation_ready` requires both fresh complete components and
does not change cluster `observation_ready` or execution `ready`. Coverage remains
`unverified`; identifiers, addresses, raw types, credentials and completion-clock
values are excluded. Unselected/fake services report null infrastructure and false
infrastructure readiness.

For `http-observe`, existing `ready` remains false and `/readyz` returns 503 even
when `observation_ready` is true. Inspect its JSON using `curl -sS` (without `-f`).
The evidence label is `visibility_only_coverage_unverified`. Successful GETs cannot
heal database/outbox failures or latched execution faults. Fake readiness retains
its existing policy. No health endpoint or execution selector is added.

Run the owned macOS integration proof with:

```bash
env -u RUST_CONTROLLER_TEST_DATABASE_URL -u DOCKER_HOST -u DOCKER_CONTEXT cargo test --locked --manifest-path rust-controller/Cargo.toml -p controller-service --test service observation_service::authenticated_service
env -u RUST_CONTROLLER_TEST_DATABASE_URL -u DOCKER_HOST -u DOCKER_CONTEXT cargo test --locked --manifest-path rust-controller/Cargo.toml -p controller-service --test service observation_service::infrastructure_service:: -- --test-threads=1
```

Its setup and complete test have 30 and 55 second async bounds, plus finite owned
cleanup grace. The test uses populated native journal/attempt/lease/dispatch/receipt
and legacy-job sentinels, exact before/after row snapshots, a SELECT-only role with
advisory-lock access revoked, and complete owned-container SQL logs. Log capture
verifies container ownership and fails on incomplete collection, a 3 second deadline,
or a 256 KiB cap; it never trims the trace. Sampled service process trees contain no
children; the typed HTTP loop additionally has no runner or store capability.
No production credential, controller, PVE endpoint or tenant is used by this proof.

The selected-node PostgreSQL proof has an 85-second whole-test bound including
the existing 30-second setup. Three node and three network GETs cover fresh,
network-only 401, and recovery while cluster visibility remains fresh; the
independent cluster loop is bounded to 4–6 GETs. Health polling is at most once per
second, and the existing complete SQL allowlist, 256 KiB log cap, 15-table snapshots,
sampled childless service process and exact owned-container cleanup apply. A
separate 12-second actual-service test uses only owned HTTP plus an unavailable
loopback database to prove both observation loops remain fresh with database,
outbox and execution readiness false and zero successful database sweeps.

Multi-node scheduling, storage/artifact observation, real write provenance and retry/reservation
policy, media/firmware/TPM/QGA, OSDeploy/CloudOSD/agent migration, shared Python fencing,
restore and approved non-production proof remain later Rust slices. Cutover requires
separate approval; deferred product tracks follow stable controller contracts.

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
setup explicitly normalizes Debian's trusted Python shebang before native tests.
Both native jobs run the actual adapter registry preflight through the
verify_adapter example and fail closed if their installed runtime violates it.
Hosted workflow execution and local execution are separate claims; see the checked-in Task 9
evidence report for what actually ran. No workflow publishes an image or package.
