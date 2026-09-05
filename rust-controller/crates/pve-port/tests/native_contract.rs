use chrono::{Duration, Utc};
use controller_domain::{OperationId, OperationKind, WorkflowKind};
use pve_port::*;
use serde_json::{Value, json};
mod support;

fn payload() -> Value {
    json!({"contract_version":1,"cluster_key":"fake-local","node":"pve-test",
        "source_vmid":9000,"target_vmid":9010,"name":"native-proof-9010",
        "storage":"local-lvm","bridge":"vmbr0","uuid":"3f2504e0-4f89-41d3-9a0c-0305e82c3301",
        "mac":"02:00:00:00:90:10","cores":2,"memory_mib":2048,"minimum_storage_bytes":17179869184_u64})
}
fn plan() -> NativeVmPlan {
    serde_json::from_value(payload()).unwrap()
}
fn config(vmid: u32, template: bool) -> NativeVmConfig {
    NativeVmConfig::from_wire(NodeName::parse("pve-test").unwrap(), Vmid::new(vmid).unwrap(),
        json!({"digest":"initial-digest","name":if template {"template"} else {"native-proof-9010"},
        "cores":1,"memory":512,"scsi0":format!("local-lvm:vm-{vmid}-disk-0"),
        "smbios1":"uuid=4f2504e0-4f89-41d3-9a0c-0305e82c3301",
        "net0":"virtio=02:00:00:00:00:01,bridge=vmbr0","agent":0,"boot":"order=scsi0","template":u8::from(template)}), Utc::now()).unwrap()
}
fn fake() -> NativeFakePve {
    let fake = NativeFakePve::new();
    fake.insert_vm(config(9000, true), PowerState::Stopped)
        .unwrap();
    fake
}
fn digest(value: &impl serde::Serialize) -> String {
    event_journal::payload_digest(&serde_json::to_value(value).unwrap()).unwrap()
}

#[test]
fn immutable_plan_roundtrip_rejects_unknown_fields_and_invalid_values() {
    assert_eq!(serde_json::to_value(plan()).unwrap(), payload());
    for key in [
        "cmd",
        "args",
        "url",
        "token",
        "delete",
        "ssh",
        "new_vmid",
        "extra_parameters",
    ] {
        let mut value = payload();
        value[key] = json!("forbidden");
        assert!(
            serde_json::from_value::<NativeVmPlan>(value).is_err(),
            "{key}"
        );
        assert!(
            serde_json::from_value::<NativeOperationPlan>(
                json!({"step":"clone","vm":payload(),key:"forbidden"})
            )
            .is_err()
        );
    }
    for (key, value) in [
        ("contract_version", json!(0)),
        ("contract_version", json!(2)),
        ("source_vmid", json!(9010)),
        ("target_vmid", json!(0)),
        ("target_vmid", json!(1_000_000_000)),
        ("cores", json!(0)),
        ("cores", json!(129)),
        ("memory_mib", json!(511)),
        ("memory_mib", json!(513)),
        ("memory_mib", json!(1_048_704)),
        ("minimum_storage_bytes", json!(0)),
        ("name", json!("")),
        ("name", json!("bad_name")),
        ("name", json!("λ")),
        ("name", json!("x".repeat(64))),
        ("cluster_key", json!("https://pve.example")),
        ("cluster_key", json!("")),
        ("node", json!("../pve")),
        ("storage", json!("bad/storage")),
        ("bridge", json!("bad,bridge")),
        ("uuid", json!("invalid")),
        ("mac", json!("invalid")),
    ] {
        let mut invalid = payload();
        invalid[key] = value;
        assert!(
            serde_json::from_value::<NativeVmPlan>(invalid).is_err(),
            "{key}"
        );
    }
    for (cores, memory) in [(1, 512), (128, 1_048_576)] {
        let mut value = payload();
        value["cores"] = json!(cores);
        value["memory_mib"] = json!(memory);
        assert!(serde_json::from_value::<NativeVmPlan>(value).is_ok());
    }
    assert_eq!(
        serde_json::to_value(OperationKind::PveClone).unwrap(),
        "pve_clone"
    );
    assert_eq!(
        serde_json::to_value(OperationKind::PveConfigure).unwrap(),
        "pve_configure"
    );
    assert_eq!(
        serde_json::to_value(OperationKind::PveStart).unwrap(),
        "pve_start"
    );
    assert_eq!(
        serde_json::to_value(WorkflowKind::NativePveVmBoot).unwrap(),
        "native_pve_vm_boot"
    );
}

