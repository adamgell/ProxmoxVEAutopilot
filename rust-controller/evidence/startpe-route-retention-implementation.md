# StartPe original-route retention implementation

Fixture-only implementation of the core path in
`startpe-route-retention-contract.md`; no production or physical VM mutation.

- The closed original response retains the configured checkpoint route hash.
  Fallible provenance derivation occurs before submission; restored adapters still
  cannot manufacture an original capture.
- Additive migration 0018 adds a nullable, constrained hash column. It does not
  change migration 0017 or backfill historical evidence.
- The original response and route hash are inserted in the existing semantic
  receipt transaction. Replay compares the hash as well as all original bytes;
  NULL compares false rather than being silently accepted.
- `load_fixture_start_pe_response_for_route` accepts sealed configured provenance,
  requires operation, generation, and stored hash equality, and returns the same
  observation-only value. It grants no send/checkpoint/callback authority.

## Focused validation

The new integration assertions first failed compilation because the capture hash
and route-validated loader APIs were absent. After implementation:

`RUST_MIN_STACK=16777216 cargo test --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone fresh_controller_clone_then_disk_capacity_then_configure_pe_reaches_satisfied -- --exact --nocapture --test-threads=1`

Passed 1/1 in 13.67s. This genuine IPC/PostgreSQL test exercises original capture,
forced transaction rollback, concurrent exact replay, independent-process
observation reload, exact configured-route acceptance, and independent rejection
of changed operation, generation, owner, and channel. It also verifies no duplicate
send or changed fixture journal.

Also passed: `cargo clippy --offline --locked -p postgres-store -p operation-controller --all-features --all-targets -- -D warnings`,
`cargo check --offline --locked -p postgres-store --lib` (default features), and
`cargo fmt --all --check`.

Scope limits: the subprocess reload still tests unbound observation data, not a
reconstructed configured-route loader. Historical NULL rejection and replay are
implemented but do not yet have an old-schema upgrade integration proof. No
replacement-generation delegation or kill-during-write proof is claimed. Linux
exact-current-source qualification and production readiness remain separate gates.
