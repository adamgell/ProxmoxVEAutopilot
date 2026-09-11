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
create journal receipt authority. Earlier-stage death matrices,
reconciliation through the full service, callback stages,
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

## Death after persisted ConfigurePe receipt

`configure_worker_death_after_receipt_reconciles_exact_prefix_to_satisfied`
complements those pre-receipt windows. Separate OS workers execute the fresh
Clone and DiskCapacity stages to Satisfied. The ConfigurePe worker generates its
own request, commits dispatch, and receives the exact synchronous daemon receipt.
An isolated PostgreSQL trigger blocks only the ConfigurePe outcome decision
insert, using a supervisor-held advisory lock. The receipt transaction remains
able to commit. The supervisor independently observes Running state, unchanged
dispatch, the exact durable receipt, and the blocked outcome insert before
forcibly terminating that owned process. The trigger and lock are removed after
death; no lease or production policy is rewritten.

A new OS recovery worker waits for the actual database lease deadline, reaps it,
and verifies Unknown with the original attempt, dispatch, and receipt. A duplicate
claim is refused. It reconstructs the exact accepted ConfigurePe effect and
receipt from the fixture ledger and waits for the scheduler's reconciliation due
record. The supervisor independently rereads the live physical VM state and exact
accepted effect before making the first ConfigurePe publication at that time.
This avoids treating observations from before the 30-second lease wait as fresh.
The recovery controller consumes the original receipt and new physical evidence
through `run_due_once`, reaches Satisfied, and preserves the original attempt,
dispatch, and receipt. Clone/DiskCapacity remain Satisfied; the fixture remains at
three attempts and three effects. A final daemon restart also preserves that
ledger count.

The fixture daemon's explicit lifetime ceiling is now 60 seconds, allowing the
unchanged 30-second lease plus reconciliation delay to fit inside one daemon
lifetime. The test uses 45 seconds. A boundary test accepts exactly 60 seconds
(then shuts down early) and rejects 60 seconds plus one nanosecond before ledger
creation. This affects the private local fixture only.

The supervisor now waits for actual host time to pass an independently sampled
PostgreSQL clock before physical observation collection. This avoids host/Linux
fixture clock differences putting observations before receipt time. It neither
backdates observations nor changes freshness validation. Initial diagnostic runs
correctly reported `observation_not_fresh`; their failures were not counted as
successful recovery evidence.

Validation uses the same explicit macOS stack setting and existing owned worker
entrypoints. No new ignored child entrypoint was added. Strict operation-controller
and pve-port feature all-targets Clippy, formatting, and diff checks pass.
The full three-case ConfigurePe worker-death matrix passes concurrently: both
pre-receipt windows remain Unknown and the persisted-receipt window reconciles
to Satisfied. The focused new test also passed independently.
