use chrono::{DateTime, Duration, Utc};
use controller_domain::{AttemptId, ExecutionState, OperationId, RunId};
use operation_controller::decision::*;
use proptest::prelude::*;
use pve_port::*;
use serde_json::{Value, json};

#[test]
fn running_clone_cannot_treat_unowned_occupant_as_pending_owned_work() {
    let mut f = Fixture::clone_outcome();
    let c = f
        .input
        .target_config
        .as_ref()
        .unwrap()
        .result
        .as_ref()
        .unwrap();
    f.target(patch_config(c, "fake_clone_provenance", Value::Null), false);
    f.input.task = Some(read(Ok(task(NativeStep::Clone, TaskState::Running))));
    assert_eq!(
        f.eval(),
        NativeEvaluation {
            decision: NativeDecision::Unknown,
            reason: NativeReason::ProvenanceMissing
        }
    );
}

#[test]
fn outcome_observations_must_postdate_dispatch_including_absence_and_power() {
    for which in 0..4 {
        let mut f = Fixture::owned(NativeStep::Start, true, true);
        let past = now() - Duration::seconds(1);
        match which {
            0 => {
                let p = f
                    .input
                    .target_power
                    .as_ref()
                    .unwrap()
                    .result
                    .as_ref()
                    .unwrap();
                let mut v = serde_json::to_value(p).unwrap();
                v["observed_at"] = json!(past);
                f.input.target_power = Some(read(Ok(serde_json::from_value(v).unwrap())));
            }
            1 => f.input.inventory.as_mut().unwrap().observed_at = past,
            2 => {
                f.context.dispatched_at = Some(now() + Duration::milliseconds(1));
                f.input.collected_at = now() + Duration::seconds(1);
                f.input.evaluated_at = f.input.collected_at;
            }
            _ => {
                f = Fixture::clone_outcome();
                f.input.target_config = Some(NativeRead::new(past, Err(PveReadError::NotFound)));
                f.input.target_power = None;
                f.input.inventory = Fixture::clone_preflight().input.inventory;
                f.input.identities.retain(|r| r.vmid != vm().target_vmid());
                f.input.task = Some(read(Ok(task(
                    NativeStep::Clone,
                    TaskState::CompleteFailure,
                ))));
            }
        }
        assert_eq!(f.eval().decision, NativeDecision::Unknown, "case {which}");
    }
}

#[test]
fn every_desired_field_is_required_for_configure_and_start_satisfaction() {
    for step in [NativeStep::Configure, NativeStep::Start] {
        for (key, value) in [
            ("uuid", json!("6f2504e0-4f89-41d3-9a0c-0305e82c3301")),
            ("mac", json!("02:00:00:00:77:77")),
            ("bridge", json!("vmbr1")),
            ("cores", json!(4)),
            ("memory_mib", json!(4096)),
            ("agent_enabled", json!(false)),
            ("boots_scsi0", json!(false)),
        ] {
            let mut f = Fixture::owned(step, true, true);
            let c = f
                .input
                .target_config
                .as_ref()
                .unwrap()
                .result
                .as_ref()
                .unwrap();
            let mut wire = serde_json::to_value(c).unwrap();
            wire[key] = value;
            if key == "boots_scsi0" {
                wire["unsupported"] = json!(["boot_order"]);
            }
            f.target(
                serde_json::from_value(wire).unwrap(),
                step == NativeStep::Start,
            );
            assert!(
                !matches!(
                    f.eval().decision,
                    NativeDecision::Ready | NativeDecision::Satisfied
                ),
                "{step:?} {key}"
            );
        }
    }
}

