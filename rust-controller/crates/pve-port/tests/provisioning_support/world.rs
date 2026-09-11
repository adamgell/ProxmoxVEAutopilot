//! Physical fake-world fixture; no queued expected observation scripts.
use super::*;
use chrono::{DateTime, Utc};
use controller_domain::{ExecutionState, OperationId};
use serde_json::json;

pub struct World {
    pub fake: NativeFakePve,
    pub expected: ProvisioningExpectationsV1,
    pub owner: Option<ProvisioningCloneOwnershipV1>,
    pub prior: Option<ProvisioningStageBaselineV1>,
}
impl World {
    pub fn new(template: u64, effective: u64) -> Self {
        let fake = NativeFakePve::new();
        let source = changed(&source(), |v| {
            v["primary_disk"]["capacity_bytes"] = json!(template)
        });
        let expected = ProvisioningExpectationsV1::new(ProvisioningExpectationsInputV1 {
            vm: vm(),
            template_config_sha256: &source.template_fingerprint().unwrap(),
            template_capacity_bytes: template,
            effective_capacity_bytes: effective,
            system_serial: "SYS-101",
            disk_serial: "DISK-101",
            deployment_iso_volid: "local:iso/deployment.iso",
            driver_iso_volid: "drivers:iso/virtio.iso",
        })
        .unwrap();
        fake.insert_provisioning_vm(source, PowerState::Stopped)
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
        Self {
            fake,
            expected,
            owner: None,
            prior: None,
        }
    }
    pub fn context(&self, action: ProvisioningActionV1) -> ProvisioningEvaluationContextV1 {
        let p = ProvisioningOperationPlanV1::new(action, self.expected.clone());
        let b = ProvisioningBindingV1::new(
            id(1),
            id(10 + action as u32),
            id(1010 + action as u32),
            SHA,
            &p,
            0,
        )
        .unwrap();
        let mut c = context(b, p, self.owner.clone(), self.prior.clone());
        c.mutation_deadline = Utc::now() + chrono::Duration::seconds(60);
        ProvisioningEvaluationContextV1::new(c).unwrap()
    }
    pub async fn collect(&self, c: &ProvisioningEvaluationContextV1) -> ProvisioningEvidenceV1 {
        fn r<T>(value: Result<T, PveReadError>) -> NativeRead<T> {
            NativeRead::new(Utc::now(), value)
        }
        let v = vm();
        let inventory = self.fake.cluster_vms().await.unwrap();
        let mut identities = vec![];
        for entry in inventory.vms().values() {
            identities.push(
                ProvisioningIdentityReadV1::new(
                    entry.node().clone(),
                    entry.vmid(),
                    r(self
                        .fake
                        .provisioning_identity(entry.node(), entry.vmid())
                        .await),
                )
                .unwrap(),
            );
        }
        let mut media = vec![];
        for storage in ["local", "drivers"] {
            media.push(r(self
                .fake
                .provisioning_media(&node(), &StorageName::parse(storage).unwrap())
                .await));
        }
        let receipt = match &c.facts().dispatch {
            ProvisioningDispatchStateV1::Recorded { receipt, .. } => receipt.clone(),
            _ => None,
        };
        let task = if let Some(MutationReceipt::Task(upid)) =
            receipt.as_ref().map(ProvisioningReceiptV1::receipt)
        {
            Some(r(self.fake.task_status(&node(), upid).await))
        } else {
            None
        };
        let f = ProvisioningEvidenceInputV1 {
            binding: c.facts().binding.clone(),
            plan: c.facts().plan.clone(),
            source: NativeEvidenceSource::FakePve,
            node: Some(r(self.fake.node_status(&node()).await)),
            storage: Some(r(self.fake.storage_status(&node(), v.storage()).await)),
            bridges: Some(r(self.fake.bridges(&node()).await)),
            inventory: Some(r(Ok(inventory))),
            inventory_coverage: ProvisioningCoverageV1::Complete,
            identities,
            source_config: Some(r(self
                .fake
                .provisioning_vm_config(&node(), v.source_vmid())
                .await)),
            source_power: Some(r(self.fake.vm_status(&node(), v.source_vmid()).await)),
            target_config: Some(r(self
                .fake
                .provisioning_vm_config(&node(), v.target_vmid())
                .await)),
            target_power: Some(r(self.fake.vm_status(&node(), v.target_vmid()).await)),
            media,
            qga: None,
            task,
            receipt,
            collected_at: Utc::now(),
        };
        ProvisioningEvidenceV1::new(f).unwrap()
    }
    pub async fn prepare(
        &self,
        action: ProvisioningActionV1,
    ) -> (
        ProvisioningEvaluationContextV1,
        ProvisioningMutationRequestV1,
    ) {
        let c = self.context(action);
        let e = self.collect(&c).await;
        assert_eq!(
            evaluate_provisioning_preflight(&c, &e, Utc::now()).decision,
            NativeDecision::Ready,
            "{action:?}"
        );
        let v = vm();
        let target = if action == ProvisioningActionV1::Clone {
            v.source_vmid()
        } else {
            v.target_vmid()
        };
        let b = ProvisioningBeforeStateV1::new(
            self.fake
                .provisioning_vm_config(&node(), target)
                .await
                .unwrap(),
            self.fake.vm_status(&node(), target).await.unwrap(),
        )
        .unwrap();
        let r = if action == ProvisioningActionV1::Clone {
            ProvisioningMutationRequestV1::Clone(
                CloneProvisioningRequestV1::new(
                    c.facts().binding.clone(),
                    c.facts().plan.clone(),
                    clone_request(),
                    b,
                    Utc::now(),
                    30,
                )
                .unwrap(),
            )
        } else {
            let i = ProvisioningOwnedRequestInputV1 {
                binding: c.facts().binding.clone(),
                plan: c.facts().plan.clone(),
                ownership: self.owner.as_ref().unwrap(),
                predecessor: self.prior.as_ref().unwrap(),
                expected_before: b,
                as_of: Utc::now(),
                freshness_seconds: 30,
            };
            match action {
                ProvisioningActionV1::EnsureCapacity => {
                    ProvisioningMutationRequestV1::GrowDisk(GrowDiskRequestV1::new(i).unwrap())
                }
                ProvisioningActionV1::ConfigurePe | ProvisioningActionV1::ConfigureDisk => {
                    ProvisioningMutationRequestV1::Configure(
                        ConfigureProvisioningRequestV1::new(i).unwrap(),
                    )
                }
                ProvisioningActionV1::StartPe | ProvisioningActionV1::StartDisk => {
                    ProvisioningMutationRequestV1::Start(
                        StartProvisioningRequestV1::new(i).unwrap(),
                    )
                }
                ProvisioningActionV1::EnsureStopped => {
                    ProvisioningMutationRequestV1::Stop(StopProvisioningRequestV1::new(i).unwrap())
                }
                _ => unreachable!(),
            }
        };
        (c, r)
    }
    pub async fn accept_proof(
        &mut self,
        c: ProvisioningEvaluationContextV1,
        r: ProvisioningMutationRequestV1,
        receipt: MutationReceipt,
        dispatched_at: DateTime<Utc>,
    ) -> ProvisioningEvaluationContextV1 {
        let d = ProvisioningDispatchV1::new(ProvisioningDispatchInputV1 {
            request: r,
            source: NativeEvidenceSource::FakePve,
            preflight_event_id: id(9000),
            original_generation: 1,
            dispatch_revision: 3,
            dispatched_at,
        })
        .unwrap();
        let receipt = ProvisioningReceiptV1::new(d.clone(), Utc::now(), receipt).unwrap();
        let mut o = c.facts().clone();
        o.mode = ProvisioningEvaluationModeV1::Outcome;
        o.state = ExecutionState::Running;
        o.dispatch = ProvisioningDispatchStateV1::Recorded {
            dispatch: d,
            receipt: Some(receipt),
        };
        let o = ProvisioningEvaluationContextV1::new(o).unwrap();
        let evidence = self.collect(&o).await;
        assert_eq!(
            evaluate_provisioning_outcome(&o, &evidence, Utc::now()).decision,
            NativeDecision::Satisfied,
            "{:?}",
            o.facts().plan.action()
        );
        if o.facts().plan.action() == ProvisioningActionV1::Clone {
            self.owner = Some(
                ProvisioningCloneOwnershipV1::from_satisfied_clone(&o, &evidence, Utc::now())
                    .unwrap(),
            );
        }
        self.prior =
            Some(ProvisioningStageBaselineV1::from_satisfied(&o, &evidence, Utc::now()).unwrap());
        o
    }
    pub async fn step(
        &mut self,
        action: ProvisioningActionV1,
    ) -> Option<ProvisioningMutationRequestV1> {
        if action == ProvisioningActionV1::EnsureCapacity
            && self.expected.template_capacity_bytes() == self.expected.effective_capacity_bytes()
        {
            let c = self.context(action);
            let e = self.collect(&c).await;
            assert_eq!(
                evaluate_provisioning_preflight(&c, &e, Utc::now()).reason,
                ProvisioningReasonV1::ObservedNoChange
            );
            self.prior =
                Some(ProvisioningStageBaselineV1::from_satisfied(&c, &e, Utc::now()).unwrap());
            return None;
        }
        let (c, r) = self.prepare(action).await;
        let sent = Utc::now();
        let receipt = self.fake.submit_provisioning(&r).await.unwrap();
        self.accept_proof(c, r.clone(), receipt, sent).await;
        Some(r)
    }
    pub async fn through(&mut self, stop_before: ProvisioningActionV1) {
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
            if action == stop_before {
                break;
            }
            self.step(action).await;
        }
    }
}
pub fn different_operation(
    r: &ProvisioningMutationRequestV1,
    op: u32,
) -> ProvisioningMutationRequestV1 {
    let mut v = serde_json::to_value(r).unwrap();
    v["request"]["binding"]["operation_id"] = json!(id::<OperationId>(op));
    if r.plan().action() == ProvisioningActionV1::Clone {
        v["request"]["clone"]["operation_id"] = json!(id::<OperationId>(op));
    }
    serde_json::from_value(v).unwrap()
}
