# Fixture PeRegister authentication audit

Source audited: `e7059c2f`, plus the focused test additions in this change.
The authenticated fixture PeRegister transaction already exists. This change
extends rejection coverage and corrects the historical assessment's scope; it
does not introduce another callback implementation or claim legacy parity.

## Source findings

`crates/postgres-store/src/scheduler/osdeploy/fixture_registration.rs` restores
the expected VM UUID, MAC and agent ID from the registered plan, verifies the
bearer using server signing configuration and database time, requires the typed
text run identity, and matches the exact token digest to committed delivery.
`crates/postgres-store/src/osdeploy/execution/load.rs::require_fixture_registration_origin`
joins origin, alias, boot session, delivery acknowledgement and exposure and
requires admission before the original registration deadline. Registration uses
its own operation and lease, rather than the old refusal probe's StartPe lease.

Under the existing authority/run/operation lock order, the transaction selects
one immutable registration result, journal decision, state transition and
completion deadline. Equivalent competing calls return the same selected event;
only one reports first selection. Replays continue to require current authority,
uncancelled run, valid credential and registration provenance. These are the
fixture protocol's semantics, not an assertion about permissive Python handlers.

## Additional rejection coverage

`crates/postgres-store/tests/osdeploy_durability.rs` now independently substitutes
VM UUID, MAC and agent ID while retaining the delivered credential. It also uses
the wrong signing secret and an expired signed token. Every call must fail and
the registration table must remain empty before the existing success/race tests.
The expired-token test establishes rejection; since that token is also an
unregistered alias, it does not alone isolate expiry as the only refusal cause.

Existing assertions in the same test cover unknown but correctly signed aliases,
old worker grants, rollback after each result/deadline/journal write boundary,
concurrent first selection, equivalent replay, conflicting identity, restored
state and cancellation. Companion tests exercise inherited deadlines, real lease
expiry/replacement and exhaustion of the original registration budget.

Command:

```sh
RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test -p postgres-store --all-features --test osdeploy_durability fixture_peregister_ -- --nocapture
```

Runtime result: **3 passed, 0 failed, 139 filtered out**, 261.07 seconds; process
exit code 0. The three owned local PostgreSQL fixtures completed without a
cleanup-unconfirmed diagnostic. Formatting and whitespace checks pass. This focused run does not establish
independent process-death coverage, current-source Linux qualification, HTTP
legacy-client compatibility or production readiness. Production and real Proxmox
remain read-only.
