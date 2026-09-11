# Microsecond PostgreSQL session diagnostic

The opt-in `postgres-store/fixture-ipc` session diagnostic now accepts a typed
`PeRegistrationAnchorV2` with the existing arming context. It validates context
before connecting, then compares original dispatch event, attempt, request,
opened/dispatched/evaluated timestamps, deadline and budget in a read-only,
rollback-only transaction. PostgreSQL timestamps are compared at exact
microsecond precision; sub-microsecond values are rejected, never rounded.

The historical v1 entrypoint remains available via checked millisecond-to-
microsecond conversion. A rounded v1 value cannot equal an original v2 anchor
with a microsecond remainder. No inferred timestamp or retry clock substitutes
for original durable fields.

All matching paths still refuse: authenticated witness unavailable before the
original deadline, or original registration deadline expired at/after it.
This is diagnostic binding, not session ownership, generation-fence validation,
atomic arming, or a StartPe dispatch/satisfaction capability. Default migrations
and production transport are unchanged.

Fresh local validation: `start_pe_arming_gate` passed 1 test (isolated database
schema test intentionally ignored); `pe_registration_precision` passed 1;
the store precision unit test passed 1. Tests cover deadline-minus-one,
deadline, deadline-plus-one microsecond, before-open rejection, rounded v1
rejection, and invalid operation/owner refusal before connection. Strict
all-target Clippy for both affected crates, formatting and diff checks passed.
No database or Docker runtime was launched. Actual durable-row binding and
rollback tests remain an isolated PostgreSQL integration gate.
