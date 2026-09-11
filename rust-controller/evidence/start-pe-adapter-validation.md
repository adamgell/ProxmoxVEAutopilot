# General adapter StartPe validation result

`FixtureProvisioningPort::validate_start_pe` now exposes the full restoration
path to a future PostgreSQL caller as
`Result<Option<FixtureStartPeValidationOutcome>, PveReadError>`.

`None` means missing evidence. `Some` is a privately constructed read-only
validation result with an immutable publication accessor; it cannot be produced
by deserializing untrusted JSON. Errors preserve timeout/invalid-response/
transport distinctions. The result is not a controller decision, receipt,
authorization, or dispatch capability.

Before I/O, the adapter checks its fixture/operation/node/source/target identity,
optional exact digest, and absence of conflicting Clone/resize/configure or
checkpoint/dispatch context. It then consumes the bounded full-bundle reader,
which validates exact stage/receipt/durable evidence and complete configuration,
inventory, media and infrastructure. It does not install observations in general
read traits, change mutation state, or write a PostgreSQL journal.

The real fresh-prefix fixture test proves missing evidence, complete result,
and matching result after daemon restart. Recomputed-checksum malicious socket
responses exercise the adapter and still reject partial coverage, mismatched
owner/generation/attempt/receipt, forged task, missing storage/bridge, and stopped
target. Separate tests prove adapter identity mismatch is rejected before
nonexistent-socket I/O.

Validation: pve-port fixture-ipc library 36 passed, inventory-v2 1 passed,
publication/restart 9 passed, stage tests 5 passed. Strict all-target fixture-ipc
Clippy for pve-port and operation-controller, fmt and diff checks passed.

Remaining: explicit PostgreSQL caller integration and fenced progression,
worker-death proof for this full evidence boundary, and later guest stages.
No Satisfied transition, real qmstart, real Proxmox mutation, or default
production path change is included.
