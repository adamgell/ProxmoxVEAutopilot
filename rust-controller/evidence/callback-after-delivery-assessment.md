# Callback implementation after credential delivery

Historical assessment at `1d61da6d`: subsequent commits implement authenticated
fixture registration and additional fixture completion paths. The gaps below
describe that historical source, not current implementation status. See
`fixture-peregister-authentication-audit.md` for the later source/test audit.

Inspected source: `1d61da6d`. This is a source assessment, not a runtime
compatibility proof. `./skill.sh status` succeeded (104 tools, 84 docs);
the live docs search for Rust callback/PeRegister returned older Python documents,
so the current isolated checkout is the authority for the Rust findings below.

## Present connected boundary

`crates/postgres-store/src/scheduler/osdeploy/fixture_delivery.rs` implements
`arm_fixture_start_pe`, `recover_fixture_start_pe`, `record_fixture_delivery_ack`
and `expose_fixture_start_pe_once`. Migration `0010_fixture_credential_delivery.sql`
connects origin, exact credential alias, boot session, dispatch, acknowledgement
and exposure. These records provide an authenticated callback implementation's
necessary provenance; they do not currently accept a callback.

`crates/api-compat/src/run_bearer.rs::verify_run_bearer` returns a closed verified
claim carrying typed identity, expiry and exact token digest. Signature validity
alone proves neither VM identity nor membership in an admitted PE session.

The existing callback APIs cannot become accepting merely by adding a success
enum variant:

- `osdeploy-adapter/src/start_pe_arming.rs::AuthenticatedPeWitnessV1` has only
  `Unavailable`, and is serializable. Keep it a descriptive refusal type; a real
  authenticated capability needs a separate private, non-deserializable type.
- `postgres-store/src/pe_register_result.rs` never opens a transaction. All its
  current fence/original-result inputs are supplied by the caller.
- `postgres-store/src/pe_register_result_probe.rs` takes locks but always rolls
  back. It requires a *live StartPe lease*. Registration after successful StartPe
  cannot reuse that fence: the callback wait needs its own admitted PeRegister
  operation/attempt and current authority, while retaining the original StartPe
  session/deadline as provenance.
- `scheduler/osdeploy/transaction.rs::admit` admits only Clone, DiskCapacity,
  ConfigurePe and opted-in StartPe. Adding a callback method without extending
  real stage admission, state restoration and controller routing leaves it
  disconnected from the workflow.

## Smallest connected implementation

Implement fixture-only PE registration first, terminating at a durable
PeRegister success and preserving the later PeComplete gate. This is a component
of the full port; it cannot establish action/result callback parity by itself.

1. Admit the registered PeRegister operation only after the required StartPe
   state, using a separately owned callback-wait attempt. Reconstruct its
   dependency, immutable session, package and original registration deadline from
   committed rows. Do not give it a fresh registration time budget on recovery.
2. Accept the bearer through trusted server configuration and resolve its exact
   digest and typed run identity against the fixture alias registry. Join the
   immutable origin, boot session, delivery and exposure records. The client may
   report identity fields, but must not select an attempt, package, role, lease,
   authority generation, result revision or expected session.
3. Under the established authority/run/operation locks, validate the current
   PeRegister grant, cancellation and database clock, then select exactly one
   immutable registration candidate. Commit selected result, journal, operation
   revision/terminal transition and successor eligibility in one transaction.
   Return an authenticated response only after commit.
4. Replay an equivalent committed candidate without a second result or attempt;
   reject conflicting identity/payload. Separate historical replay from authority
   to continue a cancelled or fenced run. Reload the same selected result after
   process loss, including a lost commit response.
5. Route a synthetic fixture client through the controller's actual callback
   entry point. Tests that call only a pure verifier or insert tables directly do
   not demonstrate this slice.

Required tests: valid delivered credential; valid signature but unknown alias;
integer/text identity confusion; wrong run/package/session; absent exposure;
expired token; exact registration-deadline boundary; stale authority/lease;
cancel-versus-register race; two concurrent first registrations; equivalent and
conflicting replay; replacement worker; rollback at each write boundary; process
death before commit and after commit before response. Assert downstream action
exposure remains gated until its own authenticated transaction is implemented.

## Compatibility surfaces to preserve distinctly

The existing corpus, `callback-compatibility-corpus.md`, describes
`web/osd_v2_endpoints.py` registration, next-action and result semantics. That is
not the sole PE registration API. `web/osdeploy_endpoints.py:2150` also exposes
`/runs/{run_id}/pe/register`; `:2171` exposes `/pe/register` with VM identity
lookup. Both inspected handlers select registration and issue a token without a
bearer dependency on the handler signature. Separately, the integer WinPE route
and persistent-agent bootstrap use different identities and selection rules.
Do not silently replace all these surfaces with one claimed compatible handler.

For the first authenticated fixture slice, require possession of the credential
already delivered through the private fixture sink. Record that as the fixture
protocol's explicit stronger prerequisite. Legacy bootstrap/import needs its
own trusted mapping and acceptance tests before real legacy clients can be
claimed supported. Existing deterministic run-only tokens do not authenticate
client-supplied VM identity even when their signature is valid.

