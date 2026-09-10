#![cfg(unix)]
#[allow(dead_code)]
#[path = "support/durable_fixture_log.rs"]
mod durable_fixture_log;
#[path = "support/fixture_daemon.rs"]
mod fixture_daemon;

use std::{
    fs,
    io::{Read, Write},
    os::unix::{fs::PermissionsExt, net::UnixStream},
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
    fn start() -> Self {
        // macOS temp_dir paths can exceed sockaddr_un's small pathname limit.
        let directory = PathBuf::from("/tmp").join(format!("pve-ipc-{}", Uuid::now_v7()));
        fs::create_dir(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
        let child = Command::new(std::env::current_exe().unwrap())
            .args(["--ignored", "--exact", "fixture_daemon_child"])
            .env("PVE_TEST_FIXTURE_DIR", &directory)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let mut daemon = Self { directory, child };
        let deadline = Instant::now() + Duration::from_secs(3);
        while !daemon.directory.join("client.sock").exists()
            || !daemon.directory.join("supervisor.sock").exists()
        {
            assert!(
                daemon.child.try_wait().unwrap().is_none(),
                "fixture exited during startup"
            );
            assert!(Instant::now() < deadline, "fixture startup timed out");
            thread::sleep(Duration::from_millis(5));
        }
        daemon
    }
    fn request(&self, socket: &str, payload: &[u8]) -> fixture_daemon::Reply {
        let mut stream = UnixStream::connect(self.directory.join(socket)).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
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

#[test]
#[ignore = "subprocess entry point only"]
fn fixture_daemon_child() {
    let path = PathBuf::from(std::env::var_os("PVE_TEST_FIXTURE_DIR").expect("fixture directory"));
    fixture_daemon::run(&path, Duration::from_secs(5)).unwrap();
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
