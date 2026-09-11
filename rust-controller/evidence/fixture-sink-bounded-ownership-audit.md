# Fixture sink bounded ownership audit

Inspected source: `228f845700ceec852550aaac0c6bd3cb50513c0b` on macOS.
This is a source audit, not a storage-stall experiment or a bounded-delivery
qualification. MCP docs were available; their credential search returned the
existing product plans rather than this Rust fixture implementation.

`postgres-store/src/scheduler/osdeploy/fixture_delivery.rs`,
`FixtureCredentialEnvelope::deliver`, performs synchronous create, write,
file fsync, hard-link publication, exact-byte replay read, and directory fsync.
Only completion of all acceptance checks constructs `FixtureDeliveryAck`.
This preserves durable, idempotent acceptance, but none of those calls has a
deadline or a cancellation point. Even temporary-file cleanup is synchronous.

`operation-controller/src/osdeploy.rs`, `advance` and
`resume_fixture_delivery`, invoke this function inside
`before_close(closed, async { envelope.deliver(&config.sink) })`. That async
block runs the entire synchronous function in one poll. `before_close` selects
between closure and the future; once the delivery branch is being polled it
cannot observe closure until the synchronous call returns. The outer
`timeout_at` in `run` similarly cannot preempt this poll. Consequently the
24-second whole-call budget and admission drain are not a proven upper bound
for a stalled fixture sink, and a runtime worker can be occupied throughout it.

A timeout wrapper cannot repair this ownership property. Moving the calls to
`spawn_blocking` would let the async caller stop waiting, but would provide no
cooperative cancellation point inside the filesystem call. Dropping or aborting
the task handle must not be interpreted as proof that credential publication
has stopped. Waiting for that worker to finish preserves ownership but leaves
the same unbounded completion gate. Removing fsync would change the meaning of
the acknowledgement and is not an acceptable shortcut.

The next implementation should place this exact acceptance protocol in a
dedicated owned helper process. Its parent must retain the child handle and
admission activity, communicate through a private bounded channel (credentials
must not appear in arguments, environment, or diagnostic output), and validate
the returned acknowledgement against the original immutable binding. On
deadline, closure, or IPC failure it must prohibit acknowledgement persistence
and exposure, terminate the helper and observe/reap its exit before releasing
ownership or starting another writer for that operation. A reap timeout is an
unresolved owned process requiring quarantine and continued supervision; it
cannot be reported as successful bounded cleanup. Process isolation alone does
not prove a hard deadline for kernel/storage completion.

After confirmed helper exit, recovery must inspect or replay the original exact
credential using the existing publication protocol: death before publication
may leave a private temporary file; death after publication but before reply
must accept only the same bytes and re-establish directory durability. Neither
case permits new alias/expiry values or a PVE send before persisted matching
acknowledgement and exposure. Tests must stall each write/publication/fsync/reply
boundary, close admission and expire the lease, then prove child exit/reaping,
no later writes after ownership release, exact replay, and no premature start.
An unreapable-helper case must prove quarantine rather than claim cancellation.

No code wrapper was added. The current private spool remains a fixture delivery
mechanism whose durability proofs do not establish bounded storage-stall or
service-drain behavior.