#[test]
fn every_mutable_plan_field_and_step_changes_canonical_digest() {
    let base = NativeOperationPlan::new(NativeStep::Clone, plan());
    for (key, replacement) in [
        ("cluster_key", json!("other")),
        ("node", json!("other")),
        ("source_vmid", json!(9001)),
        ("target_vmid", json!(9011)),
        ("name", json!("other")),
        ("storage", json!("other")),
        ("bridge", json!("vmbr1")),
        ("uuid", json!("5f2504e0-4f89-41d3-9a0c-0305e82c3301")),
        ("mac", json!("02:00:00:00:90:11")),
        ("cores", json!(3)),
        ("memory_mib", json!(2176)),
        ("minimum_storage_bytes", json!(1)),
    ] {
        let mut value = payload();
        value[key] = replacement;
        let changed =
            NativeOperationPlan::new(NativeStep::Clone, serde_json::from_value(value).unwrap());
        assert_ne!(digest(&base), digest(&changed), "{key}");
    }
    for (step, key) in [
        (NativeStep::Clone, "pve.clone.v1"),
        (NativeStep::Configure, "pve.configure.v1"),
        (NativeStep::Start, "pve.start.v1"),
    ] {
        let operation = NativeOperationPlan::new(step, plan());
        assert_eq!(operation.step().operation_key(), key);
        assert_eq!(
            serde_json::from_value::<NativeOperationPlan>(
                serde_json::to_value(&operation).unwrap()
            )
            .unwrap(),
            operation
        );
        if step != NativeStep::Clone {
            assert_ne!(digest(&base), digest(&operation));
        }
    }
}

#[tokio::test]
async fn fixed_requests_apply_identity_preserve_disk_and_provenance() {
    let plan = plan();
    let fake = fake();
    let request = CloneRequest::new(plan.clone(), OperationId::new());
    assert_eq!(request.method(), "POST");
    assert_eq!(
        request.path_segments(),
        ["nodes", "pve-test", "qemu", "9000", "clone"]
    );
    assert_eq!(
        request.form(),
        vec![
            ("newid", "9010".into()),
            ("name", "native-proof-9010".into()),
            ("full", "1".into()),
            ("storage", "local-lvm".into())
        ]
    );
    assert_eq!(request.request_digest(), digest(&request));
    let receipt = fake.clone_vm(&request).await.unwrap();
    let MutationReceipt::Task(upid) = receipt else {
        panic!("clone task required")
    };
    assert_eq!(upid.worker_type(), "qmclone");
    assert_eq!(upid.worker_id(), Some("9000"));
    assert!(
        fake.task_status(plan.node(), &upid)
            .await
            .unwrap()
            .succeeded()
    );
    let intermediate = fake
        .native_vm_config(plan.node(), plan.target_vmid())
        .await
        .unwrap();
    assert_ne!(intermediate.uuid(), plan.uuid());
    assert_ne!(intermediate.mac(), plan.mac());
    let provenance = intermediate.fake_clone_provenance().unwrap().clone();
    assert!(provenance.matches(&request));
    let disk = intermediate.boot_disk().clone();
    let configure = ConfigureRequest::new(
        plan.clone(),
        &request,
        &intermediate,
        &intermediate,
        Utc::now(),
    )
    .unwrap();
    assert_eq!(configure.method(), "PUT");
    assert_eq!(
        configure.path_segments(),
        ["nodes", "pve-test", "qemu", "9010", "config"]
    );
    assert_eq!(
        configure.form(),
        vec![
            ("digest", intermediate.digest().into()),
            (
                "smbios1",
                "uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3301".into()
            ),
            (
                "net0",
                "virtio=02:00:00:00:90:10,bridge=vmbr0,firewall=0".into()
            ),
            ("cores", "2".into()),
            ("memory", "2048".into()),
            ("agent", "enabled=1,type=virtio".into()),
            ("boot", "order=scsi0".into())
        ]
    );
    assert_eq!(
        fake.configure_vm(&configure).await.unwrap(),
        MutationReceipt::SynchronousAccepted
    );
    assert_eq!(
        fake.configure_vm(&configure).await.unwrap_err(),
        PveWriteError::Conflict
    );
    let configured = fake
        .native_vm_config(plan.node(), plan.target_vmid())
        .await
        .unwrap();
    assert_eq!(configured.uuid(), plan.uuid());
    assert_eq!(configured.mac(), plan.mac());
    assert_eq!(configured.cores(), 2);
    assert_eq!(configured.memory_mib(), 2048);
    assert!(configured.agent_enabled());
    assert!(configured.boots_scsi0());
    assert_eq!(configured.boot_disk(), &disk);
    assert_eq!(configured.fake_clone_provenance(), Some(&provenance));
    let start = StartRequest::new(
        plan.clone(),
        &request,
        &intermediate,
        &configured,
        Utc::now(),
    )
    .unwrap();
    assert_eq!(start.method(), "POST");
    assert_eq!(
        start.path_segments(),
        ["nodes", "pve-test", "qemu", "9010", "status", "start"]
    );
    assert!(start.form().is_empty());
    let MutationReceipt::Task(upid) = fake.start_vm(&start).await.unwrap() else {
        panic!("start task required")
    };
    assert_eq!(upid.worker_type(), "qmstart");
    assert_eq!(upid.worker_id(), Some("9010"));
    assert_eq!(
        fake.vm_status(plan.node(), plan.target_vmid())
            .await
            .unwrap()
            .power(),
        PowerState::Running
    );
    let final_config = fake
        .native_vm_config(plan.node(), plan.target_vmid())
        .await
        .unwrap();
    assert_eq!(final_config.fake_clone_provenance(), Some(&provenance));
    assert_eq!(
        serde_json::from_value::<NativeVmConfig>(serde_json::to_value(&final_config).unwrap())
            .unwrap(),
        final_config
    );
    assert_eq!(fake.recorded_requests().len(), 4);
}

