# Task 9 implementation and local proof report

Base revision: `a048b7a37056bf73a5c8efa857420acae7820cee`. Implemented locally on the existing isolated Rust-controller worktree. This report records implementation and executed checks; acceptance remains subject to independent review.

## Delivered behavior

- Persistent Axum service with GET /healthz and GET /readyz, loopback listener, explicit positive configured generation, fail-closed native/real-transport selection, and bounded graceful cancellation on SIGTERM/Ctrl-C.
- Health queries current PostgreSQL facts through a typed aggregate `PgStore::health_snapshot()`. The private pool remains private. One MVCC SELECT returns authority, active/expired lease counts, pending/outbox ages, backlog, and blocked/unknown/conflicted counts. Database errors become unavailable facts, never raw error text.
- Readiness needs a successful actual sweep within 15 seconds, latest sweep success, matching Rust authority/generation, accessible schema/database, at most 1,000 undelivered outbox events, and oldest undelivered age at most 300 seconds. No startup timestamp or constant readiness substitutes for progress. Dependency loss leaves process liveness 200.
- Observe constructs no scheduler/adapter. Each sweep performs the existing SELECT-only role verification/read-only compatibility transaction, rolls back, and reads aggregate health. It performs no migration, authority bootstrap, dequeue, journal write, or process spawn.
- Adapter service validates an explicitly configured synthetic JSON fixture through the accepted normalizer and registry. Atomic `claim_next_bound()` filters workflow, contract version, and fingerprint under the existing authority/cap transaction. Unrelated plans stay pending. Existing bound start still revalidates the persisted command. Actual fenced reaping and claim passes drive progress while at most one adapter run per worker executes concurrently.
- Fake PVE is explicitly constructed in memory. No runtime real-PVE selector or PVE network request is available. Health labels fake evidence as synthetic and unrelated to device readiness.
- Health exposes fixed structured metadata/counts/timestamps and adapter contract versions. It excludes URLs, DSNs, tokens, worker/operation/VM identities and payloads. Startup/runtime failures use a fixed sanitized label.
- Disposable Compose PostgreSQL, three worker processes, and a SELECT-only observer share an internal network namespace. All targets remain literal loopback and all published health ports bind host loopback. No Docker socket is exposed. Separate seed/control examples own disposable setup and operator cancellation.
- CI configures fmt, warning-denied Clippy, all tests, cargo-deny, repeated PostgreSQL proof, Linux AMD64 release/Compose, and macOS ARM64 tests. No package/image publication step exists. All crates inherit the repository's existing MIT license and are marked publish=false.

## Test-first and repair evidence

1. Bound-claim regression first failed because the API was absent, then failed executably against a forwarding stub which claimed an unrelated digest. The filtered transaction passed and preserves wrong-version/wrong-fingerprint pending work.
2. New health tests failed against an unavailable-only response skeleton: healthy observed dependencies incorrectly returned 503 and structured health was missing. They passed after the real policy/serializer implementation. Cases include DB loss, missing/mismatched authority, absent/stale sweep, backlog, and real transport.
3. Typed aggregate test initially failed on the absent API; it then proved real authority/counts through a SELECT-only, default-read-only role and a controlled failure after pool closure. The expected outbox count was corrected from three to one because command append does not emit outbox rows; the single lease event does.
4. Actual service subprocess test failed against the old one-shot entrypoint, then passed with a persistent liveness200/readiness503 server when the DB is unreachable. Secret canaries do not enter the response.
5. Linux exposed two fixture/runtime gaps. The old 10ms PVE timeout test could expire before headers arrived under AMD64 emulation. Only fixture timing changed to a 100ms deadline and 500ms delayed response, retaining the timeout and exact-one-request assertions. Linux native process tests rejected Debian's concrete but space-prefixed `#! /usr/bin/python3` shebang. Only the disposable installed entrypoint was normalized to `#!/usr/bin/python3`; accepted adapter production code stayed unchanged.
6. Early Compose invocation failed because a preliminary image lacked the newly added seed executable; cleanup succeeded. First complete scenario reached all requested final state/cap/ownership/cancellation/recovery assertions, then failed an extra fairness assumption requiring all three workers to win. Cap enforcement promises no fairness while a dead worker retains a lease slot. The proof now requires three independently ready competitors and multiple distinct winners, and keeps every requested safety assertion. The final run happened to have all three win.

