# StartPe fixture contract and execution gate

The repository-defined next stage after ConfigurePe is StartPe
(`osdeploy.start.pe.v1`), classified as a PVE mutation and dependent on satisfied
ConfigurePe. PeRegister follows StartPe and is a callback wait. StartPe itself
does not require inventing callback/session completion.

The v2 fixture request contract now represents StartPe and binds its exact
operation/stage/attempt/generation/owner/digest identity. Its only valid receipt
kind is a target-VM `qmstart` task from the expected node and `fake@pve` fixture
principal. Synchronous acceptance, resize tasks, and source-VM start tasks are
rejected. `validate_start_pe_predecessor` requires the exact ConfigurePe
predecessor binding, plan, shared Clone binding, fixture, expectations, and
synchronous predecessor receipt. Structural receipt validation grants no durable
acceptance authority.

The daemon checks the predecessor against its accepted stage ledger and validates
that contract, then refuses StartPe before consuming checkpoint authorization or
recording an attempt/effect. This explicit boundary is necessary because current
`VmState` persists disk bytes and PE configuration only; it cannot represent or
recover a running/stopped transition. Returning `qmstart` acceptance while merely
retaining those fields would claim an unrepresented physical effect.

The real IPC regression first accepts and restarts the Clone→DiskCapacity→
ConfigurePe chain. It then arms/releases an exact StartPe identity with that
accepted predecessor. Repeated StartPe submissions are refused while release
authority remains unconsumed, StartPe accepted-effect lookup remains empty, and
the original three attempts/effects remain intact. Four stage-contract tests and
two stage-consumer tests pass; strict feature all-targets pve-port Clippy,
formatting, and diff checks pass. Existing adapter checkpoint tests continue
rejecting unsupported StartPe dispatch.

StartPe execution remains unimplemented. Next requirements are a versioned durable
power-state transition and replay contract, authoritative stopped→running effect,
stage-bound task/power observations, late adapter binding, scheduler/store support,
and a fresh controller proof. PeRegister/PeComplete callback and session semantics
follow separately. Production/default controller paths are unchanged.
