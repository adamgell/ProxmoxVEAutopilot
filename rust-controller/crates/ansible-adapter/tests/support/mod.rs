use api_compat::{JobEnvelope, NormalizedPlan, normalize_job};
use controller_domain::{CommandEnvelope, OperationId, RunId, SemanticOperationKey, WorkflowKind};
use postgres_store::PgStore;
use scheduler::{ExecutorKind, LeaseGrant, Scheduler};
use sqlx::{PgPool, postgres::PgPoolOptions};
use std::{process::Command, time::Duration};

pub struct Fixture {
    pub pool: PgPool,
    pub scheduler: Scheduler,
    pub store: PgStore,
    container: String,
}
impl Fixture {
    pub async fn new() -> Self {
        if let Ok(dsn) = std::env::var("RUST_CONTROLLER_TEST_DATABASE_URL") {
            return Self::isolated_database(&dsn).await;
        }
        let output = Command::new("docker")
            .args([
                "run",
                "--pull=never",
                "-d",
                "--env",
                "POSTGRES_PASSWORD=postgres",
                "--env",
                "POSTGRES_DB=rust_controller_test",
                "--publish",
                "127.0.0.1::5432",
                "postgres:16-alpine",
            ])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "local cached PostgreSQL must start"
        );
        let container = String::from_utf8(output.stdout).unwrap().trim().to_owned();
        let output = Command::new("docker")
            .args([
                "inspect",
                "--format",
                "{{(index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostPort}}",
                &container,
            ])
            .output()
            .unwrap();
        assert!(output.status.success());
        let port = String::from_utf8(output.stdout).unwrap();
        let dsn = format!(
            "postgresql://postgres:postgres@127.0.0.1:{}/rust_controller_test",
            port.trim()
        );
        let pool = tokio::time::timeout(Duration::from_secs(20), async {
            loop {
                if let Ok(pool) = PgPoolOptions::new().max_connections(5).connect(&dsn).await {
                    break pool;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        })
        .await
        .expect("local PostgreSQL readiness");
        let store = PgStore::new(pool.clone());
        store.migrate().await.unwrap();
        sqlx::query("INSERT INTO rust_controller.orchestration_authority (singleton_key, executor_kind, generation, change_reference) VALUES (1, 'rust', 1, 'local-adapter-test')").execute(&pool).await.unwrap();
        let scheduler =
            Scheduler::new(store.clone(), ExecutorKind::Rust, 1, "adapter-test").unwrap();
        Self {
            pool,
            scheduler,
            store,
            container,
        }
    }
    // Linux Compose proof uses one disposable PostgreSQL server, with a fresh
    // database per test. Literal loopback remains mandatory before connecting.
    async fn isolated_database(dsn: &str) -> Self {
        use std::{
            str::FromStr,
            sync::atomic::{AtomicU64, Ordering},
        };
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let options = sqlx::postgres::PgConnectOptions::from_str(dsn).unwrap();
        assert!(
            options
                .get_host()
                .parse::<std::net::IpAddr>()
                .unwrap()
                .is_loopback()
        );
        let admin = PgPoolOptions::new()
            .connect_with(options.clone())
            .await
            .unwrap();
        let database = format!(
            "adapter_proof_{}_{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        );
        sqlx::query(&format!("CREATE DATABASE {database}"))
            .execute(&admin)
            .await
            .unwrap();
        admin.close().await;
        let pool = PgPoolOptions::new()
            .max_connections(5)
            .connect_with(options.database(&database))
            .await
            .unwrap();
        let store = PgStore::new(pool.clone());
        store.migrate().await.unwrap();
        sqlx::query("INSERT INTO rust_controller.orchestration_authority (singleton_key, executor_kind, generation, change_reference) VALUES (1, 'rust', 1, 'local-adapter-test')").execute(&pool).await.unwrap();
        let scheduler =
            Scheduler::new(store.clone(), ExecutorKind::Rust, 1, "adapter-test").unwrap();
        Self {
            pool,
            store,
            scheduler,
            container: String::new(),
        }
    }
    pub async fn grant(&self, plan: &NormalizedPlan) -> LeaseGrant {
        let command = CommandEnvelope::new(
            format!("test:{}", plan.fingerprint().unwrap().as_hex()),
            SemanticOperationKey::new(
                WorkflowKind::SyntheticLongSleep,
                RunId::new(),
                "local-synthetic",
                1,
            )
            .unwrap(),
            plan.fingerprint().unwrap().as_hex(),
        )
        .unwrap();
        self.store
            .append_command(OperationId::new(), &command)
            .await
            .unwrap();
        self.scheduler
            .claim_next(WorkflowKind::SyntheticLongSleep, 4)
            .await
            .unwrap()
            .unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if self.container.is_empty() {
            return;
        }
        let _ = Command::new("docker")
            .args(["rm", "-f", &self.container])
            .output();
    }
}

pub fn plan(duration: u8) -> NormalizedPlan {
    normalize_job(&JobEnvelope::from_json_str(&serde_json::json!({"id":"20260904-1234abcd", "job_type":"test_long_sleep", "playbook":"/app/playbooks/_test_long_sleep.yml", "status":"pending", "cmd":["ansible-playbook","/app/playbooks/_test_long_sleep.yml","-e",format!("duration={duration}")],"args":{"duration":duration.to_string()}}).to_string()).unwrap()).unwrap()
}
