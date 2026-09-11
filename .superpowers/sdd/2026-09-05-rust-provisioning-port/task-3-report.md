# Task 3 implementation report

2026-09-05. Task 3 is implemented and committed for main's independent review. This report establishes local synthetic transport behavior and downward conversion, not durable dispatch authority, service recovery, Windows readiness or installed-PVE compatibility.

## Exact source and ownership

- Accepted baseline: `942a5bb0e363c213f258b229993db98e400f4a90`.
- Accepted Task 2 source consumed: `15166a729b469c15539b01c5128be40c7274ac5a`.
- Task 3 source/test/README commit: `a00c003dbf8b9c3f82278b39449dadf1099916fd` (`feat(pve-port): apply provisioning in shared fake world`).
- This report is committed separately after the immutable source commit; its commit is reported in the handoff.
- Exactly 12 assigned source/test/documentation paths changed in the source commit. No Task 1/2 implementation, existing tests, controller, store, scheduler, service, dependency manifest, Cargo.lock or Dockerfile changed. `rust-controller/README.md` is the existing shared README containing the OSDeploy contract; the adapter has no separate README.
- One implementer; no children or reviewers. No helper/MCP/tunnel action, live endpoint, credentials, Docker/PostgreSQL/Compose, new fake HTTP host, service, artifact build, cleanup, push, merge, publication or deployment. Existing complete PVE regressions retain their established isolated loopback fixtures. The pure support filter uses its existing synthetic local child helpers, not a database.

## Interface map

| Location under `rust-controller` | Produced interface or implementation |
| --- | --- |
| `crates/pve-port/src/provisioning.rs` | Sealed `ProvisioningFakePort`: rich config, neutral identity and independent media reads plus `submit_provisioning`; two new compile-fail checks |
| `crates/pve-port/src/lib.rs` | Additive public exports for typed fake scripts, selectors and immutable inspection outputs |
| `crates/pve-port/src/native_fake.rs` | One private `FakeConfig` enum in the existing VM map; common inventory projections and explicit downcasts; one pending queue/task map/UPID sequence; checked incarnation allocation; independent QGA flag; legacy overlap checks and legacy-only FIFO completion |
| `crates/pve-port/src/native_fake/provisioning.rs` | Shared-lock admission, first-attempt duplicate recording, selector resolution, exact accepted receipts, immutable submission history, exact-UPID completion and original incarnation revalidation |
| `crates/pve-port/src/native_fake/provisioning/controls.rs` | Three closed script enums; action/optional-operation selector with derived profile; insert/replace, power, QGA, media, fault and pause controls; private output fields and read-only getters |
| `crates/pve-port/src/native_fake/provisioning/world.rs` | Full before-state/current infrastructure/identity/media/pending-resource validation; validated lossless rich candidate reconstruction; atomic application |
| `crates/pve-port/tests/provisioning_support/world.rs` | A single actual fake-world fixture; fresh collection of all physical facts; Task 2 bound request construction and verified outcome/predecessor chain |
| `crates/pve-port/tests/provisioning_fake.rs` | 23 behavioral tests, including nested action/outcome and replacement matrices |
| `crates/osdeploy-adapter/src/provisioning.rs`, `src/lib.rs` | `pve_expectations(&OsDeployPlanV1)` and exact fixed `UnsupportedPveDiskSerial` / `InvalidPveExpectations` errors |
| `crates/osdeploy-adapter/tests/provisioning.rs` | Three tests for all mapped fields, capacity cases, serial boundaries and separate workflow identity |

The existing crate-visible `provisioning_receipt_matches` is reachable through the existing provisioning exports and is called for every generated receipt. No duplicate matcher or extra re-export was necessary. Both internal pending request variants are boxed to avoid a large private enum without changing public signatures.

## Executed RED and GREEN

Commands ran from `rust-controller`.

```text
cargo test --offline --locked -p pve-port --test provisioning_fake
```

The first attempt reported missing APIs; that compiler failure is not counted as behavioral RED. Minimal compile-ready scaffolding then executed 1 test with 0 passed / 1 failed. After fixture/read scaffolding was added, the clone test failed at actual `submit_provisioning` returning `Rejected`. Before implementing mutation admission/effects, the complete growth and both retention sequence tests were added and executed: **0 passed / 4 failed**, all at the intentionally unimplemented clone submission. Implementing the actual shared-world path produced **4 passed / 0 failed**. Further adversarial expansion finished at **23 passed / 0 failed**.

```text
cargo test --offline --locked -p osdeploy-adapter --test provisioning
```

Missing conversion APIs were separately observed. Compile-ready conversion scaffolding returned the fixed failure; all three tests then executed RED: **0 passed / 3 failed**, with `InvalidPveExpectations` where a valid subset was required. Getter-only validated conversion produced **3 passed / 0 failed**.