#[test]
fn request_markers_and_operation_ids_are_hashed_and_provenance_is_not_circular() {
    let op = OperationId::new();
    let a = CloneRequest::new(plan(), op);
    let b = CloneRequest::new(plan(), op);
    assert_ne!(a.request_marker(), b.request_marker());
    assert_ne!(a.request_digest(), b.request_digest());
    let mut changed = serde_json::to_value(&a).unwrap();
    changed["operation_id"] = json!(OperationId::new());
    assert_ne!(
        a.request_digest(),
        event_journal::payload_digest(&changed).unwrap()
    );
    assert!(changed.get("request_digest").is_none());
    assert!(changed.get("provenance").is_none());
    let persisted = serde_json::to_value(&a).unwrap();
    let restored: CloneRequest = serde_json::from_value(persisted.clone()).unwrap();
    assert_eq!(restored, a);
    assert_eq!(restored.request_digest(), a.request_digest());
    for (key, value) in [
        ("request_marker", json!(uuid::Uuid::nil())),
        ("operation_id", json!(uuid::Uuid::nil())),
        ("url", json!("http://localhost")),
        ("request_digest", json!("unchecked")),
    ] {
        let mut bad = persisted.clone();
        bad[key] = value;
        assert!(serde_json::from_value::<CloneRequest>(bad).is_err());
    }
}

#[tokio::test]
async fn missing_or_mismatched_provenance_and_stale_or_wrong_identity_cannot_configure() {
    let plan = plan();
    let clone = CloneRequest::new(plan.clone(), OperationId::new());
    let fake = fake();
    let foreign = config(9010, false);
    assert_eq!(
        ConfigureRequest::new(plan.clone(), &clone, &foreign, &foreign, Utc::now()).unwrap_err(),
        PveWriteError::OutcomeUnknown
    );
    fake.insert_vm(foreign, PowerState::Stopped).unwrap();
    assert_eq!(
        fake.clone_vm(&clone).await.unwrap_err(),
        PveWriteError::Conflict
    );
    let fake = super_fake_clone(&clone).await;
    let observed = fake
        .native_vm_config(plan.node(), plan.target_vmid())
        .await
        .unwrap();
    let other = CloneRequest::new(plan.clone(), clone.operation_id());
    assert_eq!(
        ConfigureRequest::new(plan.clone(), &other, &observed, &observed, Utc::now()).unwrap_err(),
        PveWriteError::Conflict
    );
    assert_eq!(
        ConfigureRequest::new(
            plan.clone(),
            &clone,
            &observed,
            &observed,
            observed.observed_at() + Duration::seconds(31)
        )
        .unwrap_err(),
        PveWriteError::OutcomeUnknown
    );
    assert_eq!(
        ConfigureRequest::new(
            plan.clone(),
            &clone,
            &observed,
            &observed,
            observed.observed_at() - Duration::seconds(1)
        )
        .unwrap_err(),
        PveWriteError::OutcomeUnknown
    );
    assert_eq!(
        StartRequest::new(plan, &clone, &observed, &observed, Utc::now()).unwrap_err(),
        PveWriteError::Conflict
    );
}
async fn super_fake_clone(request: &CloneRequest) -> NativeFakePve {
    let f = fake();
    f.clone_vm(request).await.unwrap();
    f
}

