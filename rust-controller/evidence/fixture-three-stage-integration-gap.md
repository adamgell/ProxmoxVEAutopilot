# Three-stage fixture integration gap

The fresh PostgreSQL controller integration currently proves Clone. It does not
prove Clone -> DiskCapacity -> ConfigurePe progression. No predecessor records
were manufactured for this assessment.

`FixtureStageRequest` is a version-2 typed message seam for the existing validated
Clone, EnsureCapacity (DiskCapacity), and ConfigurePe requests. It hashes the full
request including operation, attempt, plan, before-state and generation binding.
Other provisioning actions are rejected. Receipt validation pins fixture and
request digest and requires a positive sequence. Clone uses `qmclone` for the
source VM, DiskCapacity uses `resize` for the target VM, and ConfigurePe requires
synchronous acceptance. Every task receipt pins node and `fake@pve` identity.
ConfigurePe has no UPID under the existing provisioning contract.

This seam does not grant authorization, persist acceptance, or submit mutations.
The daemon remains Clone-only. Its late authorization, checkpoint consumption,
post-dispatch publication, and adapter dispatched state use `FixtureCloneRequest`.
Consequently simply enabling the next controller stages would return Rejected
and cannot satisfy the requested progression.

Remaining implementation must compose the stage envelope with:

- supervisor late authorization and durable single-use consumption;
- operation-specific v2 reads and fresh predecessor-derived target observations;
- typed daemon resize/configuration effects and synchronous ConfigurePe receipt;
- post-dispatch publication for target capacity/configuration and stage task type;
- the existing controller path and durable PostgreSQL journal, with one original
  attempt per operation and actual satisfied predecessor ordering;
- missing/stale observations, cancellation, response-loss/Unknown and restart tests.

The focused `fixture_stage` test verifies the message contract and disabled later
stages. It is not controller progression or production readiness evidence.