Intermediate import/type errors and a test fixture attempting to fingerprint a media-present template were corrected and are not counted as behavioral TDD proof. Accepted Task 1 template fingerprints require installed-disk boot and absent media, so the clone source preservation test exercises admitted source serial/resource/QGA values and absent media. That upstream contract was not broadened.

## Required behavior matrix

| Area | Executed evidence |
| --- | --- |
| Full physical chain | Clone -> capacity -> PE configure -> PE start -> stop -> disk configure -> distinct disk start, all in the same world with fresh independent reads and Task 2 evaluator/ownership/baseline proofs. No scripted expected snapshots create success. Stop is a direct synthetic physical request, not proof of durable force-stop permission. |
| Capacity | Actual 80-to-120 GiB growth; 160 GiB retention for requested 120; exact 160 GiB + 17-byte retention. Retention creates no resize submission. Growth changes only capacity/digest/time; configured storage-free values remain unchanged. |
| Exact requests | Every ordered form, method and route checked against literal expectations; resize includes captured digest and absolute `120G`; PE `scsi0` contains serial with no size. History preserves the exact typed request and complete request hash. PE/disk starts have distinct operation and request identities despite identical empty forms. |
| Lossless effects | Generated clone preserves source serials/resources/channel/enabled state and admitted media/firmware fields; intermediate UUID/MAC are distinct from source and final desired values. Growth keeps volume/other fields. ConfigurePe establishes final resource/identity/media values without growing disk. Disk configuration removes both media slots and changes boot only. Starts/stop preserve the entire configuration including digest. |
| Outcome families | 7 actions x 5 outcomes = 35 combinations. Four configure/task-only combinations reject at enqueue. The other 31 submit cases check immutable request/receipt/history, distinct attempts and acceptances, unchanged world on rejection/task failure/delay, and actual resulting capacity/power/identity/media after accepted, lost-response or released delayed effects. Every subsequent same-operation submission rejects and is recorded. |
| Fault selection | Operation-specific priority over action-wide FIFO; duplicate and current-world validation failures consume no queued outcome; first rejected operation remains non-retryable; PE/disk start faults remain independent; legacy fault/pause queues remain separate. |
| Delayed identity | Clone source and rich target replacements with copied digest/identity/provenance fail original work. Five task actions x copied replacement / independent same-power fixture event = 10 cases. New target occupation and legacy source replacement also fail without changing the new occupant. QGA changes do not invalidate delayed growth. |
| Shared resources | Mixed old/rich VMID insertion collisions; shared inventory and disk-volume inventory; old/rich configuration downcasts fail closed. Neutral identities remain visible with honest partial coverage. Legacy UUID collision or partial identity coverage blocks rich work. Pending overlaps reject in both directions; old completion skips rich entries. Shared UPIDs differ. Wrong-family, unknown and repeated exact completion rejects. Wrong-node task query returns InvalidResponse. |
| Admission mismatches | Eleven top-level before-state edits and three primary-disk edits fail despite copied marker/digest where applicable. All seven actions reject changed current power. Source and target route changes reject. Both media-storage catalogs are checked independently, including missing ISO and partial coverage at clone/configure/PE start. Free space of zero or growth delta minus one rejects; exact delta succeeds. |
| Independent QGA | False after clone/configure/running/stop/disk restart unless independently set. Explicit true survives configuration/power actions and delayed growth; replacement resets false. Source QGA configuration never proves reachability. |
| Scripts | Error, snapshot and pause behavior for config/identity/media; route/source mismatch rejection; scripted timestamp preservation; normal read timestamp refresh retaining unsupported classes; pause releases the mutex and reads the changed world; no script changes history or world. Fake fixture/script source cannot claim PveApi. |
| Late completion | Original accepted delayed clone physically completes after an expired controller deadline. Fresh task/config reads show actual success, while the strict evaluator remains Unknown / DeadlineExpired. No task replay or new dispatch occurs. |
| Downward conversion | Equality for all eight PVE input categories; requested/effective capacities stay distinct; exact 20-byte serial succeeds and accepted 21-byte declaration fails conversion without truncation. Profile, policy and language changes alter full workflow fingerprint without changing PVE expectations. No reverse dependency or manifest change. |
| Public capability | Read-only `ReqwestPveObserver` cannot call provisioning submit; downstream wrapper cannot implement the private sealed capability. No public arbitrary endpoint, callback or raw mutation hook. |

The snapshot scripts are used only in explicit observation-fault tests. The full chain's fixture collector replaces every physical observation with a fresh read from the actual shared world. Inspection acceptance is not automatically converted into authoritative controller receipt evidence; direct fault tests inspect it only as server history.

## Final verification and actual counts

```text
cargo test --offline --locked -p pve-port -p osdeploy-adapter --quiet
cargo test --offline --locked -p operation-controller --test decision --quiet
cargo test --offline --locked -p operation-controller --test postgres_native support:: -- --test-threads=1
cargo fmt --all -- --check
cargo clippy --offline --locked --workspace --all-targets -- -D warnings
cargo deny --offline --locked check
git diff --check
git diff --exit-code -- Cargo.lock Cargo.toml crates/pve-port/Cargo.toml crates/osdeploy-adapter/Cargo.toml
```