#[tokio::test]
async fn clone_outcomes_distinguish_rejection_loss_failure_and_delayed_application() {
    for outcome in [
        FakeMutationOutcome::Accepted,
        FakeMutationOutcome::Rejected(PveWriteError::Unauthorized),
        FakeMutationOutcome::AppliedResponseLost,
        FakeMutationOutcome::AcceptedTaskFails,
        FakeMutationOutcome::AcceptedTaskDelayed,
    ] {
        let fake = fake();
        let plan = plan();
        let request = CloneRequest::new(plan.clone(), OperationId::new());
        fake.enqueue_outcome(NativeStep::Clone, outcome).unwrap();
        let result = fake.clone_vm(&request).await;
        match outcome {
            FakeMutationOutcome::Rejected(error) => {
                assert_eq!(result.unwrap_err(), error);
                assert_eq!(
                    fake.native_vm_config(plan.node(), plan.target_vmid())
                        .await
                        .unwrap_err(),
                    PveReadError::NotFound
                );
            }
            FakeMutationOutcome::AppliedResponseLost => {
                assert_eq!(result.unwrap_err(), PveWriteError::OutcomeUnknown);
                assert!(
                    fake.native_vm_config(plan.node(), plan.target_vmid())
                        .await
                        .unwrap()
                        .fake_clone_provenance()
                        .unwrap()
                        .matches(&request)
                );
            }
            FakeMutationOutcome::Accepted
            | FakeMutationOutcome::AcceptedTaskFails
            | FakeMutationOutcome::AcceptedTaskDelayed => {
                let MutationReceipt::Task(upid) = result.unwrap() else {
                    panic!("task required")
                };
                let state = fake.task_status(plan.node(), &upid).await.unwrap().state();
                assert_eq!(
                    state,
                    match outcome {
                        FakeMutationOutcome::Accepted => TaskState::CompleteSuccess,
                        FakeMutationOutcome::AcceptedTaskFails => TaskState::CompleteFailure,
                        _ => TaskState::Running,
                    }
                );
                if outcome != FakeMutationOutcome::Accepted {
                    assert_eq!(
                        fake.native_vm_config(plan.node(), plan.target_vmid())
                            .await
                            .unwrap_err(),
                        PveReadError::NotFound
                    );
                }
                if outcome == FakeMutationOutcome::AcceptedTaskDelayed {
                    fake.complete_pending().unwrap();
                    assert!(
                        fake.task_status(plan.node(), &upid)
                            .await
                            .unwrap()
                            .succeeded()
                    );
                    assert!(
                        fake.native_vm_config(plan.node(), plan.target_vmid())
                            .await
                            .unwrap()
                            .fake_clone_provenance()
                            .unwrap()
                            .matches(&request)
                    );
                }
            }
        }
    }
}

#[tokio::test]
async fn configure_and_start_outcomes_preserve_their_distinct_receipt_contracts() {
    for step in [NativeStep::Configure, NativeStep::Start] {
        for outcome in [
            FakeMutationOutcome::Accepted,
            FakeMutationOutcome::Rejected(PveWriteError::Rejected),
            FakeMutationOutcome::AppliedResponseLost,
            FakeMutationOutcome::AcceptedTaskFails,
            FakeMutationOutcome::AcceptedTaskDelayed,
        ] {
            let plan = plan();
            let clone = CloneRequest::new(plan.clone(), OperationId::new());
            let fake = super_fake_clone(&clone).await;
            let bound = fake
                .native_vm_config(plan.node(), plan.target_vmid())
                .await
                .unwrap();
            let config =
                ConfigureRequest::new(plan.clone(), &clone, &bound, &bound, Utc::now()).unwrap();
            if step == NativeStep::Configure
                && matches!(
                    outcome,
                    FakeMutationOutcome::AcceptedTaskFails
                        | FakeMutationOutcome::AcceptedTaskDelayed
                )
            {
                assert_eq!(
                    fake.enqueue_outcome(step, outcome).unwrap_err(),
                    UnsupportedFakeOutcome
                );
                assert_eq!(fake.recorded_requests().len(), 1);
                assert_eq!(
                    fake.native_vm_config(plan.node(), plan.target_vmid())
                        .await
                        .unwrap()
                        .digest(),
                    bound.digest()
                );
                continue;
            }
            let start = if step == NativeStep::Start {
                fake.configure_vm(&config).await.unwrap();
                let configured = fake
                    .native_vm_config(plan.node(), plan.target_vmid())
                    .await
                    .unwrap();
                Some(
                    StartRequest::new(plan.clone(), &clone, &bound, &configured, Utc::now())
                        .unwrap(),
                )
            } else {
                None
            };
            fake.enqueue_outcome(step, outcome).unwrap();
            let result = if let Some(start) = &start {
                fake.start_vm(start).await
            } else {
                fake.configure_vm(&config).await
            };
            let applied = matches!(
                outcome,
                FakeMutationOutcome::Accepted | FakeMutationOutcome::AppliedResponseLost
            );
            match outcome {
                FakeMutationOutcome::AppliedResponseLost => {
                    assert_eq!(result.unwrap_err(), PveWriteError::OutcomeUnknown)
                }
                FakeMutationOutcome::Rejected(e) => assert_eq!(result.unwrap_err(), e),
                _ if step == NativeStep::Configure => {
                    assert_eq!(result.unwrap(), MutationReceipt::SynchronousAccepted)
                }
                _ => {
                    let MutationReceipt::Task(upid) = result.unwrap() else {
                        panic!("task required")
                    };
                    assert_eq!(
                        fake.task_status(plan.node(), &upid).await.unwrap().state(),
                        match outcome {
                            FakeMutationOutcome::Accepted => TaskState::CompleteSuccess,
                            FakeMutationOutcome::AcceptedTaskFails => TaskState::CompleteFailure,
                            _ => TaskState::Running,
                        }
                    );
                }
            }
            if step == NativeStep::Configure {
                assert_eq!(
                    fake.native_vm_config(plan.node(), plan.target_vmid())
                        .await
                        .unwrap()
                        .uuid()
                        == plan.uuid(),
                    applied
                );
            } else {
                assert_eq!(
                    fake.vm_status(plan.node(), plan.target_vmid())
                        .await
                        .unwrap()
                        .power()
                        == PowerState::Running,
                    applied
                );
            }
            if outcome == FakeMutationOutcome::AcceptedTaskDelayed {
                fake.complete_pending().unwrap();
                assert_eq!(
                    fake.vm_status(plan.node(), plan.target_vmid())
                        .await
                        .unwrap()
                        .power(),
                    PowerState::Running
                );
            }
            assert!(
                fake.native_vm_config(plan.node(), plan.target_vmid())
                    .await
                    .unwrap()
                    .fake_clone_provenance()
                    .unwrap()
                    .matches(&clone)
            );
        }
    }
}