fn now() -> DateTime<Utc> {
    "2026-09-04T12:00:00Z".parse().unwrap()
}
fn vm() -> NativeVmPlan {
    serde_json::from_value(
        json!({"contract_version":1,"cluster_key":"fake-local","node":"pve-test",
        "source_vmid":9000,"target_vmid":9010,"name":"native-proof-9010","storage":"local-lvm",
        "bridge":"vmbr0","uuid":"3f2504e0-4f89-41d3-9a0c-0305e82c3301","mac":"02:00:00:00:90:10",
        "cores":2,"memory_mib":2048,"minimum_storage_bytes":17179869184_u64}),
    )
    .unwrap()
}
fn patch_config(config: &NativeVmConfig, key: &str, value: Value) -> NativeVmConfig {
    let mut wire = serde_json::to_value(config).unwrap();
    wire[key] = value;
    serde_json::from_value(wire).unwrap()
}
fn config(vmid: u32, template: bool) -> NativeVmConfig {
    NativeVmConfig::from_wire(
        vm().node().clone(),
        Vmid::new(vmid).unwrap(),
        json!({
        "digest":"initial-digest","name":if template {"template"} else {"native-proof-9010"},
        "cores":1,"memory":512,"scsi0":format!("local-lvm:vm-{vmid}-disk-0"),
        "smbios1":"uuid=4f2504e0-4f89-41d3-9a0c-0305e82c3301",
        "net0":"virtio=02:00:00:00:00:01,bridge=vmbr0","agent":0,"boot":"order=scsi0",
        "template":u8::from(template)}),
        now(),
    )
    .unwrap()
}
fn task(step: NativeStep, state: TaskState) -> TaskStatus {
    let (worker, id) = if step == NativeStep::Clone {
        ("qmclone", 9000)
    } else {
        ("qmstart", 9010)
    };
    TaskStatus::new(
        Upid::parse(format!(
            "UPID:pve-test:00000001:00000002:00000003:{worker}:{id}:proof@pve:"
        ))
        .unwrap(),
        state,
        now(),
    )
}
fn power(vmid: u32, running: bool) -> VmPowerStatus {
    VmPowerStatus::from_wire(
        vm().node().clone(),
        Vmid::new(vmid).unwrap(),
        json!({"vmid":vmid,"status":if running {"running"} else {"stopped"},"locked":0}),
        now(),
    )
    .unwrap()
}
fn read<T>(result: Result<T, PveReadError>) -> NativeRead<T> {
    NativeRead::new(now(), result)
}
fn identity(config: NativeVmConfig) -> NativeIdentityRead {
    NativeIdentityRead {
        node: config.node().clone(),
        vmid: config.vmid(),
        read: read(Ok(config)),
    }
}
#[derive(Clone)]
struct Fixture {
    input: NativeEvidenceInput,
    context: NativeEvaluationContext,
}
impl Fixture {
    fn clone_preflight() -> Self {
        let plan = NativeOperationPlan::new(NativeStep::Clone, vm());
        let binding =
            NativeBinding::new(RunId::new(), OperationId::new(), AttemptId::new(), &plan, 1);
        let clone_request = CloneRequest::new(vm(), binding.operation_id());
        let source = config(9000, true);
        let input = NativeEvidenceInput {
            binding: binding.clone(),
            plan,
            source: NativeEvidenceSource::FakePve,
            collected_at: now(),
            evaluated_at: now(),
            node: Some(read(NodeStatus::from_wire(
                vm().node().clone(),
                json!({"uptime":123}),
                now(),
            ))),
            storage: Some(read(StorageStatus::from_wire(
                vm().node().clone(),
                vm().storage().clone(),
                json!({"active":1,"enabled":1,"content":"images","avail":34359738368_u64}),
                now(),
            ))),
            bridges: Some(read(BridgeInventory::from_wire(
                vm().node().clone(),
                json!([{"type":"bridge","iface":"vmbr0","active":1}]),
                now(),
            ))),
            inventory: Some(read(ClusterVmInventory::from_wire(
                json!([{"vmid":9000,"node":"pve-test","name":"template","type":"qemu","template":1,"status":"stopped"}]),
                now(),
            ))),
            identities: vec![identity(source.clone())],
            source_config: Some(read(Ok(source))),
            source_power: Some(read(Ok(power(9000, false)))),
            target_config: Some(read(Err(PveReadError::NotFound))),
            target_power: None,
            task: None,
            receipt: None,
            bound_intermediate: None,
        };
        let context = NativeEvaluationContext {
            binding,
            source: NativeEvidenceSource::FakePve,
            state: ExecutionState::Leased,
            mode: NativeEvaluationMode::Preflight,
            cancelled: false,
            possible_dispatch: false,
            mutation_deadline: None,
            transport_lost: false,
            receipt: None,
            dispatched_at: None,
            configure_satisfied: false,
            uninterrupted_workflow: true,
            clone_request,
            clone_ownership: None,
        };
        Self { input, context }
    }
    fn target(&mut self, config: NativeVmConfig, running: bool) {
        self.input.target_config = Some(read(Ok(config.clone())));
        self.input.target_power = Some(read(Ok(power(9010, running))));
        self.input
            .identities
            .retain(|v| v.vmid != vm().target_vmid());
        self.input.identities.push(identity(config));
        self.input.inventory = Some(read(ClusterVmInventory::from_wire(
            json!([
                {"vmid":9000,"node":"pve-test","name":"template","type":"qemu","template":1,"status":"stopped"},
                {"vmid":9010,"node":"pve-test","name":"native-proof-9010","type":"qemu","template":0,"status":if running {"running"} else {"stopped"}}
            ]),
            now(),
        )));
    }
    fn clone_outcome() -> Self {
        let mut f = Self::clone_preflight();
        let clone = &f.context.clone_request;
        let mut c = serde_json::to_value(config(9010, false)).unwrap();
        c["uuid"] = json!("5f2504e0-4f89-41d3-9a0c-0305e82c3301");
        c["mac"] = json!("02:00:00:00:00:02");
        c["fake_clone_provenance"] = json!({"operation_id":clone.operation_id(),"request_marker":clone.request_marker(),
            "source_vmid":9000,"target_vmid":9010,"request_digest":clone.request_digest()});
        f.target(serde_json::from_value(c).unwrap(), false);
        f.context.mode = NativeEvaluationMode::Outcome;
        f.context.state = ExecutionState::Running;
        f.context.possible_dispatch = true;
        f.context.dispatched_at = Some(now());
        let task = task(NativeStep::Clone, TaskState::CompleteSuccess);
        f.input.receipt = Some(NativeReceipt {
            binding: f.input.binding.clone(),
            accepted_at: now(),
            receipt: MutationReceipt::Task(task.upid().clone()),
        });
        f.context.receipt = f.input.receipt.clone();
        f.input.task = Some(read(Ok(task)));
        f
    }
    fn owned(step: NativeStep, final_fields: bool, outcome: bool) -> Self {
        let mut f = Self::clone_outcome();
        let bound = f
            .input
            .target_config
            .as_ref()
            .unwrap()
            .result
            .as_ref()
            .unwrap()
            .clone();
        f.context.clone_ownership = Some(
            NativeCloneOwnership::from_satisfied_clone(
                &f.context,
                NativeEvidence::new(f.input.clone()).unwrap(),
                now(),
            )
            .unwrap(),
        );
        f.input.bound_intermediate = Some(bound.clone());
        f.input.plan = NativeOperationPlan::new(step, vm());
        f.input.binding = NativeBinding::new(
            f.input.binding.run_id(),
            OperationId::new(),
            AttemptId::new(),
            &f.input.plan,
            1,
        );
        f.context.binding = f.input.binding.clone();
        f.context.configure_satisfied = step == NativeStep::Start;
        f.context.possible_dispatch = outcome;
        f.context.state = if outcome {
            ExecutionState::Running
        } else {
            ExecutionState::Leased
        };
        f.context.dispatched_at = outcome.then(now);
        f.context.mode = if outcome {
            NativeEvaluationMode::Outcome
        } else {
            NativeEvaluationMode::Preflight
        };
        if final_fields {
            let mut c = serde_json::to_value(bound).unwrap();
            c["uuid"] = json!(vm().uuid());
            c["mac"] = json!(vm().mac());
            c["cores"] = json!(2);
            c["memory_mib"] = json!(2048);
            c["agent_enabled"] = json!(true);
            f.target(
                serde_json::from_value(c).unwrap(),
                step == NativeStep::Start && outcome,
            );
        }
        f.input.receipt = None;
        f.input.task = None;
        if outcome {
            let receipt = if step == NativeStep::Configure {
                MutationReceipt::SynchronousAccepted
            } else {
                let task = task(step, TaskState::CompleteSuccess);
                f.input.task = Some(read(Ok(task.clone())));
                MutationReceipt::Task(task.upid().clone())
            };
            f.input.receipt = Some(NativeReceipt {
                binding: f.input.binding.clone(),
                accepted_at: now(),
                receipt,
            });
        }
        f.context.receipt = f.input.receipt.clone();
        f
    }
    fn eval(&self) -> NativeEvaluation {
        let e = NativeEvidence::new(self.input.clone()).unwrap();
        if self.context.mode == NativeEvaluationMode::Preflight {
            evaluate_native_preflight(&self.context, &e, now())
        } else {
            evaluate_native_outcome(&self.context, &e, now())
        }
    }
}

