# Explicit fixture current-power source

The opt-in Unix fixture supervisor accepts `install_test_power_source` with an
explicit synthetic `Running` or `Stopped` sample. This input is independent of
the fixture VM configuration, accepted mutation receipt, and StartPe task result.
Only the supervisor socket can install or consume it. A worker cannot assert
power through this command.

Each sample binds the complete StartPe identity (operation, attempt, owner,
generation and request digest), VM ID, daemon generation, original observation
timestamp and an asserted lease expiry. The source rejects locked/unknown power,
future observations, samples older than five seconds, expired samples and daemon
generation changes. The expiry is supplied by the fixture supervisor; it does
not itself establish a PostgreSQL current-lease decision.

The sample is a bounded immutable file per StartPe identity in the private fixture
directory. Installation uses create-new, file sync and directory sync. Identical
replay syncs again and preserves the original timestamp. Conflicting replacement,
torn JSON, oversized/non-file input and stale generations are refused. A daemon
restart gets a new generation, so an old source cannot be promoted to a new read.
This intentionally supports one sample per identity; it is not a general live
polling source and must not be used to claim production power evidence.

`consume_stop_current_power` now consumes this source when installed. Running
samples feed the existing durable `refresh_start_power` path using the exact
accepted StartPe receipt and original observation timestamp. That path checks
the VM and predecessor and cannot create a mutation attempt, effect or receipt.
Stopped samples return source evidence but do not fabricate a successful stop or
publish a stopped observation. Missing source retains the prior explicit
`current_power_source_unavailable` response.

Local verification:

- `cargo test -p pve-port --features fixture-ipc power --lib`: eight focused tests,
  including accepted source publication and restart refusal.
- `cargo clippy -p pve-port --all-features --all-targets -- -D warnings`.
- Formatting and whitespace validation.

The proof covers immutable source replay, expiry, owner mismatch, Running and
Stopped source reads, conflict/torn-file refusal, worker installation refusal,
Running publication without extra attempts/effects, durable ledger recovery and
new-daemon refusal. Current EnsureStopped lease authority, actual stop dispatch,
successful stopped-state publication and independent process-death acceptance
remain separate integration work. Generic/legacy/full-port and production gates
are unchanged; this source makes no network calls.