fn changed(config: &NativeVmConfig, key: &str, value: Value) -> NativeVmConfig {
    let mut wire = serde_json::to_value(config).unwrap();
    wire[key] = value;
    serde_json::from_value(wire).unwrap()
}

#[tokio::test]
async fn retained_marker_does_not_hide_changed_intermediate_identity_or_disk() {
    let plan = plan();
    let clone = CloneRequest::new(plan.clone(), OperationId::new());
    let fake = super_fake_clone(&clone).await;
    let bound = fake
        .native_vm_config(plan.node(), plan.target_vmid())
        .await
        .unwrap();
    let request = ConfigureRequest::new(plan.clone(), &clone, &bound, &bound, Utc::now()).unwrap();
    for (key, value) in [
        ("uuid", json!("6f2504e0-4f89-41d3-9a0c-0305e82c3301")),
        ("mac", json!("02:00:00:00:99:99")),
        (
            "boot_disk",
            json!({"storage":"local-lvm","volume":"foreign-disk"}),
        ),
        ("name", json!("foreign")),
        ("locked", json!(true)),
        ("template", json!(true)),
    ] {
        let current = changed(&bound, key, value);
        assert_eq!(
            ConfigureRequest::new(plan.clone(), &clone, &bound, &current, Utc::now()).unwrap_err(),
            PveWriteError::Conflict,
            "{key}"
        );
        fake.replace_vm(current, PowerState::Stopped).unwrap();
        assert_eq!(
            fake.configure_vm(&request).await.unwrap_err(),
            PveWriteError::Conflict,
            "{key}"
        );
    }
    fake.replace_vm(bound.clone(), PowerState::Stopped).unwrap();
    fake.configure_vm(&request).await.unwrap();
    let configured = fake
        .native_vm_config(plan.node(), plan.target_vmid())
        .await
        .unwrap();
    let wrong_disk = changed(
        &configured,
        "boot_disk",
        json!({"storage":"local-lvm","volume":"foreign-disk"}),
    );
    assert_eq!(
        StartRequest::new(plan.clone(), &clone, &bound, &wrong_disk, Utc::now()).unwrap_err(),
        PveWriteError::Conflict
    );
    let start = StartRequest::new(plan.clone(), &clone, &bound, &configured, Utc::now()).unwrap();
    fake.replace_vm(wrong_disk, PowerState::Stopped).unwrap();
    assert_eq!(
        fake.start_vm(&start).await.unwrap_err(),
        PveWriteError::Conflict
    );
}

