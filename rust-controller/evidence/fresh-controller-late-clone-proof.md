# Fresh controller late Clone proof

The `fresh_controller_late_clone_records_exact_durable_receipt` integration test
starts an isolated PostgreSQL database and owned IPC fixture daemon. It invokes
`OsDeployController::run_osdeploy_once` on a newly enqueued Clone operation. The
test never calls `Scenario::ready` or manually creates a dispatch permit.
`Scenario::new` supplies the workflow and historical synthetic source/media facts;
the controller collects all preflight evidence through the IPC adapter.

The supervisor arms a generation/owner/operation-bound late checkpoint before
controller execution. At the entered checkpoint it independently reloads the
committed dispatch from PostgreSQL and constructs the exact Clone envelope. An
incorrect digest cannot release the checkpoint or create an effect. The correct
authorization releases the controller, which submits once and persists the exact
daemon-generated receipt. A fresh store reload matches that receipt and the
original attempt and dispatch digest. Reusing the late authorization is rejected.
The native fake records no submissions.

The invocation ends in `Decided(Unknown)` after the dispatch and receipt. Static
startup observations do not prove the post-dispatch outcome, and the adapter's
task-status read remains unavailable. This proof closes fresh admission and
receipt orchestration only. Fresh outcome observation/reconciliation, successful
Clone completion, independent worker takeover, the remaining stages, and current
Linux qualification remain separate requirements.

Validation commands (macOS, isolated local fixtures):

```
cargo test --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone fresh_controller_late_clone -- --nocapture
cargo clippy --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone -- -D warnings
cargo fmt --all -- --check
```

The focused test passed; strict Clippy and formatting passed. No production or
real Proxmox endpoint was used.
