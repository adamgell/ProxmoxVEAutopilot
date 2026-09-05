use std::{
    io::Read,
    net::{TcpListener, TcpStream},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

// Own only the spawned service; panic/timeout also kills and reaps that child.
struct ServiceChild(Child);
impl Drop for ServiceChild {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let deadline = Instant::now() + Duration::from_millis(200);
        while Instant::now() < deadline {
            if matches!(self.0.try_wait(), Ok(Some(_))) {
                return;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        panic!("owned service child cleanup unconfirmed");
    }
}

fn denied_startup(mode: &str, transport: &str, db: Option<&str>, pve: Option<&str>, allow: bool) {
    // Break: an activation/target guard moves after DB access or listener startup.
    // Denied destinations are never probed. Only these owned loopback canaries are observed.
    let database = TcpListener::bind("127.0.0.1:0").unwrap();
    let pve_listener = TcpListener::bind("127.0.0.1:0").unwrap();
    database.set_nonblocking(true).unwrap();
    pve_listener.set_nonblocking(true).unwrap();
    let health_reservation = TcpListener::bind("127.0.0.1:0").unwrap();
    let health = health_reservation.local_addr().unwrap();
    let local_db = format!(
        "postgresql://startup-canary@{}/proof",
        database.local_addr().unwrap()
    );
    let local_pve = format!("http://{}", pve_listener.local_addr().unwrap());
    drop(health_reservation);
    let mut child = ServiceChild(
        Command::new(env!("CARGO_BIN_EXE_controller-service"))
            .env_clear()
            .env("RUST_CONTROLLER_MODE", mode)
            .env("RUST_CONTROLLER_PVE_TRANSPORT", transport)
            .env("RUST_CONTROLLER_DATABASE_URL", db.unwrap_or(&local_db))
            .env("RUST_CONTROLLER_PVE_BASE_URL", pve.unwrap_or(&local_pve))
            .env("RUST_CONTROLLER_ALLOW_PRODUCTION_READS", allow.to_string())
            .env("RUST_CONTROLLER_AUTHORITY_GENERATION", "1")
            .env("RUST_CONTROLLER_LISTEN", health.to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(2);
    let status = loop {
        assert!(
            database.accept().is_err(),
            "startup touched owned DB canary"
        );
        assert!(
            pve_listener.accept().is_err(),
            "startup touched owned PVE canary"
        );
        assert!(
            TcpStream::connect_timeout(&health, Duration::from_millis(10)).is_err(),
            "startup exposed health listener"
        );
        if let Some(status) = child.0.try_wait().unwrap() {
            break status;
        }
        assert!(
            Instant::now() < deadline,
            "denied startup did not exit within two seconds"
        );
        std::thread::sleep(Duration::from_millis(5));
    };
    assert_eq!(status.code(), Some(1));
    let mut stderr = String::new();
    child
        .0
        .stderr
        .take()
        .unwrap()
        .take(1024)
        .read_to_string(&mut stderr)
        .unwrap();
    assert!(
        stderr
            == "controller startup or runtime failed; check local configuration and dependencies\n",
        "startup error was not the fixed sanitized label"
    );
}

#[test]
fn native_startup_fails_before_local_io_even_with_read_permission() {
    for allow in [false, true] {
        denied_startup("native", "fake", None, None, allow);
    }
}

#[test]
fn real_transport_startup_fails_before_local_io_even_with_read_permission() {
    for allow in [false, true] {
        denied_startup("observe", "real", None, None, allow);
    }
}

#[test]
fn remote_pve_and_database_startup_targets_are_denied() {
    for target in ["192.0.2.1", "[::ffff:192.0.2.1]", "denied-pve.invalid"] {
        let db = format!("postgresql://startup-canary@{target}:5432/proof");
        let pve = format!("https://{target}:8006");
        for (mode, allow) in [
            ("observe", false),
            ("adapter", false),
            ("adapter", true),
            ("native", true),
        ] {
            denied_startup(mode, "fake", Some(&db), None, allow);
            denied_startup(mode, "fake", None, Some(&pve), allow);
        }
    }
}

#[tokio::test]
async fn live_server_survives_database_failure_and_sanitizes_readiness() {
    // Break: startup connects before binding, or HTTP liveness depends on DB.
    let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let address = socket.local_addr().unwrap();
    drop(socket);
    let mut child = Command::new(env!("CARGO_BIN_EXE_controller-service"))
        .env_clear()
        .env("RUST_CONTROLLER_MODE", "observe")
        .env(
            "RUST_CONTROLLER_DATABASE_URL",
            "postgresql://secret-canary:secret-canary@127.0.0.1:1/secret-canary",
        )
        .env("RUST_CONTROLLER_PVE_BASE_URL", "http://127.0.0.1:1")
        .env("RUST_CONTROLLER_AUTHORITY_GENERATION", "1")
        .env("RUST_CONTROLLER_LISTEN", address.to_string())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let client = reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(4))
        .build()
        .unwrap();
    let result = tokio::time::timeout(Duration::from_secs(8), async {
        loop {
            if let Ok(response) = client.get(format!("http://{address}/healthz")).send().await {
                assert_eq!(response.status().as_u16(), 200);
                let ready = client
                    .get(format!("http://{address}/readyz"))
                    .send()
                    .await
                    .unwrap();
                assert_eq!(ready.status().as_u16(), 503);
                let text = ready.text().await.unwrap();
                assert!(!text.contains("secret-canary"));
                let body: serde_json::Value = serde_json::from_str(&text).unwrap();
                assert_eq!(body["database"], false);
                assert!(body["reconciler_last_success"].is_null());
                break;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    })
    .await;
    let _ = child.kill();
    let _ = child.wait();
    assert!(
        result.is_ok(),
        "health server did not stay live without a database"
    );
}