#[tokio::test]
async fn successful_clone_task_and_same_name_do_not_authorize_foreign_occupant() {
    let plan = plan();
    let clone = CloneRequest::new(plan.clone(), OperationId::new());
    let fake = fake();
    let MutationReceipt::Task(upid) = fake.clone_vm(&clone).await.unwrap() else {
        panic!("task required")
    };
    let bound = fake
        .native_vm_config(plan.node(), plan.target_vmid())
        .await
        .unwrap();
    fake.replace_vm(config(9010, false), PowerState::Stopped)
        .unwrap();
    assert!(
        fake.task_status(plan.node(), &upid)
            .await
            .unwrap()
            .succeeded()
    );
    let foreign = fake
        .native_vm_config(plan.node(), plan.target_vmid())
        .await
        .unwrap();
    assert_eq!(foreign.name(), plan.name());
    assert_eq!(
        ConfigureRequest::new(plan.clone(), &clone, &bound, &foreign, Utc::now()).unwrap_err(),
        PveWriteError::OutcomeUnknown
    );
    let provenance = serde_json::to_value(bound.fake_clone_provenance().unwrap()).unwrap();
    for (key, value) in [
        ("operation_id", json!(OperationId::new())),
        ("request_marker", json!(uuid::Uuid::now_v7())),
        ("source_vmid", json!(9001)),
        ("request_digest", json!("0".repeat(64))),
    ] {
        let mut altered = provenance.clone();
        altered[key] = value;
        let mismatch = changed(&bound, "fake_clone_provenance", altered);
        assert_eq!(
            ConfigureRequest::new(plan.clone(), &clone, &bound, &mismatch, Utc::now()).unwrap_err(),
            PveWriteError::Conflict,
            "{key}"
        );
    }
    let mut wrong_target = provenance;
    wrong_target["target_vmid"] = json!(9011);
    let mut wire = serde_json::to_value(&bound).unwrap();
    wire["fake_clone_provenance"] = wrong_target;
    assert!(serde_json::from_value::<NativeVmConfig>(wire).is_err());
}

#[tokio::test]
async fn delayed_clone_cannot_overwrite_an_occupant_inserted_before_completion() {
    let plan = plan();
    let request = CloneRequest::new(plan.clone(), OperationId::new());
    let fake = fake();
    fake.enqueue_outcome(NativeStep::Clone, FakeMutationOutcome::AcceptedTaskDelayed)
        .unwrap();
    let MutationReceipt::Task(upid) = fake.clone_vm(&request).await.unwrap() else {
        panic!("task required")
    };
    fake.insert_vm(config(9010, false), PowerState::Stopped)
        .unwrap();
    assert_eq!(
        fake.complete_pending().unwrap_err(),
        PveWriteError::Conflict
    );
    assert_eq!(
        fake.task_status(plan.node(), &upid).await.unwrap().state(),
        TaskState::CompleteFailure
    );
    assert!(
        fake.native_vm_config(plan.node(), plan.target_vmid())
            .await
            .unwrap()
            .fake_clone_provenance()
            .is_none()
    );
}

#[tokio::test]
async fn changed_digest_alone_rejects_configure_without_refresh_or_resend() {
    let plan = plan();
    let clone = CloneRequest::new(plan.clone(), OperationId::new());
    let fake = super_fake_clone(&clone).await;
    let bound = fake
        .native_vm_config(plan.node(), plan.target_vmid())
        .await
        .unwrap();
    let request = ConfigureRequest::new(plan.clone(), &clone, &bound, &bound, Utc::now()).unwrap();
    let current = changed(&bound, "digest", json!("externally-changed-digest"));
    fake.replace_vm(current, PowerState::Stopped).unwrap();
    assert_eq!(
        fake.configure_vm(&request).await.unwrap_err(),
        PveWriteError::Conflict
    );
    let observed = fake
        .native_vm_config(plan.node(), plan.target_vmid())
        .await
        .unwrap();
    assert_eq!(observed.digest(), "externally-changed-digest");
    assert_eq!(observed.uuid(), bound.uuid());
    assert_eq!(
        fake.recorded_requests(),
        vec![
            NativeMutationRequest::Clone(clone),
            NativeMutationRequest::Configure(request)
        ]
    );
}

