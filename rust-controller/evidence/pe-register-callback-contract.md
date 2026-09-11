# PeRegister callback/session witness boundary

The new osdeploy-adapter callback candidate binds the exact arming context
(run, StartPe/PeRegister operations, attempt, generation, owner, public session
identity and StartPe digest), callback request ID/digest/session revision,
claimed result ID/payload digest, and original microsecond dispatch-event/deadline
anchor. Bounded strict decoding requires independently supplied server-side
context, request and original anchor; incoming JSON cannot establish those.

AuthenticatedPeWitnessV1 still has only `Unavailable`. Candidate assessment
has only refusal outcomes: unavailable witness, expired original deadline,
duplicate, or conflict. Exact duplicate is not successful HTTP acknowledgement;
changed result identity/digest cannot replace an original. No result is stored
or accepted, and no retry clock extends the original deadline.

The result digest is a claimed identity, not proof of payload authenticity. A
future verifier must hash/validate the actual semantic result and produce a
non-forgeable witness from authenticated durable session state. The current
type deliberately cannot produce such a witness. Session UUIDs are not secrets.

The existing api-compat legacy result/phase classification remains unchanged;
its callbacks are not silently promoted into PeRegister evidence. No HTTP route,
credentials, callback receiver, store insert, dispatch or satisfaction path was
added. This slice is pure typed validation/refusal only.

Tests cover session/request/revision/attempt/generation/owner substitution,
original anchor/deadline identity, exact duplicate, result-ID and digest conflict,
nil result identity, malformed hashes, unknown authentication claims, oversized
input, and original-deadline equality. Validation passed: osdeploy-adapter 54
tests plus 6 doc tests; api-compat 22 tests plus 2 doc tests; strict all-target
fixture-ipc Clippy for osdeploy-adapter/api-compat/postgres-store/
operation-controller; workspace fmt and diff checks.

Remaining gates: authenticated verifier and session credentials, atomic
session/result compare-and-insert under durable revision/authority fence,
callback API compatibility policy, and live crash/replay/controller progression.
No production or real Proxmox behavior changed.
