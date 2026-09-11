# StartPe admission boundary audit

This follows `885f39a`. It is a local fixture boundary investigation and regression
proof, not StartPe execution or production acceptance.

## Current facts

- `stage_effect::submit` validates the exact accepted ConfigurePe predecessor and
  then rejects StartPe before consuming authorization or recording an attempt.
- `StageCheckpointRequest::AuthorizeRelease` authorizes an `after: VmState` with
  disk capacity and PE configuration only. It carries no typed power transition
  or exact durable power-observation predecessor.
- `record_effect_receipt` commits an effect and receipt in one ledger frame.
  `record_power_observation` commits a separate frame and requires that accepted
  effect to exist already. Its running case is therefore a replay prerequisite,
  not an atomic StartPe acceptance operation.
- ConfigurePe supervisor publication now persists explicit stopped evidence.
  Restart preserves that evidence but invalidates live publication clocks and
  checkpoint authorization. A historical stopped observation is not current
  authorization or a running task result.
- The fixture adapter has late Clone, resize and ConfigurePe paths, but no late
  StartPe binding/recovery path.

## Why execution remains closed

Simply returning a generated `qmstart` receipt from the existing StartPe branch
would acknowledge acceptance before a durable, stage-bound power transition is
committed. Writing the effect and then the observation introduces a crash window
with a task receipt but no matching power transition. Writing the observation
first is rejected by the existing ledger because there is no accepted effect.
Neither order is a valid substitute for a new atomic contract. Task acceptance
must also remain distinct from a subsequent observed running postcondition.

The next implementation must define a versioned atomic StartPe acceptance record
that binds the exact stopped predecessor record/effect/receipt, request digest,
operation, attempt, owner, generation and task receipt. It must consume exact live
authorization before a synthetic effect can happen, validate current stopped
state without promoting stale replay evidence, and recover ambiguous pre-receipt
windows without redispatch. A separately collected stage-bound `qmstart` task and
unlocked running observation must still be required for controller satisfaction.
The running observation cannot be fabricated from the accepted task alone.

After this ledger/IPC contract, add the explicit adapter binding and restore path,
then prove a fresh PostgreSQL-backed four-stage controller chain and process-death
windows. Callbacks remain out of scope until this prefix is genuinely proven.

## Regression proof added

The existing real-daemon three-stage publication test now attempts StartPe after
the successful ConfigurePe stopped publication, both before and after daemon
restart. Each attempt is supplied the exact accepted ConfigurePe predecessor and
a freshly armed/released checkpoint in the current generation. Two submissions
are refused; polling confirms authorization was not consumed; the ledger stays
byte-identical with exactly three attempts/effects and one stopped power record.
No synthetic running effect or `qmstart` acceptance is manufactured by this test.

This slice changes only tests and this evidence note. Production, default paths,
real Proxmox and `192.168.2.4` remain untouched.

Fresh validation passed:

- `cargo test -p pve-port --features fixture-ipc --test fixture_post_dispatch --test fixture_stage_consumers --test fixture_stage`: 15 passed, zero failed.
- `cargo clippy -p pve-port --features fixture-ipc --all-targets -- -D warnings`.
- `cargo fmt --all -- --check` and `git diff --check`.

No PostgreSQL controller or process-death suite was rerun for this test-only slice.
