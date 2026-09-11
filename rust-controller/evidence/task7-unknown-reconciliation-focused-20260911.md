# Task 7 unknown-dispatch reconciliation evidence

Date: 2026-09-11

## Command

```text
RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test --locked -p postgres-store --test osdeploy_durability task7_original_unknown_reconciles_actual_success_and_failure_without_new_lease -- --exact --nocapture --test-threads=1
```

## Result

```text
test task7_original_unknown_reconciles_actual_success_and_failure_without_new_lease ... ok
test result: ok. 1 passed; 0 failed; 127 filtered out; finished in 8.16s
```

The test executed both branches using the owned local PostgreSQL fixture and
the real `NativeFakePve` task receipts:

- an accepted task that reconciles to `Satisfied`;
- an accepted task that later reports task failure and reconciles to `Failed`;
- both branches preserve the original attempt and deadline;
- neither branch creates a new lease or resends the provisioning request.

The fixture emitted two owned proof identities during the run. No production
controller, real Proxmox endpoint, synthetic SQL rows, fabricated envelopes,
or container cleanup was used.

## Boundary

This is a local recovery proof for the generic PostgreSQL OSDeploy decision
path. It does not close the fixture-specific `PeEnsureStopped` release matrix,
physical stop execution, authenticated production callback transport, or
production acceptance.
