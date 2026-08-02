# Setup-CM Source Diagnostics Design

## Goal

Allow the Autopilot Agent on the single-box MECM server to collect the exact read-only evidence needed to diagnose why a domain-joined test client cannot read the `SMS_<site>` client source. The Agent remains the execution plane; the controller never accepts arbitrary commands or PowerShell.

## Scope

The controller adds one typed queue request, `setup_cm_diagnostics`. It accepts only a three-character site code and a NetBIOS computer name. The Agent runs a fixed diagnostic script that reports the target machine SID, SMB share ACL, NTFS ACL for the `Client` source folder, and the CIFS SPNs for the MECM server. The report is JSON, contains no credentials or tokens, and is returned as the work item result.

The request does not accept a command, script, filesystem path, FQDN, identity, or ACL mutation. The Agent does not grant access, restart services, or change the MECM server. A separate, explicitly typed remediation can be designed only after the report identifies a failed authorization boundary.

## Architecture and Data Flow

1. The controller validates `site_code` and `target_computer_name`, creates one `setup_cm_diagnostics` work item for a registered server Agent, and retains the typed request in PostgreSQL.
2. The Agent validates the same two fields and invokes only its packaged `SetupCmSourceDiagnostics.ps1` with those arguments.
3. The script derives the fixed share name `SMS_<site>`, resolves the computer account SID, reads share and client-folder ACLs, queries the local server's CIFS SPNs, and emits one JSON object.
4. The Agent parses and bounds that JSON, then completes the work item. A non-zero script result fails the item without exposing secret-bearing PowerShell output.

## Safety

- The packaged script is a product asset, not caller input.
- Site code is `[A-Z0-9]{3}` and the target computer name is a single NetBIOS label; both are passed through `ProcessStartInfo.ArgumentList`.
- Results include account and ACL identifiers only. They omit passwords, tokens, product keys, connection strings, and environment variables.
- The script uses read-only `Get-*`, `Get-Acl`, SID translation, and `setspn -Q`; it contains no mutation command.

## Error Handling

An unavailable AD lookup, share, ACL path, or SPN query is represented as a named diagnostic error in the JSON result so one missing datum does not erase the other boundaries. The Agent accepts only a JSON object with the expected top-level keys, limits output to 256 KiB, and reports an invalid result as a failed work item.

## Tests and Acceptance

- .NET contract tests prove the Agent advertises the diagnostic kind, rejects invalid or extra request fields, and invokes the fixed script only.
- Controller API tests prove valid typed requests return `202`; invalid site, path-like computer names, and unknown Agents are rejected without queueing work.
- The deployed server Agent completes a diagnostic request for `LAB` and `RING0IVY24-01`, returning the share/NTFS/SPN evidence needed for one minimal remediation.

