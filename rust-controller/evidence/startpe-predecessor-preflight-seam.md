# StartPe predecessor preflight observation seam

Implemented an opt-in `with_late_start_preflight_receipt` on the already-bound
StartPe fixture adapter. It requires a locally valid canonical ConfigurePe receipt
envelope matching the exact configured predecessor request; malformed, respelled,
repeated, or post-transition installation is rejected. It supplies observation
data only, not original-capture or checkpoint authority.

Before StartPe checkpoint binding, provisioning and inventory reads reuse the
existing ConfigurePe synchronous-publication reader with the bound predecessor
identity/request. The daemon supplies the accepted predecessor publication. Facts
are mapped to the current StartPe observation identity by the same mapping used
for other fixture operations.

As soon as the StartPe checkpoint binds state, the predecessor path is disabled
permanently. Missing StartPe receipt/publication returns unavailable, never stale
ConfigurePe facts. An in-flight predecessor read rechecks the transition after its
await and refuses to return if dispatch state has bound meanwhile.

## Focused proof

The genuine IPC/PostgreSQL test now compares the StartPe port's preflight VM
configuration with the existing restored ConfigurePe observer. After checkpoint
release and before StartPe submission, it requires TransportUnavailable, proving
no predecessor fallback. It also rejects malformed receipt, duplicate installation,
and whitespace-respelled receipt, then runs existing genuine capture, rollback,
backend-death, concurrent replay, route reload, and legacy-NULL assertions.

`RUST_MIN_STACK=16777216 cargo test --offline --locked -p operation-controller --features fixture-ipc --test postgres_fixture_clone fresh_controller_clone_then_disk_capacity_then_configure_pe_reaches_satisfied -- --exact --nocapture --test-threads=1`

Final source passed 1/1 in 14.50s. The initial API assertion failed compilation
because the method did not exist. An intermediate negative test also discovered
that the underlying ConfigurePe reader accepts whitespace-respelled receipt
semantics; the new setter now enforces canonical encoding explicitly.

Important limit: this reuses ConfigurePe's existing semantic receipt/publication
validation. It does not prove equality of caller receipt bytes against a separately
retrieved durable receipt, and canonical encoding alone is not such provenance.
The original StartPe capture and route-persistence boundaries remain separate.

All-feature/all-target Clippy for operation-controller and pve-port with warnings
denied, default-feature operation-controller compile, formatting, and whitespace
checks passed.

The next proof is normal controller preflight/admission/dispatch in an owned child,
followed by its SIGKILL at the SQL write barrier. That has not been run here. The
broader predecessor identity/channel/generation corruption matrix and an explicit
concurrent-read/checkpoint race test also remain open. No production mutation,
physical stop, credential authority, or backfill was introduced.
