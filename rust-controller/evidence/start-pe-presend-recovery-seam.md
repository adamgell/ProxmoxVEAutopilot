# StartPe pre-send recovery seam

Source inspected: `35dfff9f`. This document specifies the missing integration;
it does not introduce a delivery API or prove restart recovery. The available MCP
docs search returned general task-sequence credential material. The exact Rust
call sites below are the authority for this seam.

## Why an independent state table would be incorrect

`postgres-store/src/scheduler/osdeploy/pve.rs::begin_osdeploy_pve_dispatch`
commits the boot session and then calls `receipt::committed`. That function in
`scheduler/osdeploy/receipt.rs` returns an `OsDeployDispatchPermit` whose public,
consuming `submit_fake_once` method can immediately call the port. The permit
contains no credential-delivery requirement. Consequently, a later transaction
cannot label that existing session "definitely unsent": a caller may already
have consumed its permit. Absence of a receipt is also insufficient because a
successful send can lose its response.

`scheduler/osdeploy/fixture_credential.rs::issue_fixture_pe_credential` runs
after the session exists and accepts caller-selected expiry. It can renew an
alias but cannot identify which alias was the initial delivery identity.
The alias table deliberately admits several aliases for one session.

`operation-controller/src/osdeploy.rs::collect` selects Outcome whenever a
dispatch exists. `advance` only admits a new request in Preflight mode.
`osdeploy/send.rs::submit_and_capture_once` accepts the ordinary permit and
checks continuation before sending. These are the exact three controller seams
that must change together with credential-mode admission.

## Persist two independent facts

Credential delivery and VM-send exposure are different states. A delivery may
be repeated with the exact same immutable credential and an idempotent sink;
a VM call whose outcome is uncertain must not be repeated on that basis.

| Durable state | What it proves | Allowed recovery |
| --- | --- | --- |
| Armed, no delivery acknowledgement, no VM exposure | Credential-mode VM send has not been authorized | Reconstruct the original token and redeliver to the same sink |
| Delivery acknowledged, no VM exposure | Matching sink accepted the original credential; VM send has not been authorized | Revalidate current lease, cancellation and deadline, then compete for exposure transition |
| VM exposure committed | A VM send capability escaped, whether or not the process actually called the port | Observe/reconcile only; never reconstruct a send capability |

The last transition is intentionally conservative. Death after exposure commit
but before the network call leaves a possible-send outcome. There is no atomic
transaction spanning PostgreSQL and the PVE request. A fixture process must not
claim to solve that uncertainty using an in-memory marker.

## Proposed storage and closed types

Create credential-mode arming metadata in the same transaction as the session,
initial alias and dispatch. Its immutable key is
`(operation_id, run_id, attempt_id, dispatch_event_id, package_sha256)`.
Include the initial alias digest, absolute expiry, signing-key identifier and
stable sink identity. Use a composite foreign key to the complete alias owner,
not a digest foreign key plus unrelated operation columns. Raw credential bytes
and signing material are excluded.

Delivery acknowledgement and VM exposure are append-only rows keyed by the
arming identity. Enforce one acknowledgement and one exposure per arming;
exposure references the matching acknowledgement. Bind exposure to the current
worker, generation and lease acquisition event. Do not update the original
session deadline. The transaction lock order remains authority, run/execution,
current grant, arming, acknowledgement/exposure. Missing credential-mode rows
for historical physical-only sessions mean "legacy physical mode", never
"definitely unsent". Do not backfill delivery state from absence of receipts.

The credential-mode arming transaction returns a closed delivery envelope, with
private token storage and no conversion to `OsDeployDispatchPermit`. The sink
returns a closed acknowledgement for the exact arming and alias. Only an atomic
`acknowledged -> exposed` transaction can return a one-use send capability. A
second or restarted caller seeing exposure gets observation identity only.
Existing receipt capture remains separate from permission to send.

This requires splitting the current private receipt factory so original-response
capture can be reconstructed independently, while the ordinary public dispatch
entry point rejects credential-mode operations. Otherwise it remains an alternate
path around acknowledgement. Default production admission stays unchanged.

## Implementation and proof order

1. Extract transaction-local alias validation/insertion from the existing issuer.
   Add a credential-mode arming transaction and closed configuration/envelope.
   Prove rollback after alias insertion and same-request race behavior.
2. Introduce an operation-bound idempotent fixture sink and its closed matching
   acknowledgement. Prove wrong operation, attempt, package, alias and sink
   identity cannot acknowledge another arming.
3. Add the exposure transaction and a restricted consuming capability. Prove two
   concurrent exposers yield at most one capability; reject stale grants,
   cancellation and expired original deadlines. Compile-fail proofs must cover
   envelope-to-permit conversion, serialization and caller-made acknowledgements.
4. Route controller recovery before the existing dispatch/Outcome branch. Prove
   database reopen preserves expiry, alias and deadline; changed key or digest
   refuses delivery. Existing physical-only sessions must retain observation-only
   recovery and must never be retroactively classified unsent.
5. Kill actual fixture workers at arm commit, sink acceptance, acknowledgement
   commit, exposure commit and VM-call response loss. Assert exact sink identity,
   token digest and VM call count. Run those tests from the same source on Linux.

The existing `fixture_start_pe_atomic_arming_rollback_race_and_reload` test proves
session arming and reload. The credential alias tests prove permanent alias
ownership. Neither currently exercises a private delivery sink, exposure gate,
credential-mode controller recovery or the process-death boundaries above.
Implementing only the proposed table or returning a public recovery enum would
not fill those gaps. The cohesive integration remains authorized local Rust work;
there is no user-decision blocker identified by this audit.
