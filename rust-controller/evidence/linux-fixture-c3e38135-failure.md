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
