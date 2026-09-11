# Fixture EnsureStopped store integration

The fixture credential-enabled scheduler can activate `PeEnsureStopped` after
the selected shutdown grace expiration. Its predecessor is the guarded
`Unknown` decision carrying `ShutdownGraceDeadlineExpired`, the original due
time, and the authenticated successful PeComplete anchor. That decision is
permission to inspect and, where necessary, stop the fixture VM; it is not
evidence that the VM has already stopped.

Activation allocates a fresh `mutation_pe_ensure_stopped` scope and worker lease.
Reload reconstructs Clone, DiskCapacity, ConfigurePe, and StartPe physical
history, validates the authenticated callback/grace chain, and retains StartPe
as the physical baseline for the typed stop request. The existing durable
dispatch, task receipt, preflight/outcome evaluator, and resume paths perform
the stop. Credential-enabled due discovery includes the stop stage; ordinary
schedulers retain their existing stage set. The operation controller admits
this action only with explicit fixture credential delivery configuration.

The PostgreSQL proof extends
`fixture_pecomplete_authenticated_report_and_atomic_grace`. It exercises an
actual fake-PVE physical prefix and authenticated completion before the real
grace timer expires. It covers a rolled-back stop activation, absence of a
partial attempt/scope, a fresh deadline, reconstruction of a typed Stop
request, duplicate-dispatch refusal, task receipt capture, selected outcome,
and reload. If the evaluator parks, it also checks opt-in due discovery and
resumes the same attempt through satisfaction.

Local macOS verification on the source accompanying this document passed:

- The focused PostgreSQL test above: 1 passed, 141.89 seconds, with
  `RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1` and `fixture-ipc` enabled.
- `cargo clippy -p postgres-store -p operation-controller --all-features
  --all-targets -- -D warnings`.
- Default-feature checks for `postgres-store` and `operation-controller`,
  workspace all-feature compilation, formatting, and `git diff --check`.

This is a store/native-fake integration. The durable subprocess fixture ledger
still supports only Clone, DiskCapacity, ConfigurePe, and StartPe. Stop through
that external fixture, independent process death/restart, controller runtime
acceptance, and current-source Linux qualification remain separate required
proofs. Generic/legacy callback compatibility, later OSDeploy stages, service
execution, and production acceptance remain open. Production and real Proxmox
were not mutated by this work.
