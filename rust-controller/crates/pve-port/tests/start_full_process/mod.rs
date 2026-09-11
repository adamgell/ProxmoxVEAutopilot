//! Owned subprocesses; child entrypoints are inert in ordinary and ignored runs.
use pve_port::{fixture_ipc::FixtureStageRequest, fixture_support::*};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::Write,
    path::Path,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
use uuid::Uuid;

pub struct OwnedChild(Child);
impl OwnedChild {
    pub fn daemon(directory: &Path) -> Self {
        Self(
            Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "start_full_process::daemon_child", "--nocapture"])
                .env("PVA_START_FULL_DAEMON", directory)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .spawn()
                .unwrap(),
        )
    }
    pub fn kill(&mut self) {
        assert!(
            self.0.try_wait().unwrap().is_none(),
            "child exited before forced death"
        );
        self.0.kill().unwrap();
        assert!(!self.0.wait().unwrap().success());
    }
    pub fn wait(&mut self) {
        assert!(self.0.wait().unwrap().success());
    }
}
impl Drop for OwnedChild {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
fn daemon_child() {
    let Some(directory) = std::env::var_os("PVA_START_FULL_DAEMON") else {
        return;
    };
    run(Path::new(&directory), Duration::from_secs(30)).unwrap();
}
#[derive(Serialize, Deserialize)]
struct Input {
    identity: FixtureStageIdentity,
    request: Vec<u8>,
    receipt: Vec<u8>,
    park: bool,
}
#[test]
fn restoration_child() {
    let Some(input) = std::env::var_os("PVA_START_FULL_INPUT") else {
        return;
    };
    let path = Path::new(&input);
    let directory = path.parent().unwrap();
    let input: Input = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    let request = FixtureStageRequest::decode(&input.request).unwrap();
    let vm = request.request().plan().expected().vm();
    let adapter = FixtureProvisioningPort::new_late(
        directory.join("client.sock"),
        Duration::from_secs(2),
        FixtureReadIdentity {
            fixture_id: request.fixture_id(),
            operation: input.identity.operation,
            node: vm.node().to_string(),
            source_vmid: vm.source_vmid().get(),
            target_vmid: vm.target_vmid().get(),
        },
    )
    .unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let result = runtime
        .block_on(adapter.validate_start_pe(&input.identity, &request, &input.receipt))
        .unwrap()
        .unwrap();
    let mut output = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path.with_extension("output"))
        .unwrap();
    output
        .write_all(&serde_json::to_vec(result.publication()).unwrap())
        .unwrap();
    output.sync_all().unwrap();
    let ready = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path.with_extension("ready"))
        .unwrap();
    ready.sync_all().unwrap();
    if input.park {
        std::thread::sleep(Duration::from_secs(30));
        panic!("supervisor failed to terminate parked worker");
    }
}

pub async fn worker(
    directory: &Path,
    identity: &FixtureStageIdentity,
    request: &FixtureStageRequest,
    receipt: &[u8],
    kill: bool,
) -> serde_json::Value {
    let path = directory.join(format!("full-worker-{}.json", Uuid::now_v7()));
    fs::write(
        &path,
        serde_json::to_vec(&Input {
            identity: identity.clone(),
            request: request.encode().unwrap(),
            receipt: receipt.to_vec(),
            park: kill,
        })
        .unwrap(),
    )
    .unwrap();
    let mut child = OwnedChild(
        Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "start_full_process::restoration_child",
                "--nocapture",
            ])
            .env("PVA_START_FULL_INPUT", &path)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(5);
    while !path.with_extension("ready").exists() {
        assert!(
            child.0.try_wait().unwrap().is_none(),
            "restoration worker exited before ready"
        );
        assert!(
            Instant::now() < deadline,
            "restoration worker readiness timeout"
        );
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    if kill {
        child.kill();
    } else {
        child.wait();
    }
    serde_json::from_slice(&fs::read(path.with_extension("output")).unwrap()).unwrap()
}
