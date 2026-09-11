# StartPe full publication to PostgreSQL progression

## Read-only fixture diagnostic ingress (2026-09-10)

The default-disabled `postgres-store/fixture-ipc` feature now exposes
`PgStore::probe_osdeploy_start_pe_fixture`. It restores registration in a
repeatable-read, read-only transaction, checks StartPe stage/revision, and, when
an opaque adapter validation outcome is supplied, binds its inventory identity
and exact request to the registered operation, run, workflow, and PVE plan.
Missing evidence also grants nothing. Every valid diagnostic probe returns
`CapabilityUnavailable`; wrong stage/request/identity is validation failure and
stale revision is fence loss. No attempt, dispatch, evidence, decision, session,
or lifecycle authority is created. This is not durable StartPe acceptance.

The existing closed-stage PostgreSQL test now includes missing-evidence refusal,
stale-revision and wrong-stage probes plus a complete database snapshot comparison.
It compiles, but its fresh runtime invocation failed before assertions at owned
PostgreSQL setup (`local_database_unavailable: local_process_timeout`,
`proof_support/mod.rs:338`). The harness reported cleanup unconfirmed for
`native-proof-01a08e26-98a0-7c61-be2f-2ba950b0dba5`; removal is not claimed.
Feature-enabled all-target `cargo check`, strict all-target Clippy, formatting,
and diff checks passed. Runtime DB-unchanged proof remains pending, as does a
valid full-publication outcome composed with a PostgreSQL harness and negative
outcome/request rebinding tests. No diagnostic fixture attempt is presented as
an admitted PostgreSQL attempt. Action/scheduler/lifecycle/session gates remain
closed and unchanged.

Read-only audit at source checkpoint `0a09b3e1`. Full fixture publication is
available, but no four-stage PostgreSQL controller proof is established here.
No production, schema, transport or controller admission changes were made.

## Current independent boundaries

1. `pve-port/src/fixture_support/start_full.rs` validates the full task/running,
   config, identity, media and infrastructure publication against durable accepted
   StartPe. This provides observations, not scheduler authority or satisfaction.
2. `operation-controller/src/osdeploy.rs:173` admits only Clone, EnsureCapacity
   and ConfigurePe. A full publication cannot cross this gate on its own.
3. `postgres-store/src/scheduler/osdeploy/transaction.rs:59` independently admits
   only those three stages. `lifecycle.rs:138` independently maps only their
   predecessors when activating a new attempt.
4. Existing `record_osdeploy_pve_evidence` (`pve.rs:104`) persists evidence;
   `decide_osdeploy_pve` (`decision.rs:11`) reloads indexed context/evidence under
   current grant/revision/authority checks, uses server evaluation time and scope,
   then selects a journaled outcome. A publication must pass through both steps.
5. The pure evaluator already distinguishes StartPe task/power outcome
   (`pve-port/src/provisioning/evaluation/outcome.rs:91,128`). This does not remove
   admission or callback-session obligations.

## Required implementation boundary

Keep collection outside the database transaction, with bounded reads of the exact
accepted request/receipt and preserved original observation timestamps. Normalize
the full bundle through the existing provisioning evidence constructor. Do not
write an operation state or decision directly from `start_pe_full`, its presence,
its sidecar checksum, a successful qmstart receipt, or an adapter constructor.

Persist that normalized evidence against the original operation, attempt and
expected revision through the existing evidence path. In the decision transaction,
lock authority/run/operations/attempts/leases in existing order; reload the original
dispatch, first receipt, exact ConfigurePe selected predecessor and evidence;
check active grant, cancellation, mutation scope and current revision. Evaluate
task plus Running power plus required config/identity/media/infrastructure facts
at database time. Only the evaluator's admissible result may drive the existing
atomic journal/outbox/projection/attempt update. Old timestamps after restart must
remain old. Missing or stale facts must retain normal wait/unknown behavior.

Before any StartPe send, the existing selected specification additionally requires
real server-session preparation and atomic arming. See
`docs/superpowers/specs/2026-09-05-rust-osdeploy-durability/next-durable-transaction-boundaries.md:87–89`:
StartPe dispatch anchors PE registration and arms the session; a placeholder cannot
satisfy this. Thus adding StartPe to the three admission matches alone is incomplete.
Session arming, registration deadline anchor and committed consuming dispatch
must have one coherent transaction/replay rule before general progression opens.
Full fixture publication does not supply session role/label/credential witnesses.

## Bounded next work and required tests

The safe immediate slice is a typed full-bundle-to-evidence adapter and isolated
contract tests with an existing explicit StartPe context, without opening admission.
Verify the existing evaluator gives StartPeSatisfied only with exact accepted
request/receipt, successful task, Running target, required configuration/identity,
complete inventory/media and fresh infrastructure. Mutate each independently:
running task, failed task, stopped target, stale timestamps, changed digest/owner/
attempt, missing media, partial inventory and mismatched predecessor. Verify no
decision/attempt/session is created merely by restoring the bundle.

The subsequent PostgreSQL vertical slice must create a real session/arming proof,
activate StartPe only after selected ConfigurePe success, commit the dispatch,
consume one permit, persist its first receipt, record collected evidence and
reach a selected Satisfied decision. Crash tests must cover session-arm/dispatch
atomicity, accepted-response loss, evidence-write/decision boundary and post-decision
replay, with no duplicate send or regenerated registration deadline. Do not use
pre-admitted Ready state or manually satisfied predecessors as a fresh-run proof.

## Source hashes

Paths below are relative to `rust-controller`.

| File | SHA-256 |
| --- | --- |
| `crates/operation-controller/src/osdeploy.rs` | `d309764821aa419f6d6bee4b15d55ca1664e2ce03f38f96a5aeb92b098f42ed4` |
| `crates/postgres-store/src/scheduler/osdeploy/transaction.rs` | `6d43315ce245ec4debb569f53f6a7804a7a9f8ce9a8d6e217d6d944b3a62fbed` |
| `crates/postgres-store/src/scheduler/osdeploy/lifecycle.rs` | `aa459b10e8b5c0aba1ccf3b43949e7651471a14c72a37f071b8637f83c5288e7` |
| `crates/postgres-store/src/scheduler/osdeploy/pve.rs` | `ff39e4a5ae3b1ec4ff31bb0cf62c8b24232da05cf6c227c61380af6ac52ffdba` |
| `crates/postgres-store/src/scheduler/osdeploy/decision.rs` | `5b529276a674635495499d2884c8723c68d422c81e986f923d17d397afabd002` |

Validation for this audit: source inspection, hashes and whitespace/diff check.
No runtime test, deployment or production readiness claim is made.

## Executable PostgreSQL ingress gate

The existing `task6_growth_history_survives_old_deadline_and_all_later_stages_stay_closed`
test now also attempts StartPe context loading and evidence insertion after genuine
Clone/resize/ConfigurePe satisfaction. Both return CapabilityUnavailable and the
database snapshot stays unchanged. StartPe retains Pending with no attempt,
dispatch or receipt; existing claim/resume rejection checks still pass.

The supplied observation is freshly collected typed ConfigurePe evidence. This is
an adversarial ingress-gate test, not a claimed full StartPe bundle conversion:
the store rejects the closed stage before validating evidence contents. It proves
that caller possession of typed evidence cannot bypass admission. Full StartPe
normalization and positive transactional consumption remain separate pending
gates. The focused PostgreSQL test passed (11.09 seconds); targeted strict Clippy,
formatting and diff checks passed. Only isolated test PostgreSQL was mutated.
