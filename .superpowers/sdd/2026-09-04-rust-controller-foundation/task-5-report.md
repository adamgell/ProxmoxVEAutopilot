# Task 5 report — generation-fenced scheduler and leases

## Scope and authority

Implemented Task 5 and its two review corrections only in the isolated
`codex/rust-controller-design` worktree, based on `b3048ce`. The original Task
5 implementation is `5aca6e2`; the review correction is the subsequent
`fix: close scheduler fencing gaps` commit, followed by
`fix: make scheduler authority monotonic`.

Repository MCP preflight was healthy, but the September 4 Rust-controller plan
and spec were not indexed there. The checked-out Task 5 brief, full plan,
approved authority/scheduler/lease spec sections, progress ledger, Task 4
report/review, both Task 5 reviews, and existing domain/journal/store APIs were
therefore the implementation authority.

No production, PVE, controller, network, Compose deployment, Ansible, Windows,
Graph, Entra, or tenant action was performed. PostgreSQL tests used only the
already-local `postgres:16-alpine` image with `--pull=never`, Docker-assigned
loopback ports, and drop-guard cleanup. Cargo commands were offline and locked.

## Original RED/GREEN evidence

The original scheduler tests were written before implementation. Once the API
shell compiled, all 11 required behavior tests failed with `NotImplemented`;
the focused missing-authority case proved the suite had reached behavior. The
initial implementation then made the 12-test scheduler suite green, including
missing/stale authority, worker/token fencing, cap and skip-locked behavior,
ten-claimer ownership, cancellation, expiry, mutation-start reaping, UUIDv7
reacquisition, PostgreSQL-clock timing, atomic journal/projection/outbox writes,
and capability redaction.

Additional original mutation checks showed that retaining a finalized lease
failed the durability test, exposing the token through derived `Debug` failed
the redaction assertion, and omitting the Task 5 UUIDv7 constraint accepted a
malformed new lease token. Each passed after the intended behavior was restored.

## Round-one review RED evidence

Review corrections followed focused test-first cycles:

- Atomic start initially returned `NotImplemented`; direct terminal finalize
  from `leased` succeeded instead of returning `NotStarted`.
- Cancellation of leased work returned `CancellationNotRunning`, leaving start,
  heartbeat, and continuation insufficiently fenced.
- The ABA regression had no monotonic authority transition implementation. A
  deliberate mutation disabling the database generation trigger made
  `authority_generation_cannot_aba_back_to_an_old_grant` fail because the raw
  generation-7 rewrite succeeded.
- Two concurrent reapers initially made the loser return `LeaseNotFound`, and
  the unbounded sweep processed 33 rows instead of the asserted batch of 32.
- The downstream compile-fail proof initially compiled because public
  `PgStore::pool()` exposed unrestricted SQL.
- A deliberate replacement of checked cancellation revision arithmetic with
  `revision + 1` made the `i64::MAX` regression panic with `attempt to add with
  overflow` rather than return a controlled error.
- The explicit 0001-to-0002 upgrade proof was mutation-checked by temporarily
  removing `NOT VALID`; migration then failed with PostgreSQL `23514` because
  the deliberately malformed legacy lease existed.

All mutations were restored before final verification. The restored focused
ABA, overflow, migration-upgrade, and public-pool compile-fail tests passed.

## Round-two review RED evidence

Two focused tests reproduced both remaining authority defects before the fix:

- `authority_cannot_be_deleted_and_rebootstrapped_to_reauthorize_an_old_grant`
  deleted the Python generation-8 singleton, reinserted Rust generation 7, and
  then observed the old running grant heartbeat successfully instead of
  receiving `StaleAuthority` from both heartbeat and finalization.
- `concurrent_authority_transitions_serialize_with_one_domain_stale_loser`
  held a compatible shared blocker until two generation-7 transitions had both
  reached the update boundary. PostgreSQL chose a raw deadlock loser, so the
  test observed zero controlled `StaleAuthority` results instead of exactly one.

After the database removal fence and initial exclusive transition lock were
implemented, both tests passed. The original missing-authority test remains: it
now simulates catastrophic superuser corruption by temporarily disabling the
delete trigger, and proves every scheduler operation still fails closed if the
singleton is administratively removed outside normal database rules.

## Final behavior

The scheduler/store boundary now provides:

- an authority- and capability-fenced `start()` transaction that locks in the
  order authority, operation, lease/attempt; verifies operation, attempt,
  worker, token, executor, generation, states, and PostgreSQL expiry; appends an
  immutable `attempt_started` event and outbox entry; then atomically records
  running operation, projection, attempt, journal, and outbox state before an
  adapter may mutate;
