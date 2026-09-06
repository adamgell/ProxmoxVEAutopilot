//! Private owned local PostgreSQL lifecycle, included only by native tests and proof.
#[cfg(test)]
#[allow(
    dead_code,
    reason = "read-only audit is used by the dedicated macOS storage harness"
)]
pub mod docker_storage_probe;
#[cfg(test)]
mod linux_postgres;
mod process;
use controller_domain::RunId;
#[cfg(test)]
use std::sync::Arc;
use std::time::{Duration, Instant};

pub const SETUP_BOUND: Duration = Duration::from_secs(30);
const CLEANUP_BOUND: Duration = Duration::from_secs(3);
const OWNERSHIP_LABEL: &str = "io.proxmoxveautopilot.native-proof";
const IMAGE: &str = "postgres:16-alpine";
const OWNERSHIP_FORMAT: &str = "{{.Id}}|{{.Name}}|{{index .Config.Labels \"io.proxmoxveautopilot.native-proof\"}}|{{.Config.Image}}";
const PGDATA: &str = "/var/lib/postgresql/data";
const TMPFS_OPTIONS: &str = "rw,nosuid,nodev,size=1g,mode=0700";
const STORAGE_FORMAT: &str = r#"{"id":{{json .Id}},"tmpfs":{{json .HostConfig.Tmpfs}},"binds":{{json .HostConfig.Binds}},"volumes_from":{{json .HostConfig.VolumesFrom}},"mounts":{{json .Mounts}}}"#;

