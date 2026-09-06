# Durable OSDeploy schema and decision proposal

2026-09-05. Preparation for main's next concrete implementation brief, not implementation authority. Sources read: the complete `next-durable-transaction-boundaries.md`, `next-durable-main-decisions.md`, and committed registration/store/scheduler/operation-controller/manifest interfaces at `5327239`; `e3fd3c5` is the supplied documentation checkpoint. Main's decisions control where the earlier note was provisional. Only this proposal was written. No source edits, tests, builds, Docker, MCP, network calls, commits, or children.

## Admission and migration boundary

Propose additive migration `0005_osdeploy_durability.sql` after accepted registration. Keep migrations 0001–0004 and the existing native/generic wire and transition contracts unchanged. New SQL objects remain private to `postgres-store`; `PgStore::pool()` stays crate-private. No execution table, row, or event is created by `enqueue_osdeploy`. Existing `OsDeployRegistrationV1` remains the declaration-only reload output and preserves its getters/error messages.

The first enabled execution stages are exactly Clone, DiskCapacity, ConfigurePe. Preserve all sixteen `OsDeployStage::ALL` entries and seven PVE mappings; do not create another manifest. Initial activation/resume/dispatch methods deny stages outside this closed three-value match before execution writes. Adding a future stage requires its real dependency implementation and review, not a runtime override or persisted enablement boolean. Public generic OSDeploy intake/claim/start/cancel/finalize/heartbeat/continuation/reaper guards remain unchanged. Historical generic OSDeploy rows are readable history, not adoptable execution records.

## Private relational schema

All new UUID operation/attempt/event values obey the existing UUIDv7 admission. Hash fields use lowercase 64-hex checks; revisions/generations are positive unless explicitly a before-revision, which is nonnegative. Time fields are `timestamptz`. Every new immutable table rejects UPDATE, DELETE, and TRUNCATE using the existing immutability mechanism; independently install/check all trigger forms on migration replay. Mutable projection rows are disposable scheduling data only.

For brevity, `OP` below means `(run_id,operation_id) REFERENCES osdeploy_operation_plans(run_id,operation_id)`, `EV` means `(event_id,operation_id) REFERENCES journal_events(event_id,operation_id)`, and `AT` means `(attempt_id,operation_id) REFERENCES attempts(attempt_id,operation_id)`. Spell all pairs in this exact order in SQL. Cross-operation references carry both referenced operation and event IDs plus a same-run OP FK; never accept an event UUID alone as proof.

| Table | Concrete columns, uniqueness and local constraints |
| --- | --- |
| `osdeploy_decisions` | `operation_id uuid`, `decision_revision bigint`, `event_id uuid UNIQUE`, `run_id uuid`, `attempt_id uuid NULL`, `action text`, `resolution text NULL`, `workflow_sha256 text`, `stage_sha256 text`, `generation bigint`, `evaluated_at timestamptz`, `payload_canonical_json text`; PK(operation_id,decision_revision), UNIQUE(event_id,operation_id), OP/EV/AT. `resolution` is NULL or ready/waiting/satisfied/failed/blocked/unknown/conflicted. Closed action and action↔resolution/null-attempt checks below. Canonical text is a JSON object with at most 65536 octets. |
| `osdeploy_deadlines` | `run_id uuid`, `scope_key text`, `anchor_operation_id uuid`, `anchor_event_id uuid`, `opened_at timestamptz`, `budget_seconds integer CHECK BETWEEN 1 AND 86400`, `deadline_at timestamptz`; PK(run_id,scope_key), same-run OP and EV to anchor, FK(anchor_event_id,anchor_operation_id)→osdeploy_decisions(event_id,operation_id). CHECK `deadline_at = opened_at + budget_seconds * interval '1 second'`. Closed scope keys in the next table. |
| `osdeploy_attempt_bindings` | `operation_id uuid PK`, `run_id uuid`, `attempt_id uuid UNIQUE`, `scope_key text`, `activation_event_id uuid`, `activated_at timestamptz`, `deadline_at timestamptz`, `activation_mode text CHECK IN ('leased','parked')`; OP/AT, FK(run_id,scope_key)→deadlines, EV plus typed decision FK for activation. UNIQUE(operation_id,attempt_id). `deadline_at > activated_at`. No new attempt-number column: reload requires the single existing attempt to have number 1 and the exact pinned times. |
| `osdeploy_lease_epochs` | `acquisition_event_id uuid PK`, `operation_id uuid`, `run_id uuid`, `attempt_id uuid`, `executor_kind text CHECK='rust'`, `generation bigint`, `worker_id text CHECK btrim<>''`, `lease_token_sha256 text UNIQUE`, `acquired_at timestamptz`, `initial_expires_at timestamptz`, `deadline_at timestamptz`, `purpose text CHECK IN ('initial_evaluation','resume_evaluation','reclaimed_evaluation')`; OP/AT, EV and typed decision FK to acquisition; FK(operation_id,attempt_id)→attempt_bindings. CHECK `acquired_at < initial_expires_at AND initial_expires_at <= deadline_at`. Store the raw token only in the existing worker_leases row/grant; journal/exported history uses its hash. |
| `osdeploy_pve_evidence` | `event_id uuid PK`, `operation_id uuid`, `run_id uuid`, `attempt_id uuid`, `evidence_revision bigint`, `evidence_sha256 text`, `source text CHECK='fake_pve'`, `evidence_canonical_json text`; OP/AT/EV, FK(operation_id,attempt_id)→attempt_bindings, UNIQUE(operation_id,evidence_revision), JSON object ≤1048576 octets. This immutable provenance index distinguishes admitted typed capture from generic EvidenceRecorded JSON. Full evidence remains duplicated in its journal event and must compare identically on reload. |
| `osdeploy_pve_dispatches` | `operation_id uuid PK`, `run_id uuid`, `attempt_id uuid`, `dispatch_event_id uuid UNIQUE`, `dispatch_revision bigint`, `preflight_event_id uuid`, `workflow_sha256 text`, `pve_plan_sha256 text`, `request_sha256 text`, `request_canonical_json text`, `source text CHECK='fake_pve'`, `original_generation bigint`, `dispatched_at timestamptz`, `lease_acquisition_event_id uuid`; OP/AT, FK(operation_id,attempt_id)→attempt_bindings, EV and typed decision FK for dispatch, typed evidence/event FK for preflight, FK to lease epoch. Request JSON object ≤1048576 octets. All links are operation-bound. No send/retry count and no replaceable request slot. |
| `osdeploy_pve_receipts` | `operation_id uuid PK/FK dispatches`, `receipt_event_id uuid UNIQUE`, `receipt_kind text CHECK IN ('task','synchronous')`, `upid text NULL`, `accepted_at timestamptz`, `recorded_at timestamptz`; EV, CHECK task iff UPID nonnull/nonblank and synchronous iff UPID null; CHECK recorded_at≥accepted_at. First store capture samples one DB time for both columns. Exact typed receipt admission checks UPID/action/request binding. |
| `osdeploy_run_cancellations` | `run_id uuid PK/FK osdeploy_runs`, `anchor_operation_id uuid`, `decision_event_id uuid UNIQUE`, `generation bigint`, `requested_at timestamptz`; same-run OP/EV and typed decision FK. Anchor is the registered Clone operation, even if already terminal; it does not overwrite Clone's terminal decision. |
| `osdeploy_schedule_projection` | `operation_id uuid PK`, `run_id uuid`, `attempt_id uuid`, `mode text CHECK IN ('waiting','unknown_reconciliation')`, `basis_event_id uuid`, `basis_revision bigint`, `scope_key text`, `next_check_at timestamptz NULL`, `unavailable_count smallint CHECK BETWEEN 0 AND 4`, `rebuilt_through_revision bigint`; OP/AT, FK(operation_id,attempt_id)→attempt_bindings, EV/typed-decision basis FK, FK(run_id,scope_key)→deadlines. Waiting requires a nonnull next-check. Unknown permits null only for a task receipt still missing. This is the only mutable new table. |