- `continuation()` for matching live running or waiting attempts, while leased
  work cannot heartbeat, continue, or finalize without the start boundary;
- immediate cancellation fencing: start, heartbeat, and continuation reject
  `cancelling`; non-unknown terminal results are rejected after cancellation;
  conservative `cancelling -> unknown` finalization remains capability- and
  authority-fenced and atomically removes the lease;
- a database-enforced generation transition that advances exactly once and a
  typed transition API whose SQL uses `generation = generation + 1`; generation
  `7 -> 8 -> 9` cannot be rewritten to 7, and the initialized singleton cannot
  be deleted or truncated and reinserted, so an old scheduler and grant remain
  stale;
- authority transition acquires `FOR UPDATE` initially. Concurrent transitions
  serialize; after the winner commits, the waiter rereads generation 8 under
  lock and returns the controlled `StaleAuthority { expected: 8, actual: 7 }`
  domain error instead of attempting a shared-to-exclusive lock upgrade;
- bounded 32-row reaping serialized by a transaction-scoped advisory lock. The
  candidate query locks operation rows with `FOR UPDATE ... SKIP LOCKED` before
  lease/attempt rows, preserving authority-to-operation-to-lease lock order and
  preventing double transition or disappearing-lease races;
- checked scheduler revision increments returning `RevisionOverflow`;
- mutation-start evidence as the only gate permitting expired leased work to
  reset to pending; started/running/waiting/cancelling expiry becomes unknown;
- per-workflow serialized cap checks, skip-locked claims, UUIDv7 acquisition
  tokens, complete capability binding, PostgreSQL-clock expiry/deadlines, and
  redacted grant diagnostics; and
- no public SQLx pool accessor. Scheduler persistence resides inside
  `postgres-store`; the `scheduler` crate is a domain façade over public typed,
  invariant-preserving operations. `PgStore` retains its Task 4 APIs but its
  pool accessor is crate-private, with a downstream compile-fail guard.

Journal, projection, operation, attempt, outbox, and lease changes remain in
one transaction. The established Task 4 append, idempotency, outbox recovery,
transition, and migration behavior was not weakened.

`0002_scheduler.sql` remains additive. Its UUIDv7 check is `NOT VALID`, so a
real malformed Task 4 lease row survives upgrade while all new inserts and
updates are checked and the constraint remains unvalidated. It also installs
the monotonic authority update trigger, initialized-singleton delete/truncate
guards, and scheduler indexes idempotently.

## Adversarial proof

The final scheduler suite has 22 PostgreSQL tests. In addition to the original
proof, it covers atomic start durability, direct-finalize prohibition, running
and waiting continuation, leased cancellation fencing, conservative cancellation
finalization, database generation ABA rejection, two-connection reaper racing,
bounded multi-sweep reaping, controlled cancellation revision overflow,
delete/reinsert ABA fencing, and deterministic concurrent authority transition
serialization.

The PostgreSQL store suite has 21 tests, including the explicit sequence
`0001_foundation.sql -> malformed legacy Task 4 lease -> 0002_scheduler.sql`.
It proves preservation of the legacy row, enforcement on new inserts and
updates, and `convalidated = false`.

## Final verification

Commands and results:

```text
cargo test --offline --locked -p scheduler --test postgres -- --test-threads=1
# 22 passed, 0 failed

cargo test --offline --locked -p postgres-store --test postgres -- --test-threads=1
# 21 passed, 0 failed

cargo test --offline --locked --workspace -- --test-threads=1
# 80 runtime tests + 4 compile-fail doc tests passed, 0 failed

cargo clippy --offline --locked --workspace --all-targets -- -D warnings
# passed

cargo fmt --all -- --check
git diff --check
# passed
```

Final cleanup audit found no remaining disposable `postgres:16-alpine` test
container. No live deployment or infrastructure acceptance claim is made.

## Concerns and gates

No blocking Task 5 concern remains. A legitimate first authority insert remains
allowed as the explicit bootstrap boundary. Once that singleton exists,
ordinary SQL cannot delete or truncate it, and every authority update must
advance the generation exactly once. Even catastrophic superuser trigger
disablement/removal leaves the scheduler fail-closed on a missing singleton.
Production bootstrap/cutover, disposition of active work, adapter mutation,
deployment, and PVE validation remain later approved tasks and were not
attempted here.
