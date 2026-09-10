# Late Clone authorization protocol

Status: typed candidate validation and supervisor-only atomic authorization /
checkpoint release persistence are implemented. Collection migration, consuming
this authorization for Clone mutation, and positive controller execution are
**not implemented**.
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
The version-two stable read message and actual controller entrypoint proof
remain the next integration gate.

`FixtureReadIdentity` contains fixture, operation, node, source VM and target VM.
It permits naming historical collection facts before the controller chooses an
attempt and creates its exact request. It must replace the version-one digest
identity in a separately versioned provisioning-read message, never by treating
an absent or zero digest as a wildcard in the existing protocol.

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
