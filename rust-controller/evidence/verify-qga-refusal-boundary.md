# VerifyQga receipt/report refusal boundary

This additive pure validator binds a claimed InstallQga receipt SHA-256 and
operation/attempt to VerifyQga operation/attempt, run, node and VMID. All fields
must match the expected binding, version must be one, and missing or negative
reports refuse. Matching responsive claims still return authenticated-host-
evidence-unavailable. No success variant, decoder, network ingress, host/agent
authentication, dispatch or satisfaction is exposed.

Receipt shape/identity checks are not receipt verification. Trusted journal
reconstruction, immutable receipt bytes, host transport authentication, freshness,
current generation/owner fences and durable result CAS are still required before
execution integration. Host-QGA responsiveness does not authenticate the separate
agent. InstallQgaWatchdog, InstallAgent, AgentHeartbeat and VerifyOperational
remain subsequent independent gates.

Validation: focused missing/version/attempt/receipt/nonresponsive/malformed tests,
full osdeploy-adapter suite, strict three-crate all-target Clippy with fixture-ipc,
formatting and diff checks. No live database or Proxmox runtime proof is claimed.
