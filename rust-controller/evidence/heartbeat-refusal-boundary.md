# AgentHeartbeat refusal boundary

The additive binding retains InstallAgent artifact/receipt, nested VM/agent
identity, and exact heartbeat operation/attempt/session claims. Version, scope,
positive bounded sequence, observation timestamp and bounded freshness must
match. Missing, stale, future, substituted-session and replay/out-of-order claims
refuse. Fresh increasing claims still return authenticated-agent-unavailable.

The only witness variant remains Unavailable. The inherited witness type name
does not grant PE or full-OS agent trust. Expected identity, last sequence and
timestamps are inputs, not authenticated or persisted here. No signature,
session issuance, durable monotonic CAS, receipt verification, execution fence,
network route, acceptance, dispatch or satisfaction is implemented. Caller
timestamps do not establish trusted freshness. VerifyOperational remains the
final separate stage, and all earlier execution gates remain open.

Validation: focused missing/fresh/replay/stale/future/session/version refusal
test; full osdeploy-adapter suite; strict all-target Clippy for osdeploy-adapter,
postgres-store and operation-controller with fixture-ipc; fmt and diff checks.
No production/default behavior or live infrastructure was changed.
