mod provisioning_support;
use chrono::Duration;
use controller_domain::ExecutionState;
use provisioning_support::*;
use pve_port::*;
use serde_json::{Value, json};
#[test]
fn seven_action_physical_chain_preserves_original_clone_proof() {
    let chain = chain();
    assert_eq!(chain.len(), 7);
    let owner = chain[6].outcome.facts().clone_ownership.as_ref().unwrap();
    assert_eq!(owner.config(), &intermediate());
    assert_eq!(owner.request().expected_before().config(), &source());
}

fn outcome(
    c: ProvisioningEvaluationContextInputV1,
    e: ProvisioningEvidenceInputV1,
) -> ProvisioningEvaluationV1 {
    evaluate_provisioning_outcome(
        &ProvisioningEvaluationContextV1::new(c).unwrap(),
        &ProvisioningEvidenceV1::new(e).unwrap(),
        time(),
    )
}
fn preflight(
    c: ProvisioningEvaluationContextInputV1,
    e: ProvisioningEvidenceInputV1,
) -> ProvisioningEvaluationV1 {
    evaluate_provisioning_preflight(
        &ProvisioningEvaluationContextV1::new(c).unwrap(),
        &ProvisioningEvidenceV1::new(e).unwrap(),
        time(),
    )
}
fn rebind_target(e: &mut ProvisioningEvidenceInputV1, config: ProvisioningVmConfigV1) {
    let running = e
        .target_power
        .as_ref()
        .unwrap()
        .result
        .as_ref()
        .unwrap()
        .power()
        == PowerState::Running;
    e.target_config = Some(read(config.clone()));
    e.inventory = Some(read(inventory(&[source(), config.clone()], running)));
    e.identities[1] = ProvisioningIdentityReadV1::new(
        node(),
        config.vmid(),
        read(ProvisioningIdentitySnapshotV1::from_provisioning(&config)),
    )
    .unwrap();
}

#[test]
fn all_possible_send_states_never_return_ready() {
    for e in chain() {
        for dispatch in [
            ProvisioningDispatchStateV1::PossibleWithoutRecord,
            e.outcome.facts().dispatch.clone(),
        ] {
            let mut c = e.pre.facts().clone();
            c.dispatch = dispatch;
            let mut f = e.pre_evidence.facts().clone();
            if let ProvisioningDispatchStateV1::Recorded { receipt, .. } = &c.dispatch {
                f.receipt = receipt.clone();
            }
            assert_eq!(preflight(c, f).decision, NativeDecision::Unknown);
        }
    }
}

#[test]
fn task_dispatch_without_receipt_stays_unknown_even_with_desired_world() {
    for e in chain()
        .into_iter()
        .filter(|e| !matches!(e.request, ProvisioningMutationRequestV1::Configure(_)))
    {
        let mut c = e.outcome.facts().clone();
        let ProvisioningDispatchStateV1::Recorded { receipt, .. } = &mut c.dispatch else {
            unreachable!()
        };
        *receipt = None;
        let mut f = e.evidence.facts().clone();
        f.receipt = None;
        assert_eq!(outcome(c, f).reason, ProvisioningReasonV1::ReceiptMissing);
    }
}

#[test]
fn synchronous_lost_receipt_requires_exact_original_reconciliation() {
    for e in chain()
        .into_iter()
        .filter(|e| matches!(e.request, ProvisioningMutationRequestV1::Configure(_)))
    {
        let mut c = e.outcome.facts().clone();
        let ProvisioningDispatchStateV1::Recorded { receipt, .. } = &mut c.dispatch else {
            unreachable!()
        };
        *receipt = None;
        let mut f = e.evidence.facts().clone();
        f.receipt = None;
        assert_eq!(
            outcome(c.clone(), f.clone()).reason,
            ProvisioningReasonV1::ReceiptMissing
        );
        c.mode = ProvisioningEvaluationModeV1::Reconciliation;
        c.state = ExecutionState::Unknown;
        assert_eq!(
            outcome(c.clone(), f.clone()).decision,
            NativeDecision::Satisfied
        );
        rebind_target(&mut f, e.request.expected_before().config().clone());
        assert_eq!(outcome(c, f).decision, NativeDecision::Unknown);
    }
}

