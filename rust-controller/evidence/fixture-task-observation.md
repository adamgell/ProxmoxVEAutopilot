# Fixture task observation prefix

The default-disabled `fixture-ipc` feature exposes `FixtureReadClient::task`.
The daemon loads one strictly bounded (4096 byte) `task.json` observation from
the private supervisor directory before opening sockets. The record binds the
fixture UUID, node, operation UUID, request SHA-256, and opaque node-prefixed
UPID. Requests contain only this identity; they cannot select the result.

The recorded result is explicitly absent, running, succeeded, or failed. The
observation timestamp is historical. Missing seed data and identity mismatches
reject the read; they do not prove task absence. Unknown fields, invalid identity,
invalid versions/timestamps, control-bearing failure text, and oversized records
fail decoding. Corrupt seed data prevents daemon startup. Reads do not append to
the attempt/effect ledger. The record survives daemon restart as the same seed
file and is loaded once per daemon lifetime.

This prefix does not execute tasks, generate UPIDs, persist task transitions,
or infer success from a durable VM effect. It supplies no provisioning capability
and does not establish a controller Clone round-trip or independent worker
takeover proof. The UPID is an opaque synthetic identifier with node-prefix and
bounded printable-text validation, not a production Proxmox UPID parser.

Verification: `fixture_task` covers all four result states, invalid envelope
fields and bounds, two daemon lifetimes with identical observations, mismatches
in every identity field, and unchanged attempt count. Focused tests (2) and
feature-enabled all-target strict Clippy pass on macOS.