For operation-bound composite references, additionally declare UNIQUE(event_id,operation_id) on osdeploy_pve_evidence and UNIQUE(acquisition_event_id,operation_id) on osdeploy_lease_epochs; dispatch references those exact pairs for preflight and lease history. Use the table's named event column in each EV reference (activation_event_id, dispatch_event_id, receipt_event_id, or basis_event_id as applicable). Unless explicitly marked NULL, all table columns are NOT NULL. Add a scheduling index on (mode,next_check_at,operation_id), deadline index on (deadline_at,run_id,scope_key), and decision index on (operation_id,decision_revision DESC) filtered to nonnull/non-ready resolution.

Create typed decision FKs that participate in insertion cycles as `DEFERRABLE INITIALLY DEFERRED`; ordinary OP/AT/EV FKs remain immediate. Every reference must resolve before commit. All schema writes are through family-private transactions; SQL-local constraints do not claim full semantic validation. Reload verifies exact canonical objects, scope membership, cross-row hashes, action pairing, event kind/revision/payload, and family identity. Load all attempts for the operation and require exactly one when bound, zero when unactivated; an extra attempt is corruption, not a new retry. Compare mutable attempt `started_at/deadline_at` to the immutable binding every time. Existing shared VM reservation digest remains the NativeVmPlan digest, separate from all workflow/stage/PVE/request digests.

New decisions/evidence canonical text is produced by closed serde wire structs/enums with required nullable fields, deny-unknown-field decoding, duplicate-key rejection before normalization, and bounded input. It is never raw arbitrary command JSON or a public authenticated/ownership constructor. Recompute the canonical hash and compare with the exact journal payload. Typed PVE request/evidence restoration uses the completed named/validated provisioning contract and matching original bindings. Receipt rows reconstruct `ProvisioningReceiptV1` against the full original dispatch. No public Deserialize is added to admitted ownership/baseline types.

## Deadline scopes and atomic insertion order

| `scope_key` | Member stage(s) and policy getter | Anchor decision |
| --- | --- | --- |
| `mutation_clone`, `mutation_disk_capacity`, `mutation_configure_pe`, `mutation_start_pe`, `mutation_pe_ensure_stopped`, `mutation_configure_disk`, `mutation_start_disk` | Corresponding PVE stage, `mutation_seconds()` | That stage's first activation |
| `pe_registration` | PeRegister, `registration_seconds()` | Original StartPe dispatch |
| `pe_completion` | PeComplete, `pe_seconds()` | Selected PeRegister success |
| `shutdown_grace` | PeShutdownGrace, `shutdown_grace_seconds()` | Selected PeComplete success |
| `full_os` | InstallQga, VerifyQga, InstallQgaWatchdog, InstallAgent, AgentHeartbeat, VerifyOperational; `full_os_seconds()` | Original StartDisk dispatch |

