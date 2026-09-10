# Fixture post-dispatch publication

## Stage publication transport

`publish_stage_post_dispatch` and `stage_post_dispatch` add stage-aware
publication and readback for accepted v2 Clone and DiskCapacity effects. The
daemon validates operation, stage, attempt, generation, owner, and request digest
against the durable accepted effect before reading or publishing facts.
`FixturePostDispatchV1::decode_stage` validates the stage receipt (`qmclone` for
the source or `resize` for the target) and applies the same config, power, media,
inventory, task, identity, and observation-time checks as legacy Clone decoding.
Stage publications have distinct digest-qualified evidence filenames. Duplicate
publication is refused. Restart invalidates their observations and acceptance
clocks; a historical effect alone cannot authorize republication.

Focused validation: `cargo test --offline --locked -p pve-port --features
fixture-ipc --test fixture_post_dispatch` passes seven tests, including real
daemon Clone then resize publication, owner/generation/attempt mismatches,
duplicate refusal, restart invalidation, and resize receipt/digest/time checks.
Strict feature all-target Clippy also passes.

`FixtureProvisioningPort::with_resize_stage` now configures an exact resize
request, its stage ownership, and the original accepted v2 Clone request/receipt.
The constructor validates the predecessor binding and plan carried by GrowDisk.
Before submission, reads use the predecessor's current publication. The actual
controller request must equal the configured resize request at both checkpoint
and submission. The checkpoint enters and polls the owned stage barrier; it
does not submit. Submission switches reads away from predecessor observations
before network I/O, consumes the daemon's authorization with durable predecessor
validation, and retains the returned resize receipt. Ambiguous outcomes leave
reads unavailable; duplicate and unsupported requests are refused locally.
`with_resize_receipt` supports journal reconstruction but cannot refresh facts
invalidated by daemon restart. Legacy v1 Clone behavior remains available.

The daemon integration test exercises predecessor reads, the actual adapter
checkpoint, resize dispatch, post-publication task readback, duplicate/unsupported
refusal without extra attempts, restart reconstruction, and cancellation of an
entered stage wait without effects. This remains adapter evidence: fresh
`run_osdeploy_once` generation of the resize request and supervisor binding,
PostgreSQL journaling, and controller stage completion still need an integrated
proof. Synchronous ConfigurePe observations remain refused by this task-bearing
observation contract and need separate support.

The local fixture daemon accepts `publish_post_dispatch` only on its supervisor
socket. The command carries the exact typed Clone request and a
`FixturePostDispatchV1` observation. An accepted Clone effect with the exact
request digest and daemon receipt must already exist in the ledger. The daemon
records an acceptance time after syncing that effect and validates every
observation against this lower bound and its publication time. Task UPID,
operation, fixture, request, node, and VM identities remain bound by the decoder.

Each effect admits one immutable publication. The daemon writes the request,
receipt, daemon generation, times, and observation to a unique temporary file,
syncs it, renames it, and syncs the directory before acknowledging publication.
The worker socket supports only `post_dispatch` readback; `FixtureReadClient`
validates the response against the caller's exact request and receipt. Error,
absence, and partial coverage values retain their original typed representation.

Restart deliberately invalidates publications and acceptance clocks. Historical
publication files remain evidence, but the restarted daemon never loads them.
Recovered ledger receipts cannot authorize a fresh publication. Readback returns
`None` until a new accepted effect in that daemon lifetime has a publication.
This is a bounded local fixture protocol, with at most 64 acceptance entries and
a 196608-byte framed request ceiling under the existing request deadline.

Executable evidence: `cargo test --offline --locked -p pve-port --features
fixture-ipc --test fixture_post_dispatch` passes five tests, including the daemon
publish/read/restart proof. It rejects publication before acceptance, stale facts,
worker-socket publication, an incorrect UPID, and repeated publication. The proof
checks one attempt and one effect throughout and confirms restart returns no
publication. The decoder tests retain error/absent/partial states and reject
identity, receipt, schema, size, and observation-time substitutions.

Open integration: `FixtureProvisioningPort` still reads its startup facts. It must
select these published observations after the Clone receipt and project task,
target configuration/power, and inventory into controller reads. Consequently
the fresh `OsDeployController::run_osdeploy_once` Clone test does not yet prove
`Decided(Satisfied)` through this publication path. No service, production, real
Proxmox, or lease behavior changes are included.