struct DockerOwned {
    id: Option<String>,
    name: String,
    endpoint: String,
    #[cfg(test)]
    cleanup_script: Option<cleanup_tests::Script>,
}
enum Backend {
    Docker(DockerOwned),
    #[cfg(all(test, target_os = "linux"))]
    Linux(linux_postgres::LinuxDatabase),
}
pub struct Container {
    backend: Backend,
    cleanup_result: Option<Result<(), ()>>,
}
#[cfg(test)]
impl Container {
    #[allow(
        dead_code,
        reason = "only the dedicated storage harness obtains read-only audit handles"
    )]
    pub fn storage_audit(
        &self,
    ) -> Result<docker_storage_probe::DockerStorageAudit, docker_storage_probe::AuditError> {
        if self.cleanup_result.is_some() {
            return Err(docker_storage_probe::AuditError::Ownership);
        }
        match &self.backend {
            Backend::Docker(owned) => docker_storage_probe::DockerStorageAudit::new(owned),
            #[cfg(target_os = "linux")]
            Backend::Linux(_) => Err(docker_storage_probe::AuditError::Ownership),
        }
    }
}
impl Drop for Container {
    fn drop(&mut self) {
        if self.cleanup_result.is_none() && self.cleanup().is_err() {
            eprintln!("native_fake_cleanup_unconfirmed");
        }
    }
}
impl Container {
    fn pending(endpoint: String) -> Self {
        Self {
            backend: Backend::Docker(DockerOwned::pending(endpoint)),
            cleanup_result: None,
        }
    }
    fn docker_owned(&mut self) -> &mut DockerOwned {
        match &mut self.backend {
            Backend::Docker(owned) => owned,
            #[cfg(all(test, target_os = "linux"))]
            Backend::Linux(_) => panic!("local_fixture_backend_invalid"),
        }
    }
    pub fn cleanup(&mut self) -> Result<(), ()> {
        let deadline = Instant::now() + CLEANUP_BOUND;
        if let Some(result) = self.cleanup_result {
            return result;
        }
        // Record the attempt before entering any fallible or blocking work.
        self.cleanup_result = Some(Err(()));
        let result = match &mut self.backend {
            Backend::Docker(owned) => owned.cleanup(deadline),
            #[cfg(all(test, target_os = "linux"))]
            Backend::Linux(owned) => owned.cleanup(deadline),
        };
        self.cleanup_result = Some(result);
        result
    }
    #[cfg(test)]
    #[allow(dead_code, reason = "only authenticated service fixtures request logs")]
    pub async fn logs(&self) -> Result<String, ()> {
        match &self.backend {
            Backend::Docker(owned) => owned.logs().await,
            #[cfg(target_os = "linux")]
            Backend::Linux(_) => Err(()),
        }
    }
}
impl DockerOwned {
    #[cfg(test)]
    #[allow(
        dead_code,
        reason = "only the authenticated service fixture audits complete owned logs"
    )]
    pub async fn logs(&self) -> Result<String, ()> {
        tokio::time::timeout(Duration::from_secs(3), async {
            let out = process::docker(&[
                "--host",
                &self.endpoint,
                "inspect",
                "--format",
                OWNERSHIP_FORMAT,
                self.id.as_deref().ok_or(())?,
            ])
            .await
            .map_err(|_| ())?;
            let id = self.verified_owned_id(&out).ok_or(())?;
            let output = process::docker_logs(&self.endpoint, &id, Duration::from_secs(3))
                .await
                .map_err(|_| ())?;
            if !output.status.success() {
                return Err(());
            }
            String::from_utf8(output.stdout).map_err(|_| ())
        })
        .await
        .map_err(|_| ())?
    }
    fn pending(endpoint: String) -> Self {
        Self {
            id: None,
            name: format!("native-proof-{}", RunId::new().as_uuid()),
            endpoint,
            #[cfg(test)]
            cleanup_script: None,
        }
    }
    fn run_args(&self) -> Vec<String> {
        vec![
            "--host".into(),
            self.endpoint.clone(),
            "run".into(),
            "--pull=never".into(),
            "--detach".into(),
            "--name".into(),
            self.name.clone(),
            "--label".into(),
            format!("{OWNERSHIP_LABEL}={}", self.name),
            "--env".into(),
            "POSTGRES_PASSWORD=postgres".into(),
            "--env".into(),
            format!("POSTGRES_DB={}", super::LOCAL_DATABASE_NAME),
            "--publish".into(),
            "127.0.0.1::5432".into(),
            "--tmpfs".into(),
            format!("{PGDATA}:{TMPFS_OPTIONS}"),
            IMAGE.into(),
        ]
    }
    fn storage_admitted(&self, output: &std::process::Output) -> bool {
        if !output.status.success() {
            return false;
        }
        let Ok(value) = serde_json::from_slice::<serde_json::Value>(&output.stdout) else {
            return false;
        };
        let Some(object) = value.as_object() else {
            return false;
        };
        if object.len() != 5 {
            return false;
        }
        let Some(id) = self.id.as_deref().filter(|id| valid_id(id)) else {
            return false;
        };
        if object.get("id").and_then(|value| value.as_str()) != Some(id) {
            return false;
        }
        let Some(tmpfs) = object.get("tmpfs").and_then(|value| value.as_object()) else {
            return false;
        };
        if tmpfs.len() != 1
            || tmpfs.get(PGDATA).and_then(|value| value.as_str()) != Some(TMPFS_OPTIONS)
        {
            return false;
        }
        for key in ["binds", "volumes_from"] {
            match object.get(key) {
                Some(serde_json::Value::Null) => {}
                Some(serde_json::Value::Array(values)) if values.is_empty() => {}
                _ => return false,
            }
        }
        let Some(mounts) = object.get("mounts").and_then(|value| value.as_array()) else {
            return false;
        };
        if mounts.is_empty() {
            return true;
        }
        if mounts.len() != 1 {
            return false;
        }
        let mount = &mounts[0];
        mount.get("Type").and_then(|v| v.as_str()) == Some("tmpfs")
            && mount.get("Destination").and_then(|v| v.as_str()) == Some(PGDATA)
            && mount.get("Source").and_then(|v| v.as_str()) == Some("")
            && mount.get("RW").and_then(|v| v.as_bool()) == Some(true)
    }
    fn cleanup(&mut self, deadline: Instant) -> Result<(), ()> {
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
impl Container {
    pub async fn start() -> (Self, String) {
        #[cfg(test)]
        {
            let selected = std::env::var_os("PROXMOXVEAUTOPILOT_LINUX_TEST_DB");
            let selected = selected
                .as_ref()
                .map(|s| s.to_str().expect("local_linux_mode_invalid"));
            let linux = linux_postgres::select_mode(cfg!(target_os = "linux"), selected)
                .expect("local_linux_mode_invalid");
            #[cfg(target_os = "linux")]
            if linux {
                return tokio::time::timeout(SETUP_BOUND, async {
                    let receipt = linux_postgres::admit()
                        .await
                        .expect("local_linux_admission_refused");
                    let nonce = RunId::new().as_uuid().simple().to_string();
                    let database =
                        linux_postgres::pending(receipt, super::LOCAL_DATABASE_NAME, &nonce)
                            .expect("local_linux_identity_invalid");
                    let mut guard = Self {
                        backend: Backend::Linux(database),
                        cleanup_result: None,
                    };
                    let dsn = match &mut guard.backend {
                        Backend::Linux(database) => database
                            .create()
                            .await
                            .expect("local_linux_create_unconfirmed"),
                        Backend::Docker(_) => unreachable!(),
                    };
                    (guard, dsn)
                })
                .await
                .expect("local_fixture_setup_timeout");
            }
            #[cfg(not(target_os = "linux"))]
            assert!(!linux, "local_linux_mode_invalid");
        }
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
        let mut guard = Container::pending(endpoint);
        let container = guard.docker_owned();
        #[cfg(test)]
        eprintln!("owned_fixture_pending {}", container.name);
        let args = container.run_args();
        let borrowed: Vec<_> = args.iter().map(String::as_str).collect();
        let out = process::docker(&borrowed)
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
        #[cfg(test)]
        eprintln!("owned_fixture_created {} {}", container.name, id);
        let storage = process::docker(&[
            "--host",
            &container.endpoint,
            "inspect",
            "--format",
            STORAGE_FORMAT,
            &id,
        ])
        .await
        .expect("local_storage_unconfirmed");
        assert!(
            container.storage_admitted(&storage),
            "local_storage_unconfirmed"
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
        let dsn = format!(
            "postgresql://postgres:postgres@127.0.0.1:{port}/{}",
            super::LOCAL_DATABASE_NAME
        );
        (guard, dsn)
    }
}

#[cfg(test)]
mod storage_tests {
    use super::*;
    use serde_json::{Value, json};
    use std::os::unix::process::ExitStatusExt;

    fn owned() -> DockerOwned {
        let mut owned = DockerOwned::pending("unix:///unused-test-only".into());
        owned.id = Some("a".repeat(64));
        owned
    }
    fn output(value: Value) -> std::process::Output {
        std::process::Output {
            status: std::process::ExitStatus::from_raw(0),
            stdout: serde_json::to_vec(&value).unwrap(),
            stderr: vec![],
        }
    }
    fn projection() -> Value {
        json!({"id":"a".repeat(64),
            "tmpfs":{"/var/lib/postgresql/data":"rw,nosuid,nodev,size=1g,mode=0700"},
            "binds":null,"volumes_from":null,"mounts":[]})
    }
    #[test]
    fn actual_launch_selects_fixed_bounded_pgdata_tmpfs() {
        let args = owned().run_args();
        let selected: Vec<_> = args
            .windows(2)
            .filter(|pair| pair[0] == "--tmpfs")
            .collect();
        assert_eq!(selected.len(), 1);
        assert_eq!(
            selected[0][1],
            "/var/lib/postgresql/data:rw,nosuid,nodev,size=1g,mode=0700"
        );
        assert!(args.iter().any(|arg| arg == "--pull=never"));
        assert_eq!(args.last().unwrap(), "postgres:16-alpine");
        assert!(
            !args
                .iter()
                .any(|arg| matches!(arg.as_str(), "-v" | "--volume" | "--mount"))
        );
    }
    #[test]
    fn ownership_valid_persistent_pgdata_is_refused() {
        let owned = owned();
        let ownership = std::process::Output {
            stdout: format!(
                "{}|/{}|{}|{}",
                "a".repeat(64),
                owned.name,
                owned.name,
                IMAGE
            )
            .into_bytes(),
            ..output(json!(null))
        };
        assert_eq!(owned.verified_owned_id(&ownership), owned.id);
        let mut value = projection();
        value["tmpfs"] = Value::Null;
        value["mounts"] =
            json!([{"Type":"volume","Destination":"/var/lib/postgresql/data","Name":"unexpected"}]);
        assert!(!owned.storage_admitted(&output(value)));
    }
    #[test]
    fn both_bounded_tmpfs_representations_are_admitted() {
        let owned = owned();
        assert!(owned.storage_admitted(&output(projection())));
        let mut value = projection();
        value["binds"] = json!([]);
        value["volumes_from"] = json!([]);
        value["mounts"] = json!([{"Type":"tmpfs","Destination":PGDATA,"Source":"","RW":true}]);
        assert!(owned.storage_admitted(&output(value)));
    }
    #[test]
    fn storage_projection_rejects_each_wrong_field_and_failed_output() {
        let owned = owned();
        for (key, wrong) in [
            ("id", json!("b".repeat(64))),
            ("id", json!("a")),
            ("id", json!(null)),
            ("tmpfs", json!(null)),
            ("tmpfs", json!({})),
            ("tmpfs", json!([])),
            (
                "tmpfs",
                json!({"/wrong":"rw,nosuid,nodev,size=1g,mode=0700"}),
            ),
            ("tmpfs", json!({PGDATA:"rw"})),
            ("tmpfs", json!({PGDATA:"rw,nosuid,nodev,size=2g,mode=0700"})),
            (
                "tmpfs",
                json!({PGDATA:TMPFS_OPTIONS,"/extra":TMPFS_OPTIONS}),
            ),
            ("binds", json!(["/outside:/var/lib/postgresql/data"])),
            ("binds", json!("")),
            ("volumes_from", json!(["foreign"])),
            ("volumes_from", json!("")),
            ("mounts", json!(null)),
            ("mounts", json!([{"Type":"volume","Destination":PGDATA}])),
            ("mounts", json!([{"Type":"bind","Destination":PGDATA}])),
            (
                "mounts",
                json!([{"Type":"tmpfs","Destination":"/extra","Source":"","RW":true}]),
            ),
            (
                "mounts",
                json!([{"Type":"tmpfs","Destination":PGDATA,"Source":"foreign","RW":true}]),
            ),
            (
                "mounts",
                json!([{"Type":"tmpfs","Destination":PGDATA,"Source":"","RW":false}]),
            ),
            (
                "mounts",
                json!([{"Type":"tmpfs","Destination":PGDATA,"Source":"","RW":true},{}]),
            ),
        ] {
            let mut value = projection();
            value[key] = wrong;
            assert!(
                !owned.storage_admitted(&output(value)),
                "accepted wrong field {key}"
            );
        }
        for key in ["id", "tmpfs", "binds", "volumes_from", "mounts"] {
            let mut value = projection();
            value.as_object_mut().unwrap().remove(key);
            assert!(!owned.storage_admitted(&output(value)));
        }
        let mut extra = projection();
        extra["extra"] = json!(null);
        assert!(!owned.storage_admitted(&output(extra)));
        for key in ["Type", "Destination", "Source", "RW"] {
            let mut value = projection();
            let mut mount = json!({"Type":"tmpfs","Destination":PGDATA,"Source":"","RW":true});
            mount.as_object_mut().unwrap().remove(key);
            value["mounts"] = json!([mount]);
            assert!(!owned.storage_admitted(&output(value)));
        }
        for bytes in [b"{".as_slice(), b"null", b"[]", b"\xff"] {
            let mut out = output(projection());
            out.stdout = bytes.to_vec();
            assert!(!owned.storage_admitted(&out));
        }
        let mut failed = output(projection());
        failed.status = std::process::ExitStatus::from_raw(256);
        assert!(!owned.storage_admitted(&failed));
        let mut pending = owned;
        pending.id = None;
        assert!(!pending.storage_admitted(&output(projection())));
        pending.id = Some("a".into());
        let mut invalid = projection();
        invalid["id"] = json!("a");
        assert!(!pending.storage_admitted(&output(invalid)));
    }
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
            container.docker_owned().id = Some("b".repeat(64));
        }
        container.docker_owned().cleanup_script = Some(Script {
            scenario,
            removals: removals.clone(),
            pids: pids.clone(),
        });
        (container, removals, pids)
    }
    #[test]
    fn storage_refusal_preserves_one_shot_container_only_cleanup() {
        use std::os::unix::process::ExitStatusExt;
        let (mut container, removals, pids) = container(Scenario::Owned);
        container.docker_owned().id = Some("a".repeat(64));
        let bad = std::process::Output {
            status: std::process::ExitStatus::from_raw(0),
            stdout: b"{}".to_vec(),
            stderr: vec![],
        };
        assert!(!container.docker_owned().storage_admitted(&bad));
        assert_eq!(container.cleanup(), Ok(()));
        let count = pids.lock().unwrap().len();
        drop(container);
        assert_eq!(pids.lock().unwrap().len(), count);
        assert_eq!(*removals.lock().unwrap(), vec!["a".repeat(64)]);
        for pid in pids.lock().unwrap().iter() {
            process::assert_child_reaped(*pid);
        }
    }
    #[test]
    fn lost_create_response_recovers_only_verified_owned_id() {
        let (mut container, removals, pids) = container(Scenario::Owned);
        assert!(container.docker_owned().id.is_none());
        container.cleanup().unwrap();
        drop(container);
        assert_eq!(*removals.lock().unwrap(), vec!["a".repeat(64)]);
        for pid in pids.lock().unwrap().iter() {
            process::assert_child_reaped(*pid);
        }
    }
    #[test]
    fn explicit_cleanup_and_drop_attempt_owned_removal_only_once() {
        for scenario in [Scenario::Owned, Scenario::WrongImage] {
            let (mut container, removals, pids) = container(scenario);
            let first = container.cleanup();
            let count = pids.lock().unwrap().len();
            assert_eq!(container.cleanup(), first);
            drop(container);
            assert_eq!(pids.lock().unwrap().len(), count);
            assert_eq!(
                removals.lock().unwrap().len(),
                usize::from(matches!(scenario, Scenario::Owned))
            );
            for pid in pids.lock().unwrap().iter() {
                process::assert_child_reaped(*pid);
            }
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
        let (mut container, removals, pids) = container(Scenario::Owned);
        assert!(container.docker_owned().id.is_none());
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
