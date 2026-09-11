# Durable fake-PVE IPC recovery design

Status: reviewed design baseline; implementation is authorized only inside this isolated local PoC worktree.

## Purpose and boundary

The current `NativeFakePve` is process-local. It supports logical controller reconstruction, but it cannot prove worker-death takeover because independent processes cannot share its VM/task state. This design adds only a local test fixture for the reachable `Clone -> DiskCapacity -> ConfigurePe` prefix. It does not widen the production PVE transport, add callback/session APIs, contact real Proxmox, or authorize deployment.

## Process model

Use a supervisor, a durable fake-PVE daemon, and controller workers A/B as separate processes. PostgreSQL remains the controller journal. The daemon owns a versioned, bounded Unix-socket protocol for the three admitted fake mutations and read-only observations. A separate supervisor channel controls barriers and scripted outcomes; controller clients cannot reset the fixture or complete tasks.

## Durable state and commit rules

Use a checksummed append-only fixture log as the first implementation (one length-delimited canonical record plus checksum per line, followed by `fdatasync` before acknowledgement). Records contain VM state, task state, pending mutations, incarnation/sequence counters, attempted operation IDs, submission records, canonical request digests, admission outcomes, receipts/UPIDs, and resulting world transitions. A received submission attempt is recorded before an acceptance/rejection reply; accepted effects are then recorded with their world/task transition before the success reply. Recovery refuses the fixture if any record is corrupt or if a trailing record is partial; it never silently truncates and continues. Submission attempts remain distinct from accepted effects: duplicate attempts are recorded and rejected rather than transparently deduplicated.

## Required recovery proof

The supervisor starts worker A, releases an explicit barrier, terminates it, and starts worker B against the same database and fixture socket. The proof must cover: dispatch-before-IPC death (zero fixture submissions, uncertain durable dispatch); accepted-effect-before-response death (exactly one submission/effect, no resend); receipt-captured restart (same receipt and original attempt, no resend); normal three-stage progression; no-growth zero-submission behavior; fixture-daemon restart preserving world, tasks and counts; and an injected duplicate attempt visible in the ledger and rejected. `StartPe` remains unreachable, so the acceptance claim is limited to independent-process recovery of the three-stage prefix.

## Controller integration seam

The current controller and consuming dispatch permit are concretely bound to `NativeFakePve`, and the sealed provisioning capability is crate-private. The next implementation must add an explicitly opt-in fixture capability seam, then prove one typed `Clone` request round-trip that yields a daemon-owned validated effect and the existing receipt shape. The controller must not send caller-selected raw before/after state over IPC. Until that typed seam and receipt capture exist, this daemon remains a protocol/ledger proof only, not controller recovery evidence.

The required seam spans the controller constructor/ownership path, collection and send helpers, the postgres-store dispatch permit, and its checkpoint method; it cannot be supplied by a downstream integration-test adapter alone. The fixture world must also gain the identity and preflight facts needed to construct a real typed `Clone` receipt. These are the minimum refactor inputs before A/B worker recovery is attempted.

## Evidence and approval

Retain source/image bindings, process IDs, barrier events, durable before/after snapshots, fixture submission ledger, receipts, and bounded shutdown/descendant-cleanup records. This is a local test-infrastructure design only. Passing the resulting tests would establish independent-process recovery evidence for the supported three-stage prefix; it would not close full OSDeploy, callback reachability, Python/Ansible handoff, or production readiness.
