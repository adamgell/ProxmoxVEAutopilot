# Legacy Clone to stage resize bridge

The fixture stage protocol accepts an explicit `legacy_clone` predecessor containing
the original v1 Clone request and receipt. This remains distinct from the v2
stage predecessor. Supplying both forms is rejected. Only GrowDisk can use the
legacy bridge, and the new resize still requires a valid v2 identity and the
current supervisor checkpoint authorization.

The daemon checks exact original durable acceptance, byte-identical receipt,
both predecessor/Clone attempt bindings, plan expectations, target world state,
and requested capacity before consuming authorization or recording the resize.
It does not assign a generation or owner to the historical Clone. The ledger's
legacy-to-stage reassignment rejection remains intact.

Focused unit evidence covers missing acceptance, changed receipt, missing owner,
forbidden reassignment, one accepted resize, duplicate rejection, and replay of
both original receipts. Existing Clone protocol and v2 stage consumer tests pass.
This does not yet prove the new legacy bridge over IPC or a fresh controller
Clone-to-DiskCapacity chain. The controller adapter still needs late binding of
its generated resize request and original v1 predecessor observation readback;
`with_resize_stage` currently requires an exact request and v2 predecessor before
the controller generates its attempt. ConfigurePe integration remains open.
