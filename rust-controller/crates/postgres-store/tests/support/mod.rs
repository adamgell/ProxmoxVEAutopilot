use chrono::Utc;
use controller_domain::{EventId, RunId};
use postgres_store::{ExecutorKind, LeaseGrant, NativeWorkflowIds, PgStore, Scheduler};
use pve_port::*;
use serde_json::json;
use sqlx::{PgPool, postgres::PgPoolOptions};
use std::{
    process::{Command, Stdio},
    time::Duration,
};

pub struct Container {
    id: String,
}
impl Drop for Container {
    fn drop(&mut self) {
        let _ = Command::new("docker")
            .args(["rm", "--force", &self.id])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
}
pub struct Fixture {
    pub pool: PgPool,
    pub store: PgStore,
    pub other: PgStore,
    _container: Container,
}
impl Fixture {
    pub async fn satisfied_clone(&self) -> (NativeWorkflowIds, CloneRequest) {
        let (ids, grant, request, event, revision) = self.ready().await;
        let scheduler = self.scheduler();
        let permit = scheduler
            .begin_native_dispatch(
                &grant,
                revision,
                event,
                &request.request_digest(),
                request.request_marker(),
            )
            .await
            .unwrap();
        scheduler
            .record_native_receipt(&permit, &MutationReceipt::Task(clone_upid()))
            .await
            .unwrap();
        let snap = self
            .store
            .load_native_operation(ids.clone_id())
            .await
            .unwrap();
        let facts = clone_outcome(&snap, &request, TaskState::CompleteSuccess);
        let event = self
            .store
            .record_native_evidence(ids.clone_id(), grant.attempt_id(), snap.revision(), &facts)
            .await
            .unwrap();
        assert_eq!(
            scheduler
                .decide_native(&grant, event, snap.revision() + 1)
                .await
                .unwrap(),
            controller_domain::ExecutionState::Satisfied
        );
        (ids, request)
    }
    pub async fn start_step(&self, operation: controller_domain::OperationId) -> LeaseGrant {
        let plan = self.store.load_native_operation(operation).await.unwrap();
        let hash = digest(plan.plan());
        let scheduler = self.scheduler();
        let grant = scheduler
            .claim_native_bound(operation, &hash, 1)
            .await
            .unwrap()
            .unwrap();
        scheduler.start_native_bound(&grant, &hash).await.unwrap();
        grant
    }
    pub async fn expire(&self, operation: controller_domain::OperationId) {
        sqlx::query("UPDATE rust_controller.worker_leases SET acquired_at=clock_timestamp()-interval '3 seconds',heartbeat_at=clock_timestamp()-interval '2 seconds',lease_expires_at=clock_timestamp()-interval '1 second' WHERE operation_id=$1").bind(operation.as_uuid()).execute(&self.pool).await.unwrap();
    }
    pub async fn new() -> Self {
        Self::database(false).await
    }
    pub async fn foundation() -> Self {
        Self::database(true).await
    }
    async fn database(foundation_only: bool) -> Self {
        let host = Command::new("docker")
            .args([
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
            ])
            .output()
            .unwrap();
        assert!(host.status.success());
        assert!(
            String::from_utf8(host.stdout)
                .unwrap()
                .trim()
                .starts_with("unix:///")
        );
        assert!(std::env::var_os("DOCKER_HOST").is_none());
        let output = Command::new("docker")
            .args([
                "run",
                "--pull=never",
                "--detach",
                "--env",
                "POSTGRES_PASSWORD=postgres",
                "--env",
                "POSTGRES_DB=native_test",
                "--publish",
                "127.0.0.1::5432",
                "postgres:16-alpine",
            ])
            .output()
            .unwrap();
        assert!(output.status.success());
        let container = Container {
            id: String::from_utf8(output.stdout).unwrap().trim().to_owned(),
        };
        assert!(container.id.len() == 64 && container.id.bytes().all(|b| b.is_ascii_hexdigit()));
        let output=Command::new("docker").args(["inspect","--format","{{(index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostIp}}:{{(index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostPort}}",&container.id]).output().unwrap();
        assert!(output.status.success());
        let address = String::from_utf8(output.stdout).unwrap();
        let (host, port) = address.trim().split_once(':').unwrap();
        assert_eq!(host, "127.0.0.1");
        let port: u16 = port.parse().unwrap();
        let dsn = format!("postgresql://postgres:postgres@127.0.0.1:{port}/native_test");
        let mut connected = None;
        for _ in 0..60 {
            if let Ok(pool) = PgPoolOptions::new().max_connections(6).connect(&dsn).await {
                connected = Some(pool);
                break;
            }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
        let pool = connected.expect("owned PostgreSQL ready in 15 seconds");
        let other_pool = PgPoolOptions::new()
            .max_connections(3)
            .connect(&dsn)
            .await
            .unwrap();
        let store = PgStore::new(pool.clone());
        if foundation_only {
            sqlx::raw_sql(include_str!("../../migrations/0001_foundation.sql"))
                .execute(&pool)
                .await
                .unwrap();
            sqlx::raw_sql(include_str!("../../migrations/0002_scheduler.sql"))
                .execute(&pool)
                .await
                .unwrap();
        } else {
            store.migrate().await.unwrap();
        }
        sqlx::query("INSERT INTO rust_controller.orchestration_authority(singleton_key,executor_kind,generation,change_reference) VALUES(1,'rust',1,'native-test')").execute(&pool).await.unwrap();
        Self {
            pool,
            store,
            other: PgStore::new(other_pool),
            _container: container,
        }
    }
    pub fn scheduler(&self) -> Scheduler {
        Scheduler::new(self.store.clone(), ExecutorKind::Rust, 1, "native-worker").unwrap()
    }
    pub fn other_scheduler(&self) -> Scheduler {
        Scheduler::new(self.other.clone(), ExecutorKind::Rust, 1, "native-worker").unwrap()
    }
    pub async fn counts(&self) -> Counts {
        let r: (i64,i64,i64,i64,i64,i64,i64,i64,i64)=sqlx::query_as("SELECT (SELECT count(*) FROM rust_controller.operations),(SELECT count(*) FROM rust_controller.commands),(SELECT count(*) FROM rust_controller.native_vm_reservations),(SELECT count(*) FROM rust_controller.native_operation_plans),(SELECT count(*) FROM rust_controller.native_dispatches),(SELECT count(*) FROM rust_controller.native_receipts),(SELECT count(*) FROM rust_controller.native_decisions),(SELECT count(*) FROM rust_controller.journal_events),(SELECT count(*) FROM rust_controller.outbox)").fetch_one(&self.pool).await.unwrap();
        Counts {
            operations: r.0,
            commands: r.1,
            reservations: r.2,
            plans: r.3,
            dispatches: r.4,
            receipts: r.5,
            decisions: r.6,
            events: r.7,
            outbox: r.8,
        }
    }
    pub async fn ready(&self) -> (NativeWorkflowIds, LeaseGrant, CloneRequest, EventId, i64) {
        let ids = self
            .store
            .enqueue_native_vm(RunId::new(), &vm())
            .await
            .unwrap();
        let plan = NativeOperationPlan::new(NativeStep::Clone, vm());
        let scheduler = self.scheduler();
        let grant = scheduler
            .claim_native_bound(ids.clone_id(), &digest(&plan), 1)
            .await
            .unwrap()
            .unwrap();
        scheduler
            .start_native_bound(&grant, &digest(&plan))
            .await
            .unwrap();
        let revision = self
            .store
            .load_native_operation(ids.clone_id())
            .await
            .unwrap()
            .revision();
        let evidence = preflight(ids.run_id(), &grant, revision);
        let event = self
            .store
            .record_native_evidence(ids.clone_id(), grant.attempt_id(), revision, &evidence)
            .await
            .unwrap();
        let request = CloneRequest::new(vm(), ids.clone_id());
        (ids, grant, request, event, revision + 1)
    }
}

pub fn clone_outcome(
    snapshot: &postgres_store::NativeOperationSnapshot,
    request: &CloneRequest,
    state: TaskState,
) -> NativeEvidence {
    let t = Utc::now();
    let vm = vm();
    let dispatch = snapshot.dispatch().unwrap();
    let mut input = preflight_with_binding(
        snapshot.run_id(),
        snapshot.operation_id(),
        dispatch.attempt_id(),
        snapshot.revision(),
    )
    .facts()
    .clone();
    let mut target = serde_json::to_value(
        input
            .source_config
            .as_ref()
            .unwrap()
            .result
            .as_ref()
            .unwrap(),
    )
    .unwrap();
    target["vmid"] = json!(9010);
    target["name"] = json!("native-proof-9010");
    target["template"] = json!(false);
    target["uuid"] = json!("5f2504e0-4f89-41d3-9a0c-0305e82c3301");
    target["mac"] = json!("02:00:00:00:00:02");
    target["boot_disk"] = json!({"storage":"local-lvm","volume":"vm-9010-disk-0"});
    target["fake_clone_provenance"] = json!({"operation_id":request.operation_id(),"request_marker":request.request_marker(),"source_vmid":9000,"target_vmid":9010,"request_digest":request.request_digest()});
    let target: NativeVmConfig = serde_json::from_value(target).unwrap();
    input.identities.push(NativeIdentityRead {
        node: vm.node().clone(),
        vmid: vm.target_vmid(),
        read: NativeRead::new(t, Ok(target.clone())),
    });
    input.target_config = Some(NativeRead::new(t, Ok(target)));
    input.target_power = Some(NativeRead::new(
        t,
        VmPowerStatus::from_wire(
            vm.node().clone(),
            vm.target_vmid(),
            json!({"vmid":9010,"status":"stopped","locked":0}),
            t,
        ),
    ));
    input.inventory = Some(NativeRead::new(
        t,
        ClusterVmInventory::from_wire(
            json!([
        {"vmid":9000,"node":"pve-test","name":"template","type":"qemu","template":1,"status":"stopped"},
        {"vmid":9010,"node":"pve-test","name":"native-proof-9010","type":"qemu","template":0,"status":"stopped"}]),
            t,
        ),
    ));
    input.receipt = snapshot.receipt().cloned();
    input.task = Some(NativeRead::new(
        t,
        Ok(TaskStatus::new(clone_upid(), state, t)),
    ));
    input.collected_at = Utc::now();
    input.evaluated_at = input.collected_at;
    NativeEvidence::new(input).unwrap()
}
pub fn clone_upid() -> Upid {
    Upid::parse("UPID:pve-test:00000001:00000002:00000003:qmclone:9000:proof@pve:").unwrap()
}
pub fn owned_evidence(
    snapshot: &postgres_store::NativeOperationSnapshot,
    clone: &postgres_store::NativeOperationSnapshot,
    final_fields: bool,
    running: bool,
) -> NativeEvidence {
    let mut input = clone.evidence().unwrap().1.facts().clone();
    let t = Utc::now();
    let mut target = serde_json::to_value(
        input
            .target_config
            .as_ref()
            .unwrap()
            .result
            .as_ref()
            .unwrap(),
    )
    .unwrap();
    target["observed_at"] = json!(t);
    if final_fields {
        target["uuid"] = json!(vm().uuid());
        target["mac"] = json!(vm().mac());
        target["cores"] = json!(vm().cores());
        target["memory_mib"] = json!(vm().memory_mib());
        target["agent_enabled"] = json!(true);
        target["boots_scsi0"] = json!(true);
    }
    let target: NativeVmConfig = serde_json::from_value(target).unwrap();
    let source = input
        .source_config
        .as_ref()
        .unwrap()
        .result
        .as_ref()
        .unwrap();
    let mut wire = serde_json::to_value(source).unwrap();
    wire["observed_at"] = json!(t);
    let source: NativeVmConfig = serde_json::from_value(wire).unwrap();
    input.binding = NativeBinding::new(
        snapshot.run_id(),
        snapshot.operation_id(),
        snapshot.attempt_id().unwrap(),
        snapshot.plan(),
        snapshot.revision().try_into().unwrap(),
    );
    input.plan = snapshot.plan().clone();
    input.collected_at = t;
    input.evaluated_at = t;
    input.receipt = snapshot.receipt().cloned();
    input.task = None;
    input.bound_intermediate = Some(
        clone
            .evidence()
            .unwrap()
            .1
            .facts()
            .target_config
            .as_ref()
            .unwrap()
            .result
            .as_ref()
            .unwrap()
            .clone(),
    );
    input.target_config = Some(NativeRead::new(t, Ok(target.clone())));
    input.identities = vec![
        NativeIdentityRead {
            node: vm().node().clone(),
            vmid: vm().source_vmid(),
            read: NativeRead::new(t, Ok(source.clone())),
        },
        NativeIdentityRead {
            node: vm().node().clone(),
            vmid: vm().target_vmid(),
            read: NativeRead::new(t, Ok(target)),
        },
    ];
    input.source_config = Some(NativeRead::new(t, Ok(source)));
    input.target_power = Some(NativeRead::new(
        t,
        VmPowerStatus::from_wire(
            vm().node().clone(),
            vm().target_vmid(),
            json!({"vmid":9010,"status":if running{"running"}else{"stopped"},"locked":0}),
            t,
        ),
    ));
    input.inventory = Some(NativeRead::new(
        t,
        ClusterVmInventory::from_wire(
            json!([
        {"vmid":9000,"node":"pve-test","name":"template","type":"qemu","template":1,"status":"stopped"},
        {"vmid":9010,"node":"pve-test","name":"native-proof-9010","type":"qemu","template":0,"status":if running{"running"}else{"stopped"}}]),
            t,
        ),
    ));
    NativeEvidence::new(input).unwrap()
}
#[derive(Debug, PartialEq, Eq)]
pub struct Counts {
    pub operations: i64,
    pub commands: i64,
    pub reservations: i64,
    pub plans: i64,
    pub dispatches: i64,
    pub receipts: i64,
    pub decisions: i64,
    pub events: i64,
    pub outbox: i64,
}
impl Counts {
    pub fn intake() -> Self {
        Self {
            operations: 3,
            commands: 3,
            reservations: 1,
            plans: 3,
            dispatches: 0,
            receipts: 0,
            decisions: 0,
            events: 0,
            outbox: 0,
        }
    }
}
pub fn digest(value: &impl serde::Serialize) -> String {
    event_journal::payload_digest(&serde_json::to_value(value).unwrap()).unwrap()
}
pub fn vm() -> NativeVmPlan {
    serde_json::from_value(json!({"contract_version":1,"cluster_key":"fake-local","node":"pve-test","source_vmid":9000,"target_vmid":9010,"name":"native-proof-9010","storage":"local-lvm","bridge":"vmbr0","uuid":"3f2504e0-4f89-41d3-9a0c-0305e82c3301","mac":"02:00:00:00:90:10","cores":2,"memory_mib":2048,"minimum_storage_bytes":17179869184_u64})).unwrap()
}
pub fn preflight(run: RunId, grant: &LeaseGrant, revision: i64) -> NativeEvidence {
    preflight_with_binding(run, grant.operation_id(), grant.attempt_id(), revision)
}
pub fn preflight_with_binding(
    run: RunId,
    operation: controller_domain::OperationId,
    attempt: controller_domain::AttemptId,
    revision: i64,
) -> NativeEvidence {
    let t = Utc::now();
    let vm = vm();
    let source=NativeVmConfig::from_wire(vm.node().clone(),vm.source_vmid(),json!({"digest":"source-digest","name":"template","cores":1,"memory":512,"scsi0":"local-lvm:vm-9000-disk-0","smbios1":"uuid=4f2504e0-4f89-41d3-9a0c-0305e82c3301","net0":"virtio=02:00:00:00:00:01,bridge=vmbr0","agent":0,"boot":"order=scsi0","template":1}),t).unwrap();
    let plan = NativeOperationPlan::new(NativeStep::Clone, vm.clone());
    NativeEvidence::new(NativeEvidenceInput{
        binding:NativeBinding::new(run,operation,attempt,&plan,revision.try_into().unwrap()),plan,source:NativeEvidenceSource::FakePve,collected_at:t,evaluated_at:t,
        node:Some(NativeRead::new(t,NodeStatus::from_wire(vm.node().clone(),json!({"uptime":123}),t))),
        storage:Some(NativeRead::new(t,StorageStatus::from_wire(vm.node().clone(),vm.storage().clone(),json!({"active":1,"enabled":1,"content":"images","avail":34359738368_u64}),t))),
        bridges:Some(NativeRead::new(t,BridgeInventory::from_wire(vm.node().clone(),json!([{"type":"bridge","iface":"vmbr0","active":1}]),t))),
        inventory:Some(NativeRead::new(t,ClusterVmInventory::from_wire(json!([{"vmid":9000,"node":"pve-test","name":"template","type":"qemu","template":1,"status":"stopped"}]),t))),
        identities:vec![NativeIdentityRead{node:vm.node().clone(),vmid:vm.source_vmid(),read:NativeRead::new(t,Ok(source.clone()))}],
        source_config:Some(NativeRead::new(t,Ok(source))),source_power:Some(NativeRead::new(t,VmPowerStatus::from_wire(vm.node().clone(),vm.source_vmid(),json!({"vmid":9000,"status":"stopped","locked":0}),t))),target_config:Some(NativeRead::new(t,Err(PveReadError::NotFound))),target_power:None,task:None,receipt:None,bound_intermediate:None
    }).unwrap()
}
