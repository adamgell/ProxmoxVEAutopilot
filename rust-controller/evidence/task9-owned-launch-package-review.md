# Task9 minimal owned-v1 launch package — reviewed

New-file-only implementation at launcher checkout HEAD `69faf2902cb60e10a4169c04b667a0cb9e630ddc`. No existing source, helper, receipt protocol, migration, index or HEAD changed. No Docker, Linux workload, Cargo build, network, production, cleanup, publication or commit was executed for this assignment.

## Scope delivered

- `scripts/task9_owned_linux.py`: host admission and the same file's container-side bounded invocation. Explicit runner tag must be `rust-controller-task9:local`; inspected immutable ID must be `sha256:82b70282d5c4278b04ed15b67684fdb840235f22b6c27d759047dda59f855e2b`, and execution uses that immutable ID. PG keeps the existing fixed receipt-v1 image constant.
- Closed resource profile: shared 4GiB tmpfs, 512MiB available-block floor, PG6GiB/runner4GiB memory, equal memory-swap caps, private cgroup/IPC, no published ports, no host source/target or socket mounts. Only exact receipt and launcher files are bound read-only. Image-declared volumes are checked before create; no volume deletion exists.
- Exact generated names, full captured IDs, immutable images and session/role labels; pending state precedes create. Existing resources are never adopted. Unknown create-response identities remain pending for human resolution rather than being guessed or removed.
- Read-only PG identity/default settings, cgroup-v2 shape/counters, capacity, disk and proposed VM headroom admission. Canonical nine-field receipt v1; no family/nonce/OID/protocol widening.
- Receipt source is image source `8f07247b41e738923ee61e610d4400102b208637`, separately from launcher HEAD. Container-side CONTROLLER_GIT_SHA must match. This is not independent ELF/source provenance and not a final-source service claim.
- Fixed `smoke` (180s) or four-package `full` (1800s) Cargo invocation, offline/locked/serial. Host attach bounds200/1820s are watchdog envelopes, not larger inner workloads. Each stream capped64MiB. No arbitrary shell/workload argument. Exact smoke output must report the selected test and1passed/0failed. Successful invocation still reports `qualification: INCOMPLETE`.
- No removal operation. On failure, only a captured runner passing fresh ownership admission can be killed to bound its workload. PG and stopped containers remain for evidence/review; no automatic retry or workload rerun.

## Synthetic TDD and verification

TDD skill: began with a valid, importable capacity stub. Original exact-boundary case passed; seven below-reserve/malformed vectors failed because the stub wrongly admitted them. Then implemented strict decoding and remaining launch primitives. Original RED is retained, not replaced by GREEN.

All evidence prefixes below are in the restart checkout root, each with `.stdout.log`, `.stderr.log`, `.receipt.json`:

| Prefix | Actual result |
| --- | --- |
| `restart-task9-owned-package-red` | Exit1;2tests,7 failing subcases; UTC15:54:06.187817–06.249884; outer30s |
| `restart-task9-owned-package-green` | Exit0;9tests; UTC15:58:24.122719–26.272200;2.149547s, outer30s |
| `restart-task9-owned-package-final` | Exit0;9tests after endpoint-shape/smoke-output review; UTC15:59:25.916144–28.031616 |
| `restart-task9-owned-package-sealed` | Exit0;9tests after pre-create image-volume refusal; UTC16:00:08.041209–10.170145;2.128970s, outer30s |

UTC date is2026-09-10. All supervisor receipts report direct child reaped, group clean and no leader-left-group. Tests cover capacity boundary/overflow/shape, cgroup required keys/pressure/caps, exact receipt/source, closed argv/mount profile, PG ownership mutations, successful two-stream capture, finite deadline/output cap and descendant-held-pipe failure. They do not exercise Docker, emulate the entire host state machine or prove daemon cancellation.

Exact test command (prefix varies above):

```text
python3 restart-watchdog-capture.py 30 restart-task9-owned-package-sealed python3 -B -m unittest discover -s rust-controller/scripts -p test_task9_owned_linux.py -v
```

`git diff --check` returned0, tracked diff empty. Separate `git diff --no-index --check /dev/null <new-python-file>` checks printed no whitespace errors (exit1 denotes the new-file difference). No Rust fmt/Clippy claim: no Rust file changed. Historical candidate gate read attempted once and failed because that file remains absent; restored214-line resource proposal was read, including rereading a transport-truncated section.

## Review blockers / deliberately incomplete scope

**Not authorized or ready as a complete Task9 acceptance gate.** This is a launch/invocation primitive for review. Do not equate its exit0 with qualification or execute it before main's next ruling.

1. No periodic in-workload resource rounds, restart/counter monotonicity across samples, ordinary fixture catalog observer, retained-refusal union, exact full libtest inventory, doctest inventory, ELF manifest or evidence exporter/seal. These remain the separate proposal's work; the smoke command alone cannot prove ordinary creation/removal, and no sampling/peak/adequacy claim is made.
2. Current image availability/architecture/volume shapes, private cgroup root/schema, inherited environment and actual defaults have not been tested. `/proc/meminfo` VM semantics need platform admission. Unknown shapes fail closed; resource policy approval is not evidence that12GiB MemAvailable or18GiB disk exists.
3. No protected whole-inventory before/after equality or deletion/absence proof. Resources deliberately remain. Failure after an uncertain create response may leave a pending name without a captured ID. Manual exact-scope resolution and later separately reviewed cleanup are required; no discovery/removal fallback was added.
4. Current finite supervisor retains bytes read up to failure/cap, but does not drain unread output after a failure or independently confirm every killed descendant's disappearance. Permission-denied/group-reap paths and whole host/daemon cancellation need additional tests before live execution. The tests establish finite local examples, not the historical export supervisor's full contract.
5. Receipt/image environment binding is not source/artifact provenance. Image source differs from launcher HEAD. Cargo can compile in the runner writable layer; this package does not enforce a prebuilt ELF manifest or prohibit unexpected rebuilds. No inherited service binary is executed or relabeled.
6. Existing defaults/receipt/helper APIs remain unchanged. Full proposed runtime observation, export-before-removal and protected-resource contracts are not weakened; they are explicitly unimplemented here and must not be inferred from this primitive.

## Corrected final identities

- Launcher SHA256 `77b7c688602e48d78791f914b47329563f7f12d60d87b560532f85c699f62213`.
- Test SHA256 `969410441a4431f51fe39a9121de849b7295565bf62365d0c0e218b6dce3e305`.
- Corrected fix-2 receipt SHA256 `9735a02fee2a56049d785754d831fa85591ae1b06c8d80ea4b45808553a5f33d`.

## Sealed file identities

- Original launcher SHA256 `f16d5dae17ab7975e558feacaf4ce4757f37a8defc69cb7e62d1270603834975`.
- Test SHA256 `e146c2a36e803ecd2b5782fe1e0c3cf21c514c430b0040de0f5a4ffa11065c43`.
- RED receipt `74446fe6ea68682af239fee5cb8173e0ef8f28bf84526d0e335da4a7a9d0007c`; RED stderr `394e7e62927890ed179ced5c2d913028887ce62fe073458660a8edac2e0200cb`.
- Sealed receipt `d2466a54750375d4edb0273b60261745edd04111f7cb828a6e7175375d7b4ea0`.
- Sealed stdout (empty) `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
- Sealed stderr (complete unittest results) `2a1545f1a8dd039364c4e93170b5c8f9141c5bb5be378df4dbf3ad11175f3063`.
