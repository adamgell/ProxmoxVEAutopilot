# Durable fake-PVE IPC recovery design

Status: design only; implementation requires review and separate approval.

## Purpose and boundary

The current `NativeFakePve` is process-local. It supports logical controller reconstruction, but it cannot prove worker-death takeover because independent processes cannot share its VM/task state. This design adds only a local test fixture for the reachable `Clone -> DiskCapacity -> ConfigurePe` prefix. It does not widen the production PVE transport, add callback/session APIs, contact real Proxmox, or authorize deployment.

## Process model

Use a supervisor, a durable fake-PVE daemon, and controller workers A/B as separate processes. PostgreSQL remains the controller journal. The daemon owns a versioned, bounded Unix-socket protocol for the three admitted fake mutations and read-only observations. A separate supervisor channel controls barriers and scripted outcomes; controller clients cannot reset the fixture or complete tasks.

## Durable state and commit rules

Persist a checksummed append-only fixture log (or an equivalent transactional store) containing VM state, task state, pending mutations, incarnation/sequence counters, attempted operation IDs, submission records, canonical request digests, admission outcomes, receipts/UPIDs, and resulting world transitions. A mutation reply is sent only after its record is durably committed. Corrupt or incomplete records fail closed. Submission attempts remain distinct from accepted effects: duplicate attempts are recorded and rejected rather than transparently deduplicated.

## Required recovery proof

The supervisor starts worker A, releases an explicit barrier, terminates it, and starts worker B against the same database and fixture socket. The proof must cover: dispatch-before-IPC death (zero fixture submissions, uncertain durable dispatch); accepted-effect-before-response death (exactly one submission/effect, no resend); receipt-captured restart (same receipt and original attempt, no resend); normal three-stage progression; no-growth zero-submission behavior; fixture-daemon restart preserving world, tasks and counts; and an injected duplicate attempt visible in the ledger and rejected. `StartPe` remains unreachable, so the acceptance claim is limited to independent-process recovery of the three-stage prefix.

## Evidence and approval

Retain source/image bindings, process IDs, barrier events, durable before/after snapshots, fixture submission ledger, receipts, and bounded shutdown/descendant-cleanup records. This is a local test-infrastructure design only. Implementation should select the persistence mechanism and receive review before code changes; passing it would close the process-boundary recovery gate, not full OSDeploy, callback reachability, Python/Ansible handoff, or production readiness.
