# Fixture StartPe task-status mapping

Scope: fixture-only generic reader compatibility, not Setup integration, full
evaluator coverage, Ansible parity, or production acceptance. No physical dispatch.

`crates/pve-port/src/fixture_support/provisioning_port.rs` now maps task status
for the bound LateStart context only after `validate_bound_start_pe` validates
the original receipt/request and full publication. The query node must match the
adapter binding and the UPID must equal the validated durable task UPID. The
returned success uses the original `task_observed_unix_ms`, checked for datetime
representability, never the current time. Missing publication remains unavailable;
no running or failed task state is invented from this success-only observation.

`crates/pve-port/tests/fixture_post_dispatch.rs` exercises the actual subprocess
fixture journal: absent publication refuses the real accepted task; validated
publication returns the exact UPID, success and original timestamp; wrong node
and different valid UPID are refused. Existing full publication time, identity,
receipt, worker-death and daemon-restart validators remain in force.

Verification:

- RED: focused `full_start_pe_worker_death_preserves_exact_publication_and_ledger`
  failed with `TransportUnavailable` at the new valid task-status assertion.
- GREEN: same focused test passed (1 passed, 11 filtered; 1.42 seconds).
- `RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test -p pve-port --features
  fixture-ipc --test fixture_post_dispatch`: 12 passed, 0 failed; 3.09 seconds
  (includes two child-process entrypoints).
- `cargo fmt --all --check` and `git diff --check`: passed.

Untimed infrastructure reads remain closed. This does not establish controller
decision readiness or broaden any production authorization.
