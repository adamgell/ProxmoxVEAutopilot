# InstallQgaWatchdog refusal boundary

The manifest requires VerifyQga satisfaction before InstallQgaWatchdog. This
source-only contract retains claimed VerifyQga operation/result identity and
receipt/result hashes, watchdog operation/attempt, run, VM node/ID and agent
identity. Version and every binding field must match; missing or incomplete
reports refuse. Matching installed claims still refuse because authenticated
predecessor and agent evidence are unavailable.

The agent UUID is only a claimed identity, not enrollment or authentication.
Receipt/result digests are byte-identity claims, not independently verified
history. No host evidence verifier, signed agent report, package/service
postcondition, current fence, freshness policy, durable replay/CAS, installation,
dispatch, decoder/route or satisfaction is provided. Repeated reads are refusals.
InstallAgent, AgentHeartbeat and VerifyOperational remain separate later gates.

Validation: focused missing/version/all-UUID-substitution/VM-result-substitution/
incomplete/repeated-refusal test; full osdeploy-adapter suite; strict all-target
Clippy for osdeploy-adapter/postgres-store/operation-controller with fixture-ipc;
formatting and diff checks. Production/default execution is unchanged.
