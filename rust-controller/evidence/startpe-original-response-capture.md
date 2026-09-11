# Closed original fixture StartPe response capture

`FixtureStartPeResponseV1` now retains the original StartPe fixture identity,
request, original ConfigurePe identity/request and exact accepted response
envelope bytes. Its fields are private, it has no deserializer, and Debug does
not disclose response contents. Cloning this observation value supports
retrying storage of the same immutable original response; it conveys no
checkpoint, release or send capability.

The only constructor is inside successful typed IPC submission in
`LateStartContext::submit`. The adapter's `captured_start_pe_response()` returns
None before successful submission and returns the captured value afterward.
Transport failure still consumes send state without creating a capture. A fresh
adapter populated through `with_late_start_receipt` never produces this value,
even when its observation validates. Thus readback of supplied bytes cannot be
misrepresented as original response capture.

The genuine Clone/Resize/ConfigurePe/StartPe subprocess test verifies the
captured stage/predecessor bindings, exact envelope equality with the accepted
fixture journal, absent capture before dispatch, and absent original capture
after observation-only restoration. The existing duplicate-send and exact
publication checks remain. Two compile-fail tests prevent JSON/public-field
construction.

## Remaining atomic persistence integration

The existing `record_osdeploy_pve_receipt` transaction is suitable for adding
fixture metadata alongside the semantic receipt, but still receives only
`MutationReceipt`. This change does not add a SQL table or persistence method.
No current postgres-store or operation-controller test constructs the bound
StartPe IPC adapter; their available store-issued StartPe response captures use
the separate `NativeFakePve` path. Manufacturing this new value in those tests
would defeat the original-response guarantee.

The next connected proof must drive genuine fixture IPC StartPe through a
store-issued dispatch/capture, pass `FixtureStartPeResponseV1` to the existing
locked original-response transaction, and persist both representations in one
commit. Required proofs remain failure-after-insert rollback, identical replay,
conflicting bytes refusal and restarted loader readback. The database loader
must return a separate observation-only record rather than recreate this
original-IPC capture type.

The source gap analysis in `startpe-postgres-original-response-gap.md` remains
historically correct; this change closes its first capture-value prerequisite
only. No PostgreSQL atomic persistence, Linux qualification, physical stop or
production readiness is claimed.

Validation: focused genuine IPC subprocess proof passed (1/1, 1.34 seconds).
The full `fixture_post_dispatch` suite passed (12/12, 3.04 seconds, including two
inert child entrypoints); both closed-type compile-fail tests passed. All-target
all-feature pve-port Clippy passed with warnings denied. Formatting and targeted
whitespace checks passed.
