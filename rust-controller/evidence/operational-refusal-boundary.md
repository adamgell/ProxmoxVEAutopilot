# VerifyOperational aggregate refusal boundary

This final manifest-stage contract retains nested heartbeat/installation bindings
including run, VM, agent, session and artifact identity claims; VerifyOperational
operation/attempt; heartbeat receipt digest and exact expected sequence. It checks
version, exact identity, timestamp/freshness and four explicit tri-state claims:
QGA responsiveness, package installation, service running, remaining postconditions.
Missing reports/fields, contradictory fields, stale reports and substituted
identities refuse. Complete-looking claims still return authenticated-operational-
evidence-unavailable; there is no acceptance variant.

This is not a complete operational evidence schema: booleans do not independently
prove package version, service identity, host state, enrollment, or deployment
postconditions. Trusted source records, per-source timestamps, signatures,
receipt reconstruction, fences and durable result handling remain absent.
No default controller integration, dispatch, mutation or satisfaction is enabled.

The sixteen-stage manifest now has additional source-only later-stage refusal
contracts; that does not establish executable sixteen-stage coverage. Earlier
authenticated callback/session gates and production replacement qualification
remain open. No production or real Proxmox activity occurred.

Validation: focused complete/missing/every-partial/every-contradiction/stale/
sequence/attempt/version refusal test; full osdeploy-adapter suite; strict
all-target Clippy for osdeploy-adapter/postgres-store/operation-controller with
fixture-ipc; formatting and diff checks.
