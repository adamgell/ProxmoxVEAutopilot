# Fixture PeEnsureStopped IPC contract

This slice adds the typed IPC contract needed to carry a future stop dispatch.
It does not enable daemon stop execution or establish a stopped VM.

`FixtureStageRequest` now represents `EnsureStopped` and its distinct
`PeEnsureStopped` ledger identity. Receipt validation accepts only a nonzero
sequence with a `qmstop` task for the expected target VM, node, and `fake@pve`
user. It rejects synchronous acceptance, start tasks, source VM tasks, foreign
fixtures, altered request digests, and receipts from another stage.

`validate_ensure_stopped_physical_predecessor` checks the StartPe predecessor
operation/attempt and plan, common clone ownership, fixture, expectations, and
the StartPe task receipt envelope. This is structural validation. A runtime
consumer must additionally establish durable receipt acceptance, completed
physical history, the scheduler's guarded shutdown-grace predecessor, and the
fresh current lease. Evidence fences in historical bindings are not silently
converted into authority for a current lease.

Stop dispatch stays refused. The disk-only supervisor release explicitly rejects
EnsureStopped, the StartPe authorization requires its existing ConfigurePe
predecessor, and the generic stage-effect path has no Stop admission branch.
The controller fixture adapter still rejects an unconfigured stop checkpoint.
The message contract cannot synthesize stop task success or stopped power.

## Verification

On macOS, the changed-source `fixture_stage` target passed all 6 tests in 2.05s.
Its stop-specific contract test includes modified operation and attempt IDs,
receipt envelope corruption, wrong task type/node/VM/user, and identity mismatch.

The stop daemon test arms and enters a current-generation stop barrier, proves
disk-only release and submission refusal, and observes zero attempts/effects.
It repeats after daemon shutdown and restart. This proves persisted refusal; it
is not an independent worker-death or successful stop recovery proof.

The `fixture_stage_consumers` target passed all 3 tests with
`RUST_TEST_THREADS=1` in 2.94s. An earlier parallel run passed the new stop test
but hit `ConnectionRefused` in the pre-existing clone/resize/configure test at
socket startup. The serial rerun matches the configured fixture test policy;
the parallel startup race remains a separate test-harness issue.

Checks passed:

- `cargo test -p pve-port --features fixture-ipc --test fixture_stage`
- `RUST_TEST_THREADS=1 cargo test -p pve-port --features fixture-ipc --test fixture_stage_consumers`
- `cargo clippy -p pve-port --all-features --all-targets -- -D warnings`
- `cargo check -p pve-port`
- `cargo fmt --all -- --check` and `git diff --check`

## Connected runtime work remaining

A complete stop path requires an atomic durable stop admission frame binding the
accepted StartPe physical predecessor and guarded-grace authority; explicit
task-success and stopped-power publication; restoration with original clocks;
adapter observation and dispatch contexts; and controller tests through fresh
lease fencing, uncertain acceptance, replay, and independent worker death.
Current-source Linux qualification is also outstanding. Production and legacy
callback/import behavior are unchanged by this contract slice.
