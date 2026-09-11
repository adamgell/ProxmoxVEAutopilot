# Fixture stop release continuation audit

Audit target: branch `codex/rust-controller-design` at `2c84e0c4`.

## Result

The next physical-stop continuation is not safely connectable yet. The current
Rust PoC has three deliberate seams, but no single supervisor-owned operation
that joins them atomically:

1. `Scheduler::consume_fixture_stop_outbox` consumes the immutable PostgreSQL
   marker and returns only whether this call inserted the one-use consumption
   row. It does not return a typed physical-send authority or a durable
   accepted/ambiguous receipt state.
2. `FixtureStopReleaseProposalV1` independently binds operation, attempt, lease
   owner, generation, request/receipt/sample digests, and sealed shared-history
   provenance. It is a non-authorizing evidence contract; it cannot release a
   barrier or submit a task.
3. `StageBarrier::AdmitStop` records immutable admission while keeping the
   barrier entered. The existing `AuthorizeRelease` path explicitly refuses
   `EnsureStopped`, so a generic release cannot accidentally become a physical
   stop permit.

Connecting these today would create an unsafe split-brain window: the database
consumption could commit while IPC acknowledgement is lost, yet no durable
record would distinguish accepted, refused, and ambiguous physical submission.
A replacement worker could then either resend an unknown command or release a
barrier without a proof that the same owner/generation still holds the lease.

## Required next contract

The next implementation must be a fixture-only supervisor operation that, in
one explicit protocol, verifies the proposal's independently recomputed
digests, current operation/attempt/lease/generation, cancellation and deadline,
and sealed provenance before allowing a physical fixture submit. It must persist
an accepted, refused, or ambiguous receipt outcome before exposing any release
capability. Timeout, worker death, lost acknowledgement, stale owner, replay,
and cancellation must all fail closed and require reconciliation. A `qm stop`
fixture effect is not stopped-power evidence; that remains a separate later
observation and reconciliation gate.

## Evidence reviewed

- `crates/postgres-store/src/scheduler/osdeploy/fixture_stop_outbox.rs`:
  consumption is a one-use marker and has no physical-send receipt outcome.
- `crates/pve-port/src/fixture_ipc/stage.rs`:
  `FixtureStopReleaseProposalV1` is non-authorizing; `AuthorizeRelease` rejects
  `EnsureStopped`.
- `evidence/fixture-stop-outbox-provenance-bridge.md` and
  `evidence/fixture-stop-release-boundary.md`: document the same intentional
  separation and remaining release gate.

## Verification

- `rg -n "FixtureStopRelease|consume_fixture_stop_outbox|AuthorizeRelease"
  crates/pve-port/src crates/postgres-store/src`: confirms the three separate
  seams and no connected release/submit operation.
- No production host, `192.168.2.4`, real Proxmox, or external stop command was
  accessed or changed.

This audit is a bounded Rust controller PoC artifact, not evidence of the
complete Ansible-to-Rust port or production readiness.
