# Late registration callback after recovery

Baseline source: `e01de909`. The existing recovery test proved original-deadline
exhaustion, retained attempt identity, and refusal to restart or reclaim the
terminal operation. It did not call the authenticated registration method after
that recovery outcome.

The added assertions in
`crates/postgres-store/tests/osdeploy_durability.rs::fixture_peregister_original_deadline_expires_after_recovery`
submit the actual delivered credential and correct plan-derived client identity
after the recovered operation reaches `Unknown`. Acceptance must fail; reloaded
state, revision, attempt and deadline must remain unchanged. Neither a selected
registration nor a PE-completion deadline may appear. This establishes that a
late authenticated request cannot revive this terminal recovery path.

The credential expiry derives from the same registration deadline. Therefore the
test does not claim to isolate token expiry, provenance expiry or terminal-state
rejection as the sole reason. It exercises their combined service-store outcome
using genuine fixture issuance and persisted provenance.

Validation command:

```sh
RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test -p postgres-store --all-features --test osdeploy_durability fixture_peregister_original_deadline_expires_after_recovery -- --exact --nocapture
```

Result: **1 passed, 0 failed, 141 filtered out**, 99.41 seconds, exit code 0.
Formatting and whitespace checks pass. This is a
macOS store integration proof, not an independent process-death, Linux, HTTP
legacy-parity or production-cutover proof. No runtime code or infrastructure
configuration changed.
