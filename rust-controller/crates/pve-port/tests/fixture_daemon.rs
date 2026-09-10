#![cfg(unix)]
#[allow(dead_code)]
#[path = "../src/fixture_support/durable_fixture_log.rs"]
mod durable_fixture_log;
#[cfg(not(feature = "fixture-ipc"))]
#[path = "../src/fixture_support/fixture_daemon.rs"]
mod fixture_daemon;
#[cfg(not(feature = "fixture-ipc"))]
use durable_fixture_log::VmState;
#[cfg(feature = "fixture-ipc")]
use pve_port::fixture_support as fixture_daemon;
#[cfg(feature = "fixture-ipc")]
use pve_port::fixture_support::VmState;

use std::{
    fs,
    io::{Read, Write},
    os::unix::{
        fs::{FileTypeExt, PermissionsExt},
        net::UnixStream,
    },
    path::PathBuf,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};
use uuid::Uuid;

struct Daemon {
    directory: PathBuf,
    child: Child,
}
impl Daemon {
    fn spawn(directory: &std::path::Path) -> Child {
        Command::new(std::env::current_exe().unwrap())
            .args(["--ignored", "--exact", "fixture_daemon_child"])
            .env("PVE_TEST_FIXTURE_DIR", directory)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap()
    }

    /// Only the supervisor holding the original child handle may remove endpoints.
    /// An unresponsive socket alone never proves the old process has exited.
    fn clear_stale_sockets(&mut self) -> std::io::Result<()> {
        if self.child.try_wait()?.is_none() {
            return Err(std::io::Error::other("fixture process is still alive"));
        }
        let paths = [
            self.directory.join("client.sock"),
            self.directory.join("supervisor.sock"),
        ];
        // Validate both before deleting either; do not follow symlinks.
        for path in &paths {
            if !fs::symlink_metadata(path)?.file_type().is_socket() {
                return Err(std::io::Error::other("fixture endpoint is not a socket"));
            }
        }
        for path in paths {
            fs::remove_file(path)?;
        }
        Ok(())
    }

    fn await_ready(&mut self) {
        let deadline = Instant::now() + Duration::from_secs(3);
        while !self.directory.join("client.sock").exists()
            || !self.directory.join("supervisor.sock").exists()
        {
            assert!(
                self.child.try_wait().unwrap().is_none(),
                "fixture exited during startup"
            );
            assert!(Instant::now() < deadline, "fixture startup timed out");
            thread::sleep(Duration::from_millis(5));
        }
    }

