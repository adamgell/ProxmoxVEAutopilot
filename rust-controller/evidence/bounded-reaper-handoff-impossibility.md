# Bounded reaper handoff: incompatible unconditional guarantees

## Required contract

Keep the original absolute deadline D; return without extending that budget;
and establish that the owned child is absent, including zombies, before claiming
cleanup success. Do not treat ownership transfer or SIGKILL submission as reaping.

## Counterexample

At the final nonblocking wait before D, the child is still pending. This is an
explicitly representable outcome in `reap_until`, including the deterministic
expired-deadline regression. A kill request has been sent but the child has not
yet exited, or the responsible reaper thread has not been scheduled to collect
its exit status. The code has no upper scheduling-latency bound.

At D the caller has only three choices:

1. Wait for positive completion evidence: the original deadline may be exceeded.
2. Return and claim absence: the pending child is a counterexample to that claim.
3. Return failure/unknown: this preserves the proof, but does not prove absence.

Moving Child ownership to another thread does not add a fourth outcome. A queued
handoff acknowledgment proves ownership only. A wait-completion acknowledgment
proves reaping but may arrive after D. A detached owner can be delayed just as
the current dedicated supervisor can. PID polling also cannot turn a present
process into absent before D. Process-wide SIGCHLD policy would alter unrelated
child semantics and still would not guarantee timely process exit.

Therefore unconditional absence plus unchanged hard deadline cannot be guaranteed
by an in-process ownership handoff under the current scheduling model. The safe
existing proof result when wait remains pending is failure, not success. The
unreaped-child ownership problem is real; it is not resolved by this conclusion.

## Safe next decision

A separately owned eventual-cleanup reaper may prevent abandonment after a failed
deadline, while retaining failure/unknown for the bounded proof. That would be a
distinct resource-recovery contract: explicit owner lifetime, handoff failure,
completion tracking, shutdown and blocked-child behavior must be designed and
tested. It must not turn the Linux gate green without timely absence evidence.
Alternatively the operating environment must supply and validate a scheduling
assumption strong enough for the existing reserved grace; changing grace alone
does not prove such a bound.

No reaper or production behavior was changed. The current 14 process tests and
strict operation-controller fixture-ipc Clippy were rerun on macOS, alongside
format/diff checks. Linux qualification still requires a rerun with 4dd12e0's
PID/error/deadline diagnostics and remains unresolved. A fabricated Linux success
regression would weaken the invariant, so none was added.
