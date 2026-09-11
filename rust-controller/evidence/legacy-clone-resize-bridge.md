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
The `fixture_legacy_resize` IPC integration now proves the bridge across three
daemon lifetimes. Before Clone acceptance, structurally valid fabricated receipt
bytes are rejected without attempts or authorization consumption. The exact v1
Clone is then accepted through the mutation client. After restart, the original
receipt is recovered byte-for-byte. Changed valid receipt bytes, fixture and
ownership/attempt/digest mismatches, and relabeling the predecessor as v2 all fail
without consuming the released resize authorization. The exact bridge creates
one resize. A second restart recovers its exact receipt and refuses both stale
and newly assigned duplicate requests; two attempts and effects remain.

Validation: the new IPC target passes one test; `fixture_stage_consumers` passes
two and `fixture_post_dispatch` passes seven. Strict feature all-targets Clippy,
formatting, and diff checks pass. No production/default controller path changed.

The adapter now supports `with_late_resize_after_legacy_clone`: it reads original
accepted v1 Clone observations before binding the controller-generated resize
request at the dispatch checkpoint. Successful supervisor release is required
before its single submission. Once binding begins, stale predecessor observations
cannot be returned. `with_late_resize_receipt` restores a journaled response for
observation only and cannot resubmit. Tests cover missing ownership, unsupported
stages, absent predecessor observations, failed checkpoints and restored-response
send rejection.

A fresh controller Clone-to-DiskCapacity chain remains unproven. Its proof must
orchestrate the store's new resize attempt, arm/release its exact v2 supervisor
identity, publish accepted resize observations, and inspect journaled satisfaction.
The existing fresh Clone harness uses a v1 barrier and needs an additional v2
supervisor phase for resize. No pre-admitted Ready result was used as a substitute.
ConfigurePe integration remains open.
