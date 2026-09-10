# Rust controller production-readiness assessment

Assessment date: 2026-09-10  
Assessment source: isolated worktree `codex/rust-controller-design`  
Production and `192.168.2.4`: read-only throughout

## Decision

**Not production-ready and not approved for cutover.** The Rust controller is a strong local proof-of-concept candidate, but the evidence does not yet establish a safe replacement for the production controller or Ansible execution path.

## Proven locally

- Current Rust source is committed through `49b08779edb1d9d5f3c2b6247cbce77bd6f33751`; launcher-only qualification fixes are committed through `2d977b0b8727ff3aded6277a0ddc44aa0b4ee5d7`.
- The final macOS four-package regression at `557bd12` passed 665 tests, with 3 intentionally ignored, under a bounded supervisor.
- An exact-source Linux/amd64 image was built from `49b0877` and inspected as `sha256:d2a7622623953ba9342e11ed1d6df00d8dc62c1d62713bdf94e70f9135cb459c`.
- The owned-v1 Linux full run completed with exit 0 under the bounded 1,820-second supervisor. All executed Rust targets reported zero failures; evidence is retained in `restart-task9-owned-full-4/`.
- The isolated Linux Compose proof passed the three-worker synthetic scheduling, PostgreSQL, cancellation/recovery, native adapter, and descendant-cleanup scenarios (12 lifecycle plus 4 native PostgreSQL tests).
- Python compatibility-side contracts passed with explicit Python 3.12: producer 2/2, proof-wait 5/5, and proof-coordination 1/1.
- Rust formatting, strict offline/locked Clippy, focused protocol checks, and bounded child/process cleanup evidence passed for the accepted local source changes.
- Native real-PostgreSQL restart/recovery cases passed for restart fencing, unknown-state reconciliation, receipt reload, aged-infrastructure continuation, and response-loss handling; the combined record is `restart-recovery-1/acceptance.md`.

## Not yet proven

- Complete sixteen-stage OSDeploy service execution, including callback, guest-agent, host-side QGA, media, firmware, disk-growth, and terminal/recovery behavior.
- Independent-process restart, crash, restore, rollback, and response-loss behavior across the complete service workflow.
- Compatibility with every retained Python/Ansible ingress and callback contract under a real dual-executor handoff.
- Actual quiescence and generation fencing of the current Python/Ansible writer before Rust becomes the sole mutation writer.
- Backup/restore rehearsal, immutable release artifact/export verification, operator acceptance, rollback window, and non-production sacrificial workflow proof.
- OOBE, ESP, enrollment, usable-device, and production operational acceptance. These remain separate from controller unit and fixture tests.

## Authorization gates

The owned-v1 Linux runner was executed under the user's explicit local-only approval with the reviewed resource and observation profile (4 GiB PostgreSQL tmpfs, 512 MiB reserve, 6 GiB PostgreSQL cap, 4 GiB runner cap, private cgroup namespace, no swap, and the stated smoke/final-catalog rules). That approval does not extend to production mutation or deployment.

Production access, deployment, mutation, Ansible retirement, and cutover each require separate approval. No evidence in this assessment grants those permissions.

## Readiness level

Current level: **local PoC / release-candidate preparation**.  
Target level: **production candidate pending external gates**.  
Decision owner: Adam, after the missing Linux, service, handoff, recovery, artifact, rollback, and operator-acceptance evidence is complete.

## Evidence index

- [macOS regression receipt](restart-task9-macos-full-2.receipt.json)
- [Linux build receipt](restart-task9-linux-build-5.receipt.json)
- [Linux Compose output](restart-task9-linux-compose-1.stdout.log)
- [Python contract receipt](restart-task9-python-contracts-3.receipt.json)
- [Readiness tracker](poc-readiness-tracker.md)
- [Owned Linux full-run evidence](restart-task9-owned-full-4/)
- [Restart/recovery and compatibility evidence](restart-recovery-1/acceptance.md)
