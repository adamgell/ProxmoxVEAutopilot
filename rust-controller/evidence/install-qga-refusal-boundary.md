# InstallQga refusal contract

The manifest requires satisfied StartDisk before InstallQga, then InstallQga
before VerifyQga and VerifyQga before InstallQgaWatchdog. This slice covers only
InstallQga's descriptive run/predecessor/event/operation/attempt/node/VM scope.

Host-QGA responsiveness is a caller claim, not verified Proxmox evidence or an
authenticated agent result. Missing/nonresponsive/wrong-VM/future claims refuse;
even matching responsive claims cannot authorize installation or satisfy a stage.
Exact scope substitution also refuses. Repeat reads have no side effects.

No host transport, freshness policy admission, signed agent session/result,
generation/owner fence, package identity, durable receipt/CAS, dispatch or
satisfaction is implemented. In particular, QGA absence is not a reason to skip
installation or report success. This diagnostic boundary is not a runnable
installer. VerifyQga and InstallQgaWatchdog remain unimplemented here; later
InstallAgent, AgentHeartbeat and VerifyOperational remain separate gates.

Validation: focused refusal test, full osdeploy-adapter tests, strict all-target
Clippy for osdeploy-adapter/postgres-store/operation-controller with fixture-ipc,
formatting and diff checks. No production, real Proxmox, or runtime proof.
