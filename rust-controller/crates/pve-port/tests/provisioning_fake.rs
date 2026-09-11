mod provisioning_support;
use provisioning_support::*;
use pve_port::*;
#[path = "provisioning_support/world.rs"]
mod world;
use chrono::Utc;

#[tokio::test]
async fn scripted_errors_and_pauses_are_independent_observations() {
    let w = World::new(80 * GIB, 120 * GIB);
    let id = vm().source_vmid();
    let storage = StorageName::parse("local").unwrap();
    w.fake.enqueue_provisioning_config_read(
        node(),
        id,
        FakeProvisioningConfigReadV1::Error(PveReadError::NotFound),
    );
    w.fake.enqueue_provisioning_identity_read(
        node(),
        id,
        FakeProvisioningIdentityReadV1::Error(PveReadError::InvalidResponse),
    );
    w.fake.enqueue_provisioning_media_read(
        node(),
        storage.clone(),
        FakeProvisioningMediaReadV1::Error(PveReadError::NotFound),
    );
    assert_eq!(
        w.fake.provisioning_vm_config(&node(), id).await,
        Err(PveReadError::NotFound)
    );
    assert_eq!(
        w.fake.provisioning_identity(&node(), id).await,
        Err(PveReadError::InvalidResponse)
    );
    assert_eq!(
        w.fake.provisioning_media(&node(), &storage).await,
        Err(PveReadError::NotFound)
    );
    for kind in 0..3 {
        let gate = std::sync::Arc::new(FakePause::default());
        match kind {
            0 => w.fake.enqueue_provisioning_config_read(
                node(),
                id,
                FakeProvisioningConfigReadV1::Pause(gate.clone()),
            ),
            1 => w.fake.enqueue_provisioning_identity_read(
                node(),
                id,
                FakeProvisioningIdentityReadV1::Pause(gate.clone()),
            ),
            _ => w.fake.enqueue_provisioning_media_read(
                node(),
                storage.clone(),
                FakeProvisioningMediaReadV1::Pause(gate.clone()),
            ),
        }
        let fake = w.fake.clone();
        let storage = storage.clone();
        let task = tokio::spawn(async move {
            match kind {
                0 => fake
                    .provisioning_vm_config(&node(), id)
                    .await
                    .map(|c| c.digest().to_string()),
                1 => fake
                    .provisioning_identity(&node(), id)
                    .await
                    .map(|c| c.config_digest().to_string()),
                _ => fake
                    .provisioning_media(&node(), &storage)
                    .await
                    .map(|m| m.iso_volids().len().to_string()),
            }
        });
        tokio::time::timeout(std::time::Duration::from_secs(2), gate.entered())
            .await
            .unwrap();
        let current = changed(&source(), |v| v["digest"] = json!("after-pause"));
        w.fake
            .replace_provisioning_vm(current, PowerState::Stopped)
            .unwrap();
        gate.release();
        assert_eq!(
            task.await.unwrap().unwrap(),
            if kind == 2 { "1" } else { "after-pause" }
        );
    }
    assert!(w.fake.recorded_provisioning_submissions().is_empty());
}

#[tokio::test]
async fn fixture_source_and_script_source_cannot_claim_live_pve() {
    let w = World::new(80 * GIB, 120 * GIB);
    let live = changed(&source(), |v| v["source"] = json!("pve_api"));
    assert_eq!(
        w.fake
            .replace_provisioning_vm(live.clone(), PowerState::Stopped),
        Err(PveWriteError::Rejected)
    );
    w.fake.enqueue_provisioning_config_read(
        node(),
        vm().source_vmid(),
        FakeProvisioningConfigReadV1::Snapshot(Box::new(live.clone())),
    );
    assert_eq!(
        w.fake
            .provisioning_vm_config(&node(), vm().source_vmid())
            .await,
        Err(PveReadError::InvalidResponse)
    );
    w.fake.enqueue_provisioning_identity_read(
        node(),
        vm().source_vmid(),
        FakeProvisioningIdentityReadV1::Snapshot(Box::new(
            ProvisioningIdentitySnapshotV1::from_provisioning(&live),
        )),
    );
    assert_eq!(
        w.fake
            .provisioning_identity(&node(), vm().source_vmid())
            .await,
        Err(PveReadError::InvalidResponse)
    );
    assert_eq!(
        semantic(
            &w.fake
                .provisioning_vm_config(&node(), vm().source_vmid())
                .await
                .unwrap()
        ),
        semantic(&source())
    );
}

#[tokio::test]
async fn history_preserves_exact_ordered_forms_and_only_allowed_effects() {
    use ProvisioningActionV1::*;
    let mut w = World::new(80 * GIB, 120 * GIB);
    for action in [
        Clone,
        EnsureCapacity,
        ConfigurePe,
        StartPe,
        EnsureStopped,
        ConfigureDisk,
        StartDisk,
    ] {
        let request = w.step(action).await.unwrap();
        let b = request.expected_before().config();
        let pairs: Vec<(&str, String)> = match action {
            Clone => vec![
                ("newid", "101".into()),
                ("name", "deploy-101".into()),
                ("full", "1".into()),
                ("storage", "local-lvm".into()),
            ],
            EnsureCapacity => vec![
                ("disk", "scsi0".into()),
                ("size", "120G".into()),
                ("digest", b.digest().into()),
            ],
            ConfigurePe => vec![
                ("digest", b.digest().into()),
                ("cores", "4".into()),
                ("memory", "4096".into()),
                ("cpu", "host".into()),
                ("balloon", "0".into()),
                ("bios", "seabios".into()),
                ("agent", "enabled=1,type=virtio".into()),
                (
                    "smbios1",
                    "uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3301,serial=SYS-101".into(),
                ),
                (
                    "net0",
                    "virtio=02:00:00:00:01:01,bridge=vmbr0,firewall=0".into(),
                ),
                (
                    "scsi0",
                    format!("local-lvm:{},serial=DISK-101", b.primary_disk().volume()),
                ),
                ("ide2", "local:iso/deployment.iso,media=cdrom".into()),
                ("ide3", "drivers:iso/virtio.iso,media=cdrom".into()),
                ("boot", "order=ide2;scsi0".into()),
            ],
            ConfigureDisk => vec![
                ("digest", b.digest().into()),
                ("delete", "ide2,ide3".into()),
                ("boot", "order=scsi0".into()),
            ],
            _ => vec![],
        };
        let records = w.fake.recorded_provisioning_submissions();
        let record = records.last().unwrap();
        assert_eq!(record.request(), &request);
        assert_eq!(record.request().form(), pairs);
        assert_eq!(
            record.request().method(),
            if matches!(action, EnsureCapacity | ConfigurePe | ConfigureDisk) {
                "PUT"
            } else {
                "POST"
            }
        );
        let suffix = match action {
            Clone => vec!["900", "clone"],
            EnsureCapacity => vec!["101", "resize"],
            ConfigurePe | ConfigureDisk => vec!["101", "config"],
            EnsureStopped => vec!["101", "status", "stop"],
            _ => vec!["101", "status", "start"],
        };
        let mut path = vec!["nodes", "pve-test", "qemu"];
        path.extend(suffix);
        assert_eq!(record.request().path_segments(), path);
        let after = w
            .fake
            .provisioning_vm_config(&node(), vm().target_vmid())
            .await
            .unwrap();
        if matches!(action, Clone | EnsureCapacity | ConfigurePe | ConfigureDisk) {
            assert_ne!(after.digest(), b.digest());
        }
        if action != Clone {
            assert_eq!(after.primary_disk().volume(), b.primary_disk().volume());
        }
        if action == EnsureCapacity {
            let mut before = semantic(b);
            let mut after = semantic(&after);
            for v in [&mut before, &mut after] {
                v.as_object_mut().unwrap().remove("digest");
                v["primary_disk"]
                    .as_object_mut()
                    .unwrap()
                    .remove("capacity_bytes");
            }
            assert_eq!(before, after);
        }
        if action == ConfigureDisk {
            let mut before = semantic(b);
            let mut after = semantic(&after);
            for v in [&mut before, &mut after] {
                for key in ["digest", "deployment_iso", "driver_iso", "boot_profile"] {
                    v.as_object_mut().unwrap().remove(key);
                }
            }
            assert_eq!(before, after);
        }
    }
}

