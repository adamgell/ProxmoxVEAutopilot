# StartPe session/atomic-arming proposal

The existing deadline authority is preserved: postgres-store `load_scopes`
anchors `pe_registration` to the StartPe `PveDispatchCommitted` decision event's
original `evaluated_at`, plus the original registration policy budget. Recovery
must not substitute its current time or a new event for that anchor.

The new pure osdeploy-adapter contract provides:

- `PeRegistrationAnchorV1`: original StartPe operation/event, opening time,
  policy budget (1–86400 seconds), and checked derived deadline.
- `StartPeArmingContextV1`: exact run, StartPe/registration operations, attempt,
  generation, owner, public session correlation ID, and request SHA-256.
- `StartPeAtomicArmingProposalV1`: one immutable proposed unit for a future
  fenced session/dispatch transaction. Strict bounded restoration requires
  externally supplied original anchor and context and rejects substitutions.
- `AuthenticatedPeWitnessV1::Unavailable`: deliberately the only witness
  variant. No authenticated token or success can be fabricated by wire input.
- Assessment returns only authenticated-witness-unavailable or original-deadline-
  expired refusal. Evaluation before the original opening time is invalid.

This is an executable validation/refusal contract, **not an implemented atomic
database transaction**. It allocates no live session or secret, performs no I/O,
and has no armed/commit/success outcome. The session UUID is descriptive, not
authentication or global uniqueness proof. Duplicate/consumed-session enforcement
requires a future store transaction and cannot be claimed by this pure type.

Remaining gates: bind original context/event to PostgreSQL durable history;
atomically create/arm the session with dispatch and original deadline under the
existing owner/generation fence; implement authenticated witness verification
and replay/consumption rules; prove crash recovery. Real qmstart, callback
acceptance, controller satisfaction and default production routing remain closed.

Validation: osdeploy-adapter suite 52 tests plus 6 doc tests passed. The new
test covers deadline boundaries, replay substitution, nil identities, malformed/
unknown/oversized wire, hash shape, overflow, and forged authenticated witness
refusal. Strict all-target fixture-ipc Clippy for osdeploy-adapter, pve-port and
operation-controller, workspace fmt, and diff checks passed.
