//! An owned physical world using only real durable activation.
use crate::osdeploy_support;
use chrono::Utc;
use postgres_store::OsDeployWorkflowIds;
use pve_port::*;
use serde_json::json;
use std::sync::Arc;

pub struct Scenario {
    pub db: osdeploy_support::Fixture,
    pub ids: OsDeployWorkflowIds,
    pub fake: Arc<NativeFakePve>,
}
pub struct Ready {
    pub grant: postgres_store::LeaseGrant,
    pub event: controller_domain::EventId,
    pub revision: i64,
    pub request: ProvisioningMutationRequestV1,
}
impl Scenario {
    pub async fn reconciliation_observation(
        &self,
        grant: &postgres_store::LeaseGrant,
    ) -> (controller_domain::EventId, i64) {
        let snap = self
            .db
            .store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap();
        let context = self
            .db
            .store
            .load_osdeploy_pve_context(
                grant.operation_id(),
                snap.revision(),
                ProvisioningEvaluationModeV1::Reconciliation,
            )
            .await
            .unwrap();
        let evidence = self.collect(&context).await;
        let event = self
            .db
            .store
            .record_osdeploy_pve_evidence(
                grant.operation_id(),
                grant.attempt_id(),
                snap.revision(),
                &evidence,
            )
            .await
            .unwrap();
        let revision = self
            .db
            .store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap()
            .revision();
        (event, revision)
    }
    pub async fn wait_until(&self, at: chrono::DateTime<Utc>) {
        tokio::time::timeout(std::time::Duration::from_secs(35), sqlx::query("SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))").bind(at).execute(&self.db.pool)).await.expect("owned DB-time wait exceeded 35 seconds").unwrap();
    }
    pub async fn finish_stage(
        &self,
        stage: osdeploy_adapter::OsDeployStage,
    ) -> controller_domain::ExecutionState {
        Box::pin(async move {
        let stage_snapshot = self.db.store.load_osdeploy_operation(self.ids.operation(stage)).await.unwrap();
        let expected = stage_snapshot.plan().pve().unwrap().expected();
        let mut grant = if stage == osdeploy_adapter::OsDeployStage::DiskCapacity
            && expected.effective_capacity_bytes() == expected.template_capacity_bytes()
        {
            self.started(stage).await
        } else {
            let r = self.ready(stage).await;
            let scheduler = self.db.scheduler();
            let (permit, capture) = scheduler.begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request).await.unwrap();
            let receipt = permit.submit_fake_once(self.fake.as_ref()).await.unwrap();
            scheduler.record_osdeploy_pve_receipt(&capture, &receipt).await.unwrap();
            r.grant
        };
        for _ in 0..8 {
            let state = self.observe_and_decide(&grant).await;
            if state != controller_domain::ExecutionState::Waiting { return state; }
            let snapshot = self.db.store.load_osdeploy_operation(grant.operation_id()).await.unwrap();
            tokio::time::timeout(std::time::Duration::from_secs(4),sqlx::query("SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))").bind(snapshot.next_check_at().unwrap()).execute(&self.db.pool)).await.unwrap().unwrap();
            grant = self.db.scheduler().resume_osdeploy_bound(grant.operation_id(),grant.attempt_id(),snapshot.revision(),self.ids.workflow_sha256(),1).await.unwrap().unwrap();
            self.db.scheduler().start_osdeploy_bound(&grant,self.ids.workflow_sha256()).await.unwrap();
        }
        panic!("owned stage remained Waiting after eight observations on its original dispatch");
        }).await
    }

    pub async fn observe_and_decide(
        &self,
        grant: &postgres_store::LeaseGrant,
    ) -> controller_domain::ExecutionState {
        let (event, revision) = self.observation(grant).await;
        match self
            .db
            .scheduler()
            .decide_osdeploy_pve(grant, revision, event)
            .await
            .unwrap()
        {
            postgres_store::OsDeployProgress::Decided(state) => state,
            postgres_store::OsDeployProgress::Waiting => controller_domain::ExecutionState::Waiting,
            postgres_store::OsDeployProgress::Idle => panic!("actual evaluator unexpectedly idle"),
        }
    }

    pub async fn observation(
        &self,
        grant: &postgres_store::LeaseGrant,
    ) -> (controller_domain::EventId, i64) {
        let snap = self
            .db
            .store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap();
        let mode = if snap.dispatch().is_some() {
            ProvisioningEvaluationModeV1::Outcome
        } else {
            ProvisioningEvaluationModeV1::Preflight
        };
        let context = self
            .db
            .store
            .load_osdeploy_pve_context(grant.operation_id(), snap.revision(), mode)
            .await
            .unwrap();
        let evidence = self.collect(&context).await;
        let event = self
            .db
            .store
            .record_osdeploy_pve_evidence(
                grant.operation_id(),
                grant.attempt_id(),
                snap.revision(),
                &evidence,
            )
            .await
            .unwrap();
        let current = self
            .db
            .store
            .load_osdeploy_operation(grant.operation_id())
            .await
            .unwrap();
        (event, current.revision())
    }

    pub async fn ready(&self, stage: osdeploy_adapter::OsDeployStage) -> Ready {
        Box::pin(async move {
            let grant = self.started(stage).await;
            let snapshot = self
                .db
                .store
                .load_osdeploy_operation(grant.operation_id())
                .await
                .unwrap();
            let context = self
                .db
                .store
                .load_osdeploy_pve_context(
                    grant.operation_id(),
                    snapshot.revision(),
                    ProvisioningEvaluationModeV1::Preflight,
                )
                .await
                .unwrap();
            let evidence = self.collect(&context).await;
            let event = self
                .db
                .store
                .record_osdeploy_pve_evidence(
                    grant.operation_id(),
                    grant.attempt_id(),
                    snapshot.revision(),
                    &evidence,
                )
                .await
                .unwrap();
            let current = self
                .db
                .store
                .load_osdeploy_operation(grant.operation_id())
                .await
                .unwrap();
            let request = self
                .db
                .store
                .prepare_osdeploy_pve_request(grant.operation_id(), current.revision(), event)
                .await
                .unwrap();
            Ready {
                grant,
                event,
                revision: current.revision(),
                request,
            }
        })
        .await
    }
    pub async fn new(mutation_seconds: u32, grow: bool) -> Self {
        Self::with_freshness(mutation_seconds, grow, mutation_seconds.min(30)).await
    }
    pub async fn with_freshness(mutation_seconds: u32, grow: bool, freshness_seconds: u32) -> Self {
        Self::with_origin(mutation_seconds, grow, freshness_seconds, false, None).await
    }
    #[cfg(feature = "fixture-ipc")]
    pub async fn fixture_owned() -> Self {
        Self::with_origin(300, true, 30, true, None).await
    }
    #[cfg(feature = "fixture-ipc")]
    pub async fn fixture_delivery(sink: sqlx::types::Uuid) -> Self {
        Self::with_origin(300, true, 30, true, Some(sink)).await
    }
    async fn with_origin(
        mutation_seconds: u32,
        grow: bool,
        freshness_seconds: u32,
        fixture_origin: bool,
        sink: Option<sqlx::types::Uuid>,
    ) -> Self {
        let db = osdeploy_support::Fixture::new().await;
        let node = NodeName::parse("node-a").unwrap();
        let source = ProvisioningVmConfigV1::from_wire(
            node.clone(),
            Vmid::new(900).unwrap(),
            NativeEvidenceSource::FakePve,
            json!({"node":"node-a","vmid":900,
            "digest":"owned-template-1","name":"blank-template","cores":2,"memory":2048,
            "scsi0":"disk-store:vm-900-disk-0,size=80G",
            "smbios1":"uuid=33333333-3333-4333-8333-333333333390",
            "net0":"virtio=02:00:00:00:09:00,bridge=vmbr0,firewall=0",
            "bios":"seabios","cpu":"host","balloon":0,"agent":"enabled=0,type=virtio",
            "boot":"order=scsi0","template":1}),
            Utc::now(),
        )
        .unwrap();
        let hash = source.template_fingerprint().unwrap();
        let plan = osdeploy_support::altered(|v| {
            v["template_config_sha256"] = json!(hash);
            v["policy"]["mutation_seconds"] = json!(mutation_seconds);
            v["policy"]["evidence_freshness_seconds"] = json!(freshness_seconds);
            v["disk"]["requested_gib"] = json!(if grow { 120 } else { 80 });
            v["disk"]["effective_bytes"] = json!(if grow {
                128849018880_u64
            } else {
                85899345920_u64
            });
            v["disk"]["growth_required"] = json!(grow);
        });
        let fake = Arc::new(NativeFakePve::new());
        fake.insert_provisioning_vm(source, PowerState::Stopped)
            .unwrap();
        fake.set_node_status(
            NodeStatus::from_wire(node.clone(), json!({"uptime":100}), Utc::now()).unwrap(),
        );
        fake.set_storage_status(
            StorageStatus::from_wire(
                node.clone(),
                StorageName::parse("disk-store").unwrap(),
                json!({"active":1,"enabled":1,"content":"images","avail":999999999999_u64}),
                Utc::now(),
            )
            .unwrap(),
        );
        fake.set_bridges(
            BridgeInventory::from_wire(
                node.clone(),
                json!([{"type":"bridge","iface":"vmbr0","active":1}]),
                Utc::now(),
            )
            .unwrap(),
        );
        for (storage, volid) in [
            ("media-store", "media-store:iso/deploy.iso"),
            ("drivers", "drivers:iso/virtio.iso"),
        ] {
            fake.set_provisioning_media(
                ProvisioningMediaInventoryV1::new(
                    node.clone(),
                    StorageName::parse(storage).unwrap(),
                    vec![volid.to_owned()],
                    ProvisioningCoverageV1::Complete,
                    Utc::now(),
                )
                .unwrap(),
            );
        }
        #[cfg(feature = "fixture-ipc")]
        let owned = if fixture_origin {
            let created = if let Some(sink) = sink {
                db.store
                    .create_fixture_osdeploy_with_delivery(sqlx::types::Uuid::now_v7(), &plan, sink)
                    .await
                    .unwrap()
            } else {
                db.store
                    .create_fixture_osdeploy(sqlx::types::Uuid::now_v7(), &plan)
                    .await
                    .unwrap()
            };
            Some(created.ids().clone())
        } else {
            None
        };
        #[cfg(not(feature = "fixture-ipc"))]
        let owned = {
            assert!(!fixture_origin && sink.is_none());
            None
        };
        let ids = if let Some(ids) = owned {
            ids
        } else {
            db.store
                .enqueue_osdeploy(controller_domain::RunId::new(), &plan)
                .await
                .unwrap()
        };
        Self { db, ids, fake }
    }
    pub async fn started(
        &self,
        stage: osdeploy_adapter::OsDeployStage,
    ) -> postgres_store::LeaseGrant {
        let g = self
            .db
            .scheduler()
            .claim_osdeploy_bound(self.ids.operation(stage), self.ids.workflow_sha256(), 1)
            .await
            .unwrap()
            .unwrap();
        self.db
            .scheduler()
            .start_osdeploy_bound(&g, self.ids.workflow_sha256())
            .await
            .unwrap();
        g
    }
    pub async fn collect(
        &self,
        context: &ProvisioningEvaluationContextV1,
    ) -> ProvisioningEvidenceV1 {
        tokio::time::timeout(std::time::Duration::from_secs(6), async {
            fn r<T>(value: Result<T, PveReadError>) -> NativeRead<T> {
                NativeRead::new(Utc::now(), value)
            }
            let v = context.facts().plan.expected().vm();
            let node = v.node();
            let inventory = self.fake.cluster_vms().await;
            let mut identities = vec![];
            let coverage = if let Ok(inventory) = &inventory {
                for entry in inventory.vms().values().take(32) {
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
                if inventory.vms().len() <= 32 {
                    ProvisioningCoverageV1::Complete
                } else {
                    ProvisioningCoverageV1::Partial
                }
            } else {
                ProvisioningCoverageV1::Partial
            };
            let mut media = vec![];
            let expected = context.facts().plan.expected();
            let storages: std::collections::BTreeSet<_> =
                [expected.deployment_iso_volid(), expected.driver_iso_volid()]
                    .into_iter()
                    .map(|v| v.split_once(':').unwrap().0)
                    .collect();
            for storage in storages {
                media.push(r(self
                    .fake
                    .provisioning_media(node, &StorageName::parse(storage).unwrap())
                    .await));
            }
            let receipt = match &context.facts().dispatch {
                ProvisioningDispatchStateV1::Recorded { receipt, .. } => receipt.clone(),
                _ => None,
            };
            let task = if let Some(MutationReceipt::Task(upid)) =
                receipt.as_ref().map(ProvisioningReceiptV1::receipt)
            {
                Some(r(self.fake.task_status(node, upid).await))
            } else {
                None
            };
            ProvisioningEvidenceV1::new(ProvisioningEvidenceInputV1 {
                binding: context.facts().binding.clone(),
                plan: context.facts().plan.clone(),
                source: NativeEvidenceSource::FakePve,
                node: Some(r(self.fake.node_status(node).await)),
                storage: Some(r(self.fake.storage_status(node, v.storage()).await)),
                bridges: Some(r(self.fake.bridges(node).await)),
                inventory: Some(r(inventory)),
                inventory_coverage: coverage,
                identities,
                source_config: Some(r(self
                    .fake
                    .provisioning_vm_config(node, v.source_vmid())
                    .await)),
                source_power: Some(r(self.fake.vm_status(node, v.source_vmid()).await)),
                target_config: Some(r(self
                    .fake
                    .provisioning_vm_config(node, v.target_vmid())
                    .await)),
                target_power: Some(r(self.fake.vm_status(node, v.target_vmid()).await)),
                media,
                qga: None,
                task,
                receipt,
                collected_at: Utc::now(),
            })
            .unwrap()
        })
        .await
        .expect("owned_collection_timeout")
    }
}
