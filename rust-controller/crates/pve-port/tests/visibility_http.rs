use pve_port::{
    PveAccessMode, PveApiToken, PveBaseUrl, PveObserverConfig, PveReadError, PveRequestAudit,
    PveVisibilityReadPort, ReqwestPveObserver,
};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    task::JoinHandle,
};

struct Server {
    url: String,
    requests: Arc<Mutex<Vec<String>>>,
    task: Option<JoinHandle<()>>,
}
impl Server {
    async fn raw(response: Vec<u8>, delay: Duration) -> Self {
        Self::stream(response, delay, Duration::ZERO).await
    }
    async fn stream(response: Vec<u8>, delay: Duration, hold_open: Duration) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let requests = Arc::new(Mutex::new(Vec::new()));
        let captured = requests.clone();
        let task = tokio::spawn(async move {
            loop {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut request = Vec::new();
                while !request.windows(4).any(|v| v == b"\r\n\r\n") {
                    let mut chunk = [0; 1024];
                    let n = stream.read(&mut chunk).await.unwrap();
                    if n == 0 {
                        break;
                    }
                    request.extend_from_slice(&chunk[..n]);
                    assert!(request.len() < 16_384);
                }
                captured
                    .lock()
                    .unwrap()
                    .push(String::from_utf8(request).unwrap());
                tokio::time::sleep(delay).await;
                for chunk in response.chunks(8192) {
                    if stream.write_all(chunk).await.is_err() {
                        break;
                    }
                    tokio::task::yield_now().await;
                }
                tokio::time::sleep(hold_open).await;
            }
        });
        Self {
            url,
            requests,
            task: Some(task),
        }
    }
    async fn response(status: u16, body: &str) -> Self {
        Self::raw(format!("HTTP/1.1 {status} Fixture\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).into_bytes(), Duration::ZERO).await
    }
    fn observer(&self) -> ReqwestPveObserver {
        self.observer_with_audit(PveRequestAudit::new())
    }
    fn observer_with_audit(&self, audit: PveRequestAudit) -> ReqwestPveObserver {
        ReqwestPveObserver::new(
            PveObserverConfig::new(
                PveBaseUrl::parse(&self.url).unwrap(),
                PveAccessMode::Observe,
                false,
            )
            .with_api_token(PveApiToken::parse("observer@pve!fixture", "synthetic-secret").unwrap())
            .with_timeout(Duration::from_secs(2))
            .with_request_audit(audit),
        )
        .unwrap()
    }
    fn assert_one_get(&self) {
        let requests = self.requests.lock().unwrap();
        assert_eq!(requests.len(), 1);
        let lines: Vec<_> = requests[0].split("\r\n").collect();
        assert_eq!(
            lines[0],
            "GET /api2/json/cluster/resources?type=vm HTTP/1.1"
        );
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
    async fn finish(mut self) {
        let task = self.task.take().unwrap();
        task.abort();
        let result = task.await;
        assert!(result.is_ok() || result.unwrap_err().is_cancelled());
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        if let Some(task) = &self.task {
            task.abort();
        }
    }
}

#[tokio::test]
async fn authenticated_inventory_is_exactly_one_get_and_timestamped_after_collection() {
    let body = r#"{"data":[{"vmid":101,"node":"fixture","type":"qemu"},{"vmid":102,"node":"fixture","type":"lxc"}]}"#;
    let server = Server::raw(
        format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        )
        .into_bytes(),
        Duration::from_millis(50),
    )
    .await;
    let before = chrono::Utc::now();
    let report = server.observer().cluster_visibility().await.unwrap();
    assert_eq!(report.records().len(), 2);
    assert_eq!(report.coverage(), "unverified");
    assert!(
        report.observed_at() >= before + chrono::Duration::milliseconds(40)
            && report.observed_at() <= chrono::Utc::now()
    );
    server.assert_one_get();
    server.finish().await;
}

#[tokio::test]
async fn http_failures_are_fixed_categories_without_retry() {
    for (status, expected) in [
        (401, PveReadError::Unauthorized),
        (403, PveReadError::Unauthorized),
        (404, PveReadError::NotFound),
        (500, PveReadError::TransportUnavailable),
    ] {
        let server = Server::response(status, "synthetic-private-error").await;
        assert_eq!(
            server.observer().cluster_visibility().await.unwrap_err(),
            expected
        );
        server.assert_one_get();
        server.finish().await;
    }
}

#[tokio::test]
async fn invalid_envelopes_data_and_row_limit_are_rejected() {
    let too_many = serde_json::json!({"data": vec![serde_json::json!({}); 1025]}).to_string();
    for body in [
        "broken",
        "{}",
        r#"{"data":null}"#,
        r#"{"data":{}}"#,
        &too_many,
    ] {
        let server = Server::response(200, body).await;
        assert_eq!(
            server.observer().cluster_visibility().await.unwrap_err(),
            PveReadError::InvalidResponse
        );
        server.assert_one_get();
        server.finish().await;
    }
}

#[tokio::test]
async fn rejected_rows_remain_successful_non_authoritative_visibility() {
    let server = Server::response(
        200,
        r#"{"data":[{}, {"vmid":101,"node":"fixture","type":"future"}]}"#,
    )
    .await;
    let report = server.observer().cluster_visibility().await.unwrap();
    assert_eq!(report.rejected_rows(), 1);
    assert_eq!(report.records()[0].kind(), pve_port::GuestKind::Unsupported);
    server.assert_one_get();
    server.finish().await;
}

#[tokio::test]
async fn body_byte_limit_is_enforced_for_declared_chunked_and_eof_streams() {
    let body = format!(r#"{{"data":[],"padding":"{}"}}"#, "x".repeat(1_048_576));
    for response in [
        b"HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\n\r\n".to_vec(),
        format!(
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n{:X}\r\n{body}\r\n0\r\n\r\n",
            body.len()
        )
        .into_bytes(),
        format!("HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{body}").into_bytes(),
    ] {
        let server = Server::raw(response, Duration::ZERO).await;
        assert_eq!(
            server.observer().cluster_visibility().await.unwrap_err(),
            PveReadError::InvalidResponse
        );
        server.assert_one_get();
        server.finish().await;
    }
}

#[tokio::test]
async fn exactly_one_mib_body_is_accepted() {
    let prefix = r#"{"data":[],"padding":""}"#;
    let body = format!(
        r#"{{"data":[],"padding":"{}"}}"#,
        "x".repeat(1_048_576 - prefix.len())
    );
    assert_eq!(body.len(), 1_048_576);
    let server = Server::response(200, &body).await;
    assert!(
        server
            .observer()
            .cluster_visibility()
            .await
            .unwrap()
            .records()
            .is_empty()
    );
    server.assert_one_get();
    server.finish().await;
}

#[tokio::test]
async fn request_times_out_after_two_seconds_without_retry() {
    let server = Server::raw(Vec::new(), Duration::from_secs(10)).await;
    let started = std::time::Instant::now();
    assert_eq!(
        server.observer().cluster_visibility().await.unwrap_err(),
        PveReadError::TimedOut
    );
    assert!(started.elapsed() >= Duration::from_millis(1800));
    assert!(started.elapsed() < Duration::from_secs(3));
    server.assert_one_get();
    server.finish().await;
}

#[tokio::test]
async fn request_deadline_also_bounds_an_unfinished_streaming_body() {
    let server = Server::stream(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\n{\r\n".to_vec(),
        Duration::ZERO,
        Duration::from_secs(10),
    )
    .await;
    let started = std::time::Instant::now();
    assert_eq!(
        server.observer().cluster_visibility().await.unwrap_err(),
        PveReadError::TimedOut
    );
    assert!(
        started.elapsed() >= Duration::from_millis(1800)
            && started.elapsed() < Duration::from_secs(3)
    );
    server.assert_one_get();
    server.finish().await;
}

#[tokio::test]
async fn dropped_collection_does_not_start_another_request() {
    let server = Server::raw(Vec::new(), Duration::from_secs(10)).await;
    let audit = PveRequestAudit::new();
    let observer = server.observer_with_audit(audit.clone());
    let collection = tokio::spawn(async move { observer.cluster_visibility().await });
    tokio::time::timeout(Duration::from_secs(1), async {
        while server.requests.lock().unwrap().is_empty() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    collection.abort();
    assert!(collection.await.unwrap_err().is_cancelled());
    // Observe beyond the original request deadline: no background retry is issued.
    tokio::time::sleep(Duration::from_millis(2100)).await;
    assert_eq!(audit.request_count(), 1);
    server.assert_one_get();
    server.finish().await;
}

#[tokio::test]
async fn redirect_target_receives_no_request() {
    let canary = Server::response(200, r#"{"data":[]}"#).await;
    let server = Server::raw(
        format!(
            "HTTP/1.1 302 Found\r\nLocation: {}\r\nContent-Length: 0\r\n\r\n",
            canary.url
        )
        .into_bytes(),
        Duration::ZERO,
    )
    .await;
    assert_eq!(
        server.observer().cluster_visibility().await.unwrap_err(),
        PveReadError::TransportUnavailable
    );
    server.assert_one_get();
    assert!(canary.requests.lock().unwrap().is_empty());
    server.finish().await;
    canary.finish().await;
}

#[test]
fn proxy_environment_canary_receives_no_request() {
    if std::env::var_os("VISIBILITY_PROXY_CHILD").is_some() {
        return;
    }
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
        let mut child = ChildGuard(
            std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "authenticated_inventory_is_exactly_one_get_and_timestamped_after_collection",
                    "--nocapture",
                ])
                .env("VISIBILITY_PROXY_CHILD", "1")
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
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        loop {
            if let Some(status) = child.0.try_wait().unwrap() {
                assert!(status.success());
                break;
            }
            if std::time::Instant::now() >= deadline {
                panic!("proxy child deadline");
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert!(proxy.requests.lock().unwrap().is_empty());
        proxy.finish().await;
    });
}