Initial PVE activation locks/reloads, samples DB time T, allocates one attempt/event ID, and inserts attempts(number=1, state=leased, started_at=T, deadline_at=T+budget). Append activation decision, insert scope/binding, append lease-acquisition decision/epoch, insert the existing worker_leases row, and append Pending→Leased. All writes commit together. The stage activation decision carries the new scope and exact budget/time; no event pretends the worker has started evaluation yet. First start appends one `attempt_started` and ordinary Leased→Running plus a closed evaluator-activity decision. Safe reclaim/reacquisition never appends another attempt-start once the attempt has started.

An inherited activation reads the existing scope and uses actual activation DB time, never the anchor time as a fictional worker start. If now≥deadline, use the selected null-attempt Pending→Unknown expiry transaction; no attempt/lease/binding is inserted. If an activated but safely reclaimed Pending attempt expires, preserve that attempt and use activated-scope expiry instead. Rows with no opened applicable scope cannot expire under this rule.

**Mandatory future grace transaction:** after validating callback proof and before committing PeComplete Satisfied, use one sampled adjudication time T for its selected decision and new grace scope. Create PeShutdownGrace's actual attempt at T with deadline T+grace budget, activation_mode=parked, attempt state Waiting, and a `grace_wait_activated` decision referencing the exact PeComplete success. Use a private Pending→Waiting proof that is valid only for this exact immediate successor and atomic selected success, then record the initial park/due decision with next_check_at=T and no worker lease. No fabricated Running or attempt_started event is emitted. The full transaction commits PeComplete success, grace anchor, truthful attempt/activation, Waiting state and due proof together, even if T+budget is elapsed by commit time; due processing then emits the original eligible grace timeout. Thus restart cannot strand an otherwise valid grace as never activated. This future decision variant/transition must land with actual callback proof, not as a publicly invocable early placeholder.

The first grace evaluator resumes Waiting with the existing attempt and can append that attempt's single `attempt_started` when it truly starts; normal resumed attempts with an existing start event do not append another. Grace timeout still requires the exact PeComplete selected success and original grace binding, and emits only `ShutdownGraceDeadlineExpired`. Genuinely unactivated expiry uses `InheritedScopeDeadlineExpired` and never enables force-stop.

## Closed decision envelope and payload variants

Every `osdeploy_decisions` event payload has exactly these common fields: `contract_version:1`, `action`, `run_id`, `operation_id`, `workflow_sha256`, `stage_sha256`, `attempt_id` (required nullable), `generation`, `before_revision`, `evaluated_at`, `resolution` (required nullable), and `detail`. `before_revision` is the actual aggregate revision before this first append; `decision_revision=before_revision+1`. `detail` is a closed struct selected by action, not a map. The selected-state decision is the highest decision_revision with a nonnull resolution other than ready; audit/control events never replace it. Resolution→state changes and any wait schedule update occur in the same transaction; when state already equals resolution, append no redundant state event.

The table below specifies all currently proposed detail fields; fields denoted `?` are present with null when absent. Enum reason spelling is canonical snake_case in wire JSON, matching the closed Rust reason value. Every decision repeats no secrets, raw lease tokens, authentication values, or unbounded error text.

