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

The fixture IPC contract classifies a future checkpoint result as `Accepted`,
`Refused`, or `Ambiguous`. Timeout and transport-unavailable errors are
explicitly ambiguous and require reconciliation; they are never treated as
permission to resend or as evidence of a completed stop.

The fixture IPC layer now also exposes `FixtureStopReleaseProposalV1`, an
immutable, non-authorizing contract for the next supervisor step. It binds the
operation, attempt, lease owner, generation, request/receipt/sample digests,
and sealed provenance. Construction rejects nil identities, non-canonical
SHA-256 digests (wrong length, case, or alphabet), and operation/provenance
mismatches; the type has no release or send method.

## Verification

- `cargo test -p pve-port --features fixture-ipc stop_release --lib`: 2 passed
  (digest/provenance binding and accepted/refused/ambiguous transport outcomes).
- `cargo fmt --all -- --check`: passed.
- `cargo clippy -p pve-port --features fixture-ipc --lib -- -D warnings`:
  passed.
- `git diff --check`: passed.
- Existing sealed-provenance focused test and strict Clippy checks remain
  passing.
- Stop-release proposal validation test: 1 passed, including empty, malformed,
  uppercase, and non-hex digest rejection.
- Checkpoint outcome classification test: 1 passed; transport loss remains
  `Ambiguous` while deterministic rejection remains `Refused`.
- A positive PostgreSQL/controller end-to-end reservation proof is still an
  explicit follow-up gate; this evidence does not claim it.

This is a Rust controller PoC slice, not the complete Ansible-to-Rust port.
Production `192.168.2.4` and real Proxmox were not changed.
