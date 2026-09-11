# Frozen c3e38135 Linux build failure

The approved archive build used exact source
`c3e38135e1049f393a949373165cfb1f50e47786` and local OrbStack Linux AMD64.
The full output and frozen tree manifest are retained under
`linux-fixture-c3e38135-build/`.

Both release builds and all-feature test compilation passed. Default pve-port
runtime/doctests and operation-controller decision tests passed. The next
Dockerfile gate failed in `postgres_native`:

`support::local_postgres::process::tests::fault_supervision_progresses_during_synchronous_cleanup`

The test emitted `native_fake_child_reap_unconfirmed` and panicked at
`proof_support/process.rs:434` with `owned child must be absent, including zombies`.
That gate reported 33 passed, 1 failed, 9 ignored and 25 filtered out. Cargo
returned 101; the Docker build returned 1. See build.log lines 1349–1375.

No image export completed, no new source/blob verification or launcher pin was
made, and fixture mode was not invoked against this source. The prior qualified
image/source remains separate. This failure requires investigation of the actual
supervision/reaping boundary; no claim that it is merely a timing flake is made.
The build was not retried to erase the failure. No cleanup, engine restart or
production operation occurred.

## Isolation and next fix gate

Read-only source comparison shows `proof_support/process.rs` has identical Git
blob `455ac8b80c2ffef5ee3da078fbe4e77ada3be296` at the previously qualified
`93e04827` and failed `c3e38135`. The failing test exercises only the fixture
supervisor: a Python sleep child and a scripted `/bin/sleep` cleanup. It does
not execute controller runtime or access PostgreSQL/Proxmox. The assertion is a
`ps` check for the original child PID, including zombies.

A targeted owned Linux diagnostic using the already verified `93e04827` image
and identical helper passed (1 test, 0.67 seconds). Log:
`linux-fixture-c3e38135-build/isolated-reap-probe.log`. Retained container:
`reap-diagnostic-c3e38135-1`; network disabled, 4 GiB memory/swap cap. This does
not replace the failed c3e38135 gate or prove the failure cannot recur.

```sh
docker --host unix:///Users/Adam.Gell/.orbstack/run/docker.sock run --name reap-diagnostic-c3e38135-1 --platform linux/amd64 --network none --memory 4294967296 --memory-swap 4294967296 --workdir /workspace/rust-controller --entrypoint cargo sha256:349f1808dcc6b1582f8d633dca4849061c2b5ba4872406acf93947edd5d62317 test --offline --locked -p operation-controller --test postgres_native support::local_postgres::process::tests::fault_supervision_progresses_during_synchronous_cleanup -- --exact --nocapture --test-threads=1
```

For another run use a new retained container name. The full build command was
the frozen `git archive --format=tar c3e38135 rust-controller
autopilot-proxmox/playbooks/_test_long_sleep.yml` stream into the approved Docker
build with source argument `c3e38135e1049f393a949373165cfb1f50e47786`.

The observed defect is in the test/fixture process-reaping boundary. Root cause
is not yet fully isolated. `ManagedChild::drop` sends kill, polls `try_wait`, but
abandons confirmation once the absolute cleanup deadline is reached; late thread
scheduling can therefore consume its reserved 200 ms before reaping completes.
The test asserts PID absence before dropping/joining `FaultChild`. These are
candidate mechanisms, not a demonstrated timing diagnosis. The next fix gate
should record which owned PID remains and its process state, reproduce delayed
supervisor scheduling deterministically, and verify bounded timeout reporting
plus eventual owned-child reaping without hiding unresolved children or widening
the caller budget. Only then rerun the complete failed gate and frozen build.

## Synchronization review

At `40109ac653564a793e81bb1dec304fef3c9bc233`, the focused macOS test passed
(1 passed in 0.60 seconds) with `RUST_MIN_STACK=16777216`, offline/locked Cargo,
the exact failing test filter and serial test execution. This does not resolve
the Linux failure.

The absence assertion does run before `FaultChild::drop` joins its supervisor.
Moving that drop ahead of the assertion is not a semantics-preserving fix:
drop first sends cancellation, so the test could pass because owner cancellation
caused termination rather than because deadline supervision progressed while
synchronous cleanup blocked the runtime. Moreover `JoinHandle::join` alone does
not reap a child already abandoned by `ManagedChild::drop` after its deadline;
it joins only the Rust supervisor thread. The existing join has no independent
hard timeout, so adding a blocking join is not proof of bounded completion.

A completion-only diagnostic barrier may first require the supervisor to have
finished under the existing absolute deadline, join that already-finished
thread without sending cancellation, and retain the original PID-absence check.
It must fail if completion was late and must not turn a pending/zombie child into
success. This would distinguish supervisor scheduling from actual reap failure,
but it is not established as a fix. No source synchronization change was made;
the new per-PID/deadline diagnostics should be used for the next Linux failure
before selecting a behavior change.

## Current-head diagnostic attempt

After the reaping diagnostics were added, an exact-current-source targeted image
was built from `e9c0aa1f7ccf79ed67d58aec5ccc23ee2188995e` with the reaper source
blob `3b1ae9f6906cc602c17dd45b0a8bebe40c6b19ea`. The image digest was
`sha256:4c288588108ca19ff6873545c38b704c6208a6ac823b7c2f8855f990c55cafce`.
Only the `operation-controller/postgres_native` target was compiled, offline
with network disabled. The retained test container requested ten exact
iterations of the failing test, but Docker stopped returning output before any
iteration produced a PID or reap result; the retained `probe.log` is empty.
The associated Docker process/session is no longer present, so this attempt is
terminal but observationally inconclusive. It provides no new pass/fail claim,
does not replace the c3e38135 failure, and does not justify restarting Docker
or weakening the strict absence assertion.
