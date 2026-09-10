# ConfigurePe fixture acceptance

The local fixture accepts ConfigurePe after an exact durable GrowDisk effect.
GrowDisk acceptance itself requires an exact durable Clone predecessor. The
ConfigurePe request must preserve the GrowDisk expected plan (including target
identity and deployment/driver media), reference its exact binding, and retain
the same Clone binding. The typed request decoder validates owned request facts.
The daemon checks the current durable disk capacity against both the predecessor
effect and the request, requires PE configuration to transition false to true,
consumes the durable stage authorization, then records the attempt and effect.
Its receipt is synchronous; a resize or Clone UPID cannot substitute for it.

`fixture_stage_consumers::durable_clone_resize_configure_requires_exact_accepted_predecessor`
executes all three stages with daemon restarts between stages and after ConfigurePe.
It proves exact receipt recovery, one attempt/effect per stage, missing and owner
rebound predecessor refusal, duplicate refusal, old-generation submission refusal,
and identity-bound recovery. `fixture_stage` also checks receipt kinds and rejection
of unsupported stages. Both integration targets passed (4 tests), as did strict
feature Clippy and formatting.

This is synthetic daemon acceptance. `VmState` persists disk capacity and the PE
configured flag; complete configured target and driver observations still require
stage-aware publication/readback. Full controller progression and service execution
remain separate open gates. No real Proxmox operation or production deployment was
performed.
