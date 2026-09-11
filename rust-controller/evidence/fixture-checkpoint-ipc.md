# Fixture checkpoint IPC

The opt-in fixture daemon now accepts supervisor `arm`, `status`, and `release`
commands, and worker `enter` and `poll` commands. Each daemon lifetime supports one
barrier, bound to a fresh generation UUID, owner UUID, operation UUID and explicit
`DispatchCommitted` point. The supervisor chooses a deadline of 1–5000 ms. The
worker client has an independent bounded deadline and returns an error unless it
observes the matching released barrier. Incorrect ownership, premature release,
duplicate entry and use of the wrong socket are rejected.

State is synced to `checkpoint.json` through a unique temporary file, atomic rename
and directory sync before acknowledgement. It is separate from the mutation ledger.
A restart validates the previous file and writes a fresh idle generation; prior
release state cannot release a replacement worker. Supervisor cleanup still requires
confirmed process death before socket removal. Filesystem ownership is the local
trust boundary; UUID bindings are freshness/identity checks, not authentication.

The subprocess test covers pause/release, persisted entered state, stale owner,
restart/rebind invalidation, deadline expiry and zero mutation ledger attempts/effects.
Feature daemon suite passed 14 tests (one subprocess entry ignored); default daemon
suite passed 8 tests (one ignored). The startup test uses an actual protocol handshake
because pathname visibility can precede listener readiness.

`FixtureCheckpointClient::checkpoint` is fallible. `ControllerFixturePort` currently
returns `()` and cannot propagate checkpoint failure. Consequently this increment
does not implement that trait for `FixtureProvisioningPort`. The next controller
integration must propagate an explicit error and stop dispatch before mutation;
swallowing the error or using a no-op would invalidate the recovery proof. A real
controller Clone round-trip and Linux qualification remain outstanding.
