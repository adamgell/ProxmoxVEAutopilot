# Typed StartPe restoration and readback

`FixtureStartPeRestoration` is an explicitly read-only adapter for the durable
StartPe task/power publication. It restores the exact fixture request, dispatch
identity and original receipt, then obtains the record through the new typed
`FixtureReadClient::start_pe_observation` method. It implements no mutation,
checkpoint or controller satisfaction trait.

Every returned record is revalidated against operation, stage, attempt,
generation, owner, request digest, target VM, original receipt digest and exact
task UPID. Version, running state, predecessor ordering and original observation
time bounds must be valid. Publication time cannot be in the future. Missing
publication remains `None`, not success. Equivalent but re-encoded receipt bytes
do not satisfy the original receipt binding.

The adapter projects only the task-success and unlocked-running facts actually
represented by that validated record. It preserves the original task/power times,
acceptance time, publication time and daemon generation across restart. It does
not manufacture configuration, media, inventory, guest or callback evidence, and
does not assess whether historical facts are fresh enough for a later controller
decision. That remains a separate evaluation gate.

## Evidence

The real-daemon fixture test observes absence before publication, successful typed
readback afterward, and identical readback after daemon restart. Operation/attempt
substitution is rejected at restoration; owner/generation mismatch and changed
receipt bytes fail readback. A deliberately malformed socket server supplies
contradictory records to exercise client-side validation: stopped power, another
VM, altered generation/attempt, changed receipt digest/task UPID, and stale task
or power times all fail with `InvalidData`. These are parser/binding tests, not
claims that a trusted supervisor's external observations have been independently
verified against real infrastructure.

Validation passed: 15 fixture post-dispatch/stage/consumer tests, two default-feature
ledger tests, strict all-target Clippy for `pve-port` and `operation-controller`,
formatting and diff checks. The expanded readback test also passed after adding
the explicit target-VM contradiction check.

Open gates: general provisioning adapter integration with full configuration and
inventory postconditions, PostgreSQL four-stage controller satisfaction, OS-worker
kill/recovery matrices, and live/production evidence. No real `qmstart`, Proxmox,
production/default-path or `192.168.2.4` mutation was performed.
