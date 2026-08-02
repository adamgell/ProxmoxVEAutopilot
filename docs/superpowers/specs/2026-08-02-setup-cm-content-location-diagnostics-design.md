# Setup-CM Content-Location Diagnostics Design

## Purpose

Diagnose the exact MECM configuration that prevents a LABZ1 client from
downloading the built-in client package. The current client evidence is
specific: `192.168.16.103` receives an empty boundary-group list and no DP
location for `LAB00003`.

## Decision

Add one typed, read-only Autopilot Agent work kind:
`setup_cm_content_location_diagnostics`. It runs only on the CM01 Agent and
accepts the fixed LAB site code, a validated target computer name, and a
canonical IPv4 string. It does not accept arbitrary PowerShell, paths,
queries, package IDs, or remediation flags.

## Data Flow

1. The controller validates the LAB-specific request and queues the typed
   work item for a registered Agent.
2. The CM01 Agent validates the same closed request schema and runs its
   embedded PowerShell 7 diagnostic script.
3. The script reads the local Configuration Manager SMS Provider for the
   boundary matching the supplied IP/subnet, its boundary groups,
   the groups' assigned site and referenced DPs, and the `LAB00003`
   distribution state.
4. The Agent validates a bounded JSON result and records it as work-item
   evidence. The operator uses that evidence to choose one separate,
   minimal remediation.

## Result Contract

The result must include `site_code`, `target_computer_name`, `client_ipv4`,
`client_subnet`, `matching_boundaries`, `boundary_groups`,
`distribution_points`, `client_package`, and `errors`. Each collection is
bounded before serialization. The result contains configuration identifiers,
names, status, and errors only; it never returns credentials, installer media,
tokens, or arbitrary command output.

## Safety

The script performs no New/Set/Remove MECM cmdlets and no direct SQL writes.
It runs only through the Autopilot Agent, not generic QGA execution. A missing
boundary, group, DP reference, or package distribution is reported as
evidence; it is not silently repaired.

## Testing

Controller tests prove the endpoint accepts only the closed request and queues
the new work kind. Agent contract tests prove supported-kind registration,
strict request validation, the embedded resource name, and result-schema
validation. PowerShell parser validation proves the shipped script is
syntactically valid. Existing controller and Agent suites remain the release
gate.