#[tokio::test]
async fn clone_preserves_source_serials_resources_and_media_without_final_preconfiguration() {
    let w = World::new(80 * GIB, 120 * GIB);
    let source = changed(&source(), |v| {
        v["system_serial"] = json!("SOURCE-SERIAL");
        v["primary_disk"]["serial"] = json!("SOURCE-DISK");
        v["cores"] = json!(8);
        v["memory_mib"] = json!(8192);
        v["qga_enabled"] = json!(true);
    });
    w.fake
        .replace_provisioning_vm(source.clone(), PowerState::Stopped)
        .unwrap();
    let e = ProvisioningExpectationsV1::new(ProvisioningExpectationsInputV1 {
        vm: vm(),
        template_config_sha256: &source.template_fingerprint().unwrap(),
        template_capacity_bytes: 80 * GIB,
        effective_capacity_bytes: 120 * GIB,
        system_serial: "SYS-101",
        disk_serial: "DISK-101",
        deployment_iso_volid: "local:iso/deployment.iso",
        driver_iso_volid: "drivers:iso/virtio.iso",
    })
    .unwrap();
    let p = ProvisioningOperationPlanV1::new(ProvisioningActionV1::Clone, e);
    let b = ProvisioningBindingV1::new(id(1), clone_request().operation_id(), id(1010), SHA, &p, 0)
        .unwrap();
    let r = ProvisioningMutationRequestV1::Clone(
        CloneProvisioningRequestV1::new(
            b,
            p,
            clone_request(),
            before(source.clone(), false),
            time(),
            30,
        )
        .unwrap(),
    );
    w.fake.submit_provisioning(&r).await.unwrap();
    let c = w
        .fake
        .provisioning_vm_config(&node(), vm().target_vmid())
        .await
        .unwrap();
    assert_eq!(c.system_serial(), Some("SOURCE-SERIAL"));
    assert_eq!(c.primary_disk().serial(), Some("SOURCE-DISK"));
    assert_eq!(c.cores(), 8);
    assert_eq!(c.memory_mib(), 8192);
    assert!(c.qga_enabled());
    assert_eq!(c.deployment_iso(), source.deployment_iso());
    assert_eq!(c.driver_iso(), source.driver_iso());
    assert_eq!(c.boot_profile(), source.boot_profile());
    assert!(
        !w.fake
            .qga_ping(&node(), vm().target_vmid())
            .await
            .unwrap()
            .reachable()
    );
}

#[tokio::test]
async fn accepted_task_can_physically_finish_after_strict_controller_deadline() {
    let w = World::new(80 * GIB, 120 * GIB);
    let (context, request) = w.prepare(ProvisioningActionV1::Clone).await;
    w.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
            FakeMutationOutcome::AcceptedTaskDelayed,
        )
        .unwrap();
    let sent = Utc::now();
    let receipt = w.fake.submit_provisioning(&request).await.unwrap();
    let dispatch = ProvisioningDispatchV1::new(ProvisioningDispatchInputV1 {
        request,
        source: NativeEvidenceSource::FakePve,
        preflight_event_id: id(9000),
        original_generation: 1,
        dispatch_revision: 3,
        dispatched_at: sent,
    })
    .unwrap();
    let receipt = ProvisioningReceiptV1::new(dispatch.clone(), Utc::now(), receipt).unwrap();
    let mut expired = context.facts().clone();
    expired.mutation_deadline = sent;
    expired.mode = ProvisioningEvaluationModeV1::Outcome;
    expired.state = controller_domain::ExecutionState::Running;
    expired.dispatch = ProvisioningDispatchStateV1::Recorded {
        dispatch,
        receipt: Some(receipt.clone()),
    };
    let expired = ProvisioningEvaluationContextV1::new(expired).unwrap();
    w.fake
        .complete_provisioning_task(upid(receipt.receipt()))
        .unwrap();
    let evidence = w.collect(&expired).await;
    let result = evaluate_provisioning_outcome(&expired, &evidence, Utc::now());
    assert_eq!(result.decision, NativeDecision::Unknown);
    assert_eq!(result.reason, ProvisioningReasonV1::DeadlineExpired);
    assert_eq!(
        w.fake
            .task_status(&node(), upid(receipt.receipt()))
            .await
            .unwrap()
            .state(),
        TaskState::CompleteSuccess
    );
    assert_eq!(
        w.fake
            .provisioning_vm_config(&node(), vm().target_vmid())
            .await
            .unwrap()
            .primary_disk()
            .capacity_bytes(),
        80 * GIB
    );
}

