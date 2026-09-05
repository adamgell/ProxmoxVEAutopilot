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
| Artifact and OSDeploy input contracts | Pure artifact identity/byte-match plan prepared; implementation and OSDeploy input/plan binding pending |
| Callback compatibility and server-side binding | Pending run/attempt/identity binding, replay, duplicate, conflict and late-result proofs |
| Native OSDeploy vertical slice | Pending typed media/boot/power/QGA and fake-client end-to-end recovery proofs |
| Agent/build-host, CloudOSD and legacy WinPE | Pending compatible contracts and workflow proofs |
| Python/Rust single-writer transition | Pending local dual-executor generation fencing and handoff proofs |
| Release-candidate assurance | Pending full differential/fault/rebuild/backup-restore/rollback evidence, exact immutable artifacts, remaining contract suites and independent readiness review |
| Disposable non-production proof | Separate authorization required; exact artifact, isolated stack and sacrificial workflow targets |
| Production readiness decision | Requires local and non-production evidence plus remaining risks, rollback and operator acceptance; deployment/cutover separately approved |

## Current resource ruling

The user approved clearing only this isolated worktree's rebuildable Rust cache after preserving its verified executable and logs. A fresh pre-cleanup check showed 152,893,628 KiB available (about 146 GiB), above the 18 GiB artifact-start guard. Cleanup is unnecessary and was not performed. Keep evidence, accepted images, unrelated worktrees and Docker resources intact. Recheck headroom before the artifact build.

## Completion truth

Do not mark this goal complete from test totals, fake-PVE success, HTTP health, or OS installation alone. Report which readiness level is proven and what requires external approval. Native operational readiness, OOBE, enrollment, ESP and usable-device acceptance remain separate facts. This PoC goal does not imply the full replacement programme, production cutover, or Ansible retirement is complete.
