# Task 2 implementation report

2026-09-05. Implemented the accepted Task 2 brief in the isolated worktree. Ready for main's independent review; this is not acceptance of the controller, service orchestration, durable dispatch authority, or real PVE behavior.

## Exact source and scope

- Accepted starting baseline: `1aff2ed8a4922d6a17802ea8ca9ef782dfbc308d`.
- Accepted Task 1 source: `abd51211357b90219237ca579c58519d6350b336`.
- Main's intervening documentation-only commit `e1491bb` was preserved.
- Task 2 source/test/README commit: `15166a729b469c15539b01c5128be40c7274ac5a` (`feat(pve-port): add bound rich provisioning requests and physical proofs`).
- This report is committed separately after that immutable source commit.
- Exactly 16 Task 2 source/test/documentation paths changed in the source commit. No old native API, Task 1 implementation, dependency, fake world, adapter, controller, scheduler, store, or service source changed. Existing `lib.rs` already exports `provisioning::*`, so no redundant export edit was necessary there.
- No child agents, MCP/helper/tunnel activity, live PVE calls, real credentials, Docker/PostgreSQL/Compose, artifact build, cleanup, push, merge, publication, or deployment was performed. Existing regression tests use their established isolated loopback fixtures.

## Interface map

| Location under `rust-controller/crates/pve-port` | Produced interface / responsibility |
| --- | --- |
| `src/provisioning/requests.rs` | `ProvisioningBeforeStateV1`, `CloneProvisioningRequestV1`, `ProvisioningOwnedRequestInputV1`, `GrowDiskRequestV1`, `ConfigureProvisioningRequestV1`, `StartProvisioningRequestV1`, `StopProvisioningRequestV1`, closed `ProvisioningMutationRequestV1`; common getters, exact method/path/ordered forms and complete request digest |
| `src/provisioning/requests/state.rs` | Shared supported-state, freshness, original ownership, expected physical predecessor, semantic invariance and allowed-transition checks |
| `src/provisioning/requests/wire.rs` | Private strict validating request wire reconstruction, including standalone concrete types and string-only closed discriminators |
| `src/provisioning/evidence.rs` | `ProvisioningBindingV1`, `ProvisioningDispatchInputV1`, `ProvisioningDispatchV1`, `ProvisioningReceiptV1`, `ProvisioningEvidenceInputV1`, private `ProvisioningEvidenceV1`; one crate-visible `provisioning_receipt_matches` used by receipt validation |
| `src/provisioning/evidence/identity.rs` | Neutral `ProvisioningIdentitySnapshotV1` and validated `ProvisioningIdentityReadV1`; rich/native projections preserve source and conservative coverage |
| `src/provisioning/evidence/validation.rs` | Complete plan/binding, successful route/source, receipt/task and duplicate inventory admission checks, even when another mandatory read is absent |
| `src/provisioning/evidence/wire.rs` | Strict object wire reconstruction; duplicate-key detection and exact canonical native nested representations at the new boundary; required explicit nullable fields |
| `src/provisioning/evaluation.rs` | Fixed `ProvisioningReasonV1`, `ProvisioningEvaluationV1`, `evaluate_provisioning_preflight`, `evaluate_provisioning_outcome`; mode/state/cancellation/source/deadline eligibility |
| `src/provisioning/evaluation/context.rs` | Checked context input/context, explicit dispatch state, verified `ProvisioningCloneOwnershipV1` and `ProvisioningStageBaselineV1`; historical proof and policy getters, no unchecked constructors or Deserialize |
| `src/provisioning/evaluation/observations.rs` | Independent collection/wrapper/snapshot clocks, infrastructure, dual-storage media catalogs, complete identity coverage and collision checks |
| `src/provisioning/evaluation/outcome.rs` | Exact original-dispatch outcome transitions, bound receipts/tasks, clone failure with unchanged vacancy, receiptless synchronous reconciliation, conservative pending/failure decisions |
| `tests/provisioning_support/mod.rs` | Independent deterministic synthetic source/intermediate/final snapshots and seven-action physical proof chain; retained-capacity and serial variants |

The non-clone concrete request types share one private `OwnedRequest` backing envelope and validator. Their transparent serialization exposes precisely the accepted envelope fields, with no extra wrapper or unchecked ownership decoder. This avoids duplicating historical-proof admission across four families. Concrete types expose the historical field getters as well as the common request methods.