#[test]
fn receipt_substitution_is_rejected_in_context_and_evidence() {
    let episodes = chain();
    let pe = episodes[3].evidence.facts().receipt.clone().unwrap();
    let disk = &episodes[6];
    let mut f = disk.evidence.facts().clone();
    f.receipt = Some(pe.clone());
    assert!(ProvisioningEvidenceV1::new(f).is_err());
    let mut c = disk.outcome.facts().clone();
    let ProvisioningDispatchStateV1::Recorded { receipt, .. } = &mut c.dispatch else {
        unreachable!()
    };
    *receipt = Some(pe);
    assert!(ProvisioningEvaluationContextV1::new(c).is_err());
    for e in episodes {
        let mut c = e.outcome.facts().clone();
        let ProvisioningDispatchStateV1::Recorded { receipt, .. } = &mut c.dispatch else {
            unreachable!()
        };
        *receipt = None;
        assert_eq!(
            outcome(c, e.evidence.facts().clone()).reason,
            ProvisioningReasonV1::ReceiptMismatch
        );
    }
}

#[test]
fn cancellation_ineligible_state_deadline_and_freshness_boundaries() {
    for e in chain() {
        let mut c = e.pre.facts().clone();
        c.cancelled = true;
        assert_eq!(
            preflight(c, e.pre_evidence.facts().clone()).reason,
            ProvisioningReasonV1::Cancelled
        );
        let mut c = e.outcome.facts().clone();
        c.cancelled = true;
        assert_eq!(
            outcome(c, e.evidence.facts().clone()).reason,
            ProvisioningReasonV1::Cancelled
        );
        for state in [
            ExecutionState::Unknown,
            ExecutionState::Waiting,
            ExecutionState::Satisfied,
        ] {
            let mut c = e.pre.facts().clone();
            c.state = state;
            assert_eq!(
                preflight(c, e.pre_evidence.facts().clone()).reason,
                ProvisioningReasonV1::StateNotEligible
            );
        }
        for reconciliation in [false, true] {
            let mut c = e.outcome.facts().clone();
            c.mutation_deadline = time();
            if reconciliation {
                c.mode = ProvisioningEvaluationModeV1::Reconciliation;
                c.state = ExecutionState::Unknown;
            }
            assert_eq!(
                outcome(c, e.evidence.facts().clone()).reason,
                ProvisioningReasonV1::DeadlineExpired
            );
        }
        let r = evaluate_provisioning_preflight(
            &e.pre,
            &e.pre_evidence,
            time() + Duration::seconds(30),
        );
        assert_eq!(r.decision, NativeDecision::Ready);
        let r = evaluate_provisioning_preflight(
            &e.pre,
            &e.pre_evidence,
            time() + Duration::seconds(31),
        );
        assert_eq!(r.reason, ProvisioningReasonV1::ObservationNotFresh);
        let r = evaluate_provisioning_preflight(
            &e.pre,
            &e.pre_evidence,
            time() - Duration::milliseconds(1),
        );
        assert_eq!(r.reason, ProvisioningReasonV1::ObservationNotFresh);
        for freshness in [0, 301] {
            let mut c = e.pre.facts().clone();
            c.freshness_seconds = freshness;
            assert!(ProvisioningEvaluationContextV1::new(c).is_err());
        }
    }
}

#[test]
fn snapshot_wrapper_and_collection_clocks_are_independent() {
    let episodes = chain();
    let e = &episodes[0];
    for field in [
        "node",
        "storage",
        "bridges",
        "inventory",
        "source_config",
        "source_power",
    ] {
        for component in ["wrapper", "snapshot"] {
            for stamp in [
                time() - Duration::seconds(31),
                time() + Duration::seconds(1),
            ] {
                let mut v = serde_json::to_value(e.pre_evidence.facts()).unwrap();
                let target = if component == "wrapper" {
                    &mut v[field]["observed_at"]
                } else {
                    &mut v[field]["result"]["Ok"]["observed_at"]
                };
                *target = json!(stamp);
                let f: ProvisioningEvidenceInputV1 = serde_json::from_value(v).unwrap();
                assert_eq!(
                    preflight(e.pre.facts().clone(), f).reason,
                    ProvisioningReasonV1::ObservationNotFresh,
                    "{field}/{component}/{stamp}"
                );
            }
        }
    }
    for episode in episodes {
        let mut f = episode.evidence.facts().clone();
        let target = f.target_config.as_mut().unwrap();
        target.result = Ok(changed(target.result.as_ref().unwrap(), |v| {
            v["observed_at"] = json!(time() - Duration::seconds(1))
        }));
        assert_eq!(
            outcome(episode.outcome.facts().clone(), f).reason,
            ProvisioningReasonV1::ObservationNotFresh
        );
    }
}

