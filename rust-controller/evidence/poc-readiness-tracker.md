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
| Native OSDeploy vertical slice | Next: sibling rich PVE facts/requests/evaluators and shared fake execution in docs/superpowers/plans/2026-09-05-rust-provisioning-port.md. Actual service-driven sixteen-stage workflow and fake-client recovery proof remain pending |
| Agent/build-host, CloudOSD and legacy WinPE | Pending compatible contracts and workflow proofs |
| Python/Rust single-writer transition | Pending local dual-executor generation fencing and handoff proofs |
| Release-candidate assurance | Pending full differential/fault/rebuild/backup-restore/rollback evidence, exact immutable artifacts, remaining contract suites and independent readiness review |
| Disposable non-production proof | Separate authorization required; exact artifact, isolated stack and sacrificial workflow targets |
| Production readiness decision | Requires local and non-production evidence plus remaining risks, rollback and operator acceptance; deployment/cutover separately approved |

## Current resource ruling

The user approved clearing only this isolated worktree's rebuildable Rust cache after preserving its verified executable and logs. A fresh pre-cleanup check showed 152,893,628 KiB available (about 146 GiB), above the 18 GiB artifact-start guard. Cleanup is unnecessary and was not performed. Keep evidence, accepted images, unrelated worktrees and Docker resources intact. Recheck headroom before the artifact build.

## Completion truth

Next implementation target is a service-driven OSDeploy vertical slice built on the existing journal/scheduler, not another independent execution stack. Source research identifies distinct PE/disk start operations, durable callback waiting beyond worker leases, disk-capacity versus free-space separation, explicit firmware/media/identity representation, and legacy callback transaction boundaries. Nominal CloudOSD run-detail and bootstrap-claim GETs can write; do not use them as production read-only probes.

The local vertical sequence is fixed at sixteen stages, including actual disk growth, both media configurations, conditional force-stop with retained grace-timeout history, all base guest actions, independently authenticated agent heartbeat and host-side QGA. Current client response-loss recovery is a separate limitation: a lost successful `/next` claim response cannot be treated as permission to redeliver an executable action. Full service/fake-world restart proof remains mandatory after the input phase.

Do not mark this goal complete from test totals, fake-PVE success, HTTP health, or OS installation alone. Report which readiness level is proven and what requires external approval. Native operational readiness, OOBE, enrollment, ESP and usable-device acceptance remain separate facts. This PoC goal does not imply the full replacement programme, production cutover, or Ansible retirement is complete.