The complete provisioning request hashes are distinct from the unchanged nested `CloneRequest` marker hash. Every action's canonical hash is pinned in the tests. Ordered resize form uses `disk=scsi0`, absolute integral `size=<G>G`, then captured `digest`. PE configuration contains no size effect. Nonintegral retained capacity passes through the explicit pre-dispatch no-change branch and never produces a rounded resize request.

## Executed TDD evidence

Initial command, from `rust-controller`:

```text
cargo test --offline --locked -p pve-port --test provisioning_requests
running 1 test
binding_rejects_invalid_full_workflow_hash ... FAILED
test result: FAILED. 0 passed; 1 failed
```

The minimal binding scaffold compiled and actually admitted malformed workflow hashes. The test failed its behavioral `is_err()` assertion. After implementing hash validation and lowercase canonicalization, the same command passed 1/1. No missing-API compiler failure was counted as RED.

During adversarial expansion, the following three independent tests executed RED against the implemented evaluators:

- `outcome_cannot_use_stale_source_snapshot_in_inventory_comparison`: observed `CloneSatisfied` instead of `ObservationNotFresh`.
- `failed_clone_with_exact_unchanged_vacancy_is_failed`: observed `Unknown` instead of `Failed`.
- `clone_outcome_source_change_and_request_provenance_are_conflicts`: observed `Satisfied` instead of `Conflicted`.

The focused command at that point reported 17 passed / 3 failed. Repairs independently validate every source snapshot/wrapper used by coverage, compare clone source semantics to the original request, and evaluate attributed clone failure against fresh exact unchanged vacancy. The same tests then passed, followed by the full regression.

Intermediate compiler mistakes, fixture corrections (numeric template flags and snake_case read-error spelling), overly strict test assumptions about freely generated clone intermediate identities, and initial golden-placeholder failures are not counted as behavioral TDD proof. Golden placeholders were replaced only after pinning the expected complete shape and emitted canonical hashes.

## Required matrix exercised

| Area | Executed coverage |
| --- | --- |
| Physical chain | Every action has valid preflight and outcome; exact source/intermediate/final identity and original clone proof survive the complete chain |
| Forms and hashes | All methods/routes/ordered forms; no fake metadata in forms; complete JSON envelope for every variant; seven exact complete hash goldens; separate PE/disk start hashes with identical external forms; before config digest and both timestamps, attempt and fence changes alter request hashes |
| Capacity | Exact growth; retention without resize, including nonintegral bytes and `u64::MAX`; unexpected third capacity conflicts; PE/configure never changes capacity |
| Serial compatibility | 20-byte desired serial accepted and 21-byte rejected for each concrete constructor family and every persisted action; clone restriction applies before clone dispatch |
| Physical changes | Independent name/resources/UUID/MAC/bridge/system serial/disk volume/capacity/disk serial/QGA/boot/media changes before and after; firmware/CPU/balloon/QGA-channel unsupported classes, locks and template changes; cloning permits only its specified newly generated identity/volume fields |
| Coverage | Partial/missing/failed identity coverage, UUID/MAC collisions, duplicate keys, wrong projection route, local target 404 plus same target VMID on another node, inventory/power contradiction, complete and partial legacy projections in the shared namespace |
| Media | Both independent exact storage catalogs, missing catalog, partial catalog, complete catalog lacking the ISO, duplicate and extraneous catalogs |
| Receipts | Worker/node/VMID cross-product, including rejecting `qmresize`; configs synchronous only; task actions task only; dispatch source, generation, revision, overflow, immutable request hash and time; wrong start receipt substitution and unbound evidence receipt |
| Clocks | Collection, wrapper and snapshot clocks checked independently; exactly freshness allowed, one second beyond rejected; future facts; pre-dispatch target observations; task before acceptance; receipt after collection; strict exact deadline and historical proof reload time |
| Ambiguity | Possible-without-record and recorded states never Ready; task receipt loss remains Unknown despite desired state; receiptless configuration only reconciles in original-dispatch Reconciliation/Unknown; task success without postconditions remains Unknown |
| No-change | Only capacity and stop pre-dispatch satisfy observed no-change; both start-already-running cases remain Unknown; observed no-change can create a checked baseline |
| Failure | Running and failed tasks with desired, unchanged and replaced-disk states; failed clone with fresh unchanged source and exact vacancy; known replacement never hidden by task success/running |
| Historical proof | Wrong run/workflow/plan/marker/original request and missing predecessor rejected; syntactically altered proof hash cannot pass independent context historical comparison; original clone evidence roundtrip and checked reconstruction at original time; expired historical reconstruction rejected |
| Persisted boundary | Missing mandatory or nullable fields; explicit null where allowed; unknown and duplicate nested fields; standalone/nested object-form unit discriminators; route/source/plan and receipt relationships; roundtrip of all requests, dispatches, receipts and evidence |
| Public restriction | Four new compile-fail doctests: private evidence output, private before-state fields, no ownership Deserialize, no baseline Deserialize |

