# LABZ1 Client Network Repair Design

## Goal

Restore the fixed LABZ1 network configuration for the approved MECM test
client through a typed Autopilot Agent action, then make the existing
SHA-verified client-install flow retryable.

## Observed fault

The new `setup_cm_client_install` work item failed before running the MECM
client installer because the client could not authenticate to the private
`\\LABZ1-DC02\\SetupCm` share.

Read-only evidence identifies the configuration drift:

- `RING0IVY24-01` is attached to the Proxmox `labz1` VNet but reports
  `192.168.2.95` and DNS server `192.168.2.6`.
- The `labz1` VNet is configured as `192.168.16.0/24` with gateway
  `192.168.16.1`.
- `LABZ1-DC02` is attached to that same VNet and reports `192.168.16.12`.
- From the client, domain-controller discovery returns `ERROR_NO_SUCH_DOMAIN`
  and secure-channel verification returns `ERROR_NO_LOGON_SERVERS`.

This is a stale guest static-network configuration, not a MECM installer,
source-ACL, or module-artifact failure.

## Options considered

1. Use a generic remote PowerShell facility. This would be fast for this
   one repair but creates an unrestricted remote-command control plane and
   is therefore rejected.
2. Use QEMU Guest Agent to set the guest network. QGA is deliberately limited
   to bootstrap and read-only diagnosis in this workflow, and the DC QGA is
   currently unavailable. This is rejected.
3. Add a fixed, typed Agent work item. This is the chosen approach: it gives
   the controller only one recoverable LABZ1 repair while keeping Windows-side
   mutation in Autopilot Agent.

## Chosen design

Add `setup_cm_client_network_repair` and
`POST /api/setup-cm/v1/agents/{agent_id}/client-network-repair`.

The controller accepts no request body and permits only
`agent-ring0ivy24-01`. The Agent accepts no caller-provided IP address,
gateway, DNS server, interface name, command, or script. It finds only the
Ethernet adapter whose MAC is `BC-24-11-9C-43-E6`, assigns the fixed address
`192.168.16.103/24`, fixed default gateway `192.168.16.1`, and fixed DNS
server `192.168.16.12`, then flushes DNS.

The work result reports only non-sensitive readback: adapter MAC, IPv4
address, prefix length, gateway, configured DNS servers, domain-controller
lookup status, and whether TCP 53/445 to DC02 succeeds. It deliberately does
not return credentials, DNS cache contents, or arbitrary command output.

## Failure handling

The Agent fails without changing another adapter when the fixed MAC is absent,
ambiguous, disabled, or lacks an IPv4 configuration object. It records a
bounded error when the post-change readback differs from the fixed contract or
when DC discovery/SMB remain unavailable. The controller does not queue a
client install automatically: the operator must inspect repair evidence first.

## Validation

Endpoint tests prove only the approved agent can queue the body-free work.
Agent contract tests prove the kind is advertised and its fixed constants are
present in the embedded script. The Windows Agent build and controller suites
remain release gates. After the normal tagged release, the live repair must
complete with exact network readback before one fresh typed MECM client-install
work item is queued and independently verified from client and server.
