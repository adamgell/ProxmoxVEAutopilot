# Late Clone authorization protocol

Status: typed candidate validation implemented; daemon routing, persistence,
collection migration and positive controller execution are **not implemented**.
`LateCloneAuthorizationV1::validate_candidate` returns no mutation capability.
Existing seed and digest boundaries continue to govern actual submission.

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
