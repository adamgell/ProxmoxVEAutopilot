# Local Rust foundation acceptance

Accepted source: `a7d94433a3ca1ee882a65195311f2337df7590b6`.

All nine foundation tasks passed independent review. The whole-branch review found three integration defects; the final coordinated repair and scoped review closed immutable plan binding, scheduler-owned lifecycle ingestion, and post-collection evidence-clock handling.

## Executed proof

- macOS default-concurrency workspace: 171 Rust tests and seven compile-fail doctests passed.
- Eight Python proof/producer-drift tests, formatting, and Clippy with warnings denied passed.
- Cargo-deny passed with documented duplicate-dependency warnings.
- All 78 selected build inputs were checksum-verified inside the final Linux image.
- Actual Linux AMD64 release build and trusted Ansible-runtime preflight passed.
- Linux PVE: 24 unit/HTTP tests and five network-denial tests passed.
- Linux adapter: 12 lifecycle/process and four native PostgreSQL tests passed.
- Three separate workers passed the Compose cap, ownership, cancellation, and actual lease-expiry recovery proof.
- Standalone Linux store/scheduler test binaries compiled; their separate integration execution is covered by the macOS suite and their assembled Linux behavior by Compose.

Image: `sha256:8f3c9ed37b701b9728601fb348a6bd703b8d955f05731544c91df8404ffe37fe`.

Linux service binary SHA-256: `8d2ec35b8c21330011ee255e0dd393d033dc52824bbaa3990d9d3d65b6dcdc16`.

Full local commands and log hashes are retained in `.superpowers/sdd/2026-09-04-rust-controller-foundation/final-artifact-a7d9443.md` in the isolated worktree. Earlier tracked proof remains historical evidence at its named revisions.

## Limits

This accepts the local foundation, not a production replacement. Execution accepts one pinned synthetic Ansible contract. Native PVE mutation, full OSDeploy/CloudOSD workflows, general plan intake, external outbox delivery, restore rehearsal, non-production mutation proof, and cutover remain later work. Hosted CI is configured but has not executed. No image was published and no production system was changed.

An earlier default-concurrency PostgreSQL-store test run failed transiently; focused and subsequent full runs passed. Its root cause remains unconfirmed and no speculative harness fix is claimed. Disposable proof containers, networks, and volumes were removed; reusable local images/build caches remain.
