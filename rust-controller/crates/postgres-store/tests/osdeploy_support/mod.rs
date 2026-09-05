#[path = "../../../../proof_support/mod.rs"]
mod local_postgres;
const LOCAL_DATABASE_NAME: &str = "osdeploy_registration_test";
use osdeploy_adapter::{OsDeployPlanV1, restore_osdeploy_plan_v1};
use postgres_store::{ExecutorKind, PgStore, Scheduler};
use sqlx::{PgPool, postgres::PgPoolOptions};
use std::time::Duration;
pub const SHA: &str = "47035f6731b8a27dceabbe9fb2ff63aa952204dfa3b6420d94a9a35c1fe3e5a9";
pub fn plan() -> OsDeployPlanV1 {
    restore_osdeploy_plan_v1(
        include_str!("../../../osdeploy-adapter/tests/fixtures/plan-v1.json"),
        SHA,
    )
    .unwrap()
}
pub fn altered(change: impl FnOnce(&mut serde_json::Value)) -> OsDeployPlanV1 {
    let mut value = serde_json::to_value(plan()).unwrap();
    change(&mut value);
    let hash = event_journal::payload_digest(&value).unwrap();
    restore_osdeploy_plan_v1(&serde_json::to_string(&value).unwrap(), &hash).unwrap()
}
pub struct Fixture {
    pub pool: PgPool,
    pub store: PgStore,
    pub other: PgStore,
    _container: local_postgres::Container,
}
impl Fixture {
    pub async fn new() -> Self {
        tokio::time::timeout(local_postgres::SETUP_BOUND, Self::setup())
            .await
            .expect("local_fixture_setup_timeout")
    }
    async fn setup() -> Self {
        let (container, dsn) = local_postgres::Container::start().await;
        let pool = tokio::time::timeout(Duration::from_secs(15), async {
            loop {
                if let Ok(pool) = PgPoolOptions::new()
                    .max_connections(6)
                    .acquire_timeout(Duration::from_secs(1))
                    .connect(&dsn)
                    .await
                {
                    break pool;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        })
        .await
        .expect("local_database_timeout");
        let other = PgStore::new(
            PgPoolOptions::new()
                .max_connections(3)
                .acquire_timeout(Duration::from_secs(1))
                .connect(&dsn)
                .await
                .unwrap(),
        );
        let store = PgStore::new(pool.clone());
        store.migrate().await.unwrap();
        sqlx::query("INSERT INTO rust_controller.orchestration_authority(singleton_key,executor_kind,generation,change_reference) VALUES(1,'rust',1,'osdeploy-test')").execute(&pool).await.unwrap();
        Self {
            pool,
            store,
            other,
            _container: container,
        }
    }
    pub fn scheduler(&self) -> Scheduler {
        Scheduler::new(self.store.clone(), ExecutorKind::Rust, 1, "osdeploy-worker").unwrap()
    }
    pub fn other_scheduler(&self) -> Scheduler {
        Scheduler::new(
            self.other.clone(),
            ExecutorKind::Rust,
            1,
            "osdeploy-other-worker",
        )
        .unwrap()
    }
    pub async fn snapshot(&self) -> serde_json::Value {
        let mut values = serde_json::Map::new();
        for table in [
            "operations",
            "commands",
            "operation_projection",
            "native_vm_reservations",
            "attempts",
            "worker_leases",
            "journal_events",
            "outbox",
            "orchestration_authority",
            "osdeploy_runs",
            "osdeploy_operation_plans",
            "osdeploy_agent_reservations",
            "native_operation_plans",
            "native_dispatches",
            "native_receipts",
            "native_decisions",
            "native_run_cancellations",
        ] {
            let value: serde_json::Value = sqlx::query_scalar(&format!("SELECT COALESCE(jsonb_agg(row_to_json(t)::jsonb ORDER BY row_to_json(t)::text),'[]'::jsonb) FROM rust_controller.{table} t")).fetch_one(&self.pool).await.unwrap();
            values.insert(table.to_owned(), value);
        }
        serde_json::Value::Object(values)
    }
    pub async fn assert_fresh_counts(&self) {
        let snapshot = self.snapshot().await;
        for (table, count) in [
            ("operations", 16),
            ("commands", 16),
            ("operation_projection", 16),
            ("native_vm_reservations", 1),
            ("osdeploy_runs", 1),
            ("osdeploy_operation_plans", 16),
            ("osdeploy_agent_reservations", 1),
            ("attempts", 0),
            ("worker_leases", 0),
            ("journal_events", 0),
            ("outbox", 0),
            ("orchestration_authority", 1),
            ("native_operation_plans", 0),
            ("native_dispatches", 0),
        ] {
            assert_eq!(snapshot[table].as_array().unwrap().len(), count, "{table}");
        }
    }
    // Targets this fixture's unique database; bootstrap privileges are cluster-wide.
    // This is a test corruption helper, not a library repair API.
    pub async fn corrupt_immutable(&self, table: &str, query: &str) {
        let trigger = match table {
            "osdeploy_runs" | "osdeploy_operation_plans" | "osdeploy_agent_reservations" => {
                "osdeploy_no_mutation"
            }
            "native_vm_reservations" => "native_no_mutation",
            _ => panic!("unowned corruption table"),
        };
        let mut tx = self.pool.begin().await.unwrap();
        sqlx::query(&format!(
            "ALTER TABLE rust_controller.{table} DISABLE TRIGGER {trigger}"
        ))
        .execute(&mut *tx)
        .await
        .unwrap();
        sqlx::query(query).execute(&mut *tx).await.unwrap();
        sqlx::query(&format!(
            "ALTER TABLE rust_controller.{table} ENABLE TRIGGER {trigger}"
        ))
        .execute(&mut *tx)
        .await
        .unwrap();
        tx.commit().await.unwrap();
    }
    pub async fn wait_run_lock_waiters(&self, count: i64) {
        tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                let waiting: i64 = sqlx::query_scalar("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted AND database=(SELECT oid FROM pg_database WHERE datname=current_database())").fetch_one(&self.pool).await.unwrap();
                if waiting == count { break; }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        }).await.expect("owned run-lock waiter missing");
    }
}
