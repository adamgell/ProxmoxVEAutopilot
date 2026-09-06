# Owned Docker fixture storage prerequisite

This is a separate test-infrastructure repair required by the confirmed anonymous PGDATA volume leak. It does not change the Rust controller's production behavior, accept Task7, replace Task8, or narrow the full production-candidate goal. Implementation waits for Task7 independent acceptance and a concrete task brief pinned to an exact clean base.

## Evidence

Main's `.superpowers/sdd/2026-09-05-rust-osdeploy-durability/fixture-volume-leak-evidence.md` records a naturally observed owned container, exact Docker mount/unmount events and its detached anonymous volume remaining after container cleanup. The full `fixture-volume-leak-preflight.md` supplies source/caller/deadline analysis and qualifies historical Linux capacity evidence. Main read both completely and independently checked the current helper plus historical tuned PostgreSQL arguments.

## Selected requirements

- Prevent persistent anonymous PGDATA allocation for newly created Docker fixtures using the fixed compiled option `/var/lib/postgresql/data:rw,nosuid,nodev,size=1g,mode=0700` passed with `--tmpfs`. No caller/environment-selected path, options, size or mount.
- 1GiB is the first current-default validation candidate, not established adequacy. Require at least128MiB available at observed workload checkpoints, no observed ENOSPC/OOM, and all selected existing workload assertions and time bounds unchanged. Report sampled capacity honestly; do not claim sampling captures every possible future peak.
- Preserve PostgreSQL defaults, including existing durability settings. Do not reduce buffers/WAL, disable fsync/synchronous_commit/full_page_writes, force checkpoints for passing capacity, add a cgroup memory limit or silently enlarge tmpfs/timeouts. A failed adequacy test returns to main for a distinct decision.
- Preserve the cached image selector, `--pull=never`, local Unix Docker endpoint gate, generated exact name/label/ID ownership, loopback-only dynamic publish, and the pending cleanup guard established before create.
- Add storage admission before yielding the connection string: exact returned full container ID, exactly the fixed HostConfig.Tmpfs map, no binds/volumes/extra destination, and either empty Mounts or a single matching tmpfs entry. Reject missing/malformed/truncated/failed responses and wrong identity or configuration. Do not weaken existing ownership verification.
- Preserve setup30s, command3s, shared cleanup3s and existing bounded child cleanup allowances. New Docker reads use the existing owned process runner and remain within setup/outer budgets. No detached child or unbounded subprocess.
- Storage refusal must retain ordinary exact-container cleanup through existing identity guards; a bad mount never authorizes volume removal. Keep `rm --force <verified-id>` unchanged. Container absence alone is not proof that an unexpected volume was removed.
- The Linux receipt backend, frozen prior candidate gates, controller/store production modules, Cargo dependencies/lockfile and existing test business assertions remain unchanged.

## Permitted implementation scope

Primary paths are `rust-controller/proof_support/mod.rs` for fixed launch/storage admission and non-live unit tests, and new `rust-controller/crates/postgres-store/tests/docker_fixture_storage.rs` for dedicated owned live proofs. Keep the public Container API unchanged; any additional inspection interface is sanitized and test-only, with fixed commands/paths and no generic command or deletion interface. A narrow change to `proof_support/process.rs` requires a concrete necessity ruling before editing, not implied permission.

## Proof requirements

1. Compiled non-Docker behavioral RED for actual launch-argument selection lacking bounded tmpfs and ownership-valid persistent-volume storage being inadmissible. Do not create another leaking volume for RED. Then implement and obtain GREEN.
2. Exact negative cases for missing/wrong/unbounded tmpfs, bind/volume/extra mounts, malformed fields, failed/truncated output and wrong full ID; both admitted daemon tmpfs representations; preserve every existing ownership/cancellation/one-shot/hung-child cleanup assertion.
3. Dedicated explicitly selected Docker-only live tests with bounded setup and outer180s each: normal explicit cleanup, Drop and current-thread cancellation. At most two simultaneously owned fresh fixtures. No new live test body in the shared helper, to avoid multiplying real fixture probes across includers. Docker-only cases must not execute in the receipt-only Linux harness.
4. For those new exact-owned fixtures, verify mount configuration and actual tmpfs filesystem type/capacity; migrate and exercise committed writes, independent-pool reads and rollback while preserving defaults. Record only allowlisted settings, database sizes, filesystem capacity/free counters and bounded memory/OOM observations; never raw environment/connection secrets or data contents.
5. Confirm exact full-ID and generated-name absence after cleanup using successful bounded daemon queries. Correlate bounded volume events to those exact IDs to prove no persistent volume creation/mount for the tested lifecycle. A global unchanged count is insufficient in a shared daemon. Preserve unrelated inventory separately without deleting anything from it.
6. Only after owned smoke/capacity proof, run selected heavy current-default registration,35-member recovery and atomic rollback cases, then full affected durability/native/controller/scheduler and service fixture regression gates serially under their existing watchdogs. Record observed capacity throughout the heavy tests without changing their work to fit storage. Count repeated helper tests separately from distinct bodies. Preserve original failures and cleanup uncertainty.
7. Keep offline/locked formatting/strict checks and existing process/Linux protocol synthetic coverage. Fresh exact-source Linux runtime proof remains a later full-goal gate; prior Linux acceptance does not prove current default-profile capacity.

## Authority and non-goals

No existing-volume deletion, pruning, Docker reset, retrospective volume ownership inference, production access, key/config change or publication. This repair prevents new leaks; it does not reclaim existing leaked storage. Newly created owned fixture data remains disposable only through the approved exact-container lifecycle. Tmpfs is volatile across container stop/restart and may have host swap/backing behavior; it does not establish persistent-storage/power-loss recovery or a guarantee of zero host disk writes. Rust-controller restart proofs keep their database fixture alive, and full process/recovery/cutover requirements remain intact.

## Sequencing

Task7 source7687397f7a86b1c53fb6bddb3b2ffe32cc060007 is accepted through main tracker commit c511ca63c97b638cf8fd60fd822683d77533581d. Use a fresh Astra source owner and independent review for this prerequisite before further broad Task8 fixtures. Main must pin each implementation brief and exact clean base; this specification alone does not dispatch an implementation agent.

The prerequisite has two separately reviewable deliverables: A, fixed launch and fail-closed storage admission with compiled non-live tests; B, dedicated live lifecycle/capacity harness and affected-suite qualification. Deliverable A cannot claim runtime capacity, actual leak prevention, or permission to resume broad suites. The plan `docs/superpowers/plans/2026-09-06-owned-docker-storage-admission.md` implements A only. Main prepares B against A's accepted interface; all live requirements above remain mandatory before this prerequisite is complete. The existing 8GiB local build and 18GiB Linux artifact-start resource guards remain in effect. Source/test preparation can proceed below the local guard, but compiling RED/GREEN, builds, live fixtures and acceptance wait for a verified guard clearance; do not bypass TDD by implementing the repair before compiled RED.