All exited 0. The complete suites passed before the final test-only strengthening of response-loss postconditions. The strengthened `provisioning_fake` binary was then rerun (23/23), followed by final all-target strict Clippy, workspace formatting and diff checks. No production source changed after the complete suites.

| Suite / binary | Passed | Failed / ignored |
| --- | ---: | --- |
| pve-port library | 25 | 0 / 0 |
| credentials | 4 | 0 / 0 |
| infrastructure_visibility | 26 | 0 / 0 |
| infrastructure_visibility_http | 22 | 0 / 0 |
| native_contract | 16 | 0 / 0 |
| network_deny | 6 | 0 / 0 |
| preflight | 15 | 0 / 0 |
| provisioning_config | 17 | 0 / 0 |
| provisioning_evaluation | 25 | 0 / 0 |
| provisioning_expectations | 7 | 0 / 0 |
| provisioning_fake (new) | 23 | 0 / 0 |
| provisioning_requests | 12 | 0 / 0 |
| visibility | 17 | 0 / 0 |
| visibility_http | 11 | 0 / 0 |
| **pve-port unit/integration total** | **226** | **0 / 0** |
| pve-port doctests | 18 | 0 / 0 |
| osdeploy-adapter input_values | 12 | 0 / 0 |
| osdeploy-adapter plan | 15 | 0 / 0 |
| osdeploy-adapter provisioning (new) | 3 | 0 / 0 |
| osdeploy-adapter stages | 4 | 0 / 0 |
| **osdeploy-adapter unit/integration total** | **34** | **0 / 0** |
| osdeploy-adapter doctests | 6 | 0 / 0 |
| operation-controller decision | 24 | 0 / 0 |
| operation-controller postgres_native `support::` | 10 | 0 / 0; **25 excluded by filter** |

There are **294 unit/integration/support successes plus 24 doctests** across the required commands. The adapter library reports zero internal unit tests. Existing PVE subprocess canaries print nested one-test successes; those are already included in parent binaries and are not double-counted. New work adds 26 test functions and two compile-fail doctests; nested matrix cases are described separately above.

Cached offline dependency policy output: `advisories ok, bans ok, licenses ok, sources ok`. Warnings are the existing unused `OpenSSL` license allowance and duplicate versions of `getrandom`, `hashbrown`, `syn`, `webpki-roots`. No dependency or lock changes were made. The warning summary was reread through a bounded output filter after the full diagnostic exceeded the output limit. Offline advisory success uses the existing cached database and is not a current remote advisory refresh. Final Clippy/fmt/diff checks contain no warnings or failures.

## Self-review and limits

- Reviewed the single VM namespace, rich/native downcasts, shared task sequence and queue filtering. Old public contracts/serialization and old-only queue/fault behavior remain unchanged; all existing regressions pass.
- Reviewed atomic admission/application: no await under the state mutex; full original semantic before-state plus digest/marker/power is compared ignoring observation times only; candidate construction validates the full rich output before replacing an entry.
- Reviewed exact incarnation capture and completion: no fixture replacement, including a same-value replacement or same-power independent fixture event, can redirect accepted work. No expected state is refreshed from current state at completion. Task state changes do not rewrite original submission/receipt/timestamps.
- Reviewed exact effect boundaries and independent QGA/media/storage facts. Sanitized rich reconstruction retains capacity and all unrelated fields; it never uses the legacy wire encoder or retains arbitrary raw config maps.
- Reviewed duplicate/fault behavior and distinct PE/disk action identities. The attempted-operation set is a fake transport duplicate guard, not an authority table; there is no workflow, lease, scheduler, approval or durable reservation state in it.
- Reviewed downward conversion using accepted getters and validated `ProvisioningExpectationsV1::new`. Full workflow identity remains independent. `InvalidPveExpectations` is the fixed constructor-failure mapping; valid currently admitted plan values do not provide an artificial corruption path solely to exercise that fallback.
- The explicit synthetic task contract uses `qmclone` source VMID, `resize`, `qmstart` and `qmstop`; configurations are synchronous. No claims are made about installed PVE versions or real-wire mutation support.
- Accepted Task 1 template fingerprints disallow present media and non-disk template boot; those rejected source states cannot reach a valid clone request in this phase. Clone application preserves the admitted source fields without broadening admission.
- Delayed plus response-lost is not a combined fault mode. Completion models only a physical fake-host event; it does not acquire a controller lease, authorize a stop, register a workflow or defeat the strict controller deadline.
- Main owns independent review, exact-source Linux evidence, future existing-store integration and subsequent service/Windows acceptance. No broader completion or readiness claim follows from these local tests.
