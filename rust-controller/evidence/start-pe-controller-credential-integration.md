# StartPe controller credential integration gate

Source inspected: `bb29bf0f`, including alias ownership commit `77b5f850`.
This is an implementation contract and test plan; the integration described below
is not implemented or verified. MCP docs search was available but returned broad
task-sequence documents; current Rust source provides the exact boundaries here.

## Current call chain and missing behavior

`operation-controller/src/osdeploy.rs::advance` obtains a permit and response
capture from `Scheduler::begin_osdeploy_pve_dispatch`, then passes them through
the checkpoint and consuming send path. The StartPe branch commits the session,
registration deadline, event and dispatch together. It does not require the
server-created fixture origin or signing configuration.

`Scheduler::issue_fixture_pe_credential` checks the trusted fixture origin,
current lease, already committed session and immutable package, then commits an
alias and returns a closed bearer. Its separate transaction is appropriate for
renewal of an existing session. Calling it after `begin_osdeploy_pve_dispatch`
would leave a committed dispatch and session if issuance failed or the process
died between calls. Calling it before dispatch currently fails because no
session exists. Moving the call alone cannot satisfy initial arming atomicity.

The controller returns only `OsDeployProgress`; its consuming send accepts only a
PVE mutation permit. `StartProvisioningRequestV1` represents VM start semantics
and is serialized into dispatch history. It has no private credential delivery
channel. Adding bearer bytes to that request would persist a secret in canonical
request JSON and expand the signed request contract. A complete invocation needs
a separate explicit delivery capability before its VM start send.

## Cohesive next implementation

1. Add a fixture-only credential configuration object with private secret storage
   and redacted Debug, no serde or Display. Construction requires an explicit
   nonempty signing key, key identifier and positive bounded lifetime. No ambient
   environment fallback or default secret. The controller owns this configuration
   and an operation-bound fixture delivery sink; missing either rejects credential
   mode before claiming/starting the operation. Existing physical-only fixture
   mode remains separately named and cannot claim credential delivery.
2. Refactor dispatch admission into a shared private transaction implementation.
   A new credential-bearing StartPe entry point validates the trusted origin and
   computes expiry from database time and the configured lifetime. It inserts
   session, deadline, alias, dispatch and initial-credential metadata in that same
   transaction. The existing lease/revision/preflight/final-time checks remain.
   Refactor alias insertion/owner comparison into a transaction-local helper;
   do not nest the existing public issuance transaction.
3. Persist the exact initial expiry, alias digest and key identifier with the
   session. They are immutable and sufficient to regenerate the same deterministic
   bearer after a restart when the configured key is still available. Recomputed
   digest must equal the persisted digest. Key loss or replacement returns an
   explicit configuration failure; no fresh expiry or replacement alias is silently
   substituted. The secret and raw bearer are never persisted.
4. Return a closed committed arming capability containing the original PVE permit,
   capture and private credential delivery envelope only after commit. Delivery
   must be operation/attempt/package/alias bound and idempotent. Its fixed fixture
   sink accepts the envelope without logging or serializing raw bearer bytes into
   generic fixture observations. It returns a closed matching acknowledgement.
5. Gate the consuming PVE send on that acknowledgement plus a fresh continuation
   check. Persist acknowledgement without extending the session deadline. Failure
   or uncertain delivery prohibits the initial VM-start call. If delivery succeeded
   but acknowledgement persistence failed, re-delivery uses the same committed
   identity and digest. A lost PVE response follows existing reconciliation and
   must never be interpreted as permission to send again.
6. Define an explicit pre-send recovery state in durable history. A committed
   dispatch currently drives Outcome observation; simply retrying `advance` does
   not recover a missing credential delivery or recreate the consumed permit.
   Recovery must distinguish definitely-unsent from possibly-sent using durable
   state and preserve the existing no-blind-resend rule. This is necessary before
   calling the integrated flow restart-safe.

These are routine fixture implementation choices within the approved Rust PoC.
They do not require importing a Python run or enabling callbacks. Actual PE
package transport and serving remain a later integration dependency: the current
semantic package bytes alone are not an executable PE boot artifact.

## Required proof matrix

| Boundary | Required observable result |
|---|---|
| Missing key, sink or trusted origin | No new session, alias, dispatch or sink call |
| Error after alias insert, before commit | All arming rows/events roll back; no bearer returned |
| Two initial armers | One committed initial identity; losing invocation exposes no credential |
| Controller happy path | Origin-backed alias commit precedes matching sink acknowledgement; acknowledgement precedes exactly one start call |
| Delivery failure/timeout | No start call; original deadline and alias unchanged |
| Sink ack with wrong operation/attempt/package/digest | Rejected before send; no authority inferred from sink data |
| Cancellation, lease expiry or generation change during delivery | Continuation rejects start; alias remains historical |
| Process death after commit/before delivery | Reopen reconstructs identical initial credential; no deadline reset |
| Process death after delivery/before persisted ack | Idempotent matching delivery; no new identity or expiry |
| Process death around PVE send | Existing uncertain-outcome reconciliation; no duplicate start |
| Missing/changed signing key on restart | Explicit failure; no replacement token or accidental delivery |
| Diagnostic/serde surfaces | No secret or bearer in Debug, events, errors, SQL rows, fixture logs or generic request JSON |
| Default build | No credential-mode API/schema admission; existing closed-stage checks pass |

Database reopen tests cover durable reconstruction only. The two process-death
boundaries require killing and replacing the actual controller worker. Linux
proof must name the source commit and execute the matching integrated tests; an
older sealed image cannot qualify this prospective change. PeRegister, callback
authentication, Python import, live Proxmox and production cutover remain outside
this gate.