#[tokio::test]
async fn historical_binding_can_be_old_but_current_identity_and_final_fields_must_match() {
    let plan = plan();
    let clone = CloneRequest::new(plan.clone(), OperationId::new());
    let fake = super_fake_clone(&clone).await;
    let current = fake
        .native_vm_config(plan.node(), plan.target_vmid())
        .await
        .unwrap();
    let historical = changed(
        &current,
        "observed_at",
        json!(current.observed_at() - Duration::days(1)),
    );
    let configure = ConfigureRequest::new(
        plan.clone(),
        &clone,
        &historical,
        &current,
        current.observed_at() + Duration::seconds(30),
    )
    .unwrap();
    fake.configure_vm(&configure).await.unwrap();
    let final_config = fake
        .native_vm_config(plan.node(), plan.target_vmid())
        .await
        .unwrap();
    for (key, value) in [
        ("uuid", json!(historical.uuid())),
        ("mac", json!(historical.mac())),
        ("bridge", json!("vmbr1")),
        ("cores", json!(4)),
        ("memory_mib", json!(4096)),
        ("agent_enabled", json!(false)),
    ] {
        let altered = changed(&final_config, key, value);
        assert_eq!(
            StartRequest::new(plan.clone(), &clone, &historical, &altered, Utc::now()).unwrap_err(),
            PveWriteError::Conflict,
            "{key}"
        );
    }
    assert!(StartRequest::new(plan, &clone, &historical, &final_config, Utc::now()).is_ok());
}

#[tokio::test]
async fn typed_fake_reads_report_inventory_storage_power_and_no_guest_readiness() {
    let plan = plan();
    let fake = fake();
    let now = Utc::now();
    assert_eq!(
        fake.node_status(plan.node()).await.unwrap_err(),
        PveReadError::NotFound
    );
    let node = NodeStatus::from_wire(plan.node().clone(), json!({"uptime":100}), now).unwrap();
    let storage = StorageStatus::from_wire(
        plan.node().clone(),
        plan.storage().clone(),
        json!({"active":1,"enabled":1,"avail":17179869184_u64,"content":"images"}),
        now,
    )
    .unwrap();
    let bridges = BridgeInventory::from_wire(
        plan.node().clone(),
        json!([{"type":"bridge","iface":"vmbr0","active":1}]),
        now,
    )
    .unwrap();
    fake.set_node_status(node.clone());
    fake.set_storage_status(storage.clone());
    fake.set_bridges(bridges.clone());
    let observed_node = fake.node_status(plan.node()).await.unwrap();
    assert_eq!(observed_node.node(), node.node());
    assert_eq!(observed_node.uptime(), node.uptime());
    assert!(observed_node.online());
    let observed_storage = fake
        .storage_status(plan.node(), plan.storage())
        .await
        .unwrap();
    assert_eq!(observed_storage.node(), storage.node());
    assert_eq!(observed_storage.storage(), storage.storage());
    assert_eq!(
        observed_storage.available_bytes(),
        storage.available_bytes()
    );
    let observed_bridges = fake.bridges(plan.node()).await.unwrap();
    assert_eq!(observed_bridges.node(), bridges.node());
    assert_eq!(observed_bridges.bridges(), bridges.bridges());
    assert!(
        observe_target_absence(&fake, plan.node(), plan.target_vmid())
            .await
            .unwrap()
    );
    let clone = CloneRequest::new(plan.clone(), OperationId::new());
    fake.clone_vm(&clone).await.unwrap();
    assert!(
        !observe_target_absence(&fake, plan.node(), plan.target_vmid())
            .await
            .unwrap()
    );
    let inventory = fake.cluster_vms().await.unwrap();
    assert_eq!(inventory.vms().len(), 2);
    assert_eq!(
        inventory.find(plan.target_vmid()).unwrap().power(),
        PowerState::Stopped
    );
    assert_eq!(
        fake.storage_content(plan.node(), plan.storage())
            .await
            .unwrap()
            .iter()
            .map(|v| v.id())
            .collect::<Vec<_>>(),
        ["local-lvm:vm-9000-disk-0", "local-lvm:vm-9010-disk-0"]
    );
    let native = fake
        .native_vm_config(plan.node(), plan.target_vmid())
        .await
        .unwrap();
    assert_eq!(
        fake.vm_config(plan.node(), plan.target_vmid())
            .await
            .unwrap()
            .smbios_uuid(),
        Some(native.uuid())
    );
    assert!(
        !fake
            .qga_ping(plan.node(), plan.target_vmid())
            .await
            .unwrap()
            .reachable()
    );
    assert_eq!(
        fake.native_vm_config(&NodeName::parse("other").unwrap(), plan.target_vmid())
            .await
            .unwrap_err(),
        PveReadError::NotFound
    );
}