| Action | Resolution; detail fields and fixed admission |
| --- | --- |
| `stage_activated` | NULL; `scope_key, anchor_operation_id, anchor_event_id, opened_at, budget_seconds, deadline_at, predecessor_operation_id?, predecessor_decision_event_id?`. Attempt mandatory. Clone has null predecessor; every other enabled stage has exact selected Satisfied predecessor. |
| `lease_acquired` | NULL; `purpose, acquisition_event_id, token_sha256, worker_id, acquired_at, expires_at, deadline_at, prior_schedule_event_id?`. Attempt mandatory. Event ID equals its acquisition_event_id; purpose initial/resume/reclaimed matches epoch. |
| `evaluation_started` | NULL; `lease_acquisition_event_id, activity` where activity is `preflight_read` or `outcome_read`; this is the proof of bounded observational activity. Attempt mandatory. Dispatch presence determines activity. |
| `lease_renewed` | NULL; `lease_acquisition_event_id, heartbeat_at, expires_at, deadline_at`. Attempt mandatory; unchanged original deadline; update existing lease and journal atomically. |
| `pve_dispatch_committed` | ready; `preflight_event_id, pve_plan_sha256, request_sha256, dispatched_at, lease_acquisition_event_id`. Attempt mandatory, original request and source match dispatch row. Only Ready preflight and the enabled stage set admit this action. |
| `pve_evaluated` | waiting/satisfied/failed/blocked/unknown/conflicted; `mode, advice, evidence_event_id, evidence_sha256, scope_key, deadline_at, reason, lease_acquisition_event_id?, schedule?`. Mode is preflight/outcome/reconciliation; advice is the actual closed evaluator decision, and reason is the completed closed `ProvisioningReasonV1`, with exact evaluator-result pairing. Resolution equals advice except reconciliation Waiting advice yields Unknown resolution; Ready belongs only to the separate dispatch boundary. Reconciliation has no execution lease; other modes require one. |
| `lease_reclaimed_same_attempt` | NULL; `lease_acquisition_event_id, scope_key, deadline_at, reason=lease_expired_before_start`. Attempt mandatory; exact selected Leased→Pending proof, no exposure/start/cancellation. State event target is Pending although resolution remains null, so it is never selected as terminal proof. |
| `evaluation_reparked` | waiting; `lease_acquisition_event_id, activity_event_id, scope_key, deadline_at, reason=read_only_evaluator_lease_expired, schedule`. Attempt mandatory, original activity/epoch, no exposure/cancellation, unexpired budget. Running→Waiting or already Waiting. |
| `scope_expired_before_activation` | unknown; `scope_key, anchor_operation_id, anchor_event_id, deadline_at, reason=inherited_scope_deadline_expired`. Attempt NULL and Pending with zero attempts/exposure; exact inherited scope membership. |
| `activated_scope_expired` | unknown; `scope_key, anchor_operation_id, anchor_event_id, deadline_at, pe_complete_operation_id?, pe_complete_decision_event_id?, reason`. Attempt mandatory; reason phase_deadline_expired except exact grace timeout uses shutdown_grace_deadline_expired and mandatory paired PeComplete reference. |
| `run_cancelled` | NULL; `reason=run_cancellation_requested`. May have null attempt; always on registered Clone, referenced by immutable cancellation row. Does not replace existing terminal decision. |
| `stage_cancelled_unexposed` | blocked; `cancellation_event_id, scope_key?, deadline_at?, reason=run_cancelled_before_exposure`. Attempt nullable only when genuinely unactivated; no dispatch/exposure possible. |
| `stage_cancelled_exposed` | unknown; `cancellation_event_id, scope_key, deadline_at, dispatch_event_id, reason=run_cancelled_outcome_uncertain`. Attempt mandatory; initial phase recognizes PVE dispatch only. Guest exposure requires its future separately typed variant/constraints. |
| `residual_lease_revoked` | NULL; `terminal_decision_event_id, lease_acquisition_event_id, reason=terminal_lease_revoked`. Attempt mandatory and exact terminal linkage; state/attempt stay unchanged. |
| `reconciliation_scheduled` | NULL; `terminal_decision_event_id, dispatch_event_id, scope_key, deadline_at, reason, schedule`. Attempt mandatory and selected state Unknown. Reasons are original_dispatch_unresolved, original_receipt_captured, observation_unavailable, reconciliation_still_unknown; never opens execution authority. |
| `grace_wait_activated` | waiting; **future callback migration only:** `pe_complete_operation_id, pe_complete_decision_event_id, scope_key=shutdown_grace, deadline_at, activated_at, schedule`. Attempt mandatory; exact atomic grace transaction above. |

`schedule` is exactly `{mode, next_check_at, unavailable_count}`; nullable next_check is permitted only in unknown_reconciliation awaiting an original task receipt. A waiting `pve_evaluated` requires a waiting schedule; other resolutions have null schedule, and Unknown with an eligible original dispatch uses a separate reconciliation_scheduled event. A pve_evaluated/Waiting row can originate only from actual evaluator Waiting advice outside reconciliation; unavailable or deadline Unknown is never silently recast as Waiting. `reason` selects only permitted evaluator reasons, never free text. Run-cancelled and audit decisions are control history, not stage success evidence. The initial ledger permits null attempt only for scope_expired_before_activation, unactivated stage_cancelled_unexposed, and run_cancelled; all other initial actions require it. The future grace_wait_activated discriminator/transition is added only by the real callback phase, not to the initial enabled action set.

Additional missing-receipt/expiry-after-dispatch classification needs one closed `lease_expired_uncertain` action: resolution unknown, mandatory attempt, detail `{lease_acquisition_event_id, dispatch_event_id, scope_key, deadline_at, reason:lease_expired_after_dispatch}`. This records the reaper's conservative Unknown before any optional reconciliation schedule. An unexplained operation/lease/activity combination without admitted dispatch fails validation rather than fabricating an exposure row. Include this action in the migration's closed CHECK set.

Immutable receipt capture is an ordinary typed EvidenceRecorded event indexed by osdeploy_pve_receipts, with exact payload `{contract_version:1, action:pve_receipt_captured, dispatch_event_id, request_sha256, receipt_kind, upid, accepted_at}`. Capture compares the original dispatch and admits a matching old-worker response without current execution authority. First capture fixes accepted_at=recorded_at=DB now; reload requires that time at/after original dispatch time, and equivalent replay preserves both and adds no new receipt event. It may not insert an authoritative reconciliation schedule from a stale worker; current-authority due repair notices the new receipt, then journals scheduling separately.

Idempotent semantic keys are operation-scoped and action-specific: activation/dispatch/receipt once per operation; acquisition/start/renewal/reclaim/repark/revocation include acquisition event ID and, for repeated renewal, before_revision; expiry includes original scope and attempt-or-`unactivated`; cancellation includes run cancellation event; evaluation includes mode and evidence event; reconciliation schedule includes basis terminal event plus current before_revision. Check an existing key before sampling replacement payload times. Same key+same admitted input returns original outcome; incompatible replay conflicts. Evidence fence remains its original event revision minus one, independently of later decision CAS.

