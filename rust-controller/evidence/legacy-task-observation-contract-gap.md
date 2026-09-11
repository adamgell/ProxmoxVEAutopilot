# Legacy callback and StartPe task observations: separate authorities

Assessment only. No new observation contract, callback handler, runtime mapping,
or compatibility claim is implemented by this note. All inspection was local.

## Verified source boundaries

- `crates/pve-port/src/fixture_support/durable_fixture_log.rs`:
  `StartObservationV1` explicitly represents atomic task success plus running VM
  power. Its restoration validation binds the task UPID to the original receipt,
  operation, request digest, stage binding, VM, generation and original times.
  It has no task-state discriminator for Running or Failed. Running **power**
  must not be mistaken for a running **task**.
- `crates/pve-port/src/fixture_support/start_restoration.rs`:
  `FixtureStartPeRestoration::observe` projects that validated record into
  `FixtureTaskState::Succeeded`, not a guest callback result.
- `crates/pve-port/src/fixture_support/provisioning_port.rs`:
  the LateStart branch in `task_status` requires the validated full publication,
  exact query node/UPID and original task time. Missing evidence stays unavailable.
- `crates/api-compat/src/callback_contract.rs`: `classify_result` and
  `classify_legacy_completion` are pure semantic classifiers. Their documentation
  explicitly requires authenticated durable exposure supplied by a caller; they
  do not establish authentication or activate stages. Legacy Running/Failed step
  state and retry advice do not identify a Proxmox UPID.
- `crates/postgres-store/src/scheduler/osdeploy/fixture_completion.rs`:
  `FixturePeCompletionReport` is bound to a materialized package definition and
  `boot-files-staged.v1` result booleans. That existing fixture completion path
  is not an observation of the earlier Proxmox StartPe task.
- `autopilot-proxmox/web/ts_engine_pg.py`: `complete_step` is the source of
  the legacy step-completion classifier, not a Proxmox task-status authority.

## Exact missing evidence and interface

A guest legacy callback cannot safely become StartPe Running/Failed merely by
adding enum variants. The absent input is an independently collected task-state
observation bound to the exact accepted StartPe UPID and original receipt.
It needs an authenticated collector provenance, original acceptance and sample
times, request/operation/attempt/owner/generation binding, and a durable replay
policy. No inspected legacy classifier or completion report supplies that input.

For fixture task-state support, introduce a separately versioned supervisor
observation and validated read endpoint that records actual task state without
requiring running VM power or the success-only full publication. Keep it separate
from guest step callbacks. Define whether repeated Running samples are append-only
observations and how a terminal Failed observation prevents later conflicting
results; do not overwrite the existing success record with caller-supplied claims.

Required tests before exposing the mapping: valid independently observed Running
and Failed with original timestamps; wrong node/UPID/receipt/operation/attempt/
owner/generation refusal; pre-acceptance or future sample refusal; missing sample
unavailable; identical replay stable; conflicting terminal result rejected;
daemon restart and worker-death restoration preserving the original record;
guest step failure and VM power Running unable to create task-state evidence.

This is an implementation gate, not a declaration that legacy callbacks or the
full evaluator are supported. Existing success mapping remains unchanged.
