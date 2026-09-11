# CI fixture contention experiment

Source inspected: `77b5f850`. Completed GitHub Actions run
[34584407082](https://github.com/adamgell/ProxmoxVEAutopilot/actions/runs/34584407082)
tested `eda7e610` and supplies the observations below.

The Linux prefix binary reported three child `Storage` failures after roughly
2.2 seconds. Each parent failed its two-second Resize dispatch gate at
`postgres_fixture_clone.rs:715`. The successful-prefix case reached the
ConfigurePe result check at line 1286 but returned `Decided(Unknown)` instead
of `Decided(Satisfied)`. The generic `Storage` error discards the underlying
storage cause, and the output does not include the evidence needed to explain
that Unknown decision. Neither outcome proves a specific controller defect.

The same workflow's macOS output contains widespread
`local_database_unavailable: local_process_timeout` fixture creation failures.
The workflow starts independent test cases at default parallelism, each with
its own PostgreSQL fixture; the macOS VM has two CPUs and 4 GiB memory.
The prefix subprocess also has a two-second pool acquisition limit and bounded
IPC deadlines. These facts support fixture contention as a testable hypothesis,
without establishing it as the sole cause of every failure.

Earlier retained Linux recovery evidence at
`requalification-c9e1ca5a-recovery-3/README.md` passed two selected recovery tests
serially in 84.01 seconds on source `f92a6287`. That is supporting evidence for
the scheduling experiment, not current-source qualification. No new local or
owned Linux runtime was launched for this diagnosis.

The CI experiment sets `RUST_TEST_THREADS=1` at workflow scope. This serializes
independent test cases and their PostgreSQL fixtures. Explicit concurrent
claimers, async task races, subprocess workers, and lease-expiry tests inside
each test remain unchanged; no test filter, assertion, controller timeout,
resource guard, or production configuration is changed.

Both job ceilings are temporarily 90 minutes. The failing parallel Linux test
phase alone occupied about 12 minutes, and the subsequent release image build
has previously taken about 18 minutes. Successful serial suite duration is
unmeasured, so the former 35/45-minute ceilings cannot be asserted sufficient.
The larger ceiling bounds this measurement experiment rather than claiming
that any particular duration is required.

Acceptance requires the complete current-source CI run to finish, all prefix
and concurrency tests to pass, and actual step durations to be inspected.
If serial execution still produces `Storage` or Unknown, the next experiment
must capture a sanitized storage error classification or the durable decision
and evidence for that exact operation. Do not widen controller deadlines or
weaken outcome assertions based only on these generic failures. Production
readiness and exact-source Linux runtime qualification remain open.
