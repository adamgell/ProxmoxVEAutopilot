//! Private local proof infrastructure, shared only by the test and example.
mod process;
use chrono::Utc;
use controller_domain::RunId;
use operation_controller::NativeController;
use postgres_store::{ExecutorKind, NativeWorkflowIds, PgStore, Scheduler};
use pve_port::*;
use serde_json::json;
use sqlx::{PgPool, postgres::PgPoolOptions};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};

const CLEANUP_BOUND: Duration = Duration::from_secs(3);
const OWNERSHIP_LABEL: &str = "io.proxmoxveautopilot.native-proof";
const IMAGE: &str = "postgres:16-alpine";
const OWNERSHIP_FORMAT: &str = "{{.Id}}|{{.Name}}|{{index .Config.Labels \"io.proxmoxveautopilot.native-proof\"}}|{{.Config.Image}}";

struct Container {
    id: Option<String>,
    name: String,
    endpoint: String,
    cleanup_attempted: bool,
    #[cfg(test)]
    cleanup_script: Option<cleanup_tests::Script>,
}
impl Drop for Container {
    fn drop(&mut self) {
        if !self.cleanup_attempted && self.cleanup().is_err() {
            eprintln!("native_fake_cleanup_unconfirmed");
        }
    }
}
impl Container {
    fn pending(endpoint: String) -> Self {
        Self {
            id: None,
            name: format!("native-proof-{}", RunId::new().as_uuid()),
            endpoint,
            cleanup_attempted: false,
            #[cfg(test)]
            cleanup_script: None,
        }
    }
    fn cleanup(&mut self) -> Result<(), ()> {
        self.cleanup_attempted = true;
        let deadline = Instant::now() + CLEANUP_BOUND;
        let output = self.inspect_owned(deadline).map_err(|_| ())?;
        let id = self.verified_owned_id(&output).ok_or(())?;
        let removed = self.remove_owned(&id, deadline).map_err(|_| ())?;
        if removed.status.success() {
            Ok(())
        } else {
            Err(())
        }
    }
    fn verified_owned_id(&self, output: &std::process::Output) -> Option<String> {
        if !output.status.success() {
            return None;
        }
        let text = std::str::from_utf8(&output.stdout).ok()?.trim();
        let fields: Vec<_> = text.split('|').collect();
        if fields.len() != 4
            || !valid_id(fields[0])
            || fields[1] != format!("/{}", self.name)
            || fields[2] != self.name
            || fields[3] != IMAGE
            || self.id.as_ref().is_some_and(|id| id != fields[0])
        {
            return None;
        }
        Some(fields[0].to_owned())
    }
    fn inspect_owned(&self, deadline: Instant) -> std::io::Result<std::process::Output> {
        #[cfg(test)]
        if let Some(script) = &self.cleanup_script {
            return script.inspect(&self.name, deadline);
        }
        process::docker_cleanup(
            &[
                "--host",
                &self.endpoint,
                "inspect",
                "--format",
                OWNERSHIP_FORMAT,
                self.id.as_deref().unwrap_or(&self.name),
            ],
            deadline,
        )
    }
    fn remove_owned(&self, id: &str, deadline: Instant) -> std::io::Result<std::process::Output> {
        #[cfg(test)]
        if let Some(script) = &self.cleanup_script {
            return script.remove(id, deadline);
        }
        process::docker_cleanup(&["--host", &self.endpoint, "rm", "--force", id], deadline)
    }
}
fn valid_id(id: &str) -> bool {
    id.len() == 64 && id.bytes().all(|b| b.is_ascii_hexdigit())
}
pub struct Fixture {
    pub pool: PgPool,
    pub store: PgStore,
    pub fake: Arc<NativeFakePve>,
    _container: Container,
}
impl Fixture {
    pub async fn new() -> Self {
        assert!(
            std::env::var_os("DOCKER_HOST").is_none(),
            "local_docker_override_rejected"
        );
        let host = process::docker(&[
            "context",
            "inspect",
            "--format",
            "{{.Endpoints.docker.Host}}",
        ])
        .await
        .expect("local_docker_unavailable");
        assert!(host.status.success(), "local_docker_unavailable");
        let endpoint = String::from_utf8(host.stdout)
            .expect("local_docker_invalid")
            .trim()
            .to_owned();
        assert!(endpoint.starts_with("unix:///"), "local_docker_required");
        // Establish ownership recovery before create: the daemon may create a
        // container even when its response is lost or this future is cancelled.
        let mut container = Container::pending(endpoint);
        let label = format!("{OWNERSHIP_LABEL}={}", container.name);
        let out = process::docker(&[
            "--host",
            &container.endpoint,
            "run",
            "--pull=never",
            "--detach",
            "--name",
            &container.name,
            "--label",
            &label,
            "--env",
            "POSTGRES_PASSWORD=postgres",
            "--env",
            "POSTGRES_DB=native_proof",
            "--publish",
            "127.0.0.1::5432",
            IMAGE,
        ])
        .await
        .expect("local_database_unavailable");
        assert!(out.status.success(), "local_database_unavailable");
        let id = String::from_utf8(out.stdout)
            .expect("local_container_invalid")
            .trim()
            .to_owned();
        assert!(valid_id(&id), "local_container_invalid");
        container.id = Some(id.clone());
        let ownership = process::docker(&[
            "--host",
            &container.endpoint,
            "inspect",
            "--format",
            OWNERSHIP_FORMAT,
            &id,
        ])
        .await
        .expect("local_ownership_unconfirmed");
        assert_eq!(
            container.verified_owned_id(&ownership).as_deref(),
            Some(id.as_str()),
            "local_ownership_unconfirmed"
        );
        let out = process::docker(&["--host", &container.endpoint,"inspect","--format","{{(index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostIp}}:{{(index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostPort}}", &id]).await.expect("local_publish_invalid");
        assert!(out.status.success(), "local_publish_invalid");
        let address = String::from_utf8(out.stdout).expect("local_publish_invalid");
        let (host, port) = address
            .trim()
            .split_once(':')
            .expect("local_publish_invalid");
        assert_eq!(host, "127.0.0.1", "local_publish_invalid");
        let port: u16 = port.parse().expect("local_publish_invalid");
        let dsn = format!("postgresql://postgres:postgres@127.0.0.1:{port}/native_proof");
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

#[cfg(test)]
mod cleanup_tests {
    use super::*;
    use std::sync::Mutex;
    #[derive(Clone, Copy)]
    enum Scenario {
        Owned,
        WrongName,
        WrongLabel,
        WrongImage,
        WrongId,
        Missing,
        Hung,
        HungRemove,
    }
    pub(super) struct Script {
        scenario: Scenario,
        removals: Arc<Mutex<Vec<String>>>,
        pids: Arc<Mutex<Vec<u32>>>,
    }
    impl Script {
        pub(super) fn inspect(
            &self,
            name: &str,
            deadline: Instant,
        ) -> std::io::Result<std::process::Output> {
            let id = "a".repeat(64);
            let command = match self.scenario {
                Scenario::Missing => process::TestCleanupCommand::Missing,
                Scenario::Hung => process::TestCleanupCommand::Hang,
                Scenario::WrongName => {
                    process::TestCleanupCommand::Inspect(format!("{id}|/foreign|{name}|{IMAGE}"))
                }
                Scenario::WrongLabel => {
                    process::TestCleanupCommand::Inspect(format!("{id}|/{name}|foreign|{IMAGE}"))
                }
                Scenario::WrongImage => process::TestCleanupCommand::Inspect(format!(
                    "{id}|/{name}|{name}|foreign:latest"
                )),
                _ => process::TestCleanupCommand::Inspect(format!("{id}|/{name}|{name}|{IMAGE}")),
            };
            process::scripted_cleanup(command, deadline, &self.pids)
        }
        pub(super) fn remove(
            &self,
            id: &str,
            deadline: Instant,
        ) -> std::io::Result<std::process::Output> {
            self.removals.lock().unwrap().push(id.to_owned());
            let command = if matches!(self.scenario, Scenario::HungRemove) {
                process::TestCleanupCommand::Hang
            } else {
                process::TestCleanupCommand::Remove
            };
            process::scripted_cleanup(command, deadline, &self.pids)
        }
    }
    type CleanupCase = (Container, Arc<Mutex<Vec<String>>>, Arc<Mutex<Vec<u32>>>);
    fn container(scenario: Scenario) -> CleanupCase {
        let mut container = Container::pending("unix:///unused-test-only".to_owned());
        let removals = Arc::new(Mutex::new(vec![]));
        let pids = Arc::new(Mutex::new(vec![]));
        if matches!(scenario, Scenario::WrongId) {
            container.id = Some("b".repeat(64));
        }
        container.cleanup_script = Some(Script {
            scenario,
            removals: removals.clone(),
            pids: pids.clone(),
        });
        (container, removals, pids)
    }
    #[test]
    fn lost_create_response_recovers_only_verified_owned_id() {
        let (mut container, removals, pids) = container(Scenario::Owned);
        assert!(container.id.is_none());
        container.cleanup().unwrap();
        drop(container);
        assert_eq!(*removals.lock().unwrap(), vec!["a".repeat(64)]);
        for pid in pids.lock().unwrap().iter() {
            process::assert_child_reaped(*pid);
        }
    }
    #[test]
    fn wrong_ownership_or_ambiguous_absence_never_removes_a_target() {
        for scenario in [
            Scenario::WrongName,
            Scenario::WrongLabel,
            Scenario::WrongImage,
            Scenario::WrongId,
            Scenario::Missing,
        ] {
            let (mut container, removals, pids) = container(scenario);
            assert!(container.cleanup().is_err());
            drop(container);
            assert!(removals.lock().unwrap().is_empty());
            for pid in pids.lock().unwrap().iter() {
                process::assert_child_reaped(*pid);
            }
        }
    }
    #[tokio::test(flavor = "current_thread")]
    async fn cancellation_runs_finite_drop_cleanup_and_reaps_hung_inspector() {
        for scenario in [Scenario::Hung, Scenario::HungRemove] {
            let (container, removals, pids) = container(scenario);
            let start = Instant::now();
            let result = tokio::time::timeout(Duration::from_millis(40), async move {
                let _owned = container;
                std::future::pending::<()>().await;
            })
            .await;
            assert!(result.is_err());
            assert!(start.elapsed() < CLEANUP_BOUND + Duration::from_millis(600));
            let expected = usize::from(matches!(scenario, Scenario::HungRemove));
            assert_eq!(removals.lock().unwrap().len(), expected);
            assert_eq!(pids.lock().unwrap().len(), expected + 1);
            for pid in pids.lock().unwrap().iter() {
                process::assert_child_reaped(*pid);
            }
        }
    }
    #[tokio::test(flavor = "current_thread")]
    async fn cancellation_before_create_receipt_recovers_owned_container() {
        let (container, removals, pids) = container(Scenario::Owned);
        assert!(container.id.is_none());
        assert!(
            tokio::time::timeout(Duration::from_millis(40), async move {
                let _owned = container;
                std::future::pending::<()>().await;
            })
            .await
            .is_err()
        );
        assert_eq!(*removals.lock().unwrap(), vec!["a".repeat(64)]);
        for pid in pids.lock().unwrap().iter() {
            process::assert_child_reaped(*pid);
        }
    }
}
