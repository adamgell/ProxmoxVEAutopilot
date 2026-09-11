# PeRegister result rollback/concurrency probe

An additive fixture-only `pe_register_result_probe` module preserves the existing
refusal-only result API. It adds a session-key transaction advisory-lock probe
and a diagnostic authority/run/operation/lease/session-lock transaction.
Both explicitly roll back before returning; neither inserts results or returns
an acceptance/dispatch capability.

The full diagnostic reads Rust authority generation under a shared lock, takes
the existing run advisory lock, reads active StartPe operation/lease/epoch fields
under shared row locks, then takes a nonblocking session result lock and invokes
the pure verifier. The current context identifies the StartPe attempt, not a
separately admitted PeRegister attempt. Consequently this is not authenticated
PeRegister authority, a durable result-revision CAS, or proof of stored session
admission. Those mappings and the actual witness remain unavailable.

The opt-in SQL test requires `PVA_START_PE_SCHEMA_TEST_DSN`, checks loopback host
and absent rust_controller schema, and uses two connections. An original
transaction holds the session lock; the probe must report conflict. After the
original rolls back, a probe obtains the lock but still refuses capability and
rolls back. A subsequent transaction must acquire it, with no schema created.
The test has no Docker launcher and does not use a production DSN.

Validation: preconnection nil-session test passed; the SQL concurrency test
compiled but was not executed because the explicit DSN is absent. Pure authority
and callback tests passed, as did the existing result gate test. Strict all-target
fixture-ipc Clippy for postgres-store/osdeploy-adapter/operation-controller,
workspace fmt and diff checks passed. SQL syntax/runtime behavior of the new
diagnostic query and actual two-connection lock/rollback behavior remain pending
runtime validation; no runtime success is claimed.

No default migration, HTTP route, credential issuance, authenticated success,
result persistence, production mutation or controller satisfaction was enabled.