#[test]
fn task_snapshot_cannot_predate_receipt_acceptance() {
    for e in chain()
        .into_iter()
        .filter(|e| e.evidence.facts().task.is_some())
    {
        let mut c = e.outcome.facts().clone();
        let ProvisioningDispatchStateV1::Recorded { dispatch, receipt } = &mut c.dispatch else {
            unreachable!()
        };
        *receipt = Some(
            ProvisioningReceiptV1::new(
                dispatch.clone(),
                time() + Duration::milliseconds(1),
                receipt.as_ref().unwrap().receipt().clone(),
            )
            .unwrap(),
        );
        let mut f = e.evidence.facts().clone();
        f.receipt = receipt.clone();
        f.collected_at = time() + Duration::seconds(1);
        let r = evaluate_provisioning_outcome(
            &ProvisioningEvaluationContextV1::new(c).unwrap(),
            &ProvisioningEvidenceV1::new(f).unwrap(),
            time() + Duration::seconds(1),
        );
        assert_eq!(r.reason, ProvisioningReasonV1::ObservationNotFresh);
    }
}

#[test]
fn unrelated_before_and_after_categories_cannot_be_hidden_by_task_success() {
    let changes: Vec<(&str, Value)> = vec![
        ("/name", json!("other")),
        ("/cores", json!(8)),
        ("/memory_mib", json!(8192)),
        ("/uuid", json!("3f2504e0-4f89-41d3-9a0c-0305e82c3377")),
        ("/mac", json!("02:00:00:00:07:77")),
        ("/bridge", json!("vmbr7")),
        ("/system_serial", json!("OTHER")),
        ("/primary_disk/volume", json!("vm-101-disk-replacement")),
        ("/primary_disk/capacity_bytes", json!(121 * GIB)),
        ("/primary_disk/serial", json!("OTHER")),
        ("/qga_enabled", json!(false)),
        ("/boot_profile", json!("installed_disk")),
        ("/deployment_iso", json!({"state":"absent"})),
        ("/driver_iso", json!({"state":"absent"})),
    ];
    for e in chain() {
        for (pointer, value) in &changes {
            if e.request.plan().action() == ProvisioningActionV1::Clone
                && ["/uuid", "/mac", "/primary_disk/volume"].contains(pointer)
            {
                continue;
            }
            let current = e
                .evidence
                .facts()
                .target_config
                .as_ref()
                .unwrap()
                .result
                .as_ref()
                .unwrap();
            if serde_json::to_value(current).unwrap().pointer(pointer) == Some(value) {
                continue;
            }
            let target = changed(current, |v| {
                *v.pointer_mut(pointer).unwrap() = value.clone()
            });
            let mut f = e.evidence.facts().clone();
            rebind_target(&mut f, target);
            assert_ne!(
                outcome(e.outcome.facts().clone(), f).decision,
                NativeDecision::Satisfied,
                "after {:?}/{pointer}",
                e.request.plan().action()
            );
            if e.request.plan().action() != ProvisioningActionV1::Clone {
                let current = e
                    .pre_evidence
                    .facts()
                    .target_config
                    .as_ref()
                    .unwrap()
                    .result
                    .as_ref()
                    .unwrap();
                if serde_json::to_value(current).unwrap().pointer(pointer) == Some(value) {
                    continue;
                }
                let target = changed(current, |v| {
                    *v.pointer_mut(pointer).unwrap() = value.clone()
                });
                let mut f = e.pre_evidence.facts().clone();
                rebind_target(&mut f, target);
                let r = preflight(e.pre.facts().clone(), f);
                assert!(
                    !matches!(
                        r.decision,
                        NativeDecision::Ready | NativeDecision::Satisfied
                    ),
                    "before {:?}/{pointer}",
                    e.request.plan().action()
                );
            }
        }
    }
}

