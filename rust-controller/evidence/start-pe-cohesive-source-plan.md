# Cohesive StartPe implementation map

Source audited: `724b6101`. This is an implementation plan, not executed runtime
proof. `./skill.sh status` verified the MCP tool and document inventory; the docs
search for `StartPe credential delivery` returned general task-sequence material.
The local source below defines the Rust boundaries.

## Newly identified admission prerequisite

Credential-required mode must be persisted **before the first StartPe dispatch**.
A scheduler-local option is insufficient. `scheduler.rs:41` enables physical
StartPe on one scheduler instance; `scheduler/osdeploy/pve.rs:22` checks only that
instance option. A second scheduler can use the ordinary physical entry point
before an arming transaction creates credential metadata. Testing for an existing
credential arming row therefore cannot close this race.

Bind credential delivery policy to the server-created fixture origin during
`osdeploy/registration.rs::create_fixture_osdeploy` (entry at line 26, origin
insert at line 166). Include policy in exact idempotent replay comparison (line
78). Retain a separate explicit physical-only registration policy for historical
fixtures. Every dispatch entry point must load and check this durable policy
under the existing authority/run locks. A credential-required origin must reject
ordinary physical dispatch even when no session, alias or delivery row exists.

This policy is fixture configuration, not Python-origin ownership or callback
authentication. No historical run should be upgraded implicitly.

## Exact source work units

| Source | Existing behavior | Required change |
|---|---|---|
| `postgres-store/src/osdeploy/registration.rs` | Server creates origin and stage identities | Atomically bind immutable delivery policy and stable sink identifier; include both in replay matching |
| `postgres-store/src/scheduler/osdeploy/pve.rs:19-73` | Owns transaction and returns physical permit after commit | Extract transaction-local admission/write helper; ordinary and credential entry points enforce persisted policy |
| `postgres-store/src/scheduler/osdeploy/fixture_credential.rs::retain_alias_owner` | Retains immutable alias inside supplied transaction | Reuse directly after session insertion; keep public renewal transaction separate |
| `postgres-store/src/scheduler/osdeploy/receipt.rs::committed` | Constructs send permit and response capture together | Split private capture construction from private send-permit creation; credential arming invokes neither send factory nor physical return path |
| `postgres-store/src/scheduler/osdeploy/transaction.rs::locked_execution` | Authority precedes run, operations, attempts and leases | Preserve lock order; lock/load policy and delivery rows only after these locks |
| `postgres-store/src/osdeploy/execution/load.rs` | Reconstructs current execution and session | Validate complete credential owner and legal acknowledgement/exposure relationships on reload |
| `operation-controller/src/osdeploy.rs:411-415` | Any dispatch selects Outcome collection | Resolve credential recovery before this branch; only exposed and historical physical sessions use ordinary Outcome routing |
| `operation-controller/src/osdeploy.rs:482-507` | Starts physical send immediately after dispatch/checkpoint | Route credential origin through arm, private delivery, acknowledgement persistence and atomic exposure |
| `operation-controller/src/osdeploy/send.rs::submit_and_capture_once` | Consumes permit after continuation check | Keep ordinary path for physical mode; credential exposure must occur after matching ack and current authority check |

All line references above are against the audited source. New public types must
be explicitly feature gated through `scheduler/osdeploy.rs`, `scheduler.rs` and
`lib.rs`; the default build must retain its existing admission behavior.

## Proposed API boundaries

Names below are proposed, not existing APIs:

1. `create_fixture_osdeploy_with_delivery(request_id, plan, policy)` persists a
   closed policy containing a stable fixture sink identity. A request ID replay
   with a different policy is a conflict. The existing create API remains
   explicitly physical-only.
2. Private `prepare_dispatch_in_tx(tx, grant, revision, preflight, request, mode)`
   returns a private prepared dispatch and event identity without committing or
   making a send capability. It preserves all current reconstruction, revision,
   preflight, final-time and lease checks in `pve.rs`.
3. `arm_fixture_start_pe(grant, revision, preflight, request, signing_config)`
   validates persisted credential policy and origin, then writes session,
   deadline, initial alias, dispatch and delivery binding in one transaction.
   Expiry derives from database time and configured lifetime. Return a closed
   envelope after commit; it contains private token bytes and no physical permit.