## Final verification

The final source was verified before committing; the commit changed no source afterward. Commands ran from `rust-controller` unless otherwise stated.

```text
cargo test --offline --locked -p pve-port --quiet
cargo fmt --all -- --check
cargo clippy --offline --locked --workspace --all-targets -- -D warnings
git diff --check
```

All exited 0. Final test output reported:

| Test binary | Passed |
| --- | ---: |
| library unit tests | 25 |
| credentials | 4 |
| infrastructure_visibility | 26 |
| infrastructure_visibility_http | 22 |
| native_contract | 16 |
| network_deny | 6 |
| preflight | 15 |
| provisioning_config (unchanged Task 1) | 17 |
| provisioning_evaluation (new) | 25 |
| provisioning_expectations (unchanged Task 1) | 7 |
| provisioning_requests (new) | 12 |
| visibility | 17 |
| visibility_http | 11 |
| Unit/integration total | **203** |
| Doctests | **16** |

Every summary had 0 failed and 0 ignored. The existing isolated subprocess canaries also printed their own one-test successes; these are not double-counted above. The final new-test total is 37. The focused test command was used during iteration; the final complete run includes all 37 new tests and all 166 existing unit/integration tests.

Final formatting, strict Clippy and diff checks were clean. Earlier Clippy found a collapsible conditional (fixed) and the large explicit `ProvisioningDispatchStateV1` variant. The latter has one local documented `#[allow(clippy::large_enum_variant)]` to preserve main's exact accepted input signature; no broad lint suppression was added. Final output contained no warnings.

## Self-review and limits

- Reviewed the semantic projection comparison so every field outside an action's explicit allowance stays invariant, including disk capacity/serial/media/resource categories. Config digest and observation times remain preserved in the request hash while historical baseline semantic comparisons permit new observation timestamps/digests.
- Reviewed original dispatch versus current evidence binding: exact current evidence equality, same run/operation/attempt/full workflow/plan across receipt/request, independent first-append fences, positive original generation and checked revision arithmetic. No current generation, CAS, lease token, or resend capability is manufactured.
- Reviewed nested persisted decoding for duplicate keys and object-form enums at the new boundary. Legacy types are unchanged; exact canonical shape is enforced only when used by the new rich envelope.
- Reviewed ownership/baseline reconstruction: verified outputs keep original proof and original policy/time; no raw ownership constructor, deserialization shortcut, cross-run conversion, native ownership conversion, or recursive ownership serialization exists.
- Context/evidence inputs are caller-shaped facts. None authenticate provenance, compute the authoritative OSDeploy-to-PVE projection, or grant dispatch. The future existing-store integration must independently derive the workflow SHA, exact downward plan, operation IDs, selected durable predecessor chain and original dispatch/receipt rows, and re-evaluate under locks/current DB time.
- Stop readiness remains physical advice. PE completion, actual grace timeout revision/deadline, force-stop policy, current fences, QGA readiness and guest readiness remain unproved and outside Task 2.
- Task 3 must apply the same 20-byte desired disk serial restriction before registering derived operations. The central receipt matcher is available for its later fake path. No fake mutation/collection seam was implemented here.
- The `resize` worker and explicit forms are the accepted synthetic/pinned contract, not installed-version validation. No real PVE send, actual service lifecycle, Linux artifact gate, durable store gate, OOBE/ESP/enrollment proof, or deployment was performed. Main owns independent review and artifact acceptance.