#[test]
fn no_change_is_limited_to_capacity_and_stop_before_dispatch() {
    let episodes = chain();
    for n in [1, 4] {
        let e = &episodes[n];
        let mut f = e.pre_evidence.facts().clone();
        if n == 1 {
            let target = e
                .evidence
                .facts()
                .target_config
                .as_ref()
                .unwrap()
                .result
                .as_ref()
                .unwrap()
                .clone();
            rebind_target(&mut f, target);
        } else {
            f.target_power = Some(read(power(101, false)));
            f.inventory = Some(read(inventory(
                &[source(), e.request.expected_before().config().clone()],
                false,
            )));
        }
        let proof = ProvisioningEvidenceV1::new(f).unwrap();
        assert_eq!(
            evaluate_provisioning_preflight(&e.pre, &proof, time()).reason,
            ProvisioningReasonV1::ObservedNoChange
        );
        assert!(ProvisioningStageBaselineV1::from_satisfied(&e.pre, &proof, time()).is_ok());
    }
    for n in [3, 6] {
        let e = &episodes[n];
        let mut f = e.pre_evidence.facts().clone();
        f.target_power = Some(read(power(101, true)));
        f.inventory = Some(read(inventory(
            &[source(), e.request.expected_before().config().clone()],
            true,
        )));
        assert_eq!(
            preflight(e.pre.facts().clone(), f).decision,
            NativeDecision::Unknown
        );
    }
}

#[test]
fn inventory_coverage_errors_collisions_and_legacy_projection_are_conservative() {
    let episodes = chain();
    let e = &episodes[0];
    let mut f = e.pre_evidence.facts().clone();
    f.inventory_coverage = ProvisioningCoverageV1::Partial;
    assert_eq!(
        preflight(e.pre.facts().clone(), f).reason,
        ProvisioningReasonV1::IncompleteIdentityCoverage
    );
    let mut f = e.pre_evidence.facts().clone();
    f.identities.clear();
    assert_eq!(
        preflight(e.pre.facts().clone(), f).reason,
        ProvisioningReasonV1::IncompleteIdentityCoverage
    );
    let mut f = e.pre_evidence.facts().clone();
    f.identities[0] = ProvisioningIdentityReadV1::new(
        node(),
        Vmid::new(900).unwrap(),
        NativeRead::new(time(), Err(PveReadError::TimedOut)),
    )
    .unwrap();
    assert_eq!(
        preflight(e.pre.facts().clone(), f).reason,
        ProvisioningReasonV1::IncompleteIdentityCoverage
    );
    for field in ["uuid", "mac"] {
        let mut f = e.pre_evidence.facts().clone();
        let changed = changed(&source(), |v| {
            v[field] = if field == "uuid" {
                json!(vm().uuid())
            } else {
                json!(vm().mac())
            }
        });
        f.identities[0] = ProvisioningIdentityReadV1::new(
            node(),
            Vmid::new(900).unwrap(),
            read(ProvisioningIdentitySnapshotV1::from_provisioning(&changed)),
        )
        .unwrap();
        assert_eq!(
            preflight(e.pre.facts().clone(), f).reason,
            ProvisioningReasonV1::IdentityCollision
        );
    }
    let legacy =
        NativeVmConfig::from_wire(node(), Vmid::new(900).unwrap(), source_json(), time()).unwrap();
    let projection =
        ProvisioningIdentitySnapshotV1::from_native(&legacy, NativeEvidenceSource::FakePve)
            .unwrap();
    // Rich-only fields in a legacy parse make coverage partial, never stronger.
    assert_eq!(projection.coverage(), ProvisioningCoverageV1::Partial);
}

#[test]
fn media_requires_both_exact_full_storage_catalogs() {
    let e = &chain()[0];
    for which in [0, 1] {
        let mut f = e.pre_evidence.facts().clone();
        f.media.remove(which);
        assert_eq!(
            preflight(e.pre.facts().clone(), f).reason,
            ProvisioningReasonV1::MediaCoverageIncomplete
        );
    }
    for coverage in [
        ProvisioningCoverageV1::Partial,
        ProvisioningCoverageV1::Complete,
    ] {
        let mut f = e.pre_evidence.facts().clone();
        f.media[1] = read(
            ProvisioningMediaInventoryV1::new(
                node(),
                StorageName::parse("drivers").unwrap(),
                vec![],
                coverage,
                time(),
            )
            .unwrap(),
        );
        assert_eq!(
            preflight(e.pre.facts().clone(), f).reason,
            if coverage == ProvisioningCoverageV1::Partial {
                ProvisioningReasonV1::MediaCoverageIncomplete
            } else {
                ProvisioningReasonV1::MediaMissing
            }
        );
    }
}

