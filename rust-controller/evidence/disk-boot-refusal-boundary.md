# ConfigureDisk and StartDisk read-only boundary

The existing manifest requires PeEnsureStopped satisfaction before ConfigureDisk,
then ConfigureDisk satisfaction before StartDisk. The additive DiskBootScopeV1
retains the exact stage, run, operation, predecessor operation/event, node and
VMID. Stage-operation aliases, nil identifiers and unsupported stages refuse.

Assessment checks scope equality, VM-specific power/configuration/storage claims,
bounded field shape, monotonic observation time, and bounded freshness. Missing,
running or stale evidence never passes. Even fresh stopped/configuration/volume
claims return DurablePredecessorAndAuthorityUnavailable. Opaque PVE configuration
identity is not mislabeled as a canonical hash.

This is descriptive validation only. A volume string does not prove ownership,
storage accessibility, intended disk attachment or capacity. No trusted receipt,
predecessor-stage reconstruction, independent observation, fence, immutable
request digest, durable replay/CAS, mutation authorization or satisfaction is
provided. The typed scope is not a dispatch capability. No default adapter,
database, HTTP, fixture execution, production, or real qmstart path is changed.

Validation: two focused tests cover both stages, repeated refusals, missing and
wrong-VM evidence, running/stale/future evidence, invalid storage fields,
substituted stages and aliased operations. Full osdeploy-adapter tests and strict
all-target Clippy for osdeploy-adapter/postgres-store/operation-controller with
fixture-ipc pass, along with formatting and whitespace checks.

Execution remains gated on authenticated PeRegister/PeComplete and shutdown
predecessors. Following stages remain InstallQga, VerifyQga, InstallQgaWatchdog,
InstallAgent, AgentHeartbeat and VerifyOperational. No full workflow readiness
or runtime qualification follows from this source-only contract.
