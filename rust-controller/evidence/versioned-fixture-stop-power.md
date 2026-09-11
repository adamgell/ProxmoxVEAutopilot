# Versioned fixture stop power samples

The fixture supervisor accepts `install_versioned_test_power_source` and
`consume_versioned_stop_current_power`. Their v2 sample envelope contains the
exact StartPe identity, VM ID, daemon generation, stop request/lease identity,
prepared authority, sequence, and preceding frame SHA-256. Each sample is a new
immutable file. A stream has at most 64 samples and a daemon tracks at most 64
streams. Existing v1 samples retain their original semantics.

Installation validates post-authority observation time, strict time progression,
the complete preceding digest chain, invariant VM/authority, generation and
deadlines. Consumption requires the installed tail and a daemon-local monotonic
window. That window is bounded by freshness, remaining lease and original stage
deadline; a wall-clock rollback cannot extend it. Historical replay, conflicting
reinstall, future/stale samples, torn history, worker access and daemon restart
refuse. Read failures do not rewrite sample files or the physical journal.

The refreshed Running journal record retains the stop identity and exact
authority. Durable stop admission checks that scope and reload validates it.
Unscoped refresh cannot remove an already persisted scope. The immutable StartPe
completion, physical attempts and effects remain unchanged. This records only
independent synthetic power evidence and stop-admission bookkeeping; it creates
no dispatch release, stop task receipt or stopped-power result.

The connected fixture regression first proves that rereading v1 power cannot
cross a later lease-check boundary. It then installs two v2 samples, consumes the
new tail, rejects monotonic expiry/replay/wrong ownership, admits the bound stop,
and reloads the journal while preserving StartPe completion and two physical
effects. A separate stream test covers future/stale clocks, restart, conflicts,
VM mismatch, checksum mismatch, superseded samples and torn predecessor data.

Verification commands:

Local macOS verification passed: eight focused power tests in 0.20 seconds,
strict all-target/all-feature Clippy, default-feature compilation, formatting
and whitespace checks. This change has no current-source Linux result yet.

```sh
cargo test -p pve-port --all-features --lib power_
cargo clippy -p pve-port --all-features --all-targets -- -D warnings
cargo check -p pve-port
cargo fmt --all -- --check
git diff --check
```

This primitive still accepts a supervisor authority DTO. A positive PostgreSQL
integration proof must obtain that authority from the current committed grant,
arrange a fresh sample after preparation and revalidate cancellation/ownership
before admission. The fixture regression does not establish that database join,
production power collection, stop execution or production readiness.