## Short leases, waits and terminal reconciliation

Reuse `worker_leases` and private `LeaseGrant::new` only for ordinary active evaluators. Initial lease interval is 30 seconds, capped at the original scope; no zero-length lease. Heartbeat is requested at most every 10 seconds and rechecks authority, run cancellation, operation/attempt/epoch/token and deadline under locks before extending only that lease. Update projection/event revisions when the heartbeat event advances the aggregate; a previously collected evidence event keeps its original fence and may require recollection after the new control event. Never reuse generic claim's five-minute attempt creation.

Park commits decision, Running→Waiting (if needed), attempt Waiting/completed_at null, scheduling projection, and deletion of matching worker lease together. Resume requires the current park basis plus due signal and no residual lease; it creates a fresh epoch/token on the same attempt. Repark requires complete read-only activity and no prior dispatch/exposure. With original dispatch, an expired observer becomes Unknown and only the path below may reconcile it. Terminal cleanup validates epoch/token hash/current attempt and exact selected terminal decision; revoke only matching rows. Cancellation applies terminal-preserve→elapsed-scope→unexposed/exposed precedence across all sixteen registered operations, commits the run fence and stage outcomes together, and never releases shared identities.

Propose fixed scheduling constants for this local phase: sweep every 1 second, batch limit 32, normal PVE Waiting interval 2 seconds, Unknown reconciliation interval 5 seconds, and unavailable-observation delay 2/4/8/10 seconds with persisted counter capped at 4. Successful usable observation resets the unavailable counter. Every next_check is capped by the original deadline, except a missing-task-receipt Unknown has null next_check and is rediscovered by receipt repair or scope expiry. No immediate tight retry loop, deadline reset, user-selected poll interval, or exponential extension beyond ten seconds. Existing controller read/collection/call bounds of 2/6/24 seconds are ceilings for the new path, further capped by remaining lease/scope time; expiration yields an honest unresolved result. These are scheduler intervals, not guest action timeout values.

Waiting discovery shape (read-only; no row locks):

```sql
SELECT s.operation_id, s.basis_event_id, o.revision
FROM rust_controller.osdeploy_schedule_projection s
JOIN rust_controller.operations o USING (operation_id)
JOIN rust_controller.osdeploy_attempt_bindings b USING (operation_id)
JOIN rust_controller.osdeploy_deadlines d
  ON (d.run_id,d.scope_key)=(s.run_id,s.scope_key)
WHERE s.mode='waiting' AND o.workflow_kind='os_deploy' AND o.state='waiting'
  AND s.next_check_at<=clock_timestamp()
  AND NOT EXISTS (SELECT 1 FROM rust_controller.worker_leases l
                  WHERE l.operation_id=s.operation_id)
ORDER BY s.next_check_at,s.operation_id LIMIT 32;
```

Current-authority claim/resume repeats full validation under locks, including identity of binding/basis and cancellation, and chooses scope expiry instead of leasing at/after deadline. Expired residual leases are classified separately by the family reaper. Do not use `FOR UPDATE SKIP LOCKED` on operations during discovery: that would acquire operation locks before run/authority.

Unknown observational discovery uses the same joins plus `osdeploy_pve_dispatches x`, `s.mode='unknown_reconciliation'`, `o.state='unknown'`, `s.next_check_at<=DB now`, `DB now<d.deadline_at`, no worker lease of any age, and no run cancellation; order/limit as above. Full locked reload additionally requires the exact current selected Unknown decision, original dispatch/attempt, admitted receipt if the original request requires a task receipt, or the completed evaluator's permitted receiptless synchronous reconciliation. Missing task receipt never triggers guessed task discovery. No new attempt, worker lease, dispatch permit, or Waiting transition is produced by this path.

For Unknown, collect bounded read-only evidence outside locks; then use current authority/run/all-operation locks, original attempt/request/receipt, and expected aggregate CAS to evaluate in Reconciliation mode. Only completed evaluator Satisfied/Failed/Conflicted may privately replace Unknown before the original deadline. Waiting advice remains Unknown; persist its observation plus reconciliation scheduling with no Waiting resolution. Concurrent collectors may duplicate reads but only the first valid CAS decision commits. No second lease/authority is introduced merely to suppress harmless duplicate reads; limit local collector concurrency with the existing worker budget. Cancellation/expiry while collecting wins on re-entry and stops authoritative recovery.

Projection reconstruction selects the latest validated state-affecting decision and its subsequent scheduling/control chain, replaying absolute next_check/counter values. Evidence/receipt events may advance aggregate revision without changing the selected basis; ordinary CAS handles that, and rebuilt_through_revision is just the reconstruction watermark. On startup and each bounded repair sweep, scan registered Waiting/Unknown operations in stable UUID keyset pages of 32, reconstruct missing/stale projection rows, and detect a newly captured receipt for receipt-waiting Unknown. Retain the keyset cursor only as a scan hint and wrap to the beginning; restarting from the beginning is safe. An additional immutable-scope scan finds elapsed activated and genuinely unactivated inherited members even if every wait projection is absent. At scope expiry, emit at most one exact expiry decision and remove the scheduling row, including Unknown→Unknown expiry without another state event. Late evidence remains capturable but is not periodically promoted or polled forever. Outbox notifications may accelerate these scans but their delivery/acknowledgement is never required permission.