#[tokio::test]
async fn current_power_route_media_and_catalog_changes_are_rejected_at_admission() {
    use ProvisioningActionV1::*;
    for action in [
        Clone,
        EnsureCapacity,
        ConfigurePe,
        StartPe,
        EnsureStopped,
        ConfigureDisk,
        StartDisk,
    ] {
        let mut w = World::new(80 * GIB, 120 * GIB);
        w.through(action).await;
        let (_, r) = w.prepare(action).await;
        let id = r.expected_before().config().vmid();
        let changed_power = if action == EnsureStopped {
            PowerState::Stopped
        } else {
            PowerState::Running
        };
        w.fake
            .set_provisioning_power(&node(), id, changed_power)
            .unwrap();
        assert_eq!(
            w.fake.submit_provisioning(&r).await,
            Err(PveWriteError::Conflict)
        );
        assert_eq!(
            w.fake.vm_status(&node(), id).await.unwrap().power(),
            changed_power
        );
    }
    for action in [Clone, ConfigurePe, StartPe] {
        for partial in [false, true] {
            let mut w = World::new(80 * GIB, 120 * GIB);
            w.through(action).await;
            let (_, r) = w.prepare(action).await;
            w.fake.set_provisioning_media(
                ProvisioningMediaInventoryV1::new(
                    node(),
                    StorageName::parse("drivers").unwrap(),
                    if partial {
                        vec!["drivers:iso/virtio.iso".into()]
                    } else {
                        vec![]
                    },
                    if partial {
                        ProvisioningCoverageV1::Partial
                    } else {
                        ProvisioningCoverageV1::Complete
                    },
                    Utc::now(),
                )
                .unwrap(),
            );
            let prior = w
                .fake
                .provisioning_vm_config(&node(), vm().target_vmid())
                .await
                .ok()
                .map(|c| semantic(&c));
            assert_eq!(
                w.fake.submit_provisioning(&r).await,
                Err(PveWriteError::Rejected)
            );
            assert_eq!(
                w.fake
                    .provisioning_vm_config(&node(), vm().target_vmid())
                    .await
                    .ok()
                    .map(|c| semantic(&c)),
                prior
            );
        }
    }
    for action in [Clone, ConfigurePe] {
        let mut w = World::new(80 * GIB, 120 * GIB);
        w.through(action).await;
        let (_, r) = w.prepare(action).await;
        let other_route = changed(r.expected_before().config(), |v| {
            v["node"] = json!("wrong-node")
        });
        w.fake
            .replace_provisioning_vm(other_route, PowerState::Stopped)
            .unwrap();
        assert_eq!(
            w.fake.submit_provisioning(&r).await,
            Err(PveWriteError::Conflict)
        );
    }
}

#[tokio::test]
async fn delayed_clone_rejects_new_target_and_legacy_replacement_without_touching_occupants() {
    for replace_source in [false, true] {
        let w = World::new(80 * GIB, 120 * GIB);
        let (_, r) = w.prepare(ProvisioningActionV1::Clone).await;
        w.fake
            .enqueue_provisioning_outcome(
                ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
                FakeMutationOutcome::AcceptedTaskDelayed,
            )
            .unwrap();
        let receipt = w.fake.submit_provisioning(&r).await.unwrap();
        let old = old_config(if replace_source { 900 } else { 101 }, false, false);
        if replace_source {
            w.fake.replace_vm(old.clone(), PowerState::Stopped).unwrap();
        } else {
            w.fake.insert_vm(old.clone(), PowerState::Stopped).unwrap();
        }
        assert_eq!(
            w.fake.complete_provisioning_task(upid(&receipt)),
            Err(PveWriteError::Conflict)
        );
        let observed = w.fake.native_vm_config(&node(), old.vmid()).await.unwrap();
        assert_eq!(observed.digest(), old.digest());
        assert_eq!(observed.uuid(), old.uuid());
        assert_eq!(
            w.fake.provisioning_vm_config(&node(), old.vmid()).await,
            Err(PveReadError::InvalidResponse)
        );
    }
}
use serde_json::json;
use world::*;

fn upid(r: &MutationReceipt) -> &Upid {
    match r {
        MutationReceipt::Task(u) => u,
        _ => panic!("expected task"),
    }
}
fn semantic(c: &ProvisioningVmConfigV1) -> serde_json::Value {
    let mut v = serde_json::to_value(c).unwrap();
    v.as_object_mut().unwrap().remove("observed_at");
    v
}

#[tokio::test]
async fn selector_priority_fifo_duplicate_and_failed_admission_do_not_consume_faults() {
    let w = World::new(80 * GIB, 120 * GIB);
    let (_, r) = w.prepare(ProvisioningActionV1::Clone).await;
    let general = ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None);
    w.fake
        .enqueue_provisioning_outcome(
            general.clone(),
            FakeMutationOutcome::Rejected(PveWriteError::Rejected),
        )
        .unwrap();
    w.fake
        .enqueue_provisioning_outcome(general, FakeMutationOutcome::AcceptedTaskFails)
        .unwrap();
    w.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, Some(r.operation_id())),
            FakeMutationOutcome::AcceptedTaskFails,
        )
        .unwrap();
    let first = w.fake.submit_provisioning(&r).await.unwrap();
    assert_eq!(
        w.fake
            .task_status(&node(), upid(&first))
            .await
            .unwrap()
            .state(),
        TaskState::CompleteFailure
    );
    assert_eq!(
        w.fake.submit_provisioning(&r).await,
        Err(PveWriteError::Conflict)
    );
    let second = different_operation(&r, 200);
    assert_eq!(
        w.fake.submit_provisioning(&second).await,
        Err(PveWriteError::Rejected)
    );
    assert_eq!(
        w.fake.submit_provisioning(&second).await,
        Err(PveWriteError::Conflict)
    );
    let blocked = different_operation(&r, 201);
    let tampered = changed(&source(), |v| v["cores"] = json!(8));
    w.fake
        .replace_provisioning_vm(tampered, PowerState::Stopped)
        .unwrap();
    assert_eq!(
        w.fake.submit_provisioning(&blocked).await,
        Err(PveWriteError::Conflict)
    );
    w.fake
        .replace_provisioning_vm(source(), PowerState::Stopped)
        .unwrap();
    assert_eq!(
        w.fake.submit_provisioning(&blocked).await,
        Err(PveWriteError::Conflict)
    );
    let last = w
        .fake
        .submit_provisioning(&different_operation(&r, 202))
        .await
        .unwrap();
    assert_eq!(
        w.fake
            .task_status(&node(), upid(&last))
            .await
            .unwrap()
            .state(),
        TaskState::CompleteFailure
    );
    assert_eq!(
        w.fake
            .recorded_provisioning_submissions()
            .iter()
            .filter(|r| r.acceptance().is_some())
            .count(),
        2
    );
    assert_eq!(
        w.fake
            .provisioning_vm_config(&node(), vm().target_vmid())
            .await,
        Err(PveReadError::NotFound)
    );
}

