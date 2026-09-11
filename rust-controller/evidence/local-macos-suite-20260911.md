# Local macOS verification note — 2026-09-11

The full `cargo test --workspace --all-features --no-fail-fast` run was not a
valid single-result qualification run: concurrent subprocess-heavy targets
exhausted local stack/process/database capacity. It produced stack-overflow
abortions and two local-process timeouts while other tests were running in
parallel.

The affected controller targets were rerun serially with one test thread:

- `cargo test -p pve-port --features fixture-ipc --test fixture_post_dispatch -- --test-threads=1` — **12 passed, 0 failed** (2.84s).
- `cargo test -p operation-controller --all-features --test postgres_native -- --test-threads=1` — **60 passed, 0 failed** (91.74s).

The serial reruns establish that the observed failures were resource-contention
artifacts of the broad parallel invocation, not a reproducible failure of the
latest controller code. Other workspace packages and doctests reported in the
same run passed; the broad invocation itself remains recorded as non-qualifying.

This is local macOS evidence only. It does not establish Linux runtime,
physical-stop, callback-compatibility, or production-cutover readiness.
