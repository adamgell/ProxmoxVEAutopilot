# StartPe provisioning facts from validated full publication

Baseline: `d98ba908`. The bound StartPe adapter now maps the existing full
publication into its generic provisioning-read family. Every read first calls
`validate_bound_start_pe`, requiring the accepted request/receipt, exact stage
identity and validated full publication. No cached seed or current time fills in
missing evidence.

Source and target configuration, power and coverage come from the corresponding
validated inventory members. Their observation time is the original inventory
timestamp. Deployment/driver media retain their original typed objects and
observation times. This connects `provisioning_vm_config`, `vm_status` and
`provisioning_media` to actual accepted StartPe evidence. It does not expose
cluster inventory, task status or infrastructure preflight facts; those mappings
remain closed and full controller collection is not yet claimed.

The owned daemon/process test now checks each original config object, power
value and timestamp, and each media object through the generic adapter methods.
It checks refusal before full publication and continued refusal for unmapped
cluster inventory. Existing malformed/publication/restart/identity cases still
run in the same suite.

Validation: `RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1 cargo test -p pve-port
--features fixture-ipc --test fixture_post_dispatch` passed 12 tests, 0 failures,
3.19 seconds. `cargo check -p pve-port --features fixture-ipc` passed. Formatting
and whitespace checks pass. All-target/all-feature pve-port Clippy passed with
warnings denied in 7.63 seconds. No Setup/PostgreSQL integration, complete evaluator readiness,
physical stop or production action is claimed.
