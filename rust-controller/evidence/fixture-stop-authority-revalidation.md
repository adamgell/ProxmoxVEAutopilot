# Revalidating prepared fixture stop authority

`Scheduler::revalidate_fixture_stop_authority` reloads the current committed
stop lease and guarded grace through `fixture_stop_authority`. That reload
retains the existing database lock order and checks scheduler ownership,
cancellation, immutable grace history, active lease and original deadline.
It compares the prepared authority with the reload while preserving the
original `lease_checked_unix_ms` used by the versioned power sample. The
prepared clock must be at or after the immutable grace decision and at or
before the new database check. Every other field must remain equal.

Calling the preparation method twice and substituting its second return value
would change the authority embedded in the sample. This separate check closes
that ordering problem without restamping independent power evidence.

The method returns only `()`. It grants no opaque admission capability and
does not make the subsequent IPC request atomic with cancellation in PostgreSQL.
The orchestration still needs an owned supervisor transport, exact committed
request and physical StartPe history join, a fresh versioned sample, and a
defined cancellation ordering across database validation and durable admission.
There is no positive PostgreSQL-to-supervisor admission proof yet.

The PostgreSQL PeComplete-to-EnsureStopped regression checks successful
revalidation without changing serialized prepared bytes and refusal by a
different scheduler owner. A pure comparison test checks alteration of each
version/grace/decision/fence/clock/deadline field and a backwards recheck clock.
The first test arrangement issued eight extra complete database reloads inside
the original 30-second stop lease and then expired before the existing decision
call. Those exhaustive comparison checks now run outside the live lease;
the lease duration and original stage deadline remain unchanged.
Stop release, dispatch and physical stopped-power claims remain gated.

Local verification passed: the PostgreSQL regression in 147.13 seconds, the
pure comparison test, all-target/all-feature postgres-store Clippy with warnings
denied, default compilation, formatting and whitespace checks. No Linux result
has been collected for this change.
