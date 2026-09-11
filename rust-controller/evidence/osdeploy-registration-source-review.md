# OSDeploy registration source and macOS review

Accepted source-review milestone: `53272397cdd386ec05b7ac6332780ba9ef15e62d`, relative to phase baseline `ec20215b4f2f2d1c339e9bd6e6250f15050b7abf`. **Linux registration/database execution remains pending. This is not service or production-candidate acceptance.**

## Implemented boundary

- Named, bounded text restoration validates the complete83field plan, rejects duplicate/malformed fields, reconstructs admitted values and verifies the full workflow hash. It retains private admitted outputs without unchecked deserialization.
- Typed registration performs executable PVE validation before writes, then atomically persists the complete plan, shared VM/global agent reservations, all sixteen UUIDv7 operations, commands, projections and stage records. Registration creates no attempts, leases, dispatches, journal events, outbox entries or execution authority.
- Identical replay and public read-only reload validate the complete manifest, reservations, command cardinality, dependencies and distinct native/PVE/stage/workflow hashes. Later valid operation state does not invalidate immutable plan metadata.
- Shared run/identity locking prevents cross-family and identity adoption. Generic intake, claims, start/cancel/finalize, continuation/heartbeat and reaping cannot execute OSDeploy. Additive migration0004 preserves prior migrations and historical native/generic rows.

## Evidence and review

The implementation report records221 ordinary macOS test executions plus10 documentation tests, zero failed/ignored/filtered in the full affected four-package run. Two final test-only assertion improvements passed their exact covering reruns; no implementation changed after the full run. Shared lifecycle tests execute in three harnesses and are not thirty distinct behaviors. Formatting and strict all-target/all-feature workspace Clippy passed. Cached offline dependency policy passed with four duplicate-dependency warnings and one unmatched license allowance; no fresh advisory fetch or warning-free policy result is claimed.

Task1 restoration and Task2 registration each passed independent Astra spec/quality review with no findings. A separate whole-phase Astra review read the complete137,905byte/3,224line six-commit diff across24paths, both briefs/reports/reviews and the parent plan. It also approved with no findings. A focused outside-diff check found no cross-run operation-ID/idempotency alias bypass. Reviews were static and did not rerun the implementer's tests.

Detailed reports, review packages and ledgers are retained under `.superpowers/sdd/2026-09-05-rust-osdeploy-registration/` in this isolated worktree.

## Platform and completion limits

An independently owned local topology probe established that the cached Linux amd64 runner can use a cached Linux arm64 PostgreSQL instance in one isolated network namespace, with fixed loopback access, two separate databases and independent connections, actual tmpfs data storage and verified cleanup. It ran only a Python SQL probe—not the Rust registration suite. Two preceding metadata-template failures were diagnosed and their exact owned resources cleaned with evidence preserved; no unrelated resources were removed.

A reviewed test-only fixture adaptation and exact-source Linux Rust database execution are still required. Existing library-only Linux proofs with database filters do not satisfy this gate. Actual source/artifact manifests, ELF identities and executed suite results must be captured after that adaptation.

The full goal continues through durable attempts/deadlines, seven PVE dispatch paths, callback/guest/runtime compatibility, service integration, full sixteen-stage independent-process recovery, Python/Rust single-writer transition, fault/restore/rollback and production-candidate assessment. Real Proxmox and production192.168.2.4 remain read-only; external mutation, deployment and cutover require separate approval. RustedOutClient and downstream product tracks remain deferred.