#[tokio::test]
async fn pe_and_disk_faults_are_independent_despite_identical_start_forms() {
    use ProvisioningActionV1::*;
    let mut w = World::new(80 * GIB, 120 * GIB);
    w.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(StartDisk, None),
            FakeMutationOutcome::Rejected(PveWriteError::Rejected),
        )
        .unwrap();
    w.through(StartDisk).await;
    let (_, disk) = w.prepare(StartDisk).await;
    assert_eq!(
        w.fake.submit_provisioning(&disk).await,
        Err(PveWriteError::Rejected)
    );
    assert_eq!(
        w.fake
            .vm_status(&node(), vm().target_vmid())
            .await
            .unwrap()
            .power(),
        PowerState::Stopped
    );
    let mut w = World::new(80 * GIB, 120 * GIB);
    w.through(StartDisk).await;
    w.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(StartPe, None),
            FakeMutationOutcome::Rejected(PveWriteError::Rejected),
        )
        .unwrap();
    w.step(StartDisk).await;
    assert_eq!(
        ProvisioningFaultSelectorV1::new(EnsureStopped, None).profile(),
        Some(ProvisioningBootProfile::PeMedia)
    );
    assert_eq!(
        ProvisioningFaultSelectorV1::new(StartDisk, None).profile(),
        Some(ProvisioningBootProfile::InstalledDisk)
    );
}

fn old_plan(target: u32) -> NativeVmPlan {
    let mut v = serde_json::to_value(vm()).unwrap();
    v["source_vmid"] = json!(800);
    v["target_vmid"] = json!(target);
    serde_json::from_value(v).unwrap()
}
#[tokio::test]
async fn pending_resources_overlap_across_families_and_completions_keep_separate_queues() {
    let w = World::new(80 * GIB, 120 * GIB);
    let (_, r) = w.prepare(ProvisioningActionV1::Clone).await;
    w.fake
        .insert_vm(old_config(800, false, false), PowerState::Stopped)
        .unwrap();
    w.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::Clone, None),
            FakeMutationOutcome::AcceptedTaskDelayed,
        )
        .unwrap();
    let rich = w.fake.submit_provisioning(&r).await.unwrap();
    assert_eq!(
        w.fake
            .clone_vm(&CloneRequest::new(
                old_plan(101),
                controller_domain::OperationId::new()
            ))
            .await,
        Err(PveWriteError::Conflict)
    );
    assert_eq!(
        w.fake
            .submit_provisioning(&different_operation(&r, 201))
            .await,
        Err(PveWriteError::Conflict)
    );
    w.fake
        .enqueue_outcome(NativeStep::Clone, FakeMutationOutcome::AcceptedTaskDelayed)
        .unwrap();
    let legacy = w
        .fake
        .clone_vm(&CloneRequest::new(
            old_plan(801),
            controller_domain::OperationId::new(),
        ))
        .await
        .unwrap();
    assert_ne!(upid(&rich), upid(&legacy));
    assert_eq!(
        w.fake.complete_provisioning_task(upid(&legacy)),
        Err(PveWriteError::Rejected)
    );
    w.fake.complete_pending().unwrap();
    assert_eq!(
        w.fake
            .task_status(&node(), upid(&legacy))
            .await
            .unwrap()
            .state(),
        TaskState::CompleteSuccess
    );
    assert_eq!(
        w.fake
            .task_status(&node(), upid(&rich))
            .await
            .unwrap()
            .state(),
        TaskState::Running
    );
    w.fake.complete_provisioning_task(upid(&rich)).unwrap();
    assert_eq!(w.fake.complete_pending(), Err(PveWriteError::Rejected));
    assert_eq!(
        w.fake
            .task_status(&NodeName::parse("wrong-node").unwrap(), upid(&rich))
            .await,
        Err(PveReadError::InvalidResponse)
    );
    let unknown =
        Upid::parse("UPID:pve-test:000000FF:00000001:00000001:qmstart:101:fake@pve:").unwrap();
    assert_eq!(
        w.fake.complete_provisioning_task(&unknown),
        Err(PveWriteError::Rejected)
    );

    let w = World::new(80 * GIB, 120 * GIB);
    let (_, r) = w.prepare(ProvisioningActionV1::Clone).await;
    w.fake
        .insert_vm(old_config(800, false, false), PowerState::Stopped)
        .unwrap();
    w.fake
        .enqueue_outcome(NativeStep::Clone, FakeMutationOutcome::AcceptedTaskDelayed)
        .unwrap();
    w.fake
        .clone_vm(&CloneRequest::new(
            old_plan(101),
            controller_domain::OperationId::new(),
        ))
        .await
        .unwrap();
    assert_eq!(
        w.fake.submit_provisioning(&r).await,
        Err(PveWriteError::Conflict)
    );
    assert_eq!(
        w.fake
            .provisioning_vm_config(&node(), vm().target_vmid())
            .await,
        Err(PveReadError::NotFound)
    );
}

#[tokio::test]
async fn legacy_fault_and_pause_controls_do_not_leak_into_provisioning() {
    let w = World::new(80 * GIB, 120 * GIB);
    let (_, r) = w.prepare(ProvisioningActionV1::Clone).await;
    w.fake
        .enqueue_outcome(
            NativeStep::Clone,
            FakeMutationOutcome::Rejected(PveWriteError::Rejected),
        )
        .unwrap();
    let pause = w.fake.pause_next_submission();
    tokio::time::timeout(
        std::time::Duration::from_secs(2),
        w.fake.submit_provisioning(&r),
    )
    .await
    .unwrap()
    .unwrap();
    w.fake
        .insert_vm(old_config(800, false, false), PowerState::Stopped)
        .unwrap();
    let fake = w.fake.clone();
    let task = tokio::spawn(async move {
        fake.clone_vm(&CloneRequest::new(
            old_plan(801),
            controller_domain::OperationId::new(),
        ))
        .await
    });
    tokio::time::timeout(std::time::Duration::from_secs(2), pause.entered())
        .await
        .unwrap();
    pause.release();
    assert_eq!(task.await.unwrap(), Err(PveWriteError::Rejected));
}