## Executed checks

All commands ran locally; no hosted GitHub Actions run was dispatched.

| Check | Actual result |
| --- | --- |
| Rust host | rustc 1.92.0, aarch64-apple-darwin |
| Required MCP status and docs search | Live status/docs inventory reachable; new local Rust spec/plan absent from live inventory, so approved worktree spec and Task 9 brief governed |
| cargo fmt --all -- --check | PASS |
| cargo clippy --offline --locked --workspace --all-targets --all-features -- -D warnings | PASS |
| cargo test --offline --locked --workspace --all-features --no-fail-fast | PASS: 161 tests plus 7 compile-fail doctests; no failed or ignored tests |
| macOS native adapter | 12 unit/process, 4 allowlist, 2 binding, 4 native/PostgreSQL, and 1 compile-fail doc test passed in full workspace run |
| macOS observer | SELECT-only SQL trace/zero writes, no child process, superuser rejection and sanitizer tests passed |
| PostgreSQL repeat | Store 21 tests passed again in 20.11s; scheduler 24 tests passed again in 23.35s |
| cargo deny 0.19.0 check | advisories ok, bans ok, licenses ok, sources ok; duplicate-version warnings remain permitted policy, no advisory suppression |
| Docker Compose config | PASS |
| Linux AMD64 release build | Actual native x86_64-unknown-linux-gnu Rust1.92 compile in an AMD64 container; final example release build4m12s, service release build2m16s, adapter test compilation1m30s |
| Linux network-none PVE | 22 unit tests +5 network-deny tests passed; corrected PVE test source was bound read-only into disposable AMD64 container and tests ran with --offline --locked, --network none, --test-threads=1 |
| Three worker Compose | PASS, exact output below |
| Linux adapter unit/process | 12 passed in5.47s inside isolated Compose namespace, including actual /proc descendant/drop cleanup and deterministic lifecycle coverage |
| Linux native/PostgreSQL adapter | 4 passed in13.08s: synthetic success, bounded cancellation, authority-loss/no stale finalization, current-authority recovery and plan mismatch preservation |
| git diff --check | PASS |

Full workspace tests ran once after service implementation stabilized. The later PVE test-timing-only correction was verified by the focused native timeout test and the entire Linux network-none PVE suite. PostgreSQL tests were repeated specifically to satisfy the required repeat/crash gate. Remaining broader pre-existing Python/React/PowerShell/.NET suites and backup/restore rehearsal from the overarching design were not rerun or newly claimed by this Rust foundation task.

## Concrete Compose outcome

```json
{"attempts_per_operation_max":1,"cap":2,"observer_sweeps":39,"recovery":"real_30_second_lease_expiry","result":"PASS","states":{"pending":1,"satisfied":4,"unknown":2},"transport":"fake","workers_claimed":3,"workers_started_and_ready":3}
```

Three independent service listeners passed readyz before fault injection. The proof requested cancellation through the real scheduler, killed a running worker with SIGKILL, continuously checked live cap counts, waited for the real lease expiry, checked the durable journal/attempt projections, and verified the unrelated fingerprint stayed pending. No test shortened that lease in SQL. The observer reported real sweep/aggregate timestamps; Docker's process inventory showed only its controller process. Stable fixture fingerprints were asserted during typed seeding against reordered JSON plus the existing independently established golden prefix.

Every Compose attempt invoked its EXIT cleanup, removing its specific containers, internal network and disposable data. Native PostgreSQL fixture containers also removed themselves. The reusable local proof image and build cache remain available; no image was pushed.

## Build provenance and limits

