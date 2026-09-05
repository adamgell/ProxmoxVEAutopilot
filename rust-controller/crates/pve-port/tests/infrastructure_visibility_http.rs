use pve_port::{
    InterfaceKind, NodeName, PveAccessMode, PveApiToken, PveBaseUrl,
    PveInfrastructureVisibilityReadPort, PveObserverConfig, PveReadError, PveRequestAudit,
    ReqwestPveObserver,
};
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    sync::oneshot,
    task::{JoinHandle, JoinSet},
};

// Every connection belongs to the listener guard; finish cancels and joins all tasks.
struct Server {
    url: String,
    requests: Arc<Mutex<Vec<String>>>,
    closed: Arc<AtomicUsize>,
    stop: Option<oneshot::Sender<()>>,
    task: Option<JoinHandle<()>>,
}

impl Server {
    async fn raw(response: Vec<u8>, delay: Duration, unfinished: bool) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let requests = Arc::new(Mutex::new(Vec::new()));
        let captured = requests.clone();
        let closed = Arc::new(AtomicUsize::new(0));
        let disconnected = closed.clone();
        let (stop, mut stopped) = oneshot::channel();
        let task = tokio::spawn(async move {
            let mut connections = JoinSet::new();
            loop {
                tokio::select! {
                    _ = &mut stopped => break,
                    result = connections.join_next(), if !connections.is_empty() => {
                        result.unwrap().unwrap();
                    }
                    accepted = listener.accept() => {
                        let (mut stream, _) = accepted.unwrap();
                        let captured = captured.clone();
                        let disconnected = disconnected.clone();
                        let response = response.clone();
                        connections.spawn(async move {
                            let mut request = Vec::new();
                            while !request.windows(4).any(|v| v == b"\r\n\r\n") {
                                let mut chunk = [0; 1024];
                                let n = stream.read(&mut chunk).await.unwrap();
                                assert!(n > 0);
                                request.extend_from_slice(&chunk[..n]);
                                assert!(request.len() < 16_384);
                            }
                            captured.lock().unwrap().push(String::from_utf8(request).unwrap());
                            let mut byte = [0; 1];
                            tokio::select! {
                                _ = tokio::time::sleep(delay) => {},
                                read = stream.read(&mut byte) => {
                                    assert!(matches!(read, Ok(0) | Err(_)));
                                    disconnected.fetch_add(1, Ordering::SeqCst);
                                    return;
                                }
                            }
                            for chunk in response.chunks(8192) {
                                if stream.write_all(chunk).await.is_err() {
                                    disconnected.fetch_add(1, Ordering::SeqCst);
                                    return;
                                }
                                tokio::task::yield_now().await;
                            }
                            if unfinished {
                                let read = stream.read(&mut byte).await;
                                assert!(matches!(read, Ok(0) | Err(_)));
                                disconnected.fetch_add(1, Ordering::SeqCst);
                            }
                        });
                    }
                }
            }
            connections.abort_all();
            while let Some(result) = connections.join_next().await {
                assert!(result.is_ok() || result.unwrap_err().is_cancelled());
            }
        });
        Self {
            url,
            requests,
            closed,
            stop: Some(stop),
            task: Some(task),
        }
    }

    async fn response(status: u16, body: &str) -> Self {
        Self::raw(format!("HTTP/1.1 {status} Fixture\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).into_bytes(), Duration::ZERO, false).await
    }

    fn observer(&self, audit: PveRequestAudit) -> ReqwestPveObserver {
        ReqwestPveObserver::new(
            PveObserverConfig::new(
                PveBaseUrl::parse(&self.url).unwrap(),
                PveAccessMode::Observe,
                false,
            )
            .with_api_token(PveApiToken::parse("observer@pve!fixture", "synthetic-secret").unwrap())
            .with_timeout(Duration::from_secs(15))
            .with_request_audit(audit),
        )
        .unwrap()
    }

    fn assert_gets(&self, leaves: &[&str]) {
        let requests = self.requests.lock().unwrap();
        assert_eq!(requests.len(), leaves.len());
        for (request, leaf) in requests.iter().zip(leaves) {
            assert!(request.starts_with(&format!(
                "GET /api2/json/nodes/pve-test/{leaf} HTTP/1.1\r\n"
            )));
            let lines: Vec<_> = request.split("\r\n").collect();
            let auth: Vec<_> = lines
                .iter()
                .filter(|line| line.to_ascii_lowercase().starts_with("authorization:"))
                .collect();
            assert_eq!(auth.len(), 1);
            assert_eq!(
                auth[0].split_once(':').unwrap().1.trim(),
                "PVEAPIToken=observer@pve!fixture=synthetic-secret"
            );
            assert!(
                lines
                    .iter()
                    .filter(|line| !line.to_ascii_lowercase().starts_with("authorization:"))
                    .all(|line| !line.contains("synthetic-secret"))
            );
        }
    }

    async fn wait_closed(&self) {
        tokio::time::timeout(Duration::from_secs(1), async {
            while self.closed.load(Ordering::SeqCst) == 0 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("collection must drop the connection");
    }

    async fn finish(mut self) {
        let _ = self.stop.take().unwrap().send(());
        self.task.take().unwrap().await.unwrap();
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        if let Some(task) = &self.task {
            task.abort();
        }
    }
}

async fn collect(
    port: &dyn PveInfrastructureVisibilityReadPort,
    leaf: &str,
) -> Result<(), PveReadError> {
    let node = NodeName::parse("pve-test").unwrap();
    match leaf {
        "status" => port.node_visibility(&node).await.map(|_| ()),
        "network" => port.network_visibility(&node).await.map(|_| ()),
        _ => panic!("fixture leaf"),
    }
}

#[tokio::test]
async fn authenticated_node_zero_uptime_is_one_timestamped_unverified_get() {
    let body = r#"{"data":{"node":"pve-test","uptime":0,"private":"discard-me"}}"#;
    let server = Server::raw(
        format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .into_bytes(),
        Duration::from_millis(50),
        false,
    )
    .await;
    let audit = PveRequestAudit::new();
    let observer = server.observer(audit.clone());
    let port: &dyn PveInfrastructureVisibilityReadPort = &observer;
    let before = chrono::Utc::now();
    let report = port
        .node_visibility(&NodeName::parse("pve-test").unwrap())
        .await
        .unwrap();
    assert_eq!(report.uptime(), Some(0));
    assert_eq!(report.coverage(), "unverified");
    assert!(report.observed_at() >= before + chrono::Duration::milliseconds(40));
    assert!(report.observed_at() <= chrono::Utc::now());
    assert!(
        !serde_json::to_string(&report)
            .unwrap()
            .contains("discard-me")
    );
    assert_eq!(audit.request_count(), 1);
    server.assert_gets(&["status"]);
    server.finish().await;
}

#[tokio::test]
async fn authenticated_network_preserves_ovs_missing_activity_and_rejected_rows() {
    let server = Server::response(200, r#"{"data":[{"iface":"vmbr0","type":"bridge","active":0},{"iface":"ovs0","type":"OVSBridge"},{}]}"#).await;
    let audit = PveRequestAudit::new();
    let observer = server.observer(audit.clone());
    let port: &dyn PveInfrastructureVisibilityReadPort = &observer;
    let before = chrono::Utc::now();
    let report = port
        .network_visibility(&NodeName::parse("pve-test").unwrap())
        .await
        .unwrap();
    assert_eq!(report.records().len(), 2);
    assert_eq!(report.records()[0].active(), Some(false));
    assert_eq!(report.records()[1].kind(), InterfaceKind::OvsBridge);
    assert_eq!(report.records()[1].active(), None);
    assert_eq!(report.rejected_rows(), 1);
    assert_eq!(report.coverage(), "unverified");
    assert!(report.observed_at() >= before && report.observed_at() <= chrono::Utc::now());
    assert_eq!(audit.request_count(), 1);
    server.assert_gets(&["network"]);
    server.finish().await;
}

async fn error_both(status: u16, body: &str, expected: PveReadError) {
    for leaf in ["status", "network"] {
        let server = Server::response(status, body).await;
        let audit = PveRequestAudit::new();
        assert_eq!(
            collect(&server.observer(audit.clone()), leaf)
                .await
                .unwrap_err(),
            expected
        );
        assert_eq!(audit.request_count(), 1);
        server.assert_gets(&[leaf]);
        server.finish().await;
    }
}

#[tokio::test]
async fn unauthorized_401_is_fixed_and_not_retried() {
    error_both(401, "private-error", PveReadError::Unauthorized).await;
}
#[tokio::test]
async fn forbidden_403_is_fixed_and_not_retried() {
    error_both(403, "private-error", PveReadError::Unauthorized).await;
}
#[tokio::test]
async fn missing_404_is_fixed_and_not_retried() {
    error_both(404, "private-error", PveReadError::NotFound).await;
}
#[tokio::test]
async fn conflict_409_is_fixed_and_not_retried() {
    error_both(409, "private-error", PveReadError::Conflict).await;
}
#[tokio::test]
async fn server_500_is_fixed_and_not_retried() {
    error_both(500, "private-error", PveReadError::TransportUnavailable).await;
}

#[tokio::test]
async fn malformed_envelopes_are_rejected() {
    for body in ["broken", "{}", "[]", r#"{"data":null}"#, r#"{"data":true}"#] {
        error_both(200, body, PveReadError::InvalidResponse).await;
    }
}

async fn invalid_data(leaf: &str, body: &str) {
    let server = Server::response(200, body).await;
    assert_eq!(
        collect(&server.observer(PveRequestAudit::new()), leaf)
            .await
            .unwrap_err(),
        PveReadError::InvalidResponse
    );
    server.assert_gets(&[leaf]);
    server.finish().await;
}

#[tokio::test]
async fn invalid_node_data_is_rejected() {
    for body in [
        r#"{"data":[]}"#,
        r#"{"data":{"uptime":null}}"#,
        r#"{"data":{"uptime":-1}}"#,
        r#"{"data":{"node":"other"}}"#,
    ] {
        invalid_data("status", body).await;
    }
}
#[tokio::test]
async fn invalid_network_shape_is_rejected() {
    invalid_data("network", r#"{"data":{}}"#).await;
}
#[tokio::test]
async fn duplicate_interface_after_rejected_metadata_rejects_report() {
    invalid_data("network", r#"{"data":[{"iface":"ovs0","type":"OVSBridge","active":null},{"iface":"ovs0","type":"OVSBridge"}]}"#).await;
}
#[tokio::test]
async fn cross_node_network_row_rejects_report() {
    invalid_data(
        "network",
        r#"{"data":[{"iface":"ovs0","type":"OVSBridge","node":"other"}]}"#,
    )
    .await;
}
#[tokio::test]
async fn network_1025_rows_is_rejected() {
    invalid_data(
        "network",
        &serde_json::json!({"data":vec![serde_json::json!({});1025]}).to_string(),
    )
    .await;
}

async fn oversized(response: impl Fn(&str) -> Vec<u8>) {
    for (leaf, data) in [("status", "{}"), ("network", "[]")] {
        // The complete envelope would be valid without the byte cap, so these
        // tests distinguish size enforcement from a JSON/parser rejection.
        let mut body = format!("{{\"data\":{data}}}");
        body.push_str(&" ".repeat(1_048_577 - body.len()));
        let server = Server::raw(response(&body), Duration::ZERO, false).await;
        assert_eq!(
            collect(&server.observer(PveRequestAudit::new()), leaf)
                .await
                .unwrap_err(),
            PveReadError::InvalidResponse
        );
        server.assert_gets(&[leaf]);
        server.finish().await;
    }
}
#[tokio::test]
async fn declared_oversize_is_rejected_before_reading_body() {
    for leaf in ["status", "network"] {
        let server = Server::raw(
            b"HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\n\r\n".to_vec(),
            Duration::ZERO,
            true,
        )
        .await;
        assert_eq!(
            collect(&server.observer(PveRequestAudit::new()), leaf)
                .await
                .unwrap_err(),
            PveReadError::InvalidResponse,
        );
        server.wait_closed().await;
        server.assert_gets(&[leaf]);
        server.finish().await;
    }
}
#[tokio::test]
async fn chunked_oversize_is_rejected() {
    oversized(|body| {
        format!(
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n{:X}\r\n{body}\r\n0\r\n\r\n",
            body.len()
        )
        .into_bytes()
    })
    .await;
}
#[tokio::test]
async fn eof_oversize_is_rejected() {
    oversized(|body| format!("HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{body}").into_bytes())
        .await;
}

#[tokio::test]
async fn exactly_one_mib_valid_bodies_are_accepted() {
    for (leaf, data) in [("status", "{}"), ("network", "[]")] {
        let mut body = format!("{{\"data\":{data}}}");
        body.push_str(&" ".repeat(1_048_576 - body.len()));
        let server = Server::response(200, &body).await;
        collect(&server.observer(PveRequestAudit::new()), leaf)
            .await
            .unwrap();
        server.assert_gets(&[leaf]);
        server.finish().await;
    }
}

async fn deadline(response: Vec<u8>, delay: Duration) {
    for leaf in ["status", "network"] {
        let server = Server::raw(response.clone(), delay, true).await;
        let audit = PveRequestAudit::new();
        let observer = server.observer(audit.clone());
        let started = Instant::now();
        let result = tokio::time::timeout(Duration::from_secs(4), collect(&observer, leaf)).await;
        assert_eq!(
            result
                .expect("two-second method bound despite 15-second client timeout")
                .unwrap_err(),
            PveReadError::TimedOut
        );
        assert!(started.elapsed() >= Duration::from_millis(1800));
        assert!(started.elapsed() < Duration::from_secs(3));
        server.wait_closed().await;
        assert_eq!(audit.request_count(), 1);
        server.assert_gets(&[leaf]);
        server.finish().await;
    }
}
#[tokio::test]
async fn request_deadline_overrides_larger_client_timeout() {
    deadline(Vec::new(), Duration::from_secs(10)).await;
}
#[tokio::test]
async fn whole_body_deadline_includes_time_already_spent_waiting_for_headers() {
    deadline(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\n{\r\n".to_vec(),
        Duration::from_millis(1200),
    )
    .await;
}

#[tokio::test]
async fn cancelling_collection_closes_body_and_does_not_retry() {
    for leaf in ["status", "network"] {
        let server = Server::raw(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\n{\r\n".to_vec(),
            Duration::ZERO,
            true,
        )
        .await;
        let audit = PveRequestAudit::new();
        let observer = server.observer(audit.clone());
        let collection = tokio::spawn(async move { collect(&observer, leaf).await });
        tokio::time::timeout(Duration::from_secs(1), async {
            while server.requests.lock().unwrap().is_empty() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        // Allow response headers to arrive so cancellation covers a body read.
        tokio::time::sleep(Duration::from_millis(50)).await;
        collection.abort();
        assert!(collection.await.unwrap_err().is_cancelled());
        server.wait_closed().await;
        tokio::time::sleep(Duration::from_millis(2100)).await;
        assert_eq!(audit.request_count(), 1);
        server.assert_gets(&[leaf]);
        server.finish().await;
    }
}

#[tokio::test]
async fn redirects_never_contact_target_or_forward_credentials() {
    let canary = Server::response(200, r#"{"data":[]}"#).await;
    for leaf in ["status", "network"] {
        let server = Server::raw(
            format!(
                "HTTP/1.1 302 Found\r\nLocation: {}/forbidden\r\nContent-Length: 0\r\n\r\n",
                canary.url
            )
            .into_bytes(),
            Duration::ZERO,
            false,
        )
        .await;
        assert_eq!(
            collect(&server.observer(PveRequestAudit::new()), leaf)
                .await
                .unwrap_err(),
            PveReadError::TransportUnavailable
        );
        server.assert_gets(&[leaf]);
        server.finish().await;
    }
    canary.assert_gets(&[]);
    canary.finish().await;
}

#[test]
fn proxy_environment_never_receives_node_or_network_requests() {
    let runtime = tokio::runtime::Runtime::new().unwrap();
    runtime.block_on(async {
        let proxy = Server::response(500, "proxy canary").await;
        struct ChildGuard(std::process::Child);
        impl Drop for ChildGuard {
            fn drop(&mut self) {
                let _ = self.0.kill();
                let _ = self.0.wait();
            }
        }
        for name in [
            "authenticated_node_zero_uptime_is_one_timestamped_unverified_get",
            "authenticated_network_preserves_ovs_missing_activity_and_rejected_rows",
        ] {
            let mut child = ChildGuard(
                std::process::Command::new(std::env::current_exe().unwrap())
                    .args(["--exact", name, "--nocapture"])
                    .env("HTTP_PROXY", &proxy.url)
                    .env("HTTPS_PROXY", &proxy.url)
                    .env("ALL_PROXY", &proxy.url)
                    .env("http_proxy", &proxy.url)
                    .env("https_proxy", &proxy.url)
                    .env("all_proxy", &proxy.url)
                    .env("NO_PROXY", "")
                    .env("no_proxy", "")
                    .spawn()
                    .unwrap(),
            );
            let deadline = Instant::now() + Duration::from_secs(10);
            loop {
                if let Some(status) = child.0.try_wait().unwrap() {
                    assert!(status.success());
                    break;
                }
                assert!(Instant::now() < deadline, "proxy child deadline");
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        }
        proxy.assert_gets(&[]);
        proxy.finish().await;
    });
}