#[tokio::test]
async fn provisioning_pause_rechecks_world_and_records_only_resumed_admission() {
    let w = World::new(80 * GIB, 120 * GIB);
    let (_, r) = w.prepare(ProvisioningActionV1::Clone).await;
    let gate = w
        .fake
        .pause_provisioning_submission(ProvisioningFaultSelectorV1::new(
            ProvisioningActionV1::Clone,
            Some(r.operation_id()),
        ));
    let fake = w.fake.clone();
    let task = tokio::spawn(async move { fake.submit_provisioning(&r).await });
    tokio::time::timeout(std::time::Duration::from_secs(2), gate.entered())
        .await
        .unwrap();
    assert!(w.fake.recorded_provisioning_submissions().is_empty());
    w.fake
        .set_provisioning_power(&node(), vm().source_vmid(), PowerState::Running)
        .unwrap();
    gate.release();
    assert_eq!(task.await.unwrap(), Err(PveWriteError::Conflict));
    assert_eq!(w.fake.recorded_provisioning_submissions().len(), 1);
    assert_eq!(
        w.fake
            .vm_status(&node(), vm().source_vmid())
            .await
            .unwrap()
            .power(),
        PowerState::Running
    );
}

#[tokio::test]
async fn scripts_preserve_routes_timestamps_and_unsupported_facts_without_world_effects() {
    let w = World::new(80 * GIB, 120 * GIB);
    let bad = changed(&source(), |v| {
        v["unsupported"] = json!(["unknown_field"]);
    });
    w.fake
        .replace_provisioning_vm(bad.clone(), PowerState::Stopped)
        .unwrap();
    let fresh = w
        .fake
        .provisioning_vm_config(&node(), vm().source_vmid())
        .await
        .unwrap();
    assert_eq!(fresh.unsupported(), bad.unsupported());
    assert!(fresh.observed_at() > bad.observed_at());
    w.fake.enqueue_provisioning_config_read(
        node(),
        vm().source_vmid(),
        FakeProvisioningConfigReadV1::Snapshot(Box::new(source())),
    );
    assert_eq!(
        w.fake
            .provisioning_vm_config(&node(), vm().source_vmid())
            .await
            .unwrap(),
        source()
    );
    assert_eq!(
        w.fake
            .provisioning_vm_config(&node(), vm().source_vmid())
            .await
            .unwrap()
            .unsupported(),
        bad.unsupported()
    );
    let wrong = NodeName::parse("wrong-node").unwrap();
    w.fake.enqueue_provisioning_config_read(
        wrong.clone(),
        vm().source_vmid(),
        FakeProvisioningConfigReadV1::Snapshot(Box::new(source())),
    );
    assert_eq!(
        w.fake
            .provisioning_vm_config(&wrong, vm().source_vmid())
            .await,
        Err(PveReadError::InvalidResponse)
    );
    w.fake.enqueue_provisioning_identity_read(
        wrong.clone(),
        vm().source_vmid(),
        FakeProvisioningIdentityReadV1::Snapshot(Box::new(
            ProvisioningIdentitySnapshotV1::from_provisioning(&source()),
        )),
    );
    assert_eq!(
        w.fake
            .provisioning_identity(&wrong, vm().source_vmid())
            .await,
        Err(PveReadError::InvalidResponse)
    );
    let media = ProvisioningMediaInventoryV1::new(
        node(),
        StorageName::parse("local").unwrap(),
        vec![],
        ProvisioningCoverageV1::Partial,
        time(),
    )
    .unwrap();
    w.fake.enqueue_provisioning_media_read(
        node(),
        StorageName::parse("drivers").unwrap(),
        FakeProvisioningMediaReadV1::Snapshot(Box::new(media)),
    );
    assert_eq!(
        w.fake
            .provisioning_media(&node(), &StorageName::parse("drivers").unwrap())
            .await,
        Err(PveReadError::InvalidResponse)
    );
    assert!(w.fake.recorded_provisioning_submissions().is_empty());
}
fn old_config(vmid: u32, collision: bool, partial: bool) -> NativeVmConfig {
    let mut data = json!({"digest":"old-digest","name":"legacy","cores":1,"memory":512,"scsi0":format!("local-lvm:vm-{vmid}-disk-0"),"smbios1":format!("uuid={}",if collision {vm().uuid().to_string()} else {"4f2504e0-4f89-41d3-9a0c-0305e82c3301".into()}),"net0":"virtio=02:00:00:00:00:01,bridge=vmbr0","agent":0,"boot":"order=scsi0","template":1});
    if partial {
        data["unknown"] = json!("redacted");
    }
    NativeVmConfig::from_wire(node(), Vmid::new(vmid).unwrap(), data, Utc::now()).unwrap()
}

