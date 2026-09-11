# Prefix worker journal diagnostics

The fixture worker now emits its controller outcome and the last twelve journal
revisions, event kinds, and execution states for the exact operation when the
outcome is not Satisfied. Child stderr is inherited by the test runner, so these
lines remain in CI output even when the parent dispatch gate subsequently fails.
The read has a 250 ms bound and occurs only after the controller call returns.
Existing controller, dispatch-gate, and child-lifetime limits remain unchanged.

The diagnostic intentionally excludes journal payloads, database error text,
connection strings, and credentials. Storage read failures are classified into
fixed categories. Those categories describe the diagnostic read only: they do
not recover the original SQLx cause already erased by the controller's Storage
error. Journal revisions establish the durable transition boundary, but do not
replace a complete decision/evidence reconstruction.

Validation: formatting and diff checks passed. The prefix integration target
compiles with `cargo check --offline --locked -p operation-controller --features
fixture-ipc,postgres-store/fixture-ipc --test postgres_fixture_clone`. No runtime
fixture was started for this diagnostic change. Compiling with only the
operation-controller fixture feature exposes an existing shared-test-support
dependency on the postgres-store fixture feature; feature ownership was not
changed. The shared support UUID reference was qualified through SQLx so it can
compile in both crates that include it.
