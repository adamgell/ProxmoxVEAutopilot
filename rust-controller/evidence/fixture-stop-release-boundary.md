# Fixture stop release boundary

The supervisor now reports `stop_release_authority_unavailable` when a valid,
entered EnsureStopped identity requests generic disk-state release. It keeps the
barrier Entered and creates no authorization. The diagnostic belongs to that
reply only; a subsequent status response does not retain it.

## Why admission does not authorize release

`OsDeployController::admit_fixture_stop` prepares PostgreSQL authority, consumes
a versioned independent Running-power sample, revalidates the authority and
committed request, and sends the supervisor admission request. Each PostgreSQL
check finishes its transaction before the IPC request. Cancellation, lease loss,
or ownership replacement can occur after that check. The fixture log admission
therefore records validated historical facts but cannot authorize a later send.

`StageBarrier::AuthorizeRelease` accepts a disk-state proposal for earlier stages.
That proposal carries neither a durable PostgreSQL stop-exposure transition nor
a consumable send authorization joined to the supervisor admission. The stop
branch refuses independently of whether an admission record exists. The daemon
also requires a Released barrier before physical submission.

## Next connected implementation

The required bridge is a recoverable dispatch protocol, not a claim that a
PostgreSQL transaction and filesystem append are atomic. Under the existing
lock order, validate current lease/cancellation and the exact dispatch, then
persist a stop exposure/send decision keyed to that dispatch, admission digest,
power-sample digest, and fence. The supervisor must durably consume that exact
decision at most once before recording a stop effect. Explicitly define the
cancellation ordering at that persisted decision. Replacement workers must
reconcile the durable dispatch and supervisor history without creating a second
stop attempt after ambiguous delivery. Exercise crashes on both sides of every
handoff, including after acceptance and before the acknowledgement returns.

A stop task receipt proves acceptance only. Completion requires a separately
collected Stopped-power observation bound to the accepted stop effect and fresh
daemon generation. Preserve the StartPe physical predecessor and its immutable
completion throughout.

## Verification

The focused `fixture_stage_consumers` test
`stop_identity_cannot_acquire_disk_only_release_or_effect_across_daemon_restart`
passed locally (1 test, 2.14 seconds). It kills an independent entered worker,
checks the explicit refusal and parked phase, attempts direct submission, and
repeats after daemon restart with a new generation. It observes zero attempts
and effects and rejects stale owners and structurally valid but unproven
admissions. This test does not prove positive admission-to-stop execution.

This change does not add stop release, stop effect, Stopped publication, or
production-controller replacement readiness.
