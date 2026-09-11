# Shutdown scope refusal boundary

The stage manifest requires PeComplete satisfaction before PeShutdownGrace;
PeEnsureStopped has the distinct ShutdownGraceOrGuardedEscalation dependency.
The PostgreSQL scope loader anchors ShutdownGrace to PeComplete's original
Satisfied decision time plus shutdown_grace_seconds. Its scheduler currently
refuses shutdown-grace expiry rather than inventing a stop-enabling reason.

This additive pure contract retains that distinct run/operation/predecessor-event
scope and microsecond deadline. It describes claims, not authenticated history.
Missing, reported-running, and reported-stopped observations all refuse before
expiry because authenticated completion is unavailable. At and after expiry,
guarded escalation remains unavailable: elapsed time never grants force-stop
authority. Scope substitution, earlier observation clocks, nil operation/event
identities, aliased stage operations, invalid budgets, and overflow are rejected.

No deserialize ingress, authenticated witness, accepted result, VM identity or
freshness verification, durable replay/CAS, stop authorization, or satisfaction
is implemented. A future executable path must bind independent power evidence,
the exact accepted PeComplete predecessor, original deadline, policy allowing
guarded escalation, and current attempt/generation/owner fence. This contract
cannot stand in for any of those gates. Production/default execution is unchanged.

Validation: two focused refusal tests, full osdeploy-adapter suite, strict
all-target Clippy for osdeploy-adapter/postgres-store/operation-controller with
fixture-ipc, formatting, and diff whitespace checks. No live database or Proxmox
runtime qualification is claimed.

Following stages remain ConfigureDisk, StartDisk, InstallQga, VerifyQga,
InstallQgaWatchdog, InstallAgent, AgentHeartbeat, and VerifyOperational; neither
this slice nor the preceding callback contracts establish their readiness.
