# Fresh controller prefix through ConfigurePe

The feature-enabled fixture adapter binds ConfigurePe at the dispatch checkpoint
using the exact generated request, accepted resize identity/request/receipt, and
transitive Clone attempt binding. It requires current supervisor release before
one submission. Physical readback uses the synchronous publication contract and
does not invent a task or UPID. Restored response adapters collect observations
but cannot checkpoint or submit again.

`fresh_controller_clone_then_disk_capacity_then_configure_pe_reaches_satisfied`
uses isolated PostgreSQL and a real fixture daemon. All three stages enter through
`run_osdeploy_once`; no pre-admitted Ready helper is used. The supervisor observes
each generated dispatch, releases exact authorization, and publishes physical
facts before receipt persistence continues. Each stage reaches Satisfied. The
test compares stored request/receipt identity, confirms the ConfigurePe receipt
is synchronous, checks restored physical PE boot configuration, rejects a
restored send/checkpoint, and observes exactly three attempts and three effects.

The daemon lifetime remains bounded at ten seconds. ConfigurePe dispatch
observation allows five seconds because it follows two real database-backed
stages. This is a local macOS prefix proof, not a complete OSDeploy proof.

Remaining gates: later OSDeploy stages and callback/session contracts; worker
death/recovery spanning the new three-stage prefix; Python/Ansible single-writer
handoff; rollback/export and operator acceptance; refreshed exact-source Linux
qualification for these changes; and separately authorized non-production/live
validation. Production deployment and cutover remain unperformed.
