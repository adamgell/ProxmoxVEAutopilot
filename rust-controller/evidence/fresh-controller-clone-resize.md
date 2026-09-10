# Fresh controller Clone and DiskCapacity proof

`fresh_controller_clone_then_disk_capacity_reaches_satisfied` runs the actual
`OsDeployController::run_osdeploy_once` twice against an isolated PostgreSQL
database, its scheduler, and a local IPC fixture daemon. It uses `Scenario::new`
only for registration and source fixture inventory; it never calls
`Scenario::ready` or submits through the native fake.

The Clone controller collects IPC source/preflight facts, creates its own
attempt/request, and commits dispatch. A separate store observes the exact
dispatch before the supervisor releases Clone. The daemon accepts the original
v1 request. The supervisor publishes observations tied to that receipt, and
the real evaluator reaches `Decided(Satisfied)`.

The DiskCapacity controller then reads that exact accepted Clone publication
through the late adapter. Its own generated GrowDisk request references the
original predecessor attempt and plan. The independent store observes durable
resize dispatch before the supervisor arms the exact v2
operation/stage/attempt/generation/owner/digest identity and releases it. The
daemon verifies the original v1 receipt and current world state, accepts resize,
and produces a `resize` UPID. A test-only PostgreSQL journal insert lock holds
outcome collection until the supervisor publishes observations for the accepted
resize. Releasing that lock lets the real evaluator reach `Decided(Satisfied)`.
The separate store reloads the unchanged request and exact receipt. The daemon
contains two attempts and two effects; native fake submissions remain empty.

Fresh binding required the fixture checkpoint client to wait, within its existing
deadline, for Idle to become the exact matching Armed identity before Enter.
Different generations, identities, or phases fail closed. This resolves the
supervisor race between observing committed dispatch and arming its new attempt.
Production/default controller paths and lease settings are unchanged.

Validation passes four `fresh_controller` cases in `postgres_fixture_clone`,
seven publication/adapter cases, two checkpoint cases, and the legacy bridge IPC
case. Strict offline/locked all-targets Clippy passes for both operation-controller
and pve-port with `fixture-ipc`; formatting and diff checks pass.

This proves the first two stages in a local synthetic fixture. ConfigurePe's
synchronous observations, full sixteen-stage execution, callbacks/service
compatibility, Linux qualification at this source revision, and operator/non-
production acceptance remain separate open requirements. No real Proxmox or
production controller was changed.