#[test]
fn task_failure_and_running_do_not_mask_replaced_disks() {
    for e in chain()
        .into_iter()
        .filter(|e| e.evidence.facts().task.is_some())
    {
        let original = e
            .evidence
            .facts()
            .task
            .as_ref()
            .unwrap()
            .result
            .as_ref()
            .unwrap();
        for state in [TaskState::Running, TaskState::CompleteFailure] {
            let mut f = e.evidence.facts().clone();
            f.task = Some(read(TaskStatus::new(
                original.upid().clone(),
                state,
                time(),
            )));
            let expected = if state == TaskState::Running {
                NativeDecision::Waiting
            } else {
                NativeDecision::Unknown
            };
            assert_eq!(
                outcome(e.outcome.facts().clone(), f.clone()).decision,
                expected
            );
            let target = changed(
                f.target_config.as_ref().unwrap().result.as_ref().unwrap(),
                |v| {
                    v["primary_disk"]["volume"] = json!(if e.request.plan().action()
                        == ProvisioningActionV1::Clone
                    {
                        "vm-900-disk-0"
                    } else {
                        "replacement"
                    })
                },
            );
            rebind_target(&mut f, target);
            assert_eq!(
                outcome(e.outcome.facts().clone(), f).decision,
                NativeDecision::Conflicted
            );
        }
        if e.request.plan().action() != ProvisioningActionV1::Clone {
            let mut f = e.evidence.facts().clone();
            f.target_power = Some(read(e.request.expected_before().power().clone()));
            rebind_target(&mut f, e.request.expected_before().config().clone());
            f.task = Some(read(TaskStatus::failed(original.upid().clone(), time())));
            assert_eq!(
                outcome(e.outcome.facts().clone(), f).decision,
                NativeDecision::Failed
            );
        }
    }
}

#[test]
fn evidence_persistence_is_required_nullable_and_route_validated() {
    for episode in chain() {
        let v = serde_json::to_value(&episode.evidence).unwrap();
        assert_eq!(
            serde_json::from_value::<ProvisioningEvidenceV1>(v.clone()).unwrap(),
            episode.evidence
        );
        for field in [
            "node",
            "storage",
            "bridges",
            "inventory",
            "source_config",
            "source_power",
            "target_config",
            "target_power",
            "qga",
            "task",
            "receipt",
        ] {
            let mut bad = v.clone();
            bad.as_object_mut().unwrap().remove(field);
            assert!(
                serde_json::from_value::<ProvisioningEvidenceV1>(bad).is_err(),
                "missing {field}"
            );
        }
        let mut bad = v.clone();
        bad["qga"] = Value::Null;
        assert!(serde_json::from_value::<ProvisioningEvidenceV1>(bad).is_ok());
        for pointer in [
            "/node/result/Ok/node",
            "/storage/result/Ok/node",
            "/bridges/result/Ok/node",
            "/source_power/result/Ok/node",
            "/target_power/result/Ok/node",
        ] {
            let mut bad = v.clone();
            *bad.pointer_mut(pointer).unwrap() = json!("wrong-node");
            assert!(
                serde_json::from_value::<ProvisioningEvidenceV1>(bad).is_err(),
                "route {pointer}"
            );
        }
        for field in [
            "node",
            "storage",
            "bridges",
            "inventory",
            "source_config",
            "source_power",
            "target_config",
            "target_power",
            "task",
        ] {
            let mut bad = v.clone();
            bad[field] = json!({"observed_at":time(),"result":{"Err":PveReadError::Unauthorized}});
            let f: ProvisioningEvidenceInputV1 = serde_json::from_value(bad).unwrap();
            assert_eq!(
                outcome(episode.outcome.facts().clone(), f).reason,
                ProvisioningReasonV1::Unauthorized,
                "{field}"
            );
        }
    }
}

