# Executable callback compatibility decisions

`api_compat::callback_contract` adds two pure classifiers drawn from the callback
corpus. Phase reporting preserves legacy phase/any filtering, input order and
pending/running/awaiting-reboot selection. It retains failed steps separately:
legacy `phase_complete=true` can coexist with failure and is not Rust PeComplete.

Result classification compares the incoming payload with the original accepted
payload under an explicit operation/attempt binding. Equivalent Failed replay
remains equivalent; changed status/message/data is conflict; changed operation,
attempt or a misbound original is rejected. `FirstCandidate` does not mean
accepted: admission and atomic compare/insert remain the caller's responsibility.
These functions neither authenticate nor persist anything, and they define no
HTTP conflict policy. The legacy body carries no Rust attempt, so its binding
must ultimately come from authenticated durable action exposure.

PeRegister remains unsupported: registration alone supplies no bootstrap plus
subsequent bearer witness, authenticated session/role binding, or selected durable
milestone. No conversion to Satisfied, HTTP handler, session storage or grace
activation was added. Required atomic result/grace/successor behavior, conflict
auditing and five-second continuation remain open.

Verification: four focused classifier tests, all eighteen existing api-compat
tests and both compile-fail doctests passed. Strict all-target api-compat Clippy,
formatting and diff checks passed. No Python behavior or live system changed.
