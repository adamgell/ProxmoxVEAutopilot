# StartPe power-aware IPC authorization

This connects the durable stopped predecessor used by the atomic ledger seam to
an explicit typed supervisor IPC release contract. It does not invoke the atomic
admission method or enable StartPe execution yet.

`StageCheckpointRequest::AuthorizeStartPe` requires a version-one power assertion,
the typed StartPe request and byte-identical committed request, the exact accepted
ConfigurePe request/identity, and the original byte-identical accepted receipt.
The asserted power must be stopped and unlocked. The daemon resolves that claim
against its latest durable stopped record and latest effect for the VM; it does
not trust the supplied assertion as an observation.

The resolved record must belong to the current publication daemon generation and
be no more than five seconds old (the bounded fixture checkpoint lifetime).
Future observations are rejected. The source record's original stage, owner,
attempt, generation, digest and receipt provenance are retained. Successful
release persists that exact power record inside a distinct authorization field,
with the derived unchanged disk/PE state, rather than using the legacy disk-only
authorization as power evidence. Restart invalidates release authority and old
publication generations; replay cannot relabel the stopped record as current.

The existing StartPe submission branch still rejects before authority consumption
or any attempt/effect write, even with this new release. Task receipt/transition
acceptance and independent running/task observation have not yet been connected
to the execution path. The next slice must consume this exact power-aware token,
revalidate freshness at submission, invoke atomic admission, and bind independently
collected task/running evidence before any controller satisfaction claim. No real
Proxmox, production/default path, callback or `192.168.2.4` mutation is enabled.

## Executable evidence

The real-daemon Clone→resize→ConfigurePe publication test now exercises the new
power-aware release using the original receipt retrieved from the accepted-effect
IPC endpoint. Client-side release, running/locked assertions, wrong predecessor
owner/generation/attempt, invalid receipt bytes and semantically equivalent but
re-encoded receipt bytes are refused. Exact release succeeds once, duplicate
release refuses, and all authorization cases leave the effect ledger byte-identical.
After daemon restart, the same durable stopped predecessor cannot obtain a new
power release. Repeated StartPe submissions remain refused without consumption,
new attempts or effects. Ledger tests pin the five-second freshness boundary and
wrong-generation rejection.

Validation passed: two fixture-feature ledger tests, two default-feature ledger
tests, 15 fixture post-dispatch/stage/consumer tests, strict all-target Clippy for
`pve-port` and `operation-controller`, formatting and diff checks. PostgreSQL
controller/process-death suites were not rerun; no four-stage execution proof is
claimed by this authorization-only milestone.
