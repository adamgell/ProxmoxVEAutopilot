#![allow(dead_code)]
use chrono::{DateTime, Utc};
use pve_port::*;
use serde_json::{Value, json};
pub const GIB: u64 = 1_073_741_824;
pub const SHA: &str = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789";
pub fn time() -> DateTime<Utc> {
    "2026-09-05T12:00:00Z".parse().unwrap()
}
pub fn node() -> NodeName {
    NodeName::parse("pve-test").unwrap()
}
pub fn vm() -> NativeVmPlan {
    serde_json::from_value(json!({"contract_version":1,"cluster_key":"lab","node":"pve-test","source_vmid":900,"target_vmid":101,"name":"deploy-101","storage":"local-lvm","bridge":"vmbr0","uuid":"3f2504e0-4f89-41d3-9a0c-0305e82c3301","mac":"02:00:00:00:01:01","cores":4,"memory_mib":4096,"minimum_storage_bytes":1})).unwrap()
}
pub fn source_json() -> Value {
    json!({"node":"pve-test","vmid":900,"digest":"digest-1","name":"blank-template","cores":2,"memory":2048,"scsi0":"local-lvm:vm-900-disk-0,size=80G","smbios1":"uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3390","net0":"virtio=02:00:00:00:09:00,bridge=vmbr0,firewall=0","bios":"seabios","cpu":"host","balloon":0,"agent":"enabled=0,type=virtio","boot":"order=scsi0","template":1})
}
pub fn source() -> ProvisioningVmConfigV1 {
    ProvisioningVmConfigV1::from_wire(
        node(),
        Vmid::new(900).unwrap(),
        NativeEvidenceSource::FakePve,
        source_json(),
        time(),
    )
    .unwrap()
}
pub fn expected() -> ProvisioningExpectationsV1 {
    ProvisioningExpectationsV1::new(ProvisioningExpectationsInputV1 {
        vm: vm(),
        template_config_sha256: &source().template_fingerprint().unwrap(),
        template_capacity_bytes: 80 * GIB,
        effective_capacity_bytes: 120 * GIB,
        system_serial: "SYS-101",
        disk_serial: "DISK-101",
        deployment_iso_volid: "local:iso/deployment.iso",
        driver_iso_volid: "drivers:iso/virtio.iso",
    })
    .unwrap()
}
pub fn plan(action: ProvisioningActionV1) -> ProvisioningOperationPlanV1 {
    ProvisioningOperationPlanV1::new(action, expected())
}
pub fn id<T: serde::de::DeserializeOwned>(n: u32) -> T {
    serde_json::from_value(json!(format!("00000000-0000-7000-8000-{n:012x}"))).unwrap()
}
pub fn binding(action: ProvisioningActionV1, op: u32, fence: u64) -> ProvisioningBindingV1 {
    ProvisioningBindingV1::new(id(1), id(op), id(op + 1000), SHA, &plan(action), fence).unwrap()
}
pub fn clone_request() -> CloneRequest {
    serde_json::from_value(json!({"vm":vm(),"operation_id":id::<controller_domain::OperationId>(10),"request_marker":"00000000-0000-4000-8000-000000000099"})).unwrap()
}
pub fn read<T>(v: T) -> NativeRead<T> {
    NativeRead::new(time(), Ok(v))
}
pub fn power(vmid: u32, running: bool) -> VmPowerStatus {
    VmPowerStatus::from_wire(
        node(),
        Vmid::new(vmid).unwrap(),
        json!({"vmid":vmid,"status":if running{"running"}else{"stopped"},"locked":0}),
        time(),
    )
    .unwrap()
}
pub fn before(c: ProvisioningVmConfigV1, running: bool) -> ProvisioningBeforeStateV1 {
    let p = power(c.vmid().get(), running);
    ProvisioningBeforeStateV1::new(c, p).unwrap()
}
pub fn intermediate() -> ProvisioningVmConfigV1 {
    let mut v = serde_json::to_value(source()).unwrap();
    let clone = clone_request();
    v["vmid"] = json!(101);
    v["name"] = json!("deploy-101");
    v["template"] = json!(false);
    v["digest"] = json!("clone-digest");
    v["uuid"] = json!("3f2504e0-4f89-41d3-9a0c-0305e82c33aa");
    v["mac"] = json!("02:00:00:00:01:aa");
    v["primary_disk"]["volume"] = json!("vm-101-disk-0");
    v["fake_clone_provenance"] = json!({"operation_id":clone.operation_id(),"request_marker":clone.request_marker(),"source_vmid":900,"target_vmid":101,"request_digest":clone.request_digest()});
    serde_json::from_value(v).unwrap()
}
pub fn changed(c: &ProvisioningVmConfigV1, f: impl FnOnce(&mut Value)) -> ProvisioningVmConfigV1 {
    let mut v = serde_json::to_value(c).unwrap();
    f(&mut v);
    serde_json::from_value(v).unwrap()
}
pub fn after(
    action: ProvisioningActionV1,
    current: &ProvisioningVmConfigV1,
) -> ProvisioningVmConfigV1 {
    changed(current, |v| {
        v["digest"] = json!(format!("after-{:?}", action));
        match action {
            ProvisioningActionV1::EnsureCapacity => {
                v["primary_disk"]["capacity_bytes"] = json!(120 * GIB)
            }
            ProvisioningActionV1::ConfigurePe => {
                v["cores"] = json!(4);
                v["memory_mib"] = json!(4096);
                v["uuid"] = json!(vm().uuid());
                v["mac"] = json!(vm().mac());
                v["system_serial"] = json!("SYS-101");
                v["primary_disk"]["serial"] = json!("DISK-101");
                v["qga_enabled"] = json!(true);
                v["boot_profile"] = json!("pe_media");
                v["deployment_iso"] = json!({"state":"iso","volid":"local:iso/deployment.iso"});
                v["driver_iso"] = json!({"state":"iso","volid":"drivers:iso/virtio.iso"});
            }
            ProvisioningActionV1::ConfigureDisk => {
                v["boot_profile"] = json!("installed_disk");
                v["deployment_iso"] = json!({"state":"absent"});
                v["driver_iso"] = json!({"state":"absent"});
            }
            _ => {}
        }
    })
}
pub fn inventory(configs: &[ProvisioningVmConfigV1], running: bool) -> ClusterVmInventory {
    let rows:Vec<_>=configs.iter().map(|c|json!({"vmid":c.vmid(),"node":c.node(),"type":"qemu","name":c.name(),"template":u8::from(c.is_template()),"status":if c.vmid().get()==101&&running{"running"}else{"stopped"}})).collect();
    ClusterVmInventory::from_wire(json!(rows), time()).unwrap()
}
pub fn facts(
    binding: ProvisioningBindingV1,
    plan: ProvisioningOperationPlanV1,
    target: Option<ProvisioningVmConfigV1>,
    running: bool,
) -> ProvisioningEvidenceInputV1 {
    let target_present = target.is_some();
    let mut configs = vec![source()];
    if let Some(t) = &target {
        configs.push(t.clone());
    }
    ProvisioningEvidenceInputV1 {
        binding,
        plan,
        source: NativeEvidenceSource::FakePve,
        collected_at: time(),
        node: Some(read(
            NodeStatus::from_wire(node(), json!({"uptime":100}), time()).unwrap(),
        )),
        storage: Some(read(
            StorageStatus::from_wire(
                node(),
                StorageName::parse("local-lvm").unwrap(),
                json!({"active":1,"enabled":1,"content":"images","avail":999999999999_u64}),
                time(),
            )
            .unwrap(),
        )),
        bridges: Some(read(
            BridgeInventory::from_wire(
                node(),
                json!([{"type":"bridge","iface":"vmbr0","active":1}]),
                time(),
            )
            .unwrap(),
        )),
        inventory: Some(read(inventory(&configs, running))),
        inventory_coverage: ProvisioningCoverageV1::Complete,
        identities: configs
            .iter()
            .map(|c| {
                ProvisioningIdentityReadV1::new(
                    c.node().clone(),
                    c.vmid(),
                    read(ProvisioningIdentitySnapshotV1::from_provisioning(c)),
                )
                .unwrap()
            })
            .collect(),
        source_config: Some(read(source())),
        source_power: Some(read(power(900, false))),
        target_config: Some(NativeRead::new(
            time(),
            target.ok_or(PveReadError::NotFound),
        )),
        target_power: target_present.then(|| read(power(101, running))),
        media: vec![
            read(
                ProvisioningMediaInventoryV1::new(
                    node(),
                    StorageName::parse("local").unwrap(),
                    vec!["local:iso/deployment.iso".into()],
                    ProvisioningCoverageV1::Complete,
                    time(),
                )
                .unwrap(),
            ),
            read(
                ProvisioningMediaInventoryV1::new(
                    node(),
                    StorageName::parse("drivers").unwrap(),
                    vec!["drivers:iso/virtio.iso".into()],
                    ProvisioningCoverageV1::Complete,
                    time(),
                )
                .unwrap(),
            ),
        ],
        qga: None,
        task: None,
        receipt: None,
    }
}
pub fn context(
    binding: ProvisioningBindingV1,
    plan: ProvisioningOperationPlanV1,
    owner: Option<ProvisioningCloneOwnershipV1>,
    prior: Option<ProvisioningStageBaselineV1>,
) -> ProvisioningEvaluationContextInputV1 {
    ProvisioningEvaluationContextInputV1 {
        binding,
        plan,
        source: NativeEvidenceSource::FakePve,
        state: controller_domain::ExecutionState::Leased,
        mode: ProvisioningEvaluationModeV1::Preflight,
        cancelled: false,
        mutation_deadline: time() + chrono::Duration::seconds(60),
        freshness_seconds: 30,
        dispatch: ProvisioningDispatchStateV1::NotDispatched,
        clone_request: clone_request(),
        clone_ownership: owner,
        predecessor: prior,
    }
}
pub fn request(
    c: &ProvisioningEvaluationContextV1,
    config: ProvisioningVmConfigV1,
    running: bool,
) -> ProvisioningMutationRequestV1 {
    let c = c.facts();
    if c.plan.action() == ProvisioningActionV1::Clone {
        return ProvisioningMutationRequestV1::Clone(
            CloneProvisioningRequestV1::new(
                c.binding.clone(),
                c.plan.clone(),
                c.clone_request.clone(),
                before(config, running),
                time(),
                30,
            )
            .unwrap(),
        );
    }
    let i = ProvisioningOwnedRequestInputV1 {
        binding: c.binding.clone(),
        plan: c.plan.clone(),
        ownership: c.clone_ownership.as_ref().unwrap(),
        predecessor: c.predecessor.as_ref().unwrap(),
        expected_before: before(config, running),
        as_of: time(),
        freshness_seconds: 30,
    };
    match c.plan.action() {
        ProvisioningActionV1::EnsureCapacity => {
            ProvisioningMutationRequestV1::GrowDisk(GrowDiskRequestV1::new(i).unwrap())
        }
        ProvisioningActionV1::ConfigurePe | ProvisioningActionV1::ConfigureDisk => {
            ProvisioningMutationRequestV1::Configure(
                ConfigureProvisioningRequestV1::new(i).unwrap(),
            )
        }
        ProvisioningActionV1::StartPe | ProvisioningActionV1::StartDisk => {
            ProvisioningMutationRequestV1::Start(StartProvisioningRequestV1::new(i).unwrap())
        }
        ProvisioningActionV1::EnsureStopped => {
            ProvisioningMutationRequestV1::Stop(StopProvisioningRequestV1::new(i).unwrap())
        }
        _ => unreachable!(),
    }
}
pub fn dispatch(request: ProvisioningMutationRequestV1) -> ProvisioningDispatchV1 {
    ProvisioningDispatchV1::new(ProvisioningDispatchInputV1 {
        request,
        source: NativeEvidenceSource::FakePve,
        preflight_event_id: id(9000),
        original_generation: 1,
        dispatch_revision: 3,
        dispatched_at: time(),
    })
    .unwrap()
}
pub fn receipt(d: &ProvisioningDispatchV1) -> ProvisioningReceiptV1 {
    use ProvisioningActionV1::*;
    let r = match d.request().plan().action() {
        ConfigurePe | ConfigureDisk => MutationReceipt::SynchronousAccepted,
        a => {
            let (worker, id) = match a {
                Clone => ("qmclone", 900),
                EnsureCapacity => ("resize", 101),
                EnsureStopped => ("qmstop", 101),
                _ => ("qmstart", 101),
            };
            MutationReceipt::Task(
                Upid::parse(format!(
                    "UPID:pve-test:00000001:00000002:{:08X}:{worker}:{id}:root@pam:",
                    a as u32 + 1
                ))
                .unwrap(),
            )
        }
    };
    ProvisioningReceiptV1::new(d.clone(), time(), r).unwrap()
}
pub struct Episode {
    pub pre: ProvisioningEvaluationContextV1,
    pub pre_evidence: ProvisioningEvidenceV1,
    pub request: ProvisioningMutationRequestV1,
    pub outcome: ProvisioningEvaluationContextV1,
    pub evidence: ProvisioningEvidenceV1,
}
pub fn chain() -> Vec<Episode> {
    chain_variant(80 * GIB, 120 * GIB, "DISK-101")
}
pub fn chain_variant(template_bytes: u64, effective_bytes: u64, serial: &str) -> Vec<Episode> {
    use ProvisioningActionV1::*;
    let source = changed(&source(), |v| {
        v["primary_disk"]["capacity_bytes"] = json!(template_bytes)
    });
    let expected = ProvisioningExpectationsV1::new(ProvisioningExpectationsInputV1 {
        vm: vm(),
        template_config_sha256: &source.template_fingerprint().unwrap(),
        template_capacity_bytes: template_bytes,
        effective_capacity_bytes: effective_bytes,
        system_serial: "SYS-101",
        disk_serial: serial,
        deployment_iso_volid: "local:iso/deployment.iso",
        driver_iso_volid: "drivers:iso/virtio.iso",
    })
    .unwrap();
    let mut owner = None;
    let mut prior = None;
    let mut current = source.clone();
    let mut running = false;
    let mut episodes = Vec::new();
    for (index, action) in [
        Clone,
        EnsureCapacity,
        ConfigurePe,
        StartPe,
        EnsureStopped,
        ConfigureDisk,
        StartDisk,
    ]
    .into_iter()
    .enumerate()
    {
        let p = ProvisioningOperationPlanV1::new(action, expected.clone());
        let b = ProvisioningBindingV1::new(
            id(1),
            id(10 + index as u32),
            id(1010 + index as u32),
            SHA,
            &p,
            0,
        )
        .unwrap();
        let pre = ProvisioningEvaluationContextV1::new(context(
            b.clone(),
            p.clone(),
            owner.clone(),
            prior.clone(),
        ))
        .unwrap();
        let mut pre_facts = facts(
            b,
            p.clone(),
            if action == Clone {
                None
            } else {
                Some(current.clone())
            },
            running,
        );
        set_source(&mut pre_facts, &source, running);
        let pre_evidence = ProvisioningEvidenceV1::new(pre_facts).unwrap();
        if action == EnsureCapacity && template_bytes == effective_bytes {
            assert_eq!(
                evaluate_provisioning_preflight(&pre, &pre_evidence, time()).reason,
                ProvisioningReasonV1::ObservedNoChange
            );
            prior = Some(
                ProvisioningStageBaselineV1::from_satisfied(&pre, &pre_evidence, time()).unwrap(),
            );
            continue;
        }
        assert_eq!(
            evaluate_provisioning_preflight(&pre, &pre_evidence, time()).decision,
            NativeDecision::Ready,
            "preflight {action:?}"
        );
        let request = request(&pre, current.clone(), running);
        let dispatch = dispatch(request.clone());
        let receipt = receipt(&dispatch);
        current = if action == Clone {
            changed(&intermediate(), |v| {
                v["primary_disk"]["capacity_bytes"] = json!(template_bytes)
            })
        } else {
            after(action, &current)
        };
        if action == EnsureCapacity {
            current = changed(&current, |v| {
                v["primary_disk"]["capacity_bytes"] = json!(effective_bytes)
            });
        }
        if action == ConfigurePe {
            current = changed(&current, |v| v["primary_disk"]["serial"] = json!(serial));
        }
        running = matches!(action, StartPe | StartDisk);
        let b = ProvisioningBindingV1::new(
            id(1),
            id(10 + index as u32),
            id(1010 + index as u32),
            SHA,
            &p,
            4,
        )
        .unwrap();
        let mut o = context(b.clone(), p.clone(), owner.clone(), prior.clone());
        o.mode = ProvisioningEvaluationModeV1::Outcome;
        o.state = controller_domain::ExecutionState::Running;
        o.dispatch = ProvisioningDispatchStateV1::Recorded {
            dispatch,
            receipt: Some(receipt.clone()),
        };
        let outcome = ProvisioningEvaluationContextV1::new(o).unwrap();
        let mut e = facts(b, p, Some(current.clone()), running);
        set_source(&mut e, &source, running);
        e.receipt = Some(receipt.clone());
        if let MutationReceipt::Task(upid) = receipt.receipt() {
            e.task = Some(read(TaskStatus::complete(upid.clone(), time())));
        }
        let evidence = ProvisioningEvidenceV1::new(e).unwrap();
        assert_eq!(
            evaluate_provisioning_outcome(&outcome, &evidence, time()).decision,
            NativeDecision::Satisfied,
            "outcome {action:?}"
        );
        if action == Clone {
            owner = Some(
                ProvisioningCloneOwnershipV1::from_satisfied_clone(&outcome, &evidence, time())
                    .unwrap(),
            );
        }
        prior =
            Some(ProvisioningStageBaselineV1::from_satisfied(&outcome, &evidence, time()).unwrap());
        episodes.push(Episode {
            pre,
            pre_evidence,
            request,
            outcome,
            evidence,
        });
    }
    episodes
}
fn set_source(f: &mut ProvisioningEvidenceInputV1, s: &ProvisioningVmConfigV1, running: bool) {
    f.source_config = Some(read(s.clone()));
    f.identities[0] = ProvisioningIdentityReadV1::new(
        s.node().clone(),
        s.vmid(),
        read(ProvisioningIdentitySnapshotV1::from_provisioning(s)),
    )
    .unwrap();
    let mut configs = vec![s.clone()];
    if let Some(Ok(t)) = f.target_config.as_ref().map(|r| &r.result) {
        configs.push(t.clone());
    }
    f.inventory = Some(read(inventory(&configs, running)));
}
