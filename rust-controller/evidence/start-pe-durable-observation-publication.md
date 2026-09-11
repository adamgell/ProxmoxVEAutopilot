# Durable StartPe task and running observation publication

This is a fixture-only observation contract after atomic StartPe admission. It
does not enable real `qmstart`, the generic controller publication path, or a
controller satisfaction outcome.

## Contract

The typed supervisor command `publish_start_pe_observation` accepts a
`FixtureStartPeObservationV1`: a separately submitted successful task observation
and explicit running/unlocked power observation. Both must be timestamped after
the current daemon's independent acceptance clock and no later than publication.
Task fixture/node/operation/digest/UPID must match the exact typed StartPe request
and accepted receipt. Errors, absent power, running/failed tasks, locked/stopped
power, substituted identities and stale/future observations are not successes.

The ledger verifies the atomic StartPe transition and exact accepted receipt,
then appends both observations as one versioned framed/checksummed/fsynced record.
The running observation retains the exact stopped predecessor, effect identity,
owner/generation/attempt, receipt digest, acceptance clock and daemon provenance.
Duplicate publication cannot append another observation. The task UPID in replay
must match the original accepted receipt; both observation times are revalidated.

The worker-only `start_pe_observation` command reads the exact durable record.
Restart replays it byte-for-byte at the semantic record level with original
timestamps/generation, not with refreshed clocks. A new publication after restart
is refused because the current daemon did not accept the task. A torn publication
frame prevents recovery; it cannot expose just one half of the observation pair.

This remains a trusted-supervisor fixture boundary. It rejects contradictory and
substituted messages, but cannot establish that a trusted supervisor truthfully
observed external infrastructure. The synthetic fixture running transition and
separately supplied observation are not live Proxmox evidence. That distinction
must remain explicit in any subsequent controller or production proof.

## Evidence

The fresh four-stage IPC test publishes through the supervisor, reads the same
record after daemon restart, and rejects duplicate/restart publication. It covers
worker publication, changed owner/task operation/task UPID, nonterminal task,
stale task/power timestamps, stopped/locked power, and unchanged ledger bytes for
all refused messages. Both observations append in exactly one record. A separate
daemon refuses recovery from a truncated copy of that actual publication frame.
The existing generic StartPe post-dispatch publication gate remains closed.

Fresh validation: 15 fixture post-dispatch/stage/consumer tests, two fixture-feature
ledger tests, two default-feature ledger tests, strict all-target Clippy for
`pve-port` and `operation-controller`, formatting and diff checks all passed.

Open gates: typed adapter readback/restoration and full configuration/inventory
postconditions, fresh PostgreSQL-backed four-stage controller satisfaction,
OS-worker kill/recovery matrices, and production/live observation validation.
The torn-frame proof is not an OS-worker kill proof. No real Proxmox, production,
default-path, callback or `192.168.2.4` mutation was performed.
