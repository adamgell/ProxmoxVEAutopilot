# StartPe database-backend death during original-response write

Scope: actual PostgreSQL backend termination inside an owned disposable fixture,
not controller-process SIGKILL or production recovery qualification.

## Proven failure boundary

The genuine StartPe IPC capture test now holds a transaction-scoped advisory lock
and installs a test-owned AFTER INSERT trigger that waits on that lock inside
`record_fixture_start_pe_receipt`'s original-response transaction. The test locates
the waiting backend using current database, current user, advisory wait, and the
exact original-response INSERT query prefix. It rechecks those predicates when
calling `pg_terminate_backend` on that PID; no broad backend termination occurs.

The original write returns an error. A separate store reads no semantic receipt,
and SQL reads zero original-response rows (therefore no retained provenance row).
After removing the test barrier, concurrent exact replay of the still-owned
original capture succeeds. The remaining proof confirms route-bound independent
reload, unchanged journal, duplicate-send refusal, and historical-NULL refusal.

The AFTER INSERT boundary is deliberate: the semantic receipt and original
response have both been attempted in the same transaction, but neither can commit
before the blocked trigger returns. This is stronger than an exception-only test
for connection-loss handling, but it does not simulate losing the controller's
in-memory capture.

## Exact verification

`RUST_MIN_STACK=16777216 cargo test --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone fresh_controller_clone_then_disk_capacity_then_configure_pe_reaches_satisfied -- --exact --nocapture --test-threads=1`

Passed 1/1 in 15.02s.

`cargo clippy --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone -- -D warnings`

Passed. `cargo fmt --all --check` and scoped diff whitespace checks passed.

## Remaining controller-death harness seam

`fixture_prefix_process/mod.rs` owns killable Clone/Resize/ConfigurePe workers;
its post-receipt recovery branch requires a ConfigurePe prefix. StartPe's genuine
permit, `OsDeployResponseCapture`, and closed original IPC response are currently
owned by the parent helper `capture_start_pe_after_genuine_prefix`, not that worker.
The reload subprocess intentionally owns only observation/configuration data.

To prove StartPe controller death, move the genuine StartPe dispatch/capture/write
flow into an owned child, using the established kill-file/reap protocol, and hold
its write at an independently observed database barrier. Never serialize a closed
capture into the child or reconstruct original capture from SQL. After child death,
assert SQL rollback and fixture accepted-effect preservation; a replacement worker
must remain fail-closed on new send unless a separate reconciliation contract
authorizes recovery of an accepted effect without its lost original capture.

Replacement-generation route hashes deliberately differ and remain rejected.
Historical observation delegation across generations needs a separate explicit
API/authority contract; weakening the existing equality check is not recovery.
