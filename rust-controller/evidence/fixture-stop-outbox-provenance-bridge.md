# Fixture stop outbox provenance bridge

This bounded PoC slice connects the sealed shared-history gate to the existing
PostgreSQL fixture stop-outbox selector and one-use consumer. Both controller
methods require the operation's proven port/checkpoint provenance before they
call the scheduler/store APIs. Missing or mismatched provenance therefore
fails before database access; a decoded receipt or caller-supplied identity is
not sufficient.

The bridge remains bookkeeping only. It does not release a supervisor barrier,
send `qm stop`, publish stopped power, or perform replacement-worker recovery.
The selector and consumer retain their existing receipt, sample, lease,
guarded-grace, cancellation, deadline, replay, and orphan checks.

The fixture IPC layer now also exposes `FixtureStopReleaseProposalV1`, an
immutable, non-authorizing contract for the next supervisor step. It binds the
operation, attempt, lease owner, generation, request/receipt/sample digests,
and sealed provenance. Construction rejects nil identities, empty digests, and
operation/provenance mismatches; the type has no release or send method.

## Verification

- `cargo check -p operation-controller --features fixture-ipc --all-targets`:
  passed.
- Existing sealed-provenance focused test and strict Clippy checks remain
  passing.
- Stop-release proposal validation test: 1 passed.
- A positive PostgreSQL/controller end-to-end reservation proof is still an
  explicit follow-up gate; this evidence does not claim it.

This is a Rust controller PoC slice, not the complete Ansible-to-Rust port.
Production `192.168.2.4` and real Proxmox were not changed.
