# Setup-CM content-location remediation

## Goal

Repair the proved LABZ1 MECM content-location gap without granting a general
remote PowerShell surface. The LAB client subnet `192.168.16.0/24` must receive
the already healthy `LAB00003` client package from the local distribution point
on `LABZ1-CM01.test.gell.one`.

## Considered approaches

1. Run an ad-hoc MECM console command on CM01. This is quick but is not
   replayable or available to a later lab deployment.
2. Add a generic PowerShell execution facility to the Agent. This is flexible
   but creates an unnecessary high-privilege remote-command interface.
3. Add a typed, narrow Agent work item. This is the selected approach: it is
   rerunnable, audited in Agent work history, and permits only the intended LAB
   site/subnet/DP association.

## Contract

The controller accepts only a request with:

- `site_code`: `LAB`
- `client_subnet`: `192.168.16.0/24`
- `boundary_group_name`: `LABZ1 Client Network`
- `distribution_point_fqdn`: `LABZ1-CM01.test.gell.one`

The Agent validates the same values before loading the Configuration Manager
PowerShell module. It creates or reuses the `IPSubnet` boundary, creates or
reuses the named boundary group, associates that boundary and the local DP,
then reads the three associations back. It never deletes a boundary, group,
DP, package, site system, or content.

## Idempotence and failure behavior

The task treats existing matching objects as success. If an existing boundary
or group conflicts with the fixed contract, it returns a failed work item with
the observed values and does not mutate it. The result includes the boundary,
group, and DP readback plus an explicit `changed` flag.

## Verification

Controller tests reject extra fields and non-LAB/non-`192.168.16.0/24` inputs.
Agent contract tests reject malformed or broadened requests and require the
fixed PowerShell resource. The resource is parsed and checked to prohibit
delete/remove verbs. Live proof is a subsequent content-location diagnostic
returning the matching boundary, group, and DP, followed by client CcmExec
registration evidence.