    fn await_failure(&mut self) {
        let deadline = Instant::now() + Duration::from_secs(1);
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                assert!(!status.success());
                return;
            }
            assert!(
                Instant::now() < deadline,
                "expected startup failure timed out"
            );
            thread::sleep(Duration::from_millis(5));
        }
    }

    fn start() -> Self {
        // macOS temp_dir paths can exceed sockaddr_un's small pathname limit.
        let directory = PathBuf::from("/tmp").join(format!("pve-ipc-{}", Uuid::now_v7()));
        fs::create_dir(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
        let child = Self::spawn(&directory);
        let mut daemon = Self { directory, child };
        daemon.await_ready();
        daemon
    }
    fn request(&self, socket: &str, payload: &[u8]) -> fixture_daemon::Reply {
        let mut stream = UnixStream::connect(self.directory.join(socket)).unwrap();
        // macOS may reject SO_RCVTIMEO for Unix-domain streams with EINVAL;
        // the daemon still enforces the authoritative bounded read deadline.
        if let Err(error) = stream.set_read_timeout(Some(Duration::from_secs(1))) {
            assert_eq!(error.kind(), std::io::ErrorKind::InvalidInput);
        }
        stream
            .write_all(&(payload.len() as u32).to_be_bytes())
            .unwrap();
        stream.write_all(payload).unwrap();
        let mut header = [0; 4];
        stream.read_exact(&mut header).unwrap();
        let length = u32::from_be_bytes(header) as usize;
        assert!(length <= 1024);
        let mut bytes = vec![0; length];
        stream.read_exact(&mut bytes).unwrap();
        serde_json::from_slice(&bytes).unwrap()
    }
}
impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = fs::remove_dir_all(&self.directory);
    }
}

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn snapshot_is_daemon_owned_and_survives_restart() {
    use pve_port::fixture_support::{
        FixtureInventory, FixtureReadClient, FixtureSnapshot, FixtureVmConfig,
    };
    let mut daemon = Daemon::start();
    let client =
        FixtureReadClient::new(daemon.directory.join("client.sock"), Duration::from_secs(1))
            .unwrap();
    assert_eq!(
        client.snapshot().await.unwrap(),
        FixtureSnapshot::Unavailable {}
    );
    let inventory = FixtureSnapshot::Inventory {
        inventory: FixtureInventory {
            version: 1,
            fixture_id: Uuid::now_v7(),
            node: "fixture-node".into(),
            observed_unix_ms: 123,
            vms: vec![FixtureVmConfig {
                vmid: 900,
                name: "template".into(),
                template: true,
                disk_bytes: 1024,
            }],
        },
    };
    fs::write(
        daemon.directory.join("inventory.json"),
        serde_json::to_vec(&inventory).unwrap(),
    )
    .unwrap();
    // The daemon owns its startup observation; changing the seed is not a live update.
    assert_eq!(
        client.snapshot().await.unwrap(),
        FixtureSnapshot::Unavailable {}
    );
    for _ in 0..2 {
        daemon.child.kill().unwrap();
        daemon.child.wait().unwrap();
        daemon.clear_stale_sockets().unwrap();
        daemon.child = Daemon::spawn(&daemon.directory);
        daemon.await_ready();
        assert_eq!(client.snapshot().await.unwrap(), inventory);
    }
    fs::write(daemon.directory.join("inventory.json"), b"{}").unwrap();
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_failure();
}

#[test]
#[ignore = "subprocess entry point only"]
fn fixture_daemon_child() {
    let path = PathBuf::from(std::env::var_os("PVE_TEST_FIXTURE_DIR").expect("fixture directory"));
    fixture_daemon::run(&path, Duration::from_secs(5)).unwrap();
}

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn clone_reads_preserve_seed_errors_identity_and_restart_observations() {
    use pve_port::fixture_support::{FixtureCloneReads, FixtureReadClient};
    let mut daemon = Daemon::start();
    let id = Uuid::now_v7();
    let client =
        FixtureReadClient::new(daemon.directory.join("client.sock"), Duration::from_secs(1))
            .unwrap();
    assert!(client.clone_reads(id).await.unwrap().is_none());
    assert!(client.clone_reads(Uuid::nil()).await.is_err());
    let seed = serde_json::json!({
        "version":1,"fixture_id":id,"node":"fixture-node",
        "node_status":{"state":"observed","observed_unix_ms":123,"value":{"online":true,"uptime_seconds":4}},
        "storage":{"state":"error","observed_unix_ms":124,"error":"forbidden"},
        "bridges":{"state":"observed","observed_unix_ms":125,"value":[]},
        "cluster_inventory":{"state":"error","observed_unix_ms":126,"error":"unavailable"}
    });
    let bytes = serde_json::to_vec(&seed).unwrap();
    let expected = FixtureCloneReads::decode(&bytes, id).unwrap();
    fs::write(daemon.directory.join("clone_reads.json"), &bytes).unwrap();
    assert!(client.clone_reads(id).await.unwrap().is_none());
    for _ in 0..2 {
        daemon.child.kill().unwrap();
        daemon.child.wait().unwrap();
        daemon.clear_stale_sockets().unwrap();
        daemon.child = Daemon::spawn(&daemon.directory);
        daemon.await_ready();
        assert_eq!(
            client.clone_reads(id).await.unwrap(),
            Some(expected.clone())
        );
        assert!(client.clone_reads(Uuid::now_v7()).await.is_err());
        let request = serde_json::to_vec(
            &serde_json::json!({"command":"clone_reads","fixture_id":id,"extra":true}),
        )
        .unwrap();
        assert!(!daemon.request("client.sock", &request).ok);
        assert_eq!(client.status().await.unwrap().attempts, 0);
    }
    fs::write(daemon.directory.join("clone_reads.json"), b"{}").unwrap();
    assert_eq!(client.clone_reads(id).await.unwrap(), Some(expected));
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_failure();
}

