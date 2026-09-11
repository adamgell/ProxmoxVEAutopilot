# Legacy completion branch classifier

Baseline: `b0717f89`. The pure callback compatibility corpus lacked executable
classification of legacy reboot/retry completion branches. The new
`api_compat::callback_contract::classify_legacy_completion` supplies descriptive
step-state advice for synthetic fixtures. It has no callers in the controller,
no credentials, no persistence and no scheduling capability.

Source: `autopilot-proxmox/web/ts_engine_pg.py::complete_step` (lines 2181–2263),
SHA-256 `7b841441f7b6ca621d4dbf72d5590e6405c09b298ce6e6f6a40c5e9b3b84097a`.
The source first ignores failed results while awaiting reboot, before checking
retry eligibility. Success with required reboot remains awaiting reboot;
explicit reboot-required does likewise. Failed attempts use inclusive
`attempt <= retry_count`. General terminal-state immutability is absent in this
legacy branch selection, so the fixtures explicitly retain that difference from
Rust's immutable first-result comparison.

Validation: `cargo test -p api-compat --test callback_contract` passed 5 tests,
0 failures. The new ten-case table covers late-failure precedence both with and
without remaining retries, success/reboot interaction, skipped results, explicit
reboot, retry boundary and terminal-state differences. Existing tests continue
to require Rust result conflicts and original-attempt binding. Formatting and
whitespace checks pass.

This is not an authenticated callback endpoint or a complete Python parity
claim. The classifier takes normalized typed statuses and nonnegative counters;
it does not model Python input coercion, negative counters, run-level
continue-on-error decisions, returned HTTP bodies or response token renewal.
It does not enable native retries. Real legacy recovery still requires durable
action exposure, authenticated original-attempt resolution, atomic selected
result/successor state, and process-loss/response-loss tests through the actual
service. No infrastructure was mutated.
