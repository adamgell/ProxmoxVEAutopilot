# Callback/session implementation gap

Assessment date: 2026-09-10  
Scope: local Rust controller PoC; production `192.168.2.4` and real Proxmox remain read-only.

## Current-state evidence

- `git ls-files` contains no Rust callback/session or guest-action module.
- The current OSDeploy implementation intentionally leaves callback-dependent stages inaccessible; the reachable proof boundary is the initial Clone → DiskCapacity → ConfigurePe prefix.
- `next-durable-planning-decisions.md`, `next-durable-schema-proposal.md`, and `next-durable-transaction-boundaries.md` describe callback requirements but explicitly defer their implementation.
- Those notes refer to `callback-main-decisions.md` and `task-2-brief.md`, but neither file exists in this checkout or in the configured MCP docs inventory. `./skill.sh docs 'callback-main-decisions'` returned no result.

## Safe implementation boundary

The next source phase must first restore or approve the authoritative callback contract, including session identity/role/label/credential issuance, callback replay/conflict/late-result semantics, guest-action exposure and result binding, exact timeout serialization, truthful grace activation, and service shutdown/drain behavior. Only then should family-private constructors, migration rows, HTTP handlers, or stage reachability be implemented.

Adding a guessed callback API now would create a second, potentially incompatible execution path and would invalidate the existing strict-readiness claims. This is therefore a specification-availability gap, not permission to touch production or a reason to weaken the sixteen-stage requirement.

## Bounded identity slice implemented

The additive `osdeploy-adapter::GuestActionIdentity` contract now supplies the stable identity component needed by the later callback phase. It admits exactly the five existing guest-action stages, sets `step_id == operation_id`, retains the original attempt UUID, fixes `retry_count == 0`, and rejects non-guest stages or nil identities. It exposes no session, credential, result, or dispatch capability. The focused adapter suite passed 3 new guest-identity tests plus all existing adapter tests and six intended compile-fail documentation tests; complete output is `guest-action-contract.log` (SHA-256 `a67cbedb090bbd0291cb069ff8f36fec8ecb838728d580cc23fdba435418e2bd`).

## Current readiness impact

The local Linux execution, controller/database recovery, Python/Rust compatibility, and service observation gates are accepted. Full callback/session integration, sixteen-stage service execution, independent process recovery, and Python/Ansible single-writer handoff remain unproven pending the authoritative callback contract.