#[test]
fn historical_owner_and_predecessor_cannot_cross_run_workflow_or_request() {
    let episodes = chain();
    for e in episodes.iter().skip(1) {
        for field in ["run_id", "workflow_sha256", "operation_plan_sha256"] {
            let mut c = e.pre.facts().clone();
            let mut b = serde_json::to_value(&c.binding).unwrap();
            b[field] = if field == "run_id" {
                json!(id::<controller_domain::RunId>(555))
            } else {
                json!("a".repeat(64))
            };
            c.binding = serde_json::from_value(b).unwrap();
            assert!(ProvisioningEvaluationContextV1::new(c).is_err(), "{field}");
        }
        let mut c = e.pre.facts().clone();
        c.clone_request = CloneRequest::new(vm(), c.clone_request.operation_id());
        assert!(ProvisioningEvaluationContextV1::new(c).is_err());
        let mut c = e.pre.facts().clone();
        c.predecessor = None;
        assert!(ProvisioningEvaluationContextV1::new(c).is_err());
    }
    let clone = &episodes[0];
    let f: ProvisioningEvidenceV1 =
        serde_json::from_value(serde_json::to_value(&clone.evidence).unwrap()).unwrap();
    let historical =
        ProvisioningCloneOwnershipV1::from_satisfied_clone(&clone.outcome, &f, time()).unwrap();
    assert_eq!(historical.bound_at(), time());
    assert_eq!(historical.config(), &intermediate());
    assert!(
        ProvisioningCloneOwnershipV1::from_satisfied_clone(
            &clone.outcome,
            &f,
            time() + Duration::seconds(60)
        )
        .is_err()
    );
}

#[test]
fn failed_clone_with_exact_unchanged_vacancy_is_failed() {
    let e = &chain()[0];
    let mut f = e.pre_evidence.facts().clone();
    f.binding = e.evidence.facts().binding.clone();
    f.receipt = e.evidence.facts().receipt.clone();
    let upid = e
        .evidence
        .facts()
        .task
        .as_ref()
        .unwrap()
        .result
        .as_ref()
        .unwrap()
        .upid()
        .clone();
    f.task = Some(read(TaskStatus::failed(upid, time())));
    assert_eq!(
        outcome(e.outcome.facts().clone(), f).decision,
        NativeDecision::Failed
    );
}

#[test]
fn outcome_cannot_use_stale_source_snapshot_in_inventory_comparison() {
    let e = &chain()[0];
    let mut f = e.evidence.facts().clone();
    let s = changed(&source(), |v| {
        v["observed_at"] = json!(time() - Duration::seconds(31))
    });
    f.source_config = Some(read(s));
    assert_eq!(
        outcome(e.outcome.facts().clone(), f).reason,
        ProvisioningReasonV1::ObservationNotFresh
    );
}

#[test]
fn clone_outcome_source_change_and_request_provenance_are_conflicts() {
    let e = &chain()[0];
    let mut f = e.evidence.facts().clone();
    let s = changed(&source(), |v| v["cores"] = json!(8));
    f.source_config = Some(read(s.clone()));
    f.identities[0] = ProvisioningIdentityReadV1::new(
        node(),
        s.vmid(),
        read(ProvisioningIdentitySnapshotV1::from_provisioning(&s)),
    )
    .unwrap();
    assert_eq!(
        outcome(e.outcome.facts().clone(), f).decision,
        NativeDecision::Conflicted
    );
}

#[test]
fn wrong_identity_projection_duplicate_keys_and_incomplete_catalogs_are_rejected() {
    let e = &chain()[0];
    let mut f = e.pre_evidence.facts().clone();
    f.identities.push(f.identities[0].clone());
    assert!(ProvisioningEvidenceV1::new(f).is_err());
    let mut f = e.pre_evidence.facts().clone();
    f.media.push(f.media[0].clone());
    assert!(ProvisioningEvidenceV1::new(f).is_err());
    let mut f = e.pre_evidence.facts().clone();
    f.media[1] = f.media[0].clone();
    assert!(ProvisioningEvidenceV1::new(f).is_err());
    let mut f = e.pre_evidence.facts().clone();
    f.source = NativeEvidenceSource::PveApi;
    assert!(ProvisioningEvidenceV1::new(f).is_err());
    assert!(
        ProvisioningIdentityReadV1::new(
            NodeName::parse("wrong").unwrap(),
            Vmid::new(900).unwrap(),
            read(ProvisioningIdentitySnapshotV1::from_provisioning(&source()))
        )
        .is_err()
    );
}

#[test]
fn same_target_vmid_on_another_node_blocks_even_with_local_404() {
    let e = &chain()[0];
    let mut f = e.pre_evidence.facts().clone();
    let target = changed(&intermediate(), |v| v["node"] = json!("other"));
    f.inventory = Some(read(inventory(&[source(), target.clone()], false)));
    f.identities.push(
        ProvisioningIdentityReadV1::new(
            target.node().clone(),
            target.vmid(),
            read(ProvisioningIdentitySnapshotV1::from_provisioning(&target)),
        )
        .unwrap(),
    );
    assert_eq!(
        preflight(e.pre.facts().clone(), f).reason,
        ProvisioningReasonV1::TargetOccupied
    );
}

