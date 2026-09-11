# StartPe missing-capture quarantine after controller SIGKILL

This extends the owned-controller death proof with a separate replacement process.
No runtime policy or scheduler allowlist is changed.

## Existing gate, not automatic reconciliation

`postgres-store/src/scheduler/osdeploy/discovery.rs::discover_osdeploy_due` permits
Clone, DiskCapacity, ConfigurePe, and separately enabled PeEnsureStopped. StartPe
is absent from this stage allowlist. Its Unknown-reconciliation SQL also requires
a durable PVE receipt for stages other than ConfigurePe. Therefore a StartPe
accepted effect with no committed original receipt cannot currently produce a due
reconciliation item merely by enabling fixture StartPe dispatch.

An initial recovery experiment waited seven seconds for such an item after real
lease expiry and reaping; it timed out. The complete test failed in 47.53s. This
was a missing scheduler contract, not a reason to extend the timeout or fabricate
a due token.

## Fail-closed characterization

The owned recovery child now waits for the real database lease to expire, reaps
Running to Unknown, and verifies preserved attempt/dispatch plus missing receipt.
It checks a new claim is refused and no due item exists. It constructs a fresh
bound StartPe controller with the genuine ConfigurePe predecessor observer, but
without original capture. Two normal controller invocations must return
`Decided(Unknown)` (a terminal uncertainty report, not successful completion).
An intermediate expectation of Idle was incorrect and failed in 41.20s; the
characterization was corrected to match the controller's terminal-state contract.

The child then asserts Unknown state, one attempt, unchanged original dispatch,
no semantic receipt, and zero response/provenance rows. The parent separately
checks the same durable state, unchanged fixture journal, and exactly four
attempts/effects including one accepted StartPe. No backend/clock/lease state is
fabricated to accelerate expiry.

This proves safe quarantine, not successful reconciliation or eventual completion.
The fixture daemon lasts 45 seconds in this scenario solely to cover the existing
30-second worker lease and replacement-process checks.

## Remaining contract

Define an observation-only recovery source that can validate the accepted StartPe
effect and original attempt/route without recreating the lost closed capture.
Define which observations may move Unknown forward and how that transition is
recorded distinctly from original-response persistence. Only then extend discovery
and its typed due-token validation under fixture gates. Do not backfill the original
response table, treat daemon data as original IPC capture, reuse send authority,
or grant replacement-generation route rights by widening an allowlist alone.

No production mutation, synthetic receipt, historical backfill, or duplicate-send
authorization was introduced.

## Verification

`RUST_MIN_STACK=16777216 cargo test --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone start_pe_controller_death_during_write_rolls_back_original_response -- --exact --nocapture --test-threads=1`

Passed 1/1 in 46.69s, including the separate recovery process (28.77s, principally
real lease expiry). Focused fixture-test Clippy with warnings denied passed after
combining two identical 45-second lifetime branches into one equivalent match.
That style-only rewrite followed test compilation; the behavioral test above
executed the equivalent two-branch form. Formatting and whitespace checks passed.
