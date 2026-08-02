# Setup-CM Source Access Remediation Design

## Goal

Repair the proven single-box MECM client-source ACL gap through Autopilot Agent without restoring generic remote command execution.

## Observed fault

`setup_cm_diagnostics` on `LABZ1-CM01` proved that `TEST\\RING0IVY24-01$` has share read access to `SMS_LAB`, while the underlying MECM `Client` directory has no ACE for that machine SID. The client has a valid secure channel and a CIFS ticket, so the missing NTFS ACE is the remaining source-access fault.

## Chosen design

Add one work kind, `setup_cm_source_access`, handled by an embedded PowerShell script in the existing typed Setup-CM diagnostics service.

The controller accepts only `{ site_code, target_computer_name }`. It rejects extra fields and preserves the existing `LAB` / machine-name validation. The Agent derives the fixed local target `C:\\Program Files\\Microsoft Configuration Manager\\Client`; callers cannot supply a path, account, access mask, or command.

The script resolves only the domain machine identity `<domain>\\<target>$`, grants `ReadAndExecute` with container-and-object inheritance only when an equivalent allow ACE is absent, and then returns the target SID plus the post-change matching ACEs. It does not alter SMB share permissions, existing ACEs, ownership, inheritance protection, or any path outside the MECM Client directory.

## Failure handling

Unresolvable machine identities, missing client paths, or `Set-Acl` errors fail the work item with a bounded error. A successful command still fails if the post-change ACL lacks the target SID's allow ACE. No client-install retry is queued by the new work kind; orchestration must inspect its result first.

## Validation

Contract tests prove the new kind is advertised, strict request parsing rejects unexpected/path-like inputs, and the embedded script is shipped in the single-file Agent. Controller endpoint tests prove a registered agent receives only the typed request. Build and release validation retains the existing Agent contract/build and controller test suites.