// Catches normal infrastructure reads returning the original, expired seed time.
#[tokio::test]
async fn normal_infrastructure_reads_refresh_time_but_explicit_stale_config_does_not() {
    let p = plan();
    let fake = fake();
    let aged = Utc::now() - Duration::seconds(60);
    fake.set_node_status(
        NodeStatus::from_wire(p.node().clone(), json!({"uptime":100}), aged).unwrap(),
    );
    fake.set_storage_status(
        StorageStatus::from_wire(
            p.node().clone(),
            p.storage().clone(),
            json!({"active":1,"enabled":1,"avail":17179869184_u64,"content":"images"}),
            aged,
        )
        .unwrap(),
    );
    fake.set_bridges(BridgeInventory::from_wire(p.node().clone(),
        json!([{"type":"bridge","iface":"vmbr0","active":1},{"type":"bridge","iface":"vmbr1","active":0}]), aged).unwrap());
    let before = Utc::now();
    let node = fake.node_status(p.node()).await.unwrap();
    let storage = fake.storage_status(p.node(), p.storage()).await.unwrap();
    let bridges = fake.bridges(p.node()).await.unwrap();
    let after = Utc::now();
    for observed_at in [
        node.observed_at(),
        storage.observed_at(),
        bridges.observed_at(),
    ] {
        assert!(
            observed_at >= before && observed_at <= after,
            "normal read must observe at read time"
        );
    }
    assert!(node.is_fresh(after) && storage.is_fresh(after) && bridges.is_fresh(after));
    assert_eq!(node.uptime(), 100);
    assert_eq!(storage.available_bytes(), 17179869184);
    assert!(bridges.has_active(p.bridge()));
    assert_eq!(
        bridges.bridges().get(&BridgeName::parse("vmbr1").unwrap()),
        Some(&false)
    );

    let mut snapshot = serde_json::to_value(config(9000, true)).unwrap();
    snapshot["observed_at"] = json!(aged);
    let stale: NativeVmConfig = serde_json::from_value(snapshot).unwrap();
    fake.enqueue_config_read(
        p.source_vmid(),
        FakeConfigRead::Snapshot(Box::new(stale.clone())),
    );
    assert_eq!(
        fake.native_vm_config(p.node(), p.source_vmid())
            .await
            .unwrap(),
        stale
    );
    assert!(!stale.is_fresh(Utc::now()));
    assert!(
        fake.native_vm_config(p.node(), p.source_vmid())
            .await
            .unwrap()
            .is_fresh(Utc::now())
    );
}

#[tokio::test]
async fn http_observer_never_imports_fake_provenance_from_response() {
    let source = config(9000, true);
    let mut response = json!({"digest":source.digest(),"name":"template","cores":1,"memory":512,
        "scsi0":"local-lvm:vm-9000-disk-0","smbios1":"uuid=4f2504e0-4f89-41d3-9a0c-0305e82c3301",
        "net0":"virtio=02:00:00:00:00:01,bridge=vmbr0","agent":0,"boot":"order=scsi0","template":1});
    let fake = fake();
    let clone = CloneRequest::new(plan(), OperationId::new());
    fake.clone_vm(&clone).await.unwrap();
    let observed = fake
        .native_vm_config(plan().node(), plan().target_vmid())
        .await
        .unwrap();
    response["fake_clone_provenance"] =
        serde_json::to_value(observed.fake_clone_provenance()).unwrap();
    let server = support::Server::json(response).await;
    assert!(server.url.starts_with("http://127.0.0.1:"));
    let http = server
        .observer()
        .native_vm_config(plan().node(), plan().source_vmid())
        .await
        .unwrap();
    assert!(http.fake_clone_provenance().is_none());
    assert_eq!(server.requests.lock().unwrap().len(), 1);
    assert!(
        server.requests.lock().unwrap()[0]
            .starts_with("GET /api2/json/nodes/pve-test/qemu/9000/config HTTP/1.1")
    );
    assert!(
        http.unsupported()
            .contains(&UnsupportedConfig::UnknownField)
    );
    server.finish().await;
}
// Catches fake hooks bypassing the typed read boundary or submitting before release.
#[tokio::test]
async fn typed_fault_hooks_preserve_read_errors_and_gate_submission() {
    let f = fake();
    f.enqueue_config_read(
        plan().source_vmid(),
        FakeConfigRead::Error(PveReadError::Unauthorized),
    );
    assert_eq!(
        f.native_vm_config(plan().node(), plan().source_vmid())
            .await,
        Err(PveReadError::Unauthorized)
    );
    f.enqueue_config_read(
        plan().source_vmid(),
        FakeConfigRead::Snapshot(Box::new(config(9000, true))),
    );
    assert!(
        f.native_vm_config(plan().node(), plan().source_vmid())
            .await
            .is_ok()
    );
    let gate = f.pause_next_submission();
    let clone = f.clone();
    let task = tokio::spawn(async move {
        clone
            .clone_vm(&CloneRequest::new(plan(), OperationId::new()))
            .await
    });
    gate.entered().await;
    assert!(f.recorded_requests().is_empty());
    gate.release();
    task.await.unwrap().unwrap();
    assert_eq!(f.recorded_requests().len(), 1);
}
