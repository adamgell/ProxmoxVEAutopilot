# Owned worker lease expiry and reconciliation

The `postgres_fixture_workers` integration target extends the supervisor-owned
worker A/B proof through actual PostgreSQL lease expiry. Worker A claims, starts,
commits Clone dispatch, consumes the fixture permit, and journals the generated
receipt. The supervisor independently observes the Running attempt, dispatch,
receipt, and matching accepted fixture effect before killing A.

Worker B first verifies the original attempt and exact receipt using independent
PostgreSQL and fixture clients. Claim, resume, and reap cannot take over while
A's lease remains live. B polls database time against the persisted lease expiry
with a 33-second bound, then calls `reap_osdeploy_expired`. The supported scheduler
changes the dispatched attempt to Unknown. No lease timestamps or production
lease durations are modified.

After expiry, the supervisor restarts the bounded fixture daemon from its original
durable log. B invokes `reconcile_osdeploy_unknown` with supervisor-collected
reconciliation evidence, retaining the original attempt and exact receipt. The
test deliberately has no successful task observation for the generated UPID:
reconciliation remains Unknown, claim and resume remain refused, and the accepted
effect still has submission sequence one. The supervisor checks that PostgreSQL
contains exactly one attempt.

Validation on macOS:

```text
cargo test --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_workers worker_loss_preserves_original_attempt_and_exact_receipt -- --exact --nocapture
cargo clippy --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_workers -- -D warnings
cargo fmt --all -- --check
```

The focused test and its child recovery entry point pass. This proves natural
lease expiry and fail-closed scheduler reconciliation after process loss at the
durable receipt boundary. Evidence collection remains supervisor-assisted. It
does not prove successful task/target reconciliation, the full controller service
loop, or the crash window after effect acceptance but before receipt journaling.
Production and real Proxmox are outside this test.
