# PeRegister verifier/session authority descriptor

The pure `PeRegisterVerifierContractV1` retains expected session/context,
callback request/revision, original microsecond deadline, expected result
revision, and an explicit PostgreSQL-style authority fence. The fence keeps
numeric authority generation, worker identity, lease epoch and operation
revision separate from the UUID session context generation/owner.

Assessment checks current fence equality, exact result revision and exact
callback/session/anchor binding before replay classification. A prior result at
another revision is conflict; exact replay is duplicate; changed result payload
identity is conflict. Invalid zero/nil/out-of-range fence or revision fields are
rejected. First-time matched candidates still return authenticated-witness-
unavailable, and the original deadline still expires without extension.

Every outcome is a refusal. Descriptors are not authenticated authority: their
expected/current values must eventually be reconstructed under actual store
locks. The type has no Deserialize, credential, key, HTTP route, result commit,
success permit, or database access. It does not implement cryptographic
verification or an atomic compare-and-insert, and does not turn a matching
caller-supplied fence into authorization.

Tests cover each fence field, result revision mismatch, original revision
conflict, duplicate, changed result digest, substituted session, deadline equality,
invalid fence identities and result revision bounds. Validation passed:
osdeploy-adapter 55 tests plus 6 doc tests; api-compat 22 tests plus 2 doc tests;
strict all-target fixture-ipc Clippy for osdeploy-adapter/api-compat/postgres-store/
operation-controller; fmt and diff checks.

Remaining gates: authenticated session/witness implementation, durable fence
reconstruction and atomic result persistence, callback API policy, and crash/
controller progression proofs. Production/default paths remain unchanged.