#[tokio::test]
async fn outcome_matrix_records_attempts_acceptance_and_actual_effects() {
    use ProvisioningActionV1::*;
    for action in [
        Clone,
        EnsureCapacity,
        ConfigurePe,
        StartPe,
        EnsureStopped,
        ConfigureDisk,
        StartDisk,
    ] {
        for fault in [
            FakeMutationOutcome::Accepted,
            FakeMutationOutcome::Rejected(PveWriteError::Rejected),
            FakeMutationOutcome::AppliedResponseLost,
            FakeMutationOutcome::AcceptedTaskFails,
            FakeMutationOutcome::AcceptedTaskDelayed,
        ] {
            let mut w = World::new(80 * GIB, 120 * GIB);
            w.through(action).await;
            let (_, r) = w.prepare(action).await;
            let configured = w.fake.enqueue_provisioning_outcome(
                ProvisioningFaultSelectorV1::new(action, None),
                fault,
            );
            if matches!(action, ConfigurePe | ConfigureDisk)
                && matches!(
                    fault,
                    FakeMutationOutcome::AcceptedTaskFails
                        | FakeMutationOutcome::AcceptedTaskDelayed
                )
            {
                assert_eq!(configured, Err(UnsupportedFakeOutcome));
                continue;
            }
            configured.unwrap();
            let before = w
                .fake
                .provisioning_vm_config(&node(), vm().target_vmid())
                .await
                .ok()
                .map(|c| semantic(&c));
            let power_before = w
                .fake
                .vm_status(&node(), vm().target_vmid())
                .await
                .ok()
                .map(|p| p.power());
            let count = w.fake.recorded_provisioning_submissions().len();
            let result = w.fake.submit_provisioning(&r).await;
            let records = w.fake.recorded_provisioning_submissions();
            let record = records.last().unwrap();
            assert_eq!(records.len(), count + 1);
            assert_eq!(record.request(), &r);
            assert_eq!(record.request_sha256(), r.request_digest().unwrap());
            assert_eq!(record.returned(), &result);
            assert_eq!(record.submission_number(), records.len() as u64);
            assert_eq!(
                record.acceptance().is_some(),
                fault != FakeMutationOutcome::Rejected(PveWriteError::Rejected)
            );
            if let Some(a) = record.acceptance() {
                assert!(a.accepted_at() >= record.submitted_at());
            }
            if matches!(
                fault,
                FakeMutationOutcome::Rejected(_)
                    | FakeMutationOutcome::AcceptedTaskFails
                    | FakeMutationOutcome::AcceptedTaskDelayed
            ) {
                assert_eq!(
                    w.fake
                        .provisioning_vm_config(&node(), vm().target_vmid())
                        .await
                        .ok()
                        .map(|c| semantic(&c)),
                    before
                );
                assert_eq!(
                    w.fake
                        .vm_status(&node(), vm().target_vmid())
                        .await
                        .ok()
                        .map(|p| p.power()),
                    power_before
                );
            }
            match fault {
                FakeMutationOutcome::AppliedResponseLost => {
                    assert_eq!(result, Err(PveWriteError::OutcomeUnknown));
                    assert!(
                        w.fake
                            .provisioning_vm_config(&node(), vm().target_vmid())
                            .await
                            .is_ok()
                    );
                }
                FakeMutationOutcome::AcceptedTaskDelayed => {
                    let u = upid(result.as_ref().unwrap());
                    assert_eq!(
                        w.fake.task_status(&node(), u).await.unwrap().state(),
                        TaskState::Running
                    );
                    assert_eq!(w.fake.complete_pending(), Err(PveWriteError::Rejected));
                    w.fake.complete_provisioning_task(u).unwrap();
                    assert_eq!(
                        w.fake.task_status(&node(), u).await.unwrap().state(),
                        TaskState::CompleteSuccess
                    );
                    assert_eq!(
                        w.fake.complete_provisioning_task(u),
                        Err(PveWriteError::Rejected)
                    );
                    assert_eq!(w.fake.recorded_provisioning_submissions(), records);
                }
                FakeMutationOutcome::AcceptedTaskFails => assert_eq!(
                    w.fake
                        .task_status(&node(), upid(result.as_ref().unwrap()))
                        .await
                        .unwrap()
                        .state(),
                    TaskState::CompleteFailure
                ),
                _ => {}
            }
            if matches!(
                fault,
                FakeMutationOutcome::Accepted
                    | FakeMutationOutcome::AppliedResponseLost
                    | FakeMutationOutcome::AcceptedTaskDelayed
            ) {
                let applied = w
                    .fake
                    .provisioning_vm_config(&node(), vm().target_vmid())
                    .await
                    .unwrap();
                assert_eq!(
                    applied.primary_disk().capacity_bytes(),
                    if action == Clone { 80 * GIB } else { 120 * GIB }
                );
                assert_eq!(
                    w.fake
                        .vm_status(&node(), vm().target_vmid())
                        .await
                        .unwrap()
                        .power(),
                    if matches!(action, StartPe | StartDisk) {
                        PowerState::Running
                    } else {
                        PowerState::Stopped
                    }
                );
                if matches!(
                    action,
                    ConfigurePe | StartPe | EnsureStopped | ConfigureDisk | StartDisk
                ) {
                    assert_eq!(applied.uuid(), vm().uuid());
                    assert_eq!(applied.mac(), vm().mac());
                    assert_eq!(applied.primary_disk().serial(), Some("DISK-101"));
                    assert_eq!(applied.system_serial(), Some("SYS-101"));
                    assert_eq!(applied.cores(), 4);
                    assert_eq!(applied.memory_mib(), 4096);
                    if matches!(action, ConfigureDisk | StartDisk) {
                        assert_eq!(applied.deployment_iso(), &ProvisioningMediaSlotV1::Absent);
                        assert_eq!(applied.driver_iso(), &ProvisioningMediaSlotV1::Absent);
                        assert_eq!(
                            applied.boot_profile(),
                            Some(ProvisioningBootProfile::InstalledDisk)
                        );
                    } else {
                        assert_eq!(
                            applied.deployment_iso().volid(),
                            Some("local:iso/deployment.iso")
                        );
                        assert_eq!(applied.driver_iso().volid(), Some("drivers:iso/virtio.iso"));
                        assert_eq!(
                            applied.boot_profile(),
                            Some(ProvisioningBootProfile::PeMedia)
                        );
                    }
                }
                assert!(
                    !w.fake
                        .qga_ping(&node(), vm().target_vmid())
                        .await
                        .unwrap()
                        .reachable()
                );
            }
            assert_eq!(
                w.fake.submit_provisioning(&r).await,
                Err(PveWriteError::Conflict)
            );
            let later = w.fake.recorded_provisioning_submissions();
            assert_eq!(later.len(), records.len() + 1);
            assert!(later.last().unwrap().acceptance().is_none());
        }
    }
}

#[tokio::test]
async fn delayed_work_cannot_target_copied_replacement_or_independent_power_change() {
    use ProvisioningActionV1::*;
    for action in [Clone, EnsureCapacity, StartPe, EnsureStopped, StartDisk] {
        for change_power in [false, true] {
            let mut w = World::new(80 * GIB, 120 * GIB);
            w.through(action).await;
            let (_, r) = w.prepare(action).await;
            w.fake
                .enqueue_provisioning_outcome(
                    ProvisioningFaultSelectorV1::new(action, None),
                    FakeMutationOutcome::AcceptedTaskDelayed,
                )
                .unwrap();
            let receipt = w.fake.submit_provisioning(&r).await.unwrap();
            let target = r.expected_before().config().vmid();
            let config = w
                .fake
                .provisioning_vm_config(&node(), target)
                .await
                .unwrap();
            let power = w.fake.vm_status(&node(), target).await.unwrap().power();
            if change_power {
                w.fake
                    .set_provisioning_power(&node(), target, power)
                    .unwrap();
            } else {
                w.fake
                    .replace_provisioning_vm(config.clone(), power)
                    .unwrap();
            }
            assert_eq!(
                w.fake.complete_provisioning_task(upid(&receipt)),
                Err(PveWriteError::Conflict)
            );
            assert_eq!(
                semantic(
                    &w.fake
                        .provisioning_vm_config(&node(), target)
                        .await
                        .unwrap()
                ),
                semantic(&config)
            );
            assert_eq!(
                w.fake
                    .task_status(&node(), upid(&receipt))
                    .await
                    .unwrap()
                    .state(),
                TaskState::CompleteFailure
            );
            assert!(w.fake.pending_provisioning_tasks().is_empty());
        }
    }
}