## Complete lock order and compatibility

For new execution mutations: **authority row FOR SHARE → `native:run:{run}` → optional existing sorted VM identity locks → optional `osdeploy:agent:{expected_agent_id}` → optional `rust-controller:scheduler:os_deploy` cap lock → all sixteen operation rows sorted by UUID → attempts/worker lease rows for affected operations in that same order → family projection rows**. Never acquire an earlier lock while holding a later one. Immutable referenced rows are read after these locks, without competing mutation. First activation/resume cap counts all unexpired OSDeploy worker leases, including old-generation leases not yet safely classified; a generation change does not create spare physical capacity by itself. Existing cap validation remains positive-only, with fixed error mapping below.

Normal execution does not reacquire VM/agent identity locks: it validates immutable shared reservations under the run lock. Including their optional positions states the total order if a future atomic intake/execution path is approved; this phase does not add that path. Current registration uses run→sorted VM identities→agent→new operation inserts, never authority/cap/lease; it remains unchanged and cannot wait on a new earlier lock. Registration replay uses its same run lock and typed read without acquiring authority afterward. Old native uses authority→run→native cap→its sorted operations→lease and a disjoint cap key; preserve it. Generic intake now uses the shared run lock before command/operation locks and rejects a registered OSDeploy run. Generic observation append takes its operation lock only and never asks for run/authority afterward, so it may cause a CAS retry but not a reversed lock cycle.

New receipt capture uses authority-row FOR SHARE without generation ownership enforcement, then run→sorted operations, retaining the old-original-worker evidence principle; it acquires no evaluator lease or cap. Reaper, cancellation, terminal cleanup and authoritative projection repair use normal authority ownership validation then run→sorted operations→matching leases/projections. Cross-operation cancellation locks lease/attempt rows in sorted operation order. Authority handoff itself acquires authority FOR UPDATE and no downstream operation/run lock; the operational drain happens outside DB transactions. No network call or host observation occurs under these DB locks.

## First-three-stage callable boundary

Keep registration output/error behavior stable. Add a separate fixed, payload-free `OsDeployExecutionError` with Validation, Conflict, FenceLost, CapabilityUnavailable, and StorageUnavailable. Do not reuse registration-specific error text for runtime failures or widen old native errors. Stale generation/executor/worker/token/CAS/deadline/cancellation maps FenceLost; wrong typed proof/persisted binding maps Validation; incompatible immutable replay maps Conflict; any fourth-or-later execution entry maps CapabilityUnavailable. Invalid cap maps Validation. Candidate-not-yet-eligible/cap-exhausted claims return None without changes.

Proposed siblings on the existing store/scheduler, with private-field read outputs and no public constructors/Deserialize:

```text
PgStore::load_osdeploy_operation(operation) -> OsDeployOperationSnapshot
PgStore::record_osdeploy_pve_evidence(operation, attempt, expected_revision, &ProvisioningEvidenceV1) -> EventId
Scheduler::claim_osdeploy_bound(operation, workflow_sha256, cap) -> Option<LeaseGrant>
Scheduler::resume_osdeploy_bound(operation, attempt, expected_revision, workflow_sha256, cap) -> Option<LeaseGrant>
Scheduler::start_osdeploy_bound(&grant, workflow_sha256) -> ExecutionState
Scheduler::heartbeat_osdeploy_bound(&grant, workflow_sha256) -> current revision/lease expiry
Scheduler::continuation_osdeploy_bound(&grant, workflow_sha256) -> read-only current fence result
Scheduler::begin_osdeploy_pve_dispatch(&grant, expected_revision, preflight_event, &typed_request) -> one-shot permit
Scheduler::record_osdeploy_pve_receipt(&original_capture_handle, &MutationReceipt) -> ()
Scheduler::decide_osdeploy_pve(&grant, expected_revision, evidence_event) -> OsDeployProgress
Scheduler::reconcile_osdeploy_unknown(operation, original_attempt, expected_revision, evidence_event, workflow_sha256) -> OsDeployProgress
Scheduler::cancel_osdeploy_run(run) -> ()
Scheduler::reap_osdeploy_expired() -> family counts
Scheduler::discover_osdeploy_due() -> bounded read-only Waiting/Unknown work descriptors
```

`OsDeployProgress` is Idle, Active(state,revision), Parked(next_check_at,deadline_at), or Decided(state,revision); an Unknown decision never pretends full workflow success. Execution snapshots expose complete validated plan/state/history for observation, not a permit. Discovery only advertises the first-three enabled stages. Cancellation can fence all sixteen registered rows, including unactivated later ones, without enabling them. Deadline/recovery helpers for callback-dependent scopes remain private and unreachable until their anchors can actually be created.

Integrate a typed `run_osdeploy_once` path within the existing operation-controller crate using the same PgStore, Scheduler, and concrete NativeFakePve supplied by the existing composition. It may be a sibling module/function; it is not another daemon, store, authority, generic command executor, or production transport. Preserve existing NativeController behavior/API. Read/advice collection occurs outside transactions, and the store reconstructs all authoritative contexts. A restart discovers original durable state; it does not depend on the old controller's in-memory grants map. A fresh call cannot claim StartPe or adopt fixture-created Satisfied predecessors.

