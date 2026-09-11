# Legacy run registration provenance

This source audit identifies the missing trusted input for durable credential
association. It does not install a mapping, admit callbacks, or change production.

## Established source contract

- `autopilot-proxmox/web/ts_engine_pg.py:create_run_from_version` allocates its
  run identity with `_new_id()` and persists the task-sequence run.
- `autopilot-proxmox/web/osdeploy_pg.py:create_run` obtains that identity from
  `create_run_from_version`; `osdeploy_runs.run_id` is a UUID foreign key to
  `ts_provisioning_runs.id`. The returned OSDeploy run represents this same run.
- `autopilot-proxmox/web/osdeploy_endpoints.py:_sign` passes a text run identity
  through to `winpe_token.sign`. The token helper's `int` annotation does not
  coerce its argument: JSON retains text versus integer identity.
- `rust-controller/crates/postgres-store/src/osdeploy/registration.rs:
  enqueue_osdeploy` receives a caller-selected `RunId` and a validated plan. It
  locks `native:run:{uuid}`, checks an existing workflow fingerprint, reserves VM
  identities, and inserts Rust run and operation records in one transaction.
  None of those steps reads or validates a Python run row.
- A search of Rust production source for `enqueue_osdeploy` finds the store
  implementation; the other source caller is a scheduler test. There is no
  production create/import bridge connecting the two identity domains today.

Consequently, a bearer for a text UUID equal to a Rust `RunId` does not establish
that the Rust plan belongs to the original Python run. VM UUID, VMID, agent name,
and matching strings are also insufficient substitutes for registration provenance.

## Concrete bridge contract

The service create/import boundary must own the association. For an existing
Python OSDeploy run, it must read the persisted OSDeploy and task-sequence run in
the configured legacy database, reconstruct the admitted plan from that run's
immutable inputs, and register the Rust workflow and association together.
For a newly created Rust-owned run, the server chooses the endpoint identity and
registers it with the plan before issuing any credential. Neither path accepts
a token claim as evidence that an arbitrary existing Rust run is the target.

Persist a versioned identity tuple containing the configured source namespace,
claim kind (`text` or `integer`), exact claim value, Rust run UUID and admitted
workflow fingerprint. The source namespace distinguishes independently managed
legacy databases and must come from service configuration, never callback JSON.
Text values retain exact bytes; integer values use the existing bearer type's
integer representation. Integer `42`, text `"42"`, and text `"042"` remain
distinct. UUID-shaped text must not be reformatted to manufacture an association.

Uniqueness on the source identity tuple prevents reassignment to another Rust
run; uniqueness on the Rust run prevents replacing its originating identity.
Replay with the same tuple and fingerprint returns the existing registration.
A changed identity, run, or fingerprint conflicts. Historical ownership survives
cancellation and credential expiry. Registration must not expose a general
`bind_identity(untrusted_claim, arbitrary_run)` operation.

Extend the registration transaction under its existing run lock. A unique SQL
constraint arbitrates two different Rust run locks claiming the same source
identity, rolling back the losing registration and reservations. Any extra
identity advisory locks must follow one documented ordering shared by every
bridge caller. StartPe subsequently reads the committed association under its
existing authority, run, operation, attempt and lease lock order; it must not
create or reassign the mapping during credential issuance.

## Required implementation evidence

1. Trusted fixture create/import reconstructs a registered plan and identity;
   an independently supplied UUID or verified foreign bearer cannot do so.
2. Exact replay is idempotent. Integer/text substitutions, source namespace
   changes, run substitution and plan changes conflict.
3. Concurrent registrations for one source identity produce exactly one owner
   and no orphan workflow, VM reservation or partial operation rows.
4. A failure between mapping insertion and registration commit rolls back both;
   reload after a lost commit response returns the original owner.
5. Cancellation and restart retain ownership. StartPe refuses issuance when the
   association is absent and reads the same owner after lease replacement.

The remaining integration choice is the concrete service create/import entrypoint
and its configured legacy database source. Current store inputs cannot supply
that authority. A migration or constructor alone would leave this prerequisite
unproven. This does not block independent credential alias persistence work when
its tests explicitly provide a trusted fixture registration boundary, but such
tests must not be reported as production legacy import compatibility.

Validation: inspected the named Python allocation, signing and schema paths,
Rust registration transaction, and scheduler lock order; searched all Rust source
call sites. This is a source-derived contract, with no runtime mapping tests or
production database observations claimed.
