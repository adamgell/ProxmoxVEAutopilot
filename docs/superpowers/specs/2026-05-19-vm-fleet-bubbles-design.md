# VM Fleet Bubbles Design

## Context

The `/vms` operator page currently behaves like a flat VM inventory with
Autopilot, Intune, Entra, Proxmox, monitoring, and AutopilotAgent evidence
shown in one surface. That is no longer enough for lab and tenant operations.

The operator needs to run more than one workstation fleet, domain, or lab at
the same time without confusing assets, services, DHCP, domain join state, or
lifecycle signals. The page should lead with workstation fleet operations, then
show the critical infrastructure and connected services that make those fleets
work.

This design intentionally models tenant isolation and service relationships
before automating Proxmox bridge, VLAN, firewall, or router creation. Real
network enforcement is a later phase, but the data model and APIs must be
strong enough to support it.

## Goals

- Replace the `/vms` page's `Autopilot Devices (Intune)` section with a
  bubble-aware fleet console.
- Add a first-class lab or tenant bubble model that can group workstation
  fleets, infrastructure VMs, and connected services.
- Assign new CloudOSD and OSDeploy workloads to a bubble at provision time.
- Allow existing assets to be adopted, moved, or repaired manually with audit
  events.
- Treat AD DS, DNS, and DHCP as bubble services provided by the bubble domain
  controller.
- Bring AD, DNS, DHCP, and domain health evidence back through the
  AutopilotAgent running on the domain controller.
- Keep membership authoritative in the bubble model while treating monitoring,
  agent heartbeats, Proxmox tags, deployment runs, and cloud inventory as
  evidence streams.
- Make launch gates explicit so operators know whether a workload is allowed,
  warned, or blocked.

## Non-Goals

- Do not create Proxmox bridges, VLANs, router VMs, firewall rules, or NAT rules
  in the first implementation.
- Do not move the cloud device inventory out of `/cloud` or device detail
  surfaces; only remove the duplicate Autopilot device area from `/vms`.
- Do not require all existing VMs to be immediately assigned to a bubble during
  migration. They can appear as unassigned/adoptable assets.
- Do not make the first implementation a full topology graph. A relationship
  drawer or graph can be added after the core console and API contract exist.

## Bubble Model

Add a PostgreSQL-backed `lab_bubbles` model with fields for:

- Stable id, name, slug, description, lifecycle state, and timestamps.
- Domain name and intended NetBIOS name.
- CIDR, gateway IP, planned bridge or VLAN, and isolation status.
- Default DHCP mode, DHCP pool, and DHCP owner asset.
- DC readiness, DNS readiness, DHCP readiness, and workload readiness state.
- Policy flags for early workgroup launches, domain-join requirements, and
  multi-domain safety.

Add `lab_bubble_assets` to link assets into a bubble by stable identifiers:

- `bubble_id`
- `asset_type`: `vm`, `run`, `agent`, `service`, `external_service`
- `asset_role`: `workstation`, `domain_controller`, `dns`, `dhcp`,
  `file_server`, `configmgr`, `entra`, `gateway`, `other`
- Optional `vmid`, `vm_uuid`, `run_id`, `agent_id`, `service_id`
- Membership state, evidence state, notes, and timestamps.

Bubble membership is the source of truth for the UI and lifecycle APIs. Evidence
from deployment runs, Proxmox snapshots, monitoring probes, cloud records, and
agent heartbeats can confirm or challenge that membership but should not silently
move an asset between bubbles.

## Connected Services

Connected services should be explicit records, not incidental badges inside VM
rows. The first implementation should support these service kinds:

- AD domain
- DNS
- DHCP
- Entra tenant
- File service
- ConfigMgr or MECM
- External/shared service

By default, AD, DNS, DHCP, file service, and ConfigMgr are bubble-local.
Entra is typically external but must be explicitly linked to the bubble or a
workstation fleet. Shared services are allowed only when the link is explicit,
so the UI can show which bubble-local assets depend on services outside the
bubble.

DHCP is sourced from the bubble domain controller. The DC AutopilotAgent reports
DHCP scope configuration, lease evidence, DNS readiness, AD DS readiness, domain
health, and service health back to the controller.

## VM Page Layout

The `/vms` page becomes a fleet-first operator console.

### VM Workstation Fleets

This is the top section. It groups workstation assets by bubble and shows:

- Bubble name and readiness state.
- Workstation count and power state summary.
- Domain/workgroup state.
- Enrollment evidence such as Autopilot, Intune, Entra, and hash status.
- IP and DHCP evidence where available.
- Actions for fleet refresh, launch, and common VM operations.

### Critical Infrastructure

This section shows infrastructure assets grouped by bubble:

- Domain controllers.
- DNS and DHCP owners.
- File servers.
- ConfigMgr or MECM servers.
- Gateway or planned network assets.

Domain controller rows should show AD DS, DNS, DHCP scope, DHCP lease evidence,
domain health, agent health, and last heartbeat.

### Connected Services