#[test]
fn freshness_rejects_future_and_accepts_exact_boundary() {
    assert!(is_fresh(now() - Duration::seconds(30), now()));
    assert!(!is_fresh(
        now() - Duration::seconds(30) - Duration::milliseconds(1),
        now()
    ));
    assert!(!is_fresh(now() + Duration::milliseconds(1), now()));
}

#[test]
fn decision_matrix() {
    use NativeDecision::*;
    use NativeReason::*;
    type Change = fn(&mut Fixture);
    let rows: Vec<(&str, Fixture, Change, NativeDecision, NativeReason)> = vec![
        (
            "clone ready",
            Fixture::clone_preflight(),
            |_| {},
            Ready,
            CloneReady,
        ),
        (
            "cancelled",
            Fixture::clone_preflight(),
            |f| f.context.cancelled = true,
            Unknown,
            Cancelled,
        ),
        (
            "unauthorized preflight",
            Fixture::clone_preflight(),
            |f| f.input.node = Some(read(Err(PveReadError::Unauthorized))),
            Blocked,
            Unauthorized,
        ),
        (
            "missing inventory",
            Fixture::clone_preflight(),
            |f| f.input.inventory = None,
            Unknown,
            ObservationMissing,
        ),
        (
            "missing coverage",
            Fixture::clone_preflight(),
            |f| f.input.identities.clear(),
            Unknown,
            IncompleteIdentityCoverage,
        ),
        (
            "unreadable other VM",
            Fixture::clone_preflight(),
            |f| f.input.identities[0].read = read(Err(PveReadError::TransportUnavailable)),
            Unknown,
            ObservationUnavailable,
        ),
        (
            "occupied target",
            Fixture::clone_preflight(),
            |f| f.target(config(9010, false), false),
            Conflicted,
            TargetOccupied,
        ),
        (
            "clone success",
            Fixture::clone_outcome(),
            |_| {},
            Satisfied,
            CloneSatisfied,
        ),
        (
            "clone running",
            Fixture::clone_outcome(),
            |f| f.input.task = Some(read(Ok(task(NativeStep::Clone, TaskState::Running)))),
            Waiting,
            TaskRunning,
        ),
        (
            "clone task alone",
            Fixture::clone_outcome(),
            |f| f.input.target_config = Some(read(Err(PveReadError::NotFound))),
            Conflicted,
            InventoryContradiction,
        ),
        (
            "missing clone receipt",
            Fixture::clone_outcome(),
            |f| f.input.receipt = None,
            Unknown,
            ReceiptMissing,
        ),
        (
            "missing provenance",
            Fixture::clone_outcome(),
            |f| {
                let c = f
                    .input
                    .target_config
                    .as_ref()
                    .unwrap()
                    .result
                    .as_ref()
                    .unwrap();
                f.target(patch_config(c, "fake_clone_provenance", Value::Null), false);
            },
            Unknown,
            ProvenanceMissing,
        ),
        (
            "configure ready",
            Fixture::owned(NativeStep::Configure, false, false),
            |_| {},
            Ready,
            ConfigureReady,
        ),
        (
            "configure already set",
            Fixture::owned(NativeStep::Configure, true, false),
            |_| {},
            Satisfied,
            AlreadySatisfied,
        ),
        (
            "configure acceptance alone",
            Fixture::owned(NativeStep::Configure, false, true),
            |_| {},
            Waiting,
            DesiredConfigPending,
        ),
        (
            "configure success",
            Fixture::owned(NativeStep::Configure, true, true),
            |_| {},
            Satisfied,
            ConfigureSatisfied,
        ),
        (
            "start ready",
            Fixture::owned(NativeStep::Start, true, false),
            |_| {},
            Ready,
            StartReady,
        ),
        (
            "start configure missing",
            Fixture::owned(NativeStep::Start, true, false),
            |f| f.context.configure_satisfied = false,
            Blocked,
            ConfigureNotSatisfied,
        ),
        (
            "start success",
            Fixture::owned(NativeStep::Start, true, true),
            |_| {},
            Satisfied,
            StartSatisfied,
        ),
        (
            "start no task",
            Fixture::owned(NativeStep::Start, true, true),
            |f| f.input.task = None,
            Unknown,
            ObservationMissing,
        ),
        (
            "start running task",
            Fixture::owned(NativeStep::Start, true, true),
            |f| f.input.task = Some(read(Ok(task(NativeStep::Start, TaskState::Running)))),
            Waiting,
            TaskRunning,
        ),
        (
            "start stopped",
            Fixture::owned(NativeStep::Start, true, true),
            |f| {
                let c = f
                    .input
                    .target_config
                    .as_ref()
                    .unwrap()
                    .result
                    .as_ref()
                    .unwrap()
                    .clone();
                f.target(c, false);
            },
            Unknown,
            PowerNotRunning,
        ),
        (
            "late matching cancelled outcome",
            Fixture::clone_outcome(),
            |f| {
                f.context.cancelled = true;
                f.context.state = ExecutionState::Unknown;
                f.context.mode = NativeEvaluationMode::Reconciliation;
            },
            Satisfied,
            CloneSatisfied,
        ),
        (
            "cancelled active outcome",
            Fixture::clone_outcome(),
            |f| f.context.cancelled = true,
            Unknown,
            Cancelled,
        ),
        (
            "late conflict preserved",
            Fixture::clone_outcome(),
            |f| {
                f.context.state = ExecutionState::Conflicted;
                f.context.mode = NativeEvaluationMode::Reconciliation;
            },
            Conflicted,
            StateNotEligible,
        ),
    ];
    for (name, mut f, change, decision, reason) in rows {
        change(&mut f);
        assert_eq!(f.eval(), NativeEvaluation { decision, reason }, "{name}");
    }
}

