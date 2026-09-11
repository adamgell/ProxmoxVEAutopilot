# ConfigurePe adapter provenance binding

Baseline: `a7f69df7`. `FixtureProvisioningPort::shared_history_provenance` previously
read only its legacy Clone checkpoint field. A port configured with the existing
`LateConfigureContext` consequently returned no provenance despite holding an
operation-specific checkpoint client, generation and owner.

The trait now delegates to that context when present. The context derives the
sealed value from its stored operation, generation, owner and checkpoint channel;
it does not take caller receipt JSON, alternate identity fields or a success
assertion. Existing constructor exclusion rules prevent combining that context
with the legacy Clone checkpoint. This enables the accepted ConfigurePe
predecessor adapter to expose its bound channel identity, a prerequisite for the
connected StartPe harness.

The real subprocess prefix test now checks the configured port's provenance
against the expected checkpoint binding. Independently changing operation,
supervisor generation, owner or channel yields a different value. These checks
run in the actual ConfigurePe worker after its genuine predecessor receipt was
produced by the earlier fixture stages. No synthetic receipt or SQL fixture row
was introduced.

Validation:

```sh
RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test -p operation-controller --all-features --test postgres_fixture_clone fresh_controller_clone_then_disk_capacity_then_configure_pe_reaches_satisfied -- --exact --nocapture
```

Result: 1 parent test passed, 0 failed, 45 filtered, 11.60 seconds. All three
subprocess workers passed; the chain reached Satisfied through the owned
PostgreSQL and IPC fixtures. One initial test compilation error from ambiguous
OperationId/Uuid inference was corrected before the passing run.

This value proves configured channel/binding equality, not a new effect or
supervisor authorization. StartPe dispatch/readback context and the connected
stop outcome matrix remain unimplemented. No daemon lifetime, restart rule,
callback semantics, send gate or production configuration changed.
