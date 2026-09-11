# Focused callback compatibility evidence

On 2026-09-11, the current Rust source was tested locally with a serial stack
size suitable for the nested async fixtures:

```text
RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test --locked -p api-compat --test callback_contract -- --nocapture
RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test --locked -p osdeploy-adapter --test pe_register_callback -- --nocapture
RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test --locked -p osdeploy-adapter --test pe_register_authority -- --nocapture
RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test --locked -p osdeploy-adapter --test pe_complete_boundary -- --nocapture
```

The callback contract suite passed 5/5 tests, PeRegister callback passed 1/1,
PeRegister authority passed 1/1, and PeComplete boundary passed 2/2. These
tests prove the bounded typed refusal/replay and legacy completion-classifier
behaviors currently implemented. They do not prove authenticated production
callback transport, generic legacy action/result exposure, or end-to-end Linux
runtime qualification.
