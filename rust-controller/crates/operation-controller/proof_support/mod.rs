//! Private local proof infrastructure, shared only by the test and example.
#[path = "../../../proof_support/mod.rs"]
mod local_postgres;
const LOCAL_DATABASE_NAME: &str = "native_proof";
use chrono::Utc;
use controller_domain::RunId;
use operation_controller::NativeController;
use postgres_store::{ExecutorKind, NativeWorkflowIds, PgStore, Scheduler};
use pve_port::*;
use serde_json::json;
use sqlx::{PgPool, postgres::PgPoolOptions};
use std::{sync::Arc, time::Duration};

pub struct Fixture {
    pub pool: PgPool,
    pub store: PgStore,
    pub fake: Arc<NativeFakePve>,
    _container: local_postgres::Container,
}
impl Fixture {
    pub async fn new() -> Self {
        tokio::time::timeout(local_postgres::SETUP_BOUND, Self::setup())
            .await
            .expect("local_fixture_setup_timeout")
    }
    async fn setup() -> Self {
        let (container, dsn) = local_postgres::Container::start().await;
        let pool = tokio::time::timeout(Duration::from_secs(15), async {
            loop {
                if let Ok(pool) = PgPoolOptions::new()
                    .max_connections(8)
                    .acquire_timeout(Duration::from_secs(1))
                    .connect(&dsn)
                    .await
                {
                    break pool;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        })
        .await
        .expect("local_database_timeout");
        let store = PgStore::new(pool.clone());
        store.migrate().await.expect("local_migration_failed");
        sqlx::query("INSERT INTO rust_controller.orchestration_authority(singleton_key,executor_kind,generation,change_reference) VALUES(1,'rust',1,'native-proof')").execute(&pool).await.expect("local_authority_failed");
        let fake = Arc::new(NativeFakePve::new());
        fake.insert_vm(source(), PowerState::Stopped).unwrap();
        let p = plan();
        let t = Utc::now();
        fake.set_node_status(
            NodeStatus::from_wire(p.node().clone(), json!({"uptime":123}), t).unwrap(),
        );
        fake.set_storage_status(
            StorageStatus::from_wire(
                p.node().clone(),
                p.storage().clone(),
                json!({"active":1,"enabled":1,"content":"images","avail":34359738368_u64}),
                t,
            )
            .unwrap(),
        );
        fake.set_bridges(
            BridgeInventory::from_wire(
                p.node().clone(),
                json!([{"type":"bridge","iface":"vmbr0","active":1}]),
                t,
            )
            .unwrap(),
        );
        Self {
            pool,
            store,
            fake,
            _container: container,
        }
    }
    pub fn scheduler(&self, worker: &str) -> Scheduler {
        Scheduler::new(self.store.clone(), ExecutorKind::Rust, 1, worker).unwrap()
    }
    pub fn cleanup(mut self) -> Result<(), ()> {
        let result = self._container.cleanup();
        if result.is_err() {
            eprintln!("native_fake_cleanup_unconfirmed");
        }
        result
    }
    pub fn controller(&self, worker: &str) -> NativeController {
        NativeController::new(
            self.store.clone(),
            self.scheduler(worker),
            self.fake.clone(),
        )
    }
    pub async fn enqueue(&self) -> NativeWorkflowIds {
        self.store
            .enqueue_native_vm(RunId::new(), &plan())
            .await
            .unwrap()
    }
    pub async fn summary(&self) -> serde_json::Value {
        let (operations,satisfied,reservations,attempts,dispatches):(i64,i64,i64,i64,i64) = sqlx::query_as("SELECT (SELECT count(*) FROM rust_controller.operations),(SELECT count(*) FROM rust_controller.operations WHERE state='satisfied'),(SELECT count(*) FROM rust_controller.native_vm_reservations),(SELECT count(*) FROM rust_controller.attempts),(SELECT count(*) FROM rust_controller.native_dispatches)").fetch_one(&self.pool).await.unwrap();
        assert_eq!(attempts, 3);
        assert_eq!(dispatches, 3);
        let requests = self.fake.recorded_requests();
        assert!(matches!(
            requests.as_slice(),
            [
                NativeMutationRequest::Clone(_),
                NativeMutationRequest::Configure(_),
                NativeMutationRequest::Start(_)
            ]
        ));
        let p = plan();
        let c = self
            .fake
            .native_vm_config(p.node(), p.target_vmid())
            .await
            .unwrap();
        assert_eq!(c.uuid(), p.uuid());
        assert_eq!(c.name(), p.name());
        assert_eq!(c.mac(), p.mac());
        assert_eq!(c.bridge(), p.bridge());
        assert_eq!(c.cores(), p.cores());
        assert_eq!(c.memory_mib(), p.memory_mib());
        assert!(c.agent_enabled());
        assert!(c.boots_scsi0());
        assert!(!c.is_template() && !c.locked());
        assert_eq!(c.boot_disk().storage(), p.storage());
        assert_eq!(c.boot_disk().volume(), "vm-9010-disk-0");
        assert_eq!(self.fake.cluster_vms().await.unwrap().vms().len(), 2);
        let original = source();
        let current = self
            .fake
            .native_vm_config(p.node(), p.source_vmid())
            .await
            .unwrap();
        let mut expected = serde_json::to_value(original).unwrap();
        expected["observed_at"] = json!(current.observed_at());
        assert_eq!(serde_json::to_value(current).unwrap(), expected);
        let power = self
            .fake
            .vm_status(p.node(), p.target_vmid())
            .await
            .unwrap();
        json!({"transport":"in_memory_fake","native_operations":operations,"satisfied_operations":satisfied,"mutation_requests":requests.len(),"reservations":reservations,"final_power":power.power(),"os_readiness_proven":false})
    }
    pub async fn assert_success(&self) {
        assert_eq!(
            self.summary().await,
            json!({"transport":"in_memory_fake","native_operations":3,"satisfied_operations":3,"mutation_requests":3,"reservations":1,"final_power":"running","os_readiness_proven":false})
        );
    }
}
pub fn plan() -> NativeVmPlan {
    serde_json::from_value(json!({"contract_version":1,"cluster_key":"fake-local","node":"pve-test","source_vmid":9000,"target_vmid":9010,"name":"native-proof-9010","storage":"local-lvm","bridge":"vmbr0","uuid":"3f2504e0-4f89-41d3-9a0c-0305e82c3301","mac":"02:00:00:00:90:10","cores":2,"memory_mib":2048,"minimum_storage_bytes":17179869184_u64})).unwrap()
}
pub fn source() -> NativeVmConfig {
    let p = plan();
    NativeVmConfig::from_wire(p.node().clone(),p.source_vmid(),json!({"digest":"source-digest","name":"template","cores":1,"memory":512,"scsi0":"local-lvm:vm-9000-disk-0","smbios1":"uuid=4f2504e0-4f89-41d3-9a0c-0305e82c3301","net0":"virtio=02:00:00:00:00:01,bridge=vmbr0","agent":0,"boot":"order=scsi0","template":1}),Utc::now()).unwrap()
}
