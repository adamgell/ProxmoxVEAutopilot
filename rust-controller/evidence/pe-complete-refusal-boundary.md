# PeComplete refusal boundary

The manifest's next stages after StartPe are PeRegister and PeComplete. Existing
PeRegister callback and authority contracts remain refusal-only: no authenticated
witness, session/result persistence, or satisfied predecessor is available.

This additive source-only contract describes PeComplete's original scope,
session, registration/completion operation identities, predecessor event, report
identity/revision, and claimed payload digest. It does not certify those claims.
The completion deadline retains microseconds and is anchored to the original
PeRegister-Satisfied event plus the completion budget, matching `load_scopes`;
it is not the StartPe dispatch/registration deadline.

Assessment requires exact scope and exact required step identities. Failed,
pending, skipped, omitted, or extra steps cannot pass. Even every step reported
complete returns authenticated-registration-unavailable. There is no acceptance
variant, deserialization ingress, route, credential, database write, dispatch,
or controller satisfaction integration. Repeat assessment remains a refusal;
this is not a durable replay/CAS implementation.

Validation: two focused boundary tests cover complete-looking refusal, precise
expiry, replaced anchor, step mismatch/failure, pending/skipped replay refusal,
duplicate/nil step identities, zero revision, and deadline overflow. The full
osdeploy-adapter suite and strict all-target Clippy for osdeploy-adapter,
postgres-store, and operation-controller with fixture-ipc pass, as do formatting
and whitespace checks. No live database or production execution is claimed.

Open gates: authenticated PeRegister witness and transactional result acceptance;
authenticated durable PeComplete result/step identity and revision verification;
trusted scope reconstruction and scheduler integration. Later stages remain
PeShutdownGrace, PeEnsureStopped, ConfigureDisk, StartDisk, InstallQga, VerifyQga,
InstallQgaWatchdog, InstallAgent, AgentHeartbeat, and VerifyOperational. This
change does not prove their execution, or enable real qmstart/production writes.
