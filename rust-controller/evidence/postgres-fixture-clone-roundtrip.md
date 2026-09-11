# PostgreSQL dispatch and IPC Clone receipt proof

`crates/operation-controller/tests/postgres_fixture_clone.rs` composes the existing isolated PostgreSQL scenario with `FixtureProvisioningPort` and its owned checkpoint client.

The test verifies that an independent database pool sees the committed dispatch without a receipt, the entered checkpoint has no accepted daemon effect, and supervisor release permits consumption of the dispatch permit. It checks the daemon-generated typed receipt, synthetic target effect, exact original receipt reloaded from PostgreSQL, and duplicate submission rejection.

Validation on macOS: the feature integration target passed all 35 tests (one new composition proof plus 34 shared local PostgreSQL lifecycle checks). The new test passed again after adding exact receipt equality, with 34 shared checks filtered out. Targeted strict Clippy and workspace formatting passed.

This is a dispatch/receipt composition proof. Preflight and request admission still use the existing native scenario, and the test calls the checkpoint and dispatch permit explicitly. It does not execute `OsDeployController::run_osdeploy_once` against seeded IPC reads, reconstruct the controller, restart PostgreSQL or demonstrate independent worker-process takeover. Those gates remain open. The next controller test needs complete timestamp-bound seed observations and exact request authorization compatible with the controller's freshly collected preflight evidence.

Only the isolated local PostgreSQL fixture and synthetic Unix-socket daemon are used. No production controller or real Proxmox mutation is involved.
