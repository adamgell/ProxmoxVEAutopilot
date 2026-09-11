# Fixture stop release boundary evidence

This is a local macOS focused proof of the typed stop-release boundary. It is
not an authentic PostgreSQL stop-outcome matrix and does not claim physical
stop, production callback transport, or production readiness.

## Source and commands

- Worktree: `codex/rust-controller-design`
- Date: 2026-09-11
- `cargo test --locked -p pve-port --features fixture-ipc stop_release --lib -- --nocapture`
- `cargo test --locked -p postgres-store --features fixture-ipc --test postgres migration_creates_constrained_foundation_tables -- --exact --nocapture`

## Results

The typed proposal/classification unit filter passed 2/2:

- `checkpoint_outcomes_keep_transport_loss_ambiguous`
- `proposal_requires_matching_operation_and_nonempty_digests`

The PostgreSQL migration constraint test passed 1/1:

- `migration_creates_constrained_foundation_tables`

That migration test verified the fixture stop foundation inventory, including
the release-outcome table and its immutability/no-truncate trigger contract.
The test created an owned local fixture database only; no production host,
real Proxmox API, physical stop, synthetic outcome row, or external callback
was used.

## Boundary

The evidence proves typed accepted/refused/ambiguous classification and the
storage schema constraints. It does not prove the authentic progression from
daemon StartPe receipt through store-issued lease, consumed outbox envelope,
release proposal, and persisted accepted/refused/ambiguous outcome. That
connected matrix remains open and requires a helper that owns both the real
fixture IPC provenance and PostgreSQL execution context.
