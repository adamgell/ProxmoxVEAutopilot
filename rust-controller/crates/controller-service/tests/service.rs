use std::{
    process::{Command, Stdio},
    time::Duration,
};

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
