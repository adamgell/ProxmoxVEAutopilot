# Fresh controller IPC entrypoint prerequisites

The `fresh_controller_ipc_collection_without_seed_cannot_dispatch` regression
executes `OsDeployController::run_osdeploy_once` with isolated PostgreSQL and a
composed `FixtureProvisioningPort` / `FixtureCheckpointClient`. It does not call
`Scenario::ready`; Scenario supplies only registration and the owned database.
An unseeded IPC world yields `Decided(Unknown)`, no durable dispatch or receipt,
no accepted effect, and no checkpoint entry. The native fake records no sends.
This proves failure closure of actual controller collection, not successful
Clone admission or recovery.

The positive entrypoint proof remains open because current protocol construction
has a circular prerequisite:

1. `FixtureProvisioningIdentity` requires `request_sha256` before provisioning
   reads; the adapter requires that same digest for mutation submission.
2. The exact request is constructed after controller collection, locked
   evaluation and runtime attempt/binding selection. Its before observations
   and fence cannot truthfully be guessed before that path runs.
3. The daemon loads `FixtureCloneSeed` once at startup, pinning the whole request.
   Writing a new seed file at the dispatch checkpoint cannot update that value.

A complete seam must separate stable read identity (fixture, operation, node,
source and target) from exact mutation authorization. An owned supervisor must
read the committed dispatch from PostgreSQL, validate its expected operation and
generation, authorize that exact request while the worker is at
`DispatchCommitted`, and only then release the checkpoint. The worker must not
gain seed-update authority. Missing, mismatched, stale or duplicate authorization
must fail closed. This needs a versioned supervisor command and tests; removing
the digest check alone would discard the existing authorization boundary.

After dispatch, current `FixtureProvisioningPort::task_status` explicitly returns
`TransportUnavailable`, and its startup provisioning reads do not become a new
post-Clone target configuration automatically. Successful postcondition proof
therefore also needs daemon-owned task and target observation projection. A
receipt proves accepted submission but does not establish a completed Clone.
DiskCapacity, ConfigurePe and the other service stages remain separate gates.

Validation command:

```text
cargo test --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone fresh_controller_ipc_collection_without_seed_cannot_dispatch -- --exact --nocapture
cargo clippy --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone -- -D warnings
```

All infrastructure in this proof is locally owned synthetic fixture state.
