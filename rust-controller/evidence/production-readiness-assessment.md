# Rust controller production-readiness assessment

Assessment date: 2026-09-10  
Assessment source: isolated worktree `codex/rust-controller-design`  
Production and `192.168.2.4`: read-only throughout

## Decision

**Not production-ready and not approved for cutover.** The Rust controller is a strong local proof-of-concept candidate, but the evidence does not yet establish a safe replacement for the production controller or Ansible execution path.

## Proven locally

- The changed-source Linux qualification evidence covers Rust source `ac03e96caa70fadd9a572f5c206d03e1ec1e0121`, with launcher binding `e30454e72b132055dcf8aba926a182957ac19f99`; later commits contain only evidence/documentation updates.
- The final macOS four-package regression at `557bd12` passed 665 tests, with 3 intentionally ignored, under a bounded supervisor.
- The prior full Linux/amd64 qualification image was built from `49b0877` and inspected as `sha256:d2a7622623953ba9342e11ed1d6df00d8dc62c1d62713bdf94e70f9135cb459c`. After the guest-action change, a fresh exact-source image was built as `sha256:6c2c32025ce3a2d610c5f3be18d27b1f78a60e6b2e60257023ba8c36503fcdda`; the owned-v1 smoke and changed-source full Linux gates both passed. Full evidence is retained in `restart-task9-owned-full-guest-1/`.
- The changed-source full run completed with exit 0 under the bounded supervisor. Its 23 test-result groups reported zero failures, with zero cgroup memory OOM/event counters and exact source/image/launcher bindings.
- The isolated Linux Compose proof passed the three-worker synthetic scheduling, PostgreSQL, cancellation/recovery, native adapter, and descendant-cleanup scenarios (12 lifecycle plus 4 native PostgreSQL tests).
- Python compatibility-side contracts passed with explicit Python 3.12: producer 2/2, proof-wait 5/5, and proof-coordination 1/1.
- Rust formatting, strict offline/locked Clippy, focused protocol checks, and bounded child/process cleanup evidence passed for the accepted local source changes.
- Native real-PostgreSQL restart/recovery cases passed for restart fencing, unknown-state reconciliation, receipt reload, aged-infrastructure continuation, and response-loss handling; the combined record is `restart-recovery-1/acceptance.md`.
- The guest-action identity slice is locally verified, but authoritative callback/session decisions are missing; no authenticated callback exposure or result-ingest claim is made.
- A test-only durable fixture attempt ledger is verified at `961ef7283cf46ba2789c3ac2b68e8df48e99fe70` (four focused tests plus targeted strict Clippy). It preserves duplicate attempts and fails closed on corrupt/partial records, but is not yet an IPC daemon or accepted-world/effect store.
- Accepted synthetic VM world transitions are now durably bound and replayed by the same test-only ledger at `0c157d86caa8074994f11e3837128da3cb9d30dc` (six focused tests, formatting, and targeted strict Clippy). This remains fixture infrastructure only; it does not prove IPC, task/UPID persistence, or independent worker recovery.
- A bounded test-only Unix-socket fixture daemon is verified at `8e7409633c48e9b5255ecd07d2c6b2aaecfe643b` (six ledger tests, two subprocess protocol tests, formatting, and targeted strict Clippy). It proves client/supervisor control separation and bounded request handling, but not peer authentication, daemon restart/rebind, controller wiring, task/UPID state, or independent worker recovery.
- Stale-endpoint and daemon-rebind behavior is verified at `a1bfd719e3462bf409dda73555515faf194ae8fe` (four daemon tests, formatting, and targeted strict Clippy). Direct restart refuses stale endpoints; supervisor-owned cleanup is required before rebind; corrupt ledgers and endpoint substitution fail closed. This still does not prove controller wiring, task/UPID state, or worker A/B recovery.
- Accepted-effect and per-VM world status are now available only through the test daemon protocol at `d0fed1cf530007f35e65432fdd70cd382c615dbd` (six ledger tests, five daemon tests, formatting, and targeted strict Clippy). Effects commit before replies and recover across daemon restart; this still does not prove controller wiring, task/UPID state, or independent worker recovery.
- Controller integration remains a distinct open seam: the default `OsDeployController` path remains concretely tied to `NativeFakePve`, while the opt-in fixture capability seam is now recorded at `f78bb5f`; the fixture protocol still accepts caller-supplied synthetic state and has no typed end-to-end mutation path. The next proof must establish daemon-owned facts and one typed Clone round-trip before any A/B worker-death claim.
- A default-disabled `fixture-ipc` feature now exports reusable test daemon/ledger support (`25e4f56`), and typed Clone request/receipt envelopes are verified at `9ecd6c5`; a macOS Unix-socket timeout portability correction is `4a29b46`. These remain message/fixture contracts only; the typed end-to-end Clone path is still open.
- The sealed `fixture-ipc::ControllerFixturePort` checkpoint seam is verified at `3f2f4ee` (trait-object barrier runtime test, observer compile-fail contract, 19 feature docs, default check, formatting, and strict Clippy). It preserves the existing NativeFakePve checkpoint but does not yet adapt dispatch permits or provide typed IPC provisioning.
- The consuming dispatch permit seam is committed at `e34eacc`, with Arc caller compatibility fixed in `e8bdd27` and `feaf463`. Postgres-store all-target compilation, 22 doctests, and strict Clippy pass. The runtime database suite remains to be rerun; fixture IPC submission and daemon-backed provisioning facts are still open.
- The opt-in controller ownership/helper adaptation is now committed at `f78bb5f`: `operation-controller/fixture-ipc` stores the sealed `ControllerFixturePort` and exposes `new_fixture`, while the default NativeFakePve constructor is preserved. Feature/default checks, feature library tests and doctests, formatting, and strict Clippy pass. This does not establish daemon-backed provisioning capabilities, a controller Clone round-trip, or worker recovery.
- Commit `1254a94` adds a digest-bound daemon `accepted_effect` read that returns only durably committed effects and survives daemon restart; malformed bindings and attempt-only/absent state remain fail-closed. This strengthens recovery evidence but remains fixture-log substrate, not a `ProvisioningFakePort` implementation or controller/process takeover proof.
- Commit `488cf90` adds the feature-gated bounded `FixtureReadClient` for status, world, and accepted-effect observations. It is strictly read-only, deadline-bounded across connect/write/read, and validates reply framing and identity. This is reusable transport substrate; daemon-owned preflight/task facts, mutation capability, controller Clone round-trip, and independent takeover remain unproven.
- Commit `f0abd54` pins the negative capability boundary: `FixtureReadClient` cannot satisfy any provisioning, controller-fixture, or preflight trait, and the seam plan maps the missing daemon-owned facts required for a truthful implementation. No placeholder adapter was added; the typed controller Clone and process-recovery gates remain open.
- Commit `4c7f6c7` adds a bounded, supervisor-seeded inventory snapshot prefix with explicit unavailable state, strict validation, restart stability, and immutable startup observations. Its limitations are explicit in `fixture-snapshot-protocol.md`: it is historical inventory, not current Proxmox state, and lacks storage/network/task/UPID/full identity/configuration facts. It does not implement provisioning capability or advance controller acceptance.
- Commit `2f1cfe8` adds a typed, freshness-checked projection of the historical inventory while preserving fixture/node identity and source timestamp. It deliberately rejects mismatches and leaves absent power/configuration/task facts unavailable, so it does not advance preflight, dispatch, controller Clone, or worker-recovery readiness.
- Commit `aa2d278` adds a strictly read-only, identity-bound task/UPID observation prefix with explicit lifecycle states and restart-stable seeded reads. It is loaded once per daemon lifetime and does not execute or persist task transitions; the production UPID parser, mutation capability, controller Clone round-trip, and process takeover gates remain unproven.
- Commit `76d5e01` adds a separate feature-gated `FixtureMutationClient` for one supervisor-seeded, exact-request Clone. Attempt-before-check, daemon-generated UPID, and sync-before-reply receipt/effect persistence are verified across restart. This proves a bounded synthetic socket mutation, not full provisioning discovery, task progression, controller integration, or independent worker recovery.
- Commit `1a91843` executes the real Clone preflight evaluator with each required observation family removed in turn, proving incomplete daemon reads close admission and that absent target power is not invented when target absence is independently established. The companion mapping defines the remaining daemon seed, replay, checkpoint, and PostgreSQL/controller acceptance steps. This is a prerequisite gate, not controller integration or production readiness.
- Commit `0108405` adds a strict supervisor `FixtureCloneReads` seed schema for node, storage, bridges, and complete inventory/identity facts, preserving timestamps and explicit errors. It is validated and bounded but not yet loaded by the daemon or projected into provisioning capability; full config/media/task facts and controller acceptance remain open.
- Commit `716c149` exposes those reads through daemon startup and `FixtureReadClient::clone_reads`, with identity-bound requests, strict response caps, restart stability, and no ledger writes. Serial feature/default suites, Clippy, and formatting pass; a parallel run reproduced an existing `NotConnected` test flake, so concurrency qualification remains open. This does not implement the provisioning port or controller Clone round-trip.
- Commit `84c7213` adds a strict identity-bound seed/transport for source and target provisioning configs, separate power reads, and deployment/driver media. It preserves timestamps and explicit errors and does not infer absence or grant capability. Its populated-schema and restart/client proofs are not yet complete, so it is not a qualified provisioning adapter or controller integration.
- Commit `8707163` fixes the reproduced macOS parallel framing flake by normalizing accepted Unix sockets to blocking mode before applying existing bounded deadlines. Thirty consecutive 16-thread runs and updated serial/parallel feature plus default parallel suites pass. This is a transport reliability qualification only; Linux and controller/process recovery gates remain open.
- Astra’s review confirms this is a multi-layer refactor (controller ownership/constructor, collection/send helpers, postgres-store permit, and checkpoint), and the minimal fixture world lacks identity/preflight facts for a truthful typed Clone receipt. No partial adapter was committed; the required seam is recorded in `durable-fake-pve-ipc-recovery-design.md`.
- Independent-process recovery is explicitly unproven. Existing Clone → DiskCapacity → ConfigurePe and native restart tests reconstruct controller objects in one process; `NativeFakePve` is in-memory, so a true worker-death/takeover proof requires a separately approved durable deterministic fake-PVE service or IPC fixture.

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
Decision owner: Adam, after the remaining service, independent-process handoff/recovery, artifact/rollback, non-production, and operator-acceptance evidence is complete.

## Evidence index

- [macOS regression receipt](../../restart-task9-macos-full-2.receipt.json)
- [Linux build receipt](../../restart-task9-linux-build-5.receipt.json)
- [Linux Compose output](../../restart-task9-linux-compose-1.stdout.log)
- [Python contract receipt](../../restart-task9-python-contracts-3.receipt.json)
- [Readiness tracker](poc-readiness-tracker.md)
- [Owned Linux full-run evidence](restart-task9-owned-full-4/)
- [Changed-source owned Linux full-run evidence](restart-task9-owned-full-guest-1/)
- [Restart/recovery and compatibility evidence](restart-recovery-1/acceptance.md)