#[tokio::test]
async fn exact_before_state_rejects_tampering_even_when_digest_and_marker_are_copied() {
    for (field, value) in [
        ("digest", json!("other")),
        ("name", json!("changed-name")),
        ("uuid", json!("4f2504e0-4f89-41d3-9a0c-0305e82c3302")),
        ("mac", json!("02:00:00:00:01:fe")),
        ("cores", json!(8)),
        ("memory_mib", json!(8192)),
        ("bridge", json!("vmbr9")),
        ("system_serial", json!("TAMPERED")),
        ("unsupported", json!(["unknown_field"])),
        ("locked", json!(true)),
        ("template", json!(true)),
    ] {
        let mut w = World::new(80 * GIB, 120 * GIB);
        w.through(ProvisioningActionV1::ConfigurePe).await;
        let (_, r) = w.prepare(ProvisioningActionV1::ConfigurePe).await;
        let replaced = changed(r.expected_before().config(), |v| v[field] = value);
        w.fake
            .replace_provisioning_vm(replaced.clone(), PowerState::Stopped)
            .unwrap();
        assert_eq!(
            w.fake.submit_provisioning(&r).await,
            Err(PveWriteError::Conflict),
            "{field}"
        );
        assert_eq!(
            semantic(
                &w.fake
                    .provisioning_vm_config(&node(), vm().target_vmid())
                    .await
                    .unwrap()
            ),
            semantic(&replaced)
        );
    }
    for (field, value) in [
        ("volume", json!("replacement-volume")),
        ("capacity_bytes", json!(121 * GIB)),
        ("serial", json!("CHANGED")),
    ] {
        let mut w = World::new(80 * GIB, 120 * GIB);
        w.through(ProvisioningActionV1::ConfigurePe).await;
        let (_, r) = w.prepare(ProvisioningActionV1::ConfigurePe).await;
        let replaced = changed(r.expected_before().config(), |v| {
            v["primary_disk"][field] = value
        });
        w.fake
            .replace_provisioning_vm(replaced.clone(), PowerState::Stopped)
            .unwrap();
        assert_eq!(
            w.fake.submit_provisioning(&r).await,
            Err(PveWriteError::Conflict)
        );
        assert_eq!(
            semantic(
                &w.fake
                    .provisioning_vm_config(&node(), vm().target_vmid())
                    .await
                    .unwrap()
            ),
            semantic(&replaced)
        );
    }
}

#[tokio::test]
async fn missing_distinct_catalog_and_growth_delta_reject_without_world_change() {
    for storage in ["local", "drivers"] {
        let w = World::new(80 * GIB, 120 * GIB);
        let (_, r) = w.prepare(ProvisioningActionV1::Clone).await;
        w.fake.set_provisioning_media(
            ProvisioningMediaInventoryV1::new(
                node(),
                StorageName::parse(storage).unwrap(),
                vec![],
                ProvisioningCoverageV1::Complete,
                Utc::now(),
            )
            .unwrap(),
        );
        assert_eq!(
            w.fake.submit_provisioning(&r).await,
            Err(PveWriteError::Rejected)
        );
        assert_eq!(
            w.fake
                .provisioning_vm_config(&node(), vm().target_vmid())
                .await,
            Err(PveReadError::NotFound)
        );
    }
    for free in [0, 40 * GIB - 1, 40 * GIB] {
        let mut w = World::new(80 * GIB, 120 * GIB);
        w.through(ProvisioningActionV1::EnsureCapacity).await;
        let (_, r) = w.prepare(ProvisioningActionV1::EnsureCapacity).await;
        w.fake.set_storage_status(
            StorageStatus::from_wire(
                node(),
                vm().storage().clone(),
                json!({"active":1,"enabled":1,"content":"images","avail":free}),
                Utc::now(),
            )
            .unwrap(),
        );
        let result = w.fake.submit_provisioning(&r).await;
        assert_eq!(result.is_ok(), free == 40 * GIB);
        assert_eq!(
            w.fake
                .provisioning_vm_config(&node(), vm().target_vmid())
                .await
                .unwrap()
                .primary_disk()
                .capacity_bytes(),
            if free == 40 * GIB {
                120 * GIB
            } else {
                80 * GIB
            }
        );
        assert_eq!(
            w.fake
                .storage_status(&node(), vm().storage())
                .await
                .unwrap()
                .available_bytes(),
            free
        );
    }
    assert_eq!(
        NativeFakePve::new()
            .provisioning_media(&node(), &StorageName::parse("local").unwrap())
            .await,
        Err(PveReadError::NotFound)
    );
}

#[tokio::test]
async fn mixed_family_inventory_downcasts_coverage_and_reserved_identity_collisions() {
    for (collision, partial) in [(false, false), (true, false), (false, true)] {
        let w = World::new(80 * GIB, 120 * GIB);
        let (_, r) = w.prepare(ProvisioningActionV1::Clone).await;
        let old = old_config(800, collision, partial);
        w.fake.insert_vm(old, PowerState::Stopped).unwrap();
        assert_eq!(w.fake.cluster_vms().await.unwrap().vms().len(), 2);
        assert_eq!(
            w.fake
                .storage_content(&node(), vm().storage())
                .await
                .unwrap()
                .len(),
            2
        );
        let i = w
            .fake
            .provisioning_identity(&node(), Vmid::new(800).unwrap())
            .await
            .unwrap();
        assert_eq!(
            i.coverage(),
            if partial {
                ProvisioningCoverageV1::Partial
            } else {
                ProvisioningCoverageV1::Complete
            }
        );
        assert_eq!(
            w.fake
                .provisioning_vm_config(&node(), Vmid::new(800).unwrap())
                .await,
            Err(PveReadError::InvalidResponse)
        );
        assert_eq!(
            w.fake.native_vm_config(&node(), vm().source_vmid()).await,
            Err(PveReadError::InvalidResponse)
        );
        assert_eq!(
            w.fake.submit_provisioning(&r).await.is_ok(),
            !collision && !partial
        );
    }
    let w = World::new(80 * GIB, 120 * GIB);
    assert_eq!(
        w.fake
            .insert_vm(old_config(900, false, false), PowerState::Stopped),
        Err(PveWriteError::Conflict)
    );
    w.fake
        .insert_vm(old_config(800, false, false), PowerState::Stopped)
        .unwrap();
    assert_eq!(
        w.fake.insert_provisioning_vm(
            changed(&source(), |v| v["vmid"] = json!(800)),
            PowerState::Stopped
        ),
        Err(PveWriteError::Conflict)
    );
}