The one-shot dispatch permit must be consumed by value at the mutation call boundary. A separate non-authorizing original capture handle may survive the call for late exact response recording; it cannot be converted back into a permit or expose a reusable request to a generic sender. Pin those Rust visibility/ownership signatures in the next task brief against the actual FakePve mutation API. Existing native permit behavior is not changed by this proposal.

## Gates, unresolved contracts and acceptance evidence

StartPe stays inaccessible until a real prepared server session can be armed atomically with original dispatch and the PE-registration anchor. The callback phase must provide exact session/role/label/credential issuance and proof normalization, not an `authenticated` boolean. Add future stage-specific stop-gate rows/FKs and guest exposure/result variants only alongside their reviewed contracts; this migration deliberately creates no placeholder callback/session/action tables or nullable bypass gate in the initial dispatch row. The future stop dispatch must require its exact persisted gate, and future StartPe must require its armed session. The mandatory atomic grace activation above is part of that phase's contract.

Remaining decisions are bounded: main must accept the proposed scheduling constants, private schema/payload names and typed execution errors; the implementation brief must settle the precise one-shot send/capture Rust API against the existing FakePve methods. Callback session wire/arming, five-action timeout serialization, authenticated result continuation, persistent heartbeat and host-QGA proof remain unimplemented dependencies, so their schema cannot be truthfully frozen here. These are not reasons to weaken first-three-stage invariants or declare registration/the prefix the full goal.

Service send admission is closed on startup/shutdown/handoff, coordinates all cooperative local mutation tasks, cancels queued unsent work, and joins/drains send-capable tasks before local quiescence is declared. Drain timeout fails the handoff gate. Fresh DB continuation check is necessary but does not fence PVE between check and send; expired leases and recorded generations do not stop a suspended sender or an already accepted remote task. Cross-process Python/Rust replacement needs verified old-writer shutdown/disablement and reconciliation of ambiguous work, or an independently verified mutation-ingress fence. No such external fence is claimed or implemented here. Keep locally gated send initiation distinct from remote acceptance-time deadlines/exactly-once claims and external mutation approval.

Future validation must cover additive migration replay/immutability, reload corruption and original evidence fences, transaction rollback at each insert boundary, same attempt across independent-pool lease races, first-three real family FakePve dispatch and restart/no-resend, receipt loss and stale-worker capture, Waiting versus Unknown scan/repair, expiry at equality, no callback-stage reachability, cancellation/terminal lease linkage, native/generic regressions, and eventual crash between PeComplete adjudication and grace handling. None were executed by this design task. Full sixteen-stage service workflow, compatible guest interactions, Linux/process restart/fault/restore/rollback, single-writer cutover and readiness evidence remain required under the continuing goal; real PVE and production remain read-only.

## Amendment: concrete consuming send and original capture API

Inspected at `5327239`: `ProvisioningFakePort::submit_provisioning(&self, &ProvisioningMutationRequestV1) -> Result<MutationReceipt,PveWriteError>` is a public async method on a sealed fake capability; `NativeFakePve` implements it. `postgres-store` already depends on `pve-port`; `operation-controller` already depends on both. Existing `NativeDispatchPermit` is non-Clone/non-serde but exposes `dispatch()`; preserve that old API and do not copy its getter onto the new OSDeploy permit. `ProvisioningDispatchV1` and its request remain inspectable data values, so holding them cannot prove that a database transaction committed or that a worker still has authority.

Select this least-invasive candidate for the next implementation brief. It supersedes the earlier shorthand return type for `begin_osdeploy_pve_dispatch`. All omitted fields are private to the OSDeploy store module, and neither struct derives Clone, Copy, Serialize, or Deserialize. Use custom redacted Debug or no Debug; no derived Debug may disclose the request. No public constructors, conversion from snapshots/dispatch values, Deref, request getter, dispatch getter, generic callback accepting the request, or `into_inner` exists.

```rust
// postgres-store: declared in its private OSDeploy module, exported as opaque types.
pub struct OsDeployDispatchPermit {
    dispatch: ProvisioningDispatchV1,
}

pub struct OsDeployResponseCapture {
    identity: OriginalOsDeployDispatchIdentity, // private, no request payload
}

impl Scheduler {
    pub async fn begin_osdeploy_pve_dispatch(
        &self,
        grant: &LeaseGrant,
        expected_revision: i64,
        preflight_event: EventId,
        candidate: &ProvisioningMutationRequestV1,
    ) -> Result<(OsDeployDispatchPermit, OsDeployResponseCapture), OsDeployExecutionError>;

    pub async fn record_osdeploy_pve_receipt(
        &self,
        capture: &OsDeployResponseCapture,
        receipt: &MutationReceipt,
    ) -> Result<(), OsDeployExecutionError>;
}

impl OsDeployDispatchPermit {
    pub async fn submit_fake_once(
        self,
        fake: &NativeFakePve,
    ) -> Result<MutationReceipt, PveWriteError> {
        // This is the sole submission in this consuming body. No retry loop.
        fake.submit_provisioning(self.dispatch.request()).await
    }
}
```