#[test]
fn validated_roundtrip_rejects_cross_binding_and_fake_source_relabeling() {
    let f = Fixture::clone_outcome();
    let e = NativeEvidence::new(f.input).unwrap();
    let wire = serde_json::to_value(&e).unwrap();
    assert_eq!(
        serde_json::from_value::<NativeEvidence>(wire.clone()).unwrap(),
        e
    );
    for (path, value) in [
        ("/receipt/binding/attempt_id", json!(AttemptId::new())),
        ("/binding/plan_digest", json!("0".repeat(64))),
        ("/source", json!("pve_api")),
        (
            "/receipt/receipt",
            json!({"task":"UPID:pve-test:00000001:00000002:00000003:qmclone:9010:proof@pve:"}),
        ),
    ] {
        let mut bad = wire.clone();
        *bad.pointer_mut(path).unwrap() = value;
        assert!(
            serde_json::from_value::<NativeEvidence>(bad).is_err(),
            "{path}"
        );
    }
    let mut bad = wire;
    bad["token"] = json!("must-never-survive");
    assert!(serde_json::from_value::<NativeEvidence>(bad).is_err());
}

proptest! {
    #[test]
    fn stale_inventory_never_authorizes(age_ms in 30001_i64..1000000) {
        let mut f=Fixture::clone_preflight();let r=f.input.inventory.as_mut().unwrap();r.observed_at=now()-Duration::milliseconds(age_ms);
        let result=f.eval();prop_assert!(!matches!(result.decision,NativeDecision::Ready|NativeDecision::Satisfied));
    }
}

