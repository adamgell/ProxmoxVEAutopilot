# OSDeploy durability schema acceptance

Accepted local schema milestone, not execution or service readiness.

## Exact source and review

- Source baseline: `62e8187e4de20d25d3aee52ca72c49e817df09c9`.
- Accepted source: `93f54c792c47e9d7dfcc1380fd297a3f7d5f196a`.
- Acceptance recorded in readiness tracker: `9641bb2bb69dd40a6e8749f51240a337adac8df2`.
- Changed source: additive migration0005, its five-line SQLx transaction hook, private integration-fixture support, and the new durability integration harness. Migrations0001–0004 and existing registration behavior remain unchanged.
- Fresh independent Astra review read the complete pinned1,294-line/57,577-byte change package, full implementation report, task brief/checklist, plan and five specification records. Specification compliance and code-quality verdicts were both clean, with no actionable findings. This review was static; it did not independently rerun runtime tests.

Main read the full implementation and review reports and independently passed workspace formatting and exact-range diff checks. The tracked tree was clean at the acceptance checkpoint.

## Executed macOS evidence

Owned cached local PostgreSQL fixtures were used under bounded test-process watchdogs. No real Proxmox or production controller was mutated. These are macOS Rust executions using local database fixtures, not a Linux controller runtime result.

| Final harness | Result | Distinct contribution |
| --- | --- | --- |
| `postgres-store --test osdeploy_durability` |33 passed,0 failed,0 ignored;33.76s; exit0 |8 new schema/atomicity tests plus25 shared fixture tests |
| `postgres-store --test osdeploy_registration` |41 passed,0 failed,0 ignored;118.15s; exit0 |16 existing registration tests plus the same25 shared fixture tests |

Combined:74 passing invocations and49 distinct test bodies. Earlier focused runs repeat coverage and are not extra distinct tests. Final runs completed at00:28:17 and00:30:29 UTC on2026-09-06 according to the implementation command ledger. The implementer reported an empty final container inventory for the exact proof ownership label; this is not a claim that unrelated Docker resources were absent or removed.

The eight new tests cover additive/declaration-only behavior, all immutable trigger forms and independent replay repair, operation-bound references and uniqueness, action/resolution/nullability combinations, scalar/time/object-size constraints, immediate versus deferred references, SQL failure rollback, and cancellation rollback. Both rollback cases reach migration0005 and verify partial-object rollback plus clean reuse of the same primary backend connection. Cancellation observes the exact decisions-table DDL barrier, drops the migration future, then releases the barrier before reacquiring the connection.

## Failure history retained

The initial required behavioral RED failed at missing table count0 versus9, exit101. The first full durability run then reported32 passing and1 failing test because its new immutability assertion expected SQLSTATE P0001 while the unchanged existing trigger deliberately emits23514. A narrow serial rerun reproduced that assertion failure. Correcting the expected SQLSTATE and exact existing error message produced focused and full GREEN. This was a test-expectation correction, not a production-function defect or change.

Shared fault tests intentionally emit caught-panic and cleanup-uncertainty diagnostics and finish `ok`; the complete logs preserve those lines. This milestone does not erase the separate earlier unexplained native-controller macOS failures retained in the programme readiness tracker.

## Retained detailed evidence

The preserved local SDD directory is `.superpowers/sdd/2026-09-05-rust-osdeploy-durability/` at the isolated worktree root. It contains `task-1-brief.md`, `task-1-report.md` with all seven complete command/output records, `task-1-review.md`, `task-1-main-review-checklist.md`, `review-62e8187..93f54c7.diff`, and `progress.md`. These files are intentionally retained as local evidence; their presence does not imply remote publication.

## Remaining acceptance boundaries

The schema enforces local relational constraints, not complete semantic history validity. Strict canonical wire/journal reload, actual same-attempt activation and leases, one-shot dispatch, recovery/cancellation, callbacks, full sixteen-stage service execution and independent-process recovery remain subsequent work. Changed source needs fresh exact-source Linux evidence. Production-candidate assurance, non-production approval gates and the final readiness assessment remain open. Neither this acceptance nor the first-three-stage intermediate phase authorizes deployment, cutover or Ansible retirement.
