# Rust controller production-candidate PoC tracker

User requested goal tracking on 2026-09-05. This tracker distinguishes a locally verified production candidate from authorization to deploy or from proven production readiness. The approved replacement design remains authoritative.

## Authority boundary

- Build and verify on this macOS in the isolated worktree with Astra subagents.
- Real Proxmox and production controller `192.168.2.4` are read-only. No queue claims, sweeps, deployments, schema writes, VM operations, agent work, or tenant actions there.
- Disposable non-production mutation proof and production cutover each require separate approval. A goal to become ready does not grant those approvals.
- RustedOutClient is excluded. Reliability, OOBE, remote control, operator UX, adoption and other downstream product work wait for Rust contracts to stabilize.

## Gates

| Gate | Current evidence / next action |
| --- | --- |
| Durable foundation, scheduler and synthetic adapter | Prior local acceptance retained in foundation evidence; not proof of live replacement |
| Native fake-PVE identity/clone/config/start | Accepted native-controller evidence; no live mutation authority |
| Authenticated cluster observation | Accepted authenticated-observation evidence |
| Selected node/network visibility contracts | Accepted node-network-visibility evidence |
| Selected-node service integration | Accepted for c429807443f80b74530cbd10af334e50e97b695b; see selected-node-service-acceptance.md for exact macOS/Linux execution limits |
| Artifact and OSDeploy input contracts | Artifact identity/byte-match library accepted at 15fadd049ee7a52ea61a2edfe6789a15a6929628. OSDeploy fixed-stage/input contract accepted at 6ee5bf08eed76b20e17056df9b2d27bbc1581113 on macOS/Linux; see osdeploy-contract-acceptance.md. Durable workflow binding remains pending |
| Callback compatibility and server-side binding | Pending run/attempt/identity binding, replay, duplicate, conflict and late-result proofs |
| Native OSDeploy vertical slice | Provisioning libraries accepted on macOS/Linux through849766df50cc13fb993925d5d11a1ee912ff837c after complete phase review and focused repair. Linux297 runtime/support+24docs;25DBtests explicitly filtered. See provisioning-port-acceptance.md for exactsource/artifact and limits. Next: durable guarded sixteen-stage registration, execution/callback/service integration and recovery. No service acceptance follows from this fake chain |
| Agent/build-host, CloudOSD and legacy WinPE | Pending compatible contracts and workflow proofs |
| Python/Rust single-writer transition | Pending local dual-executor generation fencing and handoff proofs |
| Release-candidate assurance | Pending full differential/fault/rebuild/backup-restore/rollback evidence, exact immutable artifacts, remaining contract suites and independent readiness review |
| Disposable non-production proof | Separate authorization required; exact artifact, isolated stack and sacrificial workflow targets |
| Production readiness decision | Requires local and non-production evidence plus remaining risks, rollback and operator acceptance; deployment/cutover separately approved |

## Current resource ruling

The user approved clearing only this isolated worktree's rebuildable Rust cache after preserving its verified executable and logs. The historical pre-cleanup check showed 152,893,628 KiB available (about 146 GiB); the later accepted OSDeploy Linux gate recorded 119,637,000 KiB, still above the 18 GiB artifact-start guard. These are past observations, not a live free-space guarantee. Cleanup was not performed. Keep evidence, accepted images, unrelated worktrees and Docker resources intact. Recheck headroom before the next artifact build.

## Active implementation checkpoint

The rich provisioning plan separates broad observed/declaration values from executable compatibility. Its next request layer must retain the original source and target before-state, bind the full workflow and individual operation separately, preserve the original dispatch across recovery, and never turn missing receipts into resend permission. PE start and installed-disk start remain distinct operations.

Main's pinned upstream check corrected the new resize worker convention and added its before-config digest. Configuring an existing disk's serial must preserve capacity, not model informational size as growth. Desired disk serials longer than 20 ASCII bytes must be rejected at request construction and downward workflow conversion before any clone; never silently truncated. These are conservative contracts for the next local phase, not proof of compatibility with the installed production PVE version. Task 1's broad validated fact representation remains unchanged.

Upstream references pinned to qemu-server commit `6c0127e612f6c576888a13f9bfb30874911b804d`: [resize endpoint implementation](https://github.com/proxmox/qemu-server/blob/6c0127e612f6c576888a13f9bfb30874911b804d/src/PVE/API2/Qemu.pm#L5558-L5685) and [disk size/serial schema](https://github.com/proxmox/qemu-server/blob/6c0127e612f6c576888a13f9bfb30874911b804d/src/PVE/QemuServer/Drive.pm#L181-L235).

## Completion truth

Next implementation target is a service-driven OSDeploy vertical slice built on the existing journal/scheduler, not another independent execution stack. Source research identifies distinct PE/disk start operations, durable callback waiting beyond worker leases, disk-capacity versus free-space separation, explicit firmware/media/identity representation, and legacy callback transaction boundaries. Nominal CloudOSD run-detail and bootstrap-claim GETs can write; do not use them as production read-only probes.

The local vertical sequence is fixed at sixteen stages, including actual disk growth, both media configurations, conditional force-stop with retained grace-timeout history, all base guest actions, independently authenticated agent heartbeat and host-side QGA. Current client response-loss recovery is a separate limitation: a lost successful `/next` claim response cannot be treated as permission to redeliver an executable action. Full service/fake-world restart proof remains mandatory after the input phase.

Do not mark this goal complete from test totals, fake-PVE success, HTTP health, or OS installation alone. Report which readiness level is proven and what requires external approval. Native operational readiness, OOBE, enrollment, ESP and usable-device acceptance remain separate facts. This PoC goal does not imply the full replacement programme, production cutover, or Ansible retirement is complete.
