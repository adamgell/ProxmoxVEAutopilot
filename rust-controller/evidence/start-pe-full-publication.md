# Full StartPe fixture publication

The supervisor-only `publish_start_pe_full` command composes v2 cluster inventory
with complete media and infrastructure observations and the existing durable
StartPe task/running record. The worker-only `start_pe_full` command replays it.
Neither command installs general adapter state, releases authorization, dispatches
a VM action, or marks a controller operation satisfied.

Publication requires the exact current-daemon acceptance clock and generation,
accepted effect/receipt, stage identity, and already journaled independent
task/running observation. The supplied durable record must match the ledger
exactly. V2 inventory preserves opaque configuration identity, checks original
receipt bytes, full stage identity, complete coverage and unique membership.
Source template fingerprint, stopped source, running target, and exact target
configuration (except collection timestamp) are checked against the request.
Both expected media must be present in complete same-time inventories; node,
storage capacity/content/availability and bridge must meet the request.
Infrastructure fields are attested at the envelope inventory timestamp.

A separate bounded versioned sidecar is exclusively created, written and fsynced,
then its directory is fsynced before acknowledgement. Existing files cannot be
overwritten by duplicate publication. A torn sidecar is unavailable, not an
accepted partial observation. The sidecar includes a SHA-256 integrity checksum
over publication time and typed content. This detects corruption; it is not a
signature against an attacker who controls the private fixture directory.
Reads always revalidate against the original durable ledger and receipt. Restart
does not refresh timestamps or permit new publication from stale acceptance.
This is not a new ledger mutation or a fabricated acceptance record.

Tests extend the real fixture daemon's fresh Clone→DiskCapacity→ConfigurePe→
StartPe evidence chain. They cover successful publication and restart replay,
duplicate/refused worker publication, partial coverage, forged durable task,
missing infrastructure/media, stopped target, truncated/malformed sidecars,
and owner/generation/attempt substitution. Effect count remains four. This is a
daemon-thread restart and torn-file simulation, not a new OS-worker kill proof.

Validation: publication suite 9 passed, pve-port fixture-ipc library 36 passed,
inventory-v2 contract 1 passed. Strict pve-port/operation-controller all-target
fixture-ipc Clippy, workspace fmt and diff checks passed.

Remaining gates: typed bounded client restoration of this complete bundle,
general adapter integration as read-only evidence, PostgreSQL controller
progression and worker-death proofs, and later guest callback/session stages.
No real qmstart, real Proxmox mutation, default production path change, or
production-ready controller claim is included.