This async method lives in `postgres-store`, where its private request can be borrowed without exporting it across a crate boundary. It uses the existing pve-port dependency, introduces no operation-controller→store→operation-controller cycle, and needs no new Tokio production dependency in postgres-store: the controller owns timeouts and task lifecycle. Keep the receiver concrete `&NativeFakePve`; do not add a generic transport, caller closure, production observer implementation, or separate executor. The method is a single-call fake submission primitive and not a service admission manager or an authority checker; its own signature provides no timeout without the controller wrapper.

Construct both returned values only after the immutable dispatch transaction commits, using the same validated dispatch. `OriginalOsDeployDispatchIdentity` contains the exact run/operation/attempt, source, full workflow/PVE/request hashes, dispatch event/revision, original generation, original preflight event, and dispatch time. It deliberately contains neither the request payload nor a worker token. The capture handle cannot submit, regenerate a permit, or finalize state. Receipt recording reloads the complete original dispatch, compares every identity field, then applies the existing typed receipt matching and immutable first-capture rules. Borrowing the capture and receipt allows retrying **only persistence of the same response** after temporary storage failure; it does not allow another fake submission. As with the current native receipt API, the typed handle binds the response's provenance claim to the original dispatch but does not cryptographically attest the caller's supplied MutationReceipt; admission still validates the actual receipt and its original dispatch. No public API restores a capture handle from user-supplied identity fields.

The consuming method moves the permit into its returned future when called, including before that future is first polled. Dropping the permit before the call, dropping that future unpolled, cancelling it at an await, timing it out, or receiving any PveWriteError never returns a permit. There is no I/O in Drop and no compensating send, queue reinsert, request replay, or new attempt. After dispatch commit, even an apparently unsent/drop-before-poll case remains conservatively unresolved in durable state; recovery observes the original identity and never reconstructs the permit. If the original response is available, the separate capture handle can record it after a lease/generation change. If cancellation loses the response, retain Unknown according to the existing expiry/recovery policy; the handle alone cannot fill in a missing receipt. Prefer cooperative drain/join over abort when that can preserve a returned response, but do not claim dropped futures prove that an external operation was cancelled.

### Cooperative admission belongs in operation-controller composition

Add a crate-private `OsDeploySendAdmission` component beside the new OSDeploy controller path, shared by every send-capable task in that service instance. The existing service composition owns its lifecycle; it starts closed and exposes only controlled startup/open and close-and-drain operations internally. Its admission guard is a local in-flight task accounting value, not another database authority or an object imported by postgres-store. Do not put that guard type into `submit_fake_once`, which would require an inverted dependency; do not claim the public low-level primitive enforces this cooperative gate by itself.

The approved controller path owns `(permit,capture)` and is the only normal application caller of submit_fake_once. For each send it acquires an admission guard, rechecks current continuation against the original grant, derives a timeout from the configured call bound and remaining lease/scope time, then moves the permit into exactly one bounded call. A failed gate/continuation check drops the permit and proceeds only to observation; there is no retryable send object. Keep the guard alive through response capture or explicit loss classification and through completion of the send task. A private helper can have this concrete shape, with result/error types local to operation-controller:

```rust
async fn submit_and_capture_once(
    admission: &OsDeploySendAdmission,
    scheduler: &Scheduler,
    fake: &NativeFakePve,
    grant: &LeaseGrant,
    workflow_sha256: &str,
    permit: OsDeployDispatchPermit,
    capture: OsDeployResponseCapture,
) -> Result<OsDeploySendObservation, OsDeployControllerError>;
```

Its result reports observation/capture status only and never contains the permit or a reusable request. The private helper retains the capture/receipt while retrying a bounded transient persistence failure; if it cannot persist before its bound, it reports that capture is incomplete and leaves original dispatch recovery in charge. Never report transport acceptance as durably recorded until the receipt transaction succeeds. An outer run_once timeout must not detach a send-capable task from admission accounting: keep it tracked and join/drain it, or explicitly cancel/drop its owned future and preserve uncertainty. Closing the gate rejects new admitted tasks and waits for all existing guards/tasks; timeout means local quiescence was not proved. Controlled local handoff changes authority only after that drain. An independently changed authority or external writer still falls outside this cooperative guarantee.

The public fake port continues accepting borrowed request values for existing pure/fake tests, and a caller holding NativeFakePve can call it directly. This design therefore enforces consumption in the approved controller/store path, not a global language-level ban on direct fake calls. The fake itself currently tracks attempted operation IDs in its own in-memory world; that is a test behavior, not persistent cross-process exactly-once or real-PVE generation fencing. Reconstructed/public request/dispatch values are data and cannot obtain a new store permit. Keeping them inspectable does not authorize the controller to bypass committed dispatch, admission, scope, or no-resend rules. Stronger global restrictions would require a separately reviewed transport-capability redesign and are outside this phase.

Required later source scope is narrow and explicit: new OSDeploy opaque permit/capture types, post-commit tuple construction, consuming fake method and original-identity receipt validator in postgres-store; their exports; the OSDeploy-only submit/capture helper plus cooperative task admission/drain in operation-controller; and eventual wiring of that lifecycle into the existing service. Preserve NativeDispatchPermit, NativeController and ProvisioningFakePort signatures and dependency direction. Future validation should prove move-after-send/Clone/Deserialize/request-getter compilation failures, cancelled/unpolled future no-resend, one call per permit, stale-original receipt persistence, no permit returned on any error, and tracked task drain. This candidate resolves the ownership choice without authorizing those source changes or the service/external cutover.
