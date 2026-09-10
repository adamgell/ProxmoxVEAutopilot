# Rust controller production-readiness assessment

Assessment date: 2026-09-10  
Assessment source: isolated worktree `codex/rust-controller-design`  
Production and `192.168.2.4`: read-only throughout

## Decision

**Not production-ready and not approved for cutover.** The Rust controller is a strong local proof-of-concept candidate, but the evidence does not yet establish a safe replacement for the production controller or Ansible execution path.

## Proven locally

- Current Rust source is committed through `8f07247b41e738923ee61e610d4400102b208637`; subsequent tracker-only documentation commits do not change controller behavior.
- The final macOS four-package regression at `557bd12` passed 665 tests, with 3 intentionally ignored, under a bounded supervisor.
- An exact-source Linux/amd64 image was built from `8f07247` and inspected as `sha256:82b70282d5c4278b04ed15b67684fdb840235f22b6c27d759047dda59f855e2b`.
- The isolated Linux Compose proof passed the three-worker synthetic scheduling, PostgreSQL, cancellation/recovery, native adapter, and descendant-cleanup scenarios (12 lifecycle plus 4 native PostgreSQL tests).
- Python compatibility-side contracts passed with explicit Python 3.12: producer 2/2, proof-wait 5/5, and proof-coordination 1/1.
- Rust formatting, strict offline/locked Clippy, focused protocol checks, and bounded child/process cleanup evidence passed for the accepted local source changes.

## Not yet proven

- Full owned-v1 Linux four-package database/runtime qualification from the current source. The Compose proof is narrower and uses a different fixture topology.
- Complete sixteen-stage OSDeploy service execution, including callback, guest-agent, host-side QGA, media, firmware, disk-growth, and terminal/recovery behavior.
- Independent-process restart, crash, restore, rollback, and response-loss behavior across the complete service workflow.
- Compatibility with every retained Python/Ansible ingress and callback contract under a real dual-executor handoff.
- Actual quiescence and generation fencing of the current Python/Ansible writer before Rust becomes the sole mutation writer.
- Backup/restore rehearsal, immutable release artifact/export verification, operator acceptance, rollback window, and non-production sacrificial workflow proof.
- OOBE, ESP, enrollment, usable-device, and production operational acceptance. These remain separate from controller unit and fixture tests.

## Authorization gates

The owned-v1 Linux runner requires explicit approval of the reviewed resource and observation profile (4 GiB PostgreSQL tmpfs, 512 MiB reserve, 6 GiB PostgreSQL cap, 4 GiB runner cap, private cgroup namespace, no swap, and the stated smoke/final-catalog rules). No implementation or execution of that profile is authorized yet.

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
