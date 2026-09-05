//! Owned selected-node HTTP service proof; database and audit policy stay private in the parent.
use super::{Database, ServiceChild, assert_no_service_children, audit_read_only_trace};
use axum::{
    Json, Router,
    extract::State,
    http::{HeaderMap, Method, StatusCode, Uri},
    routing::any,
};
use serde_json::{Value, json};
use std::{
    net::{SocketAddr, TcpListener, TcpStream},
    os::unix::fs::PermissionsExt,
    path::Path,
    process::{Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

#[derive(Clone, Default)]
struct Requests {
    cluster: Arc<AtomicUsize>,
    node: Arc<AtomicUsize>,
    network: Arc<AtomicUsize>,
    rejected: Arc<AtomicUsize>,
}
struct HttpFixture {
    address: SocketAddr,
    requests: Requests,
    task: tokio::task::JoinHandle<()>,
}
impl Drop for HttpFixture {
    fn drop(&mut self) {
        self.task.abort();
    }
}
impl HttpFixture {
    async fn new() -> Self {
        async fn respond(
            State(r): State<Requests>,
            method: Method,
            uri: Uri,
            headers: HeaderMap,
        ) -> (StatusCode, Json<Value>) {
            let authorized = method == Method::GET
                && headers.get("authorization").and_then(|v| v.to_str().ok())
                    == Some("PVEAPIToken=observer@pve!fixture=synthetic-secret");
            let route = match (uri.path(), uri.query()) {
                ("/api2/json/cluster/resources", Some("type=vm")) => Some((&r.cluster, 6)),
                ("/api2/json/nodes/pve-test/status", None) => Some((&r.node, 3)),
                ("/api2/json/nodes/pve-test/network", None) => Some((&r.network, 3)),
                _ => None,
            };
            if !authorized || route.is_none() {
                r.rejected.fetch_add(1, Ordering::SeqCst);
                return (StatusCode::BAD_REQUEST, Json(json!({})));
            }
            let (counter, bound) = route.unwrap();
            let count = counter.fetch_add(1, Ordering::SeqCst) + 1;
            if count > bound {
                r.rejected.fetch_add(1, Ordering::SeqCst);
                return (StatusCode::TOO_MANY_REQUESTS, Json(json!({})));
            }
            let data = match uri.path() {
                "/api2/json/cluster/resources" => json!([
                    {"vmid":90101,"node":"private-node","type":"qemu","name":"private-vm","status":"running"}
                ]),
                "/api2/json/nodes/pve-test/status" => json!({"uptime":0}),
                _ if count == 2 => {
                    return (
                        StatusCode::UNAUTHORIZED,
                        Json(json!({"secret":"forbidden-body"})),
                    );
                }
                _ => json!([
                    {"iface":"private-bridge","type":"bridge","active":1,"address":"192.0.2.12"},
                    {"iface":"private-ovs","type":"OVSBridge","active":0},
                    {"iface":"private-other","type":"private-future-kind"}
                ]),
            };
            (StatusCode::OK, Json(json!({"data":data})))
        }
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let requests = Requests::default();
        let app = Router::new()
            .fallback(any(respond))
            .with_state(requests.clone());
        let task = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        Self {
            address,
            requests,
            task,
        }
    }
}

fn token(directory: &Path) -> std::path::PathBuf {
    let token = directory.join("synthetic-selected-token.json");
    std::fs::write(
        &token,
        r#"{"token_id":"observer@pve!fixture","secret":"synthetic-secret"}"#,
    )
    .unwrap();
    std::fs::set_permissions(&token, std::fs::Permissions::from_mode(0o600)).unwrap();
    token
}
fn start_service(
    db: &str,
    http: &HttpFixture,
    token: &Path,
    transport: &str,
    node: &str,
) -> (ServiceChild, SocketAddr) {
    let reservation = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = reservation.local_addr().unwrap();
    drop(reservation);
    let child = Command::new(env!("CARGO_BIN_EXE_controller-service"))
        .env_clear()
        .env("RUST_CONTROLLER_MODE", "observe")
        .env("RUST_CONTROLLER_DATABASE_URL", db)
        .env(
            "RUST_CONTROLLER_PVE_BASE_URL",
            format!("http://{}", http.address),
        )
        .env("RUST_CONTROLLER_PVE_TRANSPORT", transport)
        .env("RUST_CONTROLLER_PVE_OBSERVE_NODE", node)
        .env("RUST_CONTROLLER_PVE_TOKEN_FILE", token)
        .env("RUST_CONTROLLER_AUTHORITY_GENERATION", "1")
        .env("RUST_CONTROLLER_LISTEN", address.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    (ServiceChild(child), address)
}
fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(3))
        .build()
        .unwrap()
}
async fn wait_health(
    client: &reqwest::Client,
    address: SocketAddr,
    child: &mut ServiceChild,
    network_status: &str,
    network_success: Option<&Value>,
) -> Value {
    tokio::time::timeout(Duration::from_secs(12), async {
        loop {
            assert!(
                child.0.try_wait().unwrap().is_none(),
                "selected-node service exited"
            );
            if let Ok(response) = client.get(format!("http://{address}/readyz")).send().await {
                assert_eq!(response.status().as_u16(), 503);
                let body: Value = response.json().await.unwrap();
                assert!(
                    body.get("infrastructure_observation").is_some(),
                    "runtime has no selected-node health field"
                );
                if body["observation_ready"] == true
                    && body["infrastructure_observation"]["network"]["status"] == network_status
                    && network_success.is_none_or(|old| {
                        &body["infrastructure_observation"]["network"]["last_success"] != old
                    })
                {
                    return body;
                }
            }
            // Poll at most once per second: complete SQL capture keeps its existing cap.
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
    })
    .await
    .expect("selected-node health did not reach expected state")
}
fn assert_health(body: &Value, ready: bool, database: bool) {
    assert_eq!(body["git_sha"], env!("CONTROLLER_GIT_SHA"));
    assert_eq!(body["observation_ready"], true);
    assert_eq!(body["pve_transport"], "http-observe");
    assert_eq!(body["infrastructure_observation_ready"], ready);
    assert_eq!(body["infrastructure_observation"]["coverage"], "unverified");
    assert_eq!(body["infrastructure_observation"]["node"]["fresh"], true);
    assert_eq!(
        body["infrastructure_observation"]["node"]["uptime_known"],
        true
    );
    assert_eq!(
        body["infrastructure_observation"]["network"]["fresh"],
        ready
    );
    assert_eq!(body["database"], database);
    assert_eq!(body["outbox"], database);
    assert_eq!(body["ready"], false);
    assert_eq!(body["adapter_versions"], json!([]));
    for forbidden in [
        "pve-test",
        "90101",
        "private-node",
        "private-vm",
        "private-bridge",
        "private-ovs",
        "private-other",
        "private-future-kind",
        "192.0.2.12",
        "synthetic-secret",
        "synthetic-selected-token.json",
        "observer@pve",
        "forbidden-body",
        "completed_at",
    ] {
        assert!(
            !body.to_string().contains(forbidden),
            "health leaked a private fixture field"
        );
    }
}
async fn shutdown(child: &mut ServiceChild) {
    assert_eq!(unsafe { libc::kill(child.0.id() as i32, libc::SIGTERM) }, 0);
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            if let Some(status) = child.0.try_wait().unwrap() {
                assert!(status.success());
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("selected-node shutdown exceeded budget");
}

// Break: missing runtime wiring, coupled loops, stale failure readiness, writes or execution children.
#[tokio::test]
async fn selected_node_service_is_independent_and_never_a_writer() {
    tokio::time::timeout(Duration::from_secs(85), async {
        let database = Database::new().await;
        let before = database.snapshot().await;
        assert_eq!(before.len(), 15);
        for table in ["public.jobs", "rust_controller.operations", "rust_controller.journal_events", "rust_controller.attempts", "rust_controller.worker_leases", "rust_controller.native_dispatches", "rust_controller.native_receipts"] {
            assert_eq!(before.iter().find(|(name, _)| name == table).unwrap().1.as_array().unwrap().len(), 1);
        }
        let http = HttpFixture::new().await;
        let directory = tempfile::tempdir().unwrap();
        let token = token(directory.path());
        let (mut child, address) = start_service(&database.observer_dsn, &http, &token, "http-observe", "pve-test");
        let client = client();
        let fresh = wait_health(&client, address, &mut child, "fresh", None).await;
        assert_health(&fresh, true, true);
        assert_eq!(fresh["infrastructure_observation"]["network"]["counts"], json!({"linux_bridges":1,"ovs_bridges":1,"other_interfaces":1,"active":1,"inactive":1,"activity_unknown":1,"rejected_rows":0}));
        assert_no_service_children(child.0.id()).await;
        let failed = wait_health(&client, address, &mut child, "unauthorized", None).await;
        assert_health(&failed, false, true);
        assert!(failed["infrastructure_observation"]["network"]["counts"].is_null());
        assert!(failed["infrastructure_observation"]["network"]["observed_at"].is_null());
        assert_eq!(failed["infrastructure_observation"]["network"]["last_success"], fresh["infrastructure_observation"]["network"]["last_success"]);
        assert_no_service_children(child.0.id()).await;
        let recovered = wait_health(&client, address, &mut child, "fresh", Some(&fresh["infrastructure_observation"]["network"]["last_success"])).await;
        assert_health(&recovered, true, true);
        assert_no_service_children(child.0.id()).await;
        assert_eq!(database.snapshot().await, before);
        shutdown(&mut child).await;
        assert_eq!(database.snapshot().await, before);
        assert_eq!(http.requests.node.load(Ordering::SeqCst), 3);
        assert_eq!(http.requests.network.load(Ordering::SeqCst), 3);
        assert!((4..=6).contains(&http.requests.cluster.load(Ordering::SeqCst)));
        assert_eq!(http.requests.rejected.load(Ordering::SeqCst), 0);
        let logs = database.container.logs().await.expect("complete SQL capture failed");
        audit_read_only_trace(&logs);
        database.cleanup().await;
    }).await.expect("selected-node proof exceeded whole-test budget");
}

// Socket-free means no Docker socket/database fixture: only owned loopback HTTP and a refused DB port.
#[tokio::test]
async fn selected_node_service_stays_fresh_without_database() {
    tokio::time::timeout(Duration::from_secs(12), async {
        let http = HttpFixture::new().await;
        let directory = tempfile::tempdir().unwrap();
        let token = token(directory.path());
        let (mut child, address) = start_service(
            "postgresql://synthetic@127.0.0.1:1/unavailable",
            &http,
            &token,
            "http-observe",
            "pve-test",
        );
        let body = wait_health(&client(), address, &mut child, "fresh", None).await;
        assert_health(&body, true, false);
        assert_eq!(body["successful_sweeps"], 0);
        assert!(body["reconciler_last_success"].is_null());
        assert_no_service_children(child.0.id()).await;
        shutdown(&mut child).await;
        assert_eq!(http.requests.node.load(Ordering::SeqCst), 1);
        assert_eq!(http.requests.network.load(Ordering::SeqCst), 1);
        assert_eq!(http.requests.cluster.load(Ordering::SeqCst), 1);
        assert_eq!(http.requests.rejected.load(Ordering::SeqCst), 0);
    })
    .await
    .expect("selected-node database-unavailable proof exceeded budget");
}

#[tokio::test]
async fn selected_node_startup_denials_precede_owned_canaries_and_listener() {
    for (transport, node) in [
        ("fake", "pve-test"),
        ("http-observe", "invalid/node"),
        ("http-observe", ""),
    ] {
        let http = HttpFixture::new().await;
        let database = TcpListener::bind("127.0.0.1:0").unwrap();
        database.set_nonblocking(true).unwrap();
        let directory = tempfile::tempdir().unwrap();
        let token = token(directory.path());
        let db = format!(
            "postgresql://canary@{}/unused",
            database.local_addr().unwrap()
        );
        let (mut child, address) = start_service(&db, &http, &token, transport, node);
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                assert!(database.accept().is_err());
                assert!(TcpStream::connect_timeout(&address, Duration::from_millis(10)).is_err());
                assert_eq!(http.requests.cluster.load(Ordering::SeqCst), 0);
                assert_eq!(http.requests.node.load(Ordering::SeqCst), 0);
                assert_eq!(http.requests.network.load(Ordering::SeqCst), 0);
                assert_eq!(http.requests.rejected.load(Ordering::SeqCst), 0);
                if let Some(status) = child.0.try_wait().unwrap() {
                    assert_eq!(status.code(), Some(1));
                    break;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .expect("selected-node denial did not exit promptly");
    }
}