- Base Rust image: `rust:1.92-bookworm@sha256:e90e846de4124376164ddfbaab4b0774c7bdeef5e738866295e5a90a34a307a2`.
- Compose PostgreSQL16 image: `postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777`.
- Compiled image before runtime normalization: `sha256:ce0caec87f0020abe77aae7bb9533dc5528f6f3ef434e55c4a004bdf4f5e8fbe`.
- Executed local runtime image: `sha256:05bdb431a6630b2c5e5e4577e083a1e630d49988b2f72d6d3199377c4c9956ef`.
- Executed release binary SHA256: `05bc143d08eb53c3c4fb926290c177482dde27ef2c10e7bff74011d368883c87`; verified ELF64 x86-64, dynamically linked GNU/Linux binary.
- Cargo.lock SHA256: `e74d17cce79ee62adaff77ad5f77a4e34d02fcebb1c7fad07aee57fb05bdb311`.
- Runtime installation: Debian ansible-core2.14.18-0+deb12u2, Python3.11, python3-psycopg2 2.9.5-1+b1. No host Ansible installer was run. The entrypoint normalization is included in Dockerfile.test.
- Actual build used Dockerfile.test with verified base revision as CONTROLLER_GIT_SHA, then a small --network none layer normalized the installed shebang and copied the PVE test timing correction, preserving the compiled service binary. That incremental layer was equivalent to the final Dockerfile's normalization step; production Rust source did not change after the release compile. The final proof Python script was mounted read-only from the checkout. The conventional one-pass final Dockerfile path is configured for CI; it was not redundantly rebuilt after documentation/test-fixture-only edits.
- The health Git SHA is the checked-out base revision at compilation; the binary includes this task's then-uncommitted source changes. This is local precommit proof, not a released artifact or a claim that the base commit alone contains the implementation.
- Dependency downloads used Cargo.lock. Debian transitive packages and CI bootstrap tools are package-manager resolved, not a fully hermetic OS snapshot. CDLA-Permissive-2.0 is explicitly allowed for the webpki root data; local path dependencies are allowed while unknown registry/git sources and registry wildcard dependencies are denied.
- The GitHub workflow is configured, not remotely executed. macos-15 is documented as ARM64 in [GitHub runner documentation](https://docs.github.com/en/actions/reference/runners/github-hosted-runners). The hosted Colima/QEMU bootstrap has not been exercised here; actual macOS proof used the existing local Docker runtime and approved installed Ansible.
- No generalized queue-plan storage/intake is present: each worker accepts only its configured synthetic fixture and exact fingerprint. No external outbox delivery sink is wired, so undrained backlog ages eventually make readiness503. Real PVE/native mutation, production deployment, tenant/Graph operations and publication remain unavailable or untouched. This foundation is not production-ready.

## Task 9 review repair round 1

Repair base: `a71569f38ffeab890660463d47423f0671e8c5d3`. This section supersedes current-behavior/provenance details above where explicitly changed; original execution evidence remains historical. The work stayed local and was not published or deployed.

### Findings and meaningful failing proofs

- Observe cancellation: a real PostgreSQL AccessExclusive lock held the observer at its pending-job SELECT. Aborting that future against the original raw BEGIN implementation returned an open read-only transaction to a one-connection pool: the regression observed `transaction_read_only=on` where `off` was required. The repair uses SQLx's read-only transaction plus an outer pool-connection guard. Only acknowledged rollback recycles the session; cancellation/error before that detaches and closes it, including cancellation during BEGIN/ROLLBACK. GREEN verifies idle reuse, zero remaining locks for the interrupted backend, and a subsequent successful observe sweep. The existing exact SQL audit still passes with BEGIN READ ONLY, two SELECTs, ROLLBACK and zero mutation. The broader focused run exposed cross-test Docker children in the old process inventory assertion; that test now runs its unchanged before/after inventory in its own test subprocess.
- Operational health: an actual adapter invocation was validated against a temporary copy of the approved fixture, then that temporary playbook alone was removed. The real runner failed preparation before any durable process start. A discarded-result implementation returned readyz200, as did actual Tokio panic/abort join failures. GREEN returns503 with fixed `adapter_preparation`/`worker_join` labels, without the temporary path or panic canary in JSON, even after a later successful database sweep. Spawn/process fault reports also latch sanitized faults; normal cancellation, timeout and authority-loss reports do not. The worker stops new claims after a fault while continuing fenced reaping/reads. Recovery of this operational latch requires repair and restart.
- Readiness waiter: actual loopback HTTP503 and connection refusal escaped the original polling helper and failed the new tests. GREEN retries only HTTP503/refusal to a monotonic deadline. Persistent failure is bounded; malformed JSON, HTTP401 and safety assertions fail immediately. No broad exception suppression was added.
- The first two repaired Compose runs hit the final-state deadline. Live inspection on the second found healthy surviving workers with no operational fault, zero remaining leases and five satisfied/one unknown: a selected five-second operation had finished between selection and Docker SIGKILL. The integration harness now pauses a candidate container, verifies its still-running/unexpired durable lease, then kills it. An already-finished candidate is resumed and selected again within a bound; other failures abort. A command-boundary regression first demonstrated the missing pause/confirm sequence and then passed the retry ordering. Neither synthetic duration, lease expiry nor recovery deadline was increased. Both failed projects were removed by EXIT cleanup.

### Exact repair checks

All commands below ran from the isolated worktree. No hosted GitHub Actions execution is claimed.

| Command | Result |
| --- | --- |
| `cargo test --offline --locked --manifest-path rust-controller/Cargo.toml -p controller-service` | PASS: 17 unit/integration tests in6.89s plus1 actual service subprocess test in2.52s; no failures/ignored |
| `cargo clippy --offline --locked --manifest-path rust-controller/Cargo.toml -p controller-service --all-targets --all-features -- -D warnings` | PASS |
| `cargo fmt --manifest-path rust-controller/Cargo.toml --all -- --check` | PASS |
| `python3 -m unittest discover -s rust-controller/scripts -p 'test_*.py' -v` | PASS: 6 tests in2.943s, including real HTTP/refusal and shell coordination regression |
| `bash -n rust-controller/scripts/compose-proof.sh` | PASS |
| `cargo run --offline --locked --manifest-path rust-controller/Cargo.toml -p controller-service --example verify_adapter` | PASS on actual macOS; validates trusted entrypoint/fixture, launches no process |
| `cargo deny --manifest-path rust-controller/Cargo.toml check` | PASS: advisories/bans/licenses/sources ok. Existing duplicate-version warnings and unmatched CDLA-Permissive-2.0 license allowance warning remain documented; no risk-hiding suppression |
| `docker compose -f rust-controller/docker-compose.test.yml config --quiet` | PASS |
| `docker build --network none --platform linux/amd64 -f .superpowers/sdd/2026-09-04-rust-controller-foundation/linux-repair-round-1.Dockerfile -t rust-controller-task9:local .` | PASS: actual repaired AMD64 release compile11.28s and release verifier compile1.56s; verifier ran successfully in the Linux image |
| `bash rust-controller/scripts/compose-proof.sh` | PASS scenario below; Linux native/process results and cleanup recorded below |
| `git diff --check` | PASS |

The incremental offline Dockerfile inherits the already executed Task9 image, copies updated Cargo.lock and the controller-service crate, sets CONTROLLER_GIT_SHA to the repair base, and runs the actual release build and release verifier. It uses the previously installed normalized Debian Ansible entrypoint, not a host modification. This is a real incremental repaired release build, not a newly executed full one-pass Dockerfile or hosted job. Runtime proof scripts are bound read-only from this checkout.

Repaired image ID: `sha256:fc73f340020429d19915cf0aed07f90ed893ce34eee9b57b155d9016a16bcb76`, inspected architecture `amd64`. Repaired service binary SHA256: `853dffe32e552a82f3367c8c7bfe4a08e31740a62fe19d1da8e044bb0340cff3`. Cargo.lock SHA256: `d1f574bf7a805cf5a49c9dfea1f057d9a85b1d3bff442cbd998acce65d61f2da`. Lock change only adds the service's existing tempfile dev dependency. The artifact reports base a71569f and includes uncommitted repair source at build time.

The Linux hosted setup now explicitly normalizes the trusted system Python3 entrypoint before tests. Both Linux and macOS hosted jobs run `verify_adapter`, invoking the actual registry contract and failing closed on unexpected packaging. Actual macOS and disposable Linux preflight passed here; hosted package resolution/Colima execution remain configured, not executed. Unchanged full workspace/PVE/Python baseline suites were not rerun in this repair.

Actual corrected Compose scenario:

```json
{"attempts_per_operation_max":1,"cap":2,"observer_sweeps":39,"recovery":"real_30_second_lease_expiry","result":"PASS","states":{"pending":1,"satisfied":4,"unknown":2},"transport":"fake","workers_claimed":2,"workers_started_and_ready":3}
```

Linux adapter tests executed by the successful Compose script passed: 12 lifecycle/process tests in6.61s, including actual /proc descendant/drop cleanup; 4 native PostgreSQL tests in13.71s, including success, cancellation, authority flip/current-authority recovery and mismatched-plan preservation. No test was ignored. All three repair projects (1788572189-18573, 1788572276-19453, 1788572439-22196) executed cleanup. Post-run Docker container/network/volume listings for the repair project prefix were empty. Only the reusable local proof image/build cache remains; nothing was pushed. The observer process inventory in the diagnostic run showed only its controller process.

## Task 9 review repair round 2

Repair base: `8d067092fcb88f534bfa9fd28b5a02842a296493`. The remaining post-claim `registry.validate` failure now enters the existing fixed `adapter_preparation` operational latch before the sweep returns its sanitized failure. The change does not broaden execution capabilities or alter scheduler/adapter APIs.

Actual sweep-level RED/GREEN: the test inserts two matching commands into real PostgreSQL, validates a temporary approved registry as startup does, removes that temporary playbook before launching the real `sweeps` loop, and lets the first bound claim fail revalidation. Against the pre-fix code, a subsequent successful cap-full sweep returned readyz200 (expected503). After repair, readiness remains503 with the sanitized label and no temporary path; successful reads cannot clear it. The test then advances only its disposable lease expiry and lets the actual current-authority reaper free capacity: exactly one attempt exists, no process-start event exists, and both never-started operations remain pending rather than being claimed again. The first GREEN attempt corrected the test's expected pending count from one to two: accepted scheduler recovery returns an expired never-started lease to pending. Production/Compose lease duration remains unchanged.

Executed commands:

- `cargo test --offline --locked --manifest-path rust-controller/Cargo.toml -p controller-service sweep_validation_fault_survives_successful_reads_and_stops_later_claims`: PASS,1 test in6.00s after the executable readiness200 RED.
- `cargo clippy --offline --locked --manifest-path rust-controller/Cargo.toml -p controller-service --all-targets --all-features -- -D warnings`: PASS.
- `cargo fmt --manifest-path rust-controller/Cargo.toml --all -- --check` and `git diff --check`: PASS.
- `cargo test --offline --locked --manifest-path rust-controller/Cargo.toml --workspace --all-features --no-fail-fast`: PASS at default test concurrency,166 tests plus7 compile-fail doctests, no failed/ignored tests. The nested proxy-isolation subprocess also passed its one selected test (not double-counted). Service18 tests passed in11.07s plus live-server1 in2.59s; postgres-store21 in17.07s; scheduler24 in23.07s.
- Full stdout/stderr was captured with pipefail and tee in the ignored SDD file `task-9-round-2-workspace.log`; SHA256 `a24880618bbc03d27e76ff1c9fd447cf7d0804e7064ab2fa9f510908107e714b`. The pipeline exited0. After the run, Docker listed no running postgres:16-alpine fixture containers.

Separate acceptance limitation: the primary reviewer reported an earlier default-concurrency full run at8d06709 with18/21 postgres-store failures, while scheduler24/24 passed. The detailed store errors were lost to truncated output; no root cause was established. The primary reviewer's isolated default-concurrency rerun passed21/21 in16.45s; this repair's fully captured default-concurrency workspace run also passed21/21. Inspection found the existing readiness loop's advertised15-second bound is only60 sleeps around default-timeout connect calls, plus an unused readiness pool from startup; neither observation proves the earlier failure's cause. No speculative harness change, serial override or claim of eliminated flakiness was made. Preserve this transient failure as an unresolved reliability observation if it recurs.

This narrow common Rust error branch and the actual sweep/claim/reaper/readiness integration were exercised natively on macOS in the full suite. No new Linux release or Compose run was needed or claimed for round2: the round1 AMD64 artifact/Compose evidence above remains evidence for8d06709, not a rebuilt artifact containing this final source change. Hosted CI and publication remain unexecuted.