After registration, action exposure must bind the original logical attempt and
session durably before returning legacy action JSON. Result acceptance must
resolve that exposure from server state (legacy result bodies lack an attempt
identifier), atomically select the first result and transition the successor.
These remain separate required implementation steps, not implicit benefits of
the registration slice.

Validation: read the named current source and existing compatibility corpus;
no runtime code was changed and no callback runtime tests were claimed. No
production or real Proxmox mutation was performed.

## Implementation dependency audit at `e7cff138`

The remaining obstacle is a concrete conflict in the durable history model,
not a missing success enum. The following edges must change together before
the first accepting fixture transaction can be committed:

| Current source | Required connected change |
| --- | --- |
| `scheduler/osdeploy/lifecycle.rs::claim_osdeploy_bound` | Add a separate PeRegister claim path that resolves StartPe's selected success and opens its own attempt/lease. Read the existing `pe_registration` deadline; do not run the current unconditional deadline INSERT or allocate `mutation_seconds()` from claim time. |
| `osdeploy/execution/load.rs::load_scopes` and `validate_activations` | Registration's scope correctly belongs to StartPe's dispatch, but the current activation validator requires `anchor_operation_id == op`, `anchor_event_id == activation event`, and `opened_at == activation time`. A PeRegister activation reusing the correct deadline cannot satisfy those equalities. Introduce an explicit inherited-scope activation representation and validate its original anchor against committed StartPe dispatch/session rows. |
| `osdeploy/execution/history.rs::enabled` and physical history reconstruction | PeRegister is refused and its plan has no PVE mutation. Add callback history reconstruction separately; enabling the stage in this physical allowlist cannot provide a valid PVE evaluation context. |
| `osdeploy/execution/wire.rs::Detail` and `DecisionEnvelope::validate` | Add a closed registration selection event carrying references to immutable callback provenance and result, with a Satisfied resolution. Existing `PveEvaluated` requires PVE evidence/reason/mode and must not be fabricated for a callback. |
| `migrations/0011_fixture_delivery_reclaim.sql` action/resolution constraint | A new migration must admit the callback discriminator with its exact resolution, while retaining the existing discriminator checks. The callback result needs a unique operation owner plus references to its PeRegister attempt, original StartPe delivery/session/alias and selected journal event. |
| `scheduler/osdeploy/transition.rs` and `osdeploy/execution/load.rs` transition replay | Persist and reconstruct the selected callback proof, terminal operation/attempt transition and original PeCompletion scope anchor atomically. Current transition/replay logic has no registration proof. An operations.state UPDATE alone would be rejected by subsequent history restoration and is insufficient evidence of success. |

The important deadline distinction is between **activation time** and **scope
opening time**. StartPe has already opened `pe_registration`; the later callback
attempt may start strictly afterward, but expiry remains the original deadline.
That distinction must survive both initial claim and replacement lease recovery.
The inherited-scope representation must also preserve selected predecessor
validation: the existing activation validator's fallback predecessor is
DiskCapacity, which is not correct for PeRegister.

The callback request should supply only reported client identity and the bearer.
The server resolves run, registered PeRegister operation, current lease and
immutable expected session; the exact verified token digest joins
`fixture_pe_credential_aliases` to `fixture_pe_deliveries`, then acknowledgement
and exposure. A valid signature without those committed edges cannot create the
closed transaction proof. Equivalent replay reads the selected result without
allocating a second attempt; current authority/cancellation checks remain
separate from that historical answer.

Implementation order: first add the inherited-scope claim and restoration tests;
then add callback provenance/result migration, wire event and transition together;
then route the controller fixture callback through that transaction and exercise
identity, deadline, replay, rollback and process-loss cases. None of these source
changes was made by this dependency audit, and no accepting API was added. The
audit narrows the next implementation boundary without claiming registration or
successor readiness.

## Post-registration action boundary at `617f5b0b`

The fixture registration transaction and controller entry point now exist. The
next manifest stage is **PeComplete**, not a guest action: `osdeploy-adapter/src/stages.rs:98`
classifies it as `CallbackWait`, and `:115-120` places PeComplete, grace, stopped
proof, disk configuration and StartDisk before the first guest action, InstallQga.
`osdeploy-adapter/src/guest_action.rs:25-26` deliberately rejects callback waits.
Using PeComplete's operation ID as a legacy partition/apply action would therefore
invent an action identity outside the admitted manifest.

There is also no immutable WinPE step sequence to expose from the current package.
`postgres-store/src/osdeploy/package_semantics.rs:69-77` materializes only the
registered identity and plan; it does not map a task-sequence version to stable
partition/apply/boot-files step IDs or executable `params`/`content`. The legacy
action/result transaction consequently lacks its source action authority. A
valid registration bearer cannot supply that missing action definition.

The connected next native edge is authenticated **boot-files-staged PeComplete
adjudication**, coupled to truthful grace activation and its original scope in
the same transaction. The atomic grace requirement is already selected in
`docs/superpowers/specs/2026-09-05-rust-osdeploy-durability/next-durable-main-decisions.md:15`.
Generic legacy `/next` and `/result` acceptance additionally needs an admitted
immutable step manifest and session-bound exposure; it cannot be obtained by
relabeling this milestone. This inspection adds no runtime API and makes no
claim of full legacy compatibility.
