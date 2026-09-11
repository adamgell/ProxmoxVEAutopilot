# Fixture stop outbox prerequisite

The store now provides `select_fixture_stop_outbox` and
`consume_fixture_stop_outbox`. These are unconnected prerequisites; neither API
returns a physical send permit and the durable fixture stop release remains
gated.

Selection validates the receipt against the exact versioned sample and committed
stop request, revalidates the database-derived guarded-grace authority, then takes
the authority/run/operation/lease locks again before storing evidence. Cancellation,
current grant, dispatch identity, admission expiry, and original deadline must
still hold. The selected receipt, sample, receipt digest, attempt, lease token, and
executor generation are immutable. An identical selection replays; a different
selection for the same operation is rejected.

Consumption locks the same authority and execution scope, validates the stored
receipt and sample again, checks the current grant and immutable selected owner,
and inserts an immutable marker once. A successful first reservation returns true;
a repeat returns false. If the commit acknowledgement is lost, a retry cannot
return a second successful reservation. This is possible-exposure bookkeeping,
not evidence that a stop was sent or accepted. A replacement owner cannot consume
the old owner's selection. Recovery and reconciliation must resolve that case;
no rearming or automatic resend is implemented.

Receipt authenticity is an outstanding integration requirement. The receipt type
is deserializable; digest/sample consistency cannot prove supervisor provenance.
The eventual controller must obtain it through the trusted supervisor transport.
These store APIs do not make arbitrary decoded receipt input authoritative.

The controller now exposes `reserve_fixture_stop_outbox`, which returns the
typed `SharedHistoryUnavailable` refusal before accessing storage or IPC.
`ControllerFixturePort` currently exposes checkpoint hooks without attesting
that its accepted StartPe physical journal is the separately supplied
`FixtureCheckpointClient` supervisor journal. Matching decoded identities or
digests cannot establish that provenance. The reservation API accepts no receipt
or caller assertion that could bypass this missing join. `admit_fixture_stop`
continues to return historical admission evidence only.

The focused controller test
`stop_outbox_reservation_refuses_without_database_or_physical_access` passed
(1 test, 0.00 seconds). It repeats the refusal and reconstructs a controller with
a replacement owner, while an unreachable lazy PostgreSQL pool stays unopened
and fixture physical submissions remain empty. All-target/all-feature controller
Clippy passed with warnings denied. This is refusal evidence; it does not exercise
outbox transaction rollback, ambiguous commit acknowledgment, cancellation,
deadline, or database reload. Those positive protocol tests still require the
sealed shared-history capability and connected implementation.

## Verification and limitations

- `cargo check -p postgres-store --all-features`: passed.
- `RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test -p postgres-store
  --all-features --test postgres migration_creates_constrained_foundation_tables
  -- --exact`: 1 passed, 1.59 seconds. This verifies migration application and
  reapplication, inventory, four immutability triggers, TRUNCATE refusal, and
  orphan consumption refusal against isolated PostgreSQL.
- `cargo clippy -p postgres-store --all-features --all-targets -- -D warnings`:
  passed, 10.86 seconds.
- Formatting and whitespace checks passed.

There is **no runtime selection/consumption integration proof** for these APIs
yet. Concurrent selection/consume, lost commit acknowledgement, replacement owner,
cancellation, expiry, rollback, and reload behavior are intended implementation
semantics, not qualified end-to-end claims. The next proof must connect a genuine
supervisor receipt to the same database-backed physical history. No stopped-power
observation, successful stop dispatch, reconciliation, or production readiness is
established by this prerequisite.
