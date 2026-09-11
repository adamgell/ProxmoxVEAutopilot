# DiskCapacity fixture restart recovery

The `durable_clone_then_resize_requires_exact_accepted_predecessor` integration test now restarts the fixture daemon after DiskCapacity acceptance, in addition to its existing restart after Clone. Fresh socket connections retrieve both original durable effects and compare their receipt bytes with the original responses. The new daemon generation differs from both dispatch generations.

Recovery verifies two attempts and two effects throughout. Replaying either original submission is refused. Changing operation, stage, attempt, generation, owner, or digest yields no accepted effect; changing the submitted effective capacity from 120 GiB to 121 GiB is refused. Resize receipt decoding rejects a missing receipt, truncated bytes, and a rebound request digest.

Validation: `cargo test --offline --locked -p pve-port --features fixture-ipc --test fixture_stage_consumers` passed both tests. Targeted strict Clippy passed, and `cargo fmt --all` completed.

Scope: daemon reconstruction and fresh client connections within the integration-test process. This does not establish independent operating-system worker takeover, corruption handling of the ledger file, controller DiskCapacity progression, or production readiness. Missing and partial receipt checks exercise the consumer decoder; they do not alter a committed ledger.