#[test]
fn receipt_survives_later_evidence_fences_but_evidence_context_does_not() {
    let mut f = Fixture::clone_outcome();
    let b = &f.input.binding;
    f.input.binding = NativeBinding::new(
        b.run_id(),
        b.operation_id(),
        b.attempt_id(),
        &f.input.plan,
        9,
    );
    f.context.binding = f.input.binding.clone();
    assert_eq!(f.eval().decision, NativeDecision::Satisfied);
    let mut different = serde_json::to_value(&f.context.binding).unwrap();
    different["evidence_fence"] = json!(10);
    f.context.binding = serde_json::from_value(different).unwrap();
    assert_eq!(
        f.eval(),
        NativeEvaluation {
            decision: NativeDecision::Conflicted,
            reason: NativeReason::BindingMismatch
        }
    );
}

#[test]
fn independent_fresh_reads_can_agree_without_identical_timestamps() {
    let mut f = Fixture::clone_outcome();
    f.input.receipt.as_mut().unwrap().accepted_at = now() - Duration::seconds(1);
    f.context.receipt = f.input.receipt.clone();
    f.context.dispatched_at = Some(now() - Duration::seconds(1));
    for identity in &mut f.input.identities {
        let c = identity.read.result.as_ref().unwrap();
        identity.read.result = Ok(patch_config(
            c,
            "observed_at",
            json!(now() - Duration::milliseconds(1)),
        ));
    }
    assert_eq!(f.eval().decision, NativeDecision::Satisfied);
}

#[test]
fn coherent_receipt_and_task_substitution_cannot_replace_persisted_upid() {
    let mut f = Fixture::clone_outcome();
    let swapped =
        Upid::parse("UPID:pve-test:00000009:00000002:00000003:qmclone:9000:proof@pve:").unwrap();
    f.input.receipt.as_mut().unwrap().receipt = MutationReceipt::Task(swapped.clone());
    f.input.task = Some(read(Ok(TaskStatus::complete(swapped, now()))));
    assert_eq!(
        f.eval(),
        NativeEvaluation {
            decision: NativeDecision::Conflicted,
            reason: NativeReason::BindingMismatch
        }
    );
}

#[test]
fn receipt_requires_persisted_dispatch_and_preflight_cannot_ignore_it() {
    let mut f = Fixture::clone_outcome();
    f.context.receipt = None;
    assert_eq!(f.eval().decision, NativeDecision::Conflicted);
    let mut f = Fixture::owned(NativeStep::Configure, true, true);
    f.context.mode = NativeEvaluationMode::Preflight;
    f.context.possible_dispatch = false;
    f.context.state = ExecutionState::Leased;
    assert_eq!(f.eval().decision, NativeDecision::Unknown);
}

#[test]
fn configure_response_loss_can_reconcile_only_from_owned_exact_postconditions() {
    for (step, desired, want) in [
        (NativeStep::Configure, true, NativeDecision::Satisfied),
        (NativeStep::Configure, false, NativeDecision::Unknown),
        (NativeStep::Start, true, NativeDecision::Unknown),
    ] {
        let mut f = Fixture::owned(step, desired, true);
        f.input.receipt = None;
        f.context.receipt = None;
        f.context.transport_lost = true;
        f.context.cancelled = true;
        f.context.state = ExecutionState::Unknown;
        f.context.mode = NativeEvaluationMode::Reconciliation;
        assert_eq!(f.eval().decision, want, "{step:?} desired={desired}");
        f.context.possible_dispatch = false;
        assert_ne!(f.eval().decision, NativeDecision::Satisfied);
    }
}