4. An operation-bound concrete fixture sink consumes/borrows that envelope and
   returns a closed matching acknowledgement. Do not expose a public caller-made
   acknowledgement constructor or a trait implementation that can manufacture
   one from arbitrary metadata. Put sink implementation and acknowledgement
   construction in the same trusted module, or use an opaque receipt from the
   fixture IPC implementation. Generic PVE request JSON remains credential-free.
5. `record_fixture_delivery_ack(grant, acknowledgement)` validates the entire
   persisted owner and sink binding and inserts the append-only ack row. Replays
   preserve the original timestamp and identity.
6. `expose_fixture_start_pe_once(grant)` loads matching ack under the standard
   locks, checks cancellation, authority, current lease, original registration
   deadline and credential expiry, and inserts one exposure row. Only its
   successful commit returns the consuming permit plus response capture.
7. `recover_fixture_start_pe(grant, signing_config)` loads the immutable delivery
   binding. Before exposure it reconstructs exactly the original token and
   verifies its digest and key identifier. After exposure it returns observation
   identity only; it must never invoke the permit factory. Changed configuration
   fails without renewal or a fresh deadline.

The current checkpoint between dispatch and send is not a durable exposure
marker. Place deterministic kill checkpoints around each new commit and IPC
acceptance boundary, then exercise them using actual child processes.

## Storage changes and invariants

Add one feature-only migration containing delivery policy, immutable arming,
append-only acknowledgement and append-only exposure records. Policy must exist
at registration commit, not appear opportunistically on first arming.

The arming key binds operation, run, attempt, dispatch event and package digest.
Its initial alias owner uses a composite foreign key that includes expiry and
digest; add the matching unique index to the alias table. Bind acknowledgement
to that complete arming identity and sink. Bind exposure to the matching ack and
the current worker/generation/lease-acquisition event. Add immutability triggers
for new records and complete reload validation. A primary-key conflict on
exposure is observation-only, never a reason to regenerate a permit.

No raw token, signing key or private transport payload belongs in the migration,
journal, outbox, canonical dispatch request, error strings or generic fixture
log. Persist only non-secret reconstruction metadata and credential digest.

## Focused proof implementation order

Extend `postgres-store/tests/osdeploy_durability.rs` using
`Scenario::fixture_owned()` and its existing prefix setup:

1. Two scheduler instances share a credential-required origin. Ordinary dispatch
   is rejected before any credential row exists; race it against credential
   arming and assert no ordinary permit escapes. This covers the new admission
   prerequisite, not only post-arming rejection.
2. Inject an error after alias insertion. Assert origin survives while session,
   dispatch, alias, deadline and delivery rows all roll back; no envelope escapes.
3. Race two armers, then reopen the database. Assert one initial expiry, alias,
   package, sink and deadline; changed key or policy cannot renew silently.
4. Exercise no ack, valid ack, wrong binding, exposed and malformed row states.
   Assert recovery classification follows durable state and that old physical
   sessions never classify as unsent because delivery metadata is absent.
5. Race exposure under matching ack. Exactly one consuming permit may escape;
   cancelled, stale-generation, expired-lease and expired-original-deadline
   invocations produce none. Repeat after database reopen.
6. Use a known sentinel test token and secret. Query canonical requests,
   journal/outbox JSON and generic fixture logs; assert neither sentinel appears.
   Assert captured private sink receives the exact token and expected digest.
   Add compile-fail coverage for serialization, cloning the capability,
   envelope-to-permit conversion and caller construction of acknowledgements.
7. Add controller subprocess tests at arm commit, sink acceptance, ack commit,
   exposure commit and response loss. Assert stable token digest, idempotent sink
   identity and at most one VM send exposure across replacement workers.

Run focused database and controller tests serially with the existing 16 MiB Rust
test-thread stack setting. Build default features and fixture features separately.
After the cohesive source is stable, qualify these same named tests on Linux and
record the commit/image identity. Compilation and reopen tests alone cannot prove
the process-death cases.

## Implementation disposition

No runtime change is included in this audit. The fresh pre-registration policy
finding changes the implementation order: closing the alternate physical entry
point must precede issuing credential arming capabilities. Adding a standalone
receipt helper, recovery enum or delivery table now would leave that bypass open.
The work is authorized and implementable; no additional user decision was found.