#[test]
fn retained_capacity_including_nonintegral_bytes_continues_without_resize() {
    for bytes in [80 * GIB, 80 * GIB + 17, u64::MAX] {
        let c = chain_variant(bytes, bytes, "DISK-101");
        assert_eq!(c.len(), 6);
        assert!(
            !c.iter()
                .any(|e| e.request.plan().action() == ProvisioningActionV1::EnsureCapacity)
        );
        for e in c.iter().skip(1) {
            assert_eq!(
                e.request
                    .expected_before()
                    .config()
                    .primary_disk()
                    .capacity_bytes(),
                bytes
            );
        }
    }
}

#[test]
fn supported_layout_lock_template_and_digest_changes_are_independent() {
    for e in chain().into_iter().skip(1) {
        for (field, value, unsupported) in [
            ("firmware", json!("unsupported"), Some("firmware")),
            ("cpu", json!("unsupported"), Some("cpu")),
            ("balloon_mib", json!(128), Some("balloon")),
            ("qga_channel", json!("unsupported"), Some("qga_channel")),
            ("locked", json!(true), None),
            ("template", json!(true), None),
        ] {
            let mut f = e.evidence.facts().clone();
            let target = changed(
                f.target_config.as_ref().unwrap().result.as_ref().unwrap(),
                |v| {
                    v[field] = value;
                    if let Some(reason) = unsupported {
                        v["unsupported"] = json!([reason]);
                    }
                },
            );
            rebind_target(&mut f, target);
            assert_eq!(
                outcome(e.outcome.facts().clone(), f).decision,
                NativeDecision::Conflicted,
                "{field}"
            );
        }
    }
}

#[test]
fn complete_legacy_identity_projection_can_cover_shared_namespace() {
    let e = &chain()[0];
    let mut raw = source_json();
    for key in ["bios", "cpu", "balloon"] {
        raw.as_object_mut().unwrap().remove(key);
    }
    let legacy = NativeVmConfig::from_wire(node(), Vmid::new(900).unwrap(), raw, time()).unwrap();
    let projection =
        ProvisioningIdentitySnapshotV1::from_native(&legacy, NativeEvidenceSource::FakePve)
            .unwrap();
    assert_eq!(projection.coverage(), ProvisioningCoverageV1::Complete);
    let mut f = e.pre_evidence.facts().clone();
    f.identities[0] =
        ProvisioningIdentityReadV1::new(node(), Vmid::new(900).unwrap(), read(projection)).unwrap();
    assert_eq!(
        preflight(e.pre.facts().clone(), f).decision,
        NativeDecision::Ready
    );
}

#[test]
fn task_success_does_not_replace_missing_physical_postconditions() {
    for e in chain().into_iter().skip(1) {
        let mut f = e.evidence.facts().clone();
        f.target_power = Some(read(e.request.expected_before().power().clone()));
        rebind_target(&mut f, e.request.expected_before().config().clone());
        assert_eq!(
            outcome(e.outcome.facts().clone(), f).decision,
            NativeDecision::Unknown
        );
    }
}

#[test]
fn receipt_collection_clock_and_absence_power_contradiction_are_checked() {
    let episodes = chain();
    let e = &episodes[0];
    let mut f = e.pre_evidence.facts().clone();
    f.target_power = Some(read(power(101, false)));
    assert_eq!(
        preflight(e.pre.facts().clone(), f).reason,
        ProvisioningReasonV1::InventoryContradiction
    );
    let mut c = e.outcome.facts().clone();
    let ProvisioningDispatchStateV1::Recorded { dispatch, receipt } = &mut c.dispatch else {
        unreachable!()
    };
    *receipt = Some(
        ProvisioningReceiptV1::new(
            dispatch.clone(),
            time() + Duration::seconds(1),
            receipt.as_ref().unwrap().receipt().clone(),
        )
        .unwrap(),
    );
    let mut f = e.evidence.facts().clone();
    f.receipt = receipt.clone();
    let r = evaluate_provisioning_outcome(
        &ProvisioningEvaluationContextV1::new(c).unwrap(),
        &ProvisioningEvidenceV1::new(f).unwrap(),
        time() + Duration::seconds(2),
    );
    assert_eq!(r.reason, ProvisioningReasonV1::ObservationNotFresh);
}
