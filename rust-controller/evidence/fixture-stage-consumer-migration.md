# Stage consumer migration

The additive stage consumer protocol uses `FixtureStageIdentity`: operation,
stage, attempt, daemon generation, owner and exact request digest. Stage identity
validation also checks the decoded request's operation, attempt and action.
Legacy Clone messages and barriers retain their existing wire contract; a legacy
checkpoint binding cannot deserialize as a stage identity.

`stage_checkpoint` provides supervisor status/arm/authorize-release and worker
enter/poll. It durably records the exact identity and request before release and
requires independent committed bytes to equal the proposed bytes. Restart
creates a new generation and discards prior authority. Stage authorization does
not share the legacy Clone barrier or its consuming submission command.

`accepted_stage_effect` uses the version-two ledger's exact stage binding. This
allows recovery reads without falling back to operation-only legacy lookup.

## Remaining gate

This is a partial consumer migration. `stage_late` validates the stage identity
and released authorization but still refuses mutation without consuming it.
No stage attempts, effects or publications are created. The current publication
and controller adapters understand only version-one Clone requests. The next
implementation must compose stage receipt/effect generation, durable consumption,
publication validation/readback and the controller adapter before enabling stage
submission. Resize and ConfigurePe synthetic effects have not been implemented.

## Verification

- `fixture_stage_consumers`: all three supported stages; wrong operation, stage,
  attempt, generation, owner or digest cannot enter the barrier; workers cannot
  authorize; differing committed bytes and repeated authorization fail; stage
  mutation refuses with zero ledger attempts/effects; restart invalidates release.
- `fixture_stage`: first-three-stage request/receipt contracts and legacy-binding
  stage transport rejection, including daemon restart.
- Focused stage targets passed 3 tests; checkpoint target passed 2 tests. Strict
  feature Clippy and formatting passed.
- The existing daemon target passed serially: 18 passed, 1 subprocess entry point
  ignored. Its parallel run had 17 passed, 1 failed, 1 ignored: the existing
  `late_authorization_is_supervisor_owned_atomic_and_invalidated_on_restart`
  test's 20 ms ArmLate deadline expired before Enter (`fixture_daemon.rs:392`).
  This timing failure remains a qualification concern; the serial pass does not
  constitute a successful parallel run.

These checks use local Unix sockets and synthetic data only. They do not prove
full controller progression, stage effect consumption or production readiness.