This section shows service records and relationships:

- Service name and kind.
- Bubble-local or shared/external scope.
- Provider asset when one exists.
- Consumer fleets and infrastructure nodes.
- Readiness and evidence status.

The existing `Autopilot Devices (Intune)` table is removed from `/vms`. Cloud
inventory remains available on `/cloud` and device detail pages.

## Lifecycle Rules

New workload launches should include `bubble_id` once this feature is wired into
CloudOSD and OSDeploy.

Launch behavior:

- Standalone workgroup workloads may launch before DC, DNS, and DHCP readiness
  when the environment is a simple single-bubble context and the workload does
  not need domain join.
- Domain-joined workloads are blocked until the bubble DC agent reports AD DS,
  DNS, and DHCP scope readiness.
- ConfigMgr-dependent workloads are blocked until domain readiness and ConfigMgr
  service readiness are present.
- Multi-bubble or multi-domain contexts are blocked unless the target bubble has
  proven DC, DNS, and DHCP readiness.
- Existing VM adoption and bubble moves are allowed but must create audit events
  and surface any evidence mismatch.

The UI should show action states as allowed, warned, or blocked. Blocked and
warning states must name the missing evidence, for example `DC agent has not
reported DHCP scope readiness`.

## API Shape

Add a bubble API layer:

- `GET /api/bubbles`
- `POST /api/bubbles`
- `GET /api/bubbles/{bubble_id}`
- `PATCH /api/bubbles/{bubble_id}`
- `GET /api/bubbles/{bubble_id}/readiness`
- `GET /api/bubbles/{bubble_id}/assets`
- `POST /api/bubbles/{bubble_id}/assets`
- `PATCH /api/bubbles/{bubble_id}/assets/{asset_id}`
- `POST /api/bubbles/{bubble_id}/assets/{asset_id}/move`

Add bubble-aware launch fields to CloudOSD and OSDeploy payloads:

- `bubble_id`
- `asset_role`
- `allow_early_workgroup_launch` for explicitly supported early-launch cases

Expose a VM page payload with these top-level sections:

- `workstation_fleets`
- `critical_infrastructure`
- `connected_services`
- `unassigned_assets`
- `warnings`

## Evidence And Signals

The bubble model should consume evidence from existing systems without replacing
them:

- CloudOSD and OSDeploy runs provide run id, workflow, target OS, role, VMID,
  VM UUID, network bridge, and lifecycle state.
- AutopilotAgent heartbeats provide agent id, VMID, computer name, serial,
  IPs, domain state, Entra state, tenant id, run id, phase, and agent health.
- DC agent heartbeats add AD DS, DNS, DHCP scope, DHCP lease, and domain health
  evidence.
- Monitoring probes provide AD, Entra, Intune, and linkage evidence.
- Proxmox snapshots provide VM existence, power state, tags, network config,
  and config drift evidence.

Evidence mismatches should not mutate membership automatically. They should
produce warnings or audit entries so the operator can adopt, move, or repair the
asset intentionally.

## Testing

Add focused tests for:

- Creating and listing bubbles.
- Adding, adopting, moving, and repairing assets.
- Bubble readiness aggregation from asset and service evidence.
- VM page rendering without `Autopilot Devices (Intune)`.
- VM page rendering with `VM Workstation Fleets`, `Critical Infrastructure`,
  and `Connected Services`.
- Early workgroup launch allowed in a simple single-bubble context.
- Domain-join launch blocked before DC, DNS, and DHCP readiness.
- Multi-bubble and multi-domain launch blocked before target bubble readiness.
- Existing CloudOSD and OSDeploy behavior unchanged when bubble fields are not
  supplied during migration.

## Rollout

Phase 1: model and UI contract.

- Add tables and repository functions for bubbles, assets, services, readiness,
  and audit events.
- Add API tests and page tests.
- Reframe `/vms` with the new section headings and payload shape.
- Remove the duplicate Autopilot device table from `/vms`.

Phase 2: lifecycle wiring.

- Thread `bubble_id` into CloudOSD and OSDeploy launch payloads.
- Write membership rows during successful launch creation.
- Feed DC agent AD, DNS, DHCP, and domain health evidence into bubble readiness.
- Add launch gates and warnings.

Phase 3: isolation enforcement.

- Add bridge, VLAN, gateway, firewall, NAT, and DHCP isolation automation.
- Move from warning-only network mismatch reporting to enforced provisioning
  policy where appropriate.

## Self-Review

- Incomplete-marker scan: no incomplete requirement markers remain.
- Internal consistency: DHCP is consistently owned by the bubble domain
  controller, and gateway/firewall enforcement is deferred.
- Scope check: the first implementation is limited to the model, API contract,
  `/vms` refactor, and readiness/lifecycle gates. Proxmox network enforcement is
  explicitly deferred.
- Ambiguity check: launch gates define when early workgroup launches are allowed
  and when domain, ConfigMgr, multi-bubble, and multi-domain launches are
  blocked.
