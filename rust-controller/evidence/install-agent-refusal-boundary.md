# InstallAgent package/predecessor refusal boundary

The additive binding retains the watchdog/VerifyQga predecessor claims including
exact run, VM and agent identity; InstallAgent operation/attempt; artifact UUID
and SHA-256; and claimed watchdog receipt digest. Reports require version one
and exact binding equality. Missing or incomplete reports refuse, and identical
installed claims remain authenticated-installation-unavailable.

This neither reads artifact bytes nor verifies package signing, receipt history,
agent enrollment, installed service state, owner/generation fences or freshness.
Nested predecessor identity is descriptive, not accepted history. No decoder,
route, credentials, download, install, dispatch, CAS or satisfaction is added.
AgentHeartbeat and VerifyOperational remain later independent gates.

Validation: focused missing/version/operation/attempt/artifact/digest-substitution
and incomplete-report tests; full osdeploy-adapter tests; strict all-target
Clippy for osdeploy-adapter/postgres-store/operation-controller with fixture-ipc;
formatting and diff checks. Production/default paths remain unchanged.
