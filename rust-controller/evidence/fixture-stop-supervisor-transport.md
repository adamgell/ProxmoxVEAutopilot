# Fixture stop supervisor transport

This slice connects the existing database authority and fixture supervisor
protocol through `OsDeployController::admit_fixture_stop`. It is an explicit
fixture API, not a service execution path or production stop capability.

The controller prepares stop authority from PostgreSQL, then invokes the supplied
independent sample provider with that immutable authority. The provider returns
an `AdmitStop` request and an already installed versioned fixture power sample.
The controller checks the operation, attempt, predecessor, sample scope and exact
authority. A bounded typed supervisor client consumes the sample without
installing it or refreshing its observation clock. The controller revalidates
database ownership/cancellation without restamping authority and separately
checks the request against the committed PostgreSQL dispatch before requesting
supervisor admission. Success requires the exact entered barrier identity and
generation; the barrier remains entered.

The transport binds its reply to the exact sample and expected source, rejects
oversize replies, and has an explicit deadline. The returned publication is an
acknowledgment payload, not independently trusted evidence: durable admission
still validates the supervisor's journal, accepted physical predecessor and
scoped power publication.

Cancellation revalidation is a point-in-time database check. Admission does not
establish atomicity between PostgreSQL and the supervisor. A subsequent send
must have its own current ownership/cancellation gate. This API cannot release a
barrier, dispatch stop, publish Stopped, or claim accepted-stop recovery.

## Verification boundary

The Unix transport test exercises exact request/response binding and refusal for
a missing socket, mismatched sample, false acknowledgment, wrong source,
malformed publication, excessive reply length and timeout. Its local mock server
is a transport test, not a physical-history integration proof.

The existing PostgreSQL native fixture completion/grace test now also validates
the exact committed stop request and rejects a stale scheduler. It does not
share physical StartPe history with the durable fixture daemon. A positive test
covering PostgreSQL authority, independently sampled power and supervisor
admission against one physical history remains required before claiming the
bridge is integration-qualified.

Local macOS checks for this slice passed:

- `cargo test -p pve-port --all-features --lib stop_power_transport_binds_reply`:
  one test passed in 0.16 seconds, including the explicit timeout case.
- With `RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1`, the PostgreSQL
  `fixture_pecomplete_authenticated_report_and_atomic_grace` test passed in
  151.54 seconds, including committed-request and stale-scheduler checks.
- Default `operation-controller` compilation and all-feature compilation passed.
- All-target/all-feature Clippy for `pve-port`, `postgres-store` and
  `operation-controller` passed with warnings denied; formatting and diff checks
  passed.

These results are local source evidence. Linux qualification must be rerun on
the resulting source commit before claiming this slice is Linux-qualified.