#[tokio::test]
async fn independent_qga_survives_config_power_and_does_not_invalidate_delayed_growth() {
    let mut w = World::new(80 * GIB, 120 * GIB);
    w.through(ProvisioningActionV1::EnsureCapacity).await;
    let (c, r) = w.prepare(ProvisioningActionV1::EnsureCapacity).await;
    w.fake
        .enqueue_provisioning_outcome(
            ProvisioningFaultSelectorV1::new(ProvisioningActionV1::EnsureCapacity, None),
            FakeMutationOutcome::AcceptedTaskDelayed,
        )
        .unwrap();
    let sent = Utc::now();
    let receipt = w.fake.submit_provisioning(&r).await.unwrap();
    w.fake
        .set_qga_reachable(&node(), vm().target_vmid(), true)
        .unwrap();
    w.fake.complete_provisioning_task(upid(&receipt)).unwrap();
    w.accept_proof(c, r, receipt, sent).await;
    for action in [
        ProvisioningActionV1::ConfigurePe,
        ProvisioningActionV1::StartPe,
        ProvisioningActionV1::EnsureStopped,
        ProvisioningActionV1::ConfigureDisk,
        ProvisioningActionV1::StartDisk,
    ] {
        w.step(action).await;
        assert!(
            w.fake
                .qga_ping(&node(), vm().target_vmid())
                .await
                .unwrap()
                .reachable()
        );
    }
    let current = w
        .fake
        .provisioning_vm_config(&node(), vm().target_vmid())
        .await
        .unwrap();
    w.fake
        .replace_provisioning_vm(current, PowerState::Running)
        .unwrap();
    assert!(
        !w.fake
            .qga_ping(&node(), vm().target_vmid())
            .await
            .unwrap()
            .reachable()
    );
}

#[tokio::test]
async fn full_world_sequence_grows_80_to_120_and_preserves_power_only_fields() {
    sequence(80 * GIB, 120 * GIB).await;
}
#[tokio::test]
async fn full_world_sequence_retains_160_for_requested_120() {
    sequence(160 * GIB, 160 * GIB).await;
}
#[tokio::test]
async fn full_world_sequence_retains_exact_non_whole_gib_capacity() {
    sequence(160 * GIB + 17, 160 * GIB + 17).await;
}
async fn sequence(template: u64, effective: u64) {
    use ProvisioningActionV1::*;
    let mut w = World::new(template, effective);
    for action in [
        Clone,
        EnsureCapacity,
        ConfigurePe,
        StartPe,
        EnsureStopped,
        ConfigureDisk,
        StartDisk,
    ] {
        let before = w
            .fake
            .provisioning_vm_config(&node(), vm().target_vmid())
            .await
            .ok();
        w.step(action).await;
        let c = w
            .fake
            .provisioning_vm_config(&node(), vm().target_vmid())
            .await
            .unwrap();
        assert_eq!(
            c.primary_disk().capacity_bytes(),
            if action == Clone { template } else { effective }
        );
        if matches!(action, StartPe | EnsureStopped | StartDisk) {
            let old = before.as_ref().unwrap();
            assert_eq!(
                changed(old, |v| v["observed_at"] =
                    serde_json::json!(c.observed_at())),
                c
            );
        }
        assert!(
            !w.fake
                .qga_ping(&node(), vm().target_vmid())
                .await
                .unwrap()
                .reachable()
        );
    }
    let records = w.fake.recorded_provisioning_submissions();
    assert_eq!(records.len(), if template == effective { 6 } else { 7 });
    assert!(records.iter().all(|r| r.acceptance().is_some()));
    let pe = records
        .iter()
        .find(|r| r.request().plan().action() == StartPe)
        .unwrap();
    let disk = records
        .iter()
        .find(|r| r.request().plan().action() == StartDisk)
        .unwrap();
    assert_eq!(pe.request().form(), disk.request().form());
    assert_ne!(pe.request().operation_id(), disk.request().operation_id());
    assert_ne!(pe.request_sha256(), disk.request_sha256());
}

#[tokio::test]
async fn clone_changes_the_shared_world_without_preconfiguring_final_identity() {
    let fake = NativeFakePve::new();
    fake.insert_provisioning_vm(source(), PowerState::Stopped)
        .unwrap();
    let f = facts(
        binding(ProvisioningActionV1::Clone, 10, 0),
        plan(ProvisioningActionV1::Clone),
        None,
        false,
    );
    fake.set_node_status(f.node.unwrap().result.unwrap());
    fake.set_storage_status(f.storage.unwrap().result.unwrap());
    fake.set_bridges(f.bridges.unwrap().result.unwrap());
    for m in f.media {
        fake.set_provisioning_media(m.result.unwrap());
    }
    let r = chain()[0].request.clone();
    let receipt = fake.submit_provisioning(&r).await.unwrap();
    assert!(matches!(receipt, MutationReceipt::Task(_)));
    let c = fake
        .provisioning_vm_config(&node(), vm().target_vmid())
        .await
        .unwrap();
    assert_eq!(c.primary_disk().capacity_bytes(), 80 * GIB);
    assert_eq!(c.cores(), 2);
    assert_ne!(c.uuid(), vm().uuid());
    assert_ne!(c.uuid(), source().uuid());
    assert_eq!(
        fake.native_vm_config(&node(), vm().target_vmid()).await,
        Err(PveReadError::InvalidResponse)
    );
    assert!(
        !fake
            .qga_ping(&node(), vm().target_vmid())
            .await
            .unwrap()
            .reachable()
    );
}