#[test]
fn duplicate_boot_volume_cannot_establish_unique_clone_ownership() {
    let mut f = Fixture::clone_outcome();
    let source = f
        .input
        .source_config
        .as_ref()
        .unwrap()
        .result
        .as_ref()
        .unwrap();
    let target = f
        .input
        .target_config
        .as_ref()
        .unwrap()
        .result
        .as_ref()
        .unwrap();
    let changed = patch_config(
        source,
        "boot_disk",
        serde_json::to_value(target.boot_disk()).unwrap(),
    );
    f.input.source_config = Some(read(Ok(changed.clone())));
    f.input.identities[0] = identity(changed);
    assert_eq!(f.eval().decision, NativeDecision::Conflicted);
}

#[test]
fn provenance_conflict_precedes_running_task_and_cancellation() {
    let mut f = Fixture::clone_outcome();
    let c = f
        .input
        .target_config
        .as_ref()
        .unwrap()
        .result
        .as_ref()
        .unwrap();
    let mut marker = serde_json::to_value(c.fake_clone_provenance().unwrap()).unwrap();
    marker["request_digest"] = json!("0".repeat(64));
    f.target(patch_config(c, "fake_clone_provenance", marker), false);
    f.input.task = Some(read(Ok(task(NativeStep::Clone, TaskState::Running))));
    f.context.cancelled = true;
    assert_eq!(
        f.eval(),
        NativeEvaluation {
            decision: NativeDecision::Conflicted,
            reason: NativeReason::ProvenanceMismatch
        }
    );
}

#[test]
fn unauthorized_required_read_precedes_missing_observation() {
    let mut f = Fixture::clone_preflight();
    f.input.inventory = None;
    f.input.node = Some(read(Err(PveReadError::Unauthorized)));
    assert_eq!(
        f.eval(),
        NativeEvaluation {
            decision: NativeDecision::Blocked,
            reason: NativeReason::Unauthorized
        }
    );
}

#[test]
fn deadline_or_transport_loss_require_reconciliation_to_settle() {
    for deadline in [true, false] {
        let mut f = Fixture::clone_outcome();
        if deadline {
            f.context.mutation_deadline = Some(now());
        } else {
            f.context.transport_lost = true;
        }
        assert_eq!(f.eval().decision, NativeDecision::Unknown);
        f.context.mode = NativeEvaluationMode::Reconciliation;
        f.context.state = ExecutionState::Unknown;
        assert_eq!(f.eval().decision, NativeDecision::Satisfied);
    }
}

#[test]
fn successful_proof_cannot_use_target_snapshot_from_before_dispatch() {
    let mut f = Fixture::clone_outcome();
    let c = f
        .input
        .target_config
        .as_ref()
        .unwrap()
        .result
        .as_ref()
        .unwrap();
    f.target(
        patch_config(c, "observed_at", json!(now() - Duration::seconds(1))),
        false,
    );
    assert_eq!(f.eval().decision, NativeDecision::Unknown);
}

#[test]
fn ownership_roundtrip_preserves_dispatch_before_intervening_observations() {
    let mut f = Fixture::clone_outcome();
    f.context.dispatched_at = Some(now() - Duration::seconds(2));
    let intervening = now() - Duration::seconds(1);
    let mut input = serde_json::to_value(&f.input).unwrap();
    for path in [
        "/inventory/observed_at",
        "/inventory/result/Ok/observed_at",
        "/target_power/observed_at",
        "/target_power/result/Ok/observed_at",
    ] {
        *input.pointer_mut(path).unwrap() = json!(intervening);
    }
    let evidence = NativeEvidence::new(serde_json::from_value(input).unwrap()).unwrap();
    let ownership =
        NativeCloneOwnership::from_satisfied_clone(&f.context, evidence, now()).unwrap();
    let wire = serde_json::to_value(&ownership).unwrap();
    assert_eq!(
        serde_json::from_value::<NativeCloneOwnership>(wire.clone()).unwrap(),
        ownership
    );
    assert_eq!(wire["dispatched_at"], json!(now() - Duration::seconds(2)));

    for invalid in [
        json!(now()),
        json!(now() + Duration::seconds(1)),
        Value::Null,
        json!("invalid"),
    ] {
        let mut invalid_wire = wire.clone();
        invalid_wire["dispatched_at"] = invalid;
        assert!(serde_json::from_value::<NativeCloneOwnership>(invalid_wire).is_err());
    }
    let mut missing = wire;
    missing.as_object_mut().unwrap().remove("dispatched_at");
    assert!(serde_json::from_value::<NativeCloneOwnership>(missing).is_err());
}

