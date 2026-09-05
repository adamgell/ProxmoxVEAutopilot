# Owned Linux database fixture: source-review checkpoint

Source review accepted at `a36c166f373331ff0a66324fd9301d6a745c5721`, following implementation `3e64320d29b9db9644ded1b753572561ca3946a8`. This is a test-infrastructure source checkpoint, not Linux runtime acceptance or controller PoC readiness.

## Reviewed scope

Eight authorized helper/test paths add an explicit Linux-only owned-database backend while retaining the default Docker fixtures. The backend accepts a fixed, host-verified receipt and fixed loopback PostgreSQL instance; it generates per-fixture names and verifies instance, namespace, full ownership marker, OID and lifecycle advisory lock before cleanup. No application implementation, migration, manifest/lock, production capability or configurable external database path changed. Historical foundation/scheduler assertions and registration implementation remain intact.

Independent Astra review read the complete 2,138-line implementation package and requested one P2 correction: enforce the fault child's original absolute deadline after readiness. A single-file fix introduced an owned standard-thread supervisor with exclusive child ownership, bounded nonblocking capture, the original deadline and 200 ms reap reserve, cancellation signalling and joined owner Drop. The reviewer read the complete 295-line fix and relevant lifecycle context, approved it and found no new actionable regression. No review agent ran infrastructure or changed source.

## Evidence and its limits

- The complete macOS four-package suite passed 286 ordinary executions plus 10 doctests = 296, zero failed/ignored/filtered, retaining all 231 baseline executions. This run preceded the final private fault-checkpoint changes.
- The supervisor repair has behavioral RED evidence: four tests found children still present before owner Drop. The repair made those tests pass, with additional coverage for startup exhaustion, synchronous cleanup, cancellation/caller panic and overflow.
- Final covering verification passed 25 shared Rust helper tests, 17 Python synthetic tests, formatting and strict workspace/all-target/all-feature Clippy. Cached offline dependency policy passed with four existing duplicate-dependency warnings and one existing unused-license-allowance warning; no fresh advisory fetch was performed.
- No new complete SQL suite was run after the final repair. Projected final complete counts are 326 macOS and 371 Linux executions; these are not passing results. Nine Linux-only database lifecycle cases repeat in five selected harnesses and still require compilation/execution in the owned Linux gate with `--include-ignored`, zero ignored or filtered results.
- The first macOS attempt reported two native-controller failures then stranded at an existing unbounded test checkpoint. It was terminated as incomplete. Unchanged isolated/native-sequence reruns and the later full run passed, but the original cause was not established. This remains a release-assurance item, not a claimed fixed defect.

The exact interrupted test container was attributed and removed after preserving its logs; its database volume and all four pre-existing containers were retained. No pruning, production access, external mutation or deployment occurred.

## Next gate

The Linux gate must use the final reviewed source, immutable cached-parent-derived artifact, verified executed ELF identities and one fresh owned PostgreSQL namespace. It must preserve all real registration and baseline database assertions, run every Linux-only lifecycle case, retain the expected 25 refusal-case databases until verified instance teardown, and capture source/image/runtime/result/resource evidence. Gate scripts require inspection before execution. Neither an image build nor the earlier SQL topology probe substitutes for this proof.

Detailed reports, complete review packages, RED/GREEN logs, local suite logs, cleanup attribution and both independent verdicts are retained in `.superpowers/sdd/2026-09-05-rust-linux-owned-fixtures/`. The approved plan is `docs/superpowers/plans/2026-09-05-rust-linux-owned-fixtures.md`.
