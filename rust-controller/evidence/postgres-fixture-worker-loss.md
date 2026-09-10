# PostgreSQL and fixture receipt recovery after worker process loss

The `postgres_fixture_workers` integration target adds a supervisor-owned,
bounded process-loss proof. The supervisor owns the isolated PostgreSQL fixture
and fixture daemon. Worker A opens its own database connections, claims and
starts the queued Clone operation, prepares the admitted request, durably commits
dispatch, consumes the submission permit against the IPC provisioning adapter,
and records the returned receipt. The supervisor supplies native preflight
observations; this does not yet exercise controller collection over IPC.

The supervisor kills worker A only after independently loading PostgreSQL's
dispatch and receipt and comparing that receipt with the daemon's accepted
effect. It confirms worker A is still live before killing it. Worker B then
starts in a fresh address space, connects independently, reconstructs the
original attempt from durable data, and checks the exact receipt and submission
sequence. A request with the wrong hash fails closed. Both claiming a replacement
and resuming evaluation are refused while the original operation remains
Running with its original lease. The supervisor verifies exactly one attempt
remains afterward. No production lease duration is modified.

## Scope remaining

This proves recovery reads after forced loss of an actual dispatching process at
the durable-receipt boundary. It does not prove scheduler takeover after lease
expiry, a crash between accepted effect and receipt journaling, or final outcome
reconciliation. `resume_osdeploy_bound` accepts Waiting operations and cannot
resume this Running operation; the next harness must invoke the supported
expired-lease reconciliation path after the real lease expiry, preserve the
original attempt, and reconcile through controller collection. No missing
production API is asserted by this staged proof.

## Validation

On macOS, the targeted `worker_loss_preserves_original_attempt_and_exact_receipt`
test passed, including its separately launched worker B test. Strict Clippy for
this integration target and workspace formatting check passed. The two ignored
test entries are child-process entry points, not separately runnable acceptance
tests. This source has not yet been qualified on Linux.
