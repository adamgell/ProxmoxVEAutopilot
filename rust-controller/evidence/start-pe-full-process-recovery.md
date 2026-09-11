# Full StartPe publication process-death recovery

`full_start_pe_worker_death_preserves_exact_publication_and_ledger` reuses the
fresh Clone→DiskCapacity→ConfigurePe→StartPe fixture sequence, now with an owned
OS daemon process and separate read-only restoration worker processes.

After the supervisor has durably acknowledged the full sidecar, worker A reads
it through `FixtureProvisioningPort::validate_start_pe`, writes its observed
result, signals readiness, and parks. The test asserts it is still alive, kills
it through its owned child handle, and waits for unsuccessful exit. Ledger and
sidecar bytes remain identical. The supervisor daemon is then also forcibly
killed and reaped before stale sockets are removed and a replacement is started.
Original ledger and sidecar bytes are checked before recovery reads. Worker B
restores the identical typed publication in a fresh OS process and exits normally.
Duplicate full publication and mutation remain refused; attempt/effect counts
stay four. Existing malformed/partial/identity-substitution checks also run.

Child processes are owned by RAII handles that kill/reap on unwind. Startup and
worker readiness are bounded; daemon lifetime is capped at 30 seconds. Child
entrypoints return immediately without their explicit environment input and are
not ignored tests, so a broad `--ignored` run cannot launch them.

This proves process death after a durably published full bundle and after a
read-only worker consumes it. It does not claim a PostgreSQL receipt commit,
controller Satisfied transition, or kill at every filesystem write instruction.
The pre-receipt Unknown controller behavior is untouched.

Validation: focused process-death test passed; full publication suite passed
12 tests (10 substantive tests plus two inert child entrypoints); broad ignored
selection ran zero tests. Strict all-target fixture-ipc Clippy for pve-port and
operation-controller, workspace fmt, and diff checks passed.

Remaining gates are PostgreSQL caller/fenced progression integration and its
own crash boundaries, followed by later guest stages. No production/default
code or real qmstart/Proxmox action changed.
