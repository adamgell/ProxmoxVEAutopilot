# Fixture post-dispatch publication

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
