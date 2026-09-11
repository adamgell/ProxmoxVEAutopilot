# Authenticated PeComplete and stop prerequisite proof

This is a local-only PostgreSQL fixture proof. It does not contact the
production controller (`192.168.2.4`), a real Proxmox endpoint, or perform a
production mutation.

## Command

```text
RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test --locked -p postgres-store --features fixture-ipc --test osdeploy_durability fixture_pecomplete_authenticated_report_and_atomic_grace -- --exact --nocapture --test-threads=1
```

Working directory: `rust-controller/` in the isolated Rust-controller
worktree.

## Result

```text
test fixture_pecomplete_authenticated_report_and_atomic_grace ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 141 filtered out; finished in 151.73s
```

The fixture created its own owned local native-proof identity and exercised the
authenticated PeComplete report, atomic shutdown-grace transition, subsequent
stop-stage prerequisite, fake local stop submission, receipt persistence, and
the bounded decision/reconciliation progression. The test is prerequisite
evidence for the connected stop-outcome matrix; it does not by itself claim
that the typed stop-outbox release outcome (`accepted`/`refused`/`ambiguous`)
has been persisted.

