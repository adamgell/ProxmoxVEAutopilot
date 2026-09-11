# Late Clone authorization protocol

Status: typed candidate validation and supervisor-only atomic authorization /
checkpoint release persistence and version-two stable collection migration are
implemented. Consuming this authorization for Clone mutation and positive
controller execution are **not implemented**.
`LateCloneAuthorizationV1::validate_candidate` returns no mutation capability.
Existing seed and digest boundaries continue to govern actual submission.

The checkpoint command now supports `ArmLate` with a stable read identity and
`AuthorizeRelease` with the proposal plus independently loaded committed bytes.
The supervisor must obtain those bytes from PostgreSQL; the daemon trusts this
local supervisor capability and cannot establish database durability itself.
Late barriers reject standalone release. Authorization and Released state share
one synchronized rename and directory sync before acknowledgement. Restart
replaces the generation and clears both authorization and late identity. The
worker socket rejects both supervisor actions. The persisted authorization is
currently an inert record: the existing Clone submission path still requires
the original seeded exact request, and does not consume this new record.

The daemon process regression covers successful persistence, worker rejection,
release without authorization, altered digest, duplicate authorization, expiry,
restart invalidation, and a failed persistence rename with no acknowledgement.
The version-two stable read message now has a distinct `provisioning_reads_v2`
command and `provisioning_reads_v2.json` startup file. Its identity contains no
digest. Strict version and identity decoding prevents either protocol from
accepting the other's payload, including a digest injected into version two.
Both versions share the existing config, timestamp, media and coverage checks.
`FixtureProvisioningPort::new_late` collects these facts before an exact request
exists. Its submission path explicitly rejects all mutations, even with a
checkpoint binding, until a separate late mutation command carries that binding
and checks the released generation/owner and exact persisted authorization.
The legacy constructor retains the original exact digest check.

Executable evidence: `fixture_daemon` proves v2 source config collection through
the adapter, two daemon restarts, all five identity mismatch rejections, absent
v1 seed isolation and zero attempts. `fixture_clone_contract` proves the bound
late adapter refuses submission before any socket access. Tests passed:
`fixture_daemon` 16 plus one ignored child entrypoint, `fixture_clone_contract` 7,
`fixture_provisioning_reads` 5; feature library 32 and strict feature all-target
Clippy passed. Actual controller entrypoint success requires the bound late
mutation consumption seam and remains open.

`FixtureReadIdentity` contains fixture, operation, node, source VM and target VM.
It permits naming historical collection facts before the controller chooses an
attempt and creates its exact request. It replaces the version-one digest
identity only in the separately versioned provisioning-read message; absent or
zero digests never become wildcards in the existing protocol.

The supervisor command will carry a version, complete checkpoint binding,
stable identity, exact request bytes and their computed envelope digest. The
implemented validator compares these to an independently supplied committed
request and current Entered barrier. It rejects different owners, generations,
operations, fixtures, VM identities, digests and all other barrier phases.
Wire bytes supplied by the worker are never evidence of durable dispatch.

Required integration sequence:

1. Supervisor registers stable read facts and arms a generation/owner/operation
   checkpoint. Worker has access only to the existing worker socket.
2. Actual controller collection and locked evaluation construct the Clone
   request, persist DispatchCommitted, and enter the checkpoint.
3. Supervisor independently loads that operation's committed request from
   PostgreSQL. It verifies lease/fence/attempt against the owned worker and
   validates the proposal. The daemon cannot independently infer PostgreSQL
   durability from a JSON boolean or worker assertion.
4. A new supervisor-only command persists a one-use authorization containing the
   complete request, binding and daemon-owned post-effect facts. Authorization
   and release must share one durable state transition; the old standalone
   Release command must reject barriers armed in late-authorization mode.
5. Submission compares the complete canonical request to that authorization,
   requires the same released generation and records attempt/effect before
   acknowledgement. Expiry, duplicate authorization and restart invalidate
   authorization; durable accepted-effect lookup remains available for recovery.

Before connecting this contract to the daemon, add process tests for worker
socket rejection, release without authorization, duplicate proposal, expired
barrier, restart between authorization and send, altered request and durability
failure. Then migrate the adapter to the versioned stable read identity and
execute the actual PostgreSQL controller entrypoint. This patch's validator
tests establish proposal rejection only; they do not establish those process
or controller proofs.
