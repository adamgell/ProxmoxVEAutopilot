# Prefix worker process proof

The fresh Clone, DiskCapacity and ConfigurePe controller calls in
`postgres_fixture_clone` execute in separate OS worker processes. PostgreSQL and
the fixture daemon remain supervisor-owned. Each child reconstructs its fixture
port from the original identity and validated predecessor envelope/receipt; it
calls the real controller collection, admission, dispatch, receipt and evaluation
path. This proof does not call `Scenario::ready`.

Two ConfigurePe death windows are covered after Clone and DiskCapacity are
Satisfied: durable ConfigurePe effect before observation publication, and after
observation publication but before the journal receipt insert. The supervisor
holds an isolated PostgreSQL journal lock, observes the actual blocked receipt
insert and independently reads the durable effect, then kills and reaps the
recorded child handle. The test never treats a child marker as the effect proof.

A new recovery process waits for the real lease expiry without rewriting time
or lease rows. Reaping preserves the original attempt and exact dispatch, leaves
the absent receipt absent, and records Unknown. A second claim is refused and
the attempt count remains one. Clone and DiskCapacity remain Satisfied. After
the old daemon thread has terminated, a new daemon recovers the ledger and sees
exactly three attempts and three effects. The crash-case ledger directories are
retained under `/tmp/pglate-*` and printed by the test.

These windows establish preserved uncertainty and no duplicate submission. They
do not establish automatic completion after a lost receipt: publication does not
create journal receipt authority. Earlier-stage death matrices, death after a
persisted receipt, reconciliation through the full service, callback stages,
production cutover and Linux execution of this source remain separate work.

The macOS supervisor test needs the established `RUST_MIN_STACK=16777216` test
setting. An initial default-stack prefix run aborted in the supervisor after all
three child controller calls succeeded. Repeating with the explicit test stack
passed. This identifies a test-stack sensitivity; it does not prove a controller
runtime stack defect or production stack adequacy. The abort-created owned
fixture was retained for inspection.

Validation commands:

```sh
RUST_MIN_STACK=16777216 cargo test --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone fresh_controller_clone_then_disk_capacity_then_configure_pe_reaches_satisfied -- --exact --nocapture
RUST_MIN_STACK=16777216 cargo test --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone configure_worker_death_ -- --nocapture --test-threads=1
cargo clippy --offline --locked -p operation-controller --all-targets --features fixture-ipc -- -D warnings
cargo fmt --all -- --check
git diff --check
```

Ignored `fixture_prefix_worker` and `fixture_prefix_recovery_worker` entrypoints
are invoked only by their supervising tests. Broad `--include-ignored` runners
must exclude them when no owned input protocol has been established.
