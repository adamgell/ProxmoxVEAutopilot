use super::ServiceChild;
use axum::{
    Json, Router,
    extract::State,
    http::{HeaderMap, StatusCode, Uri},
    routing::get,
};
use postgres_store::PgStore;
use serde_json::{Value, json};
use sqlx::{PgPool, postgres::PgPoolOptions};
use std::{
    os::unix::fs::PermissionsExt,
    process::{Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::task::JoinHandle;

#[path = "infrastructure_service.rs"]
mod infrastructure_service;

const LOCAL_DATABASE_NAME: &str = "authenticated_observation_proof";
#[path = "../../../../proof_support/mod.rs"]
mod local_postgres;

struct Database {
    container: local_postgres::Container,
    admin: PgPool,
    observer_dsn: String,
}
impl Database {
    async fn new() -> Self {
        tokio::time::timeout(local_postgres::SETUP_BOUND, async {
            let (container, dsn) = local_postgres::Container::start().await;
            let admin = tokio::time::timeout(Duration::from_secs(15), async { loop {
                if let Ok(pool) = PgPoolOptions::new().max_connections(3)
                    .acquire_timeout(Duration::from_secs(1)).connect(&dsn).await { break pool; }
                tokio::time::sleep(Duration::from_millis(50)).await;
            } }).await.expect("owned PostgreSQL readiness exceeded budget");
            PgStore::new(admin.clone()).migrate().await.unwrap();
            sqlx::raw_sql(r#"
                CREATE TABLE jobs (id text PRIMARY KEY, job_type text, playbook text,
                    cmd_json jsonb, args_json jsonb, status text, created_at timestamptz DEFAULT clock_timestamp());
                INSERT INTO jobs VALUES ('20260905-deadbeef', 'test_long_sleep',
                    '/app/playbooks/_test_long_sleep.yml',
                    '["ansible-playbook","/app/playbooks/_test_long_sleep.yml","-e","duration=5"]',
                    '{"duration":"5"}', 'pending', clock_timestamp());
                INSERT INTO rust_controller.orchestration_authority(singleton_key,executor_kind,generation,change_reference)
                    VALUES(1,'rust',1,'owned-observation-proof');
                CREATE ROLE observer_login LOGIN PASSWORD 'synthetic-observer';
                GRANT SELECT ON jobs TO observer_login;
                GRANT USAGE ON SCHEMA rust_controller TO observer_login;
                GRANT SELECT ON ALL TABLES IN SCHEMA rust_controller TO observer_login;
                DO $$ DECLARE f text; BEGIN
                    FOR f IN SELECT oid::regprocedure::text FROM pg_proc WHERE proname LIKE '%advisory%'
                    LOOP EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC, observer_login', f); END LOOP;
                END $$;
                INSERT INTO rust_controller.operations(operation_id,workflow_kind,run_id,operation_key,contract_version,state,revision)
                    VALUES('00000000-0000-7000-8000-000000000001','native_pve_vm_boot','00000000-0000-7000-8000-000000000002','sentinel',1,'waiting',1);
                INSERT INTO rust_controller.attempts(attempt_id,operation_id,attempt_number,state)
                    VALUES('00000000-0000-7000-8000-000000000003','00000000-0000-7000-8000-000000000001',1,'waiting');
                INSERT INTO rust_controller.journal_events(event_id,operation_id,attempt_id,aggregate_revision,semantic_key,payload_digest,event_kind,payload,observed_at)
                    VALUES('00000000-0000-7000-8000-000000000004','00000000-0000-7000-8000-000000000001','00000000-0000-7000-8000-000000000003',1,'sentinel',repeat('a',64),'evidence_recorded','{}',clock_timestamp());
                INSERT INTO rust_controller.worker_leases(operation_id,attempt_id,executor_kind,generation,worker_id,lease_token)
                    VALUES('00000000-0000-7000-8000-000000000001','00000000-0000-7000-8000-000000000003','rust',1,'sentinel','00000000-0000-7000-8000-000000000006');
                INSERT INTO rust_controller.native_operation_plans(operation_id,step,payload_digest,plan)
                    VALUES('00000000-0000-7000-8000-000000000001','clone',repeat('a',64),'{}');
                INSERT INTO rust_controller.native_dispatches(operation_id,attempt_id,plan_digest,generation,dispatch_revision,request_digest,request_marker,preflight_event_id)
                    VALUES('00000000-0000-7000-8000-000000000001','00000000-0000-7000-8000-000000000003',repeat('a',64),1,1,repeat('b',64),'00000000-0000-7000-8000-000000000005','00000000-0000-7000-8000-000000000004');
                INSERT INTO rust_controller.native_receipts(operation_id,receipt_kind)
                    VALUES('00000000-0000-7000-8000-000000000001','synchronous');
                ALTER ROLE observer_login SET log_statement = 'all';
            "#).execute(&admin).await.unwrap();
            sqlx::query("ALTER SYSTEM SET log_line_prefix = '%m [%p] %u %d '").execute(&admin).await.unwrap();
            sqlx::query("SELECT pg_reload_conf()").execute(&admin).await.unwrap();
            let observer_dsn = dsn.replace("postgres:postgres@", "observer_login:synthetic-observer@");
            Self { container, admin, observer_dsn }
        }).await.expect("owned observation setup exceeded budget")
    }
    async fn snapshot(&self) -> Vec<(String, Value)> {
        let mut result = vec![];
        for table in [
            "public.jobs",
            "rust_controller.operations",
            "rust_controller.commands",
            "rust_controller.journal_events",
            "rust_controller.attempts",
            "rust_controller.worker_leases",
            "rust_controller.outbox",
            "rust_controller.operation_projection",
            "rust_controller.native_operation_plans",
            "rust_controller.native_vm_reservations",
            "rust_controller.native_dispatches",
            "rust_controller.native_receipts",
            "rust_controller.native_decisions",
            "rust_controller.native_run_cancellations",
            "rust_controller.orchestration_authority",
        ] {
            let value = sqlx::query_scalar(&format!("SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb) FROM {table} t"))
                .fetch_one(&self.admin).await.unwrap();
            result.push((table.into(), value));
        }
        result
    }
    async fn cleanup(mut self) {
        self.admin.close().await;
        assert!(
            self.container.cleanup().is_ok(),
            "exact owned container cleanup unconfirmed"
        );
    }
}

#[derive(Clone, Default)]
struct HttpState {
    requests: Arc<AtomicUsize>,
    rejected: Arc<AtomicUsize>,
    response: Arc<AtomicUsize>,
}
struct HttpFixture {
    address: std::net::SocketAddr,
    state: HttpState,
    task: JoinHandle<()>,
}
impl Drop for HttpFixture {
    fn drop(&mut self) {
        self.task.abort();
    }
}
impl HttpFixture {
    async fn new() -> Self {
        async fn inventory(
            State(state): State<HttpState>,
            headers: HeaderMap,
            uri: Uri,
        ) -> (StatusCode, Json<Value>) {
            state.requests.fetch_add(1, Ordering::SeqCst);
            if uri.query() != Some("type=vm")
                || headers.get("authorization").and_then(|v| v.to_str().ok())
                    != Some("PVEAPIToken=observer@pve!fixture=synthetic-secret")
            {
                state.rejected.fetch_add(1, Ordering::SeqCst);
                return (StatusCode::BAD_REQUEST, Json(json!({})));
            }
            if state.response.load(Ordering::SeqCst) == 1 {
                return (
                    StatusCode::UNAUTHORIZED,
                    Json(json!({"secret":"forbidden-http-body"})),
                );
            }
            (
                StatusCode::OK,
                Json(json!({"data":[
                    {"vmid":90101,"node":"private-node","type":"qemu","name":"private-vm-name","status":"running"},
                    {"vmid":90102,"node":"private-node","type":"lxc"},
                    {"vmid":90103,"node":"private-node","type":"future"}
                ]})),
            )
        }
        async fn rejected(State(state): State<HttpState>) -> StatusCode {
            state.rejected.fetch_add(1, Ordering::SeqCst);
            StatusCode::METHOD_NOT_ALLOWED
        }
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let state = HttpState::default();
        let app = Router::new()
            .route(
                "/api2/json/cluster/resources",
                get(inventory).fallback(rejected),
            )
            .fallback(rejected)
            .with_state(state.clone());
        let task = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        Self {
            address,
            state,
            task,
        }
    }
}

fn start_service(
    db: &str,
    http: &HttpFixture,
    token: &std::path::Path,
) -> (ServiceChild, std::net::SocketAddr) {
    let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let address = socket.local_addr().unwrap();
    drop(socket);
    let child = Command::new(env!("CARGO_BIN_EXE_controller-service"))
        .env_clear()
        .env("RUST_CONTROLLER_MODE", "observe")
        .env("RUST_CONTROLLER_DATABASE_URL", db)
        .env(
            "RUST_CONTROLLER_PVE_BASE_URL",
            format!("http://{}", http.address),
        )
        .env("RUST_CONTROLLER_PVE_TRANSPORT", "http-observe")
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
async fn wait_health(
    client: &reqwest::Client,
    address: std::net::SocketAddr,
    child: &mut ServiceChild,
    expected: &str,
) -> Value {
    tokio::time::timeout(Duration::from_secs(8), async {
        loop {
            assert!(
                child.0.try_wait().unwrap().is_none(),
                "authenticated observer exited before health became available"
            );
            if let Ok(response) = client.get(format!("http://{address}/readyz")).send().await {
                assert_eq!(response.status().as_u16(), 503);
                let body: Value = response.json().await.unwrap();
                if body["pve_observation"]["status"] == expected {
                    break body;
                }
            }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
    })
    .await
    .expect("observer health did not reach expected status")
}

// Break: failure to construct GET capability; observation granting execution readiness;
// coupling HTTP collection to DB failure; any execution-state write or child process.
#[tokio::test]
async fn authenticated_service_is_visible_but_never_execution_ready_or_a_writer() {
    tokio::time::timeout(Duration::from_secs(55), async {
        let database = Database::new().await;
        let http = HttpFixture::new().await;
        let directory = tempfile::tempdir().unwrap();
        let token = directory.path().join("synthetic-token.json");
        std::fs::write(
            &token,
            r#"{"token_id":"observer@pve!fixture","secret":"synthetic-secret"}"#,
        )
        .unwrap();
        std::fs::set_permissions(&token, std::fs::Permissions::from_mode(0o600)).unwrap();
        let before = database.snapshot().await;
        for table in [
            "public.jobs",
            "rust_controller.operations",
            "rust_controller.journal_events",
            "rust_controller.attempts",
            "rust_controller.worker_leases",
            "rust_controller.native_dispatches",
            "rust_controller.native_receipts",
        ] {
            assert_eq!(
                before
                    .iter()
                    .find(|(name, _)| name == table)
                    .unwrap()
                    .1
                    .as_array()
                    .unwrap()
                    .len(),
                1
            );
        }
        let (mut child, address) = start_service(&database.observer_dsn, &http, &token);
        let client = reqwest::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(3))
            .build()
            .unwrap();
        let body = wait_health(&client, address, &mut child, "fresh").await;
        assert_eq!(body["pve_transport"], "http-observe");
        assert_eq!(body["git_sha"], env!("CONTROLLER_GIT_SHA"));
        assert_eq!(body["pve_evidence"], "visibility_only_coverage_unverified");
        assert_eq!(body["observation_ready"], true);
        assert_eq!(body["ready"], false);
        assert_eq!(body["database"], true);
        assert_eq!(body["outbox"], true);
        assert_eq!(body["adapter_versions"], json!([]));
        assert_eq!(body["pve_observation"]["coverage"], "unverified");
        assert_eq!(body["pve_observation"]["fresh"], true);
        assert_eq!(
            body["pve_observation"]["counts"],
            json!({"visible_qemu":1,"visible_lxc":1,"visible_unsupported":1,"rejected_rows":0})
        );
        assert!(body["pve_observation"]["observed_at"].is_string());
        assert_no_service_children(child.0.id()).await;
        for forbidden in [
            "90101",
            "90102",
            "90103",
            "private-node",
            "private-vm-name",
            "synthetic-secret",
            "synthetic-token.json",
            "observer@pve",
            "forbidden-http-body",
        ] {
            assert!(!body.to_string().contains(forbidden));
        }
        http.state.response.store(1, Ordering::SeqCst);
        let failed = wait_health(&client, address, &mut child, "unauthorized").await;
        assert_eq!(failed["observation_ready"], false);
        assert_eq!(failed["database"], true);
        assert_eq!(failed["pve_observation"]["counts"], Value::Null);
        assert_eq!(failed["pve_observation"]["observed_at"], Value::Null);
        assert_eq!(
            failed["pve_observation"]["last_success"],
            body["pve_observation"]["last_success"]
        );
        assert_no_service_children(child.0.id()).await;
        http.state.response.store(0, Ordering::SeqCst);
        let recovered = wait_health(&client, address, &mut child, "fresh").await;
        assert_eq!(recovered["observation_ready"], true);
        assert_no_service_children(child.0.id()).await;
        assert_eq!(database.snapshot().await, before);
        assert_eq!(http.state.requests.load(Ordering::SeqCst), 3);
        assert_eq!(http.state.rejected.load(Ordering::SeqCst), 0);
        // SIGTERM must stop the actual service within a finite bound.
        assert_eq!(unsafe { libc::kill(child.0.id() as i32, libc::SIGTERM) }, 0);
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(status) = child.0.try_wait().unwrap() {
                assert!(status.success());
                break;
            }
            assert!(
                Instant::now() < deadline,
                "observer shutdown exceeded budget"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert_eq!(database.snapshot().await, before);
        let logs = database
            .container
            .logs()
            .await
            .expect("complete owned SQL log capture failed");
        audit_read_only_trace(&logs);
        database.cleanup().await;
    })
    .await
    .expect("owned observation proof exceeded whole-test budget");
}

async fn assert_no_service_children(pid: u32) {
    let mut command = tokio::process::Command::new("/bin/ps");
    command.args(["-axo", "ppid="]).kill_on_drop(true);
    let output = tokio::time::timeout(Duration::from_secs(1), command.output())
        .await
        .unwrap()
        .unwrap();
    assert!(output.status.success());
    assert!(
        !String::from_utf8(output.stdout)
            .unwrap()
            .lines()
            .any(|line| line.trim() == pid.to_string()),
        "sampled service process tree has a child"
    );
}

fn audit_read_only_trace(logs: &str) {
    // Complete container log capture is capped and fails on overflow; no tailing.
    // Accept only exact SQL statements emitted by the observer and health reader.
    const VERIFY: &str = "SELECT current_setting('transaction_read_only') = 'on' AND has_table_privilege(current_user, 'jobs', 'SELECT') AND NOT has_table_privilege(current_user, 'jobs', 'INSERT') AND NOT has_table_privilege(current_user, 'jobs', 'UPDATE') AND NOT has_table_privilege(current_user, 'jobs', 'DELETE') AND NOT has_table_privilege(current_user, 'jobs', 'TRUNCATE') AND NOT EXISTS (SELECT 1 FROM pg_user WHERE usename = current_user AND usesuper)";
    const JOB: &str = "SELECT json_build_object('id', id, 'job_type', job_type, 'playbook', playbook, 'cmd', cmd_json, 'args', args_json, 'status', status)::text FROM jobs WHERE job_type = $1 AND status = 'pending' ORDER BY created_at ASC, id ASC LIMIT 1";
    const HEALTH: &str = "SELECT clock_timestamp() AS observed_at, (SELECT executor_kind FROM rust_controller.orchestration_authority WHERE singleton_key=1) AS executor, (SELECT generation FROM rust_controller.orchestration_authority WHERE singleton_key=1) AS generation, (SELECT count(*) FROM rust_controller.worker_leases WHERE lease_expires_at > clock_timestamp()) AS active_leases, (SELECT count(*) FROM rust_controller.worker_leases WHERE lease_expires_at <= clock_timestamp()) AS expired_leases, (SELECT floor(extract(epoch FROM clock_timestamp()-min(created_at)))::bigint FROM rust_controller.operations WHERE state='pending') AS pending_age, (SELECT count(*) FROM rust_controller.outbox WHERE delivered_at IS NULL) AS outbox_pending, (SELECT floor(extract(epoch FROM clock_timestamp()-min(created_at)))::bigint FROM rust_controller.outbox WHERE delivered_at IS NULL) AS outbox_age, (SELECT count(*) FROM rust_controller.operations WHERE state='blocked') AS blocked, (SELECT count(*) FROM rust_controller.operations WHERE state='unknown') AS unknown, (SELECT count(*) FROM rust_controller.operations WHERE state='conflicted') AS conflicted";
    let mut records: Vec<String> = vec![];
    let mut capturing = false;
    for line in logs.lines() {
        if line.as_bytes().first().is_some_and(u8::is_ascii_digit) {
            capturing = line.contains("] observer_login authenticated_observation_proof ");
            if capturing {
                records.push(
                    line.split_once(" authenticated_observation_proof ")
                        .unwrap()
                        .1
                        .to_owned(),
                );
            }
        } else if capturing {
            records.last_mut().unwrap().push_str(&format!(" {line}"));
        }
    }
    let mut begins = 0;
    let mut rollbacks = 0;
    let mut jobs = 0;
    let mut verifies = 0;
    let mut health = 0;
    for record in records {
        let record = record.split_whitespace().collect::<Vec<_>>().join(" ");
        if record == "DETAIL: parameters: $1 = 'test_long_sleep'" {
            continue;
        }
        let sql = if let Some(sql) = record.strip_prefix("LOG: statement: ") {
            sql
        } else if record.starts_with("LOG: execute ") {
            record
                .split_once(": ")
                .unwrap()
                .1
                .split_once(": ")
                .unwrap()
                .1
        } else {
            panic!("unexpected observer SQL log record category");
        };
        match sql {
            "BEGIN TRANSACTION READ ONLY" => begins += 1,
            "ROLLBACK" => rollbacks += 1,
            "SELECT 1" => {}
            VERIFY => verifies += 1,
            JOB => jobs += 1,
            HEALTH => health += 1,
            _ => panic!("observer SQL trace contains a non-allowlisted statement"),
        }
    }
    assert!(begins >= 3 && jobs >= 3 && health >= 3);
    assert_eq!(begins, rollbacks);
    assert_eq!(begins, verifies);
    assert_eq!(begins, jobs);
}

#[tokio::test]
async fn http_observation_remains_fresh_when_database_is_unavailable() {
    tokio::time::timeout(Duration::from_secs(12), async {
        let http = HttpFixture::new().await;
        let directory = tempfile::tempdir().unwrap();
        let token = directory.path().join("synthetic-token.json");
        std::fs::write(
            &token,
            r#"{"token_id":"observer@pve!fixture","secret":"synthetic-secret"}"#,
        )
        .unwrap();
        std::fs::set_permissions(&token, std::fs::Permissions::from_mode(0o600)).unwrap();
        let (mut child, address) = start_service(
            "postgresql://synthetic@127.0.0.1:1/unavailable",
            &http,
            &token,
        );
        let client = reqwest::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(3))
            .build()
            .unwrap();
        let body = wait_health(&client, address, &mut child, "fresh").await;
        assert_eq!(body["observation_ready"], true);
        assert_eq!(body["database"], false);
        assert_eq!(body["outbox"], false);
        assert_eq!(body["ready"], false);
        assert_eq!(body["successful_sweeps"], 0);
        assert!(body["reconciler_last_success"].is_null());
        assert_no_service_children(child.0.id()).await;
        assert_eq!(http.state.requests.load(Ordering::SeqCst), 1);
        assert_eq!(http.state.rejected.load(Ordering::SeqCst), 0);
    })
    .await
    .expect("loopback service boundary exceeded budget");
}