#[test]
fn ownership_reload_requires_full_historical_success_and_coverage() {
    let f = Fixture::owned(NativeStep::Configure, false, false);
    let ownership = f.context.clone_ownership.as_ref().unwrap();
    let wire = serde_json::to_value(ownership).unwrap();
    assert_eq!(
        serde_json::from_value::<NativeCloneOwnership>(wire.clone()).unwrap(),
        *ownership
    );
    for (path, value) in [
        ("/proof/inventory", Value::Null),
        ("/proof/identities", json!([])),
        ("/proof/receipt", Value::Null),
        ("/proof/task/result/Ok/state", json!("running")),
        ("/request/operation_id", json!(OperationId::new())),
        ("/bound_at", json!(now() + Duration::seconds(31))),
    ] {
        let mut bad = wire.clone();
        *bad.pointer_mut(path).unwrap() = value;
        assert!(
            serde_json::from_value::<NativeCloneOwnership>(bad).is_err(),
            "{path}"
        );
    }
    let mut clone = Fixture::clone_outcome();
    clone.context.clone_ownership = Some(ownership.clone());
    assert!(
        NativeCloneOwnership::from_satisfied_clone(
            &clone.context,
            NativeEvidence::new(clone.input).unwrap(),
            now()
        )
        .is_err()
    );
}

#[test]
fn additional_matrix_rows() {
    use NativeDecision::*;
    use NativeReason::*;
    type Change = fn(&mut Fixture);
    let rows: Vec<(&str, Fixture, Change, NativeDecision, NativeReason)> = vec![
        (
            "unavailable target",
            Fixture::clone_preflight(),
            |f| f.input.target_config = Some(read(Err(PveReadError::TransportUnavailable))),
            Unknown,
            ObservationUnavailable,
        ),
        (
            "malformed source",
            Fixture::clone_preflight(),
            |f| f.input.source_config = Some(read(Err(PveReadError::InvalidResponse))),
            Unknown,
            ObservationUnavailable,
        ),
        (
            "stale absence",
            Fixture::clone_preflight(),
            |f| f.input.target_config.as_mut().unwrap().observed_at = now() - Duration::seconds(31),
            Unknown,
            ObservationNotFresh,
        ),
        (
            "future inventory",
            Fixture::clone_preflight(),
            |f| f.input.inventory.as_mut().unwrap().observed_at = now() + Duration::milliseconds(1),
            Unknown,
            ObservationNotFresh,
        ),
        (
            "insufficient storage",
            Fixture::clone_preflight(),
            |f| {
                f.input.storage = Some(read(StorageStatus::from_wire(
                    vm().node().clone(),
                    vm().storage().clone(),
                    json!({"active":1,"enabled":1,"content":"images","avail":1}),
                    now(),
                )));
            },
            Blocked,
            InsufficientCapacity,
        ),
        (
            "missing bridge",
            Fixture::clone_preflight(),
            |f| {
                f.input.bridges = Some(read(BridgeInventory::from_wire(
                    vm().node().clone(),
                    json!([]),
                    now(),
                )))
            },
            Blocked,
            UnsupportedInfrastructure,
        ),
        (
            "unsupported source layout",
            Fixture::clone_preflight(),
            |f| {
                let c = f
                    .input
                    .source_config
                    .as_ref()
                    .unwrap()
                    .result
                    .as_ref()
                    .unwrap();
                f.input.source_config =
                    Some(read(Ok(patch_config(c, "unsupported", json!(["args"])))));
            },
            Blocked,
            UnsupportedLayout,
        ),
        (
            "locked source",
            Fixture::clone_preflight(),
            |f| {
                let c = f
                    .input
                    .source_config
                    .as_ref()
                    .unwrap()
                    .result
                    .as_ref()
                    .unwrap();
                f.input.source_config = Some(read(Ok(patch_config(c, "locked", json!(true)))));
            },
            Blocked,
            VmNotStoppedUnlocked,
        ),
        (
            "clone failed with target",
            Fixture::clone_outcome(),
            |f| {
                f.input.task = Some(read(Ok(task(
                    NativeStep::Clone,
                    TaskState::CompleteFailure,
                ))))
            },
            Unknown,
            TaskFailed,
        ),
        (
            "clone failed with absence",
            Fixture::clone_outcome(),
            |f| {
                f.input.target_config = Some(read(Err(PveReadError::NotFound)));
                f.input.target_power = None;
                f.input.inventory = Fixture::clone_preflight().input.inventory;
                f.input.identities.retain(|r| r.vmid != vm().target_vmid());
                f.input.task = Some(read(Ok(task(
                    NativeStep::Clone,
                    TaskState::CompleteFailure,
                ))));
            },
            Failed,
            TaskFailed,
        ),
        (
            "unauthorized after dispatch",
            Fixture::clone_outcome(),
            |f| f.input.task = Some(read(Err(PveReadError::Unauthorized))),
            Unknown,
            Unauthorized,
        ),
        (
            "missing ownership",
            Fixture::owned(NativeStep::Configure, false, false),
            |f| f.context.clone_ownership = None,
            Unknown,
            OwnershipMissing,
        ),
        (
            "configure missing receipt",
            Fixture::owned(NativeStep::Configure, true, true),
            |f| f.input.receipt = None,
            Unknown,
            ReceiptMissing,
        ),
        (
            "already running owned start",
            Fixture::owned(NativeStep::Start, true, false),
            |f| {
                let c = f
                    .input
                    .target_config
                    .as_ref()
                    .unwrap()
                    .result
                    .as_ref()
                    .unwrap()
                    .clone();
                f.target(c, true);
            },
            Satisfied,
            AlreadySatisfied,
        ),
        (
            "already running interrupted workflow",
            Fixture::owned(NativeStep::Start, true, false),
            |f| {
                let c = f
                    .input
                    .target_config
                    .as_ref()
                    .unwrap()
                    .result
                    .as_ref()
                    .unwrap()
                    .clone();
                f.target(c, true);
                f.context.uninterrupted_workflow = false;
            },
            Unknown,
            StateNotEligible,
        ),
        (
            "start possible send prohibits shortcut",
            Fixture::owned(NativeStep::Start, true, false),
            |f| f.context.possible_dispatch = true,
            Unknown,
            DispatchAlreadyPossible,
        ),
    ];
    for (name, mut f, change, decision, reason) in rows {
        change(&mut f);
        assert_eq!(f.eval(), NativeEvaluation { decision, reason }, "{name}");
    }
}