#[cfg(feature = "fixture-ipc")]
#[tokio::test]
async fn read_client_observes_daemon_owned_effect_without_writing() {
    let daemon = Daemon::start();
    let client = pve_port::fixture_support::FixtureReadClient::new(
        daemon.directory.join("client.sock"),
        Duration::from_secs(1),
    )
    .unwrap();
    let operation = Uuid::now_v7();
    let digest = "a".repeat(64);
    assert_eq!(client.status().await.unwrap().attempts, 0);
    assert!(client.world(100).await.unwrap().is_none());
    assert!(
        client
            .accepted_effect(operation, &digest)
            .await
            .unwrap()
            .is_none()
    );
    let attempt = serde_json::to_vec(
        &serde_json::json!({"command":"attempt", "operation":operation, "request_sha256":digest}),
    )
    .unwrap();
    assert!(daemon.request("client.sock", &attempt).ok);
    let accepted = daemon.request("client.sock", br#"{"command":"effect","attempt_sequence":1,"vmid":100,"before":null,"after":{"disk_bytes":80,"pe_configured":false}}"#);
    assert!(accepted.ok);
    let ledger_before = fs::read(daemon.directory.join("fixture.log")).unwrap();
    assert_eq!(client.world(100).await.unwrap(), accepted.vm);
    assert!(
        client
            .accepted_effect(operation, &digest)
            .await
            .unwrap()
            .is_some()
    );
    assert_eq!(client.status().await.unwrap().effects, 1);
    assert_eq!(
        fs::read(daemon.directory.join("fixture.log")).unwrap(),
        ledger_before
    );
}

#[test]
fn separate_process_enforces_control_boundary_and_durable_duplicate_ledger() {
    let mut daemon = Daemon::start();
    assert!(
        !daemon
            .request("client.sock", br#"{"command":"shutdown"}"#)
            .ok
    );
    assert!(
        !daemon
            .request("supervisor.sock", br#"{"command":"status"}"#)
            .ok
    );
    assert!(
        !daemon
            .request("client.sock", br#"{"command":"status","extra":true}"#)
            .ok
    );
    let attempt = serde_json::to_vec(&serde_json::json!({"command":"attempt", "operation":Uuid::now_v7(), "request_sha256":"a".repeat(64)})).unwrap();
    let first = daemon.request("client.sock", &attempt);
    assert!(first.ok);
    assert_eq!(first.duplicate, Some(false));
    let second = daemon.request("client.sock", &attempt);
    assert!(second.ok);
    assert_eq!(second.duplicate, Some(true));
    assert_eq!(second.attempts, 2);
    assert!(
        daemon
            .request("supervisor.sock", br#"{"command":"shutdown"}"#)
            .ok
    );
    let deadline = Instant::now() + Duration::from_secs(1);
    loop {
        if let Some(status) = daemon.child.try_wait().unwrap() {
            assert!(status.success());
            break;
        }
        assert!(Instant::now() < deadline, "shutdown timed out");
        thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(
        durable_fixture_log::FixtureLog::recover(&daemon.directory.join("fixture.log"))
            .unwrap()
            .records()
            .len(),
        2
    );
}

#[test]
fn oversized_and_partial_requests_cannot_mutate_or_stall_daemon() {
    let daemon = Daemon::start();
    for header in [2048_u32, 12] {
        let mut stream = UnixStream::connect(daemon.directory.join("client.sock")).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        stream.write_all(&header.to_be_bytes()).unwrap();
        let mut byte = [0];
        assert!(matches!(stream.read(&mut byte), Ok(0) | Err(_)));
    }
    let status = daemon.request("client.sock", br#"{"command":"status"}"#);
    assert!(status.ok);
    assert_eq!(status.attempts, 0);
}

#[test]
fn killed_daemon_rebind_requires_supervisor_cleanup_and_preserves_ledger() {
    let mut daemon = Daemon::start();
    let attempt = serde_json::to_vec(&serde_json::json!({"command":"attempt", "operation":Uuid::now_v7(), "request_sha256":"a".repeat(64)})).unwrap();
    assert_eq!(
        daemon.request("client.sock", &attempt).duplicate,
        Some(false)
    );
    assert!(daemon.clear_stale_sockets().is_err());
    assert_eq!(
        daemon
            .request("client.sock", br#"{"command":"status"}"#)
            .attempts,
        1
    );
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    let original = fs::read(daemon.directory.join("fixture.log")).unwrap();
    // The daemon cannot infer that existing endpoints are safe to unlink.
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_failure();
    assert_eq!(
        fs::read(daemon.directory.join("fixture.log")).unwrap(),
        original
    );
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_ready();
    assert_eq!(
        daemon
            .request("client.sock", br#"{"command":"status"}"#)
            .attempts,
        1
    );
    let duplicate = daemon.request("client.sock", &attempt);
    assert_eq!(duplicate.duplicate, Some(true));
    assert_eq!(duplicate.attempts, 2);
}

#[test]
fn endpoint_substitution_and_corrupt_restart_fail_closed() {
    let mut daemon = Daemon::start();
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    let endpoint = daemon.directory.join("supervisor.sock");
    fs::remove_file(&endpoint).unwrap();
    fs::write(&endpoint, b"not a socket").unwrap();
    assert!(daemon.clear_stale_sockets().is_err());
    assert!(daemon.directory.join("client.sock").exists());
    assert_eq!(fs::read(&endpoint).unwrap(), b"not a socket");
    fs::remove_file(&endpoint).unwrap();
    let _socket = std::os::unix::net::UnixListener::bind(&endpoint).unwrap();
    daemon.clear_stale_sockets().unwrap();
    fs::write(daemon.directory.join("fixture.log"), b"partial").unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_failure();
    assert_eq!(
        fs::read(daemon.directory.join("fixture.log")).unwrap(),
        b"partial"
    );
    assert!(!daemon.directory.join("client.sock").exists());
    assert!(!daemon.directory.join("supervisor.sock").exists());
}

#[test]
fn effects_commit_replay_and_reject_duplicates_over_ipc() {
    let mut daemon = Daemon::start();
    let effect = br#"{"command":"effect","attempt_sequence":1,"vmid":100,"before":null,"after":{"disk_bytes":80,"pe_configured":false}}"#;
    assert!(!daemon.request("client.sock", effect).ok);
    assert!(!daemon.request("supervisor.sock", effect).ok);
    let attempt = serde_json::to_vec(&serde_json::json!({"command":"attempt", "operation":Uuid::now_v7(), "request_sha256":"a".repeat(64)})).unwrap();
    assert_eq!(
        daemon.request("client.sock", &attempt).duplicate,
        Some(false)
    );
    let accepted = daemon.request("client.sock", effect);
    assert!(accepted.ok);
    assert_eq!(accepted.effects, 1);
    assert_eq!(
        accepted.vm,
        Some(VmState {
            disk_bytes: 80,
            pe_configured: false
        })
    );
    assert!(!daemon.request("client.sock", effect).ok);
    assert_eq!(
        daemon.request("client.sock", &attempt).duplicate,
        Some(true)
    );
    let duplicate_effect = br#"{"command":"effect","attempt_sequence":2,"vmid":100,"before":{"disk_bytes":80,"pe_configured":false},"after":{"disk_bytes":120,"pe_configured":true}}"#;
    assert!(!daemon.request("client.sock", duplicate_effect).ok);
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_ready();
    let world = daemon.request("client.sock", br#"{"command":"world","vmid":100}"#);
    assert!(world.ok);
    assert_eq!(world.attempts, 2);
    assert_eq!(world.effects, 1);
    assert_eq!(world.vm, accepted.vm);
    assert!(!daemon.request("client.sock", effect).ok);
    assert!(
        !daemon
            .request("supervisor.sock", br#"{"command":"world","vmid":100}"#)
            .ok
    );
    // A new operation may change only the exact recovered prior state.
    let next = serde_json::to_vec(&serde_json::json!({"command":"attempt", "operation":Uuid::now_v7(), "request_sha256":"b".repeat(64)})).unwrap();
    assert_eq!(daemon.request("client.sock", &next).attempts, 3);
    let stale = br#"{"command":"effect","attempt_sequence":3,"vmid":100,"before":null,"after":{"disk_bytes":120,"pe_configured":true}}"#;
    assert!(!daemon.request("client.sock", stale).ok);
    let transition = br#"{"command":"effect","attempt_sequence":3,"vmid":100,"before":{"disk_bytes":80,"pe_configured":false},"after":{"disk_bytes":120,"pe_configured":true}}"#;
    let updated = daemon.request("client.sock", transition);
    assert!(updated.ok);
    assert_eq!(updated.effects, 2);
    assert_eq!(
        updated.vm,
        Some(VmState {
            disk_bytes: 120,
            pe_configured: true
        })
    );
}

#[test]
fn accepted_effect_lookup_binds_request_and_survives_restart() {
    let mut daemon = Daemon::start();
    let operation = Uuid::now_v7();
    let digest = "a".repeat(64);
    let query = serde_json::to_vec(&serde_json::json!({"command":"accepted_effect", "operation":operation, "request_sha256":digest})).unwrap();
    let absent = daemon.request("client.sock", &query);
    assert!(absent.ok);
    assert!(absent.accepted_effect.is_none());
    assert!(!daemon.request("supervisor.sock", &query).ok);
    let attempt = serde_json::to_vec(
        &serde_json::json!({"command":"attempt", "operation":operation, "request_sha256":digest}),
    )
    .unwrap();
    assert!(daemon.request("client.sock", &attempt).ok);
    assert!(
        daemon
            .request("client.sock", &query)
            .accepted_effect
            .is_none()
    );
    let wrong = serde_json::to_vec(&serde_json::json!({"command":"accepted_effect", "operation":operation, "request_sha256":"b".repeat(64)})).unwrap();
    assert!(!daemon.request("client.sock", &wrong).ok);
    let effect = br#"{"command":"effect","attempt_sequence":1,"vmid":100,"before":null,"after":{"disk_bytes":80,"pe_configured":false}}"#;
    assert!(daemon.request("client.sock", effect).ok);
    let accepted = daemon.request("client.sock", &query);
    assert!(accepted.ok);
    let facts = serde_json::to_value(accepted.accepted_effect.as_ref().unwrap()).unwrap();
    assert_eq!(facts["operation"], operation.to_string());
    assert_eq!(facts["request_sha256"], digest);
    assert_eq!(facts["vmid"], 100);
    assert_eq!(facts["attempt_sequence"], 1);
    assert_eq!(facts["after"]["disk_bytes"], 80);
    daemon.child.kill().unwrap();
    daemon.child.wait().unwrap();
    daemon.clear_stale_sockets().unwrap();
    daemon.child = Daemon::spawn(&daemon.directory);
    daemon.await_ready();
    let recovered = daemon.request("client.sock", &query);
    assert!(recovered.ok);
    assert_eq!(recovered.accepted_effect, accepted.accepted_effect);
    assert_eq!(recovered.attempts, 1);
    assert_eq!(recovered.effects, 1);
    assert!(!daemon.request("client.sock", &wrong).ok);
}
