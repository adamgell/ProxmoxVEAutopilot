#![cfg(target_os = "macos")]
#[path = "../../../proof_support/mod.rs"]
mod local_postgres;
const LOCAL_DATABASE_NAME: &str = "storage_qualification_test";
use local_postgres::Container;
use postgres_store::PgStore;
use sqlx::{PgPool, postgres::PgPoolOptions};
use std::time::{Duration, Instant};

async fn fresh() -> (Container, PgPool, String) {
    assert!(std::env::var_os("PROXMOXVEAUTOPILOT_LINUX_TEST_DB").is_none());
    tokio::time::timeout(local_postgres::SETUP_BOUND, async {
        let (container, dsn) = Container::start().await;
        let pool = tokio::time::timeout(Duration::from_secs(15), async {
            loop {
                if let Ok(pool) = PgPoolOptions::new()
                    .max_connections(3)
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
        .expect("owned_storage_database_timeout");
        PgStore::new(pool.clone())
            .migrate()
            .await
            .expect("owned_storage_migration");
        (container, pool, dsn)
    })
    .await
    .expect("owned_storage_setup_timeout")
}
async fn sql_proof(pool: &PgPool, dsn: &str) {
    let independent = PgPoolOptions::new()
        .max_connections(2)
        .acquire_timeout(Duration::from_secs(1))
        .connect(dsn)
        .await
        .expect("owned_storage_independent_pool");
    sqlx::query("CREATE TABLE rust_controller.fixture_storage_probe (id bigint PRIMARY KEY)")
        .execute(pool)
        .await
        .unwrap();
    let mut tx = pool.begin().await.unwrap();
    sqlx::query("INSERT INTO rust_controller.fixture_storage_probe VALUES (1)")
        .execute(&mut *tx)
        .await
        .unwrap();
    tx.commit().await.unwrap();
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.fixture_storage_probe")
            .fetch_one(&independent)
            .await
            .unwrap();
    assert_eq!(count, 1);
    let mut tx = pool.begin().await.unwrap();
    sqlx::query("INSERT INTO rust_controller.fixture_storage_probe VALUES (2)")
        .execute(&mut *tx)
        .await
        .unwrap();
    tx.rollback().await.unwrap();
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.fixture_storage_probe")
            .fetch_one(&independent)
            .await
            .unwrap();
    assert_eq!(count, 1);
    for setting in [
        "server_version_num",
        "data_directory",
        "shared_buffers",
        "max_connections",
        "max_wal_size",
        "min_wal_size",
        "checkpoint_timeout",
        "fsync",
        "synchronous_commit",
        "full_page_writes",
    ] {
        let value: String = sqlx::query_scalar("SELECT current_setting($1)")
            .bind(setting)
            .fetch_one(pool)
            .await
            .unwrap();
        if matches!(setting, "fsync" | "synchronous_commit" | "full_page_writes") {
            assert_eq!(value, "on");
        }
        if setting == "data_directory" {
            assert_eq!(value, "/var/lib/postgresql/data");
        }
        if setting == "server_version_num" {
            assert!((160000..170000).contains(&value.parse::<u32>().unwrap()));
        }
        eprintln!("owned_storage_setting {setting}={value}");
    }
    let bytes: i64 = sqlx::query_scalar("SELECT pg_database_size(current_database())")
        .fetch_one(pool)
        .await
        .unwrap();
    assert!(bytes > 0);
    eprintln!("owned_storage_database_bytes {bytes}");
    independent.close().await;
}
#[tokio::test]
#[ignore = "explicit owned Docker storage qualification only"]
async fn owned_tmpfs_sql_and_explicit_cleanup() {
    tokio::time::timeout(Duration::from_secs(150), async {
        let (mut first, first_pool, first_dsn) = fresh().await;
        let (mut second, second_pool, second_dsn) = fresh().await;
        let mut a = first.storage_audit().unwrap();
        let mut b = second.storage_audit().unwrap();
        assert_ne!(a.identity().1, b.identity().1);
        a.sample().await.unwrap();
        b.sample().await.unwrap();
        sql_proof(&first_pool, &first_dsn).await;
        sql_proof(&second_pool, &second_dsn).await;
        a.sample().await.unwrap();
        b.sample().await.unwrap();
        first_pool.close().await;
        second_pool.close().await;
        assert_eq!(first.cleanup(), Ok(()));
        a.confirm_removed().await.unwrap();
        assert_eq!(second.cleanup(), Ok(()));
        b.confirm_removed().await.unwrap();
    })
    .await
    .expect("owned_storage_sql_lifecycle_timeout");
}
#[tokio::test(flavor = "current_thread")]
#[ignore = "explicit owned Docker storage qualification only"]
async fn owned_tmpfs_drop_cleanup() {
    tokio::time::timeout(Duration::from_secs(150), async {
        let (container, pool, _dsn) = fresh().await;
        let mut audit = container.storage_audit().unwrap();
        audit.sample().await.unwrap();
        pool.close().await;
        drop(container);
        audit.confirm_removed().await.unwrap();
    })
    .await
    .expect("owned_storage_drop_timeout");
}
#[tokio::test(flavor = "current_thread")]
#[ignore = "explicit owned Docker storage qualification only"]
async fn owned_tmpfs_current_thread_cancellation() {
    tokio::time::timeout(Duration::from_secs(150), async {
        let (container, pool, _dsn) = fresh().await;
        let mut audit = container.storage_audit().unwrap();
        audit.sample().await.unwrap();
        pool.close().await;
        let start = Instant::now();
        assert!(
            tokio::time::timeout(Duration::from_millis(40), async move {
                let _owned = container;
                std::future::pending::<()>().await;
            })
            .await
            .is_err()
        );
        assert!(start.elapsed() < Duration::from_millis(3600));
        audit.confirm_removed().await.unwrap();
    })
    .await
    .expect("owned_storage_cancellation_timeout");
}