#[test]
fn all_execution_states_guard_preflight_and_unknown_requires_narrow_mode() {
    for state in [
        ExecutionState::Pending,
        ExecutionState::Leased,
        ExecutionState::Running,
        ExecutionState::Waiting,
        ExecutionState::Cancelling,
        ExecutionState::Satisfied,
        ExecutionState::Failed,
        ExecutionState::Blocked,
        ExecutionState::Unknown,
        ExecutionState::Conflicted,
    ] {
        let mut f = Fixture::clone_preflight();
        f.context.state = state;
        assert_eq!(
            f.eval().decision == NativeDecision::Ready,
            matches!(state, ExecutionState::Leased | ExecutionState::Running),
            "{state:?}"
        );
        if !matches!(state, ExecutionState::Leased | ExecutionState::Running) {
            assert_ne!(f.eval().decision, NativeDecision::Satisfied);
        }
    }
    let mut f = Fixture::clone_outcome();
    f.context.state = ExecutionState::Unknown;
    assert_eq!(f.eval().decision, NativeDecision::Unknown);
    f.context.mode = NativeEvaluationMode::Reconciliation;
    assert_eq!(f.eval().decision, NativeDecision::Satisfied);
}

proptest! {
    #[test]
    fn contradictory_target_identity_never_authorizes(which in 0_u8..5, nonce in 100_u32..8999) {
        let mut f=Fixture::owned(NativeStep::Configure,false,false);
        let c=f.input.target_config.as_ref().unwrap().result.as_ref().unwrap();
        let (key,value)=match which {
            0=>("uuid",json!(format!("{nonce:08x}-4f89-41d3-9a0c-0305e82c3301"))),
            1=>("mac",json!(format!("02:01:00:{:02x}:{:02x}:01",nonce/256,nonce%256))),
            2=>("vmid",json!(nonce)),3=>("node",json!(format!("other-{nonce}"))),
            _=>("boot_disk",json!({"storage":"local-lvm","volume":format!("other-{nonce}")})),
        };
        let mut candidate=serde_json::to_value(c).unwrap();candidate[key]=value;
        if let Ok(config)=serde_json::from_value::<NativeVmConfig>(candidate) {f.target(config,false);} else {return Ok(());}
        let evaluation=f.eval();prop_assert!(!matches!(evaluation.decision,NativeDecision::Ready|NativeDecision::Satisfied));
    }
    #[test]
    fn changed_attempt_or_digest_never_authorizes(change_attempt in any::<bool>()) {
        let mut f=Fixture::clone_preflight();
        let mut binding=serde_json::to_value(&f.context.binding).unwrap();
        if change_attempt {binding["attempt_id"]=json!(AttemptId::new());} else {binding["plan_digest"]=json!("0".repeat(64));}
        f.context.binding=serde_json::from_value(binding).unwrap();
        prop_assert_eq!(f.eval().decision,NativeDecision::Conflicted);
    }
    #[test]
    fn different_vm_desired_uuid_or_mac_collision_never_authorizes(use_uuid in any::<bool>()) {
        let mut f=Fixture::clone_preflight();
        let c=f.input.source_config.as_ref().unwrap().result.as_ref().unwrap();
        let config=if use_uuid {patch_config(c,"uuid",json!(vm().uuid()))} else {patch_config(c,"mac",json!(vm().mac()))};
        f.input.source_config=Some(read(Ok(config.clone())));f.input.identities[0]=identity(config);
        prop_assert_eq!(f.eval().decision,NativeDecision::Conflicted);
    }
}
