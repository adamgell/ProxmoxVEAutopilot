# Replacement-controller recovery focused proof

This is a bounded local macOS PostgreSQL proof for the Rust-first controller
PoC. It does not qualify the owned Linux runtime, production callback
transport, real Proxmox, or production replacement/cutover.

## Command

From `rust-controller/` at source revision
`8949a5c5f1bd77c029dff390dcef0963f3141704`:

```text
RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 \
cargo test --offline --locked -p operation-controller \
  --features fixture-ipc --test postgres_osdeploy \
  replacement_controller_recovers_armed_delivery_after_lease_expiry \
  -- --exact --nocapture --test-threads=1
```

## Result

```text
test replacement_controller_recovers_armed_delivery_after_lease_expiry ... ok
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 67 filtered out; finished in 39.97s
```

The test exercised the real local PostgreSQL-backed fixture path and a
replacement controller after lease expiry. The output included an owned
fixture pending/created identity and completed successfully without external
Proxmox access or synthetic SQL rows.

This strengthens the local recovery evidence for the existing armed-delivery
path. It does not close the separate gates for all-stage process death,
authentic stop-outcome progression, Linux execution, or production readiness.

Additional serial local recovery tests were run from the same worktree:

```text
cargo test --locked -p postgres-store --test osdeploy_durability recovery_reclaims_twice_without_replacing_original_attempt_or_budget -- --nocapture
cargo test --locked -p postgres-store --test osdeploy_durability recovery_dispatched_expired_observer_becomes_unknown_without_resend -- --nocapture
```

Both tests passed (`1 passed` each; 61.99 seconds and 31.70 seconds). They
prove repeated recovery does not replace the original attempt or budget and
that an expired dispatched observer becomes `Unknown` without resend. These
are local PostgreSQL/fixture proofs only and do not imply production or Linux
qualification.

An additional serial controller recovery test passed:

```text
cargo test --locked -p operation-controller --test postgres_osdeploy \
  due_unknown_reconciles_without_new_lease_or_send \
  -- --exact --nocapture --test-threads=1
```

Result: `1 passed; 0 failed` in 4.40 seconds. This confirms the due-unknown
reconciliation path does not issue a new lease or resend. It remains local
fixture evidence and does not close the authentic stop-outcome or Linux gates.
